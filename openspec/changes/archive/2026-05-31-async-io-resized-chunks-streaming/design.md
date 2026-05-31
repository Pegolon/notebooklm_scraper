## Context

The cloud FastAPI server (`cloud/app.py`) serves large M4A files to clients via the `/audio/{filename}` endpoint using FastAPI's `StreamingResponse`. Currently, the file read helper `_iter_file` is synchronous and reads chunks of 1 MiB. FastAPI processes sync generators by running them in a background thread pool (via Starlette's `iterate_in_threadpool`), which consumes thread resources and limits performance under concurrency. Additionally, 1 MiB chunk sizes increase server memory usage per client stream and increase initial player buffering/startup times.

## Goals / Non-Goals

**Goals:**
- Convert the `_iter_file` generator to be asynchronous (`async def`) returning an `AsyncIterable[bytes]`.
- Perform file reads asynchronously and non-blockingly using `anyio.open_file`.
- Shrink the chunk size constant `_AUDIO_CHUNK_SIZE` to 64 KiB or 128 KiB to reduce memory usage and startup latency.
- Ensure full compatibility with the existing HTTP Range support (seeking, partial responses).

**Non-Goals:**
- Adding third-party asynchronous file-system libraries (e.g. `aiofiles`). We will use `anyio` which is already a transitive dependency of FastAPI.
- Changing metadata scanning or cover art serving to be asynchronous, as they are metadata-based and lightweight.

## Decisions

### 1. Choice of Async I/O library: `anyio`
- **Option A (Chosen)**: Use `anyio.open_file`. Since FastAPI depends on Starlette, which depends on `anyio`, it is guaranteed to be available in the environment without adding new dependencies. It supports async context manager, seek, and read.
- **Option B**: Use `aiofiles`. Requires adding `aiofiles` to `pyproject.toml` and locking dependencies, which introduces unnecessary dependency overhead.
- **Option C**: Use raw synchronous `open()` inside a thread pool using `anyio.to_thread.run_sync`. This is functionally similar to what Starlette does automatically and adds boilerplate without simplifying the code.

### 2. Chunk Size Selection: 64 KiB
- **Option A (Chosen)**: Use `64 * 1024` (64 KiB) as the chunk size. It is a standard and highly compatible chunk size for streaming media files. It drastically reduces RAM consumption compared to 1 MiB (1/16th) and ensures rapid delivery of the first chunk to the client, minimizing initial player buffering latency.
- **Option B**: Use `128 * 1024` (128 KiB). Also a viable choice, but 64 KiB is even more memory-efficient and sufficient for typical audio bitrates (e.g., 128kbps M4A audio translates to ~16 KB of data per second of audio, meaning a 64 KB chunk provides 4 seconds of playback).
- **Option C**: Retain `1024 * 1024` (1 MiB). This would not address the RAM overhead and startup latency concerns.

## Risks / Trade-offs

- **[Risk]** Asynchronous generators on FastAPI can sometimes run into event loop starvation if blocking operations are run on the event loop.
  - *Mitigation*: Ensure all file operations in the generator (open, seek, read) use the awaited async counterparts provided by `anyio.open_file`, which delegates the actual blocking OS calls to anyio's worker threads under the hood.
- **[Risk]** Smaller chunk size increases the number of generator iterations and async scheduling events.
  - *Mitigation*: A chunk size of 64 KiB is large enough that the scheduling overhead is negligible compared to network and I/O costs, and it avoids the thread context switching of the sync-generator-in-threadpool design.
