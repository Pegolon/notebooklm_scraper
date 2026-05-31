## 1. Implement Incremental Per-File Loop in scraper.py

- [x] 1.1 Refactor `_run_post_passes` in `local/scraper.py` to identify candidate MP3 files and convert them to M4A.
- [x] 1.2 Implement the per-file post-processing pipeline for M4A files, running transcribe, summarize, chapters, coverart, and id3tag in sequence for each file.
- [x] 1.3 Add robust exception handling for each individual stage of a file's pipeline so that errors do not halt the rest of the queue.

## 2. Testing and Validation

- [x] 2.1 Verify that the per-file pipeline executes successfully on a directory with mixed MP3 and M4A files.
- [x] 2.2 Verify that a failure in one file (e.g. invalid VTT/MP3 file) is isolated and does not block processing of subsequent files.
