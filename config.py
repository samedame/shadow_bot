"""Frozen parameters for the three SHADOW strategies (C9 CONDOR_1D, C6 NONEQ_VRP, C3 SPY_HEDGE).

Source of truth: the research workspace's 04_prereg/PREREG.md section 4 and code/strategies.py
(options_sweep study, 2026-10). tests/test_config.py pins every value in FROZEN, so a drifting
parameter fails the suite on purpose. Change a value only with a written reason.

Everything here is SHADOW: nothing in this repo places or changes an order, and every message it posts
starts with "SHADOW - NOT A TRADE".
"""
from __future__ import annotations

# ---------------------------------------------------------------------------------------------
# Schedule (America/New_York). One daily job runs all three strategies.
# ---------------------------------------------------------------------------------------------
JOB_ID = "shadow-daily"
TARGET_ET = "16:20"            # intended fire time
USABLE_UNTIL_ET = "16:30"      # after this: no NEW entries (settlement and re-marks still run)
ON_TIME_MIN = 10               # <= this many minutes late counts as on time
SESSION_CLOSE_ET = "16:00"
CLOSE_SNAPSHOT_EARLIEST_ET = "16:05"   # a run before this on a trading day is "intraday": diagnostics only
EARLY_CLOSE_ET = "13:00"

# A snapshot older than this (minutes between the feed's file timestamp and the fetch) is flagged stale.
# The feed is delayed ~15 minutes by design, and the file timestamp is a generation time, so this is a
# sanity bound, not a quote-time measurement. Tune after the first week of diagnostics (diag_log.jsonl).
STALE_FILE_AGE_MIN = 45
# The option bid/ask inside a Cboe delayed-quote file are this many minutes older than the file's own `timestamp` (measured on
# 2026-10-13 with probes of the SPY, GLD and TLT files all afternoon: the newest option trade was always 15 min 1 s behind the
# timestamp). Messages use it only to say what time the option quotes are from; no rule reads it.
CBOE_FEED_DELAY_MIN = 15
SPOT_VS_CLOSE_WARN = 0.003     # warn when |last/close - 1| exceeds this on the underlying
# The underlying's `close` field is only the official close once its last trade is at or after (close time - this many
# minutes) on the session date; intraday the feed fills `close` with the last price. Relax if diag_log.jsonl shows GLD or
# TLT routinely last trading earlier than 15:59 ET.
CLOSE_PRINT_TOL_MIN = 1
# Waiting for the closing snapshot: re-fetch every WAIT_POLL_S seconds for at most WAIT_MAX_S seconds, and never past
# USABLE_UNTIL_ET. The Cboe delayed-quote file lags the market by about 15 minutes.
WAIT_POLL_S = 45
WAIT_MAX_S = 540

# ---------------------------------------------------------------------------------------------
# Fills, fees, liquidity (PREREG sections 3-4; engine.py)
# ---------------------------------------------------------------------------------------------
MULT = 100
K_MID, K_BASE, K_NAT, K_X3 = 0.0, 0.25, 0.5, 0.75     # fraction of the quoted spread from mid
KS = (K_MID, K_BASE, K_NAT, K_X3)
FEE_BASE, FEE_STRESS = 0.05, 0.65                     # $ per contract per leg per side
SPREAD_ABS, SPREAD_REL = 0.05, 0.30                   # spread <= max($0.05, 30% of mid)
DELTA_TOL = 0.10                                      # pick_delta_strike: best miss allowed
MAX_DTE = 130                                         # chain cleaning (chainlib.finish)
MAX_MONEYNESS = 0.35

# ---------------------------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------------------------
C9 = dict(
    name="CONDOR_1D",
    delta=0.16,            # short strikes: |delta| closest to this (miss limit DELTA_TOL)
    wing=0.01,             # long wings at short strike -/+ 1.0% x spot, nearest listed strike
    rv_window=20,          # sample std (ddof=1) of the last 20 close-to-close returns
    gate_window=20,        # gate: RV(p) < median of the 20 RV values ending at p (p included)
    dte=1,                 # next-trading-day expiry
)

