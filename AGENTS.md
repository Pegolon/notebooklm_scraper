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
- **local/**: Playwright (Chromium), mlx-whisper, Pillow, python-dotenv; the summarisation pass talks to a remote Ollama HTTP API (no Python client dep); M4A conversion shells out to `ffmpeg` (system binary on PATH); cover-art is pure-Python (Pillow + Apple Color Emoji), no AI / no network
- **cloud/**: FastAPI, uvicorn[standard], python-dotenv
- Standard library `xml.etree.ElementTree` for RSS (no extra dep)

## File map

| Path | Role |
|---|---|
| [local/scraper.py](local/scraper.py) | Playwright scraper. Lists notebooks, picks new ones, downloads audio, writes JSON, runs convert → transcribe → summarise → cover-art passes at end (sequential — order matters for manually-dropped audio). |
| [local/convert.py](local/convert.py) | ffmpeg M4A → `<md5(m4a-bytes)>.mp3`. Scans for `.m4a` files; idempotent (hash-based output filename, skips if `<hash>.mp3` already exists). Standalone CLI + importable `convert_missing()`. |
| [local/transcribe.py](local/transcribe.py) | MLX Whisper → WebVTT (on-device). Scans for MP3s missing `.vtt`. Standalone CLI + importable `transcribe_missing()`. |
| [local/summarize.py](local/summarize.py) | Ollama text model → `<basename>.json`. Scans for MP3s missing `.json` but having `.vtt`; strips VTT cue headers, asks Ollama (JSON mode) for `{title, description, emoji}`, validates the emoji (rejects shortcodes / words via `_extract_emoji()`), writes a sidecar mirroring scraper.py's shape (`source: "manual"`, `notebook_emoji` populated from the LLM). Also exposes `--backfill-emojis` (cheap emoji-only LLM call for existing manual sidecars). Standalone CLI + importable `summarise_missing()` / `backfill_emojis()`. |
| [local/coverart.py](local/coverart.py) | Pillow → `<hash>.png` (1400×1400). Reads the `notebook_emoji` field scraper.py captured from NotebookLM's auto-assigned icon and renders it full-bleed on a per-episode gradient circle. No AI, no network. Scans for MP3s missing `.png`. Standalone CLI + importable `cover_missing()`. |
| [local/pyproject.toml](local/pyproject.toml) | Deps: `playwright`, `python-dotenv`, `mlx-whisper`, `pillow`, `mutagen`, `google-genai`. |
| [local/.env.example](local/.env.example) | Local-side config template. |
| [cloud/app.py](cloud/app.py) | FastAPI app: `GET /feed.xml` + `GET /audio/{name}` (range-aware) + `GET /transcripts/{name}` + `GET /images/{name}`. Reads files directly from `OUTPUT_DIR`. |
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
uv run scraper.py --backfill-emojis    # fill notebook_emoji into legacy JSONs, rerender covers
uv run convert.py                      # transcode any .m4a files to <hash>.mp3
uv run convert.py --file foo.m4a       # convert one file
uv run transcribe.py                   # transcribe any MP3 missing a .vtt
uv run transcribe.py --file foo.mp3    # transcribe one file
uv run summarize.py                    # generate JSON sidecars for manual MP3s (needs .vtt)
uv run summarize.py --file foo.mp3     # summarise one file
uv run summarize.py --backfill-emojis  # ask LLM for an emoji for existing sidecars missing one
uv run coverart.py                     # generate cover PNGs for any MP3 missing one
uv run coverart.py --file foo.mp3      # generate one cover
uv run coverart.py --force             # regenerate everything

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
- Cover art (Pillow, on-device, no network): `COVER_SIZE` (default
  `1400`), `COVER_DEFAULT_EMOJI` (default `🎙️` — used when the sidecar
  JSON has no `notebook_emoji`), `APPLE_EMOJI_FONT` (default
  `/System/Library/Fonts/Apple Color Emoji.ttc`).
- Summarisation of manual MP3s (Ollama, same host): `OLLAMA_TEXT_MODEL`
  (default `charaf/qwen3.6-35b-a3b-coding-nvfp4-mlx:latest`),
  `SUMMARY_TIMEOUT_S` (default `600`). `TITLE_PREFIX` is reused to keep
  manual episode titles in the same `<prefix> - <topic>` shape as scraper
  output.

`cloud/.env` knobs:
- Required: `OUTPUT_DIR`, `FEED_BASE_URL` (the public URL of the FastAPI
  app itself, no trailing slash — enclosure URLs are built as
  `${FEED_BASE_URL}/audio/<filename>.mp3`)
- Optional: `FEED_TITLE`, `FEED_DESCRIPTION`, `FEED_AUTHOR`,
  `FEED_OWNER_EMAIL`, `FEED_LANGUAGE`, `FEED_CATEGORY`, `FEED_IMAGE_URL`
  (drop a `cover.png` into `OUTPUT_DIR` and set this to
  `${FEED_BASE_URL}/images/cover.png`), `FEED_LINK`

The cloud app assumes the `OUTPUT_DIR` folder is reachable as a regular
filesystem path. On hosts that can't mount Google Drive directly, run the
cloud app on the Mac Mini (where Drive is mounted) and expose it via
Tailscale Serve/Funnel, Cloudflare Tunnel, or similar.

### Path-value normalization (important gotcha)

Users naturally write shell-style escapes in `.env`:
```
OUTPUT_DIR=/Users/.../My\ Drive/Podcasts        # backslash escape
OUTPUT_DIR="/Users/.../My Drive/Podcasts"       # quoted
```
`python-dotenv` reads values **literally** — it does not unescape `\ ` or
strip quotes. All six scripts (`local/scraper.py`, `local/convert.py`,
`local/transcribe.py`, `local/summarize.py`, `local/coverart.py`,
`cloud/app.py`) defensively run path values through `_clean_path_value()`
to handle both styles. Keep this helper when adding new scripts.

## Data model

Each scraped episode produces two sibling files (and later `.vtt` + `.png`):

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
    "notebook_emoji": "<single emoji glyph or null>",
    "notebook_modified": "<ISO-8601 UTC>"
  }
  ```
  `notebook_emoji` is whatever icon NotebookLM auto-assigned to the
  notebook (captured by `parse_card_emoji()` from the home-page card).
  Default scrape runs populate it for every new episode and also run
  `backfill_emojis_into_json()` opportunistically — any pre-existing
  sidecar whose `notebook_id` matches a currently-visible notebook gets
  its emoji filled in (and its now-stale cover PNG deleted so the
  cover-art pass rerenders it). `--url` overrides without a card to
  read from leave it null; coverart.py falls back to
  `COVER_DEFAULT_EMOJI` for those. Use
  `uv run scraper.py --backfill-emojis` to run the backfill + cover
  rerender + id3 retag without scraping any new episodes.
- **`<hash>.vtt`** — WebVTT transcript (added by `transcribe.py`).
- **`<hash>.png`** — cover art (added by `coverart.py`).

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

## M4A → MP3 conversion specifics (manual audio ingest)

- Lets the user drop arbitrary `.m4a` files (e.g. from Voice Memos) into
  `OUTPUT_DIR` and have them join the pipeline. Runs as the first
  post-scrape pass in `scraper.py`; also standalone via `uv run convert.py`.
- **Idempotency hinges on the output filename being a hash of the input
  bytes** (`md5(m4a-bytes).mp3`, streamed read in 1 MiB chunks). On every
  run we re-hash each `.m4a`, check for an existing `<hash>.mp3`, and skip
  if it's already there. This means the source `.m4a` can stay in the
  folder indefinitely without ever re-encoding, and renaming the m4a
  doesn't trigger a re-encode either (same bytes → same hash → skip). To
  force a fresh encode, use `--force`.
- The original `.m4a` is **never deleted or renamed**. The user can clean
  up by hand once they're satisfied with the MP3.
- Encoder: `ffmpeg -vn -c:a libmp3lame -q:a $MP3_QUALITY` (VBR, default
  q=2 ≈ 190 kbps). `-vn` drops any embedded artwork/video stream that
  Voice Memos and the like sometimes carry.
- We write to `.<hash>.mp3.partial` first, then `Path.replace()` (atomic
  rename) to the final name — a crash mid-encode cannot leave a corrupt
  `<hash>.mp3` that the idempotency check would later treat as finished.
- Fails loudly if `ffmpeg` isn't on PATH (`shutil.which("ffmpeg")`); does
  not silently fall back to anything. macOS install: `brew install ffmpeg`.
- Once the `.mp3` lands, the downstream passes pick it up by their normal
  scan rules — no special-casing needed for "this MP3 came from an M4A".

## Summarisation specifics (manual MP3 ingest)

- Lets the user drop an arbitrary `<basename>.mp3` into `OUTPUT_DIR` and
  still get a proper feed entry. Triggered automatically by `scraper.py`
  between the transcribe and cover-art passes; also runnable standalone
  via `uv run summarize.py`.
- Acts only on MP3s that **lack** a `<basename>.json`. Scraper-downloaded
  episodes already have one, so the pass is a no-op for them.
- Needs a matching `<basename>.vtt` next to the MP3 — if it's missing
  (transcription hasn't run yet or failed), the file is skipped with a
  warning. Run transcribe.py first.
- VTT → plain text via `_vtt_to_text()`: drops the `WEBVTT` header,
  numeric cue ids, and `HH:MM:SS.mmm --> ...` timestamp lines, then joins
  remaining lines into paragraphs separated by the original blank lines.
- Sends the (truncated at 60k chars) transcript to Ollama's
  `POST /api/generate` with `format: "json"` and a low temperature (0.3).
  We **do not stream** the response (it's a single text completion, not
  long-running like image gen). The model returns a JSON object with
  `title`, `description`, and `emoji`; the first two are validated to
  be non-empty before we write the sidecar. The `emoji` field is passed
  through `_extract_emoji()` which accepts a single emoji cluster
  (including ZWJ sequences like 🏃‍♂️, flag pairs like 🇪🇺, and
  variation-selected glyphs like 🎙️) but rejects anything that's
  plainly text (`"microphone"`, `":mic:"`, `"🎙️ — microphone"`). A
  rejected or absent emoji is stored as `null`; coverart.py then falls
  back to `COVER_DEFAULT_EMOJI` for that episode.
- The written JSON mirrors `save_episode()`'s shape: `id`, `title`,
  `description`, `audio_file`, `pub_date`, plus null `notebook_*` fields
  and `"source": "manual"` so it's easy to spot manual entries. `id` is
  `md5(filename)` (same scheme the cloud app uses for synthesised GUIDs
  on bare MP3s — keeps the feed GUID stable when the sidecar is added).
- `pub_date` comes from the MP3's mtime, so dropping an old file in won't
  pretend it's brand new.
- Title is formatted as `"<TITLE_PREFIX> - <topic>"` to match the rest of
  the feed; if the model already prepended the prefix we strip it before
  prepending again.
- `summarize.py --backfill-emojis` walks any existing sidecar whose
  `notebook_emoji` is missing/empty and asks the LLM for *only* an
  emoji (using a tiny prompt, leaving title/description untouched),
  then triggers cover rerender + ID3 retag — symmetric with
  `scraper.py --backfill-emojis`. Use this after upgrading from a
  summarize.py that pre-dates the LLM emoji field.

## Cover-art specifics

- Runs after the summary pass in `scraper.py` (and on demand via
  `coverart.py`). For every `<hash>.mp3` lacking a sibling `<hash>.png`,
  read `notebook_emoji` + `title` from `<hash>.json` and render a
  `COVER_SIZE`×`COVER_SIZE` PNG (default 1400²) showing that emoji
  full-bleed on a per-episode gradient circle. Everything happens in
  one Pillow process — no AI, no Ollama, no network.
- **Emoji rendering uses Pillow with `embedded_color=True`** so we get
  the actual colour bitmaps from Apple Color Emoji's `sbix` tables.
  ImageMagick + FreeType on macOS **cannot** decode sbix and produces
  black silhouettes; same goes for `rsvg-convert` via the SVG delegate.
  Don't try to swap this for an ImageMagick pipeline unless the user
  has installed a COLR/CPAL emoji font (e.g. Noto Color Emoji built
  with COLRv1 support).
- Apple Color Emoji only ships specific sbix sizes (20/40/64/96/160 px
  on current macOS). We render at 160 into a padded 200² RGBA canvas
  (`int(160*1.25)`) so ZWJ sequences with overhang aren't clipped, then
  Lanczos-upscale to ~62% of the canvas edge (~870 px for 1400² output).
  Going much above 6× starts to soften noticeably.
- Gradient colours come from `_stable_hues()` — a hash of the episode
  title yields two hues 80–160° apart on the wheel, so every episode
  gets a distinct but reproducible look. Same title in, same colours
  out. The gradient is built as a 1-px horizontal ramp resized to a
  square and rotated 45° for a diagonal sweep.
- Bytes are written via `.<basename>.png.partial` + `Path.replace()`
  (atomic rename) so a crash mid-write can never leave behind a
  half-flushed `<hash>.png` that the idempotency check would later
  treat as finished.
- Falls back to `COVER_DEFAULT_EMOJI` (🎙️ by default) whenever the
  sidecar JSON has no `notebook_emoji` — manually-dropped MP3s,
  `--url`-forced scraper runs, and legacy episodes scraped before
  emoji capture was added. `notebook_emoji` itself is captured by
  `scraper.py`'s `parse_card_emoji()` from the first line of each
  home-page card and stored in the JSON.
- Failures don't block the rest of the pass; logged and moved on. The
  pass is also a no-op when there are no missing covers, so reruns of
  `scraper.py` are cheap.

## Cloud app (FastAPI) specifics

- **No disk writes.** The feed is built per request — there is no `feed.xml`
  stored in `OUTPUT_DIR` anymore.
- **`/audio/{filename}`** streams MP3s with HTTP Range support (single
  range only). Apple Podcasts and Overcast require Range to seek. Multi-
  range and zero-length suffix ranges return 416.
- **`/transcripts/{filename}`** serves WebVTT (`.vtt`) files whole (they're
  tens-to-hundreds of KB; no Range support). Content-Type is
  `text/vtt; charset=utf-8`.
- **`/images/{filename}`** serves `.png` cover images whole (Content-Type
  `image/png`). Used both for per-episode `<itunes:image>` (`<hash>.png`
  written by [local/coverart.py](local/coverart.py)) and for the
  show-level cover referenced by `FEED_IMAGE_URL` (drop a `cover.png` into
  `OUTPUT_DIR` and point `FEED_IMAGE_URL` at
  `${FEED_BASE_URL}/images/cover.png`).
- Each `/feed.xml` item with a sibling `.vtt` next to its `.mp3` gets a
  Podcasting 2.0 `<podcast:transcript url=… type="text/vtt" lang=…>` tag.
  Language is `FEED_LANGUAGE.split("-")[0]` (so `en-us` → `en`).
- Each item with a sibling `.png` next to its `.mp3` gets an
  `<itunes:image href=…>` tag. Apple Podcasts prefers ≥1400×1400 — the
  512×512 default of [local/coverart.py](local/coverart.py) may be
  silently rejected there even if Overcast/Pocket Casts show it fine.
- **Path safety**: `_safe_resolve()` rejects names containing `/`, `\`,
  leading `.`, wrong suffix, or that fall outside `OUTPUT_DIR` after
  `.resolve()`. All three of `/audio`, `/transcripts`, `/images` share it
  (different `suffix` arg per call).
- Iterates `*.mp3` (NOT `*.json`) — bare MP3s dropped into the folder are
  picked up too. Synthesised metadata uses filename + mtime; GUID is
  `md5(filename)` so reruns stay stable.
- Enclosure URLs: `f"{FEED_BASE_URL}/audio/{audio_file}"`. Transcript URLs:
  `f"{FEED_BASE_URL}/transcripts/{vtt_file}"`. Image URLs:
  `f"{FEED_BASE_URL}/images/{png_file}"`. `FEED_BASE_URL` is the public
  URL of the FastAPI app itself.
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
uv --project local run python -m py_compile local/scraper.py local/convert.py local/transcribe.py local/summarize.py local/coverart.py
uv --project cloud run python -m py_compile cloud/app.py

# Confirm the Ollama host is reachable and the image model is installed
curl -fsS "$OLLAMA_BASE_URL/api/tags" | python3 -c "import sys,json; print([m['name'] for m in json.load(sys.stdin).get('models',[])])"

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
- Defensive `try/except` is fine around external IO (Playwright, MLX
  Whisper, Ollama, file streaming), but log loudly — never swallow silently.
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
