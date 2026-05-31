## Why

While prepending orchestrator-level timestamps to child logs works correctly, some log lines from `scraper` and `cloud` already contain their own local timestamps (in the format `YYYY-MM-DD HH:MM:SS`). This results in double timestamps on the same log line, reducing readability.

## What Changes

- Modify `orchestrate.py` to identify and remove any leading local timestamp (`YYYY-MM-DD HH:MM:SS`) from a child log line before prepending the orchestrator's timestamp.

## Capabilities

### New Capabilities

### Modified Capabilities
- `orchestrator-logging`: The requirement is updated to strip child-level duplicate timestamps if they are present at the beginning of log lines.

## Impact

- `orchestrate.py`
