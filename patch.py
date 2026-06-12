import re

file_path = "repo/plugin.video.nzbdav/resources/lib/stream_proxy.py"
with open(file_path, "r") as f:
    content = f.read()

content = re.sub(r'(\s+)proc = subprocess\.Popen\(\n\n\s+cmd,  # codeql', r'\1proc = subprocess.Popen(\n\1    cmd,  # codeql', content)
content = re.sub(r'(\s+)self\._proc = subprocess\.Popen\(\n\n\s+cmd,  # codeql', r'\1self._proc = subprocess.Popen(\n\1    cmd,  # codeql', content)

with open(file_path, "w") as f:
    f.write(content)
