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
    tc.assertTrue(plain.isascii(), "the plain twin goes to the Actions log and the fallback post: ASCII only")
    low = plain.lower()
    for bad in FORBIDDEN:
        tc.assertNotIn(bad, low)
    if msg["embed"] is not None:
        tc.assertEqual(msg["embed"]["footer"]["text"], cfg.BANNER)
        tc.assertLessEqual(ds.embed_size(msg["embed"]), 6000)
        tc.assertLessEqual(len(msg["embed"]["description"]), 4096)
    return plain


def fields_of(msg: dict) -> dict:
    """The embed's labelled fields as {name: value}."""
    return {f["name"]: f["value"] for f in (msg["embed"] or {}).get("fields", [])}


def inline_names(msg: dict) -> list:
    return [f["name"] for f in msg["embed"]["fields"] if f["inline"]]


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

    def test_data_line(self):
        self.assertEqual(M.data_line(None, None), "no quote data")
        self.assertEqual(M.data_line(meta(), None), "underlying last traded 16:00, option quotes from about 16:04, feed file 5 min old")
        self.assertEqual(M.data_line(meta(), None, et("2026-10-13 16:20")),
                         "logged 16:20 ET, underlying last traded 16:00, option quotes from about 16:04, feed file 5 min old")
        self.assertEqual(M.data_line(meta(file_age_min=None), "closing print missing"),
                         "STALE (closing print missing); underlying last traded 16:00, option quotes from about 16:04")
        self.assertEqual(M.data_line(meta(file_ts_utc=None), None), "underlying last traded 16:00, feed file 5 min old",
                         "an unknown option-quote time is left out, not printed as n/a")

    def test_day_and_days(self):
        self.assertEqual(M.day("2026-10-14"), "Wed Oct 14")
        self.assertEqual(M.day("2026-10-14T16:20:00-04:00"), "Wed Oct 14")
        self.assertEqual(M.day("2027-01-01", "2026-11-02"), "Fri Jan 1, 2027", "the year appears only when it differs")
        self.assertEqual(M.day("2026-11-12", "2026-10-13"), "Thu Nov 12")
        self.assertEqual(M.day(None), "n/a")
        self.assertEqual((M.days_word(1), M.days_word(30), M.days_word(0)), ("1 day", "30 days", "0 days"))

    def test_verdict(self):
        self.assertEqual(M.verdict(10.0)[0::2], ("WIN", "win"))
        self.assertEqual(M.verdict(0.0)[0::2], ("WIN", "win"))
        self.assertEqual(M.verdict(None)[0::2], ("WIN", "win"))
        self.assertEqual(M.verdict(-0.01)[0::2], ("LOSS", "loss"))

    def test_record_text_says_when_the_sample_is_too_small(self):
        b = S.new_strategy_block("C9")
        t0 = M.record_text("C9", b)
        self.assertIn("No closed trades yet (needs 20 before it means anything)", t0)
        self.assertIn("Backtest average +$1.47 / +$0.35", t0, "C9 is compared with the official-close settlement numbers")
        S.update_stats(b, 10.0, 6.0, 12.0)
        S.update_stats(b, -4.0, -6.0, -2.0)
        t2 = M.record_text("C9", b)
        self.assertIn("2 closed | average +$3.00 per trade at realistic fills, +$0.00 at worst fills", t2)
        self.assertIn("Only 2 of 20 trades so far: too early to judge.", t2)
        b["closed_n"] = 20
        self.assertNotIn("too early", M.record_text("C9", b))
        self.assertNotIn("For scale", M.early_note("C9", b, 718.0))
        self.assertIn("For scale: a max loss is $718, against a backtest average of +$1.47 per trade.",
                      M.early_note("C9", S.new_strategy_block("C9"), 718.0))
        self.assertIn("Backtest average +$7.96 / -$0.34 (model-priced)", M.record_text("C6", S.new_strategy_block("C6")))
        self.assertIn("cost ledger, not an edge test", M.record_text("C3", S.new_strategy_block("C3")))

    def test_ticket_lines_never_start_with_buy_or_sell(self):
        ctx, ch, pos = c9_position()
        self.assertEqual(M.ticket_lines(pos), ["Would-be ticket (1 contract, paper)", "Calls    sell 604 / buy 610",
                                               "Puts     sell 596 / buy 590", "Expires  Wed Oct 14 (1 day)"])
        ctx, ch, pos = c3_position()
        self.assertEqual(M.ticket_lines(pos)[1:], ["Puts     buy 665 / sell 595", "Expires  Fri Jan 1, 2027 (60 days)"])

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
        # the notification line says what happened and the two numbers that matter
        self.assertEqual(lines[0], "SHADOW - NOT A TRADE | C9 CONDOR_1D | NEW paper position: SPY iron condor, exp Wed Oct 14 | "
                                   "credit $1.73, max loss $427")
        self.assertEqual(ds.payload_for(msg, True)["content"], lines[0])
        self.assertEqual(lines[1], "Keeps the full credit if SPY closes between 596 and 604 on Wed Oct 14.")
        self.assertEqual(lines[2:6], ["Would-be ticket (1 contract, paper)", "Calls    sell 604 / buy 610",
                                      "Puts     sell 596 / buy 590", "Expires  Wed Oct 14 (1 day)"])
        f = fields_of(msg)
        self.assertEqual(f["Credit (realistic fill)"], "$1.73 per share = $173.00 per contract\nworst $1.70 | mid $1.76")
        self.assertEqual(f["Max loss"], "$427 per contract\nif SPY ends beyond 590 or 610")
        self.assertEqual(f["Break-evens"], "594.27 / 605.73")
        self.assertEqual(f["Why now"], "Range-bound market: 20-day volatility 0.61% is below its median 0.74%.")
        self.assertEqual(f["Basis"], "SPY 600.00 (close). Short strikes picked at about 16 delta: 604C +0.16, 596P -0.16 (feed).")
        self.assertEqual(f["Data"], "logged 16:20 ET, underlying last traded 16:00, option quotes from about 16:04, feed file 5 min old")
        self.assertIn("No closed trades yet (needs 20 before it means anything)", f["Record"])
        self.assertNotIn("Strike check", f, "only shown when the Black-Scholes pick differs")
        self.assertEqual(inline_names(msg), ["Credit (realistic fill)", "Max loss", "Break-evens"])
        self.assertEqual(msg["embed"]["title"], "\U0001f195 C9 CONDOR_1D: new SPY iron condor, exp Wed Oct 14")
        self.assertEqual(msg["embed"]["color"], ds.COLORS["entry"])
        # every field is in the plain twin too, as "Name: value"
        for name, value in f.items():
            self.assertIn(f"{name}: {value.splitlines()[0]}", text)

    def test_flagged_status_and_stale_quote_show_in_the_headline(self):
        ctx, ch, pos = c9_position()
        blk = ctx.block("C9")
        blk["status"] = "flagged"
        msg = M.entry_c9(pos, blk, ch.meta, ctx.now_et, "file older than 45 min")
        text = msg["plain"]
        first = text.split("\n")[0]
        self.assertTrue(first.startswith("SHADOW - NOT A TRADE | C9 CONDOR_1D [FLAGGED] | NEW paper position"), first)
        self.assertTrue(first.endswith("| STALE QUOTES"), "a stale price is visible in the notification itself")
        self.assertIn("STALE QUOTES (file older than 45 min): treat these prices with caution.", text.split("\n")[1])
        self.assertIn("STALE DATA: STALE (file older than 45 min); logged 16:20 ET", text)
        self.assertIn("STALE DATA", fields_of(msg))
        self.assertNotIn("Data", fields_of(msg))

    def test_selection_check_line_when_the_black_scholes_pick_differs(self):
        alt = {"differs": True, "call": 605.0, "put": 595.0, "feed_call": 604.0, "feed_put": 596.0}
        ctx, ch, pos = c9_position(alt=alt)
        msg = M.entry_c9(pos, ctx.block("C9"), ch.meta, ctx.now_et)
        self.assertEqual(fields_of(msg)["Strike check"], "Black-Scholes deltas on feed IV would pick 605C / 595P "
                                                         "(the feed's own deltas picked 604C / 596P).")
        self.assertIn("Strike check: Black-Scholes deltas on feed IV would pick 605C / 595P", msg["plain"])

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
        for px, how in ((600.0, "inside both short strikes (596 to 604): the full credit was kept."),
                        (604.0, "inside both short strikes (596 to 604): the full credit was kept."),
                        (605.0, "1.00 past the 604 short call: part of the credit was given back."),
                        (595.0, "1.00 past the 596 short put: part of the credit was given back."),
                        (610.0, "6.00 past the 604 short call: part of the credit was given back."),
                        (611.0, "above the 610 long call: maximum loss."), (589.0, "below the 590 long put: maximum loss.")):
            pos, row, blk = self.settle(px)
            msg = M.exit_c9(pos, row, px, blk)
            text = check_message(self, msg)
            self.assertIn(f"SPY's official close was {px:.2f}, {how}", text, px)
            self.assertEqual(msg["embed"]["description"], f"SPY's official close was {px:.2f}, {how}", px)

    def test_exit_numbers_and_colour(self):
        pos, row, blk = self.settle(600.0)
        msg = M.exit_c9(pos, row, 600.0, blk)
        text = check_message(self, msg)
        self.assertEqual(text.split("\n")[0], "SHADOW - NOT A TRADE | C9 CONDOR_1D | settled Wed Oct 14: WIN +$172.80")
        f = fields_of(msg)
        self.assertEqual(f["Result"], "+$172.80 per contract (realistic fill)")          # 173.00 - 0.20 entry fee
        self.assertEqual(f["Fill range"], "worst +$169.80 | best (mid) +$176.00")        # mid: no fees
        self.assertEqual(f["Fees"], "$0.20 to enter, none at expiry")
        self.assertEqual(f["Trade"], "SELL 1 604C / BUY 1 610C / SELL 1 596P / BUY 1 590P | entered Tue Oct 13, expired Wed Oct 14")
        self.assertEqual(f["Running total"], "+$172.80 over 1 closed | drawdown $0 (flag at $3,908)")
        self.assertIn("Only 1 of 20 trades so far: too early to judge.", f["Record"])
        self.assertIn("For scale: a max loss is $427, against a backtest average of +$1.47 per trade.", f["Record"])
        self.assertEqual(inline_names(msg), ["Result", "Fill range", "Fees"])
        self.assertEqual(msg["embed"]["title"], "\u2705 C9 CONDOR_1D: WIN +$172.80, settled Wed Oct 14")
        self.assertEqual(msg["embed"]["color"], ds.COLORS["win"])
        pos, row, blk = self.settle(620.0)
        msg = M.exit_c9(pos, row, 620.0, blk)
        self.assertEqual(msg["embed"]["color"], ds.COLORS["loss"])
        self.assertEqual(msg["embed"]["title"], "\u274c C9 CONDOR_1D: LOSS -$427.20, settled Wed Oct 14")
        self.assertEqual(msg["plain"].split("\n")[0], "SHADOW - NOT A TRADE | C9 CONDOR_1D | settled Wed Oct 14: LOSS -$427.20")
        self.assertEqual(fields_of(msg)["Result"], "-$427.20 per contract (realistic fill)")
        self.assertIn("1 closed | average -$427.20 per trade", fields_of(msg)["Record"])
        self.assertIn("drawdown $427 (flag at $3,908)", fields_of(msg)["Running total"])

    def test_the_settle_notification_is_one_short_line_with_the_result(self):
        """The first line is what a phone shows. It names the strategy, the day and WIN / LOSS with the dollars, and none of the
        three fill variants or the backtest comparison that used to crowd the message."""
        pos, row, blk = self.settle(600.0)
        first = M.exit_c9(pos, row, 600.0, blk)["plain"].split("\n")[0]
        self.assertLessEqual(len(first), 80, first)
        self.assertNotIn("mid", first)
        self.assertNotIn("backtest", first)


