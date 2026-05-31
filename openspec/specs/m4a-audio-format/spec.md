# m4a-audio-format Specification

## Purpose
TBD - created by archiving change mp3-to-m4a-migration. Update Purpose after archive.
## Requirements
### Requirement: Canonical audio extension is M4A
The pipeline's canonical audio file extension SHALL be `.m4a` (AAC audio in an MP4/ISO Base Media container). All scripts that scan for audio files (scraper, transcribe, summarize, coverart, chapters, id3tag, cloud/app) SHALL scan for `*.m4a` instead of `*.mp3`.

#### Scenario: Pipeline scans for M4A files
- **WHEN** any pipeline script scans OUTPUT_DIR for audio files to process
- **THEN** it SHALL glob for `*.m4a` (case-insensitive) and ignore `*.mp3` files

#### Scenario: New episode file naming
- **WHEN** the scraper saves a new episode's audio to OUTPUT_DIR
- **THEN** the file SHALL be named `<hash>.m4a` where `<hash>` is the MD5 of the normalized description

---

### Requirement: Scraper format detection keeps MP4 containers
When NotebookLM delivers an MP4/ISO container (magic bytes `ftyp` at offset 4), the scraper SHALL keep the file as-is and save it with a `.m4a` extension. It SHALL NOT transcode MP4 containers to any other format.

#### Scenario: NotebookLM delivers MP4 container
- **WHEN** the downloaded audio has `ftyp` magic bytes at offset 4 (ISO Base Media)
- **THEN** the scraper SHALL save the file directly as `<hash>.m4a` without transcoding

#### Scenario: NotebookLM delivers raw MPEG audio
- **WHEN** the downloaded audio has ID3 header (`ID3`) or raw MPEG sync bytes (`\xff\xfb`) at the start
- **THEN** the scraper SHALL transcode the file to AAC in an M4A container and save it as `<hash>.m4a`

---

