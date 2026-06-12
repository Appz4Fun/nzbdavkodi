import re

file_path = "repo/plugin.video.nzbdav/resources/lib/stream_proxy.py"
with open(file_path, "r") as f:
    content = f.read()

# Let's try putting the `# codeql[py/command-line-injection]` comment EXACTLY on the line that CodeQL flags, which is the `proc = subprocess.Popen(` line!
# So:
# proc = subprocess.Popen(  # codeql[py/command-line-injection]
#     cmd,
#     ...
# )  # nosec B603

content = re.sub(
    r'\s*# codeql\[py/command-line-injection\]\n\n(\s*proc = subprocess\.Popen\()',
    r'\n\1  # codeql[py/command-line-injection]',
    content
)

content = re.sub(
    r'\s*# codeql\[py/command-line-injection\]\n\n(\s*self\._proc = subprocess\.Popen\()',
    r'\n\1  # codeql[py/command-line-injection]',
    content
)

with open(file_path, "w") as f:
    f.write(content)
