"""The login and doctor commands, against a fake Garmin library."""

from __future__ import annotations

from datetime import date
from typing import Any

import garminconnect
import pytest

from garmin_mcp import cli


class FakeApi:
    """Stands in for garminconnect.Garmin and records what login did."""

    instances: list[FakeApi] = []
    login_error: Exception | None = None

    def __init__(self, email: str, password: str, prompt_mfa: Any = None) -> None:
        self.email = email
        self.password = password
        self.login_args: tuple[Any, ...] | None = None
        self.dumped_to: str | None = None
        self.client = self
        FakeApi.instances.append(self)

    def login(self, *args: Any) -> None:
        self.login_args = args
        if FakeApi.login_error:
            raise FakeApi.login_error

    def dump(self, path: str) -> None:
        self.dumped_to = path

    def get_full_name(self) -> str:
        return "Test Runner"

    def get_activities_by_date(self, start: str, end: str) -> list[dict[str, Any]]:
        return [
            {
                "activityName": "Morning Run",
                "startTimeLocal": f"{end} 07:00:00",
                "distance": 10000.0,
                "duration": 3000.0,
            }
        ]

    def get_sleep_data(self, day: str) -> dict[str, Any]:
        return {
            "dailySleepDTO": {
                "sleepTimeSeconds": 27000,
                "sleepScores": {"overall": {"value": 83}},
            }
        }

    def get_training_readiness(self, day: str) -> list[dict[str, Any]]:
        return [{"score": 70, "level": "HIGH"}]

    def get_stats(self, day: str) -> dict[str, Any]:
        return {"totalSteps": 12345}

    def get_scheduled_workouts_by_period(self, start: str, end: str) -> list[dict[str, Any]]:
        return []


@pytest.fixture
def fake_login(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    FakeApi.instances = []
    FakeApi.login_error = None
    monkeypatch.setattr(garminconnect, "Garmin", FakeApi)
    monkeypatch.setenv("GARMIN_TOKEN_DIR", str(tmp_path / "tokens"))
    monkeypatch.delenv("GARMIN_EMAIL", raising=False)
    monkeypatch.delenv("GARMIN_PASSWORD", raising=False)
    monkeypatch.setattr("builtins.input", lambda _: "me@example.com")
    monkeypatch.setattr(cli.getpass, "getpass", lambda _: "secret")
    return tmp_path / "tokens"


def test_login_checks_credentials_saves_session_and_shows_data(
    fake_login: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli._login() == 0

    api = FakeApi.instances[0]
    assert api.login_args == ()  # a saved session must not stand in for the password check
    assert api.dumped_to == str(fake_login)

    out = capsys.readouterr().out
    assert "Logged in" in out
    assert "Test Runner" in out
    assert "1, 10.0 km, 50:00" in out
    assert f"Latest: {date.today().isoformat()} Morning Run" in out
    assert "score 83, 7h 30m" in out
    assert "70 (HIGH)" in out
    assert "12,345" in out
    assert "secret" not in out


def test_wrong_password_keeps_the_old_session(
    fake_login: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    FakeApi.login_error = garminconnect.GarminConnectAuthenticationError("bad password")
    assert cli._login() == 1
    assert FakeApi.instances[0].dumped_to is None
    captured = capsys.readouterr()
    assert "Login failed" in captured.err
    assert "Fetching" not in captured.out


def test_missing_metric_is_reported_without_failing(capsys: pytest.CaptureFixture[str]) -> None:
    class BasicTracker(FakeApi):
        def get_training_readiness(self, day: str) -> list[dict[str, Any]]:
            raise garminconnect.GarminConnectConnectionError("not supported")

    assert cli._check_data(cli.GarminClient(BasicTracker("a", "b"))) is True
    assert "Training readiness today: not available" in capsys.readouterr().out


def test_expired_session_fails_with_login_instructions(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class Expired(FakeApi):
        def get_full_name(self) -> str:
            raise garminconnect.GarminConnectAuthenticationError("expired")

        def login(self, *args: Any) -> None:
            raise garminconnect.GarminConnectAuthenticationError("expired")

    monkeypatch.setattr(garminconnect, "Garmin", lambda *a, **k: Expired("a", "b"))
    assert cli._check_data(cli.GarminClient(Expired("a", "b"))) is False
    assert "garmin-mcp login" in capsys.readouterr().err
