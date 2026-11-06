"""A full breakdown of one recorded activity, built from Garmin's per-activity endpoints.

Pure functions over Garmin's responses, so everything can be tested offline:

- `activity_summary`: the session as a whole, including running dynamics, training effect and RPE
- `lap_rows`: every lap, which for a structured workout is every step
- `interval_summary`: how the work reps compared, when the session had work and recovery laps
- `zone_rows`: time in each heart-rate or power zone
- `Recording`: the second-by-second recording, with every channel the device wrote
- `series_table`, `km_splits`, `hr_drift`: views of the recording
- `compliance`: each planned workout step's target against what was run
- `weather`: conditions at the start, from Garmin's weather service

Devices differ in what they record, so nothing here assumes a field exists. A metric the device
didn't send is left out rather than reported as zero, and a channel this module has no name for is
still passed through under Garmin's own key.
"""

from __future__ import annotations

import math
import re
import statistics
from datetime import UTC, datetime
from typing import Any

from garminconnect.activity_details import parse_activity_detail_metrics

from .metrics import SPORT_NAMES, drop_empty, format_duration

# Lap intensity types Garmin uses for the hard and easy parts of a structured workout.
WORK_TYPES = {"interval", "active"}
REST_TYPES = {"recovery", "rest"}

# Below this speed the athlete is standing or walking, so pace means nothing.
_MOVING_MPS = 1.0
# Heart rate lags effort, so the first minutes would bias a drift comparison.
_DRIFT_SKIP_S = 5 * 60
_DRIFT_MIN_S = 20 * 60
# A final partial kilometre shorter than this is noise, not a split.
_MIN_PARTIAL_KM_M = 200

FEEL = {0: "very weak", 25: "weak", 50: "normal", 75: "strong", 100: "very strong"}
ON_FOOT = {"run", "walk", "hike"}


# --- Small helpers -------------------------------------------------------------------------------


def _num(value: Any) -> float | None:
    """A real number, or None. Booleans and NaN don't count."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return None if math.isnan(value) else float(value)


def _round(value: Any, digits: int = 1) -> float | None:
    number = _num(value)
    return round(number, digits) if number is not None else None


def _int(value: Any) -> int | None:
    number = _num(value)
    return round(number) if number is not None else None


def _first(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if _num(data.get(key)) is not None:
            return data[key]
    return None


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def pace_seconds(speed_mps: Any) -> float | None:
    """Seconds per km from metres per second, or None when not really moving."""
    speed = _num(speed_mps)
    if speed is None or speed < _MOVING_MPS:
        return None
    return 1000.0 / speed


def format_pace(seconds_per_km: float | None) -> str | None:
    return format_duration(seconds_per_km) + "/km" if seconds_per_km else None


def _pace(speed_mps: Any) -> str | None:
    return format_pace(pace_seconds(speed_mps))


def _duration(seconds: Any) -> str | None:
    number = _num(seconds)
    return format_duration(number) if number else None


def _km(meters: Any) -> float | None:
    number = _num(meters)
    return round(number / 1000, 2) if number else None


def _stride_m(value: Any) -> float | None:
    """Garmin reports stride length in centimetres; tolerate a value already in metres."""
    number = _num(value)
    if number is None or number <= 0:
        return None
    return round(number / 100 if number > 10 else number, 2)


def _oscillation_cm(value: Any) -> float | None:
    """Vertical oscillation in centimetres; tolerate a value in millimetres."""
    number = _num(value)
    if number is None or number <= 0:
        return None
    return round(number / 10 if number > 50 else number, 1)


def _snake(key: str) -> str:
    """Garmin's camelCase key as snake_case, without the `direct` prefix."""
    key = key.removeprefix("direct")
    return re.sub(r"(?<!^)(?=[A-Z])", "_", key).lower()


def sport_of(activity: dict[str, Any]) -> str:
    kind = activity.get("activityTypeDTO") or activity.get("activityType")
    type_key = kind.get("typeKey") if isinstance(kind, dict) else None
    return SPORT_NAMES.get(type_key if isinstance(type_key, str) else "", "other")


