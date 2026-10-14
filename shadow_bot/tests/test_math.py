"""Numerical building blocks against independent oracles.

  * Black-Scholes price / delta: scipy.stats.norm oracle (when installed) and golden values computed with scipy on 2026-10-13;
  * quantile (numpy 'linear'), median, sample std (ddof=1): numpy oracle and hand-checked small cases;
  * the C9 gate (RV20 below its own 20-day median, window includes the previous day) and the C3 trailing quantile, re-derived
    from the frozen wording with numpy over the committed seed data and compared day by day with market_data.
"""
import datetime as dt
import math
import random
import unittest

from tests.helpers import D, ROOT, needs_numpy, needs_scipy, np, sps, trading_days_from, move_path

import market_data as md
import nyse_calendar as nyse
from bs import bs_call, bs_delta, bs_put, norm_cdf

GOLDEN = [
    # (S, K, T, r, sigma, put price, put delta, call price, call delta), scipy.stats.norm, 12 decimals
    (382.15, 360.0, 30 / 365, 0.0406, 0.2248, 2.071354051629, -0.156135046044, 25.420666961836, 0.843864953956),
    (78.05, 74.0, 30 / 365, 0.0406, 0.1458, 0.128397479264, -0.084488814511, 4.424922910807, 0.915511185489),
    (600.0, 584.0, 1 / 365, 0.04, 0.12, 0.000006117256, -0.000007669196, 16.064002610535, 0.999992330804),
    (777.5, 700.0, 60 / 365, 0.0406, 0.18, 1.466576093510, -0.058587451658, 83.622801870707, 0.941412548342),
    (100.0, 100.0, 0.25, 0.05, 0.2, 3.372777178991, -0.430539816792, 4.614997129603, 0.569460183208),
    (50.0, 60.0, 0.5, 0.0, 0.35, 11.811295274972, -0.730044192391, 1.811295274972, 0.269955807609),
]


class BlackScholes(unittest.TestCase):
    def test_golden_values(self):
        for S, K, T, r, s, pp, pd, cp, cd in GOLDEN:
            p, dl = bs_put(S, K, T, r, s)
            self.assertAlmostEqual(p, pp, places=9)
            self.assertAlmostEqual(dl, pd, places=9)
            c, cdl = bs_call(S, K, T, r, s)
            self.assertAlmostEqual(c, cp, places=9)
            self.assertAlmostEqual(cdl, cd, places=9)

    def test_put_call_parity(self):
        for S, K, T, r, s, *_ in GOLDEN:
            c, _ = bs_call(S, K, T, r, s)
            p, _ = bs_put(S, K, T, r, s)
            self.assertAlmostEqual(c - p, S - K * math.exp(-r * T), places=9)

    def test_delta_helper_agrees_with_pricers(self):
        for S, K, T, r, s, _pp, pd, _cp, cd in GOLDEN:
            self.assertAlmostEqual(bs_delta(-1, S, K, T, r, s), pd, places=9)
            self.assertAlmostEqual(bs_delta(+1, S, K, T, r, s), cd, places=9)

    def test_delta_helper_returns_none_instead_of_raising(self):
        self.assertIsNone(bs_delta(1, 100.0, 100.0, 0.1, 0.04, 0.0))
        self.assertIsNone(bs_delta(1, 100.0, 100.0, 0.1, 0.04, float("nan")))
        self.assertIsNone(bs_delta(1, 100.0, 100.0, 0.0, 0.04, 0.2))
        self.assertIsNone(bs_delta(1, 0.0, 100.0, 0.1, 0.04, 0.2))
        self.assertIsNone(bs_delta(1, 100.0, -1.0, 0.1, 0.04, 0.2))
        self.assertIsNone(bs_delta(1, 100.0, 100.0, 0.1, 0.04, None))

    def test_expired_or_zero_vol_is_intrinsic(self):
        self.assertEqual(bs_put(100.0, 110.0, 0.0, 0.04, 0.2), (10.0, -1.0))
        self.assertEqual(bs_put(100.0, 90.0, 0.0, 0.04, 0.2), (0.0, 0.0))
        self.assertEqual(bs_put(100.0, 110.0, 0.1, 0.04, 0.0), (10.0, -1.0))
        self.assertEqual(bs_call(100.0, 90.0, 0.0, 0.04, 0.2), (10.0, 1.0))

    def test_normal_cdf(self):
        self.assertAlmostEqual(norm_cdf(0.0), 0.5, places=15)
        self.assertAlmostEqual(norm_cdf(1.959963984540054), 0.975, places=12)
        self.assertAlmostEqual(norm_cdf(-8.0) + norm_cdf(8.0), 1.0, places=15)

    @needs_scipy
    def test_against_scipy_on_a_grid(self):
        rnd = random.Random(7)
        for _ in range(400):
            S = rnd.uniform(20, 900)
            K = S * rnd.uniform(0.7, 1.3)
            T = rnd.uniform(1 / 365, 1.0)
            r = rnd.uniform(0.0, 0.06)
            s = rnd.uniform(0.05, 0.9)
            d1 = (math.log(S / K) + (r + 0.5 * s * s) * T) / (s * math.sqrt(T))
            d2 = d1 - s * math.sqrt(T)
            put = K * math.exp(-r * T) * sps.norm.cdf(-d2) - S * sps.norm.cdf(-d1)
            call = S * sps.norm.cdf(d1) - K * math.exp(-r * T) * sps.norm.cdf(d2)
            p, pdl = bs_put(S, K, T, r, s)
            c, cdl = bs_call(S, K, T, r, s)
            self.assertAlmostEqual(p, put, places=9)
            self.assertAlmostEqual(c, call, places=9)
            self.assertAlmostEqual(pdl, sps.norm.cdf(d1) - 1.0, places=12)
            self.assertAlmostEqual(cdl, sps.norm.cdf(d1), places=12)
            self.assertAlmostEqual(bs_delta(-1, S, K, T, r, s), pdl, places=12)


