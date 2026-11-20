"""Plan builder: which weeks exist, driven by a start date and your races.

Rules
  * The plan runs from `plan_start` (a Monday) to the week of the *end race*:
    the latest A-race, or, if there is none, the latest race of any priority.
  * Adding / moving races or changing the start date creates any missing weeks.
  * Weeks outside the range are removed only if they're empty. A week with
    planned workouts or notes is never deleted automatically.
  * A race automatically fills its day in the week with a "Race" workout, but
    never overwrites a day you've already planned something else on.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from .db import blank_days, get_meta, set_meta


def monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def races(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM races ORDER BY race_date, id")]


def end_race(all_races: list[dict]) -> dict | None:
    """The race the plan builds towards: latest A-race, else the latest race."""
    pool = [r for r in all_races if r["priority"] == "A"] or all_races
    return max(pool, key=lambda r: r["race_date"]) if pool else None


def plan_start(conn: sqlite3.Connection) -> date | None:
    stored = get_meta(conn, "plan_start")
    if stored:
        return monday(date.fromisoformat(stored))
    row = conn.execute("SELECT MIN(week_start) AS m FROM plan_weeks").fetchone()
    return date.fromisoformat(row["m"]) if row and row["m"] else None


def _week_is_empty(conn: sqlite3.Connection, week_start: str) -> bool:
    """Nothing the user planned. A day holding only a known race's auto-filled 'Race' doesn't count."""
    start = date.fromisoformat(week_start)
    race_days = {r["race_date"] for r in conn.execute("SELECT race_date FROM races")}
    for day in conn.execute("SELECT * FROM plan_days WHERE week_start = ?", (week_start,)):
        if day["kind"] == "rest" and not day["distance_km"] and not day["note"] and not day["strength_focus"]:
            continue
        day_date = (start + timedelta(days=day["dow"])).isoformat()
        if day["kind"] == "race" and day_date in race_days and not day["strength_focus"]:
            continue
        return False
    notes = conn.execute("SELECT notes FROM plan_weeks WHERE week_start = ?", (week_start,)).fetchone()
    return not (notes and notes["notes"])


def plan_range(conn: sqlite3.Connection) -> tuple[date, date]:
    """(first Monday, last Monday) the plan should cover."""
    start = plan_start(conn) or monday(date.today())
    target = end_race(races(conn))
    if target:
        end = monday(date.fromisoformat(target["race_date"]))
    else:
        row = conn.execute("SELECT MAX(week_start) AS m FROM plan_weeks").fetchone()
        end = date.fromisoformat(row["m"]) if row and row["m"] else start
    return start, max(start, end)


def sync_weeks(conn: sqlite3.Connection) -> dict:
    """Make the set of weeks match the plan range. Returns a summary for the UI."""
    start, end = plan_range(conn)
    set_meta(conn, "plan_start", start.isoformat())

    wanted = []
    d = start
    while d <= end:
        wanted.append(d.isoformat())
        d += timedelta(days=7)

    for week_start in wanted:
        conn.execute(
            "INSERT OR IGNORE INTO plan_weeks(week_start, week_no, phase, notes) VALUES(?, 0, '', '')",
            (week_start,),
        )
        blank_days(conn, week_start)

    kept_outside = 0
    for row in conn.execute("SELECT week_start FROM plan_weeks").fetchall():
        if row["week_start"] in wanted:
            continue
        if _week_is_empty(conn, row["week_start"]):
            conn.execute("DELETE FROM plan_days WHERE week_start = ?", (row["week_start"],))
            conn.execute("DELETE FROM plan_weeks WHERE week_start = ?", (row["week_start"],))
        else:
            kept_outside += 1

    for number, row in enumerate(conn.execute("SELECT week_start FROM plan_weeks ORDER BY week_start").fetchall(), 1):
        conn.execute("UPDATE plan_weeks SET week_no = ? WHERE week_start = ?", (number, row["week_start"]))

    return {"start": start, "end": end, "weeks": len(wanted), "kept_outside": kept_outside}


def apply_race_day(conn: sqlite3.Connection, race_id: int, old_distance: float | None = None) -> None:
    """Put the race on its day in the weekly plan (without clobbering a day you've planned)."""
    race = conn.execute("SELECT * FROM races WHERE id = ?", (race_id,)).fetchone()
    if race is None:
        return
    race_day = date.fromisoformat(race["race_date"])
    week_start = monday(race_day).isoformat()
    day = conn.execute(
        "SELECT * FROM plan_days WHERE week_start = ? AND dow = ?", (week_start, race_day.weekday())
    ).fetchone()
    if day is None:
        return  # race is outside the weeks we keep (can't happen for the end race)
    distance = race["distance_km"] or 0.0
    free = day["kind"] == "rest"
    auto_set = day["kind"] == "race" and day["distance_km"] in (0, old_distance)
    if free or auto_set:
        conn.execute(
            "UPDATE plan_days SET kind = 'race', distance_km = ?, note = CASE WHEN note = '' OR ? THEN ? ELSE note END "
            "WHERE week_start = ? AND dow = ?",
            (distance, 1 if auto_set else 0, race["name"], week_start, race_day.weekday()),
        )


def clear_race_day(conn: sqlite3.Connection, race_date: str) -> None:
    """A race left this date: turn its day back into Rest (unless another race is still there)."""
    if conn.execute("SELECT 1 FROM races WHERE race_date = ?", (race_date,)).fetchone():
        return
    d = date.fromisoformat(race_date)
    conn.execute(
        "UPDATE plan_days SET kind = 'rest', distance_km = 0, note = '' "
        "WHERE week_start = ? AND dow = ? AND kind = 'race'",
        (monday(d).isoformat(), d.weekday()),
    )


def outside_range_with_plan(conn: sqlite3.Connection) -> int:
    start, end = plan_range(conn)
    return sum(
        1
        for row in conn.execute("SELECT week_start FROM plan_weeks")
        if not (start <= date.fromisoformat(row["week_start"]) <= end) and not _week_is_empty(conn, row["week_start"])
    )
