"""Every test gets its own empty SQLite file; the real data/dashboard.db is never touched."""

from __future__ import annotations

import os

os.environ["SYNC_INTERVAL_MIN"] = "0"  # read at import time: no background Garmin sync in tests

import pytest
from fastapi.testclient import TestClient

from app import auth, db, main, planimport


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(planimport, "TEMPLATE", tmp_path / "no-template.xlsx")  # start with an empty plan
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    monkeypatch.delenv("COOKIE_SECURE", raising=False)
    auth.reset_throttle()
    return tmp_path / "test.db"


@pytest.fixture
def conn(tmp_db):
    db.init_db()
    connection = db.connect()
    yield connection
    connection.close()


@pytest.fixture
def client(tmp_db):
    with TestClient(main.app, follow_redirects=False) as c:  # context manager runs startup (init_db)
        yield c


def add_activity(conn, id, day, type_key="running", km=10.0, minutes=60.0, category=None, **extra):
    """Insert an activity row the way sync would."""
    from app.classify import category_for

    conn.execute(
        "INSERT INTO activities(id, start_local, day, type_key, name, distance_m, duration_s, category, raw_json, source)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (id, f"{day} 07:00:00", day, type_key, f"act {id}", km * 1000, minutes * 60,
         category or category_for(type_key), "{}", "manual" if id < 0 else "garmin"),
    )
    for column, value in extra.items():
        conn.execute(f"UPDATE activities SET {column} = ? WHERE id = ?", (value, id))
    conn.commit()
