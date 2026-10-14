"""Option-chain model, Cboe parser and the strike/expiry selection helpers.

The selection helpers are ports of the study's engine.py (pick_expiry, nearest_strike, pick_delta_strike, liq_ok); the
cleaning in `clean_ok` is chainlib.finish. They are deliberately plain Python so the same code runs on a live Cboe
snapshot and on the study's cleaned chain stores (tests/ and the research replay feed both through `Chain`).

A `Chain` holds CLEAN rows only: ask > 0, bid >= 0, ask >= bid, |strike/spot - 1| <= 35%, 0 <= DTE <= 130. Lists of
strikes therefore mean "listed and quoted", exactly as in the study.
"""
from __future__ import annotations

import datetime as dt
import math
import re
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

import config as cfg

ET = ZoneInfo("America/New_York")
UTC = dt.timezone.utc
REGULAR_CLOSE = dt.time(16, 0)       # ET
EARLY_CLOSE = dt.time(13, 0)         # ET: day after Thanksgiving, Dec 24 and Jul 3 when they fall on a weekday session
OCC_RE = re.compile(r"^([A-Z]+\d?)(\d{6})([CP])(\d{8})$")
_NAN = float("nan")


def parse_occ(sym: str):
    """'SPY261015C00735000' -> ('SPY', date(2026,10,15), +1, 735.0), or None when it is not an OCC symbol."""
    m = OCC_RE.match(sym or "")
    if not m:
        return None
    root, ymd, cp, k = m.groups()
    try:
        ex = dt.date(2000 + int(ymd[:2]), int(ymd[2:4]), int(ymd[4:6]))
    except ValueError:
        return None
    return root, ex, (1 if cp == "C" else -1), int(k) / 1000.0


