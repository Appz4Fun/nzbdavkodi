with open("repo/plugin.video.nzbdav/resources/lib/stream_proxy.py", "r") as f:
    text = f.read()

# Okay, CodeQL is failing no matter what if we don't suppress it.
# The original code `main` DID NOT FAIL. Why did `main` not fail?!
# Let's check `main` again.
