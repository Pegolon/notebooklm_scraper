#!/usr/bin/env python3
"""
Generate sidecar JSON metadata for M4A files in OUTPUT_DIR that lack one,
by summarizing the matching WebVTT transcript via an Ollama text model.

For every <basename>.m4a with a matching <basename>.vtt but no <basename>.json,
strip the timestamps out of the VTT, send the plain text to Ollama's
/api/generate (JSON mode) and ask for a concise title + multi-paragraph
description, then write a sidecar <basename>.json that mirrors the shape
written by scraper.py for downloaded notebooks.

  uv run summarize.py                  # process all M4As missing a .json
  uv run summarize.py --file foo.m4a   # process one file
  uv run summarize.py --force          # regenerate even if .json already exists

Designed for M4As the user manually drops into OUTPUT_DIR. Scraper-downloaded
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
    "Given a transcript of an audio episode, you produce a concise title, "
    "a clear well-written description summarising the content, and a single "
    "emoji that captures the episode's main subject (used as the cover-art "
    "icon). "
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
        '  "emoji":       a single emoji glyph that thematically represents the\n'
        "                 episode (e.g. 🧬 for biology, 🪐 for astronomy, 🏛️ for\n"
        "                 history). Prefer concrete subject-matter symbols over\n"
        "                 generic ones like 🎙️ or 🎧. Output the emoji character\n"
        "                 itself, not a name or shortcode. Exactly one emoji.\n"
    )


# Match a single user-perceived emoji "cluster": one base emoji codepoint plus
# any trailing variation selectors / skin-tone modifiers / ZWJ-joined extra
# emoji codepoints. We don't try to enumerate every Unicode emoji block;
# instead we accept anything in the Extended_Pictographic ranges that the
# model is realistically going to output, and reject ASCII / letters /
# punctuation so a model that writes "microphone" or ":mic:" gets rejected.
_EMOJI_BASE_RANGES = (
    (0x1F300, 0x1FAFF),  # Misc symbols & pictographs, transport, food, supplemental, symbols&pictographs ext-A
    (0x2600, 0x27BF),    # Misc symbols, dingbats
    (0x1F000, 0x1F2FF),  # Mahjong, domino, playing cards, enclosed alphanum supplement
    (0x2300, 0x23FF),    # Misc technical (⌚, ⌛, ⏰, …)
    (0x2B00, 0x2BFF),    # Misc symbols and arrows (⭐, ⬆, …)
    (0x1F1E6, 0x1F1FF),  # Regional indicator symbols (flags)
)
_EMOJI_JOIN_CODEPOINTS = {
    0xFE0E, 0xFE0F,  # text / emoji variation selectors
    0x200D,           # zero-width joiner
    *range(0x1F3FB, 0x1F400),  # skin-tone modifiers
    *range(0xE0020, 0xE0080),  # tag sequences (e.g. subdivision flags)
    0xE007F,          # cancel tag
}


def _is_emoji_base(cp: int) -> bool:
    return any(lo <= cp <= hi for lo, hi in _EMOJI_BASE_RANGES)


def _extract_emoji(raw: str) -> Optional[str]:
    """Pull the first valid emoji cluster from `raw` and return it (or None).

    Accepts a leading base emoji codepoint plus any following variation
    selectors, skin-tone modifiers, and ZWJ-joined extra emoji codepoints
    (so 🏃‍♂️, 🇪🇺, 👨‍👩‍👧 all survive intact). Rejects anything that's
    plainly text — if the model wrote "microphone" or ":mic:" we return
    None and let coverart.py fall back to COVER_DEFAULT_EMOJI.
    """
    if not raw:
        return None
    s = raw.strip()
    if not s:
        return None
    chars = list(s)
    # The first codepoint must itself be an emoji base.
    if not _is_emoji_base(ord(chars[0])):
        return None
    out = [chars[0]]
    i = 1
    while i < len(chars):
        cp = ord(chars[i])
        if cp in _EMOJI_JOIN_CODEPOINTS:
            out.append(chars[i])
            i += 1
            continue
        # After a ZWJ, allow another emoji base.
        if out[-1] == "\u200d" and _is_emoji_base(cp):
            out.append(chars[i])
            i += 1
            continue
        # Two regional indicators in a row form a flag.
        if (0x1F1E6 <= ord(out[-1]) <= 0x1F1FF) and (0x1F1E6 <= cp <= 0x1F1FF):
            out.append(chars[i])
            i += 1
            continue
        break
    return "".join(out)


def _ollama_summarise(transcript: str) -> tuple[str, str, Optional[str]]:
    """POST the transcript to Ollama's /api/generate (JSON mode) and return
    (title, description, emoji). emoji is None if the model didn't return a
    parseable single-emoji glyph. Raises on any failure or schema violation."""
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
    emoji_raw = str(parsed.get("emoji", "") or "").strip()
    emoji = _extract_emoji(emoji_raw)
    if emoji_raw and not emoji:
        # The model emitted something for the emoji slot but it didn't parse
        # as one (e.g. ":mic:", "microphone", "🎙️ - microphone"). Log and
        # move on; coverart.py will use COVER_DEFAULT_EMOJI.
        log.warning("Model returned non-emoji value for emoji field: %r", emoji_raw)
    return title, description, emoji


# ---------------------------------------------------------------------------
# Per-file driver
# ---------------------------------------------------------------------------

def summarise_one(m4a: Path, *, force: bool = False) -> Optional[Path]:
    """Generate <basename>.json next to m4a by summarising <basename>.vtt via
    Ollama. Returns the JSON path, or None if the JSON already exists (and
    force=False) or the VTT is missing. Raises on Ollama / parsing failures."""
    json_path = m4a.with_suffix(".json")
    if json_path.exists() and not force:
        log.info("Sidecar JSON already exists for %s; skipping.", m4a.name)
        return None

    vtt_path = m4a.with_suffix(".vtt")
    if not vtt_path.exists():
        log.warning(
            "No transcript yet for %s (expected %s); skipping summary.",
            m4a.name, vtt_path.name,
        )
        return None

    transcript = _vtt_to_text(vtt_path)
    if not transcript:
        raise RuntimeError(f"Transcript {vtt_path.name} is empty after stripping cue headers.")

    log.info(
        "Summarising %s (%d chars) via %s @ %s ...",
        vtt_path.name, len(transcript), OLLAMA_TEXT_MODEL, OLLAMA_BASE_URL,
    )
    llm_title, description, emoji = _ollama_summarise(transcript)
    if emoji:
        log.info("Model picked emoji %s for %s.", emoji, m4a.name)

    # Build a title that matches scraper.py's `<prefix> - <topic>` shape so the
    # feed reads consistently. If the LLM already prepended the prefix, don't
    # double it.
    stripped = llm_title
    if TITLE_PREFIX and stripped.lower().startswith(TITLE_PREFIX.lower()):
        stripped = stripped[len(TITLE_PREFIX):].lstrip(" -—:|").strip()
    full_title = f"{TITLE_PREFIX} - {stripped}" if TITLE_PREFIX else stripped

    pub_date = datetime.fromtimestamp(m4a.stat().st_mtime, tz=timezone.utc)
    metadata = {
        # Stable per-file id (same scheme as the cloud app's synthesised GUID
        # for bare M4As) so the feed entry's GUID won't change once we add the
        # sidecar.
        "id": hashlib.md5(m4a.name.encode("utf-8")).hexdigest(),
        "title": full_title,
        "description": description,
        "audio_file": m4a.name,
        "pub_date": pub_date.isoformat(),
        "notebook_id": None,
        "notebook_url": None,
        # Manually-dropped M4As have no NotebookLM card to read an icon from,
        # so we ask the summarisation LLM to pick a fitting subject-matter
        # emoji from the transcript. None means the model declined or
        # returned non-emoji text — coverart.py then falls back to
        # COVER_DEFAULT_EMOJI.
        "notebook_emoji": emoji,
        "notebook_modified": None,
        "source": "manual",
    }
    json_path.write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    log.info("Wrote %s (%d bytes).", json_path.name, json_path.stat().st_size)
    return json_path


def _ollama_pick_emoji(transcript: str) -> Optional[str]:
    """Ask the LLM for *only* a fitting emoji given a transcript. Used by the
    --backfill-emojis mode to upgrade existing manual sidecars without
    rewriting their title/description. Returns None on any failure so the
    backfill loop can keep going."""
    snippet = transcript[:_TRANSCRIPT_CHAR_LIMIT]
    if len(transcript) > _TRANSCRIPT_CHAR_LIMIT:
        snippet += "\n\n[... transcript truncated for length ...]"
    prompt = (
        "Choose a single emoji that best represents the subject matter of "
        "the audio transcript below. Prefer concrete subject-matter symbols "
        "(🧬, 🪐, 🏛️, 🦀, …) over generic ones like 🎙️ or 🎧. Output strict "
        'JSON only — no markdown, no commentary — in the form {"emoji": "X"} '
        "where X is the emoji character itself, not a name or shortcode.\n\n"
        "Transcript:\n"
        f'"""\n{snippet}\n"""\n'
    )
    payload = {
        "model": OLLAMA_TEXT_MODEL,
        "prompt": prompt,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0.3},
    }
    req = urllib.request.Request(
        f"{OLLAMA_BASE_URL}/api/generate",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=SUMMARY_TIMEOUT_S) as resp:
            envelope = json.loads(resp.read().decode("utf-8", errors="replace"))
        parsed = json.loads(envelope.get("response") or "{}")
        return _extract_emoji(str(parsed.get("emoji", "") or "").strip())
    except Exception as e:  # noqa: BLE001
        log.warning("Emoji-only LLM call failed: %s", e)
        return None


def backfill_emojis(output_dir: Path) -> int:
    """Walk *.json sidecars in output_dir whose notebook_emoji is missing /
    empty and that carry a matching *.vtt transcript, ask the LLM for a
    fitting emoji, write it back, and delete the now-stale cover PNG so
    coverart.py rerenders. Skips entries that already have an emoji and
    those without a transcript. Returns the number of sidecars updated."""
    candidates = []
    for jp in sorted(output_dir.glob("*.json")):
        try:
            meta = json.loads(jp.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            log.warning("Backfill: cannot parse %s (%s); skipping.", jp.name, e)
            continue
        if meta.get("notebook_emoji"):
            continue
        vtt = jp.with_suffix(".vtt")
        if not vtt.exists():
            log.info(
                "Backfill: %s has no transcript yet; skipping "
                "(run transcribe.py first).", jp.name,
            )
            continue
        candidates.append((jp, meta, vtt))
    if not candidates:
        log.info("Emoji backfill: nothing to do.")
        return 0

    log.info("Emoji backfill: %d sidecar(s) missing notebook_emoji.", len(candidates))
    updated = 0
    for jp, meta, vtt in candidates:
        text = _vtt_to_text(vtt)
        if not text:
            log.warning("Backfill: %s transcript is empty; skipping.", vtt.name)
            continue
        emoji = _ollama_pick_emoji(text)
        if not emoji:
            log.warning("Backfill: no usable emoji returned for %s.", jp.name)
            continue
        meta["notebook_emoji"] = emoji
        jp.write_text(
            json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8",
        )
        png = jp.with_suffix(".png")
        if png.exists():
            png.unlink()
            log.info("Backfilled emoji %s into %s (stale PNG removed).", emoji, jp.name)
        else:
            log.info("Backfilled emoji %s into %s.", emoji, jp.name)
        updated += 1
    return updated


def summarise_missing(output_dir: Path) -> tuple[int, int]:
    """Find every *.m4a in output_dir lacking a matching *.json and generate one
    from its *.vtt. M4As without a sibling *.vtt are skipped (run transcribe.py
    first). Returns (successes, failures)."""
    candidates = sorted(
        m4a for m4a in output_dir.glob("*.m4a")
        if not m4a.with_suffix(".json").exists()
    )
    if not candidates:
        log.info("Summary pass: all M4As already have a .json — nothing to do.")
        return 0, 0

    missing_vtt = [m4a for m4a in candidates if not m4a.with_suffix(".vtt").exists()]
    if missing_vtt:
        log.warning(
            "Summary pass: %d M4A(s) have no .vtt yet — run transcribe.py first: %s",
            len(missing_vtt),
            ", ".join(m.name for m in missing_vtt),
        )

    actionable = [m4a for m4a in candidates if m4a.with_suffix(".vtt").exists()]
    if not actionable:
        return 0, 0

    log.info(
        "Summary pass: %d M4A file(s) need a JSON sidecar generated from VTT.",
        len(actionable),
    )
    successes, failures = 0, 0
    for m4a in actionable:
        try:
            summarise_one(m4a)
            successes += 1
        except Exception as e:  # noqa: BLE001
            failures += 1
            log.error("Failed to summarise %s: %s", m4a.name, e)
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
        help="Summarise one specific M4A file (path) instead of scanning OUTPUT_DIR.",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Regenerate the sidecar JSON even if it already exists.",
    )
    parser.add_argument(
        "--backfill-emojis", action="store_true",
        help="Only walk existing sidecars with no notebook_emoji and ask the LLM "
             "to pick one; preserves title/description. Triggers cover rerender + id3 retag.",
    )
    args = parser.parse_args()

    _require_config()
    assert OUTPUT_DIR is not None

    if args.backfill_emojis:
        n = backfill_emojis(OUTPUT_DIR)
        if n:
            # Stale covers were deleted in-place; rerender them and re-embed
            # the new atoms so podcatchers pick up the change.
            from coverart import cover_missing
            cover_missing(OUTPUT_DIR)
            try:
                from id3tag import tag_missing
                tag_missing(OUTPUT_DIR)
            except Exception as e:  # noqa: BLE001
                log.warning("MP4 retag failed: %s", e)
        return 0

    if args.file:
        m4a = Path(args.file).expanduser().resolve()
        if not m4a.exists() or m4a.suffix.lower() != ".m4a":
            log.error("Not an existing .m4a file: %s", m4a)
            return 2
        summarise_one(m4a, force=args.force)
    else:
        if args.force:
            # In bulk mode, --force means: regenerate every JSON we could
            # build (i.e. every M4A that has a VTT). Delete first so the
            # main loop treats them as missing.
            for m4a in OUTPUT_DIR.glob("*.m4a"):
                if m4a.with_suffix(".vtt").exists():
                    js = m4a.with_suffix(".json")
                    if js.exists():
                        js.unlink()
        summarise_missing(OUTPUT_DIR)
    return 0


if __name__ == "__main__":
    sys.exit(main())
