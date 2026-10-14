"""C6 NONEQ_VRP: the MODEL path (the backtest's Black-Scholes pricing) against an independent scipy implementation and golden
values, and the REAL path (listed GLD / TLT chain) on hand-made chains."""
import datetime as dt
import math
import unittest

from tests.helpers import D, make_chain, needs_scipy, rows_from, sps

import config as cfg
import fills
import market_data as md
import nyse_calendar as nyse
import rules_c6
from bs import bs_put


# --------------------------------------------------------------------------------------------- independent oracle (scipy)
def o_put(S, K, T, r, s):
    d1 = (math.log(S / K) + (r + 0.5 * s * s) * T) / (s * math.sqrt(T))
    d2 = d1 - s * math.sqrt(T)
    return K * math.exp(-r * T) * sps.norm.cdf(-d2) - S * sps.norm.cdf(-d1), sps.norm.cdf(d1) - 1.0


def o_fill(V, side, k):
    h = max(0.03, 0.04 * V)
    bid, ask = max(V - h, 0.0), V + h
    return max(0.5 * (bid + ask) + side * k * (ask - bid), 0.0)


def o_entry(S0, ivpct, r):
    s, T = ivpct / 100.0, 30 / 365.0
    best = None
    for K in range(int(S0 * 0.80), int(S0) + 1):
        _, dl = o_put(S0, float(K), T, r, s)
        diff = abs(abs(dl) - 0.25)
        if best is None or diff < best[0]:
            best = (diff, float(K))
    Ks = best[1]
    Kl = Ks - max(1, round(0.02 * S0))
    vs, vl = o_put(S0, Ks, T, r, s)[0], o_put(S0, Kl, T, r, s)[0]
    cr = {k: (o_fill(vs, -1, k) - o_fill(vl, +1, k)) * 100 for k in (0.0, 0.25, 0.5, 0.75)}
    return Ks, Kl, vs, vl, cr


# (S0, index %, r) -> (Ks, Kl, short value, long value, credits at k = 0, .25, .5, .75), computed with scipy on 2026-10-13
GOLDEN_ENTRY = {
    (382.15, 22.48, 0.0406): (368.0, 360.0, 3.8279682475, 2.0713540516, (175.66141958, 163.86277499, 152.06413039, 140.26548579)),
    (78.05, 14.58, 0.0406): (76.0, 74.0, 0.4463864828, 0.1283974793, (31.79890035, 28.79890035, 25.79890035, 22.79890035)),
    (382.15, 22.01, 0.0406): (368.0, 360.0, 3.6644879657, 1.9493110747, (171.5176891, 160.29009102, 149.06249294, 137.83489486)),
}


class ModelWidth(unittest.TestCase):
    def test_bankers_rounding_and_floor(self):
        for s0, want in ((10.0, 1.0), (25.0, 1.0), (30.0, 1.0), (37.5, 1.0), (62.5, 1.0), (75.0, 2.0), (87.5, 2.0), (100.0, 2.0),
                         (125.0, 2.0), (175.0, 4.0), (382.15, 8.0), (78.05, 2.0)):
            self.assertEqual(rules_c6.model_width(s0), want, s0)


