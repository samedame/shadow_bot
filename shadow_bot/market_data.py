"""Market data held by the bot: raw (unadjusted) closes, Cboe index closes, the 3-month T-bill, and the gate math.

Mirrors the study's market.py:
  * rv20(d)            - sample std (ddof=1) of the 20 close-to-close returns ending at d (SPY raw closes)
  * condor_gate(d)     - RV20(p) < median(RV20 over the 20 trading days ending at p), p = previous trading day (p included)
  * trailing_quantile  - numpy-style linear quantile of the previous `window` VIX closes (day excluded), >= min_obs values
  * rate(d)            - 3-month T-bill (decimal), last known value on or before d

Published vs provisional: Cboe publishes index closes after the market closes, so at 16:20 ET the day's index value can
only be a delayed quote. Published values live in `indices` (the series every gate reads); provisional ones live in `prov`
and are never mixed into a gate that decides a position.

Caches are plain CSVs in data/ (sorted, so git diffs stay small).
"""
from __future__ import annotations

import bisect
import csv
import datetime as dt
import math
import os
from typing import Optional

import config as cfg
import nyse_calendar as nyse

SOURCE_RANK = {"cboe": 0, "stooq": 1, "nasdaq": 1, "yahoo": 2, "seed": 2}      # higher wins when two sources disagree


class Series:
    """date -> float, sorted, with the positional access the study's gates use."""

    def __init__(self, items=None):
        self.dates: list = []
        self.values: list = []
        self._ix: dict = {}
        if items:
            for d, v in (items.items() if isinstance(items, dict) else items):
                self.set(d, v)

    def set(self, d: dt.date, v: float) -> None:
        if d in self._ix:
            self.values[self._ix[d]] = float(v)
            return
        if not self.dates or d > self.dates[-1]:               # the common case: append in date order, O(1)
            self._ix[d] = len(self.dates)
            self.dates.append(d)
            self.values.append(float(v))
            return
        i = bisect.bisect_left(self.dates, d)
        self.dates.insert(i, d)
        self.values.insert(i, float(v))
        self._ix = {x: j for j, x in enumerate(self.dates)}

    def get(self, d: dt.date) -> Optional[float]:
        i = self._ix.get(d)
        return None if i is None else self.values[i]

    def __contains__(self, d):
        return d in self._ix

    def __len__(self):
        return len(self.dates)

    def last_date(self) -> Optional[dt.date]:
        return self.dates[-1] if self.dates else None

    def prior_values(self, d: dt.date, n: int) -> list:
        """Up to n values strictly before d, ascending by date (positional, as the study's values[pos-n:pos])."""
        hi = bisect.bisect_left(self.dates, d)
        return self.values[max(0, hi - n):hi]

    def asof(self, d: dt.date) -> Optional[float]:
        """Last value on or before d."""
        i = bisect.bisect_right(self.dates, d) - 1
        return self.values[i] if i >= 0 else None


def quantile_linear(values: list, q: float) -> float:
    """numpy.quantile(values, q) with the default 'linear' method."""
    s = sorted(values)
    n = len(s)
    if n == 0:
        raise ValueError("empty")
    pos = q * (n - 1)
    lo = math.floor(pos)
    hi = min(lo + 1, n - 1)
    frac = pos - lo
    a, b = s[lo], s[hi]
    # numpy's lerp: a + (b - a) * t, with the symmetric form for t >= 0.5
    return b - (b - a) * (1.0 - frac) if frac >= 0.5 else a + (b - a) * frac


def median(values: list) -> float:
    s = sorted(values)
    n = len(s)
    return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])


def sample_std(xs: list) -> float:
    """numpy.std(xs, ddof=1)."""
    n = len(xs)
    m = sum(xs) / n
    return math.sqrt(sum((x - m) * (x - m) for x in xs) / (n - 1))


