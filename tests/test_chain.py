"""Chain model, liquidity filter, strike / expiry selection helpers and the Cboe parser (chain.py)."""
import datetime as dt
import unittest

from tests.helpers import D, ET, UTC, cboe_options_obj, et, make_chain, rows_from

import chain as ch


def tiny_chain(rows, spot=100.0, asof=D("2026-10-13")):
    return make_chain("TST", asof, spot, rows)


class OccSymbols(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(ch.parse_occ("SPY261015C00735000"), ("SPY", D("2026-10-15"), +1, 735.0))
        self.assertEqual(ch.parse_occ("GLD261112P00350500"), ("GLD", D("2026-11-12"), -1, 350.5))
        self.assertEqual(ch.parse_occ("TLT261014P00064000"), ("TLT", D("2026-10-14"), -1, 64.0))
        self.assertEqual(ch.parse_occ("SPXW261013C06800000")[0], "SPXW")

    def test_garbage_is_none(self):
        for bad in ("", None, "SPY", "SPY261340C00735000", "SPY26101XC00735000", "spy261015C00735000"):
            self.assertIsNone(ch.parse_occ(bad), bad)


class LiquidityFilter(unittest.TestCase):
    def test_spread_limit_is_max_of_five_cents_and_thirty_percent(self):
        self.assertIsNone(ch.liq_reason(1.00, 1.05, False))           # spread .05 <= max(.05, .3075)
        self.assertIsNone(ch.liq_reason(1.00, 1.30, False))           # .30 <= .345
        self.assertEqual(ch.liq_reason(1.00, 1.40, False), "spread")  # .40 > .36
        self.assertIsNone(ch.liq_reason(0.00, 0.05, False))           # .05 <= max(.05, .0075): the absolute floor
        self.assertEqual(ch.liq_reason(0.00, 0.06, False), "spread")
        self.assertIsNone(ch.liq_reason(10.0, 13.0, False))           # .30 of mid 11.5 = 3.45 > 3.0
        self.assertEqual(ch.liq_reason(10.0, 14.0, False), "spread")  # 4.0 > 3.6

    def test_exactly_at_the_limit_passes(self):
        self.assertIsNone(ch.liq_reason(0.95, 1.05, False))           # spread 0.10, mid 1.00 -> limit 0.30
        self.assertIsNone(ch.liq_reason(1.00, 1.05, False))
        self.assertIsNone(ch.liq_reason(0.50, 0.55, False))           # spread exactly the $0.05 floor

    def test_short_leg_needs_a_bid(self):
        self.assertEqual(ch.liq_reason(0.0, 0.05, True), "zero_bid")
        self.assertIsNone(ch.liq_reason(0.0, 0.05, False))
        self.assertIsNone(ch.liq_reason(0.01, 0.05, True))

    def test_crossed_or_dead_quotes(self):
        self.assertEqual(ch.liq_reason(1.10, 1.00, False), "crossed")
        self.assertEqual(ch.liq_reason(0.0, 0.0, False), "crossed")
        self.assertEqual(ch.liq_reason(-0.1, 0.1, False), "crossed")
        self.assertTrue(ch.liq_ok(1.0, 1.02, True))
        self.assertFalse(ch.liq_ok(1.0, 2.0, True))

    def test_clean_filter(self):
        self.assertTrue(ch.clean_ok(1.0, 1.1, 100.0, 100.0, 5))
        self.assertFalse(ch.clean_ok(1.0, 1.1, 100.0, 100.0, 131))        # beyond 130 DTE
        self.assertTrue(ch.clean_ok(1.0, 1.1, 100.0, 100.0, 130))
        self.assertFalse(ch.clean_ok(1.0, 1.1, 100.0, 100.0, -1))
        self.assertFalse(ch.clean_ok(1.0, 1.1, 136.0, 100.0, 5))           # more than 35% away
        self.assertTrue(ch.clean_ok(1.0, 1.1, 134.0, 100.0, 5))
        self.assertFalse(ch.clean_ok(1.2, 1.1, 100.0, 100.0, 5))           # crossed
        self.assertFalse(ch.clean_ok(0.0, 0.0, 100.0, 100.0, 5))          # no ask
        self.assertFalse(ch.clean_ok(None, 1.0, 100.0, 100.0, 5))


class DeltaPicks(unittest.TestCase):
    EX = D("2026-10-14")

    def block(self, table, cp=+1):
        return tiny_chain(rows_from(self.EX, cp, table))

    def test_closest_delta_wins(self):
        c = self.block([(101, 1.00, 1.02, 0.45), (102, 0.60, 0.62, 0.30), (103, 0.30, 0.32, 0.18), (104, 0.10, 0.12, 0.09)])
        self.assertEqual(ch.pick_delta_strike(c, self.EX, +1, 0.16), 103.0)

    def test_puts_use_absolute_delta(self):
        c = self.block([(99, 0.60, 0.62, -0.30), (98, 0.30, 0.32, -0.17), (97, 0.10, 0.12, -0.09)], cp=-1)
        self.assertEqual(ch.pick_delta_strike(c, self.EX, -1, 0.16), 98.0)

    def test_exact_tie_goes_to_the_lower_strike(self):
        """deltas .375 and .125 are both exactly .125 from .25 in binary floating point: the first minimum (lower strike) wins."""
        c = self.block([(100, 1.0, 1.02, 0.375), (101, 0.5, 0.52, 0.125)])
        self.assertEqual(ch.pick_delta_strike(c, self.EX, +1, 0.25, tol=0.2), 100.0)

    def test_tolerance_is_inclusive(self):
        c = self.block([(100, 1.0, 1.02, 0.125)])
        self.assertEqual(ch.pick_delta_strike(c, self.EX, +1, 0.25, tol=0.125), 100.0)       # miss == tol passes
        self.assertIsNone(ch.pick_delta_strike(c, self.EX, +1, 0.25, tol=0.1249))
        far = self.block([(100, 1.0, 1.02, 0.40)])
        self.assertIsNone(ch.pick_delta_strike(far, self.EX, +1, 0.16))                       # miss .24 > .10

    def test_default_tolerance_is_ten_points(self):
        near = self.block([(100, 1.0, 1.02, 0.26)])
        self.assertEqual(ch.pick_delta_strike(near, self.EX, +1, 0.16), 100.0)                # miss .10 (float: just under)
        over = self.block([(100, 1.0, 1.02, 0.2601)])
        self.assertIsNone(ch.pick_delta_strike(over, self.EX, +1, 0.16))

    def test_illiquid_best_delta_is_skipped(self):
        c = self.block([(103, 0.10, 0.60, 0.16), (104, 0.30, 0.32, 0.12)])     # 103 has a $0.50 spread on a $0.35 mid
        self.assertEqual(ch.pick_delta_strike(c, self.EX, +1, 0.16), 104.0)

    def test_short_leg_needs_a_bid(self):
        c = self.block([(103, 0.0, 0.05, 0.16), (104, 0.02, 0.05, 0.12)])
        self.assertEqual(ch.pick_delta_strike(c, self.EX, +1, 0.16, short=True), 104.0)
        self.assertEqual(ch.pick_delta_strike(c, self.EX, +1, 0.16, short=False), 103.0)

    def test_missing_delta_rows_are_ignored(self):
        c = self.block([(103, 0.30, 0.32, None), (104, 0.30, 0.32, 0.14)])
        self.assertEqual(ch.pick_delta_strike(c, self.EX, +1, 0.16), 104.0)
        none_at_all = self.block([(103, 0.30, 0.32, None)])
        self.assertIsNone(ch.pick_delta_strike(none_at_all, self.EX, +1, 0.16))

    def test_no_block(self):
        c = self.block([(103, 0.30, 0.32, 0.16)])
        self.assertIsNone(ch.pick_delta_strike(c, D("2026-10-20"), +1, 0.16))
        self.assertIsNone(ch.pick_delta_strike(c, self.EX, -1, 0.16))

    def test_deltas_override(self):
        c = self.block([(103, 0.30, 0.32, 0.30), (104, 0.30, 0.32, 0.30)])
        self.assertEqual(ch.pick_delta_strike(c, self.EX, +1, 0.16, deltas=[0.30, 0.16]), 104.0)


class StrikeAndExpiryPicks(unittest.TestCase):
    EX = D("2026-10-14")

    def test_nearest_strike_tie_goes_lower(self):
        c = tiny_chain(rows_from(self.EX, -1, [(100, 1, 1.02, -0.5), (101, 1, 1.02, -0.4), (102, 1, 1.02, -0.3)]))
        self.assertEqual(ch.nearest_strike(c, self.EX, -1, 100.5), 100.0)
        self.assertEqual(ch.nearest_strike(c, self.EX, -1, 100.51), 101.0)
        self.assertEqual(ch.nearest_strike(c, self.EX, -1, 50.0), 100.0)
        self.assertEqual(ch.nearest_strike(c, self.EX, -1, 500.0), 102.0)
        self.assertIsNone(ch.nearest_strike(c, self.EX, +1, 100.0))                 # no call block
        self.assertIsNone(ch.nearest_strike(c, D("2026-12-01"), -1, 100.0))

    def test_nearest_strike_counts_illiquid_strikes_too(self):
        c = tiny_chain(rows_from(self.EX, -1, [(100, 0.0, 5.0, -0.5), (101, 1, 1.02, -0.4)]))
        self.assertEqual(ch.nearest_strike(c, self.EX, -1, 100.0), 100.0)

    def test_pick_expiry(self):
        asof = D("2026-10-13")
        exps = [asof + dt.timedelta(days=n) for n in (7, 49, 50, 58, 62, 75, 76, 120)]
        rows = [(e, -1, 100.0, 1.0, 1.02, -0.3, None) for e in exps]
        c = make_chain("TST", asof, 100.0, rows)
        self.assertEqual(ch.pick_expiry(c, asof, 60, 50, 75), asof + dt.timedelta(days=58))     # |58-60| == |62-60|: shorter wins
        only_edges = make_chain("TST", asof, 100.0, [r for r in rows if (r[0] - asof).days in (49, 50, 75, 76)])
        self.assertEqual(ch.pick_expiry(only_edges, asof, 60, 50, 75), asof + dt.timedelta(days=50))   # 50 and 75 inclusive
        none = make_chain("TST", asof, 100.0, [r for r in rows if (r[0] - asof).days in (7, 49, 76, 120)])
        self.assertIsNone(ch.pick_expiry(none, asof, 60, 50, 75))

    def test_pick_expiry_thirty_days(self):
        asof = D("2026-10-13")
        rows = [(asof + dt.timedelta(days=n), -1, 100.0, 1.0, 1.02, -0.3, None) for n in (22, 23, 29, 31, 37, 38)]
        c = make_chain("TST", asof, 100.0, rows)
        self.assertEqual((ch.pick_expiry(c, asof, 30, 23, 37) - asof).days, 29)               # 29 and 31 tie -> the shorter


class RelativeSpread(unittest.TestCase):
    def test_median(self):
        self.assertAlmostEqual(ch.median_rel_spread([(1.00, 1.10), (2.00, 2.20), (3.00, 3.60)]), 0.2 / 2.1, places=12)
        self.assertAlmostEqual(ch.median_rel_spread([(1.00, 1.10), (2.00, 2.20)]), 0.5 * (0.1 / 1.05 + 0.2 / 2.1), places=12)
        self.assertIsNone(ch.median_rel_spread([]))
        self.assertIsNone(ch.median_rel_spread([(0.0, 0.0)]))


class SpotAndSnapshotRules(unittest.TestCase):
    S = D("2026-10-13")

    def test_close_print_floor(self):
        self.assertEqual(ch.close_print_floor(), dt.time(15, 59))
        self.assertEqual(ch.close_print_floor(ch.EARLY_CLOSE), dt.time(12, 59))

    def test_official_close_once_the_closing_print_is_in(self):
        self.assertEqual(ch.choose_spot(777.2, 777.5, et("2026-10-13 16:00"), self.S), (777.5, "close"))
        self.assertEqual(ch.choose_spot(777.2, 777.5, et("2026-10-13 15:59"), self.S), (777.5, "close"))
        self.assertEqual(ch.choose_spot(777.2, 777.5, et("2026-10-13 16:00:45"), self.S), (777.5, "close"))

    def test_last_price_before_the_closing_print(self):
        """Intraday the feed's `close` repeats the last price, so it is not an official close."""
        self.assertEqual(ch.choose_spot(777.2, 777.2, et("2026-10-13 10:38:53"), self.S), (777.2, "last"))
        self.assertEqual(ch.choose_spot(777.2, 777.5, et("2026-10-13 15:58:59"), self.S), (777.2, "last"))

    def test_last_trade_from_another_day_is_not_a_close(self):
        self.assertEqual(ch.choose_spot(777.2, 777.5, et("2026-10-12 16:00"), self.S), (777.2, "last"))

    def test_early_close_day_uses_one_pm(self):
        s = D("2026-11-27")
        self.assertEqual(ch.choose_spot(500.0, 501.0, et("2026-11-27 13:00"), s, ch.EARLY_CLOSE), (501.0, "close"))
        self.assertEqual(ch.choose_spot(500.0, 501.0, et("2026-11-27 12:30"), s, ch.EARLY_CLOSE), (500.0, "last"))

    def test_fallbacks(self):
        self.assertEqual(ch.choose_spot(None, 777.5, et("2026-10-13 16:00"), self.S), (777.5, "close"))
        self.assertEqual(ch.choose_spot(None, 777.5, None, self.S), (777.5, "close_unverified"))
        self.assertEqual(ch.choose_spot(777.2, None, None, self.S), (777.2, "last"))
        self.assertEqual(ch.choose_spot(None, None, None, self.S), (None, "none"))
        self.assertEqual(ch.choose_spot("n/a", 0, et("2026-10-13 16:00"), self.S), (None, "none"))

    def meta(self, **kw):
        m = {"und_last_trade_et": "2026-10-13T16:00:00", "file_age_min": 5.0}
        m.update(kw)
        return m

    def test_snapshot_stale_reasons(self):
        self.assertIsNone(ch.snapshot_stale(self.meta(), self.S))
        self.assertEqual(ch.snapshot_stale(self.meta(und_last_trade_et=None), self.S), "no_underlying_trade_time")
        self.assertEqual(ch.snapshot_stale(self.meta(und_last_trade_et="2026-10-12T16:00:00"), self.S),
                         "underlying_last_trade_2026-10-12")
        self.assertEqual(ch.snapshot_stale(self.meta(und_last_trade_et="2026-10-13T15:41:00"), self.S), "pre_close_snapshot_15:41")
        self.assertIsNone(ch.snapshot_stale(self.meta(und_last_trade_et="2026-10-13T15:41:00"), self.S, require_close=False))
        self.assertEqual(ch.snapshot_stale(self.meta(file_age_min=None), self.S), "no_file_timestamp")
        self.assertEqual(ch.snapshot_stale(self.meta(file_age_min=46.0), self.S), "file_age_46min")
        self.assertIsNone(ch.snapshot_stale(self.meta(file_age_min=45.0), self.S))
        self.assertIsNone(ch.snapshot_stale(self.meta(und_last_trade_et="2026-11-27T13:00:00"), D("2026-11-27"),
                                            True, ch.EARLY_CLOSE))

    def test_offset_timestamps_are_converted_to_eastern(self):
        """GVZ / VXTLT quotes carry a Central-time offset; the naive-Eastern parser must not just relabel it."""
        t = ch.parse_et_naive("2026-10-13T09:50:01.402000-05:00")
        self.assertEqual((t.hour, t.minute), (10, 50))
        self.assertEqual(t.utcoffset(), dt.timedelta(hours=-4))
        t2 = ch.parse_et_naive("2026-10-13T10:38:16")
        self.assertEqual((t2.hour, t2.minute, str(t2.tzinfo)), (10, 38, "America/New_York"))
        self.assertIsNone(ch.parse_et_naive(None))
        self.assertIsNone(ch.parse_et_naive("not a time"))


class CboeParser(unittest.TestCase):
    S = D("2026-10-13")

    def obj(self, rows=None, **kw):
        rows = rows if rows is not None else [
            (D("2026-10-14"), +1, 104.0, 0.30, 0.32, 0.16, 0.12), (D("2026-10-14"), -1, 96.0, 0.30, 0.32, -0.16, 0.12)]
        base = dict(last_trade=et("2026-10-13 16:00"), file_ts_utc=et("2026-10-13 16:17").astimezone(UTC))
        base.update(kw)
        return cboe_options_obj("TST", rows, 100.0, **base)

    def test_clean_parse_and_meta(self):
        c = ch.from_cboe(self.obj(), "TST", self.S, now_utc=et("2026-10-13 16:20").astimezone(UTC))
        self.assertEqual(c.spot, 100.0)
        self.assertEqual(c.meta["spot_src"], "close")
        self.assertEqual(c.meta["n_clean"], 2)
        self.assertEqual(c.meta["file_age_min"], 3.0)
        self.assertEqual(c.meta["und_last_trade_et"], "2026-10-13T16:00:00-04:00")
        self.assertEqual(c.meta["n_two_sided"], 2)
        self.assertEqual(c.quote(D("2026-10-14"), +1, 104.0), (0.30, 0.32))
        self.assertEqual(c.row(D("2026-10-14"), -1, 96.0)["delta"], -0.16)
        self.assertEqual(c.expiries(), [D("2026-10-14")])
        self.assertIsNone(ch.snapshot_stale(c.meta, self.S))

    def test_cleaning_counts_each_reason(self):
        rows = [
            (D("2026-10-14"), +1, 104.0, 0.30, 0.32, 0.16, 0.12),        # good
            (D("2026-10-14"), +1, 105.0, 0.30, 0.20, 0.10, 0.12),        # crossed
            (D("2026-10-14"), +1, 106.0, 0.00, 0.00, 0.05, 0.12),        # zero ask
            (D("2026-10-14"), +1, 190.0, 0.30, 0.32, 0.01, 0.12),        # 90% away
            (D("2027-06-18"), +1, 104.0, 0.30, 0.32, 0.16, 0.12),        # 248 DTE
            (D("2026-10-12"), +1, 104.0, 0.30, 0.32, 0.16, 0.12),        # already expired
        ]
        c = ch.from_cboe(self.obj(rows), "TST", self.S)
        self.assertEqual(c.n_rows, 1)
        d = c.meta["dropped"]
        self.assertEqual((d["crossed_or_zero_ask"], d["moneyness"], d["dte"]), (2, 1, 2))
        self.assertEqual(c.meta["n_crossed_raw"], 1)

    def test_other_roots_and_junk_symbols_are_dropped(self):
        o = self.obj()
        o["data"]["options"].append(dict(o["data"]["options"][0], option="XYZ261014C00104000"))
        o["data"]["options"].append(dict(o["data"]["options"][0], option="garbage"))
        o["data"]["options"].append(dict(o["data"]["options"][0], bid=None))
        c = ch.from_cboe(o, "TST", self.S)
        self.assertEqual(c.n_rows, 2)
        self.assertEqual((c.meta["dropped"]["root"], c.meta["dropped"]["unparsed"], c.meta["dropped"]["no_quote"]), (1, 1, 1))

    def test_missing_delta_is_kept_with_none(self):
        o = self.obj()
        o["data"]["options"][0]["delta"] = None
        c = ch.from_cboe(o, "TST", self.S)
        self.assertEqual(c.meta["n_no_delta"], 1)
        self.assertIsNone(c.row(D("2026-10-14"), +1, 104.0)["delta"])

    def test_errors(self):
        with self.assertRaises(ValueError):
            ch.from_cboe({"data": {"options": []}}, "TST", self.S)
        with self.assertRaises(ValueError):
            ch.from_cboe({}, "TST", self.S)
        o = self.obj()
        o["data"]["current_price"], o["data"]["close"] = None, None
        with self.assertRaises(ValueError):
            ch.from_cboe(o, "TST", self.S)
        o2 = self.obj([(D("2026-10-14"), +1, 104.0, 0.30, 0.20, 0.16, 0.12)])
        with self.assertRaises(ValueError):
            ch.from_cboe(o2, "TST", self.S)

    def test_intraday_snapshot_uses_the_last_price_and_is_flagged(self):
        o = self.obj(last_trade=et("2026-10-13 10:38:53"), close=101.0)
        o["data"]["current_price"] = 100.5
        c = ch.from_cboe(o, "TST", self.S, now_utc=et("2026-10-13 10:50").astimezone(UTC))
        self.assertEqual((c.spot, c.meta["spot_src"]), (100.5, "last"))
        self.assertEqual(ch.snapshot_stale(c.meta, self.S), "pre_close_snapshot_10:38")

    def test_old_file_is_stale(self):
        c = ch.from_cboe(self.obj(file_ts_utc=et("2026-10-13 15:00").astimezone(UTC)), "TST", self.S,
                         now_utc=et("2026-10-13 16:20").astimezone(UTC))
        self.assertEqual(ch.snapshot_stale(c.meta, self.S), "file_age_80min")


class ChainContainer(unittest.TestCase):
    def test_lookup_api(self):
        ex = D("2026-10-14")
        c = tiny_chain(rows_from(ex, +1, [(101, 1.0, 1.1, 0.4), (100, 1.5, 1.6, 0.5)]))
        self.assertEqual(c.strikes(ex, +1), [100.0, 101.0])                 # ascending
        self.assertTrue(c.has_expiry(ex))
        self.assertFalse(c.has_expiry(D("2026-10-15")))
        self.assertEqual(c.quote(ex, +1, 100), (1.5, 1.6))
        self.assertIsNone(c.quote(ex, +1, 100.5))
        self.assertIsNone(c.quote(ex, -1, 100))
        self.assertEqual(c.strikes(ex, -1), [])


if __name__ == "__main__":
    unittest.main()
