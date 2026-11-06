"""A full breakdown of one recorded activity, built from Garmin's per-activity endpoints.

Pure functions over Garmin's responses, so everything can be tested offline:

- `activity_summary`: the session as a whole, including training effect, load and RPE
- `lap_rows`: every lap, which for a structured workout is every step
- `interval_summary`: how the work reps compared, when the session had work and recovery laps
- `zone_rows`: time in each heart-rate zone
- `parse_series` / `series_table`: the second-by-second recording, and a downsampled table of it
- `hr_drift`: heart rate against pace, first half vs. second half
- `compliance`: each planned workout step's target against what was run
"""

from __future__ import annotations

import math
from typing import Any

from .metrics import SPORT_NAMES, drop_empty, format_duration

# Lap intensity types Garmin uses for the hard part of a structured workout.
WORK_TYPES = {"interval", "active"}
REST_TYPES = {"recovery", "rest"}

# Below this speed the athlete is standing or walking, so pace means nothing.
_MOVING_MPS = 1.0
# Heart rate lags effort, so the first minutes would bias a drift comparison.
_DRIFT_SKIP_S = 5 * 60
_DRIFT_MIN_S = 20 * 60

FEEL = {0: "very weak", 25: "weak", 50: "normal", 75: "strong", 100: "very strong"}


def _round(value: Any, digits: int = 1) -> float | None:
    return round(value, digits) if isinstance(value, int | float) else None


def _int(value: Any) -> int | None:
    return round(value) if isinstance(value, int | float) else None


def pace_seconds(speed_mps: Any) -> float | None:
    """Seconds per km from metres per second, or None when not really moving."""
    if not isinstance(speed_mps, int | float) or speed_mps < _MOVING_MPS:
        return None
    return 1000.0 / speed_mps


def format_pace(seconds_per_km: float | None) -> str | None:
    return format_duration(seconds_per_km) + "/km" if seconds_per_km else None


