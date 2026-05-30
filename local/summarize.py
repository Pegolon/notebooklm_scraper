#!/usr/bin/env python3
"""
Generate sidecar JSON metadata for MP3 files in OUTPUT_DIR that lack one,
by summarizing the matching WebVTT transcript via an Ollama text model.

For every <basename>.mp3 with a matching <basename>.vtt but no <basename>.json,
strip the timestamps out of the VTT, send the plain text to Ollama's
/api/generate (JSON mode) and ask for a concise title + multi-paragraph
description, then write a sidecar <basename>.json that mirrors the shape
written by scraper.py for downloaded notebooks.

  uv run summarize.py                  # process all MP3s missing a .json
  uv run summarize.py --file foo.mp3   # process one file
  uv run summarize.py --force          # regenerate even if .json already exists

Designed for MP3s the user manually drops into OUTPUT_DIR. Scraper-downloaded
episodes already get their .json from scraper.py, so this is a no-op for them.

Configured via OLLAMA_BASE_URL and OLLAMA_TEXT_MODEL in .env.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
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
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434").strip().rstrip("/")
OLLAMA_TEXT_MODEL = os.environ.get(
    "OLLAMA_TEXT_MODEL", "charaf/qwen3.6-35b-a3b-coding-nvfp4-mlx:latest"
).strip()

# LLM summarisation can spend a while on the first prompt (cold model load
# + a long transcript). Match the cover-art timeout default.
SUMMARY_TIMEOUT_S = int(os.environ.get("SUMMARY_TIMEOUT_S", "600"))

# Hard cap on transcript text fed to the model so we don't blow context on
# truly enormous files. Qwen-class models handle long context fine but the
# server's allocated num_ctx may not. 60k chars ≈ 15k tokens of English.
_TRANSCRIPT_CHAR_LIMIT = 60_000

# Title prefix shared with scraper.py so manual episodes show up similarly
# in the feed.
TITLE_PREFIX = os.environ.get("TITLE_PREFIX", "NotebookLM Overview").strip()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("summarize")


# ---------------------------------------------------------------------------
# VTT text extraction
# ---------------------------------------------------------------------------

_TIMESTAMP_RE = re.compile(r"^\d{2}:\d{2}:\d{2}[.,]\d{3}\s*-->")


def _vtt_to_text(vtt_path: Path) -> str:
    """Return the spoken text from a WebVTT file with all cue headers stripped.

    We skip the WEBVTT header, blank separators, numeric cue identifiers, and
    timestamp lines, then collapse the remaining cue text into paragraphs.
    """
    paragraphs: list[str] = []
    buf: list[str] = []
    for raw in vtt_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line:
            if buf:
                paragraphs.append(" ".join(buf))
                buf = []
            continue
        if line == "WEBVTT" or line.startswith("WEBVTT "):
            continue
        if _TIMESTAMP_RE.match(line):
            continue
        if line.isdigit():
            # Cue numeric identifier.
            continue
        buf.append(line)
    if buf:
        paragraphs.append(" ".join(buf))
    return "\n\n".join(paragraphs).strip()


# ---------------------------------------------------------------------------
# Ollama summarisation
# ---------------------------------------------------------------------------

_SYSTEM_INSTRUCTIONS = (
    "You are an editor preparing podcast episode metadata. "
    "Given a transcript of an audio episode, you produce a concise title and "
    "a clear, well-written description summarising the content. "
    "Write the description in neutral, informative prose (no marketing fluff, "
    "no first-person references to 'this transcript'). "
    "Respond with strict JSON only — no markdown, no commentary."
)


def _build_prompt(transcript: str) -> str:
    snippet = transcript[:_TRANSCRIPT_CHAR_LIMIT]
    if len(transcript) > _TRANSCRIPT_CHAR_LIMIT:
        snippet += "\n\n[... transcript truncated for length ...]"
    return (
        f"{_SYSTEM_INSTRUCTIONS}\n\n"
        "Transcript:\n"
        '"""\n'
        f"{snippet}\n"
        '"""\n\n'
        "Respond with a JSON object with exactly these keys:\n"
        '  "title":       a concise episode title, max 80 characters, no quotes.\n'
        '  "description": a 2–4 paragraph plain-prose summary of the episode content.\n'
    )


def _ollama_summarise(transcript: str) -> tuple[str, str]:
    """POST the transcript to Ollama's /api/generate (JSON mode) and return
    (title, description). Raises on any failure or schema violation."""
    url = f"{OLLAMA_BASE_URL}/api/generate"
    payload = {
        "model": OLLAMA_TEXT_MODEL,
        "prompt": _build_prompt(transcript),
        "stream": False,
        "format": "json",
        # Low temperature: we want consistent, faithful summaries.
        "options": {"temperature": 0.3},
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=SUMMARY_TIMEOUT_S) as resp:
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

    title = str(parsed.get("title", "")).strip()
    description = str(parsed.get("description", "")).strip()
    if not title or not description:
        raise RuntimeError(
            f"Model JSON missing required fields (title/description): {parsed!r}"
        )
    return title, description


