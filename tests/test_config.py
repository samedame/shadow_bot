"""The frozen parameters, pinned against values transcribed by hand from the pre-registration text
(claude/options-sweep-top10-preregistration.md, frozen 2026-10-08T18:53:59Z, SHA-256 57dd3806...67ce10) and the build spec.

Nothing here is imported from config.py on the right-hand side: a parameter that drifts in config.py fails on purpose. Change
a pinned value only together with a written reason in docs/DESIGN.md."""
import re
import unittest

from tests import helpers as h  # noqa: F401  (sets sys.path)

import config as cfg


class FrozenStrategyParameters(unittest.TestCase):
    def test_c9_condor(self):
        c = cfg.C9
        self.assertEqual(c["name"], "CONDOR_1D")
        self.assertEqual(c["delta"], 0.16)            # "0.16-delta shorts"
        self.assertEqual(c["wing"], 0.01)             # "wings 1.0% of spot"
        self.assertEqual(c["rv_window"], 20)          # "20-day realised vol"
        self.assertEqual(c["gate_window"], 20)        # "below its own 20-day median"
        self.assertEqual(c["dte"], 1)                 # "1-DTE", held to expiry

    def test_c6_non_equity_vrp(self):
        c = cfg.C6
        self.assertEqual(c["name"], "NONEQ_VRP")
        self.assertEqual(c["delta"], 0.25)            # "short 0.25 delta"
        self.assertEqual(c["width_frac"], 0.02)       # "2% wide"
        self.assertEqual(c["width_min"], 1)
        self.assertEqual(c["dte"], 30)                # "30-day bull put spread"
        self.assertEqual(c["profit_frac"], 0.5)       # "exit at 50% of credit"
        self.assertEqual(c["exit_dte"], 7)            # "or 7 DTE"
        self.assertEqual(c["vol_index"], {"GLD": "GVZ", "TLT": "VXTLT"})
        self.assertEqual(c["half_spread_abs"], 0.03)
        self.assertEqual(c["half_spread_rel"], 0.04)
        self.assertEqual(c["strike_scan_lo"], 0.80)

    def test_c3_spy_hedge(self):
        c = cfg.C3
        self.assertEqual(c["name"], "SPY_HEDGE")
        self.assertEqual(c["long_frac"], 0.95)        # "put spread 0.95/0.85 of spot"
        self.assertEqual(c["short_frac"], 0.85)
        self.assertEqual(c["dte_target"], 60)         # "60-DTE"
        self.assertEqual(c["dte_range"], (50, 75))
        self.assertEqual(c["exit_dte"], 21)           # "exit at 21 DTE"
        self.assertEqual(c["ratio_max"], 0.90)        # "VIX/VIX3M < 0.90"
        self.assertEqual(c["quantile"], 0.25)         # "25th percentile"
        self.assertEqual(c["window"], 252)            # "trailing-252-day"
        self.assertEqual(c["min_obs"], 200)

    def test_fills_fees_and_liquidity(self):
        self.assertEqual((cfg.K_MID, cfg.K_BASE, cfg.K_NAT, cfg.K_X3), (0.0, 0.25, 0.5, 0.75))     # base fill: mid +/- 25% of spread
        self.assertEqual((cfg.FEE_BASE, cfg.FEE_STRESS), (0.05, 0.65))
        self.assertEqual((cfg.SPREAD_ABS, cfg.SPREAD_REL), (0.05, 0.30))        # spread <= max($0.05, 30% of mid)
        self.assertEqual(cfg.MULT, 100)
        self.assertEqual(cfg.DELTA_TOL, 0.10)
        self.assertEqual(cfg.MAX_DTE, 130)
        self.assertEqual(cfg.MAX_MONEYNESS, 0.35)

    def test_frozen_dict_is_the_live_config(self):
        """FROZEN is what the other pins read; it must be the very objects the rules use."""
        self.assertIs(cfg.FROZEN["C9"], cfg.C9)
        self.assertIs(cfg.FROZEN["C6"], cfg.C6)
        self.assertIs(cfg.FROZEN["C3"], cfg.C3)


