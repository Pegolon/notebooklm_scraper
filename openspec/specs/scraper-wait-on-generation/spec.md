# scraper-wait-on-generation Specification

## Purpose
TBD - created by archiving change wait-on-podcast-creation. Update Purpose after archive.
## Requirements
### Requirement: Scraper waits on podcast audio overview generation
The scraper SHALL wait for the audio overview generation to complete when navigating to a notebook where the overview is currently being created.

#### Scenario: Audio overview is generating during download attempt
- **WHEN** the scraper opens a notebook page and the audio overview is currently generating (audio overview row has only 1 button or the last button is disabled)
- **THEN** the scraper SHALL poll every 15 seconds until the overview is fully generated (audio overview row has at least 2 buttons and the last button is enabled), up to a configurable maximum timeout of `GENERATION_TIMEOUT_S` (defaulting to 900 seconds), before proceeding to click the kebab button and download.

