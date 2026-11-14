# shadow_bot - SHADOW - NOT A TRADE

Three **paper-only** bots in one repository, run by one daily job at **16:20 ET**:

| Bot | What it logs | Why it exists |
|---|---|---|
| **C9 CONDOR_1D** | Overnight SPY 1-DTE iron condor, opened on range-bound days (RV20 below its 20-day median) | Do real quotes sit inside the backtest's "25% of the spread" fill assumption? |
| **C6 NONEQ_VRP** | Weekly GLD and TLT bull put spreads (about 30 DTE), logged twice: a **REAL** one on the listed chain and a **MODEL** one priced exactly like the backtest (Black-Scholes) | Does a real chain reproduce the model's prices? This is the one question the forward record can actually answer. |
| **C3 SPY_HEDGE** | Monthly SPY put debit spread (0.95 / 0.85 x spot, about 60 DTE), only when the VIX gate is open | A cost ledger for a crash hedge, judged by episodes. Not a profit test. |

**It never trades.** Nothing here places, changes or cancels an order, no message is a trade callout, and every Discord message
starts with `SHADOW - NOT A TRADE`. It posts to its **own** webhook (`SHADOW_DISCORD_WEBHOOK_URL`), never to the live bots'
channel, and its state is never wired into the live combined-drawdown monitor. There is no live-callout mode: the study's verdict was
"build nothing that sends trade callouts from this list", so a callout version would be a separate decision.

## Evidence status (read this first)

None of the three strategies reached Tier 1 or Tier 2 in the research sweep (`claude/options-sweep-top10-verdict.md` in the Personal
investor project). All are **Tier 3**. A shadow record cannot prove an edge for any of them; it can settle three narrower things:

