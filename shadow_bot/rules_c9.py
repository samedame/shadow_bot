"""C9 CONDOR_1D - overnight SPY 1-DTE iron condor. Pure functions, no I/O.

Port of the study's strategies.c9_trade (PREREG section 4, primary variant rb_d16_w10). On trading day d at the close
snapshot:

    GATE    market.condor_gate(d): RV20(p) < median(RV20 over the 20 trading days ending at p), p = previous trading day
            (the window INCLUDES p - the reading behind the reported +$913 on 499 trades)
    expiry  must be listed and equal next_td(d), else skip "no_1dte"
    shorts  call and put whose |delta| is closest to 0.16 (best miss <= 0.10), liquid, bid > 0
    wings   listed strike nearest short +/- 1.0% x spot (must be beyond the short strike), else "no_wings"
    legs    every leg quoted and liquid (spread <= max($0.05, 30% of mid)), else "untradeable"
    fills   sell mid - k*spread, buy mid + k*spread; k = 0 (mid), 0.25 (base), 0.5 (natural); fees $0.05 per leg per side
    credit  base-fill credit must be > 0, else "no_credit"
    exit    held to the expiry (next trading day); settled at intrinsic on that day's SPY close; no closing fee
    risk    max_loss = wider wing x 100 - credit + entry fees

Skip reasons (the study's labels): no_gate_data (new: the gate could not be computed), gate_closed (new: the study's driver
skips a falsy gate without counting it), no_chain, no_calendar, no_1dte, no_short_strike, no_wings, untradeable, no_credit.
"""
from __future__ import annotations

import datetime as dt
from typing import Optional

import config as cfg
import fills
from bs import bs_delta
from chain import Chain, median_rel_spread, nearest_strike, pick_delta_strike

P = cfg.C9


def evaluate(chain: Optional[Chain], d: dt.date, d1: Optional[dt.date], gate: Optional[bool],
             alt_ctx: Optional[dict] = None, delta_t: float = P["delta"], wing: float = P["wing"]) -> dict:
    """Entry decision for session d.

    chain   the SPY chain for d (None -> "no_chain")
    d1      next trading day (the required expiry)
    gate    True / False / None (None = could not be computed)
    alt_ctx optional {"r": decimal T-bill rate} -> also run the strike picks on Black-Scholes deltas from the feed's IV
            and report whether they differ (the spec's first-month selection diagnostic)

    -> {"skip": reason | None, "detail": {...}, "ticket": {...} | None}
    """
    detail: dict = {}
    if gate is None:
        return {"skip": "no_gate_data", "detail": detail, "ticket": None}
    if not gate:
        return {"skip": "gate_closed", "detail": detail, "ticket": None}
    if chain is None:
        return {"skip": "no_chain", "detail": detail, "ticket": None}
    if d1 is None:
        return {"skip": "no_calendar", "detail": detail, "ticket": None}
    if not chain.has_expiry(d1):
        return {"skip": "no_1dte", "detail": {"want": d1.isoformat()}, "ticket": None}
    ex = d1
    spot = chain.spot
    Kc = pick_delta_strike(chain, ex, +1, delta_t, short=True)
    Kp = pick_delta_strike(chain, ex, -1, delta_t, short=True)
    if Kc is None or Kp is None:
        return {"skip": "no_short_strike", "detail": {"call": Kc, "put": Kp}, "ticket": None}
    Kcl = nearest_strike(chain, ex, +1, Kc + wing * spot)
    Kpl = nearest_strike(chain, ex, -1, Kp - wing * spot)
    if Kcl is None or Kpl is None or not (Kcl > Kc and Kpl < Kp):
        return {"skip": "no_wings", "detail": {"call": Kc, "put": Kp, "call_wing": Kcl, "put_wing": Kpl}, "ticket": None}
    legs = [(+1, Kc, ex, -1), (+1, Kcl, ex, +1), (-1, Kp, ex, -1), (-1, Kpl, ex, +1)]
    detail["med_rel_spread"] = median_rel_spread(fills.quote_all(chain, legs) or [])
    pkg, fail = fills.price_entry(chain, legs)
    if pkg is None:
        detail["fail"] = fail
        return {"skip": "untradeable", "detail": detail, "ticket": None}
    credit = pkg["cf"][cfg.K_BASE]
    if credit <= 0:
        detail["credit_base"] = credit
        return {"skip": "no_credit", "detail": detail, "ticket": None}

    alt = None
    if alt_ctx is not None:
        alt = _alt_selection(chain, d, ex, spot, Kc, Kp, alt_ctx.get("r", 0.0), delta_t)
    ticket = {
        "legs": legs,
        "strikes": {"k_call": Kc, "k_put": Kp, "k_call_wing": Kcl, "k_put_wing": Kpl},
        "spot": spot,
        "pkg": pkg,
        "expiry": ex,
        "alt": alt,
        "med_rel_spread": detail["med_rel_spread"],
        "max_loss": fills.max_loss(legs, credit) + cfg.FEE_BASE * pkg["units"],
    }
    return {"skip": None, "detail": detail, "ticket": ticket}


def _alt_selection(chain: Chain, d: dt.date, ex: dt.date, spot: float, Kc: float, Kp: float, r: float, delta_t: float) -> dict:
    """Same pick rule on Black-Scholes deltas computed from the feed's implied volatility (q = 0)."""
    T = max((ex - d).days, 1) / 365.0
    out = {}
    for name, cp in (("call", +1), ("put", -1)):
        b = chain.block(ex, cp)
        deltas = [bs_delta(cp, spot, k, T, r, iv) if iv else None for k, iv in zip(b.strikes, b.iv)]
        out[name] = pick_delta_strike(chain, ex, cp, delta_t, short=True, deltas=deltas)
    out["differs"] = (out["call"] != Kc) or (out["put"] != Kp)
    out["feed_call"], out["feed_put"] = Kc, Kp
    return out


def settle(ticket_legs: list, settle_price: float, d_exit: dt.date) -> dict:
    """Exit package at expiry: intrinsic on the expiry-date SPY close, no closing fee, no units."""
    return {"cf": fills.settle_cf(ticket_legs, settle_price), "units": 0, "d_exit": d_exit}
