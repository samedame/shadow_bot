"""NYSE calendar: the rule generator against the literal 2026-2028 tables, spot checks against known sessions, and two
independent date sources (SPY daily bars 2016-10-12..2026-10-12 and Cboe VIX history 1998..2026)."""
import datetime as dt
import gzip
import os
import unittest

from tests.helpers import D, FIXTURES

import nyse_calendar as nyse


def read_dates(name):
    with gzip.open(os.path.join(FIXTURES, name), "rt") as f:
        return [D(line.strip()) for line in f if line.strip() and not line.startswith("#")]


class RulesMatchTables(unittest.TestCase):
    def test_generator_reproduces_the_literal_tables(self):
        for year, listed in nyse.EXPLICIT_HOLIDAYS.items():
            self.assertEqual(sorted(nyse.rule_holidays(year)), sorted(D(s) for s in listed), f"holidays {year}")
        for year, listed in nyse.EXPLICIT_EARLY_CLOSES.items():
            self.assertEqual(sorted(nyse.rule_early_closes(year)), sorted(D(s) for s in listed), f"early closes {year}")

    def test_range_guard(self):
        for bad in (D("1997-12-31"), D("2029-01-01")):
            with self.assertRaises(nyse.CalendarRange):
                nyse.is_trading_day(bad)
            with self.assertRaises(nyse.CalendarRange):
                nyse.next_td(bad)
        self.assertTrue(issubclass(nyse.CalendarRange, ValueError))
        self.assertTrue(nyse.is_trading_day(D("2028-12-29")))
        with self.assertRaises(nyse.CalendarRange):
            nyse.next_td(D("2028-12-29"), 2)          # would fall past the verified range


class KnownSessions(unittest.TestCase):
    def test_2026_closures(self):
        closed = ["2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19", "2026-07-03", "2026-09-07",
                  "2026-11-26", "2026-12-25"]
        for s in closed:
            self.assertFalse(nyse.is_trading_day(D(s)), s)
        self.assertEqual(len([d for d in nyse.holidays() if d.year == 2026 and d.weekday() < 5]), len(closed))

    def test_open_days_that_look_like_holidays(self):
        for s in ("2026-10-12",          # Columbus Day: exchanges are open
                  "2026-11-11",          # Veterans Day: open
                  "2026-07-02", "2026-12-31", "2027-12-31", "2028-01-03"):
            self.assertTrue(nyse.is_trading_day(D(s)), s)

    def test_weekends_closed(self):
        self.assertFalse(nyse.is_trading_day(D("2026-10-10")))
        self.assertFalse(nyse.is_trading_day(D("2026-10-11")))

    def test_2027_2028_edge_years(self):
        self.assertFalse(nyse.is_trading_day(D("2027-01-01")))       # Friday
        self.assertFalse(nyse.is_trading_day(D("2027-12-24")))       # Christmas Saturday -> observed Friday 24 Dec
        self.assertTrue(nyse.is_trading_day(D("2027-12-31")))        # 1 Jan 2028 is a Saturday: NOT observed on 31 Dec
        self.assertFalse(nyse.is_trading_day(D("2028-04-14")))       # Good Friday
        self.assertFalse(nyse.is_trading_day(D("2028-07-04")))
        self.assertTrue(nyse.is_early_close(D("2028-07-03")))

    def test_early_closes(self):
        self.assertEqual(sorted(d for d in nyse.EXPLICIT_EARLY_CLOSES[2026]), ["2026-11-27", "2026-12-24"])
        for s in ("2026-11-27", "2026-12-24", "2027-11-26", "2028-11-24"):
            self.assertTrue(nyse.is_early_close(D(s)), s)
            self.assertTrue(nyse.is_trading_day(D(s)), s)
        self.assertFalse(nyse.is_early_close(D("2026-12-23")))
        self.assertFalse(nyse.is_early_close(D("2026-07-02")))       # 3 Jul 2026 is the observed holiday

    def test_navigation(self):
        self.assertEqual(nyse.next_td(D("2026-10-13")), D("2026-10-14"))
        self.assertEqual(nyse.next_td(D("2026-10-09")), D("2026-10-12"))         # Friday -> Monday
        self.assertEqual(nyse.next_td(D("2026-11-25")), D("2026-11-27"))         # over Thanksgiving
        self.assertEqual(nyse.prev_td(D("2026-10-12")), D("2026-10-09"))
        self.assertEqual(nyse.next_td(D("2026-10-10")), D("2026-10-12"))         # from a weekend
        self.assertEqual(nyse.next_td(D("2026-10-12"), 5), D("2026-10-19"))
        # counted by hand: 12, 9, 8, 7, 6, 5, 2, 1 Oct; 30, 29, 28, 25, 24, 23, 22, 21, 18, 17, 16, 15 Sep -> the 20th session back
        self.assertEqual(nyse.td_offset(D("2026-10-13"), -20), D("2026-09-15"))
        self.assertEqual(nyse.td_offset(D("2026-10-13"), 0), D("2026-10-13"))
        with self.assertRaises(ValueError):
            nyse.td_offset(D("2026-10-10"), 1)                                   # a Saturday is not a session
        self.assertEqual(nyse.last_session_on_or_before(D("2026-10-11")), D("2026-10-09"))
        self.assertEqual([str(d) for d in nyse.trading_days_between(D("2026-10-08"), D("2026-10-13"))],
                         ["2026-10-09", "2026-10-12", "2026-10-13"])

    def test_first_and_last_session_of_week_and_month(self):
        self.assertTrue(nyse.is_first_td_of_week(D("2026-10-12")))               # Monday
        self.assertFalse(nyse.is_first_td_of_week(D("2026-10-13")))
        self.assertTrue(nyse.is_first_td_of_week(D("2026-09-08")))               # Monday 7 Sep is Labor Day: Tuesday is first
        self.assertFalse(nyse.is_first_td_of_week(D("2026-09-07")))              # not a session at all
        self.assertTrue(nyse.is_last_td_of_week(D("2026-10-09")))
        self.assertTrue(nyse.is_last_td_of_week(D("2026-04-02")))                # Good Friday 3 Apr: Thursday is last
        self.assertTrue(nyse.is_first_td_of_month(D("2026-10-01")))
        self.assertTrue(nyse.is_first_td_of_month(D("2026-11-02")))              # 1 Nov is a Sunday
        self.assertTrue(nyse.is_first_td_of_month(D("2027-01-04")))              # 1 Jan Friday closed, weekend, Monday
        self.assertFalse(nyse.is_first_td_of_month(D("2026-10-02")))
        self.assertTrue(nyse.is_first_td_of_month(D("2026-12-01")))