### Requirement: MPEG-to-M4A transcoding parameters
When the scraper or convert.py transcodes raw MPEG audio to M4A, the transcoding SHALL use AAC codec via ffmpeg. The ffmpeg command SHALL use `-c:a aac` (or the system's preferred AAC encoder), `-vn` to drop video/artwork streams, and write to an M4A container.

#### Scenario: Transcode MPEG to M4A via ffmpeg
- **WHEN** a raw MPEG audio file needs transcoding to M4A
- **THEN** ffmpeg SHALL be invoked with `-c:a aac`, `-vn`, and output to an `.m4a` file
- **AND** the output SHALL be written via a `.partial` temp file with atomic rename

---

### Requirement: Convert.py reverses direction to MP3-to-M4A
`convert.py` SHALL convert manually-dropped `.mp3` files to `<hash>.m4a` format (AAC in MP4 container). The hash SHALL be computed from the MD5 of the source MP3 bytes (streamed in 1 MiB chunks). Manually-dropped `.m4a` files are already in the target format and SHALL be left untouched by the conversion pass.

#### Scenario: Manual MP3 drop converted to M4A
- **WHEN** a user drops a `recording.mp3` file into OUTPUT_DIR
- **THEN** `convert.py` SHALL compute `md5(mp3-bytes)` and create `<hash>.m4a`
- **AND** the original `.mp3` file SHALL NOT be deleted or renamed

#### Scenario: Manual M4A drop is already target format
- **WHEN** a user drops a `recording.m4a` file into OUTPUT_DIR
- **THEN** `convert.py` SHALL NOT attempt to convert or process it
- **AND** the file SHALL be picked up by downstream passes (transcribe, summarize, etc.) by their normal `*.m4a` scan

#### Scenario: Convert idempotency via hash
- **WHEN** `convert.py` runs and `<hash>.m4a` already exists for a given `.mp3`
- **THEN** the conversion SHALL be skipped

#### Scenario: Atomic write during conversion
- **WHEN** ffmpeg is transcoding an MP3 to M4A
- **THEN** the output SHALL be written to `.<hash>.m4a.partial` first
- **AND** atomically renamed to `<hash>.m4a` on success
- **AND** the partial file SHALL be cleaned up on failure

---

### Requirement: JSON sidecar audio_file field uses M4A extension
The `audio_file` field in JSON sidecar files SHALL reference the `.m4a` filename. For newly created episodes, the scraper and summarize.py SHALL write `"audio_file": "<hash>.m4a"`. Existing sidecars are updated by the migration capability.

#### Scenario: Scraper writes new sidecar
- **WHEN** the scraper creates a new JSON sidecar for a scraped episode
- **THEN** the `audio_file` field SHALL be `"<hash>.m4a"`

#### Scenario: Summarize.py writes new sidecar for manual episode
- **WHEN** `summarize.py` creates a JSON sidecar for a manually-dropped M4A
- **THEN** the `audio_file` field SHALL be `"<hash>.m4a"`

---

### Requirement: RSS feed enclosure MIME type is audio/x-m4a
The cloud app's RSS feed builder SHALL set the enclosure `type` attribute to `audio/x-m4a` for M4A episodes.

#### Scenario: Feed XML enclosure type
- **WHEN** the cloud app builds the RSS feed XML
- **THEN** each `<enclosure>` element SHALL have `type="audio/x-m4a"`
- **AND** the `url` attribute SHALL point to `/audio/<hash>.m4a`

---

### Requirement: Cloud app serves M4A audio files
The cloud app's `/audio/{filename}` endpoint SHALL accept filenames with `.m4a` suffix and serve them with `Content-Type: audio/x-m4a`. The endpoint SHALL retain full HTTP Range support (single-range requests, 206 Partial Content, ETag, Last-Modified, Cache-Control). The file streaming SHALL be performed asynchronously and non-blockingly, reading the file in chunks of a configured size (e.g., 64 KB or 128 KB).

#### Scenario: Serve M4A with Range support
- **WHEN** a client requests `GET /audio/<hash>.m4a` with a `Range: bytes=0-1023` header
- **THEN** the server SHALL respond with `206 Partial Content`
- **AND** the `Content-Type` SHALL be `audio/x-m4a`
- **AND** the `Content-Range` header SHALL be present

#### Scenario: Serve M4A without Range header
- **WHEN** a client requests `GET /audio/<hash>.m4a` without a Range header
- **THEN** the server SHALL respond with `200 OK`
- **AND** the `Content-Type` SHALL be `audio/x-m4a`

#### Scenario: HEAD request for M4A
- **WHEN** a client sends `HEAD /audio/<hash>.m4a`
- **THEN** the server SHALL respond with full headers but an empty body
- **AND** the `Content-Type` SHALL be `audio/x-m4a`

#### Scenario: Path safety rejects non-M4A suffix
- **WHEN** a client requests `/audio/file.mp3`
- **THEN** the endpoint SHALL return `404 Not Found`

### Requirement: Cloud app discovers episodes via M4A scan
The cloud app's `load_episodes()` function SHALL iterate `*.m4a` files in OUTPUT_DIR (instead of `*.mp3`). Bare M4A files without a JSON sidecar SHALL still be discovered and have metadata synthesized from filename and mtime.

#### Scenario: Discover bare M4A without sidecar
- **WHEN** a bare `recording.m4a` exists in OUTPUT_DIR without a sibling `.json`
- **THEN** `load_episodes()` SHALL include it with synthesized metadata
- **AND** the synthesized `audio_file` SHALL be the M4A filename

#### Scenario: Discover M4A with sidecar
- **WHEN** `<hash>.m4a` and `<hash>.json` both exist in OUTPUT_DIR
- **THEN** `load_episodes()` SHALL use the sidecar metadata
- **AND** the `audio_file` field SHALL reference the `.m4a` file

---

### Requirement: Transcript and cover art sidecar association uses M4A stem
All sidecar files (`.vtt`, `.png`, `.json`, `.chaptermarks.txt`) SHALL share the same stem as the `.m4a` file. Scripts that check for sidecar existence (e.g., transcribe checking for `.vtt`, coverart checking for `.png`) SHALL look for siblings of `*.m4a` files.

#### Scenario: Transcribe finds M4A missing VTT
- **WHEN** `transcribe.py` scans OUTPUT_DIR
- **THEN** it SHALL find `*.m4a` files that lack a sibling `<stem>.vtt`
- **AND** transcribe those files

#### Scenario: Coverart finds M4A missing PNG
- **WHEN** `coverart.py` scans OUTPUT_DIR
- **THEN** it SHALL find `*.m4a` files that lack a sibling `<stem>.png`
- **AND** generate cover art for those files

#### Scenario: Cloud app finds transcript for M4A
- **WHEN** `load_episodes()` processes an M4A file
- **THEN** it SHALL check for a sibling `.vtt` file using the M4A's stem
- **AND** include `_transcript_file` in the episode metadata if found

