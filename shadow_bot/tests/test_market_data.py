"""The data the bot keeps between runs: merging rows from several sources, the cache CSVs, pruning and trimming.

The gates built on top of this data are tested against numpy in test_math.py; here the question is what happens to a row on its
way in (which source wins, what is only reported, what is ignored) and whether a save / load round trip changes anything."""
import datetime as dt
import os
import unittest

from tests.helpers import D, TempRoot, trading_days_from

import config as cfg
import market_data as md


def read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def market_with(sym="SPY", rows=(), src="cboe"):
    m = md.Market()
    md.merge_closes(m, sym, rows, src)
    return m


class MergeCloses(unittest.TestCase):
    def test_a_new_date_is_added_with_its_source(self):
        m = md.Market()
        rep = md.merge_closes(m, "SPY", [(D("2026-10-12"), 600.0), (D("2026-10-13"), 601.5)], "nasdaq")
        self.assertEqual(rep["added"], [D("2026-10-12"), D("2026-10-13")])
        self.assertEqual((rep["corrected"], rep["mismatch"]), ([], []))
        self.assertEqual(m.close("SPY", D("2026-10-13")), 601.5)
        self.assertEqual(m.close_src[("SPY", D("2026-10-13"))], "nasdaq")

    def test_rows_that_are_not_prices_are_ignored(self):
        m = md.Market()
        rep = md.merge_closes(m, "SPY", [(D("2026-10-12"), None), (D("2026-10-13"), 0.0), (D("2026-10-14"), -3.0),
                                         (D("2026-10-15"), float("nan"))], "nasdaq")
        self.assertEqual(rep, {"added": [], "corrected": [], "mismatch": []})
        self.assertEqual(len(m.closes["SPY"]), 0)

    def test_the_same_close_to_within_half_a_cent_changes_nothing_and_reports_nothing(self):
        m = market_with(rows=[(D("2026-10-13"), 600.00)], src="cboe")
        rep = md.merge_closes(m, "SPY", [(D("2026-10-13"), 600.004)], "yahoo")
        self.assertEqual((rep["added"], rep["corrected"], rep["mismatch"]), ([], [], []))
        self.assertEqual(m.close("SPY", D("2026-10-13")), 600.00)                 # the value is kept, only the provenance moves
        self.assertEqual(m.close_src[("SPY", D("2026-10-13"))], "yahoo")

    def test_better_provenance_never_downgrades(self):
        m = market_with(rows=[(D("2026-10-13"), 600.00)], src="yahoo")
        md.merge_closes(m, "SPY", [(D("2026-10-13"), 600.00)], "cboe")
        self.assertEqual(m.close_src[("SPY", D("2026-10-13"))], "yahoo")           # a lower rank does not replace a higher one

    def test_a_higher_ranked_source_corrects_a_small_disagreement(self):
        m = market_with(rows=[(D("2026-10-13"), 600.00)], src="cboe")
        rep = md.merge_closes(m, "SPY", [(D("2026-10-13"), 600.25)], "nasdaq")
        self.assertEqual(rep["corrected"], [(D("2026-10-13"), 600.00, 600.25)])
        self.assertEqual(rep["mismatch"], [])
        self.assertEqual(m.close("SPY", D("2026-10-13")), 600.25)
        self.assertEqual(m.close_src[("SPY", D("2026-10-13"))], "nasdaq")

    def test_an_equal_or_lower_ranked_source_is_only_reported(self):
        for stored, incoming in (("nasdaq", "stooq"), ("cboe", "cboe"), ("yahoo", "nasdaq"), ("yahoo", "cboe")):
            with self.subTest(stored=stored, incoming=incoming):
                m = market_with(rows=[(D("2026-10-13"), 600.00)], src=stored)
                rep = md.merge_closes(m, "SPY", [(D("2026-10-13"), 600.25)], incoming)
                self.assertEqual(rep["mismatch"], [(D("2026-10-13"), 600.00, 600.25)])
                self.assertEqual(rep["corrected"], [])
                self.assertEqual(m.close("SPY", D("2026-10-13")), 600.00)

    def test_a_disagreement_wider_than_two_percent_is_never_taken_even_from_a_better_source(self):
        """Found by the simulated world: a second source answering 1.00 for a 600.71 close used to replace the stored value."""
        m = market_with(rows=[(D("2026-10-13"), 600.71)], src="cboe")
        rep = md.merge_closes(m, "SPY", [(D("2026-10-13"), 1.00)], "yahoo")
        self.assertEqual(rep["mismatch"], [(D("2026-10-13"), 600.71, 1.00)])
        self.assertEqual(rep["corrected"], [])
        self.assertEqual(m.close("SPY", D("2026-10-13")), 600.71)
        rep = md.merge_closes(m, "SPY", [(D("2026-10-13"), 640.0)], "yahoo")        # +6.5%
        self.assertEqual(len(rep["mismatch"]), 1)
        self.assertEqual(m.close("SPY", D("2026-10-13")), 600.71)

    def test_just_inside_two_percent_still_corrects(self):
        m = market_with(rows=[(D("2026-10-13"), 100.0)], src="cboe")
        rep = md.merge_closes(m, "SPY", [(D("2026-10-13"), 101.9)], "nasdaq")
        self.assertEqual(len(rep["corrected"]), 1)
        rep = md.merge_closes(m, "SPY", [(D("2026-10-13"), 104.5)], "yahoo")
        self.assertEqual(len(rep["mismatch"]), 1)

    def test_an_unknown_source_ranks_lowest(self):
        m = market_with(rows=[(D("2026-10-13"), 600.00)], src="cboe")
        rep = md.merge_closes(m, "SPY", [(D("2026-10-13"), 600.25)], "mystery")
        self.assertEqual((len(rep["mismatch"]), len(rep["corrected"])), (1, 0))

    def test_a_stored_close_with_no_source_tag_counts_as_the_best_source(self):
        m = md.Market(closes={"SPY": {D("2026-10-13"): 600.0}})                    # no close_src entry at all
        rep = md.merge_closes(m, "SPY", [(D("2026-10-13"), 600.25)], "yahoo")
        self.assertEqual(len(rep["corrected"]), 1)                                  # rank 2 beats the default rank 0

    def test_merging_new_rows_forgets_the_cached_realised_vol(self):
        days = trading_days_from(D("2026-08-03"), 45)
        px = [600.0 + (i % 5) for i in range(45)]
        m = md.Market()
        md.merge_closes(m, "SPY", list(zip(days[:44], px[:44])), "cboe")
        self.assertIsNone(m.rv20(days[44]))                                        # the last close is missing
        self.assertIn(days[44], m._rv)
        md.merge_closes(m, "SPY", [(days[44], px[44])], "cboe")
        self.assertIsNotNone(m.rv20(days[44]))                                     # the cache did not keep the stale None

    def test_each_symbol_has_its_own_series(self):
        m = md.Market()
        md.merge_closes(m, "SPY", [(D("2026-10-13"), 600.0)], "cboe")
        md.merge_closes(m, "GLD", [(D("2026-10-13"), 380.0)], "cboe")
        self.assertEqual((m.close("SPY", D("2026-10-13")), m.close("GLD", D("2026-10-13"))), (600.0, 380.0))
        self.assertIsNone(m.close("TLT", D("2026-10-13")))


