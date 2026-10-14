"""Network layer: Cboe option chains and index quotes, Cboe published index history, FRED / Treasury T-bill, and daily closes
(Yahoo chart API, Nasdaq historical API, Stooq). Standard library only.

Nothing here makes a decision. Every fetch retries, and every function returns plain data or raises FeedError, so the job
can post ONE "data unavailable" notice and carry on with the strategies that still have their inputs.

Cboe's CDN answers a plain request with a 307 to cdn-api.cboe.com; urllib follows it. The User-Agent is a generic browser
string (config.USER_AGENT): the CDN rejects the default Python one, and it carries no account details of any kind.

Sources (checked against the live endpoints on 2026-10-13):
  chains          cdn.cboe.com/api/global/delayed_quotes/options/{SPY,GLD,TLT}.json   (bid/ask/iv/delta/gamma/oi/volume per contract;
                  the underlying's `close` repeats the last price until the closing print, so it is only trusted after it)
  index quotes    cdn.cboe.com/api/global/delayed_quotes/quotes/_{VIX,VIX3M,GVZ,VXTLT}.json   (provisional until published)
  index history   cdn.cboe.com/api/global/us_indices/daily_prices/{NAME}_History.csv   (published after the close)
  T-bill          FRED DTB3 (fredgraph.csv); Treasury.gov "13 WEEKS BANK DISCOUNT" (the same quantity) as the fallback
  raw closes      Yahoo chart API (unadjusted close), then the Nasdaq historical API, then Stooq. The underlying's own `close`
                  in the Cboe chain file is the same-day source (it equalled Yahoo's close to the cent for SPY, GLD and TLT)
"""
from __future__ import annotations

import csv
import datetime as dt
import http.client
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from typing import Callable, Optional
from zoneinfo import ZoneInfo

import chain as chain_mod
import config as cfg

ET = ZoneInfo("America/New_York")
UTC = dt.timezone.utc


class FeedError(Exception):
    """A source could not be read after the allowed retries."""


def http_get(url: str, timeout: int = cfg.FETCH_TIMEOUT_S, retries: int = cfg.FETCH_RETRIES,
             sleep: Callable[[float], None] = time.sleep, headers: Optional[dict] = None) -> bytes:
    """GET with a browser UA, redirects followed, and `retries` attempts with 1 s / 3 s / 6 s back-off."""
    hdr = {"User-Agent": cfg.USER_AGENT, "Accept": "*/*", "Accept-Encoding": "identity"}
    hdr.update(headers or {})
    last: Optional[Exception] = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=hdr)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read()
            if not body:
                raise FeedError(f"empty body from {url}")
            return body
        except (urllib.error.URLError, OSError, http.client.HTTPException, FeedError) as e:
            # URLError covers HTTP 4xx/5xx, OSError covers timeouts and resets, HTTPException covers a body cut short (IncompleteRead)
            last = e
            print(f"[data_feed] {url} attempt {attempt + 1}/{retries} failed: {e}", file=sys.stderr)
            if attempt < retries - 1:
                sleep((1, 3, 6)[min(attempt, 2)])
    raise FeedError(f"{url}: {last}")


