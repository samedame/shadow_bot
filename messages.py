"""Message text for the shadow bots. Pure formatting: every function takes plain data and returns
discord_shadow.make_message(...) -> {"embed": ..., "plain": ...}. No I/O, no clock, no state changes.

Every alert has the same shape, so it can be read in a few seconds:
  * the notification line (first line, what a phone shows): strategy | what happened | the key numbers;
  * an embed title with a badge (new / WIN / LOSS) and a one-sentence lead saying what has to happen for the trade to work;
  * the would-be ticket in a small block (entries only), then labelled fields: credit or cost, max loss, break-evens,
    why now, the paper exit rule, how fresh the data is, and the shadow record with a plain "too early to judge" while n is small.
Credits and debits are shown per share AND per contract (x100); max loss and P&L are dollars for one contract. No message ever
says "buy" or "sell" as an instruction: tickets are labelled "Would-be ticket" and their lines start with Calls / Puts.
Plain text is ASCII only (the badge in the embed title is the one exception).
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


def day(iso: Optional[str], ref: Optional[str] = None) -> str:
    """'Wed Oct 14' from an ISO date or datetime; the year is added only when it differs from `ref`'s year."""
    if not iso:
        return "n/a"
    d = dt.date.fromisoformat(str(iso)[:10])
    s = f"{d:%a %b} {d.day}"
    if ref and dt.date.fromisoformat(str(ref)[:10]).year != d.year:
        s += f", {d.year}"
    return s


def days_word(n: int) -> str:
    return f"{n} day" if n == 1 else f"{n} days"


def legs_line(legs: list) -> str:
    """'SELL 1 783C / BUY 1 790C / SELL 1 773P / BUY 1 766P' from position-JSON legs."""
    return " / ".join(f"{'SELL' if l['qty'] < 0 else 'BUY'} {abs(l['qty'])} {l['strike']:g}{'C' if l['cp'] > 0 else 'P'}"
                      for l in legs)


def ticket_lines(pos: dict) -> list:
    """The would-be ticket as a short block: calls and puts on their own lines, then the expiry. Lines start with the
    leg group, never with 'buy' or 'sell', so nothing reads as an instruction."""
    out = ["Would-be ticket (1 contract, paper)"]
    for cp, label in ((1, "Calls"), (-1, "Puts")):
        ls = [l for l in pos["legs"] if l["cp"] == cp]
        if ls:
            out.append(f"{label:<9}" + " / ".join(f"{'sell' if l['qty'] < 0 else 'buy'} {l['strike']:g}" for l in ls))
    out.append(f"{'Expires':<9}{day(pos['expiry'], pos['entry_date'])} ({days_word(pos['dte_in'])})")
    return out


def option_quote_hm(meta: dict) -> str:
    """HH:MM ET the option bid/ask in the feed file are from: the file's generation time minus the feed's fixed delay
    (cfg.CBOE_FEED_DELAY_MIN). 'n/a' when the file carried no timestamp."""
    ts = meta.get("file_ts_utc")
    if not ts:
        return "n/a"
    t = dt.datetime.fromisoformat(ts) - dt.timedelta(minutes=cfg.CBOE_FEED_DELAY_MIN)
    return t.astimezone(ET).strftime("%H:%M")


def data_line(meta: Optional[dict], stale: Optional[str], now_et: Optional[dt.datetime] = None) -> str:
    """Provenance of the prices on an entry alert. Two clocks, because they differ: the underlying's last trade (the closing
    print, 16:00 for SPY) and the option quotes (the feed's snapshot, a few minutes later at a 16:20 run)."""
    if not meta:
        return "no quote data"
    parts = []
    if now_et is not None:
        parts.append(f"logged {now_et:%H:%M} ET")
    parts.append(f"underlying last traded {hm(meta.get('und_last_trade_et'))}")
    oq = option_quote_hm(meta)
    if oq != "n/a":
        parts.append(f"option quotes from about {oq}")
    age = meta.get("file_age_min")
    if age is not None:
        parts.append(f"feed file {age:.0f} min old")
    s = ", ".join(parts)
    return s if not stale else f"STALE ({stale}); {s}"


