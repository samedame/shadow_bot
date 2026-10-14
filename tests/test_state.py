"""Saved state, the trade log and the message outbox (state.py). The idempotency rules live here, so they are pinned hard."""
import csv
import datetime as dt
import json
import os
import unittest

from tests.helpers import Base, D, make_chain, rows_from

import config as cfg
import state as S


class NewState(Base):
    def test_shape(self):
        st = S.new_state()
        self.assertEqual(st["schema"], cfg.SCHEMA)
        self.assertEqual(set(st["strategies"]), {"C9", "C6", "C3"})
        for sid, b in st["strategies"].items():
            self.assertEqual(b["status"], "active", sid)
            self.assertFalse(b["primed"])
            self.assertEqual((b["closed_n"], b["cum_pnl_base"], b["peak"], b["drawdown"]), (0, 0.0, 0.0, 0.0))
            self.assertEqual((b["open_positions"], b["entries_done"], b["exit_alerts"], b["kill_alerts"]), ([], [], {}, {}))
            self.assertIsNone(b["started"])
        self.assertEqual(set(st["strategies"]["C6"]["sleeves"]), {"GLD", "TLT"})
        self.assertEqual(set(st["strategies"]["C6"]["model"]["sleeves"]), {"GLD", "TLT"})
        self.assertEqual(st["strategies"]["C6"]["pairs"], [])
        c3 = st["strategies"]["C3"]
        self.assertIsNone(c3["candidate"])
        self.assertEqual((c3["gate_log"], c3["hedge_ledger"], c3["premium_paid_since_reset"]), ([], [], 0.0))
        self.assertEqual((st["outbox"], st["sent"], st["skips"], st["skip_keys"], st["runs"], st["last_session"], st["notes"]),
                         ([], {}, {}, {}, {}, {}, {}))
        self.assertEqual(st["total_skips"], {"C9": {}, "C6": {}, "C3": {}})

    def test_two_new_states_share_nothing(self):
        a, b = S.new_state(), S.new_state()
        a["strategies"]["C9"]["entries_done"].append("x")
        a["outbox"].append({"key": "k"})
        self.assertEqual(b["strategies"]["C9"]["entries_done"], [])
        self.assertEqual(b["outbox"], [])

    def test_state_is_free_of_money_and_secrets(self):
        text = json.dumps(S.new_state()).lower()
        for word in ("webhook", "token", "password", "api_key", "@"):
            self.assertNotIn(word, text)


class SaveAndLoad(Base):
    def test_round_trip(self):
        path = os.path.join(self.tmp(), "s.json")
        st = S.new_state()
        st["strategies"]["C9"]["status"] = "flagged"
        st["strategies"]["C9"]["entries_done"].append("C9-2026-10-13")
        S.save(st, path)
        with open(path) as f:
            self.assertTrue(f.read().endswith("}\n"))
        self.assertFalse(os.path.exists(path + ".tmp"), "the atomic-write temp file must not be left behind")
        self.assertEqual(S.load(path), st)

    def test_missing_file_is_a_new_state(self):
        self.assertEqual(S.load(os.path.join(self.tmp(), "nope.json")), S.new_state())

    def test_older_files_get_new_keys_without_losing_values(self):
        """A state written by an earlier version (no last_session / notes / runs, blocks without their newer keys, a strategy
        missing altogether) still loads, and keeps what it had."""
        path = os.path.join(self.tmp(), "old.json")
        old = S.new_state()
        for k in ("last_session", "notes", "runs"):
            del old[k]
        del old["strategies"]["C3"]
        del old["total_skips"]["C3"]
        for k in ("pairs", "model", "sleeves"):
            old["strategies"]["C6"].pop(k)
        old["strategies"]["C9"]["closed_n"] = 7
        old["strategies"]["C9"]["cum_pnl_base"] = 12.5
        del old["strategies"]["C9"]["quality"]
        S.save(old, path)
        st = S.load(path)
        self.assertEqual((st["last_session"], st["notes"], st["runs"]), ({}, {}, {}))
        self.assertEqual(st["strategies"]["C9"]["closed_n"], 7)
        self.assertEqual(st["strategies"]["C9"]["cum_pnl_base"], 12.5)
        self.assertEqual(st["strategies"]["C9"]["quality"], [])
        self.assertEqual(st["strategies"]["C3"], S.new_strategy_block("C3"))
        self.assertEqual(st["total_skips"]["C3"], {})
        for k in ("pairs", "model", "sleeves"):
            self.assertEqual(st["strategies"]["C6"][k], S.new_strategy_block("C6")[k])

    def test_a_c3_block_missing_its_newer_keys_is_filled(self):
        path = os.path.join(self.tmp(), "old3.json")
        old = S.new_state()
        for k in ("hedge_ledger", "episode", "spy_peak", "spy_peak_date", "candidate", "gate_log"):
            old["strategies"]["C3"].pop(k)
        old["strategies"]["C3"]["premium_paid_since_reset"] = 646.35
        S.save(old, path)
        c3 = S.load(path)["strategies"]["C3"]
        self.assertEqual((c3["hedge_ledger"], c3["gate_log"], c3["candidate"], c3["episode"]), ([], [], None, None))
        self.assertEqual(c3["premium_paid_since_reset"], 646.35)

    def test_dates_are_saved_as_text(self):
        path = os.path.join(self.tmp(), "d.json")
        st = S.new_state()
        st["notes"]["d"] = D("2026-10-13")
        S.save(st, path)
        with open(path) as f:
            self.assertEqual(json.load(f)["notes"]["d"], "2026-10-13")


