"""healthsync.py — getting steps, sleep, weight and the rest in from a phone or band.

There is no universal free way to read a fitness tracker server-side, so this is deliberately
a dumb, open door rather than a set of integrations:

  * Apple Health (any iPhone) — the free "Health Auto Export" app POSTs its bundle here on a
    schedule. Because most Android bands (Fitbit, Garmin, Mi Band, Samsung) can sync into
    Apple Health or an automation app, this one endpoint covers far more than iPhones.
  * Anything else — Tasker, Health Sync, IFTTT, a Shortcut, or a two-line script can POST
    flat JSON like {"steps": 6200, "sleep_hours": 7.5}.
  * Nothing at all — the person just tells the bot, or photographs their scale.

Field names are matched loosely, because every exporter names things differently and a user
should not have to care. Unknown fields are ignored rather than rejected.

Adding a real OAuth integration later (Fitbit, Garmin) means writing a function that returns
{canonical_key: value} and calling nutrition.record_health with it. Nothing else changes.
"""
from datetime import datetime

from . import accounts
from .db import HEALTH

ALIASES = {
    "bodyfat": "body_fat", "body_fat_percentage": "body_fat", "fat_percentage": "body_fat",
    "body_fat_pct": "body_fat", "bodyfatpercentage": "body_fat",
    "step_count": "steps", "step": "steps", "steps_count": "steps",
    "water": "water_ml", "water_intake": "water_ml", "dietary_water": "water_ml",
    "sleep": "sleep_hours", "sleep_analysis": "sleep_hours", "sleep_h": "sleep_hours",
    "sleep_duration": "sleep_hours", "time_asleep": "sleep_hours",
    "weight": "weight_kg", "body_mass": "weight_kg", "weight_body_mass": "weight_kg",
    "resting_heart_rate": "resting_hr", "resting_heartrate": "resting_hr", "rhr": "resting_hr",
    "active_energy": "active_kcal", "active_energy_burned": "active_kcal",
    "active_calories": "active_kcal", "calories_burned": "active_kcal",
}

KNOWN = {k for k, _, _, _ in HEALTH}
# Cumulative metrics add up over the day; the rest are point-in-time, so the latest wins.
CUMULATIVE = {"steps", "active_kcal", "water_ml"}


def canonical(key: str) -> str | None:
    norm = (key or "").strip().lower().replace(" ", "_")
    canon = norm if norm in KNOWN else ALIASES.get(norm)
    return canon if canon in KNOWN else None


def parse_auto_export(payload: dict, today: str) -> dict:
    """Flatten a Health Auto Export bundle into {canonical_key: value} for `today`.
    Returns {} when the payload isn't that shape, so the flat-JSON path can take over."""
    metrics_list = (payload.get("data") or {}).get("metrics")
    if not isinstance(metrics_list, list):
        return {}

    def point_value(pt):  # the export uses 'qty'; sleep points use 'asleep'/'totalSleep'
        for k in ("qty", "totalSleep", "asleep", "Asleep"):
            if isinstance(pt, dict) and pt.get(k) is not None:
                try:
                    return float(pt[k])
                except (TypeError, ValueError):
                    return None
        return None

    out = {}
    for m in metrics_list:
        canon = canonical(m.get("name", ""))
        if not canon:
            continue
        pts = m.get("data") or []
        today_pts = [p for p in pts if str(p.get("date", "")).startswith(today)]
        if canon in CUMULATIVE:
            vals = [v for v in (point_value(p) for p in today_pts) if v is not None]
            if not vals:
                continue
            value = sum(vals)
        else:  # latest reading; fall back to the most recent overall if none today
            src = sorted(today_pts or pts, key=lambda p: str(p.get("date", "")))
            value = next((point_value(p) for p in reversed(src)
                          if point_value(p) is not None), None)
            if value is None:
                continue
        if canon == "water_ml" and str(m.get("units", "")).strip().lower() in (
                "l", "liter", "litre", "litres", "liters"):
            value *= 1000  # water is always stored in ml
        out[canon] = value
    return out


def parse_flat(payload: dict) -> dict:
    """{"steps": 6200, "Body Fat Percentage": 22} -> {canonical_key: value}."""
    out = {}
    for key, value in (payload or {}).items():
        if key in ("token", "user"):
            continue
        canon = canonical(key)
        if not canon:
            continue
        if canon == "water_ml":
            # A source sending water in litres ("water": 2.5) would be stored as 2.5ml.
            # Nobody logs under 20ml of water, so a tiny number here means litres.
            try:
                if 0 < float(value) < 20:
                    value = float(value) * 1000
            except (TypeError, ValueError):
                pass
        out[canon] = value
    return out


def parse(payload: dict, user: str) -> dict:
    """Whatever shape arrived -> {canonical_key: value}, in the user's own day."""
    today = accounts.now_local(user).strftime("%Y-%m-%d")
    return parse_auto_export(payload, today) or parse_flat(payload)


def stamp_sync(user: str):
    from . import db
    db.set_profile(user, "last_health_sync", datetime.now().isoformat(timespec="seconds"))
