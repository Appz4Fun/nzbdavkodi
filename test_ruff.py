import subprocess
cmd = ["very_long_command", "with_lots_of_args", "that_makes_this_line_very_long", "and_exceeds_88_chars"]
kw = {}
proc = subprocess.Popen(cmd, **kw)  # lgtm [py/command-line-injection]  # nosec B603  # noqa: E501
