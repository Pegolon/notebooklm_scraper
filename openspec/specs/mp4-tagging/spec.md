# mp4-tagging Specification

## Purpose
TBD - created by archiving change mp3-to-m4a-migration. Update Purpose after archive.
## Requirements
### Requirement: Tagging uses MP4 atoms via mutagen.mp4
`id3tag.py` SHALL use `mutagen.mp4.MP4` to read and write metadata on M4A files instead of `mutagen.mp3.MP3` and `mutagen.id3.ID3`. The script SHALL open files with `mutagen.mp4.MP4(path)` and access tags via the `.tags` dict (a `mutagen.mp4.MP4Tags` instance).

#### Scenario: Open M4A file for tagging
- **WHEN** `id3tag.py` opens an M4A file for tag verification or writing
- **THEN** it SHALL use `mutagen.mp4.MP4(str(path))` to load the file
- **AND** it SHALL NOT import or use `mutagen.id3` or `mutagen.mp3` classes

#### Scenario: Invalid MP4 container detection
- **WHEN** `id3tag.py` attempts to open a file that is not a valid MP4 container
- **THEN** it SHALL raise a clear error indicating the file is not a valid M4A/MP4 container

---

### Requirement: MP4 atom tag set mapping
The standard tag set SHALL map from ID3v2.4 frames to MP4 atom keys as follows:

| ID3 Frame | MP4 Atom Key | Content |
|-----------|-------------|---------|
| TIT2 | `©nam` | Episode title (from JSON `title`) |
| TPE1 | `©ART` | Artist/host (`PODCAST_AUTHOR`) |
| TALB | `©alb` | Album/show name (`PODCAST_ALBUM`) |
| TCON | `©gen` | Genre (`PODCAST_GENRE`) |
| TDRC | `©day` | Recording date (from JSON `pub_date`) |
| COMM | `©cmt` | Comment (from JSON `description`) |
| TDES | `desc` | iTunes long description (freeform atom) |
| TGID | Episode GUID | iTunes freeform atom (`com.apple.iTunes`, `EPISODE ID`) |
| PCST | `pcst` | Podcast flag (boolean) |
| WFED | `purl` | Feed URL (freeform atom) |
| APIC | `covr` | Cover art (PNG bytes) |

All text atoms (`©nam`, `©ART`, `©alb`, `©gen`, `©day`, `©cmt`) SHALL store values as a list containing a single string, per mutagen's MP4Tags convention.

#### Scenario: All standard text atoms written
- **WHEN** `id3tag.py` writes tags to an M4A file that has a JSON sidecar
- **THEN** the file SHALL contain `©nam`, `©ART`, `©alb`, `©gen`, `©day`, and `©cmt` atoms
- **AND** each atom's value SHALL be a list with a single string element

#### Scenario: Date atom from sidecar
- **WHEN** the JSON sidecar has a `pub_date` field
- **THEN** the `©day` atom SHALL contain that value as-is (ISO-8601 string)

#### Scenario: Date atom fallback to mtime
- **WHEN** the JSON sidecar has no `pub_date` or it is empty
- **THEN** the `©day` atom SHALL contain the M4A file's mtime formatted as ISO-8601

---

### Requirement: Cover art embedded as PNG in covr atom
Cover art SHALL be embedded using the `covr` atom key with `mutagen.mp4.MP4Cover` wrapping the PNG bytes. The image format indicator SHALL be `mutagen.mp4.MP4Cover.FORMAT_PNG`.

#### Scenario: PNG cover art embedded
- **WHEN** a sibling `<hash>.png` file exists next to the M4A
- **THEN** the `covr` atom SHALL contain a single `MP4Cover` entry with the PNG bytes
- **AND** the format SHALL be `MP4Cover.FORMAT_PNG`

#### Scenario: No cover art available
- **WHEN** no sibling `<hash>.png` exists
- **THEN** the `covr` atom SHALL NOT be written
- **AND** a log message SHALL indicate that cover art will not be embedded

---

### Requirement: Podcast flag as pcst atom
The podcast flag SHALL be written as the `pcst` atom with a boolean true value. This is the MP4 equivalent of the ID3 `PCST` frame.

#### Scenario: Podcast flag set
- **WHEN** `id3tag.py` writes tags to an M4A file
- **THEN** the `pcst` atom SHALL be present and set to true

