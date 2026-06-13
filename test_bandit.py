import subprocess
def f(cmd):
    # lgtm [py/command-line-injection]
    proc = subprocess.Popen(cmd, shell=False)  # nosec B603
    return proc
