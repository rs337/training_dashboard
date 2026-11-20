"""Rules for categories and the planned-workout vocabulary."""

from __future__ import annotations

# --- Garmin activity -> tracking category -----------------------------------
RUN_TYPES = {
    "running",
    "trail_running",
    "treadmill_running",
    "track_running",
    "indoor_running",
    "virtual_run",
}
STRENGTH_TYPES = {"strength_training"}

CATEGORIES = ("run", "strength", "extra")  # extra = stored, never scored


def category_for(type_key: str) -> str:
    if type_key in RUN_TYPES:
        return "run"
    if type_key in STRENGTH_TYPES:
        return "strength"
    return "extra"


STRENGTH_FOCUS = ("upper", "lower", "full")

# --- Planned workouts ----------------------------------------------------------
PLAN_KINDS = [
    ("rest", "Rest"),
    ("easy", "Easy run"),
    ("recovery", "Recovery run"),
    ("long", "Long run"),
    ("tempo", "Tempo"),
    ("intervals", "Intervals"),
    ("hills", "Hills"),
    ("race", "Race"),
    ("cross", "Cross-train"),
]
KIND_LABEL = dict(PLAN_KINDS)
NO_DISTANCE_KINDS = {"rest", "cross"}  # nothing to run, so distance is forced to 0