def _first(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if data.get(key) is not None:
            return data[key]
    return None


def activity_summary(activity: dict[str, Any]) -> dict[str, Any]:
    """The session as a whole, from Garmin's single-activity response."""
    summary = activity.get("summaryDTO") or {}
    type_key = (activity.get("activityTypeDTO") or {}).get("typeKey") or "other"
    sport = SPORT_NAMES.get(type_key, "other")
    distance = summary.get("distance")
    duration = summary.get("duration")
    moving = summary.get("movingDuration")

    pace = None
    if sport in ("run", "walk", "hike") and distance and (moving or duration):
        pace = format_duration((moving or duration) / (distance / 1000)) + "/km"

    rpe = summary.get("directWorkoutRpe")
    if isinstance(rpe, int | float) and rpe > 10:
        rpe = rpe / 10  # Garmin stores RPE 1-10 as 10-100

    return drop_empty(
        {
            "id": activity.get("activityId"),
            "name": activity.get("activityName"),
            "sport": sport,
            "start_local": summary.get("startTimeLocal"),
            "distance_km": round(distance / 1000, 2) if distance else None,
            "duration": format_duration(duration) if duration else None,
            "moving_duration": format_duration(moving) if moving else None,
            "pace": pace,
            "avg_hr": _int(summary.get("averageHR")),
            "max_hr": _int(summary.get("maxHR")),
            "avg_cadence": _int(_first(summary, "averageRunCadence", "averageBikeCadence")),
            "avg_power_w": _int(summary.get("averagePower")),
            "normalized_power_w": _int(summary.get("normalizedPower")),
            "elevation_gain_m": _round(summary.get("elevationGain")),
            "elevation_loss_m": _round(summary.get("elevationLoss")),
            "ground_contact_ms": _int(summary.get("groundContactTime")),
            "stride_length_m": _round(_meters(summary.get("strideLength")), 2),
            "vertical_oscillation_cm": _round(summary.get("verticalOscillation")),
            "vertical_ratio_pct": _round(summary.get("verticalRatio")),
            "aerobic_training_effect": _round(summary.get("trainingEffect")),
            "anaerobic_training_effect": _round(summary.get("anaerobicTrainingEffect")),
            "training_effect_label": summary.get("trainingEffectLabel"),
            "training_load": _round(summary.get("activityTrainingLoad")),
            "rpe": _round(rpe),
            "feel": FEEL.get(summary.get("directWorkoutFeel")),
            "calories": _int(summary.get("calories")),
        }
    )


def _meters(stride: Any) -> float | None:
    """Garmin reports stride length in centimetres."""
    return stride / 100 if isinstance(stride, int | float) else None


def _lap_type(lap: dict[str, Any]) -> str:
    return str(lap.get("intensityType") or "active").lower()


def lap_rows(splits: dict[str, Any] | None) -> list[dict[str, Any]]:
    """One row per lap. For a structured workout each step is its own lap."""
    rows = []
    for number, lap in enumerate((splits or {}).get("lapDTOs") or [], start=1):
        distance = lap.get("distance")
        rows.append(
            drop_empty(
                {
                    "lap": number,
                    "type": _lap_type(lap),
                    "duration": format_duration(lap["duration"]) if lap.get("duration") else None,
                    "distance_km": round(distance / 1000, 2) if distance else None,
                    "pace": format_pace(pace_seconds(lap.get("averageSpeed"))),
                    "avg_hr": _int(lap.get("averageHR")),
                    "max_hr": _int(lap.get("maxHR")),
                    "avg_cadence": _int(_first(lap, "averageRunCadence", "averageBikeCadence")),
                    "avg_power_w": _int(lap.get("averagePower")),
                    "elevation_gain_m": _round(lap.get("elevationGain")),
                    "elevation_loss_m": _round(lap.get("elevationLoss")),
                    "ground_contact_ms": _int(lap.get("groundContactTime")),
                    "stride_length_m": _round(_meters(lap.get("strideLength")), 2),
                }
            )
        )
    return rows


def interval_summary(splits: dict[str, Any] | None) -> dict[str, Any] | None:
    """How the work reps compared. None unless the session alternated work and recovery."""
    laps = (splits or {}).get("lapDTOs") or []
    if not any(_lap_type(lap) in REST_TYPES for lap in laps):
        return None  # auto-laps of a steady run, not reps
    reps = [lap for lap in laps if _lap_type(lap) in WORK_TYPES]
    if len(reps) < 2:
        return None

    paces = [pace_seconds(lap.get("averageSpeed")) for lap in reps]
    hrs = [lap.get("averageHR") for lap in reps]
    known_paces = [p for p in paces if p]
    known_hrs = [h for h in hrs if h]
    result: dict[str, Any] = {"reps": len(reps)}
    if known_paces:
        result.update(
            {
                "avg_pace": format_pace(sum(known_paces) / len(known_paces)),
                "fastest_pace": format_pace(min(known_paces)),
                "slowest_pace": format_pace(max(known_paces)),
                "pace_spread_s_per_km": round(max(known_paces) - min(known_paces)),
            }
        )
        if paces[0] and paces[-1]:
            # Positive means the last rep was slower than the first.
            result["fade_s_per_km"] = round(paces[-1] - paces[0])
    if known_hrs:
        result["avg_hr"] = round(sum(known_hrs) / len(known_hrs))
        if hrs[0] and hrs[-1]:
            result["hr_first_rep"] = round(hrs[0])
            result["hr_last_rep"] = round(hrs[-1])
            result["hr_rise_bpm"] = round(hrs[-1] - hrs[0])
    return result


def zone_rows(zones: Any) -> list[dict[str, Any]]:
    """Time in each heart-rate zone, with its bpm range and share of the session."""
    entries = sorted(
        (z for z in zones or [] if isinstance(z, dict) and z.get("zoneNumber") is not None),
        key=lambda z: z["zoneNumber"],
    )
    total = sum(z.get("secsInZone") or 0 for z in entries)
    rows = []
    for i, zone in enumerate(entries):
        low = zone.get("zoneLowBoundary")
        high = entries[i + 1].get("zoneLowBoundary") if i + 1 < len(entries) else None
        secs = zone.get("secsInZone") or 0
        bpm = None
        if low is not None:
            bpm = f"{low}-{high - 1}" if high is not None else f"{low}+"
        rows.append(
            drop_empty(
                {
                    "zone": zone["zoneNumber"],
                    "bpm": bpm,
                    "time": format_duration(secs),
                    "percent": round(100 * secs / total, 1) if total else None,
                }
            )
        )
    return rows


# Garmin's names for the recorded channels, first match wins.
_CHANNELS = {
    "t": ("sumDuration", "sumElapsedDuration"),
    "distance": ("sumDistance",),
    "hr": ("directHeartRate",),
    "speed": ("directSpeed", "directEnhancedSpeed"),
    "elevation": ("directElevation", "directEnhancedElevation"),
    "power": ("directPower",),
}


def parse_series(details: dict[str, Any] | None) -> list[dict[str, float]]:
    """The recording as a list of samples keyed t, distance, hr, speed, elevation, power."""
    if not details:
        return []
    index = {
        d.get("key"): d.get("metricsIndex")
        for d in details.get("metricDescriptors") or []
        if d.get("metricsIndex") is not None
    }
    columns = {}
    for name, keys in _CHANNELS.items():
        column = next((index[k] for k in keys if k in index), None)
        if column is not None:
            columns[name] = column
    if "t" not in columns:
        return []

    samples = []
    for row in details.get("activityDetailMetrics") or []:
        values = row.get("metrics") or []
        sample = {}
        for name, column in columns.items():
            value = values[column] if column < len(values) else None
            if isinstance(value, int | float):
                sample[name] = float(value)
        if "t" in sample:
            samples.append(sample)
    samples.sort(key=lambda s: s["t"])
    return samples


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def auto_interval(samples: list[dict[str, float]]) -> int:
    """A bucket size, in multiples of 30 s, that keeps the table near 60 rows."""
    if not samples:
        return 60
    minutes = (samples[-1]["t"] - samples[0]["t"]) / 60
    return max(30, math.ceil(minutes / 30) * 30)


def series_table(samples: list[dict[str, float]], interval_s: int) -> dict[str, Any] | None:
    """The recording averaged into fixed time buckets, as compact columns and rows."""
    if not samples:
        return None
    buckets: dict[int, list[dict[str, float]]] = {}
    for sample in samples:
        buckets.setdefault(int(sample["t"] // interval_s), []).append(sample)

    has = {name: any(name in s for s in samples) for name in _CHANNELS}
    columns = ["min"]
    if has["distance"]:
        columns.append("km")
    labels = (("hr", "hr"), ("speed", "pace"), ("elevation", "elev_m"), ("power", "power_w"))
    for name, label in labels:
        if has[name]:
            columns.append(label)

    rows = []
    for key in sorted(buckets):
        group = buckets[key]
        row: list[Any] = [round((key + 1) * interval_s / 60, 1)]
        if has["distance"]:
            last = [s["distance"] for s in group if "distance" in s]
            row.append(round(last[-1] / 1000, 2) if last else None)
        if has["hr"]:
            row.append(_int(_mean([s["hr"] for s in group if s.get("hr")])))
        if has["speed"]:
            speed = _mean([s["speed"] for s in group if "speed" in s])
            row.append(format_pace(pace_seconds(speed)))
        if has["elevation"]:
            row.append(_int(_mean([s["elevation"] for s in group if "elevation" in s])))
        if has["power"]:
            row.append(_int(_mean([s["power"] for s in group if s.get("power")])))
        rows.append(row)
    return {"interval_s": interval_s, "columns": columns, "rows": rows}


def hr_drift(samples: list[dict[str, float]]) -> dict[str, Any] | None:
    """Heart rate against pace, first half vs. second half of the moving time.

    Decoupling is how much less speed each heartbeat bought in the second half. Under about 5% on a
    steady run means the effort stayed aerobic. Meaningless for intervals, where effort changes on
    purpose.
    """
    moving = [s for s in samples if s.get("hr") and (s.get("speed") or 0) >= _MOVING_MPS]
    if not moving or moving[-1]["t"] - moving[0]["t"] < _DRIFT_MIN_S:
        return None
    start = moving[0]["t"] + _DRIFT_SKIP_S
    moving = [s for s in moving if s["t"] >= start]
    if len(moving) < 20:
        return None
    middle = (moving[0]["t"] + moving[-1]["t"]) / 2
    halves = [[s for s in moving if s["t"] < middle], [s for s in moving if s["t"] >= middle]]
    if not all(halves):
        return None

    hr = [_mean([s["hr"] for s in half]) for half in halves]
    speed = [_mean([s["speed"] for s in half]) for half in halves]
    efficiency = [v / h for v, h in zip(speed, hr, strict=True)]
    return {
        "first_half": {"avg_hr": round(hr[0]), "pace": format_pace(pace_seconds(speed[0]))},
        "second_half": {"avg_hr": round(hr[1]), "pace": format_pace(pace_seconds(speed[1]))},
        "hr_change_bpm": round(hr[1] - hr[0]),
        "decoupling_pct": round(100 * (efficiency[0] - efficiency[1]) / efficiency[0], 1),
    }


def _lap_bounds(laps: list[dict[str, Any]]) -> list[tuple[float, float]]:
    """Each lap's start and end on the timer, assuming laps run back to back."""
    bounds, start = [], 0.0
    for lap in laps:
        end = start + float(lap.get("duration") or 0)
        bounds.append((start, end))
        start = end
    return bounds


def _format_target(step: dict[str, Any]) -> str | None:
    kind, low, high = step.get("target_type"), step.get("target_low"), step.get("target_high")
    if low is None or high is None:
        return None
    if kind == "heart_rate":
        return f"{round(low)}-{round(high)} bpm"
    if kind == "pace":
        return f"{format_duration(low)}-{format_duration(high)}/km"
    if kind == "power":
        return f"{round(low)}-{round(high)} W"
    return None


def _verdict(actual: float, low: float, high: float, unit: str) -> str:
    if actual < low:
        return f"below by {round(low - actual)} {unit}"
    if actual > high:
        return f"above by {round(actual - high)} {unit}"
    return "in range"


def compliance(
    steps: list[dict[str, Any]],
    splits: dict[str, Any] | None,
    samples: list[dict[str, float]],
) -> dict[str, Any]:
    """Each planned step's target against what was run in its lap.

    Laps are paired with steps in order, which holds when the watch ran the workout and the lap
    button wasn't pressed. If the counts differ, the pairing is skipped rather than guessed.
    """
    laps = (splits or {}).get("lapDTOs") or []
    if len(laps) != len(steps):
        return {
            "matched": False,
            "note": f"{len(laps)} laps vs {len(steps)} planned steps, so they can't be paired "
            "reliably (was the lap button pressed, or the workout stopped early?).",
        }

    rows = []
    for number, (step, lap, (start, end)) in enumerate(
        zip(steps, laps, _lap_bounds(laps), strict=True), start=1
    ):
        row: dict[str, Any] = {"step": number, "type": step.get("type")}
        target = _format_target(step)
        low, high, kind = step.get("target_low"), step.get("target_high"), step.get("target_type")
        if target is None or low is None or high is None:
            row["target"] = "none"
            rows.append(row)
            continue
        row["target"] = target

        window = [s for s in samples if start <= s["t"] < end]
        if kind == "heart_rate":
            actual = lap.get("averageHR")
            if actual:
                row["actual"] = f"{round(actual)} bpm"
                row["result"] = _verdict(actual, low, high, "bpm")
            in_target = [low <= s["hr"] <= high for s in window if s.get("hr")]
        elif kind == "pace":
            actual = pace_seconds(lap.get("averageSpeed"))
            if actual:
                row["actual"] = format_pace(actual)
                # Lower seconds per km is faster, so "below" the range means too fast.
                if actual < low:
                    row["result"] = f"faster by {round(low - actual)} s/km"
                elif actual > high:
                    row["result"] = f"slower by {round(actual - high)} s/km"
                else:
                    row["result"] = "in range"
            paces = [pace_seconds(s.get("speed")) for s in window]
            in_target = [low <= p <= high for p in paces if p]
        else:
            actual = lap.get("averagePower")
            if actual:
                row["actual"] = f"{round(actual)} W"
                row["result"] = _verdict(actual, low, high, "W")
            in_target = [low <= s["power"] <= high for s in window if s.get("power")]

        if in_target:
            row["time_in_target_pct"] = round(100 * sum(in_target) / len(in_target))
        rows.append(row)

    targeted = [r for r in rows if r.get("result")]
    return drop_empty(
        {
            "matched": True,
            "steps_with_targets": len(targeted),
            "steps_in_range": sum(1 for r in targeted if r["result"] == "in range"),
            "steps": rows,
        }
    )


def linked_workout_id(activity: dict[str, Any]) -> str | None:
    """The workout the watch says it ran, when the activity carries the link itself."""
    metadata = activity.get("metadataDTO") or {}
    workout_id = _first(metadata, "associatedWorkoutId", "workoutId") or activity.get("workoutId")
    return str(workout_id) if workout_id else None


def find_planned_workout_id(schedules: list[dict[str, Any]] | None, activity_id: str) -> str | None:
    """The workout a calendar entry says this activity completed, if any."""
    for item in schedules or []:
        if str(item.get("associatedActivityId")) == str(activity_id):
            workout_id = (item.get("workout") or {}).get("workoutId")
            return str(workout_id) if workout_id else None
    return None
