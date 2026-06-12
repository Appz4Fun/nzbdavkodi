with open("repo/plugin.video.nzbdav/resources/lib/stream_proxy.py", "r") as f:
    text = f.read()

import re

# Wait, `proc = subprocess.Popen(...)  # nosec B603` failed Bandit? NO!
# The log says:
# "Process completed with exit code 4."
# "tests/test_stream_proxy.py:6281:0: E0001: Cannot import 'resources.lib.stream_proxy' due to 'keyword argument repeated (resources.lib.stream_proxy, line 2094)' (syntax-error)"
# WAIT! The duplicate keyword argument error is BACK?
# Yes! `fix_popen_final.py` from my PREVIOUS attempt caused duplicate kwargs again!!

# Let's check `repo/plugin.video.nzbdav/resources/lib/stream_proxy.py` again.