def _parse_gmt(text: Any) -> float | None:
    """Garmin's GMT timestamps, such as 2026-11-06T17:56:15.0, as epoch milliseconds."""
    if not isinstance(text, str) or not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", ""))
    except ValueError:
        return None
    return moment.replace(tzinfo=UTC).timestamp() * 1000


# --- Fields shared by the activity summary and each lap ------------------------------------------

# (output name, how to format, Garmin keys in order of preference)
_EFFORT_FIELDS: tuple[tuple[str, Any, tuple[str, ...]], ...] = (
    ("duration", _duration, ("duration",)),
    ("moving_duration", _duration, ("movingDuration",)),
    ("elapsed_duration", _duration, ("elapsedDuration",)),
    ("distance_km", _km, ("distance",)),
    ("pace", _pace, ("averageMovingSpeed", "averageSpeed")),
    ("best_pace", _pace, ("maxSpeed",)),
    ("grade_adjusted_pace", _pace, ("avgGradeAdjustedSpeed",)),
    ("avg_hr", _int, ("averageHR",)),
    ("max_hr", _int, ("maxHR",)),
    ("min_hr", _int, ("minHR",)),
    (
        "avg_cadence",
        _int,
        ("averageRunCadence", "averageRunningCadenceInStepsPerMinute", "averageBikeCadence"),
    ),
    (
        "max_cadence",
        _int,
        ("maxRunCadence", "maxRunningCadenceInStepsPerMinute", "maxBikeCadence"),
    ),
    ("avg_power_w", _int, ("averagePower", "avgPower")),
    ("max_power_w", _int, ("maxPower",)),
    ("normalized_power_w", _int, ("normalizedPower", "normPower")),
    ("ground_contact_ms", _int, ("groundContactTime", "avgGroundContactTime")),
    ("ground_contact_balance_left_pct", lambda v: _round(v), ("avgGroundContactBalance",)),
    ("stride_length_m", _stride_m, ("strideLength", "avgStrideLength")),
    ("vertical_oscillation_cm", _oscillation_cm, ("verticalOscillation", "avgVerticalOscillation")),
    ("vertical_ratio_pct", lambda v: _round(v), ("verticalRatio", "avgVerticalRatio")),
    ("elevation_gain_m", lambda v: _round(v), ("elevationGain",)),
    ("elevation_loss_m", lambda v: _round(v), ("elevationLoss",)),
    ("max_elevation_m", lambda v: _round(v), ("maxElevation",)),
    ("min_elevation_m", lambda v: _round(v), ("minElevation",)),
    ("avg_respiration_brpm", lambda v: _round(v), ("avgRespirationRate",)),
    ("max_respiration_brpm", lambda v: _round(v), ("maxRespirationRate",)),
    ("avg_temperature_c", lambda v: _round(v), ("averageTemperature",)),
    ("calories", _int, ("calories",)),
    ("strokes", _int, ("totalNumberOfStrokes",)),
    ("avg_swolf", _int, ("averageSWOLF", "averageSwolf")),
)


def _effort_fields(data: dict[str, Any]) -> dict[str, Any]:
    row: dict[str, Any] = {}
    for name, fmt, keys in _EFFORT_FIELDS:
        value = _first(data, *keys)
        if value is not None:
            row[name] = fmt(value)
    return row


