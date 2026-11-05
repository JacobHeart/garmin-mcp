"""The MCP server: tool definitions over a live Garmin Connect session."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from . import __version__, metrics
from .client import GarminClient, GarminError, GarminRequestFailed
from .workouts import (
    Sport,
    WorkoutStep,
    build_update,
    build_workout,
    read_steps,
    summarize_workout,
)

INSTRUCTIONS = """\
Live access to the user's Garmin Connect account.

Reporting data:
- A missing field means that device doesn't track it. Never report it as zero or guess a value.
- Report totals as returned by the tools rather than adding numbers up yourself.
- Be specific: "4 runs, 35 km, longest 12 km on Saturday" beats "you ran a lot".
- Every read calls Garmin live and Garmin rate-limits aggressively. Ask for the days you need.

Building workouts:
- Workouts are written to the user's real Garmin account and can reach their watch.
- For a zone-based request such as "Zone 2 run", call garmin_hr_zones and convert to bpm yourself
  instead of asking the user for numbers.
- Write repeats out as a flat list: 5x1km is 5 interval+recovery pairs.
- When the user refines a workout you just built, use garmin_workout_update. A new one leaves a
  duplicate on their watch.
- A workout whose `source` is another service, such as TrainingPeaks, is managed there. Your changes
  may be overwritten by its next sync, so tell the user that before editing one.
