#!/usr/bin/env python3
"""
NotebookLM Audio Overview scraper (Part 1).

Modes:
  uv run scraper.py --login    # one-time, headful, sign into Google manually
  uv run scraper.py            # default: pick newest notebook since last run, scrape its Audio Overview
  uv run scraper.py --list     # debug: list notebooks visible on the home page
  uv run scraper.py --url URL  # override: scrape a specific notebook URL

Writes <md5>.mp3 and <md5>.json into OUTPUT_DIR (a folder inside Google Drive).
Tracks the most recently scraped notebook's modified-date in scraper_state.json
so re-runs only pick up genuinely newer notebooks. Initial cutoff is set by
INITIAL_SINCE_DATE (defaults to 2026-05-01).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from playwright.sync_api import (
    BrowserContext,
    Download,
    Locator,
    Page,
    TimeoutError as PWTimeout,
    sync_playwright,
)

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


NOTEBOOK_URL = os.environ.get("NOTEBOOK_URL", "").strip()  # optional override
OUTPUT_DIR = (
    Path(_clean_path_value(os.environ["OUTPUT_DIR"])).expanduser()
    if os.environ.get("OUTPUT_DIR") else None
)
USER_DATA_DIR = Path(
    _clean_path_value(os.environ.get("USER_DATA_DIR", "")) or str(SCRIPT_DIR / "playwright_profile")
).expanduser()
TITLE_PREFIX = os.environ.get("TITLE_PREFIX", "NotebookLM Overview").strip()
EXTRA_SETTLE_MS = int(os.environ.get("EXTRA_SETTLE_MS", "2500"))
INITIAL_SINCE_DATE = os.environ.get("INITIAL_SINCE_DATE", "2026-05-01").strip()

STATE_PATH = SCRIPT_DIR / "scraper_state.json"
HOME_URL = "https://notebooklm.google.com/"

NAV_TIMEOUT_MS = 60_000
UI_TIMEOUT_MS = 20_000

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("notebooklm")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def normalize_for_hash(text: str) -> str:
    """Collapse whitespace, lowercase — so trivial UI re-renders don't change the hash."""
    return re.sub(r"\s+", " ", text or "").strip().lower()


def md5_of(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def require_output_dir() -> None:
    if OUTPUT_DIR is None:
        log.error("Missing required env var OUTPUT_DIR (set in .env)")
        sys.exit(2)
    log.info("Output dir: %s", OUTPUT_DIR)
    # We're happy to create OUTPUT_DIR itself, but its parent must already exist.
    # Google Drive mounts won't let us mkdir inside non-existent ancestors, and
    # silent failure on a typo'd path is worse than failing fast.
    parent = OUTPUT_DIR.parent
    if not parent.exists():
        log.error(
            "OUTPUT_DIR parent does not exist: %s\n"
            "  Check the path in .env. For Google Drive on macOS the parent is usually\n"
            "  something like /Users/<you>/Library/CloudStorage/GoogleDrive-<email>/My Drive\n"
            "  Note: do not escape spaces with backslashes in .env; just write the path\n"
            "  verbatim, or wrap it in double quotes.",
            parent,
        )
        sys.exit(2)


def launch_context(playwright, headless: bool) -> BrowserContext:
    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    log.info("Using persistent profile: %s", USER_DATA_DIR)
    # Force English locale so selectors and date parsing have a deterministic UI,
    # independent of the signed-in Google account's display language.
    return playwright.chromium.launch_persistent_context(
        user_data_dir=str(USER_DATA_DIR),
        headless=headless,
        viewport={"width": 1440, "height": 900},
        accept_downloads=True,
        locale="en-US",
        extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        args=[
            "--disable-blink-features=AutomationControlled",
            "--lang=en-US",
        ],
    )


def with_english_hint(url: str) -> str:
    """Append ?hl=en (or &hl=en) so Google renders the page in English even when
    the account's display language is different."""
    if "hl=" in url:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}hl=en"


# ---------------------------------------------------------------------------
# State file
# ---------------------------------------------------------------------------

def parse_iso(s: str) -> datetime:
    # Accept bare dates as midnight UTC.
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        s = s + "T00:00:00+00:00"
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            log.warning("scraper_state.json unreadable (%s); starting fresh.", e)
    return {"processed_notebook_ids": []}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def load_processed_ids(state: dict) -> set[str]:
    """Return the set of notebook IDs we've already scraped.
    Combines the state file's list with a scan of OUTPUT_DIR's *.json metadata,
    so deleting scraper_state.json never causes a re-download."""
    ids: set[str] = set(state.get("processed_notebook_ids", []))
    if OUTPUT_DIR and OUTPUT_DIR.exists():
        for jp in OUTPUT_DIR.glob("*.json"):
            try:
                meta = json.loads(jp.read_text(encoding="utf-8"))
            except Exception:
                continue
            nb_id = meta.get("notebook_id")
            if nb_id:
                ids.add(nb_id)
    return ids


