import subprocess


def test():
    cmd = ["ls"]
    proc = subprocess.Popen(
        cmd,  # lgtm [py/command-line-injection]  # nosec B603
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
    )
