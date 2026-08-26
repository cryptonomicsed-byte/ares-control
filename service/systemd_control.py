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


_HEALTH_PROPERTIES = [
    "ActiveState", "SubState", "MemoryCurrent", "TasksCurrent",
    "NRestarts", "CPUUsageNSec", "MainPID", "ActiveEnterTimestamp",
]


def query_health(unit_name: str) -> dict:
    """Deeper per-unit snapshot for the dashboard: memory/tasks/restarts/CPU."""
    try:
        result = subprocess.run(
            ["systemctl", "show", unit_name, f"--property={','.join(_HEALTH_PROPERTIES)}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        out = {}
        for line in result.stdout.strip().split("\n"):
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            out[key] = value
        mem = out.get("MemoryCurrent", "[not set]")
        cpu_ns = out.get("CPUUsageNSec", "[not set]")
        return {
            "active_state": out.get("ActiveState", "unknown"),
            "sub_state": out.get("SubState", "unknown"),
            "memory_bytes": int(mem) if mem.isdigit() else None,
            "tasks_current": int(out["TasksCurrent"]) if out.get("TasksCurrent", "").isdigit() else None,
            "n_restarts": int(out["NRestarts"]) if out.get("NRestarts", "").isdigit() else None,
            "cpu_usage_ns": int(cpu_ns) if cpu_ns.isdigit() else None,
            "main_pid": out.get("MainPID"),
            "active_enter_timestamp": out.get("ActiveEnterTimestamp") or None,
        }
    except Exception as exc:  # noqa: BLE001
        return {"active_state": "unknown", "sub_state": "unknown", "error": str(exc)}
