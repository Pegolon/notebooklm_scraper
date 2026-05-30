# NotebookLM → Podcast

Two-part system that turns Google NotebookLM "Audio Overviews" into a
personal podcast RSS feed with transcripts:

- [**local/**](local/) — Mac Mini side. Playwright scraper, on-device
  MLX-Whisper transcriber, and Ollama-driven cover-art generator. Writes
  `<hash>.mp3` / `.json` / `.vtt` / `.png` quartets into a
  Google-Drive-synced folder (`OUTPUT_DIR`).
- [**cloud/**](cloud/) — Wherever you can publish. FastAPI app that reads
  the same folder (synced down to the cloud box via Google Drive), generates
  the RSS feed on the fly, streams MP3s to podcast clients with HTTP
  Range support, and serves WebVTT transcripts via the Podcasting 2.0
  `<podcast:transcript>` tag.

```diagram
╭─ Mac Mini ─────────────────────╮      ╭─ Cloud / VPS ──────────────────────────╮
│ local/scraper.py               │      │ cloud/app.py  (FastAPI / uvicorn)      │
│   → MP3 + JSON sidecar         │      │   GET /feed.xml         →  RSS XML     │
│ local/transcribe.py            │      │   GET /audio/<n>        →  MP3 (Range) │
│   → <name>.vtt                 │      │   GET /transcripts/<n>  →  WebVTT      │
│ local/coverart.py              │      ╰──────────────┬─────────────────────────╯
│   → <name>.png                 │                     │
╰──────────┬─────────────────────╯                     │ reads
           ▼ writes into                               │
    ╭────────────────────────────╮  ◀─── same folder ──╯
    │ OUTPUT_DIR (Google Drive)  │
    │   ↳ <hash>.mp3             │
    │   ↳ <hash>.json            │
    │   ↳ <hash>.vtt             │
    │   ↳ <hash>.png             │   (manual MP3 drops also work)
    ╰────────────────────────────╯
```

Both halves are independent uv-managed Python 3.14 projects. Each has its
own `pyproject.toml`, `.env.example`, and `README.md`. Deploy them
separately.

## Quick start

Install [uv](https://docs.astral.sh/uv/) once:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### Local (scraper + transcriber)

```bash
cd local
uv sync
uv run playwright install chromium
cp .env.example .env                   # set OUTPUT_DIR (+ optional OLLAMA_BASE_URL for cover art)
uv run scraper.py --login              # one-time Google sign-in (headful)
uv run scraper.py                      # main entry point
```

See [local/README.md](local/README.md) for all commands and the launchd recipe.

### Cloud (FastAPI feed server)

```bash
cd cloud
uv sync
cp .env.example .env                   # set OUTPUT_DIR and FEED_BASE_URL
uv run uvicorn app:app --host 0.0.0.0 --port 8000
```

Subscribe podcast clients to `${FEED_BASE_URL}/feed.xml`. See
[cloud/README.md](cloud/README.md) for endpoint details and reverse-proxy
notes.

## Configuration shared between halves

Both halves need their own `.env` files, but the only setting that must
agree between them is **`OUTPUT_DIR`** — both must resolve to the same
synced Google Drive folder. Everything else (Whisper / Ollama settings on
local, RSS metadata on cloud) is one-sided.

## Data model

Each scraped episode produces this set of sibling files in `OUTPUT_DIR`:

- **`<hash>.mp3`** — the Audio Overview download.
- **`<hash>.json`** — metadata: title, description, pub_date, source notebook id/URL.
- **`<hash>.vtt`** — WebVTT transcript (added by the transcribe pass).
- **`<hash>.png`** — cover art themed by the description (added by the cover-art pass).

`<hash>` is `md5(normalized_description)` so regeneration of the same
NotebookLM content is idempotent.

If a bare `.mp3` is dropped into `OUTPUT_DIR` with no sidecar JSON, the
cloud feed synthesises a minimal episode from the filename + mtime so the
file still shows up.

## Notes / gotchas

- The Playwright user-data dir defaults to `local/playwright_profile/`,
  **not** your real Chrome `Profile 1`. Chrome locks its own profile while
  running, which breaks Playwright. If you want to point at a real Chrome
  profile, close Chrome first and set `USER_DATA_DIR` in `local/.env`.
- NotebookLM's DOM changes regularly. Selectors live in
  [local/scraper.py](local/scraper.py) in `extract_description()` and
  `download_audio_overview()` — adjust there if Google rearranges things.
- Idempotency is keyed on the **description text hash**. If Google
  regenerates the overview with new text, you'll get a new episode
  (intended).
- The cloud app generates the feed per request — no `feed.xml` is written
  to `OUTPUT_DIR` anymore.
