"""HTTP behaviour of the plan-editing and activity routes (auth off, throwaway DB)."""

from __future__ import annotations

from app import db
from tests.conftest import add_activity

WEEK = "2026-11-16"


def rows(sql, *args):
    with db.connect() as conn:
        return conn.execute(sql, args).fetchall()


def day(week, dow):
    return rows("SELECT * FROM plan_days WHERE week_start = ? AND dow = ?", week, dow)[0]


def seed_plan(client, start=WEEK, race_date="2026-12-06"):
    """Start date + one A race -> a three-week plan."""
    client.post("/plan/settings", data={"plan_start": start})
    r = client.post("/races", data={"name": "Test Half", "race_date": race_date, "distance_km": "21.0975", "priority": "A"})
    assert r.status_code == 303


def test_empty_app_renders_every_page(client):
    for path in ("/", "/races", "/activities"):
        assert client.get(path).status_code == 200, path


def test_adding_a_race_builds_the_plan_and_fills_race_day(client):
    seed_plan(client)
    assert [r["week_start"] for r in rows("SELECT week_start FROM plan_weeks ORDER BY week_start")] == [
        "2026-11-16", "2026-11-23", "2026-11-30"]
    race_day = day("2026-11-30", 6)
    assert (race_day["kind"], race_day["note"]) == ("race", "Test Half")
    assert client.get("/week/2026-11-30").status_code == 200
    assert client.get("/").status_code == 200


def test_race_validation(client):
    assert client.post("/races", data={"name": "x", "race_date": "not-a-date"}).status_code == 422
    assert client.post("/races/999", data={"name": "x", "race_date": "2027-01-01"}).status_code == 404
    assert client.post("/races/999/delete").status_code == 404


def test_blank_race_name_and_bad_priority_are_cleaned(client):
    client.post("/races", data={"name": "  ", "race_date": "2026-12-06", "priority": "Z"})
    race = rows("SELECT * FROM races")[0]
    assert (race["name"], race["priority"]) == ("Race", "B")


def test_moving_and_deleting_a_race_updates_the_plan(client):
    seed_plan(client)
    race_id = rows("SELECT id FROM races")[0]["id"]
    r = client.post(f"/races/{race_id}", data={"name": "Test Half", "race_date": "2026-12-05", "distance_km": "21.1", "priority": "A"})
    assert r.headers["HX-Refresh"] == "true"
    assert day("2026-11-30", 6)["kind"] == "rest"
    assert day("2026-11-30", 5)["kind"] == "race"
    client.post(f"/races/{race_id}/delete")
    assert day("2026-11-30", 5)["kind"] == "rest"


def test_editing_a_day(client):
    seed_plan(client)
    r = client.post(f"/week/{WEEK}/day/1", data={"kind": "tempo", "distance_km": "12.5", "note": " 3x3km ", "strength_focus": ""})
    assert r.status_code == 200
    d = day(WEEK, 1)
    assert (d["kind"], d["distance_km"], d["note"]) == ("tempo", 12.5, "3x3km")


def test_rest_and_cross_train_force_zero_distance(client):
    seed_plan(client)
    r = client.post(f"/week/{WEEK}/day/2", data={"kind": "rest", "distance_km": "8"})
    assert day(WEEK, 2)["distance_km"] == 0
    assert "distSet" in r.headers["HX-Trigger"]


def test_day_edit_rejects_bad_input(client):
    seed_plan(client)
    assert client.post(f"/week/{WEEK}/day/1", data={"kind": "teleport"}).status_code == 422
    assert client.post(f"/week/{WEEK}/day/9", data={"kind": "easy"}).status_code == 404
    assert client.post("/week/2020-01-06/day/1", data={"kind": "easy"}).status_code == 404


def test_negative_and_garbage_distances_are_sanitised(client):
    seed_plan(client)
    client.post(f"/week/{WEEK}/day/1", data={"kind": "easy", "distance_km": "-5"})
    assert day(WEEK, 1)["distance_km"] == 0
    client.post(f"/week/{WEEK}/day/1", data={"kind": "easy", "distance_km": "abc"})
    assert day(WEEK, 1)["distance_km"] == 0


