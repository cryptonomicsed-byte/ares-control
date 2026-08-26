"""
Thin wrapper around systemctl. Only ever called from worker.py (root,
on hostinger-vps) -- never from the API process.
"""

import subprocess

from daemons import all_units


def run_action(unit_name: str, action: str) -> tuple[bool, str]:
    if unit_name not in all_units():
        return False, f"unknown unit: {unit_name}"
    if action not in ("start", "stop"):
        return False, f"invalid action: {action}"
    try:
        result = subprocess.run(
            ["systemctl", action, unit_name],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return False, result.stderr.strip() or f"systemctl {action} exited {result.returncode}"
        return True, ""
    except subprocess.TimeoutExpired:
        return False, "systemctl call timed out after 30s"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def query_status(unit_name: str) -> tuple[str, str]:
    """Returns (active_state, sub_state), e.g. ('active', 'running')."""
    try:
        result = subprocess.run(
            ["systemctl", "show", unit_name, "--property=ActiveState,SubState", "--value"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        lines = result.stdout.strip().split("\n")
        if len(lines) >= 2:
            return lines[0].strip(), lines[1].strip()
        return "unknown", "unknown"
    except Exception:  # noqa: BLE001
        return "unknown", "unknown"
