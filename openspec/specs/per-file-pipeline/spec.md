# per-file-pipeline Specification

## Purpose
TBD - created by archiving change incremental-per-file-pipeline. Update Purpose after archive.
## Requirements
### Requirement: Sequential per-file post-processing execution
The post-processing pipeline SHALL identify candidate files (both MP3 files needing conversion and M4A files needing downstream metadata processing) in the output directory and process each file through the entire sequence of outstanding stages (convert -> transcribe -> summarize -> chapters -> coverart -> id3tag) before moving to the next file.

#### Scenario: Sequential processing of files
- **WHEN** the scraper finishes downloading or when the standalone post-processing is triggered
- **THEN** it SHALL list all MP3 files needing conversion and M4A files needing processing
- **AND** for each file, it SHALL run its outstanding stages to completion before starting on the next file

### Requirement: Failure isolation per file
If a post-processing stage fails for a specific file, the pipeline SHALL log the error, skip any remaining dependent stages for that specific file, and proceed to the next file in the queue without terminating the entire pipeline run.

#### Scenario: Pipeline continues after file processing failure
- **WHEN** a post-processing stage (such as transcribe or summarize) fails for a specific file
- **THEN** the failure SHALL be logged
- **AND** the pipeline SHALL skip any remaining stages for that specific file
- **AND** it SHALL continue processing the remaining files in the queue

### Requirement: Stage idempotency and re-entrancy
Each individual stage of the post-processing chain for a given file SHALL check for its prerequisite files and target output files to determine if execution is necessary. It SHALL skip execution if the target output file already exists, unless explicitly forced.

#### Scenario: Pipeline skips completed stages
- **WHEN** a file already has a sidecar VTT transcript but is missing the cover art PNG
- **THEN** the pipeline SHALL skip the transcribe stage for that file
- **AND** it SHALL proceed to run the remaining stages (summarize, chapters, coverart, id3tag) for that file

