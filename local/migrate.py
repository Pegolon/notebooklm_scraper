#!/usr/bin/env python3
"""
Bulk migration script to convert podcast audio files from MP3 to M4A.

Processes all *.mp3 files in OUTPUT_DIR:
  1. Re-encodes the MP3 file to M4A using ffmpeg (AAC, CBR 128k, iPod format).
  2. Updates the sibling JSON sidecar's "audio_file" field.
  3. Re-embeds chapters from the sibling .chaptermarks.txt file (if present).
  4. Applies standard podcast MP4 metadata atoms via id3tag.py.
  5. Deletes the original MP3 (unless --keep-mp3 is specified).

Supports:
  - --dry-run: Preview all migration changes without modifying files.
  - --keep-mp3: Convert and update sidecars but keep original MP3s.
  - --file <path>: Migrate a single MP3 file.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# Ensure we can import from current directory
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from convert import _clean_path_value, transcode_to_m4a
from id3tag import tag_one
from chapters import _update_m4a_metadata

load_dotenv(SCRIPT_DIR / ".env")

OUTPUT_DIR = (
    Path(_clean_path_value(os.environ["OUTPUT_DIR"])).expanduser()
    if os.environ.get("OUTPUT_DIR")
    else None
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("migrate")


def check_ffmpeg() -> bool:
    """Check if ffmpeg is available on PATH and supports the aac encoder."""
    if shutil.which("ffmpeg") is None:
        log.error("ffmpeg not found on PATH. Install it (e.g. `brew install ffmpeg`).")
        return False
    try:
        res = subprocess.run(["ffmpeg", "-encoders"], capture_output=True, text=True, check=True)
        if "aac" not in res.stdout:
            log.error("ffmpeg is present but does not support the 'aac' encoder.")
            return False
    except Exception as e:
        log.error("Failed to verify ffmpeg encoders: %s", e)
        return False
    return True


def migrate_one(
    mp3_path: Path,
    *,
    dry_run: bool = False,
    keep_mp3: bool = False,
) -> bool:
    """Migrate a single MP3 file to M4A. Returns True on success, False on failure."""
    m4a_path = mp3_path.with_suffix(".m4a")
    json_path = mp3_path.with_suffix(".json")
    chapters_path = mp3_path.with_suffix(".chaptermarks.txt")

    log.info("--- Migrating %s ---", mp3_path.name)

    # 1. Re-encode MP3 to M4A (AAC)
    if m4a_path.exists():
        log.info("M4A already exists at %s; skipping transcode.", m4a_path.name)
    else:
        if dry_run:
            log.info("[DRY RUN] Would transcode %s -> %s", mp3_path.name, m4a_path.name)
        else:
            try:
                # Clean up any stale partial files first
                partial_m4a = m4a_path.with_name(f".{m4a_path.name}.partial")
                partial_m4a.unlink(missing_ok=True)

                transcode_to_m4a(mp3_path, m4a_path)
            except Exception as e:
                log.error("Transcode failed for %s: %s", mp3_path.name, e)
                return False

    # 2. Update JSON Sidecar
    if json_path.exists():
        try:
            meta = json.loads(json_path.read_text(encoding="utf-8"))
            if meta.get("audio_file") == mp3_path.name:
                if dry_run:
                    log.info("[DRY RUN] Would update JSON sidecar %s: audio_file -> %s", json_path.name, m4a_path.name)
                else:
                    meta["audio_file"] = m4a_path.name
                    json_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
                    log.info("Updated audio_file in %s", json_path.name)
            else:
                log.info("JSON sidecar %s is already up-to-date or references another file.", json_path.name)
        except Exception as e:
            log.error("Failed to parse/update JSON sidecar %s: %s", json_path.name, e)
            if not dry_run:
                return False
    else:
        log.info("No JSON sidecar found for %s; skipping JSON update.", mp3_path.name)

    # 3. Re-embed Chapters (if .chaptermarks.txt sidecar exists)
    if chapters_path.exists():
        if dry_run:
            log.info("[DRY RUN] Would embed chapters from %s into %s", chapters_path.name, m4a_path.name)
        else:
            try:
                log.info("Re-embedding chapters from %s...", chapters_path.name)
                _update_m4a_metadata(m4a_path, chapters_path)
                log.info("Chapters successfully embedded into M4A.")
            except Exception as e:
                log.error("Failed to embed chapters: %s", e)
                return False
    else:
        log.info("No chaptermarks file found for %s; skipping chapter embedding.", mp3_path.name)

    # 4. Apply Metadata Tagging (re-tag M4A atoms)
    if json_path.exists():
        if dry_run:
            log.info("[DRY RUN] Would write MP4 atoms to %s", m4a_path.name)
        else:
            try:
                # tag_one returns updated keys list or None if missing sidecar
                res = tag_one(m4a_path, force=True)
                if res is not None:
                    log.info("Applied MP4 atoms to %s.", m4a_path.name)
            except Exception as e:
                log.error("Failed to tag M4A metadata atoms: %s", e)
                return False

    # 5. Clean up original MP3
    if not keep_mp3:
        if dry_run:
            log.info("[DRY RUN] Would delete original MP3: %s", mp3_path.name)
        else:
            try:
                mp3_path.unlink()
                log.info("Deleted original MP3: %s", mp3_path.name)
            except Exception as e:
                log.error("Failed to delete %s: %s", mp3_path.name, e)
                return False
    else:
        log.info("Skipping original MP3 deletion (keep_mp3=True).")

    return True


def migrate_all(
    output_dir: Path,
    *,
    dry_run: bool = False,
    keep_mp3: bool = False,
) -> int:
    """Scan and migrate all MP3s in output_dir. Returns exit code (0 on success, 1 on failure)."""
    mp3s = sorted(output_dir.glob("*.mp3"))
    if not mp3s:
        log.info("No .mp3 files found in %s — nothing to migrate.", output_dir)
        return 0

    log.info("Found %d MP3 file(s) in %s for migration.", len(mp3s), output_dir)
    if dry_run:
        log.info("=== DRY RUN MODE: No files will be modified or deleted ===")

    successes = 0
    failures = 0

    for mp3 in mp3s:
        try:
            if migrate_one(mp3, dry_run=dry_run, keep_mp3=keep_mp3):
                successes += 1
            else:
                failures += 1
        except Exception as e:
            failures += 1
            log.error("Unexpected error migrating %s: %s", mp3.name, e)

    log.info(
        "Migration pass done. %d succeeded, %d failed.",
        successes, failures,
    )
    return 1 if failures > 0 else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--file", type=str, default=None,
        help="Migrate a single specific MP3 file instead of scanning OUTPUT_DIR.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print actions that would be taken without modifying any files.",
    )
    parser.add_argument(
        "--keep-mp3", action="store_true",
        help="Do not delete the original .mp3 files after successful conversion.",
    )
    args = parser.parse_args()

    if not check_ffmpeg():
        return 1

    if OUTPUT_DIR is None:
        log.error("OUTPUT_DIR is not set (configure it in .env).")
        return 2

    if not OUTPUT_DIR.exists():
        log.error("OUTPUT_DIR does not exist: %s", OUTPUT_DIR)
        return 2

    if args.file:
        mp3 = Path(args.file).expanduser().resolve()
        if not mp3.exists() or mp3.suffix.lower() != ".mp3":
            log.error("Not an existing .mp3 file: %s", mp3)
            return 2
        success = migrate_one(mp3, dry_run=args.dry_run, keep_mp3=args.keep_mp3)
        return 0 if success else 1

    return migrate_all(OUTPUT_DIR, dry_run=args.dry_run, keep_mp3=args.keep_mp3)


if __name__ == "__main__":
    sys.exit(main())
