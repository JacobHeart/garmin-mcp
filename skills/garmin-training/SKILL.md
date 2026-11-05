---
name: garmin-training
description: Report on Garmin training, sleep and recovery data, and build structured workouts for a Garmin watch. Use when the user asks about their training, recovery, sleep or race fitness, mentions Garmin, or wants a workout on their watch or calendar. Requires the garmin-mcp server.
---

# Garmin training

## Choosing what to read

| Question | Tool |
| --- | --- |
| "How has my training been?" | `garmin_activities` |
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
