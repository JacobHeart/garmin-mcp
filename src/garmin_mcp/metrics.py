"""Pure translation of Garmin's undocumented responses into small, flat records.

No network access here, so everything can be tested offline. Fields a device
doesn't report are dropped rather than returned as null or zero: an absent
field means "not tracked".
"""

from __future__ import annotations

from typing import Any

SPORT_NAMES = {
    "running": "run",
    "trail_running": "run",
    "treadmill_running": "run",
    "cycling": "ride",
    "road_biking": "ride",
    "mountain_biking": "ride",
    "virtual_ride": "ride",
    "indoor_cycling": "ride",
    "open_water_swimming": "swim",
    "lap_swimming": "swim",
    "walking": "walk",
    "hiking": "hike",
    "strength_training": "strength",
}

# Garmin names cadence per sport. Units follow the sport: steps/min for running
# and walking, rev/min for cycling, strokes/min for swimming.
AVG_CADENCE_KEYS = (
    "averageRunningCadenceInStepsPerMinute",
    "averageBikingCadenceInRevPerMinute",
    "averageSwimCadenceInStrokesPerMinute",
)
MAX_CADENCE_KEYS = (
    "maxRunningCadenceInStepsPerMinute",
    "maxBikingCadenceInRevPerMinute",
    "maxSwimCadenceInStrokesPerMinute",
)


def drop_empty(row: dict[str, Any]) -> dict[str, Any]:
    """Remove null fields, keeping real zeros."""
    return {k: v for k, v in row.items() if v is not None}


