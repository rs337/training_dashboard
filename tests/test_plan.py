"""Plan builder rules: week range, race-day auto-fill, never deleting planned weeks."""

from __future__ import annotations

from datetime import date

from app import plan


def add_race(conn, name, day, priority="B", km=None):
    rid = conn.execute(
        "INSERT INTO races(name, race_date, distance_km, priority) VALUES(?,?,?,?)", (name, day, km, priority)
    ).lastrowid
    conn.commit()
    return rid


def week_starts(conn):
    return [r["week_start"] for r in conn.execute("SELECT week_start FROM plan_weeks ORDER BY week_start")]


def setup_plan(conn, start="2026-11-16"):
    conn.execute("INSERT INTO meta(key, value) VALUES('plan_start', ?)", (start,))
    conn.commit()


def test_end_race_prefers_latest_a_race():
    races = [
        {"id": 1, "race_date": "2027-02-21", "priority": "A"},
        {"id": 2, "race_date": "2027-04-18", "priority": "A"},
        {"id": 3, "race_date": "2027-05-30", "priority": "B"},
    ]
    assert plan.end_race(races)["id"] == 2


def test_end_race_falls_back_to_latest_when_no_a_race():
    races = [{"id": 1, "race_date": "2027-02-21", "priority": "B"}, {"id": 2, "race_date": "2027-03-01", "priority": "C"}]
    assert plan.end_race(races)["id"] == 2
    assert plan.end_race([]) is None


def test_weeks_run_from_start_to_the_end_race_week(conn):
    setup_plan(conn, "2026-11-16")
    add_race(conn, "Half", "2026-12-06", "A")  # a Sunday: week of Mon 30 Nov
    result = plan.sync_weeks(conn)
    assert week_starts(conn) == ["2026-11-16", "2026-11-23", "2026-11-30"]
    assert result["weeks"] == 3
    assert [r["week_no"] for r in conn.execute("SELECT week_no FROM plan_weeks ORDER BY week_start")] == [1, 2, 3]
    # every week has seven day rows
    assert conn.execute("SELECT COUNT(*) FROM plan_days").fetchone()[0] == 21


def test_start_date_is_snapped_to_monday(conn):
    setup_plan(conn, "2026-11-19")  # Thursday
    add_race(conn, "10k", "2026-11-29", "A")
    plan.sync_weeks(conn)
    assert week_starts(conn)[0] == "2026-11-16"


def test_race_fills_its_own_day(conn):
    setup_plan(conn)
    rid = add_race(conn, "Half", "2026-12-06", "A", km=21.0975)
    plan.sync_weeks(conn)
    plan.apply_race_day(conn, rid)
    day = conn.execute("SELECT * FROM plan_days WHERE week_start = '2026-11-30' AND dow = 6").fetchone()
    assert (day["kind"], day["distance_km"], day["note"]) == ("race", 21.0975, "Half")


def test_race_never_overwrites_a_planned_day(conn):
    setup_plan(conn)
    rid = add_race(conn, "Half", "2026-12-06", "A", km=21.0975)
    plan.sync_weeks(conn)
    conn.execute("UPDATE plan_days SET kind='easy', distance_km=5, note='shakeout' WHERE week_start='2026-11-30' AND dow=6")
    plan.apply_race_day(conn, rid)
    day = conn.execute("SELECT * FROM plan_days WHERE week_start = '2026-11-30' AND dow = 6").fetchone()
    assert (day["kind"], day["distance_km"], day["note"]) == ("easy", 5, "shakeout")


def test_changing_race_distance_updates_auto_filled_day_only(conn):
    setup_plan(conn)
    rid = add_race(conn, "Half", "2026-12-06", "A", km=21.0975)
    plan.sync_weeks(conn)
    plan.apply_race_day(conn, rid)
    conn.execute("UPDATE races SET distance_km = 10 WHERE id = ?", (rid,))
    plan.apply_race_day(conn, rid, old_distance=21.0975)
    day = conn.execute("SELECT distance_km FROM plan_days WHERE week_start = '2026-11-30' AND dow = 6").fetchone()
    assert day["distance_km"] == 10


def test_empty_weeks_outside_range_are_removed(conn):
    setup_plan(conn)
    add_race(conn, "Half", "2026-12-06", "A")
    plan.sync_weeks(conn)
    conn.execute("UPDATE races SET race_date = '2026-11-22'")  # race moves earlier: last week is now surplus
    result = plan.sync_weeks(conn)
    assert week_starts(conn) == ["2026-11-16"]
    assert result["kept_outside"] == 0


def test_weeks_with_a_planned_workout_are_never_deleted(conn):
    setup_plan(conn)
    add_race(conn, "Half", "2026-12-06", "A")
    plan.sync_weeks(conn)
    conn.execute("UPDATE plan_days SET kind='long', distance_km=18 WHERE week_start='2026-11-30' AND dow=5")
    conn.execute("UPDATE races SET race_date = '2026-11-22'")
    result = plan.sync_weeks(conn)
    assert "2026-11-30" in week_starts(conn)
    assert result["kept_outside"] == 1
    assert plan.outside_range_with_plan(conn) == 1


def test_weeks_with_notes_are_kept(conn):
    setup_plan(conn)
    add_race(conn, "Half", "2026-12-06", "A")
    plan.sync_weeks(conn)
    conn.execute("UPDATE plan_weeks SET notes = 'deload' WHERE week_start = '2026-11-30'")
    conn.execute("UPDATE races SET race_date = '2026-11-22'")
    plan.sync_weeks(conn)
    assert "2026-11-30" in week_starts(conn)


def test_untouched_race_day_does_not_count_as_planned(conn):
    """Moving a race earlier: the week that only held its auto-filled 'Race' day is cleaned up (as update_race does)."""
    setup_plan(conn)
    rid = add_race(conn, "Half", "2026-12-06", "A", km=21.0975)
    plan.sync_weeks(conn)
    plan.apply_race_day(conn, rid)
    conn.execute("UPDATE races SET race_date = '2026-11-22' WHERE id = ?", (rid,))
    plan.clear_race_day(conn, "2026-12-06")
    plan.sync_weeks(conn)
    assert week_starts(conn) == ["2026-11-16"]


def test_clear_race_day_resets_to_rest_unless_another_race_is_there(conn):
    setup_plan(conn)
    rid = add_race(conn, "Half", "2026-12-06", "A", km=21)
    other = add_race(conn, "Fun run", "2026-12-06", "C", km=5)
    plan.sync_weeks(conn)
    plan.apply_race_day(conn, rid)
    conn.execute("DELETE FROM races WHERE id = ?", (rid,))
    plan.clear_race_day(conn, "2026-12-06")  # the other race still holds the date
    assert conn.execute("SELECT kind FROM plan_days WHERE week_start='2026-11-30' AND dow=6").fetchone()["kind"] == "race"
    conn.execute("DELETE FROM races WHERE id = ?", (other,))
    plan.clear_race_day(conn, "2026-12-06")
    assert conn.execute("SELECT kind FROM plan_days WHERE week_start='2026-11-30' AND dow=6").fetchone()["kind"] == "rest"


def test_plan_start_defaults_to_this_monday_with_no_data(conn):
    start, end = plan.plan_range(conn)
    assert start == plan.monday(date.today()) and end == start
