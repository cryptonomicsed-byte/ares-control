"""
ares-control API server.

HTTP-only by default -- this process never runs systemctl and never
polls daemon status itself. All of that happens in worker.py, a
separate, niced, singleton-locked process. This mirrors the split
verified in HKUDS/AI-Trader's service/server/main.py +
service/server/worker.py: the exact fix for the failure mode we hit on
2026-08-26, where Vantage's API became unresponsive because it shared
CPU with ~40 background daemon processes.

Run:
    python service/main.py            # API only, port 8090
    python service/worker.py          # separately, does the real work
"""

import os
import secrets
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Header
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from daemons import DAEMON_REGISTRY, requires_approval
from database import get_db_connection, init_database

API_AUTH_KEY = os.getenv("ARES_CONTROL_API_KEY")

app = FastAPI(title="ares-control", description="Toggle/monitor the ares-* daemon fleet")


def require_auth(x_api_key: Optional[str] = Header(default=None)) -> None:
    if not API_AUTH_KEY:
        # No key configured -- only acceptable for local-only deployments.
        return
    if not x_api_key or not secrets.compare_digest(x_api_key, API_AUTH_KEY):
        raise HTTPException(status_code=401, detail="invalid or missing X-API-Key")


@app.on_event("startup")
def startup() -> None:
    init_database()


@app.get("/health")
def health():
    return {"status": "ok", "units_tracked": len(DAEMON_REGISTRY)}


@app.get("/daemons", dependencies=[Depends(require_auth)])
def list_daemons():
    conn = get_db_connection()
    try:
        rows = conn.execute("SELECT * FROM daemon_status_cache").fetchall()
        status_by_name = {row["unit_name"]: dict(row) for row in rows}
    finally:
        conn.close()

    return [
        {
            "unit_name": name,
            "category": category.value,
            "requires_approval": requires_approval(name),
            "status": status_by_name.get(name),
        }
        for name, category in DAEMON_REGISTRY.items()
    ]


