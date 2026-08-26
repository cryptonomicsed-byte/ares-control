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

from daemons import DAEMON_REGISTRY, requires_approval
from database import get_db_connection, init_database
from systemd_control import query_status, run_action

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

STATUS_POLL_INTERVAL_SECONDS = int(os.getenv("ARES_CONTROL_STATUS_POLL_SECONDS", "30"))
TOGGLE_POLL_INTERVAL_SECONDS = int(os.getenv("ARES_CONTROL_TOGGLE_POLL_SECONDS", "3"))


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

                ok, error = run_action(unit_name, action)
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
    while not stop_event.is_set():
        conn = get_db_connection()
        try:
            for unit_name in DAEMON_REGISTRY:
                active_state, sub_state = query_status(unit_name)
                conn.execute(
                    """
                    INSERT INTO daemon_status_cache (unit_name, active_state, sub_state, checked_at)
                    VALUES (?, ?, ?, datetime('now'))
                    ON CONFLICT(unit_name) DO UPDATE SET
                        active_state=excluded.active_state,
                        sub_state=excluded.sub_state,
                        checked_at=excluded.checked_at
                    """,
                    (unit_name, active_state, sub_state),
                )
            conn.commit()
        finally:
            conn.close()
        await asyncio.sleep(STATUS_POLL_INTERVAL_SECONDS)


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
        )
    finally:
        _release_file_lock(lock_handle)


if __name__ == "__main__":
    asyncio.run(main())
