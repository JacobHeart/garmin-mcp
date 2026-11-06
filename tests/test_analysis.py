"""Session analysis. Fixtures are synthetic, shaped like real Garmin responses."""

from __future__ import annotations

from typing import Any

from garmin_mcp import analysis


def _lap(kind: str, seconds: float, meters: float, hr: float, **extra: Any) -> dict[str, Any]:
    return {
        "intensityType": kind,
        "duration": seconds,
        "distance": meters,
        "averageSpeed": meters / seconds,
        "averageHR": hr,
        "maxHR": hr + 5,
        **extra,
    }


# 10 min warm-up, 3 x 8 min with 2 min jogs, 8 min cool-down: tonight's session.
SPLITS = {
    "lapDTOs": [
        _lap("WARMUP", 600, 1800, 138),
        _lap("INTERVAL", 480, 1700, 158, averageRunCadence=178.4, strideLength=118.0),
        _lap("RECOVERY", 120, 330, 150),
        _lap("INTERVAL", 480, 1690, 162),
        _lap("RECOVERY", 120, 330, 152),
        _lap("INTERVAL", 480, 1660, 166),
        _lap("COOLDOWN", 480, 1400, 145),
    ]
}

STEPS = [
    {"type": "warmup", "target_type": "none"},
    {"type": "interval", "target_type": "heart_rate", "target_low": 160, "target_high": 165},
    {"type": "recovery", "target_type": "none"},
    {"type": "interval", "target_type": "heart_rate", "target_low": 160, "target_high": 165},
    {"type": "recovery", "target_type": "none"},
    {"type": "interval", "target_type": "heart_rate", "target_low": 160, "target_high": 165},
    {"type": "cooldown", "target_type": "none"},
]


def _details(rows: list[list[Any]]) -> dict[str, Any]:
    keys = ["directTimestamp", "sumDuration", "sumDistance", "directHeartRate", "directSpeed"]
    return {
        "metricDescriptors": [{"metricsIndex": i, "key": k} for i, k in enumerate(keys)],
        "activityDetailMetrics": [{"metrics": row} for row in rows],
    }


def _steady_run(minutes: int, hr_start: float, hr_end: float, speed: float = 3.1) -> dict:
    rows = []
    for second in range(0, minutes * 60, 10):
        hr = hr_start + (hr_end - hr_start) * second / (minutes * 60)
        rows.append([1.7e12 + second * 1000, second, second * speed, hr, speed])
    return _details(rows)


def test_summary_converts_units_and_drops_untracked_fields() -> None:
    activity = {
        "activityId": 1,
        "activityName": "Sub-threshold 3x8 min",
        "activityTypeDTO": {"typeKey": "running"},
        "summaryDTO": {
            "startTimeLocal": "2026-11-06T19:56:15.0",
            "distance": 9240.0,
            "duration": 2807.0,
            "averageHR": 155.2,
            "strideLength": 118.0,
            "trainingEffect": 3.9000000953674316,
            "directWorkoutRpe": 70,
            "directWorkoutFeel": 75,
        },
    }
    summary = analysis.activity_summary(activity)
    assert summary["pace"] == "5:04/km"
    assert summary["stride_length_m"] == 1.18
    assert summary["aerobic_training_effect"] == 3.9
    assert summary["rpe"] == 7.0
    assert summary["feel"] == "strong"
    assert "avg_power_w" not in summary


def test_laps_show_each_step() -> None:
    laps = analysis.lap_rows(SPLITS)
    assert len(laps) == 7
    assert laps[1] == {
        "lap": 2,
        "type": "interval",
        "duration": "8:00",
        "distance_km": 1.7,
        "pace": "4:42/km",
        "avg_hr": 158,
        "max_hr": 163,
        "avg_cadence": 178,
        "stride_length_m": 1.18,
    }


def test_interval_summary_reports_fade_and_hr_rise() -> None:
    summary = analysis.interval_summary(SPLITS)
    assert summary is not None
    assert summary["reps"] == 3
    assert summary["fastest_pace"] == "4:42/km"
    assert summary["slowest_pace"] == "4:49/km"
    assert summary["fade_s_per_km"] == 7
    assert summary["hr_rise_bpm"] == 8


