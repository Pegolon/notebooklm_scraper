#!/usr/bin/env python3
"""
Generate a podcast RSS feed (RSS 2.0 + iTunes namespace) from a folder of
MP3 episodes — both NotebookLM scrapes and ad-hoc files you drop in by hand.

  uv run feed.py            # write feed.xml into OUTPUT_DIR
  uv run feed.py --stdout   # print the feed to stdout instead

Behaviour:
  * Iterates every *.mp3 in OUTPUT_DIR.
  * If <basename>.json exists alongside it, uses that metadata (the scraper
    writes these).
  * If the MP3 stands alone, synthesises a minimal episode from the filename
    and file mtime — so you can just drop random.mp3 into the folder and it
    appears in the feed.
  * Sorts episodes newest-first by pub_date.
  * Writes <FEED_FILE> (default feed.xml) alongside the episodes.

Audio enclosure URLs are constructed as f"{FEED_BASE_URL}/{audio_file}".
You are responsible for serving OUTPUT_DIR publicly under FEED_BASE_URL
(nginx, caddy, rclone-to-S3, Cloudflare Tunnel, …). This script is intended
to be deployed wherever the folder is served from — typically alongside the
hosting, not on the scraping machine.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree as ET

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
FEED_BASE_URL = os.environ.get("FEED_BASE_URL", "").strip().rstrip("/")
FEED_FILE = os.environ.get("FEED_FILE", "feed.xml").strip()
FEED_TITLE = os.environ.get("FEED_TITLE", "NotebookLM Audio Overviews").strip()
FEED_DESCRIPTION = os.environ.get(
    "FEED_DESCRIPTION",
    "Personal podcast feed of Google NotebookLM Audio Overviews.",
).strip()
FEED_AUTHOR = os.environ.get("FEED_AUTHOR", "NotebookLM").strip()
FEED_OWNER_EMAIL = os.environ.get("FEED_OWNER_EMAIL", "").strip()
FEED_LANGUAGE = os.environ.get("FEED_LANGUAGE", "en-us").strip()
FEED_LINK = os.environ.get("FEED_LINK", "").strip()  # optional public site URL
FEED_IMAGE_URL = os.environ.get("FEED_IMAGE_URL", "").strip()
FEED_CATEGORY = os.environ.get("FEED_CATEGORY", "Technology").strip()

ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"
ATOM_NS = "http://www.w3.org/2005/Atom"
# Register prefixes globally so the serializer emits `itunes:` / `atom:`
# instead of auto-generated ns0:/ns1: prefixes.
ET.register_namespace("itunes", ITUNES_NS)
ET.register_namespace("atom", ATOM_NS)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("feed")


# ---------------------------------------------------------------------------
# Load episode metadata
# ---------------------------------------------------------------------------

def _synthesize_metadata(mp3: Path) -> dict:
    """Build a minimal episode record from a bare MP3 file (no JSON sidecar).
    Used when the user drops random audio into OUTPUT_DIR."""
    stat = mp3.stat()
    mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
    # Stable GUID derived from the filename so podcast clients don't see
    # "new" episodes whenever the script reruns.
    guid = hashlib.md5(mp3.name.encode("utf-8")).hexdigest()
    # Prettify the filename: strip extension, swap separators for spaces.
    title = re.sub(r"[\-_]+", " ", mp3.stem).strip() or mp3.stem
    return {
        "id": guid,
        "title": title,
        "description": f"Audio file: {mp3.name}",
        "audio_file": mp3.name,
        "pub_date": mtime.isoformat(),
    }


def load_episodes(output_dir: Path) -> list[dict]:
    """Return episode dicts (newest first), one per *.mp3 in OUTPUT_DIR.
    Uses the matching <basename>.json metadata when present, otherwise
    synthesises a minimal record from the filename + mtime."""
    out: list[dict] = []
    for mp3 in output_dir.glob("*.mp3"):
        sidecar = mp3.with_suffix(".json")
        meta: Optional[dict] = None
        if sidecar.exists():
            try:
                meta = json.loads(sidecar.read_text(encoding="utf-8"))
            except Exception as e:  # noqa: BLE001
                log.warning("Sidecar %s unreadable (%s); synthesising.", sidecar.name, e)
                meta = None
        if meta is None:
            meta = _synthesize_metadata(mp3)
            log.info("Synthesised metadata for %s.", mp3.name)
        # Always trust the actual file on disk for the enclosure target.
        meta["audio_file"] = mp3.name
        st = mp3.stat()
        meta["_size"] = st.st_size
        meta["_mtime"] = st.st_mtime
        out.append(meta)
    out.sort(key=lambda m: m.get("pub_date") or "", reverse=True)
    return out


# ---------------------------------------------------------------------------
# Build the XML
# ---------------------------------------------------------------------------

def _to_rfc2822(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return format_datetime(dt)
    except Exception:
        return format_datetime(datetime.now(timezone.utc))


def build_feed(episodes: list[dict], base_url: str) -> str:
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")

    ET.SubElement(channel, "title").text = FEED_TITLE
    ET.SubElement(channel, "description").text = FEED_DESCRIPTION
    ET.SubElement(channel, "language").text = FEED_LANGUAGE
    ET.SubElement(channel, "link").text = FEED_LINK or base_url
    ET.SubElement(channel, "generator").text = "notebooklm_scraper"
    ET.SubElement(channel, "lastBuildDate").text = format_datetime(
        datetime.now(timezone.utc)
    )

    # atom:link self-reference (required by many podcast directories).
    feed_self_url = f"{base_url}/{FEED_FILE}"
    atom = ET.SubElement(channel, f"{{{ATOM_NS}}}link")
    atom.set("href", feed_self_url)
    atom.set("rel", "self")
    atom.set("type", "application/rss+xml")

    # iTunes channel-level metadata.
    ET.SubElement(channel, f"{{{ITUNES_NS}}}author").text = FEED_AUTHOR
    ET.SubElement(channel, f"{{{ITUNES_NS}}}summary").text = FEED_DESCRIPTION
    ET.SubElement(channel, f"{{{ITUNES_NS}}}explicit").text = "false"
    ET.SubElement(channel, f"{{{ITUNES_NS}}}category").set("text", FEED_CATEGORY)
    owner = ET.SubElement(channel, f"{{{ITUNES_NS}}}owner")
    ET.SubElement(owner, f"{{{ITUNES_NS}}}name").text = FEED_AUTHOR
    if FEED_OWNER_EMAIL:
        ET.SubElement(owner, f"{{{ITUNES_NS}}}email").text = FEED_OWNER_EMAIL
    if FEED_IMAGE_URL:
        img = ET.SubElement(channel, "image")
        ET.SubElement(img, "url").text = FEED_IMAGE_URL
        ET.SubElement(img, "title").text = FEED_TITLE
        ET.SubElement(img, "link").text = FEED_LINK or base_url
        itunes_img = ET.SubElement(channel, f"{{{ITUNES_NS}}}image")
        itunes_img.set("href", FEED_IMAGE_URL)

    for ep in episodes:
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = ep.get("title", "Untitled")
        desc = ep.get("description", "")
        ET.SubElement(item, "description").text = desc
        guid = ET.SubElement(item, "guid", {"isPermaLink": "false"})
        guid.text = ep.get("id", "")
        if ep.get("pub_date"):
            ET.SubElement(item, "pubDate").text = _to_rfc2822(ep["pub_date"])

        enclosure = ET.SubElement(item, "enclosure")
        enclosure.set("url", f"{base_url}/{ep['audio_file']}")
        enclosure.set("length", str(ep["_size"]))
        enclosure.set("type", "audio/mpeg")

        ET.SubElement(item, f"{{{ITUNES_NS}}}author").text = FEED_AUTHOR
        if desc:
            ET.SubElement(item, f"{{{ITUNES_NS}}}summary").text = desc
        ET.SubElement(item, f"{{{ITUNES_NS}}}explicit").text = "false"

    ET.indent(rss, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(
        rss, encoding="unicode", short_empty_elements=False
    )


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def write_feed(output_dir: Path, base_url: str) -> Path:
    """Build the feed and write it to OUTPUT_DIR/FEED_FILE. Returns the path."""
    episodes = load_episodes(output_dir)
    log.info("Loaded %d episode(s).", len(episodes))
    xml = build_feed(episodes, base_url)
    out_path = output_dir / FEED_FILE
    out_path.write_text(xml, encoding="utf-8")
    log.info("Wrote %s (%d bytes, %d episodes).", out_path, len(xml), len(episodes))
    return out_path


def _require_config() -> None:
    if OUTPUT_DIR is None:
        log.error("OUTPUT_DIR is not set (configure it in .env).")
        sys.exit(2)
    if not FEED_BASE_URL:
        log.error(
            "FEED_BASE_URL is not set. Configure it in .env to the public URL\n"
            "  that serves OUTPUT_DIR (e.g. https://podcasts.example.com)."
        )
        sys.exit(2)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--stdout", action="store_true", help="Print the feed instead of writing to disk.")
    args = parser.parse_args()

    _require_config()
    assert OUTPUT_DIR is not None
    if args.stdout:
        episodes = load_episodes(OUTPUT_DIR)
        sys.stdout.write(build_feed(episodes, FEED_BASE_URL))
    else:
        write_feed(OUTPUT_DIR, FEED_BASE_URL)
    return 0


if __name__ == "__main__":
    sys.exit(main())
