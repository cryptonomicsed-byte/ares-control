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
