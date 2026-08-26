# ares-control

Standalone control panel for the ares-* daemon fleet on hostinger-vps
(~70 systemd units: intel/scanning, Vantage bridges, and a handful of
real trade-execution daemons).

## Why this exists

On 2026-08-26 Vantage's own API became fully unresponsive (health
checks timing out at 20s-3min) because it shared CPU with ~40
always-on background daemons. Stopping the daemon fleet immediately
brought Vantage back (load 138.79 -> 78.56, health check 200 in
6.2s). The existing "Ares dashboards" (`ares-council-dashboard` on
:8870, `ares_dashboard.py` on :8879) are both explicitly read-only --
neither can start/stop anything.

This app is the missing piece: a genuinely separate, authenticated
control surface that can toggle daemons on/off, built so that **it can
never itself become the next thing that starves Vantage** or
anything else it's controlling.

## Architecture

Split into two processes, verified against the pattern in
[HKUDS/AI-Trader](https://github.com/HKUDS/AI-Trader)
(`service/server/main.py` + `service/server/worker.py`,
commit `a234743`, "Separate API service from background workers"):

- **`service/main.py`** -- FastAPI, HTTP-only. Never calls `systemctl`,
  never polls status itself. Just reads/writes the SQLite DB.
- **`service/worker.py`** -- separate process. `os.nice(10)`'d so it can
  never starve the API. Polls for pending toggle requests and actually
  runs `systemctl start/stop`. Also polls each unit's live status on a
  30s interval. File-lock singleton so a second worker instance can
  never double-process the same request.

Both share `service/daemons.py` -- one registry, one source of truth
for unit names and their category (`intel`, `bridge`, `dashboard`,
`backup`, `execution`). `execution` units (real trading daemons) can
never be toggled without the caller explicitly acknowledging it via
`approve_execution=true` on the request body.

## Run

```bash
pip install -r requirements.txt

# API (safe to run anywhere with network access to hostinger-vps's DB path,
# but the DB itself should live on hostinger-vps since worker.py needs
# root + systemctl access there)
export ARES_CONTROL_API_KEY="<set a real key>"
python service/main.py            # :8090

# Worker -- must run ON hostinger-vps, as root
python service/worker.py
```

## Endpoints

- `GET /health` -- no auth, for the API's own liveness check
- `GET /daemons` -- list all units + cached status
- `POST /daemons/{unit_name}/toggle` -- body `{action, requested_by, approve_execution}`
- `GET /daemons/toggle-requests/{id}` -- poll a request's outcome

All except `/health` require header `X-API-Key: <ARES_CONTROL_API_KEY>`.

## Deploy as systemd units on hostinger-vps

See `deploy/ares-control-api.service` and `deploy/ares-control-worker.service`.
