#!/usr/bin/env python3
"""
Analyze WebVTT transcripts for M4A files in OUTPUT_DIR to generate logical
chapter markers using Ollama, write them as FFmpeg metadata sidecars (.chaptermarks.txt),
and embed them into the M4A files using FFmpeg.

  uv run chapters.py                  # process all M4As missing a .chaptermarks.txt
  uv run chapters.py --file foo.m4a   # process one specific file
  uv run chapters.py --force          # regenerate chapters even if already present

Requires ffmpeg on PATH.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from mutagen.mp4 import MP4

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
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434").strip().rstrip("/")
OLLAMA_TEXT_MODEL = os.environ.get(
    "OLLAMA_TEXT_MODEL", "charaf/qwen3.6-35b-a3b-coding-nvfp4-mlx:latest"
).strip()
SUMMARY_TIMEOUT_S = int(os.environ.get("SUMMARY_TIMEOUT_S", "600"))
CHAPTERS_TIMEOUT_S = int(os.environ.get("CHAPTERS_TIMEOUT_S", str(SUMMARY_TIMEOUT_S)))

# Context window/transcript truncation threshold.
_TRANSCRIPT_CHAR_LIMIT = 60_000

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("chapters")


# ---------------------------------------------------------------------------
# VTT Parsing & Formatting
# ---------------------------------------------------------------------------

_TIMESTAMP_RE = re.compile(r"^\d{2}:\d{2}:\d{2}[.,]\d{3}\s*-->")


def _vtt_to_timestamped_text(vtt_path: Path) -> str:
    """Read WebVTT and return text formatted as [HH:MM:SS.mmm] Text."""
    lines: list[str] = []
    current_ts = ""
    text_buf: list[str] = []

    for raw in vtt_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line:
            if current_ts and text_buf:
                lines.append(f"[{current_ts}] {' '.join(text_buf)}")
                text_buf = []
                current_ts = ""
            continue
        if line == "WEBVTT" or line.startswith("WEBVTT "):
            continue
        match = _TIMESTAMP_RE.match(line)
        if match:
            if current_ts and text_buf:
                lines.append(f"[{current_ts}] {' '.join(text_buf)}")
                text_buf = []
            parts = line.split("-->")
            current_ts = parts[0].strip()
            continue
        if line.isdigit():
            continue
        text_buf.append(line)

    if current_ts and text_buf:
        lines.append(f"[{current_ts}] {' '.join(text_buf)}")

    return "\n".join(lines).strip()


def _get_last_vtt_timestamp_ms(vtt_path: Path) -> int:
    """Scan VTT file to find the end timestamp of the final cue."""
    last_end = 0
    for line in vtt_path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = _TIMESTAMP_RE.match(line.strip())
        if match:
            parts = line.split("-->")
            if len(parts) == 2:
                last_end = max(last_end, _ts_to_ms(parts[1].strip()))
    return last_end


# ---------------------------------------------------------------------------
# Timestamp Conversion Utilities
# ---------------------------------------------------------------------------

def _ts_to_ms(ts: str) -> int:
    """Convert HH:MM:SS.mmm or MM:SS.mmm to milliseconds."""
    parts = ts.strip().split(":")
    if len(parts) == 3:
        try:
            h = int(parts[0])
            m = int(parts[1])
            s = float(parts[2])
            return h * 3600000 + m * 60000 + int(round(s * 1000))
        except ValueError:
            return 0
    elif len(parts) == 2:
        try:
            m = int(parts[0])
            s = float(parts[1])
            return m * 60000 + int(round(s * 1000))
        except ValueError:
            return 0
    else:
        try:
            return int(round(float(ts) * 1000))
        except ValueError:
            return 0


# ---------------------------------------------------------------------------
# Ollama Prompts and Requests
# ---------------------------------------------------------------------------

_SYSTEM_INSTRUCTIONS = (
    "You are an editor analyzing a podcast transcript to partition it into logical chapters/segments. "
    "Combine all logical chunks and paragraphs of the transcript into cohesive, chronological chapters "
    "that span the entire episode. Avoid fragmented or overly detailed segments. Aim for 3 to 10 chapters in total "
    "depending on the length of the transcript. "
    "Each chapter must have a descriptive, concise title and start timestamp. "
    "Respond with strict JSON only — no markdown, no commentary."
)


def _build_prompt(timestamped_transcript: str) -> str:
    snippet = timestamped_transcript[:_TRANSCRIPT_CHAR_LIMIT]
    if len(timestamped_transcript) > _TRANSCRIPT_CHAR_LIMIT:
        snippet += "\n\n[... transcript truncated for length ...]"
    return (
        f"{_SYSTEM_INSTRUCTIONS}\n\n"
        "Transcript with timestamps:\n"
        '"""\n'
        f"{snippet}\n"
        '"""\n\n'
        "Respond with a JSON object containing a 'chapters' array. "
        "Each item in the array must be an object with exactly these keys:\n"
        '  "start": the exact start timestamp of the chapter, formatted as HH:MM:SS.mmm (e.g. 00:02:15.500) '
        "           as seen in the transcript. Make sure the timestamp matches one of the segment start timestamps exactly or closely.\n"
        '  "title": a concise chapter title, max 60 characters.\n\n'
        "The first chapter must start at 00:00:00.000.\n"
        "Chapters must cover the entire transcript chronologically without overlapping."
    )