class CashFlowNames(Base):
    def test_named_and_back(self):
        cf = {0.0: 176.0, 0.25: 173.0, 0.5: 170.0, 0.75: 167.0}
        named = S.named_cf(cf)
        self.assertEqual(named, {"mid": 176.0, "base": 173.0, "nat": 170.0, "x3": 167.0})
        self.assertEqual(S.unnamed_cf(named), cf)

    def test_rounding_keeps_eight_places(self):
        self.assertEqual(S.named_cf({0.25: 1 / 3, 0.0: 0, 0.5: 0, 0.75: 0})["base"], round(1 / 3, 8))


class Legs(Base):
    EX = D("2026-10-30")

    def test_json_round_trip_and_labels(self):
        legs = [(-1, 100.0, self.EX, -1), (-1, 98.0, self.EX, +1), (+1, 350.5, self.EX, -1)]
        quotes = [(1.00, 1.10), (0.40, 0.50), (2.0, 2.2)]
        items = S.legs_to_json(legs, quotes)
        self.assertEqual([i["label"] for i in items], ["SELL 1 100P 2026-10-30", "BUY 1 98P 2026-10-30", "SELL 1 350.5C 2026-10-30"])
        self.assertEqual(items[0]["mid"], 1.05)
        self.assertEqual((items[0]["iv"], items[0]["delta"], items[0]["delta_src"]), (None, None, "none"))
        self.assertEqual(S.legs_from_json(json.loads(json.dumps(items))), legs)

    def test_delta_and_iv_come_from_the_chain_row(self):
        c = make_chain("TST", D("2026-10-13"), 105.0,
                       [(self.EX, -1, 100.0, 1.0, 1.1, -0.25, 0.31), (self.EX, -1, 98.0, 0.4, 0.5, None, None)])
        items = S.legs_to_json([(-1, 100.0, self.EX, -1), (-1, 98.0, self.EX, +1)], [(1.0, 1.1), (0.4, 0.5)], c)
        self.assertEqual((items[0]["delta"], items[0]["iv"], items[0]["delta_src"]), (-0.25, 0.31, "feed"))
        self.assertEqual((items[1]["delta"], items[1]["delta_src"]), (None, "none"))


