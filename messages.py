"""Message text for the shadow bots. Pure formatting: every function takes plain data and returns
discord_shadow.make_message(...) -> {"embed": ..., "plain": ...}. No I/O, no clock, no state changes.

One compact message per new shadow position and per exit, a weekly summary, a one-line monthly C3 gate heartbeat, kill-switch
flags and data notices. Credits and debits are shown PER SHARE (x100 = one contract) as in the spec's sample; max loss and P&L are
dollars for one contract. No message ever says "buy" or "sell" as an instruction: tickets are labelled "Would-be ticket".
"""
from __future__ import annotations

import datetime as dt
import statistics
from typing import Optional
from zoneinfo import ZoneInfo

import config as cfg
import state as state_mod
from discord_shadow import make_message

NAMES = {"C9": "CONDOR_1D", "C6": "NONEQ_VRP", "C3": "SPY_HEDGE"}
ET = ZoneInfo("America/New_York")


# ---------------------------------------------------------------------------------------------- number formats
def usd(x: Optional[float], signed: bool = False, dec: int = 2) -> str:
    if x is None:
        return "n/a"
    s = f"{abs(x):,.{dec}f}"
    return f"-${s}" if x < 0 else (f"+${s}" if signed else f"${s}")


def ps(dollars: Optional[float], signed: bool = False) -> str:
    """Dollars for one contract -> per-share text."""
    return "n/a" if dollars is None else usd(dollars / cfg.MULT, signed)


def pct(x: Optional[float], dec: int = 2) -> str:
    return "n/a" if x is None else f"{x * 100:.{dec}f}%"


def hm(iso: Optional[str]) -> str:
    return "n/a" if not iso else iso[11:16]


def run_ts(now_et: dt.datetime) -> str:
    return f"{now_et:%Y-%m-%d %H:%M} ET"


def legs_line(legs: list) -> str:
    """'SELL 1 783C / BUY 1 790C / SELL 1 773P / BUY 1 766P' from position-JSON legs."""
    return " / ".join(f"{'SELL' if l['qty'] < 0 else 'BUY'} {abs(l['qty'])} {l['strike']:g}{'C' if l['cp'] > 0 else 'P'}"
                      for l in legs)


def option_quote_hm(meta: dict) -> str:
    """HH:MM ET the option bid/ask in the feed file are from: the file's generation time minus the feed's fixed delay
    (cfg.CBOE_FEED_DELAY_MIN). 'n/a' when the file carried no timestamp."""
    ts = meta.get("file_ts_utc")
    if not ts:
        return "n/a"
    t = dt.datetime.fromisoformat(ts) - dt.timedelta(minutes=cfg.CBOE_FEED_DELAY_MIN)
    return t.astimezone(ET).strftime("%H:%M")


def quote_note(meta: Optional[dict], stale: Optional[str]) -> str:
    """Provenance of the prices on an entry message. Two clocks, because they differ: the underlying's last trade (the closing
    print, 16:00 for SPY) and the option quotes (the feed's snapshot, a few minutes later at a 16:20 run)."""
    if not meta:
        return "quotes: n/a"
    t = hm(meta.get("und_last_trade_et"))
    age = meta.get("file_age_min")
    age_s = "" if age is None else f", file {age:.0f} min old"
    return (f"underlying last trade {t}, option quotes ~{option_quote_hm(meta)}{age_s}, "
            f"stale: {'no' if not stale else 'YES (' + stale + ')'}")


def status_tag(block: dict) -> str:
    return "" if block.get("status", "active") == "active" else f" [{block['status'].upper()}]"


def mean_of(block: dict, key: str) -> Optional[float]:
    n = block.get("closed_n", 0)
    return None if not n else block[key] / n


def record_line(sid: str, block: dict) -> str:
    n = block.get("closed_n", 0)
    bt = cfg.BACKTEST[sid]
    if n == 0:
        rec = f"n=0 closed (primed at {cfg.PRIMED_MIN_TRADES})"
    else:
        rec = (f"n={n} | mean $/trade base {usd(mean_of(block, 'cum_pnl_base'), True)} "
               f"natural {usd(mean_of(block, 'cum_pnl_nat'), True)}")
    if sid == "C9":
        ref = (f"backtest {usd(bt['edge_base'], True)} / {usd(bt['edge_nat'], True)} "
               f"({usd(bt['edge_base_official'], True)} / {usd(bt['edge_nat_official'], True)} at official-close settlement)")
    elif sid == "C6":
        ref = f"backtest {usd(bt['edge_base'], True)} / {usd(bt['edge_nat'], True)} (model-priced)"
    else:
        ref = "no edge claimed: a cost ledger (backtest hold-out: 13 trades)"
    return f"{rec} | {ref}"


