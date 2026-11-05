from __future__ import annotations

import pytest
from garminconnect.workout import ConditionType, SportType, StepType, TargetType
from pydantic import ValidationError

from garmin_mcp.workouts import (
    END_CONDITIONS,
    HR_TARGET,
    NO_TARGET,
    PACE_TARGET,
    SPORT_TYPES,
    STEP_TYPES,
    WorkoutStep,
    build_update,
    build_workout,
    read_steps,
)


def test_ids_match_the_garmin_library() -> None:
    assert SPORT_TYPES["run"]["sportTypeId"] == SportType.RUNNING
    assert SPORT_TYPES["ride"]["sportTypeId"] == SportType.CYCLING
    assert SPORT_TYPES["other"]["sportTypeId"] == SportType.OTHER
    assert SPORT_TYPES["swim"]["sportTypeId"] == SportType.SWIMMING
    assert SPORT_TYPES["strength"]["sportTypeId"] == SportType.STRENGTH_TRAINING
    assert STEP_TYPES["warmup"]["stepTypeId"] == StepType.WARMUP
    assert STEP_TYPES["cooldown"]["stepTypeId"] == StepType.COOLDOWN
    assert STEP_TYPES["interval"]["stepTypeId"] == StepType.INTERVAL
    assert STEP_TYPES["recovery"]["stepTypeId"] == StepType.RECOVERY
    assert STEP_TYPES["rest"]["stepTypeId"] == StepType.REST
    assert STEP_TYPES["other"]["stepTypeId"] == StepType.OTHER
    assert END_CONDITIONS["time"]["conditionTypeId"] == ConditionType.TIME
    assert END_CONDITIONS["distance"]["conditionTypeId"] == ConditionType.DISTANCE
    assert END_CONDITIONS["lap_button"]["conditionTypeId"] == ConditionType.LAP_BUTTON
    assert NO_TARGET["workoutTargetTypeId"] == TargetType.NO_TARGET
    assert HR_TARGET["workoutTargetTypeId"] == TargetType.HEART_RATE_ZONE
    assert PACE_TARGET["workoutTargetTypeId"] == TargetType.PACE_ZONE


def test_build_orders_steps_and_converts_pace_to_speed() -> None:
    body = build_workout(
        "Intervals",
        "run",
        [
            WorkoutStep(type="warmup", duration_type="time", duration_value=600),
            WorkoutStep(
                type="interval",
                duration_type="distance",
                duration_value=1000,
                target_type="pace",
                target_low=240,
                target_high=250,
            ),
        ],
    )
    steps = body["workoutSegments"][0]["workoutSteps"]
    assert [s["stepOrder"] for s in steps] == [1, 2]
    assert steps[0]["targetType"] == NO_TARGET
    # Slower pace (250 s/km) is the lower speed.
    assert steps[1]["targetValueOne"] == pytest.approx(4.0)
    assert steps[1]["targetValueTwo"] == pytest.approx(1000 / 240)


def test_steps_round_trip_through_garmin_format() -> None:
    original = [
        WorkoutStep(type="warmup", duration_type="lap_button"),
        WorkoutStep(
            type="interval",
            duration_type="distance",
            duration_value=1000,
            target_type="pace",
            target_low=240,
            target_high=250,
            description="Hard",
        ),
        WorkoutStep(
            type="recovery",
            duration_type="time",
            duration_value=120,
            target_type="heart_rate",
            target_low=110,
            target_high=130,
        ),
        WorkoutStep(
            type="interval",
            duration_type="time",
            duration_value=480,
            target_type="power",
            target_low=139,
            target_high=222,
        ),
    ]
    body = build_workout("x", "run", original)
    read = read_steps(body["workoutSegments"][0]["workoutSteps"])
    assert [WorkoutStep(**row) for row in read] == original


def test_repeat_groups_are_expanded() -> None:
    step = {
        "stepType": {"stepTypeKey": "interval"},
        "endCondition": {"conditionTypeKey": "time"},
        "endConditionValue": 60,
        "targetType": {"workoutTargetTypeKey": "no.target"},
    }
    group = {"type": "RepeatGroupDTO", "numberOfIterations": 3, "workoutSteps": [step]}
    assert len(read_steps([group])) == 3


@pytest.mark.parametrize(
    "kwargs",
    [
        {"type": "interval", "duration_type": "time"},
        {
            "type": "interval",
            "duration_type": "time",
            "duration_value": 60,
            "target_type": "pace",
            "target_low": 240,
        },
        {
            "type": "interval",
            "duration_type": "time",
            "duration_value": 60,
            "target_type": "heart_rate",
            "target_low": 150,
            "target_high": 140,
        },
        {"type": "sprint", "duration_type": "time", "duration_value": 60},
    ],
)
def test_invalid_steps_are_rejected(kwargs: dict) -> None:
    with pytest.raises(ValidationError):
        WorkoutStep(**kwargs)


def test_real_swim_sport_id_is_four() -> None:
    assert SPORT_TYPES["swim"] == {
        "sportTypeId": 4,
        "sportTypeKey": "swimming",
        "displayOrder": 4,
    }


def test_update_keeps_pool_settings_and_description() -> None:
    step = WorkoutStep(type="interval", duration_type="distance", duration_value=100)
    existing = {"description": "Notes", "poolLength": 25.0, "poolLengthUnit": {"unitKey": "meter"}}
    swim = build_update(existing, "x", "swim", [step])
    assert swim["poolLength"] == 25.0
    assert swim["poolLengthUnit"] == {"unitKey": "meter"}
    assert swim["description"] == "Notes"
    assert "poolLength" not in build_update(existing, "x", "run", [step])


def test_unsupported_targets_still_show_their_values() -> None:
    step = {
        "stepType": {"stepTypeKey": "interval"},
        "endCondition": {"conditionTypeKey": "time"},
        "endConditionValue": 60,
        "targetType": {"workoutTargetTypeKey": "cadence"},
        "targetValueOne": 80.0,
        "targetValueTwo": 90.0,
    }
    assert read_steps([step])[0]["target_type"] == "cadence"
    assert read_steps([step])[0]["target_low"] == 80.0