class Ledgers(Base):
    def test_entries_are_registered_once_and_found(self):
        b = S.new_strategy_block("C9")
        self.assertFalse(S.entry_exists(b, "C9-2026-10-13"))
        S.register_entry(b, "C9-2026-10-13")
        S.register_entry(b, "C9-2026-10-13")
        self.assertEqual(b["entries_done"], ["C9-2026-10-13"])
        self.assertTrue(S.entry_exists(b, "C9-2026-10-13"))
        b["open_positions"].append({"id": "C9-2026-10-14"})
        self.assertTrue(S.entry_exists(b, "C9-2026-10-14"), "an open position counts even if it was never registered")
        self.assertEqual(S.find_position(b, "C9-2026-10-14"), {"id": "C9-2026-10-14"})
        self.assertIsNone(S.find_position(b, "nope"))

    def test_entries_done_keeps_the_newest(self):
        b = S.new_strategy_block("C9")
        for i in range(S.KEEP_ENTRIES + 25):
            S.register_entry(b, f"id{i:04d}")
        self.assertEqual(len(b["entries_done"]), S.KEEP_ENTRIES)
        self.assertEqual(b["entries_done"][-1], f"id{S.KEEP_ENTRIES + 24:04d}")
        self.assertEqual(b["entries_done"][0], "id0025")

    def test_exit_alert_once_per_position_and_reason(self):
        b = S.new_strategy_block("C6")
        self.assertTrue(S.mark_exit_alert(b, "P1", "profit", "2026-10-13T16:20:00-04:00"))
        self.assertFalse(S.mark_exit_alert(b, "P1", "profit", "2026-10-14T16:20:00-04:00"))
        self.assertTrue(S.mark_exit_alert(b, "P1", "dte", "2026-10-14T16:20:00-04:00"))
        self.assertTrue(S.mark_exit_alert(b, "P2", "profit", "2026-10-14T16:20:00-04:00"))
        self.assertEqual(b["exit_alerts"]["P1:profit"], "2026-10-13T16:20:00-04:00", "the first time stays on record")

    def test_exit_alert_ledger_is_capped(self):
        b = S.new_strategy_block("C6")
        for i in range(801):
            S.mark_exit_alert(b, f"P{i:04d}", "dte", f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}")
        self.assertEqual(len(b["exit_alerts"]), 601, "past 800 entries the oldest 200 go")
        self.assertNotIn("P0000:dte", b["exit_alerts"])
        self.assertIn("P0800:dte", b["exit_alerts"])