class MergePublishedIndex(unittest.TestCase):
    def test_new_dates_are_reported_once(self):
        m = md.Market()
        new = md.merge_published_index(m, "VIX", [(D("2026-10-12"), 16.2), (D("2026-10-13"), 17.0)])
        self.assertEqual(new, [D("2026-10-12"), D("2026-10-13")])
        self.assertEqual(md.merge_published_index(m, "VIX", [(D("2026-10-13"), 17.0)]), [])

    def test_a_published_close_replaces_the_provisional_one(self):
        m = md.Market(prov={("VIX", D("2026-10-13")): 17.4})
        self.assertEqual(m.index_val("VIX", D("2026-10-13")), None)                 # the gate never reads a provisional value
        self.assertEqual(m.index_val("VIX", D("2026-10-13"), allow_prov=True), 17.4)
        new = md.merge_published_index(m, "VIX", [(D("2026-10-13"), 17.05)])
        self.assertEqual(new, [D("2026-10-13")])
        self.assertEqual(m.index_val("VIX", D("2026-10-13")), 17.05)
        self.assertEqual(m.index_val("VIX", D("2026-10-13"), allow_prov=True), 17.05)
        self.assertNotIn(("VIX", D("2026-10-13")), m.prov)

    def test_a_restated_published_value_overwrites_but_is_not_new(self):
        m = md.Market()
        md.merge_published_index(m, "VIX", [(D("2026-10-13"), 17.0)])
        new = md.merge_published_index(m, "VIX", [(D("2026-10-13"), 17.1)])
        self.assertEqual(new, [])
        self.assertEqual(m.index_val("VIX", D("2026-10-13")), 17.1)

    def test_empty_values_are_skipped(self):
        m = md.Market()
        self.assertEqual(md.merge_published_index(m, "VIX", [(D("2026-10-13"), None)]), [])
        self.assertEqual(len(m.index_series("VIX")), 0)

    def test_provisional_values_do_not_leak_into_the_published_series(self):
        m = md.Market(indices={"VIX": {D("2026-10-12"): 16.0}}, prov={("VIX", D("2026-10-13")): 17.4})
        self.assertEqual(m.index_series("VIX").dates, [D("2026-10-12")])
        self.assertEqual(m.trailing_quantile("VIX", D("2026-10-14"), 0.25, window=252, min_obs=1), 16.0)


