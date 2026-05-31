#!/usr/bin/env python3
"""
Verify and (re)write the standard podcast MP4 atoms on every <hash>.m4a
in OUTPUT_DIR, using each M4A's sibling <hash>.json for metadata and the
sibling <hash>.png as cover art (when present).

For every *.m4a in OUTPUT_DIR we build the canonical tag set from the JSON
sidecar and compare it to what's currently on the file. If anything is
missing or differs, we rewrite the affected atoms. Files that already
carry the full expected tag set are skipped — the pass is fully idempotent.

  uv run id3tag.py                 # verify/repair tags on all M4As
  uv run id3tag.py --file foo.m4a  # tag one specific file
  uv run id3tag.py --check         # report mismatches without writing
  uv run id3tag.py --force         # rewrite tags even when they already match

Configured via PODCAST_AUTHOR / PODCAST_ALBUM / PODCAST_GENRE / PODCAST_FEED_URL
in .env. The album defaults to TITLE_PREFIX (shared with scraper.py and
summarize.py) so manual + scraped episodes appear under the same show.

Standard atoms we maintain:
  ©nam  episode title       (from JSON.title)
  ©ART  artist / host       (PODCAST_AUTHOR)
  ©alb  album / show name   (PODCAST_ALBUM, defaults to TITLE_PREFIX)
  ©gen  genre               (PODCAST_GENRE, defaults to "Podcast")
  ©day  recording date      (JSON.pub_date)
  ©cmt  comment             (JSON.description, plain-text)
  desc  iTunes long desc.   (JSON.description)
  ----:com.apple.iTunes:PODCAST-GUID  episode GUID (JSON.id)
  purl  feed URL            (PODCAST_FEED_URL, optional)
  covr  cover image         (<hash>.png, if present — PNG format)
  pcst  podcast flag        (constant boolean True)
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
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm

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
# Atom construction
# ---------------------------------------------------------------------------

def _load_sidecar(m4a: Path) -> Optional[dict]:
    json_path = m4a.with_suffix(".json")
    if not json_path.exists():
        return None
    try:
        return json.loads(json_path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        log.warning("Could not parse %s: %s", json_path.name, e)
        return None


def _pub_date_for_m4a(meta: dict, m4a: Path) -> str:
    """Return the date string for ©day. Accepts an ISO-8601 timestamp from the
    sidecar, falls back to the M4A's mtime in ISO-8601 form."""
    raw = (meta.get("pub_date") or "").strip()
    if raw:
        return raw
    from datetime import datetime, timezone

    return datetime.fromtimestamp(m4a.stat().st_mtime, tz=timezone.utc).isoformat()


def _expected_atoms(meta: dict, m4a: Path, png_bytes: Optional[bytes]) -> dict:
    """Build a dict of atom_key → mutagen atom representation representing the
    canonical atom set for this episode. Used both to write tags and to
    compare against what's already on disk."""
    title = (meta.get("title") or m4a.stem).strip()
    description = (meta.get("description") or "").strip()
    episode_id = (meta.get("id") or m4a.stem).strip()
    date_str = _pub_date_for_m4a(meta, m4a)

    atoms = {
        "©nam": [title],
        "©ART": [PODCAST_AUTHOR],
        "©alb": [PODCAST_ALBUM],
        "©gen": [PODCAST_GENRE],
        "©day": [date_str],
        "©cmt": [description],
        "desc": [description],
        "pcst": True,
        "----:com.apple.iTunes:PODCAST-GUID": [MP4FreeForm(episode_id.encode("utf-8"))],
    }
    if PODCAST_FEED_URL:
        atoms["purl"] = [PODCAST_FEED_URL]
    if png_bytes:
        atoms["covr"] = [MP4Cover(png_bytes, imageformat=MP4Cover.FORMAT_PNG)]
    return atoms


def _atoms_equal(existing, expected) -> bool:
    """Compare just enough of two atoms/structures to decide whether a rewrite is
    needed."""
    if existing is None or expected is None:
        return existing is None and expected is None
    if isinstance(expected, list):
        if not isinstance(existing, list) or len(existing) != len(expected):
            return False
        return all(_atoms_equal(ex, eq) for ex, eq in zip(existing, expected))
    if isinstance(expected, MP4Cover):
        return (
            isinstance(existing, MP4Cover)
            and existing.imageformat == expected.imageformat
            and bytes(existing) == bytes(expected)
        )
    if isinstance(expected, MP4FreeForm):
        return (
            isinstance(existing, MP4FreeForm)
            and bytes(existing) == bytes(expected)
        )
    return existing == expected


