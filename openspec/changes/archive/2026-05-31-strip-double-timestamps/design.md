## Context

To avoid double timestamps when scraper or cloud apps print their own date/time prefixes (e.g. `2026-05-31 13:28:13 INFO ...`), we must check for and strip these prefixes before prepending the orchestrator's millisecond-precision timestamp.

## Goals / Non-Goals

**Goals:**
- Identify and strip `YYYY-MM-DD HH:MM:SS` (followed by whitespace) from the start of forwarded lines.
- Preserve other logging attributes (such as the log level and message content) intact.

**Non-Goals:**
- Strip timestamps that appear in the middle of log lines.

## Decisions

### Decision 1: Use `re` (regular expressions) in `orchestrate.py`
- **Chosen Option:** Define `TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\s")` at the module level. Before prepending the orchestrator timestamp, use `TIMESTAMP_RE.sub("", decoded_line, count=1)` to remove the duplicate timestamp prefix if it exists.
- **Alternatives Considered:** Doing string prefix slicing (e.g. checking length and slices). However, regex is far more robust against variation in prefix spacing/content structure and guarantees we only strip valid timestamps.

## Risks / Trade-offs

- **Risk:** Unintentional stripping of text lines that happen to look like timestamps.
- **Mitigation:** The regex pattern requires a very specific structure (`YYYY-MM-DD HH:MM:SS `) anchored strictly to the beginning of the line (`^`), which is extremely unlikely to occur in normal non-timestamp output.