class AgainstIndependentDateLists(unittest.TestCase):
    def test_spy_bars_ten_years(self):
        """SPY traded exactly on the calendar's sessions from 2016-10-12 to 2026-10-12 (2,513 of them)."""
        spy = read_dates("spy_dates_2016-10-12_2026-10-12.txt.gz")
        cal = nyse.trading_days_between(D("2016-10-11"), D("2026-10-12"))
        self.assertEqual(len(spy), 2513)
        self.assertEqual(spy, cal)

    def test_vix_history_dates_since_1998(self):
        """Cboe's VIX history has a row on every session; the calendar agrees except for two quirks of the VIX file, and the
        file carries extra rows on NYSE holidays from 2022-05-30 on (33 of them through 2026-10-12)."""
        vix = read_dates("vix_dates_1998_2026-10-12.txt.gz")
        cal = nyse.trading_days_between(D("1998-01-01"), D("2026-10-12"))
        vix = [d for d in vix if d > D("1998-01-01")]
        cal = [d for d in cal if d > D("1998-01-01")]
        cut = D("2022-05-27")
        v_old, c_old = {d for d in vix if d <= cut}, {d for d in cal if d <= cut}
        self.assertEqual(sorted(v_old - c_old), [D("2004-06-11")])               # NYSE was closed (Reagan day of mourning)
        self.assertEqual(sorted(c_old - v_old), [D("1999-12-31")])               # NYSE traded; the VIX file has no row
        v_new, c_new = {d for d in vix if d > cut}, {d for d in cal if d > cut}
        self.assertEqual(c_new - v_new, set())                                   # no session missing
        extra = sorted(v_new - c_new)
        self.assertEqual(len(extra), 33)
        for d in extra:
            self.assertLess(d.weekday(), 5, d)                                   # weekday rows on exchange holidays, not weekends
            self.assertIn(d, nyse.holidays(), d)

    def test_calendar_sessions_per_year_are_plausible(self):
        for year in range(2010, 2029):
            n = len([d for d in nyse.trading_days() if d.year == year])
            self.assertIn(n, (250, 251, 252, 253, 254), f"{year}: {n}")


if __name__ == "__main__":
    unittest.main()
