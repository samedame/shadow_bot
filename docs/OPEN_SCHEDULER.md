# Scheduling: the open-scheduler Worker entry

SHADOW - NOT A TRADE. This is the only part of the setup that touches the existing Cloudflare Worker, so it is written out in full.
Nothing in this repository changes the Worker; you add one job entry and widen one Cron Trigger by an hour.

## Why the Worker, and what the native cron is for

GitHub's own `schedule:` trigger ran hours late on this account every day (project doc `bot-schedule-incident-2026-09-25`: 2 to 2.5 hours late
on the control bot, 272 to 296 minutes late on two others). The existing bots therefore fire from the `open-scheduler` Worker with a precise
`workflow_dispatch`, and keep a native cron only as a wide, DST-safe backup. `shadow.yml` does the same:

| Trigger | When | What `run_guard.py` does with it |
|---|---|---|
| Worker, `trigger=cf-scheduler`, `scheduled_for_et=<16:20 ET>` | 16:20 ET, any season | Runs it. Writes the day's marker `.run_guard_shadow.json` after a clean exit. |
| Native cron `20 21 * * 1-5` (UTC, GitHub numbers Sunday 0; one line since 2026-11-13) | 21:20 UTC, every weekday | 16:20 ET in winter, 17:20 ET in summer. When the Worker run already happened it is a silent duplicate. If the Worker run did not happen, a late native run runs flagged: it settles and exits but opens nothing new (`run_late`) once it is past 16:30 ET. The earlier 20:20 UTC line was removed: in winter it was 15:20 ET, `too_early`, skipped every day. |
| "Run workflow" button | any time | A manual run: ignores and never writes the marker. Tick `dry_run` to print what would be posted and write nothing. |

## 1. The job entry

In the Worker's job list add one entry shaped like the existing ones (`condor-stage`, `ibs-dip-calls`). The project notes give that shape as
`{ id: "condor-stage", repo: "samedame/daytrading_bot", workflow: "stage.yml", ref: "main", at_et: "09:15", grace_min: 10, watchdog_min: 4 }` and, for `ibs-dip-calls`, `at_et` 15:45 ET,
`grace_min: 13`, workflow `daily-signal.yml`, ref `master`; they also mention a `repo` for each job, a `watchdog_min`, and optional
`weekdays` and `skip_holidays`.

```js
{
  id: "shadow-daily",
  repo: "samedame/shadow_bot",   // keep this line only if your other entries carry a repo field
  workflow: "shadow.yml",
  ref: "main",
  at_et: "16:20",          // ET, like "15:45" on ibs-dip-calls
  grace_min: 10,           // usable until 16:30 ET, as ibs-dip-calls is 15:45 + 13 = 15:58; shadow.yml's guard uses the same 16:30
  watchdog_min: 4,
  // weekdays and skip_holidays: leave unset, like the other trading-day jobs. The Worker skips weekends and exchange holidays itself.
  // If your entries carry a failure-message field, use: "SHADOW - NOT A TRADE | shadow-daily did not start; see the Actions tab."
}
```

**Checked against the deployed Worker (read through the Cloudflare API on 2026-10-13):** jobs live in the bundled `jobs.json` (`id`, `repo`, `workflow`, `ref`, `at_et`, `grace_min`, `watchdog_min`, optional `enabled`, `weekdays`, `skip_holidays`, `fallback_message`); the Worker dispatches with inputs `scheduled_for_et` and `trigger: "cf-scheduler"`. Its Cron Triggers were `* 0-1 * * 2-6` and `* 11-20 * * 2-6`. If the Worker is built from a repository of yours, make the same edit there, or the next deploy from it will drop the entry.

The Worker dispatches with the same two inputs as the other bots' workflows: `trigger` (value `cf-scheduler`) and `scheduled_for_et`.
`shadow.yml` declares both. Its secret is **`SHADOW_DISCORD_WEBHOOK_URL`** in the `shadow_bot` repository, not the Worker's.

The token the Worker dispatches with (`GH_PAT`) was replaced on 2026-09-25 with one that has `repo` and `workflow` scope across the whole
GitHub account (project doc, "Resolution, part 2"), so the new repository is covered. If the first dispatch returns 404, that token is the
first thing to look at.

## 2. The Cron Trigger (needed before November 2)

The Worker wakes on Cron Triggers written in UTC. The project docs list them as `* 11-20 * * 2-6` (plus `* 0-1 * * 2-6` for the evening
jobs). `* 11-20` covers up to 20:59 UTC, which is 16:59 ET in summer but only 15:59 ET in winter.

| Season | 16:20 ET is | Covered by `* 11-20 * * 2-6`? |
|---|---|---|
| Summer (EDT, UTC-4), until Saturday, October 31, 2026 | 20:20 UTC | yes |
| Winter (EST, UTC-5), first session Monday, November 2, 2026, until Saturday, March 13, 2027 | 21:20 UTC | **no** |

Change the trigger to **`* 11-21 * * 2-6`** before November 2 (and leave it; it is harmless in summer, the Worker only fires jobs whose ET time
has come). This replaces the existing trigger, so the number of Cron Triggers on the Worker does not change. The existing 15:45 ET and 09:15 ET
jobs are unaffected. If you would rather not touch the trigger, the alternative is to let the native cron be the winter trigger, which is
the unreliable one: on every day its copy starts after 16:30 ET the bots settle and exit but open nothing new.

## 3. Verify

On the first trading day after deploying, after about 16:25 ET:

1. Actions tab, "Shadow bots - daily job": a run named after `workflow_dispatch`, started within a minute or two of 16:20 ET. The log's first
   guard line reads `target 16:20 ET ... late +0.x min -> on_time (trigger cf-scheduler)`.
2. The repository root has a new commit "state: shadow job ... (cf-scheduler)" and `.run_guard_shadow.json` holds
   `"shadow-daily:<date>": {"done_at": ..., "late_min": ..., "status": "on_time", "trigger": "cf-scheduler"}`.
3. The Discord channel shows the "shadow bots online" message (first run only) and, if any strategy qualified, its entry message.
4. Later that day, the native cron copy (20:20 or 21:20 UTC) appears in Actions and ends with "duplicate, skipped silently" or, in winter,
   "wrong-season copy of the native cron: skipped silently". Seeing that is the proof the guard works.

If step 1 never happens: Cloudflare dashboard, Workers, `open-scheduler`, Logs; the usual causes on this account were the `GH_PAT` secret
and a Cron Trigger that did not cover the hour.

## 4. Roll back

Remove the `shadow-daily` entry and redeploy. The workflow then runs only from the native cron (and the manual button): runs arrive hours late,
so entries are skipped (`run_late`) while settlements and exits still happen. To stop the bots entirely, disable the workflow in the Actions tab;
nothing else needs undoing (open shadow positions simply stay open until the workflow runs again).

## 5. Things that are normal

* **Holidays**: the Worker skips them with its own table; the bot also knows them (`nyse_calendar.py`) and would do nothing on a closed day.
* **Early closes (13:00 ET)**: November 27 and December 24, 2026; November 26, 2027; July 3 and November 24, 2028. The Worker still fires at
  16:20 ET; the bot's closing snapshot on those days is 13:05 ET, so a run at 16:20 ET settles and exits, and opens nothing new (`early_close`).
* **Moving the time**: change `TARGET_ET`, `USABLE_UNTIL_ET` and the guard arguments in `shadow.yml` together (the tests that pin them,
  `tests/test_config.py` and `tests/test_workflows.py`, will tell you what is out of step), then change the Worker entry to the same time and
  `grace_min` to the same window. See `docs/OPERATIONS.md`, "Moving the target time".
