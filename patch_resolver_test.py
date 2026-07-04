import re

filepath = "tests/test_resolver.py"
with open(filepath, "r") as f:
    content = f.read()

# Replace hardcoded sleep with monitor-aware sleep logic in test mock
old_block = """    def slow_history_lookup(_title):
        _time.sleep(0.18)
        return {
            "status": "Completed",
            "storage": "/mnt/nzbdav/completed-symlinks/uncategorized/movie",
        }"""
new_block = """    def slow_history_lookup(_title):
        _time.sleep(0.18)
        return {
            "status": "Completed",
            "storage": "/mnt/nzbdav/completed-symlinks/uncategorized/movie",
        }"""

# Wait, why did the test timeout fail with 10.015s?
