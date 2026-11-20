"""FastAPI app. Run with:  uv run uvicorn app.main:app --reload"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager, suppress
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import auth, garmin, plan, planimport, stats
from .classify import CATEGORIES, KIND_LABEL, NO_DISTANCE_KINDS, PLAN_KINDS, STRENGTH_FOCUS, category_for
from .db import blank_days, connect, get_meta, init_db

HERE = Path(__file__).resolve().parent

# Background Garmin sync every N minutes (0 disables). Override with SYNC_INTERVAL_MIN.
SYNC_INTERVAL_MIN = int(os.environ.get("SYNC_INTERVAL_MIN", "30"))
STALE_AFTER_MIN = 10  # opening a page triggers a quiet sync if the last one is older than this


def _minutes_since_sync(conn) -> float | None:
    last = get_meta(conn, "last_sync_at")
    if not last:
        return None
    return (datetime.now() - datetime.strptime(last, "%Y-%m-%d %H:%M")).total_seconds() / 60


async def _sync_loop() -> None:
    with connect() as conn:
        age = _minutes_since_sync(conn)
    wait = 5.0 if age is None else max(5.0, (SYNC_INTERVAL_MIN - age) * 60)
    while True:
        await asyncio.sleep(wait)
        with suppress(Exception):  # failures are recorded in meta and shown as a banner
            await asyncio.to_thread(garmin.run_sync)
        wait = SYNC_INTERVAL_MIN * 60


@asynccontextmanager
async def lifespan(_: FastAPI):
    auth.disabled_warning()
    init_db()
    with connect() as conn:
        planimport.maybe_import(conn)
    task = asyncio.create_task(_sync_loop()) if SYNC_INTERVAL_MIN > 0 else None
    yield
    if task:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


app = FastAPI(title="Race training dashboard", lifespan=lifespan)
app.middleware("http")(auth.require_login)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")


# ---- template helpers --------------------------------------------------------
def _km(value: float | None) -> str:
    if value is None:
        return "–"
    return f"{value:.1f}".removesuffix(".0")


def _pct(diff: float | None) -> str:
    return "–" if diff is None else f"{diff * 100:+.0f}%"


def _duration(seconds: float) -> str:
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def _pace(sec_per_km: float | None) -> str:
    if not sec_per_km:
        return "–"
    return f"{int(sec_per_km // 60)}:{int(sec_per_km % 60):02d}/km"


def _static_version() -> int:
    """Changes whenever style.css does, so browsers never show a stale stylesheet."""
    return int((HERE / "static" / "style.css").stat().st_mtime)


templates.env.globals["static_v"] = _static_version
templates.env.globals["auth_enabled"] = auth.enabled
templates.env.filters.update(km=_km, pct=_pct, duration=_duration, pace=_pace)
templates.env.globals.update(
    STRENGTH_FOCUS=STRENGTH_FOCUS,
    CATEGORIES=CATEGORIES,
    PLAN_KINDS=PLAN_KINDS,
    KIND_LABEL=KIND_LABEL,
    STATUS_LABELS={"on_track": "On track", "ahead": "Ahead", "behind": "Behind", "none": "–"},
)


def render(request: Request, name: str, **ctx) -> HTMLResponse:
    with connect() as conn:
        age = _minutes_since_sync(conn)
        ctx.setdefault("last_sync_at", get_meta(conn, "last_sync_at"))
        ctx.setdefault("sync_error", get_meta(conn, "last_sync_error") or "")
        this_week = plan.monday(date.today()).isoformat()  # the phone tab bar links straight to it
        if not conn.execute("SELECT 1 FROM plan_weeks WHERE week_start = ?", (this_week,)).fetchone():
            this_week = None
        ctx.setdefault("this_week", this_week)
    ctx.setdefault("sync_stale", age is None or age > STALE_AFTER_MIN)
    ctx.setdefault("today", date.today())
    return templates.TemplateResponse(request, name, ctx)


def _week_or_404(weeks: list[dict], week_start: str) -> dict:
    week = next((w for w in weeks if w["week_start"] == week_start), None)
    if week is None:
        raise HTTPException(404, "No such plan week")
    return week


def _refresh() -> Response:
    return Response(status_code=200, headers={"HX-Refresh": "true"})


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        raise HTTPException(422, "Dates must look like 2027-02-21") from None


def _parse_float(value: str) -> float | None:
    try:
        return max(0.0, float(value)) if value.strip() else None
    except ValueError:
        return None


# ---- login -------------------------------------------------------------------
def _login_page(request: Request, next_url: str, error: str = "", status_code: int = 200) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "login.html", {"next": auth.safe_next(next_url), "error": error}, status_code=status_code
    )


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/"):
    if not auth.enabled() or auth.valid_token(request.cookies.get(auth.COOKIE)):
        return RedirectResponse(auth.safe_next(next), status_code=303)
    return _login_page(request, next)


@app.post("/login")
def login(request: Request, password: str = Form(""), next: str = Form("/")):
    if not auth.enabled():
        return RedirectResponse("/", status_code=303)
    if auth.throttled(request):
        return _login_page(request, next, "Too many wrong passwords. Wait a few minutes and try again.", 429)
    if not auth.password_ok(password):
        auth.record_failure(request)
        return _login_page(request, next, "Wrong password.", 401)
    auth.clear_failures(request)
    return auth.login_redirect(request, next)


@app.post("/logout")
def logout():
    return auth.logged_out()


@app.get("/healthz")
def healthz():
    """For hosting health checks. Public and reveals nothing."""
    return {"ok": True}


# ---- pages -------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    today = date.today()
    with connect() as conn:
        weeks = stats.build_weeks(conn)
        races = [dict(r) for r in conn.execute("SELECT * FROM races ORDER BY race_date, id")]
    for race in races:
        race["date"] = date.fromisoformat(race["race_date"])
        race["days_to_go"] = (race["date"] - today).days
    upcoming = [r for r in races if r["days_to_go"] >= 0][:4]
    overview = stats.overview(weeks)
    return render(
        request,
        "dashboard.html",
        weeks=weeks,
        races=upcoming,
        overview=overview,
        chart_json=json.dumps(overview["chart"]),
    )


@app.get("/week/{week_start}", response_class=HTMLResponse)
def week_page(request: Request, week_start: str):
    with connect() as conn:
        weeks = stats.build_weeks(conn)
    week = _week_or_404(weeks, week_start)
    idx = weeks.index(week)
    return render(
        request,
        "week.html",
        w=week,
        prev_week=weeks[idx - 1] if idx > 0 else None,
        next_week=weeks[idx + 1] if idx < len(weeks) - 1 else None,
    )


@app.get("/activities", response_class=HTMLResponse)
def activities_page(request: Request, category: str = ""):
    with connect() as conn:
        rows = [stats._enrich(r) for r in conn.execute(stats.ACTIVITIES_SQL + " ORDER BY start_local DESC")]
    if category in CATEGORIES:
        rows = [r for r in rows if r["category"] == category]
    return render(request, "activities.html", activities=rows, category=category)


@app.get("/races", response_class=HTMLResponse)
def races_page(request: Request):
    today = date.today()
    with connect() as conn:
        rows = plan.races(conn)
        start, end = plan.plan_range(conn)
        info = {
            "start": start,
            "end": end,
            "weeks": (end - start).days // 7 + 1,
            "outside_with_plan": plan.outside_range_with_plan(conn),
        }
        target = plan.end_race(rows)
    for race in rows:
        race["date"] = date.fromisoformat(race["race_date"])
        race["days_to_go"] = (race["date"] - today).days
        race["is_end"] = bool(target and race["id"] == target["id"])
    return render(request, "races.html", races=rows, info=info, target=target)


# ---- sync --------------------------------------------------------------------
@app.post("/refresh")
def refresh():
    """The Refresh button: pull the latest activities from Garmin Connect."""
    try:
        result = garmin.run_sync()
    except garmin.GarminAuthError as exc:
        return HTMLResponse(f'<span class="sync-msg error">{exc}</span>')
    except Exception as exc:  # noqa: BLE001 - surface anything else to the user
        return HTMLResponse(f'<span class="sync-msg error">Sync failed: {type(exc).__name__}: {exc}</span>')
    if result.get("skipped"):
        return HTMLResponse('<span class="muted">A sync is already running…</span>')
    return Response(headers={"HX-Refresh": "true"}, content=json.dumps(result))


@app.post("/sync")
def sync_quietly():
    """Called by the page itself when data looks stale. JSON so the page can decide whether to reload."""
    try:
        result = garmin.run_sync()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    return {"ok": True, **result}


# ---- races & plan range --------------------------------------------------------
def _clean_priority(value: str) -> str:
    return value if value in ("A", "B", "C") else "B"


@app.post("/races")
def add_race(
    name: str = Form(...),
    race_date: str = Form(...),
    distance_km: str = Form(""),
    priority: str = Form("B"),
    goal_time: str = Form(""),
    notes: str = Form(""),
):
    day = _parse_date(race_date)
    with connect() as conn:
        race_id = conn.execute(
            "INSERT INTO races(name, race_date, distance_km, priority, goal_time, notes) VALUES(?,?,?,?,?,?)",
            (name.strip() or "Race", day.isoformat(), _parse_float(distance_km), _clean_priority(priority),
             goal_time.strip(), notes.strip()),
        ).lastrowid
        plan.sync_weeks(conn)
        plan.apply_race_day(conn, race_id)
    return RedirectResponse("/races", status_code=303)


@app.post("/races/{race_id}")
def update_race(
    race_id: int,
    name: str = Form(...),
    race_date: str = Form(...),
    distance_km: str = Form(""),
    priority: str = Form("B"),
    goal_time: str = Form(""),
    notes: str = Form(""),
):
    day = _parse_date(race_date)
    with connect() as conn:
        old = conn.execute("SELECT * FROM races WHERE id = ?", (race_id,)).fetchone()
        if old is None:
            raise HTTPException(404)
        conn.execute(
            "UPDATE races SET name = ?, race_date = ?, distance_km = ?, priority = ?, goal_time = ?, notes = ? WHERE id = ?",
            (name.strip() or "Race", day.isoformat(), _parse_float(distance_km), _clean_priority(priority),
             goal_time.strip(), notes.strip(), race_id),
        )
        if old["race_date"] != day.isoformat():
            plan.clear_race_day(conn, old["race_date"])
        plan.sync_weeks(conn)
        plan.apply_race_day(conn, race_id, old_distance=old["distance_km"])
    return _refresh()


@app.post("/races/{race_id}/delete")
def delete_race(race_id: int):
    with connect() as conn:
        old = conn.execute("SELECT * FROM races WHERE id = ?", (race_id,)).fetchone()
        if old is None:
            raise HTTPException(404)
        conn.execute("DELETE FROM races WHERE id = ?", (race_id,))
        plan.clear_race_day(conn, old["race_date"])
        plan.sync_weeks(conn)
    return _refresh()


@app.post("/plan/settings")
def plan_settings(plan_start: str = Form(...)):
    start = plan.monday(_parse_date(plan_start))
    with connect() as conn:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('plan_start', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (start.isoformat(),),
        )
        plan.sync_weeks(conn)
        for race in plan.races(conn):  # races that just came into range get their day filled in
            plan.apply_race_day(conn, race["id"])
    return _refresh()


# ---- weekly plan editing --------------------------------------------------------
@app.post("/week/{week_start}/meta")
def save_week_meta(week_start: str, phase: str = Form(""), notes: str = Form("")):
    with connect() as conn:
        conn.execute(
            "UPDATE plan_weeks SET phase = ?, notes = ? WHERE week_start = ?",
            (phase.strip(), notes.strip(), week_start),
        )
    return Response(status_code=204)


@app.post("/week/{week_start}/day/{dow}", response_class=HTMLResponse)
def save_day(
    request: Request,
    week_start: str,
    dow: int,
    kind: str = Form("rest"),
    distance_km: str = Form(""),
    note: str = Form(""),
    strength_focus: str = Form(""),
):
    if kind not in KIND_LABEL:
        raise HTTPException(422, "Unknown workout type")
    distance = _parse_float(distance_km) or 0.0
    forced_zero = kind in NO_DISTANCE_KINDS
    if forced_zero:
        distance = 0.0

    with connect() as conn:
        updated = conn.execute(
            "UPDATE plan_days SET kind = ?, distance_km = ?, note = ?, strength_focus = ? "
            "WHERE week_start = ? AND dow = ?",
            (kind, distance, note.strip(), strength_focus if strength_focus in STRENGTH_FOCUS else "", week_start, dow),
        ).rowcount
        if not updated:
            raise HTTPException(404)
        weeks = stats.build_weeks(conn)
    week = _week_or_404(weeks, week_start)
    response = templates.TemplateResponse(
        request, "_day_update.html", {"w": week, "day": week["days"][dow], "today": date.today()}
    )
    if forced_zero:  # keep the distance box honest when switching to Rest / Cross-train
        response.headers["HX-Trigger"] = json.dumps({"distSet": {"dow": dow, "value": 0}})
    return response


@app.post("/week/{week_start}/swap")
def swap_days(week_start: str, a: int = Form(...), b: int = Form(...)):
    """Drag one planned day onto another: their workouts trade places (or the workout moves to a rest day)."""
    if not (0 <= a <= 6 and 0 <= b <= 6) or a == b:
        raise HTTPException(422, "Pick two different days")
    fields = ("kind", "distance_km", "note", "strength_focus")
    with connect() as conn:
        rows = {
            r["dow"]: r
            for r in conn.execute("SELECT * FROM plan_days WHERE week_start = ? AND dow IN (?, ?)", (week_start, a, b))
        }
        if len(rows) != 2:
            raise HTTPException(404)
        for target, source in ((a, b), (b, a)):
            conn.execute(
                "UPDATE plan_days SET kind = ?, distance_km = ?, note = ?, strength_focus = ? WHERE week_start = ? AND dow = ?",
                (*(rows[source][f] for f in fields), week_start, target),
            )
    return _refresh()


@app.post("/week/{week_start}/copy-previous")
def copy_previous(week_start: str):
    """Fill this week with last week's plan (types, distances, notes, strength)."""
    previous = (date.fromisoformat(week_start) - timedelta(days=7)).isoformat()
    with connect() as conn:
        if not conn.execute("SELECT 1 FROM plan_weeks WHERE week_start = ?", (previous,)).fetchone():
            raise HTTPException(404, "There is no previous week to copy")
        blank_days(conn, week_start)
        conn.execute(
            """
            UPDATE plan_days SET
              kind           = (SELECT p.kind           FROM plan_days p WHERE p.week_start = ? AND p.dow = plan_days.dow),
              distance_km    = (SELECT p.distance_km    FROM plan_days p WHERE p.week_start = ? AND p.dow = plan_days.dow),
              note           = (SELECT p.note           FROM plan_days p WHERE p.week_start = ? AND p.dow = plan_days.dow),
              strength_focus = (SELECT p.strength_focus FROM plan_days p WHERE p.week_start = ? AND p.dow = plan_days.dow)
            WHERE week_start = ?
            """,
            (previous, previous, previous, previous, week_start),
        )
    return _refresh()


