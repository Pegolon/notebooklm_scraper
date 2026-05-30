#!/usr/bin/env python3
"""
Verify and (re)write the standard podcast ID3v2 tag set on every <hash>.mp3
in OUTPUT_DIR, using each MP3's sibling <hash>.json for metadata and the
sibling <hash>.png as cover art (when present).

For every *.mp3 in OUTPUT_DIR we build the canonical tag set from the JSON
sidecar and compare it to what's currently on the file. If anything is
missing or differs, we rewrite the affected frames. Files that already
carry the full expected tag set are skipped — the pass is fully idempotent.

  uv run id3tag.py                 # verify/repair tags on all MP3s
  uv run id3tag.py --file foo.mp3  # tag one specific file
  uv run id3tag.py --check         # report mismatches without writing
  uv run id3tag.py --force         # rewrite tags even when they already match

Configured via PODCAST_AUTHOR / PODCAST_ALBUM / PODCAST_GENRE / PODCAST_FEED_URL
in .env. The album defaults to TITLE_PREFIX (shared with scraper.py and
summarize.py) so manual + scraped episodes appear under the same show.

Standard frames we maintain (ID3v2.4):
  TIT2  episode title       (from JSON.title)
  TPE1  artist / host       (PODCAST_AUTHOR)
  TALB  album / show name   (PODCAST_ALBUM, defaults to TITLE_PREFIX)
  TCON  genre               (PODCAST_GENRE, defaults to "Podcast")
  TDRC  recording date      (JSON.pub_date)
  COMM  comment             (JSON.description, plain-text)
  TDES  iTunes long desc.   (JSON.description)
  TGID  episode GUID        (JSON.id)
  WFED  feed URL            (PODCAST_FEED_URL, optional)
  APIC  cover image         (<hash>.png, if present — PNG, "Cover (front)")
  PCST  podcast flag        (constant 0x00000000)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from mutagen.id3 import (
    APIC,
    COMM,
    ID3,
    ID3NoHeaderError,
    PCST,
    TALB,
    TCON,
    TDES,
    TDRC,
    TGID,
    TIT2,
    TPE1,
    WFED,
)
from mutagen.mp3 import MP3

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
TITLE_PREFIX = os.environ.get("TITLE_PREFIX", "NotebookLM Overview").strip()
PODCAST_AUTHOR = os.environ.get("PODCAST_AUTHOR", "NotebookLM").strip() or "NotebookLM"
PODCAST_ALBUM = os.environ.get("PODCAST_ALBUM", "").strip() or TITLE_PREFIX
PODCAST_GENRE = os.environ.get("PODCAST_GENRE", "Podcast").strip() or "Podcast"
PODCAST_FEED_URL = os.environ.get("PODCAST_FEED_URL", "").strip()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("id3tag")


# ---------------------------------------------------------------------------
# Frame construction
# ---------------------------------------------------------------------------

def _load_sidecar(mp3: Path) -> Optional[dict]:
    json_path = mp3.with_suffix(".json")
    if not json_path.exists():
        return None
    try:
        return json.loads(json_path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        log.warning("Could not parse %s: %s", json_path.name, e)
        return None


def _pub_date_for_id3(meta: dict, mp3: Path) -> str:
    """Return the date string for TDRC. Accepts an ISO-8601 timestamp from the
    sidecar, falls back to the MP3's mtime in YYYY-MM-DD form.

    TDRC takes an ID3v2.4 timestamp; mutagen happily accepts ISO-ish strings
    (YYYY, YYYY-MM-DD, full ISO) and stores them verbatim."""
    raw = (meta.get("pub_date") or "").strip()
    if raw:
        # Drop any trailing timezone offset that mutagen's TDRC parser
        # doesn't strip (it stores "2026-05-30T12:34:56+00:00" verbatim
        # which is fine for podcatchers but noisy on display). Keep
        # whatever the sidecar wrote — we just want a stable, comparable
        # representation.
        return raw
    from datetime import datetime, timezone

    return datetime.fromtimestamp(mp3.stat().st_mtime, tz=timezone.utc).isoformat()


def _expected_frames(meta: dict, mp3: Path, png_bytes: Optional[bytes]) -> dict:
    """Build a dict of frame_id → mutagen frame instance representing the
    canonical tag set for this episode. Used both to write tags and to
    compare against what's already on disk."""
    title = (meta.get("title") or mp3.stem).strip()
    description = (meta.get("description") or "").strip()
    episode_id = (meta.get("id") or mp3.stem).strip()
    date_str = _pub_date_for_id3(meta, mp3)

    frames: dict = {
        "TIT2": TIT2(encoding=3, text=title),
        "TPE1": TPE1(encoding=3, text=PODCAST_AUTHOR),
        "TALB": TALB(encoding=3, text=PODCAST_ALBUM),
        "TCON": TCON(encoding=3, text=PODCAST_GENRE),
        "TDRC": TDRC(encoding=3, text=date_str),
        # COMM is keyed by (lang, desc); use 'eng' + empty desc which is what
        # most players display as the file-level comment.
        "COMM::eng": COMM(encoding=3, lang="eng", desc="", text=description),
        "TDES": TDES(encoding=3, text=description),
        "TGID": TGID(encoding=3, text=episode_id),
        # PCST's payload is a 4-byte big-endian integer; iTunes only checks
        # that the frame exists. mutagen stores it as `value = 0`.
        "PCST": PCST(value=0),
    }
    if PODCAST_FEED_URL:
        frames["WFED"] = WFED(url=PODCAST_FEED_URL)
    if png_bytes:
        frames["APIC:"] = APIC(
            encoding=3,
            mime="image/png",
            type=3,           # 3 = Cover (front)
            desc="",
            data=png_bytes,
        )
    return frames


