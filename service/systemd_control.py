"""
Thin wrapper around systemctl. Only ever called from worker.py (root).

As of the 2026-08-27 migration, worker.py itself runs on Contabo, but most
of the fleet still runs on hostinger-vps -- so most calls here go out over
SSH rather than running systemctl in-process. daemons.host_for(unit_name)
decides which; "local" runs systemctl directly, anything else is an SSH
host alias (see ~/.ssh/config -- must already have a working, non-
interactive (key-based) entry).
"""

import subprocess

from daemons import all_units, host_for

SSH_KEY = "/root/.ssh/ares_control_remote"
SSH_TIMEOUT_PADDING = 10  # extra seconds of slack on top of the systemctl call's own timeout, for SSH connection setup itself


def _run(unit_name: str, argv: list[str], timeout: int) -> subprocess.CompletedProcess:
    """Run a systemctl argv either locally or over SSH, depending on where
    this unit actually lives. The remote command is quoted as a single
    string for the SSH-side shell, not passed as argv, since SSH always
    hands the remote end one string regardless of how it's invoked here."""
    host = host_for(unit_name)
    if host == "local":
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    remote_cmd = " ".join(argv)
    return subprocess.run(
        ["ssh", "-i", SSH_KEY, "-o", "BatchMode=yes", "-o", f"ConnectTimeout={SSH_TIMEOUT_PADDING}",
         host, remote_cmd],
        capture_output=True, text=True, timeout=timeout + SSH_TIMEOUT_PADDING,
    )


def run_action(unit_name: str, action: str) -> tuple[bool, str]:
    if unit_name not in all_units():
        return False, f"unknown unit: {unit_name}"
    if action not in ("start", "stop"):
        return False, f"invalid action: {action}"
    try:
        result = _run(unit_name, ["systemctl", action, unit_name], timeout=30)
        if result.returncode != 0:
            return False, result.stderr.strip() or f"systemctl {action} exited {result.returncode}"
        return True, ""
    except subprocess.TimeoutExpired:
        return False, "systemctl call timed out (including SSH round trip for remote units)"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def query_status(unit_name: str) -> tuple[str, str]:
    """Returns (active_state, sub_state), e.g. ('active', 'running')."""
    try:
        result = _run(
            unit_name,
            ["systemctl", "show", unit_name, "--property=ActiveState,SubState", "--value"],
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
        result = _run(
            unit_name,
            ["systemctl", "show", unit_name, f"--property={','.join(_HEALTH_PROPERTIES)}"],
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
