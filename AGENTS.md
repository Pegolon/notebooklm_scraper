# AGENTS.md — notebooklm_scraper

Guidance for AI assistants working in this repo. Read this first.

## What this project is

Two-app pipeline that turns Google NotebookLM "Audio Overviews" into a
personal podcast feed with transcripts. The user (Markus / GitHub: Pegolon)
runs the **local** side on a Mac Mini via cron/launchd; the **cloud** side
runs wherever the Google Drive folder is reachable and exposes the feed +
audio over HTTPS.

```diagram
╭─ Mac Mini ─────────────────────╮      ╭─ Cloud / VPS ──────────────────────────╮
│ local/scraper.py               │      │ cloud/app.py  (FastAPI / uvicorn)      │
│   → MP3 + JSON sidecar         │      │   GET /feed.xml         →  RSS XML     │
│ local/transcribe.py            │      │   GET /audio/<n>        →  MP3 (Range) │
│   → <name>.vtt                 │      │   GET /transcripts/<n>  →  WebVTT      │
│                                │      ╰──────────────┬─────────────────────────╯
╰──────────┬─────────────────────╯                     │ reads
           ▼ writes into                               │
    ╭────────────────────────────╮  ◀─── same folder ──╯
    │ OUTPUT_DIR (Google Drive)  │
    │   ↳ <hash>.mp3             │
    │   ↳ <hash>.json            │
    │   ↳ <hash>.vtt             │   (manual MP3 drops also work)
    ╰────────────────────────────╯
```

The two halves are **independent uv-managed Python 3.14 projects**. Each
subdir has its own `pyproject.toml`, `.env.example`, `.python-version`,
and `README.md`. Never merge them — they ship to different hosts.

## Stack

