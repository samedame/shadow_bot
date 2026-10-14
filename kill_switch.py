"""Kill-switch adapted to shadow mode (spec section 2.8). ALL thresholds are proposals from the holdout tables, not backtested.

A shadow bot holds no money, and stopping the log would destroy the data. So a trip:
  * sets the strategy's status to "flagged" (the dossiers' "paused"),
  * posts ONE message per reason (re-posted only if the reason clears and trips again),
  * KEEPS LOGGING.
Only the user moves a strategy to "retired" (the dossiers' "killed"), by editing shadow_state.json; "flagged" is sticky until
the user sets it back to "active".

Checks (per strategy):
  edge      primed (>= 20 closed shadow trades) and trailing-20 mean of base-fill P&L below 25% of the backtest edge.
            It WILL trip by chance (the standard error of a 20-trade mean is large); treat it as a prompt, not a verdict.
  drawdown  cumulative base-fill P&L (one contract, by exit date) down from its peak by more than the stop. Always evaluated.
            C3's equivalent is the cumulative net premium paid since reset against the budget the user sets (None = not set).
  quality   more than 20% of the last 20 signals had a median leg spread above 30% of mid (replaces the fill-quality check:
            no real fills exist in shadow).
Not proposed: the single-trade 3x-credit check (max loss is 8.1x / 4.3x the credit, so the drawdown stop governs).
"""
from __future__ import annotations

import math

import config as cfg


def active_reasons(sid: str, block: dict) -> dict:
    """{reason_key: human text} for every check currently tripped."""
    out: dict = {}
    bt = cfg.BACKTEST[sid]
    # edge (C9, C6)
    if sid in ("C9", "C6") and block["primed"] and len(block["recent_pnl_base"]) >= cfg.TRAILING_WINDOW:
        avg = math.fsum(block["recent_pnl_base"]) / len(block["recent_pnl_base"])      # fsum: same answer on every Python version
        floor = cfg.EDGE_FLOOR_FRACTION * bt["edge_base"]
        if avg < floor:
            out["edge"] = (f"trailing-{len(block['recent_pnl_base'])} mean ${avg:+.2f}/trade at base fills is below ${floor:.2f} "
                           f"({cfg.EDGE_FLOOR_FRACTION:.0%} of the backtest edge ${bt['edge_base']:.2f}). "
                           f"Chance trips are expected at n=20: treat as a prompt.")
    # drawdown / premium
    if sid in ("C9", "C6"):
        if block["drawdown"] > bt["dd_stop"]:
            out["drawdown"] = (f"drawdown ${block['drawdown']:,.0f} from the running peak exceeds the ${bt['dd_stop']:,.0f} stop "
                               f"(1.5x the worst holdout drawdown ${bt['worst_dd']:,.0f}).")
    elif sid == "C3":
        budget = cfg.C3_HEDGE_BUDGET
        if budget is not None and block.get("premium_paid_since_reset", 0.0) > budget:
            out["premium"] = (f"cumulative net premium paid since reset ${block['premium_paid_since_reset']:,.0f} exceeds the "
                              f"hedge budget ${budget:,.0f}.")
    # quote quality
    flagged = sum(1 for q in block["quality"][-cfg.QUALITY_WINDOW:] if q["flag"])
    if flagged > cfg.QUALITY_SHARE * cfg.QUALITY_WINDOW:
        out["quality"] = (f"{flagged} of the last {len(block['quality'][-cfg.QUALITY_WINDOW:])} signals had a median leg spread above "
                          f"{cfg.QUALITY_SPREAD_REL:.0%} of mid (or no liquid short strike): quotes are wider than the backtest assumed.")
    return out


def evaluate(sid: str, block: dict, now_iso: str) -> dict:
    """Update the block's status and alert ledger. -> {"new": {reason: text}, "active": {reason: text}, "status": str}.

    Retired strategies are left alone. Reasons that cleared are removed from the ledger, so a later re-trip alerts again."""
    if block["status"] == "retired":
        return {"new": {}, "active": {}, "status": "retired"}
    active = active_reasons(sid, block)
    ledger = block["kill_alerts"]
    for k in [k for k in ledger if k not in active]:
        del ledger[k]
    new = {k: t for k, t in active.items() if k not in ledger}
    for k in new:
        ledger[k] = now_iso
    if new and block["status"] == "active":
        block["status"] = "flagged"
    return {"new": new, "active": active, "status": block["status"]}


def summary_line(sid: str, block: dict) -> str:
    bt = cfg.BACKTEST[sid]
    n = len(block["recent_pnl_base"])
    avg = (math.fsum(block["recent_pnl_base"]) / n) if n else None
    parts = [f"status {block['status']}", f"n={block['closed_n']}", "primed" if block["primed"] else f"not primed ({block['closed_n']}/{cfg.PRIMED_MIN_TRADES})"]
    if avg is not None:
        parts.append(f"trailing-{n} mean ${avg:+.2f}")
    if sid in ("C9", "C6"):
        parts.append(f"drawdown ${block['drawdown']:,.0f} / stop ${bt['dd_stop']:,.0f}")
    elif sid == "C3":
        budget = cfg.C3_HEDGE_BUDGET
        parts.append(f"premium since reset ${block.get('premium_paid_since_reset', 0.0):,.0f} / budget "
                     f"{'unset' if budget is None else f'${budget:,.0f}'}")
    return " | ".join(parts)