def _ollama_chapters(timestamped_transcript: str) -> list[dict]:
    """POST to Ollama /api/generate (JSON mode) and return parsed chapters."""
    url = f"{OLLAMA_BASE_URL}/api/generate"
    payload = {
        "model": OLLAMA_TEXT_MODEL,
        "prompt": _build_prompt(timestamped_transcript),
        "stream": False,
        "format": "json",
        "options": {"temperature": 0.3},
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=CHAPTERS_TIMEOUT_S) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError(f"Ollama HTTP {e.code} from {url}: {detail or e.reason}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Cannot reach Ollama at {url}: {e.reason}") from e

    try:
        envelope = json.loads(body)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Ollama returned non-JSON envelope: {body[:200]!r}") from e
    if envelope.get("error"):
        raise RuntimeError(f"Ollama error: {envelope['error']}")

    raw_response = envelope.get("response") or ""
    if not raw_response.strip():
        raise RuntimeError("Ollama returned an empty 'response' field.")

    try:
        parsed = json.loads(raw_response)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"Model did not return valid JSON in 'response': {raw_response[:200]!r}"
        ) from e

    chapters = parsed.get("chapters")
    if not isinstance(chapters, list):
        raise RuntimeError(f"Model JSON response did not contain a 'chapters' array: {parsed!r}")

    return chapters


# ---------------------------------------------------------------------------
# Normalization & File Writing
# ---------------------------------------------------------------------------

def _normalize_chapters(raw_chapters: list[dict], duration_ms: int) -> list[dict]:
    """Parse, sort, clean, and compute boundaries of chapters."""
    parsed = []
    for rc in raw_chapters:
        title = str(rc.get("title", "")).strip()
        start_str = str(rc.get("start", "")).strip()
        if not title or not start_str:
            continue
        start_ms = _ts_to_ms(start_str)
        parsed.append({"title": title, "start_ms": start_ms})

    parsed.sort(key=lambda x: x["start_ms"])

    if not parsed:
        parsed.append({"title": "Introduction", "start_ms": 0})

    # Force first chapter to start at 0
    parsed[0]["start_ms"] = 0

    # Filter out chapters starting beyond the audio length
    filtered = [c for c in parsed if c["start_ms"] < duration_ms]
    if not filtered:
        filtered = [{"title": "Introduction", "start_ms": 0}]

    # Filter duplicate start times (keep the first one)
    seen = set()
    unique = []
    for c in filtered:
        if c["start_ms"] not in seen:
            unique.append(c)
            seen.add(c["start_ms"])

    # Compute END for each chapter
    for i in range(len(unique) - 1):
        unique[i]["end_ms"] = unique[i + 1]["start_ms"]
    unique[-1]["end_ms"] = duration_ms

    return unique


