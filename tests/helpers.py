"""Shared builders for the test suite. Standard library only; numpy / scipy are used by the oracle tests when they are installed.

Three kinds of fixture:
  * hand-made chains (explicit strike / bid / ask / delta tables) so a test can say exactly which strike must be picked;
  * Black-Scholes chains (realistic spreads and deltas) for the flow tests;
  * a `World`: a temp directory holding saved "network" files (Cboe chains and quotes, history CSVs, Nasdaq closes, Treasury
    bills) in the formats the live sources use, plus a bot root with data/ seeds, so shadow_job.main() runs end to end offline.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import chain as chain_mod                    # noqa: E402
import config as cfg                         # noqa: E402
import market_data as md                     # noqa: E402
import nyse_calendar as nyse                 # noqa: E402
from bs import bs_call, bs_put               # noqa: E402

try:                                         # oracles: optional, the suite skips their tests when they are missing
    import numpy as np
except ImportError:                          # pragma: no cover
    np = None
try:
    import scipy.stats as sps
except ImportError:                          # pragma: no cover
    sps = None

ET = ZoneInfo("America/New_York")
UTC = dt.timezone.utc
FIXTURES = os.path.join(HERE, "fixtures")
needs_numpy = unittest.skipUnless(np is not None, "numpy not installed (oracle test)")
needs_scipy = unittest.skipUnless(sps is not None, "scipy not installed (oracle test)")


def D(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


def et(s: str) -> dt.datetime:
    """'2026-10-13 16:20' -> aware New York datetime."""
    return dt.datetime.fromisoformat(s.replace(" ", "T")).replace(tzinfo=ET)


# ----------------------------------------------------------------------------------------------------------- hand-made chains
def rows_from(expiry: dt.date, cp: int, table) -> list:
    """table: [(strike, bid, ask, delta)] -> chain rows."""
    return [(expiry, cp, float(k), float(b), float(a), dl, None) for (k, b, a, dl) in table]


def make_chain(symbol: str, asof: dt.date, spot: float, rows: list, **meta) -> chain_mod.Chain:
    m = dict(symbol=symbol, session=asof.isoformat(), spot_src="close", und_last_trade_et=f"{asof.isoformat()}T16:00:00",
             file_ts_utc=None, file_age_min=5.0)
    m.update(meta)
    return chain_mod.Chain(symbol, asof, spot, rows, m)


# ----------------------------------------------------------------------------------------------------------- Black-Scholes chains
def bs_rows(spot: float, asof: dt.date, expiries, strikes, vol: float = 0.18, r: float = 0.04, rel: float = 0.02,
            floor: float = 0.01, vol_slope: float = 0.0) -> list:
    """Realistic chain rows: BS mid, half-spread max(floor, rel x mid), BS delta, iv. vol_slope adds a skew (vol rises as the
    strike falls). Rows with ask <= 0 are dropped, as the Cboe cleaner does."""
    rows = []
    for ex in expiries:
        T = max((ex - asof).days, 0.5) / 365.0
        for k in strikes:
            sig = max(0.05, vol + vol_slope * (spot - k) / spot)
            for cp in (+1, -1):
                price, delta = bs_call(spot, k, T, r, sig) if cp > 0 else bs_put(spot, k, T, r, sig)
                h = max(floor, rel * price)
                bid, ask = round(max(price - h, 0.0), 2), round(price + h, 2)
                if ask <= 0:
                    continue
                rows.append((ex, cp, float(k), bid, ask, round(delta, 4), sig))
    return rows


def occ(symbol: str, ex: dt.date, cp: int, k: float) -> str:
    return f"{symbol}{ex:%y%m%d}{'C' if cp > 0 else 'P'}{int(round(k * 1000)):08d}"


def cboe_options_obj(symbol: str, rows: list, spot: float, *, last_trade: dt.datetime, file_ts_utc: dt.datetime,
                     close: float = None, prev_close: float = None) -> dict:
    """The JSON object of cdn.cboe.com/api/global/delayed_quotes/options/<SYM>.json, in the live field layout."""
    opts = []
    for (ex, cp, k, bid, ask, delta, iv) in rows:
        opts.append({"option": occ(symbol, ex, cp, k), "bid": bid, "bid_size": 10.0, "ask": ask, "ask_size": 10.0,
                     "iv": iv or 0.0, "open_interest": 100.0, "volume": 10.0, "delta": delta, "gamma": 0.0, "vega": 0.0,
                     "theta": 0.0, "rho": 0.0, "theo": round(0.5 * (bid + ask), 4), "change": 0.0, "open": 0.0, "high": 0.0,
                     "low": 0.0, "tick": "no_change", "last_trade_price": round(0.5 * (bid + ask), 2),
                     "last_trade_time": last_trade.replace(tzinfo=None).isoformat(timespec="seconds"),
                     "percent_change": 0.0, "prev_day_close": 0.0})
    data = {"options": opts, "symbol": symbol, "security_type": "etf", "exchange_id": 3, "current_price": spot,
            "price_change": 0.0, "price_change_percent": 0.0, "bid": spot - 0.01, "ask": spot + 0.01, "bid_size": 100,
            "ask_size": 100, "open": spot, "high": spot, "low": spot, "close": spot if close is None else close,
            "prev_day_close": prev_close if prev_close is not None else spot, "volume": 1000, "iv30": 12.0,
            "iv30_change": 0.0, "iv30_change_percent": 0.0, "seqno": 1,
            "last_trade_time": last_trade.replace(tzinfo=None).isoformat(timespec="seconds"), "tick": "no_change"}
    return {"timestamp": file_ts_utc.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S"), "data": data}


def cboe_quote_obj(name: str, value: float, last_trade: dt.datetime, file_ts_utc: dt.datetime, offset_style: bool = False) -> dict:
    lt = last_trade.isoformat(timespec="milliseconds") if offset_style else last_trade.replace(tzinfo=None).isoformat(timespec="seconds")
    return {"timestamp": file_ts_utc.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S"),
            "data": {"symbol": f"_{name}", "current_price": value, "close": value, "prev_day_close": value,
                     "last_trade_time": lt}, "symbol": f"_{name}"}


def history_csv(rows, name: str = "VIX") -> str:
    out = io.StringIO()
    out.write("DATE,OPEN,HIGH,LOW,CLOSE\n" if name == "VIX" else f"DATE,{name}\n")
    for d, v in rows:
        if name == "VIX":
            out.write(f"{d:%m/%d/%Y},{v},{v},{v},{v}\n")
        else:
            out.write(f"{d:%m/%d/%Y},{v}\n")
    return out.getvalue()


def treasury_csv(rows) -> str:
    """Treasury daily bill-rate CSV with both a bank-discount and a coupon-equivalent column (the par-yield trap)."""
    out = io.StringIO()
    out.write('"Date","4 WEEKS BANK DISCOUNT","4 WEEKS COUPON EQUIVALENT","13 WEEKS BANK DISCOUNT","13 WEEKS COUPON EQUIVALENT"\n')
    for d, v in rows:
        out.write(f'"{d:%m/%d/%Y}","{v - 0.1:.2f}","{v:.2f}","{v:.2f}","{v + 0.19:.2f}"\n')
    return out.getvalue()


def nasdaq_json(rows) -> str:
    return json.dumps({"data": {"tradesTable": {"rows": [
        {"date": f"{d:%m/%d/%Y}", "close": f"${v:,.2f}", "volume": "1,000", "open": "$1.00", "high": "$1.00", "low": "$1.00"}
        for d, v in sorted(rows, reverse=True)]}}, "status": {"rCode": 200}})


# ----------------------------------------------------------------------------------------------------------- price paths
def trading_days_from(start: dt.date, n: int) -> list:
    days, d = [], start
    while len(days) < n:
        if nyse.is_trading_day(d):
            days.append(d)
        d += dt.timedelta(days=1)
    return days


def move_path(days: list, split: int, first: float, second: float, start: float = 600.0) -> list:
    """Deterministic closes whose daily move alternates +amp / -amp, with amp = `first` before index `split` and `second` from
    it on. (0.002, 0.015) is a quiet run followed by a loud one: realised vol rises, so the C9 gate closes; (0.015, 0.002)
    the other way round: realised vol falls below its own median, so the gate opens."""
    px, out = start, []
    for i, _d in enumerate(days):
        amp = first if i < split else second
        px *= 1.0 + (amp if i % 2 == 0 else -amp)
        out.append(round(px, 2))
    return out


# ----------------------------------------------------------------------------------------------------------- senders and roots
class FakeSender:
    """A Discord sender double: records payloads, can be told to fail (status codes in order; None = succeed, "net" = no answer)."""

    def __init__(self, fail_with=()):
        self.fail_with = list(fail_with)
        self.posts: list = []

    def __call__(self, url, payload):
        self.posts.append((url, payload))
        if self.fail_with:
            code = self.fail_with.pop(0)
            if code == "net":                                   # no HTTP answer at all (DNS, timeout, refused)
                return False, None, "network: unreachable"
            if code is not None:
                return False, code, f"HTTP {code}"
        return True, 204, ""

    @property
    def texts(self) -> list:
        return [p["content"] for _u, p in self.posts]


def full_text(payload: dict) -> str:
    """What a reader sees of one posted message: the content line, then every embed's description and labelled fields."""
    out = [payload.get("content") or ""]
    for e in payload.get("embeds") or []:
        out.append(e.get("description", ""))
        out += [f"{f['name']}: {f['value']}" for f in e.get("fields", [])]
    return "\n".join(out)


WEBHOOK = "https://discord.com/api/webhooks/123456789/test-token-not-real"


class TempRoot:
    """A temp directory that is removed afterwards; usable as a context manager or via addCleanup."""

    def __init__(self, prefix="shadow-test-"):
        self.path = tempfile.mkdtemp(prefix=prefix)

    def __enter__(self):
        return self.path

    def __exit__(self, *exc):
        shutil.rmtree(self.path, ignore_errors=True)

    def join(self, *p):
        return os.path.join(self.path, *p)


class Base(unittest.TestCase):
    def tmp(self) -> str:
        t = TempRoot()
        self.addCleanup(shutil.rmtree, t.path, True)
        return t.path