class Feed:
    def __init__(self, http: Callable[..., bytes] = http_get, sleep: Callable[[float], None] = time.sleep):
        self.http = http
        self.sleep = sleep

    # ------------------------------------------------------------------ option chains
    def chain(self, symbol: str, session: dt.date, now_utc: Optional[dt.datetime] = None,
              close_et: dt.time = chain_mod.REGULAR_CLOSE) -> chain_mod.Chain:
        """The clean chain for `symbol` from the Cboe delayed-quotes file. A network failure raises FeedError at once (the
        http layer has already retried); a file that parses to nothing is fetched again (3 times in all), then raises."""
        url = cfg.CBOE_CHAIN_URL.format(symbol=symbol)
        last: Optional[Exception] = None
        for attempt in range(cfg.FETCH_RETRIES):
            try:
                raw = self.http(url)
            except FeedError as e:
                raise FeedError(f"chain {symbol}: {e}")
            try:
                obj = json.loads(raw)
                return chain_mod.from_cboe(obj, symbol, session, now_utc=now_utc or dt.datetime.now(UTC), close_et=close_et)
            except (ValueError, KeyError, TypeError) as e:        # ValueError covers JSON errors and empty chains
                last = e
                print(f"[data_feed] chain {symbol} attempt {attempt + 1}/{cfg.FETCH_RETRIES}: {e}", file=sys.stderr)
                if attempt < cfg.FETCH_RETRIES - 1:
                    self.sleep((2, 5, 10)[min(attempt, 2)])
        raise FeedError(f"chain {symbol}: {last}")

    # ------------------------------------------------------------------ index quotes and history
    def index_quote(self, name: str) -> dict:
        """Provisional index value from the delayed quote: {"value", "close", "last_trade_et", "file_ts_utc"}."""
        url = cfg.CBOE_QUOTE_URL.format(name=name)
        try:
            obj = json.loads(self.http(url))
            data = obj["data"]
            val = chain_mod._f(data.get("current_price"))
            if val is None or val <= 0:
                raise ValueError("no price")
        except (FeedError, ValueError, KeyError, TypeError) as e:
            raise FeedError(f"index quote {name}: {e}")
        t = chain_mod.parse_et_naive(data.get("last_trade_time"))
        return {"value": val, "close": chain_mod._f(data.get("close")),
                "last_trade_et": None if t is None else t.isoformat(timespec="seconds"), "file_ts_utc": obj.get("timestamp")}

    def index_history(self, name: str) -> list:
        """Published daily closes [(date, close)] from Cboe's history CSV (ascending)."""
        url = cfg.CBOE_HISTORY_URL.format(name=name)
        try:
            rows = parse_cboe_history(self.http(url).decode("utf-8", "replace"))
        except (FeedError, ValueError, KeyError) as e:
            raise FeedError(f"index history {name}: {e}")
        if not rows:
            raise FeedError(f"index history {name}: no rows")
        return rows

    # ------------------------------------------------------------------ T-bill
    def tbill(self, today: Optional[dt.date] = None) -> tuple:
        """([(date, percent)], source): FRED DTB3, else Treasury.gov's 13-week bank-discount rate (same quantity)."""
        errs = []
        try:
            rows = parse_fred(self.http(cfg.FRED_DTB3_URL).decode("utf-8", "replace"))
            if rows:
                return rows, "fred"
            errs.append("FRED: no rows")
        except (FeedError, ValueError, KeyError) as e:
            errs.append(f"FRED: {e}")
        try:
            year = (today or dt.datetime.now(ET).date()).year
            rows = []
            for y in (year - 1, year):
                try:
                    rows += parse_treasury_bills(self.http(cfg.TREASURY_BILLS_URL.format(year=y)).decode("utf-8", "replace"))
                except FeedError:
                    if y == year:
                        raise
            rows = sorted(set(rows))
            if rows:
                return rows, "treasury"
            errs.append("Treasury: no rows")
        except (FeedError, ValueError, KeyError) as e:
            errs.append(f"Treasury: {e}")
        raise FeedError("T-bill: " + "; ".join(errs))

    # ------------------------------------------------------------------ raw closes
    def closes(self, symbol: str, days: int = 20, now_utc: Optional[dt.datetime] = None) -> tuple:
        """([(date, raw close)], source) for roughly the last `days` calendar days. Yahoo first, then Nasdaq, then Stooq."""
        now = now_utc or dt.datetime.now(UTC)
        today_et = now.astimezone(ET).date()
        errs = []
        try:
            p2 = int(now.timestamp()) + 86400
            p1 = p2 - (days + 3) * 86400
            rows = parse_yahoo_chart(self.http(cfg.YAHOO_CHART_URL.format(symbol=symbol, period1=p1, period2=p2)))
            if rows:
                return rows, "yahoo"
            errs.append("yahoo: no rows")
        except (FeedError, ValueError, KeyError, TypeError, IndexError) as e:
            errs.append(f"yahoo: {e}")
        try:
            url = cfg.NASDAQ_HIST_URL.format(symbol=symbol, fromdate=(today_et - dt.timedelta(days=days + 3)).isoformat(),
                                             todate=today_et.isoformat())
            rows = parse_nasdaq_hist(self.http(url, headers={"Accept": "application/json, text/plain, */*"}))
            if rows:
                return rows, "nasdaq"
            errs.append("nasdaq: no rows")
        except (FeedError, ValueError, KeyError, TypeError, IndexError) as e:
            errs.append(f"nasdaq: {e}")
        try:
            rows = parse_stooq(self.http(cfg.STOOQ_URL.format(symbol=symbol.lower())).decode("utf-8", "replace"))
            lo = today_et - dt.timedelta(days=days + 3)
            rows = [(d, v) for d, v in rows if d >= lo]
            if rows:
                return rows, "stooq"
            errs.append("stooq: no rows")
        except (FeedError, ValueError, KeyError) as e:
            errs.append(f"stooq: {e}")
        raise FeedError(f"closes {symbol}: " + "; ".join(errs))


# ---------------------------------------------------------------------------------------------- parsers (pure)
def parse_cboe_history(text: str) -> list:
    """Cboe index-history CSV -> [(date, close)]. Header 'DATE,OPEN,HIGH,LOW,CLOSE' or 'DATE,<NAME>'; dates are MM/DD/YYYY."""
    rd = csv.DictReader(io.StringIO(text))
    cols = rd.fieldnames or []
    if "DATE" not in cols:
        raise ValueError(f"unexpected header {cols}")
    col = "CLOSE" if "CLOSE" in cols else next(c for c in cols if c != "DATE")
    out = {}
    for r in rd:
        try:
            d = dt.datetime.strptime(r["DATE"].strip(), "%m/%d/%Y").date()
            v = float(r[col])
        except (ValueError, TypeError, KeyError):
            continue
        out[d] = v
    return sorted(out.items())