def activity_summary(activity: dict[str, Any]) -> dict[str, Any]:
    """The session as a whole, from Garmin's single-activity response."""
    summary = activity.get("summaryDTO") or {}
    sport = sport_of(activity)

    rpe = _num(summary.get("directWorkoutRpe"))
    if rpe is not None and rpe > 10:
        rpe = rpe / 10  # Garmin stores RPE 1-10 as 10-100

    fields = _effort_fields(summary)
    if sport not in ON_FOOT:
        for key in ("pace", "best_pace", "grade_adjusted_pace"):
            fields.pop(key, None)
        speed = _num(_first(summary, "averageMovingSpeed", "averageSpeed"))
        if speed:
            fields["avg_speed_kmh"] = round(speed * 3.6, 1)

    return drop_empty(
        {
            "id": activity.get("activityId"),
            "name": activity.get("activityName"),
            "sport": sport,
            "start_local": summary.get("startTimeLocal"),
            **fields,
            "aerobic_training_effect": _round(summary.get("trainingEffect")),
            "anaerobic_training_effect": _round(summary.get("anaerobicTrainingEffect")),
            "training_effect_label": summary.get("trainingEffectLabel"),
            "training_load": _round(summary.get("activityTrainingLoad")),
            "rpe": _round(rpe),
            "feel": FEEL.get(_int(summary.get("directWorkoutFeel"))),
            "stamina_start_pct": _int(summary.get("beginPotentialStamina")),
            "stamina_end_pct": _int(summary.get("endPotentialStamina")),
            "body_battery_change": _int(summary.get("differenceBodyBattery")),
            "sweat_loss_ml": _int(summary.get("waterEstimated")),
        }
    )


# --- Laps ----------------------------------------------------------------------------------------


def _laps(splits: Any) -> list[dict[str, Any]]:
    laps = (splits or {}).get("lapDTOs") if isinstance(splits, dict) else None
    return [lap for lap in laps or [] if isinstance(lap, dict)]


def _lap_type(lap: dict[str, Any]) -> str:
    return str(lap.get("intensityType") or "active").lower()


def lap_rows(splits: Any, sport: str = "run") -> list[dict[str, Any]]:
    """One row per lap. For a structured workout each step is its own lap."""
    rows = []
    for number, lap in enumerate(_laps(splits), start=1):
        fields = _effort_fields(lap)
        if sport not in ON_FOOT:
            for key in ("pace", "best_pace", "grade_adjusted_pace"):
                fields.pop(key, None)
        rows.append({"lap": number, "type": _lap_type(lap), **fields})
    return rows


def interval_summary(splits: Any) -> dict[str, Any] | None:
    """How the work reps compared. None unless the session alternated work and recovery."""
    laps = _laps(splits)
    if not any(_lap_type(lap) in REST_TYPES for lap in laps):
        return None  # auto-laps of a steady run, not reps
    reps = [lap for lap in laps if _lap_type(lap) in WORK_TYPES]
    if len(reps) < 2:
        return None

    def series(*keys: str) -> list[float | None]:
        return [_num(_first(lap, *keys)) for lap in reps]

    paces = [pace_seconds(s) for s in series("averageMovingSpeed", "averageSpeed")]
    hrs = series("averageHR")
    cadences = series("averageRunCadence", "averageBikeCadence")
    powers = series("averagePower")

    result: dict[str, Any] = {"reps": len(reps)}
    known = [p for p in paces if p]
    if known:
        result.update(
            {
                "avg_pace": format_pace(_mean(known)),
                "fastest_pace": format_pace(min(known)),
                "slowest_pace": format_pace(max(known)),
                "pace_spread_s_per_km": round(max(known) - min(known)),
            }
        )
        if paces[0] and paces[-1]:
            result["fade_s_per_km"] = round(paces[-1] - paces[0])  # positive = slowed down
    for name, values in (("hr", hrs), ("cadence", cadences), ("power_w", powers)):
        present = [v for v in values if v]
        if not present:
            continue
        result[f"avg_{name}"] = round(_mean(present))
        if values[0] and values[-1]:
            result[f"{name}_first_rep"] = round(values[0])
            result[f"{name}_last_rep"] = round(values[-1])
    if "hr_first_rep" in result:
        result["hr_rise_bpm"] = result["hr_last_rep"] - result["hr_first_rep"]
    return result


# --- Zones ---------------------------------------------------------------------------------------


