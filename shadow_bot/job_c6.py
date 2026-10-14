"""C6 NONEQ_VRP - job-side wiring. Two parallel versions of every weekly entry:

  REAL   the spread on the listed GLD / TLT chain, quoted bid / ask, exit checked on every daily run.
  MODEL  the backtest's own Black-Scholes pricing (GVZ / VXTLT flat vol, T-bill rate), recomputed statelessly from official closes
         and PUBLISHED index values (so it lags the real path by up to a day). It posts no entry message; the real entry message
         carries its provisional credit, and a compact message goes out when it closes.

Comparing them is the point: real credit / model credit at entry and real mark / model mark at exit (state `pairs`).
Rules live in rules_c6.py.
"""
from __future__ import annotations

import datetime as dt

import config as cfg
import fills
import messages
import nyse_calendar as nyse
import rules_c6
import state as state_mod
from jobctx import Ctx, build_position, close_out, unpriced_out

SID = "C6"
TICKERS = tuple(cfg.C6["vol_index"])


def _sleeve_active(blk: dict, tk: str) -> bool:
    return blk["sleeves"].get(tk, {}).get("status", "active") != "retired"


def _pair(blk: dict, pair_id: str) -> dict | None:
    for p in blk["pairs"]:
        if p["pair_id"] == pair_id:
            return p
    return None


# ---------------------------------------------------------------------------------------------- daily upkeep
def process(ctx: Ctx) -> None:
    if not ctx.wants(SID) or ctx.phase != "close":
        return
    blk = ctx.block(SID)
    _real_exits(ctx, blk)
    _model_update(ctx, blk)
    _fill_exit_marks(ctx, blk)


def _real_exits(ctx: Ctx, blk: dict) -> None:
    d = ctx.session
    for pos in list(blk["open_positions"]):
        tk = pos["sleeve"]
        ex = dt.date.fromisoformat(pos["expiry"])
        chain, _why = ctx.chain_for(tk)
        legs = state_mod.legs_from_json(pos["legs"])
        quotes = fills.quote_all(chain, legs) if chain is not None else None
        dec = rules_c6.real_exit_decision(legs, quotes, pos["entry_cf"]["base"], ex, d)
        if dec["action"] == "hold":
            pos["last_mark"] = {"date": d.isoformat(), "cost_to_close": dec["cost_to_close"]}
            continue
        if dec["action"] == "due_no_quotes":
            pos["exit_attempts"] = pos.get("exit_attempts", 0) + 1
            if pos["exit_attempts"] > cfg.EXIT_DRIFT_DAYS:
                unpriced_out(ctx, SID, pos, d, f"no {tk} quotes for the legs on {pos['exit_attempts']} exit attempts")
                ctx.problems.append(f"C6 {pos['id']}: exit could not be priced; logged unpriced")
            else:
                ctx.problems.append(f"C6 {pos['id']}: exit due but the {tk} legs are not quoted (attempt {pos['exit_attempts']})")
            continue
        ext = {"cf": fills.close_cf(legs, quotes), "units": int(sum(abs(L[3]) for L in legs))}
        row, _rec = close_out(ctx, SID, pos, ext, d, dec["reason"], count_sleeve=True,
                              notes=f"mid-mark cost to close ${dec['cost_to_close']:.2f}")
        pair = _pair(blk, pos.get("pair_id", ""))
        if pair is not None:
            pair["real_exit_mark"] = dec["cost_to_close"]
            pair["real_exit_date"] = d.isoformat()
        why = (f"profit target: mid-mark cost to close {usd(dec['cost_to_close'])} <= 50% of the base entry credit "
               f"{usd(pos['entry_cf']['base'])}" if dec["reason"] == "profit"
               else f"DTE {row['dte_out']} <= {cfg.C6['exit_dte']}: closed at that day's quotes")
        ctx.event(f"C6 closed {pos['id']} ({dec['reason']}): base {row['pnl_base']}")
        if blk["status"] != "retired":
            ctx.emit(f"exit:{pos['id']}:{dec['reason']}", "exit", SID, messages.exit_c6_real(pos, row, blk, why))


def usd(x: float) -> str:
    return messages.usd(x)


# ---------------------------------------------------------------------------------------------- model path
def _last_complete(ctx: Ctx, tk: str) -> dt.date | None:
    idx = cfg.C6["vol_index"][tk]
    c = ctx.mkt.closes.get(tk)
    i = ctx.mkt.indices.get(idx)
    a, b = (c.last_date() if c else None), (i.last_date() if i else None)
    return None if a is None or b is None else min(a, b)


