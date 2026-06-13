with open("repo/plugin.video.nzbdav/resources/lib/stream_proxy.py", "r") as f:
    lines = f.readlines()
with open("repo/plugin.video.nzbdav/resources/lib/stream_proxy.py", "w") as f:
    for line in lines:
        if "proc = subprocess.Popen(  # lgtm [py/command-line-injection]  # nosec B603" in line:
            indent = line.split("proc")[0]
            f.write(indent + "proc = subprocess.Popen(  # noqa: E501  # lgtm [py/command-line-injection]  # nosec B603\n")
        else:
            f.write(line)