# ---------------------------------------------------------------------------------------------- C9
def entry_c9(pos: dict, block: dict, meta: dict, now_et: dt.datetime, stale: Optional[str] = None) -> dict:
    cf = pos["entry_cf"]
    g = pos.get("gate") or {}
    why = (f"RV20 {pct(g.get('rv20'))} < median {pct(g.get('median'))} (range-bound); "
           f"expiry next trading day ({pos['expiry']})")
    legs = pos["legs"]
    sc = next(l for l in legs if l["cp"] > 0 and l["qty"] < 0)
    sp = next(l for l in legs if l["cp"] < 0 and l["qty"] < 0)
    body = [
        f"Would-be ticket : {legs_line(legs)}   exp {pos['expiry']}",
        f"Credit          : mid {ps(cf['mid'])} | base fill (25%) {ps(cf['base'])} | natural {ps(cf['nat'])}"
        f"   Max loss {usd(pos['max_loss'], dec=0)}",
        f"Underlying      : SPY {pos['spot_in']:.2f} ({meta.get('spot_src', 'n/a')}) | shorts {sc['strike']:g}C delta "
        f"{_fmt_delta(sc.get('delta'))}, {sp['strike']:g}P delta {_fmt_delta(sp.get('delta'))} ({sc.get('delta_src', 'n/a')})",
        f"Why             : {why}",
    ]
    alt = pos.get("alt")
    if alt and alt.get("differs"):
        body.append(f"Selection check : Black-Scholes deltas on feed IV would pick {alt['call']:g}C / {alt['put']:g}P "
                    f"(feed delta picked {alt['feed_call']:g}C / {alt['feed_put']:g}P)")
    body.append(f"Shadow record   : {record_line('C9', block)}")
    head = f"C9 CONDOR_1D{status_tag(block)} | {run_ts(now_et)}  ({quote_note(meta, stale)})"
    return make_message(head, body, "entry")


def _fmt_delta(d: Optional[float]) -> str:
    return "n/a" if d is None else f"{d:+.2f}"


def exit_c9(pos: dict, row: dict, px: float, block: dict) -> dict:
    legs = pos["legs"]
    kc = next(l["strike"] for l in legs if l["cp"] > 0 and l["qty"] < 0)
    kp = next(l["strike"] for l in legs if l["cp"] < 0 and l["qty"] < 0)
    wc = next(l["strike"] for l in legs if l["cp"] > 0 and l["qty"] > 0)
    wp = next(l["strike"] for l in legs if l["cp"] < 0 and l["qty"] > 0)
    if px > wc:
        how = f"above the call wing {wc:g}: maximum loss region"
    elif px < wp:
        how = f"below the put wing {wp:g}: maximum loss region"
    elif px > kc:
        how = f"call side breached ({px - kc:.2f} through {kc:g})"
    elif px < kp:
        how = f"put side breached ({kp - px:.2f} through {kp:g})"
    else:
        how = "inside both short strikes: full credit kept"
    body = [
        f"Position : {legs_line(legs)}   exp {pos['expiry']} (entered {pos['entry_date']})",
        f"Settle   : SPY official close {px:.2f} -> {how}",
        f"P&L (1 contract): mid {usd(_f(row['pnl_mid']), True)} | base {usd(_f(row['pnl_base']), True)} | "
        f"natural {usd(_f(row['pnl_nat']), True)}   (entry fees {usd(_f(row['entry_fees']))}, no closing fee at expiry)",
        f"Record   : {record_line('C9', block)} | cumulative base {usd(block['cum_pnl_base'], True)} | "
        f"drawdown {usd(block['drawdown'], dec=0)}",
    ]
    return make_message(f"C9 CONDOR_1D{status_tag(block)} | settled {row['exit_date']}", body,
                        "win" if (_f(row["pnl_base"]) or 0) >= 0 else "loss")


