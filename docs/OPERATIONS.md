# Operations

SHADOW - NOT A TRADE. How to set the bots up, what a normal day looks like, how to check the data after the first week, and what to do when
something goes wrong. For why the code is built the way it is, see `docs/DESIGN.md`; for the Cloudflare entry, `docs/OPEN_SCHEDULER.md`.

## 1. Set-up checklist

Nothing below was done for you: this build had no GitHub or Cloudflare access.

1. Create the private repository and push this directory to `main`:
   ```bash
   cd shadow_bot
   git init -b main && git add -A && git commit -m "Shadow bots: C9 CONDOR_1D, C6 NONEQ_VRP, C3 SPY_HEDGE"
   gh repo create samedame/shadow_bot --private --source . --remote origin --push
   ```
2. Create a **new** Discord channel and webhook (Server settings, Integrations, Webhooks) and store it as a secret:
   `gh secret set SHADOW_DISCORD_WEBHOOK_URL --repo samedame/shadow_bot`. The code reads only that name, never the live `DISCORD_WEBHOOK_URL`.
3. Repository Settings, Actions, General, Workflow permissions: choose **Read and write permissions**. The workflow already asks for
   `contents: write` itself, so this is normally unnecessary; set it if the "Commit state" step ever fails with a 403.
4. The "Tests" workflow runs on the first push (Python 3.11 and 3.13). It must be green before the first real run.
5. Dry run: Actions, "Shadow bots - daily job", Run workflow, `dry_run` ticked. Run it after 16:05 ET on a trading day. It fetches the live
   data and prints what it would post; nothing is written, posted or committed. Before 16:05 ET it reports `intraday` and does little.
6. Add the Worker entry and (before November 2) the winter Cron Trigger hour: `docs/OPEN_SCHEDULER.md`.
7. After the first scheduled run, check the four things listed under "Verify" in that document.

## 2. A normal day (all times New York)

| Time | What happens |
|---|---|
| 16:00 | The market closes. The Cboe delayed-quote files lag the market by about 15 minutes. |
| 16:20 | The Worker dispatches `shadow.yml`. `run_guard.py` classifies the start (on time, late, too late, duplicate, too early). |
| 16:20 to 16:29 | `shadow_job.py` fetches SPY, GLD and TLT chains. If a file does not yet hold the closing print or is too old, it fetches again every 45 seconds for up to 9 minutes and never past 16:30. |
| after the fetch | Settle yesterday's condor; check C3 exits and the candidate from the first trading day of the month; check the C6 REAL exits and update the MODEL path; then enter: C9 if its gate is open, C6 on the first trading day of the week, C3 on the first trading day of the month. |
| then | Kill-switch checks, one data notice if anything went wrong, the weekly summary on the week's last trading day. State, log and caches are saved, committed to the repository, and the messages are posted. |

The channel is **quiet on most days by design**: a closed C9 gate, a C6 week with nothing to do, or a C3 month between entries produce no
message (skips are counted and appear in the weekly summary). Expect roughly: a C9 entry about every other trading day and its exit the next
evening; two C6 entries each week (GLD, TLT) and an exit about every 1 to 4 weeks; one C3 gate line a month (pass or fail).

## 3. The one-week data check

The pre-registered rules assume a 16:00 ET snapshot. The bot runs at 16:20 on a feed that lags by about 15 minutes, so what it sees is the
market as of about 16:05. Whether that is good enough depends on facts about the free Cboe files that can only be measured live. The job
records them in `diag_log.jsonl` on every run. After five trading days:

```bash
git pull
python diag_report.py             # all runs;  --last 10 for the last ten
```

