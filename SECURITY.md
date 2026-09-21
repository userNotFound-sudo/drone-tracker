# Security Notes — JROTC Swarm Tactical Console

This document describes the threat model and defensive measures for the drone swarm tracker.
It is written for a school/training context: the application is intended to run on a single
local machine and is never deployed to the public internet.

---

## What the application exposes

| Component | Listens on | Purpose |
|---|---|---|
| FastAPI backend | `127.0.0.1:8001` | REST + WebSocket API |
| Vite dev server | `127.0.0.1:5173` | React frontend |
| SQLite database | local file only | track samples, zones, events |

Neither server is reachable from the network by default; both bind to the loopback
interface only.

---

## Threat model

### Who could attack it

| Actor | Access level | Realistic? |
|---|---|---|
| Another process on the same machine | Full localhost access | Possible |
| Another user account on the same machine | Depends on OS permissions | Unlikely in a training lab |
| Remote attacker via a malicious web page | Indirect (browser → localhost) | Limited by CORS |
| Remote attacker over the network | None (not exposed externally) | Not applicable |

### What they could attempt

| Goal | Method | Residual risk |
|---|---|---|
| Read or corrupt the database | Craft a malicious SQL query | **None** — all queries parameterised |
| Exhaust server memory | POST a zone with millions of coordinates | **Low** — Pydantic + length guard cap at 256 pts |
| Fill `_pending_events` buffer | Generate events faster than a WS client drains them | **Low** — hard cap of 1 000 entries |
| Access arbitrary files | Path-traversal via a parameter | **None** — all paths are hardcoded |
| Make cross-origin requests from a malicious site | Exploit CORS misconfiguration | **Low** — origin regex limited to localhost |
| Inject script into zone names | Store XSS payload in the zone name field | **None** — React escapes all rendered text |
| Request years of event data in one query | Abuse unbounded `start_ms`/`end_ms` parameters | **Low** — 10-minute window cap on all data endpoints |
| Exfiltrate secrets | Read credentials or API keys from source or history | **None** — no secrets are used or committed |

---

## Mitigations implemented

### Localhost binding
The backend is started with `host="127.0.0.1"` (see `backend/app/main.py`,
`if __name__ == "__main__"` block).  Running uvicorn directly should always
include `--host 127.0.0.1`; omitting it would default to `0.0.0.0` and expose
the API on all network interfaces.

### CORS allowlist
```python
allow_origin_regex = r"^http://(localhost|127\.0\.0\.1):\d+$"
allow_methods      = ["GET", "POST", "DELETE"]
allow_headers      = ["Content-Type", "Accept"]
```
Cross-origin requests are only permitted from localhost/127.0.0.1 on any port.
Allowed headers are restricted to what the frontend actually sends; custom headers
are not reflected back.

### HTTP parameter validation
Every endpoint validates its query parameters before touching the database:

- `start_ms < end_ms` required
- Time window capped at 10 minutes (`_MAX_REPLAY_MS`) on `/replay`, `/events`, and `/ws/replay`
- `limit` clamped to `[1, 20 000]`
- WebSocket `rate` clamped to `[0.1, 10]`; `hz` clamped to `[1, 30]`

### Parameterised SQL
All database queries use `?` placeholders via the `sqlite3` module.  No
f-strings or string concatenation are used to build SQL.  SQL injection is
therefore not possible regardless of input content.

### Filesystem path validation
All file paths (`VIDEO_PATH`, `DB_PATH`, `CONFIG_PATH`) are constructed at
import time using `pathlib.Path(__file__).parent / ...`.  No user-supplied value
is ever incorporated into a file path, so path traversal is structurally
impossible.

### Zone input validation (`_validate_zone`)
Zone POST requests are checked in this order before any data is written:

1. **Name** — ASCII and Unicode control/bidi characters stripped; truncated to
   128 chars; empty result rejected.
2. **Shape** — must be `"rectangle"` or `"polygon"` (allowlist).
3. **Coordinate count** — checked *before* the per-point loop so a request with
   100 000 coordinates is rejected immediately (rectangle: exactly 2; polygon:
   3–256).  Pydantic also enforces `max_length=256` at parse time.
4. **Coordinate values** — each `[x, y]` must be in `[0.0, 1.0]`.
5. **Orientation** — rectangle requires `x1 < x2` and `y1 < y2`.

### Altitude configuration validation
`load_config()` validates every field against physical bounds before the
estimator is constructed:

| Field | Allowed range |
|---|---|
| `camera_lat` | −90 to 90 |
| `camera_lon` | −180 to 180 |
| `camera_height_m` | 0 to 9 000 m |
| `v_fov_deg` | 10 to 170° |
| `landmark.elevation_m` | 0 to 9 000 m |
| `landmark.distance_m` | 1 to 500 000 m |

The file is opened with `encoding='utf-8'` to avoid platform-dependent decoding.

### No secrets committed
- No API keys, passwords, or tokens appear anywhere in the source tree.
- `.env` and `.env.local` are in `.gitignore`.
- `backend/data/*.json` (which may contain camera GPS coordinates) is gitignored.
- `*.pem` and `*.key` are gitignored.
- The `.venv/` directory and SQLite database files are gitignored.

---

## What this project does not protect against

This is a local training tool, not a hardened production system.  The following
are **known non-goals**:

- **Authentication / authorisation** — any process on localhost can call the API.
- **TLS** — traffic between the frontend and backend is plaintext HTTP/WS on
  loopback (acceptable for localhost-only use).
- **Rate limiting** — no per-IP request throttling is implemented.
- **Multi-client event delivery** — `_pending_events` is drained by the first
  connected WebSocket client; a second simultaneous client will not see those events.
- **Audit logging** — server actions are printed to stdout only.

If this project is ever adapted for network deployment, all of the above should
be addressed before going live.
