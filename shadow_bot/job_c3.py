"""C3 SPY_HEDGE - job-side wiring: monthly candidate -> confirmation by the published VIX / VIX3M closes -> hold -> exit at
DTE <= 21, plus the cost counters and the hedge ledger. Rules live in rules_c3.py.

Candidate then confirm (spec section 5): the gate needs the entry day's VIX and VIX3M closes, which Cboe publishes after the
close. On the first trading day of a month the job prices the would-be ticket at the close snapshot and stores it as a
`candidate` WHETHER OR NOT the provisional (delayed-quote) gate passes. A later run applies the gate to the published closes of
the entry date and either promotes the candidate to a shadow position (entry date and quotes from the first day) or discards
it. Both outcomes post the one-line monthly heartbeat. A flip between the provisional and the published gate is logged.
"""
from __future__ import annotations

import datetime as dt

import config as cfg
import fills
import messages
import nyse_calendar as nyse
import rules_c3
import state as state_mod
from jobctx import Ctx, build_position, close_out, unpriced_out

SID = "C3"
P = cfg.C3


# ---------------------------------------------------------------------------------------------- daily upkeep
def process(ctx: Ctx) -> None:
    if not ctx.wants(SID) or ctx.phase != "close":
        return
    blk = ctx.block(SID)
    _exits(ctx, blk)
    resolve_candidate(ctx, blk)
    _hedge_ledger(ctx, blk)


def _exits(ctx: Ctx, blk: dict) -> None:
    d = ctx.session
    for pos in list(blk["open_positions"]):
        ex = dt.date.fromisoformat(pos["expiry"])
        if not rules_c3.exit_due(ex, d):
            continue
        chain, _why = ctx.chain_for("SPY")
        legs = state_mod.legs_from_json(pos["legs"])
        quotes = fills.quote_all(chain, legs) if chain is not None else None
        if quotes is None:                                   # the study's drift rule: retry on the next trading days
            pos["exit_attempts"] = pos.get("exit_attempts", 0) + 1
            if pos["exit_attempts"] > cfg.EXIT_DRIFT_DAYS:
                unpriced_out(ctx, SID, pos, d, f"no SPY quotes for the legs on {pos['exit_attempts']} exit attempts")
                ctx.problems.append(f"C3 {pos['id']}: exit could not be priced after {pos['exit_attempts']} attempts; logged unpriced")
            else:
                ctx.problems.append(f"C3 {pos['id']}: exit due but the SPY legs are not quoted (attempt {pos['exit_attempts']})")
            continue
        ext = rules_c3.close_at(legs, quotes, d)
        row, _rec = close_out(ctx, SID, pos, ext, d, "dte")
        proceeds = ext["cf"][cfg.K_BASE] - cfg.FEE_BASE * ext["units"]
        blk["premium_paid_since_reset"] = round(blk["premium_paid_since_reset"] - proceeds, 6)
        ctx.event(f"C3 closed {pos['id']}: base {row['pnl_base']}")
        if blk["status"] != "retired":
            ctx.emit(f"exit:{pos['id']}:dte", "exit", SID, messages.exit_c3(pos, row, blk))