def status_tag(block: dict) -> str:
    return "" if block.get("status", "active") == "active" else f" [{block['status'].upper()}]"


def mean_of(block: dict, key: str) -> Optional[float]:
    n = block.get("closed_n", 0)
    return None if not n else block[key] / n


def verdict(pnl: Optional[float]) -> tuple:
    """-> (word, badge, colour) for a closed paper trade. Same rule as before: zero or better is a win."""
    return ("WIN", "✅", "win") if (pnl or 0) >= 0 else ("LOSS", "❌", "loss")


def record_line(sid: str, block: dict) -> str:
    """Compact record (also used by the weekly summary tests' reference): n, mean per trade, and the backtest numbers."""
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


def baseline(sid: str) -> tuple:
    """The like-for-like backtest mean per trade (realistic fill, worst fill). C9 settles at the official close here."""
    bt = cfg.BACKTEST[sid]
    if sid == "C9":
        return bt["edge_base_official"], bt["edge_nat_official"]
    return bt["edge_base"], bt["edge_nat"]


def record_text(sid: str, block: dict, only_sleeve: Optional[str] = None) -> str:
    """The shadow record in plain words. Says outright when the sample is too small to mean anything."""
    n = block.get("closed_n", 0)
    need = cfg.PRIMED_MIN_TRADES
    if sid == "C3":
        if n == 0:
            return "No closed hedges yet. This is a cost ledger, not an edge test (backtest: 13 trades)."
        return (f"{n} closed | cumulative {usd(block['cum_pnl_base'], True)} at realistic fills | "
                f"premium paid since reset {usd(block['premium_paid_since_reset'], dec=0)}")
    b_base, b_nat = baseline(sid)
    ref = f"Backtest average {usd(b_base, True)} / {usd(b_nat, True)}" + (" (model-priced)" if sid == "C6" else "")
    if n == 0:
        txt = f"No closed trades yet (needs {need} before it means anything). {ref} per trade (realistic / worst fills)."
    else:
        txt = (f"{n} closed | average {usd(mean_of(block, 'cum_pnl_base'), True)} per trade at realistic fills, "
               f"{usd(mean_of(block, 'cum_pnl_nat'), True)} at worst fills. {ref}.")
        if n < need:
            txt += f" Only {n} of {need} trades so far: too early to judge."
    return txt


def early_note(sid: str, block: dict, max_loss: float) -> str:
    """One sentence for an exit alert while the record is still small: one result is not evidence."""
    if block.get("closed_n", 0) >= cfg.PRIMED_MIN_TRADES:
        return ""
    b_base, _ = baseline(sid)
    return f" For scale: a max loss is {usd(max_loss, dec=0)}, against a backtest average of {usd(b_base, True)} per trade."


def _fill_range(cf: dict) -> str:
    return f"worst {ps(cf['nat'])} | mid {ps(cf['mid'])}"


def _delta(d: Optional[float]) -> str:
    return "n/a" if d is None else f"{d:+.2f}"


def _fmt_delta(d: Optional[float]) -> str:        # kept for callers that import it
    return _delta(d)


def _f(x) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _pnl_fields(row: dict, fee_text: str) -> list:
    """The three result fields every exit alert starts with."""
    return [
        ("Result", f"{usd(_f(row['pnl_base']), True)} per contract (realistic fill)", True),
        ("Fill range", f"worst {usd(_f(row['pnl_nat']), True)} | best (mid) {usd(_f(row['pnl_mid']), True)}", True),
        ("Fees", fee_text, True),
    ]


