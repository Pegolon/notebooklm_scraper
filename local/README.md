# local/ — NotebookLM scraper + transcriber

Runs on the Mac Mini (or wherever you have a logged-in browser). Downloads new
Audio Overviews from NotebookLM into the Google-Drive-synced `OUTPUT_DIR` and
transcribes them locally via MLX Whisper.

```bash
uv sync                                # install deps into .venv
uv run playwright install chromium     # one-time browser install
cp .env.example .env                   # then edit OUTPUT_DIR (model defaults are fine)
uv run scraper.py --login              # one-time, headful, sign into Google
uv run scraper.py                      # main entry point (cron / launchd)
uv run scraper.py --list               # debug: list visible notebooks
uv run scraper.py --url <NOTEBOOK_URL> # force a specific notebook
uv run transcribe.py                   # transcribe any MP3 missing a .vtt
uv run transcribe.py --file foo.mp3    # transcribe one file
```

See the top-level [README.md](../README.md) for the full setup story, launchd
example, and gotchas.