- garmin_workout_delete can't be undone. Name the workout and get a clear yes first.
- If a write fails, say so plainly rather than retrying. These are undocumented Garmin endpoints.
"""

READ = ToolAnnotations(read_only_hint=True, open_world_hint=True)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)
REPLACE = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=True)

Days = Annotated[int, Field(ge=1, le=30, description="Number of past days, including today.")]
IsoDate = Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$", description="YYYY-MM-DD")]
WorkoutId = Annotated[str, Field(pattern=r"^\d+$", description="From garmin_workouts_list.")]


def _past_days(days: int) -> list[date]:
    today = date.today()
    return [today - timedelta(days=offset) for offset in range(days)]


def _require_workout_id(created: Any) -> str:
    if not created or "workoutId" not in created:
        raise GarminError(f"Garmin did not return a workoutId: {created}")
    return str(created["workoutId"])


def build_server(client: GarminClient | None = None) -> MCPServer:
    garmin = client or GarminClient()
    mcp = MCPServer("garmin", instructions=INSTRUCTIONS, version=__version__)

    @mcp.tool(annotations=READ)
    def garmin_activities(
        days: Annotated[int, Field(ge=1, le=365)] = 7,
    ) -> dict[str, Any]:
        """Recorded activities for the last N days, newest first, with totals: sport, distance,
        duration, pace, heart rate, cadence, elevation, power, training effect and load."""
        today = date.today()
        start = today - timedelta(days=days - 1)
        data = garmin.call("get_activities_by_date", start.isoformat(), today.isoformat())
        return metrics.activities_report(data or [])

    @mcp.tool(annotations=READ)
    def garmin_readiness(days: Days = 3) -> dict[str, Any]:
        """Training readiness, recovery time, HRV against the user's baseline, training status and
        load (acute, chronic, ratio), VO2max, plus current endurance and hill scores. Use for "am I
        recovered?" or "should I go hard today?". Makes about 3 Garmin requests per day."""
        rows = []
        for day in _past_days(days):
            iso = day.isoformat()
            rows.append(
                {
                    "date": iso,
                    **metrics.readiness_fields(
                        garmin.try_call("get_training_readiness", iso),
                        garmin.try_call("get_hrv_data", iso),
                    ),
                    **metrics.training_status_fields(garmin.try_call("get_training_status", iso)),
                }
            )
        today = date.today().isoformat()
        endurance = garmin.try_call("get_endurance_score", today) or {}
        hill = garmin.try_call("get_hill_score", today) or {}
        return metrics.drop_empty(
            {
                "endurance_score": endurance.get("overallScore"),
                "hill_score": hill.get("overallScore"),
                "days": rows,
            }
        )

    @mcp.tool(annotations=READ)
    def garmin_sleep(days: Days = 7) -> dict[str, Any]:
        """Per night: sleep score, duration against Garmin's sleep need, deep/REM/light/awake
        minutes, overnight heart rate, HRV, stress, respiration and restless moments."""
        return {
            "nights": [
                metrics.sleep_row(d.isoformat(), garmin.try_call("get_sleep_data", d.isoformat()))
                for d in _past_days(days)
            ]
        }

    @mcp.tool(annotations=READ)
    def garmin_body_metrics(days: Days = 3) -> dict[str, Any]:
        """Per day: body battery, steps vs. goal, distance, calories, floors, stress, heart rate
        range, intensity minutes, respiration, SpO2 and any weigh-in; plus current fitness age.
        Makes about 3 Garmin requests per day."""
        dates = _past_days(days)
        start, end = dates[-1].isoformat(), dates[0].isoformat()

        battery = {b.get("date"): b for b in garmin.try_call("get_body_battery", start, end) or []}
        weigh_ins = metrics.weigh_ins_by_date(garmin.try_call("get_body_composition", start, end))
        fitness = garmin.try_call("get_fitnessage_data", end) or {}
        fitness_age = fitness.get("fitnessAge")

        rows = []
        for day in dates:
            iso = day.isoformat()
            rows.append(
                metrics.body_row(
                    iso,
                    garmin.try_call("get_stats", iso),
                    garmin.try_call("get_respiration_data", iso),
                    garmin.try_call("get_spo2_data", iso),
                    battery.get(iso),
                    weigh_ins.get(iso),
                )
            )
        return metrics.drop_empty(
            {"fitness_age": round(fitness_age, 1) if fitness_age else None, "days": rows}
        )

    @mcp.tool(annotations=READ)
    def garmin_race_predictions() -> dict[str, Any]:
        """Garmin's predicted 5K, 10K, half marathon and marathon times, plus current VO2max."""
        predictions = metrics.race_predictions(garmin.call("get_race_predictions"))
        status = garmin.try_call("get_training_status", date.today().isoformat())
        vo2max = metrics.training_status_fields(status).get("vo2max")
        return metrics.drop_empty({"race_predictions": predictions, "vo2max": vo2max})

    @mcp.tool(annotations=READ)
    def garmin_hr_zones(sport: Literal["run", "ride"] = "run") -> dict[str, Any]:
        """The user's own heart-rate zones in bpm, from their Garmin settings. Call this before
        garmin_workout_create whenever a workout is described by zone."""
        zones = metrics.hr_zones(garmin.call("get_heart_rate_zones"), sport)
        if zones is None:
            raise GarminError("No heart-rate zones are configured in Garmin. Ask the user for bpm.")
        return zones

    @mcp.tool(annotations=READ)
    def garmin_upcoming_workouts(
        days: Annotated[int, Field(ge=1, le=60)] = 14,
    ) -> dict[str, Any]:
        """Workouts on the user's Garmin calendar for the next N days, including today. Returns
        each one's schedule_id (for garmin_workout_unschedule) and workout_id."""
        start = date.today()
        end = start + timedelta(days=days - 1)
        schedules = garmin.call(
            "get_scheduled_workouts_by_period", start.isoformat(), end.isoformat()
        )
        return {"upcoming": metrics.scheduled_rows(schedules or [])}

    @mcp.tool(annotations=READ)
    def garmin_workouts_list(
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> dict[str, Any]:
        """Saved workouts in the user's Garmin library with their workout_id. Use it to find a
        workout to change, and to check one doesn't already exist before creating it."""
        workouts = garmin.call("get_workouts", 0, limit) or []
        return {"count": len(workouts), "workouts": [summarize_workout(w) for w in workouts]}

    @mcp.tool(annotations=READ)
    def garmin_workout_get(workout_id: WorkoutId) -> dict[str, Any]:
        """One saved workout with all of its steps, in the format garmin_workout_update accepts.
        Repeat groups come back written out as individual steps."""
        workout = garmin.call("get_workout_by_id", workout_id)
        steps: list[dict[str, Any]] = []
        for segment in workout.get("workoutSegments") or []:
            steps.extend(read_steps(segment.get("workoutSteps") or []))
        return {**summarize_workout(workout), "steps": steps}

    @mcp.tool(annotations=WRITE)
    def garmin_workout_create(
        name: str,
        steps: list[WorkoutStep],
        sport: Sport = "run",
        scheduled_date: IsoDate | None = None,
    ) -> dict[str, Any]:
        """Create a structured workout in Garmin Connect, ready to sync to the user's watch. Give
        scheduled_date to also put it on their calendar. Write repeats out as a flat list: 5x1km
        is 5 interval+recovery pairs. To change an existing workout, use garmin_workout_update."""
        created = garmin.call("upload_workout", build_workout(name, sport, steps))
        workout_id = _require_workout_id(created)
        result: dict[str, Any] = {"workout_id": workout_id, "name": name, "steps": len(steps)}
        if scheduled_date:
            try:
                garmin.call("schedule_workout", workout_id, scheduled_date)
            except GarminRequestFailed as exc:
                raise GarminError(
                    f"Workout {workout_id} was created but could not be scheduled: {exc}"
                ) from exc
            result["scheduled_date"] = scheduled_date
        return result

    @mcp.tool(annotations=REPLACE)
    def garmin_workout_update(
        workout_id: WorkoutId,
        name: str,
        steps: list[WorkoutStep],
        sport: Sport = "run",
    ) -> dict[str, Any]:
        """Replace a workout in place, keeping its id and calendar dates. Garmin replaces the whole
        workout, so pass the COMPLETE step list; read it with garmin_workout_get first if needed."""
        existing = garmin.call("get_workout_by_id", workout_id) or {}
        garmin.call("update_workout", workout_id, build_update(existing, name, sport, steps))
        return {"workout_id": workout_id, "name": name, "steps": len(steps)}

    @mcp.tool(annotations=WRITE)
    def garmin_workout_schedule(workout_id: WorkoutId, scheduled_date: IsoDate) -> dict[str, Any]:
        """Put a saved workout on the user's Garmin calendar for a date."""
        result = garmin.call("schedule_workout", workout_id, scheduled_date) or {}
        return metrics.drop_empty(
            {
                "workout_id": workout_id,
                "scheduled_date": scheduled_date,
                "schedule_id": result.get("workoutScheduleId"),
            }
        )

    @mcp.tool(annotations=REPLACE)
    def garmin_workout_unschedule(
        schedule_id: Annotated[
            str,
            Field(pattern=r"^\d+$", description="From garmin_upcoming_workouts. Not workout_id."),
        ],
    ) -> dict[str, Any]:
        """Remove a workout from the calendar. The workout stays in the user's library."""
        garmin.call("unschedule_workout", schedule_id)
        return {"unscheduled": schedule_id}

    @mcp.tool(annotations=REPLACE)
    def garmin_workout_delete(workout_id: WorkoutId) -> dict[str, Any]:
        """Permanently delete a workout from Garmin Connect, including from the calendar. This can't
        be undone: name the workout and get the user's explicit go-ahead first. Only use an id from
        garmin_workouts_list."""
        existing = garmin.try_call("get_workout_by_id", workout_id) or {}
        garmin.call("delete_workout", workout_id)
        return metrics.drop_empty({"deleted": workout_id, "name": existing.get("workoutName")})

    return mcp
