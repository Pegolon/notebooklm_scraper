#!/usr/bin/env python3
"""
FastAPI cloud app: serves a podcast RSS feed and streams MP3s from OUTPUT_DIR.

The same OUTPUT_DIR the local scraper writes into (typically a Google Drive
folder synced down to this machine) is read here. Episodes are discovered by
scanning *.mp3 files; if a sibling <basename>.json exists (written by the
scraper) its metadata is used, otherwise a minimal record is synthesised from
the filename and mtime — so dropping a bare audio.mp3 into the folder still
gets picked up.

Endpoints:
  GET /                    → tiny human-readable index
  GET /feed.xml            → RSS 2.0 + iTunes + Podcasting 2.0 feed (per request)
  GET /audio/{name}        → streams an MP3 from OUTPUT_DIR, with HTTP Range
                             support (required by Apple Podcasts for seeking)
  GET /transcripts/{name}  → serves a WebVTT transcript from OUTPUT_DIR.
                             Referenced from each item via <podcast:transcript>.

Run:
  uv sync
  uv run uvicorn app:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import Iterable, Optional
from xml.etree import ElementTree as ET

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
load_dotenv(SCRIPT_DIR / ".env")


def _clean_path_value(raw: str) -> str:
    r"""Strip wrapping quotes and undo shell-style escapes from an env path value.

    Users naturally write ``OUTPUT_DIR=/path/with\ spaces/x`` or
    ``="/path with spaces/x"``, but python-dotenv reads values literally.
    Normalize both styles to a real path.
    """
    s = raw.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        s = s[1:-1]
    return s.replace("\\ ", " ").replace('\\"', '"').replace("\\'", "'")


OUTPUT_DIR = (
    Path(_clean_path_value(os.environ["OUTPUT_DIR"])).expanduser()
    if os.environ.get("OUTPUT_DIR")
    else None
)
# Public base URL of THIS FastAPI app (no trailing slash). Used to build
# enclosure URLs (→ /audio/<name>) and the atom self-link (→ /feed.xml).
FEED_BASE_URL = os.environ.get("FEED_BASE_URL", "").strip().rstrip("/")

FEED_TITLE = os.environ.get("FEED_TITLE", "NotebookLM Audio Overviews").strip()
FEED_DESCRIPTION = os.environ.get(
    "FEED_DESCRIPTION",
    "Personal podcast feed of Google NotebookLM Audio Overviews.",
).strip()
FEED_AUTHOR = os.environ.get("FEED_AUTHOR", "NotebookLM").strip()
FEED_OWNER_EMAIL = os.environ.get("FEED_OWNER_EMAIL", "").strip()
FEED_LANGUAGE = os.environ.get("FEED_LANGUAGE", "en-us").strip()
FEED_LINK = os.environ.get("FEED_LINK", "").strip()
FEED_IMAGE_URL = os.environ.get("FEED_IMAGE_URL", "").strip()
FEED_CATEGORY = os.environ.get("FEED_CATEGORY", "Technology").strip()

ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"
ATOM_NS = "http://www.w3.org/2005/Atom"
PODCAST_NS = "https://podcastindex.org/namespace/1.0"
ET.register_namespace("itunes", ITUNES_NS)
ET.register_namespace("atom", ATOM_NS)
ET.register_namespace("podcast", PODCAST_NS)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("cloud")


# ---------------------------------------------------------------------------
# Episode discovery (same shape as the old feed.py)
# ---------------------------------------------------------------------------

def _synthesize_metadata(mp3: Path) -> dict:
    """Build a minimal episode record from a bare MP3 file (no JSON sidecar)."""
    stat = mp3.stat()
    mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
    guid = hashlib.md5(mp3.name.encode("utf-8")).hexdigest()
    title = re.sub(r"[\-_]+", " ", mp3.stem).strip() or mp3.stem
    return {
        "id": guid,
        "title": title,
        "description": f"Audio file: {mp3.name}",
        "audio_file": mp3.name,
        "pub_date": mtime.isoformat(),
    }


def load_episodes(output_dir: Path) -> list[dict]:
    """Return episode dicts (newest first), one per *.mp3 in output_dir."""
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
        meta["audio_file"] = mp3.name
        st = mp3.stat()
        meta["_size"] = st.st_size
        meta["_mtime"] = st.st_mtime
        vtt = mp3.with_suffix(".vtt")
        meta["_transcript_file"] = vtt.name if vtt.exists() else None
        out.append(meta)
    out.sort(key=lambda m: m.get("pub_date") or "", reverse=True)
    return out


# ---------------------------------------------------------------------------
# Feed XML builder
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
    ET.SubElement(channel, "generator").text = "notebooklm_scraper/cloud"
    ET.SubElement(channel, "lastBuildDate").text = format_datetime(
        datetime.now(timezone.utc)
    )

    feed_self_url = f"{base_url}/feed.xml"
    atom = ET.SubElement(channel, f"{{{ATOM_NS}}}link")
    atom.set("href", feed_self_url)
    atom.set("rel", "self")
    atom.set("type", "application/rss+xml")

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
        enclosure.set("url", f"{base_url}/audio/{ep['audio_file']}")
        enclosure.set("length", str(ep["_size"]))
        enclosure.set("type", "audio/mpeg")

        # Podcasting 2.0 transcript tag — points clients at the WebVTT file
        # alongside the MP3 (written by local/transcribe.py).
        if ep.get("_transcript_file"):
            transcript = ET.SubElement(item, f"{{{PODCAST_NS}}}transcript")
            transcript.set("url", f"{base_url}/transcripts/{ep['_transcript_file']}")
            transcript.set("type", "text/vtt")
            transcript.set("lang", FEED_LANGUAGE.split("-")[0] or "en")

        ET.SubElement(item, f"{{{ITUNES_NS}}}author").text = FEED_AUTHOR
        if desc:
            ET.SubElement(item, f"{{{ITUNES_NS}}}summary").text = desc
        ET.SubElement(item, f"{{{ITUNES_NS}}}explicit").text = "false"

    ET.indent(rss, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(
        rss, encoding="unicode", short_empty_elements=False
    )


# ---------------------------------------------------------------------------
# Range-aware audio streaming
# ---------------------------------------------------------------------------

_RANGE_RE = re.compile(r"^\s*bytes=(\d*)-(\d*)\s*$")
_AUDIO_CHUNK_SIZE = 1024 * 1024  # 1 MiB


def _iter_file(path: Path, start: int, end: int) -> Iterable[bytes]:
    """Yield bytes from path in [start, end] inclusive, in chunks."""
    remaining = end - start + 1
    with open(path, "rb") as f:
        f.seek(start)
        while remaining > 0:
            data = f.read(min(_AUDIO_CHUNK_SIZE, remaining))
            if not data:
                break
            remaining -= len(data)
            yield data


def _safe_resolve(name: str, suffix: str) -> Path:
    """Resolve `name` inside OUTPUT_DIR, rejecting traversal and wrong suffixes.
    Raises HTTPException(404) on any rejection."""
    if OUTPUT_DIR is None:
        raise HTTPException(status_code=500, detail="OUTPUT_DIR is not configured.")
    # Reject anything other than a plain filename — no slashes, no leading dot.
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise HTTPException(status_code=404, detail="Not found.")
    if not name.lower().endswith(suffix):
        raise HTTPException(status_code=404, detail="Not found.")
    candidate = (OUTPUT_DIR / name).resolve()
    try:
        candidate.relative_to(OUTPUT_DIR.resolve())
    except ValueError:
        raise HTTPException(status_code=404, detail="Not found.") from None
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail="Not found.")
    return candidate


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="NotebookLM Podcast", docs_url=None, redoc_url=None)


@app.on_event("startup")
def _log_config() -> None:
    log.info("OUTPUT_DIR = %s", OUTPUT_DIR)
    log.info("FEED_BASE_URL = %s", FEED_BASE_URL or "(unset!)")
    if OUTPUT_DIR is None or not FEED_BASE_URL:
        log.warning(
            "Both OUTPUT_DIR and FEED_BASE_URL must be set for the feed to work."
        )


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (
        "<!doctype html><meta charset=utf-8><title>NotebookLM Podcast</title>"
        "<h1>NotebookLM Podcast</h1>"
        "<p>Subscribe in your podcast client to <a href=\"/feed.xml\">/feed.xml</a>.</p>"
    )


@app.api_route("/feed.xml", methods=["GET", "HEAD"])
def serve_feed(request: Request) -> Response:
    if OUTPUT_DIR is None:
        raise HTTPException(status_code=500, detail="OUTPUT_DIR is not configured.")
    if not FEED_BASE_URL:
        raise HTTPException(status_code=500, detail="FEED_BASE_URL is not configured.")
    if not OUTPUT_DIR.exists():
        raise HTTPException(status_code=500, detail=f"OUTPUT_DIR does not exist: {OUTPUT_DIR}")
    episodes = load_episodes(OUTPUT_DIR)
    xml = build_feed(episodes, FEED_BASE_URL)
    body = b"" if request.method == "HEAD" else xml.encode("utf-8")
    return Response(
        content=body,
        media_type="application/rss+xml; charset=utf-8",
        headers={
            "Cache-Control": "public, max-age=60",
            "Content-Length": str(len(xml.encode("utf-8"))),
        },
    )


@app.api_route("/audio/{filename}", methods=["GET", "HEAD"])
def serve_audio(filename: str, request: Request) -> Response:
    """Stream an MP3 from OUTPUT_DIR with HTTP Range support.

    Range support is required by Apple Podcasts and most podcast clients to
    seek inside an episode. We honour single-range requests; multi-range is
    rare in podcast clients and rejected with 416.
    """
    path = _safe_resolve(filename, ".mp3")
    file_size = path.stat().st_size
    last_modified = format_datetime(
        datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    )
    etag = f'"{hashlib.md5(f"{path.name}-{file_size}-{path.stat().st_mtime}".encode()).hexdigest()}"'

    is_head = request.method == "HEAD"
    range_header = request.headers.get("range") or request.headers.get("Range")
    if range_header:
        m = _RANGE_RE.match(range_header)
        if not m or "," in range_header:
            raise HTTPException(
                status_code=416,
                detail="Invalid or unsupported Range header.",
                headers={"Content-Range": f"bytes */{file_size}"},
            )
        start_s, end_s = m.group(1), m.group(2)
        if start_s == "" and end_s == "":
            raise HTTPException(status_code=416, headers={"Content-Range": f"bytes */{file_size}"})
        if start_s == "":
            # Suffix range: last N bytes.
            suffix = int(end_s)
            if suffix == 0:
                raise HTTPException(status_code=416, headers={"Content-Range": f"bytes */{file_size}"})
            start = max(0, file_size - suffix)
            end = file_size - 1
        else:
            start = int(start_s)
            end = int(end_s) if end_s else file_size - 1
            end = min(end, file_size - 1)
        if start >= file_size or start > end:
            raise HTTPException(status_code=416, headers={"Content-Range": f"bytes */{file_size}"})

        headers = {
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(end - start + 1),
            "Last-Modified": last_modified,
            "ETag": etag,
            "Cache-Control": "public, max-age=3600",
        }
        if is_head:
            return Response(status_code=206, headers=headers, media_type="audio/mpeg")
        return StreamingResponse(
            _iter_file(path, start, end),
            status_code=206,
            headers=headers,
            media_type="audio/mpeg",
        )

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(file_size),
        "Last-Modified": last_modified,
        "ETag": etag,
        "Cache-Control": "public, max-age=3600",
    }
    if is_head:
        return Response(headers=headers, media_type="audio/mpeg")
    return StreamingResponse(
        _iter_file(path, 0, file_size - 1),
        headers=headers,
        media_type="audio/mpeg",
    )


@app.api_route("/transcripts/{filename}", methods=["GET", "HEAD"])
def serve_transcript(filename: str, request: Request) -> Response:
    """Serve a WebVTT transcript from OUTPUT_DIR.

    Referenced by the feed's <podcast:transcript> tag. Transcripts are small
    (tens to hundreds of KB) so we read the whole file rather than streaming.
    """
    path = _safe_resolve(filename, ".vtt")
    stat = path.stat()
    last_modified = format_datetime(
        datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
    )
    etag = f'"{hashlib.md5(f"{path.name}-{stat.st_size}-{stat.st_mtime}".encode()).hexdigest()}"'
    headers = {
        "Content-Length": str(stat.st_size),
        "Last-Modified": last_modified,
        "ETag": etag,
        "Cache-Control": "public, max-age=3600",
    }
    body = b"" if request.method == "HEAD" else path.read_bytes()
    return Response(
        content=body,
        media_type="text/vtt; charset=utf-8",
        headers=headers,
    )
