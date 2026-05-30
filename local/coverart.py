#!/usr/bin/env python3
"""
Generate PNG cover art for MP3 files in OUTPUT_DIR.

For every <basename>.mp3 without a matching <basename>.png we read the sibling
<basename>.json, grab the ``notebook_emoji`` field that scraper.py captured
from NotebookLM's auto-assigned notebook icon (falling back to
``COVER_DEFAULT_EMOJI`` when none is available), and render a 1400×1400 PNG
showing that emoji full-bleed on top of a gradient-filled circle.

Implementation note: rendering Apple Color Emoji's sbix bitmap tables
requires Pillow — ImageMagick + FreeType on macOS only sees the glyph
outlines (you get a black silhouette). Pillow handles the bitmap directly
when you pass ``embedded_color=True``, so we use it for the whole pipeline.

  uv run coverart.py                  # generate covers for all MP3s missing one
  uv run coverart.py --file foo.mp3   # generate cover for a single file
  uv run coverart.py --force          # regenerate even if a .png already exists
"""

from __future__ import annotations

import argparse
import colorsys
import hashlib
import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFilter, ImageFont

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

# Final cover dimensions. Apple Podcasts wants 1400×1400 minimum.
COVER_SIZE = int(os.environ.get("COVER_SIZE", "1400"))

# Fallback emoji used when the sidecar JSON has no notebook_emoji (e.g. for
# manually-dropped MP3s or pre-existing episodes scraped before emoji capture
# was added).
DEFAULT_EMOJI = (os.environ.get("COVER_DEFAULT_EMOJI", "🎙️").strip() or "🎙️")

# Path to Apple Color Emoji. The font only ships specific sbix bitmap sizes
# (20 / 40 / 64 / 96 / 160 px on current macOS); 160 is the largest we can
# ask for, and we upscale from there to whatever we need on the canvas.
APPLE_EMOJI_FONT = os.environ.get(
    "APPLE_EMOJI_FONT", "/System/Library/Fonts/Apple Color Emoji.ttc"
).strip()
_APPLE_EMOJI_SBIX_SIZE = 160

# Fraction of the canvas the emoji bitmap occupies (square, centered). 0.75
# keeps the emoji inscribed (roughly) inside the gradient circle while making
# it big enough to read as the dominant visual element.
_EMOJI_FRACTION = 0.75

# Fraction of the canvas occupied by the circle (centered). 0.97 keeps a tiny
# margin so the gradient doesn't bleed into the very corner pixels.
_CIRCLE_FRACTION = 0.97

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("coverart")


# ---------------------------------------------------------------------------
# Sidecar JSON → (emoji, seed)
# ---------------------------------------------------------------------------

def _load_metadata(mp3: Path) -> tuple[str, str]:
    """Return (emoji, seed) for the given MP3.

    ``emoji`` is what we render; we prefer the sidecar JSON's
    ``notebook_emoji`` (captured by scraper.py from NotebookLM's auto-icon)
    and fall back to ``DEFAULT_EMOJI`` for anything else (manual MP3s,
    legacy episodes, etc.).

    ``seed`` is what we hash to pick stable per-episode gradient hues — the
    title + emoji combination, or the filename if no JSON is present. Same
    inputs always produce the same colours, but each episode gets its own.
    """
    json_path = mp3.with_suffix(".json")
    emoji = ""
    title = ""
    if json_path.exists():
        try:
            meta = json.loads(json_path.read_text(encoding="utf-8"))
            emoji = (meta.get("notebook_emoji") or "").strip()
            title = (meta.get("title") or "").strip()
        except Exception as e:  # noqa: BLE001
            log.warning("Could not parse %s: %s", json_path.name, e)
    return emoji or DEFAULT_EMOJI, (title or mp3.stem)


# ---------------------------------------------------------------------------
# Drawing primitives
# ---------------------------------------------------------------------------

def _stable_hues(seed: str) -> tuple[float, float]:
    """Two distinct hue values in [0, 1) derived from a stable hash of seed."""
    digest = hashlib.md5(seed.encode("utf-8")).digest()
    h1 = digest[0] / 255.0
    # Offset second hue by ~80°–160° around the wheel so the gradient always
    # has visible variation (rather than two near-identical tones).
    spread = 80 + (digest[1] % 80)  # 80..159
    h2 = ((digest[0] + spread) % 256) / 255.0
    return h1, h2


def _hsv_rgb(h: float, s: float, v: float) -> tuple[int, int, int]:
    r, g, b = colorsys.hsv_to_rgb(h, s, v)
    return int(r * 255), int(g * 255), int(b * 255)


def _build_gradient(size: int, color_a: tuple[int, int, int],
                    color_b: tuple[int, int, int]) -> Image.Image:
    """Diagonal (top-left → bottom-right) linear gradient of size×size."""
    # Build a single-pixel-wide ramp, then rotate+resize to the full square.
    # Pure-Python putpixel over 1.96M pixels is slow; this is ~instant.
    ramp = Image.new("RGB", (size, 1))
    for x in range(size):
        t = x / max(size - 1, 1)
        ramp.putpixel((x, 0), (
            int(color_a[0] * (1 - t) + color_b[0] * t),
            int(color_a[1] * (1 - t) + color_b[1] * t),
            int(color_a[2] * (1 - t) + color_b[2] * t),
        ))
    horizontal = ramp.resize((size, size))
    # Rotate 45° around the centre and crop back to size so the gradient
    # runs corner-to-corner. We expand first to avoid black wedge edges,
    # then centre-crop.
    rotated = horizontal.rotate(45, resample=Image.BICUBIC, expand=True)
    rw, rh = rotated.size
    left = (rw - size) // 2
    top = (rh - size) // 2
    return rotated.crop((left, top, left + size, top + size))


