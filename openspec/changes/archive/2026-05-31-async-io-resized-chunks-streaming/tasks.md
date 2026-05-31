## 1. Code Changes in cloud/app.py

- [x] 1.1 Import `anyio` and import `AsyncIterable` from `typing` in `cloud/app.py`.
- [x] 1.2 Update `_AUDIO_CHUNK_SIZE` to `64 * 1024` (64 KiB).
- [x] 1.3 Convert `_iter_file` to an asynchronous generator (`async def`) using `anyio.open_file` for asynchronous, non-blocking file access.
- [x] 1.4 Update the `serve_audio` endpoint in `cloud/app.py` to be an `async def` function.

## 2. Verification and Testing

- [x] 2.1 Start the FastAPI app and verify it launches successfully without errors.
- [x] 2.2 Perform a GET request on `/audio/{filename}` without a Range header and confirm a 200 OK response with `Content-Type: audio/x-m4a` and correct file size.
- [x] 2.3 Perform a GET request with a `Range` header (e.g., `Range: bytes=0-1023`) and confirm a 206 Partial Content response with correct headers (`Content-Range` and `Content-Length`) and payload.
- [x] 2.4 Perform a HEAD request on `/audio/{filename}` and verify that the correct headers are returned without body content.
