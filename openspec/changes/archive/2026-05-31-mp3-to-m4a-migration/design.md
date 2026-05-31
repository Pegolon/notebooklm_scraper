# MP3 → M4A Migration — Design Document

## Context

The pipeline currently produces `.mp3` files as its canonical audio format.
Chapter marks are embedded as ID3 CHAP/CTOC frames, which only Overcast
reads reliably — Apple Podcasts, Castro, Pocket Casts, and most other
major clients ignore them entirely.  Meanwhile, `chapters.py` has to run a
fragile two-step dance: FFmpeg's `-map_metadata 1` wipes all existing ID3
frames, so `id3tag.tag_one()` must be called immediately after to restore
them.

NotebookLM already sometimes delivers audio in MP4/ISO containers (`ftyp`
magic bytes).  The scraper currently *transcodes these away* to MP3
(`scraper.py` → `convert.transcode_to_mp3()`), losing quality in a
lossy-to-lossy re-encode that exists only because MP3 was the pipeline's
assumed format.

Switching to M4A (AAC audio in an MP4 container) solves both problems:
MP4 chapter atoms are natively supported by every major podcast client
except Spotify (which ignores all chapters regardless of format), and MP4
atom writes are additive — no wipe-and-restore needed.

### Files affected

Every script that references `.mp3` must change.  The full list (with the
nature of each change):

| Script | What changes |
|--------|-------------|
| `local/scraper.py` | Format detection inverted: MP4 → keep as `.m4a`; MPEG → transcode to `.m4a`. `audio_file` in JSON sidecar becomes `<hash>.m4a`. |
| `local/convert.py` | Direction reversed: MP3→M4A converter for manual drops. M4A drops are already in target format (no-op). |
| `local/transcribe.py` | `*.mp3` glob → `*.m4a`; CLI `--file` suffix check `.mp3` → `.m4a`. MLX Whisper handles M4A natively. |
| `local/summarize.py` | `*.mp3` glob → `*.m4a`; `audio_file` in written sidecar → `<name>.m4a`; CLI suffix check. |
| `local/coverart.py` | `*.mp3` glob → `*.m4a`; CLI suffix check. |
| `local/chapters.py` | `*.mp3` glob → `*.m4a`; FFmpeg `-f mp3` → `-f ipod`; drop `id3tag.tag_one()` restore call; duration via `mutagen.mp4.MP4` instead of `mutagen.mp3.MP3`. |
| `local/id3tag.py` | Full rewrite of frame layer: `mutagen.mp3.MP3` / `mutagen.id3.*` → `mutagen.mp4.MP4` / `mutagen.mp4.MP4Tags`; atom namespace mapping; diff logic adapted for MP4 atoms. |
| `cloud/app.py` | `*.mp3` glob → `*.m4a`; `_safe_resolve(…, ".m4a")`; MIME type `audio/x-m4a`; enclosure type in RSS. |
| `local/migrate.py` | **New.** One-time bulk migration script. |
| `AGENTS.md` | Documentation update throughout. |


## Goals / Non-Goals

### Goals

- **G1**: All new audio files produced by the pipeline use the `.m4a`
  extension (AAC in an MP4 container).
- **G2**: Chapter marks embedded via FFmpeg use MP4 chapter atoms (`chpl` /
  QuickTime chapter track) that Apple Podcasts, Overcast, Pocket Casts, and
  Castro all read.
- **G3**: The fragile "wipe-and-restore ID3 tags after chapter embedding"
  dance in `chapters.py` is eliminated.
- **G4**: When NotebookLM delivers an MP4/ISO container, the scraper keeps
  it as-is instead of lossy-transcoding to MP3.
- **G5**: Existing MP3 episodes can be migrated to M4A via a one-time
  script with `--dry-run` support.
- **G6**: RSS feed GUIDs remain stable across the migration (no duplicate
  episodes in podcast clients).

### Non-Goals

- **NG1**: Supporting both MP3 and M4A simultaneously in the pipeline long-term.
  After migration, MP3 is a legacy format. The only MP3 handling that
  remains is `convert.py` accepting manually-dropped `.mp3` files and
  transcoding them to M4A.
