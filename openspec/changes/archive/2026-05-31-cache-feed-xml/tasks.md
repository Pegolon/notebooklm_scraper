## 1. Implement Feed Caching Logic

- [x] 1.1 Add `threading.Lock` and global cache state variables in `cloud/app.py` (`_feed_cache`, `_feed_cache_mtime`, `_feed_cache_file_count`, `_feed_cache_lock`).
- [x] 1.2 Refactor `serve_feed` to check the current directory modified time and `.m4a` file count, and retrieve from or rebuild cache under thread-lock control.

## 2. Verification

- [x] 2.1 Verify that subsequent `/feed.xml` requests serve content from memory immediately, checking logs to ensure no disk I/O occurs.
- [x] 2.2 Verify cache invalidation by copying a new `.m4a`/`.json` file into `OUTPUT_DIR` and confirming `/feed.xml` refreshes and rebuilds.
