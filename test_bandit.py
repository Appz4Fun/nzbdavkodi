import subprocess
cmd = ["ls"]
kw = dict(
    stdin=subprocess.DEVNULL,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    shell=False,
)
# fmt: off
proc = subprocess.Popen(cmd, **kw)  # lgtm [py/command-line-injection]  # nosec B603  # noqa: E501
# fmt: on
