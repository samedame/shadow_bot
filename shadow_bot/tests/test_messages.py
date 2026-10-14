"""Message text (messages.py). Positions come from the real strategy code (rules_* + jobctx.build_position), so the wording is
checked against exactly the dicts the job will pass in."""
import datetime as dt
import re
import unittest

from tests.helpers import D, et
from tests.test_rules_c3 import ASOF as C3_ASOF, chain as c3_chain
from tests.test_rules_c6 import ASOF as C6_ASOF, gld_chain
from tests.test_rules_c9 import ASOF as C9_ASOF, EX as C9_EX, chain as c9_chain

import config as cfg
import discord_shadow as ds
import fills
import messages as M
import rules_c3
import rules_c6
import rules_c9
import state as S
from jobctx import Ctx, build_position, close_out
from market_data import Market

FORBIDDEN = ("place this order", "place the order", "enter this trade", "you should", "buy now", "sell now", "execute", "go long",
             "go short", "@everyone", "@here", "<@")


def make_ctx(session: str, now: str) -> Ctx:
    return Ctx(root="", now_et=et(now), session=D(session), phase="close", early_close=False, close_et=dt.time(16, 0),
               trigger="cf-scheduler", guard_status="on_time", late_min=0.5, dry_run=False, only=set(cfg.STRATEGIES), feed=None,
               st=S.new_state(), mkt=Market(), log_rows=[])


def meta(**kw):
    m = {"und_last_trade_et": "2026-10-13T16:00:00-04:00", "file_age_min": 5.2, "spot_src": "close",
         "file_ts_utc": "2026-10-13T20:19:32+00:00"}
    m.update(kw)
    return m


def c9_position(ctx=None, alt=None):
    ctx = ctx or make_ctx("2026-10-13", "2026-10-13 16:20")
    ch = c9_chain()
    ch.meta["file_ts_utc"] = "2026-10-13T20:19:32+00:00"      # the feed file's generation time; its option quotes are ~15 min older
    res = rules_c9.evaluate(ch, C9_ASOF, C9_EX, True)
    t = res["ticket"]
    pos = build_position(ctx, "C9", "SPY", "C9-2026-10-13", t, ch, extra={
        "gate": {"prev": "2026-10-12", "rv20": 0.0061, "median": 0.0074, "window": 20}, "alt": alt, "strikes": t["strikes"]})
    return ctx, ch, pos


def c6_position(prov=True):
    ctx = make_ctx("2026-10-13", "2026-10-13 16:20")
    ch = gld_chain()
    t = rules_c6.real_entry(ch, C6_ASOF, 382.15)["ticket"]
    pos = build_position(ctx, "C6", "GLD", "C6-GLD-2026-10-13", t, ch, extra={
        "pair_id": "C6-GLD-2026-10-13", "model_credit_base": 163.86, "real_over_model": round(130.0 / 163.86, 4),
        "strikes": t["strikes"], "width": t["width"]})
    return ctx, ch, pos


def c3_position():
    """-> (confirmation-day ctx, chain, position). The position is priced and built on the entry day, 2026-11-02, and the
    entry message goes out on the day the published closes confirm the gate, 2026-11-03."""
    entry_ctx = make_ctx("2026-11-02", "2026-11-02 16:20")
    ch = c3_chain()
    t = rules_c3.build_ticket(ch, C3_ASOF)["ticket"]
    pos = build_position(entry_ctx, "C3", "SPY", "C3-2026-11-02", t, ch, extra={"net_debit_base": t["net_debit_base"], "strikes": t["strikes"]})
    return make_ctx("2026-11-03", "2026-11-03 16:20"), ch, pos


def check_message(tc: unittest.TestCase, msg: dict) -> str:
    plain = msg["plain"]
    tc.assertTrue(plain.startswith(cfg.BANNER + " | "), plain[:60])
    tc.assertLessEqual(len(plain), 1900)
    low = plain.lower()
    for bad in FORBIDDEN:
        tc.assertNotIn(bad, low)
    if msg["embed"] is not None:
        tc.assertEqual(msg["embed"]["footer"]["text"], cfg.BANNER)
        tc.assertLessEqual(ds.embed_size(msg["embed"]), 6000)
        tc.assertLessEqual(len(msg["embed"]["description"]), 4096)
    return plain


