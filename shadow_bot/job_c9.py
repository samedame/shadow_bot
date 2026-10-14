"""C9 CONDOR_1D - the job-side wiring: settle yesterday's condor, open tonight's. Rules live in rules_c9.py."""
from __future__ import annotations

import datetime as dt

import config as cfg
import messages
import nyse_calendar as nyse
import rules_c9
import state as state_mod
from jobctx import Ctx, build_position, close_out

SID = "C9"


def settle(ctx: Ctx) -> None:
    """Every open condor whose expiry has arrived is settled at intrinsic on that day's official SPY close. A condor whose
    close is not available yet simply stays open (problem noted) and is settled by a later run, still at the expiry-date close."""
    if not ctx.wants(SID) or ctx.phase != "close":
        return
    blk = ctx.block(SID)
    for pos in list(blk["open_positions"]):
        ex = dt.date.fromisoformat(pos["expiry"])
        if ex > ctx.session:
            continue
        px = ctx.mkt.close("SPY", ex)
        if px is None:
            ctx.problems.append(f"C9 {pos['id']}: SPY close for {ex} is not available yet; settlement waits")
            continue
        legs = state_mod.legs_from_json(pos["legs"])
        ext = rules_c9.settle(legs, px, ex)
        late = "" if ex == ctx.session else f"settled late by run of {ctx.session}"
        row, _ = close_out(ctx, SID, pos, ext, ex, "expiry", notes=(f"SPY close {px:.2f}; " + late).strip("; "))
        ctx.event(f"C9 settled {pos['id']} at SPY {px:.2f}: base {row['pnl_base']}")
        if blk["status"] != "retired":
            ctx.emit(f"exit:{pos['id']}:expiry", "exit", SID, messages.exit_c9(pos, row, px, blk))


def enter(ctx: Ctx) -> None:
    """Open tonight's shadow condor when the range-bound gate is open and the chain allows it."""
    if not ctx.wants(SID):
        return
    d = ctx.session
    blk = ctx.block(SID)
    diag = ctx.diag.setdefault("c9", {})
    why = ctx.entry_block_reason(SID)
    if why:
        diag["result"] = f"blocked:{why}"
        if why in ("early_close", "run_late") and ctx.phase == "close":
            state_mod.record_skip(ctx.st, SID, d, why)
        return
    if blk["started"] is None:
        blk["started"] = d.isoformat()
    pid = f"C9-{d.isoformat()}"
    if state_mod.entry_exists(blk, pid):
        diag["result"] = "already_entered"
        return

    gate, ginfo = ctx.mkt.condor_gate(d)
    galt, ainfo = ctx.mkt.condor_gate_alt(d)
    diag.update(gate=gate, gate_info=ginfo, gate_alt=galt, gate_alt_info=ainfo)
    if gate is None:
        missing = ctx.mkt.missing_spy_closes_for_gate(d)
        diag["missing_closes"] = [m.isoformat() for m in missing][:10]
        ctx.problems.append("C9 gate cannot be computed: missing SPY closes " + ", ".join(m.isoformat() for m in missing[:5]))
        state_mod.record_skip(ctx.st, SID, d, "no_gate_data")
        diag["result"] = "skip:no_gate_data"
        return
    if not gate:
        state_mod.record_skip(ctx.st, SID, d, "gate_closed")
        diag["result"] = "skip:gate_closed"
        return

    chain, reason = ctx.chain_for("SPY")
    if reason:
        state_mod.record_skip(ctx.st, SID, d, reason)
        diag["result"] = f"skip:{reason}"
        return
    try:
        d1 = nyse.next_td(d)
    except nyse.CalendarRange:
        d1 = None

    res = rules_c9.evaluate(chain, d, d1, gate, alt_ctx={"r": ctx.mkt.rate(d)})
    det = res["detail"]
    diag["detail"] = {k: v for k, v in det.items() if k != "fail"} | ({"fail": det["fail"]} if "fail" in det else {})
    if res["skip"]:
        state_mod.record_skip(ctx.st, SID, d, res["skip"])
        diag["result"] = f"skip:{res['skip']}"
        if res["skip"] in ("untradeable", "no_short_strike"):
            state_mod.add_quality(blk, d.isoformat(), True, det.get("med_rel_spread"), res["skip"])
        return

    t = res["ticket"]
    mrs = t["med_rel_spread"]
    state_mod.add_quality(blk, d.isoformat(), bool(mrs is not None and mrs > cfg.QUALITY_SPREAD_REL), mrs, "ticket")
    gate_keep = {k: ginfo[k] for k in ("prev", "rv20", "median", "window") if k in ginfo}
    pos = build_position(ctx, SID, "SPY", pid, t, chain, extra={
        "gate": gate_keep, "gate_alt": galt, "alt": t["alt"], "strikes": t["strikes"],
        "why": "RV20 below its 20-day median; next-trading-day expiry"})
    blk["open_positions"].append(pos)
    state_mod.register_entry(blk, pid)
    diag["result"] = "entered"
    diag["legs"] = [{"label": l["label"], "bid": l["bid"], "ask": l["ask"], "delta": l["delta"], "delta_src": l["delta_src"]}
                    for l in pos["legs"]]
    diag["alt_differs"] = None if t["alt"] is None else t["alt"]["differs"]
    ctx.event(f"C9 entered {pid}: {pos['legs'][0]['label']} ...")
    ctx.emit(f"entry:{pid}", "entry", SID, messages.entry_c9(pos, blk, chain.meta, ctx.now_et, ctx.stale.get("SPY")))