def _model_update(ctx: Ctx, blk: dict) -> None:
    m = blk["model"]
    for rec in list(m["open"]):
        tk = rec["tk"]
        e = dt.date.fromisoformat(rec["entry_date"])
        lc = _last_complete(ctx, tk)
        if lc is None:
            continue
        res = rules_c6.model_trade(ctx.mkt, tk, e, lc, holes="wait", hole_grace=5)
        if res.get("entry") and not res["entry"].get("skip"):
            _sync_pair(blk, rec, res["entry"])
        status = res["status"]
        if status == "pending":
            continue
        if status == "skip":
            state_mod.record_skip(ctx.st, SID, e, "model_" + str(res["skip"]), sleeve=tk)
            m["open"] = [r for r in m["open"] if r["id"] != rec["id"]]
            continue
        if status == "open":
            rec["status"] = "open"
            continue
        # closed
        row = _model_row(rec, res)
        ctx.append_log(row)
        pnl = res["pnl"]
        state_mod.update_model_stats(blk, pnl["pnl"], pnl["pnl_nat"], tk)
        m["open"] = [r for r in m["open"] if r["id"] != rec["id"]]
        ctx.event(f"C6 model {rec['id']} closed ({res['exit']['reason']}): base {row['pnl_base']}")
        if blk["status"] != "retired":
            ctx.emit(f"exit:{rec['id']}:{res['exit']['reason']}", "exit", SID, messages.exit_c6_model(row, blk))


def _sync_pair(blk: dict, rec: dict, ent: dict) -> None:
    pair = _pair(blk, rec.get("pair_id", ""))
    if pair is None:
        return
    cf = ent["ent"]["cf"]
    pair["model_credit_base"] = cf[cfg.K_BASE]
    pair["model_credit_mid"] = cf[cfg.K_MID]
    pair["model_Ks"], pair["model_Kl"] = ent["Ks"], ent["Kl"]
    pair["model_expiry"] = ent["expiry"].isoformat()


def _model_row(rec: dict, res: dict) -> dict:
    ent, ex, pnl = res["entry"], res["exit"], res["pnl"]
    cfe, cfx = ent["ent"]["cf"], ex["cf"]
    legs = res["legs"]
    expiry = ent["expiry"]
    d_exit = ex["date"]
    row = {k: "" for k in state_mod.LOG_COLUMNS}
    row.update(
        id=rec["id"], strategy=SID, sleeve=rec["tk"], version="model", pair_id=rec.get("pair_id") or "",
        entry_date=rec["entry_date"], exit_date=d_exit.isoformat(), exit_reason=ex["reason"], expiry=expiry.isoformat(),
        dte_in=cfg.C6["dte"], dte_out=(expiry - d_exit).days, spot_in=ent["S0"],
        legs=" ; ".join(fills.leg_label(cp, K, q, e) for (cp, K, e, q) in legs),
        credit_mid=round(cfe[cfg.K_MID], 4), credit_base=round(cfe[cfg.K_BASE], 4), credit_nat=round(cfe[cfg.K_NAT], 4),
        entry_fees=round(cfg.FEE_BASE * ent["ent"]["units"], 4),
        exit_cf_mid=round(cfx[cfg.K_MID], 4), exit_cf_base=round(cfx[cfg.K_BASE], 4), exit_cf_nat=round(cfx[cfg.K_NAT], 4),
        exit_fees=round(cfg.FEE_BASE * 2, 4), units=pnl["units"], max_loss=round(pnl["max_loss"], 4),
        pnl_mid=round(pnl["pnl_mid"], 4), pnl_base=round(pnl["pnl"], 4), pnl_nat=round(pnl["pnl_nat"], 4),
        pnl_x3=round(pnl["pnl_x3"], 4), pnl_f65=round(pnl["pnl_f65"], 4), stale="no",
        notes=f"iv {ent['iv_pct']:.2f} r {ent['r']:.4f} S0 {ent['S0']:.2f} Ks {ent['Ks']:g} Kl {ent['Kl']:g}")
    return row


def _fill_exit_marks(ctx: Ctx, blk: dict) -> None:
    """Model-side mark on the date of each real exit, once the published index value for that date exists."""
    idx_of = cfg.C6["vol_index"]
    for pair in blk["pairs"]:
        if pair.get("real_exit_mark") is None or pair.get("model_exit_mark") is not None or "model_Ks" not in pair:
            continue
        t = dt.date.fromisoformat(pair["real_exit_date"])
        tk = pair["tk"]
        St, it = ctx.mkt.close(tk, t), ctx.mkt.index_val(idx_of[tk], t)
        if St is None or it is None:
            continue
        dte = (dt.date.fromisoformat(pair["model_expiry"]) - t).days
        pair["model_exit_mark"] = rules_c6.model_mark(pair["model_Ks"], pair["model_Kl"], St, it, ctx.mkt.rate(t), dte)


# ---------------------------------------------------------------------------------------------- weekly entries
def enter(ctx: Ctx) -> None:
    if not ctx.wants(SID) or ctx.phase != "close":
        return
    blk = ctx.block(SID)
    if blk["status"] == "retired":
        return
    d = ctx.session
    diag = ctx.diag.setdefault("c6", {})
    if blk["started"] is None:
        blk["started"] = d.isoformat()
    if nyse.is_first_td_of_week(d):
        for tk in TICKERS:
            if _sleeve_active(blk, tk):
                _enter_sleeve(ctx, blk, tk, d, diag.setdefault(tk, {}))
    _backfill_model(ctx, blk)


