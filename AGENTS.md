# Race training dashboard

Single-user web app to plan a marathon + half-marathon build (Manchester Marathon 18 Apr 2027, half 21 Feb 2027)
and compare the plan with what Garmin Connect actually recorded. Distances are **km** everywhere.

## Run it

```
uv run uvicorn app.main:app --port 8000        # http://localhost:8000
uv run python -m app.garmin login              # one-off Garmin login (prompts for password/MFA); tokens -> ~/.garminconnect
uv run python -m app.garmin sync               # same as the Refresh button
```

- **Phone access (private, free, works away from home)**: install Tailscale on the Mac and the phone, sign in to the same
  account, keep the app on `127.0.0.1` and run `tailscale serve --bg 8000`. It prints an `https://<mac>.<tailnet>.ts.net`
  URL that only your devices can open. Set `COOKIE_SECURE=1` in `.env` when using it. The Mac must be awake and the
  server running. Same-Wi-Fi quick test only: `uvicorn ... --host 0.0.0.0` then `http://<mac-lan-ip>:8000` (plain HTTP,
  anyone on that network can reach the login page; do not leave it like this).
- Env `DASHBOARD_PASSWORD`: turns on the single-password login (`app/auth.py`). Unset = no login (localhost only; a
  warning is logged). Copy `.env.example` to `.env` (gitignored). Also `SESSION_SECRET`, `COOKIE_SECURE=1`.
- Env `SYNC_INTERVAL_MIN` (default 30, `0` = off): background Garmin sync while the server runs.
- Env `DASHBOARD_DB=/path/to/copy.db`: point the app at a different SQLite file. **Use this to test anything
  destructive on a copy**; never experiment on the real DB.
- Stop servers with `pgrep -f '[u]vicorn' | xargs kill`. Plain `pkill -f "uvicorn app.main"` also kills the shell
  running it, because the command line contains the pattern.
- Python deps via `uv` (`uv add <pkg>`); no Node. Front end is server-rendered Jinja + HTMX + Chart.js (both from CDNs).

## Layout

- `app/main.py`: all routes, template filters, background sync loop. HTMX pattern: inputs autosave via `hx-post`
  with `hx-swap="none"`; actions that change totals return `HX-Refresh: true` (see `_refresh()`).
- `app/auth.py`: signed-cookie password login middleware (30-day session, 5-failures/5-min throttle). Public paths:
  `/login`, `/static/*`, `/healthz`. Temporary: the multi-user version replaces it with per-user accounts.
- `tests/`: pytest. `conftest.py` gives each test a temp DB and an empty plan (the xlsx seed is disabled).
- `app/db.py`: SQLite schema + versioned migrations (`PRAGMA user_version`, now 3). Each migration backs the DB
  up to `data/dashboard.pre-vN-backup.db` first. Data file `data/dashboard.db` is gitignored.
- `app/plan.py`: plan builder (which weeks exist, race-day auto-fill).
- `app/stats.py`: plan-vs-actual scoring (`build_weeks`, `overview`), pure functions over the DB.
- `app/garmin.py`: cached-token login, `sync()`, `run_sync()` (lock + records `last_sync_error` in meta).
- `app/classify.py`: Garmin type -> category (run / strength / extra), planned workout kinds.
- `app/planimport.py`: seeds weeks/phases/races from `plan-template.xlsx` on an empty DB only. The
  spreadsheet's daily workouts were filler and are deliberately NOT imported.
- `app/templates/`, `app/static/style.css` (served with a `?v=mtime` cache-buster).

## Rules the user agreed (keep them unless told otherwise)

- Only **runs** are scored on distance. **Strength** is counted and tagged upper/lower/full (manual). Everything
  else (warm-up bikes, rowing, walks) is stored as "extra" and never scored.
- Weekly headline status = actual / planned within **±10%** is "on track". The current week is compared
  **to date** (finished days, plus today only if a run is logged). Weeks with no plan are not scored.
- Plan range = start date (Monday) to the week of the **latest A-race** (else latest race). Weeks that contain
  planned workouts/notes are never auto-deleted. A race fills its day with a "Race" workout but never overwrites
  a day the user already planned.
- Manual activities have **negative ids** and `source='manual'`; sync never touches them. `category_override`,
  `strength_focus` and `day_override` on Garmin activities survive re-syncs (upsert doesn't write them).
- Weeks are Mon-Sun, local time (Garmin `startTimeLocal`, user is in Dublin).

## Gotchas

- Garmin access uses the **unofficial** `garminconnect` library. It can break when Garmin changes login. The
  user's Garmin also syncs to Strava, so Strava's official API is the planned fallback. Sync only needs cached
  tokens (no password); never log or commit credentials. Tokens live in `~/.garminconnect`, outside the repo.
- The real plan lives in `data/dashboard.db`. Back it up before schema changes.
- Run `uv run pytest` before committing. UI behaviour (HTMX, drag-to-swap, charts) is still only checked by hand.
- Status uses a 1e-9 epsilon on the ±10% rule: `110/100 - 1` is 0.10000000000000009 in floating point.

## Not built yet (rough priority)

1. Target paces (easy/tempo/HM/MP) + lap-level checks of tempo/interval sessions (needs Garmin splits).
2. Weekly load / ramp-rate warnings (>~10% jumps, long-run share).
3. Week templates and bulk edits ("add 2 km to every easy run in weeks 9-12").
4. Calendar (.ics) export; pushing planned workouts to the watch (fragile).
5. Phone access / hosting with auth and backups; drag-to-swap is mouse-only today.
6. Race readiness / finish-time predictions; race result linking to a Garmin activity.
7. More tests (UI/browser level; the data layer and routes are covered).
