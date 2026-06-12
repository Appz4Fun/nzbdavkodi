import re

with open("repo/plugin.video.nzbdav/resources/lib/stream_proxy.py", "r") as f:
    text = f.read()

text = text.replace(
    "# fmt: off\n            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False)  # lgtm [py/command-line-injection]  # nosec B603  # noqa: E501\n            # fmt: on",
    "proc = subprocess.Popen(  # nosec B603\n                cmd,\n                stdin=subprocess.DEVNULL,\n                stdout=subprocess.PIPE,\n                stderr=subprocess.PIPE,\n                shell=False,\n            )"
)

text = text.replace(
    "# fmt: off\n                self._proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=self._ffmpeg_log, shell=False, cwd=self.session_dir)  # lgtm [py/command-line-injection]  # nosec B603  # noqa: E501\n                # fmt: on",
    "self._proc = subprocess.Popen(  # nosec B603\n                    cmd,\n                    stdin=subprocess.DEVNULL,\n                    stdout=subprocess.DEVNULL,\n                    stderr=self._ffmpeg_log,\n                    shell=False,\n                    cwd=self.session_dir,\n                )"
)

text = text.replace(
    "# fmt: off\n            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, shell=False)  # lgtm [py/command-line-injection]  # nosec B603  # noqa: E501\n            # fmt: on",
    "proc = subprocess.Popen(  # nosec B603\n                cmd,\n                stdin=subprocess.DEVNULL,\n                stdout=subprocess.DEVNULL,\n                stderr=subprocess.PIPE,\n                shell=False,\n            )"
)

text = text.replace(
    "# fmt: off\n            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False)  # lgtm [py/command-line-injection]  # nosec B603  # noqa: E501\n            # fmt: on",
    "proc = subprocess.Popen(  # nosec B603\n                cmd,\n                stdin=subprocess.DEVNULL,\n                stdout=subprocess.PIPE,\n                stderr=subprocess.PIPE,\n                shell=False,\n            )"
)

with open("repo/plugin.video.nzbdav/resources/lib/stream_proxy.py", "w") as f:
    f.write(text)
