## 2025-02-24 - [Fix child process stdin inheritance causing hangs]
**Vulnerability:** Subprocesses (`subprocess.Popen` running `ffmpeg` and `ffprobe`) were inheriting the parent process's standard input.
**Learning:** By not specifying `stdin=subprocess.DEVNULL`, long-running child processes like ffmpeg can hang indefinitely waiting for input on `stdin`, creating a denial of service (DoS) vulnerability or breaking stream proxy execution completely.
**Prevention:** Always explicitly set `stdin=subprocess.DEVNULL` for `subprocess.Popen` in non-interactive tasks, particularly background stream transcoding/probing where `stdin` is not required.