class ModelEntry(unittest.TestCase):
    D0 = D("2026-10-12")

    def test_golden_entries(self):
        for (s0, iv, r), (ks, kl, vs, vl, cr) in GOLDEN_ENTRY.items():
            e = rules_c6.model_entry(s0, iv, r, self.D0)
            self.assertEqual((e["Ks"], e["Kl"]), (ks, kl), (s0, iv))
            self.assertAlmostEqual(e["vs"], vs, places=8)
            self.assertAlmostEqual(e["vl"], vl, places=8)
            for k, want in zip(cfg.KS, cr):
                self.assertAlmostEqual(e["ent"]["cf"][k], want, places=5, msg=f"{s0} k={k}")
            self.assertIsNone(e["skip"])
            self.assertEqual(e["expiry"], self.D0 + dt.timedelta(days=30))
            self.assertEqual(e["width"], rules_c6.model_width(s0))
            self.assertEqual(e["ent"]["units"], 2)
            self.assertAlmostEqual(e["ent"]["mid_value"], (vs - vl) * 100, places=5)

    @needs_scipy
    def test_against_scipy_over_a_grid(self):
        for s0 in (24.0, 61.7, 78.05, 119.9, 202.2, 382.15, 450.0):
            for iv in (9.0, 14.58, 22.48, 31.0):
                for r in (0.0, 0.0406):
                    e = rules_c6.model_entry(s0, iv, r, self.D0)
                    ks, kl, vs, vl, cr = o_entry(s0, iv, r)
                    self.assertEqual((e["Ks"], e["Kl"]), (ks, kl), (s0, iv, r))
                    self.assertAlmostEqual(e["vs"], vs, places=9)
                    self.assertAlmostEqual(e["vl"], vl, places=9)
                    for k in cfg.KS:
                        self.assertAlmostEqual(e["ent"]["cf"][k], cr[k], places=6)

    def test_half_spread_rule(self):
        # V = 3.83 -> h = 4% x V = .1531 (above the $0.03 floor); V = 0.40 -> h = max(.03, .016) = .03
        self.assertAlmostEqual(rules_c6.m_fill(3.83, +1, 0.25), 3.83 + 0.25 * 2 * 0.1532, places=9)
        self.assertAlmostEqual(rules_c6.m_fill(0.40, +1, 0.5), 0.43, places=12)           # the ask: V + h
        self.assertAlmostEqual(rules_c6.m_fill(0.40, -1, 0.5), 0.37, places=12)           # the bid: V - h
        self.assertAlmostEqual(rules_c6.m_fill(0.40, -1, 0.0), 0.40, places=12)
        self.assertEqual(rules_c6.m_fill(0.02, -1, 0.75), 0.0)                            # bid floors at zero, fill never negative

    def test_no_credit_skip(self):
        """With a 0.5% index level the spread is worth almost nothing and the $0.03 half-spread floor makes the base-fill
        credit negative: the backtest skips such a day, and so does the model path."""
        e = rules_c6.model_entry(100.0, 0.5, 0.0406, self.D0)
        self.assertEqual(e["skip"], "no_credit")
        self.assertLessEqual(e["ent"]["cf"][cfg.K_BASE], 0)
        self.assertIsNone(rules_c6.model_entry(100.0, 5.0, 0.0406, self.D0)["skip"])

    def test_short_strike_scan_range(self):
        ks = rules_c6.model_short_strike(382.15, 0.0406, 0.2248)
        self.assertGreaterEqual(ks, int(382.15 * 0.8))
        self.assertLessEqual(ks, int(382.15))
        _p, dl = bs_put(382.15, ks, 30 / 365, 0.0406, 0.2248)
        self.assertLess(abs(abs(dl) - 0.25), 0.01)

    def test_mark_and_exit_cash_flow(self):
        e = rules_c6.model_entry(382.15, 22.48, 0.0406, self.D0)
        mark = rules_c6.model_mark(e["Ks"], e["Kl"], 385.0, 21.0, 0.0405, 20)
        self.assertAlmostEqual(mark, 99.43994339, places=5)
        cf = rules_c6.model_exit_cf(e["Ks"], e["Kl"], 385.0, 21.0, 0.0405, 20)
        for k, want in zip(cfg.KS, (-99.43994339, -104.23707967, -109.03421596, -113.83135224)):
            self.assertAlmostEqual(cf[k], want, places=5)
        self.assertAlmostEqual(cf[cfg.K_MID], -mark, places=9)


# --------------------------------------------------------------------------------------------- the stateless model trade
def market_for(d0, closes, idx, r=0.0406, symbol="GLD", index="GVZ"):
    """closes / idx: {date: value}"""
    return md.Market(closes={symbol: closes}, indices={index: idx}, tbill=[(d0 - dt.timedelta(days=5), r * 100 / 100.0)])


def run_days(d0, n=40):
    out, d = [], d0
    while len(out) < n:
        if nyse.is_trading_day(d):
            out.append(d)
        d += dt.timedelta(days=1)
    return out


