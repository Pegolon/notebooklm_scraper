## Why

Google NotebookLM generates high-quality Audio Overviews, and the local pipeline partitions them into chapters (`.chaptermarks.txt` and embedded MP4 chapters). Currently, the cloud-side podcast server (`cloud/app.py`) serves the RSS feed and audio streams but does not expose these chapter markers in a web-standard format. Adding support for Podcasting 2.0 Chapters allows compliant podcast players (like Overcast, Pocket Casts, and Fountain) to display chapter names and timestamps directly during streaming without needing to download the entire audio file first.

## What Changes

*   **FastAPI Chapters Endpoint:** Expose a new route `GET /chapters/{filename}` that returns standard Podcast Chapters JSON metadata, parsed on-demand from the sibling `<basename>.chaptermarks.txt` file in `OUTPUT_DIR`.
*   **RSS Feed Integration:** Inject the `<podcast:chapters>` tag with the absolute URL to the JSON chapters endpoint and `type="application/json+chapters"` for every feed item that has a sibling `.chaptermarks.txt` file.
*   **Response Header & Content-Type:** Return chapters with `Content-Type: application/json+chapters` as recommended by the Podcasting 2.0 specification, with standard `application/json` compatibility.

## Capabilities

### New Capabilities
*   `podcasting-2-0-chapters`: Exposing chapter files in standard JSON format and integration with the RSS feed.

### Modified Capabilities
<!-- None -->

## Impact

*   **Affected Files:** `cloud/app.py` (adds endpoint and updates XML generator).
*   **External APIs:** Exposes `/chapters/{filename}` publicly.
*   **Bandwidth/Resources:** Extremely minimal. Directory check is done during existing `load_episodes()` scan to look for the `.chaptermarks.txt` sidecar.