def _escape_metadata_val(val: str) -> str:
    """Escape special characters as expected by FFmpeg metadata files."""
    escaped = ""
    for char in val:
        if char in ('\\', '=', ';', '#', '\n'):
            escaped += '\\' + char
        else:
            escaped += char
    return escaped


def _write_metadata_file(txt_path: Path, title: str, chapters: list[dict]) -> None:
    """Write standard FFMETADATA1 file containing chapters."""
    lines = [
        ";FFMETADATA1",
        f"title={_escape_metadata_val(title)}",
        ""
    ]
    for ch in chapters:
        lines.extend([
            "[CHAPTER]",
            "TIMEBASE=1/1000",
            f"START={ch['start_ms']}",
            f"END={ch['end_ms']}",
            f"title={_escape_metadata_val(ch['title'])}",
            ""
        ])
    txt_path.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# Pipeline Driver functions
# ---------------------------------------------------------------------------

def _load_episode_title(m4a_path: Path) -> str:
    json_path = m4a_path.with_suffix(".json")
    if json_path.exists():
        try:
            meta = json.loads(json_path.read_text(encoding="utf-8"))
            if meta.get("title"):
                return meta["title"]
        except Exception as e:  # noqa: BLE001
            log.warning("Could not parse sidecar JSON for title: %s", e)
    return m4a_path.stem


def _get_m4a_duration_ms(m4a_path: Path) -> int:
    try:
        audio = MP4(str(m4a_path))
        return int(round(audio.info.length * 1000))
    except Exception as e:  # noqa: BLE001
        log.warning("Could not read M4A duration via mutagen: %s", e)
        return 0


def _update_m4a_metadata(m4a_path: Path, metadata_path: Path) -> None:
    """Use FFmpeg to apply the metadata file to the M4A atomically."""
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg not found on PATH. Install it (e.g. `brew install ffmpeg`).")

    tmp_m4a = m4a_path.with_name(f".{m4a_path.name}.partial")
    cmd = [
        "ffmpeg",
        "-y",
        "-loglevel", "error",
        "-i", str(m4a_path),
        "-i", str(metadata_path),
        "-map", "0:a",
        "-map_metadata", "0",
        "-map_chapters", "1",
        "-c:a", "copy",
        "-f", "ipod",
        str(tmp_m4a)
    ]
    log.info("Embedding chapters into %s via FFmpeg...", m4a_path.name)
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        tmp_m4a.unlink(missing_ok=True)
        raise RuntimeError(
            f"ffmpeg failed to apply chapters to {m4a_path.name}: {(e.stderr or '').strip() or e}"
        ) from e
    except FileNotFoundError as e:
        tmp_m4a.unlink(missing_ok=True)
        raise RuntimeError("ffmpeg disappeared from PATH.") from e

    tmp_m4a.replace(m4a_path)


