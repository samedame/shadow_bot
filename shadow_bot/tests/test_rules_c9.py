"""C9 CONDOR_1D entry rules (rules_c9.py) on hand-made chains. Expected numbers are worked out in the comments."""
import unittest

from tests.helpers import D, bs_rows, make_chain, rows_from

import config as cfg
import fills
import rules_c9

ASOF, EX = D("2026-10-13"), D("2026-10-14")
SPOT = 600.0
CALLS = [(600, 3.00, 3.04, 0.50), (601, 2.40, 2.44, 0.42), (602, 1.85, 1.89, 0.33), (603, 1.35, 1.39, 0.25),
         (604, 0.90, 0.94, 0.16), (605, 0.55, 0.58, 0.10), (606, 0.30, 0.32, 0.06), (608, 0.08, 0.10, 0.02),
         (610, 0.03, 0.05, 0.01), (612, 0.01, 0.03, 0.005)]
PUTS = [(600, 3.00, 3.04, -0.50), (599, 2.40, 2.44, -0.42), (598, 1.85, 1.89, -0.33), (597, 1.35, 1.39, -0.25),
        (596, 0.90, 0.94, -0.16), (595, 0.55, 0.58, -0.10), (594, 0.30, 0.32, -0.06), (592, 0.08, 0.10, -0.02),
        (590, 0.03, 0.05, -0.01), (588, 0.01, 0.03, -0.005)]


def chain(calls=CALLS, puts=PUTS, spot=SPOT, expiry=EX):
    return make_chain("SPY", ASOF, spot, rows_from(expiry, +1, calls) + rows_from(expiry, -1, puts))


class EntryTicket(unittest.TestCase):
    def test_strikes_wings_and_fills(self):
        res = rules_c9.evaluate(chain(), ASOF, EX, True)
        self.assertIsNone(res["skip"])
        t = res["ticket"]
        # shorts: |delta| nearest 0.16 -> 604C and 596P. Wings: 604 + 1% x 600 = 610, 596 - 6 = 590, both listed.
        self.assertEqual(t["strikes"], {"k_call": 604.0, "k_put": 596.0, "k_call_wing": 610.0, "k_put_wing": 590.0})
        self.assertEqual(t["legs"], [(+1, 604.0, EX, -1), (+1, 610.0, EX, +1), (-1, 596.0, EX, -1), (-1, 590.0, EX, +1)])
        cf = t["pkg"]["cf"]
        # per side: sell the short at mid .92 -/+ k x .04, buy the wing at mid .04 +/- k x .02;  two sides
        self.assertAlmostEqual(cf[0.0], 2 * (0.92 - 0.04) * 100)                    # 176.0
        self.assertAlmostEqual(cf[0.25], 2 * ((0.92 - 0.01) - (0.04 + 0.005)) * 100)  # 173.0
        self.assertAlmostEqual(cf[0.5], 2 * (0.90 - 0.05) * 100)                    # 170.0
        self.assertAlmostEqual(cf[0.75], 2 * ((0.92 - 0.03) - (0.04 + 0.015)) * 100)  # 167.0
        self.assertEqual(t["pkg"]["units"], 4)
        self.assertEqual(t["spot"], SPOT)
        self.assertEqual(t["expiry"], EX)
        # worst case: a wing is 6.00 wide -> 600 - 173 credit + 4 x $0.05 entry fees
        self.assertAlmostEqual(t["max_loss"], 600.0 - 173.0 + 0.20)
        self.assertAlmostEqual(t["med_rel_spread"], 0.5 * (0.04 / 0.92 + 0.5), places=9)

    def test_settlement_outcomes(self):
        t = rules_c9.evaluate(chain(), ASOF, EX, True)["ticket"]
        ent = {"cf": t["pkg"]["cf"], "units": 4}
        for px, pnl in ((600.0, 172.8), (604.0, 172.8), (605.0, 72.8), (610.0, -427.2), (700.0, -427.2), (596.0, 172.8),
                        (595.0, 72.8), (594.0, -27.2), (590.0, -427.2), (400.0, -427.2)):
            ext = rules_c9.settle(t["legs"], px, EX)
            self.assertEqual(ext["units"], 0)
            self.assertEqual(ext["d_exit"], EX)
            rec = fills.assemble(t["legs"], ent, ext)
            self.assertAlmostEqual(rec["pnl"], pnl, places=6, msg=f"settle at {px}")

    def test_a_tie_in_delta_goes_to_the_lower_strike(self):
        # .3125 and .1875 are both exactly .0625 from .25 in binary floating point (the tolerance is .10)
        calls = [(603, 1.35, 1.39, 0.3125), (604, 0.90, 0.94, 0.1875), (610, 0.03, 0.05, 0.01)]
        res = rules_c9.evaluate(chain(calls=calls), ASOF, EX, True, delta_t=0.25)
        self.assertEqual(res["ticket"]["strikes"]["k_call"], 603.0)

    def test_gate_states(self):
        self.assertEqual(rules_c9.evaluate(chain(), ASOF, EX, None)["skip"], "no_gate_data")
        self.assertEqual(rules_c9.evaluate(chain(), ASOF, EX, False)["skip"], "gate_closed")
        self.assertEqual(rules_c9.evaluate(None, ASOF, EX, True)["skip"], "no_chain")
        self.assertEqual(rules_c9.evaluate(chain(), ASOF, None, True)["skip"], "no_calendar")
        # the gate is looked at before the chain: a closed gate hides a missing chain
        self.assertEqual(rules_c9.evaluate(None, ASOF, EX, False)["skip"], "gate_closed")


