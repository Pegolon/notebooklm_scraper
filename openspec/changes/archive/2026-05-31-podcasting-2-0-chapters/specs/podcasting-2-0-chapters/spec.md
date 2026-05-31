## ADDED Requirements

### Requirement: Serve Chapter JSON Endpoint
The cloud server SHALL expose a `GET /chapters/{filename}` endpoint. The endpoint SHALL parse the sibling `<basename>.chaptermarks.txt` file for a requested `<basename>.json` file, and return the output as a standard Podcast Chapters JSON payload. The HTTP response status code SHALL be `200` on success, and `404` if the corresponding file does not exist or is unreadable. The `Content-Type` header of the response SHALL be `application/json+chapters`.

#### Scenario: Successful retrieval of chapters JSON
- **WHEN** a client makes a `GET /chapters/9a8b7c.json` request
- **AND** the file `9a8b7c.chaptermarks.txt` exists and is valid in `OUTPUT_DIR`
- **THEN** the server SHALL return status code `200`
- **AND** the response body SHALL contain a valid chapters JSON object
- **AND** the `Content-Type` header SHALL be `application/json+chapters`

#### Scenario: Sibling file does not exist
- **WHEN** a client makes a `GET /chapters/nonexistent.json` request
- **AND** the file `nonexistent.chaptermarks.txt` does not exist in `OUTPUT_DIR`
- **THEN** the server SHALL return status code `404`

---

### Requirement: Parse FFMETADATA1 to JSON
The cloud server SHALL parse the FFMETADATA1 chapters from `<basename>.chaptermarks.txt` into a JSON document. The output JSON document SHALL contain a `"version": "1.2"` string and a `"chapters"` array. Each object in the `"chapters"` array SHALL contain a `"startTime"` field as a float value representing the start time of the chapter in seconds (computed from `START` milliseconds divided by `1000.0`), and a `"title"` field containing the unescaped chapter title. Unescaping SHALL decode backslash-escaped characters (`\\`, `\=`, `\;`, `\#`, `\n`) to their literal representations.

#### Scenario: FFMETADATA1 file parsed successfully
- **WHEN** the server reads a `.chaptermarks.txt` file containing chapter definitions with `START=125500` and `title=Intro\\=Chapter`
- **THEN** the generated JSON chapters array SHALL include an item with `"startTime": 125.5`
- **AND** `"title": "Intro=Chapter"`

---

### Requirement: Inject podcast:chapters tag in feed.xml
The cloud server SHALL inject a `<podcast:chapters>` tag under the `<item>` element of each episode in the `GET /feed.xml` RSS feed, if and only if the episode has a sibling `.chaptermarks.txt` file in `OUTPUT_DIR`. The tag SHALL have an `url` attribute containing the absolute URL to the chapters endpoint (e.g. `${FEED_BASE_URL}/chapters/<basename>.json`) and a `type` attribute containing `application/json+chapters`.

#### Scenario: Feed item includes chapters tag
- **WHEN** the server builds the RSS feed for an episode `<hash>.m4a`
- **AND** the file `<hash>.chaptermarks.txt` exists in `OUTPUT_DIR`
- **THEN** the corresponding `<item>` element in the RSS feed SHALL include a `<podcast:chapters>` element
- **AND** its `url` attribute SHALL be `${FEED_BASE_URL}/chapters/<hash>.json`
- **AND** its `type` attribute SHALL be `application/json+chapters`

#### Scenario: Feed item excludes chapters tag
- **WHEN** the server builds the RSS feed for an episode `<hash>.m4a`
- **AND** the file `<hash>.chaptermarks.txt` does not exist in `OUTPUT_DIR`
- **THEN** the corresponding `<item>` element in the RSS feed SHALL NOT include a `<podcast:chapters>` element

---

### Requirement: Feed Cache Invalidation checks chaptermarks modification
The cloud server's feed cache validation logic SHALL inspect the modification time (`st_mtime`) of `.chaptermarks.txt` files and the total count of `.chaptermarks.txt` files, in addition to `.m4a` files. If any `.chaptermarks.txt` file is added, deleted, or modified, the cached feed XML SHALL be invalidated and rebuilt on the next request.

#### Scenario: Cache invalidated when chapter marks change
- **WHEN** a `.chaptermarks.txt` file is modified in `OUTPUT_DIR`
- **THEN** the server SHALL detect that the cached feed is stale
- **AND** rebuild the RSS feed XML from scratch on the next request