- **Python 3.14** (pinned via `.python-version` in each subdir)
- **uv** for env / deps / lockfile (`uv sync`, `uv run …`)
- **local/**: Playwright (Chromium), mlx-whisper, google-genai (for cover art), python-dotenv
- **cloud/**: FastAPI, uvicorn[standard], python-dotenv
- Standard library `xml.etree.ElementTree` for RSS (no extra dep)

## File map

| Path | Role |
|---|---|
| [local/scraper.py](local/scraper.py) | Playwright scraper. Lists notebooks, picks new ones, downloads audio, writes JSON, runs transcribe pass at end. |
| [local/transcribe.py](local/transcribe.py) | MLX Whisper → WebVTT (on-device). Scans for MP3s missing `.vtt`. Standalone CLI + importable `transcribe_missing()`. |
| [local/pyproject.toml](local/pyproject.toml) | Deps: `playwright`, `python-dotenv`, `mlx-whisper`, `google-genai`. |
| [local/.env.example](local/.env.example) | Local-side config template. |
| [cloud/app.py](cloud/app.py) | FastAPI app: `GET /feed.xml` + `GET /audio/{name}` (range-aware). |
| [cloud/pyproject.toml](cloud/pyproject.toml) | Deps: `fastapi`, `uvicorn[standard]`, `python-dotenv`. |
| [cloud/.env.example](cloud/.env.example) | Cloud-side config template. |

### Common commands

```bash
# local side (Mac Mini)
cd local
uv sync
uv run playwright install chromium     # one-time browser install
uv run scraper.py --login              # one-time headful Google sign-in
uv run scraper.py                      # main entry (cron-driven)
uv run scraper.py --list               # debug: enumerate notebooks
uv run scraper.py --url <URL>          # force a specific notebook
uv run transcribe.py                   # transcribe any MP3 missing a .vtt
uv run transcribe.py --file foo.mp3    # transcribe one file

# cloud side
cd cloud
uv sync
uv run uvicorn app:app --host 0.0.0.0 --port 8000
```

## Configuration (`.env` per subdir)

Both apps need their own `.env` file (in `local/` and `cloud/`). The only
setting that must agree between them is **`OUTPUT_DIR`** — both must point
to the same Google-Drive-synced folder.

`local/.env` knobs:
- Required: `OUTPUT_DIR`
- Scraper: `INITIAL_SINCE_DATE` (default `2026-05-01`), `NOTEBOOK_URL`
  (override), `USER_DATA_DIR`, `TITLE_PREFIX`, `EXTRA_SETTLE_MS`
- Transcription (MLX Whisper, on-device): `WHISPER_MODEL` (default
  `mlx-community/whisper-large-v3-mlx`), `WHISPER_LANGUAGE` (optional
  ISO-639-1 hint; empty = auto-detect), `WHISPER_INITIAL_PROMPT` (short
  style-priming sentence; default biases toward proper punctuation +
  capitalisation). Model is fetched from HF Hub on first run and cached.

`cloud/.env` knobs:
- Required: `OUTPUT_DIR`, `FEED_BASE_URL` (the public URL of the FastAPI
  app itself, no trailing slash — enclosure URLs are built as
  `${FEED_BASE_URL}/audio/<filename>.mp3`)
- Optional: `FEED_TITLE`, `FEED_DESCRIPTION`, `FEED_AUTHOR`,
  `FEED_OWNER_EMAIL`, `FEED_LANGUAGE`, `FEED_CATEGORY`, `FEED_IMAGE_URL`,
  `FEED_LINK`

### Path-value normalization (important gotcha)

Users naturally write shell-style escapes in `.env`:
```
OUTPUT_DIR=/Users/.../My\ Drive/Podcasts        # backslash escape
OUTPUT_DIR="/Users/.../My Drive/Podcasts"       # quoted
```
`python-dotenv` reads values **literally** — it does not unescape `\ ` or
strip quotes. Both `local/scraper.py`, `local/transcribe.py`, and
`cloud/app.py` defensively run path values through `_clean_path_value()` to
handle both styles. Keep this helper when adding new scripts.

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

- `local/scraper_state.json` (gitignored, next to `scraper.py`) tracks
  `processed_notebook_ids: [...]`.
- Dedup combines that list with a scan of `OUTPUT_DIR/*.json` for
  `notebook_id` — so deleting the state file never causes re-downloads
  (`load_processed_ids()` in `scraper.py`).
- Filter: `modified >= INITIAL_SINCE_DATE AND id not in processed_ids`.
- Candidates are processed **oldest-first** so a mid-run failure leaves the
  newest still queued for the next run.

## Hard-won knowledge about NotebookLM's DOM

The UI is Angular, dynamic class names, and **changes regularly**. The current
selectors live in `local/scraper.py`. Key strategies:

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

- Runs on-device via `mlx_whisper.transcribe(...)` (Apple-Silicon MLX backend).
  No network call per file once the model is cached.
- Default model: `mlx-community/whisper-large-v3-mlx`. Override with
  `WHISPER_MODEL` (any HF repo from `mlx-community/whisper-*` or a local path).
- We build the WebVTT ourselves from the returned `segments` list
  (`_segments_to_vtt()` in `transcribe.py`) — timestamps are formatted as
  `HH:MM:SS.mmm`. Empty-text segments are dropped.
- `WHISPER_LANGUAGE` (ISO-639-1) can force a language; otherwise Whisper
  auto-detects.
- We pass `condition_on_previous_text=True` plus a short, *non-imperative*
  `initial_prompt` (`WHISPER_INITIAL_PROMPT`) so Whisper carries punctuation
  and capitalisation style across windows. Important: imperative priming
  text ("Welcome to the show. Let's get started.") makes the model
  *continue the prompt* on short/quiet windows instead of transcribing —
  keep the default ("The following is a clear, well-punctuated transcript
  …") or any descriptive variant.
- Failures don't block the rest of the pass; logged and moved on.
- The pass always runs; the only requirement is that `mlx-whisper` and its
  MLX deps are installed (Mac-only). On non-Mac hosts the import will fail
  per-file and be logged.

## Cloud app (FastAPI) specifics

- **No disk writes.** The feed is built per request — there is no `feed.xml`
  stored in `OUTPUT_DIR` anymore.
- **`/audio/{filename}`** streams MP3s with HTTP Range support (single
  range only). Apple Podcasts and Overcast require Range to seek. Multi-
  range and zero-length suffix ranges return 416.
- **`/transcripts/{filename}`** serves WebVTT (`.vtt`) files whole (they're
  tens-to-hundreds of KB; no Range support). Content-Type is
  `text/vtt; charset=utf-8`.
- Each `/feed.xml` item with a sibling `.vtt` next to its `.mp3` gets a
  Podcasting 2.0 `<podcast:transcript url=… type="text/vtt" lang=…>` tag.
  Language is `FEED_LANGUAGE.split("-")[0]` (so `en-us` → `en`).
- **Path safety**: `_safe_resolve()` rejects names containing `/`, `\`,
  leading `.`, wrong suffix, or that fall outside `OUTPUT_DIR` after
  `.resolve()`. Both audio and transcript routes share it.
- Iterates `*.mp3` (NOT `*.json`) — bare MP3s dropped into the folder are
  picked up too. Synthesised metadata uses filename + mtime; GUID is
  `md5(filename)` so reruns stay stable.
- Enclosure URLs: `f"{FEED_BASE_URL}/audio/{audio_file}"`. Transcript URLs:
  `f"{FEED_BASE_URL}/transcripts/{vtt_file}"`. `FEED_BASE_URL` is the
  public URL of the FastAPI app itself.
- All three namespaces (`itunes`, `atom`, `podcast`) are registered via
  `ET.register_namespace()` (do NOT pass `xmlns:*` manually as an
  attribute — ElementTree will emit `ns0:`/`ns1:` prefixes).
- All endpoints support both `GET` and `HEAD` (via `@app.api_route`); HEAD
  short-circuits to a body-less `Response` with full headers. Apple
  Podcasts probes enclosures with HEAD.
- Behind a reverse proxy: pass through `Range`, `If-None-Match`,
  `If-Modified-Since`. The app sets `ETag`, `Last-Modified`,
  `Cache-Control` on audio and transcript responses.

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
uv --project local run python -m py_compile local/scraper.py local/transcribe.py
uv --project cloud run python -m py_compile cloud/app.py

# Dump notebooks the scraper currently sees
( cd local && uv run scraper.py --list )

# Smoke-test the cloud app
( cd cloud && uv run uvicorn app:app --port 8000 ) &
curl -fsS http://127.0.0.1:8000/feed.xml | xmllint --noout - && echo VALID
curl -sI -H 'Range: bytes=0-1023' http://127.0.0.1:8000/audio/<hash>.mp3
# Expect: 206 Partial Content + Content-Range: bytes 0-1023/<size>

# Inspect cleaned descriptions for existing episodes
( cd local && uv run python -c "
import json, pathlib, os
from scraper import _clean_description
for f in pathlib.Path(os.environ['OUTPUT_DIR']).glob('*.json'):
    m = json.load(f.open()); print(m['title']); print(m['description'][:300]); print()
" )
```

## Style notes for this repo

- Single-purpose modules. Don't merge them unless asked.
- Each top-level script has a docstring with `uv run …` invocation examples.
- Logging via stdlib `logging` with the same format string everywhere.
- Defensive `try/except` is fine around external IO (Playwright, Gemini,
  file streaming), but log loudly — never swallow silently.
- Selector logic in `scraper.py` is the volatile part. Comment WHY a
  selector exists when you add one.

## When NotebookLM breaks the scraper

1. `cd local && uv run scraper.py --list` to see if listing still works.
2. If it returns 0 notebooks, `debug_home.html` will be written — inspect.
3. If listing works but a specific notebook fails, try
   `uv run scraper.py --url <URL>` and look at which extraction step blew up.
4. For description/title regressions, ask the user for a screenshot of the
   chat panel — guessing at selectors blind has burned cycles before.

## What not to do

- Don't add a streaming fallback to audio download. (User said download-only.)
- Don't auto-commit/push. (Global rule + repo rule.)
- Don't merge local/ and cloud/ back into one project. (They ship to
  different hosts.)
- Don't generate the feed from inside the scraper. (User wants them
  decoupled so the cloud-side can run independently.)
- Don't write `feed.xml` to disk from the cloud app. (Generated per request.)
- Don't escape spaces in `.env` paths with backslashes — write them
  verbatim or wrap in double quotes. The scripts normalize either form.
- Don't point Playwright at Chrome's real profile — it'll fail to attach
  while Chrome is running.