class SkipReasons(unittest.TestCase):
    def test_no_one_day_expiry(self):
        c = chain(expiry=D("2026-10-16"))
        res = rules_c9.evaluate(c, ASOF, EX, True)
        self.assertEqual((res["skip"], res["detail"]["want"]), ("no_1dte", "2026-10-14"))

    def test_no_short_strike_when_no_delta_is_near(self):
        calls = [(c[0], c[1], c[2], 0.5) for c in CALLS]                  # every call is 0.50 delta: miss 0.34
        self.assertEqual(rules_c9.evaluate(chain(calls=calls), ASOF, EX, True)["skip"], "no_short_strike")
        no_puts = rules_c9.evaluate(chain(puts=[]), ASOF, EX, True)
        self.assertEqual(no_puts["skip"], "no_short_strike")

    def test_no_wings_when_the_short_is_the_outermost_strike(self):
        calls = [c for c in CALLS if c[0] <= 604]
        res = rules_c9.evaluate(chain(calls=calls), ASOF, EX, True)
        self.assertEqual(res["skip"], "no_wings")
        self.assertEqual(res["detail"]["call_wing"], 604.0)

    def test_untradeable_when_a_wing_is_illiquid(self):
        calls = [c if c[0] != 610 else (610, 0.00, 0.20, 0.01) for c in CALLS]        # $0.20 spread on a $0.10 mid
        res = rules_c9.evaluate(chain(calls=calls), ASOF, EX, True)
        self.assertEqual(res["skip"], "untradeable")
        self.assertEqual((res["detail"]["fail"]["reason"], res["detail"]["fail"]["strike"]), ("spread", 610.0))
        self.assertIsNotNone(res["detail"]["med_rel_spread"])

    def test_no_credit(self):
        calls = [c if c[0] != 610 else (610, 0.95, 1.00, 0.01) for c in CALLS]        # the wing costs more than the short earns
        puts = [p if p[0] != 590 else (590, 0.95, 1.00, -0.01) for p in PUTS]
        res = rules_c9.evaluate(chain(calls=calls, puts=puts), ASOF, EX, True)
        self.assertEqual(res["skip"], "no_credit")
        self.assertLess(res["detail"]["credit_base"], 0)

    def test_all_four_legs_must_pass_the_liquidity_filter(self):
        puts = [p if p[0] != 590 else (590, 0.00, 0.30, -0.01) for p in PUTS]
        self.assertEqual(rules_c9.evaluate(chain(puts=puts), ASOF, EX, True)["skip"], "untradeable")


class AltSelectionDiagnostic(unittest.TestCase):
    def test_black_scholes_deltas_from_the_feed_iv(self):
        rows = bs_rows(SPOT, ASOF, [EX], [float(k) for k in range(560, 641)], vol=0.14, r=0.0406, rel=0.02)
        c = make_chain("SPY", ASOF, SPOT, rows)
        res = rules_c9.evaluate(c, ASOF, EX, True, alt_ctx={"r": 0.0406})
        self.assertIsNone(res["skip"], res["detail"])
        alt = res["ticket"]["alt"]
        self.assertIn("differs", alt)
        # the chain's own deltas are Black-Scholes at q=0, so the two selections agree to within a strike
        self.assertLessEqual(abs(alt["call"] - alt["feed_call"]), 1.0)
        self.assertLessEqual(abs(alt["put"] - alt["feed_put"]), 1.0)

    def test_no_alt_context_no_alt(self):
        self.assertIsNone(rules_c9.evaluate(chain(), ASOF, EX, True)["ticket"]["alt"])


class RealisticChain(unittest.TestCase):
    """A Black-Scholes chain with 1-DTE structure: the picks land where the 0.16 delta is, and the package prices sensibly."""

    def test_condor_on_a_modelled_chain(self):
        rows = bs_rows(SPOT, ASOF, [EX], [float(k) for k in range(560, 641)], vol=0.14, r=0.0406, rel=0.03, floor=0.01)
        c = make_chain("SPY", ASOF, SPOT, rows)
        res = rules_c9.evaluate(c, ASOF, EX, True)
        self.assertIsNone(res["skip"], res["detail"])
        t = res["ticket"]
        s = t["strikes"]
        self.assertGreater(s["k_call"], SPOT)
        self.assertLess(s["k_put"], SPOT)
        self.assertAlmostEqual(s["k_call_wing"] - s["k_call"], 6.0, delta=1.0)        # 1% of 600, listed at $1 steps
        self.assertAlmostEqual(s["k_put"] - s["k_put_wing"], 6.0, delta=1.0)
        deltas = [c.row(EX, +1, s["k_call"])["delta"], -c.row(EX, -1, s["k_put"])["delta"]]
        for dl in deltas:
            self.assertLess(abs(dl - 0.16), 0.05)
        self.assertGreater(t["pkg"]["cf"][cfg.K_BASE], 0)
        self.assertGreater(t["pkg"]["cf"][cfg.K_MID], t["pkg"]["cf"][cfg.K_BASE], "slippage lowers the credit")
        self.assertGreater(t["pkg"]["cf"][cfg.K_BASE], t["pkg"]["cf"][cfg.K_NAT])
        self.assertGreater(t["pkg"]["cf"][cfg.K_NAT], t["pkg"]["cf"][cfg.K_X3])


if __name__ == "__main__":
    unittest.main()
