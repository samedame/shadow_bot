"""Kill-switch adapted to shadow mode (kill_switch.py): flag, alert once per reason, keep logging, never retire by itself.

Thresholds are the config's proposals; the expected numbers are worked out from them in the comments."""
import unittest
from unittest import mock

import config as cfg
import kill_switch as ks
import state as S

T0 = "2026-10-13T16:21:00-04:00"
T1 = "2026-10-14T16:21:00-04:00"
T2 = "2026-10-15T16:21:00-04:00"


def block(sid="C9", recent=None, closed=None, dd=0.0, premium=0.0, flags=()):
    b = S.new_strategy_block(sid)
    recent = list(recent or [])
    b["recent_pnl_base"] = recent
    b["closed_n"] = len(recent) if closed is None else closed
    b["primed"] = b["closed_n"] >= cfg.PRIMED_MIN_TRADES
    b["drawdown"] = dd
    b["premium_paid_since_reset"] = premium
    for i, f in enumerate(flags):
        S.add_quality(b, f"2026-09-{i + 1:02d}", f, 0.5 if f else 0.05, "ticket")
    return b


class EdgeCheck(unittest.TestCase):
    # C9: 25% of the backtest edge $1.83 = $0.4575.  C6: 25% of $7.96 = $1.99.
    def test_floor_values(self):
        self.assertAlmostEqual(cfg.EDGE_FLOOR_FRACTION * cfg.BACKTEST["C9"]["edge_base"], 0.4575)
        self.assertAlmostEqual(cfg.EDGE_FLOOR_FRACTION * cfg.BACKTEST["C6"]["edge_base"], 1.99)

    def test_trips_below_the_floor_once_primed(self):
        b = block("C9", [0.40] * 20)
        r = ks.active_reasons("C9", b)
        self.assertEqual(set(r), {"edge"})
        self.assertIn("$+0.40/trade", r["edge"])
        self.assertIn("$0.46", r["edge"])
        self.assertIn("$1.83", r["edge"])

    def test_exactly_at_the_floor_does_not_trip(self):
        """Strictly below the floor trips, equal does not. Dyadic numbers keep the arithmetic exact on every Python version
        (sum() changed its rounding in 3.12; the kill-switch uses math.fsum)."""
        with mock.patch.dict(cfg.BACKTEST["C9"], {"edge_base": 4.0}):                  # floor = 25% of 4.00 = 1.00 exactly
            self.assertEqual(ks.active_reasons("C9", block("C9", [1.0] * 20)), {})
            self.assertEqual(ks.active_reasons("C9", block("C9", [1.5] * 20)), {})
            self.assertEqual(set(ks.active_reasons("C9", block("C9", [0.9999] * 20))), {"edge"})
        self.assertEqual(ks.active_reasons("C9", block("C9", [0.46] * 20)), {})
        self.assertEqual(set(ks.active_reasons("C9", block("C9", [0.4574] * 20))), {"edge"})

    def test_the_mean_is_exact_on_every_python_version(self):
        """Twenty trades of exactly $0.30 against a floor of exactly $0.30 (25% of $1.20). A left-to-right float sum gives
        5.999999999999998 - 'below the floor' - on Python 3.11; math.fsum gives 6.0 on every version."""
        naive = 0.0
        for x in [0.3] * 20:
            naive += x
        self.assertLess(naive, 6.0)
        with mock.patch.dict(cfg.BACKTEST["C9"], {"edge_base": 1.2}):
            self.assertEqual(ks.active_reasons("C9", block("C9", [0.3] * 20)), {})

    def test_not_primed_never_trips(self):
        self.assertEqual(ks.active_reasons("C9", block("C9", [-50.0] * 19)), {})

    def test_primed_but_short_window_never_trips(self):
        b = block("C9", [-50.0] * 10, closed=25)               # 25 closed trades but only 10 in the trailing window
        self.assertEqual(ks.active_reasons("C9", b), {})

    def test_uses_only_the_last_twenty(self):
        b = S.new_strategy_block("C9")
        for pnl in [-100.0] * 5 + [5.0] * 20:                  # five early losses, then twenty small wins
            S.update_stats(b, pnl, pnl, pnl)
        self.assertEqual((b["closed_n"], len(b["recent_pnl_base"]), b["primed"]), (25, 20, True))
        self.assertEqual(ks.active_reasons("C9", b), {}, "the early losses have left the trailing window (and 500 < the stop)")

    def test_c6_has_its_own_floor(self):
        self.assertEqual(set(ks.active_reasons("C6", block("C6", [1.9] * 20))), {"edge"})
        self.assertEqual(ks.active_reasons("C6", block("C6", [2.0] * 20)), {})

    def test_c3_has_no_edge_check(self):
        self.assertEqual(ks.active_reasons("C3", block("C3", [-500.0] * 20)), {})


