import subprocess
kw = {"stdin": None}
subprocess.Popen(["ls"], **kw) # nosec B603