class Formats(unittest.TestCase):
    def test_usd(self):
        self.assertEqual(M.usd(1234.5), "$1,234.50")
        self.assertEqual(M.usd(-3.0), "-$3.00")
        self.assertEqual(M.usd(0.25, signed=True), "+$0.25")
        self.assertEqual(M.usd(-0.25, signed=True), "-$0.25")
        self.assertEqual(M.usd(0.0, signed=True), "+$0.00")
        self.assertEqual(M.usd(None), "n/a")
        self.assertEqual(M.usd(3908.4, dec=0), "$3,908")
        self.assertEqual(M.usd(-0.004), "-$0.00")

    def test_per_share(self):
        self.assertEqual(M.ps(173.0), "$1.73")
        self.assertEqual(M.ps(-646.25, signed=True), "-$6.46")
        self.assertEqual(M.ps(None), "n/a")

    def test_pct_and_clock(self):
        self.assertEqual(M.pct(0.0061), "0.61%")
        self.assertEqual(M.pct(None), "n/a")
        self.assertEqual(M.hm("2026-10-13T16:00:00-04:00"), "16:00")
        self.assertEqual(M.hm(None), "n/a")
        self.assertEqual(M.run_ts(et("2026-10-13 16:20")), "2026-10-13 16:20 ET")

    def test_legs_line(self):
        legs = [{"qty": -1, "strike": 604.0, "cp": +1}, {"qty": 1, "strike": 610.0, "cp": +1},
                {"qty": -1, "strike": 350.5, "cp": -1}, {"qty": 1, "strike": 345.0, "cp": -1}]
        self.assertEqual(M.legs_line(legs), "SELL 1 604C / BUY 1 610C / SELL 1 350.5P / BUY 1 345P")

    def test_quote_note(self):
        self.assertEqual(M.quote_note(None, None), "quotes: n/a")
        self.assertEqual(M.quote_note(meta(), None), "underlying last trade 16:00, option quotes ~16:04, file 5 min old, stale: no")
        self.assertEqual(M.quote_note(meta(file_age_min=None), "closing print missing"),
                         "underlying last trade 16:00, option quotes ~16:04, stale: YES (closing print missing)")
        self.assertEqual(M.quote_note(meta(file_ts_utc=None), None),
                         "underlying last trade 16:00, option quotes ~n/a, file 5 min old, stale: no")

    def test_option_quote_clock_is_the_file_time_minus_the_feed_delay_in_new_york_time(self):
        self.assertEqual(M.option_quote_hm({"file_ts_utc": "2026-10-13T20:19:32+00:00"}), "16:04")      # EDT, UTC-4
        self.assertEqual(M.option_quote_hm({"file_ts_utc": "2026-11-03T21:20:00+00:00"}), "16:05")      # EST, UTC-5
        self.assertEqual(M.option_quote_hm({"file_ts_utc": "2026-10-13T20:15:00+00:00"}), "16:00")
        self.assertEqual(M.option_quote_hm({}), "n/a")

    def test_status_tag(self):
        self.assertEqual(M.status_tag({"status": "active"}), "")
        self.assertEqual(M.status_tag({"status": "flagged"}), " [FLAGGED]")
        self.assertEqual(M.status_tag({"status": "retired"}), " [RETIRED]")
        self.assertEqual(M.status_tag({}), "")

    def test_ratio_stats(self):
        self.assertEqual(M.ratio_stats([]), "n=0")
        self.assertEqual(M.ratio_stats([None, None]), "n=0")
        self.assertEqual(M.ratio_stats([0.1, -0.2, 0.3, None]), "n=3 median +10.0% p90 +26.0%")
        self.assertEqual(M.ratio_stats([0.05]), "n=1 median +5.0% p90 +5.0%")

    def test_record_line(self):
        b = S.new_strategy_block("C9")
        self.assertEqual(M.record_line("C9", b)[:34], "n=0 closed (primed at 20) | backte")
        self.assertIn("backtest +$1.83 / +$0.71 (+$1.47 / +$0.35 at official-close settlement)", M.record_line("C9", b))
        S.update_stats(b, 10.0, 6.0, 12.0)
        S.update_stats(b, -4.0, -6.0, -2.0)
        self.assertIn("n=2 | mean $/trade base +$3.00 natural +$0.00", M.record_line("C9", b))
        self.assertIn("backtest +$7.96 / -$0.34 (model-priced)", M.record_line("C6", S.new_strategy_block("C6")))
        self.assertIn("no edge claimed", M.record_line("C3", S.new_strategy_block("C3")))


