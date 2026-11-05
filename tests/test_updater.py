"""Self-update, with git and pip replaced by fakes so nothing is installed or fetched."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from garmin_mcp import cli, updater

OLD = "a" * 40
NEW = "b" * 40


@pytest.fixture
def commands(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[tuple[str, ...]]:
    """Record every command run, answering the latest-commit lookup with NEW."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(updater, "installed_commit", lambda: OLD)
    monkeypatch.setattr(
        updater.importlib.metadata, "requires", lambda _: ["mcp>=2", "x; extra == 'dev'"]
    )
    seen: list[tuple[str, ...]] = []

    def run(command: tuple[str, ...], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.append(command)
        stdout = f"{NEW}\tHEAD\n" if command[0] == "git" else ""
        return subprocess.CompletedProcess(command, 0, stdout, "")

    monkeypatch.setattr(updater.subprocess, "run", run)
    return seen


def test_new_commit_reinstalls_that_exact_commit_then_checks_dependencies(
    commands: list[tuple[str, ...]],
) -> None:
    message = updater.update()
    assert "Updated from aaaaaaa to bbbbbbb" in message

    git, package, dependencies = commands
    assert git[:2] == ("git", "ls-remote")
    assert package[:4] == (sys.executable, "-m", "pip", "install")
    assert "--force-reinstall" in package  # pip skips a git install if the version is unchanged
    assert "--no-deps" in package
    assert package[-1] == f"git+{updater.REPO}@{NEW}"
    assert dependencies[-1] == "mcp>=2"
    assert "--force-reinstall" not in dependencies


def test_up_to_date_installs_nothing(
    commands: list[tuple[str, ...]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(updater, "installed_commit", lambda: NEW)
    assert "Already up to date" in updater.update()
    assert [c[0] for c in commands] == ["git"]


def test_development_checkout_is_left_alone(
    commands: list[tuple[str, ...]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(updater, "installed_commit", lambda: None)
    assert "can't update itself" in updater.update()
    assert commands == []


def test_no_pip_gives_a_clear_error(
    commands: list[tuple[str, ...]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(updater.importlib.util, "find_spec", lambda _: None)
    with pytest.raises(updater.UpdateError, match="pip isn't available"):
        updater.update()
    assert commands == []


@pytest.mark.parametrize(
    "error",
    [
        subprocess.CalledProcessError(128, "git", stderr="could not resolve host"),
        subprocess.TimeoutExpired("git", 30),
        FileNotFoundError("git"),
    ],
)
def test_lookup_failure_installs_nothing_and_is_reported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, error: Exception, capsys: Any
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(updater, "installed_commit", lambda: OLD)

    def run(command: tuple[str, ...], **kwargs: Any) -> Any:
        assert command[0] == "git"
        raise error

    monkeypatch.setattr(updater.subprocess, "run", run)
    assert cli._update() == 1
    assert "Update failed" in capsys.readouterr().err


def test_second_update_backs_off_while_one_is_running(
    commands: list[tuple[str, ...]], tmp_path: Path
) -> None:
    if updater.fcntl is None:
        pytest.skip("no file locks on this platform")
    state = tmp_path / ".garmin-mcp"
    state.mkdir()
    with (state / "update.lock").open("w") as held:
        updater.fcntl.flock(held, updater.fcntl.LOCK_EX)
        assert "already running" in updater.update()
    assert commands == []


def test_background_update_keeps_stdout_clear(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    started: dict[str, Any] = {}

    def popen(command: list[str], **kwargs: Any) -> None:
        started.update(command=command, **kwargs)

    monkeypatch.setattr(updater.subprocess, "Popen", popen)
    updater.start_background_update()
    assert started["command"][-2:] == ["garmin_mcp.cli", "update"]
    assert started["stdout"] not in (None, sys.stdout)
    assert started["start_new_session"] is True


def test_background_update_problem_never_reaches_the_server(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))

    def popen(*args: Any, **kwargs: Any) -> None:
        raise OSError("no such file")

    monkeypatch.setattr(updater.subprocess, "Popen", popen)
    updater.start_background_update()  # must not raise


@pytest.mark.parametrize("flags, expected", [([], False), (["--auto-update"], True)])
def test_server_only_updates_when_asked(
    monkeypatch: pytest.MonkeyPatch, flags: list[str], expected: bool
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(updater, "start_background_update", lambda: calls.append("update"))

    class FakeServer:
        def run(self, transport: str) -> None:
            calls.append(transport)

    monkeypatch.setattr("garmin_mcp.server.build_server", lambda: FakeServer())
    monkeypatch.setattr(sys, "argv", ["garmin-mcp", "serve", *flags])
    cli.main()
    assert calls == (["update", "stdio"] if expected else ["stdio"])
