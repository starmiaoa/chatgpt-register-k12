"""Small command runner for the local-only WebUI terminal."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from chatgpt_register_sub2api.webui.redact import redact_text


def run_terminal_command(
    command: str,
    cwd: str | Path,
    timeout: int = 120,
) -> dict[str, Any]:
    text = str(command or "").strip()
    if not text:
        return {"ok": False, "error": "empty command", "output": ""}
    if len(text) > 4000:
        return {"ok": False, "error": "command is too long", "output": ""}

    workdir = Path(cwd).resolve()
    try:
        completed = subprocess.run(
            text,
            cwd=str(workdir),
            shell=True,
            capture_output=True,
            text=True,
            timeout=max(1, int(timeout)),
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired as error:
        output = (error.stdout or "") + (error.stderr or "")
        return {
            "ok": False,
            "returncode": None,
            "error": f"command timed out after {timeout}s",
            "output": redact_text(output),
            "cwd": str(workdir),
        }
    except Exception as error:
        return {
            "ok": False,
            "returncode": None,
            "error": str(error),
            "output": "",
            "cwd": str(workdir),
        }

    output = (completed.stdout or "") + (completed.stderr or "")
    return {
        "ok": completed.returncode == 0,
        "returncode": completed.returncode,
        "error": "" if completed.returncode == 0 else f"exit {completed.returncode}",
        "output": redact_text(output),
        "cwd": str(workdir),
    }
