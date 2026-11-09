"""Session handling: shared token refreshes and recovering from a rejected session."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import garminconnect
import pytest

from garmin_mcp import client as client_module
from garmin_mcp.client import GarminAuthRequired, GarminClient, share_token_refreshes


class FakeSession:
    """Stands in for garminconnect's token-holding client."""

    def __init__(self, refresh_token: str, expiring: bool = False) -> None:
        self.di_token = "access-" + refresh_token
        self.di_refresh_token = refresh_token
        self.di_client_id = "client"
        self.expiring = expiring
        self.refreshes = 0

    def _token_expires_soon(self) -> bool:
        return self.expiring

    def _refresh_session(self) -> None:
        self.refreshes += 1

    def loads(self, text: str) -> None:
        data = json.loads(text)
        self.di_token = data["di_token"]
        self.di_refresh_token = data["di_refresh_token"]
        self.expiring = data.get("expiring", False)


class FakeApi:
    def __init__(self, session: FakeSession) -> None:
        self.client = session


def _save(directory: Path, refresh_token: str, expiring: bool = False) -> None:
    (directory / "garmin_tokens.json").write_text(
        json.dumps(
            {
                "di_token": "access-" + refresh_token,
                "di_refresh_token": refresh_token,
                "di_client_id": "client",
                "expiring": expiring,
            }
        )
    )


def test_refresh_uses_tokens_another_process_already_renewed(tmp_path: Path) -> None:
    session = FakeSession("old", expiring=True)
    share_token_refreshes(FakeApi(session), tmp_path)
    _save(tmp_path, "new")

    session._refresh_session()

    assert session.di_refresh_token == "new"
    assert session.refreshes == 0  # the stale refresh token was never spent


def test_refresh_renews_when_the_saved_tokens_are_its_own(tmp_path: Path) -> None:
    session = FakeSession("current", expiring=True)
    share_token_refreshes(FakeApi(session), tmp_path)
    _save(tmp_path, "current")

    session._refresh_session()

    assert session.refreshes == 1


def test_refresh_renews_adopted_tokens_that_are_also_expiring(tmp_path: Path) -> None:
    session = FakeSession("old", expiring=True)
    share_token_refreshes(FakeApi(session), tmp_path)
    _save(tmp_path, "newer", expiring=True)

    session._refresh_session()

    assert session.di_refresh_token == "newer"
    assert session.refreshes == 1


def test_refresh_works_without_a_token_file(tmp_path: Path) -> None:
    session = FakeSession("only", expiring=True)
    share_token_refreshes(FakeApi(session), tmp_path)

    session._refresh_session()

    assert session.refreshes == 1


def test_refresh_ignores_a_malformed_token_file(tmp_path: Path) -> None:
    session = FakeSession("only", expiring=True)
    share_token_refreshes(FakeApi(session), tmp_path)
    (tmp_path / "garmin_tokens.json").write_text("not json")

    session._refresh_session()

    assert session.di_refresh_token == "only"
    assert session.refreshes == 1


class Http401(garminconnect.GarminConnectConnectionError):
    """What a write call raises when Garmin rejects the session."""

    def __init__(self) -> None:
        super().__init__("API Error 401")
        self.response = type("Response", (), {"status_code": 401})()


class Rejecting:
    client: Any = None

    def upload_workout(self, payload: Any) -> Any:
        raise Http401()


class Accepting:
    instances = 0

    def __init__(self, *args: Any) -> None:
        Accepting.instances += 1
        self.client = FakeSession("fresh")

    def login(self, tokenstore: str) -> None:
        pass

    def upload_workout(self, payload: Any) -> Any:
        return {"workoutId": 1}


def test_a_401_on_a_write_reloads_the_session_and_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GARMIN_TOKEN_DIR", str(tmp_path))
    monkeypatch.setattr(client_module.garminconnect, "Garmin", Accepting)
    garmin = GarminClient(Rejecting())

    assert garmin.call("upload_workout", {}) == {"workoutId": 1}
    assert Accepting.instances == 1


def test_a_session_rejected_twice_asks_for_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class AlwaysRejecting(Rejecting):
        def __init__(self, *args: Any) -> None:
            self.client = FakeSession("dead")

        def login(self, tokenstore: str) -> None:
            pass

    monkeypatch.setenv("GARMIN_TOKEN_DIR", str(tmp_path))
    monkeypatch.setattr(client_module.garminconnect, "Garmin", AlwaysRejecting)

    with pytest.raises(GarminAuthRequired):
        GarminClient(Rejecting()).call("upload_workout", {})