# ---------------------------------------------------------------------------------------------- monthly entry day
def enter(ctx: Ctx) -> None:
    """First trading day of the month: price the would-be ticket and store it as a candidate."""
    if not ctx.wants(SID) or ctx.phase != "close":
        return
    d = ctx.session
    if not nyse.is_first_td_of_month(d):
        return
    blk = ctx.block(SID)
    diag = ctx.diag.setdefault("c3", {})
    if blk["status"] == "retired":
        diag["result"] = "blocked:retired"
        return
    if blk["started"] is None:
        blk["started"] = d.isoformat()
    pid = f"C3-{d.isoformat()}"
    cand = blk["candidate"]
    if (cand and cand["entry_date"] == d.isoformat()) or any(g["date"] == d.isoformat() for g in blk["gate_log"]) \
            or state_mod.entry_exists(blk, pid):
        diag["result"] = "already_handled"
        return

    skip, position = ctx.entry_block_reason(SID), None
    if skip is None:
        chain, skip = ctx.chain_for("SPY")
        if skip is None:
            res = rules_c3.build_ticket(chain, d)
            skip = res["skip"]
            diag["detail"] = res["detail"]
            if skip is None:
                t = res["ticket"]
                position = build_position(ctx, SID, "SPY", pid, t, chain, extra={"net_debit_base": t["net_debit_base"],
                                                                                 "strikes": t["strikes"]})
    q25 = ctx.mkt.trailing_quantile("VIX", d, P["quantile"], P["window"], P["min_obs"])
    pv, p3 = ctx.mkt.prov.get(("VIX", d)), ctx.mkt.prov.get(("VIX3M", d))
    prov_pass, prov_info = (None, None) if pv is None or p3 is None else rules_c3.gate(pv, p3, q25)
    blk["candidate"] = {
        "entry_date": d.isoformat(), "month": d.strftime("%Y-%m"), "created": ctx.now_iso, "wait_runs": 0, "skip": skip,
        "position": position, "prov": {"vix": pv, "vix3m": p3, "q25": q25, "passes": prov_pass},
    }
    diag.update(result="candidate_stored" if position else f"candidate_no_ticket:{skip}", skip=skip,
                prov_gate=prov_info)
    ctx.event(f"C3 candidate for {d}: {'ticket priced' if position else 'no ticket (' + str(skip) + ')'}; "
              f"provisional gate {prov_pass}")
    resolve_candidate(ctx, blk)           # the published closes may already be out


# ---------------------------------------------------------------------------------------------- confirmation
def resolve_candidate(ctx: Ctx, blk: dict) -> None:
    cand = blk["candidate"]
    if not cand:
        return
    d = ctx.session
    e = dt.date.fromisoformat(cand["entry_date"])
    vix, vix3m = ctx.mkt.index_val("VIX", e), ctx.mkt.index_val("VIX3M", e)       # PUBLISHED values only
    if vix is None or vix3m is None:
        if d > e:
            seen = cand.setdefault("wait_dates", [])             # one count per session date, however often that session is re-run
            if d.isoformat() not in seen:
                seen.append(d.isoformat())
            cand["wait_runs"] = len(seen)
            if cand["wait_runs"] > P["confirm_max_days"]:
                state_mod.record_skip(ctx.st, SID, e, "gate_unconfirmed")
                blk["gate_log"].append({"date": e.isoformat(), "month": cand["month"], "vix": vix, "vix3m": vix3m,
                                        "passes": None, "why": "unconfirmed"})
                ctx.emit(f"c3gate:{cand['month']}", "heartbeat", SID, messages.heartbeat_c3(
                    cand["month"], e.isoformat(), {"why": "missing_index"}, "UNCONFIRMED",
                    "The published closes did not arrive in time; no hedge this month.", blk))
                ctx.problems.append(f"C3 gate for {e} could not be confirmed: published VIX / VIX3M closes missing")
                blk["candidate"] = None
        return

    q25 = ctx.mkt.trailing_quantile("VIX", e, P["quantile"], P["window"], P["min_obs"])
    passes, ginfo = rules_c3.gate(vix, vix3m, q25)
    prov = cand.get("prov") or {}
    flipped = prov.get("passes") is not None and bool(prov["passes"]) != passes
    entry = {"date": e.isoformat(), "month": cand["month"], "vix": vix, "vix3m": vix3m, "ratio": ginfo.get("ratio"),
             "q25": q25, "passes": passes, "prov_vix": prov.get("vix"), "prov_vix3m": prov.get("vix3m"),
             "prov_passes": prov.get("passes"), "flipped": flipped, "confirmed_on": d.isoformat()}
    blk["gate_log"].append(entry)
    blk["gate_log"] = blk["gate_log"][-60:]
    ctx.diag.setdefault("c3", {})["gate_resolved"] = entry
    note = " (provisional quote said " + ("OPEN" if prov.get("passes") else "CLOSED") + ": FLIPPED)" if flipped else ""
    if flipped:
        ctx.event(f"C3 gate for {e} flipped between the provisional quote and the published close")
    month = cand["month"]
    if passes:
        pos = cand.get("position")
        if pos is None:
            skip = cand.get("skip") or "no_ticket"
            state_mod.record_skip(ctx.st, SID, e, skip)
            ctx.emit(f"c3gate:{month}", "heartbeat", SID, messages.heartbeat_c3(
                month, e.isoformat(), ginfo, "OPEN", f"No ticket: {skip}.", blk, note))
        else:
            mrs = pos.get("med_rel_spread")
            state_mod.add_quality(blk, e.isoformat(), bool(mrs is not None and mrs > cfg.QUALITY_SPREAD_REL), mrs, "ticket")
            pos["gate"] = {k: ginfo.get(k) for k in ("vix", "vix3m", "ratio", "q25", "ratio_ok", "level_ok")}
            pos["why"] = "gate open on the published closes"
            blk["open_positions"].append(pos)
            state_mod.register_entry(blk, pos["id"])
            blk["premium_paid_since_reset"] = round(
                blk["premium_paid_since_reset"] - pos["entry_cf"]["base"] + cfg.FEE_BASE * pos["entry_units"], 6)
            ctx.event(f"C3 promoted {pos['id']}")
            if blk["status"] != "retired":
                ctx.emit(f"entry:{pos['id']}", "entry", SID, messages.entry_c3(pos, blk, ctx.chains.get("SPY").meta
                                                                            if ctx.chains.get("SPY") else {}, ginfo, d, ctx.now_et))
                ctx.emit(f"c3gate:{month}", "heartbeat", SID, messages.heartbeat_c3(
                    month, e.isoformat(), ginfo, "OPEN", f"Hedge opened: {pos['id']}.", blk, note))
    else:
        ctx.emit(f"c3gate:{month}", "heartbeat", SID, messages.heartbeat_c3(
            month, e.isoformat(), ginfo, "CLOSED", "No hedge this month.", blk, note))
    blk["candidate"] = None