@app.get("/health-endpoints", dependencies=[Depends(require_auth)])
def list_health_endpoints():
    conn = get_db_connection()
    try:
        rows = conn.execute("SELECT * FROM health_endpoint_cache").fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(x_api_key: Optional[str] = None):
    # Dashboard is a plain HTML page (browser tab, no custom header support),
    # so auth is a query param here instead of X-API-Key -- same key, same
    # no-op-if-unconfigured behavior as require_auth.
    if API_AUTH_KEY and (not x_api_key or not secrets.compare_digest(x_api_key, API_AUTH_KEY)):
        raise HTTPException(status_code=401, detail="?key=<ARES_CONTROL_API_KEY> required")

    conn = get_db_connection()
    try:
        daemon_rows = {r["unit_name"]: dict(r) for r in conn.execute("SELECT * FROM daemon_status_cache")}
        endpoint_rows = list(conn.execute("SELECT * FROM health_endpoint_cache"))
    finally:
        conn.close()

    def fmt_bytes(n):
        if n is None:
            return "-"
        for unit in ("B", "KB", "MB", "GB"):
            if n < 1024:
                return f"{n:.0f}{unit}"
            n /= 1024
        return f"{n:.1f}TB"

    def fmt_cpu(ns):
        if ns is None:
            return "-"
        return f"{ns / 1e9:.1f}s"

    endpoint_html = "".join(
        f"<tr class='{'ok' if r['ok'] else 'bad'}'>"
        f"<td>{r['name']}</td><td>{r['url']}</td><td>{r['http_code'] or '-'}</td>"
        f"<td>{r['response_time_ms']}ms</td><td>{r['error'] or ''}</td></tr>"
        for r in endpoint_rows
    )

    daemon_html_rows = []
    for name, category in sorted(DAEMON_REGISTRY.items(), key=lambda kv: (kv[1].value, kv[0])):
        st = daemon_rows.get(name, {})
        state = st.get("active_state", "unknown")
        css = "ok" if state == "active" else ("bad" if state in ("failed", "unknown") else "warn")
        restarts = st.get("n_restarts")
        restart_flag = " ⚠" if isinstance(restarts, int) and restarts > 20 else ""
        daemon_html_rows.append(
            f"<tr class='{css}'>"
            f"<td>{name}{' 🔒' if requires_approval(name) else ''}</td>"
            f"<td>{category.value}</td>"
            f"<td>{state}/{st.get('sub_state','?')}</td>"
            f"<td>{fmt_bytes(st.get('memory_bytes'))}</td>"
            f"<td>{st.get('tasks_current') or '-'}</td>"
            f"<td>{fmt_cpu(st.get('cpu_usage_ns'))}</td>"
            f"<td>{restarts if restarts is not None else '-'}{restart_flag}</td>"
            f"<td>{st.get('checked_at','-')}</td>"
            f"</tr>"
        )

    active_count = sum(1 for r in daemon_rows.values() if r.get("active_state") == "active")
    html = f"""<!doctype html><html><head><title>ares-control dashboard</title>
    <meta http-equiv="refresh" content="30">
    <style>
    body {{ font-family: -apple-system, sans-serif; background:#0d1117; color:#c9d1d9; padding:1.5rem; }}
    h1 {{ font-size:1.2rem; }} h2 {{ font-size:1rem; margin-top:2rem; }}
    table {{ border-collapse:collapse; width:100%; font-size:0.85rem; }}
    td, th {{ border-bottom:1px solid #30363d; padding:4px 8px; text-align:left; }}
    tr.ok td:nth-child(3) {{ color:#3fb950; }}
    tr.bad td:nth-child(3) {{ color:#f85149; }}
    tr.warn td:nth-child(3) {{ color:#d29922; }}
    .summary {{ color:#8b949e; margin-bottom:1rem; }}
    </style></head><body>
    <h1>ares-control — daemon fleet dashboard</h1>
    <div class="summary">{active_count}/{len(DAEMON_REGISTRY)} active · auto-refreshes every 30s · 🔒 = EXECUTION (gated)</div>
    <h2>Health endpoints</h2>
    <table><tr><th>service</th><th>url</th><th>code</th><th>latency</th><th>error</th></tr>{endpoint_html}</table>
    <h2>Daemons</h2>
    <table><tr><th>unit</th><th>category</th><th>state</th><th>mem</th><th>tasks</th><th>cpu</th><th>restarts</th><th>checked</th></tr>
    {''.join(daemon_html_rows)}</table>
    </body></html>"""
    return HTMLResponse(content=html)


class ToggleRequest(BaseModel):
    action: str  # "start" | "stop"
    requested_by: str
    approve_execution: bool = False


@app.post("/daemons/{unit_name}/toggle", dependencies=[Depends(require_auth)])
def toggle_daemon(unit_name: str, body: ToggleRequest):
    if unit_name not in DAEMON_REGISTRY:
        raise HTTPException(status_code=404, detail=f"unknown unit: {unit_name}")
    if body.action not in ("start", "stop"):
        raise HTTPException(status_code=422, detail="action must be 'start' or 'stop'")
    if requires_approval(unit_name) and not body.approve_execution:
        raise HTTPException(
            status_code=403,
            detail=(
                f"{unit_name} is an EXECUTION-category unit (places real orders/"
                "signs transactions). Re-submit with approve_execution=true to confirm."
            ),
        )

    conn = get_db_connection()
    try:
        cursor = conn.execute(
            "INSERT INTO toggle_requests (unit_name, action, requested_by, approved) VALUES (?, ?, ?, ?)",
            (unit_name, body.action, body.requested_by, int(body.approve_execution)),
        )
        conn.commit()
        request_id = cursor.lastrowid
    finally:
        conn.close()

    return {"request_id": request_id, "status": "pending", "unit_name": unit_name, "action": body.action}


@app.get("/daemons/toggle-requests/{request_id}", dependencies=[Depends(require_auth)])
def get_toggle_request(request_id: int):
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT * FROM toggle_requests WHERE id = ?", (request_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail="request not found")
    return dict(row)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("ARES_CONTROL_PORT", "8090")))
