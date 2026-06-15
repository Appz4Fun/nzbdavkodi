import subprocess
def run():
    kw = dict(stdin=None, stdout=None, stderr=None, shell=False)
    # fmt: off
    subprocess.Popen(["ls"], **kw)  # noqa: E501  # nosec B603  # lgtm [py/command-line-injection]
    # fmt: on
