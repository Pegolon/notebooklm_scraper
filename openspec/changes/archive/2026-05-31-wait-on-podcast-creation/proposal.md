## Why

When NotebookLM is generating/creating a new podcast overview, the "Audio Overview" row is present but disabled, and the download kebab button is not yet rendered. Currently, the scraper immediately tries to find and click the kebab button to download the audio, which fails with a `Locator.click: Timeout 5000ms exceeded` error. This change allows the scraper to wait for the generation process to complete, ensuring it handles new, in-progress episodes gracefully.

## What Changes

- Add a polling and waiting mechanism to the scraper when it detects that the audio overview is currently being generated.
- Introduce an environment variable (`GENERATION_TIMEOUT_S`, defaulting to 900 seconds / 15 minutes) to specify the maximum amount of time the scraper should wait for generation.
- Poll every 15 seconds, checking if the required action buttons (particularly the kebab menu button) are rendered and enabled.
- Log periodic progress messages (every 30 seconds) to inform the user that the scraper is waiting for generation to complete rather than having hung.

## Capabilities

### New Capabilities

- `scraper-wait-on-generation`: Introduce the ability to detect and wait for an in-progress audio overview generation to finish before initiating download.

### Modified Capabilities

None.

## Impact

- `local/scraper.py`: Updated config and `download_audio_overview` method.
- `local/.env.example`: Added documentation for the new configuration variable `GENERATION_TIMEOUT_S`.
