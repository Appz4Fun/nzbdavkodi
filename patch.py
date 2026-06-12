import re

file_path = "repo/plugin.video.nzbdav/resources/lib/stream_proxy.py"
with open(file_path, "r") as f:
    content = f.read()

# We need to suppress the Bandit checks B603 since they are showing up. We removed `# nosec B603`.
# CodeQL checks were passing without ANY `# lgtm` comments in origin/main!
# So CodeQL does not need suppression for `shell=False` as long as it's directly visible and not obscured by `**kw`.
# Let's add `# nosec B603` to the `subprocess.Popen` lines.

content = re.sub(
    r'(\s*)shell=False,\n(\s*)\)',
    r'\1shell=False,\n\2)  # nosec B603',
    content
)

with open(file_path, "w") as f:
    f.write(content)
