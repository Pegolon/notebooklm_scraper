## 1. Environment Configuration

- [x] 1.1 Document `GENERATION_TIMEOUT_S` setting in `local/.env.example` under the scraper configuration section.

## 2. Scraper Implementation

- [x] 2.1 Import `time` in `local/scraper.py`.
- [x] 2.2 Define the config constant `GENERATION_TIMEOUT_S` loaded from the environment with a default of 900.
- [x] 2.3 Implement the polling and waiting logic in `download_audio_overview` of `local/scraper.py` checking if the kebab button is ready and enabled, logging progress every 30 seconds.

## 3. Verification

- [x] 3.1 Verify there are no syntax errors in the updated `local/scraper.py` file.