class SmallStatistics(unittest.TestCase):
    def test_median(self):
        self.assertEqual(md.median([3, 1, 2]), 2)
        self.assertEqual(md.median([4, 1, 3, 2]), 2.5)
        self.assertEqual(md.median([5.0]), 5.0)

    def test_quantile_hand_cases(self):
        v = [10.0, 20.0, 30.0, 40.0, 50.0]
        self.assertEqual(md.quantile_linear(v, 0.0), 10.0)
        self.assertEqual(md.quantile_linear(v, 1.0), 50.0)
        self.assertEqual(md.quantile_linear(v, 0.5), 30.0)
        self.assertEqual(md.quantile_linear(v, 0.25), 20.0)                  # position 1.0 exactly
        self.assertAlmostEqual(md.quantile_linear(v, 0.3), 22.0, places=12)  # position 1.2 -> 20 + 0.2 x 10
        self.assertEqual(md.quantile_linear([7.0], 0.25), 7.0)
        self.assertEqual(md.quantile_linear([9.0, 1.0], 0.5), 5.0)
        with self.assertRaises(ValueError):
            md.quantile_linear([], 0.25)

    def test_sample_std_uses_n_minus_one(self):
        self.assertAlmostEqual(md.sample_std([1.0, 2.0, 3.0, 4.0]), math.sqrt(5.0 / 3.0), places=12)
        self.assertAlmostEqual(md.sample_std([2.0, 4.0]), math.sqrt(2.0), places=12)

    @needs_numpy
    def test_against_numpy(self):
        rnd = random.Random(11)
        for n in (2, 3, 7, 20, 199, 200, 201, 251, 252):
            for _ in range(25):
                xs = [rnd.gauss(18.0, 6.0) for _ in range(n)]
                for q in (0.1, 0.25, 0.5, 0.9):
                    self.assertAlmostEqual(md.quantile_linear(xs, q), float(np.quantile(xs, q)), places=10, msg=f"n={n} q={q}")
                self.assertAlmostEqual(md.median(xs), float(np.median(xs)), places=12)
                self.assertAlmostEqual(md.sample_std(xs), float(np.std(xs, ddof=1)), places=12)


class Series(unittest.TestCase):
    def test_prior_values_excludes_the_day_and_is_positional(self):
        s = md.Series([(D("2026-01-05"), 1.0), (D("2026-01-06"), 2.0), (D("2026-01-07"), 3.0), (D("2026-01-08"), 4.0)])
        self.assertEqual(s.prior_values(D("2026-01-08"), 2), [2.0, 3.0])
        self.assertEqual(s.prior_values(D("2026-01-08"), 10), [1.0, 2.0, 3.0])
        self.assertEqual(s.prior_values(D("2026-01-05"), 3), [])
        self.assertEqual(s.prior_values(D("2026-01-07"), 1), [2.0])           # "previous" values only
        self.assertEqual(s.prior_values(D("2026-01-09"), 2), [3.0, 4.0])      # a day that is not in the series

    def test_out_of_order_insert_and_overwrite(self):
        s = md.Series()
        for d, v in ((D("2026-01-07"), 3.0), (D("2026-01-05"), 1.0), (D("2026-01-06"), 2.0), (D("2026-01-06"), 2.5)):
            s.set(d, v)
        self.assertEqual(s.values, [1.0, 2.5, 3.0])
        self.assertEqual(s.get(D("2026-01-06")), 2.5)
        self.assertEqual(s.asof(D("2026-01-10")), 3.0)
        self.assertIsNone(s.asof(D("2026-01-01")))


