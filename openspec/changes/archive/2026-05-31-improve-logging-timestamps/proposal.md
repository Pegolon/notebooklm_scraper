## Why

When running the supervisor/orchestrator, output lines from the long-running cloud app (`cloud/app.py` via uvicorn) do not have timestamps, and lines from the scraper (`local/scraper.py`) carry their own timestamps, making it difficult to analyze and align logs chronologically. Having a consistent orchestrator timestamp prefix for every line of child output solves this and improves overall log readability.

## What Changes

- Modify `orchestrate.py`'s `_pipe_to_log` method to generate and prepend the current timestamp to every line printed from both `scraper` and `cloud` processes.
- Ensure the prepended timestamp format matches the main orchestrator log format (`%Y-%m-%d %H:%M:%S,%f` truncated to milliseconds).

## Capabilities

### New Capabilities
- `orchestrator-logging`: Unified timestamp logging prefix for all orchestrated child outputs.

### Modified Capabilities
<!-- Existing capabilities whose REQUIREMENTS are changing (not just implementation).
     Only list here if spec-level behavior changes. Each needs a delta spec file.
     Use existing spec names from openspec/specs/. Leave empty if no requirement changes. -->
