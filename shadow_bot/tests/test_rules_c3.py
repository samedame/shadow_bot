"""C3 SPY_HEDGE rules (rules_c3.py): the gate and the monthly put-debit-spread ticket."""
import datetime as dt
import unittest

from tests.helpers import D, make_chain, rows_from

import config as cfg
import fills
import rules_c3

ASOF = D("2026-11-02")
EXP = ASOF + dt.timedelta(days=60)
SPOT = 700.0
PUTS = [(590, 1.05, 1.10, -0.045), (595, 1.20, 1.25, -0.05), (600, 1.40, 1.46, -0.055), (655, 5.60, 5.70, -0.18),
        (660, 6.80, 6.90, -0.20), (665, 7.60, 7.70, -0.22), (670, 8.50, 8.62, -0.245), (675, 9.60, 9.72, -0.27)]


def chain(puts=PUTS, expiry=EXP, spot=SPOT, extra_rows=()):
    return make_chain("SPY", ASOF, spot, rows_from(expiry, -1, puts) + list(extra_rows))


class Gate(unittest.TestCase):
    def test_open(self):
        ok, info = rules_c3.gate(14.0, 17.0, 14.5)
        self.assertTrue(ok)
        self.assertAlmostEqual(info["ratio"], 14.0 / 17.0)
        self.assertTrue(info["ratio_ok"] and info["level_ok"])

    def test_ratio_must_be_strictly_below_point_nine(self):
        self.assertFalse(rules_c3.gate(18.0, 20.0, 99.0)[0])            # exactly 0.90 (18/20 == 0.9 in floats)
        self.assertTrue(rules_c3.gate(17.98, 20.0, 99.0)[0])
        self.assertFalse(rules_c3.gate(18.02, 20.0, 99.0)[0])
        self.assertEqual(rules_c3.gate(18.0, 20.0, 99.0)[1]["ratio_ok"], False)

    def test_level_may_equal_the_quantile(self):
        self.assertTrue(rules_c3.gate(14.0, 17.0, 14.0)[0])             # <= q25
        self.assertFalse(rules_c3.gate(14.01, 17.0, 14.0)[0])

    def test_both_conditions_are_needed(self):
        ok, info = rules_c3.gate(14.0, 15.0, 20.0)                       # ratio .933 fails, level passes
        self.assertFalse(ok)
        self.assertEqual((info["ratio_ok"], info["level_ok"]), (False, True))
        ok, info = rules_c3.gate(14.0, 17.0, 10.0)                       # ratio passes, level fails
        self.assertFalse(ok)
        self.assertEqual((info["ratio_ok"], info["level_ok"]), (True, False))

    def test_missing_inputs_never_pass(self):
        self.assertEqual(rules_c3.gate(None, 17.0, 14.0)[1]["why"], "missing_index")
        self.assertEqual(rules_c3.gate(14.0, None, 14.0)[1]["why"], "missing_index")
        self.assertEqual(rules_c3.gate(14.0, 0.0, 14.0)[1]["why"], "missing_index")
        ok, info = rules_c3.gate(14.0, 17.0, None)
        self.assertFalse(ok)
        self.assertEqual(info["why"], "no_quantile")


