import re
with open("repo/plugin.video.nzbdav/resources/lib/stream_proxy.py", "r") as f:
    text = f.read()

# I am completely losing my mind on the CodeQL parsing rules.
# Let's read `direct_indexers.py` or any other file to see if THEY have `# lgtm [py/command-line-injection]`.
