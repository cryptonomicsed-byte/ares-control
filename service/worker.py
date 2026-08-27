"""
Standalone worker for ares-control.

Runs separately from the FastAPI API process so that HTTP requests
(viewing daemon status, submitting a toggle request) never compete for
CPU with the actual systemctl calls or the periodic status poll.

Pattern verified directly against HKUDS/AI-Trader's service/server/worker.py:
- os.nice() to deliberately deprioritize this process at the OS
  scheduler level, so it can never starve the API's /health endpoint
  the way the ares-* daemon fleet starved Vantage's on 2026-08-26.
- A file-lock singleton guard so a second worker instance can never
  double-process the same toggle request. (AI-Trader prefers a Redis
  lock with this as the fallback; we skip the Redis dependency
  entirely at this scale -- one worker process is enough for ~70 units.)
"""

import asyncio
import fcntl
import logging
import os
import signal
import sys
import time
from contextlib import suppress

import urllib.error
import urllib.request

from daemons import DAEMON_REGISTRY, requires_approval
from database import get_db_connection, init_database
from systemd_control import query_health, run_action

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

STATUS_POLL_INTERVAL_SECONDS = int(os.getenv("ARES_CONTROL_STATUS_POLL_SECONDS", "30"))
TOGGLE_POLL_INTERVAL_SECONDS = int(os.getenv("ARES_CONTROL_TOGGLE_POLL_SECONDS", "3"))
HEALTH_ENDPOINT_POLL_INTERVAL_SECONDS = int(os.getenv("ARES_CONTROL_HEALTH_ENDPOINT_POLL_SECONDS", "30"))

# Services that expose their own HTTP health/liveness endpoint -- polled
# separately from systemd unit state, since "active/running" doesn't
# prove the app inside is actually responding (see: the connection-pool
# incident, where vantage.service stayed "active" for minutes while
# genuinely unresponsive).
HEALTH_ENDPOINTS: dict[str, str] = {
    "vantage": "http://localhost:8001/api/health",
    "frankenstream": "http://localhost:3034/api/health",
    "poolhealth": "http://localhost:8004/",
    "wigolo": "http://10.88.0.1:3334/",
    "opencode": "http://localhost:18888/",
}


def _acquire_file_lock():
    lock_path = os.getenv("ARES_CONTROL_WORKER_LOCK_FILE", "/tmp/ares-control-worker.lock")
    handle = open(lock_path, "w", encoding="utf-8")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        logger.warning("Another ares-control worker is already running; lock_file=%s", lock_path)
        return None
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def _release_file_lock(handle) -> None:
    if handle is None:
        return
    with suppress(Exception):
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


