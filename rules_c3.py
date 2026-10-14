"""C3 SPY_HEDGE - monthly SPY put debit spread, gated. Pure functions, no I/O.

Port of the study's strategies.c3_gate / c3_trade (primary variant G_A_95-85). On the first trading day of each month:

    GATE    VIX / VIX3M < 0.90 (strict)  AND  VIX <= the 25th percentile of the previous 252 VIX closes
            (day excluded, numpy linear quantile, at least 200 prior values)
    expiry  listed SPY expiry with DTE closest to 60 inside [50, 75] (ties -> the shorter), else "no_expiry"
    legs    BUY the listed put nearest 0.95 x spot, SELL the listed put nearest 0.85 x spot (must be lower), else "no_strikes"
    fills   buy mid + k*spread, sell mid - k*spread (k = 0, 0.25, 0.5); fees $0.05 per leg per side; both legs liquid
    debit   base-fill cash flow must be negative, else "no_debit"
    exit    the first trading day with DTE <= 21, at that day's quotes (closing fees apply)
    risk    max_loss = net debit x 100 + entry fees

The gate uses the entry day's VIX and VIX3M CLOSES, which are only published after the market closes. The job therefore
builds the ticket at the 16:20 snapshot as a *candidate* (gate provisional) and applies the gate to the published closes
on a later run (see shadow_job); `gate()` below is the pure test both calls use.
"""
from __future__ import annotations

import datetime as dt
from typing import Optional

import config as cfg
import fills
from chain import Chain, median_rel_spread, nearest_strike, pick_expiry

P = cfg.C3


def gate(vix: Optional[float], vix3m: Optional[float], q25: Optional[float]):
    """-> (passes: bool, info). Missing inputs never pass."""
    info = {"vix": vix, "vix3m": vix3m, "q25": q25}
    if vix is None or vix3m is None or vix3m <= 0:
        info["why"] = "missing_index"
        return False, info
    ratio = vix / vix3m
    info["ratio"] = ratio
    if q25 is None:
        info["why"] = "no_quantile"
        return False, info
    info["ratio_ok"] = ratio < P["ratio_max"]
    info["level_ok"] = vix <= q25
    return bool(info["ratio_ok"] and info["level_ok"]), info


def build_ticket(chain: Optional[Chain], d: dt.date, a: float = P["long_frac"], b: float = P["short_frac"]) -> dict:
    """The would-be ticket for entry day d, gate NOT applied. -> {"skip": reason | None, "detail", "ticket"}."""
    detail: dict = {}
    if chain is None:
        return {"skip": "no_chain", "detail": detail, "ticket": None}
    ex = pick_expiry(chain, d, P["dte_target"], P["dte_range"][0], P["dte_range"][1])
    if ex is None:
        return {"skip": "no_expiry", "detail": detail, "ticket": None}
    spot = chain.spot
    K1, K2 = nearest_strike(chain, ex, -1, a * spot), nearest_strike(chain, ex, -1, b * spot)
    if K1 is None or K2 is None or not K2 < K1:
        return {"skip": "no_strikes", "detail": {"k_long": K1, "k_short": K2}, "ticket": None}
    legs = [(-1, K1, ex, +1), (-1, K2, ex, -1)]
    detail["med_rel_spread"] = median_rel_spread(fills.quote_all(chain, legs) or [])
    pkg, fail = fills.price_entry(chain, legs)
    if pkg is None:
        detail["fail"] = fail
        return {"skip": "untradeable", "detail": detail, "ticket": None}
    if pkg["cf"][cfg.K_BASE] >= 0:
        detail["cf_base"] = pkg["cf"][cfg.K_BASE]
        return {"skip": "no_debit", "detail": detail, "ticket": None}
    ticket = {
        "legs": legs, "strikes": {"k_long": K1, "k_short": K2}, "spot": spot, "pkg": pkg, "expiry": ex,
        "med_rel_spread": detail["med_rel_spread"],
        "max_loss": fills.max_loss(legs, pkg["cf"][cfg.K_BASE]) + cfg.FEE_BASE * pkg["units"],
        "net_debit_base": -pkg["cf"][cfg.K_BASE],
    }
    return {"skip": None, "detail": detail, "ticket": ticket}


def exit_due(expiry: dt.date, t: dt.date) -> bool:
    """First trading day with DTE <= 21 (or on/after the expiry)."""
    return (expiry - t).days <= P["exit_dte"] or t >= expiry


def close_at(legs: list, quotes: list, d_exit: dt.date) -> dict:
    """Exit package at the given quotes (closing fees apply: units = legs)."""
    return {"cf": fills.close_cf(legs, quotes), "units": int(sum(abs(L[3]) for L in legs)), "d_exit": d_exit}
