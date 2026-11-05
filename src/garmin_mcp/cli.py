"""Command line: `garmin-mcp` serves over stdio, `login` and `doctor` manage the session."""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import sys
from collections.abc import Callable
from datetime import date, timedelta

import garminconnect

from . import __version__, metrics, updater
from .client import GarminClient, GarminError, GarminRequestFailed, prepare_token_dir


def _activities_line(client: GarminClient) -> str:
    today = date.today()
    start = today - timedelta(days=6)
    data = client.call("get_activities_by_date", start.isoformat(), today.isoformat()) or []
    report = metrics.activities_report(data)
    if not report["count"]:
        return "none recorded"
    latest = report["activities"][0]
    return (
        f"{report['count']}, {report['total_distance_km']} km, {report['total_duration']}. "
        f"Latest: {latest.get('start_local', '')[:10]} {latest.get('name', '')}"
    )


def _sleep_line(client: GarminClient) -> str | None:
    today = date.today()
    for day in (today, today - timedelta(days=1)):
        iso = day.isoformat()
        row = metrics.sleep_row(iso, client.call("get_sleep_data", iso))
        if row.get("sleep_min"):
            hours, mins = divmod(row["sleep_min"], 60)
            score = f"score {row['sleep_score']}, " if row.get("sleep_score") else ""
            return f"{iso}: {score}{hours}h {mins}m"
    return None


def _readiness_line(client: GarminClient) -> str | None:
    data = client.call("get_training_readiness", date.today().isoformat())
    fields = metrics.readiness_fields(data, None)
    if "readiness_score" not in fields:
        return None
    return f"{fields['readiness_score']} ({fields.get('readiness_level', 'no level')})"


def _steps_line(client: GarminClient) -> str | None:
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    steps = (client.call("get_stats", yesterday) or {}).get("totalSteps")
    return f"{steps:,}" if steps is not None else None


def _next_workout_line(client: GarminClient) -> str:
    today = date.today()
    end = today + timedelta(days=13)
    schedules = client.call("get_scheduled_workouts_by_period", today.isoformat(), end.isoformat())
    rows = metrics.scheduled_rows(schedules or [])
    if not rows:
        return "nothing scheduled in the next 14 days"
    return f"{rows[0].get('date')} {rows[0].get('name', '')} ({len(rows)} in the next 14 days)"


def _check_data(client: GarminClient) -> bool:
    """Fetch a little real data and print it, proving the connection works end to end."""
    checks: list[tuple[str, Callable[[GarminClient], str | None]]] = [
        ("Account", lambda c: c.call("get_full_name")),
        ("Activities, last 7 days", _activities_line),
        ("Latest sleep", _sleep_line),
        ("Training readiness today", _readiness_line),
        ("Steps yesterday", _steps_line),
        ("Next workout", _next_workout_line),
    ]
    print("Fetching your data from Garmin:")
    fetched = 0
    for label, check in checks:
        try:
            value = check(client)
        except GarminRequestFailed:
            value = None
        except GarminError as exc:  # logged out or rate-limited: every other call would fail too
            print(f"  {label}: {exc}", file=sys.stderr)
            return False
        fetched += value is not None
        print(f"  {label}: {value if value is not None else 'not available from this device'}")
    return fetched > 0


def _login() -> int:
    email = os.getenv("GARMIN_EMAIL") or input("Garmin email: ").strip()
    password = os.getenv("GARMIN_PASSWORD") or getpass.getpass("Garmin password: ")
    api = garminconnect.Garmin(
        email, password, prompt_mfa=lambda: input("Garmin MFA code: ").strip()
    )
    # No tokenstore, so a saved session can't stand in for checking these credentials. The old
    # session is only replaced once this login has succeeded.
    os.environ.pop("GARMINTOKENS", None)
    try:
        api.login()
        tokens = prepare_token_dir()
        api.client.dump(str(tokens))
    except (
        garminconnect.GarminConnectAuthenticationError,
        garminconnect.GarminConnectTooManyRequestsError,
        garminconnect.GarminConnectConnectionError,
        OSError,
    ) as exc:
        print(f"Login failed: {exc}", file=sys.stderr)
        return 1

    print(f"Logged in. Session saved to {tokens} (your password was not saved).\n")
    if not _check_data(GarminClient(api)):
        print("\nLogged in, but no data came back from Garmin.", file=sys.stderr)
        return 1
    print("\nAll set. Your data is coming through.")
    return 0


def _doctor() -> int:
    if not _check_data(GarminClient()):
        print("\nNot working. See the message above.", file=sys.stderr)
        return 1
    print("\nAll set. Your data is coming through.")
    return 0


def _update() -> int:
    try:
        print(updater.update())
    except updater.UpdateError as exc:
        print(f"Update failed: {exc}", file=sys.stderr)
        return 1
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="garmin-mcp", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "command",
        nargs="?",
        default="serve",
        choices=["serve", "login", "doctor", "update"],
        help="serve: run the MCP server over stdio (default). login: sign in to Garmin, save the "
        "session and show some of your data. doctor: show some of your data using the saved "
        "session, without asking for a password. update: install the latest version from GitHub.",
    )
    parser.add_argument(
        "--auto-update",
        action="store_true",
        help="with serve: check GitHub for a new version in the background on every start. It "
        "applies the next time the server starts.",
    )
    args = parser.parse_args()

    # stdout carries the MCP protocol, so all logging goes to stderr.
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)

    if args.command == "login":
        sys.exit(_login())
    if args.command == "doctor":
        sys.exit(_doctor())
    if args.command == "update":
        sys.exit(_update())

    if args.auto_update:
        updater.start_background_update()

    from .server import build_server

    build_server().run("stdio")


if __name__ == "__main__":
    main()
