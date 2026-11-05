"""Response parsing. Fixtures are synthetic, shaped like real Garmin responses."""

from __future__ import annotations

from garmin_mcp import metrics


def test_training_status_prefers_primary_device() -> None:
    data = {
        "mostRecentVO2Max": {"generic": {"vo2MaxPreciseValue": 50.5, "vo2MaxValue": 50.0}},
        "mostRecentTrainingStatus": {
            "latestTrainingStatusData": {
                "1": {
                    "trainingStatusFeedbackPhrase": "UNPRODUCTIVE_1",
                    "primaryTrainingDevice": False,
                    "acuteTrainingLoadDTO": {"dailyTrainingLoadAcute": 100},
                },
                "2": {
                    "trainingStatusFeedbackPhrase": "PRODUCTIVE_3",
                    "primaryTrainingDevice": True,
                    "acuteTrainingLoadDTO": {
                        "dailyTrainingLoadAcute": 700,
                        "acwrStatus": "OPTIMAL",
                    },
                },
            }
        },
    }
    assert metrics.training_status_fields(data) == {
        "training_status": "productive",
        "training_load": 700,
        "training_load_status": "OPTIMAL",
        "vo2max": 50.5,
    }


def test_basic_tracker_yields_nothing_rather_than_zeros() -> None:
    empty = {
        "mostRecentVO2Max": None,
        "mostRecentTrainingStatus": {"latestTrainingStatusData": None},
    }
    assert metrics.training_status_fields(empty) == {}
    assert metrics.training_status_fields(None) == {}
    assert metrics.readiness_fields(None, None) == {}
    assert metrics.sleep_row("2026-01-01", None) == {"date": "2026-01-01"}


def test_drop_empty_keeps_real_zeros() -> None:
    assert metrics.drop_empty({"steps": 0, "vo2max": None}) == {"steps": 0}


def test_readiness_uses_newest_entry_and_hrv_baseline() -> None:
    out = metrics.readiness_fields(
        [{"score": 80, "level": "HIGH"}, {"score": 40, "level": "LOW"}],
        {"hrvSummary": {"lastNightAvg": 60, "baseline": {"balancedLow": 50, "balancedUpper": 70}}},
    )
    assert out == {
        "readiness_score": 80,
        "readiness_level": "HIGH",
        "hrv_last_night": 60,
        "hrv_baseline_low": 50,
        "hrv_baseline_high": 70,
    }


def test_sleep_seconds_become_minutes_and_need_is_already_minutes() -> None:
    row = metrics.sleep_row(
        "2026-01-01",
        {
            "dailySleepDTO": {
                "sleepTimeSeconds": 27000,
                "sleepNeed": {"actual": 480},
                "sleepScores": {"overall": {"value": 83, "qualifierKey": "GOOD"}},
            }
        },
    )
    assert row["sleep_min"] == 450
    assert row["sleep_need_min"] == 480
    assert row["sleep_score"] == 83


def test_activity_cadence_key_follows_the_sport_and_rounds() -> None:
    run = metrics.activity_row(
        {
            "activityId": 1,
            "activityType": {"typeKey": "trail_running"},
            "distance": 10000.0,
            "duration": 3000.0,
            "averageRunningCadenceInStepsPerMinute": 160.625,
        }
    )
    assert run["sport"] == "run"
    assert run["avg_cadence"] == 161
    assert run["pace"] == "5:00/km"
    assert run["distance_km"] == 10.0

    ride = metrics.activity_row(
        {"activityType": {"typeKey": "road_biking"}, "averageBikingCadenceInRevPerMinute": 85.0}
    )
    assert ride["sport"] == "ride"
    assert ride["avg_cadence"] == 85


def test_activities_report_totals_and_order() -> None:
    report = metrics.activities_report(
        [
            {"startTimeLocal": "2026-01-01 07:00:00", "distance": 5000.0, "duration": 1800.0},
            {"startTimeLocal": "2026-01-03 07:00:00", "distance": 2500.0, "duration": 1800.0},
        ]
    )
    assert report["count"] == 2
    assert report["total_distance_km"] == 7.5
    assert report["total_duration"] == "1:00:00"
    assert report["activities"][0]["start_local"].startswith("2026-01-03")


def test_weigh_ins_convert_grams() -> None:
    data = {"dateWeightList": [{"calendarDate": "2026-01-01", "weight": 70500.0, "bmi": 22.1}]}
    assert metrics.weigh_ins_by_date(data)["2026-01-01"]["weight_kg"] == 70.5
    assert metrics.weigh_ins_by_date(None) == {}


def test_race_predictions_format_seconds() -> None:
    assert metrics.race_predictions({"time5K": 1200, "time10K": 2520, "timeMarathon": None}) == {
        "5k": "20:00",
        "10k": "42:00",
    }


def test_hr_zones_ranges_and_default_fallback() -> None:
    profile = {
        "sport": "DEFAULT",
        "zone1Floor": 100,
        "zone2Floor": 120,
        "zone3Floor": 140,
        "zone4Floor": 160,
        "zone5Floor": 175,
        "maxHeartRateUsed": 190,
    }
    zones = metrics.hr_zones([profile], "run")
    assert zones["profile"] == "DEFAULT"
    assert zones["zones"]["zone1"] == {"low": 100, "high": 119}
    assert zones["zones"]["zone5"] == {"low": 175, "high": 190}
    assert metrics.hr_zones([], "run") is None


def test_scheduled_rows_sorted_with_distinct_ids() -> None:
    def schedule(day: str, schedule_id: int, workout_id: int, name: str) -> dict:
        return {
            "calendarDate": day,
            "workoutScheduleId": schedule_id,
            "workout": {
                "workoutId": workout_id,
                "workoutName": name,
                "sportType": {"sportTypeKey": "running"},
                "estimatedDurationInSecs": 3600,
                "estimatedDistanceInMeters": 10000.0,
                "workoutProvider": "TrainingPeaks",
            },
        }

    rows = metrics.scheduled_rows(
        [schedule("2026-01-02", 9, 5, "Later"), schedule("2026-01-01", 8, 4, "Sooner")]
    )
    assert [r["name"] for r in rows] == ["Sooner", "Later"]
    assert rows[0]["schedule_id"] == "8"
    assert rows[0]["workout_id"] == "4"
    assert rows[0]["estimated_duration"] == "1:00:00"
    assert rows[0]["estimated_distance_km"] == 10.0
    assert rows[0]["source"] == "TrainingPeaks"


def test_negative_stress_means_no_reading() -> None:
    row = metrics.body_row(
        "2026-01-01",
        {"averageStressLevel": -1, "maxStressLevel": -2, "stressQualifier": "UNKNOWN"},
        None,
        None,
        None,
        None,
    )
    assert row == {"date": "2026-01-01"}
    real = metrics.body_row("2026-01-01", {"averageStressLevel": 0}, None, None, None, None)
    assert real["avg_stress"] == 0


def test_activity_floats_are_rounded() -> None:
    row = metrics.activity_row(
        {"aerobicTrainingEffect": 1.7000000476837158, "activityTrainingLoad": 128.65484619140625}
    )
    assert row["aerobic_training_effect"] == 1.7
    assert row["training_load"] == 128.7
