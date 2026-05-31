## Why

Currently, the FastAPI application (`cloud/app.py`) serves M4A audio overview files via the synchronous/blocking helper function `_iter_file`, which yields bytes from the file in large 1 MiB chunks. Because FastAPI runs synchronous generators inside a thread pool, this consumes worker threads and can limit request throughput under concurrent streaming requests. Furthermore, a large 1 MiB chunk size leads to high initial buffering latency for client media players (slower startup time) and increased RAM consumption on the server per active stream.

Converting `_iter_file` to an asynchronous generator and reducing the chunk size to a smaller, more optimal range (e.g. 64 KB or 128 KB) will eliminate blocking filesystem I/O in worker threads, reduce memory overhead, and speed up stream startup times.

## What Changes

- Convert `_iter_file` to an asynchronous generator (`async def`) that performs non-blocking async file reads using `anyio.open_file`.
- Shrink the chunk size constant `_AUDIO_CHUNK_SIZE` from 1 MiB to 64 KB (or 128 KB) to reduce RAM usage and improve player startup times.
- Update `/audio/{filename}` endpoint to correctly handle the asynchronous generator with `StreamingResponse`.

## Capabilities

### New Capabilities
<!-- None -->

### Modified Capabilities
- `m4a-audio-format`: Specify that M4A streaming is performed asynchronously and in smaller chunks to reduce memory footprint and prevent thread blocking.

## Impact

- **Affected Code**: `cloud/app.py` (specifically `_iter_file`, `_AUDIO_CHUNK_SIZE`, and the `/audio/{filename}` route).
- **APIs**: `/audio/{filename}` will serve files asynchronously. No functional API signature changes are introduced.
- **Dependencies**: Uses `anyio` (already a dependency of FastAPI/Starlette) for async file I/O, so no new packages are required.
