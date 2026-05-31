# orchestrator-logging Specification

## Purpose
TBD - created by archiving change improve-logging-timestamps. Update Purpose after archive.
## Requirements
### Requirement: Unified timestamp prefixing for child processes
The orchestrator SHALL format and prepend a timestamp to all stdout/stderr lines captured from child processes (`cloud` and `scraper`) before printing them to stdout. The timestamp format MUST match the orchestrator's native timestamp format (YYYY-MM-DD HH:MM:SS,mmm).

#### Scenario: Child log line timestamp prepended
- **WHEN** the orchestrator's pipe log task receives a line of text from a child process stream
- **THEN** the orchestrator SHALL retrieve the current local date and time
- **AND** format it as `YYYY-MM-DD HH:MM:SS,mmm`
- **AND** write the line to stdout prefixed with the formatted timestamp, a space, and the child identifier prefix (e.g. `[cloud]` or `[scraper]`)

