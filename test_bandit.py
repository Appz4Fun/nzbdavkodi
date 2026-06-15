import subprocess
kw = {"stdin": None}
# lgtm [py/command-line-injection]
subprocess.Popen(["ls"], **kw) # noqa: E501 # nosec B603