def _build_circle_mask(size: int) -> Image.Image:
    inset = int(size * (1 - _CIRCLE_FRACTION) / 2)
    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    draw.ellipse((inset, inset, size - inset - 1, size - inset - 1), fill=255)
    return mask


def _render_emoji(emoji: str, target_px: int) -> Image.Image:
    """Render `emoji` into a transparent RGBA image `target_px` square.

    Apple Color Emoji's largest sbix strike is 160 px and the font reports
    ascent=160 / descent=50, so Pillow's `anchor="mm"` aims at the middle
    of the *line box* (y≈105) rather than the middle of the glyph (y≈80).
    With a tight render canvas this clips the top of the bitmap by ~25 px
    in native scale — magnified into a very visible bite at 1400² output.

    To avoid both clipping and font-metric centering bugs we:

    1. render onto a generously oversized canvas so nothing can spill off,
    2. crop to the actual non-transparent bbox of the glyph,
    3. pad to a square so subsequent scaling stays proportional,
    4. Lanczos-upscale to `target_px`,
    5. apply a gentle unsharp mask to recover edge crispness lost in the
       5–6× upscale from the 160-px source.
    """
    try:
        font = ImageFont.truetype(APPLE_EMOJI_FONT, _APPLE_EMOJI_SBIX_SIZE)
    except OSError as e:
        raise RuntimeError(
            f"Could not load Apple Color Emoji from {APPLE_EMOJI_FONT}. "
            "Set APPLE_EMOJI_FONT in .env to point at a sbix/COLR-bearing font."
        ) from e

    # 2× the strike size is generous enough for any ZWJ sequence or
    # ascender overhang we've seen.
    pad = _APPLE_EMOJI_SBIX_SIZE * 2
    canvas = Image.new("RGBA", (pad, pad), (0, 0, 0, 0))
    ImageDraw.Draw(canvas).text(
        (pad / 2, pad / 2), emoji,
        font=font, embedded_color=True, anchor="mm",
    )

    bbox = canvas.getbbox()
    if bbox is None:
        # Empty/unsupported glyph — return a fully-transparent square so
        # the rest of the pipeline keeps working (we'll just paint a
        # gradient circle with no symbol on top).
        return Image.new("RGBA", (target_px, target_px), (0, 0, 0, 0))

    cropped = canvas.crop(bbox)
    cw, ch = cropped.size
    side = max(cw, ch)
    square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    square.paste(cropped, ((side - cw) // 2, (side - ch) // 2), cropped)

    big = square.resize((target_px, target_px), Image.LANCZOS)
    # Very gentle unsharp to reclaim a touch of edge crispness without
    # amplifying the JPEG-ish artefacts baked into Apple's sbix PNGs
    # (radius>1 or percent>40 makes high-contrast outlines visibly noisy).
    big = big.filter(ImageFilter.UnsharpMask(radius=0.8, percent=35, threshold=4))
    return big


# ---------------------------------------------------------------------------
# Per-file driver
# ---------------------------------------------------------------------------

def generate_one(mp3: Path, *, force: bool = False) -> Optional[Path]:
    """Render <basename>.png next to mp3. Returns the PNG path or None when
    a cover already exists and force=False."""
    png_path = mp3.with_suffix(".png")
    if png_path.exists() and not force:
        log.info("Cover already exists for %s; skipping.", mp3.name)
        return None

    emoji, seed = _load_metadata(mp3)
    log.info(
        "Rendering cover for %s (emoji=%r, %d×%d)...",
        mp3.name, emoji, COVER_SIZE, COVER_SIZE,
    )

    h1, h2 = _stable_hues(seed)
    color_a = _hsv_rgb(h1, 0.62, 0.88)
    color_b = _hsv_rgb(h2, 0.55, 0.55)

    canvas = Image.new("RGB", (COVER_SIZE, COVER_SIZE), (250, 250, 252))
    gradient = _build_gradient(COVER_SIZE, color_a, color_b)
    mask = _build_circle_mask(COVER_SIZE)
    canvas.paste(gradient, (0, 0), mask)

    emoji_px = int(COVER_SIZE * _EMOJI_FRACTION)
    emoji_img = _render_emoji(emoji, emoji_px)
    offset = (COVER_SIZE - emoji_px) // 2
    canvas.paste(emoji_img, (offset, offset), emoji_img)

    # Write via a hidden partial file + atomic rename so a crash mid-write
    # can't leave behind a half-flushed <hash>.png that the idempotency
    # check would later treat as finished.
    tmp = png_path.with_name(f".{png_path.name}.partial")
    canvas.save(tmp, "PNG", optimize=True)
    tmp.replace(png_path)
    log.info("Wrote %s (%d bytes).", png_path.name, png_path.stat().st_size)
    return png_path


def cover_missing(output_dir: Path) -> tuple[int, int]:
    """Find every *.mp3 in output_dir lacking a matching *.png and render one.
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
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
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