def mark_processed(state: dict, nb_id: str) -> None:
    ids = state.setdefault("processed_notebook_ids", [])
    if nb_id not in ids:
        ids.append(nb_id)
    save_state(state)


# ---------------------------------------------------------------------------
# Date parsing for the home page
# ---------------------------------------------------------------------------

_REL_EN_RE = re.compile(
    r"(\d+)\s+(second|minute|hour|day|week|month|year)s?\s+ago", re.I
)
_REL_DE_RE = re.compile(
    r"vor\s+(\d+)\s+(sekunde|minute|stunde|tag|woche|monat|jahr)", re.I
)
_DE_UNIT_DELTA = {
    "sekunde": lambda n: timedelta(seconds=n),
    "minute": lambda n: timedelta(minutes=n),
    "stunde": lambda n: timedelta(hours=n),
    "tag": lambda n: timedelta(days=n),
    "woche": lambda n: timedelta(weeks=n),
    "monat": lambda n: timedelta(days=30 * n),
    "jahr": lambda n: timedelta(days=365 * n),
}
_EN_UNIT_DELTA = {
    "second": lambda n: timedelta(seconds=n),
    "minute": lambda n: timedelta(minutes=n),
    "hour": lambda n: timedelta(hours=n),
    "day": lambda n: timedelta(days=n),
    "week": lambda n: timedelta(weeks=n),
    "month": lambda n: timedelta(days=30 * n),
    "year": lambda n: timedelta(days=365 * n),
}
_DDMMYYYY_RE = re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})$")


