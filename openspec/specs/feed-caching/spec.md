# feed-caching Specification

## Purpose
TBD - created by archiving change cache-feed-xml. Update Purpose after archive.
## Requirements
### Requirement: Feed In-Memory Caching
The system SHALL cache the generated feed XML in memory and serve it on subsequent requests without scanning the output directory or parsing sidecar files.

#### Scenario: Serve feed from cache on subsequent request
- **WHEN** a request is made to `/feed.xml` and a valid cached feed XML exists
- **THEN** the system SHALL return the cached XML immediately without reading files from the disk

### Requirement: Cache Invalidation by Directory State
The system SHALL check the output directory's modified time (`st_mtime`) and total file count to determine if the cache is stale. If either has changed, the system SHALL invalidate the cache, scan the directory, and rebuild the feed.

#### Scenario: Invalidate cache when new file is added
- **WHEN** a request is made to `/feed.xml` and the file count or modified time of the output directory has changed
- **THEN** the system SHALL invalidate the cached XML, rebuild the feed from disk, update the cached XML, and return the new XML

