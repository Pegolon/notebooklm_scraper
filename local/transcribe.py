#!/usr/bin/env python3
"""
Generate WebVTT transcripts for MP3 files in OUTPUT_DIR using Google Gemini.

For every <basename>.mp3 without a matching <basename>.vtt, upload the audio
to the Gemini File API, ask for a verbatim WebVTT transcription, and save
the result alongside the MP3.

  uv run transcribe.py                 # transcribe all MP3s missing a VTT
  uv run transcribe.py --file foo.mp3  # transcribe a single file

Requires GEMINI_API_KEY in .env.
"""

from __future__ import annotations

import argparse
import logging
import os
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
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash").strip()

TRANSCRIPTION_PROMPT = (
    "Transcribe the following audio file verbatim. "
    "Output the transcription strictly in the official WebVTT (.vtt) format, "
    "including the 'WEBVTT' header and precise timestamps "
    "(e.g., 00:00:01.000 --> 00:00:04.500). "
    "Do not include markdown code blocks (```vtt) or any conversational text "
    "in your response, only the raw VTT content."
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("transcribe")


def _strip_code_fence(text: str) -> str:
    """If the model wrapped the output in a ```vtt … ``` fence, strip it."""
    s = text.strip()
    if not s.startswith("```"):
        return s
    lines = s.splitlines()
    # Drop the opening fence line (```vtt, ```webvtt, etc.)
    lines = lines[1:]
    # Drop the trailing fence if present.
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def transcribe_one(mp3: Path) -> Path:
    """Upload mp3 to Gemini, request a VTT transcription, write <basename>.vtt
    next to it. Returns the VTT path. Raises on failure."""
    # Imported lazily so the module loads even without google-genai installed
    # (matters for cron environments missing the optional dep).
    from google import genai

    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not set; cannot transcribe.")

    size_mb = mp3.stat().st_size / 1024 / 1024
    log.info("Transcribing %s (%.1f MB) via %s...", mp3.name, size_mb, GEMINI_MODEL)
    client = genai.Client(api_key=GEMINI_API_KEY)

    uploaded = client.files.upload(file=str(mp3))
    try:
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=[uploaded, TRANSCRIPTION_PROMPT],
        )
        vtt_text = _strip_code_fence(response.text or "")
        if not vtt_text:
            raise RuntimeError("Gemini returned an empty response.")
        if not vtt_text.startswith("WEBVTT"):
            log.warning("Response missing WEBVTT header; prepending it.")
            vtt_text = "WEBVTT\n\n" + vtt_text

        vtt_path = mp3.with_suffix(".vtt")
        vtt_path.write_text(vtt_text, encoding="utf-8")
        log.info("Wrote %s (%d bytes).", vtt_path.name, vtt_path.stat().st_size)
        return vtt_path
    finally:
        try:
            client.files.delete(name=uploaded.name)
        except Exception as e:  # noqa: BLE001
            log.debug("Failed to delete uploaded Gemini file %s: %s", uploaded.name, e)


def transcribe_missing(output_dir: Path) -> tuple[int, int]:
    """Find every *.mp3 in output_dir lacking a matching *.vtt and transcribe each.
    Returns (successes, failures). Silently no-ops if GEMINI_API_KEY isn't set."""
    if not GEMINI_API_KEY:
        log.info("GEMINI_API_KEY not set; skipping transcription pass.")
        return 0, 0

    missing = sorted(
        mp3 for mp3 in output_dir.glob("*.mp3")
        if not mp3.with_suffix(".vtt").exists()
    )
    if not missing:
        log.info("Transcription pass: all MP3s already have a .vtt — nothing to do.")
        return 0, 0

    log.info("Transcription pass: %d MP3 file(s) missing a transcript.", len(missing))
    successes, failures = 0, 0
    for mp3 in missing:
        try:
            transcribe_one(mp3)
            successes += 1
        except Exception as e:  # noqa: BLE001
            failures += 1
            log.error("Failed to transcribe %s: %s", mp3.name, e)
            # Keep going so a single bad file doesn't block the rest.
    log.info("Transcription pass done. %d succeeded, %d failed.", successes, failures)
    return successes, failures


def _require_config() -> None:
    if OUTPUT_DIR is None:
        log.error("OUTPUT_DIR is not set (configure it in .env).")
        sys.exit(2)
    if not GEMINI_API_KEY:
        log.error("GEMINI_API_KEY is not set (configure it in .env).")
        sys.exit(2)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--file", type=str, default=None,
        help="Transcribe one specific MP3 file (path) instead of scanning OUTPUT_DIR.",
    )
    args = parser.parse_args()

    _require_config()
    assert OUTPUT_DIR is not None

    if args.file:
        mp3 = Path(args.file).expanduser().resolve()
        if not mp3.exists() or mp3.suffix.lower() != ".mp3":
            log.error("Not an existing .mp3 file: %s", mp3)
            return 2
        transcribe_one(mp3)
    else:
        transcribe_missing(OUTPUT_DIR)
    return 0


if __name__ == "__main__":
    sys.exit(main())