class TBill(unittest.TestCase):
    def test_percent_is_stored_as_a_decimal(self):
        m = md.Market()
        new = md.merge_tbill(m, [(D("2026-10-12"), 3.62), (D("2026-10-13"), 3.60), (D("2026-10-14"), None)])
        self.assertEqual(new, [D("2026-10-12"), D("2026-10-13")])
        self.assertAlmostEqual(m.tbill.get(D("2026-10-13")), 0.036, places=12)

    def test_the_rate_is_the_last_one_on_or_before_the_day(self):
        m = md.Market()
        md.merge_tbill(m, [(D("2026-10-09"), 3.55), (D("2026-10-13"), 3.60)])
        self.assertAlmostEqual(m.rate(D("2026-10-13")), 0.036, places=12)
        self.assertAlmostEqual(m.rate(D("2026-10-12")), 0.0355, places=12)          # a holiday weekend: Friday's rate carries over
        self.assertAlmostEqual(m.rate(D("2026-11-30")), 0.036, places=12)
        self.assertEqual(m.rate(D("2026-10-01")), 0.0)                              # nothing known yet

    def test_a_restated_rate_is_not_new(self):
        m = md.Market()
        md.merge_tbill(m, [(D("2026-10-13"), 3.60)])
        self.assertEqual(md.merge_tbill(m, [(D("2026-10-13"), 3.61)]), [])
        self.assertAlmostEqual(m.rate(D("2026-10-13")), 0.0361, places=12)