def zone_rows(zones: Any, unit: str = "bpm") -> list[dict[str, Any]]:
    """Time in each zone, with its range and share of the session."""
    if isinstance(zones, dict):  # some accounts wrap the list
        zones = zones.get("zones") or zones.get("timeInZones") or []
    entries = sorted(
        (z for z in zones or [] if isinstance(z, dict) and _num(z.get("zoneNumber")) is not None),
        key=lambda z: z["zoneNumber"],
    )
    total = sum(_num(z.get("secsInZone")) or 0 for z in entries)
    rows = []
    for i, zone in enumerate(entries):
        low = _int(zone.get("zoneLowBoundary"))
        high = _int(entries[i + 1].get("zoneLowBoundary")) if i + 1 < len(entries) else None
        secs = _num(zone.get("secsInZone")) or 0
        span = None
        if low is not None:
            span = f"{low}-{high - 1} {unit}" if high is not None else f"{low}+ {unit}"
        rows.append(
            drop_empty(
                {
                    "zone": _int(zone["zoneNumber"]),
                    "range": span,
                    "time": format_duration(secs),
                    "percent": round(100 * secs / total, 1) if total else None,
                }
            )
        )
    return rows


# --- The recording -------------------------------------------------------------------------------

# Garmin channel key -> (output name, converter). Anything else that starts with `direct` is passed
# through under its own name, so a metric this table doesn't know about is never lost.
_KNOWN_CHANNELS: dict[str, tuple[str, Any]] = {
    "directHeartRate": ("hr", _int),
    "directElevation": ("elev_m", _int),
    "directPower": ("power_w", _int),
    "directGroundContactTime": ("gct_ms", _int),
    "directGroundContactBalanceLeft": ("gct_balance_left_pct", lambda v: _round(v)),
    "directVerticalOscillation": ("vert_osc_cm", _oscillation_cm),
    "directVerticalRatio": ("vert_ratio_pct", lambda v: _round(v)),
    "directStrideLength": ("stride_m", _stride_m),
    "directRespirationRate": ("resp_brpm", _int),
    "directAirTemperature": ("temp_c", lambda v: _round(v)),
    "directPerformanceCondition": ("perf_condition", _int),
    "directVerticalSpeed": ("vert_speed_mps", lambda v: _round(v, 2)),
    "directBodyBattery": ("body_battery", _int),
    "directStamina": ("stamina_pct", _int),
    "directPotentialStamina": ("potential_stamina_pct", _int),
}
# Handled separately (time, distance, pace, cadence), duplicates, or not useful in a table.
_SPECIAL_CHANNELS = {
    "directTimestamp",
    "sumDuration",
    "sumElapsedDuration",
    "sumMovingDuration",
    "sumDistance",
    "sumAccumulatedPower",
    "directSpeed",
    "directGradeAdjustedSpeed",
    "directEnhancedSpeed",
    "directDoubleCadence",
    "directRunCadence",
    "directFractionalCadence",
    "directBikeCadence",
    "directLatitude",
    "directLongitude",
    "directCorrectedElevation",
    "directUncorrectedElevation",
    "directEnhancedElevation",
}