def test_invalid_strength_focus_is_dropped(client):
    seed_plan(client)
    client.post(f"/week/{WEEK}/day/3", data={"kind": "rest", "strength_focus": "legs"})
    assert day(WEEK, 3)["strength_focus"] == ""
    client.post(f"/week/{WEEK}/day/3", data={"kind": "rest", "strength_focus": "lower"})
    assert day(WEEK, 3)["strength_focus"] == "lower"


def test_week_meta_is_saved(client):
    seed_plan(client)
    assert client.post(f"/week/{WEEK}/meta", data={"phase": " Base ", "notes": "easy week"}).status_code == 204
    wk = rows("SELECT * FROM plan_weeks WHERE week_start = ?", WEEK)[0]
    assert (wk["phase"], wk["notes"]) == ("Base", "easy week")


def test_swap_days(client):
    seed_plan(client)
    client.post(f"/week/{WEEK}/day/0", data={"kind": "easy", "distance_km": "8", "note": "a"})
    client.post(f"/week/{WEEK}/day/4", data={"kind": "long", "distance_km": "20", "note": "b"})
    assert client.post(f"/week/{WEEK}/swap", data={"a": 0, "b": 4}).headers["HX-Refresh"] == "true"
    assert (day(WEEK, 0)["kind"], day(WEEK, 0)["distance_km"], day(WEEK, 0)["note"]) == ("long", 20, "b")
    assert (day(WEEK, 4)["kind"], day(WEEK, 4)["distance_km"], day(WEEK, 4)["note"]) == ("easy", 8, "a")


def test_swap_onto_a_rest_day_moves_the_workout(client):
    seed_plan(client)
    client.post(f"/week/{WEEK}/day/0", data={"kind": "easy", "distance_km": "8"})
    client.post(f"/week/{WEEK}/swap", data={"a": 0, "b": 2})
    assert day(WEEK, 0)["kind"] == "rest" and day(WEEK, 2)["distance_km"] == 8


def test_swap_validation(client):
    seed_plan(client)
    assert client.post(f"/week/{WEEK}/swap", data={"a": 1, "b": 1}).status_code == 422
    assert client.post(f"/week/{WEEK}/swap", data={"a": 1, "b": 7}).status_code == 422
    assert client.post("/week/2020-01-06/swap", data={"a": 1, "b": 2}).status_code == 404


def test_copy_previous_week(client):
    seed_plan(client)
    client.post(f"/week/{WEEK}/day/1", data={"kind": "tempo", "distance_km": "10", "note": "t"})
    client.post(f"/week/{WEEK}/day/5", data={"kind": "long", "distance_km": "18", "strength_focus": "full"})
    assert client.post("/week/2026-11-23/copy-previous").status_code == 200
    d = day("2026-11-23", 5)
    assert (d["kind"], d["distance_km"], d["strength_focus"]) == ("long", 18, "full")
    assert day("2026-11-23", 1)["note"] == "t"


def test_copy_previous_needs_a_previous_week(client):
    seed_plan(client)
    assert client.post(f"/week/{WEEK}/copy-previous").status_code == 404


def test_clear_week(client):
    seed_plan(client)
    client.post(f"/week/{WEEK}/day/1", data={"kind": "easy", "distance_km": "8", "note": "x"})
    client.post(f"/week/{WEEK}/clear")
    assert day(WEEK, 1)["kind"] == "rest" and day(WEEK, 1)["distance_km"] == 0 and day(WEEK, 1)["note"] == ""