# ---------------------------------------------------------------------------------------------- hedge ledger
def _hedge_ledger(ctx: Ctx, blk: dict) -> None:
    """After each 10% SPY drawdown from its running peak, record whether any open or recent hedge paid, and how much."""
    d = ctx.session
    px = ctx.mkt.close("SPY", d)
    if px is None:
        return
    if blk["spy_peak"] is None:
        s = ctx.mkt.closes.get("SPY")
        lo = d - dt.timedelta(days=365)
        hist = [v for dd, v in zip(s.dates, s.values) if dd >= lo] if s else []
        blk["spy_peak"], blk["spy_peak_date"] = (max(hist), d.isoformat()) if hist else (px, d.isoformat())
    if px >= blk["spy_peak"]:
        blk["spy_peak"], blk["spy_peak_date"], blk["episode"] = px, d.isoformat(), None
        return
    dd = 1.0 - px / blk["spy_peak"]
    step = cfg.C3_REVIEW_DRAWDOWN
    level = int(dd / step + 1e-9)
    if level < 1:
        return
    ep = blk["episode"] or {"peak": blk["spy_peak"], "peak_date": blk["spy_peak_date"], "levels_hit": []}
    blk["episode"] = ep
    for k in range(1, level + 1):
        if k in ep["levels_hit"]:
            continue
        ep["levels_hit"].append(k)
        chain, _ = ctx.chain_for("SPY")
        opens, paid = [], 0.0
        for pos in blk["open_positions"]:
            legs = state_mod.legs_from_json(pos["legs"])
            qs = fills.quote_all(chain, legs) if chain is not None else None
            if qs is None:
                opens.append({"id": pos["id"], "mid_value": None, "mid_pnl": None})
                continue
            val = fills.mid_mark(legs, qs)
            pnl = val + pos["entry_cf"]["mid"]
            opens.append({"id": pos["id"], "mid_value": round(val, 2), "mid_pnl": round(pnl, 2)})
            paid += pnl
        lo = (d - dt.timedelta(days=90)).isoformat()
        recent = [{"id": r["id"], "exit_date": r["exit_date"], "pnl_base": float(r["pnl_base"])}
                  for r in ctx.log_rows if r["strategy"] == SID and r["exit_date"] >= lo and r["pnl_base"] not in ("", None)]
        paid += sum(r["pnl_base"] for r in recent)
        blk["hedge_ledger"].append({"date": d.isoformat(), "level": round(k * step, 4), "spy_close": px, "peak": blk["spy_peak"],
                                    "peak_date": blk["spy_peak_date"], "open": opens, "recent_closed": recent,
                                    "paid_total": round(paid, 2)})
        blk["hedge_ledger"] = blk["hedge_ledger"][-40:]
        ctx.event(f"C3 hedge ledger: SPY {dd:.1%} below its peak; hedges paid {paid:+.2f}")