class C9Messages(unittest.TestCase):
    def test_entry(self):
        ctx, ch, pos = c9_position()
        blk = ctx.block("C9")
        msg = M.entry_c9(pos, blk, ch.meta, ctx.now_et)
        text = check_message(self, msg)
        lines = text.split("\n")
        self.assertEqual(lines[0], "SHADOW - NOT A TRADE | C9 CONDOR_1D | 2026-10-13 16:20 ET  (underlying last trade 16:00, option quotes ~16:04, file 5 min old, stale: no)")
        self.assertIn("Would-be ticket : SELL 1 604C / BUY 1 610C / SELL 1 596P / BUY 1 590P   exp 2026-10-14", text)
        self.assertIn("Credit          : mid $1.76 | base fill (25%) $1.73 | natural $1.70   Max loss $427", text)
        self.assertIn("Underlying      : SPY 600.00 (close) | shorts 604C delta +0.16, 596P delta -0.16 (feed)", text)
        self.assertIn("Why             : RV20 0.61% < median 0.74% (range-bound); expiry next trading day (2026-10-14)", text)
        self.assertIn("Shadow record   : n=0 closed (primed at 20)", text)
        self.assertNotIn("Selection check", text, "only shown when the Black-Scholes pick differs")
        self.assertEqual(msg["embed"]["color"], ds.COLORS["entry"])

    def test_flagged_status_and_stale_quote_show_in_the_headline(self):
        ctx, ch, pos = c9_position()
        blk = ctx.block("C9")
        blk["status"] = "flagged"
        text = M.entry_c9(pos, blk, ch.meta, ctx.now_et, "file older than 45 min")["plain"]
        self.assertIn("C9 CONDOR_1D [FLAGGED] | 2026-10-13 16:20 ET", text)
        self.assertIn("stale: YES (file older than 45 min)", text)

    def test_selection_check_line_when_the_black_scholes_pick_differs(self):
        alt = {"differs": True, "call": 605.0, "put": 595.0, "feed_call": 604.0, "feed_put": 596.0}
        ctx, ch, pos = c9_position(alt=alt)
        text = M.entry_c9(pos, ctx.block("C9"), ch.meta, ctx.now_et)["plain"]
        self.assertIn("Selection check : Black-Scholes deltas on feed IV would pick 605C / 595P (feed delta picked 604C / 596P)", text)

    def test_entry_never_reads_as_an_instruction(self):
        ctx, ch, pos = c9_position()
        text = M.entry_c9(pos, ctx.block("C9"), ch.meta, ctx.now_et)["plain"]
        self.assertIn("Would-be ticket", text)
        self.assertNotRegex(text, r"(?im)^(buy|sell)\b")

    def settle(self, px):
        ctx, ch, pos = c9_position()
        blk = ctx.block("C9")
        blk["open_positions"].append(pos)
        legs = S.legs_from_json(pos["legs"])
        ext = rules_c9.settle(legs, px, C9_EX)
        ctx.now_et = et("2026-10-14 16:20")
        row, _rec = close_out(ctx, "C9", pos, ext, C9_EX, "expiry")
        return pos, row, blk

    def test_exit_wordings(self):
        for px, how in ((600.0, "inside both short strikes: full credit kept"), (604.0, "inside both short strikes: full credit kept"),
                        (605.0, "call side breached (1.00 through 604)"), (595.0, "put side breached (1.00 through 596)"),
                        (610.0, "call side breached (6.00 through 604)"),
                        (611.0, "above the call wing 610: maximum loss region"), (589.0, "below the put wing 590: maximum loss region")):
            pos, row, blk = self.settle(px)
            msg = M.exit_c9(pos, row, px, blk)
            text = check_message(self, msg)
            self.assertIn(f"Settle   : SPY official close {px:.2f} -> {how}", text, px)

    def test_exit_numbers_and_colour(self):
        pos, row, blk = self.settle(600.0)
        msg = M.exit_c9(pos, row, 600.0, blk)
        text = msg["plain"]
        self.assertIn("C9 CONDOR_1D | settled 2026-10-14", text)
        self.assertIn("P&L (1 contract): mid +$176.00 | base +$172.80 | natural +$169.80", text)   # mid: no fees; base 173.00 - 0.20
        self.assertIn("entry fees $0.20, no closing fee at expiry", text)
        self.assertEqual(msg["embed"]["color"], ds.COLORS["win"])
        pos, row, blk = self.settle(620.0)
        msg = M.exit_c9(pos, row, 620.0, blk)
        self.assertEqual(msg["embed"]["color"], ds.COLORS["loss"])
        self.assertIn("base -$427.20", msg["plain"])
        self.assertIn("Record   : n=1 | mean $/trade base -$427.20", msg["plain"])
        self.assertIn("drawdown $427", msg["plain"])