class Market:
    def __init__(self, closes=None, indices=None, tbill=None, calendar=nyse, prov=None, close_src=None):
        self.cal = calendar
        self.closes = {s: (v if isinstance(v, Series) else Series(v)) for s, v in (closes or {}).items()}
        self.indices = {s: (v if isinstance(v, Series) else Series(v)) for s, v in (indices or {}).items()}
        self.tbill = tbill if isinstance(tbill, Series) else Series(tbill or {})      # decimal rates
        self.prov: dict = dict(prov or {})                  # (name, date) -> provisional index value
        self.close_src: dict = dict(close_src or {})        # (symbol, date) -> source tag
        self._rv: dict = {}

    # ---- accessors ------------------------------------------------------------------------
    def close(self, sym: str, d: dt.date) -> Optional[float]:
        s = self.closes.get(sym)
        return None if s is None else s.get(d)

    def index_val(self, name: str, d: dt.date, allow_prov: bool = False) -> Optional[float]:
        s = self.indices.get(name)
        v = None if s is None else s.get(d)
        if v is None and allow_prov:
            v = self.prov.get((name, d))
        return v

    def index_series(self, name: str) -> Series:
        return self.indices.setdefault(name, Series())

    def rate(self, d: dt.date) -> float:
        """3-month T-bill as a decimal, last known on or before d (0.0 if none)."""
        v = self.tbill.asof(d)
        return 0.0 if v is None else v

    # ---- gates ----------------------------------------------------------------------------
    def rv20(self, d: dt.date) -> Optional[float]:
        if d in self._rv:
            return self._rv[d]
        out = None
        try:
            days = [self.cal.td_offset(d, k) for k in range(-20, 1)]
        except (ValueError, nyse.CalendarRange, IndexError):
            days = None
        if days is not None:
            cl = [self.close("SPY", x) for x in days]
            if all(c is not None for c in cl):
                out = sample_std([cl[i + 1] / cl[i] - 1.0 for i in range(20)])
        self._rv[d] = out
        return out

    def condor_gate(self, d: dt.date):
        """C9 gate at entry day d using closes through the previous trading day p.
        -> (gate: bool | None, info). None means 'cannot be computed' (a missing close), never 'closed'."""
        try:
            p = self.cal.prev_td(d)
        except (ValueError, nyse.CalendarRange):
            return None, {"why": "no_calendar"}
        vals = []
        for k in range(cfg.C9["gate_window"]):
            try:
                q = self.cal.td_offset(p, -k)
            except (ValueError, nyse.CalendarRange):
                return None, {"why": "no_calendar", "prev": p.isoformat()}
            v = self.rv20(q)
            if v is None:
                return None, {"why": "missing_close", "prev": p.isoformat(), "need": q.isoformat()}
            vals.append(v)
        med = median(vals)
        return bool(vals[0] < med), {"prev": p.isoformat(), "rv20": vals[0], "median": med, "window": len(vals)}

    def condor_gate_alt(self, d: dt.date):
        """The OTHER reading of the C9 gate, logged but never used to decide: RV20(p) < median of the 20 RV20 values BEFORE
        p (p-20 .. p-1, p excluded). The study's first reading (501 trades, +$851.80); the primary above includes p
        (499 trades, +$912.68). -> (gate | None, info)."""
        try:
            p = self.cal.prev_td(d)
            vals = [self.rv20(self.cal.td_offset(p, -k)) for k in range(1, cfg.C9["gate_window"] + 1)]
        except (ValueError, nyse.CalendarRange):
            return None, {"why": "no_calendar"}
        rp = self.rv20(p)
        if rp is None or any(v is None for v in vals):
            return None, {"why": "missing_close"}
        med = median(vals)
        return bool(rp < med), {"rv20": rp, "median": med}

    def trailing_quantile(self, name: str, d: dt.date, q: float, window: int = 252, min_obs: int = 200):
        """Linear quantile q of the previous `window` published values of index `name` (day d excluded); None if fewer
        than min_obs values."""
        prev = self.index_series(name).prior_values(d, window)
        if len(prev) < min_obs:
            return None
        return quantile_linear(prev, q)

    def missing_spy_closes_for_gate(self, d: dt.date) -> list:
        """Trading days whose SPY close is missing but needed by condor_gate(d); empty when the gate is computable."""
        try:
            p = self.cal.prev_td(d)
            need = [self.cal.td_offset(p, -k) for k in range(0, cfg.C9["gate_window"] + cfg.C9["rv_window"])]
        except (ValueError, nyse.CalendarRange):
            return []
        return sorted(x for x in need if self.close("SPY", x) is None)


# ----------------------------------------------------------------------------------------- merging
def merge_closes(m: Market, symbol: str, rows, src: str, tol: float = 0.005, max_rel: float = 0.02) -> dict:
    """Merge (date, close) rows from one source. Higher-ranked sources win a small disagreement (the same close, a few cents
    apart); equal or lower ones, and ANY disagreement wider than `max_rel` (two sources 2% apart means one of them is wrong and
    nothing here can tell which), keep what is stored and are only reported.
    -> {"added": [...], "corrected": [(date, old, new)], "mismatch": [(date, old, new)]}"""
    s = m.closes.setdefault(symbol, Series())
    rep = {"added": [], "corrected": [], "mismatch": []}
    for d, v in rows:
        if v is None or not (v > 0):
            continue
        old = s.get(d)
        if old is None:
            s.set(d, v)
            m.close_src[(symbol, d)] = src
            rep["added"].append(d)
            m._rv.clear()
        elif abs(old - v) > tol:
            if abs(v / old - 1.0) <= max_rel and SOURCE_RANK.get(src, 0) > SOURCE_RANK.get(m.close_src.get((symbol, d), "cboe"), 0):
                s.set(d, v)
                m.close_src[(symbol, d)] = src
                rep["corrected"].append((d, old, v))
                m._rv.clear()
            else:
                rep["mismatch"].append((d, old, v))
        elif SOURCE_RANK.get(src, 0) > SOURCE_RANK.get(m.close_src.get((symbol, d), "cboe"), 0):
            m.close_src[(symbol, d)] = src                  # same value, better provenance
    return rep


