"""Plan vs actual. Pure functions over what's in the database. Distances in km.

Scoring rules (agreed for the proof of concept):
  * Only runs are scored on distance. Strength is shown (with its manual
    upper/lower/full tag) and counted, but not scored. Everything else
    ("extra": warm-up bikes, rowing, walks...) is stored and ignored.
  * The week total is the headline. Status = actual / planned within +-10%.
  * The current week is compared "to date": only days already finished count
    (plus today, if a run has already been logged).
  * A week with nothing planned yet has no status (nothing to compare to).
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import date, timedelta

DOW_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
TOLERANCE = 0.10
# 'eff_day' is the day an activity counts on: Garmin's date unless you moved it.
ACTIVITIES_SQL = "SELECT *, COALESCE(day_override, day) AS eff_day FROM activities"
BLANK_DAY = {"kind": "rest", "distance_km": 0.0, "note": "", "strength_focus": ""}


def status_for(actual: float, planned: float) -> tuple[str, float | None]:
    """('on_track' | 'ahead' | 'behind' | 'none', fractional difference vs plan)."""
    if planned <= 0:
        return "none", None
    diff = actual / planned - 1
    if abs(diff) <= TOLERANCE + 1e-9:  # epsilon: 110/100 - 1 is 0.10000000000000009 in floating point
        return "on_track", diff
    return ("ahead" if diff > 0 else "behind"), diff


def _enrich(activity: sqlite3.Row) -> dict:
    a = dict(activity)
    a["category"] = a["category_override"] or a["category"]
    a["auto_category"] = activity["category"]
    a["km"] = a["distance_m"] / 1000
    a["minutes"] = a["duration_s"] / 60
    a["pace_s_per_km"] = a["duration_s"] / a["km"] if a["km"] > 0.1 else None
    a.pop("raw_json", None)
    return a


def _day_status(planned_km: float, run_km: float, d: date, today: date) -> str:
    if planned_km == 0:
        return "unplanned" if run_km > 0 else "rest"
    if d > today:
        return "upcoming"
    if d == today and run_km == 0:
        return "today"
    if run_km == 0:
        return "missed"
    state, _ = status_for(run_km, planned_km)
    return {"on_track": "done", "behind": "short", "ahead": "over"}[state]


def build_weeks(conn: sqlite3.Connection, today: date | None = None) -> list[dict]:
    today = today or date.today()

    plan_days: dict[str, dict[int, dict]] = defaultdict(dict)
    for row in conn.execute("SELECT * FROM plan_days"):
        plan_days[row["week_start"]][row["dow"]] = dict(row)

    by_day: dict[str, list[dict]] = defaultdict(list)
    for row in conn.execute(ACTIVITIES_SQL + " ORDER BY start_local"):
        by_day[row["eff_day"]].append(_enrich(row))

    races_by_week: dict[str, list[dict]] = defaultdict(list)
    for row in conn.execute("SELECT * FROM races ORDER BY race_date, id"):
        race_day = date.fromisoformat(row["race_date"])
        races_by_week[(race_day - timedelta(days=race_day.weekday())).isoformat()].append(
            {**dict(row), "date": race_day}
        )

    weeks = []
    for wk in conn.execute("SELECT * FROM plan_weeks ORDER BY week_start"):
        start = date.fromisoformat(wk["week_start"])
        end = start + timedelta(days=6)
        state = "future" if start > today else ("current" if today <= end else "past")

        week_plan = plan_days[wk["week_start"]]
        has_plan = any(p["kind"] != "rest" or p["distance_km"] > 0 or p["strength_focus"] for p in week_plan.values())

        days = []
        for dow in range(7):
            d = start + timedelta(days=dow)
            acts = by_day.get(d.isoformat(), [])
            runs = [a for a in acts if a["category"] == "run"]
            plan = week_plan.get(dow) or dict(BLANK_DAY)
            run_km = sum(a["km"] for a in runs)
            days.append(
                {
                    "dow": dow,
                    "label": DOW_LABELS[dow],
                    "date": d,
                    "is_today": d == today,
                    "plan": plan,
                    "runs": runs,
                    "run_km": run_km,
                    "strength": [a for a in acts if a["category"] == "strength"],
                    "extras": [a for a in acts if a["category"] == "extra"],
                    # No plan yet for the week -> don't judge individual days either.
                    "status": _day_status(plan["distance_km"], run_km, d, today) if has_plan else "none",
                }
            )

        planned_total = sum(d["plan"]["distance_km"] for d in days)
        actual_total = sum(d["run_km"] for d in days)

        # What the headline comparison is based on.
        if state == "future":
            basis_planned, basis_actual = planned_total, None
        elif state == "past":
            basis_planned, basis_actual = planned_total, actual_total
        else:
            through = today if any(d["run_km"] > 0 and d["date"] == today for d in days) else today - timedelta(days=1)
            basis_planned = sum(d["plan"]["distance_km"] for d in days if d["date"] <= through)
            basis_actual = sum(d["run_km"] for d in days if d["date"] <= through)

        if basis_actual is None or not has_plan:
            status, diff = "none", None
        else:
            status, diff = status_for(basis_actual, basis_planned)

        long_days = [d for d in days if d["plan"]["kind"] in ("long", "race")]
        weeks.append(
            {
                "week_start": wk["week_start"],
                "week_no": wk["week_no"],
                "start": start,
                "end": end,
                "phase": wk["phase"],
                "notes": wk["notes"],
                "state": state,
                "has_plan": has_plan,
                "days": days,
                "planned_total": planned_total,
                "actual_total": actual_total,
                "basis_planned": basis_planned,
                "basis_actual": basis_actual,
                "status": status,
                "diff": diff,
                "planned_long": max((d["plan"]["distance_km"] for d in long_days), default=0.0),
                "actual_long": max((d["run_km"] for d in days), default=0.0),
                "races": races_by_week.get(wk["week_start"], []),
                "activities": [a for d in days for a in by_day.get(d["date"].isoformat(), [])],
                "strength_count": sum(len(d["strength"]) for d in days),
                "strength_planned": sum(1 for d in days if d["plan"]["strength_focus"]),
            }
        )
    return weeks


def overview(weeks: list[dict]) -> dict:
    """Totals to date across the plan, plus chart series."""
    planned_cum = actual_cum = 0.0
    to_date_planned = to_date_actual = 0.0
    chart = {
        "labels": [], "planned": [], "actual": [], "planned_cum": [], "actual_cum": [],
        # per-week extras used for click-through and richer tooltips
        "week_starts": [], "titles": [], "statuses": [], "diffs": [],
    }
    for w in weeks:
        planned_cum += w["planned_total"]
        chart["labels"].append(f"W{w['week_no']} {w['start']:%d %b}")
        chart["week_starts"].append(w["week_start"])
        chart["titles"].append(
            f"Week {w['week_no']} · {w['start']:%d %b} – {w['end']:%d %b}" + (f" · {w['phase']}" if w["phase"] else "")
        )
        chart["statuses"].append(w["status"])
        chart["diffs"].append(None if w["diff"] is None else round(w["diff"] * 100))
        chart["planned"].append(round(w["planned_total"], 1) if w["has_plan"] else None)
        chart["planned_cum"].append(round(planned_cum, 1))
        if w["state"] == "future":
            chart["actual"].append(None)
            chart["actual_cum"].append(None)
        else:
            actual_cum += w["actual_total"]
            chart["actual"].append(round(w["actual_total"], 1))
            chart["actual_cum"].append(round(actual_cum, 1))
            if w["has_plan"]:  # only weeks that were planned count towards "on track"
                to_date_planned += w["basis_planned"]
                to_date_actual += w["basis_actual"]
    status, diff = status_for(to_date_actual, to_date_planned)

    current = next((w for w in weeks if w["state"] == "current"), None)
    after = [w for w in weeks if w["state"] == "future"]
    return {
        "planned_to_date": to_date_planned,
        "actual_to_date": to_date_actual,
        "status": status,
        "diff": diff,
        "plan_total": planned_cum,
        "chart": chart,
        "current": current,
        "next": after[0] if after else None,
    }
