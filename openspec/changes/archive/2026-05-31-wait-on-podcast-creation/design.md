## Context

When a user initiates the creation of an audio overview podcast in NotebookLM, the UI creates a placeholder row in the Studio panel. During generation, this row has a single button (the main, stretched button) that is disabled. The other action buttons (Interactive mode, Play, kebab menu) are not present. Currently, the scraper assumes the row is fully ready, counts the buttons (obtaining 1), tries to click the last button (the main, disabled button), and times out after 5000ms.

We need to implement a waiting mechanism in `local/scraper.py` that polls the row's buttons until the kebab menu button is present and enabled.

## Goals / Non-Goals

**Goals:**
- Implement a robust polling mechanism in `download_audio_overview` to wait for generation to finish.
- Log clear, periodic messages so that users (and orchestrator logs) can see the scraper is waiting rather than hung.
- Make the maximum wait time configurable via a new environment variable `GENERATION_TIMEOUT_S`.

**Non-Goals:**
- We do not initiate the generation of the podcast overview ourselves (the scraper only handles downloading what is already requested/generated).
- We do not handle automatic regeneration of the podcast overview if generation fails on Google's side.

## Decisions

### 1. Wait Condition and Polling Location
We will place the polling logic directly within `download_audio_overview(page: Page)` in `local/scraper.py`.
- *Alternative Considered*: Place the logic inside `_find_audio_overview_row(page: Page)`.
- *Rationale*: `_find_audio_overview_row` is meant to locate the row element, which is already present (as a `<artifact-library-item>`) even when generation is in progress. Placing the wait in `download_audio_overview` makes logical sense because the action of downloading depends on the buttons within the row being fully initialized and interactive.

### 2. Ready Detection Strategy
We will define the overview as "ready" when:
1. The row has at least 2 buttons (indicating the kebab button is rendered, in addition to the main button).
2. The last button (kebab menu) is not disabled.
- *Alternative Considered*: Wait specifically for the button count to equal 4.
- *Rationale*: Google could change the button count (e.g. from 4 to 3 or 5). Relying on `n >= 2` and checking that the kebab button (which is always the last button) is enabled is more future-proof and resilient to minor DOM adjustments.

## Risks / Trade-offs

- [Risk] Playwright locator evaluation might raise exceptions if the DOM element is temporarily detached or redrawn during generation updates.
  - *Mitigation*: Run the `is_disabled()` check inside a `try/except` block and re-fetch the buttons locator on every iteration of the polling loop to avoid stale element references.
- [Risk] Infinite loop if generation hangs or fails on Google's side.
  - *Mitigation*: Limit the polling loop by a timeout (`GENERATION_TIMEOUT_S`, default 900 seconds) and raise a descriptive `RuntimeError` if exceeded.
