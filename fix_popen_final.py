with open("repo/plugin.video.nzbdav/resources/lib/stream_proxy.py", "r") as f:
    text = f.read()

import re

# Okay, wait.
# Before I touched anything, it WAS multi-line:
# ```python
#             proc = subprocess.Popen(
#                 cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False
#             )
# ```
# And there was NO SUPPRESSION AT ALL for LGTM. CodeQL passed!
# Why did CodeQL pass without `# lgtm`?
# Because `shell=False` was passed directly. CodeQL's `py/command-line-injection` rule ONLY triggers when `shell=True` or when the command string could be interpreted as a shell command (which doesn't happen if `cmd` is a list and `shell=False`).
# The problem is that when I changed it to `**kw`, CodeQL couldn't see `shell=False`.
# BUT now I changed it to:
# ```python
# proc = subprocess.Popen(
#     cmd,
#     stdin=subprocess.DEVNULL,
#     stdout=subprocess.PIPE,
#     stderr=subprocess.PIPE,
#     shell=False,
# )  # nosec B603
# ```
# Wait... when did I do that?
# In Attempt 9 I did EXACTLY what memory said:
# `# fmt: off\n proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False)  # noqa: E501  # nosec B603  # lgtm [py/command-line-injection]\n# fmt: on`
# And CodeQL FAILED!

# Why did CodeQL fail?
# Maybe CodeQL is NOT parsing `cmd` correctly because of `# noqa: E501`? NO.
# Is it because I passed `cmd` as a list? Yes, `cmd` is a list.

# What if I REVERT EXACTLY to the original layout, but just add `stdin=subprocess.DEVNULL` to it?
# In origin/main:
# ```python
#             proc = subprocess.Popen(
#                 cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False
#             )
# ```
# Let's change it to:
# ```python
#             proc = subprocess.Popen(
#                 cmd,
#                 stdin=subprocess.DEVNULL,
#                 stdout=subprocess.PIPE,
#                 stderr=subprocess.PIPE,
#                 shell=False,
#             )
# ```
# Without `# lgtm`, without `# nosec`, without `# noqa`!
# BUT bandit will fail if we don't put `# nosec B603`.
# So let's put `# nosec B603` on the first line:
# ```python
#             proc = subprocess.Popen(  # nosec B603
#                 cmd,
#                 stdin=subprocess.DEVNULL,
#                 stdout=subprocess.PIPE,
#                 stderr=subprocess.PIPE,
#                 shell=False,
#             )
# ```
# I tested this in bandit locally, and it passed Bandit!
# Since CodeQL passed without `# lgtm` originally, it should pass without `# lgtm` now, because `shell=False` is literal and explicitly visible.

text = text.replace(
    "# fmt: off\n            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False)  # noqa: E501  # nosec B603  # lgtm [py/command-line-injection]\n            # fmt: on",
    "proc = subprocess.Popen(  # nosec B603\n                cmd,\n                stdin=subprocess.DEVNULL,\n                stdout=subprocess.PIPE,\n                stderr=subprocess.PIPE,\n                shell=False,\n            )"
)

text = text.replace(
    "# fmt: off\n                self._proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=self._ffmpeg_log, shell=False, cwd=self.session_dir)  # noqa: E501  # nosec B603  # lgtm [py/command-line-injection]\n                # fmt: on",
    "self._proc = subprocess.Popen(  # nosec B603\n                    cmd,\n                    stdin=subprocess.DEVNULL,\n                    stdout=subprocess.DEVNULL,\n                    stderr=self._ffmpeg_log,\n                    shell=False,\n                    cwd=self.session_dir,\n                )"
)

text = text.replace(
    "# fmt: off\n            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, shell=False)  # noqa: E501  # nosec B603  # lgtm [py/command-line-injection]\n            # fmt: on",
    "proc = subprocess.Popen(  # nosec B603\n                cmd,\n                stdin=subprocess.DEVNULL,\n                stdout=subprocess.DEVNULL,\n                stderr=subprocess.PIPE,\n                shell=False,\n            )"
)

text = text.replace(
    "# fmt: off\n            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False)  # noqa: E501  # nosec B603  # lgtm [py/command-line-injection]\n            # fmt: on",
    "proc = subprocess.Popen(  # nosec B603\n                cmd,\n                stdin=subprocess.DEVNULL,\n                stdout=subprocess.PIPE,\n                stderr=subprocess.PIPE,\n                shell=False,\n            )"
)

with open("repo/plugin.video.nzbdav/resources/lib/stream_proxy.py", "w") as f:
    f.write(text)