class Ticket(unittest.TestCase):
    def test_strikes_expiry_and_fills(self):
        res = rules_c3.build_ticket(chain(), ASOF)
        self.assertIsNone(res["skip"])
        t = res["ticket"]
        self.assertEqual(t["expiry"], EXP)
        self.assertEqual(t["strikes"], {"k_long": 665.0, "k_short": 595.0})         # nearest to .95 x 700 and .85 x 700
        self.assertEqual(t["legs"], [(-1, 665.0, EXP, +1), (-1, 595.0, EXP, -1)])
        cf = t["pkg"]["cf"]
        self.assertAlmostEqual(cf[0.0], (-7.65 + 1.225) * 100)                      # -642.5
        self.assertAlmostEqual(cf[0.25], (-(7.65 + 0.025) + (1.225 - 0.0125)) * 100)  # -646.25
        self.assertAlmostEqual(cf[0.5], (-7.70 + 1.20) * 100)                       # -650.0
        self.assertAlmostEqual(t["net_debit_base"], 646.25)
        self.assertAlmostEqual(t["max_loss"], 646.25 + 0.10)                        # the debit plus 2 x $0.05 entry fees

    def test_expiry_is_the_listed_one_closest_to_sixty(self):
        rows = []
        for n in (49, 56, 63, 76):
            rows += rows_from(ASOF + dt.timedelta(days=n), -1, PUTS)
        c = make_chain("SPY", ASOF, SPOT, rows)
        res = rules_c3.build_ticket(c, ASOF)
        self.assertEqual(res["ticket"]["expiry"], ASOF + dt.timedelta(days=63))      # |56-60| == 4 > |63-60| == 3
        rows = rows_from(ASOF + dt.timedelta(days=56), -1, PUTS) + rows_from(ASOF + dt.timedelta(days=64), -1, PUTS)
        res = rules_c3.build_ticket(make_chain("SPY", ASOF, SPOT, rows), ASOF)
        self.assertEqual(res["ticket"]["expiry"], ASOF + dt.timedelta(days=56))      # tie 4 vs 4 -> the shorter

    def test_skip_reasons(self):
        self.assertEqual(rules_c3.build_ticket(None, ASOF)["skip"], "no_chain")
        self.assertEqual(rules_c3.build_ticket(chain(expiry=ASOF + dt.timedelta(days=45)), ASOF)["skip"], "no_expiry")
        self.assertEqual(rules_c3.build_ticket(chain(expiry=ASOF + dt.timedelta(days=80)), ASOF)["skip"], "no_expiry")
        self.assertEqual(rules_c3.build_ticket(chain(puts=[PUTS[0]]), ASOF)["skip"], "no_strikes")
        wide = [p if p[0] != 665 else (665, 5.00, 9.00, -0.22) for p in PUTS]
        res = rules_c3.build_ticket(chain(puts=wide), ASOF)
        self.assertEqual((res["skip"], res["detail"]["fail"]["reason"]), ("untradeable", "spread"))
        credit = [p if p[0] != 665 else (665, 0.40, 0.45, -0.22) for p in PUTS]        # long leg cheaper than the short: a credit
        credit = [p if p[0] != 595 else (595, 5.00, 5.05, -0.05) for p in credit]
        res = rules_c3.build_ticket(chain(puts=credit), ASOF)
        self.assertEqual(res["skip"], "no_debit")
        self.assertGreater(res["detail"]["cf_base"], 0)

    def test_the_short_leg_needs_a_bid(self):
        nobid = [p if p[0] != 595 else (595, 0.0, 0.05, -0.05) for p in PUTS]
        res = rules_c3.build_ticket(chain(puts=nobid), ASOF)
        self.assertEqual((res["skip"], res["detail"]["fail"]["reason"]), ("untradeable", "zero_bid"))

    def test_the_long_leg_may_have_no_bid(self):
        res = rules_c3.build_ticket(chain(puts=[(665, 0.0, 0.05, -0.22), (595, 0.01, 0.02, -0.05)]), ASOF)
        self.assertIsNone(res["skip"], res["detail"])
        self.assertAlmostEqual(res["ticket"]["net_debit_base"], 2.5)             # buy at .025 + .0125, sell at .015 - .0025

    def test_the_hedge_pays_off_in_a_selloff(self):
        t = rules_c3.build_ticket(chain(), ASOF)["ticket"]
        ent = {"cf": t["pkg"]["cf"], "units": t["pkg"]["units"]}
        quotes = [(60.0, 60.4), (2.0, 2.2)]                     # SPY fell hard: 665P deep in the money
        ext = rules_c3.close_at(t["legs"], quotes, ASOF + dt.timedelta(days=39))
        self.assertEqual(ext["units"], 2)
        rec = fills.assemble(t["legs"], ent, ext)
        # exit: sell 665P at 60.20 - .25 x .40 = 60.10; buy back 595P at 2.10 + .25 x .20 = 2.15  -> +5795.0
        self.assertAlmostEqual(ext["cf"][cfg.K_BASE], (60.10 - 2.15) * 100)
        self.assertAlmostEqual(rec["pnl"], -646.25 + 5795.0 - 0.05 * 4)
        self.assertGreater(rec["pnl"], 0)


class Exit(unittest.TestCase):
    def test_exit_due_boundaries(self):
        exp = D("2027-01-15")
        self.assertFalse(rules_c3.exit_due(exp, exp - dt.timedelta(days=22)))
        self.assertTrue(rules_c3.exit_due(exp, exp - dt.timedelta(days=21)))        # DTE <= 21
        self.assertTrue(rules_c3.exit_due(exp, exp - dt.timedelta(days=5)))
        self.assertTrue(rules_c3.exit_due(exp, exp))
        self.assertTrue(rules_c3.exit_due(exp, exp + dt.timedelta(days=2)))

    def test_the_exit_session_for_a_sixty_day_hedge(self):
        """Entered Monday 2 Nov 2026 with a 60-day expiry (1 Jan 2027, a holiday, so the listed expiry would be 31 Dec): the
        first session with DTE <= 21 is Thursday 10 Dec."""
        exp = D("2026-12-31")
        import nyse_calendar as nyse
        t = D("2026-11-03")
        while not rules_c3.exit_due(exp, t) or not nyse.is_trading_day(t):
            t += dt.timedelta(days=1)
        self.assertEqual(t, D("2026-12-10"))


if __name__ == "__main__":
    unittest.main()
