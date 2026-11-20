"""Plan-vs-actual scoring rules from AGENTS.md."""

from __future__ import annotations

from datetime import date

import pytest

from app import plan, stats
from app.classify import category_for
from tests.conftest import add_activity

MON = date(2026, 11, 16)  # a Monday


def make_week(conn, week_start=MON, days=None):
    """days: {dow: (kind, km)} -> plan_weeks + plan_days rows."""
    from app.db import blank_days

    conn.execute("INSERT OR IGNORE INTO plan_weeks(week_start, week_no) VALUES(?, 1)", (week_start.isoformat(),))
    blank_days(conn, week_start.isoformat())
    for dow, (kind, km) in (days or {}).items():
        conn.execute(
            "UPDATE plan_days SET kind = ?, distance_km = ? WHERE week_start = ? AND dow = ?",
            (kind, km, week_start.isoformat(), dow),
        )
    conn.commit()


def only_week(conn, today):
    (week,) = stats.build_weeks(conn, today=today)
    return week


# ---- status_for ------------------------------------------------------------------
@pytest.mark.parametrize(
    "actual, planned, expected",
    [
        (100, 100, "on_track"),
        (110, 100, "on_track"),  # exactly +10% is still on track
        (90, 100, "on_track"),  # exactly -10% too
        (110.1, 100, "ahead"),
        (89.9, 100, "behind"),
        (0, 100, "behind"),
        (5, 0, "none"),  # nothing planned -> nothing to compare
        (0, 0, "none"),
    ],
)
def test_status_for(actual, planned, expected):
    assert stats.status_for(actual, planned)[0] == expected


def test_status_for_reports_the_difference():
    assert stats.status_for(120, 100)[1] == pytest.approx(0.2)
    assert stats.status_for(1, 0)[1] is None


# ---- classify ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "type_key, category",
    [
        ("running", "run"), ("trail_running", "run"), ("treadmill_running", "run"),
        ("strength_training", "strength"),
        ("cycling", "extra"), ("indoor_cardio", "extra"), ("walking", "extra"), ("rowing", "extra"),
    ],
)
def test_category_for(type_key, category):
    assert category_for(type_key) == category


# ---- build_weeks ----------------------------------------------------------------------
def test_past_week_on_track(conn):
    make_week(conn, days={1: ("easy", 10), 3: ("tempo", 10), 5: ("long", 20)})
    for i, (day, km) in enumerate([("2026-11-17", 10), ("2026-11-19", 9.5), ("2026-11-21", 20.5)]):
        add_activity(conn, i + 1, day, km=km)
    week = only_week(conn, today=date(2026, 11, 30))
    assert week["state"] == "past"
    assert week["planned_total"] == 40
    assert week["actual_total"] == pytest.approx(40)
    assert week["status"] == "on_track"


def test_past_week_behind_with_missed_day(conn):
    make_week(conn, days={1: ("easy", 10), 3: ("tempo", 10)})
    add_activity(conn, 1, "2026-11-17", km=10)
    week = only_week(conn, today=date(2026, 11, 30))
    assert week["status"] == "behind"
    assert [d["status"] for d in week["days"]][3] == "missed"
    assert week["days"][1]["status"] == "done"


def test_current_week_is_compared_to_date_only(conn):
    make_week(conn, days={0: ("easy", 10), 2: ("easy", 10), 5: ("long", 30)})
    add_activity(conn, 1, "2026-11-16", km=10)
    add_activity(conn, 2, "2026-11-18", km=10)
    # Thursday: Saturday's long run hasn't happened yet, so it must not make the week "behind".
    week = only_week(conn, today=date(2026, 11, 19))
    assert week["state"] == "current"
    assert week["basis_planned"] == 20
    assert week["basis_actual"] == pytest.approx(20)
    assert week["status"] == "on_track"
    assert week["planned_total"] == 50