def _f(x) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------------------------- C6
def entry_c6(pos: dict, block: dict, meta: dict, now_et: dt.datetime, prov: Optional[dict], stale: Optional[str] = None) -> dict:
    tk = pos["sleeve"]
    cf = pos["entry_cf"]
    legs = pos["legs"]
    sh = next(l for l in legs if l["qty"] < 0)
    model_line = "n/a (no provisional index quote)"
    if prov and prov.get("credit_base") is not None:
        ratio = pos.get("real_over_model")
        model_line = (f"base {ps(prov['credit_base'])} per share, provisional ({prov['index']} quote {prov['iv_pct']:.2f}; "
                      f"the published close replaces it)")
        if ratio is not None:
            model_line += f" | real / model {ratio:.2f}"
    sl = block["sleeves"][tk]
    sl_mean = (sl["cum_pnl_base"] / sl["closed_n"]) if sl["closed_n"] else None
    body = [
        f"Would-be ticket : {legs_line(legs)}   exp {pos['expiry']} ({pos['dte_in']} DTE) | {tk} {pos['spot_in']:.2f}, "
        f"width ${abs(legs[0]['strike'] - legs[1]['strike']):g}",
        f"Credit (real)   : mid {ps(cf['mid'])} | base fill (25%) {ps(cf['base'])} | natural {ps(cf['nat'])}"
        f"   Max loss {usd(pos['max_loss'], dec=0)}",
        f"Credit (model)  : {model_line}",
        f"Why             : first trading day of the week; short put delta {_fmt_delta(sh.get('delta'))} "
        f"({sh.get('delta_src', 'n/a')}), target 0.25; expiry closest to 30 DTE",
        f"Shadow record   : {tk} n={sl['closed_n']} (mean base {usd(sl_mean, True)}) | pooled {record_line('C6', block)}",
    ]
    head = f"C6 NONEQ_VRP{status_tag(block)} {tk} | {run_ts(now_et)}  ({quote_note(meta, stale)})"
    return make_message(head, body, "entry")


def exit_c6_real(pos: dict, row: dict, block: dict, why: str) -> dict:
    tk = pos["sleeve"]
    body = [
        f"Position : {legs_line(pos['legs'])}   exp {pos['expiry']} (entered {pos['entry_date']}, credit base "
        f"{ps(pos['entry_cf']['base'])})",
        f"Exit     : {why}",
        f"P&L (1 contract): mid {usd(_f(row['pnl_mid']), True)} | base {usd(_f(row['pnl_base']), True)} | "
        f"natural {usd(_f(row['pnl_nat']), True)}   (fees in: {usd(_f(row['entry_fees']))} + {usd(_f(row['exit_fees']))})",
        f"Record   : {record_line('C6', block)} | cumulative base {usd(block['cum_pnl_base'], True)} | "
        f"drawdown {usd(block['drawdown'], dec=0)}",
    ]
    return make_message(f"C6 NONEQ_VRP{status_tag(block)} {tk} | closed {row['exit_date']} ({row['exit_reason']})", body,
                        "win" if (_f(row["pnl_base"]) or 0) >= 0 else "loss")


def exit_c6_model(row: dict, block: dict) -> dict:
    tk = row["sleeve"]
    m = block["model"]
    body = [
        f"MODEL spread (Black-Scholes, GVZ/VXTLT-priced; the backtest's own method): {row['legs']}",
        f"Closed {row['exit_date']} ({row['exit_reason']}) | P&L (1 contract): mid {usd(_f(row['pnl_mid']), True)} | "
        f"base {usd(_f(row['pnl_base']), True)} | natural {usd(_f(row['pnl_nat']), True)}",
        f"Model record: n={m['closed_n']} | cumulative base {usd(m['cum_pnl_base'], True)} | backtest mean "
        f"{usd(cfg.BACKTEST['C6']['edge_base'], True)} per trade",
    ]
    return make_message(f"C6 NONEQ_VRP {tk} MODEL | closed {row['exit_date']} ({row['exit_reason']})", body,
                        "win" if (_f(row["pnl_base"]) or 0) >= 0 else "loss")


# ---------------------------------------------------------------------------------------------- C3
def gate_text(g: dict) -> str:
    if g.get("why") == "missing_index":
        return "VIX or VIX3M close missing"
    if g.get("why") == "no_quantile":
        return f"fewer than {cfg.C3['min_obs']} prior VIX closes"
    ratio = g.get("ratio")
    q25 = g.get("q25")
    return (f"VIX/VIX3M {ratio:.3f} {'<' if g.get('ratio_ok') else '>='} {cfg.C3['ratio_max']:.2f}; "
            f"VIX {g['vix']:.2f} {'<=' if g.get('level_ok') else '>'} 25th pct {q25:.2f}")


