"""Authenticated access to Garmin Connect, built on the unofficial `garminconnect` library.

Sessions are saved in a token directory and reused, so a password is only needed
for the first login (`garmin-mcp login`) or after a session has expired.

Garmin's access token lasts about a day and is renewed with a refresh token, and every
renewal returns a new refresh token that replaces the old one. Each MCP client starts its
own server process, and they all share one token file, so the processes coordinate: a
refresh happens under a lock on the token directory, and a process first picks up tokens
another process has already renewed instead of spending its own stale refresh token.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import garminconnect
from garminconnect.client.tokens import token_file_path
from mcp.server.mcpserver.exceptions import ToolError

try:
    import fcntl
except ImportError:  # Windows: no cross-process lock, refreshes still work in one process
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

DEFAULT_TOKEN_DIR = "~/.garmin-mcp/tokens"
_RATE_LIMIT_COOLDOWN_S = 20 * 60
_LOCK_FILE = ".refresh.lock"


class GarminError(ToolError):
    """A failure with a message that is shown to the MCP client."""


class GarminAuthRequired(GarminError):
    pass


class GarminRateLimited(GarminError):
    pass


class GarminRequestFailed(GarminError):
    """One request failed, for example because this device doesn't support the endpoint."""


def token_dir() -> Path:
    return Path(os.getenv("GARMIN_TOKEN_DIR") or DEFAULT_TOKEN_DIR).expanduser()


def prepare_token_dir() -> Path:
    """Create the token directory, readable only by the current user."""
    path = token_dir()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


@contextlib.contextmanager
def refresh_lock(directory: Path) -> Iterator[None]:
    """Hold an exclusive lock shared by every garmin-mcp process using this token directory."""
    if fcntl is None:
        yield
        return
    fd = os.open(directory / _LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # closing releases the lock


def _adopt_saved_tokens(session: Any, directory: Path) -> bool:
    """Load the saved tokens if another process has renewed them since this one loaded."""
    try:
        saved = token_file_path(str(directory)).read_text(encoding="utf-8")
        refresh_token = json.loads(saved).get("di_refresh_token")
    except Exception:  # missing, unreadable or malformed: keep what's in memory
        return False
    if not refresh_token or refresh_token == session.di_refresh_token:
        return False
    try:
        session.loads(saved)
    except Exception:
        return False
    logger.info("Picked up Garmin tokens renewed by another garmin-mcp process.")
    return True


def share_token_refreshes(api: Any, directory: Path) -> None:
    """Route this session's token refreshes through the shared lock and token file.

    Without this, two processes holding the same refresh token both try to renew it.
    The first succeeds and the refresh token it used stops working, so the second is
    rejected, and keeps failing with 401 until someone logs in again.
    """
    session = getattr(api, "client", None)
    refresh = getattr(session, "_refresh_session", None)
    if refresh is None:  # a library version without this hook: refreshes stay uncoordinated
        logger.warning("Can't coordinate Garmin token refreshes with this garminconnect version.")
        return

    def coordinated_refresh() -> None:
        with refresh_lock(directory):
            if _adopt_saved_tokens(session, directory) and not session._token_expires_soon():
                return
            refresh()  # the library saves the renewed tokens to the token file

    session._refresh_session = coordinated_refresh


def _is_auth_rejection(exc: Exception) -> bool:
    if isinstance(exc, garminconnect.GarminConnectAuthenticationError):
        return True
    # Write calls (create, schedule, ...) report a rejected session as a plain HTTP 401.
    return getattr(getattr(exc, "response", None), "status_code", None) == 401


class GarminClient:
    """A lazily authenticated session. One instance serves one Garmin account."""

    def __init__(self, api: Any = None) -> None:
        self._api = api  # an already logged-in session, or None to load the saved one
        self._lock = threading.Lock()
        self._rate_limited_until = 0.0

    def _session(self) -> Any:
        with self._lock:
            if self._api is not None:
                return self._api

            remaining = self._rate_limited_until - time.monotonic()
            if remaining > 0:
                raise GarminRateLimited(
                    f"Garmin is rate-limiting this IP. Try again in {int(remaining // 60) + 1} min."
                )

            email = os.getenv("GARMIN_EMAIL")
            password = os.getenv("GARMIN_PASSWORD")
            api = (
                garminconnect.Garmin(email, password)
                if email and password
                else garminconnect.Garmin()
            )
            directory = prepare_token_dir()
            share_token_refreshes(api, directory)
            try:
                api.login(str(directory))
            except garminconnect.GarminConnectTooManyRequestsError as exc:
                self._start_cooldown()
                raise GarminRateLimited(
                    "Garmin rate-limited this IP (429). Wait 15-30 minutes and retry."
                ) from exc
            except garminconnect.GarminConnectAuthenticationError as exc:
                raise GarminAuthRequired(
                    "Not logged in to Garmin, or the saved session has expired. "
                    "Run `garmin-mcp login` in a terminal."
                ) from exc
            self._api = api
            return api

    def _start_cooldown(self) -> None:
        self._rate_limited_until = time.monotonic() + _RATE_LIMIT_COOLDOWN_S

    def call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        """Call a garminconnect method, re-authenticating once if the session was rejected.

        Re-authenticating reloads the token file, so it also picks up tokens that another
        process or a fresh `garmin-mcp login` saved after this session started.
        """
        for attempt in (1, 2):
            api = self._session()
            try:
                return getattr(api, method)(*args, **kwargs)
            except garminconnect.GarminConnectTooManyRequestsError as exc:
                self._start_cooldown()
                raise GarminRateLimited(
                    "Garmin rate-limited this IP (429). Wait 15-30 minutes and retry."
                ) from exc
            except Exception as exc:
                if not _is_auth_rejection(exc):
                    raise GarminRequestFailed(
                        f"Garmin request {method} failed: {type(exc).__name__}: {exc}"
                    ) from exc
                with self._lock:
                    if self._api is api:
                        self._api = None
                if attempt == 2:
                    raise GarminAuthRequired(
                        "The Garmin session was rejected. Run `garmin-mcp login` in a terminal."
                    ) from exc
        raise AssertionError("unreachable")

    def try_call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        """Like `call`, but a failure of this one metric returns None.

        Devices differ in what they support, so one missing endpoint shouldn't
        sink a whole report. Auth and rate-limit failures still raise: every
        later call would fail the same way.
        """
        try:
            return self.call(method, *args, **kwargs)
        except GarminRequestFailed as exc:
            logger.warning("%s", exc)
            return None