# ---------------------------------------------------------------------------------------------- C9
def entry_c9(pos: dict, block: dict, meta: dict, now_et: dt.datetime, stale: Optional[str] = None) -> dict:
    cf = pos["entry_cf"]
    cb = cf["base"] / cfg.MULT
    g = pos.get("gate") or {}
    legs = pos["legs"]
    sc = next(l for l in legs if l["cp"] > 0 and l["qty"] < 0)
    sp = next(l for l in legs if l["cp"] < 0 and l["qty"] < 0)
    wc = next(l["strike"] for l in legs if l["cp"] > 0 and l["qty"] > 0)
    wp = next(l["strike"] for l in legs if l["cp"] < 0 and l["qty"] > 0)
    exp = day(pos["expiry"], pos["entry_date"])
    lead = f"Keeps the full credit if SPY closes between {sp['strike']:g} and {sc['strike']:g} on {exp}."
    if stale:
        lead = f"STALE QUOTES ({stale}): treat these prices with caution. " + lead
    fields = [
        ("Credit (realistic fill)", f"{ps(cf['base'])} per share = {usd(cf['base'])} per contract\n{_fill_range(cf)}", True),
        ("Max loss", f"{usd(pos['max_loss'], dec=0)} per contract\nif SPY ends beyond {wp:g} or {wc:g}", True),
        ("Break-evens", f"{sp['strike'] - cb:.2f} / {sc['strike'] + cb:.2f}", True),
        ("Why now", f"Range-bound market: 20-day volatility {pct(g.get('rv20'))} is below its median {pct(g.get('median'))}."),
        ("Basis", f"SPY {pos['spot_in']:.2f} ({meta.get('spot_src', 'n/a')}). Short strikes picked at about "
                  f"{cfg.C9['delta'] * 100:.0f} delta: {sc['strike']:g}C {_delta(sc.get('delta'))}, "
                  f"{sp['strike']:g}P {_delta(sp.get('delta'))} ({sc.get('delta_src', 'n/a')})."),
    ]
    alt = pos.get("alt")
    if alt and alt.get("differs"):
        fields.append(("Strike check", f"Black-Scholes deltas on feed IV would pick {alt['call']:g}C / {alt['put']:g}P "
                                       f"(the feed's own deltas picked {alt['feed_call']:g}C / {alt['feed_put']:g}P)."))
    fields.append(("STALE DATA" if stale else "Data", data_line(meta, stale, now_et)))
    fields.append(("Record", record_text("C9", block)))
    tag = status_tag(block)
    summary = (f"C9 CONDOR_1D{tag} | NEW paper position: SPY iron condor, exp {exp} | credit {ps(cf['base'])}, "
               f"max loss {usd(pos['max_loss'], dec=0)}" + (" | STALE QUOTES" if stale else ""))
    return make_message(f"C9 CONDOR_1D{tag}: new SPY iron condor, exp {exp}", ticket_lines(pos), "entry", fields,
                        summary=summary, lead=lead, badge="\U0001f195")


def exit_c9(pos: dict, row: dict, px: float, block: dict) -> dict:
    legs = pos["legs"]
    kc = next(l["strike"] for l in legs if l["cp"] > 0 and l["qty"] < 0)
    kp = next(l["strike"] for l in legs if l["cp"] < 0 and l["qty"] < 0)
    wc = next(l["strike"] for l in legs if l["cp"] > 0 and l["qty"] > 0)
    wp = next(l["strike"] for l in legs if l["cp"] < 0 and l["qty"] > 0)
    if px > wc:
        how = f"above the {wc:g} long call: maximum loss."
    elif px < wp:
        how = f"below the {wp:g} long put: maximum loss."
    elif px > kc:
        how = f"{px - kc:.2f} past the {kc:g} short call: part of the credit was given back."
    elif px < kp:
        how = f"{kp - px:.2f} past the {kp:g} short put: part of the credit was given back."
    else:
        how = f"inside both short strikes ({kp:g} to {kc:g}): the full credit was kept."
    pnl = _f(row["pnl_base"])
    word, badge, colour = verdict(pnl)
    bt = cfg.BACKTEST["C9"]
    fields = _pnl_fields(row, f"{usd(_f(row['entry_fees']))} to enter, none at expiry")
    fields += [
        ("Trade", f"{legs_line(legs)} | entered {day(pos['entry_date'])}, expired {day(pos['expiry'])}"),
        ("Running total", f"{usd(block['cum_pnl_base'], True)} over {block['closed_n']} closed | drawdown "
                          f"{usd(block['drawdown'], dec=0)} (flag at {usd(bt['dd_stop'], dec=0)})"),
        ("Record", record_text("C9", block) + early_note("C9", block, pos["max_loss"])),
    ]
    when = day(row["exit_date"])
    return make_message(f"C9 CONDOR_1D{status_tag(block)}: {word} {usd(pnl, True)}, settled {when}", [], colour, fields,
                        summary=f"C9 CONDOR_1D{status_tag(block)} | settled {when}: {word} {usd(pnl, True)}",
                        lead=f"SPY's official close was {px:.2f}, {how}", badge=badge)