class ModelTrade(unittest.TestCase):
    D0 = D("2026-10-12")

    def build(self, spot_fn, vol_fn, n=40, r=0.0406):
        days = run_days(self.D0, n)
        closes = {d: spot_fn(i) for i, d in enumerate(days)}
        idx = {d: vol_fn(i) for i, d in enumerate(days)}
        m = md.Market(closes={"GLD": closes}, indices={"GVZ": idx})
        m.tbill.set(self.D0 - dt.timedelta(days=3), r)
        return m, days

    def test_profit_target_exit_matches_an_independent_replay(self):
        m, days = self.build(lambda i: 382.15 + 0.6 * i, lambda i: 22.48 - 0.15 * i)
        res = rules_c6.model_trade(m, "GLD", self.D0, days[-1])
        self.assertEqual(res["status"], "closed")
        self.assertEqual(res["exit"]["reason"], "profit")
        e = res["entry"]
        base = e["ent"]["cf"][cfg.K_BASE]
        expiry = self.D0 + dt.timedelta(days=30)
        first = None
        for i, t in enumerate(days[1:], start=1):                   # independent replay of the exit rule
            dte = (expiry - t).days
            mark = (bs_put(m.close("GLD", t), e["Ks"], dte / 365, 0.0406, m.index_val("GVZ", t) / 100)[0]
                    - bs_put(m.close("GLD", t), e["Kl"], dte / 365, 0.0406, m.index_val("GVZ", t) / 100)[0]) * 100
            if mark <= 0.5 * base:
                first = t
                break
            if dte <= 7:
                first = t
                break
        self.assertEqual(res["exit"]["date"], first)
        self.assertLess(first, expiry - dt.timedelta(days=7))
        # P&L = entry credit + exit cash flow - fees on 4 contracts (2 in, 2 out)
        ex = res["exit"]
        self.assertAlmostEqual(res["pnl"]["pnl"], base + ex["cf"][cfg.K_BASE] - 0.05 * 4, places=6)
        self.assertEqual(res["pnl"]["units"], 4)
        self.assertGreater(res["pnl"]["pnl"], 0)

    def test_dte_exit_when_nothing_moves(self):
        m, days = self.build(lambda i: 382.15, lambda i: 22.48, n=30)
        res = rules_c6.model_trade(m, "GLD", self.D0, days[-1])
        expiry = self.D0 + dt.timedelta(days=30)
        if res["exit"]["reason"] == "dte":
            self.assertLessEqual((expiry - res["exit"]["date"]).days, 7)
            self.assertGreater((expiry - nyse.prev_td(res["exit"]["date"])).days, 7)
        else:                                                         # theta alone reached 50% before 7 DTE: still a valid profit exit
            self.assertEqual(res["exit"]["reason"], "profit")

    def test_loss_runs_to_the_seven_dte_exit(self):
        m, days = self.build(lambda i: 382.15 - 1.2 * i, lambda i: 22.48 + 0.2 * i, n=30)
        res = rules_c6.model_trade(m, "GLD", self.D0, days[-1])
        self.assertEqual(res["status"], "closed")
        self.assertEqual(res["exit"]["reason"], "dte")
        self.assertEqual(res["exit"]["date"], D("2026-11-04"))        # 11 Nov expiry: 7 DTE on Wednesday 4 Nov
        self.assertLess(res["pnl"]["pnl"], 0)

    def test_open_until_the_data_arrives(self):
        m, days = self.build(lambda i: 382.15, lambda i: 22.48, n=6)
        res = rules_c6.model_trade(m, "GLD", self.D0, days[3])
        self.assertEqual(res["status"], "open")
        self.assertEqual(res["last_mark"], days[3])

    def test_pending_when_the_entry_day_has_no_data_yet(self):
        m = md.Market(closes={"GLD": {}}, indices={"GVZ": {}})
        res = rules_c6.model_trade(m, "GLD", self.D0, D("2026-10-09"))
        self.assertEqual(res["status"], "pending")

    def test_no_data_on_a_completed_day_is_a_skip(self):
        m = md.Market(closes={"GLD": {D("2026-10-14"): 380.0}}, indices={"GVZ": {D("2026-10-14"): 22.0}})
        res = rules_c6.model_trade(m, "GLD", self.D0, D("2026-10-14"))
        self.assertEqual((res["status"], res["skip"]), ("skip", "no_data"))
        res_wait = rules_c6.model_trade(m, "GLD", self.D0, D("2026-10-14"), holes="wait", hole_grace=5)
        self.assertEqual(res_wait["status"], "pending")

    def test_a_hole_on_an_interior_day_is_skipped_per_the_study(self):
        m, days = self.build(lambda i: 382.15 - 1.2 * i, lambda i: 22.48 + 0.2 * i, n=30)
        del m.closes["GLD"].values[2]
        m.closes["GLD"].dates.pop(2)
        m.closes["GLD"]._ix = {d: i for i, d in enumerate(m.closes["GLD"].dates)}
        res = rules_c6.model_trade(m, "GLD", self.D0, days[-1])
        self.assertEqual(res["status"], "closed")
        self.assertEqual(res["exit"]["date"], D("2026-11-04"))

    def test_vol_index_selection_per_ticker(self):
        self.assertEqual(cfg.C6["vol_index"], {"GLD": "GVZ", "TLT": "VXTLT"})
        days = run_days(self.D0, 3)
        m = md.Market(closes={"TLT": {d: 78.05 for d in days}}, indices={"VXTLT": {d: 14.58 for d in days}})
        m.tbill.set(self.D0, 0.0406)
        res = rules_c6.model_trade(m, "TLT", self.D0, days[-1])
        self.assertEqual((res["entry"]["Ks"], res["entry"]["Kl"]), (76.0, 74.0))


