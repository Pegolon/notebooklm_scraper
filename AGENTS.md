# AGENTS.md — notebooklm_scraper

Guidance for AI assistants working in this repo. Read this first.

## What this project is

Three-script pipeline that turns Google NotebookLM "Audio Overviews" into a
personal podcast feed with transcripts. The user (Markus / GitHub: Pegolon)
runs the scraper on a Mac Mini via cron/launchd; the feed generator runs
wherever the resulting folder is served from.

```diagram
╭─ Mac Mini ────────────────────────────────╮      ╭─ Cloud/host side ──╮
│ scraper.py     → MP3 + JSON sidecar       │      │ feed.py            │
│   then runs:                              │      │   scans *.mp3      │
│ transcribe.py  → Gemini → <name>.vtt      │      │   builds feed.xml  │
╰──────────┬────────────────────────────────╯      ╰─────────┬──────────╯
           ▼ writes into                                     │ reads from
    ╭──────────────────────────────╮  ◀── same folder ──────╯
    │ OUTPUT_DIR (Google Drive)    │
    │   ↳ <hash>.mp3               │
    │   ↳ <hash>.json              │
    │   ↳ <hash>.vtt               │
    │   ↳ feed.xml                 │  (manual MP3 drops also work)
    ╰──────────────────────────────╯
```

## Stack

- **Python 3.14** (pinned via `.python-version`)
- **uv** for env / deps / lockfile (`uv sync`, `uv run …`)
- **Playwright** (Chromium) for scraping — uses a persistent context at
  `./playwright_profile/`, **never** Chrome's real profile (Chrome locks it).
- **google-genai** SDK for Gemini transcription
- Standard library `xml.etree.ElementTree` for RSS generation (no extra dep)

### Common commands

```bash
uv sync                                # install deps into .venv
uv run playwright install chromium     # one-time browser install
uv run scraper.py --login              # one-time headful Google sign-in
uv run scraper.py                      # main entry (cron-driven)
uv run scraper.py --list               # debug: enumerate notebooks
uv run scraper.py --url <NOTEBOOK_URL> # force a specific notebook
uv run transcribe.py                   # transcribe any MP3 missing a .vtt
uv run transcribe.py --file foo.mp3    # transcribe one file
uv run feed.py                         # write feed.xml to OUTPUT_DIR
uv run feed.py --stdout                # print feed for inspection
```

## File map

| File | Role |
|---|---|
| [scraper.py](scraper.py) | Playwright scraper. Lists notebooks, picks new ones, downloads audio, writes JSON, runs transcribe pass at end. |
| [transcribe.py](transcribe.py) | Gemini → WebVTT. Scans for MP3s missing `.vtt`. Standalone CLI + importable `transcribe_missing()`. |
| [feed.py](feed.py) | MP3-first RSS/iTunes generator. Synthesises metadata for bare MP3s with no JSON sidecar. |
| [pyproject.toml](pyproject.toml) | Deps: `playwright`, `python-dotenv`, `google-genai`. Requires Python 3.14. |
| [.env.example](.env.example) | Full annotated config template. |
| [.gitignore](.gitignore) | Hides `.env`, `playwright_profile/`, `scraper_state.json`, caches. |

## Configuration (`.env`)

Required:
- `OUTPUT_DIR` — folder inside the Google Drive desktop-sync mount

Optional / by feature:
- Scraper: `INITIAL_SINCE_DATE` (default `2026-05-01`), `NOTEBOOK_URL` (override),
  `USER_DATA_DIR`, `TITLE_PREFIX`, `EXTRA_SETTLE_MS`
- Transcription: `GEMINI_API_KEY` (enables the pass), `GEMINI_MODEL`
  (default `gemini-2.5-flash`)
- Feed: `FEED_BASE_URL` (required for feed), `FEED_FILE`, `FEED_TITLE`,
  `FEED_DESCRIPTION`, `FEED_AUTHOR`, `FEED_OWNER_EMAIL`, `FEED_LANGUAGE`,
  `FEED_CATEGORY`, `FEED_IMAGE_URL`, `FEED_LINK`