# ---------------------------------------------------------------------------------------------- C6
def entry_c6(pos: dict, block: dict, meta: dict, now_et: dt.datetime, prov: Optional[dict], stale: Optional[str] = None) -> dict:
    tk = pos["sleeve"]
    cf = pos["entry_cf"]
    cb = cf["base"] / cfg.MULT
    legs = pos["legs"]
    sh = next(l for l in legs if l["qty"] < 0)
    lg = next(l for l in legs if l["qty"] > 0)
    width = abs(sh["strike"] - lg["strike"])
    exp = day(pos["expiry"], pos["entry_date"])
    lead = (f"Keeps the full credit if {tk} stays above {sh['strike']:g} until {exp} "
            f"({days_word(pos['dte_in'])} out; {tk} is {pos['spot_in']:.2f} now).")
    if stale:
        lead = f"STALE QUOTES ({stale}): treat these prices with caution. " + lead
    if prov and prov.get("credit_base") is not None:
        model = f"{ps(prov['credit_base'])} per share (provisional, from the {prov['index']} quote {prov['iv_pct']:.2f}; the published close replaces it)"
        ratio = pos.get("real_over_model")
        if ratio is not None:
            model += f". Real quotes pay {ratio:.0%} of the model price."
    else:
        model = "n/a (no provisional index quote)"
    sl = block["sleeves"][tk]
    sl_mean = (sl["cum_pnl_base"] / sl["closed_n"]) if sl["closed_n"] else None
    profit_pct = cfg.C6["profit_frac"] * 100
    fields = [
        ("Credit (real quotes)", f"{ps(cf['base'])} per share = {usd(cf['base'])} per contract\n{_fill_range(cf)}", True),
        ("Max loss", f"{usd(pos['max_loss'], dec=0)} per contract\nif {tk} ends below {lg['strike']:g}", True),
        ("Break-even", f"{sh['strike'] - cb:.2f}", True),
        ("Backtest-model credit", model),
        ("Why now", f"First trading day of the week. Short put delta {_delta(sh.get('delta'))} ({sh.get('delta_src', 'n/a')}), "
                    f"target {cfg.C6['delta']:.2f}; expiry closest to {cfg.C6['dte']} days. Spread width ${width:g}."),
        ("Exit rule (paper)", f"Close when the mid cost to close is {profit_pct:.0f}% of the entry credit or less, "
                              f"otherwise on the first day with {cfg.C6['exit_dte']} or fewer days left."),
        ("STALE DATA" if stale else "Data", data_line(meta, stale, now_et)),
        ("Record", f"{tk}: {sl['closed_n']} closed" + ("" if sl_mean is None else f", average {usd(sl_mean, True)}") +
                   f". All C6: {record_text('C6', block)}"),
    ]
    tag = status_tag(block)
    summary = (f"C6 NONEQ_VRP{tag} {tk} | NEW paper position: put spread, exp {exp} | credit {ps(cf['base'])}, "
               f"max loss {usd(pos['max_loss'], dec=0)}" + (" | STALE QUOTES" if stale else ""))
    return make_message(f"C6 NONEQ_VRP{tag} {tk}: new put credit spread, exp {exp}", ticket_lines(pos), "entry", fields,
                        summary=summary, lead=lead, badge="\U0001f195")


def exit_c6_real(pos: dict, row: dict, block: dict, why: str) -> dict:
    tk = pos["sleeve"]
    pnl = _f(row["pnl_base"])
    word, badge, colour = verdict(pnl)
    fields = _pnl_fields(row, f"{usd(_f(row['entry_fees']))} in + {usd(_f(row['exit_fees']))} out")
    fields += [
        ("Trade", f"{legs_line(pos['legs'])} | entered {day(pos['entry_date'])} for {ps(pos['entry_cf']['base'])} per share, "
                  f"expiry {day(pos['expiry'])}"),
        ("Running total", f"{usd(block['cum_pnl_base'], True)} over {block['closed_n']} closed | drawdown "
                          f"{usd(block['drawdown'], dec=0)} (flag at {usd(cfg.BACKTEST['C6']['dd_stop'], dec=0)})"),
        ("Record", record_text("C6", block) + early_note("C6", block, pos["max_loss"])),
    ]
    when = day(row["exit_date"])
    reason = row["exit_reason"]
    return make_message(f"C6 NONEQ_VRP{status_tag(block)} {tk}: {word} {usd(pnl, True)}, closed {when}", [], colour, fields,
                        summary=f"C6 NONEQ_VRP{status_tag(block)} {tk} | closed {when} ({reason}): {word} {usd(pnl, True)}",
                        lead=f"Why it closed: {why}", badge=badge)