# --------------------------------------------------------------------------------------------- the real path
ASOF = D("2026-10-13")
EXP = D("2026-11-12")          # 30 DTE
GLD_PUTS = [(372, 3.10, 3.20, -0.33), (370, 2.60, 2.70, -0.29), (368, 2.12, 2.22, -0.25), (366, 1.70, 1.78, -0.215),
            (364, 1.38, 1.45, -0.18), (362, 1.05, 1.12, -0.15), (360, 0.80, 0.86, -0.12), (358, 0.60, 0.65, -0.095)]


def gld_chain(puts=GLD_PUTS, expiry=EXP, spot=382.15):
    return make_chain("GLD", ASOF, spot, rows_from(expiry, -1, puts))


class RealEntry(unittest.TestCase):
    def test_ticket(self):
        res = rules_c6.real_entry(gld_chain(), ASOF, 382.15)
        self.assertIsNone(res["skip"])
        t = res["ticket"]
        self.assertEqual(t["strikes"], {"k_short": 368.0, "k_long": 360.0})          # delta .25 -> 368; 368 - round(7.64) = 360
        self.assertEqual(t["width"], 8.0)
        self.assertEqual(t["expiry"], EXP)
        cf = t["pkg"]["cf"]
        self.assertAlmostEqual(cf[0.0], (2.17 - 0.83) * 100)                       # 134.0
        self.assertAlmostEqual(cf[0.25], (2.17 - 0.025 - 0.83 - 0.015) * 100)       # 130.0
        self.assertAlmostEqual(cf[0.5], (2.12 - 0.86) * 100)                       # 126.0
        self.assertAlmostEqual(t["max_loss"], 800.0 - 130.0 + 0.10)
        self.assertAlmostEqual(t["med_rel_spread"], 0.5 * (0.10 / 2.17 + 0.06 / 0.83), places=9)

    def test_expiry_window(self):
        far = gld_chain(expiry=ASOF + dt.timedelta(days=45))
        self.assertEqual(rules_c6.real_entry(far, ASOF, 382.15)["skip"], "no_expiry")
        near = gld_chain(expiry=ASOF + dt.timedelta(days=22))
        self.assertEqual(rules_c6.real_entry(near, ASOF, 382.15)["skip"], "no_expiry")
        edge = gld_chain(expiry=ASOF + dt.timedelta(days=23))
        self.assertIsNone(rules_c6.real_entry(edge, ASOF, 382.15)["skip"])
        edge2 = gld_chain(expiry=ASOF + dt.timedelta(days=37))
        self.assertIsNone(rules_c6.real_entry(edge2, ASOF, 382.15)["skip"])

    def test_skip_reasons(self):
        self.assertEqual(rules_c6.real_entry(None, ASOF, 382.15)["skip"], "no_chain")
        far_delta = [(k, b, a, -0.5) for (k, b, a, _d) in GLD_PUTS]
        self.assertEqual(rules_c6.real_entry(gld_chain(far_delta), ASOF, 382.15)["skip"], "no_short_strike")
        only_short = [p for p in GLD_PUTS if p[0] == 368]
        res = rules_c6.real_entry(gld_chain(only_short), ASOF, 382.15)
        self.assertEqual(res["skip"], "no_wings")
        wide_long = [p if p[0] != 360 else (360, 0.10, 1.50, -0.12) for p in GLD_PUTS]
        res = rules_c6.real_entry(gld_chain(wide_long), ASOF, 382.15)
        self.assertEqual((res["skip"], res["detail"]["fail"]["reason"]), ("untradeable", "spread"))
        rich_long = [p if p[0] != 360 else (360, 2.50, 2.55, -0.12) for p in GLD_PUTS]
        res = rules_c6.real_entry(gld_chain(rich_long), ASOF, 382.15)
        self.assertEqual(res["skip"], "no_credit")

    def test_the_long_leg_is_the_nearest_listed_strike(self):
        puts = [p for p in GLD_PUTS if p[0] != 360]                  # 360 not listed: 358 and 362 are both 2 away from 360
        res = rules_c6.real_entry(gld_chain(puts), ASOF, 382.15)
        self.assertEqual(res["ticket"]["strikes"]["k_long"], 358.0)  # tie -> the lower strike


