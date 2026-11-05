"""Structured workouts: a simple step format, translated to and from Garmin's workout format.

Steps are a flat list. "5x1km" is written out as five interval+recovery pairs.
Repeat groups in workouts made in the Garmin app are expanded when read, so the
output of `read_steps` can always be sent back through an update.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from .metrics import drop_empty, format_duration

Sport = Literal["run", "ride", "swim", "strength", "other"]

SPORT_TYPES: dict[str, dict[str, Any]] = {
    "run": {"sportTypeId": 1, "sportTypeKey": "running", "displayOrder": 1},
    "ride": {"sportTypeId": 2, "sportTypeKey": "cycling", "displayOrder": 2},
    "other": {"sportTypeId": 3, "sportTypeKey": "other", "displayOrder": 3},
    "swim": {"sportTypeId": 4, "sportTypeKey": "swimming", "displayOrder": 4},
    "strength": {"sportTypeId": 5, "sportTypeKey": "strength_training", "displayOrder": 5},
}

STEP_TYPES: dict[str, dict[str, Any]] = {
    "warmup": {"stepTypeId": 1, "stepTypeKey": "warmup", "displayOrder": 1},
    "cooldown": {"stepTypeId": 2, "stepTypeKey": "cooldown", "displayOrder": 2},
    "interval": {"stepTypeId": 3, "stepTypeKey": "interval", "displayOrder": 3},
    "recovery": {"stepTypeId": 4, "stepTypeKey": "recovery", "displayOrder": 4},
    "rest": {"stepTypeId": 5, "stepTypeKey": "rest", "displayOrder": 5},
    "other": {"stepTypeId": 7, "stepTypeKey": "other", "displayOrder": 7},
}

END_CONDITIONS: dict[str, dict[str, Any]] = {
    "lap_button": {
        "conditionTypeId": 1,
        "conditionTypeKey": "lap.button",
        "displayOrder": 1,
        "displayable": True,
    },
    "time": {
        "conditionTypeId": 2,
        "conditionTypeKey": "time",
        "displayOrder": 2,
        "displayable": True,
    },
    "distance": {
        "conditionTypeId": 3,
        "conditionTypeKey": "distance",
        "displayOrder": 3,
        "displayable": True,
    },
}

NO_TARGET = {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target", "displayOrder": 1}
HR_TARGET = {"workoutTargetTypeId": 4, "workoutTargetTypeKey": "heart.rate.zone", "displayOrder": 4}
PACE_TARGET = {"workoutTargetTypeId": 6, "workoutTargetTypeKey": "pace.zone", "displayOrder": 6}
POWER_TARGET = {"workoutTargetTypeId": 10, "workoutTargetTypeKey": "power.3s", "displayOrder": 10}

# Kept on update: Garmin replaces the whole workout, and steps don't carry these.
_SWIM_FIELDS = ("poolLength", "poolLengthUnit")


class WorkoutStep(BaseModel):
    type: Literal["warmup", "cooldown", "interval", "recovery", "rest", "other"]
    duration_type: Literal["time", "distance", "lap_button"] = Field(
        description="How the step ends: after a time, a distance, or a lap-button press."
    )
    duration_value: float | None = Field(
        default=None,
        gt=0,
        description="Seconds for time, meters for distance. Omit for lap_button.",
    )
    target_type: Literal["none", "pace", "heart_rate", "power"] = "none"
    target_low: float | None = Field(
        default=None,
        gt=0,
        description="pace: faster bound in seconds/km. heart_rate: lower bpm. power: lower watts.",
    )
    target_high: float | None = Field(
        default=None,
        gt=0,
        description="pace: slower bound in seconds/km. heart_rate: upper bpm. power: upper watts.",
    )
    description: str | None = None

    @model_validator(mode="after")
    def _check_values(self) -> WorkoutStep:
        if self.duration_type != "lap_button" and self.duration_value is None:
            raise ValueError(f"duration_value is required when duration_type={self.duration_type}")
        if self.target_type != "none":
            if self.target_low is None or self.target_high is None:
                raise ValueError(f"target_low and target_high are required for {self.target_type}")
            if self.target_low > self.target_high:
                raise ValueError("target_low must not exceed target_high")
        return self


def _build_step(step: WorkoutStep, order: int) -> dict[str, Any]:
    dto: dict[str, Any] = {
        "type": "ExecutableStepDTO",
        "stepOrder": order,
        "stepType": STEP_TYPES[step.type],
        "endCondition": END_CONDITIONS[step.duration_type],
        "endConditionValue": step.duration_value,
        "description": step.description,
    }
    if step.target_type in ("heart_rate", "power"):
        dto["targetType"] = HR_TARGET if step.target_type == "heart_rate" else POWER_TARGET
        dto["targetValueOne"] = step.target_low
        dto["targetValueTwo"] = step.target_high
    elif step.target_type == "pace":
        # Garmin stores pace as speed in m/s, so the slower pace is the lower value.
        dto["targetType"] = PACE_TARGET
        dto["targetValueOne"] = 1000.0 / step.target_high
        dto["targetValueTwo"] = 1000.0 / step.target_low
    else:
        dto["targetType"] = NO_TARGET
    return dto


def build_workout(name: str, sport: str, steps: list[WorkoutStep]) -> dict[str, Any]:
    """The request body for creating or fully replacing a workout."""
    sport_type = SPORT_TYPES[sport]
    return {
        "workoutName": name,
        "sportType": sport_type,
        "workoutSegments": [
            {
                "segmentOrder": 1,
                "sportType": sport_type,
                "workoutSteps": [_build_step(s, i) for i, s in enumerate(steps, start=1)],
            }
        ],
    }


def build_update(
    existing: dict[str, Any], name: str, sport: str, steps: list[WorkoutStep]
) -> dict[str, Any]:
    """A full replacement body that keeps the workout's description and pool settings."""
    body = build_workout(name, sport, steps)
    if existing.get("description"):
        body["description"] = existing["description"]
    if sport == "swim":
        body.update({k: existing[k] for k in _SWIM_FIELDS if existing.get(k) is not None})
    return body