@app.post("/week/{week_start}/clear")
def clear_week(week_start: str):
    with connect() as conn:
        conn.execute(
            "UPDATE plan_days SET kind = 'rest', distance_km = 0, note = '', strength_focus = '' WHERE week_start = ?",
            (week_start,),
        )
    return _refresh()


# ---- activities --------------------------------------------------------------------
MANUAL_TYPES = {"run": "running", "strength": "strength_training", "other": "other"}


@app.post("/week/{week_start}/manual")
def add_manual_activity(
    week_start: str,
    day: str = Form(...),
    kind: str = Form("run"),
    distance_km: str = Form(""),
    minutes: str = Form(""),
    name: str = Form(""),
    avg_hr: str = Form(""),
):
    """Log something Garmin didn't record. Manual activities get negative ids and are never touched by sync."""
    when = _parse_date(day)
    start = date.fromisoformat(week_start)
    if not start <= when <= start + timedelta(days=6):
        raise HTTPException(422, "Pick a day inside this week")
    if kind not in MANUAL_TYPES:
        raise HTTPException(422, "Unknown activity type")
    type_key = MANUAL_TYPES[kind]
    km, mins, hr = _parse_float(distance_km) or 0.0, _parse_float(minutes) or 0.0, _parse_float(avg_hr)
    with connect() as conn:
        lowest = conn.execute("SELECT COALESCE(MIN(id), 0) AS m FROM activities").fetchone()["m"]
        conn.execute(
            """
            INSERT INTO activities(id, start_local, day, type_key, name, distance_m, duration_s, avg_hr,
                                   category, raw_json, source)
            VALUES(?,?,?,?,?,?,?,?,?,?, 'manual')
            """,
            (
                min(-1, lowest - 1),
                f"{when.isoformat()} 12:00:00",
                when.isoformat(),
                type_key,
                name.strip() or f"Manual {kind}",
                km * 1000,
                mins * 60,
                hr,
                category_for(type_key),
                "{}",
            ),
        )
    return _refresh()