def chapters_one(m4a: Path, *, force: bool = False) -> Optional[Path]:
    """Generate chapter marks file for an M4A and update its embedded metadata.

    Returns path to the generated chaptermarks file, or None if skipped.
    """
    txt_path = m4a.with_suffix(".chaptermarks.txt")
    if txt_path.exists() and not force:
        log.info("Chapter marks already exist for %s; skipping.", m4a.name)
        return None

    vtt_path = m4a.with_suffix(".vtt")
    if not vtt_path.exists():
        log.warning(
            "No transcript yet for %s (expected %s); skipping chapter generation.",
            m4a.name, vtt_path.name,
        )
        return None

    timestamped_transcript = _vtt_to_timestamped_text(vtt_path)
    if not timestamped_transcript:
        raise RuntimeError(f"Transcript {vtt_path.name} is empty.")

    log.info(
        "Analyzing %s (%d chars) for chapters via %s @ %s...",
        vtt_path.name, len(timestamped_transcript), OLLAMA_TEXT_MODEL, OLLAMA_BASE_URL,
    )

    raw_chapters = _ollama_chapters(timestamped_transcript)

    # Determine audio length in milliseconds
    duration_ms = _get_m4a_duration_ms(m4a)
    if duration_ms <= 0:
        duration_ms = _get_last_vtt_timestamp_ms(vtt_path)
    if duration_ms <= 0:
        raise RuntimeError(f"Could not determine audio duration for {m4a.name}")

    chapters = _normalize_chapters(raw_chapters, duration_ms)
    log.info("Generated %d chapter(s) for %s.", len(chapters), m4a.name)

    title = _load_episode_title(m4a)
    _write_metadata_file(txt_path, title, chapters)
    log.info("Wrote %s.", txt_path.name)

    # Apply to M4A
    _update_m4a_metadata(m4a, txt_path)

    # Restore metadata tags and cover art if sidecar JSON exists
    json_path = m4a.with_suffix(".json")
    if json_path.exists():
        try:
            from id3tag import tag_one
            tag_one(m4a, force=True)
            log.info("Restored MP4 metadata tags/cover art after chapter embedding.")
        except Exception as e:
            log.warning("Could not restore MP4 tags: %s", e)

    return txt_path


def chapters_missing(output_dir: Path) -> tuple[int, int]:
    """Find M4As lacking .chaptermarks.txt and generate chapters for them."""
    candidates = sorted(
        m4a for m4a in output_dir.glob("*.m4a")
        if not m4a.with_suffix(".chaptermarks.txt").exists()
    )
    if not candidates:
        log.info("Chapters pass: all M4As already have chapter marks — nothing to do.")
        return 0, 0

    missing_vtt = [m4a for m4a in candidates if not m4a.with_suffix(".vtt").exists()]
    if missing_vtt:
        log.warning(
            "Chapters pass: %d M4A(s) have no .vtt yet — run transcribe.py first: %s",
            len(missing_vtt),
            ", ".join(m.name for m in missing_vtt),
        )

    actionable = [m4a for m4a in candidates if m4a.with_suffix(".vtt").exists()]
    if not actionable:
        return 0, 0

    log.info(
        "Chapters pass: %d M4A file(s) need chapter marks generated from VTT.",
        len(actionable),
    )
    successes, failures = 0, 0
    for m4a in actionable:
        try:
            chapters_one(m4a)
            successes += 1
        except Exception as e:  # noqa: BLE001
            failures += 1
            log.error("Failed to generate chapters for %s: %s", m4a.name, e)

    log.info("Chapters pass done. %d succeeded, %d failed.", successes, failures)
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
        help="Generate chapters for one specific M4A file (path) instead of scanning OUTPUT_DIR.",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Regenerate the chapter marks even if they already exist.",
    )
    args = parser.parse_args()

    _require_config()
    assert OUTPUT_DIR is not None

    if args.file:
        m4a = Path(args.file).expanduser().resolve()
        if not m4a.exists() or m4a.suffix.lower() != ".m4a":
            log.error("Not an existing .m4a file: %s", m4a)
            return 2
        try:
            chapters_one(m4a, force=args.force)
        except Exception as e:  # noqa: BLE001
            log.error("Failed to generate chapters: %s", e)
            return 1
    else:
        if args.force:
            for m4a in OUTPUT_DIR.glob("*.m4a"):
                if m4a.with_suffix(".vtt").exists():
                    txt = m4a.with_suffix(".chaptermarks.txt")
                    txt.unlink(missing_ok=True)
        chapters_missing(OUTPUT_DIR)
    return 0


if __name__ == "__main__":
    sys.exit(main())