def _frame_text(frame) -> str:
    """Normalize a text/URL frame to a comparable plain string."""
    if frame is None:
        return ""
    if hasattr(frame, "text"):
        # Text frames hold a list of strings (or ID3TimeStamps for TDRC).
        return " ".join(str(t) for t in frame.text)
    if hasattr(frame, "url"):
        return str(frame.url)
    return str(frame)


def _frames_equal(existing, expected) -> bool:
    """Compare just enough of two frames to decide whether a rewrite is
    needed. We compare textual content and (for APIC) the image bytes —
    not low-level encoding flags, which differ harmlessly across writers."""
    if existing is None or expected is None:
        return existing is None and expected is None
    if isinstance(expected, APIC):
        return (
            isinstance(existing, APIC)
            and existing.mime == expected.mime
            and existing.type == expected.type
            and existing.data == expected.data
        )
    if isinstance(expected, PCST):
        return isinstance(existing, PCST)
    if isinstance(expected, COMM):
        return (
            isinstance(existing, COMM)
            and existing.lang == expected.lang
            and _frame_text(existing) == _frame_text(expected)
        )
    return _frame_text(existing) == _frame_text(expected)


# ---------------------------------------------------------------------------
# Per-file driver
# ---------------------------------------------------------------------------

def _open_mp3(mp3: Path) -> MP3:
    """Open an MP3 with mutagen, raising a clear RuntimeError if the file
    isn't actually MPEG audio (NotebookLM has been known to deliver
    DASH/MP4 chunks with an .mp3 extension — those need different tag
    frames and should be flagged, not silently re-tagged)."""
    try:
        return MP3(str(mp3), ID3=ID3)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(
            f"{mp3.name} is not a valid MPEG audio file ({e}); "
            "ID3 frames cannot be applied. If this came from the scraper, "
            "the upstream download is probably an MP4/M4A container — "
            "re-encode via `uv run convert.py` (rename to .m4a first) "
            "or delete the file."
        ) from e


def _diff_tags(mp3: Path, expected: dict) -> list[str]:
    """Return a list of frame keys that are missing or stale on disk.
    Raises if the file isn't valid MPEG audio."""
    audio = _open_mp3(mp3)
    tags = audio.tags
    diffs: list[str] = []
    for key, want in expected.items():
        have = tags.get(key) if tags is not None else None
        if not _frames_equal(have, want):
            diffs.append(key)
    return diffs


