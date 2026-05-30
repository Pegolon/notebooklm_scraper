#!/usr/bin/env python3
"""
Convert M4A audio files in OUTPUT_DIR to MP3, using the MD5 of the original
M4A bytes as the filename so the conversion is fully idempotent.

For every <name>.m4a in OUTPUT_DIR, compute md5(file-bytes); if a sibling
<hash>.mp3 already exists, skip (already converted in a previous run);
otherwise transcode via ffmpeg into <hash>.mp3 alongside the rest of the
episodes. The original M4A is left untouched — delete it manually once
you're happy with the conversion.

This is the first pass in the manual-audio pipeline; once the MP3 lands in
the folder, the rest of the chain (transcribe → summarise → coverart)
picks it up by its normal scan rules:

  convert (m4a → <hash>.mp3) → transcribe → summarise → coverart

  uv run convert.py                  # convert all M4As not yet converted
  uv run convert.py --file foo.m4a   # convert one specific file
  uv run convert.py --force          # re-encode even if <hash>.mp3 exists

Requires ffmpeg on PATH. Install via `brew install ffmpeg` on macOS.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

SCRIPT_DIR = Path(__file__).resolve().parent
load_dotenv(SCRIPT_DIR / ".env")


def _clean_path_value(raw: str) -> str:
    s = raw.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        s = s[1:-1]
    return s.replace("\\ ", " ").replace('\\"', '"').replace("\\'", "'")


OUTPUT_DIR = (
    Path(_clean_path_value(os.environ["OUTPUT_DIR"])).expanduser()
    if os.environ.get("OUTPUT_DIR")
    else None
)

# libmp3lame VBR quality. q:a 2 ≈ 190 kbps avg — well above transparent for
# voice content while staying compact. Override via .env if needed.
MP3_QUALITY = os.environ.get("MP3_QUALITY", "2").strip()

# Read buffer for hashing. 1 MiB is a good fit for spinning Drive-synced files.
_HASH_CHUNK = 1024 * 1024

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("convert")


def _hash_file(path: Path) -> str:
    """Stream-MD5 a file's bytes. Stable across runs → idempotent conversion."""
    h = hashlib.md5()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(_HASH_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def transcode_to_mp3(src: Path, dst: Path, *, quality: Optional[str] = None) -> Path:
    """Transcode `src` (any audio container ffmpeg can read) to MP3 at `dst`.

    Writes via a hidden `<dst>.partial` temp file and atomically renames on
    success — a crash mid-encode cannot leave a half-written file at `dst`.
    Returns `dst`. Raises RuntimeError on any ffmpeg failure.

    Shared between the standalone convert pass (which names `dst` by the m4a
    bytes hash) and the scraper (which preserves the description-based hash
    so the JSON sidecar's `audio_file` stays valid).
    """
    if shutil.which("ffmpeg") is None:
        raise RuntimeError(
            "ffmpeg not found on PATH. Install it (e.g. `brew install ffmpeg`) "
            "and re-run."
        )

    q = (quality or MP3_QUALITY).strip()
    tmp = dst.with_name(f".{dst.name}.partial")
    cmd = [
        "ffmpeg",
        "-y",                       # overwrite tmp if it somehow exists
        "-loglevel", "error",       # quiet output; we only care about errors
        "-i", str(src),
        "-vn",                      # drop any embedded artwork/video stream
        "-c:a", "libmp3lame",
        "-q:a", q,
        "-f", "mp3",                # force muxer (tmp filename ends in .partial,
                                    # which would otherwise confuse ffmpeg's
                                    # extension-based format detection)
        str(tmp),
    ]
    log.info("Transcoding %s → %s (libmp3lame VBR q=%s)...", src.name, dst.name, q)
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        tmp.unlink(missing_ok=True)
        # ffmpeg writes its real diagnostic to stderr.
        raise RuntimeError(
            f"ffmpeg failed on {src.name}: {(e.stderr or '').strip() or e}"
        ) from e
    except FileNotFoundError as e:  # noqa: BLE001 — race: PATH lost ffmpeg between which() and run()
        tmp.unlink(missing_ok=True)
        raise RuntimeError("ffmpeg disappeared from PATH mid-run.") from e

    tmp.replace(dst)
    return dst


def convert_one(m4a: Path, *, output_dir: Path, force: bool = False) -> Optional[Path]:
    """Convert one M4A to <hash>.mp3 in output_dir, where <hash> is the MD5
    of the M4A bytes (so re-runs on the same input skip the work). Returns
    the MP3 path, or None if the output already existed (and force=False).
    Raises on ffmpeg failure."""
    size_mb = m4a.stat().st_size / 1024 / 1024
    log.info("Hashing %s (%.1f MB)...", m4a.name, size_mb)
    digest = _hash_file(m4a)
    mp3 = output_dir / f"{digest}.mp3"

    if mp3.exists() and not force:
        log.info("%s already converted (%s); skipping.", m4a.name, mp3.name)
        return None

    transcode_to_mp3(m4a, mp3)
    out_size = mp3.stat().st_size
    log.info("Wrote %s (%.1f KB).", mp3.name, out_size / 1024)
    return mp3


def convert_missing(output_dir: Path) -> tuple[int, int]:
    """Find every *.m4a in output_dir and convert it to <md5>.mp3 unless that
    output file already exists. Returns (successes, failures). Files that
    were already up-to-date count as neither success nor failure."""
    # Case-insensitive glob: pick up .m4a and .M4A both.
    m4as = sorted(
        p for p in output_dir.iterdir()
        if p.is_file() and p.suffix.lower() == ".m4a"
    )
    if not m4as:
        log.info("Conversion pass: no .m4a files in %s — nothing to do.", output_dir)
        return 0, 0

    log.info("Conversion pass: %d .m4a file(s) found.", len(m4as))
    successes, failures = 0, 0
    for m4a in m4as:
        try:
            convert_one(m4a, output_dir=output_dir)
            successes += 1
        except Exception as e:  # noqa: BLE001
            failures += 1
            log.error("Failed to convert %s: %s", m4a.name, e)
            # Keep going so a single bad file doesn't block the rest.
    log.info("Conversion pass done. %d succeeded, %d failed.", successes, failures)
    return successes, failures


def _require_config() -> None:
    if OUTPUT_DIR is None:
        log.error("OUTPUT_DIR is not set (configure it in .env).")
        sys.exit(2)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--file", type=str, default=None,
        help="Convert one specific M4A file (path) instead of scanning OUTPUT_DIR.",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Re-encode even if <hash>.mp3 already exists.",
    )
    args = parser.parse_args()

    _require_config()
    assert OUTPUT_DIR is not None

    if args.file:
        m4a = Path(args.file).expanduser().resolve()
        if not m4a.exists() or m4a.suffix.lower() != ".m4a":
            log.error("Not an existing .m4a file: %s", m4a)
            return 2
        convert_one(m4a, output_dir=OUTPUT_DIR, force=args.force)
    else:
        if args.force:
            # In bulk mode, --force means: for every M4A, drop its <hash>.mp3
            # first so the main loop will re-encode. We only delete files that
            # we'd recreate — never any random .mp3 the user dropped in.
            for m4a in OUTPUT_DIR.iterdir():
                if m4a.is_file() and m4a.suffix.lower() == ".m4a":
                    mp3 = OUTPUT_DIR / f"{_hash_file(m4a)}.mp3"
                    if mp3.exists():
                        mp3.unlink()
        convert_missing(OUTPUT_DIR)
    return 0


if __name__ == "__main__":
    sys.exit(main())