class Recording:
    """The activity's samples, each a dict of Garmin channel key to value, on a common clock.

    `t` is seconds on the activity timer (pauses excluded when Garmin says so), `ts` is epoch
    milliseconds when the device recorded timestamps.
    """

    def __init__(self, details: Any, sport: str = "run") -> None:
        self.sport = sport
        raw = parse_activity_detail_metrics(details) if isinstance(details, dict) else []
        samples = []
        for row in raw:
            sample = {k: float(v) for k, v in row.items() if _num(v) is not None}
            if sample:
                samples.append(sample)

        clock = next(
            (
                k
                for k in ("sumDuration", "sumMovingDuration", "sumElapsedDuration")
                if any(k in s for s in samples)
            ),
            None,
        )
        if clock is None and any("directTimestamp" in s for s in samples):
            start = min(s["directTimestamp"] for s in samples if "directTimestamp" in s)
            for s in samples:
                if "directTimestamp" in s:
                    s["t"] = (s["directTimestamp"] - start) / 1000
        for s in samples:
            if clock and clock in s:
                s["t"] = s[clock]
            if "directTimestamp" in s:
                s["ts"] = s["directTimestamp"]
        self.samples = sorted((s for s in samples if "t" in s), key=lambda s: s["t"])
        self._cadence_factor = self._find_cadence_factor()

    def __bool__(self) -> bool:
        return bool(self.samples)

    @property
    def channels(self) -> list[str]:
        return sorted({k for s in self.samples for k in s if k not in ("t", "ts")})

    def _find_cadence_factor(self) -> float:
        """Garmin's running cadence channel is sometimes per leg. Steps per minute are wanted."""
        if self.sport not in ON_FOOT or any("directDoubleCadence" in s for s in self.samples):
            return 1.0
        values = [s["directRunCadence"] for s in self.samples if s.get("directRunCadence")]
        if not values:
            return 1.0
        return 2.0 if statistics.median(values) < 120 else 1.0

    def cadence(self, sample: dict[str, float]) -> float | None:
        if sample.get("directDoubleCadence"):
            return sample["directDoubleCadence"]
        if sample.get("directRunCadence"):
            per_leg = sample["directRunCadence"] + (sample.get("directFractionalCadence") or 0)
            return per_leg * self._cadence_factor
        return sample.get("directBikeCadence") or None

    @staticmethod
    def speed(sample: dict[str, float]) -> float | None:
        value = sample.get("directSpeed", sample.get("directEnhancedSpeed"))
        return value if value is not None else None

    def between(self, start_t: float, end_t: float) -> list[dict[str, float]]:
        return [s for s in self.samples if start_t <= s["t"] < end_t]

    def between_ts(self, start_ms: float, end_ms: float) -> list[dict[str, float]]:
        return [s for s in self.samples if "ts" in s and start_ms <= s["ts"] < end_ms]


def _channel_columns(recording: Recording) -> list[tuple[str, str]]:
    """(Garmin key, output name) for every plain channel, known names first."""
    present = set(recording.channels)
    known = [(key, name) for key, (name, _) in _KNOWN_CHANNELS.items() if key in present]
    extra = sorted(
        k
        for k in present
        if k.startswith("direct") and k not in _KNOWN_CHANNELS and k not in _SPECIAL_CHANNELS
    )
    return known + [(key, _snake(key)) for key in extra]


def auto_interval(recording: Recording) -> int:
    """A bucket size, in multiples of 30 s, that keeps the table near 60 rows."""
    if not recording:
        return 60
    minutes = (recording.samples[-1]["t"] - recording.samples[0]["t"]) / 60
    return max(30, math.ceil(minutes / 30) * 30)


def _bucket_row(
    recording: Recording,
    group: list[dict[str, float]],
    columns: list[tuple[str, str]],
    flags: dict[str, bool],
) -> dict[str, Any]:
    row: dict[str, Any] = {}
    if flags["distance"]:
        distances = [s["sumDistance"] for s in group if "sumDistance" in s]
        row["km"] = round(distances[-1] / 1000, 2) if distances else None
    if flags["speed"]:
        speeds = [v for s in group if (v := recording.speed(s)) is not None]
        if recording.sport in ON_FOOT:
            row["pace"] = _pace(_mean(speeds))
        else:
            mean = _mean(speeds)
            row["speed_kmh"] = round(mean * 3.6, 1) if mean is not None else None
    if flags["gap"] and recording.sport in ON_FOOT:
        row["gap"] = _pace(
            _mean([s["directGradeAdjustedSpeed"] for s in group if "directGradeAdjustedSpeed" in s])
        )
    if flags["cadence"]:
        row["cadence"] = _int(_mean([c for s in group if (c := recording.cadence(s))]))
    for key, name in columns:
        values = [s[key] for s in group if key in s]
        convert = _KNOWN_CHANNELS.get(key, (name, lambda v: _round(v)))[1]
        # Zeros in heart rate, power and cadence mean a dropout or a stop, not a reading.
        if key in ("directHeartRate", "directPower"):
            values = [v for v in values if v > 0]
        row[name] = convert(_mean(values)) if values else None
    return row