# --------------------------------------------------------------------------------------------------- the gates on seed data
def load_seed():
    return md.load_market(ROOT)


@needs_numpy
class C9GateAgainstNumpy(unittest.TestCase):
    """Re-derive the frozen gate from its wording (not from market_data's code) and compare on every session of the seed."""

    @classmethod
    def setUpClass(cls):
        cls.m = load_seed()
        s = cls.m.closes["SPY"]
        cls.dates, cls.c = list(s.dates), np.array(s.values)
        ret = cls.c[1:] / cls.c[:-1] - 1.0
        rv = np.full(len(cls.c), np.nan)
        for i in range(20, len(cls.c)):
            rv[i] = np.std(ret[i - 20:i], ddof=1)           # the 20 close-to-close returns ending at session i
        cls.rv = rv
        cls.pos = {d: i for i, d in enumerate(cls.dates)}

    def oracle(self, d):
        ip = self.pos[nyse.prev_td(d)]                       # p = previous trading day
        if ip - 19 < 20:
            return None
        window = self.rv[ip - 19:ip + 1]                     # the 20 RV20 values ending at p, p included
        return bool(self.rv[ip] < np.median(window))

    def test_seed_is_complete_enough(self):
        self.assertGreater(len(self.dates), 280)
        self.assertEqual(self.dates, [d for d in nyse.trading_days_between(self.dates[0] - dt.timedelta(days=1), self.dates[-1])])

    def test_gate_matches_on_every_session(self):
        checked, opened = 0, 0
        for d in self.dates:
            exp = self.oracle(d) if nyse.prev_td(d) in self.pos else None
            if exp is None:
                continue
            got, info = self.m.condor_gate(d)
            self.assertEqual(got, exp, f"gate differs on {d}: {info}")
            checked += 1
            opened += bool(got)
        self.assertGreater(checked, 250)
        self.assertGreater(opened, 60)                       # the check is not vacuous: both outcomes occur
        self.assertGreater(checked - opened, 60)

    def test_rv20_values(self):
        for d in self.dates[-40:]:
            i = self.pos[d]
            self.assertAlmostEqual(self.m.rv20(d), float(self.rv[i]), places=14)

    def test_the_gate_for_the_next_session(self):
        """Regression pin for 2026-10-13 on the committed seed (closes through 2026-10-12)."""
        got, info = self.m.condor_gate(D("2026-10-13"))
        self.assertEqual(info["prev"], "2026-10-12")
        ip = self.pos[D("2026-10-12")]
        self.assertAlmostEqual(info["rv20"], float(self.rv[ip]), places=14)
        self.assertAlmostEqual(info["median"], float(np.median(self.rv[ip - 19:ip + 1])), places=14)
        self.assertEqual(got, bool(self.rv[ip] < np.median(self.rv[ip - 19:ip + 1])))

    def test_alt_reading_excludes_the_previous_day_from_the_median(self):
        for d in self.dates[-30:]:
            ip = self.pos[nyse.prev_td(d)]
            exp = bool(self.rv[ip] < np.median(self.rv[ip - 20:ip]))
            got, _info = self.m.condor_gate_alt(d)
            self.assertEqual(got, exp, d)


