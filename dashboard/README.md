# Monitoring and control dashboard

A separate application (FastAPI + React + Recharts + WebSockets) that monitors and controls the
autonomous development controller. It reads the controller's SQLite database, which remains the
single source of truth.

## Run

```powershell
cd dashboard
..\.venv\Scripts\python.exe -m backend.manage set-password     # once; writes a bcrypt hash to dashboard/.env
cd frontend; npm install; npm run build; cd ..                 # once (or after frontend changes)
..\.venv\Scripts\python.exe -m backend.app                     # http://127.0.0.1:8000
```

For frontend development, run `npm run dev` in `frontend/` (port 5173, which proxies to 8000).

Settings (environment variables or `dashboard/.env`):

| Variable | Default |
|---|---|
| `DASHBOARD_HOST` / `DASHBOARD_PORT` | `127.0.0.1` / `8000` (non-local hosts are refused unless `DASHBOARD_ALLOW_REMOTE=true`) |
| `DASHBOARD_DB_PATH` | `../autonomous-dev/database/agent_state.db` |
| `DASHBOARD_USERNAME` / `DASHBOARD_PASSWORD_HASH` | `admin` / set with `backend.manage set-password` |
| `DASHBOARD_ALLOWED_PROJECT_ROOTS` | the folder containing this repository (paths allowed for new/open project) |
| `DASHBOARD_SECURE_COOKIES` | `false` (set `true` behind HTTPS) |

## Modules

Overview, Agent Monitor, Live Console, Task Manager, Testing Center, Security Center, Git
Management, Deployment Monitor, Usage & Cost, and a Control Panel (start, pause, resume, stop,
new/open project, retry or cancel tasks, approvals, final report).

## How data flows

* The orchestrator writes structured events (`events` table) for agent lifecycle, SDK tool
  use, test, security, deployment and task transitions.
* `/ws/events` streams new rows. The browser reconnects with exponential backoff and sends its
  last event id, and the server replays everything after it, so no history is lost. Invalid
  WebSocket messages are rejected, and cross-origin connections are refused.
* Agents are shown ACTIVE only while their run is `RUNNING` in a live controller (lock
  heartbeat). Progress is the number of completed mandatory requirements, not an invented
  percentage.

## Security

Every page and the WebSocket require login: bcrypt-hashed password, HttpOnly SameSite=Strict
session cookie, login rate limiting. State-changing requests need the CSRF header. Control actions
call the controller CLI with a fixed argument vector. There is no shell access, and no Git write or
destructive action of any kind is available. Artifact downloads are limited to the controller's
`reports/` folder and safe file types. Do not expose the dashboard publicly without TLS, a reverse
proxy and firewalling.

## Tests

```bash
cd dashboard
..\.venv\Scripts\python.exe -m pytest      # runs a real orchestrator to produce the events it checks
```