def test_today_counts_only_once_a_run_is_logged(conn):
    make_week(conn, days={0: ("easy", 10), 1: ("easy", 10)})
    add_activity(conn, 1, "2026-11-16", km=10)
    tuesday = date(2026, 11, 17)
    assert only_week(conn, tuesday)["basis_planned"] == 10  # today's run not done yet
    add_activity(conn, 2, "2026-11-17", km=10)
    assert only_week(conn, tuesday)["basis_planned"] == 20  # now it is


def test_future_week_has_no_status(conn):
    make_week(conn, days={0: ("easy", 10)})
    week = only_week(conn, today=date(2026, 11, 1))
    assert week["state"] == "future"
    assert week["status"] == "none"
    assert week["basis_actual"] is None


def test_week_without_plan_is_not_scored(conn):
    make_week(conn)
    add_activity(conn, 1, "2026-11-17", km=12)
    week = only_week(conn, today=date(2026, 11, 30))
    assert week["has_plan"] is False
    assert week["status"] == "none"
    assert week["actual_total"] == pytest.approx(12)  # still recorded, just not judged


def test_only_runs_are_scored(conn):
    make_week(conn, days={1: ("easy", 10)})
    add_activity(conn, 1, "2026-11-17", "running", km=10)
    add_activity(conn, 2, "2026-11-17", "strength_training", km=0)
    add_activity(conn, 3, "2026-11-17", "cycling", km=30)
    add_activity(conn, 4, "2026-11-17", "walking", km=5)
    week = only_week(conn, today=date(2026, 11, 30))
    assert week["actual_total"] == pytest.approx(10)
    assert week["strength_count"] == 1
    assert len(week["days"][1]["extras"]) == 2


def test_category_override_changes_scoring(conn):
    make_week(conn, days={1: ("easy", 10)})
    add_activity(conn, 1, "2026-11-17", "treadmill_running", km=10, category_override="extra")
    assert only_week(conn, date(2026, 11, 30))["actual_total"] == 0
    add_activity(conn, 2, "2026-11-17", "cycling", km=10, category_override="run")
    assert only_week(conn, date(2026, 11, 30))["actual_total"] == pytest.approx(10)


def test_day_override_moves_an_activity(conn):
    make_week(conn, days={1: ("easy", 10), 2: ("rest", 0)})
    add_activity(conn, 1, "2026-11-18", km=10, day_override="2026-11-17")
    week = only_week(conn, date(2026, 11, 30))
    assert week["days"][1]["run_km"] == pytest.approx(10)
    assert week["days"][2]["run_km"] == 0


def test_unplanned_run_is_flagged(conn):
    make_week(conn, days={0: ("easy", 5)})
    add_activity(conn, 1, "2026-11-20", km=8)
    assert only_week(conn, date(2026, 11, 30))["days"][4]["status"] == "unplanned"


def test_week_long_run_uses_long_and_race_days(conn):
    make_week(conn, days={2: ("easy", 12), 5: ("long", 28)})
    assert only_week(conn, date(2026, 11, 30))["planned_long"] == 28


def test_overview_counts_only_planned_weeks_and_accumulates(conn):
    make_week(conn, MON, {1: ("easy", 10)})
    make_week(conn, date(2026, 11, 23), {})  # unplanned: must not drag the headline down
    make_week(conn, date(2026, 11, 30), {1: ("easy", 10)})  # future
    add_activity(conn, 1, "2026-11-17", km=10)
    add_activity(conn, 2, "2026-11-24", km=15)
    weeks = stats.build_weeks(conn, today=date(2026, 11, 29))  # Sunday: third week hasn't started
    overview = stats.overview(weeks)
    assert overview["status"] == "on_track"
    assert overview["planned_to_date"] == 10
    assert overview["actual_to_date"] == pytest.approx(10)
    assert overview["plan_total"] == 20
    assert overview["chart"]["actual_cum"][:2] == [10.0, 25.0]
    assert overview["chart"]["planned"][1] is None  # unplanned week isn't drawn as a zero target
    assert overview["next"] is None or overview["next"]["state"] == "future"


def test_overview_with_no_weeks():
    assert stats.overview([])["status"] == "none"


def test_plan_helper_monday():
    assert plan.monday(date(2026, 11, 22)) == MON  # Sunday
    assert plan.monday(MON) == MON
