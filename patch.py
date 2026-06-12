import re

file_path = "repo/plugin.video.nzbdav/resources/lib/stream_proxy.py"
with open(file_path, "r") as f:
    content = f.read()

# Replace lgtm with codeql in existing `# lgtm [py/command-line-injection]` comments
content = re.sub(
    r'# lgtm \[py/command-line-injection\]',
    r'# codeql[py/command-line-injection]',
    content
)

with open(file_path, "w") as f:
    f.write(content)
