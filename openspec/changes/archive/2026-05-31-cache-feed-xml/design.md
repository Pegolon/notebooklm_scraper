## Context

The current `cloud/app.py` serves `/feed.xml` by running `load_episodes(OUTPUT_DIR)` and `build_feed()` on every request. This scans `OUTPUT_DIR` (using `glob("*.m4a")`), reads/parses every JSON sidecar, and stats all M4A/VTT/PNG files. When the directory is large or mounted over a slower virtual filesystem (e.g. Google Drive), this blocks thread execution and degrades API performance.

## Goals / Non-Goals

**Goals:**
- Eliminate redundant disk I/O on `/feed.xml` when no new episodes have been added or removed.
- Maintain a thread-safe, memory-based cache in `cloud/app.py` that invalidates automatically.
- Keep the implementation simple and standard-library-only.

**Non-Goals:**
- Caching feed XML on disk (in-memory is sufficient for the service life-cycle and simple restart behavior).
- Caching individual audio or image responses (which already have correct HTTP Cache-Control headers).

## Decisions

### Decision 1: Directory State Checking (st_mtime + File Count)
We will determine folder freshness by checking two lightweight metrics:
1. `OUTPUT_DIR.stat().st_mtime`: The directory's modified time, which changes on macOS/Linux when files are added, deleted, or renamed.
2. The number of `.m4a` files in the directory.
- **Why**: Doing a single directory `stat` and listing filenames is extremely fast (microsecond range) compared to scanning, reading, and parsing dozens of JSON sidecar files.
- **Alternatives Considered**: 
  - *Time-to-Live (TTL) Caching (e.g. 60s)*: While simple, this would either serve stale feeds for up to 60 seconds after a scraper run or still poll unnecessarily. Using `st_mtime` provides instant freshness.
  - *Inotify / File Watchers*: Introduces OS-dependent code or heavy external dependencies like `watchdog`.

### Decision 2: Thread-Safe Cache Operations
Since `serve_feed` is defined as a synchronous `def` function, FastAPI executes it in an external thread pool. To prevent race conditions or cache stampedes (where multiple threads rebuild the cache simultaneously on invalidation):
- We will protect the cache check-and-rebuild logic using a `threading.Lock`.
- **Why**: Threading locks prevent concurrent disk read execution when the cache is invalidated, serializing the generation path while allowing subsequent requests to read the new cache immediately.

---

## Risks / Trade-offs

- **Risk**: Google Drive desktop sync updates might delay directory modified-time changes.
  - **Mitigation**: Google Drive for macOS propagates file metadata changes locally in near real-time. Since we also track `.m4a` file count, adding/removing files is guaranteed to trigger invalidation.
- **Risk**: Lock contention during heavy traffic when cache is invalid.
  - **Mitigation**: Since rebuilding the feed takes only a fraction of a second, the locked section executes quickly, and subsequent threads will read the updated cache without hitting the disk.
