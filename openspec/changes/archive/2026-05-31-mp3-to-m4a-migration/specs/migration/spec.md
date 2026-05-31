## ADDED Requirements

### Requirement: Re-encode MP3 files to M4A
The migration script SHALL re-encode every `<hash>.mp3` file in OUTPUT_DIR to `<hash>.m4a` using ffmpeg with AAC codec. The output filename SHALL preserve the same `<hash>` stem — only the extension changes.

#### Scenario: Successful re-encode
- **WHEN** a `<hash>.mp3` exists in OUTPUT_DIR and no `<hash>.m4a` exists
- **THEN** the migration script SHALL invoke ffmpeg to encode `<hash>.mp3` to `<hash>.m4a`
- **AND** the audio SHALL be encoded with AAC codec (`-c:a aac`) into an M4A container

#### Scenario: Atomic write during re-encode
- **WHEN** ffmpeg is re-encoding an MP3 to M4A
- **THEN** the output SHALL be written to `.<hash>.m4a.partial` first
- **AND** atomically renamed to `<hash>.m4a` on success
- **AND** the partial file SHALL be cleaned up on failure

#### Scenario: Re-encode preserves hash-based naming
- **WHEN** `abc123def456.mp3` is migrated
- **THEN** the output SHALL be `abc123def456.m4a` (same stem, new extension)

---

### Requirement: Update JSON sidecar audio_file fields
After successfully re-encoding an MP3 to M4A, the migration script SHALL update the corresponding JSON sidecar's `audio_file` field from `<hash>.mp3` to `<hash>.m4a`.

#### Scenario: Sidecar updated after successful conversion
- **WHEN** `<hash>.mp3` has been successfully converted to `<hash>.m4a`
- **AND** a sibling `<hash>.json` exists
- **THEN** the `audio_file` field in the JSON SHALL be updated from `"<hash>.mp3"` to `"<hash>.m4a"`
- **AND** all other fields in the JSON SHALL remain unchanged

#### Scenario: No sidecar exists
- **WHEN** `<hash>.mp3` has been converted but no `<hash>.json` exists
- **THEN** the migration SHALL proceed without error (the sidecar is optional)
- **AND** a log message SHALL note the missing sidecar

---

### Requirement: Delete old MP3 files after successful conversion
After both the M4A file is confirmed to exist and the JSON sidecar has been updated (if present), the migration script SHALL delete the original `<hash>.mp3` file.

#### Scenario: MP3 deleted after successful migration
- **WHEN** `<hash>.m4a` exists and is valid (non-zero size)
- **AND** the JSON sidecar (if any) has been updated
- **THEN** the original `<hash>.mp3` SHALL be deleted

#### Scenario: MP3 NOT deleted if M4A creation failed
- **WHEN** ffmpeg fails to create `<hash>.m4a`
- **THEN** the original `<hash>.mp3` SHALL NOT be deleted
- **AND** an error SHALL be logged for that file
- **AND** migration SHALL continue with the remaining files

---

### Requirement: Delete stale chaptermarks.txt files
The migration script SHALL delete any `<hash>.chaptermarks.txt` file whose corresponding audio has been migrated from MP3 to M4A. This forces `chapters.py` to regenerate chapter marks with proper MP4 chapter embedding on its next run.

#### Scenario: Chaptermarks deleted for migrated file
- **WHEN** `<hash>.mp3` is successfully migrated to `<hash>.m4a`
- **AND** a sibling `<hash>.chaptermarks.txt` exists
- **THEN** the `<hash>.chaptermarks.txt` file SHALL be deleted

#### Scenario: No chaptermarks to delete
- **WHEN** `<hash>.mp3` is migrated and no `<hash>.chaptermarks.txt` exists
- **THEN** the migration SHALL proceed without error

---

### Requirement: Preserve format-independent sidecars
The migration script SHALL NOT delete or modify `.vtt` (transcript) or `.png` (cover art) sidecar files. These files are format-independent and remain valid after migration.

#### Scenario: VTT transcript preserved
- **WHEN** `<hash>.mp3` is migrated to `<hash>.m4a`
- **AND** a sibling `<hash>.vtt` exists
- **THEN** the `<hash>.vtt` file SHALL NOT be deleted or modified

#### Scenario: PNG cover art preserved
- **WHEN** `<hash>.mp3` is migrated to `<hash>.m4a`
- **AND** a sibling `<hash>.png` exists
- **THEN** the `<hash>.png` file SHALL NOT be deleted or modified

---