C6 = dict(
    name="NONEQ_VRP",
    delta=0.25,
    width_frac=0.02,       # width = max(1, round(2% x spot)), Python banker's rounding
    width_min=1,
    dte=30,                # model expiry = entry + 30 calendar days
    profit_frac=0.5,       # exit when the mid-mark <= 50% of the BASE entry credit
    exit_dte=7,            # otherwise on the first day with DTE <= 7
    strike_scan_lo=0.80,   # model short strike scanned over range(int(0.80 S0), int(S0) + 1)
    half_spread_abs=0.03,  # model half-spread h = max($0.03, 4% x V)
    half_spread_rel=0.04,
    vol_index={"GLD": "GVZ", "TLT": "VXTLT"},      # flat vol = index / 100 for both legs
    real_dte_range=(23, 37),   # REAL version only: listed expiry closest to 30 DTE inside this window.
                               # The window is a guard added here; the spec says only "closest to 30".
)

C3 = dict(
    name="SPY_HEDGE",
    long_frac=0.95,        # buy the listed put nearest 0.95 x spot
    short_frac=0.85,       # sell the listed put nearest 0.85 x spot
    dte_target=60,
    dte_range=(50, 75),    # expiry DTE closest to 60 inside [50, 75]; ties -> the shorter
    exit_dte=21,           # first trading day with DTE <= 21
    ratio_max=0.90,        # gate: VIX / VIX3M strictly below this
    quantile=0.25,         # AND VIX <= the 25th percentile of the previous `window` VIX closes
    window=252,
    min_obs=200,
    confirm_max_days=5,    # a candidate not confirmable by published closes within this many runs is dropped
)

# What the tests pin. Add a value to the right-hand dict to put it under test.
FROZEN = {
    "C9": C9, "C6": C6, "C3": C3,
    "fills": dict(K_MID=K_MID, K_BASE=K_BASE, K_NAT=K_NAT, K_X3=K_X3, FEE_BASE=FEE_BASE, FEE_STRESS=FEE_STRESS,
                  SPREAD_ABS=SPREAD_ABS, SPREAD_REL=SPREAD_REL, DELTA_TOL=DELTA_TOL, MULT=MULT),
}

# ---------------------------------------------------------------------------------------------
# Kill-switch thresholds, adapted to shadow (spec section 2.8). ALL are proposals, not backtested rules.
# A trip sets status "flagged" and keeps logging; only the user moves a strategy to "retired".
# ---------------------------------------------------------------------------------------------
PRIMED_MIN_TRADES = 20
TRAILING_WINDOW = 20
EDGE_FLOOR_FRACTION = 0.25
QUALITY_SPREAD_REL = 0.30      # median spread of the legs above 30% of mid ...
QUALITY_SHARE = 0.20           # ... on more than 20% of the last 20 signals
QUALITY_WINDOW = 20

BACKTEST = {
    # per-trade means from the holdout (2022-2025), base = 25% of the spread from mid, natural = bid/ask
    # C9: edge_base / edge_nat are the study's (settled at the chain's underlying). The bot settles at the official SPY
    # close, so the like-for-like baseline is the *_official pair (same 499 trades).
    "C9": dict(edge_base=1.83, edge_nat=0.71, edge_base_official=1.47, edge_nat_official=0.35,
               dd_stop=3908.0, worst_dd=2606.0, trades=499),
    "C6": dict(edge_base=7.96, edge_nat=-0.34, dd_stop=2920.0, worst_dd=1946.0, trades=418),
    "C3": dict(edge_base=47.0, edge_nat=None, dd_stop=None, worst_dd=None, trades=13),
}
# C3: the cumulative-premium stop is the USER's number (OPEN_QUESTIONS item 6). None = not set yet; the
# premium counter is still kept and shown, the stop is simply not evaluated.
C3_HEDGE_BUDGET = None
C3_REVIEW_DRAWDOWN = 0.10      # record whether the hedge paid after every 10% SPY drawdown from its prior peak

