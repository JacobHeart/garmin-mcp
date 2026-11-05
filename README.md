# garmin-mcp

Connect Claude, or any [MCP](https://modelcontextprotocol.io) client, to your Garmin account. It can
read your training and health data and build structured workouts for your watch.

- "Am I recovered enough for intervals today?"
- "How did I sleep this week compared to what Garmin says I need?"
- "Summarize my last two weeks of running."
- "Put 5x1km at threshold on my calendar for Tuesday, with Zone 2 recoveries."

It runs on your own computer and talks only to Garmin. There's no account to create and no database
to set up.

> **Unofficial.** This uses Garmin's private API through the
> [`garminconnect`](https://github.com/cyberjunky/python-garminconnect) library. It isn't affiliated
> with or endorsed by Garmin, and it can break when Garmin changes something.

## Quick start

You need Python 3.12 or newer.

**1. Install**

```bash
pipx install git+https://github.com/AlexYakovlevCa/garmin-mcp
```

Or with [uv](https://docs.astral.sh/uv/):

```bash
uv tool install git+https://github.com/AlexYakovlevCa/garmin-mcp
```

**2. Log in**

```bash
garmin-mcp login
```

Enter your Garmin email, password and MFA code if you use one. It then fetches some of your data, so
you know right away that everything works:

```text
Logged in. Session saved to /Users/you/.garmin-mcp/tokens (your password was not saved).

Fetching your data from Garmin:
  Account: Your Name
  Activities, last 7 days: 4, 30.12 km, 2:38:55. Latest: 2026-01-14 Morning Run
  Latest sleep: 2026-01-14: score 81, 7h 29m
  Training readiness today: 63 (MODERATE)
  Steps yesterday: 12,345
  Next workout: 2026-01-16 Tempo Run (3 in the next 14 days)

All set. Your data is coming through.
```

**3. Connect your AI client**

Claude Code:

```bash
claude mcp add garmin -- garmin-mcp
```

Claude Desktop: run `which garmin-mcp`, then add its full path to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "garmin": { "command": "/full/path/to/garmin-mcp" }
  }
}
```

Other MCP clients: run `garmin-mcp` as a stdio server.

## Tools

| Tool | What it does |
| --- | --- |
| `garmin_activities` | Recent activities with pace, heart rate, cadence, power, training effect and totals |
| `garmin_readiness` | Training readiness, recovery time, HRV, training status and load, VO2max, endurance and hill scores |
| `garmin_sleep` | Sleep score, duration vs. need, stages, overnight heart rate, HRV, stress and respiration |
| `garmin_body_metrics` | Body battery, steps, calories, stress, heart rate, intensity minutes, SpO2, weigh-ins, fitness age |
| `garmin_race_predictions` | Predicted 5K, 10K, half marathon and marathon times, plus VO2max |
| `garmin_hr_zones` | Your own heart-rate zones in bpm |
| `garmin_upcoming_workouts` | Workouts on your calendar |
| `garmin_workouts_list` | Your saved workout library |
| `garmin_workout_get` | One workout with all of its steps |
| `garmin_workout_create` | **Writes.** Create a workout, optionally on a calendar date |
| `garmin_workout_update` | **Writes.** Replace a workout, keeping its id and calendar dates |
| `garmin_workout_schedule` | **Writes.** Put a saved workout on the calendar |
| `garmin_workout_unschedule` | **Writes.** Take a workout off the calendar, keeping it in your library |
| `garmin_workout_delete` | **Writes.** Permanently delete a workout |

The write tools change your real Garmin account. They're marked with MCP's read-only and destructive
hints, so clients that support approvals can ask you first.

### Workout steps

Workouts are a flat list of steps, so 5x1km is written out as five interval and recovery pairs. Each
step needs a `type` (`warmup`, `interval`, `recovery`, `rest`, `cooldown` or `other`) and a way to end:

| `duration_type` | `duration_value` |
| --- | --- |
| `time` | seconds |
| `distance` | meters |
| `lap_button` | none, it ends when you press lap |

A step can also have a `target_type` with a `target_low` and `target_high`:

| `target_type` | Range |
| --- | --- |
| `pace` | seconds per km, where low is the faster pace |
| `heart_rate` | bpm |
| `power` | watts |

```json
{
  "name": "Threshold 1km",
  "sport": "run",
  "scheduled_date": "2026-01-20",
  "steps": [
    { "type": "warmup", "duration_type": "time", "duration_value": 600 },
    {
      "type": "interval", "duration_type": "distance", "duration_value": 1000,
      "target_type": "pace", "target_low": 240, "target_high": 250
    },
    {
      "type": "recovery", "duration_type": "time", "duration_value": 120,
      "target_type": "heart_rate", "target_low": 120, "target_high": 140
    },
    { "type": "cooldown", "duration_type": "lap_button" }
  ]
}
```

## Optional: training guidance for Claude

The server already gives clients its basic rules. For fuller guidance on reporting data and building
workouts in Claude Code, install the included skill:

```bash
mkdir -p ~/.claude/skills && cp -R skills/garmin-training ~/.claude/skills/
```

## Commands and settings

| Command | What it does |
| --- | --- |
| `garmin-mcp` | Run the MCP server over stdio |
| `garmin-mcp login` | Sign in, save the session and show some of your data |
| `garmin-mcp doctor` | Show some of your data using the saved session, with no password needed |

| Environment variable | What it does |
| --- | --- |
| `GARMIN_TOKEN_DIR` | Where the session is saved. The default is `~/.garmin-mcp/tokens` |
| `GARMIN_EMAIL`, `GARMIN_PASSWORD` | Optional, for machines where you can't log in interactively |

If the session expires, tools will ask you to run `garmin-mcp login` again.

## Privacy

- Your password isn't saved by `garmin-mcp login`. Only a session token is, in a folder only your user
  can read. Treat that folder like a password.
- The server sends nothing anywhere except Garmin, and has no telemetry.
- **Whatever a tool returns goes to the AI client you connected**, and is handled under that client's
  terms. That can include sensitive health data such as sleep, heart rate and weight.

## Known limits

- **Tested on one account**, on a watch with training metrics. Basic trackers report less, and missing
  fields are left out rather than shown as zero.
- **Rate limits.** Garmin limits requests aggressively, and every read is a live call. After a 429 the
  server stops calling for 20 minutes.
- **Repeats are written out.** Repeat groups in existing workouts are expanded when read, so updating
  one saves it as a flat list.
- **Pool length.** Swim workouts created here don't set a pool length. Existing ones keep theirs when
  updated.
- **Other services.** Workouts synced from a service such as TrainingPeaks may be overwritten by its
  next sync. These show a `source` field.
- **Your watch.** Workouts reach your watch through Garmin's normal sync. Check a new workout before
  relying on it in a session.

This isn't medical advice, and the numbers are only as good as Garmin's estimates.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
.venv/bin/ruff check .
```

Tests use made-up data and never contact Garmin.

## License

[MIT](LICENSE). Garmin is a trademark of Garmin Ltd., which has no connection to this project.
