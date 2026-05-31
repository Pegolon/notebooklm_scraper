## Context

Currently, the orchestrator logs events using `logging.basicConfig` with a custom formatter that prefixes lines with `%(asctime)s [orchestrate]`. However, lines forwarded from the child processes (`scraper` and `cloud`) are piped directly to `sys.stdout` without any orchestrator-level timestamps. This leads to missing timestamps on cloud lines and inconsistent timestamps on scraper lines.

## Goals / Non-Goals

**Goals:**
- Add a timestamp prefix at the start of every line forwarded from child processes.
- Ensure the timestamp format matches the orchestrator's standard `%(asctime)s` format: `YYYY-MM-DD HH:MM:SS,mmm`.

**Non-Goals:**
- Parsing the child processes' log level to dynamically adjust the logging level.
- Suppressing or rewriting the child logs' internal timestamps.

## Decisions

### Decision 1: Prepend a manually formatted timestamp in `_pipe_to_log`
- **Options:**
  1. Route child logs through python's `logging` system.
  2. Manually format the current time and write to `sys.stdout` directly.
- **Chosen Option:** Option 2. Routing raw stdout from child processes through the Python `logging` module would append the logging level (e.g. `INFO`) to every line, even if it is a debug line or trace, resulting in redundant information and confusing log levels. Option 2 preserves the original format of child outputs while adding the timestamp and prefix consistently.
- **Format Implementation:** Use `datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S,%f")[:-3]` to format the current date and time down to milliseconds.

## Risks / Trade-offs

- **Risk:** Time zone discrepancies or formatting mismatches.
- **Mitigation:** The manual formatting matches `logging`'s default `asctime` representation (localtime) and uses standard library formatting functions.
