## MODIFIED Requirements

### Requirement: Cloud app serves M4A audio files
The cloud app's `/audio/{filename}` endpoint SHALL accept filenames with `.m4a` suffix and serve them with `Content-Type: audio/x-m4a`. The endpoint SHALL retain full HTTP Range support (single-range requests, 206 Partial Content, ETag, Last-Modified, Cache-Control). The file streaming SHALL be performed asynchronously and non-blockingly, reading the file in chunks of a configured size (e.g., 64 KB or 128 KB).

#### Scenario: Serve M4A with Range support
- **WHEN** a client requests `GET /audio/<hash>.m4a` with a `Range: bytes=0-1023` header
- **THEN** the server SHALL respond with `206 Partial Content`
- **AND** the `Content-Type` SHALL be `audio/x-m4a`
- **AND** the `Content-Range` header SHALL be present

#### Scenario: Serve M4A without Range header
- **WHEN** a client requests `GET /audio/<hash>.m4a` without a Range header
- **THEN** the server SHALL respond with `200 OK`
- **AND** the `Content-Type` SHALL be `audio/x-m4a`

#### Scenario: HEAD request for M4A
- **WHEN** a client sends `HEAD /audio/<hash>.m4a`
- **THEN** the server SHALL respond with full headers but an empty body
- **AND** the `Content-Type` SHALL be `audio/x-m4a`

#### Scenario: Path safety rejects non-M4A suffix
- **WHEN** a client requests `/audio/file.mp3`
- **THEN** the endpoint SHALL return `404 Not Found`
