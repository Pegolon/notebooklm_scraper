#!/usr/bin/env python3
"""
FastAPI cloud app: serves a podcast RSS feed and streams M4As from OUTPUT_DIR.

The same OUTPUT_DIR the local scraper writes into (typically a Google Drive
folder synced down to this machine) is read here. Episodes are discovered by
scanning *.m4a files; if a sibling <basename>.json exists (written by the
scraper) its metadata is used, otherwise a minimal record is synthesised from
the filename and mtime — so dropping a bare audio.m4a into the folder still
gets picked up.

Endpoints:
  GET /                    → tiny human-readable index
  GET /feed.xml            → RSS 2.0 + iTunes + Podcasting 2.0 feed (per request)
  GET /audio/{name}        → streams an M4A from OUTPUT_DIR, with HTTP Range
                             support (required by Apple Podcasts for seeking)
  GET /transcripts/{name}  → serves a WebVTT transcript from OUTPUT_DIR.
                             Referenced from each item via <podcast:transcript>.

Run:
  uv sync
  uv run uvicorn app:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

from collections import defaultdict
import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import AsyncIterable, Iterable, Optional
from xml.etree import ElementTree as ET

import anyio

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

# Feed in-memory caching state
import threading

_feed_cache: Optional[str] = None
_feed_cache_mtime: float = 0.0
_feed_cache_file_count: int = -1
_feed_cache_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Episode discovery (same shape as the old feed.py)
# ---------------------------------------------------------------------------

def _synthesize_metadata(m4a: Path) -> dict:
    """Build a minimal episode record from a bare M4A file (no JSON sidecar)."""
    stat = m4a.stat()
    mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
    guid = hashlib.md5(m4a.name.encode("utf-8")).hexdigest()
    title = re.sub(r"[\-_]+", " ", m4a.stem).strip() or m4a.stem
    return {
        "id": guid,
        "title": title,
        "description": f"Audio file: {m4a.name}",
        "audio_file": m4a.name,
        "pub_date": mtime.isoformat(),
    }


def load_episodes(output_dir: Path) -> list[dict]:
    """Return episode dicts (newest first), one per *.m4a in output_dir."""
    out: list[dict] = []
    for m4a in output_dir.glob("*.m4a"):
        sidecar = m4a.with_suffix(".json")
        meta: Optional[dict] = None
        if sidecar.exists():
            try:
                meta = json.loads(sidecar.read_text(encoding="utf-8"))
            except Exception as e:  # noqa: BLE001
                log.warning("Sidecar %s unreadable (%s); synthesising.", sidecar.name, e)
                meta = None
        if meta is None:
            meta = _synthesize_metadata(m4a)
        meta["audio_file"] = m4a.name
        st = m4a.stat()
        meta["_size"] = st.st_size
        meta["_mtime"] = st.st_mtime
        vtt = m4a.with_suffix(".vtt")
        meta["_transcript_file"] = vtt.name if vtt.exists() else None
        png = m4a.with_suffix(".png")
        meta["_image_file"] = png.name if png.exists() else None
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
        enclosure.set("type", "audio/x-m4a")

        # Podcasting 2.0 transcript tag — points clients at the WebVTT file
        # alongside the MP3 (written by local/transcribe.py).
        if ep.get("_transcript_file"):
            transcript = ET.SubElement(item, f"{{{PODCAST_NS}}}transcript")
            transcript.set("url", f"{base_url}/transcripts/{ep['_transcript_file']}")
            transcript.set("type", "text/vtt")
            transcript.set("lang", FEED_LANGUAGE.split("-")[0] or "en")

        # Per-episode cover art (written by local/coverart.py). Falls back to
        # the channel-level FEED_IMAGE_URL in clients that don't see an
        # item-level <itunes:image>.
        if ep.get("_image_file"):
            ep_img = ET.SubElement(item, f"{{{ITUNES_NS}}}image")
            ep_img.set("href", f"{base_url}/images/{ep['_image_file']}")

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
_AUDIO_CHUNK_SIZE = 64 * 1024  # 64 KiB


async def _iter_file(path: Path, start: int, end: int) -> AsyncIterable[bytes]:
    """Yield bytes from path in [start, end] inclusive, in chunks."""
    remaining = end - start + 1
    async with await anyio.open_file(path, "rb") as f:
        await f.seek(start)
        while remaining > 0:
            data = await f.read(min(_AUDIO_CHUNK_SIZE, remaining))
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


# Dynamic IP blocking configuration
BANNED_IPS: dict[str, float] = {}  # client_ip -> ban_expires_timestamp
BAD_REQUESTS: dict[str, list[float]] = defaultdict(list)  # client_ip -> list of timestamps

BAN_DURATION = 86400  # Ban for 24 hours
MAX_BAD_REQUESTS = 3   # Ban after 3 attempts
WINDOW_SECONDS = 300   # 5-minute tracking window

# Patterns indicating malicious vulnerability scanning
SCAN_PATTERNS = [
    re.compile(r"\.env", re.IGNORECASE),
    re.compile(r"\.git", re.IGNORECASE),
    re.compile(r"\.php", re.IGNORECASE),
    re.compile(r"wp-", re.IGNORECASE),
    re.compile(r"config", re.IGNORECASE),
    re.compile(r"phpinfo", re.IGNORECASE),
    re.compile(r"xmlrpc", re.IGNORECASE),
    re.compile(r"backup", re.IGNORECASE),
]


def get_client_ip(request: Request) -> str:
    """Extract client IP, respecting proxy headers if present."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()
    return request.client.host if request.client else "unknown"


