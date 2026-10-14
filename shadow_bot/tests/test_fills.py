"""Fills, fees, settlement, closing, max loss and the P&L record (fills.py). Every expected number below is worked out by hand
in the comment next to it."""
import unittest

from tests.helpers import D, make_chain, rows_from

import config as cfg
import fills

EX = D("2026-10-30")
# A bull put spread: SELL 100P (bid 1.00 ask 1.10), BUY 98P (bid 0.40 ask 0.50)
PUT_SPREAD_LEGS = [(-1, 100.0, EX, -1), (-1, 98.0, EX, +1)]
PUT_SPREAD_ROWS = rows_from(EX, -1, [(100, 1.00, 1.10, -0.25), (98, 0.40, 0.50, -0.15)])


def put_spread_chain():
    return make_chain("TST", D("2026-10-13"), 105.0, PUT_SPREAD_ROWS)


class FillPrices(unittest.TestCase):
    def test_buy_and_sell(self):
        self.assertAlmostEqual(fills.fill(1.00, 1.10, +1, 0.25), 1.075)     # mid 1.05 + 25% of the .10 spread
        self.assertAlmostEqual(fills.fill(1.00, 1.10, -1, 0.25), 1.025)
        self.assertAlmostEqual(fills.fill(1.00, 1.10, +1, 0.5), 1.10)       # natural: the ask
        self.assertAlmostEqual(fills.fill(1.00, 1.10, -1, 0.5), 1.00)       # natural: the bid
        self.assertAlmostEqual(fills.fill(1.00, 1.10, +1, 0.0), 1.05)       # mid
        self.assertAlmostEqual(fills.fill(1.00, 1.10, +1, 0.75), 1.125)

    def test_never_negative(self):
        self.assertEqual(fills.fill(0.00, 0.02, -1, 0.75), 0.0)             # 0.01 - 0.015 clipped

    def test_intrinsic(self):
        self.assertEqual(fills.intrinsic(+1, 100.0, 103.0), 3.0)
        self.assertEqual(fills.intrinsic(+1, 100.0, 97.0), 0.0)
        self.assertEqual(fills.intrinsic(-1, 100.0, 97.0), 3.0)
        self.assertEqual(fills.intrinsic(-1, 100.0, 103.0), 0.0)


class EntryPackage(unittest.TestCase):
    def test_put_credit_spread_cash_flows(self):
        pkg, fail = fills.price_entry(put_spread_chain(), PUT_SPREAD_LEGS)
        self.assertIsNone(fail)
        cf = pkg["cf"]
        # sell 100P, buy 98P, x100 shares:  mid (1.05 - 0.45), base (1.025 - 0.475), natural (1.00 - 0.50), x3 (0.975 - 0.525)
        self.assertAlmostEqual(cf[0.0], 60.0)
        self.assertAlmostEqual(cf[0.25], 55.0)
        self.assertAlmostEqual(cf[0.5], 50.0)
        self.assertAlmostEqual(cf[0.75], 45.0)
        self.assertEqual(pkg["units"], 2)
        self.assertAlmostEqual(pkg["mid_value"], -60.0)        # worth -$60 to the holder (short the spread)
        self.assertEqual(pkg["quotes"], [(1.00, 1.10), (0.40, 0.50)])

    def test_failure_reasons(self):
        c = put_spread_chain()
        pkg, fail = fills.price_entry(c, [(-1, 100.0, EX, -1), (-1, 97.0, EX, +1)])
        self.assertIsNone(pkg)
        self.assertEqual((fail["reason"], fail["leg"], fail["strike"]), ("missing_quote", 1, 97.0))
        wide = make_chain("TST", D("2026-10-13"), 105.0, rows_from(EX, -1, [(100, 1.00, 1.60, -0.25), (98, 0.40, 0.50, -0.15)]))
        pkg, fail = fills.price_entry(wide, PUT_SPREAD_LEGS)
        self.assertEqual((pkg, fail["reason"], fail["leg"]), (None, "spread", 0))
        nobid = make_chain("TST", D("2026-10-13"), 105.0, rows_from(EX, -1, [(100, 0.0, 0.05, -0.25), (98, 0.0, 0.05, -0.15)]))
        pkg, fail = fills.price_entry(nobid, PUT_SPREAD_LEGS)
        self.assertEqual((pkg, fail["reason"], fail["leg"]), (None, "zero_bid", 0))          # the short leg needs a bid
        ok_long = fills.price_entry(nobid, [(-1, 100.0, EX, +1)])[0]
        self.assertIsNotNone(ok_long)                                                          # a long leg with no bid is fine

    def test_quote_all_ignores_liquidity(self):
        wide = make_chain("TST", D("2026-10-13"), 105.0, rows_from(EX, -1, [(100, 0.10, 5.00, -0.25)]))
        self.assertEqual(fills.quote_all(wide, [(-1, 100.0, EX, -1)]), [(0.10, 5.00)])
        self.assertIsNone(fills.quote_all(wide, [(-1, 99.0, EX, -1)]))