def _read_step(step: dict[str, Any]) -> dict[str, Any]:
    target_key = (step.get("targetType") or {}).get("workoutTargetTypeKey") or "no.target"
    low = step.get("targetValueOne")
    high = step.get("targetValueTwo")

    row: dict[str, Any] = {"type": (step.get("stepType") or {}).get("stepTypeKey")}
    condition = (step.get("endCondition") or {}).get("conditionTypeKey")
    if condition == "lap.button":
        row["duration_type"] = "lap_button"
    else:
        row["duration_type"] = condition
        row["duration_value"] = step.get("endConditionValue")

    if target_key == "pace.zone" and low and high:
        row["target_type"] = "pace"
        row["target_low"] = round(1000.0 / high, 1)  # faster pace comes from the higher speed
        row["target_high"] = round(1000.0 / low, 1)
    elif target_key == "heart.rate.zone":
        row["target_type"] = "heart_rate"
        if low and high:
            row["target_low"] = low
            row["target_high"] = high
        elif step.get("zoneNumber"):
            row["zone_number"] = step["zoneNumber"]  # resolve via garmin_hr_zones before updating
    elif target_key.startswith("power") and low and high:
        row["target_type"] = "power"
        row["target_low"] = low
        row["target_high"] = high
    elif target_key == "no.target":
        row["target_type"] = "none"
    else:
        row["target_type"] = target_key  # cadence etc.: readable, not writable here
        if low is not None:
            row["target_low"] = low
        if high is not None:
            row["target_high"] = high

    if step.get("description"):
        row["description"] = step["description"]
    return row


def read_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Garmin's steps as a flat list in our own format, with repeat groups expanded."""
    out: list[dict[str, Any]] = []
    for step in steps:
        if step.get("workoutSteps"):  # RepeatGroupDTO
            for _ in range(int(step.get("numberOfIterations") or 1)):
                out.extend(read_steps(step["workoutSteps"]))
        else:
            out.append(_read_step(step))
    return out


def summarize_workout(workout: dict[str, Any]) -> dict[str, Any]:
    """Enough to identify a workout. Garmin's full version is several KB of nested enums."""
    seconds = workout.get("estimatedDurationInSecs")
    meters = workout.get("estimatedDistanceInMeters")
    return drop_empty(
        {
            "workout_id": str(workout.get("workoutId", "")),
            "name": workout.get("workoutName"),
            "sport": (workout.get("sportType") or {}).get("sportTypeKey"),
            "estimated_duration": format_duration(seconds) if seconds else None,
            "estimated_distance_km": round(meters / 1000, 2) if meters else None,
            "updated": (workout.get("updatedDate") or workout.get("createdDate") or "")[:10]
            or None,
            "source": workout.get("workoutProvider"),
        }
    )