---

### Requirement: iTunes freeform atoms for TDES, TGID, and WFED
The iTunes-specific tags (long description, episode GUID, and feed URL) SHALL be stored as freeform (`----`) atoms using the `com.apple.iTunes` namespace, accessed via mutagen's freeform atom API.

#### Scenario: Episode GUID as freeform atom
- **WHEN** the JSON sidecar has an `id` field
- **THEN** a freeform atom with mean `com.apple.iTunes` and name `EPISODE ID` SHALL be written with the GUID string

#### Scenario: Feed URL as purl atom
- **WHEN** `PODCAST_FEED_URL` is configured in `.env`
- **THEN** the `purl` atom SHALL contain the feed URL
- **AND** if `PODCAST_FEED_URL` is empty, the `purl` atom SHALL NOT be written

#### Scenario: Long description as freeform atom
- **WHEN** the JSON sidecar has a `description` field
- **THEN** a freeform atom for the iTunes long description SHALL be written with the description text

---

### Requirement: Diff-based idempotency
`id3tag.py` SHALL compare the expected atom values against the file's current atoms before writing. Only atoms that are missing or have stale values SHALL be rewritten. Files that already carry the full expected tag set SHALL be skipped with no file modification.

#### Scenario: File already fully tagged
- **WHEN** `id3tag.py` processes an M4A whose atoms all match the expected values
- **THEN** it SHALL NOT modify the file
- **AND** it SHALL log that the file already has the full standard tag set

#### Scenario: Stale title atom
- **WHEN** an M4A's `©nam` atom contains "Old Title" but the JSON sidecar says "New Title"
- **THEN** `id3tag.py` SHALL update `©nam` to "New Title"
- **AND** it SHALL log which atoms were updated

#### Scenario: Missing cover art atom added
- **WHEN** an M4A has no `covr` atom but a sibling PNG exists
- **THEN** `id3tag.py` SHALL add the `covr` atom with the PNG bytes

#### Scenario: Force flag rewrites all atoms
- **WHEN** `id3tag.py` is invoked with `--force`
- **THEN** all standard atoms SHALL be rewritten regardless of whether they match

---

### Requirement: Check-only mode reports mismatches
The `--check` flag SHALL cause `id3tag.py` to report atom mismatches without modifying any files. The exit code SHALL be 1 if any mismatches are found, 0 otherwise.

#### Scenario: Check mode with mismatches
- **WHEN** `id3tag.py --check` finds M4A files with missing or stale atoms
- **THEN** it SHALL log the mismatched atom keys for each file
- **AND** it SHALL NOT modify any files
- **AND** it SHALL exit with code 1

#### Scenario: Check mode all clean
- **WHEN** `id3tag.py --check` finds all M4A files fully tagged
- **THEN** it SHALL exit with code 0

---

### Requirement: Atom comparison handles type differences
When comparing existing atoms against expected values, the comparison SHALL handle mutagen's MP4 value types correctly: text atoms as string lists, `covr` atoms by comparing PNG bytes, `pcst` as a boolean, and freeform atoms as byte strings.

#### Scenario: Text atom comparison
- **WHEN** comparing a `©nam` atom
- **THEN** the comparison SHALL extract the string value from the list and compare it to the expected string

#### Scenario: Cover art comparison
- **WHEN** comparing a `covr` atom
- **THEN** the comparison SHALL compare the raw PNG bytes and the format indicator

---

### Requirement: CLI accepts M4A files
The `--file` argument SHALL accept paths to `.m4a` files. Passing a `.mp3` file SHALL result in an error.

#### Scenario: Tag single M4A file
- **WHEN** `id3tag.py --file episode.m4a` is invoked
- **THEN** it SHALL process that specific M4A file

#### Scenario: Reject MP3 file
- **WHEN** `id3tag.py --file episode.mp3` is invoked
- **THEN** it SHALL log an error and exit with code 2

---

### Requirement: Batch mode scans for M4A
When invoked without `--file`, `id3tag.py` SHALL scan OUTPUT_DIR for `*.m4a` files and verify/repair tags on each.

#### Scenario: Batch tagging of all M4A files
- **WHEN** `id3tag.py` is invoked without `--file`
- **THEN** it SHALL process every `*.m4a` file in OUTPUT_DIR
- **AND** report counts of already-complete, updated, and failed files