### Requirement: Migration is idempotent
The migration script SHALL be safe to run multiple times. If `<hash>.m4a` already exists for a given `<hash>.mp3`, the re-encode step SHALL be skipped. If the JSON sidecar already references `.m4a`, it SHALL not be rewritten. Re-running the migration on a fully-migrated OUTPUT_DIR SHALL be a no-op.

#### Scenario: M4A already exists, skip re-encode
- **WHEN** both `<hash>.mp3` and `<hash>.m4a` exist in OUTPUT_DIR
- **THEN** the re-encode step SHALL be skipped
- **AND** the MP3 file SHALL still be deleted (it's leftover from a partial prior run)
- **AND** the JSON sidecar SHALL be updated if it still references `.mp3`

#### Scenario: Fully migrated directory is no-op
- **WHEN** OUTPUT_DIR contains only `*.m4a` files (no `*.mp3`)
- **THEN** the migration script SHALL log that there is nothing to migrate and exit cleanly

#### Scenario: JSON already references M4A
- **WHEN** the JSON sidecar's `audio_file` already ends with `.m4a`
- **THEN** the migration script SHALL NOT rewrite the JSON file

---

### Requirement: Dry-run flag
The migration script SHALL support a `--dry-run` flag that reports what actions would be taken without modifying any files.

#### Scenario: Dry run lists planned actions
- **WHEN** `migrate.py --dry-run` is invoked
- **THEN** it SHALL log each MP3 that would be re-encoded to M4A
- **AND** each JSON sidecar that would be updated
- **AND** each MP3 that would be deleted
- **AND** each chaptermarks.txt that would be deleted
- **AND** it SHALL NOT create, modify, or delete any files

#### Scenario: Dry run reports totals
- **WHEN** `--dry-run` completes
- **THEN** it SHALL log a summary with counts: files to convert, sidecars to update, files to delete

---

### Requirement: Graceful handling of partial migration state
The migration script SHALL handle the state where some episodes have been migrated (M4A exists, MP3 deleted) and others have not. It SHALL process only the remaining un-migrated episodes.

#### Scenario: Mixed state in OUTPUT_DIR
- **WHEN** OUTPUT_DIR contains some episodes as `.m4a` only, some as `.mp3` only, and some with both
- **THEN** the migration SHALL skip fully-migrated episodes (M4A only)
- **AND** process MP3-only episodes (convert + update sidecar + delete MP3)
- **AND** clean up both-exist episodes (update sidecar if needed + delete MP3)

#### Scenario: Partial file from interrupted prior run
- **WHEN** a `.<hash>.m4a.partial` file exists from a prior interrupted migration
- **THEN** the migration SHALL delete the stale partial file before starting the conversion

---

### Requirement: ffmpeg must be available
The migration script SHALL verify that `ffmpeg` is on PATH before starting any work. If ffmpeg is not found, it SHALL exit immediately with a clear error message.

#### Scenario: ffmpeg not on PATH
- **WHEN** the migration script starts and `shutil.which("ffmpeg")` returns None
- **THEN** it SHALL log an error: "ffmpeg not found on PATH"
- **AND** exit with a non-zero exit code without processing any files

---

### Requirement: Error handling does not block remaining files
If ffmpeg fails to re-encode a specific MP3, the migration script SHALL log the error and continue processing the remaining files. The script SHALL report a summary of successes and failures at the end.

#### Scenario: Single file failure
- **WHEN** ffmpeg fails to convert `abc.mp3` but succeeds on `def.mp3`
- **THEN** `abc.mp3` SHALL remain untouched (not deleted)
- **AND** `def.mp3` SHALL be fully migrated (M4A created, JSON updated, MP3 deleted)
- **AND** the final log line SHALL report "1 succeeded, 1 failed"

#### Scenario: All files fail
- **WHEN** ffmpeg fails on every MP3 in OUTPUT_DIR
- **THEN** no MP3 files SHALL be deleted
- **AND** the script SHALL exit with a non-zero exit code

---

### Requirement: Migration operates on OUTPUT_DIR
The migration script SHALL read `OUTPUT_DIR` from the `.env` file (via `_clean_path_value()`) and process all `*.mp3` files found there. It SHALL NOT recurse into subdirectories.

#### Scenario: Configured via OUTPUT_DIR
- **WHEN** the migration script starts
- **THEN** it SHALL load `OUTPUT_DIR` from `local/.env`
- **AND** scan only the top-level directory for `*.mp3` files

#### Scenario: OUTPUT_DIR not set
- **WHEN** `OUTPUT_DIR` is not configured
- **THEN** the script SHALL exit with code 2 and log an error
