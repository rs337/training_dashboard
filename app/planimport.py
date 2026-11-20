"""Seed the plan skeleton from the spreadsheet: week dates, phases and the two races.

The spreadsheet's daily workouts were placeholder (and in miles), so they are
NOT imported: every day starts as Rest and you plan weeks in the app, in km.

    uv run python -m app.planimport            # import if the plan is empty
    uv run python -m app.planimport --reset    # wipe weeks/days/races and re-seed
"""

from __future__ import annotations

import re
import sqlite3
import sys
from datetime import date, timedelta

import openpyxl

from .db import ROOT, blank_days, connect, init_db

TEMPLATE = ROOT / "plan-template.xlsx"
FIRST_ROW, LAST_ROW = 7, 28
_DATE_FORMULA = re.compile(r"DATE\(\s*(\d{4})\s*,\s*(\d{1,2})\s*,\s*(\d{1,2})\s*\)")


def _formula_date(value: object) -> date | None:
    match = _DATE_FORMULA.search(str(value or ""))
    return date(*map(int, match.groups())) if match else None


def import_plan(conn: sqlite3.Connection) -> None:
    ws = openpyxl.load_workbook(TEMPLATE).active

    first_monday = _formula_date(ws[f"A{FIRST_ROW}"].value)
    if first_monday is None or first_monday.weekday() != 0:
        raise ValueError("A7 should be =DATE(y,m,d) for the first Monday of the plan")

    for i, row in enumerate(range(FIRST_ROW, LAST_ROW + 1)):
        week_start = (first_monday + timedelta(days=7 * i)).isoformat()
        conn.execute(
            "INSERT INTO plan_weeks(week_start, week_no, phase) VALUES(?,?,?)",
            (week_start, i + 1, ws[f"J{row}"].value or ""),
        )
        blank_days(conn, week_start)

    # Race names from the header cells, dates from their =...DATE(y,m,d) formulas.
    for name_cell, date_cell in (("A2", "A3"), ("D2", "D3")):
        race_date = _formula_date(ws[date_cell].value)
        if race_date:
            conn.execute(
                "INSERT INTO races(name, race_date) VALUES(?,?)",
                (str(ws[name_cell].value).title(), race_date.isoformat()),
            )


def maybe_import(conn: sqlite3.Connection) -> None:
    has_plan = conn.execute("SELECT COUNT(*) FROM plan_weeks").fetchone()[0]
    if not has_plan and TEMPLATE.exists():
        import_plan(conn)


def main() -> None:
    init_db()
    with connect() as conn:
        if "--reset" in sys.argv:
            for table in ("plan_days", "plan_weeks", "races"):
                conn.execute(f"DELETE FROM {table}")
        maybe_import(conn)
        weeks = conn.execute("SELECT COUNT(*) FROM plan_weeks").fetchone()[0]
    print(f"Plan has {weeks} weeks.")


if __name__ == "__main__":
    main()