async def process_pending_toggles(stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        conn = get_db_connection()
        try:
            rows = conn.execute(
                "SELECT * FROM toggle_requests WHERE status = 'pending' ORDER BY id"
            ).fetchall()
            for row in rows:
                unit_name = row["unit_name"]
                action = row["action"]
                if unit_name not in DAEMON_REGISTRY:
                    conn.execute(
                        "UPDATE toggle_requests SET status='failed', error=?, processed_at=datetime('now') WHERE id=?",
                        (f"unknown unit: {unit_name}", row["id"]),
                    )
                    conn.commit()
                    continue
                if requires_approval(unit_name) and not row["approved"]:
                    conn.execute(
                        "UPDATE toggle_requests SET status='failed', error=?, processed_at=datetime('now') WHERE id=?",
                        ("EXECUTION-category unit requires explicit approval", row["id"]),
                    )
                    conn.commit()
                    continue

                conn.execute("UPDATE toggle_requests SET status='processing' WHERE id=?", (row["id"],))
                conn.commit()

                # Off the event loop: run_action can now be a multi-second
                # SSH round trip (remote units), and process_pending_toggles
                # shares this loop with poll_daemon_status via asyncio.gather
                # -- a blocking call here would freeze both, starving new
                # toggle requests behind whatever the status poll happens to
                # be doing (observed directly during the 2026-08-27 migration:
                # a toggle sat 'pending' for tens of seconds behind an
                # in-flight status query for an unrelated unit).
                ok, error = await asyncio.to_thread(run_action, unit_name, action)
                logger.info("toggle %s %s -> ok=%s error=%s", action, unit_name, ok, error)
                conn.execute(
                    "UPDATE toggle_requests SET status=?, error=?, processed_at=datetime('now') WHERE id=?",
                    ("done" if ok else "failed", error or None, row["id"]),
                )
                conn.commit()
        finally:
            conn.close()
        await asyncio.sleep(TOGGLE_POLL_INTERVAL_SECONDS)


async def poll_daemon_status(stop_event: asyncio.Event) -> None:
    # One commit per unit, not one for the whole pass. As of the
    # 2026-08-27 multi-host migration, most units are queried over SSH
    # (seconds each, not milliseconds), so a single end-of-loop commit
    # held a write-lock open for minutes at a time -- the exact same
    # antipattern that caused the wallet_learner.py WAL-lock incident
    # earlier the same night. The API's own toggle-request INSERT was
    # getting starved out with "database is locked" as a result.
    while not stop_event.is_set():
        for unit_name in DAEMON_REGISTRY:
            h = await asyncio.to_thread(query_health, unit_name)
            conn = get_db_connection()
            try:
                conn.execute(
                    """
                    INSERT INTO daemon_status_cache
                        (unit_name, active_state, sub_state, memory_bytes, tasks_current,
                         n_restarts, cpu_usage_ns, main_pid, active_enter_timestamp, checked_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                    ON CONFLICT(unit_name) DO UPDATE SET
                        active_state=excluded.active_state,
                        sub_state=excluded.sub_state,
                        memory_bytes=excluded.memory_bytes,
                        tasks_current=excluded.tasks_current,
                        n_restarts=excluded.n_restarts,
                        cpu_usage_ns=excluded.cpu_usage_ns,
                        main_pid=excluded.main_pid,
                        active_enter_timestamp=excluded.active_enter_timestamp,
                        checked_at=excluded.checked_at
                    """,
                    (
                        unit_name, h.get("active_state", "unknown"), h.get("sub_state", "unknown"),
                        h.get("memory_bytes"), h.get("tasks_current"), h.get("n_restarts"),
                        h.get("cpu_usage_ns"), h.get("main_pid"), h.get("active_enter_timestamp"),
                    ),
                )
                conn.commit()
            finally:
                conn.close()
        await asyncio.sleep(STATUS_POLL_INTERVAL_SECONDS)


def _check_health_endpoint(url: str, timeout: float = 5.0) -> dict:
    start = time.monotonic()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            elapsed_ms = (time.monotonic() - start) * 1000
            return {"http_code": resp.status, "response_time_ms": round(elapsed_ms, 1), "ok": True, "error": None}
    except urllib.error.HTTPError as exc:
        elapsed_ms = (time.monotonic() - start) * 1000
        # A non-2xx response still proves the process is alive and listening.
        return {"http_code": exc.code, "response_time_ms": round(elapsed_ms, 1), "ok": True, "error": None}
    except Exception as exc:  # noqa: BLE001
        elapsed_ms = (time.monotonic() - start) * 1000
        return {"http_code": None, "response_time_ms": round(elapsed_ms, 1), "ok": False, "error": str(exc)}


async def poll_health_endpoints(stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        conn = get_db_connection()
        try:
            for name, url in HEALTH_ENDPOINTS.items():
                result = _check_health_endpoint(url)
                conn.execute(
                    """
                    INSERT INTO health_endpoint_cache
                        (name, url, http_code, response_time_ms, ok, error, checked_at)
                    VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
                    ON CONFLICT(name) DO UPDATE SET
                        url=excluded.url, http_code=excluded.http_code,
                        response_time_ms=excluded.response_time_ms,
                        ok=excluded.ok, error=excluded.error, checked_at=excluded.checked_at
                    """,
                    (name, url, result["http_code"], result["response_time_ms"], int(result["ok"]), result["error"]),
                )
            conn.commit()
        finally:
            conn.close()
        await asyncio.sleep(HEALTH_ENDPOINT_POLL_INTERVAL_SECONDS)


async def main() -> None:
    try:
        os.nice(int(os.getenv("ARES_CONTROL_WORKER_NICE", "10")))
    except Exception:  # noqa: BLE001
        pass

    lock_handle = _acquire_file_lock()
    if lock_handle is None:
        return

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signame in ("SIGINT", "SIGTERM"):
        with suppress(Exception):
            loop.add_signal_handler(getattr(signal, signame), stop_event.set)

    try:
        init_database()
        logger.info("ares-control worker started, nice=%s, %d units tracked", os.nice(0), len(DAEMON_REGISTRY))
        await asyncio.gather(
            process_pending_toggles(stop_event),
            poll_daemon_status(stop_event),
            poll_health_endpoints(stop_event),
        )
    finally:
        _release_file_lock(lock_handle)


if __name__ == "__main__":
    asyncio.run(main())
