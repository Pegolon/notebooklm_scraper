#!/usr/bin/env python3
"""
Convert MP3 audio files in OUTPUT_DIR to M4A (AAC in MP4/ISO container), using
the MD5 of the original MP3 bytes as the filename so the conversion is fully idempotent.

For every <name>.mp3 in OUTPUT_DIR, compute md5(file-bytes); if a sibling
<hash>.m4a already exists, skip (already converted in a previous run);
otherwise transcode via ffmpeg into <hash>.m4a alongside the rest of the
episodes. The original MP3 is left untouched — delete it manually once
you're happy with the conversion.

This is the first pass in the manual-audio pipeline; once the M4A lands in
the folder, the rest of the chain (transcribe → summarise → coverart)
picks it up by its normal scan rules:

  convert (mp3 → <hash>.m4a) → transcribe → summarise → coverart

  uv run convert.py                  # convert all MP3s not yet converted
  uv run convert.py --file foo.mp3   # convert one specific file
  uv run convert.py --force          # re-encode even if <hash>.m4a exists

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

# AAC bitrate setting. 128k is transparent and standard for speech content.
# Override via .env if needed.
AAC_BITRATE = os.environ.get("AAC_BITRATE", "128k").strip()

# Read buffer for hashing. 1 MiB is a good fit for streaming files.
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


def transcode_to_m4a(src: Path, dst: Path, *, bitrate: Optional[str] = None) -> Path:
    """Transcode `src` (any audio container ffmpeg can read) to M4A (AAC) at `dst`.

    Writes via a hidden `<dst>.partial` temp file and atomically renames on
    success — a crash mid-encode cannot leave a half-written file at `dst`.
    Returns `dst`. Raises RuntimeError on any ffmpeg failure.

    Shared between the standalone convert pass (which names `dst` by the mp3
    bytes hash) and the scraper (which preserves the description-based hash
    so the JSON sidecar's `audio_file` stays valid).
    """
    if shutil.which("ffmpeg") is None:
        raise RuntimeError(
            "ffmpeg not found on PATH. Install it (e.g. `brew install ffmpeg`) "
            "and re-run."
        )

    b = (bitrate or AAC_BITRATE).strip()
    tmp = dst.with_name(f".{dst.name}.partial")
    cmd = [
        "ffmpeg",
        "-y",                       # overwrite tmp if it somehow exists
        "-loglevel", "error",       # quiet output; we only care about errors
        "-i", str(src),
        "-vn",                      # drop any embedded artwork/video stream
        "-c:a", "aac",
        "-b:a", b,
        "-f", "ipod",               # force iPod/M4A muxer
        str(tmp),
    ]
    log.info("Transcoding %s → %s (AAC CBR %s)...", src.name, dst.name, b)
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        tmp.unlink(missing_ok=True)
        # ffmpeg writes its real diagnostic to stderr.
        raise RuntimeError(
            f"ffmpeg failed on {src.name}: {(e.stderr or '').strip() or e}"
        ) from e
    except FileNotFoundError as e:  # noqa: BLE001
        tmp.unlink(missing_ok=True)
        raise RuntimeError("ffmpeg disappeared from PATH mid-run.") from e

    tmp.replace(dst)
    return dst


def convert_one(mp3: Path, *, output_dir: Path, force: bool = False) -> Optional[Path]:
    """Convert one MP3 to <hash>.m4a in output_dir, where <hash> is the MD5
    of the MP3 bytes (so re-runs on the same input skip the work). Returns
    the M4A path, or None if the output already existed (and force=False).
    Raises on ffmpeg failure."""
    size_mb = mp3.stat().st_size / 1024 / 1024
    log.info("Hashing %s (%.1f MB)...", mp3.name, size_mb)
    digest = _hash_file(mp3)
    m4a = output_dir / f"{digest}.m4a"

    if m4a.exists() and not force:
        log.info("%s already converted (%s); skipping.", mp3.name, m4a.name)
        return None

    transcode_to_m4a(mp3, m4a)
    out_size = m4a.stat().st_size
    log.info("Wrote %s (%.1f KB).", m4a.name, out_size / 1024)
    return m4a


def convert_missing(output_dir: Path) -> tuple[int, int]:
    """Find every *.mp3 in output_dir and convert it to <md5>.m4a unless that
    output file already exists. Returns (successes, failures). Files that
    were already up-to-date count as neither success nor failure."""
    mp3s = sorted(
        p for p in output_dir.iterdir()
        if p.is_file() and p.suffix.lower() == ".mp3"
    )
    if not mp3s:
        log.info("Conversion pass: no .mp3 files in %s — nothing to do.", output_dir)
        return 0, 0

    log.info("Conversion pass: %d .mp3 file(s) found.", len(mp3s))
    successes, failures = 0, 0
    for mp3 in mp3s:
        try:
            convert_one(mp3, output_dir=output_dir)
            successes += 1
        except Exception as e:  # noqa: BLE001
            failures += 1
            log.error("Failed to convert %s: %s", mp3.name, e)
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
        help="Convert one specific MP3 file (path) instead of scanning OUTPUT_DIR.",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Re-encode even if <hash>.m4a already exists.",
    )
    args = parser.parse_args()

    _require_config()
    assert OUTPUT_DIR is not None

    if args.file:
        mp3 = Path(args.file).expanduser().resolve()
        if not mp3.exists() or mp3.suffix.lower() != ".mp3":
            log.error("Not an existing .mp3 file: %s", mp3)
            return 2
        convert_one(mp3, output_dir=OUTPUT_DIR, force=args.force)
    else:
        if args.force:
            for mp3 in OUTPUT_DIR.iterdir():
                if mp3.is_file() and mp3.suffix.lower() == ".mp3":
                    m4a = OUTPUT_DIR / f"{_hash_file(mp3)}.m4a"
                    if m4a.exists():
                        m4a.unlink()
        convert_missing(OUTPUT_DIR)
    return 0


if __name__ == "__main__":
    sys.exit(main())
