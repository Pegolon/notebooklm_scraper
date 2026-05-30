#!/usr/bin/env python3
"""
Generate PNG cover-art images for MP3 files in OUTPUT_DIR using a local Ollama
instance running an image-generation model (e.g. FLUX.2 Klein).

For every <basename>.mp3 without a matching <basename>.png, read the sibling
<basename>.json description and ask Ollama for a cover image themed by that
description, on top of a fixed global style so all cover art shares a
consistent look.

  uv run coverart.py                  # generate covers for all MP3s missing one
  uv run coverart.py --file foo.mp3   # generate cover for a single file
  uv run coverart.py --force          # regenerate even if a .png already exists

Configured via OLLAMA_BASE_URL and OLLAMA_IMAGE_MODEL in .env.
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import sys
import urllib.error
import urllib.request
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
OLLAMA_IMAGE_MODEL = os.environ.get("OLLAMA_IMAGE_MODEL", "x/flux2-klein:9b").strip()

# Image dimensions and diffusion steps. Defaults stay small so a full cover
# fits comfortably inside the COVER_TIMEOUT_S window on modest hardware
# (~2 min for 512×512 / 12 steps on FLUX.2 Klein 9B). Crank these up via
# .env if your Ollama host is beefy enough.
COVER_WIDTH = int(os.environ.get("COVER_WIDTH", "512"))
COVER_HEIGHT = int(os.environ.get("COVER_HEIGHT", "512"))
COVER_STEPS = int(os.environ.get("COVER_STEPS", "12"))

# Image generation can take a while (cold model load + ~30s diffusion on CPU
# or modest GPUs). Set a generous timeout for the HTTP call.
COVER_TIMEOUT_S = int(os.environ.get("COVER_TIMEOUT_S", "600"))

# Global style prompt — every cover image is generated with this prefix so the
# whole podcast feed shares one visual identity. Override via env if you want
# a different look without touching code.
DEFAULT_STYLE_PROMPT = (
    "Podcast cover art, square 1:1 composition, centered subject, "
    "bold flat-vector illustration with a soft grain texture, "
    "limited palette of deep indigo, warm amber, and off-white, "
    "subtle geometric background shapes, clean and modern editorial feel, "
    "no text, no logos, no watermarks, no faces of real people, "
    "high contrast so it reads well as a small thumbnail."
)
COVER_STYLE_PROMPT = os.environ.get("COVER_STYLE_PROMPT", "").strip() or DEFAULT_STYLE_PROMPT

# How much of the (often long) description to feed into the image model.
_DESCRIPTION_CHAR_LIMIT = 1200

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("coverart")


def _load_description(mp3: Path) -> tuple[str, str]:
    """Return (title, description) for the given MP3 by reading its sidecar JSON.
    Falls back to filename-derived defaults if the JSON is missing/unreadable."""
    json_path = mp3.with_suffix(".json")
    title = mp3.stem
    description = ""
    if json_path.exists():
        try:
            meta = json.loads(json_path.read_text(encoding="utf-8"))
            title = (meta.get("title") or title).strip()
            description = (meta.get("description") or "").strip()
        except Exception as e:  # noqa: BLE001
            log.warning("Could not parse %s: %s", json_path.name, e)
    return title, description


def _build_prompt(title: str, description: str) -> str:
    snippet = description[:_DESCRIPTION_CHAR_LIMIT].strip()
    if len(description) > _DESCRIPTION_CHAR_LIMIT:
        snippet += " …"
    parts = [COVER_STYLE_PROMPT, "", f"Episode title: {title}"]
    if snippet:
        parts += ["", "Episode summary (use as thematic inspiration only):", snippet]
    parts += [
        "",
        "Design a cover image whose imagery evokes the themes and mood of the "
        "summary above, while strictly adhering to the global style guide. "
        "Do not render any text or letters in the image.",
    ]
    return "\n".join(parts)


def _ollama_generate_image(prompt: str) -> bytes:
    """POST to Ollama's /api/generate (streaming) and return PNG bytes.

    Uses stream=true so we get a JSONL response: per-step progress lines
    (``{"completed": N, "total": M, "done": false}``) followed by a final
    line carrying ``{"image": "<base64>", "done": true}``. Streaming also
    means the socket timeout is per-chunk, not for the whole generation —
    a 20-step 1024² render on a cold model load doesn't trip it.
    """
    url = f"{OLLAMA_BASE_URL}/api/generate"
    payload = {
        "model": OLLAMA_IMAGE_MODEL,
        "prompt": prompt,
        "stream": True,
        "width": COVER_WIDTH,
        "height": COVER_HEIGHT,
        "steps": COVER_STEPS,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    b64: Optional[str] = None
    try:
        with urllib.request.urlopen(req, timeout=COVER_TIMEOUT_S) as resp:
            for raw_line in resp:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    log.debug("Skipping non-JSON line from Ollama: %r", line[:120])
                    continue
                if msg.get("error"):
                    raise RuntimeError(f"Ollama error: {msg['error']}")
                if "completed" in msg and "total" in msg and not msg.get("done"):
                    log.info("  step %s/%s", msg["completed"], msg["total"])
                    continue
                if msg.get("done"):
                    b64 = msg.get("image") or (msg.get("images") or [None])[0]
                    break
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError(f"Ollama HTTP {e.code} from {url}: {detail or e.reason}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Cannot reach Ollama at {url}: {e.reason}") from e

    if not b64:
        raise RuntimeError("Ollama stream ended without an image payload.")
    try:
        return base64.b64decode(b64)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"Failed to decode base64 image from Ollama: {e}") from e


def generate_one(mp3: Path, *, force: bool = False) -> Optional[Path]:
    """Generate a <basename>.png cover next to mp3 via Ollama. Returns the PNG
    path (or None if it already existed and force=False). Raises on failure."""
    png_path = mp3.with_suffix(".png")
    if png_path.exists() and not force:
        log.info("Cover already exists for %s; skipping.", mp3.name)
        return None

    title, description = _load_description(mp3)
    prompt = _build_prompt(title, description)

    log.info(
        "Generating cover for %s via %s @ %s (%dx%d, %d steps)...",
        mp3.name, OLLAMA_IMAGE_MODEL, OLLAMA_BASE_URL,
        COVER_WIDTH, COVER_HEIGHT, COVER_STEPS,
    )
    png_bytes = _ollama_generate_image(prompt)
    png_path.write_bytes(png_bytes)
    log.info("Wrote %s (%d bytes).", png_path.name, png_path.stat().st_size)
    return png_path


def cover_missing(output_dir: Path) -> tuple[int, int]:
    """Find every *.mp3 in output_dir lacking a matching *.png and generate one.
    Returns (successes, failures)."""
    missing = sorted(
        mp3 for mp3 in output_dir.glob("*.mp3")
        if not mp3.with_suffix(".png").exists()
    )
    if not missing:
        log.info("Cover-art pass: all MP3s already have a .png — nothing to do.")
        return 0, 0

    log.info("Cover-art pass: %d MP3 file(s) missing a cover.", len(missing))
    successes, failures = 0, 0
    for mp3 in missing:
        try:
            generate_one(mp3)
            successes += 1
        except Exception as e:  # noqa: BLE001
            failures += 1
            log.error("Failed to generate cover for %s: %s", mp3.name, e)
            # Keep going so a single bad file doesn't block the rest.
    log.info("Cover-art pass done. %d succeeded, %d failed.", successes, failures)
    return successes, failures


def _require_config() -> None:
    if OUTPUT_DIR is None:
        log.error("OUTPUT_DIR is not set (configure it in .env).")
        sys.exit(2)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--file", type=str, default=None,
        help="Generate a cover for one specific MP3 file (path) instead of scanning OUTPUT_DIR.",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Regenerate the cover even if a .png already exists next to the MP3.",
    )
    args = parser.parse_args()

    _require_config()
    assert OUTPUT_DIR is not None

    if args.file:
        mp3 = Path(args.file).expanduser().resolve()
        if not mp3.exists() or mp3.suffix.lower() != ".mp3":
            log.error("Not an existing .mp3 file: %s", mp3)
            return 2
        generate_one(mp3, force=args.force)
    else:
        if args.force:
            # Honor --force in bulk mode by deleting existing .pngs first.
            for mp3 in OUTPUT_DIR.glob("*.mp3"):
                png = mp3.with_suffix(".png")
                if png.exists():
                    png.unlink()
        cover_missing(OUTPUT_DIR)
    return 0


if __name__ == "__main__":
    sys.exit(main())
