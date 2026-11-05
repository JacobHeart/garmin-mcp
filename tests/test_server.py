"""The tools as an MCP client sees them, over an in-memory connection to a fake Garmin."""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
from typing import Any

import pytest
from mcp import Client

from garmin_mcp.client import GarminAuthRequired, GarminClient, GarminRequestFailed
from garmin_mcp.server import build_server

EXPECTED_TOOLS = {
    "garmin_activities",
    "garmin_readiness",
    "garmin_sleep",
    "garmin_body_metrics",
    "garmin_race_predictions",
    "garmin_hr_zones",
    "garmin_upcoming_workouts",
    "garmin_workouts_list",
    "garmin_workout_get",
    "garmin_workout_create",
    "garmin_workout_update",
    "garmin_workout_schedule",
    "garmin_workout_unschedule",
    "garmin_workout_delete",
}


class FakeGarmin(GarminClient):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((method, args))
        if method == "upload_workout":
            return {"workoutId": 42}
        if method == "get_stats":
            raise GarminRequestFailed("device doesn't support this")
        return None


def _run(garmin: GarminClient, tool: str, arguments: dict[str, Any]) -> Any:
    async def go() -> Any:
        async with Client(build_server(garmin)) as client:
            return await client.call_tool(tool, arguments)

    return asyncio.run(go())


def test_all_tools_are_listed_with_correct_hints() -> None:
    async def go() -> Any:
        async with Client(build_server(FakeGarmin())) as client:
            return await client.list_tools()

    tools = {t.name: t for t in asyncio.run(go()).tools}
    assert set(tools) == EXPECTED_TOOLS
    assert tools["garmin_sleep"].annotations.read_only_hint is True
    assert tools["garmin_workout_delete"].annotations.destructive_hint is True


def test_create_and_schedule_workout() -> None:
    garmin = FakeGarmin()
    step = {"type": "interval", "duration_type": "time", "duration_value": 60}
    result = _run(
        garmin,
        "garmin_workout_create",
        {"name": "Test", "sport": "swim", "steps": [step], "scheduled_date": "2026-01-02"},
    )
    assert not result.is_error
    assert result.structured_content["workout_id"] == "42"
    method, args = garmin.calls[0]
    assert method == "upload_workout"
    assert args[0]["sportType"]["sportTypeKey"] == "swimming"
    assert garmin.calls[1] == ("schedule_workout", ("42", "2026-01-02"))


def test_bad_step_never_reaches_garmin() -> None:
    garmin = FakeGarmin()
    result = _run(
        garmin,
        "garmin_workout_create",
        {"name": "Test", "steps": [{"type": "interval", "duration_type": "time"}]},
    )
    assert result.is_error
    assert garmin.calls == []


def test_one_unsupported_metric_does_not_sink_the_report() -> None:
    result = _run(FakeGarmin(), "garmin_body_metrics", {"days": 1})
    assert not result.is_error
    assert len(result.structured_content["days"]) == 1


def test_auth_failure_message_reaches_the_client() -> None:
    class LoggedOut(FakeGarmin):
        def call(self, method: str, *args: Any, **kwargs: Any) -> Any:
            raise GarminAuthRequired("Run `garmin-mcp login` in a terminal.")

    result = _run(LoggedOut(), "garmin_workouts_list", {})
    assert result.is_error
    assert "garmin-mcp login" in result.content[0].text


def test_failed_write_reports_the_reason() -> None:
    class Failing(FakeGarmin):
        def call(self, method: str, *args: Any, **kwargs: Any) -> Any:
            raise GarminRequestFailed("Garmin request upload_workout failed: 500")

    step = {"type": "interval", "duration_type": "time", "duration_value": 60}
    result = _run(Failing(), "garmin_workout_create", {"name": "Test", "steps": [step]})
    assert result.is_error
    assert "upload_workout failed" in result.content[0].text


def test_transport_errors_become_request_failures(monkeypatch: Any) -> None:
    class Api:
        def get_workouts(self, *args: Any) -> Any:
            raise OSError("network down")

    garmin = GarminClient()
    monkeypatch.setattr(garmin, "_session", lambda: Api())
    assert garmin.try_call("get_workouts", 0, 1) is None


def test_update_reads_the_workout_first_and_keeps_pool_length() -> None:
    class SwimWorkout(FakeGarmin):
        def call(self, method: str, *args: Any, **kwargs: Any) -> Any:
            self.calls.append((method, args))
            return {"poolLength": 25.0} if method == "get_workout_by_id" else None

    garmin = SwimWorkout()
    step = {"type": "interval", "duration_type": "distance", "duration_value": 100}
    result = _run(
        garmin,
        "garmin_workout_update",
        {"workout_id": "7", "name": "Swim", "sport": "swim", "steps": [step]},
    )
    assert not result.is_error
    assert garmin.calls[0][0] == "get_workout_by_id"
    method, args = garmin.calls[1]
    assert method == "update_workout"
    assert args[1]["poolLength"] == 25.0


def test_upcoming_uses_the_period_endpoint_without_duplicates() -> None:
    garmin = FakeGarmin()
    result = _run(garmin, "garmin_upcoming_workouts", {"days": 40})
    assert not result.is_error
    assert [c[0] for c in garmin.calls] == ["get_scheduled_workouts_by_period"]


@pytest.mark.parametrize(("days", "days_after_today"), [(1, 0), (14, 13)])
def test_upcoming_days_count_today_because_garmin_includes_the_end_date(
    days: int, days_after_today: int
) -> None:
    garmin = FakeGarmin()
    assert not _run(garmin, "garmin_upcoming_workouts", {"days": days}).is_error
    start, end = garmin.calls[0][1]
    assert date.fromisoformat(end) - date.fromisoformat(start) == timedelta(days=days_after_today)
