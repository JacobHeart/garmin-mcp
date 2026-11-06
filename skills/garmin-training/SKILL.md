---
name: garmin-training
description: Report on Garmin training, sleep and recovery data, and build structured workouts for a Garmin watch. Use when the user asks about their training, recovery, sleep or race fitness, mentions Garmin, or wants a workout on their watch or calendar. Requires the garmin-mcp server.
---

# Garmin training

## Choosing what to read

| Question | Tool |
| --- | --- |
| "How has my training been?" | `garmin_activities` |
| "How did that run go?", "Did I hit my targets?" | `garmin_activities` for the id, then `garmin_activity_analysis` |
| "Am I recovered?", "Should I go hard today?" | `garmin_readiness`, then `garmin_sleep` |
| "How did I sleep?" | `garmin_sleep` |
| Steps, stress, body battery, weight | `garmin_body_metrics` |
| "What's my workout today?" | `garmin_upcoming_workouts` |
| "What could I run a 10K in?" | `garmin_race_predictions` |

Every read is a live request and Garmin rate-limits aggressively. Ask for the days the question needs.
`garmin_readiness` and `garmin_body_metrics` cost about three requests per day, so warn the user before
pulling more than a couple of weeks. If you get rate-limited, say so and stop rather than retrying.

## Reporting data

- **A missing field means the device doesn't track it.** Never report it as zero, and never fill it in
  from memory. Today's row is often sparse until the watch syncs.
- Report the totals tools return. Don't add up distances yourself.
- Be specific: "4 runs, 35 km, longest 12 km on Saturday" beats "you ran a lot".
- Give numbers context. HRV of 47 means little alone, but below the user's baseline range it does.
- Recovery is a combination of signals. Readiness, HRV, sleep and body battery can disagree, and when
  they do, say so.
- Cadence units follow the sport: steps per minute for running and walking, revolutions for cycling,
  strokes for swimming.

## Analysing a session

`garmin_activity_analysis` takes the `id` from `garmin_activities` and costs 5-8 requests, so analyse
the sessions the user asks about rather than every run in a week.

- **activity** and **laps** carry every metric the device sent: pace, grade-adjusted pace, heart rate,
  cadence, power, ground contact, stride length, vertical oscillation and ratio, elevation,
  respiration. Laps are the workout's steps when the watch ran a structured workout, and auto-laps
  otherwise.
- **intervals** only appears when work alternated with recovery. `fade_s_per_km` is the last rep's
  pace minus the first's, so positive means slowing down. Heart rate, cadence and power are compared
  first rep to last too.
- **km_splits** are computed from the recording, so they exist whatever the watch's lap settings were.
- **hr_drift** only appears for steady runs of 20 minutes or more. Under about 5% decoupling means
  the effort stayed aerobic. It uses grade-adjusted pace when the device has it. Heat raises it too,
  so check **weather** before blaming fitness.
- **planned_workout** pairs each lap with its step when the session was a scheduled workout, with the
  share of each step spent inside its target. If the counts don't match, it says so instead of
  guessing. Lead with the verdict, such as "2 of 3 reps in range, the last 1 bpm high".
- **series** is the recording averaged into buckets. Its columns depend on the device, and channels
  without a friendly name keep Garmin's own. Use it to spot where pace faded or heart rate climbed.
- **notes** lists any part Garmin couldn't supply. Mention the gap rather than filling it in.
- `show_field_names: true` adds the raw Garmin keys, useful when a new watch reports something odd.

## Building workouts

These write to the user's real Garmin account and can reach their watch.

1. **Check first.** Use `garmin_workouts_list` if the workout might already exist.
2. **Resolve zones yourself.** For "Zone 2", call `garmin_hr_zones` and convert to bpm. Only ask the
   user for numbers if no zones come back.
3. **Write repeats out.** Steps are a flat list, so 5x1km is five interval and recovery pairs.
4. **Say what you built** before or right after creating it: steps, targets and date.

Units: `duration_value` is seconds for `time` and meters for `distance`, and `lap_button` takes none.
Pace targets are seconds per km, with `target_low` as the faster pace. So 4:30-4:45/km is `target_low`
270 and `target_high` 285. Heart rate is bpm and power is watts.

### Changing a workout

- When the user refines a workout you built, such as "make the intervals 3 minutes", use
  `garmin_workout_update`. A new one leaves a duplicate on their watch.
- Update replaces the whole workout. Read it with `garmin_workout_get` and send back the complete step
  list, not just the changed steps.
- Repeat groups come back written out as individual steps, and updating saves them that way. Mention
  this if the user cares how the workout looks on their watch.
- If `source` is another service such as TrainingPeaks, the workout is managed there and your change may
  be overwritten by its next sync. Tell the user before editing it.

### Calendar and deleting

- `garmin_workout_unschedule` takes the `schedule_id` from `garmin_upcoming_workouts`, not the
  `workout_id`. The workout stays in the library.
- To move a workout, schedule it on the new date, then unschedule the old one.
- `garmin_workout_delete` can't be undone. Name the workout and get a clear yes first. Never guess an id.

## When something fails

These are undocumented Garmin endpoints. If a write fails, say so plainly, don't retry blindly, and
suggest checking the Garmin Connect app. If a tool says to run `garmin-mcp login`, the user has to do
that in a terminal, because you can't enter their password for them.

## Staying in your lane

Report what the data shows. Only suggest training plans when asked, and say that Garmin's numbers are
estimates. For pain, illness or anything that sounds medical, suggest a professional rather than
interpreting the data.
