"""C6 NONEQ_VRP - GLD / TLT weekly bull put spreads. Pure functions, no I/O.

Two parallel versions of every entry; comparing them is the point of the shadow.

MODEL version = the backtest, exactly (port of strategies.c6_trade, variant "flat"):
    S0 = raw close on the entry day; vol = GVZ/100 (GLD) or VXTLT/100 (TLT), flat for both legs; r = 3-month T-bill; no dividends
    expiry = entry + 30 calendar days;  width = max(1, round(2% x S0)) with Python's banker's rounding
    short strike = the integer K in range(int(0.80 S0), int(S0) + 1) whose Black-Scholes |put delta| is closest to 0.25
    (first minimum wins);  long strike = short - width;  V = Black-Scholes value over 30/365 years
    half-spread h = max($0.03, 4% x V);  bid = max(V - h, 0), ask = V + h;  fills at V -/+ k*(2h) (k = 0, 0.25, 0.5, 0.75)
    skip when the base-fill credit <= 0
    each later trading day: re-mark both legs with THAT day's close, index value and T-bill rate and the remaining days/365;
    exit at the first day the mid-mark <= 50% of the base credit ("profit"), else at the first day with DTE <= 7 ("dte")
    fees $0.05 per contract per leg per side (closing fee applies: these spreads never expire)
  Inputs are PUBLISHED index closes and official closes only, so the model path lags the real one by up to a day.

REAL version (new, no backtest):
    expiry = the listed expiry with DTE closest to 30 inside [23, 37];  short put = |delta| closest to 0.25 (feed delta);
    long put = the listed strike nearest short - width;  same liquidity filter as C9;  fills from the real bid and ask
    exit on every daily run: close when the mid-mark cost to close <= 50% of the BASE entry credit, else on the first day
    with DTE <= 7 (quotes missing on that day: retry on the next 3 trading days, as close_package's drift rule)
"""
from __future__ import annotations

import datetime as dt
from typing import Optional

import config as cfg
import fills
import nyse_calendar as nyse
from bs import bs_put
from chain import Chain, median_rel_spread, nearest_strike, pick_delta_strike, pick_expiry

P = cfg.C6
HS_ABS, HS_REL = P["half_spread_abs"], P["half_spread_rel"]


# ------------------------------------------------------------------------------------------ MODEL path
def m_fill(value: float, side: int, k: float) -> float:
    """strategies._m_fill: half-spread h = max($0.03, 4% x V); bid = max(V - h, 0), ask = V + h."""
    h = max(HS_ABS, HS_REL * value)
    bid, ask = max(value - h, 0.0), value + h
    return fills.fill(bid, ask, side, k)


def model_width(S0: float) -> float:
    return float(max(P["width_min"], round(P["width_frac"] * S0)))


def model_short_strike(S0: float, r: float, sig: float) -> float:
    """The integer strike whose Black-Scholes |put delta| at 30/365 years is closest to 0.25 (first minimum wins)."""
    T0 = P["dte"] / 365.0
    best, bk = None, None
    for K in range(int(S0 * P["strike_scan_lo"]), int(S0) + 1):
        _, dl = bs_put(S0, float(K), T0, r, sig)
        diff = abs(abs(dl) - P["delta"])
        if best is None or diff < best:
            best, bk = diff, float(K)
    return bk


def model_entry(S0: float, iv_pct: float, r: float, d: dt.date) -> dict:
    """Entry pricing of the model spread from numbers only. -> {"skip": "no_credit"} or the entry dict."""
    sig = iv_pct / 100.0
    expiry = d + dt.timedelta(days=P["dte"])
    T0 = P["dte"] / 365.0
    width = model_width(S0)
    Ks = model_short_strike(S0, r, sig)
    Kl = Ks - width
    vs = bs_put(S0, Ks, T0, r, sig)[0]
    vl = bs_put(S0, Kl, T0, r, sig)[0]
    credit = {k: (m_fill(vs, -1, k) - m_fill(vl, +1, k)) * cfg.MULT for k in cfg.KS}
    ent = {"cf": credit, "units": 2, "mid_value": (vs - vl) * cfg.MULT}
    return {"skip": "no_credit" if credit[cfg.K_BASE] <= 0 else None,
            "Ks": Ks, "Kl": Kl, "width": width, "expiry": expiry, "S0": S0, "iv_pct": iv_pct, "r": r,
            "vs": vs, "vl": vl, "ent": ent}


