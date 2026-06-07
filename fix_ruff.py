import re
with open('repo/plugin.video.nzbdav/resources/lib/stream_proxy.py', 'r') as f:
    content = f.read()

# Ruff passed because we've set `line-length = 88` in pyproject.toml but probably there's some leniency,
# wait! Earlier ruff failed! Why did it pass now?
# Because `proc = subprocess.Popen(cmd,  # lgtm [py/command-line-injection]  # nosec B603` is 89 characters!
# Wait, `# noqa: E501` is not there.
# Let's check the length of line 2092.
# `            proc = subprocess.Popen(cmd,  # lgtm [py/command-line-injection]  # nosec B603`
# `            ` = 12 chars
# `proc = subprocess.Popen(cmd,  # lgtm [py/command-line-injection]  # nosec B603` = 76 chars
# Total = 88 chars exactly! It fits!
# What about line 6676?
# `                self._proc = subprocess.Popen(cmd,  # lgtm [py/command-line-injection]  # nosec B603`
# `                ` = 16 chars
# `self._proc = subprocess.Popen(cmd,  # lgtm [py/command-line-injection]  # nosec B603` = 82 chars
# Total = 98 chars. Why didn't Ruff fail?!
# Wait, `# fmt: off` is there. Does Ruff ignore lines in `# fmt: off`? NO. Ruff is not Black.
# Oh! Ruff has `ignore = ["E501"]` or something in pyproject.toml?
