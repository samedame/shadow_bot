# Design

SHADOW - NOT A TRADE. This repository logs would-be trades for three strategies from the options-sweep study
(`claude/options-sweep-top10-verdict.md`, pre-registration frozen 2026-10-08T18:53:59Z, SHA-256 `57dd3806...67ce10`).
It places, changes and cancels nothing, and no message it posts is a trade callout.

## 1. Why it is shadow-only

The verdict is "no candidate reached Tier 1 or Tier 2; build nothing that sends trade callouts from this list". C9, C6 and C3 are
Tier 3. A shadow record cannot prove an edge for any of them. It can settle three narrower things, and that is what each bot is for:

| Bot | The question the forward record can answer | What it cannot answer |
|---|---|---|
| **C9 CONDOR_1D** (SPY overnight 1-DTE iron condor, range-bound days) | Do real quotes sit inside the backtest's "fill at 25% of the spread" assumption? Is the net edge still positive at bid/ask? | Whether the edge is real: the backtest edge (+$1.83 per trade, +$0.71 at bid/ask) is smaller than fill uncertainty. |
| **C6 NONEQ_VRP** (GLD and TLT weekly bull put spreads, about 30 DTE) | Does a real GLD / TLT chain reproduce the Black-Scholes prices the backtest used? Every entry is logged twice, REAL and MODEL, so the ratio is measured. | Anything about profit before the model is checked: the backtest had no real GLD or TLT quotes. |
| **C3 SPY_HEDGE** (monthly gated SPY put debit spread) | What does the hedge cost, and did it pay in the drawdowns that happened? | Anything statistical: the holdout had 13 trades, and about 30 shadow trades would take 9 years. |

The recommended Step 0 from the spec, logging the live condor bot's real fills, is **not built**: it needs changes to a live repository,
and the instructions for this build were to leave the live repositories alone.

## 2. Shape of the system

```
Cloudflare open-scheduler Worker --workflow_dispatch(trigger=cf-scheduler, scheduled_for_et)--+
GitHub native cron (20:20 and 21:20 UTC, DST-safe backup) ------------------------------------+--> shadow.yml
manual "Run workflow" -------------------------------------------------------------------------+
                                                                                               |
   run_guard.py  (too early -> skip silently | duplicate -> skip silently | late -> run, flagged | too late -> run, no new entries)
        |
   shadow_job.py
        1 data     fetch_chains -> refresh_closes -> refresh_indices -> refresh_tbill
        2 upkeep   C9 settlement | C3 exits + candidate confirmation + hedge ledger | C6 real exits + model path
        3 entries  C9 condor | C6 weekly REAL + MODEL pair | C3 monthly candidate
        4 review   kill switch | one data notice | weekly summary | first-run hello
        5 persist  log rows -> state -> market caches -> export; THEN post the outbox, saving state after each post
        |
   commit step: shadow_state.json, shadow_log.csv, diag_log.jsonl, shadow_state_export.json, .run_guard_shadow.json, data/
```

