"""Schema migrations and Garmin sync (with a fake Garmin client: no network, no credentials)."""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from app import db, garmin
from tests.conftest import add_activity


# ---- migrations ----------------------------------------------------------------------
def test_fresh_database_is_current_and_makes_no_backup(tmp_db):
    db.init_db()
    with db.connect() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert list(tmp_db.parent.glob("*backup*")) == []


def test_init_db_is_idempotent(tmp_db):
    db.init_db()
    db.init_db()


def test_v2_database_migrates_to_v3_keeping_data_and_backing_up(tmp_db):
    old = sqlite3.connect(tmp_db)
    old.executescript(
        """
        CREATE TABLE races (id INTEGER PRIMARY KEY, name TEXT NOT NULL, race_date TEXT NOT NULL, goal TEXT);
        INSERT INTO races(name, race_date) VALUES('Dublin Half Marathon', '2027-02-21'), ('Manchester Marathon', '2027-04-18');
        CREATE TABLE activities (
            id INTEGER PRIMARY KEY, start_local TEXT NOT NULL, day TEXT NOT NULL, type_key TEXT NOT NULL,
            name TEXT NOT NULL DEFAULT '', distance_m REAL NOT NULL DEFAULT 0, duration_s REAL NOT NULL DEFAULT 0,
            avg_hr REAL, category TEXT NOT NULL, category_override TEXT, strength_focus TEXT, raw_json TEXT NOT NULL
        );
        INSERT INTO activities(id, start_local, day, type_key, category, raw_json)
            VALUES(7, '2026-11-17 07:00:00', '2026-11-17', 'running', 'run', '{}');
        PRAGMA user_version = 2;
        """
    )
    old.commit()
    old.close()

    db.init_db()

    with db.connect() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
        races = {r["name"]: r for r in conn.execute("SELECT * FROM races")}
        assert races["Manchester Marathon"]["priority"] == "A"  # latest race becomes the goal race
        assert races["Dublin Half Marathon"]["priority"] == "B"
        assert races["Dublin Half Marathon"]["distance_km"] == pytest.approx(21.0975)
        assert races["Manchester Marathon"]["distance_km"] == pytest.approx(42.195)
        act = conn.execute("SELECT * FROM activities WHERE id = 7").fetchone()
        assert act["source"] == "garmin" and act["day_override"] is None
    assert (tmp_db.parent / "dashboard.pre-v3-backup.db").exists()


# ---- sync --------------------------------------------------------------------------------
def garmin_activity(id, day, type_key="running", km=10.0, seconds=3000, hr=150):
    return {
        "activityId": id,
        "startTimeLocal": f"{day} 07:30:00",
        "activityType": {"typeKey": type_key},
        "activityName": f"Run {id}",
        "distance": km * 1000,
        "duration": seconds,
        "averageHR": hr,
    }


class FakeGarmin:
    def __init__(self, activities):
        self.activities = activities
        self.calls = []

    def get_activities_by_date(self, start, end):
        self.calls.append((start, end))
        return self.activities


def count(conn):
    return conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0]


def test_sync_inserts_and_categorises(conn):
    api = FakeGarmin([garmin_activity(1, "2026-11-17"), garmin_activity(2, "2026-11-18", "strength_training", 0),
                      garmin_activity(3, "2026-11-19", "cycling", 20)])
    result = garmin.sync(conn, api)
    assert result["fetched"] == 3 and result["new"] == 3
    cats = {r["id"]: r["category"] for r in conn.execute("SELECT id, category FROM activities")}
    assert cats == {1: "run", 2: "strength", 3: "extra"}
    assert conn.execute("SELECT day FROM activities WHERE id = 1").fetchone()["day"] == "2026-11-17"


def test_resync_updates_without_duplicating_and_keeps_manual_fields(conn):
    garmin.sync(conn, FakeGarmin([garmin_activity(1, "2026-11-17", km=10)]))
    conn.execute(
        "UPDATE activities SET category_override='extra', strength_focus='upper', day_override='2026-11-18' WHERE id=1"
    )
    conn.commit()
    result = garmin.sync(conn, FakeGarmin([garmin_activity(1, "2026-11-17", km=12.5)]))  # edited in Garmin Connect
    assert result["new"] == 0 and count(conn) == 1
    row = conn.execute("SELECT * FROM activities WHERE id = 1").fetchone()
    assert row["distance_m"] == 12500  # Garmin's values refreshed...
    assert (row["category_override"], row["strength_focus"], row["day_override"]) == ("extra", "upper", "2026-11-18")


def test_sync_never_touches_manual_activities(conn):
    add_activity(conn, -1, "2026-11-17", km=7)
    garmin.sync(conn, FakeGarmin([garmin_activity(5, "2026-11-17")]))
    assert conn.execute("SELECT distance_m, source FROM activities WHERE id = -1").fetchone()[:] == (7000, "manual")


def test_sync_window_overlaps_recent_days(conn):
    conn.execute("INSERT INTO meta(key, value) VALUES('last_sync_date', '2026-11-10')")
    api = FakeGarmin([])
    garmin.sync(conn, api)
    assert api.calls[0][0] == "2026-11-03"  # last sync minus the 7-day overlap
    assert api.calls[0][1] == date.today().isoformat()


def test_first_sync_starts_before_the_plan(conn):
    conn.execute("INSERT INTO plan_weeks(week_start, week_no) VALUES('2026-11-16', 1)")
    api = FakeGarmin([])
    garmin.sync(conn, api)
    assert api.calls[0][0] == "2026-11-02"  # plan start minus 14 days of history


def test_sync_records_when_it_ran(conn):
    garmin.sync(conn, FakeGarmin([]))
    assert db.get_meta(conn, "last_sync_date") == date.today().isoformat()
    assert db.get_meta(conn, "last_sync_at")


def test_run_sync_records_login_failure_for_the_banner(tmp_db, monkeypatch):
    db.init_db()

    def no_login():
        raise garmin.GarminAuthError("Garmin login expired or missing.")

    monkeypatch.setattr(garmin, "client_from_cache", no_login)
    with pytest.raises(garmin.GarminAuthError):
        garmin.run_sync()
    with db.connect() as conn:
        assert "expired" in db.get_meta(conn, "last_sync_error")

    monkeypatch.setattr(garmin, "client_from_cache", lambda: FakeGarmin([]))
    garmin.run_sync()
    with db.connect() as conn:
        assert db.get_meta(conn, "last_sync_error") == ""  # cleared after a good sync


def test_run_sync_skips_when_one_is_already_running(tmp_db):
    db.init_db()
    assert garmin._SYNC_LOCK.acquire(blocking=False)
    try:
        assert garmin.run_sync() == {"skipped": "busy"}
    finally:
        garmin._SYNC_LOCK.release()