def _f(x) -> Optional[float]:
    """float or None (None, '', NaN and non-numbers all become None)."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(v) else v


# ----------------------------------------------------------------------------- liquidity and fills
def liq_reason(bid: float, ask: float, short: bool) -> Optional[str]:
    """None when the leg passes the liquidity filter; otherwise why it fails: 'crossed', 'spread' or 'zero_bid'.
    engine.liq_ok: ask > 0, bid >= 0, ask >= bid, spread <= max($0.05, 30% of mid), and a short leg needs bid > 0."""
    sp = ask - bid
    mid = 0.5 * (ask + bid)
    if not (ask > 0 and bid >= 0 and sp >= 0):
        return "crossed"
    if sp > max(cfg.SPREAD_ABS, cfg.SPREAD_REL * mid) + 1e-9:
        return "spread"
    if short and bid <= 0:
        return "zero_bid"
    return None


def liq_ok(bid: float, ask: float, short: bool) -> bool:
    return liq_reason(bid, ask, short) is None


def clean_ok(bid, ask, strike, spot, dte) -> bool:
    """chainlib.finish row filter."""
    if bid is None or ask is None or not (spot and spot > 0):
        return False
    if not (0 <= dte <= cfg.MAX_DTE):
        return False
    if abs(strike / spot - 1.0) > cfg.MAX_MONEYNESS:
        return False
    return ask > 0 and bid >= 0 and ask >= bid


# ----------------------------------------------------------------------------- the chain
class Block:
    """One (expiry, call/put) block, strikes ascending, parallel lists."""
    __slots__ = ("strikes", "bid", "ask", "delta", "iv", "_ix")

    def __init__(self, rows):
        rows = sorted(rows, key=lambda r: r[0])
        self.strikes = [r[0] for r in rows]
        self.bid = [r[1] for r in rows]
        self.ask = [r[2] for r in rows]
        self.delta = [r[3] for r in rows]
        self.iv = [r[4] for r in rows]
        self._ix = {round(k * 1000): i for i, k in enumerate(self.strikes)}

    def __len__(self):
        return len(self.strikes)

    def index(self, strike: float) -> Optional[int]:
        return self._ix.get(round(float(strike) * 1000))


class Chain:
    """Clean option chain for one underlying on one session date.

    rows: iterable of (expiry: date, cp: +1/-1, strike, bid, ask, delta|None, iv|None). They are NOT re-cleaned here;
    use `from_cboe` (or pre-cleaned store rows).
    """

    def __init__(self, symbol: str, asof: dt.date, spot: float, rows: Iterable, meta: Optional[dict] = None):
        self.symbol, self.asof, self.spot = symbol, asof, float(spot)
        self.meta = meta or {}
        grouped: dict = {}
        n = 0
        for (ex, cp, k, bid, ask, delta, iv) in rows:
            grouped.setdefault((ex, cp), []).append((float(k), float(bid), float(ask), delta, iv))
            n += 1
        self._blocks = {key: Block(v) for key, v in grouped.items()}
        self._expiries = sorted({ex for (ex, _cp) in self._blocks})
        self.n_rows = n

    # lookups -----------------------------------------------------------------------------
    def expiries(self) -> list:
        return list(self._expiries)

    def has_expiry(self, ex: dt.date) -> bool:
        return ex in set(self._expiries)

    def block(self, ex: dt.date, cp: int) -> Optional[Block]:
        return self._blocks.get((ex, 1 if cp > 0 else -1))

    def strikes(self, ex: dt.date, cp: int) -> list:
        b = self.block(ex, cp)
        return [] if b is None else b.strikes

    def quote(self, ex: dt.date, cp: int, strike: float):
        """(bid, ask) or None if that contract is not quoted."""
        b = self.block(ex, cp)
        if b is None:
            return None
        i = b.index(strike)
        return None if i is None else (b.bid[i], b.ask[i])

    def row(self, ex: dt.date, cp: int, strike: float):
        """dict(bid, ask, delta, iv) or None."""
        b = self.block(ex, cp)
        if b is None:
            return None
        i = b.index(strike)
        return None if i is None else dict(bid=b.bid[i], ask=b.ask[i], delta=b.delta[i], iv=b.iv[i])


# ----------------------------------------------------------------------------- selection (engine.py)
def pick_expiry(chain: Chain, d: dt.date, target: int, lo: int, hi: int) -> Optional[dt.date]:
    """Expiry minimising |DTE - target| with lo <= DTE <= hi (calendar days); ties -> the shorter. None if none."""
    best = None
    for ex in chain.expiries():
        dte = (ex - d).days
        if lo <= dte <= hi:
            key = (abs(dte - target), ex)
            if best is None or key < best[0]:
                best = (key, ex)
    return None if best is None else best[1]


def nearest_strike(chain: Chain, ex: dt.date, cp: int, target: float) -> Optional[float]:
    """Listed strike nearest `target` (all quoted strikes count, not only liquid ones); ties -> the lower strike."""
    st = chain.strikes(ex, cp)
    if not st:
        return None
    best = min(st, key=lambda k: (abs(k - target), k))
    return float(best)


def pick_delta_strike(chain: Chain, ex: dt.date, cp: int, target: float, short: bool = True,
                      tol: float = cfg.DELTA_TOL, deltas: Optional[list] = None) -> Optional[float]:
    """Strike whose |delta| is closest to `target` among legs passing the liquidity filter; None if the best miss > tol.

    A leg needs: ask > 0, a delta, spread <= max($0.05, 30% of mid), and (short) bid > 0. The first minimum wins, so
    ties go to the lower strike. `deltas` optionally replaces the feed deltas (parallel to the block's strikes); it is
    how the Black-Scholes 'alt selection' diagnostic runs the same rule.
    """
    b = chain.block(ex, cp)
    if b is None or len(b) == 0:
        return None
    dl = b.delta if deltas is None else deltas
    best_j, best_diff = None, math.inf
    for j in range(len(b)):
        bid, ask, d = b.bid[j], b.ask[j], dl[j]
        if d is None or (isinstance(d, float) and math.isnan(d)) or not ask > 0:
            continue
        sp, mid = ask - bid, 0.5 * (ask + bid)
        if sp > max(cfg.SPREAD_ABS, cfg.SPREAD_REL * mid) + 1e-9:
            continue
        if short and not bid > 0:
            continue
        diff = abs(abs(float(d)) - target)
        if diff < best_diff:                         # strict: the first (lowest-strike) minimum wins
            best_j, best_diff = j, diff
    if best_j is None or best_diff > tol:
        return None
    return float(b.strikes[best_j])


def median_rel_spread(quotes: list) -> Optional[float]:
    """Median of (ask - bid) / mid over the given (bid, ask) quotes; None when empty or every mid is 0."""
    vals = sorted((a - b) / (0.5 * (a + b)) for (b, a) in quotes if (a + b) > 0)
    if not vals:
        return None
    n = len(vals)
    return vals[n // 2] if n % 2 else 0.5 * (vals[n // 2 - 1] + vals[n // 2])


# ----------------------------------------------------------------------------- Cboe parser
def parse_file_ts(s) -> Optional[dt.datetime]:
    """The feed's top-level 'timestamp' ('2026-10-13 03:53:23', UTC) -> aware datetime."""
    try:
        return dt.datetime.strptime(str(s), "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return None


def parse_et_naive(s) -> Optional[dt.datetime]:
    """Per-contract and underlying 'last_trade_time' ('2026-10-12T16:00:00', Eastern, no offset) -> aware ET datetime.
    The index quotes GVZ and VXTLT carry an offset instead ('2026-10-13T09:50:01.402000-05:00', Central time): a string with
    an offset is converted to Eastern, a string without one is taken to be Eastern already."""
    if not s:
        return None
    try:
        t = dt.datetime.fromisoformat(str(s))
    except ValueError:
        return None
    return t.astimezone(ET) if t.tzinfo is not None else t.replace(tzinfo=ET)


def close_print_floor(close_et: dt.time = REGULAR_CLOSE) -> dt.time:
    """Earliest last-trade time that proves the closing print is in: the close time less CLOSE_PRINT_TOL_MIN (15:59 ET on a
    regular day, 12:59 on an early close)."""
    base = dt.datetime.combine(dt.date(2000, 1, 3), close_et)
    return (base - dt.timedelta(minutes=cfg.CLOSE_PRINT_TOL_MIN)).time()


def choose_spot(und_last, und_close, last_trade_et: Optional[dt.datetime], session: dt.date,
                close_et: dt.time = REGULAR_CLOSE):
    """-> (spot, source). The official close once the closing print is in (the underlying's last trade is on the session
    date at or after close_et - CLOSE_PRINT_TOL_MIN); otherwise the last price. Intraday the feed's `close` field simply
    repeats the last price, so it is never trusted before the closing print. The study's chain 'und' is the snapshot-time
    underlying."""
    c, l = _f(und_close), _f(und_last)
    if c and c > 0 and last_trade_et is not None and last_trade_et.date() == session \
            and last_trade_et.timetz().replace(tzinfo=None) >= close_print_floor(close_et):
        return c, "close"
    if l and l > 0:
        return l, "last"
    if c and c > 0:
        return c, "close_unverified"
    return None, "none"


def from_cboe(obj: dict, symbol: str, session: dt.date, now_utc: Optional[dt.datetime] = None,
              close_et: dt.time = REGULAR_CLOSE) -> Chain:
    """Parse one Cboe delayed-quotes options file into a clean Chain with feed diagnostics in `meta`.

    Raises ValueError when the file has no usable underlying or no parseable options (the caller treats that as
    'data unavailable', retries, and posts one notice)."""
    data = (obj or {}).get("data") or {}
    opts = data.get("options") or []
    if not opts:
        raise ValueError(f"{symbol}: feed returned no options")
    last_et = parse_et_naive(data.get("last_trade_time"))
    spot, spot_src = choose_spot(data.get("current_price"), data.get("close"), last_et, session, close_et)
    if not spot:
        raise ValueError(f"{symbol}: feed has no underlying price")
    file_ts = parse_file_ts(obj.get("timestamp"))
    dropped = {"root": 0, "unparsed": 0, "no_quote": 0, "dte": 0, "moneyness": 0, "crossed_or_zero_ask": 0}
    rows, n_two_sided, n_zero_bid, n_no_delta, n_crossed = [], 0, 0, 0, 0
    newest = None
    atm_window = []
    for o in opts:
        p = parse_occ(o.get("option"))
        if p is None:
            dropped["unparsed"] += 1
            continue
        root, ex, cp, k = p
        if root != symbol:
            dropped["root"] += 1
            continue
        bid, ask = _f(o.get("bid")), _f(o.get("ask"))
        if bid is None or ask is None:
            dropped["no_quote"] += 1
            continue
        dte = (ex - session).days
        if not (0 <= dte <= cfg.MAX_DTE):
            dropped["dte"] += 1
            continue
        if abs(k / spot - 1.0) > cfg.MAX_MONEYNESS:
            dropped["moneyness"] += 1
            continue
        if bid > ask:
            n_crossed += 1
        if not (ask > 0 and bid >= 0 and ask >= bid):
            dropped["crossed_or_zero_ask"] += 1
            continue
        delta, iv = _f(o.get("delta")), _f(o.get("iv"))
        if delta is None:
            n_no_delta += 1
        if bid > 0:
            n_two_sided += 1
        else:
            n_zero_bid += 1
        t = parse_et_naive(o.get("last_trade_time"))
        if t is not None and abs(k / spot - 1.0) <= 0.02 and (newest is None or t > newest):
            newest = t
        rows.append((ex, cp, k, bid, ask, delta, iv))
    if not rows:
        raise ValueError(f"{symbol}: no clean option rows after filtering ({dropped})")
    now_utc = now_utc or dt.datetime.now(UTC)
    age = None if file_ts is None else (now_utc - file_ts).total_seconds() / 60.0
    meta = dict(
        symbol=symbol, session=session.isoformat(), spot_src=spot_src,
        und_last=_f(data.get("current_price")), und_close=_f(data.get("close")),
        und_bid=_f(data.get("bid")), und_ask=_f(data.get("ask")), und_iv30=_f(data.get("iv30")),
        und_last_trade_et=None if last_et is None else last_et.isoformat(timespec="seconds"),
        file_ts_utc=None if file_ts is None else file_ts.isoformat(timespec="seconds"),
        file_age_min=None if age is None else round(age, 1),
        newest_atm_trade_et=None if newest is None else newest.isoformat(timespec="seconds"),
        n_raw=len(opts), n_clean=len(rows), dropped=dropped, n_two_sided=n_two_sided, n_zero_bid=n_zero_bid,
        n_no_delta=n_no_delta, n_crossed_raw=n_crossed,
    )
    return Chain(symbol, session, spot, rows, meta)


def snapshot_stale(meta: dict, session: dt.date, require_close: bool = True,
                   close_et: dt.time = REGULAR_CLOSE) -> Optional[str]:
    """Reason the snapshot cannot be trusted as the session's closing snapshot, or None.

    (1) the underlying's last trade is not on the session date, (2) with require_close, the closing print (a trade at or
    after close_et - CLOSE_PRINT_TOL_MIN) is not in the file yet, i.e. the file is a pre-close snapshot, (3) the file is
    older than STALE_FILE_AGE_MIN (the file timestamp is a generation time, so this is a sanity bound), (4) there is no
    timestamp."""
    lt = meta.get("und_last_trade_et")
    if not lt:
        return "no_underlying_trade_time"
    t = dt.datetime.fromisoformat(lt)
    if t.date() != session:
        return f"underlying_last_trade_{lt[:10]}"
    if require_close and t.timetz().replace(tzinfo=None) < close_print_floor(close_et):
        return f"pre_close_snapshot_{lt[11:16]}"
    age = meta.get("file_age_min")
    if age is None:
        return "no_file_timestamp"
    if age > cfg.STALE_FILE_AGE_MIN:
        return f"file_age_{age:.0f}min"
    return None