class Cache(unittest.TestCase):
    def build(self):
        m = md.Market()
        md.merge_closes(m, "SPY", [(D("2026-10-12"), 0.1 + 0.2 + 600.0), (D("2026-10-13"), 601.2345)], "cboe")
        md.merge_closes(m, "GLD", [(D("2026-10-13"), 380.01)], "nasdaq")
        md.merge_closes(m, "TLT", [(D("2026-10-12"), 88.8)], "yahoo")
        md.merge_published_index(m, "VIX", [(D("2026-10-12"), 16.25), (D("2026-10-13"), 17.0)])
        md.merge_published_index(m, "GVZ", [(D("2026-10-13"), 18.5)])
        m.prov[("VIX", D("2026-10-14"))] = 17.7
        md.merge_tbill(m, [(D("2026-10-12"), 3.62), (D("2026-10-13"), 3.6)])
        return m

    def test_a_round_trip_changes_nothing(self):
        m = self.build()
        with TempRoot() as root:
            md.save_market(m, root)
            m2 = md.load_market(root)
        for sym in m.closes:
            self.assertEqual(m2.closes[sym].dates, m.closes[sym].dates)
            self.assertEqual(m2.closes[sym].values, m.closes[sym].values)             # exact: repr() round-trips a float
        self.assertEqual(m2.close_src, m.close_src)
        for name in m.indices:
            self.assertEqual((m2.indices[name].dates, m2.indices[name].values), (m.indices[name].dates, m.indices[name].values))
        self.assertEqual(m2.prov, m.prov)
        self.assertEqual((m2.tbill.dates, m2.tbill.values), (m.tbill.dates, m.tbill.values))

    def test_provisional_rows_come_back_provisional(self):
        m = self.build()
        with TempRoot() as root:
            md.save_market(m, root)
            m2 = md.load_market(root)
        self.assertEqual(m2.index_val("VIX", D("2026-10-14")), None)
        self.assertEqual(m2.index_val("VIX", D("2026-10-14"), allow_prov=True), 17.7)
        self.assertNotIn(D("2026-10-14"), m2.index_series("VIX"))

    def test_the_files_are_sorted_and_deterministic(self):
        m = self.build()
        with TempRoot() as root:
            md.save_market(m, root)
            first = {n: read_bytes(os.path.join(root, n)) for n in (cfg.CLOSES_FILE, cfg.INDICES_FILE, cfg.TBILL_FILE)}
            md.save_market(md.load_market(root), root)
            second = {n: read_bytes(os.path.join(root, n)) for n in first}
            self.assertEqual(first, second)
            lines = first[cfg.CLOSES_FILE].decode().splitlines()
            self.assertEqual(lines[0], "date,symbol,close,src")
            keys = [tuple(x.split(",")[:2]) for x in lines[1:]]
            self.assertEqual(keys, sorted(keys))
            self.assertNotIn(b"\r", first[cfg.CLOSES_FILE])                               # LF only, so git diffs stay small
            self.assertEqual(sorted(os.listdir(os.path.join(root, "data"))), ["closes.csv", "indices.csv", "tbill.csv"])    # no .tmp left

    def test_loading_from_an_empty_directory_gives_an_empty_market(self):
        with TempRoot() as root:
            m = md.load_market(root)
        self.assertEqual((m.closes, m.indices, m.prov), ({}, {}, {}))
        self.assertEqual(len(m.tbill), 0)

    def test_a_row_without_a_source_tag_is_a_seed_row(self):
        with TempRoot() as root:
            os.makedirs(os.path.join(root, "data"))
            with open(os.path.join(root, cfg.CLOSES_FILE), "w", newline="") as f:
                f.write("date,symbol,close,src\n2026-10-13,SPY,600.5,\n")
            m = md.load_market(root)
        self.assertEqual(m.close_src[("SPY", D("2026-10-13"))], "seed")

    def test_tbill_percent_survives_a_round_trip_exactly(self):
        m = md.Market()
        md.merge_tbill(m, [(D("2026-10-13"), 3.6), (D("2026-10-14"), 3.595), (D("2026-10-15"), 0.07)])
        with TempRoot() as root:
            md.save_market(m, root)
            m2 = md.load_market(root)
        self.assertEqual(m2.tbill.values, m.tbill.values)

    def test_the_committed_seed_files_load(self):
        m = md.load_market(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        for sym in ("SPY", "GLD", "TLT"):
            self.assertGreater(len(m.closes[sym]), 250, sym)
        self.assertGreater(len(m.index_series("VIX")), 250)
        self.assertGreater(len(m.tbill), 50)
        self.assertEqual(m.prov, {})                                                      # a committed seed holds only published values


class PruneAndTrim(unittest.TestCase):
    def test_provisional_values_expire_after_ten_days(self):
        m = md.Market(prov={("VIX", D("2026-10-01")): 17.0, ("VIX", D("2026-10-03")): 17.1, ("GVZ", D("2026-10-13")): 18.0})
        md.prune_prov(m, D("2026-10-13"))
        self.assertEqual(sorted(m.prov), [("GVZ", D("2026-10-13")), ("VIX", D("2026-10-03"))])       # 10 days old is kept, 12 is not

    def test_trimming_keeps_what_the_strategies_look_back_over(self):
        today = D("2026-10-13")
        old, edge, recent = today - dt.timedelta(days=cfg.CLOSES_KEEP_DAYS + 1), today - dt.timedelta(days=cfg.CLOSES_KEEP_DAYS), today
        m = md.Market()
        md.merge_closes(m, "SPY", [(old, 500.0), (edge, 510.0), (recent, 600.0)], "cboe")
        md.merge_published_index(m, "VIX", [(today - dt.timedelta(days=cfg.HISTORY_KEEP_DAYS["VIX"] + 1), 20.0),
                                            (today - dt.timedelta(days=cfg.HISTORY_KEEP_DAYS["VIX"]), 21.0), (today, 17.0)])
        md.merge_published_index(m, "VIX3M", [(today - dt.timedelta(days=cfg.HISTORY_KEEP_DAYS["VIX3M"] + 1), 20.0), (today, 19.0)])
        md.merge_published_index(m, "OTHER", [(today - dt.timedelta(days=151), 1.0), (today - dt.timedelta(days=150), 2.0)])
        md.merge_tbill(m, [(today - dt.timedelta(days=cfg.TBILL_KEEP_DAYS + 1), 3.0), (today, 3.6)])
        m.rv20(today)                                                                    # fill the cache so the trim has to clear it
        md.trim_market(m, today)
        self.assertEqual(m.closes["SPY"].dates, [edge, recent])
        self.assertNotIn(("SPY", old), m.close_src)
        self.assertIn(("SPY", edge), m.close_src)
        self.assertEqual(len(m.indices["VIX"]), 2)                                       # the 430-day VIX window is longer than the others'
        self.assertEqual(m.indices["VIX3M"].dates, [today])
        self.assertEqual(len(m.indices["OTHER"]), 1)                                     # an unlisted index defaults to 150 days
        self.assertEqual(m.tbill.dates, [today])
        self.assertEqual(m._rv, {})

    def test_trimming_never_touches_a_fresh_market(self):
        today = D("2026-10-13")
        days = trading_days_from(D("2026-08-03"), 50)
        m = md.Market()
        md.merge_closes(m, "SPY", [(d, 600.0 + i) for i, d in enumerate(days)], "cboe")
        md.trim_market(m, today)
        self.assertEqual(len(m.closes["SPY"]), 50)


class MissingClosesForTheGate(unittest.TestCase):
    def test_a_complete_history_needs_nothing(self):
        d = D("2026-10-13")
        days = trading_days_from(D("2026-07-01"), 90)
        m = md.Market()
        md.merge_closes(m, "SPY", [(x, 600.0 + i) for i, x in enumerate(days) if x < d], "cboe")
        self.assertEqual(m.missing_spy_closes_for_gate(d), [])

    def test_a_hole_is_named(self):
        d = D("2026-10-13")
        days = [x for x in trading_days_from(D("2026-07-01"), 90) if x < d]
        hole = days[-5]
        m = md.Market()
        md.merge_closes(m, "SPY", [(x, 600.0 + i) for i, x in enumerate(days) if x != hole], "cboe")
        self.assertEqual(m.missing_spy_closes_for_gate(d), [hole])
        gate, info = m.condor_gate(d)
        self.assertIsNone(gate)                                                           # unknown, never "closed"
        self.assertEqual(info["why"], "missing_close")

    def test_with_no_history_every_needed_day_is_missing(self):
        d = D("2026-10-13")
        need = md.Market().missing_spy_closes_for_gate(d)
        self.assertEqual(len(need), cfg.C9["gate_window"] + cfg.C9["rv_window"])
        self.assertEqual(need, sorted(need))


if __name__ == "__main__":
    unittest.main()
