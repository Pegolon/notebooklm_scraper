# local/ — NotebookLM scraper, transcriber, cover-art generator

Runs on the Mac Mini (or wherever you have a logged-in browser). Downloads
new Audio Overviews from NotebookLM into the Google-Drive-synced
`OUTPUT_DIR`, transcribes them locally via MLX Whisper, and renders a
per-episode cover image via a remote Ollama instance (e.g. FLUX.2 Klein).

Every `scraper.py` run does all three in sequence; the transcribe and
cover-art passes are also runnable standalone for backfills.

```bash
uv sync                                # install deps into .venv
uv run playwright install chromium     # one-time browser install
cp .env.example .env                   # then edit OUTPUT_DIR + OLLAMA_BASE_URL
uv run scraper.py --login              # one-time, headful, sign into Google
uv run scraper.py                      # main entry point (cron / launchd)
uv run scraper.py --list               # debug: list visible notebooks
uv run scraper.py --url <NOTEBOOK_URL> # force a specific notebook
uv run transcribe.py                   # transcribe any MP3 missing a .vtt
uv run transcribe.py --file foo.mp3    # transcribe one file
uv run coverart.py                     # render cover PNGs for any MP3 missing one
uv run coverart.py --file foo.mp3      # render one cover
uv run coverart.py --force             # regenerate every cover
```

### Cover-art prerequisites

The cover-art pass talks to Ollama over HTTP. Point `OLLAMA_BASE_URL` at a
host running an image-generation model (default: `x/flux2-klein:9b`):

```bash
# on the Ollama host (one-time)
ollama pull x/flux2-klein:9b
# (optional) bind to all interfaces so other machines can reach it
OLLAMA_HOST=0.0.0.0:11434 ollama serve

# from the Mac Mini, smoke-test reachability
curl -fsS "$OLLAMA_BASE_URL/api/tags"
```

Defaults are tuned conservatively (512×512 / 12 diffusion steps, ~2 min
per cover on FLUX.2 Klein 9B). Bump `COVER_WIDTH/HEIGHT/STEPS` in `.env`
for Apple-Podcasts-spec 1400² art and raise `COVER_TIMEOUT_S` accordingly.

See the top-level [README.md](../README.md) for the full setup story, launchd
example, and gotchas.