class C6Messages(unittest.TestCase):
    PROV = {"credit_base": 163.86, "index": "GVZ", "iv_pct": 22.48}

    def test_entry_with_the_provisional_model_credit(self):
        ctx, ch, pos = c6_position()
        msg = M.entry_c6(pos, ctx.block("C6"), ch.meta, ctx.now_et, self.PROV)
        text = check_message(self, msg)
        lines = text.split("\n")
        self.assertEqual(lines[0], "SHADOW - NOT A TRADE | C6 NONEQ_VRP GLD | NEW paper position: put spread, exp Thu Nov 12 | "
                                   "credit $1.30, max loss $670")
        self.assertEqual(lines[1], "Keeps the full credit if GLD stays above 368 until Thu Nov 12 (30 days out; GLD is 382.15 now).")
        self.assertEqual(lines[2:5], ["Would-be ticket (1 contract, paper)", "Puts     sell 368 / buy 360",
                                      "Expires  Thu Nov 12 (30 days)"])
        f = fields_of(msg)
        self.assertEqual(f["Credit (real quotes)"], "$1.30 per share = $130.00 per contract\nworst $1.26 | mid $1.34")
        self.assertEqual(f["Max loss"], "$670 per contract\nif GLD ends below 360")
        self.assertEqual(f["Break-even"], "366.70")
        self.assertEqual(f["Backtest-model credit"], "$1.64 per share (provisional, from the GVZ quote 22.48; the published close "
                                                     "replaces it). Real quotes pay 79% of the model price.")
        self.assertEqual(f["Why now"], "First trading day of the week. Short put delta -0.25 (feed), target 0.25; "
                                       "expiry closest to 30 days. Spread width $8.")
        self.assertEqual(f["Exit rule (paper)"], "Close when the mid cost to close is 50% of the entry credit or less, "
                                                 "otherwise on the first day with 7 or fewer days left.")
        self.assertTrue(f["Record"].startswith("GLD: 0 closed. All C6: No closed trades yet"), f["Record"])
        self.assertEqual(msg["embed"]["title"], "\U0001f195 C6 NONEQ_VRP GLD: new put credit spread, exp Thu Nov 12")

    def test_entry_without_a_model_credit(self):
        ctx, ch, pos = c6_position()
        msg = M.entry_c6(pos, ctx.block("C6"), ch.meta, ctx.now_et, None)
        self.assertEqual(fields_of(msg)["Backtest-model credit"], "n/a (no provisional index quote)")
        pos["real_over_model"] = None
        text = M.entry_c6(pos, ctx.block("C6"), ch.meta, ctx.now_et, self.PROV)["plain"]
        self.assertNotIn("of the model price", text)

    def test_stale_entry_is_loud_in_the_notification(self):
        ctx, ch, pos = c6_position()
        msg = M.entry_c6(pos, ctx.block("C6"), ch.meta, ctx.now_et, None, "closing print missing")
        self.assertTrue(msg["plain"].split("\n")[0].endswith("| STALE QUOTES"))
        self.assertIn("STALE DATA", fields_of(msg))

    def test_sleeve_record_shows_the_mean(self):
        ctx, ch, pos = c6_position()
        blk = ctx.block("C6")
        S.update_stats(blk, 10.0, 6.0, 12.0, sleeve="GLD")
        S.update_stats(blk, 6.0, 2.0, 8.0, sleeve="GLD")
        msg = M.entry_c6(pos, blk, ch.meta, ctx.now_et, None)
        self.assertTrue(fields_of(msg)["Record"].startswith("GLD: 2 closed, average +$8.00. All C6: 2 closed | average +$8.00 per trade"))

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
        self.assertEqual(text.split("\n")[0], "SHADOW - NOT A TRADE | C6 NONEQ_VRP GLD | closed Tue Oct 20 (profit): WIN +$76.30")
        self.assertEqual(text.split("\n")[1], f"Why it closed: {why}")
        f = fields_of(msg)
        self.assertEqual(f["Result"], "+$76.30 per contract (realistic fill)")
        self.assertEqual(f["Fill range"], "worst +$68.80 | best (mid) +$84.00")
        self.assertEqual(f["Fees"], "$0.10 in + $0.10 out")
        self.assertEqual(f["Trade"], "SELL 1 368P / BUY 1 360P | entered Tue Oct 13 for $1.30 per share, expiry Thu Nov 12")
        self.assertEqual(f["Running total"], "+$76.30 over 1 closed | drawdown $0 (flag at $2,920)")
        self.assertEqual(msg["embed"]["title"], "\u2705 C6 NONEQ_VRP GLD: WIN +$76.30, closed Tue Oct 20")
        self.assertEqual(msg["embed"]["color"], ds.COLORS["win"])

    def test_model_exit_text(self):
        blk = S.new_strategy_block("C6")
        S.update_model_stats(blk, 7.5, 1.0, "TLT")
        row = {"sleeve": "TLT", "legs": "SELL 1 76P 2026-11-12 ; BUY 1 74P 2026-11-12", "exit_date": "2026-11-05", "exit_reason": "dte",
               "pnl_mid": "12.0", "pnl_base": "7.5", "pnl_nat": "1.0"}
        msg = M.exit_c6_model(row, blk)
        text = check_message(self, msg)
        self.assertEqual(text.split("\n")[0], "SHADOW - NOT A TRADE | C6 NONEQ_VRP TLT MODEL | closed Thu Nov 5 (dte): WIN +$7.50")
        self.assertIn("Model twin, not a real-quote trade: priced with Black-Scholes from the GVZ / VXTLT index", text)
        f = fields_of(msg)
        self.assertEqual(f["Model spread"], "SELL 1 76P 2026-11-12 ; BUY 1 74P 2026-11-12")
        self.assertEqual(f["Model record"], "1 closed | cumulative +$7.50 | backtest average +$7.96 per trade")
        self.assertEqual(msg["embed"]["color"], ds.COLORS["win"])
        row["pnl_base"] = "-3.0"
        loss = M.exit_c6_model(row, blk)
        self.assertEqual(loss["embed"]["color"], ds.COLORS["loss"])
        self.assertIn("LOSS -$3.00", loss["plain"].split("\n")[0])


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
        lines = text.split("\n")
        self.assertEqual(lines[0], "SHADOW - NOT A TRADE | C3 SPY_HEDGE | NEW paper hedge: SPY put spread, exp Fri Jan 1, 2027 | "
                                   "cost $6.46, max loss $646 | confirmed Tue Nov 3")
        self.assertEqual(lines[1], "Insurance, not a profit trade: it pays off if SPY falls below 665 by Fri Jan 1, 2027. "
                                   "The most it can lose is what it costs.")
        f = fields_of(msg)
        self.assertEqual(f["Cost (realistic fill)"], "$6.46 per share = $646.25 per contract\nworst $6.50 | mid $6.42")
        self.assertEqual(f["Max loss"], "$646 per contract\n(net debit + fees)")
        self.assertEqual(f["Max payoff"], "about $6,354 if SPY is 595 or lower")      # 70 points x 100 - the $646.25 debit
        self.assertEqual(f["Break-even"], "SPY 658.54 at expiry")                     # 665 - 6.4625
        self.assertEqual(f["Why now"], "Gate OPEN on the published 2026-11-02 closes: VIX/VIX3M 0.824 < 0.90; VIX 14.00 <= 25th pct 14.50.")
        self.assertEqual(f["Exit rule (paper)"], "Close on the first trading day with 21 or fewer days left.")
        self.assertEqual(f["Priced"], "at the Mon Nov 2 snapshot (SPY 700.00); confirmed Tue Nov 3")
        self.assertEqual(f["Ledger"], "0 closed | premium paid since reset $646 | budget not set")

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
        self.assertEqual(text.split("\n")[0], "SHADOW - NOT A TRADE | C3 SPY_HEDGE | closed Thu Dec 10: hedge PAID OFF +$5,148.55")
        self.assertIn("Closed on the first trading day with 21 or fewer days left", text)
        self.assertNotIn("working as designed", text, "that sentence is only for a hedge that cost money")
        self.assertIn("for a $6.46 per share debit", fields_of(msg)["Trade"])
        self.assertEqual(msg["embed"]["color"], ds.COLORS["win"])
        # a hedge that expires worthless is a COST, not a loss to be alarmed by
        row2 = dict(row, pnl_base="-646.35", pnl_nat="-650.0", pnl_mid="-642.0")
        cost = M.exit_c3(pos, row2, blk)
        self.assertIn("hedge COST -$646.35", cost["plain"].split("\n")[0])
        self.assertIn("A hedge that costs money in a calm market is working as designed.", cost["plain"])
        self.assertEqual(cost["embed"]["color"], ds.COLORS["loss"])

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
        self.assertIn("NEW = a paper position was logged. WIN / LOSS", text)
        self.assertIn("Realistic fill = 25% of the bid/ask spread away from the mid price", text)

    def test_notice(self):
        text = check_message(self, M.notice("data unavailable", ["SPY chain: feed down", "GLD chain: feed down"]))
        self.assertEqual(text, "SHADOW - NOT A TRADE | data unavailable\nSPY chain: feed down\nGLD chain: feed down")

    def test_kill_alert(self):
        b = S.new_strategy_block("C6")
        b["status"] = "flagged"
        text = check_message(self, M.kill_alert("C6", b, "drawdown", "drawdown $3,000 from the running peak exceeds the $2,920 stop", "active"))
        self.assertEqual(text.split("\n")[0], "SHADOW - NOT A TRADE | C6 NONEQ_VRP | FLAGGED: drawdown")
        self.assertIn("Status: flagged (was active). Shadow logging continues: nothing is paused and nothing was traded.", text)
        self.assertIn('To retire it: set strategies.C6.status to "retired" in shadow_state.json', text)


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