def _model_record(blk: dict, tk: str, d: dt.date, pair_id: str | None, prov_iv: float | None) -> bool:
    mid = f"C6M-{tk}-{d.isoformat()}"
    if mid in blk["entries_done"] or any(r["id"] == mid for r in blk["model"]["open"]):
        return False
    blk["model"]["open"].append({"id": mid, "tk": tk, "entry_date": d.isoformat(), "pair_id": pair_id, "prov_iv": prov_iv,
                                 "status": "pending"})
    state_mod.register_entry(blk, mid)
    return True


def _enter_sleeve(ctx: Ctx, blk: dict, tk: str, d: dt.date, diag: dict) -> None:
    idx = cfg.C6["vol_index"][tk]
    pid = f"C6-{tk}-{d.isoformat()}"
    prov_iv = ctx.mkt.prov.get((idx, d))
    pair = _pair(blk, pid)
    if pair is None:
        pair = {"pair_id": pid, "tk": tk, "entry_date": d.isoformat(), "real_id": None, "model_id": f"C6M-{tk}-{d.isoformat()}",
                "real_credit_base": None, "model_credit_base_prov": None, "model_credit_base": None,
                "real_exit_mark": None, "model_exit_mark": None}
        blk["pairs"].append(pair)
        blk["pairs"] = blk["pairs"][-300:]
    _model_record(blk, tk, d, pid, prov_iv)             # the model path does not depend on the snapshot's quality

    chain, skip = ctx.chain_for(tk)
    why = ctx.entry_block_reason(SID)
    S0 = ctx.mkt.close(tk, d) or (chain.spot if chain else None)
    prov = None
    if prov_iv and S0:
        ment = rules_c6.model_entry(S0, prov_iv, ctx.mkt.rate(d), d)
        if not ment["skip"]:
            prov = {"credit_base": ment["ent"]["cf"][cfg.K_BASE], "index": idx, "iv_pct": prov_iv}
            pair["model_credit_base_prov"] = prov["credit_base"]
    diag["provisional_model"] = prov

    if state_mod.entry_exists(blk, pid):
        diag["result"] = "already_entered"
        return
    skip = why or skip
    if skip:
        state_mod.record_skip(ctx.st, SID, d, skip, sleeve=tk)
        diag["result"] = f"skip:{skip}"
        return
    res = rules_c6.real_entry(chain, d, chain.spot)
    det = res["detail"]
    diag["detail"] = det
    if res["skip"]:
        state_mod.record_skip(ctx.st, SID, d, res["skip"], sleeve=tk)
        diag["result"] = f"skip:{res['skip']}"
        if res["skip"] in ("untradeable", "no_short_strike"):
            state_mod.add_quality(blk, d.isoformat(), True, det.get("med_rel_spread"), f"{tk}:{res['skip']}")
        return
    t = res["ticket"]
    mrs = t["med_rel_spread"]
    state_mod.add_quality(blk, d.isoformat(), bool(mrs is not None and mrs > cfg.QUALITY_SPREAD_REL), mrs, f"{tk}:ticket")
    real_base = t["pkg"]["cf"][cfg.K_BASE]
    model_base = None if prov is None else prov["credit_base"]
    ratio = None if not model_base or model_base <= 0 else real_base / model_base
    pos = build_position(ctx, SID, tk, pid, t, chain, extra={
        "pair_id": pid, "model_credit_base": model_base, "real_over_model": None if ratio is None else round(ratio, 4),
        "strikes": t["strikes"], "width": t["width"]})
    blk["open_positions"].append(pos)
    state_mod.register_entry(blk, pid)
    pair["real_id"] = pid
    pair["real_credit_base"] = real_base
    diag["result"] = "entered"
    ctx.event(f"C6 entered {pid}")
    ctx.emit(f"entry:{pid}", "entry", SID, messages.entry_c6(pos, blk, chain.meta, ctx.now_et, prov, ctx.stale.get(tk)))


def _backfill_model(ctx: Ctx, blk: dict) -> None:
    """A weekly entry day that was missed (the job did not run) still gets its MODEL record, so the model path stays an exact
    forward replication of the backtest. The real path cannot be backfilled: it needs that day's quotes."""
    if not blk["started"]:
        return
    d = ctx.session
    start = dt.date.fromisoformat(blk["started"])
    for k in range(1, 13):
        t = d - dt.timedelta(days=k)
        if t < start or not nyse.is_trading_day(t) or not nyse.is_first_td_of_week(t):
            continue
        for tk in TICKERS:
            if _sleeve_active(blk, tk) and _model_record(blk, tk, t, None, None):
                ctx.event(f"C6 model backfilled for the missed entry day {t} ({tk})")
