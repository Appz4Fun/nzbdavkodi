# TODO

## Backlog

### Cache unbounded growth (Low Priority)

**Problem:** Each search caches results as a JSON file in `~/.kodi/userdata/addon_data/plugin.video.nzbdav/cache/`. There is no limit on the number of cached files or total disk usage. Over time, a user who searches frequently could accumulate hundreds of MB of cache files that are never cleaned up (they expire after TTL but the files remain on disk).

**Current behavior:** `cache.py:set_cached()` writes a new JSON file per search. `cache.py:get_cached()` checks TTL and returns `None` for stale entries but does not delete the stale file. `cache.py:clear_cache()` deletes all files but is only called when the user manually selects "Clear Cache" from the main menu.

**Impact:** Gradual disk usage growth on devices with limited storage (Raspberry Pi, CoreELEC boxes). A typical cache entry is 50-200KB, so 1000 searches = 50-200MB of orphaned JSON files.

**Proposed fix:** Add an LRU eviction policy to `set_cached()` — after writing a new entry, check total cache directory size and delete the oldest files if over a threshold (e.g., 50MB configurable via settings). Alternatively, have `get_cached()` delete stale files it encounters instead of just returning `None`.

**Files:** `plugin.video.nzbdav/resources/lib/cache.py`

---

### Storage path fallback edge case (Low Priority)

**Problem:** `resolver.py:_storage_to_webdav_path()` converts nzbdav's storage path to a WebDAV content path. If the storage path doesn't start with the expected prefix (`/mnt/nzbdav/completed-symlinks/`), the fallback takes the last 2 path components and constructs `/content/category/name/`. This fails for paths with fewer than 2 components (e.g., `/mnt/file.mkv` produces `/content/mnt/file.mkv/` which is wrong).

**Current behavior:** Line 60-61 in `resolver.py`:
```python
parts = storage.rstrip("/").split("/")
relative = "/".join(parts[-2:]) if len(parts) >= 2 else parts[-1]
```
This always takes 2 components even if they're not category/name (e.g., the empty string at index 0 from leading `/`).

**Impact:** Only affects users whose nzbdav uses a non-standard storage path. Standard nzbdav installations use the expected prefix so the fallback is never hit. If hit, playback would fail with a "file not found" error.

**Proposed fix:** Validate that the fallback path produces a sensible WebDAV URL. If the storage path has fewer than 3 components after splitting (i.e., can't extract category + name), log a warning and return `None` so the caller can surface a meaningful error instead of silently constructing a wrong path.

**Files:** `plugin.video.nzbdav/resources/lib/resolver.py:50-62`

---

### WebDAV auth URL encoding with special characters (Low Priority)

**Problem:** `webdav.py:get_webdav_stream_url()` and `get_webdav_stream_url_for_path()` embed WebDAV credentials directly in the URL using `urllib.parse.quote()`. If the password contains `@`, the resulting URL `http://user:p%40ss@host/path` is technically valid per RFC 3986, but some HTTP clients and Kodi's player may not parse it correctly because `@` is the userinfo delimiter.

**Current behavior:** Lines 52-59 in `webdav.py`:
```python
proto, rest = base.split("://", 1)
return "{}://{}:{}@{}/{}".format(
    proto,
    quote(username, safe=""),
    quote(password, safe=""),
    rest,
    quote(filename, safe=""),
)
```
The `quote(password, safe="")` correctly percent-encodes `@` to `%40`, but the resulting URL structure `user:p%40ss@host` is ambiguous to clients that split on the first `@` before percent-decoding.

**Impact:** Only affects users whose WebDAV password contains `@`, `:`, or other URL-special characters. Most users won't encounter this.

**Proposed fix:** Use `urllib.parse.urlsplit` and `urllib.parse.urlunsplit` to construct the URL properly, or use HTTP Basic Auth headers instead of URL-embedded credentials for the streaming URL (Kodi's player supports `|` header syntax: `url|Authorization=Basic base64`).

**Files:** `plugin.video.nzbdav/resources/lib/webdav.py:46-60`, `webdav.py:250-267`