@app.post("/activity/{activity_id}/focus")
def set_focus(activity_id: int, focus: str = Form("")):
    value = focus if focus in STRENGTH_FOCUS else None
    with connect() as conn:
        conn.execute("UPDATE activities SET strength_focus = ? WHERE id = ?", (value, activity_id))
    return Response(status_code=204)


@app.post("/activity/{activity_id}/category")
def set_category(activity_id: int, category: str = Form("")):
    value = category if category in CATEGORIES else None  # '' = back to automatic
    with connect() as conn:
        conn.execute("UPDATE activities SET category_override = ? WHERE id = ?", (value, activity_id))
    return _refresh()


@app.post("/activity/{activity_id}/day")
def set_activity_day(activity_id: int, day: str = Form("")):
    """Count an activity on a different day (empty = back to the day Garmin recorded)."""
    with connect() as conn:
        row = conn.execute("SELECT day FROM activities WHERE id = ?", (activity_id,)).fetchone()
        if row is None:
            raise HTTPException(404)
        value = _parse_date(day).isoformat() if day.strip() else None
        if value == row["day"]:
            value = None
        conn.execute("UPDATE activities SET day_override = ? WHERE id = ?", (value, activity_id))
    return _refresh()


@app.post("/activity/{activity_id}/delete")
def delete_manual_activity(activity_id: int):
    with connect() as conn:
        deleted = conn.execute("DELETE FROM activities WHERE id = ? AND source = 'manual'", (activity_id,)).rowcount
    if not deleted:
        raise HTTPException(404, "Only manually added activities can be deleted")
    return _refresh()