def exit_c6_model(row: dict, block: dict) -> dict:
    tk = row["sleeve"]
    m = block["model"]
    pnl = _f(row["pnl_base"])
    word, badge, colour = verdict(pnl)
    fields = [
        ("Result", f"{usd(pnl, True)} per contract (realistic fill)", True),
        ("Fill range", f"worst {usd(_f(row['pnl_nat']), True)} | best (mid) {usd(_f(row['pnl_mid']), True)}", True),
        ("Model spread", row["legs"]),
        ("Model record", f"{m['closed_n']} closed | cumulative {usd(m['cum_pnl_base'], True)} | backtest average "
                         f"{usd(cfg.BACKTEST['C6']['edge_base'], True)} per trade"),
    ]
    when = day(row["exit_date"])
    reason = row["exit_reason"]
    return make_message(f"C6 NONEQ_VRP {tk} MODEL: {word} {usd(pnl, True)}, closed {when}", [], colour, fields,
                        summary=f"C6 NONEQ_VRP {tk} MODEL | closed {when} ({reason}): {word} {usd(pnl, True)}",
                        lead=("Model twin, not a real-quote trade: priced with Black-Scholes from the GVZ / VXTLT index, "
                              "the backtest's own method. Compared against the real spread to check the model."),
                        badge=badge)


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
    legs = pos["legs"]
    lg = next(l for l in legs if l["qty"] > 0)
    sh = next(l for l in legs if l["qty"] < 0)
    debit_ps = -cf["base"] / cfg.MULT
    top = abs(lg["strike"] - sh["strike"]) * cfg.MULT
    exp = day(pos["expiry"], pos["entry_date"])
    lead = (f"Insurance, not a profit trade: it pays off if SPY falls below {lg['strike']:g} by {exp}. "
            f"The most it can lose is what it costs.")
    fields = [
        ("Cost (realistic fill)", f"${debit_ps:.2f} per share = {usd(-cf['base'])} per contract\n"
                                  f"worst {ps(-cf['nat'])} | mid {ps(-cf['mid'])}", True),
        ("Max loss", f"{usd(pos['max_loss'], dec=0)} per contract\n(net debit + fees)", True),
        ("Max payoff", f"about {usd(top + cf['base'], dec=0)} if SPY is {sh['strike']:g} or lower", True),
        ("Break-even", f"SPY {lg['strike'] - debit_ps:.2f} at expiry"),
        ("Why now", f"Gate OPEN on the published {pos['entry_date']} closes: {gate_text(gate)}."),
        ("Exit rule (paper)", f"Close on the first trading day with {cfg.C3['exit_dte']} or fewer days left."),
        ("Priced", f"at the {day(pos['entry_date'])} snapshot (SPY {pos['spot_in']:.2f}); confirmed {day(confirmed_on.isoformat())}"),
        ("Ledger", f"{block['closed_n']} closed | premium paid since reset {usd(block['premium_paid_since_reset'], dec=0)} | "
                   f"budget {'not set' if cfg.C3_HEDGE_BUDGET is None else usd(cfg.C3_HEDGE_BUDGET, dec=0)}"),
    ]
    tag = status_tag(block)
    summary = (f"C3 SPY_HEDGE{tag} | NEW paper hedge: SPY put spread, exp {exp} | cost {ps(-cf['base'])}, "
               f"max loss {usd(pos['max_loss'], dec=0)} | confirmed {day(confirmed_on.isoformat())}")
    return make_message(f"C3 SPY_HEDGE{tag}: new SPY put debit spread, exp {exp}", ticket_lines(pos), "entry", fields,
                        summary=summary, lead=lead, badge="\U0001f195")