class C6Messages(unittest.TestCase):
    def test_entry_with_the_provisional_model_credit(self):
        ctx, ch, pos = c6_position()
        prov = {"credit_base": 163.86, "index": "GVZ", "iv_pct": 22.48}
        text = check_message(self, M.entry_c6(pos, ctx.block("C6"), ch.meta, ctx.now_et, prov))
        self.assertIn("C6 NONEQ_VRP GLD | 2026-10-13 16:20 ET", text.split("\n")[0])
        self.assertIn("Would-be ticket : SELL 1 368P / BUY 1 360P   exp 2026-11-12 (30 DTE) | GLD 382.15, width $8", text)
        self.assertIn("Credit (real)   : mid $1.34 | base fill (25%) $1.30 | natural $1.26   Max loss $670", text)
        self.assertIn("Credit (model)  : base $1.64 per share, provisional (GVZ quote 22.48; the published close replaces it) | real / model 0.79", text)
        self.assertIn("short put delta -0.25 (feed), target 0.25; expiry closest to 30 DTE", text)
        self.assertIn("Shadow record   : GLD n=0 (mean base n/a) | pooled n=0 closed (primed at 20)", text)

    def test_entry_without_a_model_credit(self):
        ctx, ch, pos = c6_position()
        text = M.entry_c6(pos, ctx.block("C6"), ch.meta, ctx.now_et, None)["plain"]
        self.assertIn("Credit (model)  : n/a (no provisional index quote)", text)
        pos["real_over_model"] = None
        prov = {"credit_base": 163.86, "index": "GVZ", "iv_pct": 22.48}
        text = M.entry_c6(pos, ctx.block("C6"), ch.meta, ctx.now_et, prov)["plain"]
        self.assertNotIn("real / model", text)

    def test_sleeve_record_shows_the_mean(self):
        ctx, ch, pos = c6_position()
        blk = ctx.block("C6")
        S.update_stats(blk, 10.0, 6.0, 12.0, sleeve="GLD")
        S.update_stats(blk, 6.0, 2.0, 8.0, sleeve="GLD")
        text = M.entry_c6(pos, blk, ch.meta, ctx.now_et, None)["plain"]
        self.assertIn("GLD n=2 (mean base +$8.00)", text)

    def close(self, reason):
        ctx, ch, pos = c6_position()
        blk = ctx.block("C6")
        blk["open_positions"].append(pos)
        legs = S.legs_from_json(pos["legs"])
        ext = {"cf": fills.close_cf(legs, [(0.55, 0.65), (0.08, 0.12)]), "units": 2}
        ctx.now_et = et("2026-10-20 16:20")
        row, _ = close_out(ctx, "C6", pos, ext, D("2026-10-20"), reason, count_sleeve=True)
        return pos, row, blk

    def test_real_exit_texts(self):
        pos, row, blk = self.close("profit")
        why = "profit target: mid-mark cost to close $0.50 <= 50% of the base entry credit $1.30"
        msg = M.exit_c6_real(pos, row, blk, why)
        text = check_message(self, msg)
        self.assertIn("C6 NONEQ_VRP GLD | closed 2026-10-20 (profit)", text)
        self.assertIn("Position : SELL 1 368P / BUY 1 360P   exp 2026-11-12 (entered 2026-10-13, credit base $1.30)", text)
        self.assertIn(f"Exit     : {why}", text)
        self.assertIn("(fees in: $0.10 + $0.10)", text)
        self.assertEqual(msg["embed"]["color"], ds.COLORS["win"])

    def test_model_exit_text(self):
        blk = S.new_strategy_block("C6")
        S.update_model_stats(blk, 7.5, 1.0, "TLT")
        row = {"sleeve": "TLT", "legs": "SELL 1 76P 2026-11-12 ; BUY 1 74P 2026-11-12", "exit_date": "2026-11-05", "exit_reason": "dte",
               "pnl_mid": "12.0", "pnl_base": "7.5", "pnl_nat": "1.0"}
        msg = M.exit_c6_model(row, blk)
        text = check_message(self, msg)
        self.assertIn("C6 NONEQ_VRP TLT MODEL | closed 2026-11-05 (dte)", text)
        self.assertIn("MODEL spread (Black-Scholes, GVZ/VXTLT-priced; the backtest's own method): SELL 1 76P 2026-11-12 ; BUY 1 74P", text)
        self.assertIn("Model record: n=1 | cumulative base +$7.50 | backtest mean +$7.96 per trade", text)
        self.assertEqual(msg["embed"]["color"], ds.COLORS["win"])
        row["pnl_base"] = "-3.0"
        self.assertEqual(M.exit_c6_model(row, blk)["embed"]["color"], ds.COLORS["loss"])


