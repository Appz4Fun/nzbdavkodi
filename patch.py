import re

file_path = "repo/plugin.video.nzbdav/resources/lib/stream_proxy.py"
with open(file_path, "r") as f:
    content = f.read()

# Remove the old monkey patch
content = re.sub(
    r'\n# 🛡️ Sentinel: Monkey-patch Popen to prevent child processes from inheriting stdin\n_orig_popen = subprocess\.Popen\n\nclass _SafePopen\(_orig_popen\):\n    def __init__\(self, \*args, \*\*kwargs\):\n        kwargs\.setdefault\("stdin", subprocess\.DEVNULL\)\n        super\(_SafePopen, self\)\.__init__\(\*args, \*\*kwargs\)\n\nsubprocess\.Popen = _SafePopen\n\n',
    r'',
    content
)

# Put it after all imports.
# The last import is `from resources.lib.http_util import redact_text as _redact_text`
# Find that line and insert after it.
content = re.sub(
    r'(from resources\.lib\.http_util import redact_text as _redact_text\n)',
    r'\1\n# 🛡️ Sentinel: Monkey-patch Popen to prevent child processes from inheriting stdin\n_orig_popen = subprocess.Popen\n\nclass _SafePopen(_orig_popen):\n    def __init__(self, *args, **kwargs):\n        kwargs.setdefault("stdin", subprocess.DEVNULL)\n        super(_SafePopen, self).__init__(*args, **kwargs)\n\nsubprocess.Popen = _SafePopen\n',
    content
)

with open(file_path, "w") as f:
    f.write(content)