| In the report | Fine | Act |
|---|---|---|
| runs that had to wait | a few, each catching up within the 9 minutes | on most days: move the target later (section 5) |
| runs where the file never caught up | 0 | any: move the target later, or raise `WAIT_MAX_S`; until then those days' entries are skipped (`stale`) |
| spot source used | `close` for SPY, GLD, TLT | `last` for GLD or TLT on many days: the closing print was missing from their files; relax `CLOSE_PRINT_TOL_MIN` in `config.py` (it is 1 minute) |
| file age, median / max | well under 45 minutes | median above 30: the files are being regenerated slowly; move the target later |
| two-sided quotes (share of clean rows with a bid and an ask) | high for SPY; GLD and TLT somewhat lower | low for GLD/TLT: C6 REAL entries will often be `untradeable`; the MODEL path is unaffected |
| rows without delta | few | many: the REAL strikes would be picked on Black-Scholes deltas from the feed IV (each entry logs the alternative selection) |
| last-vs-close above 0.3% | none | many on one symbol: the `close` field is not the closing print on that symbol |
| provisional vs published index | VIX and VIX3M within a point or so | large and erratic: do not use the provisional C3 gate for anything (it decides nothing; it is only displayed) |
| problems logged | none or a few one-offs | repeated ones: read them; each names its cause |

The weekly summary message prints the same drift numbers for the index quotes, the skip counts by reason, and the run count.

## 4. Reading and editing the files

| File | Notes |
|---|---|
| `shadow_state.json` | Everything the bots remember. Edit only when no run is in progress (not between 16:15 and 16:45 ET on trading days). Pull before editing, push right after, or the job's own commit will conflict with yours. |
| `shadow_log.csv` | Append-only, one row per closed shadow trade (REAL and MODEL versions; MODEL rows have `version=model`). Safe to read anywhere. Do not edit: the id column is the duplicate check. |
| `diag_log.jsonl` | One line per run, trimmed to the newest 600 lines once it passes 1,000. |
| `data/*.csv` | Caches of closes, published index closes and the T-bill. Extended by every run; do not edit. |
| `.run_guard_shadow.json` | The "already ran today" marker. Delete a day's entry only to force a second scheduled-style run on that day. |
| `.delivery_failed` | Written by a run that left Discord messages in the outbox, read by the workflow's last step, removed by the next run that empties the queue. Never committed. |
| `shadow_state_export.json` | A shadow-only export in the shape of the live book's cross-bot export. **Never connect it to the live combined-drawdown monitor.** |

## 5. Status, flags, retiring and resetting

Each strategy block in `shadow_state.json` has `status`: `active`, `flagged` or `retired`.

* **A flag** (`FLAGGED: edge | drawdown | quality | premium`) means one of the proposed thresholds tripped. Logging continues. The flag is sticky.
  To clear it, set `status` back to `"active"`. A reason that still holds will **not** be announced again unless you also delete its entry from
  that strategy's `kill_alerts`.
* **Retire** a strategy by setting `status` to `"retired"`: it then opens nothing and posts nothing. Positions already open are still settled and
  logged silently so the record stays complete. To retire one C6 sleeve only, add `"status": "retired"` inside `strategies.C6.sleeves.GLD` or
  `.TLT`.
* **Reset the drawdown reference** (for example after you decide to continue past a drawdown stop): set the strategy's `peak` to its
  `cum_pnl_base` and `drawdown` to `0`.
* **Reset C3's premium counter**: set `premium_paid_since_reset` to `0` and `premium_reset_date` to today's date. The C3 premium budget itself
  is `C3_HEDGE_BUDGET` in `config.py`; it is unset (`None`) because it is your number. While it is unset the counter is kept and shown but no stop
  is evaluated.
* **Start the whole record over**: delete `shadow_state.json`, `shadow_log.csv` and `.run_guard_shadow.json` (keep `data/`). The next run posts
  the "online" message again and begins at that day; nothing is back-filled.

All thresholds are proposals, not backtested rules. In particular the edge check will trip by chance: the standard error of a 20-trade C9
mean is about $27 against a backtest edge of $1.83 per trade. Treat a flag as a prompt to look at `shadow_log.csv`, not as a verdict.

## 6. Moving the target time

If section 3 shows the files routinely lag the 16:20 target, move it later. Keep every place in step:

1. `config.py`: `TARGET_ET`, and `USABLE_UNTIL_ET` (no new entries after it) and, if needed, `WAIT_MAX_S` (the wait is also capped by `USABLE_UNTIL_ET`).
2. `.github/workflows/shadow.yml`: the guard's `--target` and `--usable-until`, and the native cron minutes (the cron is only a backup; keep it
   at the same minute, on both UTC hours).