class RunningStats(Base):
    def test_cumulative_peak_and_drawdown(self):
        b = S.new_strategy_block("C9")
        # (base pnl, cumulative, peak, drawdown) after each trade; natural is 1 lower and mid 2 higher on every trade
        seq = [(10, 10, 10, 0), (20, 30, 30, 0), (-15, 15, 30, 15), (-10, 5, 30, 25), (5, 10, 30, 20)]
        for pnl, cum, peak, dd in seq:
            S.update_stats(b, pnl, pnl - 1, pnl + 2)
            self.assertEqual((b["cum_pnl_base"], b["peak"], b["drawdown"]), (cum, peak, dd))
        self.assertEqual(b["closed_n"], 5)
        self.assertAlmostEqual(b["cum_pnl_nat"], 5.0)
        self.assertAlmostEqual(b["cum_pnl_mid"], 20.0)
        self.assertEqual(b["max_drawdown"], 25.0)

    def test_the_peak_starts_at_zero_so_a_losing_start_is_a_drawdown(self):
        b = S.new_strategy_block("C9")
        S.update_stats(b, -40.0, -45.0, -35.0)
        self.assertEqual((b["peak"], b["drawdown"], b["max_drawdown"]), (0.0, 40.0, 40.0))

    def test_primed_at_twenty_closed_trades(self):
        b = S.new_strategy_block("C9")
        for i in range(19):
            S.update_stats(b, 1.0, 1.0, 1.0)
        self.assertFalse(b["primed"])
        S.update_stats(b, 1.0, 1.0, 1.0)
        self.assertTrue(b["primed"])
        self.assertEqual(cfg.PRIMED_MIN_TRADES, 20)

    def test_trailing_window_keeps_the_last_twenty(self):
        b = S.new_strategy_block("C9")
        for i in range(25):
            S.update_stats(b, float(i), float(-i), 0.0)
        self.assertEqual(b["recent_pnl_base"], [float(i) for i in range(5, 25)])
        self.assertEqual(b["recent_pnl_nat"], [float(-i) for i in range(5, 25)])

    def test_sleeve_counters_for_c6(self):
        b = S.new_strategy_block("C6")
        S.update_stats(b, 8.0, 1.0, 9.0, sleeve="GLD")
        S.update_stats(b, -3.0, -4.0, -2.0, sleeve="GLD")
        S.update_stats(b, 5.0, 4.0, 6.0, sleeve="TLT")
        self.assertEqual(b["sleeves"]["GLD"], {"closed_n": 2, "cum_pnl_base": 5.0, "cum_pnl_nat": -3.0})
        self.assertEqual(b["sleeves"]["TLT"], {"closed_n": 1, "cum_pnl_base": 5.0, "cum_pnl_nat": 4.0})
        S.update_stats(b, 1.0, 1.0, 1.0)                          # no sleeve given: only the pooled counters move
        self.assertEqual(b["closed_n"], 4)
        self.assertEqual(b["sleeves"]["GLD"]["closed_n"], 2)

    def test_model_stats_are_separate_from_the_real_ones(self):
        b = S.new_strategy_block("C6")
        S.update_model_stats(b, 7.0, 0.5, "GLD")
        S.update_model_stats(b, -2.0, -3.0, "TLT")
        m = b["model"]
        self.assertEqual((m["closed_n"], m["cum_pnl_base"], m["cum_pnl_nat"]), (2, 5.0, -2.5))
        self.assertEqual(m["sleeves"]["GLD"], {"closed_n": 1, "cum_pnl_base": 7.0})
        self.assertEqual((b["closed_n"], b["cum_pnl_base"], b["drawdown"]), (0, 0.0, 0.0), "the model path never feeds the kill-switch")

    def test_quality_window(self):
        b = S.new_strategy_block("C9")
        for i in range(25):
            S.add_quality(b, f"2026-10-{i + 1:02d}", i % 2 == 0, 0.1 * i, "ticket")
        self.assertEqual(len(b["quality"]), cfg.QUALITY_WINDOW)
        self.assertEqual(b["quality"][0]["date"], "2026-10-06")
        self.assertEqual(b["quality"][-1], {"date": "2026-10-25", "flag": True, "med_rel_spread": 2.4000000000000004, "why": "ticket"})


