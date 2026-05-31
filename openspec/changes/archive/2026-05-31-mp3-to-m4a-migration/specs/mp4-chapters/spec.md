## ADDED Requirements

### Requirement: Chapter embedding uses FFmpeg ipod muxer
`chapters.py` SHALL embed chapter marks into M4A files using FFmpeg with the `-f ipod` output muxer (instead of `-f mp3`). The FFmpeg command SHALL copy the audio stream (`-c:a copy`), apply the FFMETADATA1 chapter file via `-map_metadata 1 -map_chapters 1`, and output to an M4A container.

#### Scenario: FFmpeg invocation for chapter embedding
- **WHEN** `chapters.py` embeds chapters into an M4A file
- **THEN** the ffmpeg command SHALL include `-f ipod` as the output format
- **AND** it SHALL include `-c:a copy` to avoid re-encoding the audio
- **AND** it SHALL include `-map_metadata 1 -map_chapters 1` to apply the chapter metadata

#### Scenario: Output is valid M4A with chapters
- **WHEN** FFmpeg successfully embeds chapters
- **THEN** the resulting file SHALL be a valid MP4/M4A container with embedded chapter atoms
- **AND** the audio stream SHALL be bit-identical to the input (copy, not re-encode)

---

### Requirement: Chapter embedding SHALL NOT wipe existing MP4 atoms
MP4 chapter embedding via FFmpeg with `-f ipod` and `-c:a copy` SHALL preserve existing MP4 metadata atoms (such as `©nam`, `©ART`, `covr`, etc.). There SHALL be no post-embed tag restoration step — unlike the MP3 pipeline, where `-map_metadata 1` on MP3 files wiped ID3 tags and required a `id3tag.tag_one()` call to restore them.

#### Scenario: Existing tags preserved after chapter embed
- **WHEN** an M4A file already has MP4 atoms (`©nam`, `©ART`, `covr`, etc.) and chapters are embedded
- **THEN** the existing MP4 atoms SHALL still be present in the output file
- **AND** `chapters.py` SHALL NOT call `id3tag.tag_one()` or any tag restoration function after embedding

#### Scenario: No id3tag import in chapters.py
- **WHEN** `chapters.py` successfully embeds chapters
- **THEN** it SHALL NOT import or invoke `id3tag.tag_one()` to restore metadata
- **AND** the chapter embedding step SHALL be self-contained

---

### Requirement: FFMETADATA1 format is unchanged
The FFMETADATA1 chapter file format SHALL remain the same as the current MP3 pipeline. Each chapter SHALL have `TIMEBASE=1/1000`, `START` and `END` in milliseconds, and an escaped `title`. The file header SHALL be `;FFMETADATA1` followed by the episode title.

#### Scenario: Chapter marks file content
- **WHEN** `chapters.py` generates a `.chaptermarks.txt` file
- **THEN** the file SHALL start with `;FFMETADATA1`
- **AND** each `[CHAPTER]` section SHALL have `TIMEBASE=1/1000`, `START=<ms>`, `END=<ms>`, and `title=<escaped>`
- **AND** the first chapter SHALL have `START=0`

#### Scenario: Chapter file naming
- **WHEN** `chapters.py` generates chapter marks for `<hash>.m4a`
- **THEN** the chapter file SHALL be named `<hash>.chaptermarks.txt`

---

### Requirement: Duration detection uses mutagen.mp4.MP4
`chapters.py` SHALL determine audio duration using `mutagen.mp4.MP4` instead of `mutagen.mp3.MP3`. The duration in milliseconds SHALL be computed as `int(round(audio.info.length * 1000))`.

#### Scenario: Duration from M4A via mutagen
- **WHEN** `chapters.py` needs the audio duration of an M4A file
- **THEN** it SHALL open the file with `mutagen.mp4.MP4(str(path))`
- **AND** compute `int(round(audio.info.length * 1000))` for the duration in milliseconds

#### Scenario: Duration fallback to VTT timestamps
- **WHEN** mutagen cannot determine the M4A duration (returns 0 or raises)
- **THEN** `chapters.py` SHALL fall back to scanning the VTT file for the last end timestamp

---

### Requirement: Atomic write via .partial pattern
Chapter embedding SHALL write FFmpeg's output to a hidden `.partial` temp file and atomically rename it to the final M4A path on success. On FFmpeg failure, the partial file SHALL be cleaned up and the original M4A SHALL be left untouched.

#### Scenario: Successful chapter embedding atomic write
- **WHEN** FFmpeg successfully embeds chapters into an M4A
- **THEN** the output SHALL first be written to `.<hash>.m4a.partial`
- **AND** atomically renamed to `<hash>.m4a` via `Path.replace()`

#### Scenario: FFmpeg failure cleanup
- **WHEN** FFmpeg fails during chapter embedding
- **THEN** the `.<hash>.m4a.partial` file SHALL be deleted
- **AND** the original `<hash>.m4a` SHALL remain unchanged

---

### Requirement: CLI and scan operate on M4A files
`chapters.py` SHALL scan for `*.m4a` files in OUTPUT_DIR (instead of `*.mp3`). The `--file` argument SHALL accept `.m4a` paths.

#### Scenario: Scan for M4A files missing chapters
- **WHEN** `chapters.py` scans OUTPUT_DIR
- **THEN** it SHALL find `*.m4a` files that lack a sibling `<stem>.chaptermarks.txt`
- **AND** process only those files that also have a sibling `<stem>.vtt`

#### Scenario: Single file processing
- **WHEN** `chapters.py --file episode.m4a` is invoked
- **THEN** it SHALL process that specific M4A file

#### Scenario: Reject MP3 file argument
- **WHEN** `chapters.py --file episode.mp3` is invoked
- **THEN** it SHALL log an error and exit with code 2

---

### Requirement: Force mode regenerates chapters for M4A
When invoked with `--force`, `chapters.py` SHALL delete existing `.chaptermarks.txt` files for M4A episodes and regenerate chapters from the VTT transcript.

#### Scenario: Force regeneration
- **WHEN** `chapters.py --force` is invoked
- **THEN** it SHALL delete `.chaptermarks.txt` for every `*.m4a` that has a `.vtt`
- **AND** regenerate chapters via the Ollama LLM call
- **AND** re-embed them into the M4A file

---

### Requirement: Idempotency via chaptermarks file existence
`chapters.py` SHALL skip M4A files that already have a sibling `.chaptermarks.txt` (unless `--force` is specified). This is the same idempotency mechanism as the current MP3 pipeline.

#### Scenario: Skip already-chaptered M4A
- **WHEN** `chapters.py` encounters `<hash>.m4a` with a sibling `<hash>.chaptermarks.txt`
- **THEN** it SHALL log that chapter marks already exist and skip the file
