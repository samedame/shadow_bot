"""The shadow-only state export (export_state.py): same field set as the live book's cross-bot export, but impossible to mistake
for it, and impossible to feed into the live combined-drawdown monitor by accident."""
import json
import os
import unittest

from tests.helpers import Base

import config as cfg
import export_state as ex
import state as S

GEN = "2026-10-15T20:30:00+00:00"


def row(**kw):
    r = {k: "" for k in S.LOG_COLUMNS}
    r.update(id="C9-2026-10-13", strategy="C9", sleeve="SPY", version="real", entry_date="2026-10-13", exit_date="2026-10-14",
            exit_reason="expiry", pnl_mid="176.0", pnl_base="172.6", pnl_nat="167.0")
    r.update(kw)
    return r


def sample_state():
    st = S.new_state()
    c9 = st["strategies"]["C9"]
    S.update_stats(c9, 172.6, 167.0, 176.0)
    S.update_stats(c9, -427.2, -440.0, -420.0)
    c9["open_positions"].append({
        "id": "C9-2026-10-15", "sleeve": "SPY", "version": "real", "entry_date": "2026-10-15", "expiry": "2026-10-16",
        "max_loss": 427.2, "legs": [{"label": "SELL 1 604C 2026-10-16"}, {"label": "BUY 1 610C 2026-10-16"}]})
    c3 = st["strategies"]["C3"]
    c3["premium_paid_since_reset"] = 646.35
    return st


class Export(Base):
    def build(self, rows=None):
        return ex.build(sample_state(), rows if rows is not None else [row()], GEN)

    def test_top_level_is_not_the_live_monitors_shape(self):
        snap = self.build()
        self.assertNotIn("history", snap, "the live monitor reads a top-level `history`; this file must not have one")
        self.assertTrue(snap["shadow"])
        self.assertEqual(snap["banner"], cfg.BANNER)
        self.assertIn("Not for the live combined-drawdown monitor", snap["note"])
        self.assertEqual(snap["generated_at"], GEN)
        self.assertEqual(set(snap["strategies"]), {"C9", "C6", "C3"})

    def test_every_record_is_marked_shadow(self):
        snap = self.build()

        def walk(o, path="$"):
            if isinstance(o, dict):
                if any(k in o for k in ("strategy_id", "legs", "net_pnl_base", "entry_date")):
                    self.assertIs(o.get("shadow"), True, f"{path} must carry shadow: true")
                for k, v in o.items():
                    walk(v, f"{path}.{k}")
            elif isinstance(o, list):
                for i, v in enumerate(o):
                    walk(v, f"{path}[{i}]")
        walk(snap)
        for sid, rec in snap["strategies"].items():
            self.assertIs(rec["shadow"], True, sid)

    def test_fields(self):
        snap = self.build()
        c9 = snap["strategies"]["C9"]
        self.assertEqual((c9["strategy_id"], c9["name"], c9["status"], c9["primed"]), ("C9", "CONDOR_1D", "active", False))
        self.assertAlmostEqual(c9["cumulative_pnl_base"], -254.6)
        self.assertEqual((c9["running_peak"], c9["trades_since_reset"]), (172.6, 2))
        self.assertAlmostEqual(c9["drawdown"], 427.2)
        self.assertAlmostEqual(c9["max_drawdown"], 427.2)
        closed = c9["closed_history"]
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0], {"id": "C9-2026-10-13", "sleeve": "SPY", "entry_date": "2026-10-13", "exit_date": "2026-10-14",
                                     "exit_reason": "expiry", "net_pnl_base": 172.6, "net_pnl_natural": 167.0, "net_pnl_mid": 176.0,
                                     "fill_vs_mid": -3.4, "shadow": True})
        op = c9["open_positions"]
        self.assertEqual(op, [{"id": "C9-2026-10-15", "sleeve": "SPY", "version": "real", "entry_date": "2026-10-15",
                               "expiry": "2026-10-16", "legs": ["SELL 1 604C 2026-10-16", "BUY 1 610C 2026-10-16"],
                               "max_loss": 427.2, "shadow": True}])

    def test_only_c3_carries_premium_fields(self):
        snap = self.build()
        self.assertEqual(snap["strategies"]["C3"]["premium_paid_since_reset"], 646.35)
        self.assertIsNone(snap["strategies"]["C3"]["premium_budget"])
        self.assertNotIn("premium_paid_since_reset", snap["strategies"]["C9"])

    def test_model_rows_are_not_exported(self):
        rows = [row(), row(id="C6M-GLD-2026-10-12", strategy="C6", sleeve="GLD", version="model", pnl_base="7.0")]
        snap = self.build(rows)
        self.assertEqual(snap["strategies"]["C6"]["closed_history"], [], "the model path has no tradable position behind it")
        self.assertEqual(len(snap["strategies"]["C9"]["closed_history"]), 1)

    def test_rows_go_to_their_own_strategy(self):
        rows = [row(), row(id="C3-2026-09-01", strategy="C3", sleeve="SPY", pnl_base="-120.5", pnl_mid="-100.0", pnl_nat="-140.0")]
        snap = self.build(rows)
        self.assertEqual([r["id"] for r in snap["strategies"]["C3"]["closed_history"]], ["C3-2026-09-01"])
        self.assertEqual(snap["strategies"]["C3"]["closed_history"][0]["fill_vs_mid"], -20.5)

    def test_unpriced_exits_have_no_numbers_but_do_not_break_the_export(self):
        snap = self.build([row(exit_reason="unpriced", pnl_base="", pnl_mid="", pnl_nat="")])
        h = snap["strategies"]["C9"]["closed_history"][0]
        self.assertEqual((h["net_pnl_base"], h["net_pnl_natural"], h["net_pnl_mid"], h["fill_vs_mid"]), (None, None, None, None))

    def test_json_serialisable_and_nothing_secret(self):
        text = json.dumps(self.build())
        for word in ("webhook", "token", "@"):
            self.assertNotIn(word, text.lower())

    def test_the_file_is_written_atomically(self):
        d = self.tmp()
        sp, lp, op = (os.path.join(d, n) for n in ("s.json", "l.csv", "e.json"))
        S.save(sample_state(), sp)
        S.append_log([row()], lp)
        snap = ex.export(sp, lp, op)
        self.assertFalse(os.path.exists(op + ".tmp"))
        with open(op) as f:
            on_disk = json.load(f)
        self.assertEqual(on_disk["strategies"]["C9"]["closed_history"], snap["strategies"]["C9"]["closed_history"])
        self.assertNotIn("history", on_disk)

    def test_the_export_file_name_is_not_the_live_monitors(self):
        self.assertNotIn(cfg.EXPORT_FILE, ("options_state.json", "state.json", "pipeline_state.json"))
        self.assertTrue(cfg.EXPORT_FILE.startswith("shadow_"))


if __name__ == "__main__":
    unittest.main()