def tag_one(mp3: Path, *, force: bool = False, check_only: bool = False) -> Optional[list[str]]:
    """Verify (and, unless check_only, repair) ID3 tags on a single MP3.

    Returns the list of frame keys that needed updating (empty if the file
    was already complete). Returns None if metadata is missing and we
    can't even build the expected tag set."""
    meta = _load_sidecar(mp3)
    if meta is None:
        log.warning(
            "No JSON sidecar for %s — run summarize.py first; skipping ID3 verification.",
            mp3.name,
        )
        return None

    png_path = mp3.with_suffix(".png")
    png_bytes = png_path.read_bytes() if png_path.exists() else None
    if png_bytes is None:
        log.info("No cover PNG for %s; APIC frame will not be embedded.", mp3.name)

    expected = _expected_frames(meta, mp3, png_bytes)
    diffs = list(expected.keys()) if force else _diff_tags(mp3, expected)

    if not diffs:
        log.info("%s already has the full standard tag set.", mp3.name)
        return []

    if check_only:
        log.warning(
            "%s missing/stale frames: %s",
            mp3.name, ", ".join(sorted(diffs)),
        )
        return diffs

    # Load (or create) the ID3 container, replace the affected frames,
    # and save back as ID3v2.4. We DELETE existing variants of each
    # frame before re-adding so duplicates can't accumulate.
    audio = _open_mp3(mp3)

    if audio.tags is None:
        try:
            audio.add_tags()
        except ID3NoHeaderError:
            audio.tags = ID3()

    for key in diffs:
        # `delall` accepts the frame id without the ":desc" suffix and
        # removes every instance — so "COMM" wipes COMM::eng, COMM::deu,
        # etc. That's what we want: one canonical tag per file.
        base = key.split(":", 1)[0]
        audio.tags.delall(base)

    for key in diffs:
        audio.tags.add(expected[key])

    audio.save(v2_version=4)
    log.info(
        "Updated %d frame(s) on %s: %s",
        len(diffs), mp3.name, ", ".join(sorted(diffs)),
    )
    return diffs


def tag_missing(output_dir: Path, *, check_only: bool = False) -> tuple[int, int, int]:
    """Verify ID3 tags on every MP3 in output_dir. Returns
    (already_ok, updated, failures). When check_only=True, `updated`
    counts files that *would* be updated."""
    mp3s = sorted(output_dir.glob("*.mp3"))
    if not mp3s:
        log.info("ID3 pass: no .mp3 files in %s — nothing to do.", output_dir)
        return 0, 0, 0

    log.info("ID3 pass: verifying %d MP3 file(s).", len(mp3s))
    ok = updated = failures = 0
    for mp3 in mp3s:
        try:
            result = tag_one(mp3, check_only=check_only)
            if result is None:
                # No sidecar; counts as neither — already logged.
                continue
            if result:
                updated += 1
            else:
                ok += 1
        except Exception as e:  # noqa: BLE001
            failures += 1
            log.error("Failed to verify/tag %s: %s", mp3.name, e)

    verb = "would update" if check_only else "updated"
    log.info(
        "ID3 pass done. %d already complete, %d %s, %d failed.",
        ok, updated, verb, failures,
    )
    return ok, updated, failures


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

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
        help="Verify/tag one specific MP3 file (path) instead of scanning OUTPUT_DIR.",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="Report mismatches without modifying any files (exit 1 if any).",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Rewrite all standard frames even if they already match.",
    )
    args = parser.parse_args()

    _require_config()
    assert OUTPUT_DIR is not None

    if args.file:
        mp3 = Path(args.file).expanduser().resolve()
        if not mp3.exists() or mp3.suffix.lower() != ".mp3":
            log.error("Not an existing .mp3 file: %s", mp3)
            return 2
        result = tag_one(mp3, force=args.force, check_only=args.check)
        if args.check and result:
            return 1
        return 0

    _, updated, failures = tag_missing(OUTPUT_DIR, check_only=args.check)
    if failures:
        return 1
    if args.check and updated:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