### Path-value normalization (important gotcha)

Users naturally write shell-style escapes in `.env`:
```
OUTPUT_DIR=/Users/.../My\ Drive/Podcasts        # backslash escape
OUTPUT_DIR="/Users/.../My Drive/Podcasts"       # quoted
```
`python-dotenv` reads values **literally** — it does not unescape `\ ` or strip
quotes. Both `scraper.py`, `feed.py`, and `transcribe.py` defensively run path
values through `_clean_path_value()` to handle both styles. Keep this helper
when adding new scripts.

## Data model

Each scraped episode produces two sibling files (and later a `.vtt`):

- **`<hash>.mp3`** — raw audio download from NotebookLM's "Audio Overview" kebab → Download.
- **`<hash>.json`** — episode metadata:
  ```json
  {
    "id": "<md5-of-normalized-description>",
    "title": "NotebookLM Overview - <notebook title>",
    "description": "<cleaned chat-panel summary>",
    "audio_file": "<hash>.mp3",
    "pub_date": "<ISO-8601 UTC>",
    "notebook_id": "<UUID from URL>",
    "notebook_url": "https://notebooklm.google.com/notebook/<UUID>",
    "notebook_modified": "<ISO-8601 UTC>"
  }
  ```
- **`<hash>.vtt`** — WebVTT transcript (added by `transcribe.py`).

`<hash>` = MD5 of the normalized (whitespace-collapsed, lowercased) description.
This makes regeneration of the same content idempotent.

## State / dedup

- `scraper_state.json` (gitignored, next to `scraper.py`) tracks
  `processed_notebook_ids: [...]`.
- Dedup combines that list with a scan of `OUTPUT_DIR/*.json` for
  `notebook_id` — so deleting the state file never causes re-downloads
  (`load_processed_ids()` in `scraper.py`).
- Filter: `modified >= INITIAL_SINCE_DATE AND id not in processed_ids`.
- Candidates are processed **oldest-first** so a mid-run failure leaves the
  newest still queued for the next run.

## Hard-won knowledge about NotebookLM's DOM

The UI is Angular, dynamic class names, and **changes regularly**. The current
selectors live in `scraper.py`. Key strategies:

1. **Force English UI** — `locale="en-US"`, `Accept-Language: en-US,en;q=0.9`,
   and `?hl=en` appended to every URL. Independent of the Google account's
   display language. Don't remove this without a reason.

2. **Listing notebooks (home page)** — tries three strategies in order:
   1. `a[href*="/notebook/"]` (grid view).
   2. Click any "Grid view"/"Rasteransicht" toggle, retry anchors.
   3. Table-view fallback: walk `[role="row"]`/`mat-row` and use a JS scan
      (`_FIND_NB_ID_JS`) that looks for any descendant attribute containing
      `/notebook/<id>`.
   On total failure: dump page HTML to `debug_home.html`.