def parse_fred(text: str) -> list:
    """FRED graph CSV -> [(date, percent)]; '.' rows (holidays) are skipped."""
    rd = csv.reader(io.StringIO(text))
    head = next(rd, None)
    if not head or len(head) < 2:
        raise ValueError(f"unexpected FRED header {head}")
    out = []
    for row in rd:
        if len(row) < 2:
            continue
        try:
            out.append((dt.date.fromisoformat(row[0].strip()), float(row[1])))
        except ValueError:
            continue
    return out


def parse_treasury_bills(text: str, column: str = cfg.TREASURY_BILL_COLUMN) -> list:
    """Treasury daily bill-rate CSV -> [(date, percent)] from the 13-week bank-discount column (header text is matched
    case-insensitively; dates are MM/DD/YYYY)."""
    rd = csv.reader(io.StringIO(text))
    head = next(rd, None)
    if not head:
        raise ValueError("empty Treasury file")
    names = [h.strip().strip('"').upper() for h in head]
    if "DATE" not in names or column.upper() not in names:
        raise ValueError(f"unexpected Treasury header {head}")
    i_d, i_v = names.index("DATE"), names.index(column.upper())
    out = []
    for row in rd:
        if len(row) <= max(i_d, i_v):
            continue
        try:
            out.append((dt.datetime.strptime(row[i_d].strip(), "%m/%d/%Y").date(), float(row[i_v])))
        except ValueError:
            continue
    return sorted(out)


def parse_yahoo_chart(raw: bytes) -> list:
    """Yahoo chart JSON -> [(New York date of the bar, raw close)], bars with a null close dropped."""
    j = json.loads(raw)["chart"]["result"][0]
    ts = j["timestamp"]
    cl = j["indicators"]["quote"][0]["close"]
    out = {}
    for t, c in zip(ts, cl):
        if c is None:
            continue
        out[dt.datetime.fromtimestamp(t, tz=ET).date()] = float(c)
    return sorted(out.items())


def parse_nasdaq_hist(raw: bytes) -> list:
    """Nasdaq historical-quote JSON -> [(date, raw close)]. Rows look like {"date": "10/12/2026", "close": "778.10", ...}."""
    j = json.loads(raw)
    rows = ((((j.get("data") or {}).get("tradesTable")) or {}).get("rows"))
    if not rows:
        raise ValueError("no rows")
    out = {}
    for r in rows:
        try:
            d = dt.datetime.strptime(r["date"].strip(), "%m/%d/%Y").date()
            v = float(str(r["close"]).replace("$", "").replace(",", "").strip())
        except (ValueError, TypeError, KeyError):
            continue
        if v > 0:
            out[d] = v
    return sorted(out.items())


def parse_stooq(text: str) -> list:
    rd = csv.DictReader(io.StringIO(text))
    out = []
    for r in rd:
        try:
            out.append((dt.date.fromisoformat(r["Date"]), float(r["Close"])))
        except (ValueError, TypeError, KeyError):
            continue
    return sorted(out)


# ---------------------------------------------------------------------------------------------- fixtures
class FixtureHttp:
    """Serve every network source from saved files in a directory (tests, replays, `shadow_job.py --fixture-dir`).

    File names: options_<SYM>.json, quote_<NAME>.json, history_<NAME>.csv, fred_DTB3.csv, treasury_bills_<YEAR>.csv,
    yahoo_<SYM>.json, nasdaq_<SYM>.json, stooq_<SYM>.csv. A missing file behaves like a source that is down (FeedError)."""

    PATTERNS = (
        (re.compile(r"/delayed_quotes/options/([A-Z0-9]+)\.json"), "options_{0}.json"),
        (re.compile(r"/delayed_quotes/quotes/_([A-Z0-9]+)\.json"), "quote_{0}.json"),
        (re.compile(r"/daily_prices/([A-Z0-9]+)_History\.csv"), "history_{0}.csv"),
        (re.compile(r"fredgraph\.csv\?id=([A-Z0-9]+)"), "fred_{0}.csv"),
        (re.compile(r"daily-treasury-rates\.csv/(\d{4})/all\?type=daily_treasury_bill_rates"), "treasury_bills_{0}.csv"),
        (re.compile(r"/v8/finance/chart/([A-Z0-9.^-]+)\?"), "yahoo_{0}.json"),
        (re.compile(r"api\.nasdaq\.com/api/quote/([A-Z0-9.]+)/historical"), "nasdaq_{0}.json"),
        (re.compile(r"stooq\.com/q/d/l/\?s=([a-z0-9.^-]+?)\.us", re.I), "stooq_{0}.csv"),
    )

    def __init__(self, directory: str):
        self.dir = directory
        self.calls: list = []

    def name_for(self, url: str) -> Optional[str]:
        for rx, tmpl in self.PATTERNS:
            m = rx.search(url)
            if m:
                return tmpl.format(*[g.upper() if tmpl.startswith(("options", "yahoo", "nasdaq", "stooq")) else g
                                     for g in m.groups()])
        return None

    def __call__(self, url: str, **_kw) -> bytes:
        name = self.name_for(url)
        self.calls.append(name or url)
        path = None if name is None else os.path.join(self.dir, name)
        if path is None or not os.path.exists(path):
            raise FeedError(f"fixture missing for {url} ({name})")
        with open(path, "rb") as f:
            return f.read()