3. The Worker entry: the time, and `grace_min` so that time plus `grace_min` equals `USABLE_UNTIL_ET`.
4. Run the tests: `tests/test_config.py` and `tests/test_workflows.py` pin the config and workflow values against each other.

Section 10 shows why an *earlier* target (16:16, nearer the first file that holds the closing print) is worth considering after the one-week data check.
Constraints: the target must be at or after 16:05 ET (`CLOSE_SNAPSHOT_EARLIEST_ET`), and SPY options stop trading at 16:15 ET, so a *later* target
means the SPY option quotes in the file are quotes of a closed market. The condor entry is a shadow entry priced from those quotes; a *live*
version of this bot would have to run earlier than 16:15, and that is a different design.

## 7. When something goes wrong

| What you see | What the bot did | What to do |
|---|---|---|
| One "data problem" message for the day | Listed every problem once; the affected strategy skipped or waited | Read the list. Most are one-off feed hiccups. `diag_log.jsonl` has the detail. |
| `stale` skips in the weekly summary | The Cboe file was not the closing snapshot within the wait | Section 3: move the target later. |
| `feed_down` | A chain could not be read at all (network or layout change) | Open the Cboe URL in `config.py` (`CBOE_CHAIN_URL`) from a browser. If the host or layout changed, the parser needs a change; the log of the run shows the error text. |
| "shadow job crashed" message | Nothing was saved from that run; the backup trigger may retry | Open the Actions log. The traceback names the line. The guard wrote no marker, so a manual "Run workflow" before 16:30 ET (or the next day's run) recovers. |
| No message at all after 16:35 ET on a trading day | Nothing ran, or it ran and had nothing to say | Actions tab: was there a run? If not, the Worker did not dispatch (`docs/OPEN_SCHEDULER.md`). Missed sessions are counted as `missed_run` and appear in the weekly summary. |
| "run started N min after its target" | Settled and exited, opened nothing new | The trigger was late. Check the Worker. |
| The run is red, and only its last step ("Fail the run if Discord messages are stuck") failed; GitHub emails you. Or messages are missing but runs look fine | Discord rejected or refused them (webhook missing, revoked, or Discord down). The job itself stays green and saved everything | The failed messages stay in the outbox for 3 days and go out on the next run; the red step clears itself once the queue is empty. A revoked webhook (404) or a missing secret stops posting entirely: create a new webhook and update the secret `SHADOW_DISCORD_WEBHOOK_URL`. The log and state are unaffected. A broken webhook is only noticed when a message is first sent, so a quiet channel on a quiet day proves nothing; the "online" message and the weekly summary are the proof it works. |
| `C3 ... UNCONFIRMED` | The published VIX/VIX3M closes for the entry day never arrived in 5 sessions | No hedge that month. Check that Cboe's history files still update. |
| The push step fails five times | That run's state never reached the repository (messages already posted stay in Discord) | Rerun the workflow before 16:30 ET: positions are keyed by id, so the rerun converges, at the price of repeating that day's Discord messages. Later than 16:30 ET that day's entries are not recorded. |
| `CalendarRange` / "calendar table ends 2028-12-31" notice | The holiday tables end in 2028 | Extend `EXPLICIT_HOLIDAYS` and `EXPLICIT_EARLY_CLOSES` in `nyse_calendar.py` before the end of 2028. |

## 8. Running it by hand

```bash
python shadow_job.py --dry-run                         # live data, nothing written or posted
python shadow_job.py --dry-run --strategy c9           # one strategy (all | c9 | c6 | c3 | comma list)
python shadow_job.py --root /path/to/copy --now "2026-10-13T16:22" --fixture-dir /path/to/files --dry-run
python diag_report.py --root /path/to/checkout
python -m unittest discover -s tests -t .              # about two minutes
```

`--now` starts the clock at that time (naive means New York time). `--fixture-dir` serves every network source from saved files named
`options_<SYM>.json`, `quote_<NAME>.json`, `history_<NAME>.csv`, `fred_DTB3.csv`, `treasury_bills_<YEAR>.csv`, `yahoo_<SYM>.json`,
`nasdaq_<SYM>.json`, `stooq_<SYM>.csv`; a missing file behaves like a source that is down. With `--fixture-dir` the clock is virtual: the
snapshot-wait loop moves it forward instead of sleeping, so a replay is instant. A dry run never touches the state files. A non-dry run without `--root` writes into the current directory, so use a scratch copy.

A manual run of the real workflow (dry_run unticked) after 16:05 ET and before 16:30 ET does a normal run: it enters, settles, posts and
commits, and does not write the duplicate-run marker (so it cannot block the scheduled run either).

## 9. What was tested, and what was not

The standard-library test suite (550 tests, under a minute) runs on Python 3.11, 3.12 and 3.13 in the build environment, and on
3.11 and 3.13 in `tests.yml`. It covers: the frozen parameters and baselines; the NYSE calendar against the literal tables and against every date SPY
traded between 2016-10-12 and 2026-10-12 and every date in Cboe's VIX history; the chain parser, strike and expiry selection and the
closing-snapshot rules; fills, fees and settlement; Black-Scholes against independent numpy/scipy oracles; the gates and quantiles against
numpy; every data parser and fallback with simulated source failures; the Discord transport against a local server (including 403, 429, 500,
revoked webhooks and plain-text fallback); the run guard (every status, duplicates, the wrong-season cron copy); the state and idempotency
rules; the kill switch; message content; the two workflows (their text is checked, including that only the shadow secret is used); the
export; multi-day flows for each bot on a deterministic synthetic market (entry, hold, exit, outages, crashes, reruns, status changes,
replays from saved files); and one replay of the real 2026-10-13 closing feed (`tests/test_real_data.py`), the only test that does not run
on synthetic data.

What was **not** exercised, because the build environment could not:
* posting to a real Discord webhook, a real GitHub Actions run, a real Worker dispatch, and the real `gh` / git steps in `shadow.yml`;
* the data endpoints from GitHub's runners, and on any day but 2026-10-13. That afternoon Cboe, Nasdaq and the Treasury answered from the
  build environment; Yahoo (HTTP 429) and FRED (connection closed) did not (section 10). Any change they make to their layout is unseen;
* the study's own trade lists: the uploaded rules were compared with the frozen text of the pre-registration, not run against the study's data
  (the workspace holding it, including `BUILD_SPEC_TOP3.md`, was not available to this build);
* the live repositories' own `run_guard.py` (this one was written from its documented contract).

## 10. Measured on 2026-10-13

The bot was run once against the live sources on the day it was built, from the build environment (not from GitHub's runners), with
probes of the same sources every minute or two from 15:55 to 16:35 ET. The recording is in `tests/fixtures/real_20261013/` and is replayed by
`tests/test_real_data.py`. This is one afternoon, so treat each line as "seen once", and check it again in the one-week data check (section 3).

| What | Measured |
|---|---|
| Cboe option files, delay | The data inside a file is **15 minutes and 1 second older than the file's own `timestamp`** (the newest SPY option trade in a file generated at 16:19:32 was at 16:04:31; the same gap in every SPY, GLD and TLT file while their options were trading). The options files are regenerated about once a minute (median gap 60 s, longest 2 minutes); the separate `quotes/_SPY.json`-style files about every 1.8 minutes. |
| The closing print | In the files generated from **16:15:32 ET** on (SPY, GLD) and 16:15:41 (TLT); the files one minute earlier did not have it. Then the underlying's `last_trade_time` is 16:00:00 for SPY and 15:59:59 for GLD and TLT, and the `close` field is the official close (SPY 778.01, GLD 381.65, TLT 78.37), which differs from `current_price` (SPY 777.93 at 16:15:32; `current_price` keeps moving after hours, `close` does not). A run that starts at 16:20 to 16:25 therefore finds it at once; the 45-second wait loop is a safety net, not the normal path. |
| Which minute the option quotes are from | A run that fetches at 16:20 gets option quotes from about **16:05**. SPY options trade until 16:15, so those are live quotes of the SPY option market, with SPY itself trading after hours. GLD and TLT options have no trades after 16:00 (the newest trade in their files is 15:59:59), so their 16:05 quotes do not come from a live market. Entry messages say both clocks: "underlying last trade 16:00, option quotes ~16:04". |
| Does the exact minute matter? | **Yes, at the one-strike level for two of the three tickets.** The production rules were run on every full file saved between data times 15:48 and 16:20 (the files before 16:00 are refused by the bot as pre-close; they are shown only for comparison). SPY: the short call is 783 in all of them; the short put is 773 (15:48), **772** (15:52, 15:57, 16:02) and 773 again from 16:06, so the condor's mid credit is $0.72 at data time 16:02 and $0.83 at 16:06 (the 16:20 run's ticket), $0.82 at 16:13 and $0.81 at 16:20. TLT: short 76 with mid credit $0.25 up to 16:02, 76.5 with $0.315 from 16:06. GLD: 368/360 at $1.52 in every file. The cause is the delta rule itself: the two nearest strikes are about equally far from the 0.16 or 0.25 target, and the feed's deltas move with the underlying's after-hours price. So a shadow ticket is a draw from a band of one-strike variants, not the one answer the study's end-of-day snapshot gave; the position record keeps the quote time (`quote_time`, `quote_file_ts_utc`) and C9's diagnostics keep the Black-Scholes cross-check (`alt_differs`). The earliest file with the closing print (generated 16:15:32) has data from about 16:00:30; moving `TARGET_ET` to 16:16 would take the snapshot about four minutes nearer the close (section 6), at the cost of less slack for the Worker's dispatch lag. That was not done: the choice is better made after the one-week data check. |
| Spreads at that time | SPY 1-DTE legs: 1 cent wide on the short legs and the wings (condor natural fill $0.81 against $0.83 mid). GLD and TLT 30-day legs: 2 to 20 cents wide, median relative spread 4.4%. C3's SPY 60-day puts: 1 to 3 cents wide. |
| Cboe history CSVs (VIX, VIX3M, GVZ, VXTLT) | Last row 2026-10-12 from 16:00 to 16:35 ET (the end of the probe at the time of writing; the 10-13 row was not yet there). Until a published row exists the gates use the delayed index quote, as provisional values, and the next runs replace it. The VIX quote file was still moving at 16:04:31 (the index is computed to 16:15 ET). |
| Nasdaq historical closes | Last row 2026-10-12 for SPY, GLD and TLT from 16:00 to 16:35 ET. The job's own close for the day comes from the chain's closing print; Nasdaq confirms or corrects it on a later run (`closes` in the diagnostics: `added`, `corrected`, `mismatch`). Check in the first week that `spot_vs_close` is 0 and `mismatch` is empty. |
| Treasury bill CSV | The 2026-10-13 row was there at 16:00 ET (13-week bank discount 4.09, used as 0.0409). |
| Yahoo Finance, FRED | **Both failed from the build environment**: Yahoo with HTTP 429 on every try, FRED with a connection closed without an answer (the job retried each three times and moved on). Nothing depended on them. GitHub's runners may fare better or worse; the diagnostics log records which source each number came from. |
| Run time | 119.8 seconds from 16:20:01 to done, almost all of it network (the three retried Yahoo calls, the FRED retries, the 5.9 MB SPY file); no waiting for the closing print. |
| Cboe rate limit | A probe that fetched about eight Cboe files a minute (three 1 to 6 MB option files and five small ones) was answered with HTTP 429 once, for about 40 seconds (16:23:46 to 16:24:27), then recovered by itself. The job's own burst (three option files, four index quotes, four history CSVs) never triggered it. If it did, the wait loop treats an unreadable chain like a pre-close one and fetches it again 45 seconds later, for up to nine minutes. |
| Discord | **Not contacted** from the build environment, so nothing was measured (section 9). Discord's edge is known to refuse the default `Python-urllib` User-Agent with HTTP 403 ("error code: 1010"); `discord_shadow.py` sends a descriptive one, and the unit tests run the transport against a local server. |

What this changes: nothing was retuned. `TARGET_ET` 16:20, `USABLE_UNTIL_ET` 16:30, `WAIT_POLL_S`, `WAIT_MAX_S`, `STALE_FILE_AGE_MIN` and
`CLOSE_PRINT_TOL_MIN` fit what was seen. What was added: the second clock in the entry messages (`CBOE_FEED_DELAY_MIN` in `config.py` is
only used for that label).
