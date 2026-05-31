## 1. Parse and Serve Chapters JSON Endpoint

- [x] 1.1 Implement a parser helper `_parse_chaptermarks(path: Path) -> dict` in `cloud/app.py` to parse FFMETADATA1 formatted files.
- [x] 1.2 Implement unescaping function `_unescape_metadata_val(val: str) -> str` to decode FFmpeg escapes back to normal characters.
- [x] 1.3 Implement the `GET /chapters/{filename}` endpoint in `cloud/app.py` that resolves `{filename}` to a physical `.chaptermarks.txt` file using the `_safe_resolve` helper.
- [x] 1.4 Configure the endpoint response header to set `Content-Type: application/json+chapters`.

## 2. RSS Feed XML Modification

- [x] 2.1 Update the episode loading logic (`load_episodes`) in `cloud/app.py` to check for the presence of sibling `.chaptermarks.txt` files and add a key (e.g., `_chapters_file`) to the episode metadata dictionary.
- [x] 2.2 In `build_feed`, if the chapters sidecar exists, inject a `<podcast:chapters>` element into the item element.
- [x] 2.3 Ensure the element contains `url` pointing to the public endpoint `/chapters/{filename}` and `type="application/json+chapters"`.

## 3. Cache Invalidation and Verification

- [x] 3.1 Update the feed cache check in `serve_feed` to monitor the modification times and count of `.chaptermarks.txt` files in `OUTPUT_DIR` so changes to chapters invalidate the cached feed XML.
- [x] 3.2 Add validation checks or manual tests to verify the chapters response payload and the feed XML structure.