class C3Messages(unittest.TestCase):
    GATE = {"vix": 14.0, "vix3m": 17.0, "ratio": 14.0 / 17.0, "q25": 14.5, "ratio_ok": True, "level_ok": True}

    def test_gate_texts(self):
        self.assertEqual(M.gate_text(self.GATE), "VIX/VIX3M 0.824 < 0.90; VIX 14.00 <= 25th pct 14.50")
        self.assertEqual(M.gate_text(dict(self.GATE, ratio=0.93, ratio_ok=False)), "VIX/VIX3M 0.930 >= 0.90; VIX 14.00 <= 25th pct 14.50")
        self.assertEqual(M.gate_text(dict(self.GATE, level_ok=False, vix=16.0)), "VIX/VIX3M 0.824 < 0.90; VIX 16.00 > 25th pct 14.50")
        self.assertEqual(M.gate_text({"why": "missing_index"}), "VIX or VIX3M close missing")
        self.assertEqual(M.gate_text({"why": "no_quantile"}), "fewer than 200 prior VIX closes")

    def test_entry(self):
        ctx, ch, pos = c3_position()
        blk = ctx.block("C3")
        blk["premium_paid_since_reset"] = 646.35
        msg = M.entry_c3(pos, blk, ch.meta, self.GATE, D("2026-11-03"), ctx.now_et)
        text = check_message(self, msg)
        self.assertIn("C3 SPY_HEDGE | confirmed 2026-11-03 (entry date 2026-11-02)", text.split("\n")[0])
        self.assertIn("Would-be ticket : BUY 1 665P / SELL 1 595P   exp 2027-01-01 (60 DTE) | priced at the 2026-11-02 snapshot (SPY 700.00)", text)
        self.assertIn("Debit           : mid $6.42 | base fill (25%) $6.46 | natural $6.50   Max loss $646 (net debit + fees)", text)
        self.assertIn("Why             : gate OPEN on the published 2026-11-02 closes: VIX/VIX3M 0.824 < 0.90; VIX 14.00 <= 25th pct 14.50", text)
        self.assertIn("premium paid since reset $646 | budget not set", text)

    def test_exit(self):
        ctx, ch, pos = c3_position()
        blk = ctx.block("C3")
        blk["open_positions"].append(pos)
        legs = S.legs_from_json(pos["legs"])
        ext = {"cf": fills.close_cf(legs, [(60.0, 60.4), (2.0, 2.2)]), "units": 2}
        ctx.now_et = et("2026-12-10 16:20")
        row, _ = close_out(ctx, "C3", pos, ext, D("2026-12-10"), "dte")
        msg = M.exit_c3(pos, row, blk)
        text = check_message(self, msg)
        self.assertIn("C3 SPY_HEDGE | closed 2026-12-10", text)
        self.assertIn(f"Exit     : first trading day with DTE <= 21 ({row['dte_out']} DTE) at that day's quotes", text)
        self.assertIn("debit base $6.46)", text)
        self.assertEqual(msg["embed"]["color"], ds.COLORS["win"])

    def test_heartbeat(self):
        ctx, ch, pos = c3_position()
        blk = ctx.block("C3")
        msg = M.heartbeat_c3("2026-11", "2026-11-02", self.GATE, "OPEN", "Hedge opened: C3-2026-11-02.", blk)
        text = check_message(self, msg)
        self.assertEqual(text, "SHADOW - NOT A TRADE | C3 SPY_HEDGE | monthly gate OPEN\n2026-11 gate OPEN (2026-11-02 closes): "
                               "VIX/VIX3M 0.824 < 0.90; VIX 14.00 <= 25th pct 14.50. Hedge opened: C3-2026-11-02.")
        self.assertEqual(msg["embed"]["color"], ds.COLORS["entry"])
        closed = M.heartbeat_c3("2026-11", "2026-11-02", self.GATE, "CLOSED", "No hedge this month.", blk, " (provisional quote said OPEN: FLIPPED)")
        self.assertEqual(closed["embed"]["color"], ds.COLORS["info"])
        self.assertIn("(provisional quote said OPEN: FLIPPED). No hedge this month.", closed["plain"])


