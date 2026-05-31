## Why

The post-processing pipeline currently runs each post-pass stage (convert, transcribe, summarize, chapters, coverart, id3tag) in staged bulk loops across all files in the output directory. If the pipeline crashes mid-run (e.g., due to an LLM timeout, FFmpeg failure, or OOM), all files are left partially processed, and subsequent stages never execute for any of them. Running the complete post-processing chain per file isolates failures to a single file and ensures that other files can proceed to completion.

## What Changes

- Modify the pipeline orchestration to run the post-processing passes sequentially per file rather than in staged bulk loops over all files.
- The pipeline execution order for a file remains: convert (for MP3s) -> transcribe -> summarize -> chapters -> coverart -> id3tag.
- A failure in one stage for a specific file should be caught and logged, preventing it from blocking the next stages for other files.
- Refactor the detection logic to identify all candidate files (both MP3s needing conversion and M4As requiring downstream processing) and process them individually.

## Capabilities

### New Capabilities
- `per-file-pipeline`: Executes the sequential chain of post-processing steps (convert, transcribe, summarize, chapters, coverart, id3tag) on a per-file basis.

### Modified Capabilities
<!-- Existing capabilities whose REQUIREMENTS are changing (not just implementation).
     Only list here if spec-level behavior changes. Each needs a delta spec file.
     Use existing spec names from openspec/specs/. Leave empty if no requirement changes. -->

## Impact

- `local/scraper.py`: Update the orchestration in `_run_post_passes` to implement the per-file loop.
- `local/convert.py`: Expose helper/importable interfaces to check and run conversion for a single MP3 file.
- `local/transcribe.py`, `local/summarize.py`, `local/chapters.py`, `local/coverart.py`, `local/id3tag.py`: Ensure clean support for executing a single file's post-processing steps sequentially.
