with open("repo/plugin.video.nzbdav/resources/lib/stream_proxy.py", "r") as f:
    lines = f.readlines()
with open("repo/plugin.video.nzbdav/resources/lib/stream_proxy.py", "w") as f:
    for line in lines:
        if "proc = subprocess.Popen(cmd, **kw)  # nosec B603  # lgtm[py/command-line-injection]  # noqa: E501" in line:
            indent = line.split("proc")[0]
            if "self._proc" in line:
                indent = line.split("self._proc")[0]
                f.write(indent + "self._proc = subprocess.Popen(cmd, **kw)  # nosec B603  # lgtm [py/command-line-injection]  # noqa: E501\n")
            else:
                f.write(indent + "proc = subprocess.Popen(cmd, **kw)  # nosec B603  # lgtm [py/command-line-injection]  # noqa: E501\n")
        else:
            f.write(line)