class Notices(unittest.TestCase):
    def test_hello(self):
        text = check_message(self, M.hello(et("2026-10-13 16:20")))
        self.assertIn("Online 2026-10-13 16:20 ET", text)
        self.assertIn("One daily job at 16:20 ET", text)
        self.assertIn("Nothing here places or changes an order, and no message is a trade callout", text)
        self.assertIn("Tier 3", text)

    def test_notice(self):
        text = check_message(self, M.notice("data unavailable", ["SPY chain: feed down", "GLD chain: feed down"]))
        self.assertEqual(text, "SHADOW - NOT A TRADE | data unavailable\nSPY chain: feed down\nGLD chain: feed down")

    def test_kill_alert(self):
        b = S.new_strategy_block("C6")
        b["status"] = "flagged"
        text = check_message(self, M.kill_alert("C6", b, "drawdown", "drawdown $3,000 from the running peak exceeds the $2,920 stop", "active"))
        self.assertIn("C6 NONEQ_VRP | FLAGGED: drawdown", text)
        self.assertIn("Status  : flagged (was active)", text)


class Weekly(unittest.TestCase):
    def build(self):
        st = S.new_state()
        for d in ("2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15"):
            S.bump_run(st, D(d))
        c9 = st["strategies"]["C9"]
        S.update_stats(c9, 172.8, 169.8, 175.8)
        c9["open_positions"].append({"id": "C9-2026-10-15", "entry_date": "2026-10-15", "legs": []})
        S.record_skip(st, "C9", D("2026-10-13"), "gate_closed")           # two different nights (one night is only ever counted once)
        S.record_skip(st, "C9", D("2026-10-14"), "gate_closed")
        c6 = st["strategies"]["C6"]
        S.update_stats(c6, 8.0, 1.0, 9.0, sleeve="GLD")
        S.update_model_stats(c6, 6.0, 0.0, "GLD")
        c6["pairs"] = [{"real_credit_base": 130.0, "model_credit_base": 160.0, "real_exit_mark": 60.0, "model_exit_mark": 50.0},
                       {"real_credit_base": 120.0, "model_credit_base": None, "real_exit_mark": None, "model_exit_mark": None}]
        c3 = st["strategies"]["C3"]
        c3["gate_log"] = [{"date": "2026-10-01"}]
        c3["hedge_ledger"] = [{"date": "2026-10-14", "level": 0.1, "open": [1, 2], "paid_total": 812.5}]
        c3["premium_paid_since_reset"] = 646.35
        st["notes"]["prov_vs_pub"] = [{"name": "VIX", "prov": 16.1, "pub": 16.3}, {"name": "VIX", "prov": 16.0, "pub": 15.8},
                                      {"name": "GVZ", "prov": 22.0, "pub": 22.5}]
        rows = [
            {"id": "C9-2026-10-13", "strategy": "C9", "version": "real", "entry_date": "2026-10-13", "exit_date": "2026-10-14", "pnl_base": "172.8"},
            {"id": "C9-2026-10-09", "strategy": "C9", "version": "real", "entry_date": "2026-10-09", "exit_date": "2026-10-12", "pnl_base": "-427.2"},
            {"id": "C9-2026-10-02", "strategy": "C9", "version": "real", "entry_date": "2026-10-02", "exit_date": "2026-10-05", "pnl_base": "50"},
            {"id": "C6M-GLD-2026-10-12", "strategy": "C6", "version": "model", "entry_date": "2026-10-12", "exit_date": "2026-10-14", "pnl_base": "6.0"},
        ]
        days = ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15"]
        return M.weekly(st, rows, "2026-W42", days, 5)

    def test_content(self):
        msg = self.build()
        text = check_message(self, msg)
        self.assertIn("weekly summary 2026-W42", text.split("\n")[0])
        self.assertIn("Week 2026-W42 | job runs 4/5 expected", text)
        self.assertIn("C9 CONDOR_1D [active] n=1 (primed at 20) | open 1", text)
        # new: the 10-13 row plus the open 10-15 position; closed: 10-13 (+172.80) and the 10-09 trade exiting 10-12 (-427.20)
        self.assertIn("this week: 2 new, 2 closed (-$254.40 base) | skips: gate_closed 2", text)
        self.assertIn("drawdown $0 of stop $3,908 | backtest +$1.83 / +$0.71", text)
        self.assertIn("MODEL path: n=1 cumulative base +$6.00 | open 0", text)
        self.assertIn("GLD: real n=1 cum base +$8.00 | model n=1 cum base +$6.00", text)
        self.assertIn("TLT: real n=0 cum base +$0.00 | model n=0 cum base +$0.00", text)
        self.assertIn("real / model - 1, entry credit (base): n=1 median -18.8% p90 -18.8%", text)
        self.assertIn("real / model - 1, exit mark (mid): n=1 median +20.0% p90 +20.0%", text)
        self.assertIn("premium paid since reset $646 | budget not set | gate months logged 1", text)
        self.assertIn("last hedge-ledger event: 2026-10-14 SPY -10% from peak; hedges open 2, paid +$812", text)
        self.assertIn("Index quote vs published close, mean |diff|: GVZ 0.50 (n=1), VIX 0.20 (n=2)", text)

    def test_model_rows_do_not_count_as_trades(self):
        text = self.build()["plain"]
        c6_block = text.split("C6 NONEQ_VRP")[1].split("C3 SPY_HEDGE")[0]
        self.assertIn("this week: 0 new, 0 closed (+$0.00 base)", c6_block)

    def test_fits_in_one_message(self):
        msg = self.build()
        self.assertLessEqual(len(msg["plain"]), 1900)


class EveryBuilder(unittest.TestCase):
    def test_all_messages_obey_the_rules(self):
        ctx9, ch9, p9 = c9_position()
        ctx6, ch6, p6 = c6_position()
        ctx3, ch3, p3 = c3_position()
        gate = C3Messages.GATE
        msgs = [
            M.entry_c9(p9, ctx9.block("C9"), ch9.meta, ctx9.now_et),
            M.entry_c6(p6, ctx6.block("C6"), ch6.meta, ctx6.now_et, None),
            M.entry_c3(p3, ctx3.block("C3"), ch3.meta, gate, D("2026-11-03"), ctx3.now_et),
            M.heartbeat_c3("2026-11", "2026-11-02", gate, "OPEN", "x", ctx3.block("C3")),
            M.hello(et("2026-10-13 16:20")),
            M.notice("headline", ["a"]),
            Weekly().build(),
        ]
        for m in msgs:
            check_message(self, m)
            self.assertIsNone(re.search(r"\bfill(ed)? at\b.*\bconfirm", m["plain"].lower()))


if __name__ == "__main__":
    unittest.main()
