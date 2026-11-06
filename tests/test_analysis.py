"""Session analysis. Fixtures are synthetic, shaped like real Garmin responses."""

from __future__ import annotations

from typing import Any

import pytest

from garmin_mcp import analysis
from garmin_mcp.analysis import Recording

START_MS = 1_762_451_775_000.0  # 2026-11-06T17:56:15Z


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


# 10 min warm-up, 3 x 8 min with 2 min jogs, 8 min cool-down: the first session of the plan.
SPLITS = {
    "lapDTOs": [
        _lap("WARMUP", 600, 1800, 138),
        _lap(
            "INTERVAL",
            480,
            1700,
            158,
            averageRunCadence=178.4,
            maxRunCadence=186.0,
            strideLength=118.0,
            groundContactTime=241.3,
            verticalOscillation=8.9,
            verticalRatio=7.6,
            avgGradeAdjustedSpeed=3.6,
            averagePower=430.0,
        ),
        _lap("RECOVERY", 120, 330, 150, averageRunCadence=164.0, averagePower=300.0),
        _lap("INTERVAL", 480, 1690, 162, averageRunCadence=179.0, averagePower=428.0),
        _lap("RECOVERY", 120, 330, 152),
        _lap("INTERVAL", 480, 1660, 166, averageRunCadence=177.0, averagePower=421.0),
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


def _details(keys: list[str], rows: list[list[Any]]) -> dict[str, Any]:
    return {
        "metricDescriptors": [{"metricsIndex": i, "key": k} for i, k in enumerate(keys)],
        "activityDetailMetrics": [{"metrics": row} for row in rows],
    }


def _steady_run(minutes: int, hr_start: float, hr_end: float, speed: float = 3.1) -> dict:
    keys = ["directTimestamp", "sumDuration", "sumDistance", "directHeartRate", "directSpeed"]
    rows = []
    for second in range(0, minutes * 60, 10):
        hr = hr_start + (hr_end - hr_start) * second / (minutes * 60)
        rows.append([START_MS + second * 1000, second, second * speed, hr, speed])
    return _details(keys, rows)


def _session_recording(**overrides: Any) -> Recording:
    """Ten-second samples matching SPLITS, with every channel a running watch records."""
    keys = [
        "directTimestamp",
        "sumDuration",
        "sumDistance",
        "directHeartRate",
        "directSpeed",
        "directGradeAdjustedSpeed",
        "directRunCadence",
        "directFractionalCadence",
        "directPower",
        "directGroundContactTime",
        "directVerticalOscillation",
        "directVerticalRatio",
        "directStrideLength",
        "directElevation",
        "directRespirationRate",
        "directLatitude",
        "directFutureMetric",
    ]
    rows, t, distance = [], 0.0, 0.0
    for lap in SPLITS["lapDTOs"]:
        speed = lap["averageSpeed"]
        for _ in range(int(lap["duration"] // 10)):
            rows.append(
                [
                    START_MS + t * 1000,
                    t,
                    distance,
                    lap["averageHR"],
                    speed,
                    speed * 1.01,
                    88.0,  # per leg: 176 steps per minute
                    0.5,
                    420.0,
                    240.0,
                    8.9,
                    7.5,
                    120.0,
                    50.0 + t / 600,
                    38.0,
                    32.1,
                    7.0,
                ]
            )
            t += 10
            distance += speed * 10
    return Recording(_details(keys, rows), overrides.get("sport", "run"))


def test_summary_converts_units_and_drops_untracked_fields() -> None:
    activity = {
        "activityId": 1,
        "activityName": "Sub-threshold 3x8 min",
        "activityTypeDTO": {"typeKey": "running"},
        "summaryDTO": {
            "startTimeLocal": "2026-11-06T19:56:15.0",
            "distance": 9240.0,
            "duration": 2807.0,
            "averageSpeed": 3.29,
            "averageHR": 155.2,
            "averageRunCadence": 175.3,
            "strideLength": 118.0,
            "verticalOscillation": 8.9,
            "trainingEffect": 3.9000000953674316,
            "directWorkoutRpe": 70,
            "directWorkoutFeel": 75,
            "averagePower": None,
        },
    }
    summary = analysis.activity_summary(activity)
    assert summary["pace"] == "5:04/km"
    assert summary["avg_cadence"] == 175
    assert summary["stride_length_m"] == 1.18
    assert summary["vertical_oscillation_cm"] == 8.9
    assert summary["aerobic_training_effect"] == 3.9
    assert summary["rpe"] == 7.0
    assert summary["feel"] == "strong"
    assert "avg_power_w" not in summary


def test_rides_report_speed_not_pace() -> None:
    activity = {
        "activityTypeDTO": {"typeKey": "road_biking"},
        "summaryDTO": {"averageSpeed": 8.0, "averageBikeCadence": 88.0},
    }
    summary = analysis.activity_summary(activity)
    assert summary["avg_speed_kmh"] == 28.8
    assert summary["avg_cadence"] == 88
    assert "pace" not in summary


def test_laps_carry_cadence_power_and_running_dynamics() -> None:
    laps = analysis.lap_rows(SPLITS)
    assert len(laps) == 7
    assert laps[1] == {
        "lap": 2,
        "type": "interval",
        "duration": "8:00",
        "distance_km": 1.7,
        "pace": "4:42/km",
        "grade_adjusted_pace": "4:38/km",
        "avg_hr": 158,
        "max_hr": 163,
        "avg_cadence": 178,
        "max_cadence": 186,
        "avg_power_w": 430,
        "ground_contact_ms": 241,
        "stride_length_m": 1.18,
        "vertical_oscillation_cm": 8.9,
        "vertical_ratio_pct": 7.6,
    }


def test_interval_summary_reports_fade_hr_rise_cadence_and_power() -> None:
    summary = analysis.interval_summary(SPLITS)
    assert summary is not None
    assert summary["reps"] == 3
    assert summary["fastest_pace"] == "4:42/km"
    assert summary["slowest_pace"] == "4:49/km"
    assert summary["fade_s_per_km"] == 7
    assert summary["hr_rise_bpm"] == 8
    assert summary["avg_cadence"] == 178
    assert summary["power_w_first_rep"] == 430
    assert summary["power_w_last_rep"] == 421


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
        {"zone": 1, "range": "109-133 bpm", "time": "5:00", "percent": 25.0},
        {"zone": 2, "range": "134-143 bpm", "time": "15:00", "percent": 75.0},
        {"zone": 3, "range": "144+ bpm", "time": "0:00", "percent": 0.0},
    ]
    assert analysis.zone_rows([{"zoneNumber": 1, "zoneLowBoundary": 200}], "W")[0]["range"] == (
        "200+ W"
    )


def test_series_has_every_channel_including_unknown_ones() -> None:
    recording = _session_recording()
    table = analysis.series_table(recording, 60)
    assert table is not None
    columns = table["columns"]
    for name in (
        "km",
        "pace",
        "gap",
        "cadence",
        "hr",
        "power_w",
        "gct_ms",
        "vert_osc_cm",
        "vert_ratio_pct",
        "stride_m",
        "elev_m",
        "resp_brpm",
        "future_metric",  # a channel this module has no name for still comes through
    ):
        assert name in columns, name
    assert "latitude" not in columns
    first = dict(zip(columns, table["rows"][0], strict=True))
    assert first["cadence"] == 177  # (88 + 0.5) per leg, doubled
    assert first["stride_m"] == 1.2
    assert first["hr"] == 138


def test_double_cadence_is_used_as_is() -> None:
    details = _details(
        ["sumDuration", "directDoubleCadence", "directRunCadence"],
        [[0, 176.0, 88.0], [10, 178.0, 89.0]],
    )
    recording = Recording(details, "run")
    assert recording.cadence(recording.samples[0]) == 176.0


def test_cadence_already_in_steps_per_minute_is_not_doubled() -> None:
    details = _details(["sumDuration", "directRunCadence"], [[0, 174.0], [10, 176.0]])
    recording = Recording(details, "run")
    assert recording.cadence(recording.samples[0]) == 174.0


def test_km_splits_come_from_the_recording() -> None:
    splits = analysis.km_splits(_session_recording())
    assert splits is not None
    assert [s["km"] for s in splits[:3]] == [1.0, 2.0, 3.0]
    assert splits[0]["pace"] == "5:33/km"  # the 3 m/s warm-up
    assert splits[0]["cadence"] == 177
    assert splits[-1]["km"] == pytest.approx(8.88, abs=0.01)  # final partial km kept (last sample)


def test_km_splits_skip_rides() -> None:
    assert analysis.km_splits(_session_recording(sport="ride")) is None


def test_drift_flags_a_run_that_stopped_being_aerobic() -> None:
    drift = analysis.hr_drift(Recording(_steady_run(60, 140, 160)))
    assert drift is not None
    assert drift["basis"] == "pace"
    assert drift["hr_change_bpm"] > 0
    assert drift["decoupling_pct"] > 5
    assert drift["efficiency_factor"] > 0


def test_drift_needs_a_long_enough_run() -> None:
    assert analysis.hr_drift(Recording(_steady_run(15, 140, 150))) is None


def test_series_is_downsampled_to_compact_rows() -> None:
    recording = Recording(_steady_run(60, 140, 150))
    table = analysis.series_table(recording, analysis.auto_interval(recording))
    assert table is not None
    assert table["interval_s"] == 60
    assert table["columns"] == ["min", "km", "pace", "hr"]
    assert len(table["rows"]) == 60
    assert table["rows"][0][2] == "5:23/km"


def test_bad_recordings_give_nothing_instead_of_crashing() -> None:
    assert not Recording(None)
    assert not Recording({"metricDescriptors": [{"metricsIndex": 0, "key": "directHeartRate"}]})
    garbage = {
        "metricDescriptors": [
            {"metricsIndex": 0, "key": "sumDuration"},
            {"metricsIndex": "x", "key": "directHeartRate"},
            {"metricsIndex": 5, "key": "directPower"},
        ],
        "activityDetailMetrics": [{"metrics": [None]}, {"metrics": [10, "a"]}, {}],
    }
    recording = Recording(garbage)
    assert len(recording.samples) == 1
    assert analysis.series_table(Recording(None), 60) is None
    assert analysis.km_splits(recording) is None
    assert analysis.hr_drift(recording) is None


def test_timestamps_alone_are_enough_for_a_clock() -> None:
    details = _details(
        ["directTimestamp", "directHeartRate"], [[START_MS, 120], [START_MS + 5000, 121]]
    )
    recording = Recording(details)
    assert [s["t"] for s in recording.samples] == [0.0, 5.0]


def _with_gmt_starts(pause_after_lap: int = -1, pause_s: float = 0) -> dict[str, Any]:
    laps, t = [], 0.0
    for i, lap in enumerate(SPLITS["lapDTOs"]):
        start = START_MS / 1000 + t
        from datetime import UTC, datetime

        stamp = datetime.fromtimestamp(start, UTC).strftime("%Y-%m-%dT%H:%M:%S.0")
        laps.append({**lap, "startTimeGMT": stamp, "elapsedDuration": lap["duration"]})
        t += lap["duration"] + (pause_s if i == pause_after_lap else 0)
    return {"lapDTOs": laps}


def test_compliance_compares_each_step_with_its_target() -> None:
    result = analysis.compliance(STEPS, _with_gmt_starts(), _session_recording())
    assert result["matched"] is True
    assert result["steps_with_targets"] == 3
    assert result["steps_in_range"] == 1
    reps = [s for s in result["steps"] if s["type"] == "interval"]
    assert reps[0]["result"] == "below by 2 bpm"
    assert reps[0]["time_in_target_pct"] == 0
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


def test_compliance_without_timestamps_lays_laps_back_to_back() -> None:
    result = analysis.compliance(STEPS, SPLITS, _session_recording())
    assert result["steps"][3]["time_in_target_pct"] == 100


def test_compliance_reads_pace_targets_the_right_way_round() -> None:
    steps = [{"type": "interval", "target_type": "pace", "target_low": 240, "target_high": 250}]
    splits = {"lapDTOs": [_lap("INTERVAL", 230, 1000, 170)]}
    result = analysis.compliance(steps, splits, Recording(None))
    assert result["steps"][0]["target"] == "4:00-4:10/km"
    assert result["steps"][0]["result"] == "faster by 10 s/km"


def test_compliance_skips_pairing_when_laps_and_steps_differ() -> None:
    result = analysis.compliance(STEPS[:3], SPLITS, Recording(None))
    assert result["matched"] is False
    assert "7 laps vs 3 planned steps" in result["note"]


def test_planned_workout_is_found_through_the_calendar() -> None:
    schedules = [
        {"associatedActivityId": 99, "workout": {"workoutId": 5}},
        {"associatedActivityId": 24989712925, "workout": {"workoutId": 1753442394}},
        "junk",
    ]
    assert analysis.find_planned_workout_id(schedules, "24989712925") == "1753442394"
    assert analysis.find_planned_workout_id(schedules, "1") is None
    assert analysis.linked_workout_id({"metadataDTO": {"associatedWorkoutId": 7}}) == "7"
    assert analysis.linked_workout_id({}) is None


def test_weather_is_converted_to_metric() -> None:
    data = {
        "temp": 68,
        "apparentTemp": 70,
        "relativeHumidity": 55,
        "windSpeed": 10,
        "windDirectionCompassPoint": "nw",
        "weatherTypeDTO": {"desc": "Clear"},
    }
    assert analysis.weather(data) == {
        "conditions": "Clear",
        "temp_c": 20.0,
        "feels_like_c": 21.1,
        "humidity_pct": 55,
        "wind_kmh": 16,
        "wind_from": "nw",
    }
    assert analysis.weather(None) is None
    assert analysis.weather({}) is None


def test_field_names_list_what_the_device_sent() -> None:
    names = analysis.field_names(
        {"summaryDTO": {"averageHR": 150, "maxPower": None}}, SPLITS, _session_recording()
    )
    assert names["summary"] == ["averageHR"]
    assert "averageRunCadence" in names["lap"]
    assert "directFutureMetric" in names["recording"]
