#!/usr/bin/env python3
"""
Generate WebVTT transcripts for M4A files in OUTPUT_DIR using MLX Whisper.

Runs entirely on-device via Apple-Silicon-accelerated MLX. For every
<basename>.m4a without a matching <basename>.vtt, transcribe the audio with
`mlx_whisper.transcribe(...)` and save the WebVTT result alongside the M4A.

  uv run transcribe.py                 # transcribe all M4As missing a VTT
  uv run transcribe.py --file foo.m4a  # transcribe a single file

The default model is mlx-community/whisper-large-v3-mlx; override via
WHISPER_MODEL in .env. The model is fetched from the Hugging Face Hub on
first use and cached locally.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

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
WHISPER_MODEL = os.environ.get(
    "WHISPER_MODEL", "mlx-community/whisper-large-v3-mlx"
).strip()
# Optional ISO-639-1 language hint (e.g. "en", "de"). Empty = auto-detect.
WHISPER_LANGUAGE = os.environ.get("WHISPER_LANGUAGE", "").strip() or None

# Short, well-punctuated priming text. Whisper conditions on this for the
# first window, which strongly nudges the model to emit proper capitalisation
# and punctuation throughout the file. The exact wording matters less than
# the *style* (Title Case headings, full stops, commas, question marks).
# Override via WHISPER_INITIAL_PROMPT in .env to taste.
_DEFAULT_INITIAL_PROMPT = (
    "The following is a clear, well-punctuated transcript with proper "
    "capitalisation, commas, and full stops."
)
WHISPER_INITIAL_PROMPT = (
    os.environ.get("WHISPER_INITIAL_PROMPT", _DEFAULT_INITIAL_PROMPT).strip()
    or None
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("transcribe")


def _fmt_ts(seconds: float) -> str:
    """Format seconds as WebVTT timestamp HH:MM:SS.mmm."""
    if seconds < 0:
        seconds = 0.0
    ms_total = int(round(seconds * 1000))
    hours, rem = divmod(ms_total, 3600 * 1000)
    minutes, rem = divmod(rem, 60 * 1000)
    secs, ms = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{ms:03d}"


def _segments_to_vtt(segments: list[dict]) -> str:
    """Convert mlx_whisper segments to a WebVTT document."""
    lines = ["WEBVTT", ""]
    for i, seg in enumerate(segments, start=1):
        start = float(seg.get("start", 0.0))
        end = float(seg.get("end", start))
        text = str(seg.get("text", "")).strip()
        if not text:
            continue
        lines.append(str(i))
        lines.append(f"{_fmt_ts(start)} --> {_fmt_ts(end)}")
        lines.append(text)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def transcribe_one(m4a: Path) -> Path:
    """Run MLX Whisper on m4a, write <basename>.vtt next to it. Returns the
    VTT path. Raises on failure."""
    # Imported lazily so the module loads even without mlx-whisper installed
    # (e.g. when running on a non-Mac host).
    import mlx_whisper

    size_mb = m4a.stat().st_size / 1024 / 1024
    log.info(
        "Transcribing %s (%.1f MB) via %s%s...",
        m4a.name,
        size_mb,
        WHISPER_MODEL,
        f" (lang={WHISPER_LANGUAGE})" if WHISPER_LANGUAGE else " (auto-detect)",
    )

    kwargs: dict = {
        "path_or_hf_repo": WHISPER_MODEL,
        # Carry context (and our priming style) across windows so punctuation
        # and capitalisation stay consistent for long files.
        "condition_on_previous_text": True,
    }
    if WHISPER_LANGUAGE:
        kwargs["language"] = WHISPER_LANGUAGE
    if WHISPER_INITIAL_PROMPT:
        kwargs["initial_prompt"] = WHISPER_INITIAL_PROMPT

    result = mlx_whisper.transcribe(str(m4a), **kwargs)
    segments = result.get("segments") or []
    if not segments:
        # Fall back to a single cue spanning the whole text if the model
        # returned text without segments (shouldn't happen, but be safe).
        text = (result.get("text") or "").strip()
        if not text:
            raise RuntimeError("mlx_whisper returned no segments and no text.")
        segments = [{"start": 0.0, "end": 0.0, "text": text}]

    vtt_text = _segments_to_vtt(segments)
    vtt_path = m4a.with_suffix(".vtt")
    vtt_path.write_text(vtt_text, encoding="utf-8")
    log.info(
        "Wrote %s (%d bytes, %d cues).",
        vtt_path.name,
        vtt_path.stat().st_size,
        len(segments),
    )
    return vtt_path


def transcribe_missing(output_dir: Path) -> tuple[int, int]:
    """Find every *.m4a in output_dir lacking a matching *.vtt and transcribe each.
    Returns (successes, failures)."""
    missing = sorted(
        m4a for m4a in output_dir.glob("*.m4a")
        if not m4a.with_suffix(".vtt").exists()
    )
    if not missing:
        log.info("Transcription pass: all M4As already have a .vtt — nothing to do.")
        return 0, 0

    log.info("Transcription pass: %d M4A file(s) missing a transcript.", len(missing))
    successes, failures = 0, 0
    for m4a in missing:
        try:
            transcribe_one(m4a)
            successes += 1
        except Exception as e:  # noqa: BLE001
            failures += 1
            log.error("Failed to transcribe %s: %s", m4a.name, e)
            # Keep going so a single bad file doesn't block the rest.
    log.info("Transcription pass done. %d succeeded, %d failed.", successes, failures)
    return successes, failures


def _require_config() -> None:
    if OUTPUT_DIR is None:
        log.error("OUTPUT_DIR is not set (configure it in .env).")
        sys.exit(2)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--file", type=str, default=None,
        help="Transcribe one specific M4A file (path) instead of scanning OUTPUT_DIR.",
    )
    args = parser.parse_args()

    _require_config()
    assert OUTPUT_DIR is not None

    if args.file:
        m4a = Path(args.file).expanduser().resolve()
        if not m4a.exists() or m4a.suffix.lower() != ".m4a":
            log.error("Not an existing .m4a file: %s", m4a)
            return 2
        transcribe_one(m4a)
    else:
        transcribe_missing(OUTPUT_DIR)
    return 0


if __name__ == "__main__":
    sys.exit(main())
