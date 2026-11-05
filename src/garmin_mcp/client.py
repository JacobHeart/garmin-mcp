"""Authenticated access to Garmin Connect, built on the unofficial `garminconnect` library.

Sessions are saved in a token directory and reused, so a password is only needed
for the first login (`garmin-mcp login`) or after a session has expired.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

import garminconnect
from mcp.server.mcpserver.exceptions import ToolError

logger = logging.getLogger(__name__)

DEFAULT_TOKEN_DIR = "~/.garmin-mcp/tokens"
_RATE_LIMIT_COOLDOWN_S = 20 * 60


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
            try:
                api.login(str(prepare_token_dir()))
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
        """Call a garminconnect method, re-authenticating once if the session was rejected."""
        for attempt in (1, 2):
            api = self._session()
            try:
                return getattr(api, method)(*args, **kwargs)
            except garminconnect.GarminConnectTooManyRequestsError as exc:
                self._start_cooldown()
                raise GarminRateLimited(
                    "Garmin rate-limited this IP (429). Wait 15-30 minutes and retry."
                ) from exc
            except garminconnect.GarminConnectAuthenticationError as exc:
                self._api = None
                if attempt == 2:
                    raise GarminAuthRequired(
                        "The Garmin session was rejected. Run `garmin-mcp login` in a terminal."
                    ) from exc
            except Exception as exc:
                raise GarminRequestFailed(
                    f"Garmin request {method} failed: {type(exc).__name__}: {exc}"
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