def minutes(seconds: Any) -> int | None:
    return int(seconds // 60) if seconds else None


def format_duration(seconds: float) -> str:
    total = round(seconds)
    hours, rest = divmod(total, 3600)
    mins, secs = divmod(rest, 60)
    return f"{hours}:{mins:02d}:{secs:02d}" if hours else f"{mins}:{secs:02d}"


def _rounded(value: Any, digits: int = 1) -> float | None:
    """Garmin sends single-precision floats such as 1.7000000476837158."""
    return round(value, digits) if value is not None else None


def _reading(value: Any) -> Any:
    """Garmin uses negative numbers to mean "no reading" for stress: -1 is not a score."""
    return value if value is not None and value >= 0 else None


def _first_number(data: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        value = data.get(key)
        if value:
            return round(float(value))
    return None


def _first_item(data: Any) -> dict[str, Any]:
    """Some endpoints return a dict on some accounts and a list on others."""
    if isinstance(data, list):
        return data[0] if data else {}
    return data or {}


def activity_row(activity: dict[str, Any]) -> dict[str, Any]:
    type_key = (activity.get("activityType") or {}).get("typeKey") or "other"
    sport = SPORT_NAMES.get(type_key, "other")
    distance = activity.get("distance")
    duration = activity.get("duration")

    pace = None
    if sport in ("run", "walk", "hike") and distance and duration:
        pace = format_duration(duration / (distance / 1000)) + "/km"

    return drop_empty(
        {
            "id": activity.get("activityId"),
            "name": activity.get("activityName"),
            "sport": sport,
            "garmin_type": type_key,
            "start_local": activity.get("startTimeLocal"),
            "distance_km": round(distance / 1000, 2) if distance else None,
            "duration": format_duration(duration) if duration else None,
            "pace": pace,
            "avg_hr": round(activity["averageHR"]) if activity.get("averageHR") else None,
            "max_hr": round(activity["maxHR"]) if activity.get("maxHR") else None,
            "avg_cadence": _first_number(activity, AVG_CADENCE_KEYS),
            "max_cadence": _first_number(activity, MAX_CADENCE_KEYS),
            "elevation_gain_m": _rounded(activity.get("elevationGain")),
            "avg_power_w": _rounded(activity.get("avgPower"), 0),
            "calories": _rounded(activity.get("calories"), 0),
            "aerobic_training_effect": _rounded(activity.get("aerobicTrainingEffect")),
            "anaerobic_training_effect": _rounded(activity.get("anaerobicTrainingEffect")),
            "training_load": _rounded(activity.get("activityTrainingLoad")),
        }
    )


def activities_report(activities: list[dict[str, Any]]) -> dict[str, Any]:
    """Rows newest first, plus totals, so nobody has to add them up by hand."""
    ordered = sorted(activities, key=lambda a: a.get("startTimeLocal") or "", reverse=True)
    distance = sum(a.get("distance") or 0 for a in activities)
    duration = sum(a.get("duration") or 0 for a in activities)
    return {
        "count": len(ordered),
        "total_distance_km": round(distance / 1000, 2),
        "total_duration": format_duration(duration),
        "activities": [activity_row(a) for a in ordered],
    }


def training_status_fields(data: dict[str, Any] | None) -> dict[str, Any]:
    """Flatten get_training_status. The primary training device wins on multi-watch accounts."""
    if not data:
        return {}

    by_device = (data.get("mostRecentTrainingStatus") or {}).get("latestTrainingStatusData") or {}
    devices = list(by_device.values())
    device = next(
        (d for d in devices if d.get("primaryTrainingDevice")), devices[0] if devices else {}
    )
    acute = device.get("acuteTrainingLoadDTO") or {}

    # "MAINTAINING_2" -> "maintaining". The numeric trainingStatus is an undocumented enum.
    phrase = device.get("trainingStatusFeedbackPhrase") or ""
    label = phrase.rsplit("_", 1)[0].replace("_", " ").lower() or None

    vo2 = data.get("mostRecentVO2Max") or {}
    generic = vo2.get("generic") or {}
    cycling = vo2.get("cycling") or {}

    return drop_empty(
        {
            "training_status": label,
            "training_load": acute.get("dailyTrainingLoadAcute"),
            "training_load_status": acute.get("acwrStatus"),
            "training_load_chronic": acute.get("dailyTrainingLoadChronic"),
            "training_load_acwr": acute.get("dailyAcuteChronicWorkloadRatio"),
            "vo2max": generic.get("vo2MaxPreciseValue") or generic.get("vo2MaxValue"),
            "vo2max_cycling": cycling.get("vo2MaxPreciseValue") or cycling.get("vo2MaxValue"),
        }
    )


def readiness_fields(readiness: Any, hrv: dict[str, Any] | None) -> dict[str, Any]:
    entry = _first_item(readiness)  # one entry per intraday recalculation, newest first
    summary = (hrv or {}).get("hrvSummary") or hrv or {}
    baseline = summary.get("baseline") or {}
    return drop_empty(
        {
            "readiness_score": entry.get("score"),
            "readiness_level": entry.get("level"),
            "readiness_feedback": entry.get("feedbackShort"),
            "recovery_time_min": entry.get("recoveryTime"),
            "hrv_last_night": summary.get("lastNightAvg"),
            "hrv_weekly_avg": summary.get("weeklyAvg"),
            "hrv_status": summary.get("status"),
            "hrv_baseline_low": baseline.get("balancedLow"),
            "hrv_baseline_high": baseline.get("balancedUpper"),
        }
    )


def sleep_row(day: str, sleep: dict[str, Any] | None) -> dict[str, Any]:
    sleep = sleep or {}
    dto = sleep.get("dailySleepDTO") or {}
    score = (dto.get("sleepScores") or {}).get("overall") or {}
    return drop_empty(
        {
            "date": day,
            "sleep_score": score.get("value"),
            "sleep_quality": score.get("qualifierKey"),
            "sleep_min": minutes(dto.get("sleepTimeSeconds")),
            "sleep_need_min": (dto.get("sleepNeed") or {}).get("actual"),  # already minutes
            "deep_min": minutes(dto.get("deepSleepSeconds")),
            "rem_min": minutes(dto.get("remSleepSeconds")),
            "light_min": minutes(dto.get("lightSleepSeconds")),
            "awake_min": minutes(dto.get("awakeSleepSeconds")),
            "avg_hr": dto.get("avgHeartRate"),
            "avg_stress": _reading(dto.get("avgSleepStress")),
            "respiration_avg": dto.get("averageRespirationValue"),
            "restless_moments": sleep.get("restlessMomentsCount"),
            "hrv_avg": sleep.get("avgOvernightHrv"),
        }
    )


def body_row(
    day: str,
    summary: dict[str, Any] | None,
    respiration: dict[str, Any] | None,
    spo2: dict[str, Any] | None,
    battery: dict[str, Any] | None,
    weigh_in: dict[str, Any] | None,
) -> dict[str, Any]:
    summary = summary or {}
    respiration = respiration or {}
    spo2 = spo2 or {}
    battery = battery or {}
    weigh_in = weigh_in or {}
    qualifier = summary.get("stressQualifier")
    return drop_empty(
        {
            "date": day,
            "body_battery_high": summary.get("bodyBatteryHighestValue"),
            "body_battery_low": summary.get("bodyBatteryLowestValue"),
            "body_battery_charged": battery.get("charged"),
            "body_battery_drained": battery.get("drained"),
            "steps": summary.get("totalSteps"),
            "step_goal": summary.get("dailyStepGoal"),
            "distance_m": summary.get("totalDistanceMeters"),
            "calories_total": summary.get("totalKilocalories"),
            "calories_active": summary.get("activeKilocalories"),
            "floors_climbed": summary.get("floorsAscended"),
            "avg_stress": _reading(summary.get("averageStressLevel")),
            "max_stress": _reading(summary.get("maxStressLevel")),
            "stress_qualifier": qualifier if qualifier != "UNKNOWN" else None,
            "resting_hr": summary.get("restingHeartRate"),
            "resting_hr_7d_avg": summary.get("lastSevenDaysAvgRestingHeartRate"),
            "min_hr": summary.get("minHeartRate"),
            "max_hr": summary.get("maxHeartRate"),
            "intensity_minutes_moderate": summary.get("moderateIntensityMinutes"),
            "intensity_minutes_vigorous": summary.get("vigorousIntensityMinutes"),
            "respiration_avg": respiration.get("avgSleepRespirationValue")
            or respiration.get("avgWakingRespirationValue"),
            "spo2_avg": spo2.get("averageSpO2") or summary.get("averageSpo2"),
            "weight_kg": weigh_in.get("weight_kg"),
            "body_fat_pct": weigh_in.get("body_fat_pct"),
            "bmi": weigh_in.get("bmi"),
        }
    )


def weigh_ins_by_date(data: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Index a body-composition response by date. Garmin reports weight in grams."""
    out: dict[str, dict[str, Any]] = {}
    for entry in (data or {}).get("dateWeightList") or []:
        day = entry.get("calendarDate")
        if day:
            grams = entry.get("weight")
            out[day] = {
                "weight_kg": round(grams / 1000, 2) if grams else None,
                "body_fat_pct": entry.get("bodyFat"),
                "bmi": entry.get("bmi"),
            }
    return out


def race_predictions(data: Any) -> dict[str, str]:
    entry = _first_item(data)
    labels = {
        "5k": "time5K",
        "10k": "time10K",
        "half_marathon": "timeHalfMarathon",
        "marathon": "timeMarathon",
    }
    return {race: format_duration(entry[key]) for race, key in labels.items() if entry.get(key)}


def hr_zones(profiles: list[dict[str, Any]] | None, sport: str) -> dict[str, Any] | None:
    """Turn zone floors into bpm ranges, falling back to the DEFAULT profile."""
    if not profiles:
        return None
    key = {"run": "RUNNING", "ride": "CYCLING"}[sport]
    profile = next((p for p in profiles if p.get("sport") == key), None)
    if profile is None:
        profile = next((p for p in profiles if p.get("sport") == "DEFAULT"), profiles[0])

    floors = [profile.get(f"zone{n}Floor") for n in range(1, 6)]
    max_hr = profile.get("maxHeartRateUsed")
    if max_hr is None or any(f is None for f in floors):
        return None

    ceilings = [floor - 1 for floor in floors[1:]] + [max_hr]
    return drop_empty(
        {
            "profile": profile.get("sport"),
            "max_heart_rate": max_hr,
            "resting_heart_rate": profile.get("restingHeartRateUsed"),
            "zones": {
                f"zone{n}": {"low": low, "high": high}
                for n, (low, high) in enumerate(zip(floors, ceilings, strict=True), start=1)
            },
        }
    )


def scheduled_rows(schedules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Scheduled workouts, soonest first. `schedule_id` is a different number from `workout_id`."""
    rows = []
    for item in schedules:
        workout = item.get("workout") or {}
        seconds = workout.get("estimatedDurationInSecs")
        meters = workout.get("estimatedDistanceInMeters")
        rows.append(
            drop_empty(
                {
                    "date": item.get("calendarDate"),
                    "name": item.get("newName") or workout.get("workoutName"),
                    "sport": (workout.get("sportType") or {}).get("sportTypeKey"),
                    "schedule_id": str(item.get("workoutScheduleId")),
                    "workout_id": str(workout.get("workoutId")),
                    "estimated_duration": format_duration(seconds) if seconds else None,
                    "estimated_distance_km": round(meters / 1000, 2) if meters else None,
                    "source": workout.get("workoutProvider"),
                    "completed_activity_id": item.get("associatedActivityId"),
                }
            )
        )
    return sorted(rows, key=lambda r: r.get("date") or "")