class DrawdownCheck(unittest.TestCase):
    def test_c9_stop(self):
        self.assertEqual(ks.active_reasons("C9", block("C9", dd=3908.0)), {})
        r = ks.active_reasons("C9", block("C9", dd=3908.01))
        self.assertEqual(set(r), {"drawdown"})
        self.assertIn("$3,908", r["drawdown"])
        self.assertIn("$2,606", r["drawdown"])

    def test_c6_stop(self):
        self.assertEqual(ks.active_reasons("C6", block("C6", dd=2920.0)), {})
        self.assertEqual(set(ks.active_reasons("C6", block("C6", dd=2920.5))), {"drawdown"})

    def test_it_does_not_wait_for_priming(self):
        b = block("C9", [], dd=5000.0)
        self.assertFalse(b["primed"])
        self.assertEqual(set(ks.active_reasons("C9", b)), {"drawdown"})

    def test_c3_ignores_the_drawdown_field(self):
        self.assertEqual(ks.active_reasons("C3", block("C3", dd=1e9)), {})


class PremiumCheck(unittest.TestCase):
    def test_not_evaluated_while_the_budget_is_unset(self):
        self.assertIsNone(cfg.C3_HEDGE_BUDGET)
        self.assertEqual(ks.active_reasons("C3", block("C3", premium=1e9)), {})

    def test_evaluated_once_the_user_sets_one(self):
        with mock.patch.object(cfg, "C3_HEDGE_BUDGET", 2000.0):
            self.assertEqual(ks.active_reasons("C3", block("C3", premium=2000.0)), {})
            r = ks.active_reasons("C3", block("C3", premium=2000.01))
            self.assertEqual(set(r), {"premium"})
            self.assertIn("$2,000", r["premium"])
            self.assertEqual(ks.active_reasons("C9", block("C9", premium=1e9)), {}, "only C3 has a premium budget")


class QualityCheck(unittest.TestCase):
    # "more than 20% of the last 20 signals": more than 4 flagged
    def test_four_flagged_of_twenty_is_fine(self):
        self.assertEqual(ks.active_reasons("C9", block("C9", flags=[True] * 4 + [False] * 16)), {})

    def test_five_flagged_of_twenty_trips(self):
        r = ks.active_reasons("C9", block("C9", flags=[True] * 5 + [False] * 15))
        self.assertEqual(set(r), {"quality"})
        self.assertIn("5 of the last 20", r["quality"])
        self.assertIn("30%", r["quality"])

    def test_only_the_last_twenty_count(self):
        b = block("C9", flags=[True] * 10 + [False] * 20)
        self.assertEqual(len(b["quality"]), 20)
        self.assertEqual(ks.active_reasons("C9", b), {})

    def test_the_count_is_absolute_so_five_flags_in_five_signals_trips(self):
        """With fewer than 20 signals the rule still needs MORE THAN 4 flagged (not 20% of however many there are)."""
        self.assertEqual(set(ks.active_reasons("C9", block("C9", flags=[True] * 5))), {"quality"})
        self.assertEqual(ks.active_reasons("C9", block("C9", flags=[True] * 4)), {})

    def test_applies_to_every_strategy(self):
        for sid in cfg.STRATEGIES:
            self.assertEqual(set(ks.active_reasons(sid, block(sid, flags=[True] * 6))), {"quality"}, sid)