- **NG2**: Opus or other codec support. AAC is the universally-supported
  podcast codec; Opus adoption is still patchy in Apple Podcasts.
- **NG3**: Lossless formats (ALAC, FLAC). Speech content doesn't benefit
  and file sizes would balloon.
- **NG4**: Backward-compatible feed serving (serving both `.mp3` and `.m4a`
  URLs for the same episode). Clients match by GUID, not by enclosure URL.


## Decisions

### D1 — AAC Encoder: built-in `aac` (not `libfdk_aac`)

**Decision**: Use FFmpeg's built-in `aac` encoder with `-b:a 128k` (CBR).

**Rationale**: `libfdk_aac` produces marginally better quality at low
bitrates, but the difference is inaudible for speech content.  More
importantly, `libfdk_aac` is a non-default, patent-encumbered library that
many FFmpeg builds (including Homebrew's default `brew install ffmpeg`) do
not include — the user would need `brew install ffmpeg --with-fdk-aac` or
a custom build.  The built-in `aac` encoder has been "good enough" since
FFmpeg 3.0 and is guaranteed present in every FFmpeg binary.

128 kbps CBR for mono/stereo speech is transparent and keeps file sizes
comparable to the current MP3 VBR q=2 (~190 kbps).  CBR is preferred over
VBR for podcast distribution because some older clients handle VBR AAC
poorly (seeking artifacts, incorrect duration display).

**FFmpeg invocation**:
```
ffmpeg -y -loglevel error -i <src> \
  -vn -c:a aac -b:a 128k \
  -f ipod \
  <dst>
```

### D2 — MP4 Muxer: `-f ipod` (not `-f mp4`)

**Decision**: Use `-f ipod` for all M4A output.

**Rationale**: The `ipod` muxer is FFmpeg's name for the Apple-compatible
M4A variant.  It produces a file with proper `ftyp M4A ` brand and — critically
— uses the `chpl` chapter atom format that Apple Podcasts reads.  The
generic `-f mp4` muxer uses Nero-style chapters (`chap` track reference +
`tref`), which some Apple clients ignore.

The `.partial` → atomic-rename pattern continues as before; `-f ipod` is
needed because the temp filename ends in `.partial`, which FFmpeg can't
auto-detect.

### D3 — MP4 Tag Library: `mutagen.mp4.MP4` + `mutagen.mp4.MP4Tags`

**Decision**: Use mutagen's MP4 support for all metadata tagging.  No new
dependency needed — mutagen already ships `mutagen.mp4`.

**Atom mapping** (ID3v2.4 → MP4):

| Current ID3 Frame | MP4 Atom Key | Notes |
|---|---|---|
| `TIT2` (title) | `©nam` | Free-text, list of strings |
| `TPE1` (artist) | `©ART` | Free-text |
| `TALB` (album) | `©alb` | Free-text |
| `TCON` (genre) | `©gen` | Free-text |
| `TDRC` (date) | `©day` | Free-text (ISO-8601 string) |
| `COMM` (comment) | `©cmt` | Free-text; MP4 has no lang/desc sub-fields |
| `APIC` (cover art) | `covr` | `MP4Cover(png_bytes, imageformat=MP4Cover.FORMAT_PNG)` |
| `TDES` (iTunes desc) | `desc` | Free-text |
| `PCST` (podcast flag) | `pcst` | Boolean atom: `[True]` |
| `TGID` (episode GUID) | `----:com.apple.iTunes:PODCAST-GUID` | Freeform atom via `MP4FreeForm` |
| `WFED` (feed URL) | `purl` | Free-text |

**Key differences from ID3 tagging**:

1. MP4 atoms are stored as lists of values (e.g. `tags["©nam"] = ["Episode Title"]`).
2. Cover art uses `MP4Cover` objects, not raw `APIC` frames.  Format must
   be declared explicitly (`FORMAT_PNG`).
3. The podcast flag (`pcst`) is a simple boolean atom, not a 4-byte integer.
4. Freeform atoms (like the podcast GUID) use the `----:com.apple.iTunes:<name>`
   key convention with `MP4FreeForm` byte values.
5. **No wipe-on-save**: calling `tags.save()` on an MP4 file updates atoms
   in-place without destroying unrelated atoms (including chapter atoms).
   This is the key property that eliminates the chapters.py / id3tag.py
   coupling.

**Diff-based idempotency** works the same way: build the expected atom dict,
compare against what's on disk, and only write if something differs.

### D4 — `convert.py` Role Reversal

**Decision**: `convert.py` becomes an MP3→M4A converter.

**Current behavior**: Scans `OUTPUT_DIR` for `*.m4a`, hashes each, transcodes
to `<hash>.mp3` via `libmp3lame`.

**New behavior**:
- Scans `OUTPUT_DIR` for `*.mp3` (manually-dropped files).
- Hashes each MP3, transcodes to `<hash>.m4a` via `aac` / `-f ipod`.
- Manually-dropped `.m4a` files are already in the target format — they
  become no-ops handled by the rest of the pipeline directly (same as
  manually-dropped `.mp3` files are today).
- The exported function renames from `transcode_to_mp3()` to
  `transcode_to_m4a()`.  The scraper imports this for in-place transcoding
  of MPEG downloads.
- `convert_one()` returns the M4A path.  `convert_missing()` scans for `*.mp3`.
- `_hash_file()` and the atomic-write pattern are unchanged.
- The `MP3_QUALITY` env var is replaced by `AAC_BITRATE` (default `128k`).

### D5 — `scraper.py` Format Detection (Inverted Logic)

**Decision**: Invert the current format detection in `save_episode()`.

**Current logic** (`scraper.py:853-876`):
```python
is_mp4 = head[4:8] == b"ftyp"
if is_mp4:
    transcode_to_mp3(tmp_download, mp3_path)  # MP4 → MP3
else:
    tmp_download.replace(mp3_path)             # keep as MP3
```

**New logic**:
```python
is_mp4 = head[4:8] == b"ftyp"
if is_mp4:
    tmp_download.replace(m4a_path)             # keep as M4A (native)
else:
    transcode_to_m4a(tmp_download, m4a_path)   # MPEG → M4A
```

This means:
- When NotebookLM delivers an MP4/ISO container (which it increasingly
  does), we keep it as-is — **no lossy re-encode**.  Just rename the temp
  file to `<hash>.m4a`.
- When it delivers raw MPEG audio (ID3-tagged MP3 or raw sync bytes), we
  transcode to M4A via `convert.transcode_to_m4a()`.
- The sidecar JSON writes `"audio_file": "<hash>.m4a"` in both cases.

### D6 — `chapters.py` Simplification

**Decision**: Replace MP3-specific chapter embedding with MP4-native approach.

**Changes**:
1. `-f mp3` → `-f ipod` in the FFmpeg command.
2. Drop the post-embed `from id3tag import tag_one; tag_one(mp3, force=True)`
   call entirely.  MP4 chapter embedding via `-map_chapters 1` is additive —
   it writes a `chpl` atom without touching `©nam`, `©ART`, `covr`, etc.
3. Duration detection: `mutagen.mp3.MP3` → `mutagen.mp4.MP4`.  The
   `MP4.info.length` property works identically.
4. The chaptermarks.txt sidecar format (FFMETADATA1) is unchanged — FFmpeg
   reads it the same way for both MP3 and MP4 output.
5. Variable names: `mp3` → `m4a` throughout for clarity (or keep as `audio`
   for format-neutrality).

### D7 — Cloud App Changes

**Decision**: Update `cloud/app.py` for M4A serving.

**Changes**:
1. **Episode discovery**: `output_dir.glob("*.mp3")` → `output_dir.glob("*.m4a")`
   in `load_episodes()` and `_synthesize_metadata()`.
2. **Audio endpoint**: `_safe_resolve(filename, ".mp3")` →
   `_safe_resolve(filename, ".m4a")`.
3. **MIME type**: All `media_type="audio/mpeg"` → `media_type="audio/x-m4a"`
   in the `/audio/{filename}` endpoint (both full and Range responses).
4. **RSS enclosure**: `enclosure.set("type", "audio/mpeg")` →
   `enclosure.set("type", "audio/x-m4a")` in `build_feed()`.
5. **Range support**: Unchanged.  HTTP Range works identically for any
   binary file; the only difference is the Content-Type header.

### D8 — RSS Enclosure MIME Type: `audio/x-m4a`

**Decision**: Use `audio/x-m4a` as the RSS enclosure type.

**Rationale**: This is the MIME type Apple specifies in its [Podcasting
specification](https://podcasters.apple.com/support/823-podcast-requirements)
for M4A enclosures.  All major podcast clients (Apple Podcasts, Overcast,
Pocket Casts, Castro, Spotify, Google Podcasts) recognize it.  The
alternative `audio/mp4` is technically more correct per IANA but is not
what Apple's validator expects and some older clients may not recognize it
for audio-only content.

### D9 — Migration Strategy: One-Time `migrate.py` Script

**Decision**: Provide a standalone `local/migrate.py` script for bulk migration.

**Process per episode**:
1. For each `<hash>.mp3` in `OUTPUT_DIR`:
   a. Skip if `<hash>.m4a` already exists (idempotent).
   b. Transcode `<hash>.mp3` → `<hash>.m4a` via `transcode_to_m4a()`.
   c. Read `<hash>.json`, update `"audio_file"` from `<hash>.mp3` to
      `<hash>.m4a`, write back.
   d. If `<hash>.chaptermarks.txt` exists: re-embed chapters into the new
      `.m4a` via FFmpeg, then delete the `.chaptermarks.txt` (chapters now
      live as MP4 atoms — the sidecar is no longer needed for re-embedding).
   e. Re-tag the new `.m4a` via the updated `id3tag.tag_one()` (now `m4atag`).
   f. Delete the original `<hash>.mp3`.

**CLI flags**:
- `--dry-run`: Print what would happen without modifying any files.
- `--keep-mp3`: Don't delete the original `.mp3` after successful conversion
  (useful for cautious rollback).
- `--file <path>`: Migrate a single episode instead of the whole directory.

**Idempotency**: The script is safe to run multiple times.  If `<hash>.m4a`
already exists and `<hash>.mp3` is gone, the episode is skipped entirely.
If `<hash>.m4a` exists but `<hash>.mp3` is still present (interrupted
previous run or `--keep-mp3`), it skips the transcode but still updates
the JSON sidecar and cleans up the MP3.

**Ordering**: No ordering dependency — episodes are independent.  Processed
alphabetically for deterministic logs.


## Risks / Trade-offs

### R1 — FFmpeg `aac` Encoder Availability

**Risk**: The built-in `aac` encoder might not be present in some FFmpeg build.

**Likelihood**: Very low.  The `aac` encoder has been a default built-in since
FFmpeg 3.0 (2016).  Homebrew's `ffmpeg` formula always includes it.

**Mitigation**: The existing `shutil.which("ffmpeg")` check catches missing
ffmpeg entirely.  Add a pre-flight `ffmpeg -encoders | grep aac` check in
migrate.py to fail fast before processing any files.

### R2 — RSS GUID Stability

**Risk**: Podcast clients might create duplicate episodes if GUIDs change.

**Likelihood**: None.  GUIDs are `md5(normalized_description)` or
`md5(filename)` — they're based on content hashes, not filenames.  The
`<guid>` element in the RSS feed is populated from the JSON sidecar's `id`
field, which doesn't change.  The enclosure URL changes from
`/audio/<hash>.mp3` to `/audio/<hash>.m4a`, but RSS clients use `<guid>`
for deduplication, not the enclosure URL.

**Mitigation**: GUIDs remain `isPermaLink="false"` and are never derived
from the audio filename or URL.

### R3 — Google Drive Sync Churn During Migration

**Risk**: Migrating many episodes at once creates a burst of file creations
and deletions that Google Drive must sync, potentially causing temporary
bandwidth spikes or sync conflicts.

**Likelihood**: Medium (depends on episode count and Drive sync speed).

**Mitigation**: Run migration during off-hours.  The `--keep-mp3` flag lets
the user stagger: first create all `.m4a` files, verify the feed works,
then delete `.mp3` files in a second pass.  The cloud app (post-code-update)
only scans `*.m4a`, so leftover `.mp3` files are simply invisible to the feed.

### R4 — Podcast Client Re-Download

**Risk**: Clients that match episodes by enclosure URL (instead of GUID)
may re-download all episodes as M4A even though they already have the MP3
version.

**Likelihood**: Low-to-medium.  Most major clients (Apple Podcasts, Overcast,
Pocket Casts) use `<guid>` for episode identity.  Some smaller or older
clients may use the enclosure URL.

**Mitigation**: This is a one-time event.  Users with bandwidth concerns
can migrate in batches.  The audio content is identical (same source
material), so the "re-download" is functionally harmless — it just updates
the local file format.

### R5 — MLX Whisper M4A Compatibility

**Risk**: `mlx_whisper.transcribe()` might not handle M4A input.

**Likelihood**: Very low.  MLX Whisper uses FFmpeg internally for audio
decoding and handles any container FFmpeg can read — M4A/MP4 is one of its
best-supported formats.

**Mitigation**: Verify during implementation with a test M4A file.  No code
change needed in `transcribe.py` beyond the glob/suffix changes.

### R6 — Mutagen MP4 Tag Compatibility

**Risk**: Some MP4 atom keys (especially freeform `----:com.apple.iTunes:*`)
might not be read correctly by all podcast clients.

**Likelihood**: Low.  `©nam`, `©ART`, `©alb`, `covr` are universally
supported.  The podcast-specific atoms (`pcst`, `purl`, freeform GUID) are
Apple-defined and work in Apple Podcasts + Overcast.  Clients that don't
read them simply ignore them (same behavior as today with ID3 `PCST`/`WFED`).

**Mitigation**: The tag set is functionally identical to what we write
today in ID3 — just mapped to MP4 atoms.  No information is lost.


## Migration Plan

### Phase 1: Code Changes (all scripts)

Update all eight scripts to work with `.m4a` as the canonical format.
This is the main implementation work.  The cloud app must be updated
simultaneously — there's no graceful way to serve both formats from the
same glob.

**Order of implementation** (respects import dependencies):

1. `convert.py` — reverse direction, export `transcode_to_m4a()`
2. `id3tag.py` — rewrite for MP4 atoms
3. `chapters.py` — `-f ipod`, drop id3tag restore call
4. `scraper.py` — invert format detection, update sidecar
5. `transcribe.py` — glob + suffix
6. `summarize.py` — glob + suffix + sidecar `audio_file`
7. `coverart.py` — glob + suffix
8. `cloud/app.py` — glob, MIME, enclosure type

### Phase 2: Migration Script

Implement `local/migrate.py` with `--dry-run` and `--keep-mp3` support.
Test on a small subset of episodes before running on the full library.

### Phase 3: Run Migration

```bash
cd local
uv run migrate.py --dry-run          # verify plan
uv run migrate.py --keep-mp3         # convert without deleting originals
# verify feed in podcast client
uv run migrate.py                    # delete originals (or manually rm *.mp3)
```

### Phase 4: Documentation

Update `AGENTS.md` to reflect M4A as the canonical format throughout.
Update `.env.example` files (`MP3_QUALITY` → `AAC_BITRATE`).


## Open Questions

1. **Should `migrate.py` re-embed chapters?**  The current plan re-embeds
   chapters from the existing `.chaptermarks.txt` sidecar.  An alternative
   is to skip chapter re-embedding during migration and let the user run
   `uv run chapters.py --force` afterward — simpler migration script, but
   requires a manual follow-up step.
   **Recommendation**: Re-embed during migration.  The `.chaptermarks.txt`
   is already there; skipping it just creates more manual work.

2. **Should we keep `.chaptermarks.txt` sidecars after migration?**  MP4
   chapters live inside the container — the sidecar is only needed as a
   human-readable record or for re-embedding.  Deleting it is cleaner;
   keeping it provides auditability.
   **Recommendation**: Keep them.  `chapters.py` still uses them as its
   idempotency marker (skip if `.chaptermarks.txt` exists).  Deleting them
   would cause `chapters.py --force` to redo the LLM call.

3. **CBR vs VBR for AAC?**  We chose CBR 128k above for maximum client
   compatibility.  AAC VBR can be more efficient, but some podcast clients
   display incorrect durations or have seeking issues.  Is there a preference?
   **Recommendation**: Stay with CBR 128k.  Predictable behavior is more
   valuable than marginal size savings for speech content.
