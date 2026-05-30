# local/ — NotebookLM scraper, transcriber, cover-art generator

Runs on the Mac Mini (or wherever you have a logged-in browser). Downloads
new Audio Overviews from NotebookLM into the Google-Drive-synced
`OUTPUT_DIR`, transcribes them locally via MLX Whisper, and renders a
per-episode cover image showing the notebook's auto-assigned emoji on a
gradient circle (pure Pillow — no AI, no network).

Every `scraper.py` run does all five passes in sequence; each one is also
runnable standalone for backfills.

```bash
uv sync                                # install deps into .venv
uv run playwright install chromium     # one-time browser install
cp .env.example .env                   # then edit OUTPUT_DIR (everything else optional)
uv run scraper.py --login              # one-time, headful, sign into Google
uv run scraper.py                      # main entry point (cron / launchd)
uv run scraper.py --list               # debug: list visible notebooks (with emojis)
uv run scraper.py --url <NOTEBOOK_URL> # force a specific notebook
uv run transcribe.py                   # transcribe any MP3 missing a .vtt
uv run transcribe.py --file foo.mp3    # transcribe one file
uv run coverart.py                     # render cover PNGs for any MP3 missing one
uv run coverart.py --file foo.mp3      # render one cover
uv run coverart.py --force             # regenerate every cover
```

### Cover art

A 1400×1400 PNG per episode, with the notebook's emoji rendered full-bleed
on top of a per-episode gradient circle (the gradient hues are derived
from a stable hash of the episode title, so each cover is distinct but
reproducible). The emoji is whichever icon NotebookLM auto-assigned to
the notebook — `scraper.py` captures it from the home-page card and
stores it in the JSON sidecar as `notebook_emoji`. Manually-dropped MP3s
and `--url`-forced runs fall back to `COVER_DEFAULT_EMOJI` (🎙️ by default).

Apple Color Emoji ships only as `sbix` bitmap tables, which ImageMagick /
FreeType on macOS can't decode (you get a black silhouette). Pillow
renders the bitmaps directly via `embedded_color=True`, so the whole
pipeline is one Pillow process — no shell-out, no Ollama, no model
weights.

See the top-level [README.md](../README.md) for the full setup story, launchd
example, and gotchas.