def model_mark(Ks: float, Kl: float, St: float, it_pct: float, rt: float, dte: int) -> float:
    """Dollar mid-mark cost to close the model spread on a later day (positive number)."""
    Tt, sg = max(dte, 0) / 365.0, it_pct / 100.0
    ms = bs_put(St, Ks, Tt, rt, sg)[0]
    ml = bs_put(St, Kl, Tt, rt, sg)[0]
    return (ms - ml) * cfg.MULT


def model_exit_cf(Ks: float, Kl: float, St: float, it_pct: float, rt: float, dte: int) -> dict:
    Tt, sg = max(dte, 0) / 365.0, it_pct / 100.0
    vsx = bs_put(St, Ks, Tt, rt, sg)[0]
    vlx = bs_put(St, Kl, Tt, rt, sg)[0]
    return {k: (-m_fill(vsx, +1, k) + m_fill(vlx, -1, k)) * cfg.MULT for k in cfg.KS}


def model_trade(mkt, tk: str, d: dt.date, last_complete: dt.date, holes: str = "study", hole_grace: int = 5) -> dict:
    """Recompute the whole model trade for entry day d from published data (stateless, so it is exactly the backtest
    function whether replayed over history or re-run daily).

    last_complete  the latest date for which the closes AND the published index value are both expected to exist.
    holes          what to do with a day <= last_complete that has no data: "study" (exit at DTE<=7 or skip the day, as the
                   backtest does) or "wait" (return status "pending" for up to `hole_grace` trading days, then the study rule).

    -> {"status": "pending" | "skip" | "open" | "closed", "skip": reason | None, "entry": {...} | None, "exit": {...} | None,
        "last_mark": date | None, "pnl": {...} | None}
    """
    idx = P["vol_index"][tk]
    out = {"status": "skip", "skip": None, "entry": None, "exit": None, "last_mark": None, "pnl": None}
    S0, iv = mkt.close(tk, d), mkt.index_val(idx, d)
    if S0 is None or iv is None:
        if d > last_complete or (holes == "wait" and _hole_waiting(d, last_complete, hole_grace)):
            out["status"] = "pending"
            return out
        out["skip"] = "no_data"
        return out
    r = mkt.rate(d)
    ent = model_entry(S0, iv, r, d)
    out["entry"] = ent
    if ent["skip"]:
        out["skip"] = ent["skip"]
        return out
    expiry, Ks, Kl = ent["expiry"], ent["Ks"], ent["Kl"]
    credit_base = ent["ent"]["cf"][cfg.K_BASE]
    d_exit, why = None, None
    t = d
    while True:
        t = nyse.next_td(t)
        if t > last_complete:
            out["status"], out["last_mark"] = "open", _prev_td_or_none(t, d)
            return out
        dte = (expiry - t).days
        St, it = mkt.close(tk, t), mkt.index_val(idx, t)
        if St is None or it is None:
            if holes == "wait" and _hole_waiting(t, last_complete, hole_grace):
                out["status"], out["last_mark"] = "pending", _prev_td_or_none(t, d)
                return out
            if dte <= P["exit_dte"]:
                d_exit, why = t, "dte"
                break
            continue
        if model_mark(Ks, Kl, St, it, mkt.rate(t), dte) <= P["profit_frac"] * credit_base:
            d_exit, why = t, "profit"
            break
        if dte <= P["exit_dte"]:
            d_exit, why = t, "dte"
            break
    St, it = mkt.close(tk, d_exit), mkt.index_val(idx, d_exit)
    if St is None or it is None:
        out["skip"] = "exit_missing"
        return out
    cfx = model_exit_cf(Ks, Kl, St, it, mkt.rate(d_exit), (expiry - d_exit).days)
    legs = [(-1, Ks, expiry, -1), (-1, Kl, expiry, +1)]
    ext = {"cf": cfx, "units": 2}
    out["status"], out["last_mark"] = "closed", d_exit
    out["exit"] = {"date": d_exit, "reason": why, "St": St, "it_pct": it, "cf": cfx}
    out["legs"] = legs
    out["pnl"] = fills.assemble(legs, ent["ent"], ext)
    return out


