# NotebookLM → Podcast

Three-part system that turns Google NotebookLM "Audio Overviews" into a
personal podcast RSS feed with transcripts:

1. **Scraper** ([scraper.py](scraper.py)) — automates Chromium via Playwright,
   downloads new audio overviews, writes `<hash>.mp3` + `<hash>.json` into a
   Google-Drive-synced folder. After each run, kicks off the transcription pass.
2. **Transcriber** ([transcribe.py](transcribe.py)) — for every MP3 in
   `OUTPUT_DIR` without a sibling `.vtt`, asks Google Gemini for a verbatim
   WebVTT transcript and writes it next to the audio.
3. **Feed generator** ([feed.py](feed.py)) — reads `OUTPUT_DIR`, builds an
   RSS 2.0 + iTunes feed (`feed.xml`). Runs independently on the
   serving/cloud side; also picks up bare MP3s with no sidecar JSON.

Built for **Python 3.14** and managed with **[uv](https://docs.astral.sh/uv/)**.

## Setup

Install uv (one-time, if you don't already have it):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Then, from this project directory:

```bash
cd ~/Documents/notebooklm_scraper

# uv reads .python-version and pyproject.toml, downloads Python 3.14 if needed,
# creates .venv/, and installs locked deps.
uv sync

# Install the Chromium browser Playwright drives.
uv run playwright install chromium

# Config
cp .env.example .env
# edit .env and set NOTEBOOK_URL and OUTPUT_DIR
```

## First-time login

```bash
uv run scraper.py --login
```

A Chromium window opens. Sign into Google, confirm you can see NotebookLM,
then press Enter in the terminal. Cookies are saved to `./playwright_profile/`.

## Scrape (cron / launchd)

```bash
uv run scraper.py
```

What happens on every run:

1. Opens the NotebookLM home page (forced to English UI) and enumerates every
   notebook visible.
2. Filters to notebooks **modified on/after `INITIAL_SINCE_DATE`** (default
   `2026-05-01`) that we haven't already scraped. "Already scraped" =
   notebook id present in `scraper_state.json` **or** in any
   `<hash>.json` already in `OUTPUT_DIR` — so deleting state never causes a
   re-download.
3. Processes the queue **oldest-first** (a mid-run failure leaves the newest
   notebooks still queued for the next run).
4. For each candidate: opens it, extracts the chat-panel summary as the
   description, clicks the Audio Overview kebab → Download, writes
   `<hash>.mp3` and `<hash>.json` to `OUTPUT_DIR`, and appends the
   notebook id to `scraper_state.json`.

### Debugging / one-off scrapes

```bash
uv run scraper.py --list                              # print notebooks the scraper sees
uv run scraper.py --url https://notebooklm.google.com/notebook/XXXX  # force a specific notebook
```

## Transcription (Part 2)

After every scrape, `scraper.py` invokes `transcribe.py`, which scans
`OUTPUT_DIR` for any `<name>.mp3` that doesn't have a matching `<name>.vtt`
and asks Gemini (default model: `gemini-2.5-flash`) for a verbatim WebVTT
transcript. Failures don't block other files.

```bash
# .env
GEMINI_API_KEY=...                  # https://aistudio.google.com/app/apikey
GEMINI_MODEL=gemini-2.5-flash       # optional override
```

```bash
uv run transcribe.py                # transcribe all MP3s missing a VTT
uv run transcribe.py --file foo.mp3 # transcribe a single file
```

Leave `GEMINI_API_KEY` empty to disable the pass entirely — the scraper just
skips it.

## RSS feed (Part 3)

`feed.py` is **independent of the scraper**. It scans `OUTPUT_DIR` for `*.mp3`
files and writes `feed.xml` next to them. Run it wherever the folder is
hosted — typically on the cloud/server side, not on the Mac Mini that scrapes.

For each MP3 it finds:

- If `<basename>.json` exists (the scraper writes these), use that rich
  metadata.
- Otherwise synthesise a minimal episode from the filename + mtime — so you
  can drop any `random.mp3` into the folder and it just appears in the feed.

Configure the public URL prefix and any optional channel metadata:

```bash
# .env (cloud side)
OUTPUT_DIR=/path/to/Podcasts             # the same folder served publicly
FEED_BASE_URL=https://podcasts.example.com
FEED_TITLE=NotebookLM Audio Overviews
FEED_AUTHOR=Markus Kirschner
FEED_OWNER_EMAIL=you@example.com
FEED_IMAGE_URL=https://podcasts.example.com/cover.png   # optional, 1400×1400+
```

```bash
uv run feed.py             # writes OUTPUT_DIR/feed.xml
uv run feed.py --stdout    # print to terminal for inspection
```

Set this up on its own schedule (cron / launchd / systemd timer / cloud
function) — every few minutes is plenty, since the script is cheap.

### Serving the folder

`feed.py` does **not** ship a web server. You're responsible for exposing
`OUTPUT_DIR` over HTTP under `FEED_BASE_URL`. Options:

- a static-site CDN (Cloudflare R2 / S3 / Backblaze B2 with rclone sync)
- nginx / Caddy pointed at the directory, fronted by Cloudflare Tunnel /
  Tailscale Funnel
- any local web server reachable from your podcast client

Each episode's enclosure URL is built as `f"{FEED_BASE_URL}/{audio_file}"`,
so as long as the `.mp3` is fetchable at that URL the feed works.

Point your podcast client at `<FEED_BASE_URL>/feed.xml`.

### Example launchd plist (runs every 30 min)

`~/Library/LaunchAgents/com.you.notebooklm.scraper.plist`:

> **Note**: launchd does **not** expand `~` or `$HOME` inside `<string>`
> values — every path must be absolute. Replace `USERNAME` below with the
> output of `whoami`, and check `which uv` for the correct binary path.

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.you.notebooklm.scraper</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/USERNAME/.local/bin/uv</string>
    <string>run</string>
    <string>--project</string>
    <string>/Users/USERNAME/Documents/notebooklm_scraper</string>
    <string>scraper.py</string>
  </array>
  <key>WorkingDirectory</key><string>/Users/USERNAME/Documents/notebooklm_scraper</string>
  <key>StartInterval</key><integer>1800</integer>
  <key>StandardOutPath</key><string>/Users/USERNAME/Documents/notebooklm_scraper/scraper.log</string>
  <key>StandardErrorPath</key><string>/Users/USERNAME/Documents/notebooklm_scraper/scraper.err</string>
</dict>
</plist>
```

Load with: `launchctl load ~/Library/LaunchAgents/com.you.notebooklm.scraper.plist`

## Notes / gotchas

- The user-data dir defaults to `./playwright_profile/`, **not** your real
  Chrome `Profile 1`. Chrome locks its own profile while running, which breaks
  Playwright. If you really want to point at a Chrome profile, close Chrome
  first and set `USER_DATA_DIR` in `.env`.
- NotebookLM's DOM changes regularly. Selectors live in `scraper.py` in
  `extract_description()` and `trigger_audio_download()` — adjust there if
  Google rearranges things.
- Idempotency is keyed on the **description text hash**, per spec. If Google
  regenerates the overview with new text, you'll get a new episode (intended).