# ---- manual activities and overrides ---------------------------------------------------
def test_manual_activity_lifecycle(client):
    seed_plan(client)
    r = client.post(f"/week/{WEEK}/manual", data={"day": "2026-11-18", "kind": "run", "distance_km": "6.5", "minutes": "40", "avg_hr": "150"})
    assert r.headers["HX-Refresh"] == "true"
    act = rows("SELECT * FROM activities")[0]
    assert act["id"] < 0 and act["source"] == "manual" and act["category"] == "run"
    assert act["distance_m"] == 6500 and act["duration_s"] == 2400 and act["avg_hr"] == 150
    client.post(f"/week/{WEEK}/manual", data={"day": "2026-11-19", "kind": "strength"})
    ids = [a["id"] for a in rows("SELECT id FROM activities ORDER BY id")]
    assert len(set(ids)) == 2 and all(i < 0 for i in ids)  # unique negative ids
    assert client.post(f"/activity/{act['id']}/delete").status_code == 200
    assert len(rows("SELECT id FROM activities")) == 1


def test_manual_activity_validation(client):
    seed_plan(client)
    assert client.post(f"/week/{WEEK}/manual", data={"day": "2026-12-25", "kind": "run"}).status_code == 422  # outside week
    assert client.post(f"/week/{WEEK}/manual", data={"day": "2026-11-18", "kind": "swim"}).status_code == 422
    assert client.post(f"/week/{WEEK}/manual", data={"day": "nope", "kind": "run"}).status_code == 422
    assert rows("SELECT id FROM activities") == []


def test_only_manual_activities_can_be_deleted(client, conn):
    add_activity(conn, 123, "2026-11-17")
    assert client.post("/activity/123/delete").status_code == 404
    assert client.post("/activity/-77/delete").status_code == 404
    assert len(rows("SELECT id FROM activities")) == 1


def test_category_override_and_reset(client, conn):
    add_activity(conn, 5, "2026-11-17", "treadmill_running")
    client.post("/activity/5/category", data={"category": "extra"})
    assert rows("SELECT category_override FROM activities WHERE id = 5")[0][0] == "extra"
    client.post("/activity/5/category", data={"category": ""})
    assert rows("SELECT category_override FROM activities WHERE id = 5")[0][0] is None
    client.post("/activity/5/category", data={"category": "bogus"})
    assert rows("SELECT category_override FROM activities WHERE id = 5")[0][0] is None


def test_strength_focus_on_activity(client, conn):
    add_activity(conn, 6, "2026-11-17", "strength_training", km=0)
    assert client.post("/activity/6/focus", data={"focus": "lower"}).status_code == 204
    assert rows("SELECT strength_focus FROM activities WHERE id = 6")[0][0] == "lower"
    client.post("/activity/6/focus", data={"focus": "nonsense"})
    assert rows("SELECT strength_focus FROM activities WHERE id = 6")[0][0] is None


def test_activity_day_override(client, conn):
    add_activity(conn, 7, "2026-11-17")
    client.post("/activity/7/day", data={"day": "2026-11-18"})
    assert rows("SELECT day_override FROM activities WHERE id = 7")[0][0] == "2026-11-18"
    client.post("/activity/7/day", data={"day": "2026-11-17"})  # same as recorded -> no override
    assert rows("SELECT day_override FROM activities WHERE id = 7")[0][0] is None
    assert client.post("/activity/7/day", data={"day": "garbage"}).status_code == 422
    assert client.post("/activity/404/day", data={"day": "2026-11-18"}).status_code == 404


def test_activities_page_filters_by_category(client, conn):
    add_activity(conn, 1, "2026-11-17", "running")
    add_activity(conn, 2, "2026-11-17", "cycling")
    assert client.get("/activities?category=run").status_code == 200
    assert client.get("/activities?category=bogus").status_code == 200


def test_dashboard_and_week_pages_render_with_data(client, conn):
    seed_plan(client)
    client.post(f"/week/{WEEK}/day/1", data={"kind": "easy", "distance_km": "10"})
    add_activity(conn, 1, "2026-11-17", km=10)
    assert client.get("/").status_code == 200
    assert client.get(f"/week/{WEEK}").status_code == 200
    assert client.get("/week/1999-01-04").status_code == 404
    assert client.get("/races").status_code == 200
