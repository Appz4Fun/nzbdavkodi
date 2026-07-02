1. **Identify the optimization opportunity:** In `repo/plugin.video.nzbdav/resources/lib/resolver_playback.py`, the `_kodi_video_db_version` function compiles the regex `r"MyVideos(\d+)\.db$"` on every invocation using `re.search`.
2. **Apply optimization:** Pre-compile the regular expression at the module level (e.g. `_MY_VIDEOS_DB_RE = re.compile(r"MyVideos(\d+)\.db$")`) and use `_MY_VIDEOS_DB_RE.search(...)` inside `_kodi_video_db_version`. This avoids recompiling the regex on every database file lookup.
3. **Verify:** Run format, lint checks, and the test suite using `just`.
4. **Document:** Ensure comments explain the performance optimization. Create `.jules/bolt.md` entry if this is a novel finding.
5. **Pre-commit and PR:** Create the PR following Bolt's specific formatting requirements.