| Module | Role |
|---|---|
| `config.py` | Every frozen parameter, the schedule, thresholds, URLs, file names. `tests/test_config.py` pins the frozen values to numbers transcribed from the pre-registration text. |
| `nyse_calendar.py` | Trading days, holidays and 13:00 early closes, 1998 to 2028, as a rule generator checked against explicit tables. Raises `CalendarRange` outside those years rather than guess. |
| `chain.py` | The clean option-chain model, the Cboe parser, strike and expiry selection (ports of the study's `engine.py`), the closing-print test and the stale-snapshot test. |
| `fills.py` | Fills at mid / base (25% of the spread) / natural (50%) / 3x (75%), fees, packages, settlement, max loss, the P&L record. |
| `rules_c9.py`, `rules_c6.py`, `rules_c3.py` | Pure strategy rules. No I/O. |
| `market_data.py` | Raw closes, published and provisional index closes, the T-bill, the C9 gate and the C3 quantile, merging of sources, the CSV caches. |
| `data_feed.py` | Every network source, plus `FixtureHttp` to replay saved files. |
| `jobctx.py`, `job_c9.py`, `job_c6.py`, `job_c3.py` | The run context and the wiring of each strategy into the daily run. |
| `state.py` | `shadow_state.json`, the trade log, the message outbox, the skip ledger. |
| `kill_switch.py` | The shadow form of the kill switch (flag and keep logging). |
| `messages.py`, `discord_shadow.py` | Message text; the Discord transport. |
| `run_guard.py` | Lateness and duplicate guard (own copy; own marker file `.run_guard_shadow.json`). |
| `export_state.py` | Shadow-only export in the shape of the live book's cross-bot risk export. **Not for the live drawdown monitor.** |
| `diag_report.py` | Reads `diag_log.jsonl` and prints the one-week data check. |

## 3. The frozen rules, as built

Every number below is in `config.py` and is pinned by `tests/test_config.py`.

**C9 CONDOR_1D.** On a trading day d at the close snapshot:
* Gate: RV20(p) < the median of the 20 RV20 values ending at p, where p is the previous trading day and is included in the window
  (the reading behind the reported +$913 on 499 trades). RV20 is the sample standard deviation (ddof=1) of the last 20 close-to-close
  returns of the raw SPY close. The other reading (p excluded; 501 trades, +$851.80) is logged on every entry but never decides.
* Expiry: the next trading day, which must be listed. Shorts: the call and the put whose feed |delta| is closest to 0.16 (best miss at
  most 0.10), liquid, bid > 0. Wings: the listed strike nearest the short strike +/- 1.0% of spot, beyond the short strike.
* Every leg must be quoted and liquid: spread at most max($0.05, 30% of mid). Base-fill credit must be positive.
* Held to expiry and settled at intrinsic value on the official SPY close of the expiry date, no closing fee. Fees $0.05 per contract per
  leg per side (so $0.20 for the four entry legs).

**C6 NONEQ_VRP.** First trading day of each ISO week, GLD and TLT separately.
* MODEL (the backtest exactly): S0 = raw close; flat volatility GVZ/100 (GLD) or VXTLT/100 (TLT); the 3-month T-bill rate; expiry =
  entry + 30 calendar days; width = max(1, round(2% of S0)) with Python's banker's rounding; short strike = the integer strike whose
  Black-Scholes |put delta| is closest to 0.25; half-spread max($0.03, 4% of the value). Exit at the first day the mid-mark is at most 50% of
  the base credit, else the first day with DTE at most 7. It is recomputed statelessly from official closes and **published** index
  values, so it lags the real path by up to a day.
* REAL (new, no backtest): the listed expiry closest to 30 DTE inside 23 to 37 days; short put with feed |delta| closest to 0.25;
  long put at the listed strike nearest short minus the width; same liquidity filter as C9; checked on every daily run against the 50%
  profit target and the DTE-7 rule.

**C3 SPY_HEDGE.** First trading day of each month. Gate: VIX/VIX3M strictly below 0.90 **and** VIX at or below the 25th percentile
(numpy "linear") of the previous 252 published VIX closes (at least 200 required). Buy the listed put nearest 0.95 x spot and sell the
one nearest 0.85 x spot, expiry closest to 60 DTE inside 50 to 75 (ties go to the shorter), exit on the first trading day with DTE at most 21.
VIX and VIX3M closes are published only after the close, so the job stores a **candidate** priced at the entry-day snapshot and applies the
gate to the published closes on a later run (up to 5 sessions; otherwise the month is logged `UNCONFIRMED`).

**Fills and costs (all strategies).** k = 0 (mid), 0.25 (base), 0.5 (natural), 0.75 (3x) of the quoted spread from mid; fee $0.05
per contract per leg per side, $0.65 for the stress column. P&L is recorded at every level in `shadow_log.csv`.

## 4. Data and the closing snapshot

| Need | Source (in order) |
|---|---|
| Option chains SPY, GLD, TLT | Cboe delayed-quote JSON (`cdn.cboe.com`, which answers HTTP 307 to `cdn-api.cboe.com`; the redirect is followed) |
| Raw daily closes | the chain file's own `close` once the closing print is in (rank 0), then the first of Yahoo chart API (rank 2), Nasdaq historical API (rank 1), Stooq (rank 1) that answers; the committed `data/closes.csv` seed counts as rank 2 |
| VIX, VIX3M, GVZ, VXTLT, published | Cboe index-history CSVs, after the close |
| Same indices, provisional | Cboe index delayed-quote JSON, used only for the "provisional" C3 reading and the C6 provisional model credit; never decides a position |
| 3-month T-bill | FRED DTB3, then Treasury.gov "13 WEEKS BANK DISCOUNT" (the same quantity; **not** the par-yield column, which runs about 0.2 points higher) |

Merging rule for closes: a source of higher rank may replace a stored close when the two differ by no more than 2%; a wider disagreement keeps
the stored value and is reported (`mismatch`), because two sources more than 2% apart means one of them is wrong and nothing here can tell
which (found while simulating a source that returned a nonsense value for one day: the uploaded code let it overwrite the real close). A source of equal or
lower rank never replaces a stored close; it is reported when it disagrees by more than 0.5 cent.

**The closing snapshot.** The Cboe options file lags the market by about 15 minutes, so a run at 16:20 ET sees the market as of about
16:05 ET. The underlying's `close` field simply repeats the last price during the session, so it is trusted only once the underlying's last
trade is at or after 15:59 ET (12:59 on early closes) on the session date. A chain that does not yet hold the closing print, or whose file
is older than 45 minutes, is "stale": the job re-fetches every 45 seconds for up to 9 minutes and never past 16:30 ET, and a symbol that
never catches up skips its entry (`stale`) and posts one data notice. C9 settlement never needs a chain: it uses the stored official SPY
close of the expiry date. A C3 or C6 exit needs its legs quoted in a closing-snapshot chain; if the chain is stale or a leg is not quoted,
the exit is retried on the next run and counted as an attempt, and the fourth failed attempt is logged `unpriced` (no P&L; counted
separately).

## 5. State, idempotency, crash safety

* **Positions** are keyed by id (`C9-<date>`, `C6-<TICKER>-<date>`, `C6M-<TICKER>-<date>`, `C3-<date>`). A second run of the same session
  cannot open a second copy.
* **Skips** are counted once per (session date, strategy, reason), however often the session is re-run.
* **Messages** are queued with a deterministic key before posting, posted oldest first, and moved to `sent` with the state saved after
  each post. A failed post stays queued and is dropped after 3 days. A webhook that is down or refuses us stops the loop for that run; an
  embed Discord rejects (HTTP 400) is re-sent once as plain text.
* **Persistence order:** log rows first (deduplicated by id), then state, then the market caches and the export, then the messages. A crash
  between the log and the state is repaired by the next run (the log row is not duplicated and the statistics are counted once).
* **A crash** during the run saves no strategy state; it records a once-a-day note, adds a diagnostics line (`crashed`), posts one
  "shadow job crashed" notice and exits non-zero, so the guard writes no marker and the backup trigger may retry. A crash after the
  log has been written (during the state save or the posting) is repaired by the next run, which does not duplicate the log row.
* **Phases:** `closed_day` (weekend or holiday) does nothing; `intraday` (before 16:05 ET, 13:05 on early closes) records diagnostics only;
  `close` is the real run. All transitions are keyed to dates, official closes and published index values, never to the wall clock, so a
  late or repeated run can delay a transition but not corrupt one.

## 6. Kill switch (shadow form)

All thresholds are **proposals** derived from the holdout tables, not backtested rules. A trip sets the strategy's status to `flagged`,
posts **one** message per reason, and **keeps logging** (stopping the log would destroy the data). Only the user sets `retired` (the strategy
then opens nothing and posts nothing; positions already open are still settled and logged, silently).

| Check | Trips when | Notes |
|---|---|---|
| edge (C9, C6) | after 20 closed trades, the trailing-20 mean base-fill P&L is below 25% of the backtest edge | Will trip by chance (the standard error of a 20-trade C9 mean is about $27 against an edge of $1.83). A prompt, not a verdict. |
| drawdown (C9, C6) | cumulative base-fill P&L, by exit date, is down from its peak by more than 1.5 x the worst holdout drawdown ($3,908 C9, $2,920 C6) | Always evaluated. |
| premium (C3) | cumulative net premium paid since reset exceeds the hedge budget | `C3_HEDGE_BUDGET` is unset: the stop is the user's number. The counter is kept and shown. |
| quality | more than 4 of the last 20 signals had a median leg spread above 30% of mid, or no liquid short strike | Needs at least 5 flagged signals even when fewer than 20 exist. |

## 7. Decisions and deviations to review

1. **Shadow only; no live callout mode.** See section 1.
2. **Snapshot at about 16:05 ET on a 15-minute-delayed feed.** The pre-registration assumed a 16:00 ET snapshot. A run at 16:20 ET sees
   the market as of about 16:05 ET: the closing print is in, but SPY options keep trading until 16:15 ET and SPY itself trades after hours,
   so the SPY quotes can differ slightly from the 16:00 quotes the study used. The condor's entry price here is therefore a small
   departure from the backtest's, in a direction nobody has measured. Also: SPY options stop trading at 16:15 ET, so a *live* version of
   this entry would have to run earlier than 16:20. On the one afternoon that was checked (2026-10-13, `docs/OPERATIONS.md` section 10) the
   rules picked a different put short for snapshots a few minutes apart (772 at data time 16:02, 773 from 16:06: mid credit $0.72 against
   $0.83), and a different TLT short strike (76 against 76.5: $0.25 against $0.315); GLD did not change. A shadow ticket is therefore one
   draw from a band of one-strike variants. Entry messages print both clocks (the underlying's last trade and the option quotes' time,
   15 minutes before the file's stamp).
3. **C9 settles at the official SPY close** of the expiry date (as the spec says), not at the chain's underlying as the study did. The
   like-for-like backtest baseline is +$1.47 / +$0.35 (base / natural) on the same 499 trades, not +$1.83 / +$0.71. Messages and the weekly
   summary print both.
4. **C6 enters on the first trading day of each ISO week**, GLD and TLT. The pre-registration says "every Monday"; the study's handling of a
   Monday holiday could not be checked from the project documents. The two rules differ only in holiday weeks.
5. **C6 REAL expiry window 23 to 37 DTE** is a guard added here; the spec says only "closest to 30".
6. **Early-close days (13:00 ET) block new entries** (`early_close`); settlements and exits still run. Conservative: the study's chains
   were 16:00 ET snapshots.
7. **A missed run is never back-filled** for the real paths (the signal is tied to the closing snapshot); the C6 MODEL twin is, within 12
   days, because it is recomputed from official closes and published index values. Missed sessions are counted (`missed_run`).
8. **A run that starts after 16:30 ET** (`too_late`) settles and exits but opens nothing (`run_late`). A manual run is classified the same way.
9. **No yfinance fallback.** The bot is standard library only (`requirements.txt` is empty); if the Cboe chain is unavailable the entry is skipped
   (`feed_down`) with one notice.
10. **Python 3.13** in `shadow.yml`; the test workflow runs 3.11 and 3.13.
11. **`data/` seed files** hold closes (SPY, GLD, TLT: 295 rows, 2025-08-11 to 2026-10-12), published index closes (VIX 303 rows; VIX3M, GVZ,
    VXTLT 102 rows from 2026-05-18) and 101 T-bill rates. On 2026-10-13 they were compared with Nasdaq, Cboe and Treasury and agreed on every
    row, and the 2026-10-12 closes equal Cboe's own `prev_day_close`.
12. **Index quotes carry mixed time stamps.** VIX and VIX3M quote times are Eastern with no offset; GVZ and VXTLT carry a Central-time offset
    (`-05:00`). The parser converts any string with an offset to Eastern and takes the rest as Eastern.

## 8. Known limitations (read before relying on a number)

* The Cboe files are a free, delayed, undocumented feed. Their layout, host (`cdn.cboe.com` already redirects to `cdn-api.cboe.com`) or
  rate limits can change without notice; `diag_log.jsonl` is where a change shows first.
* Real posting to Discord, real GitHub Actions runs and real Worker dispatches were **not** exercised in the build environment. Everything
  else was exercised against saved and simulated sources (`docs/OPERATIONS.md`, section "What was tested").
* `run_guard.py` was written from the described contract of the live bots' guard (the live repositories' file was not available); it keeps its own
  marker file and never touches theirs.
* The VIX history file contains rows for some market holidays; those count as published closes, exactly as the study's data did.
* A small disagreement (under 0.3%) between a stored close and a higher-ranked second source is corrected silently.
* The kill-switch thresholds are proposals. The edge check will fire by chance.
* No independent reviewer has read this code; the review was the author's own, plus the tests.

## 9. What was deliberately not built

* Any order-placing or order-routing code, and any "live callout" mode for the three strategies.
* Step 0 (logging the live condor bot's real fills).
* A wiring of `shadow_state_export.json` into the live combined-drawdown monitor (it would corrupt the live book's drawdown).
* A yfinance dependency.
* A creation of the GitHub repository, the Discord webhook secret, or the Worker entry: these need credentials this build did not have.

## 10. History: what was wrong with the uploaded code, and what was done about it

The first version of this repository arrived as 22 files (modules and a README; no tests, workflows, data seeds, `run_guard.py` or
`docs/`). They were an incomplete snapshot: they **would have crashed on the first real run**, because they used names that were not defined
anywhere in the upload. The originals are kept outside this repository; the differences are:

**Missing pieces that the uploaded code called (added):**
* `chain.REGULAR_CLOSE` / `EARLY_CLOSE` (used by `jobctx.compute_phase`), and the `close_et` argument that `shadow_job.py` passed to
  `Feed.chain` and `snapshot_stale` but neither accepted. The closing-print floor now follows the session's close (13:00 on early closes).
* `config.CLOSES_FETCH_DAYS`, `HISTORY_KEEP_DAYS`, `TBILL_KEEP_DAYS`, `DIAG_MAX_LINES`, `DIAG_KEEP_LINES`, `EXIT_DRIFT_DAYS`.
* `market_data.trim_market`, `data_feed.FixtureHttp` (the `--fixture-dir` replay), the `run_guard` module, and the state field `last_session`.

**Behaviour that was wrong (fixed):**
* `chain.parse_et_naive` read every time stamp as Eastern; the GVZ and VXTLT quotes carry a Central-time offset (an hour off).
* `data_feed.tbill`: the Treasury fallback used the par-yield "3 Mo" column, which runs about 0.2 points above FRED's DTB3 (the bank-discount
  column is the same quantity). `Feed.chain` retried a network failure three more times on top of the HTTP layer's own retries.
  The Yahoo request now asks for an explicit date window (`period1` / `period2`); the Nasdaq historical API was added as a second source.
* `discord_shadow`: Discord's edge answers the default Python User-Agent with HTTP 403 (error 1010); the sender now sends Discord's documented
  `DiscordBot (url, version)` form, and an HTTP 429 now stops the flush instead of hammering the webhook.
* `kill_switch`: `math.fsum` for the trailing mean (Python 3.12 changed the rounding of the built-in `sum` of floats; the trip threshold must not
  depend on the interpreter).
* `market_data.merge_closes`: a higher-ranked source could overwrite a stored close by any amount; now only by 2% or less. The Nasdaq source has rank 1.
* `state.record_skip`: a second run of the same session counted its skips again; now one count per (date, strategy, reason).
* `job_c3.resolve_candidate`: the confirmation wait counted runs, so a manual rerun used up the five-session allowance; it now counts sessions.
* `shadow_job`: a snapshot-wait loop (previously one fetch, so a pre-close file meant a skipped day); the first-run hello is queued first and so
  posted first; a late-start notice; close disagreements are reported only for today's and yesterday's rows; the diagnostics line is written
  even when the run crashes; diagnostics-only runs never post; a virtual clock for replays.
* `config`: the C9 official-close baselines, the wait-loop settings, the Nasdaq and Treasury URLs.

**Replaced:** the uploaded `README.md` (it claimed 521 tests, hash-checked parity fixtures and a real-data fixture for 2026-10-12, none of which
were in the upload).

**Unchanged from the upload** (read against the frozen rules, but not rewritten): `bs.py`, `fills.py`, `nyse_calendar.py`, `rules_c3.py`,
`rules_c6.py`, `rules_c9.py`, `job_c6.py`, `job_c9.py`, `export_state.py`, `diag_report.py`. `messages.py` changed once, after the live check of
2026-10-13: the entry-message header shows both clocks (the underlying's last trade and the option quotes' time) instead of one.

**Written here:** `run_guard.py`, both workflows, `.gitignore`, the data seeds, `docs/`, and the whole test suite.
