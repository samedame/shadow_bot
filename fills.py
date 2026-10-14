"""Fills, fees, packages (multi-leg positions), settlement and max loss. Port of the study's engine.py.

Conventions (PREREG sections 3-4):
  * leg = (cp, strike, expiry, qty)   cp: +1 call / -1 put,  qty: +n long / -n short (contracts)
  * prices per share; cash flows in dollars (x100 multiplier); cf > 0 = cash received
  * fills: price = mid + side*k*(ask - bid), side +1 buy / -1 sell, k = 0.25 base, 0.5 natural, 0.75 (3x), 0 = mid
  * fees: $0.05 (base) / $0.65 (stress) per contract per leg per side; legs settled at expiry have no closing fee
  * pnl_mid carries NO fees (as in the study's `assemble`); pnl (base), pnl_nat, pnl_x3 and pnl_f65 do.
No strategy decisions are made here.
"""
from __future__ import annotations

import datetime as dt
from typing import Optional

import config as cfg
from chain import Chain, liq_reason

MULT = cfg.MULT
KS = cfg.KS


def intrinsic(cp: int, K: float, S: float) -> float:
    return max(0.0, S - K) if cp > 0 else max(0.0, K - S)


def fill(bid: float, ask: float, side: int, k: float) -> float:
    """Price per share paid (side=+1 buy) or received (side=-1 sell); clipped at zero."""
    return max(0.5 * (bid + ask) + side * k * (ask - bid), 0.0)


def price_entry(chain: Chain, legs: list):
    """Price an entry on the chain's date.

    -> (package, None) on success, or (None, failure) when any quote is missing or any leg fails the liquidity filter.
    failure = {"reason": "missing_quote" | "crossed" | "spread" | "zero_bid", "leg": index, "cp", "strike", "bid", "ask"}.
    package = {"cf": {k: dollars}, "units": int, "mid_value": dollars, "quotes": [(bid, ask), ...]}.
    """
    cf = {k: 0.0 for k in KS}
    quotes = []
    for i, (cp, K, ex, q) in enumerate(legs):
        bq = chain.quote(ex, cp, K)
        if bq is None:
            return None, {"reason": "missing_quote", "leg": i, "cp": cp, "strike": K, "bid": None, "ask": None}
        bid, ask = bq
        why = liq_reason(bid, ask, q < 0)
        if why is not None:
            return None, {"reason": why, "leg": i, "cp": cp, "strike": K, "bid": bid, "ask": ask}
        quotes.append((bid, ask))
        for k in KS:
            px = fill(bid, ask, +1 if q > 0 else -1, k)
            cf[k] += (-1 if q > 0 else +1) * px * MULT * abs(q)
    mid_value = sum(q * 0.5 * (b + a) * MULT for (cp, K, ex, q), (b, a) in zip(legs, quotes))
    return {"cf": cf, "units": int(sum(abs(L[3]) for L in legs)), "mid_value": mid_value, "quotes": quotes}, None


def quote_all(chain: Chain, legs: list) -> Optional[list]:
    """Quotes for every leg regardless of the liquidity filter (used for marks and the quote-quality metric)."""
    out = []
    for (cp, K, ex, q) in legs:
        bq = chain.quote(ex, cp, K)
        if bq is None:
            return None
        out.append(bq)
    return out


def mid_mark(legs: list, quotes: list) -> float:
    """Dollar value of the position at mid (positive = worth cash to the holder)."""
    return sum(q * 0.5 * (b + a) * MULT for (cp, K, ex, q), (b, a) in zip(legs, quotes))


def settle_cf(legs: list, settle_price: float) -> dict:
    """Cash flows when every leg expires at `settle_price`: intrinsic value, no closing fee. Same for every fill k."""
    v = sum(q * intrinsic(cp, K, settle_price) * MULT for (cp, K, ex, q) in legs)
    return {k: v for k in KS}


def close_cf(legs: list, quotes: list) -> dict:
    """Cash flows when closing at quotes: sell what is long (side -1), buy back what is short (side +1)."""
    cf = {k: 0.0 for k in KS}
    for (cp, K, ex, q), (bid, ask) in zip(legs, quotes):
        for k in KS:
            px = fill(bid, ask, -1 if q > 0 else +1, k)
            cf[k] += (1 if q > 0 else -1) * px * MULT * abs(q)
    return cf


def max_loss(legs: list, cf_entry: float) -> float:
    """Dollar max loss at expiry for same-expiry legs (the kinks of the payoff), given the net entry cash flow in dollars.
    Different expiries: the net debit. Unbounded: inf."""
    exps = {L[2] for L in legs}
    if len(exps) > 1:
        return max(0.0, -cf_entry)
    ks = sorted({L[1] for L in legs})
    pts = [0.0] + ks + [ks[-1] * 4.0 + 1.0, ks[-1] * 8.0 + 2.0]

    def payoff(S):
        return sum(q * intrinsic(cp, K, S) for (cp, K, ex, q) in legs) * MULT

    vals = [payoff(s) for s in pts]
    if vals[-1] < vals[-2] - 1e-9:        # net short calls: unbounded
        return float("inf")
    return max(0.0, -(cf_entry + min(vals)))


def assemble(legs: list, ent: dict, ext: dict) -> dict:
    """P&L record from entry and exit packages. ent/ext carry {"cf": {k: dollars}, "units": n}."""
    units = ent["units"] + ext["units"]
    tot = {k: ent["cf"][k] + ext["cf"][k] for k in KS}
    return {
        "units": units,
        "pnl": tot[cfg.K_BASE] - cfg.FEE_BASE * units,
        "pnl_mid": tot[cfg.K_MID],
        "pnl_nat": tot[cfg.K_NAT] - cfg.FEE_BASE * units,
        "pnl_x3": tot[cfg.K_X3] - cfg.FEE_BASE * units,
        "pnl_f65": tot[cfg.K_BASE] - cfg.FEE_STRESS * units,
        "entry_cf_mid": ent["cf"][cfg.K_MID], "entry_cf_base": ent["cf"][cfg.K_BASE], "entry_cf_nat": ent["cf"][cfg.K_NAT],
        "entry_slip": ent["cf"][cfg.K_MID] - ent["cf"][cfg.K_BASE],
        "max_loss": max_loss(legs, ent["cf"][cfg.K_BASE]) + cfg.FEE_BASE * ent["units"],
    }


def leg_label(cp: int, K: float, q: int, ex: dt.date) -> str:
    return f"{'BUY' if q > 0 else 'SELL'} {abs(q)} {K:g}{'C' if cp > 0 else 'P'} {ex.isoformat()}"