# ---------------------------------------------------------------------------
# Per-file driver
# ---------------------------------------------------------------------------

def _open_m4a(m4a: Path) -> MP4:
    """Open an M4A with mutagen, raising a clear RuntimeError if the file
    isn't a valid MP4 container."""
    try:
        return MP4(str(m4a))
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(
            f"{m4a.name} is not a valid MP4 audio file ({e}); "
            "MP4 atoms cannot be applied."
        ) from e


def _diff_tags(m4a: Path, expected: dict) -> list[str]:
    """Return a list of atom keys that are missing or stale on disk.
    Raises if the file isn't valid MP4 audio."""
    audio = _open_m4a(m4a)
    tags = audio.tags
    diffs: list[str] = []
    for key, want in expected.items():
        have = tags.get(key) if tags is not None else None
        if not _atoms_equal(have, want):
            diffs.append(key)
    return diffs


def tag_one(m4a: Path, *, force: bool = False, check_only: bool = False) -> Optional[list[str]]:
    """Verify (and, unless check_only, repair) MP4 atoms on a single M4A.

    Returns the list of atom keys that needed updating (empty if the file
    was already complete). Returns None if metadata is missing and we
    can't even build the expected tag set."""
    meta = _load_sidecar(m4a)
    if meta is None:
        log.warning(
            "No JSON sidecar for %s — run summarize.py first; skipping tag verification.",
            m4a.name,
        )
        return None

    png_path = m4a.with_suffix(".png")
    png_bytes = png_path.read_bytes() if png_path.exists() else None
    if png_bytes is None:
        log.info("No cover PNG for %s; covr atom will not be embedded.", m4a.name)

    expected = _expected_atoms(meta, m4a, png_bytes)
    diffs = list(expected.keys()) if force else _diff_tags(m4a, expected)

    if not diffs:
        log.info("%s already has the full standard tag set.", m4a.name)
        return []

    if check_only:
        log.warning(
            "%s missing/stale atoms: %s",
            m4a.name, ", ".join(sorted(diffs)),
        )
        return diffs

    audio = _open_m4a(m4a)
    if audio.tags is None:
        audio.add_tags()

    for key in diffs:
        audio.tags[key] = expected[key]

    audio.save()
    log.info(
        "Updated %d atom(s) on %s: %s",
        len(diffs), m4a.name, ", ".join(sorted(diffs)),
    )
    return diffs


def tag_missing(output_dir: Path, *, check_only: bool = False) -> tuple[int, int, int]:
    """Verify MP4 atoms on every M4A in output_dir. Returns
    (already_ok, updated, failures). When check_only=True, `updated`
    counts files that *would* be updated."""
    m4as = sorted(output_dir.glob("*.m4a"))
    if not m4as:
        log.info("M4A tag pass: no .m4a files in %s — nothing to do.", output_dir)
        return 0, 0, 0

    log.info("M4A tag pass: verifying %d M4A file(s).", len(m4as))
    ok = updated = failures = 0
    for m4a in m4as:
        try:
            result = tag_one(m4a, check_only=check_only)
            if result is None:
                # No sidecar; counts as neither — already logged.
                continue
            if result:
                updated += 1
            else:
                ok += 1
        except Exception as e:  # noqa: BLE001
            failures += 1
            log.error("Failed to verify/tag %s: %s", m4a.name, e)

    verb = "would update" if check_only else "updated"
    log.info(
        "M4A tag pass done. %d already complete, %d %s, %d failed.",
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
        help="Verify/tag one specific M4A file (path) instead of scanning OUTPUT_DIR.",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="Report mismatches without modifying any files (exit 1 if any).",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Rewrite all standard atoms even if they already match.",
    )
    args = parser.parse_args()

    _require_config()
    assert OUTPUT_DIR is not None

    if args.file:
        m4a = Path(args.file).expanduser().resolve()
        if not m4a.exists() or m4a.suffix.lower() != ".m4a":
            log.error("Not an existing .m4a file: %s", m4a)
            return 2
        result = tag_one(m4a, force=args.force, check_only=args.check)
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
