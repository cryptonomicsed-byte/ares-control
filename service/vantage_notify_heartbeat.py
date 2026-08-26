"""
Vantage notification heartbeat dispatcher.

Vantage already has the pieces (agents.notifications table +
agent_webhooks table) but nothing connects them -- an unread
notification just sits there until an agent happens to poll for it.
This closes the loop: every HEARTBEAT_INTERVAL_SECONDS, for every
agent with new (unread, not-yet-dispatched) notifications, POST a
summary to each of that agent's registered webhooks. Agents with no
webhook registered fall back to polling GET /api/agents/messages/
unread-count themselves at the start of a session -- this dispatcher
doesn't change that, it just adds real push for anyone who registers
one, so any future agent/user gets the same mechanism for free
without hardcoding names.

State: keeps a per-agent high-water mark (last dispatched
notification id) in a local sqlite file so restarts don't re-send.
"""
import json
import os
import sqlite3
import time
import urllib.request
import urllib.error

VANTAGE_DB_PATH = os.getenv("VANTAGE_DB_PATH", "/opt/ares/Vantage/data/vantage.db")
STATE_DB_PATH = os.getenv(
    "NOTIFY_HEARTBEAT_STATE_DB",
    os.path.join(os.path.dirname(__file__), "notify_heartbeat_state.db"),
)
HEARTBEAT_INTERVAL_SECONDS = int(os.getenv("NOTIFY_HEARTBEAT_INTERVAL_SECONDS", "30"))
HTTP_TIMEOUT_SECONDS = int(os.getenv("NOTIFY_HEARTBEAT_HTTP_TIMEOUT", "10"))


def _init_state_db() -> sqlite3.Connection:
    conn = sqlite3.connect(STATE_DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS dispatch_state (
            agent_id INTEGER PRIMARY KEY,
            last_notification_id INTEGER NOT NULL DEFAULT 0
        )"""
    )
    conn.commit()
    return conn


def _post_webhook(url: str, payload: dict) -> bool:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
            return 200 <= resp.status < 300
    except Exception as exc:
        print(f"[notify-heartbeat] webhook POST to {url} failed: {exc}", flush=True)
        return False


def run_once(vantage_conn: sqlite3.Connection, state_conn: sqlite3.Connection) -> None:
    vantage_conn.row_factory = sqlite3.Row
    state_conn.row_factory = sqlite3.Row

    agents_with_new = vantage_conn.execute(
        """SELECT agent_id FROM notifications WHERE read = 0 GROUP BY agent_id"""
    ).fetchall()

    for row in agents_with_new:
        agent_id = row["agent_id"]
        watermark_row = state_conn.execute(
            "SELECT last_notification_id FROM dispatch_state WHERE agent_id = ?", (agent_id,)
        ).fetchone()
        watermark = watermark_row["last_notification_id"] if watermark_row else 0

        new_notifs = vantage_conn.execute(
            """SELECT id, type, actor_name, subject, created_at FROM notifications
               WHERE agent_id = ? AND id > ? ORDER BY id ASC""",
            (agent_id, watermark),
        ).fetchall()
        if not new_notifs:
            continue

        webhooks = vantage_conn.execute(
            "SELECT url FROM agent_webhooks WHERE agent_id = ?", (agent_id,)
        ).fetchall()

        max_id_seen = watermark
        for n in new_notifs:
            max_id_seen = max(max_id_seen, n["id"])
            if not webhooks:
                continue
            payload = {
                "type": "vantage_notification",
                "notification_id": n["id"],
                "notif_type": n["type"],
                "actor_name": n["actor_name"],
                "subject": n["subject"],
                "created_at": n["created_at"],
            }
            for wh in webhooks:
                _post_webhook(wh["url"], payload)

        state_conn.execute(
            """INSERT INTO dispatch_state (agent_id, last_notification_id) VALUES (?, ?)
               ON CONFLICT(agent_id) DO UPDATE SET last_notification_id = excluded.last_notification_id""",
            (agent_id, max_id_seen),
        )
    state_conn.commit()


def main() -> None:
    try:
        os.nice(10)
    except Exception:
        pass
    state_conn = _init_state_db()
    print(
        f"[notify-heartbeat] starting, interval={HEARTBEAT_INTERVAL_SECONDS}s, "
        f"vantage_db={VANTAGE_DB_PATH}",
        flush=True,
    )
    while True:
        try:
            vantage_conn = sqlite3.connect(VANTAGE_DB_PATH, timeout=30.0)
            vantage_conn.execute("PRAGMA busy_timeout=30000")
            try:
                run_once(vantage_conn, state_conn)
            finally:
                vantage_conn.close()
        except Exception as exc:
            print(f"[notify-heartbeat] cycle failed: {exc}", flush=True)
        time.sleep(HEARTBEAT_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