| | C9 | C6 | C3 |
|---|---|---|---|
| Backtest (2022-2025 holdout) | +$1.83 / trade at 25% of the spread, +$0.71 at bid/ask (settled at the chain's underlying). **+$1.47 / +$0.35** on the same 499 trades when settled at the official close, which is what this bot does | +$7.96 / -$0.34 per trade, **model-priced** (418 trades); 2025 supplied 102% of the profit | +$47 / trade on 13 trades (+$614 in all: the two 2022 trades earned +$1,661, the other eleven lost $1,047) |
| What the forward record can settle | Quoted-spread and fill costs only | Whether the model matches real quotes | Nothing statistical; hedge episodes and premium paid |
| Time to 30 / 100 shadow trades | about 2.9 / 9.6 months | about 3.4 / 11.5 months | about 9 / 31 years |

Compare the C9 record with the **official-close** baseline (+$1.47 / +$0.35), not the study's +$1.83 / +$0.71; the weekly summary
prints both. The study's recommended first step is still **Step 0: log real fills on the live condor bot**. That touches the live
repository, so it was not done here.

## Set it up (about 15 minutes; nothing below has been done for you)

1. **Create the repository** (private) and push this directory to `main`:
   ```bash
   cd shadow_bot
   git init -b main && git add -A && git commit -m "Shadow bots: C9 CONDOR_1D, C6 NONEQ_VRP, C3 SPY_HEDGE"
   gh repo create samedame/shadow_bot --private --source . --remote origin --push
   ```
2. **Add the webhook secret** for a *separate* Discord channel (Server settings, Integrations, Webhooks):
   ```bash
   gh secret set SHADOW_DISCORD_WEBHOOK_URL --repo samedame/shadow_bot
   ```
   Use the shadow channel's URL, not the live `DISCORD_WEBHOOK_URL`. This repo only reads `SHADOW_DISCORD_WEBHOOK_URL`.
3. **Check the tests** ran green on the first push (Actions, "Tests"; Python 3.11 and 3.13).
4. **Dry run** (Actions, "Shadow bots - daily job", Run workflow, `dry_run` ticked). It fetches live data and prints what it would
   post; nothing is posted, written or committed. Run it after 16:05 ET on a trading day for a meaningful result (earlier runs are
   labelled `intraday` and only record diagnostics).
5. **Schedule it.** The workflow carries one backup cron line (`20 21 * * 1-5` UTC: 16:20 ET in winter, 17:20 ET in summer), but native schedules ran hours
   late on this account (see `bot-schedule-incident-2026-09-25`). Add the job to the Cloudflare **open-scheduler** Worker and widen its
   Cron Trigger by one hour before **November 2**: step by step in [`docs/OPEN_SCHEDULER.md`](docs/OPEN_SCHEDULER.md). The Worker's own code could not be read
   from here (the field names come from two project notes: `at_et: "16:20"`; Cloudflare numbers weekdays 2-6, so the Cron Trigger is `* 11-21 * * 2-6`); mirror an existing entry.
6. **First run.** The first real run posts a "shadow bots online" message, then one message per new would-be position. If you start
   in the middle of a week, C6 simply waits for its next weekly entry day; nothing is back-filled from before the bot existed.
7. **After five trading days**, run `python diag_report.py` (the one-week data check) and read [`docs/OPERATIONS.md`](docs/OPERATIONS.md), section 3.

To pause everything, disable the workflow in the Actions tab. To stop one strategy, see "Status, flags and retiring" below.

## What you will see in the channel

One compact embed per event, always starting `SHADOW - NOT A TRADE | ...`:

* **Entry** (one per new would-be position): the ticket, credit at mid / base (25%) / natural, max loss, why it qualified, quote time and
  freshness (two clocks: the underlying's last trade, and the option quotes' time, about 15 minutes before the feed file's stamp), and the
  running shadow record against the backtest. C6 shows the REAL and MODEL credit side by side with the ratio.
* **Exit** (once per position per reason): settlement or exit, P&L at mid / base / natural, running record. C6's MODEL twin posts a compact
  exit message too.
* **C3 monthly gate line** (one per month, pass or fail), and an `UNCONFIRMED` line if the published closes never arrived.
* **Weekly summary** (last trading day of the week): job runs, per-strategy record, drawdown against its stop, skips by reason, the
  C6 real/model comparison, and index-quote-versus-published differences.
* **Flags** (`FLAGGED: edge | drawdown | quality | premium`): one message per reason. Logging continues.
* **One data-problem notice per day**, however many things went wrong, and one notice if the job crashes.

Most days are quiet: a closed C9 gate, or a week with nothing to enter, posts nothing (it is counted in the weekly summary).

## Files

| File | What it is |
|---|---|
| `shadow_state.json` | All open positions, counters, skip ledger, outbox of unsent messages (committed by the job) |
| `shadow_log.csv` | One row per closed shadow trade (append-only, keyed by position id): fills at four levels, fees, P&L at five levels, flags |
| `diag_log.jsonl` | One line per run: feed freshness, spot source, how long the job waited for the closing snapshot, problems, gate values. This is what the one-week data check reads |
| `shadow_state_export.json` | Shadow-only export in the combined-drawdown format. **Do not wire it into the live monitor** |
| `data/closes.csv`, `data/indices.csv`, `data/tbill.csv` | Raw (unadjusted) closes, published Cboe index closes, 3-month T-bill. Seeded through 2026-10-12 (checked against Nasdaq, Cboe and Treasury on 2026-10-13), extended by each run |
| `.run_guard_shadow.json` | Duplicate-run marker (its own file, not the live bots') |
| `docs/DESIGN.md`, `docs/OPERATIONS.md`, `docs/OPEN_SCHEDULER.md` | Design and decisions; runbook; the Worker entry |

## Status, flags and retiring

Each strategy has a status in `shadow_state.json`: `active`, `flagged` or `retired`.

* A kill-switch trip sets `flagged`, posts **one message per reason**, and **keeps logging** (stopping the log would destroy the data).
  Checks: trailing-20 mean below 25% of the backtest edge (only once 20 trades are closed, "primed"), drawdown above the stop
  (C9 $3,908, C6 $2,920), and quote quality (more than 4 of the last 20 signals with a median leg spread above 30% of mid). C3 has no edge
  check; its stop is a premium budget you set (`C3_HEDGE_BUDGET` in `config.py`, unset by default).
* `flagged` is sticky. Set it back to `active` by editing `strategies.<ID>.status` in `shadow_state.json`. A reason that still holds
  is not reported a second time unless you also delete its entry from that strategy's `kill_alerts`.
* Only **you** set `retired`: the strategy then opens nothing and posts nothing. Positions already open are still settled and
  logged to `shadow_log.csv`, silently, so the record stays complete.
* Every threshold is a proposal derived from the hold-out tables, not a backtested rule. The edge check will trip by chance (the
  standard error of a 20-trade C9 mean is about $27 against an edge of $1.83): treat it as a prompt to look, not a verdict.

## Promotion and closure triggers (proposals, carried over from the code that came with this build)

The text below came with the uploaded README, which attributes it to `BUILD_SPEC_TOP3.md`. That file was not available to this build, so
the triggers are **unchecked against it**. The one the study's verdict states itself: close C6 if the bid/ask result is still at or below zero, or if one
year still supplies most of the profit.

* **C9.** Promote only if there are at least 100 live condor-bot fills with average slippage at most 25% of the quoted spread **and** at least
  100 forward shadow alerts with a positive natural-fill mean. Close it on a natural-fill mean at or below zero after 100 shadow alerts, or live
  slippage above 50% of the spread.
* **C6.** The forward record gets the same test after 100 entries per underlying: real GLD and TLT chains reproduce a positive mean at natural
  fills and with $0.65 fees, and no year supplies more than half of the profit. Close it if the GLD-only real mean at natural fills is at or below
  zero. Judge the sleeves separately (the uploaded README gave a backtest of GLD +$5,098 and TLT -$1,772; they add up to the study's +$3,326
  total, but the split itself could not be checked against the study's tables).
* **C3.** No statistical trigger exists. Adopt it only if you want crash protection and accept a recurring premium; close it when cumulative
  premium exceeds the budget you set.

Before any of these becomes a live strategy: the account is Robinhood Level 2, and iron condors and put spreads need **Level 3**.

## How it behaves on a bad day

* **Waits for the closing snapshot.** If the Cboe file does not yet hold the closing print (or is too old), the job re-fetches every 45 s for up
  to 9 minutes and never past 16:30 ET. The wait is recorded in `diag_log.jsonl`.
* **No new entries after 16:30 ET** (the quotes are no longer the closing snapshot): skip label `run_late`; settlement, exits and re-marks still run.
* **Stale or missing data skips the entry** (`stale`, `feed_down`) and posts one notice. Another run the same day, inside the 16:30 window, can still
  enter; a later day never back-fills an entry, because the signal is tied to the closing snapshot. (C6's MODEL twin is the exception: it is
  recomputed from official closes and published index values, so it is back-filled for a missed entry day within 12 days.)
* **Early-close days** (13:00 ET): no new entries (`early_close`); settlement still runs.
* **A missed session** is counted (`missed_run`) and shown in the weekly summary.
* **Idempotent.** Re-running a day, or a duplicate trigger, never duplicates a position, a log row or a message (positions are keyed by id,
  messages by key; the outbox is at-least-once and de-duplicated).
* **Discord trouble never loses data.** Unsent messages stay in the outbox and go out on the next run (dropped after 3 days); an embed that is
  rejected is re-sent as plain text.

## Tests

```bash
python -m unittest discover -s tests -t .
```

550 tests, standard library only (the oracle tests use numpy and scipy when installed, `requirements-test.txt`, and skip otherwise), under a minute.
They passed on Python 3.11, 3.12 (12 oracle tests skipped, no numpy there) and 3.13 in the build environment;
`.github/workflows/tests.yml` runs 3.11 and 3.13.

* **Frozen rules and baselines**: every strategy parameter is pinned to the pre-registration's numbers (`tests/test_config.py`).
* **Maths and rules**: Black-Scholes against scipy, gates and quantiles against numpy, strike and expiry selection, fills, fees, settlement.
* **Calendar**: the NYSE rules against literal 2026-2028 tables, and against every date SPY traded from 2016-10-12 to 2026-10-12 and every date
  in Cboe's VIX history.
* **Data**: every parser and every fallback, with simulated outages, bad rows and disagreeing sources; replay of saved files.
* **Operations**: the run guard (every status, duplicates, the wrong-season cron copy), the Discord transport against a local server (403,
  429, 500, revoked webhook, plain-text fallback), the state and idempotency rules, the kill switch, message content, the two workflows' text,
  the export, and **multi-day flows** for each bot on a deterministic synthetic market (entry, hold, exit, outages, crashes, reruns, status changes).
* **Live data, once**: on 2026-10-13 at 16:20 ET the whole job was run against the real sources (Cboe's delayed-quote files for SPY, GLD and
  TLT, the index quotes, Nasdaq, the Treasury; Yahoo and FRED refused from the build environment). It produced a condor ticket, GLD and TLT
  spreads and a hedge candidate, and nothing was posted. The strikes were re-derived by hand from the raw contract lists, the recording is
  kept as a fixture and replayed by `tests/test_real_data.py`, and the seed data was compared row by row with Nasdaq, Cboe and the Treasury.
  What was measured (15-minute feed delay, when the closing print appears, quote drift across the close): `docs/OPERATIONS.md`, section 10.

**Not tested**, because it cannot be done from the build environment: posting to a real Discord webhook, real GitHub Actions and Worker runs,
the `git push` step, how the Cboe files behave on days other than the ones inspected, and the study's own trade lists (the study workspace was not
available). The first week of `diag_log.jsonl` answers the feed questions (`docs/OPERATIONS.md`, section 3). No independent reviewer has read the code.

## Deviations from the spec, and choices to review

* **Shadow only**, and `BUILD_SPEC_TOP3.md` itself was not available: this build follows the frozen pre-registration text and the study's
  verdict in the project documents. Details and the numbered list of decisions: `docs/DESIGN.md`, section 7.
* **Snapshot at about 16:05 ET on a 15-minute-delayed feed**, not the pre-registration's 16:00 ET snapshot.
* **Python 3.13** in `shadow.yml`, as specified. The other repos run 3.11, so the test matrix covers both.
* **Standard library only.** `requirements.txt` is empty, so there is no yfinance fallback for option chains: if the Cboe chain is unavailable the
  entry is skipped (`feed_down`) with one notice. Daily closes come from the chain's own closing print, then Yahoo's chart API, Nasdaq or Stooq.
* **C9 settles at the official SPY close** on expiry day (per the spec's settlement paragraph), not at the chain's underlying as the study did.
* **Closing-print rule.** The underlying's `close` field is used as spot only once its last trade is at or after 15:59 ET (12:59 on early closes);
  otherwise the last price is used and the source is labelled.
* **C6 enters on the first trading day of each ISO week**; the pre-registration says "every Monday". **C6 REAL expiry** is the listed expiry closest
  to 30 DTE inside 23 to 37 days (a guard added here).
* **The 22 uploaded files would not have run as uploaded.** They called names the upload did not define (`chain.REGULAR_CLOSE`,
  `config.EXIT_DRIFT_DAYS`, `market_data.trim_market`, `data_feed.FixtureHttp`, a `run_guard` module, and arguments `Feed.chain` and
  `snapshot_stale` did not accept), so the first real run would have crashed. Those pieces were added, and several real faults were fixed:
  index quote times in two different time zones, a Treasury fallback that read a different (higher) rate, a Discord User-Agent that Discord's
  edge rejects, a close-merge rule that let one bad source overwrite a good close, skip counts that doubled on a rerun, and a C3 confirmation wait
  that counted runs instead of days. The full list is in `docs/DESIGN.md`, section 10. `run_guard.py`, the workflows, the tests and the data
  seeds were written here.
* **No Step 0.** Fill logging in the live condor bot was not touched.
