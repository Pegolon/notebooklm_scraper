## Context

Currently, the post-processing pipeline in `scraper.py` runs sequential stages across all files:
1. `convert_missing(output_dir)` - Converts all `.mp3` files in the directory to `<hash>.m4a`.
2. `transcribe_missing(output_dir)` - Transcribes all `.m4a` files missing `.vtt` to WebVTT.
3. `summarise_missing(output_dir)` - Generates sidecar JSON summaries for `.m4a` files missing them.
4. `chapters_missing(output_dir)` - Generates chapter marks and embeds them in the `.m4a` files.
5. `cover_missing(output_dir)` - Renders `.png` cover art.
6. `tag_missing(output_dir)` - Updates MP4 tags.

If the script crashes or encounters an error during one of the earlier stages (e.g., an Ollama connection issue or an FFmpeg failure), none of the files get to proceed to the later stages (like embedding chapters or writing ID3 tags). 

## Goals / Non-Goals

**Goals:**
- Implement a per-file post-processing pipeline where each candidate file goes through the complete chain (`convert` -> `transcribe` -> `summarize` -> `chapters` -> `coverart` -> `id3tag`) before processing the next file.
- Prevent a failure in a specific file's post-processing from halting the processing of other files.
- Maintain existing command-line interfaces for individual modules (`convert.py`, `transcribe.py`, etc.).

**Non-Goals:**
- Multi-threaded or parallel processing of files. (Whisper and Ollama are CPU/GPU-intensive on the Mac Mini, so sequential execution is necessary to prevent overloading the system).
- Changing the actual logic of the post-processing modules (only their orchestration is modified).

## Decisions

### 1. Identify Candidate Files and Loop Per-File
We will update `_run_post_passes(output_dir)` in `local/scraper.py` to:
- Find all `.mp3` files in the output directory.
- For each `.mp3`, calculate its target `.m4a` filename by hashing its bytes.
- Run `convert_one` for the MP3, then run the downstream post-processing chain on the resulting `.m4a` file.
- Track all `.m4a` files processed from MP3s to avoid double-processing.
- Find all remaining `.m4a` files in the output directory and run the downstream post-processing chain on them.

### 2. Leverage Existing Per-File Functions
Each module already exposes a per-file execution function that we can import and call:
- `convert`: `convert_one(mp3, output_dir=output_dir)` and `_hash_file(mp3)`
- `transcribe`: `transcribe_one(m4a)`
- `summarize`: `summarise_one(m4a)`
- `chapters`: `chapters_one(m4a)`
- `coverart`: `generate_one(m4a)`
- `id3tag`: `tag_one(m4a)`

### 3. Idempotency Checks
To keep execution efficient, we check if the outputs exist before calling each stage:
- **Transcribe**: Check if `not m4a.with_suffix(".vtt").exists()`
- **Summarize**: Check if `not m4a.with_suffix(".json").exists()`
- **Chapters**: Check if `not m4a.with_suffix(".chaptermarks.txt").exists()`
- **Coverart**: Check if `not m4a.with_suffix(".png").exists()`
- **ID3Tag**: Check inside `tag_one(m4a)` (mutagen parses and detects if any atom is stale or missing).

## Risks / Trade-offs

- **[Risk]** A failure or hang in a single heavy step (e.g. LLM call) blocks the overall pipeline run.
  - *Mitigation*: Each individual call is run within a `_safe_stage` block that catches all exceptions and logs them, and respects the existing timeout environments (e.g., `SUMMARY_TIMEOUT_S`, `CHAPTERS_TIMEOUT_S`).
- **[Risk]** Extra imports and dependencies inside `scraper.py` might cause failure on non-Mac hosts (e.g. `mlx_whisper` import).
  - *Mitigation*: The transcription and MLX modules already lazily import heavy libraries only when running, and we will perform standard lazy imports/imports inside `_run_post_passes`.