STRATEGIES = ("C9", "C6", "C3")
STATUSES = ("active", "flagged", "retired")

# ---------------------------------------------------------------------------------------------
# Data sources
# ---------------------------------------------------------------------------------------------
# Browser-like UA for the data sources: Cboe's CDN rejects the default Python-urllib one. It carries no account details of
# any kind. (Discord's edge rejects it too; the Discord sender uses discord_shadow.USER_AGENT, Discord's documented form.)
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
CBOE_CHAIN_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{symbol}.json"
CBOE_QUOTE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/quotes/_{name}.json"
CBOE_HISTORY_URL = "https://cdn.cboe.com/api/global/us_indices/daily_prices/{name}_History.csv"
FRED_DTB3_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DTB3"
YAHOO_CHART_URL = ("https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
                   "?period1={period1}&period2={period2}&interval=1d&events=div,splits")
NASDAQ_HIST_URL = ("https://api.nasdaq.com/api/quote/{symbol}/historical"
                   "?assetclass=etf&fromdate={fromdate}&limit=9999&todate={todate}")
STOOQ_URL = "https://stooq.com/q/d/l/?s={symbol}.us&i=d"
# Fallback T-bill source (FRED is primary). The "13 WEEKS BANK DISCOUNT" column is the same quantity as FRED DTB3
# (3-month bill, discount basis). The par-yield "3 Mo" column is a different, higher number (about +0.2 pt): do not use it.
TREASURY_BILLS_URL = ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/"
                      "{year}/all?type=daily_treasury_bill_rates&field_tdr_date_value={year}&page&_format=csv")
TREASURY_BILL_COLUMN = "13 WEEKS BANK DISCOUNT"
FETCH_RETRIES = 3
FETCH_TIMEOUT_S = 45

# How much history each run asks for and keeps (calendar days)
CLOSES_FETCH_DAYS = 30          # daily top-up window for raw closes (widened automatically after a gap)
CLOSES_BOOTSTRAP_DAYS = 430     # used when a symbol has fewer than CLOSES_MIN_ROWS stored closes (first run)
CLOSES_MIN_ROWS = 60
CLOSES_KEEP_DAYS = 430          # C3's hedge ledger looks back a year for the SPY peak; C9 needs 41 trading days
HISTORY_KEEP_DAYS = {"VIX": 430, "VIX3M": 150, "GVZ": 150, "VXTLT": 150}     # published index closes
TBILL_KEEP_DAYS = 150
EXIT_DRIFT_DAYS = 3             # an exit whose legs are not quoted is retried on the next 3 trading days, then logged unpriced
INDEX_NAMES = ("VIX", "VIX3M", "GVZ", "VXTLT")
CLOSE_SYMBOLS = ("SPY", "GLD", "TLT")

# ---------------------------------------------------------------------------------------------
# Files (all committed back by the workflow)
# ---------------------------------------------------------------------------------------------
STATE_FILE = "shadow_state.json"
LOG_FILE = "shadow_log.csv"
DIAG_FILE = "diag_log.jsonl"
DIAG_MAX_LINES = 1000          # rotate: when the diagnostics log passes this many lines, keep the newest DIAG_KEEP_LINES
DIAG_KEEP_LINES = 600
EXPORT_FILE = "shadow_state_export.json"
GUARD_FILE = ".run_guard_shadow.json"
DELIVERY_FAILED_FILE = ".delivery_failed"       # written (never committed) when messages are still queued after a run; the
                                                # workflow's last step turns that into a red run, the only signal outside Discord
DATA_DIR = "data"
CLOSES_FILE = "data/closes.csv"
INDICES_FILE = "data/indices.csv"
TBILL_FILE = "data/tbill.csv"

BANNER = "SHADOW - NOT A TRADE"
SCHEMA = 1