def entry_c3(pos: dict, block: dict, meta: dict, gate: dict, confirmed_on: dt.date, now_et: dt.datetime) -> dict:
    cf = pos["entry_cf"]
    body = [
        f"Would-be ticket : {legs_line(pos['legs'])}   exp {pos['expiry']} ({pos['dte_in']} DTE) | priced at the "
        f"{pos['entry_date']} snapshot (SPY {pos['spot_in']:.2f})",
        f"Debit           : mid {ps(-cf['mid'])} | base fill (25%) {ps(-cf['base'])} | natural {ps(-cf['nat'])}"
        f"   Max loss {usd(pos['max_loss'], dec=0)} (net debit + fees)",
        f"Why             : gate OPEN on the published {pos['entry_date']} closes: {gate_text(gate)}",
        f"Shadow record   : n={block['closed_n']} closed | premium paid since reset {usd(block['premium_paid_since_reset'], dec=0)} "
        f"| budget {'not set' if cfg.C3_HEDGE_BUDGET is None else usd(cfg.C3_HEDGE_BUDGET, dec=0)}",
    ]
    head = f"C3 SPY_HEDGE{status_tag(block)} | confirmed {confirmed_on} (entry date {pos['entry_date']})"
    return make_message(head, body, "entry")


def exit_c3(pos: dict, row: dict, block: dict) -> dict:
    body = [
        f"Position : {legs_line(pos['legs'])}   exp {pos['expiry']} (entered {pos['entry_date']}, debit base "
        f"{ps(-pos['entry_cf']['base'])})",
        f"Exit     : first trading day with DTE <= {cfg.C3['exit_dte']} ({row['dte_out']} DTE) at that day's quotes",
        f"P&L (1 contract): mid {usd(_f(row['pnl_mid']), True)} | base {usd(_f(row['pnl_base']), True)} | "
        f"natural {usd(_f(row['pnl_nat']), True)}",
        f"Ledger   : n={block['closed_n']} | cumulative base {usd(block['cum_pnl_base'], True)} | premium paid since reset "
        f"{usd(block['premium_paid_since_reset'], dec=0)}",
    ]
    return make_message(f"C3 SPY_HEDGE{status_tag(block)} | closed {row['exit_date']}", body,
                        "win" if (_f(row["pnl_base"]) or 0) >= 0 else "loss")


def heartbeat_c3(month: str, entry_date: str, gate: dict, result: str, detail: str, block: dict, prov_note: str = "") -> dict:
    """One line per month: the gate, pass or fail."""
    line = f"{month} gate {result} ({entry_date} closes): {gate_text(gate)}{prov_note}. {detail}"
    return make_message(f"C3 SPY_HEDGE | monthly gate {result}", [line], "info" if result == "CLOSED" else "entry")


# ---------------------------------------------------------------------------------------------- flags, notices, hello
def kill_alert(sid: str, block: dict, reason: str, text: str, prev_status: str) -> dict:
    body = [
        f"Reason  : {text}",
        f"Status  : {block['status']} (was {prev_status}). Shadow logging CONTINUES: nothing is paused, nothing was traded.",
        f"To retire it: set strategies.{sid}.status to \"retired\" in shadow_state.json. To clear the flag: set it back to \"active\".",
        "Thresholds are proposals from the hold-out tables, not backtested rules.",
    ]
    return make_message(f"{sid} {NAMES[sid]} | FLAGGED: {reason}", body, "warn")


def notice(headline: str, lines: list, color: str = "warn") -> dict:
    return make_message(headline, lines, color)


def hello(now_et: dt.datetime) -> dict:
    body = [
        f"Online {run_ts(now_et)}. Three paper-only bots: C9 CONDOR_1D, C6 NONEQ_VRP (GLD + TLT), C3 SPY_HEDGE.",
        f"One daily job at {cfg.TARGET_ET} ET. Nothing here places or changes an order, and no message is a trade callout.",
        "Evidence so far is Tier 3 for all three: the forward record can settle fill costs, the C6 pricing model and hedge",
        "episodes. It cannot prove an edge.",
        "Messages: one per new would-be position, one per exit, a weekly summary, a monthly C3 gate line, flags.",
    ]
    return make_message("shadow bots online", body, "info")


