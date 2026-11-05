"""Optional self-update for copies installed from GitHub.

`garmin-mcp update` moves this install to the repository's latest commit. With `--auto-update`, the
server does the same in the background each time it starts, so a slow network or a failed update
can't delay or stop it. New code takes effect the next time the server starts.

Commits are compared rather than versions, because `pip install --upgrade` leaves a git install
alone unless its version number changed.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
import json
import logging
import subprocess
import sys
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

REPO = "https://github.com/JacobHeart/garmin-mcp"
PACKAGE = "garmin-mcp"
_LOOKUP_TIMEOUT_S = 30
_INSTALL_TIMEOUT_S = 300


class UpdateError(RuntimeError):
    pass


def _state_dir() -> Path:
    path = Path.home() / ".garmin-mcp"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def installed_commit() -> str | None:
    """The commit this copy was installed from, or None if it didn't come from git."""
    try:
        text = importlib.metadata.distribution(PACKAGE).read_text("direct_url.json")
    except importlib.metadata.PackageNotFoundError:
        return None
    return (json.loads(text or "{}").get("vcs_info") or {}).get("commit_id")


def _run(*command: str, timeout: int) -> str:
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, check=True
        )
    except subprocess.CalledProcessError as exc:
        raise UpdateError(f"{command[0]} failed: {exc.stderr.strip()[-500:]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise UpdateError(f"{command[0]} timed out after {timeout}s") from exc
    except OSError as exc:
        raise UpdateError(f"Couldn't run {command[0]}: {exc}") from exc
    return result.stdout


def latest_commit() -> str:
    output = _run("git", "ls-remote", REPO, "HEAD", timeout=_LOOKUP_TIMEOUT_S)
    if not output.strip():
        raise UpdateError(f"{REPO} returned no commits")
    return output.split()[0]


def _pip_install(*args: str) -> None:
    _run(
        sys.executable,
        "-m",
        "pip",
        "install",
        "--quiet",
        "--disable-pip-version-check",
        *args,
        timeout=_INSTALL_TIMEOUT_S,
    )


def _install(commit: str) -> None:
    # --force-reinstall because the version number may not have changed, --no-deps so that only
    # this package is replaced. Pinning the commit installs exactly what was checked.
    _pip_install("--force-reinstall", "--no-deps", f"git+{REPO}@{commit}")

    importlib.invalidate_caches()
    required = importlib.metadata.requires(PACKAGE) or []
    runtime_requirements = [r for r in required if "extra ==" not in r]
    if runtime_requirements:
        _pip_install(*runtime_requirements)  # only installs what's missing or too old


def update() -> str:
    """Move to the latest commit if needed and describe what happened."""
    current = installed_commit()
    if current is None:
        return (
            f"This copy wasn't installed from {REPO} (it may be a development checkout), "
            "so it can't update itself."
        )
    if importlib.util.find_spec("pip") is None:
        raise UpdateError(
            "pip isn't available here. Use the tool that installed this copy to upgrade it."
        )

    # Two servers starting together must not install over each other.
    with (_state_dir() / "update.lock").open("w") as lock:
        if fcntl is not None:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return "Another update is already running."

        latest = latest_commit()
        if latest == current:
            return f"Already up to date ({current[:7]})."
        _install(latest)
    return (
        f"Updated from {current[:7]} to {latest[:7]}. It applies the next time the server starts."
    )


def start_background_update() -> None:
    """Start an update that outlives this call. A problem here must never affect the server."""
    try:
        with (_state_dir() / "update.log").open("w") as log:
            # The child must not inherit stdout, which carries the MCP protocol.
            subprocess.Popen(
                [sys.executable, "-m", "garmin_mcp.cli", "update"],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
    except OSError as exc:
        logger.warning("Couldn't start the background update: %s", exc)
