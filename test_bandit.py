import subprocess
cmd = []
kw = {}
proc = subprocess.Popen(cmd, **kw)  # noqa: E501  # lgtm[py/command-line-injection]  # nosec B603