3. **Card text parsing** — `parse_card_text()` skips the first line if it's
   just the notebook's emoji icon (uses `_looks_like_real_title()`). Date
   parser handles English ("3 days ago", "May 28, 2026") AND German ("vor 3
   Tagen", "heute", "29.05.2026") as belt-and-suspenders against locale
   slippage.

4. **Audio download** — find the audio-overview row by "Deep Dive" text, walk
   up to smallest ancestor with ≥2 buttons, click the **last** button
   (kebab), then click "Download" in the popup menu. Capture via
   `page.expect_download()`. **No streaming fallback** — the user
   explicitly chose download-only.

5. **Description extraction** — anchor on `<h1>`, walk up to smallest
   ancestor with ≥200 chars text, capped at 8000 chars so the walk can't
   escape into Sources/Studio panels. Then `_clean_description()` strips
   Material Symbols ligature names (`landscape_2`, `photo_spark`,
   `chevron_forward` …), button labels (`Customize`, `Add note`, `keep`,
   `thumb_up` …), Studio artifact-type labels, "Deep Dive · …" subtitles,
   `N sources`, the inaccuracy disclaimer, and source filenames like
   `Micromania.pdf`.

6. **Notebook title** — read from `<h1>` on the notebook page itself, NOT
   from the home-page card (where the first line is the emoji icon).

When NotebookLM ships UI changes, the most fragile pieces are (in order):
the audio overview kebab structure, the chat-panel container around the
summary, the Studio panel label set.

## Transcription specifics

- Uses Gemini File API: upload MP3, ask for verbatim WebVTT, delete file.
- Default model: `gemini-2.5-flash`. ~2 minutes per ~100 MB file.
- Output is sometimes wrapped in ```vtt fences — `_strip_code_fence()` handles
  that. If `WEBVTT` header is missing, we prepend it.
- Failures don't block the rest of the pass; logged and moved on.
- Skips entirely when `GEMINI_API_KEY` is empty (so cron jobs without a key
  just no-op).

## RSS generator specifics

- Iterates `*.mp3` (NOT `*.json`) — bare MP3s dropped into the folder are
  picked up too. Synthesised metadata uses filename + mtime; GUID is
  `md5(filename)` so reruns stay stable.
- Enclosure URLs: `f"{FEED_BASE_URL}/{audio_file}"`. No web server is bundled.
- iTunes namespace registered via `ET.register_namespace()` (do NOT pass
  `xmlns:itunes` manually as an attribute — ElementTree will emit `ns0:`
  prefixes).

## Git setup

- Remote `origin → git@github-pegolon:Pegolon/notebooklm_scraper.git` via SSH
  alias in `~/.ssh/config`.
- The user has TWO GitHub identities and matching SSH keys:
  - `~/.ssh/id_rsa` → MarkusKirschner (other repos)
  - `~/.ssh/id_ed25519` → Pegolon (this repo)
- The `github-pegolon` host alias forces `IdentityFile id_ed25519` +
  `IdentitiesOnly yes` so the right key is used regardless of agent order.
- Do not push without explicit user instruction (per global guidelines).

## Verification recipes

```bash
# Sanity-check Python compiles after edits
uv run python -m py_compile scraper.py feed.py transcribe.py

# Dump notebooks the scraper currently sees
uv run scraper.py --list

# Validate the generated feed XML
xmllint --noout "$OUTPUT_DIR/feed.xml" && echo VALID

# Inspect cleaned descriptions for existing episodes
uv run python -c "
import json, pathlib
from scraper import _clean_description
for f in pathlib.Path('$OUTPUT_DIR').glob('*.json'):
    m = json.load(f.open()); print(m['title']); print(m['description'][:300]); print()
"
```

## Style notes for this repo

- Single-purpose modules. Don't merge them unless asked.
- Each top-level script has a docstring with `uv run …` invocation examples.
- Logging via stdlib `logging` with the same format string in all three.
- Defensive `try/except` is fine around external IO (Playwright, Gemini),
  but log loudly — never swallow silently.
- Selector logic is the volatile part. Comment WHY a selector exists when
  you add one.

## When NotebookLM breaks the scraper

1. `uv run scraper.py --list` to see if listing still works.
2. If it returns 0 notebooks, `debug_home.html` will be written — inspect.
3. If listing works but a specific notebook fails, try
   `uv run scraper.py --url <URL>` and look at which extraction step blew up.
4. For description/title regressions, ask the user for a screenshot of the
   chat panel — guessing at selectors blind has burned cycles before.

## What not to do

- Don't add a streaming fallback to audio download. (User said download-only.)
- Don't auto-commit/push. (Global rule + repo rule.)
- Don't generate the feed from inside the scraper. (User wants them
  decoupled so the cloud-side can run feed.py independently.)
- Don't escape spaces in `.env` paths with backslashes — write them
  verbatim or wrap in double quotes. The scripts normalize either form.
- Don't point Playwright at Chrome's real profile — it'll fail to attach
  while Chrome is running.