def exit_c3(pos: dict, row: dict, block: dict) -> dict:
    pnl = _f(row["pnl_base"])
    _word, badge, colour = verdict(pnl)
    word = "PAID OFF" if colour == "win" else "COST"
    fields = [
        ("Result", f"{usd(pnl, True)} per contract (realistic fill)", True),
        ("Fill range", f"worst {usd(_f(row['pnl_nat']), True)} | best (mid) {usd(_f(row['pnl_mid']), True)}", True),
        ("Trade", f"{legs_line(pos['legs'])} | entered {day(pos['entry_date'])} for a {ps(-pos['entry_cf']['base'])} per share debit, "
                  f"expiry {day(pos['expiry'])}"),
        ("Ledger", f"{block['closed_n']} closed | cumulative {usd(block['cum_pnl_base'], True)} | premium paid since reset "
                   f"{usd(block['premium_paid_since_reset'], dec=0)}"),
    ]
    when = day(row["exit_date"])
    lead = (f"Closed on the first trading day with {cfg.C3['exit_dte']} or fewer days left ({row['dte_out']} days left) "
            f"at that day's quotes.")
    if colour == "loss":
        lead += " A hedge that costs money in a calm market is working as designed."
    return make_message(f"C3 SPY_HEDGE{status_tag(block)}: hedge {word} {usd(pnl, True)}, closed {when}", [], colour, fields,
                        summary=f"C3 SPY_HEDGE{status_tag(block)} | closed {when}: hedge {word} {usd(pnl, True)}",
                        lead=lead, badge=badge)


def heartbeat_c3(month: str, entry_date: str, gate: dict, result: str, detail: str, block: dict, prov_note: str = "") -> dict:
    """One line per month: the gate, pass or fail."""
    line = f"{month} gate {result} ({entry_date} closes): {gate_text(gate)}{prov_note}. {detail}"
    return make_message(f"C3 SPY_HEDGE | monthly gate {result}", [], "info" if result == "CLOSED" else "entry", lead=line)


# ---------------------------------------------------------------------------------------------- flags, notices, hello
def kill_alert(sid: str, block: dict, reason: str, text: str, prev_status: str) -> dict:
    fields = [
        ("Why", text),
        ("Status", f"{block['status']} (was {prev_status}). Shadow logging continues: nothing is paused and nothing was traded."),
        ("To retire it", f"set strategies.{sid}.status to \"retired\" in shadow_state.json"),
        ("To clear the flag", "set it back to \"active\""),
        ("Note", "Thresholds are proposals from the hold-out tables, not backtested rules."),
    ]
    return make_message(f"{sid} {NAMES[sid]} flagged: {reason}", [], "warn", fields,
                        summary=f"{sid} {NAMES[sid]} | FLAGGED: {reason}", badge="⚠️")


def notice(headline: str, lines: list, color: str = "warn") -> dict:
    return make_message(headline, lines, color)


def hello(now_et: dt.datetime) -> dict:
    lead = (f"Online {run_ts(now_et)}. Three paper-only bots: C9 CONDOR_1D, C6 NONEQ_VRP (GLD + TLT), C3 SPY_HEDGE.\n"
            f"One daily job at {cfg.TARGET_ET} ET. Nothing here places or changes an order, and no message is a trade callout.")
    fields = [
        ("What the record can show", "Evidence so far is Tier 3 for all three: the forward record can settle fill costs, the C6 "
                                      "pricing model and hedge episodes. It cannot prove an edge."),
        ("What you will see", "One message per new would-be position, one per exit, a weekly summary, a monthly C3 gate line, flags."),
        ("Reading the labels", "NEW = a paper position was logged. WIN / LOSS = it settled or closed (C3: PAID OFF / COST). "
                               "FLAGGED = a safety threshold tripped; logging continues."),
        ("Fill terms", "Realistic fill = 25% of the bid/ask spread away from the mid price. Worst = the bad side of the "
                       "bid/ask. Mid = halfway between bid and ask."),
    ]
    return make_message("shadow bots online", [], "info", fields, lead=lead)


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
