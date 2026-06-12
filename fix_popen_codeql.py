import re

with open("repo/plugin.video.nzbdav/resources/lib/stream_proxy.py", "r") as f:
    text = f.read()

text = text.replace(
    "# fmt: off\n            proc = subprocess.Popen(cmd, **kw)  # nosec B603  # lgtm[py/command-line-injection]  # noqa: E501",
    "# fmt: off\n            proc = subprocess.Popen(cmd, **kw)  # lgtm [py/command-line-injection]  # nosec B603  # noqa: E501"
)

text = text.replace(
    "# fmt: off\n                self._proc = subprocess.Popen(cmd, **kw)  # nosec B603  # lgtm[py/command-line-injection]  # noqa: E501",
    "# fmt: off\n                self._proc = subprocess.Popen(cmd, **kw)  # lgtm [py/command-line-injection]  # nosec B603  # noqa: E501"
)

with open("repo/plugin.video.nzbdav/resources/lib/stream_proxy.py", "w") as f:
    f.write(text)
