# Integrating the UI with the Python backend

The Node server in this repo (`server.js`) can run in front of the Python backend (`backend/`). Set `BACKEND_URL` and it
becomes a thin backend-for-frontend: the browser still talks to the same `/api/*` it always has, and each route is
mapped to the real backend call where one exists. Without `BACKEND_URL` everything runs standalone (demo mode), exactly
as before.

```
browser ──/api/*, /v1/*──▶ Node (server.js) ──/user/*, /admin/*, /v1/*──▶ FastAPI ──▶ Postgres
                              │                                              ▲
                              └── monitoring pipeline (local)    crawl worker ─┘
```

Why a BFF and not calling the backend from the browser: the backend uses bearer tokens; here the token lives in an
`HttpOnly; SameSite=Lax` cookie (`rw_session`) so page JavaScript never sees it. The design doc
(`docs/superpowers/specs/2026-10-09-ui-alignment.md`) plans for the backend to serve `/api/*` itself; when it does, the
browser can talk to it directly with no UI change, because the UI only knows `/api/*`.

## Run it

```bash
# 1. backend: Postgres, migrations, API (:8000) and the crawl worker (see backend/docker-compose.yml)
cd backend && cp .env.example .env        # set JWT_SECRET (32+ chars)
docker compose up -d --build

# 2. an admin (admins are created in the database only; compose publishes Postgres on :5433)
python backend/scripts/create_admin.py boss@example.com 'a-long-password' | psql postgresql://postgres:postgres@localhost:5433/competitor

# 3. the UI
BACKEND_URL=http://localhost:8000 npm start        # http://localhost:3000
```

> I verified the integration by running the same pieces directly (Postgres 16, `alembic upgrade head`, `uvicorn`, and
> `python -m competitor_moves.workers.worker`), not through `docker compose`. The backend's own 88 tests pass in that setup.

| Env | Meaning |
|---|---|
| `BACKEND_URL` | Turns the integration on. Unset = standalone demo. |
| `DEMO_COMPETITORS` | `1`/`0`. Whether new workspaces start with the three editable demo competitors. Default: on standalone, **off** with a backend (real accounts start empty and the *Live demo* tab is hidden; set `DEMO_COMPETITORS=1` to bring it back). |
| `DEMO_DATA` | `1` opts in to fabricated data (sample report history, fake admin customers). Default off, so every screen shows real data. |
| `DEMO_USER`, `DEMO_ADMIN` | Optional `email:password` shown as one-click buttons on the login page. |

Worker required: crawl jobs sit in `queued` until the backend's worker container is running. The UI says
"Is the backend crawl worker running?" if a job doesn't finish in two minutes.

## What goes where

| UI route | With a backend | Notes |
|---|---|---|
| `POST /api/auth/login` | `POST /user/login`, then `POST /admin/login` on 401 | one form for both roles; `redirect` is `/admin` or `/app` |
| `POST /api/auth/signup` | `POST /user/signup` | `name` and `plan` are accepted by the form but **ignored by the backend** |
| `POST /api/auth/logout` | `POST /user/logout` or `/admin/logout` | the token is revoked server-side too |
| `GET /api/session`, `GET /api/me` | `GET /user/me` | cached 4 s; suspended/expired/idle sessions return `null` / 401 |
| `GET /api/plans` | local (same data as `core/plans.py`) | plus a `backend` object of capability flags the UI reads |
| `POST /api/me/plan`, `/api/me/rotate-key` | — | **501**: no backend endpoint yet. UI disables/hides them |
| `/v1/*` | proxied verbatim to the backend `/v1/*` | per-user key, credits, robots.txt, SSRF checks all backend-side |
| `GET /api/admin/users` | `GET /admin/users` | `plan`, `credits`, `name` are `null`/derived (not in the list yet); adds `activeNow`, `sessions`, `totalSessionSec` |
| `PATCH /api/admin/users/:id {status}` | `PATCH /admin/users/{id} {is_active}` | also ends that user's sessions; `plan`/`role` edits → 501 |
| `DELETE /api/admin/users/:id` | `DELETE /admin/users/{id}` | |
| `POST /api/admin/users` | `POST /user/signup` (+ logout of the session it opens) | `role: admin` → 403 |
| `/api/admin/stats` | computed from `/admin/users` | no plan mix / MRR (`byPlan: null`); shows "Signed in now" instead |
| `reset-credits`, `reset-password` | — | **501** |
| `/api/admin/audit` | local, in memory | records actions made through this server only |
| `/api/state`, `/api/competitors`, `/api/crawl`, `/api/reports*`, `/api/test/*` | **local** pipeline | the backend has no monitoring/report endpoints yet (see below) |

### Crawling with a backend
The monitoring pipeline stays in Node, but *how pages are fetched* changes: competitor sites are crawled with the
backend's `POST /v1/web/crawl` using the user's own API key, so they get real credit charging (1 per page), `robots.txt`
handling and SSRF protection, and the backend worker does the fetching. The app's own demo pages (`/test/...`) are never
sent to the backend (it blocks local addresses by design); they stay local and are not charged.

Errors from the backend show on the competitor card: `blocked by the site's robots.txt`,
`private/internal addresses are blocked`, `insufficient credits`.

## What the backend still needs (for the backend owner)

The integration works around each of these; none requires a UI change when they're added.

1. **The `/api/*` contract** from the design doc §2: `state`, `competitors` (`POST`/`PATCH`/`DELETE`), `crawl`, `reports`,
   `reports/summary`. Today the monitoring pipeline, change history, digests and reports run in Node memory, so they
   reset on restart and aren't shared between instances. When these exist, point the BFF at them (or serve the UI from the backend).
2. **Account fields**: signup should store `name` and `plan` (the form sends both); `GET /user/me` should include `created_at`.
3. **Plan changes and key rotation**: `POST /api/me/plan` (resets credits to the plan allowance) and `POST /api/me/rotate-key`.
4. **Admin list** (`GET /admin/users`) should return `name`, `plan`, `credits` so the panel can show plan mix, MRR and usage;
   plus plan/role edits, `reset-credits`, `reset-password`, and an audit log.
5. **Scrape result** has no `description` (meta description), so the company profile falls back to the first paragraph.
6. **Disabled accounts** get the same `401 Invalid credentials` as a wrong password, so the UI can't say "account disabled".
7. **Dashboard crawls aren't metered for demo sites**, and scheduled crawls keep running for a suspended user's
   workspace until their API key is rejected (the backend rejects it, so those crawls just fail).

## Verifying it

* `npm test`: unit tests, including the integration layer against an in-process fake of the backend's contract.
* `npm run test:integration`: end-to-end against a **real** backend (see `integration/README.md`): 53 checks across
  auth, sessions, real crawls through the worker, credits, change detection, robots.txt/SSRF errors, the `/v1` proxy,
  and the admin panel.