def test_auto_laps_of_a_steady_run_are_not_reps() -> None:
    splits = {"lapDTOs": [_lap("ACTIVE", 330, 1000, 145) for _ in range(5)]}
    assert analysis.interval_summary(splits) is None


def test_zone_rows_give_ranges_and_shares() -> None:
    zones = [
        {"zoneNumber": 2, "secsInZone": 900.0, "zoneLowBoundary": 134},
        {"zoneNumber": 1, "secsInZone": 300.0, "zoneLowBoundary": 109},
        {"zoneNumber": 3, "secsInZone": 0.0, "zoneLowBoundary": 144},
    ]
    assert analysis.zone_rows(zones) == [
        {"zone": 1, "bpm": "109-133", "time": "5:00", "percent": 25.0},
        {"zone": 2, "bpm": "134-143", "time": "15:00", "percent": 75.0},
        {"zone": 3, "bpm": "144+", "time": "0:00", "percent": 0.0},
    ]


def test_drift_flags_a_run_that_stopped_being_aerobic() -> None:
    samples = analysis.parse_series(_steady_run(60, 140, 160))
    drift = analysis.hr_drift(samples)
    assert drift is not None
    assert drift["hr_change_bpm"] > 0
    assert drift["decoupling_pct"] > 5


def test_drift_needs_a_long_enough_run() -> None:
    assert analysis.hr_drift(analysis.parse_series(_steady_run(15, 140, 150))) is None


def test_series_is_downsampled_to_compact_rows() -> None:
    samples = analysis.parse_series(_steady_run(60, 140, 150))
    table = analysis.series_table(samples, analysis.auto_interval(samples))
    assert table is not None
    assert table["interval_s"] == 60
    assert table["columns"] == ["min", "km", "hr", "pace"]
    assert len(table["rows"]) == 60
    assert table["rows"][0][3] == "5:23/km"


def test_series_without_timing_is_ignored() -> None:
    details = {"metricDescriptors": [{"metricsIndex": 0, "key": "directHeartRate"}]}
    assert analysis.parse_series(details) == []
    assert analysis.series_table([], 60) is None


def test_compliance_compares_each_step_with_its_target() -> None:
    rows = []
    t = 0.0
    for lap in SPLITS["lapDTOs"]:
        for _ in range(int(lap["duration"] // 10)):
            rows.append([0, t, 0, lap["averageHR"], lap["averageSpeed"]])
            t += 10
    result = analysis.compliance(STEPS, SPLITS, analysis.parse_series(_details(rows)))
    assert result["matched"] is True
    assert result["steps_with_targets"] == 3
    assert result["steps_in_range"] == 1
    reps = [s for s in result["steps"] if s["type"] == "interval"]
    assert reps[0]["result"] == "below by 2 bpm"
    assert reps[1] == {
        "step": 4,
        "type": "interval",
        "target": "160-165 bpm",
        "actual": "162 bpm",
        "result": "in range",
        "time_in_target_pct": 100,
    }
    assert reps[2]["result"] == "above by 1 bpm"
    assert result["steps"][0]["target"] == "none"


def test_compliance_reads_pace_targets_the_right_way_round() -> None:
    steps = [{"type": "interval", "target_type": "pace", "target_low": 240, "target_high": 250}]
    splits = {"lapDTOs": [_lap("INTERVAL", 230, 1000, 170)]}
    result = analysis.compliance(steps, splits, [])
    assert result["steps"][0]["target"] == "4:00-4:10/km"
    assert result["steps"][0]["result"] == "faster by 10 s/km"


def test_compliance_skips_pairing_when_laps_and_steps_differ() -> None:
    result = analysis.compliance(STEPS[:3], SPLITS, [])
    assert result["matched"] is False
    assert "7 laps vs 3 planned steps" in result["note"]


def test_planned_workout_is_found_through_the_calendar() -> None:
    schedules = [
        {"associatedActivityId": 99, "workout": {"workoutId": 5}},
        {"associatedActivityId": 24989712925, "workout": {"workoutId": 1753442394}},
    ]
    assert analysis.find_planned_workout_id(schedules, "24989712925") == "1753442394"
    assert analysis.find_planned_workout_id(schedules, "1") is None
    assert analysis.linked_workout_id({"metadataDTO": {"associatedWorkoutId": 7}}) == "7"
    assert analysis.linked_workout_id({}) is None
