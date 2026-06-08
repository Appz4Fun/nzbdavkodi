import subprocess


def foo():
    cmd = ["ls"]
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
    )  # noqa: E501  # lgtm [py/command-line-injection]  # nosec B603
