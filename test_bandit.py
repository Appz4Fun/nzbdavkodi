import subprocess
cmd = ["ls"]
proc = subprocess.Popen(  # nosec B603
    cmd,
    stdin=subprocess.DEVNULL,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    shell=False,
)
