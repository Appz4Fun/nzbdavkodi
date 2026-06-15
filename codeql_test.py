import subprocess
def run():
    kw = dict(stdin=None, stdout=None, stderr=None, shell=False)
    # lgtm [py/command-line-injection]
    subprocess.Popen(["ls"], **kw)  # noqa: E501  # nosec B603