class C9GateRules(unittest.TestCase):
    def market(self, closes):
        return md.Market(closes={"SPY": closes})

    def test_open_after_a_loud_stretch_that_has_calmed(self):
        days = trading_days_from(D("2026-06-01"), 60)
        m = self.market(list(zip(days, move_path(days, 40, 0.015, 0.002))))
        gate, info = m.condor_gate(nyse.next_td(days[-1]))
        self.assertTrue(gate)
        self.assertLess(info["rv20"], info["median"])
        self.assertEqual(info["window"], 20)

    def test_closed_after_a_calm_stretch_that_turned_loud(self):
        days = trading_days_from(D("2026-06-01"), 60)
        m = self.market(list(zip(days, move_path(days, 40, 0.002, 0.015))))
        gate, _ = m.condor_gate(nyse.next_td(days[-1]))
        self.assertFalse(gate)

    def test_missing_close_is_unknown_not_closed(self):
        days = trading_days_from(D("2026-06-01"), 60)
        closes = list(zip(days, move_path(days, 40, 0.015, 0.002)))
        del closes[30]
        m = self.market(closes)
        gate, info = m.condor_gate(nyse.next_td(days[-1]))
        self.assertIsNone(gate)
        self.assertEqual(info["why"], "missing_close")
        self.assertEqual(m.missing_spy_closes_for_gate(nyse.next_td(days[-1])), [days[30]])

    def test_equal_to_the_median_is_not_open(self):
        """Strictly below: a flat price path has RV20 == median == 0."""
        days = trading_days_from(D("2026-06-01"), 60)
        m = self.market([(d, 600.0) for d in days])
        gate, info = m.condor_gate(nyse.next_td(days[-1]))
        self.assertFalse(gate)
        self.assertEqual(info["rv20"], info["median"])

    def test_today_is_never_used(self):
        """Changing the entry day's own close cannot change the gate (it uses closes through the previous session)."""
        days = trading_days_from(D("2026-06-01"), 61)
        base = move_path(days, 40, 0.015, 0.002)
        d = days[-1]
        a = self.market(list(zip(days, base)))
        b_closes = list(zip(days, base[:-1] + [base[-1] * 1.2]))
        b = self.market(b_closes)
        self.assertEqual(a.condor_gate(d)[0], b.condor_gate(d)[0])


@needs_numpy
class C3QuantileAgainstNumpy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load_seed()
        s = cls.m.indices["VIX"]
        cls.dates, cls.v = list(s.dates), list(s.values)

    def test_trailing_quantile_matches_numpy_for_every_day(self):
        n_defined = 0
        for i, d in enumerate(self.dates):
            prev = self.v[max(0, i - 252):i]                 # the previous 252 published closes, the day itself excluded
            got = self.m.trailing_quantile("VIX", d, 0.25, 252, 200)
            if len(prev) < 200:
                self.assertIsNone(got, d)
                continue
            n_defined += 1
            self.assertAlmostEqual(got, float(np.quantile(prev, 0.25)), places=10, msg=str(d))
        self.assertGreater(n_defined, 50)

    def test_needs_200_prior_values(self):
        s = md.Series([(D("2026-01-01") + dt.timedelta(days=i), 15.0 + (i % 7)) for i in range(260)])
        m = md.Market(indices={"VIX": s})
        d199 = s.dates[199]                                  # 199 values before it
        d200 = s.dates[200]
        self.assertIsNone(m.trailing_quantile("VIX", d199, 0.25))
        self.assertIsNotNone(m.trailing_quantile("VIX", d200, 0.25))

    def test_window_is_at_most_252(self):
        s = md.Series([(D("2025-01-01") + dt.timedelta(days=i), float(i)) for i in range(400)])
        m = md.Market(indices={"VIX": s})
        d = s.dates[399]
        prev252 = s.values[147:399]
        self.assertEqual(len(prev252), 252)
        self.assertAlmostEqual(m.trailing_quantile("VIX", d, 0.25), float(np.quantile(prev252, 0.25)), places=12)

    def test_holiday_rows_published_by_cboe_count_as_published_closes(self):
        """Cboe's VIX history has rows on NYSE holidays since 2022-05-30. The frozen rule says 'the previous 252 published
        VIX closes', and the bot keeps the series exactly as published. A sensitivity run over every first session of the
        month 2010-01..2026-10 found the gate identical with and without those rows (0 months differ), so the choice is
        immaterial to date; this test pins the convention so a change is deliberate."""
        start = D("2025-01-01")
        base = [(start + dt.timedelta(days=2 * i), 20.0 + (i % 5)) for i in range(300)]
        holiday_row = (start + dt.timedelta(days=2 * 290 + 1), 99.0)          # a date between two regular rows
        m_a = md.Market(indices={"VIX": base})
        m_b = md.Market(indices={"VIX": sorted(base + [holiday_row])})
        d = base[-1][0] + dt.timedelta(days=1)
        a, b = m_a.trailing_quantile("VIX", d, 0.999), m_b.trailing_quantile("VIX", d, 0.999)
        self.assertEqual(a, 24.0)
        self.assertGreater(b, 70.0)                                   # the extra published row is inside the window and counts


if __name__ == "__main__":
    unittest.main()
