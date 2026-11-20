"""Garmin Connect access: cached-token login and activity sync.

    uv run python -m app.garmin login            # one-off, prompts for password / MFA
    uv run python -m app.garmin sync             # same thing the Refresh button does
    uv run python -m app.garmin list --since 2026-11-16
"""

from __future__ import annotations

import argparse
import getpass
import json
import sqlite3
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

from garminconnect import Garmin

from .classify import category_for
from .db import connect, get_meta, init_db, set_meta

TOKEN_DIR = Path.home() / ".garminconnect"  # outside the repo on purpose
OVERLAP_DAYS = 7  # re-fetch recent days so edits made in Garmin Connect are picked up
_SYNC_LOCK = threading.Lock()  # the button, page-open check and background loop share one sync at a time


class GarminAuthError(Exception):
    """No usable cached login. Fix: run `uv run python -m app.garmin login`."""


def client_from_cache() -> Garmin:
    api = Garmin()
    try:
        api.login(str(TOKEN_DIR))
    except Exception as exc:  # noqa: BLE001 - library raises several types
        raise GarminAuthError(
            "Garmin login expired or missing. In a terminal run: uv run python -m app.garmin login"
        ) from exc
    return api


def login_interactive() -> Garmin:
    TOKEN_DIR.mkdir(mode=0o700, exist_ok=True)
    email = input("Garmin email: ").strip()
    password = getpass.getpass("Garmin password: ")
    api = Garmin(
        email=email,
        password=password,
        prompt_mfa=lambda: input("Garmin MFA code: ").strip(),
    )
    api.login(str(TOKEN_DIR))
    print(f"Logged in. Tokens cached in {TOKEN_DIR}")
    return api


def _plan_start(conn: sqlite3.Connection) -> date:
    row = conn.execute("SELECT MIN(week_start) AS d FROM plan_weeks").fetchone()
    return date.fromisoformat(row["d"]) if row and row["d"] else date.today() - timedelta(days=28)


def sync(conn: sqlite3.Connection, api: Garmin | None = None) -> dict:
    """Pull activities from Garmin and upsert them. Manual fields are never overwritten."""
    api = api or client_from_cache()
    last = get_meta(conn, "last_sync_date")
    if last:
        since = min(date.fromisoformat(last) - timedelta(days=OVERLAP_DAYS), date.today())
    else:
        since = _plan_start(conn) - timedelta(days=14)  # a little history before the plan
    since = max(since, date(2000, 1, 1))
    today = date.today()

    activities = api.get_activities_by_date(since.isoformat(), today.isoformat())
    before = conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0]
    for a in activities:
        type_key = (a.get("activityType") or {}).get("typeKey", "other")
        start_local = a["startTimeLocal"]
        conn.execute(
            """
            INSERT INTO activities(id, start_local, day, type_key, name, distance_m,
                                   duration_s, avg_hr, category, raw_json)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                start_local = excluded.start_local, day = excluded.day,
                type_key = excluded.type_key, name = excluded.name,
                distance_m = excluded.distance_m, duration_s = excluded.duration_s,
                avg_hr = excluded.avg_hr, category = excluded.category,
                raw_json = excluded.raw_json
            """,
            (
                a["activityId"],
                start_local,
                start_local[:10],
                type_key,
                a.get("activityName") or "",
                a.get("distance") or 0.0,
                a.get("duration") or 0.0,
                a.get("averageHR"),
                category_for(type_key),
                json.dumps(a),
            ),
        )
    after = conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0]
    set_meta(conn, "last_sync_date", today.isoformat())
    set_meta(conn, "last_sync_at", datetime.now().strftime("%Y-%m-%d %H:%M"))
    return {"fetched": len(activities), "new": after - before, "since": since.isoformat()}


def run_sync() -> dict:
    """Sync with its own connection, one at a time. Used by the button, page-open check and background loop.

    Records the outcome in meta ('last_sync_error') so the UI can show a banner when the login has expired.
    Returns {"skipped": "busy"} if another sync is already running.
    """
    if not _SYNC_LOCK.acquire(blocking=False):
        return {"skipped": "busy"}
    conn = connect()
    try:
        try:
            result = sync(conn)
            set_meta(conn, "last_sync_error", "")
            conn.commit()
            return result
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            message = str(exc) if isinstance(exc, GarminAuthError) else f"Sync failed: {type(exc).__name__}: {exc}"
            set_meta(conn, "last_sync_error", message)
            conn.commit()
            raise
    finally:
        conn.close()
        _SYNC_LOCK.release()


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.garmin")
    parser.add_argument("command", choices=["login", "sync", "list"])
    parser.add_argument("--since", help="YYYY-MM-DD (list only; default 14 days ago)")
    args = parser.parse_args()

    if args.command == "login":
        login_interactive()
        return

    init_db()
    if args.command == "sync":
        with connect() as conn:
            print(sync(conn))
        return

    since = args.since or (date.today() - timedelta(days=14)).isoformat()
    api = client_from_cache()
    for a in sorted(api.get_activities_by_date(since, date.today().isoformat()), key=lambda x: x["startTimeLocal"]):
        km = (a.get("distance") or 0) / 1000
        print(f"{a['startTimeLocal'][:16]}  {a['activityType']['typeKey']:<24}{km:6.2f} km  {a.get('activityName', '')}")


if __name__ == "__main__":
    main()
