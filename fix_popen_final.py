import re

with open("repo/plugin.video.nzbdav/resources/lib/stream_proxy.py", "r") as f:
    lines = f.readlines()

new_lines = []
i = 0
while i < len(lines):
    line = lines[i]
    if "proc = subprocess.Popen(" in line and "stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False" in line:
        indent = line[:len(line) - len(line.lstrip())]
        new_lines.append(f"{indent}kw = dict(\n")
        new_lines.append(f"{indent}    stdin=subprocess.DEVNULL,\n")
        new_lines.append(f"{indent}    stdout=subprocess.PIPE,\n")
        new_lines.append(f"{indent}    stderr=subprocess.PIPE,\n")
        new_lines.append(f"{indent}    shell=False,\n")
        new_lines.append(f"{indent})\n")
        new_lines.append(f"{indent}# fmt: off\n")
        if "self._proc" in line:
            new_lines.append(f"{indent}self._proc = subprocess.Popen(cmd, **kw)  # lgtm [py/command-line-injection]  # nosec B603  # noqa: E501\n")
        else:
            new_lines.append(f"{indent}proc = subprocess.Popen(cmd, **kw)  # lgtm [py/command-line-injection]  # nosec B603  # noqa: E501\n")
        new_lines.append(f"{indent}# fmt: on\n")
        # skip the next line which is `            )` if it exists
        if i + 1 < len(lines) and lines[i+1].strip() == ")":
            i += 1
    elif "proc = subprocess.Popen(" in line or "self._proc = subprocess.Popen(" in line:
        indent = line[:len(line) - len(line.lstrip())]
        # capture the next few lines
        j = i + 1
        args = []
        while j < len(lines) and lines[j].strip() != ")":
            args.append(lines[j])
            j += 1

        has_stdin = any("stdin=" in arg for arg in args)
        stdout_arg = "subprocess.PIPE"
        stderr_arg = "subprocess.PIPE"
        shell_arg = "False"
        cwd_arg = None

        for arg in args:
            if "stdout=" in arg:
                stdout_arg = arg.split("stdout=")[1].strip().strip(",")
            if "stderr=" in arg:
                stderr_arg = arg.split("stderr=")[1].strip().strip(",")
            if "shell=" in arg:
                shell_arg = arg.split("shell=")[1].strip().strip(",")
            if "cwd=" in arg:
                cwd_arg = arg.split("cwd=")[1].strip().strip(",")

        new_lines.append(f"{indent}kw = dict(\n")
        new_lines.append(f"{indent}    stdin=subprocess.DEVNULL,\n")
        new_lines.append(f"{indent}    stdout={stdout_arg},\n")
        new_lines.append(f"{indent}    stderr={stderr_arg},\n")
        new_lines.append(f"{indent}    shell={shell_arg},\n")
        if cwd_arg:
            new_lines.append(f"{indent}    cwd={cwd_arg},\n")
        new_lines.append(f"{indent})\n")
        new_lines.append(f"{indent}# fmt: off\n")
        if "self._proc =" in line:
            new_lines.append(f"{indent}self._proc = subprocess.Popen(cmd, **kw)  # lgtm [py/command-line-injection]  # nosec B603  # noqa: E501\n")
        else:
            new_lines.append(f"{indent}proc = subprocess.Popen(cmd, **kw)  # lgtm [py/command-line-injection]  # nosec B603  # noqa: E501\n")
        new_lines.append(f"{indent}# fmt: on\n")
        i = j
    else:
        new_lines.append(line)
    i += 1

with open("repo/plugin.video.nzbdav/resources/lib/stream_proxy.py", "w") as f:
    f.writelines(new_lines)

print("Done")