class Evaluate(unittest.TestCase):
    def test_first_trip_flags_and_reports_once(self):
        b = block("C9", dd=4000.0)
        r = ks.evaluate("C9", b, T0)
        self.assertEqual(set(r["new"]), {"drawdown"})
        self.assertEqual(set(r["active"]), {"drawdown"})
        self.assertEqual(r["status"], "flagged")
        self.assertEqual(b["status"], "flagged")
        self.assertEqual(b["kill_alerts"], {"drawdown": T0})
        r = ks.evaluate("C9", b, T1)
        self.assertEqual(r["new"], {}, "the same reason is not reported twice")
        self.assertEqual(set(r["active"]), {"drawdown"})
        self.assertEqual(b["kill_alerts"], {"drawdown": T0}, "the ledger keeps the first time")

    def test_a_second_reason_is_reported_on_its_own(self):
        b = block("C9", dd=4000.0)
        ks.evaluate("C9", b, T0)
        b["quality"] = [{"date": "d", "flag": True, "med_rel_spread": 0.5, "why": ""}] * 6
        r = ks.evaluate("C9", b, T1)
        self.assertEqual(set(r["new"]), {"quality"})
        self.assertEqual(set(b["kill_alerts"]), {"drawdown", "quality"})

    def test_both_reasons_at_once(self):
        b = block("C9", [0.0] * 20, dd=4000.0)
        r = ks.evaluate("C9", b, T0)
        self.assertEqual(set(r["new"]), {"edge", "drawdown"})

    def test_a_cleared_reason_re_alerts_when_it_comes_back(self):
        b = block("C9", dd=4000.0)
        ks.evaluate("C9", b, T0)
        b["drawdown"] = 100.0
        r = ks.evaluate("C9", b, T1)
        self.assertEqual((r["new"], r["active"]), ({}, {}))
        self.assertEqual(b["kill_alerts"], {}, "a reason that cleared leaves the ledger")
        self.assertEqual(b["status"], "flagged", "the flag is sticky: only the user clears it")
        b["drawdown"] = 4100.0
        r = ks.evaluate("C9", b, T2)
        self.assertEqual(set(r["new"]), {"drawdown"})
        self.assertEqual(b["kill_alerts"], {"drawdown": T2})

    def test_nothing_tripped_changes_nothing(self):
        b = block("C9", [3.0] * 20)
        r = ks.evaluate("C9", b, T0)
        self.assertEqual((r["new"], r["active"], r["status"]), ({}, {}, "active"))
        self.assertEqual(b["kill_alerts"], {})

    def test_retired_strategies_are_left_alone(self):
        b = block("C9", dd=1e6)
        b["status"] = "retired"
        r = ks.evaluate("C9", b, T0)
        self.assertEqual(r, {"new": {}, "active": {}, "status": "retired"})
        self.assertEqual((b["status"], b["kill_alerts"]), ("retired", {}))

    def test_a_trip_never_retires(self):
        b = block("C9", dd=1e6)
        ks.evaluate("C9", b, T0)
        self.assertEqual(b["status"], "flagged")
        for _ in range(5):
            ks.evaluate("C9", b, T1)
        self.assertEqual(b["status"], "flagged")

    def test_the_user_resetting_the_flag_while_the_condition_persists_does_not_re_alert(self):
        b = block("C9", dd=4000.0)
        ks.evaluate("C9", b, T0)
        b["status"] = "active"                                  # the user edits shadow_state.json
        r = ks.evaluate("C9", b, T1)
        self.assertEqual(r["new"], {})
        self.assertEqual(b["status"], "active", "no new reason, so no new flag")

    def test_a_flagged_strategy_with_a_new_reason_stays_flagged(self):
        b = block("C9", dd=4000.0)
        b["status"] = "flagged"
        r = ks.evaluate("C9", b, T0)
        self.assertEqual((set(r["new"]), b["status"]), ({"drawdown"}, "flagged"))

    def test_status_names(self):
        self.assertEqual(cfg.STATUSES, ("active", "flagged", "retired"))


class Summary(unittest.TestCase):
    def test_not_primed(self):
        b = block("C9", [2.0, 4.0], dd=150.0)
        line = ks.summary_line("C9", b)
        self.assertEqual(line, "status active | n=2 | not primed (2/20) | trailing-2 mean $+3.00 | drawdown $150 / stop $3,908")

    def test_primed(self):
        line = ks.summary_line("C6", block("C6", [1.0] * 20, dd=0.0))
        self.assertIn("| primed |", line)
        self.assertIn("trailing-20 mean $+1.00", line)
        self.assertIn("stop $2,920", line)

    def test_no_trades_yet(self):
        line = ks.summary_line("C9", block("C9"))
        self.assertNotIn("trailing", line)
        self.assertIn("n=0", line)

    def test_c3_shows_the_premium_and_an_unset_budget(self):
        line = ks.summary_line("C3", block("C3", premium=646.35))
        self.assertIn("premium since reset $646 / budget unset", line)
        with mock.patch.object(cfg, "C3_HEDGE_BUDGET", 3000.0):
            self.assertIn("budget $3,000", ks.summary_line("C3", block("C3")))


class Alerts(unittest.TestCase):
    def test_the_alert_message_says_nothing_was_paused_or_traded(self):
        import messages
        b = block("C9", dd=4000.0)
        r = ks.evaluate("C9", b, T0)
        msg = messages.kill_alert("C9", b, "drawdown", r["new"]["drawdown"], "active")
        self.assertTrue(msg["plain"].startswith(cfg.BANNER))
        self.assertIn("FLAGGED: drawdown", msg["plain"])
        self.assertIn("Shadow logging CONTINUES", msg["plain"])
        self.assertIn("nothing was traded", msg["plain"])
        self.assertIn("not backtested", msg["plain"])
        self.assertIn('strategies.C9.status to "retired"', msg["plain"])


if __name__ == "__main__":
    unittest.main()