def _hole_waiting(t: dt.date, last_complete: dt.date, grace: int) -> bool:
    return len(nyse.trading_days_between(t, last_complete)) < grace


def _prev_td_or_none(t: dt.date, floor: dt.date) -> Optional[dt.date]:
    p = nyse.prev_td(t)
    return p if p >= floor else None


# ------------------------------------------------------------------------------------------ REAL path
def real_entry(chain: Optional[Chain], d: dt.date, spot: float) -> dict:
    """Entry on the real GLD/TLT chain. -> {"skip": reason | None, "detail", "ticket"}."""
    detail: dict = {}
    if chain is None:
        return {"skip": "no_chain", "detail": detail, "ticket": None}
    lo, hi = P["real_dte_range"]
    ex = pick_expiry(chain, d, P["dte"], lo, hi)
    if ex is None:
        return {"skip": "no_expiry", "detail": detail, "ticket": None}
    Ks = pick_delta_strike(chain, ex, -1, P["delta"], short=True)
    if Ks is None:
        return {"skip": "no_short_strike", "detail": {"expiry": ex.isoformat()}, "ticket": None}
    width = model_width(spot)
    Kl = nearest_strike(chain, ex, -1, Ks - width)
    if Kl is None or not Kl < Ks:
        return {"skip": "no_wings", "detail": {"k_short": Ks, "k_long": Kl}, "ticket": None}
    legs = [(-1, Ks, ex, -1), (-1, Kl, ex, +1)]
    detail["med_rel_spread"] = median_rel_spread(fills.quote_all(chain, legs) or [])
    pkg, fail = fills.price_entry(chain, legs)
    if pkg is None:
        detail["fail"] = fail
        return {"skip": "untradeable", "detail": detail, "ticket": None}
    if pkg["cf"][cfg.K_BASE] <= 0:
        detail["credit_base"] = pkg["cf"][cfg.K_BASE]
        return {"skip": "no_credit", "detail": detail, "ticket": None}
    ticket = {
        "legs": legs, "strikes": {"k_short": Ks, "k_long": Kl}, "spot": spot, "pkg": pkg, "expiry": ex, "width": width,
        "med_rel_spread": detail["med_rel_spread"],
        "max_loss": fills.max_loss(legs, pkg["cf"][cfg.K_BASE]) + cfg.FEE_BASE * pkg["units"],
    }
    return {"skip": None, "detail": detail, "ticket": ticket}


def real_exit_decision(legs: list, quotes: Optional[list], entry_credit_base: float, expiry: dt.date, t: dt.date) -> dict:
    """What to do with an open REAL spread on day t.
    -> {"action": "hold" | "close" | "due_no_quotes", "reason": "profit" | "dte" | None, "cost_to_close": dollars | None}"""
    dte = (expiry - t).days
    cost = None if quotes is None else -fills.mid_mark(legs, quotes)
    if cost is not None and cost <= P["profit_frac"] * entry_credit_base:
        return {"action": "close", "reason": "profit", "cost_to_close": cost}
    if dte <= P["exit_dte"] or t >= expiry:
        if quotes is None:
            return {"action": "due_no_quotes", "reason": "dte", "cost_to_close": None}
        return {"action": "close", "reason": "dte", "cost_to_close": cost}
    return {"action": "hold", "reason": None, "cost_to_close": cost}