def parse_card_date(text: str, now: datetime) -> Optional[datetime]:
    """Parse a date string from a notebook card (relative or absolute, EN or DE)."""
    if not text:
        return None
    t = text.strip()
    low = t.lower()

    # Words.
    if low in ("just now", "now", "moments ago", "jetzt", "soeben", "gerade eben"):
        return now
    if low in ("today", "heute"):
        return now
    if low in ("yesterday", "gestern"):
        return now - timedelta(days=1)

    # English relative.
    m = _REL_EN_RE.search(low)
    if m:
        return now - _EN_UNIT_DELTA[m.group(2).lower()](int(m.group(1)))

    # German relative ("vor 3 Tagen").
    m = _REL_DE_RE.search(low)
    if m:
        return now - _DE_UNIT_DELTA[m.group(2).lower()](int(m.group(1)))

    # German short date: DD.MM.YYYY (e.g. "29.05.2026").
    m = _DDMMYYYY_RE.match(t)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return datetime(y, mo, d, tzinfo=timezone.utc)
        except ValueError:
            pass

    # Absolute formats: "May 28, 2026", "28 May 2026", "2026-05-28", "5/28/2026".
    for fmt in (
        "%b %d, %Y", "%B %d, %Y",
        "%d %b %Y", "%d %B %Y",
        "%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y",
    ):
        try:
            return datetime.strptime(t, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Home page: enumerate notebooks
# ---------------------------------------------------------------------------

NOTEBOOK_HREF_RE = re.compile(r"/notebook/([^/?#]+)")

# JS snippet used to locate a notebook id from any descendant attribute.
# Notebook IDs appear in router-link attributes, data-* attributes, or anchor hrefs.
_FIND_NB_ID_JS = """
(root) => {
  const re = /\\/notebook\\/([^\\/?#"' ]+)/;
  const stack = [root];
  while (stack.length) {
    const el = stack.pop();
    if (el && el.attributes) {
      for (const a of el.attributes) {
        const m = a.value && a.value.match(re);
        if (m) return m[1];
      }
    }
    if (el && el.children) {
      for (const c of el.children) stack.push(c);
    }
  }
  return null;
}
"""


def _try_switch_to_grid(page: Page) -> bool:
    """Best-effort: click any 'grid view' / 'Rasteransicht' toggle so anchors appear.
    Returns True if a click landed."""
    candidates = [
        page.get_by_role("button", name=re.compile(r"\bgrid\b", re.I)),
        page.get_by_role("button", name=re.compile(r"raster", re.I)),
        page.get_by_role("button", name=re.compile(r"kachel", re.I)),
        page.locator('button[aria-label*="grid" i]'),
        page.locator('button[aria-label*="raster" i]'),
        page.locator('button[aria-label*="kachel" i]'),
    ]
    for cand in candidates:
        try:
            btn = cand.first
            if btn.count() > 0:
                btn.click(timeout=2_000)
                page.wait_for_timeout(800)
                log.info("Switched to grid view.")
                return True
        except Exception:
            continue
    return False


def _extract_via_anchors(page: Page) -> list[dict]:
    anchors = page.locator('a[href*="/notebook/"]').all()
    if not anchors:
        return []
    log.info("Found %d notebook anchor(s).", len(anchors))
    now = datetime.now(timezone.utc)
    seen: set[str] = set()
    out: list[dict] = []
    for a in anchors:
        try:
            href = a.get_attribute("href") or ""
        except Exception:
            continue
        m = NOTEBOOK_HREF_RE.search(href)
        if not m:
            continue
        nb_id = m.group(1)
        if nb_id in seen:
            continue
        seen.add(nb_id)

        # Try to grab a meatier ancestor for title+date text.
        card_text = ""
        for xp in (
            'xpath=ancestor::project-button[1]',
            'xpath=ancestor::*[@role="listitem"][1]',
            'xpath=ancestor::*[contains(@class,"project-button") '
            'or contains(@class,"notebook-card") '
            'or contains(@class,"card")][1]',
            'xpath=ancestor::*[self::mat-card or self::article][1]',
        ):
            try:
                anc = a.locator(xp)
                if anc.count() > 0:
                    card_text = anc.first.inner_text(timeout=1_500)
                    if card_text:
                        break
            except Exception:
                continue
        if not card_text:
            try:
                card_text = a.inner_text(timeout=1_500)
            except Exception:
                pass

        title, modified_raw = parse_card_text(card_text)
        modified = parse_card_date(modified_raw, now) if modified_raw else None
        url = href if href.startswith("http") else f"https://notebooklm.google.com{href}"
        out.append({
            "id": nb_id, "url": url,
            "title": title or f"(untitled {nb_id[:8]})",
            "emoji": parse_card_emoji(card_text),
            "modified": modified,
            "modified_raw": modified_raw or "",
        })
    return out


def _extract_via_rows(page: Page) -> list[dict]:
    """Table-view fallback. Rows don't carry <a href>, so we scan each row's
    descendants for any attribute containing a notebook id."""
    row_selectors = [
        '[role="row"]',
        'mat-row, [mat-row]',
        'project-button',
        'tr',
    ]
    rows: list[Locator] = []
    chosen = None
    for sel in row_selectors:
        loc = page.locator(sel)
        cnt = loc.count()
        if cnt >= 1:
            rows = loc.all()
            chosen = sel
            log.info("Table-view rows via %r: %d", sel, cnt)
            break
    if not rows:
        return []

    now = datetime.now(timezone.utc)
    seen: set[str] = set()
    out: list[dict] = []
    for row in rows:
        try:
            nb_id = row.evaluate(_FIND_NB_ID_JS)
        except Exception:
            nb_id = None
        if not nb_id or nb_id in seen:
            continue
        seen.add(nb_id)
        try:
            text = row.inner_text(timeout=1_500)
        except Exception:
            text = ""
        title, modified_raw = parse_card_text(text)
        modified = parse_card_date(modified_raw, now) if modified_raw else None
        out.append({
            "id": nb_id,
            "url": f"https://notebooklm.google.com/notebook/{nb_id}",
            "title": title or f"(untitled {nb_id[:8]})",
            "emoji": parse_card_emoji(text),
            "modified": modified,
            "modified_raw": modified_raw or "",
        })
    log.info("Extracted %d notebook(s) via %s.", len(out), chosen)
    return out


def list_notebooks(page: Page) -> list[dict]:
    """Return [{id, url, title, modified, modified_raw}, ...]."""
    log.info("Loading NotebookLM home page...")
    page.goto(with_english_hint(HOME_URL), wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    try:
        page.wait_for_load_state("networkidle", timeout=NAV_TIMEOUT_MS)
    except PWTimeout:
        log.warning("networkidle not reached on home page; continuing.")
    page.wait_for_timeout(EXTRA_SETTLE_MS)

    if "accounts.google.com" in page.url or "ServiceLogin" in page.url:
        raise RuntimeError("Not authenticated. Run `uv run scraper.py --login` first.")

    # 1) Try grid-view anchors as-is.
    out = _extract_via_anchors(page)
    if out:
        return out

    # 2) Try switching to grid view, then anchors again.
    log.info("No anchors in current view; trying to switch to grid view...")
    if _try_switch_to_grid(page):
        out = _extract_via_anchors(page)
        if out:
            return out

    # 3) Table-view fallback: scan rows for any descendant attribute holding a notebook id.
    log.info("Falling back to table-view row extraction...")
    out = _extract_via_rows(page)
    if out:
        return out

    # 4) Give up: dump the DOM for inspection.
    debug_path = SCRIPT_DIR / "debug_home.html"
    try:
        debug_path.write_text(page.content(), encoding="utf-8")
        log.warning("No notebooks extracted. Saved DOM to %s for debugging.", debug_path)
    except Exception as e:  # noqa: BLE001
        log.warning("No notebooks extracted, and DOM dump failed: %s", e)
    return []


_EMOJI_ONLY_STRIPPER = re.compile(r"[\ufe00-\ufe0f\u200d\u2640-\u2642]")


def _looks_like_real_title(line: str) -> bool:
    """True if `line` looks like an actual notebook title rather than just an
    emoji/icon. Card text usually starts with the notebook's emoji on its own line."""
    stripped = _EMOJI_ONLY_STRIPPER.sub("", line)
    # Require at least 5 chars and at least one ASCII letter.
    return len(stripped) >= 5 and bool(re.search(r"[A-Za-z]", stripped))


def parse_card_emoji(text: str) -> str:
    """Extract the leading emoji icon from a notebook card's text, if present.

    NotebookLM auto-assigns an emoji to every notebook and renders it as the
    first line of each card on the home page (above the title). We capture it
    so cover-art generation can use it as the per-episode visual identity.
    Returns "" if the first line already looks like a title (no separate icon).
    """
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return ""
    first = lines[0]
    stripped = _EMOJI_ONLY_STRIPPER.sub("", first)
    # If the first line has any ASCII alphanumeric content it's the title,
    # not a standalone icon. Some cards just don't have an emoji.
    if re.search(r"[A-Za-z0-9]", stripped):
        return ""
    return first


def parse_card_text(text: str) -> tuple[str, str]:
    """Split a notebook card's text into (title, date_raw).

    Cards look roughly like:
        ⛏️                     (emoji icon, often its own line)
        Title line
        N sources · 3 days ago
    """
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return "", ""
    # First line that actually looks like a title (skip the icon line).
    title = next((ln for ln in lines if _looks_like_real_title(ln)), lines[0])
    date_raw = ""
    date_hint = re.compile(
        r"(ago|vor\s+\d|today|heute|yesterday|gestern|jetzt|soeben|"
        r"\d{4}-\d{2}-\d{2}|\d{1,2}\.\d{1,2}\.\d{4}|"
        r"jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|"
        r"märz|mär|mai|juni|juli|okt|dez|januar|februar|april|august|september|oktober|november|dezember)",
        re.I,
    )
    for ln in lines[1:]:
        # Strip leading bullets / pipe separators commonly used in cards.
        parts = re.split(r"[·•\|]", ln)
        for p in parts:
            p = p.strip()
            if date_hint.search(p):
                date_raw = p
                break
        if date_raw:
            break
    return title, date_raw


def backfill_emojis_into_json(notebooks: list[dict]) -> int:
    """Update any *.json in OUTPUT_DIR whose `notebook_emoji` is missing or
    empty by looking the notebook_id up in `notebooks` (output of
    list_notebooks()). Stale cover PNGs for those entries are deleted so the
    next cover-art pass regenerates them with the correct emoji.

    Returns the number of sidecars updated. Idempotent: JSONs that already
    carry an emoji are left untouched.
    """
    if OUTPUT_DIR is None or not OUTPUT_DIR.exists():
        return 0
    by_id = {n["id"]: n.get("emoji") for n in notebooks if n.get("emoji")}
    if not by_id:
        return 0
    updated = 0
    for jp in sorted(OUTPUT_DIR.glob("*.json")):
        try:
            meta = json.loads(jp.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            log.warning("Backfill: cannot parse %s (%s); skipping.", jp.name, e)
            continue
        if meta.get("notebook_emoji"):
            continue
        emoji = by_id.get(meta.get("notebook_id") or "")
        if not emoji:
            continue
        meta["notebook_emoji"] = emoji
        jp.write_text(
            json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8",
        )
        # Nuke any stale cover so the next cover-art pass renders the real
        # emoji instead of leaving COVER_DEFAULT_EMOJI baked in.
        png = jp.with_suffix(".png")
        if png.exists():
            png.unlink()
            log.info("Backfilled emoji %s into %s (stale PNG removed).", emoji, jp.name)
        else:
            log.info("Backfilled emoji %s into %s.", emoji, jp.name)
        updated += 1
    return updated


def select_candidates(
    notebooks: list[dict],
    since_date: datetime,
    processed_ids: set[str],
) -> list[dict]:
    """Return notebooks modified on/after since_date and not yet processed,
    sorted oldest-first (so a mid-run failure leaves the newest still queued)."""
    candidates = [
        n for n in notebooks
        if n["modified"] is not None
        and n["modified"] >= since_date
        and n["id"] not in processed_ids
    ]
    candidates.sort(key=lambda n: n["modified"])
    return candidates


# ---------------------------------------------------------------------------
# Notebook page: extract description + audio
# ---------------------------------------------------------------------------

def open_notebook(page: Page, url: str) -> None:
    log.info("Navigating to notebook: %s", url)
    page.goto(with_english_hint(url), wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    try:
        page.wait_for_load_state("networkidle", timeout=NAV_TIMEOUT_MS)
    except PWTimeout:
        log.warning("networkidle not reached on notebook; continuing.")
    page.wait_for_timeout(EXTRA_SETTLE_MS)

    if "accounts.google.com" in page.url or "ServiceLogin" in page.url:
        raise RuntimeError("Not authenticated. Run `uv run scraper.py --login` first.")


# JS scanner: anchor on the notebook's title heading in the chat panel, then
# walk up to the smallest ancestor that contains enough text to be the summary
# container. Capped at ~8000 chars so we never accidentally grab the entire
# page body.
#
# Anchor selectors, in order of preference:
#   1. h2.cover-title — current UI (rolled out ~May 2026); the notebook title
#      is rendered as <h2 class="cover-title mat-headline-medium"> inside
#      <chat-panel-header>.
#   2. h1 — legacy layout, kept as a fallback in case Google A/B-tests this.
_DESCRIPTION_JS = r"""
() => {
  const anchor =
    document.querySelector('h2.cover-title') ||
    document.querySelector('h1');
  if (!anchor) return null;
  let el = anchor.parentElement;
  let best = null;
  while (el && el !== document.body) {
    const text = (el.innerText || '').trim();
    if (text.length > 8000) break;     // gone past the chat panel — stop.
    if (text.length >= 200) best = text;
    el = el.parentElement;
  }
  return best;
}
"""


# Lines we always want to drop from a captured chat-panel block.
_NOISE_LINE_RES: list[re.Pattern[str]] = [
    re.compile(r"^[a-z][a-z0-9_]*$"),                    # material icon ligatures
    re.compile(r"^(Customize|Add note|Save to note|Saved responses)$", re.I),
    re.compile(r"^(keep|copy_all|thumb_up|thumb_down)$", re.I),  # message action labels
    re.compile(r"^(Sources|Studio|Chat|Notes?|Add sources)$", re.I),
    re.compile(r"^(Select all|Discover sources)$", re.I),
    re.compile(r"^(Settings|Share|Analytics|PRO|Create notebook)$", re.I),
    re.compile(r"^(NotebookLM can be inaccurate.*)$", re.I),
    re.compile(r"^Ask a question.*$", re.I),
    re.compile(r"^[·•|]+$"),                              # standalone separators
    # Studio artifact-type labels (when the walk-up leaked into the Studio panel).
    re.compile(
        r"^(Audio Overview|Slide Deck|Video Overview|Mind Map|Reports|"
        r"Flashcards|Quiz|Infographic|Data Table)$", re.I,
    ),
    re.compile(r"^Deep Dive\b.*$", re.I),                 # audio overview row subtitle
    re.compile(r"^Try\b.*$", re.I),                       # "Try new…" tips, "Try it" CTA
    re.compile(r"^\d+\s+sources?$", re.I),                # standalone "N sources"
    re.compile(r"^\d+\s+source$", re.I),
    re.compile(r"^[\w-]+\.(pdf|docx?|pptx?|txt|md|html?)$", re.I),  # source filenames
]


def _clean_description(text: str) -> str:
    """Strip Material Symbols ligature names, common button labels, and chat
    boilerplate from extracted text. Preserves paragraph spacing."""
    kept: list[str] = []
    for raw in text.splitlines():
        ln = raw.strip()
        if not ln:
            if kept and kept[-1] != "":
                kept.append("")
            continue
        # Drop standalone emoji-only lines (notebook icon, etc.).
        if not re.search(r"[A-Za-z0-9]", _EMOJI_ONLY_STRIPPER.sub("", ln)):
            continue
        if any(p.match(ln) for p in _NOISE_LINE_RES):
            continue
        kept.append(ln)
    result = "\n".join(kept).strip()
    return re.sub(r"\n{3,}", "\n\n", result)


def extract_description(page: Page) -> str:
    """Extract the chat-panel summary text used as the episode description.

    Anchors on the page's <h1> (the notebook title is always rendered there)
    and walks up until it has enough text to look like a summary container,
    capped so the walk can't escape into Sources / Studio. Then strips UI
    noise (icon ligatures, button labels, chat input boilerplate).
    """
    try:
        raw = page.evaluate(_DESCRIPTION_JS)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"Description scan crashed: {e}") from e
    if not raw:
        raise RuntimeError("No <h1>-anchored container with description text was found.")
    cleaned = _clean_description(raw)
    if len(cleaned) < 100:
        raise RuntimeError(
            f"Description too short after cleanup ({len(cleaned)} chars). "
            "The chat panel may not have generated a summary for this notebook."
        )
    log.info("Description extracted (%d raw → %d cleaned chars).", len(raw), len(cleaned))
    return cleaned


def extract_notebook_title(page: Page) -> str:
    """Read the notebook's real title from the page.

    Tries (in order):
      1. ``h2.cover-title`` — current UI (~May 2026), inside ``<chat-panel-header>``.
      2. ``h1`` — legacy layout, kept as a fallback in case Google A/B-tests this.
      3. The browser ``<title>``, with ``" - NotebookLM"`` suffix trimmed.
    """
    for selector in ("h2.cover-title", "h1"):
        try:
            loc = page.locator(selector).first
            if loc.count() > 0:
                txt = (loc.inner_text(timeout=2_000) or "").strip()
                if len(txt) >= 3:
                    return txt
        except Exception:
            continue
    raw = (page.title() or "").strip()
    return re.sub(r"\s*[\-—–|]\s*NotebookLM.*$", "", raw, flags=re.I).strip()


def _find_audio_overview_row(page: Page) -> Locator:
    """Locate the generated audio overview row in the Studio panel.

    The row carries a 'Deep Dive · N sources · …' subtitle and four action
    buttons (artifact-stretched body button, Interactive mode, Play, kebab).
    Strategies, tried in order:

      1. ``<artifact-library-item>`` — current UI (~May 2026). The Studio
         panel's generated-overview row is a custom element with exactly
         this tag, regardless of label text.
      2. Text-anchored: find an element whose text matches
         ``Deep Dive · …`` (the artifact subtitle uses a bullet separator)
         and walk up to the smallest ancestor with ≥2 buttons. Used as a
         fallback for older / A/B-tested layouts.

    The bullet-separator filter on (2) is important: notebooks routinely
    contain *sources* titled ``Deep Dive: <topic>``, and an unqualified
    ``Deep Dive`` regex grabs the wrong row (the source's kebab opens a
    Rename/Remove menu with no Download item).
    """
    item = page.locator("artifact-library-item").first
    if item.count() > 0:
        log.info("Audio overview row anchored on <artifact-library-item>.")
        return item

    for needle in (r"Deep Dive\s*[·•]", r"Audio Overview\s*[·•]"):
        text = page.get_by_text(re.compile(needle, re.I)).first
        if text.count() == 0:
            continue
        row = text.locator("xpath=ancestor::*[count(.//button) >= 2][1]")
        if row.count() > 0:
            log.info("Audio overview row anchored on %r.", needle)
            return row.first

    raise RuntimeError(
        "Could not locate the generated audio overview row. "
        "Has the overview been generated for this notebook yet?"
    )


def download_audio_overview(page: Page) -> Download:
    """Open the audio overview's kebab menu, click 'Download', return Download.
    Raises with a clear message on failure — never falls back to streaming."""
    row = _find_audio_overview_row(page)
    try:
        row.scroll_into_view_if_needed(timeout=3_000)
    except Exception:
        pass

    buttons = row.locator("button")
    n = buttons.count()
    if n == 0:
        raise RuntimeError("Audio overview row has no buttons.")
    log.info("Audio overview row has %d button(s); clicking the last (kebab).", n)

    # Click the kebab (rightmost button) to open the action menu.
    kebab = buttons.nth(n - 1)
    kebab.click(timeout=5_000)
    page.wait_for_timeout(300)  # let the popover render

    # Click the 'Download' menu item. Note: the visible text often includes a
    # leading Material Symbols ligature (e.g. "save_alt Download"), so we
    # match by substring rather than exact equality.
    download_item = page.get_by_role(
        "menuitem", name=re.compile(r"\bdownload\b", re.I)
    ).first
    if download_item.count() == 0:
        # MDC sometimes uses button instead of menuitem.
        download_item = page.locator(
            '[role="menuitem"]:has-text("Download"), button:has-text("Download")'
        ).first
    if download_item.count() == 0:
        raise RuntimeError("Kebab menu opened but no 'Download' item was found.")

    with page.expect_download(timeout=UI_TIMEOUT_MS) as dl_info:
        download_item.click(timeout=5_000)
    log.info("Download triggered.")
    return dl_info.value


def save_episode(
    notebook: Optional[dict],
    notebook_title: str,
    description: str,
    download: Download,
) -> tuple[Path, str]:
    assert OUTPUT_DIR is not None
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    ep_hash = md5_of(normalize_for_hash(description))
    log.info("Episode hash: %s", ep_hash)

    json_path = OUTPUT_DIR / f"{ep_hash}.json"
    mp3_path = OUTPUT_DIR / f"{ep_hash}.mp3"

    if json_path.exists():
        log.info("Episode already exists (%s); skipping write.", json_path.name)
        return json_path, ep_hash

    # Save the raw download to a hidden temp file first — we don't yet know
    # whether NotebookLM delivered real MPEG audio or an MP4/DASH container,
    # and the two need very different post-processing. Sniffing magic bytes
    # before committing to <hash>.mp3 prevents us from saving a mislabelled
    # ISO Media stream under a .mp3 extension (which silently breaks every
    # downstream pass that assumes MPEG audio — id3tag in particular).
    tmp_download = OUTPUT_DIR / f".{ep_hash}.download.partial"
    download.save_as(str(tmp_download))

    raw_size = tmp_download.stat().st_size
    if raw_size < 10_000:
        tmp_download.unlink(missing_ok=True)
        raise RuntimeError(f"Downloaded audio is suspiciously small ({raw_size} bytes); aborting.")

    with tmp_download.open("rb") as f:
        head = f.read(12)
    is_mp4 = head[4:8] == b"ftyp"   # ISO Base Media (MP4, M4A, DASH segments)

    if is_mp4:
        # Hand off to the same ffmpeg helper convert.py uses. We force the
        # output name so the description-hash naming (and the JSON sidecar
        # that references it) stays valid.
        log.info(
            "NotebookLM served an MP4/ISO container; transcoding to MP3 "
            "in-place via ffmpeg..."
        )
        from convert import transcode_to_mp3

        try:
            transcode_to_mp3(tmp_download, mp3_path)
        finally:
            tmp_download.unlink(missing_ok=True)
    else:
        # ID3-tagged MP3 (b"ID3...") or raw MPEG sync (b"\xff\xfb...") — keep
        # as-is. Any unrecognised header still passes through here; the
        # size check above ruled out tiny error pages, and downstream
        # tools (id3tag) will surface real corruption with a clear error.
        tmp_download.replace(mp3_path)

    size = mp3_path.stat().st_size
    if size < 10_000:
        mp3_path.unlink(missing_ok=True)
        raise RuntimeError(f"Final audio is suspiciously small ({size} bytes); aborting.")

    now = datetime.now(timezone.utc)
    title_part = notebook_title.strip() or now.strftime("%Y-%m-%d")
    metadata = {
        "id": ep_hash,
        "title": title_part,
        "description": description,
        "audio_file": f"{ep_hash}.mp3",
        "pub_date": now.isoformat(),
        "notebook_id": (notebook or {}).get("id"),
        "notebook_url": (notebook or {}).get("url"),
        # NotebookLM's auto-assigned notebook icon. Captured from the home-page
        # card (see parse_card_emoji) and used by coverart.py as the cover
        # symbol. Only present in --list-driven runs; --url overrides have no
        # card to read from and leave this null.
        "notebook_emoji": (notebook or {}).get("emoji") or None,
        "notebook_modified": (
            notebook["modified"].isoformat()
            if notebook and notebook.get("modified") else None
        ),
    }
    json_path.write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("Wrote %s (%.1f KB) and %s", mp3_path.name, size / 1024, json_path.name)
    return json_path, ep_hash


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------

def run_login() -> None:
    with sync_playwright() as pw:
        ctx = launch_context(pw, headless=False)
        page = ctx.new_page()
        page.goto(with_english_hint(HOME_URL), wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        log.info("Sign into Google in the opened window.")
        log.info("When you can see your NotebookLM home page, press <Enter> here to exit.")
        try:
            input()
        except EOFError:
            pass
        ctx.close()
        log.info("Login session saved.")


def run_list() -> None:
    with sync_playwright() as pw:
        ctx = launch_context(pw, headless=True)
        try:
            page = ctx.new_page()
            page.set_default_timeout(UI_TIMEOUT_MS)
            notebooks = list_notebooks(page)
            print(f"Found {len(notebooks)} notebook(s):")
            for n in notebooks:
                mod = n["modified"].isoformat() if n["modified"] else f"?  raw={n['modified_raw']!r}"
                emoji = n.get("emoji") or "·"
                print(f"  - {n['id']}  {mod}  {emoji}  {n['title']}")
        finally:
            ctx.close()


def run_backfill() -> None:
    """Fetch the notebook list, write notebook_emoji into any sidecar JSON
    that's missing it, drop the now-stale cover PNG, and rerun the cover-art
    pass. Useful after upgrading from a scraper version that didn't capture
    emojis (or for notebooks originally scraped via --url)."""
    require_output_dir()
    assert OUTPUT_DIR is not None
    with sync_playwright() as pw:
        ctx = launch_context(pw, headless=True)
        try:
            page = ctx.new_page()
            page.set_default_timeout(UI_TIMEOUT_MS)
            notebooks = list_notebooks(page)
        finally:
            ctx.close()
    if not notebooks:
        log.info("No notebooks visible; nothing to backfill.")
        return
    n_back = backfill_emojis_into_json(notebooks)
    log.info("Backfill done. %d sidecar(s) updated.", n_back)
    if n_back:
        from coverart import cover_missing
        cover_missing(OUTPUT_DIR)
        # Re-tag MP3s so the embedded APIC picks up the new cover.
        try:
            from id3tag import tag_missing
            tag_missing(OUTPUT_DIR)
        except Exception as e:  # noqa: BLE001
            log.warning("ID3 retag failed: %s", e)


def _process_notebook(page: Page, notebook: Optional[dict], url: str, state: dict) -> bool:
    """Open one notebook, extract its description, download the audio overview,
    write episode files, and mark the notebook id as processed.
    Returns True on success, False if there was nothing new to download."""
    open_notebook(page, url)

    notebook_title = extract_notebook_title(page)
    log.info("Notebook title: %s", notebook_title or "(unknown)")

    description = extract_description(page)

    assert OUTPUT_DIR is not None
    preview_hash = md5_of(normalize_for_hash(description))
    if (OUTPUT_DIR / f"{preview_hash}.json").exists():
        log.info("Episode hash %s already on disk; skipping download.", preview_hash)
        if notebook:
            mark_processed(state, notebook["id"])
        return False

    log.info("New episode detected, downloading audio overview...")
    download = download_audio_overview(page)
    save_episode(notebook, notebook_title, description, download)
    if notebook:
        mark_processed(state, notebook["id"])
    return True


def run_scrape(url_override: Optional[str]) -> None:
    require_output_dir()
    state = load_state()
    since_date = parse_iso(INITIAL_SINCE_DATE)
    processed_ids = load_processed_ids(state)
    log.info(
        "Since cutoff: %s; %d notebook(s) already processed.",
        since_date.isoformat(), len(processed_ids),
    )

    with sync_playwright() as pw:
        ctx = launch_context(pw, headless=True)
        try:
            page = ctx.new_page()
            page.set_default_timeout(UI_TIMEOUT_MS)

            # URL override mode: just scrape that one notebook, ignore state.
            if url_override or NOTEBOOK_URL:
                target_url = url_override or NOTEBOOK_URL
                log.info("URL override in use; skipping home-page listing: %s", target_url)
                _process_notebook(page, notebook=None, url=target_url, state=state)
            else:
                notebooks = list_notebooks(page)
                if not notebooks:
                    log.info("No notebooks visible; nothing to scrape.")
                else:
                    # Opportunistic emoji backfill: any episode whose JSON sidecar
                    # predates the notebook_emoji field (or that was scraped via
                    # --url with no card to read) gets its emoji filled in now,
                    # and the stale cover deleted so the cover-art pass below
                    # rerenders it with the right glyph.
                    n_back = backfill_emojis_into_json(notebooks)
                    if n_back:
                        log.info("Backfilled notebook_emoji into %d sidecar(s).", n_back)

                    undated = [n for n in notebooks if n["modified"] is None]
                    if undated:
                        log.warning(
                            "Skipping %d notebook(s) with unparseable dates "
                            "(re-check selectors if this is unexpected).", len(undated),
                        )

                    candidates = select_candidates(notebooks, since_date, processed_ids)
                    if not candidates:
                        log.info("No new notebooks to process.")
                    else:
                        log.info("Queue: %d notebook(s) to process (oldest first):", len(candidates))
                        for n in candidates:
                            log.info("  - %s  %s  %s", n["modified"].date(), n["id"], n["title"])

                        successes, failures = 0, 0
                        for n in candidates:
                            log.info("=== %s — %s ===", n["modified"].date(), n["title"])
                            try:
                                _process_notebook(page, notebook=n, url=n["url"], state=state)
                                successes += 1
                            except Exception as e:  # noqa: BLE001
                                failures += 1
                                log.error("Failed to process %s (%s): %s", n["title"], n["id"], e)
                                # Do NOT mark as processed — next run will retry.

                        log.info("Scrape done. %d succeeded, %d failed.", successes, failures)
        finally:
            ctx.close()

    # Run the post-passes sequentially. Order matters because each later
    # pass depends on artefacts the earlier ones produced for manually-
    # dropped audio (no JSON sidecar from the scraper itself):
    #   1. convert    : .m4a → <md5(m4a-bytes)>.mp3 (ffmpeg, idempotent)
    #   2. transcribe : MP3 → .vtt (MLX Whisper, on-device)
    #   3. summarise  : .vtt → .json (Ollama text model, only acts when
    #                                 a sidecar JSON is missing)
    #   4. coverart   : .json + MP3 → .png (Ollama image model)
    #   5. id3tag     : verify standard podcast ID3 frames on every MP3
    #                   (uses the JSON sidecar + PNG cover; idempotent —
    #                   only rewrites frames that are missing or stale)
    # For scraper-downloaded notebooks passes 1 and 3 are no-ops because
    # we already have an MP3 + JSON sidecar from save_episode().
    assert OUTPUT_DIR is not None
    _run_post_passes(OUTPUT_DIR)


def _run_post_passes(output_dir: Path) -> None:
    from convert import convert_missing
    from transcribe import transcribe_missing
    from summarize import summarise_missing
    from coverart import cover_missing
    from id3tag import tag_missing

    def _safe(name: str, fn, *args) -> None:
        try:
            fn(*args)
        except Exception as e:  # noqa: BLE001
            log.warning("%s pass failed: %s", name, e)

    _safe("M4A conversion", convert_missing, output_dir)
    _safe("Transcription", transcribe_missing, output_dir)
    _safe("Summary", summarise_missing, output_dir)
    _safe("Cover-art", cover_missing, output_dir)
    _safe("ID3 verification", tag_missing, output_dir)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--login", action="store_true", help="Open headful browser to sign into Google.")
    group.add_argument("--list", action="store_true", help="List notebooks on the home page and exit.")
    group.add_argument("--url", type=str, default=None, help="Scrape this specific notebook URL (ignores state).")
    group.add_argument(
        "--backfill-emojis", action="store_true",
        help="Fetch the notebook list, write notebook_emoji into any sidecar JSON missing it, "
             "drop the stale cover PNG, and rerender + retag covers.",
    )
    args = parser.parse_args()

    if args.login:
        run_login()
    elif args.list:
        run_list()
    elif args.backfill_emojis:
        run_backfill()
    else:
        run_scrape(args.url)


if __name__ == "__main__":
    main()
