## 1. Implement MP4 Metadata Tagging in id3tag.py

- [x] 1.1 Update `local/id3tag.py` imports, replacing `mutagen.mp3` and `mutagen.id3` with `mutagen.mp4`.
- [x] 1.2 Implement the MP4 atom tag set mapping (e.g., `©nam`, `©ART`, `©alb`, `©gen`, `©day`, `©cmt`, `desc`, `pcst`, `purl`, `covr`, and the iTunes podcast GUID freeform atom).
- [x] 1.3 Update comparison and diff logic in `id3tag.py` to compare expected MP4 atoms with existing atoms on disk.
- [x] 1.4 Port `--check` and `--force` flag behaviors and error handling in `id3tag.py` to work with M4A files.


## 2. Implement M4A Chapter Embedding in chapters.py

- [x] 2.1 Update `local/chapters.py` to use `mutagen.mp4.MP4` for duration detection.
- [x] 2.2 Modify the ffmpeg command in `chapters.py` to use `-f ipod` output format and keep the audio codec stream copy (`-c:a copy`).
- [x] 2.3 Remove the metadata restoration import and call to `id3tag.tag_one()` in `chapters.py`.
- [x] 2.4 Update file globbing, `--file` argument validation, and CLI commands in `chapters.py` to target `.m4a` files.


## 3. Reverse Conversion Direction in convert.py

- [x] 3.1 Update `local/convert.py` to scan for manually-dropped `.mp3` files instead of `.m4a` files.
- [x] 3.2 Change transcoding function to `transcode_to_m4a()` using ffmpeg with AAC codec (`aac`), CBR `128k` bitrate (configurable via `AAC_BITRATE` env), and `-f ipod`.
- [x] 3.3 Handle manually-dropped `.m4a` files as no-ops in `convert.py`.
- [x] 3.4 Update environment variables, replacing `MP3_QUALITY` with `AAC_BITRATE` in `convert.py`.

## 4. Adapt Scraper, Transcriber, Summarizer, and Cover Art Scripts

- [x] 4.1 Update format detection in `local/scraper.py` (`save_episode`): keep MP4/ftyp containers as `.m4a`, and transcode MPEG audio downloads to `.m4a` via `convert.transcode_to_m4a()`.
- [x] 4.2 Update sidecar JSON writer in `local/scraper.py` to save `"audio_file": "<hash>.m4a"`.
- [x] 4.3 Update file globbing and CLI validation in `local/transcribe.py` to search for and verify `.m4a` files.
- [x] 4.4 Update `local/summarize.py` to scan for `.m4a` files, write `"audio_file": "<hash>.m4a"` in JSON sidecars, and verify input file suffix.
- [x] 4.5 Update `local/coverart.py` to scan for `.m4a` files and verify input file suffix.

## 5. Update Cloud Application serving M4A files

- [x] 5.1 Update `cloud/app.py` `load_episodes()` and `_synthesize_metadata()` functions to discover and parse `*.m4a` files instead of `*.mp3`.
- [x] 5.2 Update `/audio/{filename}` endpoint in `cloud/app.py` to validate `.m4a` extension, serve with `audio/x-m4a` Content-Type, and maintain HTTP Range support.
- [x] 5.3 Update RSS feed builder in `cloud/app.py` to output `<enclosure>` elements with `type="audio/x-m4a"` pointing to `/audio/<hash>.m4a`.

## 6. Build the Standalone Migration Script

- [x] 6.1 Create `local/migrate.py` that reads `OUTPUT_DIR` and processes all `*.mp3` files.
- [x] 6.2 Implement pre-flight checks in `migrate.py` to verify `ffmpeg` is available on the PATH.
- [x] 6.3 Implement MP3-to-M4A transcoding using `convert.transcode_to_m4a()`.
- [x] 6.4 Implement JSON sidecar update in `migrate.py` to set `"audio_file": "<hash>.m4a"`.
- [x] 6.5 Implement chapter re-embedding from existing `.chaptermarks.txt` using the ported `chapters.py` logic, while keeping the `.chaptermarks.txt` sidecar files (as recommended in open questions).
- [x] 6.6 Implement atomic rename pattern (`.partial` file) for output files, deletion of original `.mp3` files upon successful conversion, and `--dry-run`/`--keep-mp3` CLI flags.

## 7. Configuration, Documentation, and Attributions

- [x] 7.1 Update `local/.env.example` and `cloud/.env.example` configuration files to replace `MP3_QUALITY` with `AAC_BITRATE=128k`.
- [x] 7.2 Update project architecture documentation in `AGENTS.md` to reflect M4A/AAC as the primary audio format.