# ---------------------------------------------------------------------------------------------- weekly summary
def _pctl(xs: list, q: float) -> float:
    s = sorted(xs)
    pos = q * (len(s) - 1)
    lo, hi = int(pos), min(int(pos) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def ratio_stats(vals: list) -> str:
    vals = [v for v in vals if v is not None]
    if not vals:
        return "n=0"
    return f"n={len(vals)} median {statistics.median(vals):+.1%} p90 {_pctl(vals, 0.9):+.1%}"


def weekly(st: dict, log_rows: list, week_key: str, week_days: list, expected_runs: int) -> dict:
    """Weekly summary. `week_days` = ISO date strings Monday..today of the week (for 'this week' counts)."""
    days = set(week_days)
    lines = [f"Week {week_key} | job runs {st['runs'].get(week_key, 0)}/{expected_runs} expected"]
    for sid in cfg.STRATEGIES:
        b = st["strategies"][sid]
        n = b["closed_n"]
        tl = b["recent_pnl_base"]
        trailing = f"trailing-{len(tl)} {usd(sum(tl) / len(tl), True)}" if tl else "trailing n/a"
        real_rows = [r for r in log_rows if r["strategy"] == sid and r.get("version") != "model"]
        w_in = sum(1 for r in real_rows if r["entry_date"] in days) + sum(
            1 for p in b["open_positions"] if p["entry_date"] in days and p["id"] not in {r["id"] for r in real_rows})
        w_out = [r for r in real_rows if r["exit_date"] in days]
        w_pnl = sum(float(r["pnl_base"]) for r in w_out if r["pnl_base"] not in ("", None))
        skips = st["skips"].get(week_key, {}).get(sid, {})
        sk = ", ".join(f"{k} {v}" for k, v in sorted(skips.items())) or "none"
        bt = cfg.BACKTEST[sid]
        lines.append("")
        lines.append(f"{sid} {NAMES[sid]} [{b['status']}] n={n} {'primed' if b['primed'] else f'(primed at {cfg.PRIMED_MIN_TRADES})'}"
                     f" | open {len(b['open_positions'])}")
        if n:
            lines.append(f"  mean/trade base {usd(mean_of(b, 'cum_pnl_base'), True)} natural {usd(mean_of(b, 'cum_pnl_nat'), True)} | "
                         f"{trailing} | cumulative base {usd(b['cum_pnl_base'], True)}")
        if sid in ("C9", "C6"):
            official = (f" ({usd(bt['edge_base_official'], True)} / {usd(bt['edge_nat_official'], True)} at official-close settlement)"
                        if sid == "C9" else "")
            lines.append(f"  drawdown {usd(b['drawdown'], dec=0)} of stop {usd(bt['dd_stop'], dec=0)} | backtest "
                         f"{usd(bt['edge_base'], True)} / {usd(bt['edge_nat'], True)}{official}")
        lines.append(f"  this week: {w_in} new, {len(w_out)} closed ({usd(w_pnl, True)} base) | skips: {sk}")
        if sid == "C6":
            m = b["model"]
            lines.append(f"  MODEL path: n={m['closed_n']} cumulative base {usd(m['cum_pnl_base'], True)} | open {len(m['open'])}")
            for tk, s in b["sleeves"].items():
                mm = m["sleeves"].get(tk, {"closed_n": 0, "cum_pnl_base": 0.0})
                lines.append(f"  {tk}: real n={s['closed_n']} cum base {usd(s['cum_pnl_base'], True)} | model n={mm['closed_n']} "
                             f"cum base {usd(mm['cum_pnl_base'], True)}")
            pairs = b["pairs"]
            lines.append(f"  real / model - 1, entry credit (base): {ratio_stats([_ratio_minus_1(p.get('real_credit_base'), p.get('model_credit_base')) for p in pairs])}")
            lines.append(f"  real / model - 1, exit mark (mid): {ratio_stats([_ratio_minus_1(p.get('real_exit_mark'), p.get('model_exit_mark')) for p in pairs])}")
        if sid == "C3":
            lines.append(f"  premium paid since reset {usd(b['premium_paid_since_reset'], dec=0)} | budget "
                         f"{'not set' if cfg.C3_HEDGE_BUDGET is None else usd(cfg.C3_HEDGE_BUDGET, dec=0)} | gate months logged "
                         f"{len(b['gate_log'])}")
            if b["hedge_ledger"]:
                e = b["hedge_ledger"][-1]
                lines.append(f"  last hedge-ledger event: {e['date']} SPY -{e['level'] * 100:.0f}% from peak; hedges open "
                             f"{len(e['open'])}, paid {usd(e['paid_total'], True, 0)}")
    diffs = st["notes"].get("prov_vs_pub", [])
    if diffs:
        by: dict = {}
        for d in diffs[-120:]:
            by.setdefault(d["name"], []).append(abs(d["pub"] - d["prov"]))
        lines.append("")
        lines.append("Index quote vs published close, mean |diff|: " + ", ".join(
            f"{k} {sum(v) / len(v):.2f} (n={len(v)})" for k, v in sorted(by.items())))
    return make_message(f"weekly summary {week_key}", lines, "info")


def _ratio_minus_1(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or b is None or b == 0:
        return None
    return a / b - 1.0