def series_table(recording: Recording, interval_s: int) -> dict[str, Any] | None:
    """The recording averaged into fixed time buckets, as compact columns and rows.

    Every channel the device recorded is included, so the column list differs between watches.
    """
    if not recording:
        return None
    flags = {
        "distance": any("sumDistance" in s for s in recording.samples),
        "speed": any(recording.speed(s) is not None for s in recording.samples),
        "gap": any("directGradeAdjustedSpeed" in s for s in recording.samples),
        "cadence": any(recording.cadence(s) for s in recording.samples),
    }
    columns = _channel_columns(recording)

    buckets: dict[int, list[dict[str, float]]] = {}
    for sample in recording.samples:
        buckets.setdefault(int(sample["t"] // interval_s), []).append(sample)

    dict_rows = []
    for key in sorted(buckets):
        row = {"min": round((key + 1) * interval_s / 60, 1)}
        row.update(_bucket_row(recording, buckets[key], columns, flags))
        dict_rows.append(row)

    # Keep only columns with at least one value, in a stable order.
    names = list(dict.fromkeys(name for row in dict_rows for name in row))
    names = [n for n in names if any(row.get(n) is not None for row in dict_rows)]
    return {
        "interval_s": interval_s,
        "columns": names,
        "rows": [[row.get(n) for n in names] for row in dict_rows],
    }


def km_splits(recording: Recording) -> list[dict[str, Any]] | None:
    """Kilometre splits computed from the recording, whatever the watch's lap settings were."""
    if recording.sport not in ON_FOOT:
        return None
    points = [s for s in recording.samples if "sumDistance" in s]
    if len(points) < 2 or points[-1]["sumDistance"] < 1000:
        return None

    # Times at which each whole kilometre was crossed, interpolated between samples.
    marks = [(0.0, points[0]["t"])]
    next_km = 1000.0
    for before, after in zip(points, points[1:], strict=False):
        while after["sumDistance"] >= next_km > before["sumDistance"]:
            span = after["sumDistance"] - before["sumDistance"]
            fraction = (next_km - before["sumDistance"]) / span if span else 0
            marks.append((next_km, before["t"] + fraction * (after["t"] - before["t"])))
            next_km += 1000
    final = points[-1]
    if final["sumDistance"] - marks[-1][0] >= _MIN_PARTIAL_KM_M:
        marks.append((final["sumDistance"], final["t"]))

    splits = []
    for (d0, t0), (d1, t1) in zip(marks, marks[1:], strict=False):
        window = recording.between(t0, t1)
        meters = d1 - d0
        elevations = [s["directElevation"] for s in window if "directElevation" in s]
        gaps = [s["directGradeAdjustedSpeed"] for s in window if "directGradeAdjustedSpeed" in s]
        splits.append(
            drop_empty(
                {
                    "km": round(d1 / 1000, 2),
                    "time": format_duration(t1 - t0),
                    "pace": format_pace((t1 - t0) / (meters / 1000)) if meters else None,
                    "gap": _pace(_mean(gaps)),
                    "avg_hr": _int(
                        _mean([s["directHeartRate"] for s in window if s.get("directHeartRate")])
                    ),
                    "cadence": _int(_mean([c for s in window if (c := recording.cadence(s))])),
                    "elev_change_m": round(elevations[-1] - elevations[0]) if elevations else None,
                }
            )
        )
    return splits


def hr_drift(recording: Recording) -> dict[str, Any] | None:
    """Heart rate against speed, first half vs. second half of the moving time.

    Decoupling is how much less speed each heartbeat bought in the second half. Under about 5% on a
    steady run means the effort stayed aerobic. Grade-adjusted speed is used when the device has it,
    so hills don't count as drift. Meaningless for intervals, where effort changes on purpose.
    """
    use_gap = any("directGradeAdjustedSpeed" in s for s in recording.samples)

    def speed(s: dict[str, float]) -> float | None:
        return s.get("directGradeAdjustedSpeed") if use_gap else recording.speed(s)

    moving = [
        s
        for s in recording.samples
        if (s.get("directHeartRate") or 0) > 0 and (speed(s) or 0) >= _MOVING_MPS
    ]
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

    hr = [_mean([s["directHeartRate"] for s in half]) or 0 for half in halves]
    spd = [_mean([speed(s) or 0 for s in half]) or 0 for half in halves]
    efficiency = [v / h for v, h in zip(spd, hr, strict=True)]
    return {
        "basis": "grade-adjusted pace" if use_gap else "pace",
        "first_half": {"avg_hr": round(hr[0]), "pace": _pace(spd[0])},
        "second_half": {"avg_hr": round(hr[1]), "pace": _pace(spd[1])},
        "hr_change_bpm": round(hr[1] - hr[0]),
        "decoupling_pct": round(100 * (efficiency[0] - efficiency[1]) / efficiency[0], 1),
        # Friel's efficiency factor: metres per minute per heartbeat, higher is fitter.
        "efficiency_factor": round(_mean([v * 60 for v in spd]) / _mean(hr), 2),
    }


# --- Planned workout -----------------------------------------------------------------------------


def _lap_windows(laps: list[dict[str, Any]], recording: Recording) -> list[list[dict[str, float]]]:
    """The samples inside each lap.

    Laps are placed by their GMT start time when both they and the samples carry timestamps,
    which stays right across pauses. Otherwise they're laid back to back on the timer.
    """
    starts = [_parse_gmt(lap.get("startTimeGMT")) for lap in laps]
    if all(starts) and any("ts" in s for s in recording.samples):
        windows = []
        for lap, start in zip(laps, starts, strict=True):
            length = _num(_first(lap, "elapsedDuration", "duration")) or 0
            windows.append(recording.between_ts(start, start + length * 1000))
        return windows

    windows, t = [], recording.samples[0]["t"] if recording else 0.0
    for lap in laps:
        end = t + (_num(lap.get("duration")) or 0)
        windows.append(recording.between(t, end))
        t = end
    return windows


def _format_target(step: dict[str, Any]) -> str | None:
    kind = step.get("target_type")
    low, high = _num(step.get("target_low")), _num(step.get("target_high"))
    if low is None or high is None:
        return None
    if kind == "heart_rate":
        return f"{round(low)}-{round(high)} bpm"
    if kind == "pace":
        return f"{format_duration(low)}-{format_duration(high)}/km"
    if kind == "power":
        return f"{round(low)}-{round(high)} W"
    return None


def _check(actual: float, low: float, high: float, unit: str) -> str:
    if actual < low:
        return f"below by {round(low - actual)} {unit}"
    if actual > high:
        return f"above by {round(actual - high)} {unit}"
    return "in range"


def _check_pace(actual: float, low: float, high: float) -> str:
    # Lower seconds per km is faster, so under the range means too fast.
    if actual < low:
        return f"faster by {round(low - actual)} s/km"
    if actual > high:
        return f"slower by {round(actual - high)} s/km"
    return "in range"


def compliance(steps: list[dict[str, Any]], splits: Any, recording: Recording) -> dict[str, Any]:
    """Each planned step's target against what was run in its lap.

    Laps are paired with steps in order, which holds when the watch ran the workout and the lap
    button wasn't pressed. If the counts differ, the pairing is skipped rather than guessed.
    """
    laps = _laps(splits)
    if len(laps) != len(steps):
        return {
            "matched": False,
            "note": f"{len(laps)} laps vs {len(steps)} planned steps, so they can't be paired "
            "reliably (was the lap button pressed, or the workout stopped early?).",
        }

    windows = _lap_windows(laps, recording)
    rows = []
    for number, (step, lap, window) in enumerate(zip(steps, laps, windows, strict=True), start=1):
        row: dict[str, Any] = {"step": number, "type": step.get("type")}
        target = _format_target(step)
        if target is None:
            row["target"] = "none"
            rows.append(row)
            continue
        row["target"] = target
        kind = step.get("target_type")
        low, high = float(step["target_low"]), float(step["target_high"])

        if kind == "heart_rate":
            actual = _num(lap.get("averageHR"))
            if actual:
                row["actual"] = f"{round(actual)} bpm"
                row["result"] = _check(actual, low, high, "bpm")
            readings = [s["directHeartRate"] for s in window if s.get("directHeartRate")]
        elif kind == "pace":
            actual = pace_seconds(_first(lap, "averageMovingSpeed", "averageSpeed"))
            if actual:
                row["actual"] = format_pace(actual)
                row["result"] = _check_pace(actual, low, high)
            readings = [p for s in window if (p := pace_seconds(recording.speed(s)))]
        else:
            actual = _num(lap.get("averagePower"))
            if actual:
                row["actual"] = f"{round(actual)} W"
                row["result"] = _check(actual, low, high, "W")
            readings = [s["directPower"] for s in window if s.get("directPower")]

        if readings:
            inside = sum(1 for v in readings if low <= v <= high)
            row["time_in_target_pct"] = round(100 * inside / len(readings))
        rows.append(row)

    targeted = [r for r in rows if r.get("result")]
    return {
        "matched": True,
        "steps_with_targets": len(targeted),
        "steps_in_range": sum(1 for r in targeted if r["result"] == "in range"),
        "steps": rows,
    }


def linked_workout_id(activity: dict[str, Any]) -> str | None:
    """The workout the watch says it ran, when the activity carries the link itself."""
    metadata = activity.get("metadataDTO") or {}
    workout_id = _first(metadata, "associatedWorkoutId", "workoutId") or _num(
        activity.get("workoutId")
    )
    return str(int(workout_id)) if workout_id else None


def find_planned_workout_id(schedules: Any, activity_id: str) -> str | None:
    """The workout a calendar entry says this activity completed, if any."""
    for item in schedules or []:
        if isinstance(item, dict) and str(item.get("associatedActivityId")) == str(activity_id):
            workout_id = (item.get("workout") or {}).get("workoutId")
            return str(workout_id) if workout_id else None
    return None


# --- Weather -------------------------------------------------------------------------------------


def weather(data: Any) -> dict[str, Any] | None:
    """Conditions at the start. Garmin's weather service reports °F and mph."""
    if not isinstance(data, dict):
        return None

    def celsius(value: Any) -> float | None:
        number = _num(value)
        return round((number - 32) * 5 / 9, 1) if number is not None else None

    wind = _num(data.get("windSpeed"))
    gust = _num(data.get("windGust"))
    result = drop_empty(
        {
            "conditions": (data.get("weatherTypeDTO") or {}).get("desc"),
            "temp_c": celsius(data.get("temp")),
            "feels_like_c": celsius(data.get("apparentTemp")),
            "dew_point_c": celsius(data.get("dewPoint")),
            "humidity_pct": _int(data.get("relativeHumidity")),
            "wind_kmh": round(wind * 1.609) if wind is not None else None,
            "wind_gust_kmh": round(gust * 1.609) if gust is not None else None,
            "wind_from": data.get("windDirectionCompassPoint"),
        }
    )
    return result or None


# --- Field names, for checking a new device ------------------------------------------------------


def field_names(
    activity: dict[str, Any], splits: Any, recording: Recording
) -> dict[str, list[str]]:
    """The raw Garmin keys behind the report, to see what a device sends."""
    summary = activity.get("summaryDTO") or {}
    laps = _laps(splits)
    return {
        "summary": sorted(k for k, v in summary.items() if v is not None),
        "lap": sorted({k for lap in laps for k, v in lap.items() if v is not None}),
        "recording": recording.channels,
    }