class RealExit(unittest.TestCase):
    LEGS = [(-1, 368.0, EXP, -1), (-1, 360.0, EXP, +1)]

    def quotes(self, short_mid, long_mid):
        return [(short_mid - 0.01, short_mid + 0.01), (long_mid - 0.01, long_mid + 0.01)]

    def test_hold_then_profit(self):
        d = ASOF + dt.timedelta(days=5)
        hold = rules_c6.real_exit_decision(self.LEGS, self.quotes(1.80, 0.60), 130.0, EXP, d)       # cost 120 > 65
        self.assertEqual((hold["action"], hold["cost_to_close"]), ("hold", 120.0))
        edge = rules_c6.real_exit_decision(self.LEGS, self.quotes(1.25, 0.60), 130.0, EXP, d)       # cost 65 == 50%
        self.assertEqual((edge["action"], edge["reason"]), ("close", "profit"))
        win = rules_c6.real_exit_decision(self.LEGS, self.quotes(0.90, 0.40), 130.0, EXP, d)         # cost 50
        self.assertEqual((win["action"], win["reason"], round(win["cost_to_close"], 6)), ("close", "profit", 50.0))

    def test_dte_exit(self):
        d = EXP - dt.timedelta(days=7)
        res = rules_c6.real_exit_decision(self.LEGS, self.quotes(1.80, 0.60), 130.0, EXP, d)
        self.assertEqual((res["action"], res["reason"]), ("close", "dte"))
        d8 = EXP - dt.timedelta(days=8)
        self.assertEqual(rules_c6.real_exit_decision(self.LEGS, self.quotes(1.80, 0.60), 130.0, EXP, d8)["action"], "hold")

    def test_profit_label_wins_when_both_apply(self):
        d = EXP - dt.timedelta(days=3)
        res = rules_c6.real_exit_decision(self.LEGS, self.quotes(0.80, 0.40), 130.0, EXP, d)
        self.assertEqual(res["reason"], "profit")

    def test_due_but_not_quoted(self):
        d = EXP - dt.timedelta(days=6)
        self.assertEqual(rules_c6.real_exit_decision(self.LEGS, None, 130.0, EXP, d)["action"], "due_no_quotes")
        self.assertEqual(rules_c6.real_exit_decision(self.LEGS, None, 130.0, EXP, EXP + dt.timedelta(days=1))["action"],
                         "due_no_quotes")
        early = rules_c6.real_exit_decision(self.LEGS, None, 130.0, EXP, ASOF + dt.timedelta(days=2))
        self.assertEqual((early["action"], early["cost_to_close"]), ("hold", None))


if __name__ == "__main__":
    unittest.main()
