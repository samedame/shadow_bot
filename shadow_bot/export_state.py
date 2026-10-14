"""Shadow-only state export (shadow_state_export.json), in the field set the live book's cross-bot risk export uses:
strategy id, open positions with legs and max loss, closed history with dates / net P&L / fill versus mid, cumulative P&L,
running peak and drawdown, trades since reset, status.

DO NOT wire this into the live combined-drawdown monitor. Shadow positions hold no money; mixing them into the live book's
drawdown would corrupt it. The file is named and flagged so it cannot be picked up by accident: it has no top-level `history`
key (the live monitor's options_state.json shape), and every record carries "shadow": true. tests/test_export.py pins both.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys

import config as cfg
import state as state_mod

NAMES = {"C9": "CONDOR_1D", "C6": "NONEQ_VRP", "C3": "SPY_HEDGE"}


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def build(st: dict, log_rows: list, generated_at: str) -> dict:
    out = {
        "schema": cfg.SCHEMA,
        "shadow": True,
        "banner": cfg.BANNER,
        "note": "Shadow-only export. Positions hold no money. Not for the live combined-drawdown monitor.",
        "generated_at": generated_at,
        "strategies": {},
    }
    for sid in cfg.STRATEGIES:
        blk = st["strategies"][sid]
        closed = []
        for r in log_rows:
            if r["strategy"] != sid or r.get("version") == "model":
                continue                                    # the REAL version is the one that mirrors a tradable position
            pb, pm = _f(r["pnl_base"]), _f(r["pnl_mid"])
            closed.append({
                "id": r["id"], "sleeve": r["sleeve"], "entry_date": r["entry_date"], "exit_date": r["exit_date"],
                "exit_reason": r["exit_reason"], "net_pnl_base": pb, "net_pnl_natural": _f(r["pnl_nat"]),
                "net_pnl_mid": pm, "fill_vs_mid": None if (pb is None or pm is None) else round(pb - pm, 6),
                "shadow": True,
            })
        opens = [{
            "id": p["id"], "sleeve": p["sleeve"], "version": p.get("version", "real"), "entry_date": p["entry_date"],
            "expiry": p["expiry"], "legs": [l["label"] for l in p["legs"]], "max_loss": p.get("max_loss"), "shadow": True,
        } for p in blk["open_positions"]]
        rec = {
            "strategy_id": sid, "name": NAMES[sid], "status": blk["status"], "primed": blk["primed"],
            "open_positions": opens, "closed_history": closed,
            "cumulative_pnl_base": blk["cum_pnl_base"], "cumulative_pnl_natural": blk["cum_pnl_nat"],
            "running_peak": blk["peak"], "drawdown": blk["drawdown"], "max_drawdown": blk["max_drawdown"],
            "trades_since_reset": blk["closed_n"], "shadow": True,
        }
        if sid == "C3":
            rec["premium_paid_since_reset"] = blk.get("premium_paid_since_reset", 0.0)
            rec["premium_budget"] = cfg.C3_HEDGE_BUDGET
        out["strategies"][sid] = rec
    return out


def export(state_path: str = cfg.STATE_FILE, log_path: str = cfg.LOG_FILE, out_path: str = cfg.EXPORT_FILE) -> dict:
    st = state_mod.load(state_path)
    snap = build(st, state_mod.read_log(log_path), dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))
    tmp = out_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(snap, f, indent=2, default=str)
        f.write("\n")
    os.replace(tmp, out_path)
    print(f"Exported shadow state to {out_path}")
    return snap


if __name__ == "__main__":
    export(*(sys.argv[1:4]))
