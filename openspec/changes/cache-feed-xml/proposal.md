## Why

Currently, the FastAPI application (`cloud/app.py`) parses all metadata sidecar `.json` files and computes stats on all `.m4a` files in `OUTPUT_DIR` on every single request to `/feed.xml`. Since the output directory is located on a Google-Drive-synced path, reading all sidecars synchronously on every request incurs high disk I/O latency. This severely degrades response times and can lead to timeouts or slow responsiveness in podcast clients as the library grows.

Caching the compiled feed XML in memory and only regenerating it when the directory's modified time (`st_mtime`) or the file count changes will eliminate redundant disk reads and deliver near-instantaneous responses.

## What Changes

- Add in-memory caching for the generated feed XML in `cloud/app.py`.
- Introduce a directory state tracker that checks the filesystem modified time (`st_mtime` of the `OUTPUT_DIR`) and the total number of files.
- Bypass file scanning, sidecar parsing, and XML building if the directory state has not changed since the last cached feed build.
- Force feed regeneration if the directory state changes.

## Capabilities

### New Capabilities
- `feed-caching`: In-memory caching and invalidation of the feed XML based on directory state modifications (mtime and file count).

### Modified Capabilities
<!-- None -->

## Impact

- **Affected Code**: `cloud/app.py` (specifically `serve_feed` and helper methods).
- **APIs**: `/feed.xml` will be served from cache when valid.
- **Dependencies**: Standard library only (no external caching library needed).