class SkipsAndRuns(Base):
    def test_week_key_is_the_iso_week(self):
        self.assertEqual(S.week_key(D("2026-10-13")), "2026-W42")
        self.assertEqual(S.week_key(D("2026-10-12")), "2026-W42")      # Monday
        self.assertEqual(S.week_key(D("2026-10-18")), "2026-W42")      # Sunday
        self.assertEqual(S.week_key(D("2026-12-31")), "2026-W53")      # 2026 starts on a Thursday: 53 weeks
        self.assertEqual(S.week_key(D("2027-01-01")), "2026-W53")
        self.assertEqual(S.week_key(D("2027-01-04")), "2027-W01")
        self.assertEqual(S.week_key(D("2024-12-30")), "2025-W01")

    def test_skip_counts_weekly_and_total(self):
        st = S.new_state()
        S.record_skip(st, "C9", D("2026-10-13"), "gate_closed")
        S.record_skip(st, "C9", D("2026-10-14"), "gate_closed")
        S.record_skip(st, "C6", D("2026-10-12"), "stale", sleeve="GLD")
        S.record_skip(st, "C9", D("2026-10-20"), "no_chain")
        self.assertEqual(st["skips"]["2026-W42"], {"C9": {"gate_closed": 2}, "C6": {"GLD:stale": 1}})
        self.assertEqual(st["skips"]["2026-W43"], {"C9": {"no_chain": 1}})
        self.assertEqual(st["total_skips"]["C9"], {"gate_closed": 2, "no_chain": 1})
        self.assertEqual(st["total_skips"]["C6"], {"GLD:stale": 1})

    def test_a_session_is_counted_once_however_often_it_is_re_run(self):
        st = S.new_state()
        self.assertTrue(S.record_skip(st, "C9", D("2026-10-13"), "gate_closed"))
        self.assertFalse(S.record_skip(st, "C9", D("2026-10-13"), "gate_closed"))
        self.assertFalse(S.record_skip(st, "C9", D("2026-10-13"), "gate_closed"))
        self.assertTrue(S.record_skip(st, "C9", D("2026-10-13"), "stale"), "a different reason is a different count")
        self.assertTrue(S.record_skip(st, "C6", D("2026-10-13"), "stale", sleeve="GLD"))
        self.assertTrue(S.record_skip(st, "C6", D("2026-10-13"), "stale", sleeve="TLT"), "sleeves count separately")
        self.assertFalse(S.record_skip(st, "C6", D("2026-10-13"), "stale", sleeve="GLD"))
        self.assertTrue(S.record_skip(st, "C9", D("2026-10-14"), "gate_closed"))
        self.assertEqual(st["total_skips"]["C9"], {"gate_closed": 2, "stale": 1})
        self.assertEqual(st["total_skips"]["C6"], {"GLD:stale": 1, "TLT:stale": 1})

    def test_the_skip_keys_are_trimmed_with_the_weekly_ledger(self):
        st = S.new_state()
        d = D("2026-01-05")
        for i in range(S.KEEP_WEEKS + 12):
            S.record_skip(st, "C9", d + dt.timedelta(days=7 * i), "gate_closed")
        newest = d + dt.timedelta(days=7 * (S.KEEP_WEEKS + 11))
        self.assertTrue(all(k[:10] >= (newest - dt.timedelta(days=7 * S.KEEP_WEEKS + 14)).isoformat() for k in st["skip_keys"]))
        self.assertLessEqual(len(st["skip_keys"]), S.KEEP_WEEKS + 3)

    def test_an_older_state_file_gains_the_skip_keys_on_load(self):
        path = os.path.join(self.tmp(), "s.json")
        st = S.new_state()
        del st["skip_keys"]
        S.save(st, path)
        self.assertEqual(S.load(path)["skip_keys"], {})

    def test_old_weeks_are_dropped(self):
        st = S.new_state()
        d = D("2026-01-05")
        for i in range(S.KEEP_WEEKS + 4):
            S.record_skip(st, "C9", d + dt.timedelta(days=7 * i), "gate_closed")
        self.assertEqual(len(st["skips"]), S.KEEP_WEEKS)
        self.assertNotIn("2026-W02", st["skips"])
        self.assertEqual(sum(st["total_skips"]["C9"].values()), S.KEEP_WEEKS + 4, "totals are never trimmed")

    def test_run_counter(self):
        st = S.new_state()
        for d in ("2026-10-12", "2026-10-13", "2026-10-13", "2026-10-19"):
            S.bump_run(st, D(d))
        self.assertEqual(st["runs"], {"2026-W42": 3, "2026-W43": 1})