def is_legitimate_route(path: str) -> bool:
    """Check if the path matches one of our defined endpoints to prevent false positives."""
    if path in ("/", "/feed.xml"):
        return True
    for prefix in ("/audio/", "/images/", "/transcripts/"):
        if path.startswith(prefix):
            return True
    return False


@app.middleware("http")
async def block_scanners_middleware(request: Request, call_next):
    client_ip = get_client_ip(request)
    now = time.time()

    # 1. Check if IP is currently banned
    if client_ip in BANNED_IPS:
        if now < BANNED_IPS[client_ip]:
            # Silent 403 response for banned IPs
            return Response(status_code=403, content="Forbidden")
        else:
            # Ban expired
            del BANNED_IPS[client_ip]

    # 2. Inspect non-legitimate routes for malicious scanning patterns
    path = request.url.path
    if not is_legitimate_route(path):
        is_scan = any(pattern.search(path) for pattern in SCAN_PATTERNS)
        if is_scan:
            log.warning("Malicious scan detected: IP %s -> PATH %s", client_ip, path)

            # Record bad request
            BAD_REQUESTS[client_ip].append(now)
            # Prune expired records
            BAD_REQUESTS[client_ip] = [t for t in BAD_REQUESTS[client_ip] if now - t <= WINDOW_SECONDS]

            # Ban if threshold exceeded
            if len(BAD_REQUESTS[client_ip]) >= MAX_BAD_REQUESTS:
                BANNED_IPS[client_ip] = now + BAN_DURATION
                log.error("IP %s has been banned for 24h due to excessive scans. Last path: %s", client_ip, path)

            return Response(status_code=404, content="Not Found")

    return await call_next(request)


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

    global _feed_cache, _feed_cache_mtime, _feed_cache_file_count

    try:
        current_mtime = OUTPUT_DIR.stat().st_mtime
        current_file_count = sum(1 for p in OUTPUT_DIR.iterdir() if p.is_file() and p.suffix.lower() == ".m4a")
    except Exception as e:
        log.error("Failed to read directory state of %s: %s. Rebuilding feed dynamically.", OUTPUT_DIR, e)
        episodes = load_episodes(OUTPUT_DIR)
        xml = build_feed(episodes, FEED_BASE_URL)
        body = b"" if request.method == "HEAD" else xml.encode("utf-8")
        return Response(
            content=body,
            media_type="application/rss+xml; charset=utf-8",
            headers={
                "Cache-Control": "public, max-age=60",
                "Content-Length": str(len(body)),
            },
        )

    with _feed_cache_lock:
        if (
            _feed_cache is None
            or _feed_cache_mtime != current_mtime
            or _feed_cache_file_count != current_file_count
        ):
            log.info("Feed cache stale or uninitialized. Rebuilding feed...")
            episodes = load_episodes(OUTPUT_DIR)
            _feed_cache = build_feed(episodes, FEED_BASE_URL)
            _feed_cache_mtime = current_mtime
            _feed_cache_file_count = current_file_count
        else:
            log.info("Serving feed from cache.")

        xml = _feed_cache

    body = b"" if request.method == "HEAD" else xml.encode("utf-8")
    return Response(
        content=body,
        media_type="application/rss+xml; charset=utf-8",
        headers={
            "Cache-Control": "public, max-age=60",
            "Content-Length": str(len(body)),
        },
    )


@app.api_route("/audio/{filename}", methods=["GET", "HEAD"])
async def serve_audio(filename: str, request: Request) -> Response:
    """Stream an M4A from OUTPUT_DIR with HTTP Range support.

    Range support is required by Apple Podcasts and most podcast clients to
    seek inside an episode. We honour single-range requests; multi-range is
    rare in podcast clients and rejected with 416.
    """
    path = _safe_resolve(filename, ".m4a")
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
            return Response(status_code=206, headers=headers, media_type="audio/x-m4a")
        return StreamingResponse(
            _iter_file(path, start, end),
            status_code=206,
            headers=headers,
            media_type="audio/x-m4a",
        )

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(file_size),
        "Last-Modified": last_modified,
        "ETag": etag,
        "Cache-Control": "public, max-age=3600",
    }
    if is_head:
        return Response(headers=headers, media_type="audio/x-m4a")
    return StreamingResponse(
        _iter_file(path, 0, file_size - 1),
        headers=headers,
        media_type="audio/x-m4a",
    )


@app.api_route("/images/{filename}", methods=["GET", "HEAD"])
def serve_image(filename: str, request: Request) -> Response:
    """Serve a PNG cover image from OUTPUT_DIR.

    Used for both per-episode `<itunes:image>` (written by local/coverart.py
    as `<hash>.png`) and the channel-level cover (e.g. `cover.png` dropped
    into OUTPUT_DIR by hand and referenced via FEED_IMAGE_URL).
    """
    path = _safe_resolve(filename, ".png")
    stat = path.stat()
    last_modified = format_datetime(
        datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
    )
    etag = f'"{hashlib.md5(f"{path.name}-{stat.st_size}-{stat.st_mtime}".encode()).hexdigest()}"'
    headers = {
        "Content-Length": str(stat.st_size),
        "Last-Modified": last_modified,
        "ETag": etag,
        "Cache-Control": "public, max-age=86400",
    }
    body = b"" if request.method == "HEAD" else path.read_bytes()
    return Response(content=body, media_type="image/png", headers=headers)


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