# ---------------------------------------------------------------------------
# Per-file driver
# ---------------------------------------------------------------------------

def summarise_one(mp3: Path, *, force: bool = False) -> Optional[Path]:
    """Generate <basename>.json next to mp3 by summarising <basename>.vtt via
    Ollama. Returns the JSON path, or None if the JSON already exists (and
    force=False) or the VTT is missing. Raises on Ollama / parsing failures."""
    json_path = mp3.with_suffix(".json")
    if json_path.exists() and not force:
        log.info("Sidecar JSON already exists for %s; skipping.", mp3.name)
        return None

    vtt_path = mp3.with_suffix(".vtt")
    if not vtt_path.exists():
        log.warning(
            "No transcript yet for %s (expected %s); skipping summary.",
            mp3.name, vtt_path.name,
        )
        return None

    transcript = _vtt_to_text(vtt_path)
    if not transcript:
        raise RuntimeError(f"Transcript {vtt_path.name} is empty after stripping cue headers.")

    log.info(
        "Summarising %s (%d chars) via %s @ %s ...",
        vtt_path.name, len(transcript), OLLAMA_TEXT_MODEL, OLLAMA_BASE_URL,
    )
    llm_title, description = _ollama_summarise(transcript)

    # Build a title that matches scraper.py's `<prefix> - <topic>` shape so the
    # feed reads consistently. If the LLM already prepended the prefix, don't
    # double it.
    stripped = llm_title
    if TITLE_PREFIX and stripped.lower().startswith(TITLE_PREFIX.lower()):
        stripped = stripped[len(TITLE_PREFIX):].lstrip(" -—:|").strip()
    full_title = f"{TITLE_PREFIX} - {stripped}" if TITLE_PREFIX else stripped

    pub_date = datetime.fromtimestamp(mp3.stat().st_mtime, tz=timezone.utc)
    metadata = {
        # Stable per-file id (same scheme as the cloud app's synthesised GUID
        # for bare MP3s) so the feed entry's GUID won't change once we add the
        # sidecar.
        "id": hashlib.md5(mp3.name.encode("utf-8")).hexdigest(),
        "title": full_title,
        "description": description,
        "audio_file": mp3.name,
        "pub_date": pub_date.isoformat(),
        "notebook_id": None,
        "notebook_url": None,
        "notebook_modified": None,
        "source": "manual",
    }
    json_path.write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    log.info("Wrote %s (%d bytes).", json_path.name, json_path.stat().st_size)
    return json_path


def summarise_missing(output_dir: Path) -> tuple[int, int]:
    """Find every *.mp3 in output_dir lacking a matching *.json and generate one
    from its *.vtt. MP3s without a sibling *.vtt are skipped (run transcribe.py
    first). Returns (successes, failures)."""
    candidates = sorted(
        mp3 for mp3 in output_dir.glob("*.mp3")
        if not mp3.with_suffix(".json").exists()
    )
    if not candidates:
        log.info("Summary pass: all MP3s already have a .json — nothing to do.")
        return 0, 0

    missing_vtt = [mp3 for mp3 in candidates if not mp3.with_suffix(".vtt").exists()]
    if missing_vtt:
        log.warning(
            "Summary pass: %d MP3(s) have no .vtt yet — run transcribe.py first: %s",
            len(missing_vtt),
            ", ".join(m.name for m in missing_vtt),
        )

    actionable = [mp3 for mp3 in candidates if mp3.with_suffix(".vtt").exists()]
    if not actionable:
        return 0, 0

    log.info(
        "Summary pass: %d MP3 file(s) need a JSON sidecar generated from VTT.",
        len(actionable),
    )
    successes, failures = 0, 0
    for mp3 in actionable:
        try:
            summarise_one(mp3)
            successes += 1
        except Exception as e:  # noqa: BLE001
            failures += 1
            log.error("Failed to summarise %s: %s", mp3.name, e)
            # Keep going so a single bad file doesn't block the rest.
    log.info("Summary pass done. %d succeeded, %d failed.", successes, failures)
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
        help="Summarise one specific MP3 file (path) instead of scanning OUTPUT_DIR.",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Regenerate the sidecar JSON even if it already exists.",
    )
    args = parser.parse_args()

    _require_config()
    assert OUTPUT_DIR is not None

    if args.file:
        mp3 = Path(args.file).expanduser().resolve()
        if not mp3.exists() or mp3.suffix.lower() != ".mp3":
            log.error("Not an existing .mp3 file: %s", mp3)
            return 2
        summarise_one(mp3, force=args.force)
    else:
        if args.force:
            # In bulk mode, --force means: regenerate every JSON we could
            # build (i.e. every MP3 that has a VTT). Delete first so the
            # main loop treats them as missing.
            for mp3 in OUTPUT_DIR.glob("*.mp3"):
                if mp3.with_suffix(".vtt").exists():
                    js = mp3.with_suffix(".json")
                    if js.exists():
                        js.unlink()
        summarise_missing(OUTPUT_DIR)
    return 0


if __name__ == "__main__":
    sys.exit(main())