class ExitAndSettlement(unittest.TestCase):
    CONDOR = [(+1, 104.0, EX, -1), (+1, 110.0, EX, +1), (-1, 96.0, EX, -1), (-1, 90.0, EX, +1)]

    def test_settlement_cash_flow(self):
        for px, expect in ((100.0, 0.0), (104.0, 0.0), (106.0, -200.0), (110.0, -600.0), (130.0, -600.0),
                           (96.0, 0.0), (93.0, -300.0), (90.0, -600.0), (50.0, -600.0)):
            cf = fills.settle_cf(self.CONDOR, px)
            self.assertAlmostEqual(cf[0.25], expect, msg=str(px))
            self.assertEqual(len(set(cf.values())), 1)                  # intrinsic: the same for every fill assumption

    def test_close_at_quotes(self):
        quotes = [(0.55, 0.65), (0.15, 0.25)]
        cf = fills.close_cf(PUT_SPREAD_LEGS, quotes)
        # buy back the short 100P at mid .60 + k x .10; sell the long 98P at mid .20 - k x .10
        self.assertAlmostEqual(cf[0.0], -40.0)          # (-0.60 + 0.20) x 100
        self.assertAlmostEqual(cf[0.25], -45.0)         # (-0.625 + 0.175)
        self.assertAlmostEqual(cf[0.5], -50.0)          # (-0.65 + 0.15)
        self.assertAlmostEqual(cf[0.75], -55.0)         # (-0.675 + 0.125)

    def test_mid_mark(self):
        self.assertAlmostEqual(fills.mid_mark(PUT_SPREAD_LEGS, [(0.55, 0.65), (0.15, 0.25)]), -40.0)


class MaxLoss(unittest.TestCase):
    def test_condor(self):
        legs = [(+1, 104.0, EX, -1), (+1, 110.0, EX, +1), (-1, 96.0, EX, -1), (-1, 90.0, EX, +1)]
        self.assertAlmostEqual(fills.max_loss(legs, 173.0), 600.0 - 173.0)       # wider wing 6.00 x 100, less the credit

    def test_unequal_wings_use_the_wider(self):
        legs = [(+1, 104.0, EX, -1), (+1, 108.0, EX, +1), (-1, 96.0, EX, -1), (-1, 90.0, EX, +1)]
        self.assertAlmostEqual(fills.max_loss(legs, 100.0), 600.0 - 100.0)       # put wing 6 > call wing 4

    def test_put_credit_spread(self):
        self.assertAlmostEqual(fills.max_loss(PUT_SPREAD_LEGS, 55.0), 200.0 - 55.0)

    def test_put_debit_spread_is_the_net_debit(self):
        legs = [(-1, 95.0, EX, +1), (-1, 85.0, EX, -1)]
        self.assertAlmostEqual(fills.max_loss(legs, -310.0), 310.0)

    def test_naked_short_call_is_unbounded(self):
        self.assertEqual(fills.max_loss([(+1, 100.0, EX, -1)], 50.0), float("inf"))

    def test_mixed_expiries_fall_back_to_the_debit(self):
        legs = [(-1, 100.0, EX, +1), (-1, 100.0, D("2026-11-30"), -1)]
        self.assertAlmostEqual(fills.max_loss(legs, -40.0), 40.0)

    def test_a_credit_that_exceeds_the_risk_floors_at_zero(self):
        self.assertEqual(fills.max_loss(PUT_SPREAD_LEGS, 250.0), 0.0)


class PnlRecord(unittest.TestCase):
    def test_assemble_a_closed_put_spread(self):
        pkg, _ = fills.price_entry(put_spread_chain(), PUT_SPREAD_LEGS)
        ent = {"cf": pkg["cf"], "units": pkg["units"]}
        ext = {"cf": fills.close_cf(PUT_SPREAD_LEGS, [(0.55, 0.65), (0.15, 0.25)]), "units": 2}
        rec = fills.assemble(PUT_SPREAD_LEGS, ent, ext)
        self.assertEqual(rec["units"], 4)
        self.assertAlmostEqual(rec["pnl_mid"], 20.0)            # 60 - 40, no fees
        self.assertAlmostEqual(rec["pnl"], 55.0 - 45.0 - 0.05 * 4)          # 9.80
        self.assertAlmostEqual(rec["pnl_nat"], 50.0 - 50.0 - 0.05 * 4)      # -0.20
        self.assertAlmostEqual(rec["pnl_x3"], 45.0 - 55.0 - 0.05 * 4)       # -10.20
        self.assertAlmostEqual(rec["pnl_f65"], 55.0 - 45.0 - 0.65 * 4)      # 7.40
        self.assertAlmostEqual(rec["entry_slip"], 5.0)
        self.assertAlmostEqual(rec["max_loss"], 200.0 - 55.0 + 0.05 * 2)    # 145.10

    def test_expiry_settlement_has_no_closing_fee(self):
        legs = [(+1, 104.0, EX, -1), (+1, 110.0, EX, +1), (-1, 96.0, EX, -1), (-1, 90.0, EX, +1)]
        ent = {"cf": {k: 173.0 for k in cfg.KS}, "units": 4}
        rec = fills.assemble(legs, ent, {"cf": fills.settle_cf(legs, 100.0), "units": 0})
        self.assertAlmostEqual(rec["pnl"], 173.0 - 0.05 * 4)
        rec = fills.assemble(legs, ent, {"cf": fills.settle_cf(legs, 106.0), "units": 0})
        self.assertAlmostEqual(rec["pnl"], 173.0 - 200.0 - 0.05 * 4)
        rec = fills.assemble(legs, ent, {"cf": fills.settle_cf(legs, 150.0), "units": 0})
        self.assertAlmostEqual(rec["pnl"], -rec["max_loss"])                # the worst case equals the stated max loss

    def test_leg_label(self):
        self.assertEqual(fills.leg_label(+1, 735.0, -1, D("2026-10-15")), "SELL 1 735C 2026-10-15")
        self.assertEqual(fills.leg_label(-1, 350.5, +1, D("2026-11-12")), "BUY 1 350.5P 2026-11-12")


if __name__ == "__main__":
    unittest.main()
