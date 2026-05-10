import os
import subprocess
import textwrap
from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parent
    / "extreme"
    / "scripts"
    / "install_nzbdav_addon.sh"
)


def _extract_shell_function(script: str, name: str) -> str:
    start = script.find(f"{name}() {{")
    assert start != -1, f"{name} function not found"
    lines = script[start:].splitlines()
    body = []
    depth = 0
    for line in lines:
        body.append(line)
        depth += line.count("{")
        depth -= line.count("}")
        if body and depth == 0:
            return "\n".join(body)
    raise AssertionError(f"{name} function was not closed")


def _run_version_check(payload: str) -> subprocess.CompletedProcess:
    function = _extract_shell_function(SCRIPT.read_text(), "kodi_version_supported")
    program = textwrap.dedent(
        f"""
        set -euo pipefail
        REQUIRED_KODI_MAJOR=21
        {function}
        kodi_version_supported "$1"
        """
    )
    env = os.environ.copy()
    env["PATH"] = "/usr/bin:/bin"
    return subprocess.run(
        ["bash", "-c", program, "bash", payload],
        check=False,
        env=env,
        capture_output=True,
        text=True,
    )


def test_install_script_accepts_future_kodi_major_without_jq_fallback():
    payload = '{"result":{"version":{"major":22,"minor":0}}}'

    result = _run_version_check(payload)

    assert result.returncode == 0, result.stderr


def test_install_script_rejects_too_old_kodi_major_without_jq_fallback():
    payload = '{"result":{"version":{"major":20,"minor":5}}}'

    result = _run_version_check(payload)

    assert result.returncode == 1
