## Why

MP3's chapter mark support (ID3 CHAP/CTOC frames) is ignored by Apple Podcasts, Castro, Pocket Casts, and most major podcast clients. Only Overcast reads them reliably. Switching the pipeline's primary audio format from MP3 to M4A (AAC in an MP4 container) gives us native MP4 chapter atoms that are supported by every major client except Spotify (which ignores all chapters regardless of format). As a bonus, the current fragile tag-wipe-and-restore dance between `chapters.py` and `id3tag.py` disappears — MP4 atom writes are additive, not destructive.

NotebookLM already sometimes delivers MP4/ISO containers that the scraper currently transcodes *away from* MP3. After this change we keep the native format instead of lossy-transcoding it.

## What Changes

- **BREAKING**: The canonical audio file extension changes from `.mp3` to `.m4a` across the entire pipeline. All eight scripts (`scraper.py`, `convert.py`, `transcribe.py`, `summarize.py`, `coverart.py`, `chapters.py`, `id3tag.py`, `cloud/app.py`) switch from scanning `*.mp3` to `*.m4a`.
- **BREAKING**: The RSS feed enclosure MIME type changes from `audio/mpeg` to `audio/x-m4a`. Existing podcast client subscriptions will see new episodes as M4A; old MP3 episodes stay in the feed if not migrated.
- **BREAKING**: The JSON sidecar `audio_file` field changes from `<hash>.mp3` to `<hash>.m4a`.
- `convert.py` reverses direction: it becomes an MP3-to-M4A converter for manually-dropped MP3 files (the current M4A-to-MP3 direction is no longer needed). Manually-dropped M4A files are already in the target format.
- `id3tag.py` switches from ID3v2.4 frames (`mutagen.mp3.MP3`) to MP4 atoms (`mutagen.mp4.MP4`). The tag set is equivalent but uses the MP4 atom namespace (`©nam`, `©ART`, `©alb`, `covr`, etc.).
- `chapters.py` switches from `-f mp3` to `-f ipod` in FFmpeg and drops the post-embed `id3tag.tag_one()` restore call — MP4 chapter embedding doesn't wipe other atoms.
- `scraper.py` stops transcoding MP4 downloads to MP3. When NotebookLM delivers an MP4 container, we keep it. When it delivers raw MPEG audio, we transcode to M4A (AAC).
- A one-time migration script re-encodes all existing `<hash>.mp3` files to `<hash>.m4a`, regenerates sidecar JSON `audio_file` fields, and cleans up.
- The cloud app's `/audio/{filename}` endpoint accepts `.m4a` suffix and serves `audio/x-m4a` MIME type.

## Capabilities

### New Capabilities
- `m4a-audio-format`: Primary audio container is M4A (AAC in MP4). Covers file naming, encoding parameters, format detection, and MIME types across the pipeline.
- `mp4-tagging`: Metadata tagging uses MP4 atoms instead of ID3v2 frames. Covers the tag set, cover art embedding, podcast flag, and diff-based idempotency.
- `mp4-chapters`: Chapter marks are embedded as MP4 chapter atoms via FFmpeg. No post-embed tag restoration needed.
- `migration`: One-time bulk migration of existing MP3 episodes to M4A with sidecar updates and cleanup.

### Modified Capabilities
_(none — no existing specs)_

## Impact

- **All 8 Python scripts** in `local/` and `cloud/` are modified (every file that references `.mp3`).
- **RSS feed**: Enclosure type changes. Podcast clients re-download or recognize the new format on next sync.
- **Dependencies**: `mutagen` already supports MP4 (`mutagen.mp4`); no new Python deps. FFmpeg already handles AAC encoding.
- **OUTPUT_DIR**: After migration, contains `.m4a` files instead of `.mp3`. Google Drive sync will upload the new files.
- **Existing subscribers**: Will see a one-time re-download of episodes as M4A. GUIDs stay the same (hash-based), so clients should match them as the same episodes.
- **AGENTS.md**: Needs updating to reflect the new format throughout.
