## MODIFIED Requirements

### Requirement: Unified timestamp prefixing for child processes
The orchestrator SHALL format and prepend a timestamp to all stdout/stderr lines captured from child processes (`cloud` and `scraper`) before printing them to stdout. The timestamp format MUST match the orchestrator's native timestamp format (YYYY-MM-DD HH:MM:SS,mmm). If the child process output line already begins with a local-side timestamp in the format `YYYY-MM-DD HH:MM:SS `, that duplicate timestamp MUST be stripped from the line.

#### Scenario: Child log line timestamp prepended
- **WHEN** the orchestrator's pipe log task receives a line of text from a child process stream
- **THEN** the orchestrator SHALL retrieve the current local date and time
- **AND** format it as `YYYY-MM-DD HH:MM:SS,mmm`
- **AND** strip any leading `YYYY-MM-DD HH:MM:SS ` timestamp from the child output line if present
- **AND** write the line to stdout prefixed with the formatted timestamp, a space, and the child identifier prefix (e.g. `[cloud]` or `[scraper]`)