class Outbox(Base):
    def test_enqueue_is_idempotent(self):
        st = S.new_state()
        self.assertTrue(S.enqueue(st, "entry:C9-2026-10-13", "entry", "C9", None, "SHADOW - NOT A TRADE | x", "2026-10-13T16:20:00-04:00"))
        self.assertFalse(S.enqueue(st, "entry:C9-2026-10-13", "entry", "C9", None, "again", "2026-10-13T16:21:00-04:00"))
        self.assertEqual(len(st["outbox"]), 1)
        self.assertEqual(st["outbox"][0]["plain"], "SHADOW - NOT A TRADE | x")

    def test_a_sent_message_is_never_queued_again(self):
        st = S.new_state()
        S.enqueue(st, "k", "entry", "C9", None, "p", "2026-10-13T16:20:00-04:00")
        S.mark_sent(st, "k", "2026-10-13T16:21:00-04:00")
        self.assertEqual(st["outbox"], [])
        self.assertEqual(st["sent"], {"k": "2026-10-13T16:21:00-04:00"})
        self.assertFalse(S.enqueue(st, "k", "entry", "C9", None, "p", "2026-10-14T16:20:00-04:00"))
        self.assertEqual(st["outbox"], [])

    def test_mark_sent_only_removes_its_own_key(self):
        st = S.new_state()
        for k in ("a", "b", "c"):
            S.enqueue(st, k, "notice", None, None, k, "2026-10-13T16:20:00-04:00")
        S.mark_sent(st, "b", "2026-10-13T16:21:00-04:00")
        self.assertEqual([m["key"] for m in st["outbox"]], ["a", "c"])

    def test_sent_ledger_pruning(self):
        st = S.new_state()
        today = D("2026-10-13")
        cutoff_day = today - dt.timedelta(days=S.KEEP_SENT_DAYS)
        st["sent"] = {"old": (cutoff_day - dt.timedelta(days=1)).isoformat() + "T16:20:00-04:00",
                      "edge": cutoff_day.isoformat() + "T16:20:00-04:00",
                      "new": today.isoformat() + "T16:20:00-04:00"}
        S.prune_sent(st, today)
        self.assertEqual(set(st["sent"]), {"edge", "new"})

    def test_undeliverable_messages_are_dropped_after_three_days(self):
        st = S.new_state()
        today = D("2026-10-13")
        for k, day in (("fresh", "2026-10-13"), ("two", "2026-10-11"), ("edge", "2026-10-10"), ("stale", "2026-10-09")):
            S.enqueue(st, k, "notice", None, None, k, day + "T16:20:00-04:00")
        dropped = S.drop_stale_outbox(st, today)
        self.assertEqual([m["key"] for m in dropped], ["stale"])
        self.assertEqual([m["key"] for m in st["outbox"]], ["fresh", "two", "edge"])


class TradeLog(Base):
    def test_header(self):
        path = os.path.join(self.tmp(), "log.csv")
        S.init_log(path)
        S.init_log(path)
        with open(path, newline="") as f:
            rows = list(csv.reader(f))
        self.assertEqual(rows, [S.LOG_COLUMNS])
        self.assertEqual(len(S.LOG_COLUMNS), 35)
        self.assertEqual(len(set(S.LOG_COLUMNS)), 35)
        for col in ("id", "strategy", "version", "pnl_mid", "pnl_base", "pnl_nat", "pnl_x3", "pnl_f65", "exit_reason", "stale"):
            self.assertIn(col, S.LOG_COLUMNS)

    def test_append_read_round_trip(self):
        path = os.path.join(self.tmp(), "log.csv")
        row = {"id": "C9-2026-10-13", "strategy": "C9", "sleeve": "SPY", "version": "real", "entry_date": "2026-10-13",
               "exit_date": "2026-10-14", "legs": "SELL 1 604C 2026-10-14 ; BUY 1 610C 2026-10-14, odd \"quote\"", "pnl_base": 172.6,
               "pnl_mid": None, "not_a_column": "ignored", "units": 4}
        S.append_log([row], path)
        S.append_log([dict(row, id="C9-2026-10-14")], path)
        got = S.read_log(path)
        self.assertEqual(len(got), 2)
        self.assertEqual(got[0]["legs"], row["legs"])
        self.assertEqual(got[0]["pnl_base"], "172.6")
        self.assertEqual(got[0]["pnl_mid"], "", "None is written as an empty cell")
        self.assertEqual(got[0]["notes"], "", "a column the row does not mention is empty")
        self.assertNotIn("not_a_column", got[0])
        self.assertEqual(S.log_ids(path), {"C9-2026-10-13", "C9-2026-10-14"})
        with open(path) as f:
            self.assertEqual(sum(1 for line in f if line.startswith("id,")), 1, "one header only")

    def test_missing_log(self):
        path = os.path.join(self.tmp(), "none.csv")
        self.assertEqual(S.read_log(path), [])
        self.assertEqual(S.log_ids(path), set())

    def test_appending_nothing_still_creates_the_header(self):
        path = os.path.join(self.tmp(), "log.csv")
        S.append_log([], path)
        with open(path) as f:
            self.assertEqual(f.read().strip(), ",".join(S.LOG_COLUMNS))


if __name__ == "__main__":
    unittest.main()