def merge_published_index(m: Market, name: str, rows) -> list:
    """Add published index closes; returns the dates that were new. A published value replaces a provisional one."""
    s = m.index_series(name)
    new = []
    for d, v in rows:
        if v is None:
            continue
        if d not in s:
            new.append(d)
        s.set(d, v)
        m.prov.pop((name, d), None)
    return new


def merge_tbill(m: Market, rows) -> list:
    new = []
    for d, pct in rows:
        if pct is None:
            continue
        if d not in m.tbill:
            new.append(d)
        m.tbill.set(d, pct / 100.0)
    return new


# ----------------------------------------------------------------------------------------- CSV cache IO
def _d(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


def _read(path: str):
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def _write_atomic(path: str, header: list, rows: list) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(header)
        w.writerows(rows)
    os.replace(tmp, path)


def load_market(root: str = ".", calendar=nyse) -> Market:
    m = Market(calendar=calendar)
    for r in _read(os.path.join(root, cfg.CLOSES_FILE)):
        sym, d = r["symbol"], _d(r["date"])
        m.closes.setdefault(sym, Series()).set(d, float(r["close"]))
        m.close_src[(sym, d)] = r.get("src") or "seed"
    for r in _read(os.path.join(root, cfg.INDICES_FILE)):
        name, d, v = r["symbol"], _d(r["date"]), float(r["value"])
        if r.get("status") == "prov":
            m.prov[(name, d)] = v
        else:
            m.index_series(name).set(d, v)
    for r in _read(os.path.join(root, cfg.TBILL_FILE)):
        m.tbill.set(_d(r["date"]), float(r["rate_pct"]) / 100.0)
    return m


def save_market(m: Market, root: str = ".") -> None:
    rows = []
    for sym in sorted(m.closes):
        s = m.closes[sym]
        rows += [(d.isoformat(), sym, repr(v), m.close_src.get((sym, d), "seed")) for d, v in zip(s.dates, s.values)]
    rows.sort(key=lambda r: (r[0], r[1]))
    _write_atomic(os.path.join(root, cfg.CLOSES_FILE), ["date", "symbol", "close", "src"], rows)

    rows = []
    for name in sorted(m.indices):
        s = m.indices[name]
        rows += [(d.isoformat(), name, repr(v), "pub") for d, v in zip(s.dates, s.values)]
    rows += [(d.isoformat(), name, repr(v), "prov") for (name, d), v in m.prov.items()]
    rows.sort(key=lambda r: (r[0], r[1], r[3]))
    _write_atomic(os.path.join(root, cfg.INDICES_FILE), ["date", "symbol", "value", "status"], rows)

    rows = [(d.isoformat(), repr(round(v * 100.0, 6))) for d, v in zip(m.tbill.dates, m.tbill.values)]
    _write_atomic(os.path.join(root, cfg.TBILL_FILE), ["date", "rate_pct"], rows)


def prune_prov(m: Market, today: dt.date, keep_days: int = 10) -> None:
    for key in [k for k in m.prov if (today - k[1]).days > keep_days]:
        del m.prov[key]


def _trimmed(s: Series, lo: dt.date) -> Series:
    return Series([(d, v) for d, v in zip(s.dates, s.values) if d >= lo])


def trim_market(m: Market, today: dt.date) -> None:
    """Drop history nobody reads any more, so the committed CSVs stay small. Keeps what the strategies look back over:
    SPY/GLD/TLT closes (C9 needs 41 trading days, C3's hedge ledger a year), the VIX series (252 values for the C3 quantile),
    the other index series and the T-bill for the life of a C6 spread."""
    lo = today - dt.timedelta(days=cfg.CLOSES_KEEP_DAYS)
    for sym in list(m.closes):
        m.closes[sym] = _trimmed(m.closes[sym], lo)
    for key in [k for k in m.close_src if k[1] < lo]:
        del m.close_src[key]
    for name in list(m.indices):
        m.indices[name] = _trimmed(m.indices[name], today - dt.timedelta(days=cfg.HISTORY_KEEP_DAYS.get(name, 150)))
    m.tbill = _trimmed(m.tbill, today - dt.timedelta(days=cfg.TBILL_KEEP_DAYS))
    m._rv.clear()