class StudyBaselines(unittest.TestCase):
    """The per-trade means the kill-switch compares against: verdict table (holdout 2022-2025, base cost model)."""

    def test_c9(self):
        b = cfg.BACKTEST["C9"]
        self.assertEqual(b["trades"], 499)
        self.assertAlmostEqual(913 / 499, 1.83, places=2)        # +$913 on 499 trades
        self.assertEqual(b["edge_base"], 1.83)
        self.assertEqual(b["edge_nat"], 0.71)                    # "+$0.71 at the bid/ask"
        self.assertEqual(b["dd_stop"], 3908.0)                   # 1.5 x the worst holdout drawdown $2,606
        self.assertEqual(b["worst_dd"], 2606.0)
        self.assertAlmostEqual(1.5 * 2606.0, b["dd_stop"], delta=1.5)      # the worst drawdown is shown rounded

    def test_c6(self):
        b = cfg.BACKTEST["C6"]
        self.assertEqual(b["trades"], 418)
        self.assertAlmostEqual(3326 / 418, 7.96, places=2)       # +$3,326 on 418 trades
        self.assertEqual(b["edge_base"], 7.96)
        self.assertEqual(b["edge_nat"], -0.34)                   # "-$143 at the bid/ask" / 418
        self.assertAlmostEqual(-143 / 418, -0.34, places=2)
        self.assertEqual(b["dd_stop"], 2920.0)
        self.assertEqual(b["worst_dd"], 1946.0)
        self.assertAlmostEqual(1.5 * 1946.0, b["dd_stop"], delta=1.5)

    def test_c3(self):
        b = cfg.BACKTEST["C3"]
        self.assertEqual(b["trades"], 13)
        self.assertAlmostEqual(614 / 13, 47.0, places=0)         # +$614 on 13 trades
        self.assertEqual(b["edge_base"], 47.0)
        self.assertIsNone(cfg.C3_HEDGE_BUDGET)                   # the stop is the user's number, not set yet

    def test_official_close_baseline_for_c9(self):
        """The bot settles at the official SPY close; the like-for-like baseline is the study's re-run at that settlement."""
        b = cfg.BACKTEST["C9"]
        self.assertEqual((b["edge_base_official"], b["edge_nat_official"]), (1.47, 0.35))


class ScheduleAndFiles(unittest.TestCase):
    def test_schedule(self):
        self.assertEqual(cfg.JOB_ID, "shadow-daily")
        self.assertEqual((cfg.TARGET_ET, cfg.USABLE_UNTIL_ET), ("16:20", "16:30"))
        self.assertEqual(cfg.SESSION_CLOSE_ET, "16:00")
        self.assertEqual(cfg.EARLY_CLOSE_ET, "13:00")
        self.assertEqual(cfg.CLOSE_SNAPSHOT_EARLIEST_ET, "16:05")
        self.assertLessEqual(cfg.WAIT_MAX_S, 10 * 60)             # the wait can never run past the usable-until minute

    def test_files_are_not_the_live_bots(self):
        """Shadow state must never collide with the live condor bot's files."""
        for name in (cfg.STATE_FILE, cfg.LOG_FILE, cfg.EXPORT_FILE, cfg.GUARD_FILE):
            self.assertNotIn(name, ("state.json", "pipeline_state.json", "options_state.json", ".run_guard.json"))
        self.assertTrue(cfg.STATE_FILE.startswith("shadow_"))
        self.assertTrue(cfg.GUARD_FILE.endswith("_shadow.json"))

    def test_banner(self):
        self.assertEqual(cfg.BANNER, "SHADOW - NOT A TRADE")

    def test_urls_carry_no_account_details(self):
        """No config string may contain an e-mail address (the sources are queried with a generic browser User-Agent)."""
        email = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
        for name in dir(cfg):
            v = getattr(cfg, name)
            if isinstance(v, str):
                self.assertIsNone(email.search(v), name)
        self.assertNotIn("@", cfg.USER_AGENT)
        self.assertTrue(cfg.USER_AGENT.startswith("Mozilla/5.0"))

    def test_t_bill_fallback_is_the_discount_column_not_par_yield(self):
        """The par-yield '3 Mo' column runs about 0.2 pt above FRED DTB3; the bank-discount column is the same quantity."""
        self.assertEqual(cfg.TREASURY_BILL_COLUMN, "13 WEEKS BANK DISCOUNT")
        self.assertIn("daily_treasury_bill_rates", cfg.TREASURY_BILLS_URL)
        self.assertNotIn("par_yield", cfg.TREASURY_BILLS_URL)


class SourceHygiene(unittest.TestCase):
    """The shadow repo must never read the live bots' secrets or write to their files."""

    def test_no_module_reads_the_live_webhook(self):
        import glob
        import os
        for path in glob.glob(os.path.join(h.ROOT, "*.py")):
            with open(path, encoding="utf-8") as f:
                text = f.read()
            for m in re.finditer(r"""["']DISCORD_WEBHOOK_URL["']""", text):
                self.fail(f"{os.path.basename(path)} references the live DISCORD_WEBHOOK_URL")

    def test_no_order_placing_code(self):
        import glob
        import os
        banned = ("robin_stocks", "alpaca", "ib_insync", "place_order", "submit_order", "orders.post", "schwab", "tradier")
        for path in glob.glob(os.path.join(h.ROOT, "*.py")):
            with open(path, encoding="utf-8") as f:
                text = f.read().lower()
            for word in banned:
                self.assertNotIn(word, text, f"{os.path.basename(path)} mentions {word}")


if __name__ == "__main__":
    unittest.main()
