"""End-to-end: whole days (and weeks) through shadow_job.main() on a simulated market (tests/world.py).

Every expectation about WHAT the bot should do is computed here from the simulated chains with small, independent restatements of
the rules (the liquidity filter, the delta pick, the fills, the intrinsic payoff, the realised-vol gate via the statistics module),
so a wiring mistake in the job (wrong chain, wrong date, wrong symbol, a transition applied twice) shows up as a number that does
not match, not just as an exception. Nothing touches the network or Discord.
"""
import csv
import datetime as dt
import json
import os
import re
import unittest
from unittest import mock

from tests.helpers import Base, D, FakeSender, WEBHOOK, et
from tests.world import World

import config as cfg
import nyse_calendar as nyse
import state as state_mod


# ---------------------------------------------------------------------------------------------------- independent oracles
def liquid(bid, ask, short):
    """The study's liquidity filter, restated."""
    mid = 0.5 * (bid + ask)
    return ask > 0 and bid >= 0 and ask >= bid and (ask - bid) <= max(0.05, 0.30 * mid) + 1e-9 and (not short or bid > 0)


def pick_short(rows, ex, cp, target):
    cands = [r for r in rows if r[0] == ex and r[1] == cp and r[5] is not None and liquid(r[3], r[4], True)]
    best = min(cands, key=lambda r: (abs(abs(r[5]) - target), r[2]))
    return best


def nearest(rows, ex, cp, target):
    ks = sorted({r[2] for r in rows if r[0] == ex and r[1] == cp})
    return min(ks, key=lambda k: (abs(k - target), k))


def quote(rows, ex, cp, k):
    r = next(r for r in rows if r[0] == ex and r[1] == cp and r[2] == k)
    return r[3], r[4]


def fill(bid, ask, side, kfrac):
    return max(0.5 * (bid + ask) + side * kfrac * (ask - bid), 0.0)


def cash(legs_q, kfrac):
    """legs_q: [(qty, bid, ask)] -> dollars received (+) / paid (-) opening them at fraction kfrac of the spread from mid."""
    return sum((-1 if q > 0 else 1) * fill(b, a, 1 if q > 0 else -1, kfrac) * 100 * abs(q) for q, b, a in legs_q)


def payoff(legs, S):
    """legs: [(cp, K, qty)] -> dollars at expiry."""
    return sum(q * (max(S - K, 0.0) if cp > 0 else max(K - S, 0.0)) for cp, K, q in legs) * 100


def expected_condor(w, d):
    rows = w.rows("SPY", d)
    spot = w.px["SPY"][d]
    ex = nyse.next_td(d)
    kc, kp = pick_short(rows, ex, +1, 0.16)[2], pick_short(rows, ex, -1, 0.16)[2]
    kcl, kpl = nearest(rows, ex, +1, kc + 0.01 * spot), nearest(rows, ex, -1, kp - 0.01 * spot)
    legs = [(+1, kc, -1), (+1, kcl, +1), (-1, kp, -1), (-1, kpl, +1)]
    lq = [(q, *quote(rows, ex, cp, k)) for cp, k, q in legs]
    out = {"expiry": ex, "kc": kc, "kcl": kcl, "kp": kp, "kpl": kpl, "legs": legs, "spot": spot}
    for name, kf in (("mid", 0.0), ("base", 0.25), ("nat", 0.5)):
        out[name] = cash(lq, kf)
    return out


def condor_pnl(c, S, kname="base"):
    fees = 0.05 * 4 if kname in ("base", "nat") else 0.0
    return c[kname] + payoff(c["legs"], S) - fees


def first_open_pair(w, after=None):
    """First session d (optionally after a date) where the C9 gate is open on d and on the next session, by the independent oracle."""
    for d in w.days:
        if d < w.first or (after and d <= after):
            continue
        n = nyse.next_td(d)
        if n in w.px["SPY"] and w.spy_gate(d) and w.spy_gate(n) and d.weekday() < 3 and not nyse.is_early_close(n):
            return d
    raise AssertionError("scenario has no pair of consecutive open-gate sessions")


def read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def data_files(w):
    return {n: read_bytes(w.path("data", n)) for n in sorted(os.listdir(w.path("data")))}


class Flow(Base):
    def world(self, **kw) -> World:
        kw.setdefault("first_session", "2026-10-26")
        kw.setdefault("n_sessions", 45)
        return World(self.tmp(), **kw)

    def world_where(self, pred, **kw) -> World:
        """The first deterministic world (seeds 1, 2, ...) in which pred(world) holds: for scenarios that need a particular
        night to have the C9 gate open."""
        for seed in range(1, 80):
            w = self.world(seed=seed, **kw)
            if pred(w):
                return w
        raise AssertionError("no seed gives the scenario")

    def assertNoTempFiles(self, w):
        for dirpath, _dirs, files in os.walk(w.root):
            for f in files:
                self.assertFalse(f.endswith(".tmp"), os.path.join(dirpath, f))


# ---------------------------------------------------------------------------------------------------- the first run
class FirstRun(Flow):
    def test_empty_repository_is_bootstrapped_from_the_feeds(self):
        w = self.world(first_session="2026-11-02", n_sessions=5, seeded=False)
        self.assertFalse(os.path.exists(w.path("data")))
        r = w.run("2026-11-02 16:20")
        self.assertEqual(r.code, 0)
        m = w.market()
        for s in ("SPY", "GLD", "TLT"):
            self.assertGreaterEqual(len(m.closes[s]), 290, s)                           # CLOSES_KEEP_DAYS = 430 calendar days
            self.assertGreaterEqual(m.closes[s].dates[0], D("2026-11-02") - dt.timedelta(days=cfg.CLOSES_KEEP_DAYS), s)
            self.assertEqual(m.close(s, D("2026-11-02")), w.px[s][D("2026-11-02")], s)
            self.assertEqual(m.close(s, D("2026-10-30")), w.px[s][D("2026-10-30")], s)
        self.assertGreaterEqual(len(m.index_series("VIX")), 250)
        self.assertEqual(m.index_series("VIX").last_date(), D("2026-10-30"))          # 11-02 is published after 19:00
        self.assertEqual(m.prov[("VIX", D("2026-11-02"))], round(w.idx["VIX"][D("2026-11-02")] + 0.10, 2))
        self.assertAlmostEqual(m.rate(D("2026-11-02")), 0.036)
        for name in (cfg.STATE_FILE, cfg.LOG_FILE, cfg.DIAG_FILE, cfg.EXPORT_FILE, "data/closes.csv", "data/indices.csv", "data/tbill.csv"):
            self.assertTrue(os.path.exists(w.path(name)), name)
        self.assertNoTempFiles(w)

    def test_hello_is_the_first_message_and_is_sent_once(self):
        w = self.world(first_session="2026-11-02", n_sessions=5)
        r1 = w.run("2026-11-02 16:20")
        self.assertIn("shadow bots online", r1.texts[0])
        self.assertTrue(all(t.startswith(cfg.BANNER) for t in r1.texts))
        r2 = w.run("2026-11-03 16:20")
        self.assertFalse(any("shadow bots online" in t for t in r2.texts))
        self.assertEqual(sum("shadow bots online" in t for t in w.sender.texts), 1)

    def test_a_monday_that_is_also_the_first_of_the_month_opens_all_three(self):
        w = self.world(first_session="2026-11-02", n_sessions=5)
        r = w.run("2026-11-02 16:20")
        st = r.state
        d = D("2026-11-02")
        c6 = st["strategies"]["C6"]
        self.assertEqual({p["id"] for p in c6["open_positions"]}, {"C6-GLD-2026-11-02", "C6-TLT-2026-11-02"})
        self.assertEqual({m["id"] for m in c6["model"]["open"]}, {"C6M-GLD-2026-11-02", "C6M-TLT-2026-11-02"})
        self.assertEqual({p["pair_id"] for p in c6["pairs"]}, {"C6-GLD-2026-11-02", "C6-TLT-2026-11-02"})
        cand = st["strategies"]["C3"]["candidate"]
        self.assertEqual((cand["entry_date"], cand["month"], cand["wait_runs"]), ("2026-11-02", "2026-11", 0))
        self.assertIsNotNone(cand["position"])
        self.assertEqual(st["strategies"]["C3"]["open_positions"], [])                  # not a position until the gate is confirmed
        self.assertEqual(w.spy_gate(d), bool(st["strategies"]["C9"]["open_positions"]))  # C9 follows the gate, nothing else
        self.assertEqual(st["last_run"]["session"], "2026-11-02")
        self.assertEqual(st["last_run"]["phase"], "close")
        self.assertEqual(set(st["last_session"]), {"C9", "C6", "C3"})

    def test_every_message_is_labelled_and_none_can_ping(self):
        w = self.world(first_session="2026-11-02", n_sessions=5)
        w.run("2026-11-02 16:20")
        self.assertTrue(w.sender.posts)
        for url, payload in w.sender.posts:
            self.assertEqual(url, WEBHOOK)
            self.assertEqual(payload["allowed_mentions"], {"parse": []})
            self.assertTrue(payload["content"].startswith("SHADOW - NOT A TRADE"), payload["content"])
            self.assertEqual(payload["embeds"][0]["footer"]["text"], "SHADOW - NOT A TRADE")


# ---------------------------------------------------------------------------------------------------- C9: one night, one settlement
class C9Cycle(Flow):
    def setUp(self):
        self.w = self.world(n_sessions=40)
        self.d0 = first_open_pair(self.w)
        self.w.seed_root(nyse.prev_td(self.d0))
        self.c = expected_condor(self.w, self.d0)
        self.d1 = self.c["expiry"]

    def entry(self):
        return self.w.run(f"{self.d0} 16:20", strategy="c9")

    def test_the_entry_is_the_condor_the_rules_pick(self):
        r = self.entry()
        pos = r.state["strategies"]["C9"]["open_positions"]
        self.assertEqual(len(pos), 1)
        p, c = pos[0], self.c
        self.assertEqual(p["id"], f"C9-{self.d0}")
        self.assertEqual((p["entry_date"], p["expiry"], p["dte_in"]), (str(self.d0), str(self.d1), (self.d1 - self.d0).days))
        self.assertEqual(p["strikes"], {"k_call": c["kc"], "k_put": c["kp"], "k_call_wing": c["kcl"], "k_put_wing": c["kpl"]})
        self.assertEqual([(l["cp"], l["strike"], l["qty"]) for l in p["legs"]], c["legs"])
        self.assertEqual(p["spot_in"], c["spot"])
        for name in ("mid", "base", "nat"):
            self.assertAlmostEqual(p["entry_cf"][name], c[name], places=6, msg=name)
        self.assertAlmostEqual(p["max_loss"], (c["kcl"] - c["kc"]) * 100 - c["base"] + 0.05 * 4, places=6)
        self.assertEqual(r.state["strategies"]["C9"]["entries_done"], [p["id"]])
        self.assertEqual(r.n_posts, 2)                                                  # hello + the entry
        self.assertIn("C9 CONDOR_1D", r.texts[1])

    def test_a_win_settles_at_the_official_close_with_the_right_pnl(self):
        self.entry()
        S = self.w.px["SPY"][self.d1]
        self.assertTrue(self.c["kp"] < S < self.c["kc"], "scenario: the close must land between the short strikes")
        r = self.w.run(f"{self.d1} 16:20", strategy="c9")
        rows = self.w.log()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row["id"], row["strategy"], row["version"], row["exit_reason"]), (f"C9-{self.d0}", "C9", "real", "expiry"))
        self.assertEqual((row["entry_date"], row["exit_date"], row["expiry"], row["dte_out"]), (str(self.d0), str(self.d1), str(self.d1), "0"))
        self.assertAlmostEqual(float(row["pnl_base"]), condor_pnl(self.c, S, "base"), places=3)
        self.assertAlmostEqual(float(row["pnl_nat"]), condor_pnl(self.c, S, "nat"), places=3)
        self.assertAlmostEqual(float(row["pnl_mid"]), condor_pnl(self.c, S, "mid"), places=3)      # the mid P&L carries no fees
        self.assertAlmostEqual(float(row["pnl_base"]), self.c["base"] - 0.20, places=3)             # inside both shorts: the credit is kept
        self.assertAlmostEqual(float(row["entry_fees"]), 0.20)
        self.assertEqual(float(row["exit_fees"]), 0.0)
        self.assertIn(f"SPY close {S:.2f}", row["notes"])
        blk = r.state["strategies"]["C9"]
        self.assertEqual((blk["closed_n"], blk["open_positions"][0]["id"] if blk["open_positions"] else None),
                         (1, f"C9-{self.d1}" if self.w.spy_gate(self.d1) else None))
        self.assertAlmostEqual(blk["cum_pnl_base"], float(row["pnl_base"]), places=3)
        self.assertEqual(list(blk["exit_alerts"]), [f"C9-{self.d0}:expiry"])
        self.assertTrue(blk["exit_alerts"][f"C9-{self.d0}:expiry"].startswith(f"{self.d1}T16:20:00"))
        settle_msg = [t for t in r.texts if "settled" in t]
        self.assertEqual(len(settle_msg), 1)

    def test_a_loss_beyond_the_call_wing_is_the_maximum_loss(self):
        self.entry()
        S = self.c["kcl"] + 3.0
        self.w.set_close("SPY", self.d1, S)
        self.w.run(f"{self.d1} 16:20", strategy="c9")
        row = self.w.log()[0]
        width = (self.c["kcl"] - self.c["kc"]) * 100
        self.assertAlmostEqual(float(row["pnl_base"]), self.c["base"] - width - 0.20, places=3)
        self.assertAlmostEqual(float(row["pnl_base"]), condor_pnl(self.c, S), places=3)
        self.assertAlmostEqual(-float(row["pnl_base"]), float(row["max_loss"]), places=3)           # the loss equals the stated maximum
        self.assertLess(self.w.state()["strategies"]["C9"]["cum_pnl_base"], 0)
        self.assertGreater(self.w.state()["strategies"]["C9"]["drawdown"], 0)

    def test_a_breach_between_a_short_strike_and_its_wing_is_partial(self):
        self.entry()
        S = self.c["kp"] - 2.0
        self.assertGreater(S, self.c["kpl"])
        self.w.set_close("SPY", self.d1, S)
        self.w.run(f"{self.d1} 16:20", strategy="c9")
        row = self.w.log()[0]
        self.assertAlmostEqual(float(row["pnl_base"]), self.c["base"] - 200.0 - 0.20, places=3)

    def test_running_the_same_session_again_changes_nothing(self):
        r1 = self.entry()
        before = json.dumps({k: v for k, v in r1.state.items() if k != "last_run"}, sort_keys=True)
        r2 = self.w.run(f"{self.d0} 16:26", strategy="c9", trigger="manual")
        self.assertEqual(r2.n_posts, 0)
        self.assertEqual(json.dumps({k: v for k, v in r2.state.items() if k != "last_run"}, sort_keys=True), before)
        self.assertEqual(r2.state["runs"], r1.state["runs"])
        self.assertEqual(r2.diag["c9"]["result"], "already_entered")

    def test_running_the_settlement_session_twice_logs_and_posts_once(self):
        self.entry()
        self.w.run(f"{self.d1} 16:20", strategy="c9")
        r = self.w.run(f"{self.d1} 16:27", strategy="c9", trigger="manual")
        self.assertEqual(len(self.w.log()), 1)
        self.assertEqual(r.n_posts, 0)
        self.assertEqual(r.state["strategies"]["C9"]["closed_n"], 1)
        self.assertEqual(sum("settled" in t for t in self.w.sender.texts), 1)

    def test_a_crash_between_the_log_and_the_state_is_repaired_by_the_next_run(self):
        self.entry()
        real_save, calls = state_mod.save, []

        def flaky(st, path):
            if not calls:
                calls.append(1)
                raise OSError("disk full")
            return real_save(st, path)

        with mock.patch.object(state_mod, "save", flaky), mock.patch("discord_shadow.send", FakeSender()):
            with self.assertRaises(OSError):
                self.w.run(f"{self.d1} 16:20", strategy="c9")
        self.assertEqual(len(self.w.log()), 1)                                          # the log row was written first
        self.assertEqual(len(self.w.state()["strategies"]["C9"]["open_positions"]), 1)  # the state was not: still open
        r = self.w.run(f"{self.d1} 16:31", strategy="c9", trigger="manual")
        self.assertEqual(len(self.w.log()), 1)                                          # same id: not written twice
        blk = r.state["strategies"]["C9"]
        self.assertEqual(blk["closed_n"], 1)                                            # the stats were folded in exactly once
        self.assertAlmostEqual(blk["cum_pnl_base"], float(self.w.log()[0]["pnl_base"]), places=3)
        self.assertEqual(sum("settled" in t for t in self.w.sender.texts), 1)

    def test_a_crash_while_posting_loses_nothing_and_repeats_nothing(self):
        class Boom(FakeSender):
            armed = True

            def __call__(self, url, payload):
                if len(self.posts) == 1 and self.armed:
                    self.armed = False                                                  # explodes once, on the second post
                    raise RuntimeError("socket exploded")
                return super().__call__(url, payload)

        boom = Boom()
        with mock.patch("discord_shadow.send", FakeSender()):
            with self.assertRaises(RuntimeError):
                self.w.run(f"{self.d0} 16:20", strategy="c9", sender=boom)
        st = self.w.state()
        self.assertEqual(len(boom.posts), 1)                                            # the hello went out
        self.assertEqual(len(st["strategies"]["C9"]["open_positions"]), 1)             # the position was saved before posting
        self.assertEqual([m["kind"] for m in st["outbox"]], ["entry"])                  # the unsent entry waits in the outbox
        r = self.w.run(f"{self.d0} 16:31", strategy="c9", trigger="manual", sender=boom)
        self.assertEqual(r.n_posts, 1)
        self.assertIn("C9 CONDOR_1D", r.texts[0])
        self.assertEqual(r.state["outbox"], [])
        self.assertEqual(len(r.state["strategies"]["C9"]["open_positions"]), 1)
        self.assertEqual(len([t for t in boom.texts if "shadow bots online" in t]), 1)

    def test_a_run_flagged_too_late_settles_but_opens_nothing(self):
        self.entry()
        r = self.w.run(f"{self.d1} 16:41", strategy="c9", guard="too_late", late_min=21)
        blk = r.state["strategies"]["C9"]
        self.assertEqual(blk["closed_n"], 1)                                            # settlement still ran
        self.assertEqual(blk["open_positions"], [])                                     # but no new position
        self.assertEqual(r.state["total_skips"]["C9"].get("run_late"), 1)
        self.assertEqual(r.diag["c9"]["result"], "blocked:run_late")
        notice = [t for t in r.texts if "data problem" in t]
        self.assertEqual(len(notice), 1)
        self.assertEqual(r.state["last_run"]["guard"], "too_late")
        emb = [p["embeds"][0]["description"] for _u, p in self.w.sender.posts if "data problem" in p["content"]][0]
        self.assertIn("started 21 min after its 16:20 ET target", emb)
        self.assertIn("settlements and exits still ran", emb)

    def test_a_later_manual_run_on_the_same_session_can_still_enter(self):
        self.entry()
        r = self.w.run(f"{self.d1} 16:41", strategy="c9", guard="too_late", late_min=21)
        self.assertEqual(r.state["strategies"]["C9"]["open_positions"], [])
        if not self.w.spy_gate(self.d1):
            self.skipTest("gate closed on the second night in this scenario")
        r2 = self.w.run(f"{self.d1} 17:05", strategy="c9", trigger="manual")
        self.assertEqual([p["id"] for p in r2.state["strategies"]["C9"]["open_positions"]], [f"C9-{self.d1}"])


# ---------------------------------------------------------------------------------------------------- when the data is late, old or missing
class DataTrouble(Flow):
    def setUp(self):
        self.w = self.world(n_sessions=40)
        self.d0 = first_open_pair(self.w)
        self.w.seed_root(nyse.prev_td(self.d0))
        self.c = expected_condor(self.w, self.d0)
        self.d1 = self.c["expiry"]

    def spy_fetches(self):
        return [t for t, n in self.w.calls if n == "options_SPY.json"]

    def test_it_waits_for_the_closing_print_and_then_enters(self):
        self.w.ready_at["SPY"] = "16:23"
        r = self.w.run(f"{self.d0} 16:20", strategy="c9")
        self.assertEqual(r.diag["snapshot_wait"], {"polls": 4, "waited_s": 180.0, "still_stale": []})
        times = self.spy_fetches()
        self.assertEqual([(b - a).total_seconds() for a, b in zip(times, times[1:])], [45.0] * 4)
        self.assertEqual(len(r.state["strategies"]["C9"]["open_positions"]), 1)
        entry = [t for t in r.texts if "C9 CONDOR_1D" in t][0]
        self.assertIn("16:23 ET", entry)                                                # stamped with the time the data was read
        self.assertIn("stale: no", entry)
        self.assertEqual(r.diag["feed"]["SPY"]["stale"], None)

    def test_it_stops_waiting_at_nine_minutes_and_skips_the_night_as_stale(self):
        self.w.ready_at["SPY"] = "17:00"
        r = self.w.run(f"{self.d0} 16:20", strategy="c9")
        self.assertEqual(r.diag["snapshot_wait"], {"polls": 12, "waited_s": 540.0, "still_stale": ["SPY"]})
        self.assertEqual(len(self.spy_fetches()), 13)
        self.assertEqual(r.state["strategies"]["C9"]["open_positions"], [])
        self.assertEqual(r.state["total_skips"]["C9"], {"stale": 1})
        self.assertEqual(r.diag["c9"]["result"], "skip:stale")
        self.assertIn("pre_close_snapshot_15:50", r.diag["feed"]["SPY"]["stale"])
        self.assertEqual(sum("data problem" in t for t in r.texts), 1)

    def test_it_never_waits_past_sixteen_thirty(self):
        self.w.ready_at["SPY"] = "17:00"
        r = self.w.run(f"{self.d0} 16:25", strategy="c9", trigger="manual")
        self.assertEqual(r.diag["snapshot_wait"], {"polls": 6, "waited_s": 270.0, "still_stale": ["SPY"]})   # 16:25 -> 16:29:30

    def test_a_file_older_than_forty_five_minutes_is_not_a_closing_snapshot(self):
        self.w.file_lag_min = 60.0
        r = self.w.run(f"{self.d0} 16:20", strategy="c9")
        self.assertEqual(r.diag["snapshot_wait"]["still_stale"], ["SPY"])
        self.assertTrue(r.diag["feed"]["SPY"]["stale"].startswith("file_age_60"))
        self.assertEqual(r.state["strategies"]["C9"]["open_positions"], [])

    def test_a_down_chain_skips_the_night_and_the_condor_is_settled_the_next_day(self):
        self.w.run(f"{self.d0} 16:20", strategy="c9")
        d2 = nyse.next_td(self.d1)
        r = self.w.run(f"{self.d1} 16:20", strategy="c9", overrides={"options_SPY.json": None})
        blk = r.state["strategies"]["C9"]
        self.assertEqual(len(blk["open_positions"]), 1)                                  # the d0 condor waits: no close for d1 yet
        self.assertEqual(blk["closed_n"], 0)
        self.assertEqual(r.state["total_skips"]["C9"], {"feed_down": 1})
        notice = [p["embeds"][0]["description"] for _u, p in self.w.sender.posts if "data problem" in p["content"]]
        self.assertEqual(len(notice), 1)
        self.assertIn("SPY option chain unavailable", notice[0])
        self.assertIn("settlement waits", notice[0])
        r3 = self.w.run(f"{d2} 16:20", strategy="c9")
        row = self.w.log()[0]
        S = self.w.px["SPY"][self.d1]
        self.assertAlmostEqual(float(row["pnl_base"]), condor_pnl(self.c, S), places=3)  # still the expiry-date close
        self.assertEqual(row["exit_date"], str(self.d1))
        self.assertIn(f"settled late by run of {d2}", row["notes"])
        self.assertEqual(r3.state["strategies"]["C9"]["closed_n"], 1)

    def test_a_bad_chain_file_is_retried_then_reported(self):
        r = self.w.run(f"{self.d0} 16:20", strategy="c9", overrides={"options_SPY.json": '{"timestamp": "x", "data": {}}'})
        self.assertIn("SPY", r.diag["feed"])
        self.assertFalse(r.diag["feed"]["SPY"]["ok"])
        self.assertEqual(r.state["total_skips"]["C9"], {"feed_down": 1})

    def test_no_close_from_any_source_is_reported_once_and_the_condor_waits(self):
        self.w.run(f"{self.d0} 16:20", strategy="c9")
        r = self.w.run(f"{self.d1} 16:20", strategy="c9",
                       overrides={"options_SPY.json": None, "nasdaq_SPY.json": None})
        self.assertIn("error", r.diag["closes"]["SPY"])
        notice = [p["embeds"][0]["description"] for _u, p in self.w.sender.posts if "data problem" in p["content"]]
        self.assertIn(f"SPY close for {self.d1} unavailable from every source", notice[0])
        self.assertEqual(len(r.state["strategies"]["C9"]["open_positions"]), 1)

    def test_a_wildly_different_second_source_cannot_overwrite_a_stored_close(self):
        self.w.run(f"{self.d0} 16:20", strategy="c9")
        stored = self.w.px["SPY"][self.d0]
        bad = lambda now: self.w.nasdaq_text("SPY", now).replace(f"{stored:,.2f}", "1.00")      # noqa: E731
        r = self.w.run(f"{self.d1} 16:20", strategy="c9", overrides={"nasdaq_SPY.json": bad})
        self.assertEqual(self.w.market().close("SPY", self.d0), stored)                 # the Cboe close of d0 stands
        self.assertEqual(r.diag["closes"]["SPY"]["mismatch"], [[str(self.d0), stored, 1.0]])
        self.assertEqual(r.diag["closes"]["SPY"]["corrected"], [])
        notice = [p["embeds"][0]["description"] for _u, p in self.w.sender.posts if "data problem" in p["content"]]
        self.assertIn(f"SPY close for {self.d0}: stored {stored:.2f} KEPT, but nasdaq says 1.00", notice[-1])

    def test_a_few_cents_of_disagreement_is_corrected_silently_by_the_later_source(self):
        self.w.run(f"{self.d0} 16:20", strategy="c9")
        stored = self.w.px["SPY"][self.d0]
        nudged = lambda now: self.w.nasdaq_text("SPY", now).replace(f"{stored:,.2f}", f"{stored + 0.03:,.2f}")   # noqa: E731
        r = self.w.run(f"{self.d1} 16:20", strategy="c9", overrides={"nasdaq_SPY.json": nudged})
        self.assertAlmostEqual(self.w.market().close("SPY", self.d0), stored + 0.03)
        self.assertEqual(len(r.diag["closes"]["SPY"]["corrected"]), 1)
        self.assertFalse([p for _u, p in self.w.sender.posts if "data problem" in p["content"]])      # below the 0.3% warning level


# ---------------------------------------------------------------------------------------------------- the calendar
class CalendarDays(Flow):
    def test_a_weekend_run_does_nothing_at_all(self):
        w = self.world()
        r = w.run("2026-10-31 16:20")
        self.assertEqual(r.diag["phase"], "closed_day")
        self.assertEqual(r.diag["note"], "not a trading day: nothing to do")
        self.assertEqual((r.n_posts, w.calls), (0, []))
        self.assertFalse(os.path.exists(w.path(cfg.STATE_FILE)))

    def test_thanksgiving_is_a_closed_day(self):
        w = self.world()
        r = w.run("2026-11-26 16:20")
        self.assertEqual((r.diag["phase"], r.n_posts, w.calls), ("closed_day", 0, []))

    def test_a_morning_run_records_diagnostics_and_changes_no_state(self):
        w = self.world()
        d0 = D("2026-10-26")
        w.seed_root(nyse.prev_td(d0))
        before = data_files(w)
        r = w.run(f"{d0} 10:30")
        self.assertEqual(r.diag["phase"], "intraday")
        self.assertIn("diagnostics only", r.diag["note"])
        self.assertEqual(r.n_posts, 0)
        self.assertFalse(os.path.exists(w.path(cfg.STATE_FILE)))
        self.assertFalse(os.path.exists(w.path(cfg.LOG_FILE)))
        self.assertEqual(data_files(w), before)
        self.assertNotIn("snapshot_wait", r.diag)    # no waiting outside the close phase
        self.assertEqual(len([n for t, n in w.calls if n == "options_SPY.json"]), 1)

    def test_a_dry_run_prints_the_messages_and_writes_nothing(self):
        w = self.world()
        d0 = first_open_pair(w)
        w.seed_root(nyse.prev_td(d0))
        before = data_files(w)
        r = w.run(f"{d0} 16:20", strategy="c9", dry_run=True)
        self.assertIn("would post: entry:C9-", r.stdout)
        self.assertIn("SHADOW - NOT A TRADE", r.stdout)
        self.assertEqual(sorted(os.listdir(w.root)), ["data"])                           # not even a diagnostics line
        self.assertEqual(data_files(w), before)
        self.assertEqual(w.sender.posts, [])

    def test_the_friday_after_thanksgiving_settles_but_does_not_enter(self):
        w = self.world_where(lambda x: x.spy_gate(D("2026-11-25")), n_sessions=40)
        d0, d1 = D("2026-11-25"), D("2026-11-27")
        self.assertTrue(nyse.is_early_close(d1) and nyse.next_td(d0) == d1)
        w.seed_root(nyse.prev_td(d0))
        c = expected_condor(w, d0)
        w.run(f"{d0} 16:20", strategy="c9")
        self.assertEqual(len(w.state()["strategies"]["C9"]["open_positions"]), 1)
        r = w.run(f"{d1} 16:20", strategy="c9")
        self.assertTrue(r.state["last_run"]["phase"] == "close" and r.diag["early_close"])
        row = w.log()[0]
        self.assertEqual(row["exit_date"], str(d1))
        self.assertAlmostEqual(float(row["pnl_base"]), condor_pnl(c, w.px["SPY"][d1]), places=3)
        self.assertEqual(r.state["strategies"]["C9"]["open_positions"], [])
        self.assertEqual(r.state["total_skips"]["C9"], {"early_close": 1})
        self.assertEqual(r.diag["c9"]["result"], "blocked:early_close")

    def test_a_close_run_on_the_early_close_day_is_not_stale_at_thirteen_twenty(self):
        w = self.world()
        d1 = D("2026-11-27")
        w.seed_root(nyse.prev_td(d1))
        r = w.run(f"{d1} 13:20", strategy="c9")
        self.assertEqual(r.diag["phase"], "close")
        self.assertIsNone(r.diag["feed"]["SPY"]["stale"])
        self.assertIn("T13:00:00", r.diag["feed"]["SPY"]["und_last_trade_et"])


# ---------------------------------------------------------------------------------------------------- missed runs
class MissedRuns(Flow):
    def test_missing_sessions_are_counted_and_the_condor_is_settled_late(self):
        w = self.world(n_sessions=40)
        d0 = first_open_pair(w)
        w.seed_root(nyse.prev_td(d0))
        c = expected_condor(w, d0)
        d1 = c["expiry"]
        d2 = nyse.next_td(d1)
        d3 = nyse.next_td(d2)
        w.run(f"{d0} 16:20", strategy="c9")
        r = w.run(f"{d3} 16:20", strategy="c9")
        self.assertEqual(r.state["total_skips"]["C9"].get("missed_run"), 2)
        self.assertEqual(r.diag["missed"]["C9"], [str(d1), str(d2)])
        row = w.log()[0]
        self.assertEqual(row["exit_date"], str(d1))
        self.assertAlmostEqual(float(row["pnl_base"]), condor_pnl(c, w.px["SPY"][d1]), places=3)
        self.assertIn(f"settled late by run of {d3}", row["notes"])
        gate_d3 = w.spy_gate(d3)
        self.assertEqual(bool(r.state["strategies"]["C9"]["open_positions"]), gate_d3)

    def test_a_missed_monday_still_gets_its_model_record_but_no_real_trade(self):
        w = self.world(n_sessions=40)
        fri, mon, tue = D("2026-10-30"), D("2026-11-02"), D("2026-11-03")
        w.seed_root(nyse.prev_td(D("2026-10-26")))
        w.run("2026-10-26 16:20", strategy="c6")                                            # starts the book (a Monday)
        w.run(f"{fri} 16:20", strategy="c6")
        r = w.run(f"{tue} 16:20", strategy="c6")
        c6 = r.state["strategies"]["C6"]
        self.assertEqual(r.state["total_skips"]["C6"].get("GLD:missed_run"), 1)
        self.assertEqual(r.state["total_skips"]["C6"].get("TLT:missed_run"), 1)
        ids = {m["id"] for m in c6["model"]["open"]} | set(c6["entries_done"])
        self.assertIn(f"C6M-GLD-{mon}", ids)
        self.assertIn(f"C6M-TLT-{mon}", ids)
        self.assertNotIn(f"C6-GLD-{mon}", {p["id"] for p in c6["open_positions"]})


# ---------------------------------------------------------------------------------------------------- C6: weekly GLD / TLT put spreads
def expected_put_spread(w, tk, d):
    """The REAL spread the rules pick on `tk` at session d: expiry closest to 30 DTE inside [23, 37] (ties shorter), short put at
    |delta| closest to 0.25, long put the listed strike nearest short - max(1, round(2% x spot))."""
    rows = w.rows(tk, d)
    spot = w.px[tk][d]
    ex = min((e for e in {r[0] for r in rows} if 23 <= (e - d).days <= 37), key=lambda e: (abs((e - d).days - 30), e))
    ks = pick_short(rows, ex, -1, 0.25)[2]
    width = max(1, round(0.02 * spot))
    kl = nearest(rows, ex, -1, ks - width)
    legs = [(-1, ks, -1), (-1, kl, +1)]
    lq = [(q, *quote(rows, ex, cp, k)) for cp, k, q in legs]
    return {"expiry": ex, "ks": ks, "kl": kl, "legs": legs, "width": width, "spot": spot,
            "mid": cash(lq, 0.0), "base": cash(lq, 0.25), "nat": cash(lq, 0.5)}


def cost_to_close_mid(w, tk, t, sp):
    """Dollars to buy the short back and sell the long, both at mid, on session t."""
    rows = w.rows(tk, t)
    (bs_, as_), (bl, al) = quote(rows, sp["expiry"], -1, sp["ks"]), quote(rows, sp["expiry"], -1, sp["kl"])
    return (0.5 * (bs_ + as_) - 0.5 * (bl + al)) * 100


def close_cash_base(w, tk, t, sp):
    rows = w.rows(tk, t)
    (bs_, as_), (bl, al) = quote(rows, sp["expiry"], -1, sp["ks"]), quote(rows, sp["expiry"], -1, sp["kl"])
    return fill(bl, al, -1, 0.25) * 100 - fill(bs_, as_, +1, 0.25) * 100


class C6Weekly(Flow):
    D0 = D("2026-11-02")

    def setUp(self):
        self.w = self.world(n_sessions=45)
        self.w.seed_root(nyse.prev_td(self.D0))

    def run_days(self, a, b, **kw):
        out = []
        for t in self.w.sessions(a, b):
            out.append(self.w.run(f"{t} 16:20", strategy="c6", **kw))
        return out

    def test_monday_opens_the_spread_the_rules_pick_on_each_underlying(self):
        r = self.w.run(f"{self.D0} 16:20", strategy="c6")
        pos = {p["sleeve"]: p for p in r.state["strategies"]["C6"]["open_positions"]}
        self.assertEqual(set(pos), {"GLD", "TLT"})
        for tk, p in pos.items():
            sp = expected_put_spread(self.w, tk, self.D0)
            self.assertEqual(p["id"], f"C6-{tk}-{self.D0}")
            self.assertEqual((p["expiry"], p["spot_in"]), (str(sp["expiry"]), sp["spot"]), tk)
            self.assertEqual(p["strikes"], {"k_short": sp["ks"], "k_long": sp["kl"]}, tk)
            self.assertEqual(p["width"], sp["width"], tk)
            for name in ("mid", "base", "nat"):
                self.assertAlmostEqual(p["entry_cf"][name], sp[name], places=6, msg=f"{tk} {name}")
            self.assertTrue(23 <= p["dte_in"] <= 37)
            self.assertEqual(p["pair_id"], p["id"])
        self.assertEqual(self.w.state()["strategies"]["C6"]["sleeves"]["GLD"]["closed_n"], 0)
        self.assertEqual(sum("C6 NONEQ_VRP" in t for t in r.texts), 2)

    def test_the_model_path_waits_for_the_published_index_and_then_prices_the_backtest_spread(self):
        import rules_c6
        r1 = self.w.run(f"{self.D0} 16:20", strategy="c6")
        c6 = r1.state["strategies"]["C6"]
        self.assertEqual({m["status"] for m in c6["model"]["open"]}, {"pending"})       # GVZ / VXTLT for 11-02 are not published yet
        prov_gld = [p for p in c6["pairs"] if p["tk"] == "GLD"][0]
        self.assertIsNotNone(prov_gld["model_credit_base_prov"])                        # but the delayed quote gave a provisional credit
        self.assertIsNone(prov_gld["model_credit_base"])
        d1 = nyse.next_td(self.D0)
        r2 = self.w.run(f"{d1} 16:20", strategy="c6")
        c6 = r2.state["strategies"]["C6"]
        self.assertEqual({m["status"] for m in c6["model"]["open"]}, {"open"})
        for tk, idx in (("GLD", "GVZ"), ("TLT", "VXTLT")):
            pair = [p for p in c6["pairs"] if p["tk"] == tk][0]
            ent = rules_c6.model_entry(self.w.px[tk][self.D0], self.w.idx[idx][self.D0], 0.036, self.D0)
            self.assertAlmostEqual(pair["model_credit_base"], ent["ent"]["cf"][0.25], places=6, msg=tk)
            self.assertEqual((pair["model_Ks"], pair["model_Kl"]), (ent["Ks"], ent["Kl"]), tk)
            self.assertEqual(pair["model_expiry"], str(self.D0 + dt.timedelta(days=30)), tk)
            ratio = pair["real_credit_base"] / pair["model_credit_base"]
            self.assertTrue(0.6 < ratio < 1.6, f"{tk}: real/model {ratio:.2f} should be near 1 in this simulated market")

    def test_a_rally_closes_the_gld_spread_at_the_fifty_percent_profit_target(self):
        import rules_c6
        d0 = self.D0
        sp = expected_put_spread(self.w, "GLD", d0)
        base = self.w.px["GLD"][d0]
        for k, t in enumerate(self.w.sessions(nyse.next_td(d0), "2026-11-20"), start=1):
            self.w.set_close("GLD", t, base * (1.0 + 0.015 * min(k, 3)))
        due = next(t for t in self.w.sessions(nyse.next_td(d0), "2026-11-20")
                   if cost_to_close_mid(self.w, "GLD", t, sp) <= 0.5 * sp["base"])
        self.assertLess((due - d0).days, 10, "scenario: the profit target should be reached in the first days")
        runs = self.run_days(d0, nyse.next_td(due, 2))
        gld = [x for x in self.w.log() if x["id"] == f"C6-GLD-{d0}"]
        self.assertEqual(len(gld), 1)
        row = gld[0]
        self.assertEqual((row["exit_date"], row["exit_reason"], row["version"]), (str(due), "profit", "real"))
        exp = sp["base"] + close_cash_base(self.w, "GLD", due, sp) - 0.05 * 4
        self.assertAlmostEqual(float(row["pnl_base"]), exp, places=3)
        blk = runs[-1].state["strategies"]["C6"]
        self.assertEqual(blk["sleeves"]["GLD"]["closed_n"], 1)
        self.assertIn(f"C6-GLD-{d0}", " ".join(blk["exit_alerts"]))
        pair = [p for p in blk["pairs"] if p["pair_id"] == f"C6-GLD-{d0}"][0]
        self.assertAlmostEqual(pair["real_exit_mark"], cost_to_close_mid(self.w, "GLD", due, sp), places=6)
        self.assertEqual(pair["real_exit_date"], str(due))
        self.assertIsNotNone(pair["model_exit_mark"])                                   # filled once the published index for `due` came out
        # the MODEL trade closes by the same rule on the published data, one run after `due`
        mrow = [x for x in self.w.log() if x["id"] == f"C6M-GLD-{d0}"]
        self.assertEqual(len(mrow), 1)
        self.assertEqual(mrow[0]["version"], "model")
        ent = rules_c6.model_entry(self.w.px["GLD"][d0], self.w.idx["GVZ"][d0], 0.036, d0)
        mexp = None
        t = d0
        while mexp is None:
            t = nyse.next_td(t)
            dte = (ent["expiry"] - t).days
            mk = rules_c6.model_mark(ent["Ks"], ent["Kl"], self.w.px["GLD"][t], self.w.idx["GVZ"][t], 0.036, dte)
            if mk <= 0.5 * ent["ent"]["cf"][0.25]:
                mexp = (t, "profit")
            elif dte <= 7:
                mexp = (t, "dte")
        self.assertEqual((mrow[0]["exit_date"], mrow[0]["exit_reason"]), (str(mexp[0]), mexp[1]))
        # one real exit message and one model exit message for GLD
        texts = self.w.sender.texts
        self.assertEqual(sum("C6 NONEQ_VRP GLD | closed" in t for t in texts), 1)
        self.assertEqual(sum("C6 NONEQ_VRP GLD MODEL | closed" in t for t in texts), 1)

    def test_a_slide_holds_the_spread_to_the_seven_day_exit_on_the_early_close_friday(self):
        d0, ex = self.D0, None
        sp = expected_put_spread(self.w, "GLD", d0)
        base = self.w.px["GLD"][d0]
        for k, t in enumerate(self.w.sessions(nyse.next_td(d0), "2026-12-04"), start=1):
            self.w.set_close("GLD", t, base * (1.0 - 0.012 * min(k, 3)))
        ex = sp["expiry"]
        due = next(t for t in self.w.sessions(d0, ex) if (ex - t).days <= 7)
        self.assertEqual(due, D("2026-11-27"))
        self.assertTrue(nyse.is_early_close(due))
        for t in self.w.sessions(nyse.next_td(d0), due):                                 # the profit target is never touched
            if t < due:
                self.assertGreater(cost_to_close_mid(self.w, "GLD", t, sp), 0.5 * sp["base"], str(t))
        self.run_days(d0, due)
        row = [x for x in self.w.log() if x["id"] == f"C6-GLD-{d0}"][0]
        self.assertEqual((row["exit_date"], row["exit_reason"], row["dte_out"]), (str(due), "dte", "7"))
        exp = sp["base"] + close_cash_base(self.w, "GLD", due, sp) - 0.05 * 4
        self.assertAlmostEqual(float(row["pnl_base"]), exp, places=3)
        self.assertLess(float(row["pnl_base"]), 0)                                      # the slide lost money

    def test_each_monday_adds_its_own_pair_and_older_spreads_stay_open(self):
        self.run_days(self.D0, "2026-11-10")
        st = self.w.state()["strategies"]["C6"]
        ids = {p["id"] for p in st["open_positions"]} | {r["id"] for r in self.w.log() if r["version"] == "real"}
        self.assertEqual(ids, {f"C6-{tk}-{d}" for tk in ("GLD", "TLT") for d in ("2026-11-02", "2026-11-09")})
        self.assertEqual(len({p["pair_id"] for p in st["pairs"]}), 4)
        self.assertEqual(len(st["entries_done"]), 8)                                    # 4 real + 4 model ids

    def test_no_listed_expiry_near_thirty_days_skips_the_real_trade_but_keeps_the_model_record(self):
        orig = World.listed_expiries
        self.w.listed_expiries = lambda sym, d: [e for e in orig(self.w, sym, d) if sym == "SPY" or not 20 <= (e - d).days <= 40]
        r = self.w.run(f"{self.D0} 16:20", strategy="c6")
        c6 = r.state["strategies"]["C6"]
        self.assertEqual(c6["open_positions"], [])
        self.assertEqual(r.state["total_skips"]["C6"], {"GLD:no_expiry": 1, "TLT:no_expiry": 1})
        self.assertEqual({m["id"] for m in c6["model"]["open"]}, {f"C6M-GLD-{self.D0}", f"C6M-TLT-{self.D0}"})

    def test_a_tuesday_run_opens_nothing_new(self):
        self.w.run(f"{self.D0} 16:20", strategy="c6")
        d1 = nyse.next_td(self.D0)
        r = self.w.run(f"{d1} 16:20", strategy="c6")
        self.assertEqual(len(r.state["strategies"]["C6"]["open_positions"]), 2)
        self.assertEqual(r.state["strategies"]["C6"]["entries_done"].count(f"C6-GLD-{self.D0}"), 1)
        self.assertNotIn("c6", {k for k in r.diag if k == "c6" and r.diag[k].get("GLD")})   # nothing evaluated for a new entry


# ---------------------------------------------------------------------------------------------------- C3: the monthly SPY hedge
def expected_hedge(w, d):
    """The would-be ticket on session d: put debit spread, expiry closest to 60 DTE inside [50, 75], long put nearest 95% of
    spot, short put nearest 85%."""
    rows = w.rows("SPY", d)
    spot = w.px["SPY"][d]
    ex = min((e for e in {r[0] for r in rows} if 50 <= (e - d).days <= 75), key=lambda e: (abs((e - d).days - 60), e))
    k1, k2 = nearest(rows, ex, -1, 0.95 * spot), nearest(rows, ex, -1, 0.85 * spot)
    legs = [(-1, k1, +1), (-1, k2, -1)]
    lq = [(q, *quote(rows, ex, cp, k)) for cp, k, q in legs]
    return {"expiry": ex, "k1": k1, "k2": k2, "legs": legs, "spot": spot, "mid": cash(lq, 0.0), "base": cash(lq, 0.25),
            "nat": cash(lq, 0.5)}


def hedge_close_cash_base(w, t, h):
    rows = w.rows("SPY", t)
    (b1, a1), (b2, a2) = quote(rows, h["expiry"], -1, h["k1"]), quote(rows, h["expiry"], -1, h["k2"])
    return fill(b1, a1, -1, 0.25) * 100 - fill(b2, a2, +1, 0.25) * 100


def linear_quantile(values, q):
    s = sorted(values)
    pos = q * (len(s) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


class C3Monthly(Flow):
    D0 = D("2026-11-02")

    def setUp(self):
        self.w = self.world(n_sessions=45)
        self.w.seed_root(nyse.prev_td(self.D0))
        self.h = expected_hedge(self.w, self.D0)

    def open_gate(self, vix=12.0, vix3m=15.5):
        self.w.set_index("VIX", self.D0, vix)
        self.w.set_index("VIX3M", self.D0, vix3m)

    def c3(self, when, **kw):
        return self.w.run(when, strategy="c3", **kw)

    def test_an_open_gate_is_confirmed_by_the_published_closes_and_the_hedge_starts(self):
        self.open_gate()
        r1 = self.c3(f"{self.D0} 16:20")
        c3 = r1.state["strategies"]["C3"]
        self.assertEqual(c3["open_positions"], [])
        self.assertTrue(c3["candidate"]["prov"]["passes"])                                # the delayed quote already says open
        self.assertEqual([t for t in r1.texts if "C3" in t], [])                          # but nothing is announced yet
        d1 = nyse.next_td(self.D0)
        r2 = self.c3(f"{d1} 16:20")
        c3 = r2.state["strategies"]["C3"]
        self.assertIsNone(c3["candidate"])
        p = c3["open_positions"][0]
        h_ = self.h
        self.assertEqual((p["id"], p["entry_date"], p["expiry"]), (f"C3-{self.D0}", str(self.D0), str(h_["expiry"])))
        self.assertEqual(p["strikes"], {"k_long": h_["k1"], "k_short": h_["k2"]})
        self.assertEqual(p["spot_in"], h_["spot"])                                        # priced at the 11-02 snapshot, not 11-03's
        for name in ("mid", "base", "nat"):
            self.assertAlmostEqual(p["entry_cf"][name], h_[name], places=6, msg=name)
        self.assertLess(p["entry_cf"]["base"], 0)
        prior = [v for d, v in sorted(self.w.idx["VIX"].items()) if d < self.D0][-252:]
        g = c3["gate_log"][-1]
        self.assertEqual((g["date"], g["vix"], g["vix3m"], g["passes"], g["flipped"], g["confirmed_on"]),
                         (str(self.D0), 12.0, 15.5, True, False, str(d1)))
        self.assertAlmostEqual(g["q25"], linear_quantile(prior, 0.25), places=9)
        self.assertAlmostEqual(g["ratio"], 12.0 / 15.5, places=9)
        self.assertAlmostEqual(c3["premium_paid_since_reset"], -h_["base"] + 0.05 * 2, places=5)
        self.assertEqual(sum("C3 SPY_HEDGE" in t for t in r2.texts), 2)                   # the entry and the monthly gate line
        self.assertTrue(any("monthly gate OPEN" in t for t in r2.texts))

    def test_a_closed_gate_discards_the_candidate_and_says_so(self):
        self.w.set_index("VIX", self.D0, 19.5)
        self.c3(f"{self.D0} 16:20")
        r2 = self.c3(f"{nyse.next_td(self.D0)} 16:20")
        c3 = r2.state["strategies"]["C3"]
        self.assertEqual((c3["open_positions"], c3["candidate"]), ([], None))
        self.assertFalse(c3["gate_log"][-1]["passes"])
        self.assertTrue(any("monthly gate CLOSED" in t for t in r2.texts))
        self.assertEqual(c3["premium_paid_since_reset"], 0.0)

    def test_a_gate_that_flips_between_the_quote_and_the_published_close_is_flagged(self):
        self.w.set_index("VIX", self.D0, 19.5)
        self.w.prov_noise = -8.0                                                          # the delayed quote reads 11.5 / 14.4
        r1 = self.c3(f"{self.D0} 16:20")
        self.assertTrue(r1.state["strategies"]["C3"]["candidate"]["prov"]["passes"])
        r2 = self.c3(f"{nyse.next_td(self.D0)} 16:20")
        g = r2.state["strategies"]["C3"]["gate_log"][-1]
        self.assertEqual((g["prov_passes"], g["passes"], g["flipped"]), (True, False, True))
        emb = [p["embeds"][0]["description"] for _u, p in self.w.sender.posts if "monthly gate" in p["content"]]
        self.assertIn("FLIPPED", emb[-1])
        self.assertEqual(r2.state["strategies"]["C3"]["open_positions"], [])             # the published close decides

    def test_published_closes_that_never_arrive_end_in_an_unconfirmed_month_after_five_runs(self):
        self.open_gate()
        down = {"history_VIX.csv": None, "history_VIX3M.csv": None}
        days = self.w.sessions(self.D0, "2026-11-10")
        seen = []
        for t in days:
            r = self.c3(f"{t} 16:20", overrides=down)
            cand = r.state["strategies"]["C3"]["candidate"]
            seen.append(None if cand is None else cand["wait_runs"])
        self.assertEqual(seen, [0, 1, 2, 3, 4, 5, None])
        c3 = r.state["strategies"]["C3"]
        self.assertEqual(c3["gate_log"][-1]["why"], "unconfirmed")
        self.assertEqual(r.state["total_skips"]["C3"], {"gate_unconfirmed": 1})
        self.assertEqual(c3["open_positions"], [])
        self.assertTrue(any("UNCONFIRMED" in p["embeds"][0]["description"] + p["content"] for _u, p in self.w.sender.posts))

    def test_re_running_a_session_does_not_advance_the_confirmation_wait(self):
        self.open_gate()
        down = {"history_VIX.csv": None, "history_VIX3M.csv": None}
        self.c3(f"{self.D0} 16:20", overrides=down)
        d1 = nyse.next_td(self.D0)
        for minute in (20, 26, 31, 40):                                                   # the same session, four times
            r = self.c3(f"{d1} 16:{minute}", overrides=down, trigger="manual")
        cand = r.state["strategies"]["C3"]["candidate"]
        self.assertEqual((cand["wait_runs"], cand["wait_dates"]), (1, [str(d1)]))
        r = self.c3(f"{nyse.next_td(d1)} 16:20", overrides=down)
        self.assertEqual(r.state["strategies"]["C3"]["candidate"]["wait_runs"], 2)        # the next session is the second count

    def test_late_published_closes_still_confirm_the_candidate(self):
        self.open_gate()
        down = {"history_VIX.csv": None, "history_VIX3M.csv": None}
        for t in self.w.sessions(self.D0, "2026-11-05"):
            self.c3(f"{t} 16:20", overrides=down)
        r = self.c3("2026-11-06 16:20")
        c3 = r.state["strategies"]["C3"]
        self.assertEqual([p["id"] for p in c3["open_positions"]], [f"C3-{self.D0}"])
        self.assertEqual(c3["gate_log"][-1]["confirmed_on"], "2026-11-06")
        self.assertEqual(c3["open_positions"][0]["spot_in"], self.h["spot"])             # still the entry-day snapshot

    def test_the_hedge_is_closed_on_the_first_day_with_twenty_one_days_or_less_to_expiry(self):
        self.open_gate()
        self.c3(f"{self.D0} 16:20")
        self.c3(f"{nyse.next_td(self.D0)} 16:20")
        ex = self.h["expiry"]
        due = next(t for t in self.w.sessions(self.D0, ex) if (ex - t).days <= 21)
        before = nyse.prev_td(due)
        r0 = self.c3(f"{before} 16:20")
        self.assertEqual(len(r0.state["strategies"]["C3"]["open_positions"]), 1)          # one day early: still open
        r = self.c3(f"{due} 16:20")
        row = self.w.log()[0]
        self.assertEqual((row["id"], row["exit_date"], row["exit_reason"], row["dte_out"]), (f"C3-{self.D0}", str(due), "dte", str((ex - due).days)))
        self.assertLessEqual(int(row["dte_out"]), 21)
        exp = self.h["base"] + hedge_close_cash_base(self.w, due, self.h) - 0.05 * 4
        self.assertAlmostEqual(float(row["pnl_base"]), exp, places=3)
        c3 = r.state["strategies"]["C3"]
        self.assertEqual((c3["closed_n"], c3["open_positions"]), (1, []))
        self.assertAlmostEqual(c3["premium_paid_since_reset"], -float(row["pnl_base"]), places=4)   # the ledger nets to the loss
        self.assertTrue(any("C3 SPY_HEDGE" in t and "closed" in t for t in r.texts))

    def test_a_ten_percent_drawdown_writes_a_hedge_ledger_entry_with_the_open_hedge_marked(self):
        self.open_gate()
        self.c3(f"{self.D0} 16:20")
        d1 = nyse.next_td(self.D0)
        self.c3(f"{d1} 16:20")
        d2 = nyse.next_td(d1)
        peak = max(v for d, v in self.w.px["SPY"].items() if d <= d1 and d >= d1 - dt.timedelta(days=365))
        self.w.set_close("SPY", d2, peak * 0.885)
        r = self.c3(f"{d2} 16:20")
        led = r.state["strategies"]["C3"]["hedge_ledger"]
        self.assertEqual(len(led), 1)
        e = led[0]
        self.assertEqual((e["date"], e["level"], e["spy_close"]), (str(d2), 0.1, self.w.px["SPY"][d2]))
        self.assertAlmostEqual(e["peak"], peak)
        rows = self.w.rows("SPY", d2)
        (b1, a1), (b2, a2) = quote(rows, self.h["expiry"], -1, self.h["k1"]), quote(rows, self.h["expiry"], -1, self.h["k2"])
        value = (0.5 * (b1 + a1) - 0.5 * (b2 + a2)) * 100
        self.assertEqual(len(e["open"]), 1)
        self.assertAlmostEqual(e["open"][0]["mid_value"], round(value, 2), places=2)
        self.assertAlmostEqual(e["open"][0]["mid_pnl"], round(value + self.h["mid"], 2), places=2)
        self.assertGreater(e["open"][0]["mid_pnl"], 0)                                    # the hedge is in the money after a -11.5% day
        r2 = self.c3(f"{nyse.next_td(d2)} 16:20")                                         # the same level is not recorded twice
        self.assertEqual(len(r2.state["strategies"]["C3"]["hedge_ledger"]), 1)

    def test_skipping_the_first_trading_day_of_a_month_is_counted(self):
        self.open_gate()
        self.c3(f"{self.D0} 16:20")
        r = self.c3("2026-12-02 16:20")                                                    # 12-01 never ran
        self.assertEqual(r.state["total_skips"]["C3"].get("missed_run"), 1)


# ---------------------------------------------------------------------------------------------------- eight weeks, every session
def parse_legs(label_text):
    """'SELL 1 473C 2026-11-03 ; BUY 1 478C 2026-11-03' -> [(cp, strike, qty)]."""
    out = []
    for part in label_text.split(" ; "):
        side, qty, ks, _ex = part.split()
        out.append((+1 if ks.endswith("C") else -1, float(ks[:-1]), (1 if side == "BUY" else -1) * int(qty)))
    return out


def vix_gate(w, d):
    """C3 gate restated: VIX/VIX3M < 0.90 and VIX <= the 25th percentile of the previous 252 published VIX closes."""
    prior = [v for x, v in sorted(w.idx["VIX"].items()) if x < d][-252:]
    return w.idx["VIX"][d] / w.idx["VIX3M"][d] < 0.90 and w.idx["VIX"][d] <= linear_quantile(prior, 0.25)


def expected_c6_real_exit(w, tk, d0):
    sp = expected_put_spread(w, tk, d0)
    for t in w.sessions(nyse.next_td(d0), w.days[-1]):
        if cost_to_close_mid(w, tk, t, sp) <= 0.5 * sp["base"]:
            return t, "profit"
        if (sp["expiry"] - t).days <= 7:
            return t, "dte"
    return None, None


class Soak(Base):
    """One run of every session from 2026-10-26 to 2026-12-18 (39 sessions: Thanksgiving, the early-close Friday, eight Mondays,
    two month starts), shared by the tests below. Each test re-derives one part of the book independently."""
    FIRST, LAST = "2026-10-26", "2026-12-18"

    @classmethod
    def setUpClass(cls):
        from tests.helpers import TempRoot
        cls._tmp = TempRoot()
        w = cls.w = World(cls._tmp.path, first_session=cls.FIRST, n_sessions=45)
        w.set_index("VIX", "2026-11-02", 12.0)                                         # an open C3 gate for November
        w.set_index("VIX3M", "2026-11-02", 15.5)
        cls.sessions = w.sessions(cls.FIRST, cls.LAST)
        cls.runs = [w.run(f"{t} 16:20") for t in cls.sessions]
        cls.state = w.state()
        cls.rows = w.log()
        cls.all_keys = set().union(*(r.state["sent"] for r in cls.runs))                  # the ledger itself forgets keys after 45 days

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls._tmp.path, ignore_errors=True)

    def test_every_run_succeeds_quietly(self):
        self.assertEqual(len(self.sessions), 39)
        self.assertTrue(all(r.code == 0 for r in self.runs))
        self.assertEqual([r.stderr for r in self.runs if r.stderr], [])
        self.assertEqual([(r.when, r.diag["problems"]) for r in self.runs if r.diag["problems"]], [])
        self.assertEqual({r.diag["phase"] for r in self.runs}, {"close"})
        self.assertEqual(len(self.w.diag_lines()), 39)
        self.assertEqual(self.state["runs"], {"2026-W44": 5, "2026-W45": 5, "2026-W46": 5, "2026-W47": 5, "2026-W48": 3 + 1,
                                              "2026-W49": 5, "2026-W50": 5, "2026-W51": 5})

    def test_c9_trades_exactly_the_open_gate_nights_and_every_pnl_is_right(self):
        w = self.w
        rows = [r for r in self.rows if r["strategy"] == "C9"]
        nights = {r["entry_date"] for r in rows} | {p["entry_date"] for p in self.state["strategies"]["C9"]["open_positions"]}
        want = {str(t) for t in self.sessions if w.spy_gate(t) and not nyse.is_early_close(t)}
        self.assertEqual(nights, want)
        self.assertGreater(len(want), 15)
        self.assertLess(len(want), len(self.sessions) - 10, "scenario: the gate must be shut on a fair number of nights too")
        for r in rows:
            expiry = D(r["expiry"])
            self.assertEqual(expiry, nyse.next_td(D(r["entry_date"])))
            legs = parse_legs(r["legs"])
            exp = float(r["credit_base"]) + payoff(legs, w.px["SPY"][expiry]) - 0.20
            self.assertAlmostEqual(float(r["pnl_base"]), exp, places=3, msg=r["id"])
            self.assertEqual((r["exit_date"], r["exit_reason"], r["version"]), (str(expiry), "expiry", "real"))
            self.assertEqual(sorted(k for _cp, k, _q in legs)[0] < w.px["SPY"][D(r["entry_date"])] < sorted(k for _cp, k, _q in legs)[-1], True)
        self.assertEqual(self.state["total_skips"]["C9"], {"gate_closed": len(self.sessions) - len(want) - 1, "early_close": 1})

    def test_c6_opens_both_underlyings_every_monday_and_closes_by_the_rules(self):
        w = self.w
        mondays = [t for t in self.sessions if t.weekday() == 0]
        self.assertEqual(len(mondays), 8)
        real = [r for r in self.rows if r["strategy"] == "C6" and r["version"] == "real"]
        open_real = self.state["strategies"]["C6"]["open_positions"]
        self.assertEqual({r["id"] for r in real} | {p["id"] for p in open_real},
                         {f"C6-{tk}-{d}" for tk in ("GLD", "TLT") for d in mondays})
        for r in real:
            d0 = D(r["entry_date"])
            tk = r["sleeve"]
            sp = expected_put_spread(w, tk, d0)
            t, why = expected_c6_real_exit(w, tk, d0)
            self.assertEqual((r["exit_date"], r["exit_reason"]), (str(t), why), r["id"])
            self.assertAlmostEqual(float(r["credit_base"]), sp["base"], places=3, msg=r["id"])
            exp = sp["base"] + close_cash_base(w, tk, t, sp) - 0.20
            self.assertAlmostEqual(float(r["pnl_base"]), exp, places=3, msg=r["id"])
        for p in open_real:                                                              # not due yet, by the same oracle
            t, _why = expected_c6_real_exit(w, p["sleeve"], D(p["entry_date"]))
            self.assertTrue(t is None or t > self.sessions[-1], p["id"])                  # None: not before the end of the simulation
        model_ids = {r["id"] for r in self.rows if r["version"] == "model"}
        open_model = {m["id"] for m in self.state["strategies"]["C6"]["model"]["open"]}
        self.assertEqual(model_ids | open_model, {f"C6M-{tk}-{d}" for tk in ("GLD", "TLT") for d in mondays})

    def test_c3_follows_the_gate_and_the_november_hedge_is_closed_on_time(self):
        w = self.w
        gl = {g["date"]: g for g in self.state["strategies"]["C3"]["gate_log"]}
        for d in ("2026-11-02", "2026-12-01"):
            self.assertEqual(gl[d]["passes"], vix_gate(w, D(d)), d)
        self.assertTrue(gl["2026-11-02"]["passes"])
        hedge = [r for r in self.rows if r["strategy"] == "C3"]
        h_ = expected_hedge(w, D("2026-11-02"))
        due = next(t for t in self.sessions if t >= D("2026-11-02") and (h_["expiry"] - t).days <= 21)
        self.assertEqual([(r["id"], r["exit_date"]) for r in hedge], [("C3-2026-11-02", str(due))])
        exp = h_["base"] + hedge_close_cash_base(w, due, h_) - 0.20
        self.assertAlmostEqual(float(hedge[0]["pnl_base"]), exp, places=3)
        december = [p["id"] for p in self.state["strategies"]["C3"]["open_positions"]]
        self.assertEqual(december, ["C3-2026-12-01"] if gl["2026-12-01"]["passes"] else [])

    def test_the_books_agree_with_the_log(self):
        for sid in ("C9", "C6", "C3"):
            blk = self.state["strategies"][sid]
            real = [r for r in self.rows if r["strategy"] == sid and r["version"] == "real"]
            self.assertEqual(blk["closed_n"], len(real), sid)
            self.assertAlmostEqual(blk["cum_pnl_base"], sum(float(r["pnl_base"]) for r in real), places=3, msg=sid)
            self.assertAlmostEqual(blk["cum_pnl_nat"], sum(float(r["pnl_nat"]) for r in real), places=3, msg=sid)
            ids = [p["id"] for p in blk["open_positions"]]
            self.assertEqual(len(ids), len(set(ids)), sid)
            self.assertFalse(set(ids) & {r["id"] for r in real}, f"{sid}: a position is both open and closed")
            self.assertEqual(blk["unpriced_n"], 0)
        m = self.state["strategies"]["C6"]["model"]
        model = [r for r in self.rows if r["version"] == "model"]
        self.assertEqual(m["closed_n"], len(model))
        self.assertAlmostEqual(m["cum_pnl_base"], sum(float(r["pnl_base"]) for r in model), places=3)
        self.assertEqual(len({r["id"] for r in self.rows}), len(self.rows), "duplicate ids in the trade log")
        self.assertEqual([r["id"] for r in self.rows if not r["pnl_base"]], [])
        c9 = self.state["strategies"]["C9"]
        self.assertEqual(c9["status"], "active")
        self.assertFalse(c9["primed"] and not c9["closed_n"] >= 20)

    def test_the_message_stream_is_labelled_unique_and_complete(self):
        posts = self.w.sender.posts
        texts = [p["content"] for _u, p in posts]
        self.assertTrue(all(t.startswith("SHADOW - NOT A TRADE | ") for t in texts))
        self.assertEqual(len(posts), len(self.all_keys))                                 # one post per message key, none twice
        self.assertEqual(len({(p["content"], json.dumps(p.get("embeds"), sort_keys=True)) for _u, p in posts}), len(posts))
        self.assertEqual(self.state["outbox"], [])
        self.assertEqual(sum("shadow bots online" in t for t in texts), 1)
        self.assertEqual(sorted(k for k in self.all_keys if k.startswith("weekly:")),
                         [f"weekly:2026-W{n}" for n in range(44, 52)])
        entries = [t for t in texts if "C9 CONDOR_1D" in t and "settled" not in t]
        c9_rows = [r for r in self.rows if r["strategy"] == "C9"]
        self.assertEqual(len(entries), len(c9_rows) + len(self.state["strategies"]["C9"]["open_positions"]))
        self.assertEqual(sum("C9 CONDOR_1D | settled" in t for t in texts), len(c9_rows))
        self.assertFalse([t for t in texts if "FLAGGED" in t], "no kill-switch flag expected in this simulated book")

    def test_weekly_summaries_count_every_run_including_their_own(self):
        """The summary is written during Friday's own run: that run must already be in "job runs N/M". Every session ran here."""
        weekly = [p["embeds"][0]["description"] for _u, p in self.w.sender.posts if "weekly summary" in p["content"]]
        self.assertEqual(len(weekly), 8)
        for text in weekly:
            m = re.search(r"job runs (\d+)/(\d+) expected", text)
            self.assertIsNotNone(m, text[:80])
            self.assertEqual(m.group(1), m.group(2), text.split("\n")[0])
        self.assertEqual([re.search(r"job runs (\d+)/", t).group(1) for t in weekly], ["5", "5", "5", "5", "4", "5", "5", "5"])
        self.assertIn("backtest +$1.83 / +$0.71 (+$1.47 / +$0.35 at official-close settlement)", weekly[0])

    def test_running_the_last_session_again_changes_nothing(self):
        before = json.dumps({k: v for k, v in self.state.items() if k != "last_run"}, sort_keys=True)
        r = self.w.run(f"{self.LAST} 16:26", trigger="manual")
        self.assertEqual(r.n_posts, 0)
        self.assertEqual(json.dumps({k: v for k, v in r.state.items() if k != "last_run"}, sort_keys=True), before)
        self.assertEqual(len(self.w.log()), len(self.rows))

    def test_the_export_matches_the_book_and_is_marked_shadow(self):
        with open(self.w.path(cfg.EXPORT_FILE)) as f:
            snap = json.load(f)
        self.assertTrue(snap["shadow"])
        self.assertNotIn("history", snap)
        for sid in ("C9", "C6", "C3"):
            e = snap["strategies"][sid]
            self.assertEqual(e["trades_since_reset"], self.state["strategies"][sid]["closed_n"], sid)
            self.assertAlmostEqual(e["cumulative_pnl_base"], self.state["strategies"][sid]["cum_pnl_base"], places=3)
            self.assertEqual(len(e["open_positions"]), len(self.state["strategies"][sid]["open_positions"]), sid)

    def test_the_data_files_are_trimmed_sorted_and_free_of_temp_files(self):
        m = self.w.market()
        for s_ in ("SPY", "GLD", "TLT"):
            self.assertEqual(m.closes[s_].last_date(), D(self.LAST))
            self.assertEqual(m.closes[s_].dates, sorted(m.closes[s_].dates))
            self.assertGreaterEqual(m.closes[s_].dates[0], D(self.LAST) - dt.timedelta(days=cfg.CLOSES_KEEP_DAYS))
        self.assertEqual(m.index_series("VIX").last_date(), D("2026-12-17"))              # 12-18 is published after the run
        self.assertIn(("VIX", D(self.LAST)), m.prov)
        for dirpath, _dirs, files in os.walk(self.w.root):
            self.assertEqual([f for f in files if f.endswith(".tmp")], [])
        with open(self.w.path("data", "closes.csv")) as f:
            keys = [(r["date"], r["symbol"]) for r in csv.DictReader(f)]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(len(keys), len(set(keys)))


# ---------------------------------------------------------------------------------------------------- switches, outages, replays
class StatusAndSubsets(Flow):
    def setUp(self):
        self.w = self.world(n_sessions=45)
        self.d0 = first_open_pair(self.w)
        self.w.seed_root(nyse.prev_td(self.d0))
        self.c = expected_condor(self.w, self.d0)
        self.d1 = self.c["expiry"]

    def set_status(self, sid, status):
        st = self.w.state()
        st["strategies"][sid]["status"] = status
        state_mod.save(st, self.w.path(cfg.STATE_FILE))

    def test_a_strategy_subset_leaves_the_other_books_untouched(self):
        r = self.w.run(f"{self.d0} 16:20", strategy="c9")
        st = r.state
        self.assertEqual(set(st["last_session"]), {"C9"})
        self.assertIsNone(st["strategies"]["C6"]["started"])
        self.assertIsNone(st["strategies"]["C3"]["started"])
        self.assertEqual(st["strategies"]["C6"]["pairs"], [])
        self.assertEqual(st["last_run"]["strategies"], ["C9"])
        self.assertNotIn("nasdaq_GLD.json", [n for _t, n in self.w.calls])               # no GLD data was even requested
        self.assertNotIn("options_TLT.json", [n for _t, n in self.w.calls])

    def test_two_subsets_on_one_day_are_two_runs_of_one_session(self):
        self.w.run(f"{self.d0} 16:20", strategy="c9")
        r = self.w.run(f"{self.d0} 16:26", strategy="c6,c3", trigger="manual")
        self.assertEqual(set(r.state["last_session"]), {"C9", "C6", "C3"})
        self.assertEqual(len(r.state["strategies"]["C9"]["entries_done"]), 1)
        self.assertEqual(r.state["runs"][f"{self.d0.isocalendar()[0]}-W{self.d0.isocalendar()[1]:02d}"], 1)

    def test_a_flagged_strategy_keeps_logging_and_keeps_entering(self):
        self.w.run(f"{self.d0} 16:20", strategy="c9")
        self.set_status("C9", "flagged")
        r = self.w.run(f"{self.d1} 16:20", strategy="c9")
        blk = r.state["strategies"]["C9"]
        self.assertEqual((blk["status"], blk["closed_n"]), ("flagged", 1))
        self.assertEqual([p["id"] for p in blk["open_positions"]], [f"C9-{self.d1}"] if self.w.spy_gate(self.d1) else [])
        self.assertTrue(any("[FLAGGED]" in t for t in r.texts))                           # the status shows in the headline

    def test_a_retired_strategy_opens_nothing_and_stays_silent_but_closes_what_is_open(self):
        self.w.run(f"{self.d0} 16:20", strategy="c9")
        self.set_status("C9", "retired")
        r = self.w.run(f"{self.d1} 16:20", strategy="c9")
        blk = r.state["strategies"]["C9"]
        self.assertEqual((blk["closed_n"], blk["open_positions"]), (1, []))               # the open condor was still settled and logged
        self.assertEqual(len(self.w.log()), 1)
        self.assertEqual(r.n_posts, 0)                                                    # but no message about it, and no new entry
        self.assertEqual(r.diag["c9"]["result"], "blocked:retired")
        self.assertEqual(r.state["total_skips"]["C9"], {})                                # a retired strategy does not count skips

    def test_a_retired_sleeve_is_left_alone_while_the_other_trades(self):
        d = D("2026-11-02")
        self.w.seed_root(nyse.prev_td(d))
        st = self.w.state()
        st["strategies"]["C6"]["sleeves"]["GLD"]["status"] = "retired"
        state_mod.save(st, self.w.path(cfg.STATE_FILE))
        r = self.w.run(f"{d} 16:20", strategy="c6")
        c6 = r.state["strategies"]["C6"]
        self.assertEqual([p["id"] for p in c6["open_positions"]], [f"C6-TLT-{d}"])
        self.assertEqual([m["id"] for m in c6["model"]["open"]], [f"C6M-TLT-{d}"])


class Outages(Flow):
    def setUp(self):
        self.w = self.world(n_sessions=40)
        self.d0 = first_open_pair(self.w)
        self.w.seed_root(nyse.prev_td(self.d0))
        self.d1 = nyse.next_td(self.d0)

    def test_messages_wait_in_the_outbox_while_discord_is_down_and_arrive_in_order(self):
        self.w.sender = FakeSender(fail_with=[500])
        r1 = self.w.run(f"{self.d0} 16:20", strategy="c9")
        self.assertEqual(r1.code, 0)                                                      # an outage never fails the job
        self.assertEqual([m["kind"] for m in r1.state["outbox"]], ["hello", "entry"])
        self.assertEqual(r1.n_posts, 1)                                                   # one attempt, then it stopped hammering
        self.assertIn("stays queued", r1.stderr)
        r2 = self.w.run(f"{self.d1} 16:20", strategy="c9")
        self.assertEqual(r2.state["outbox"], [])
        at = lambda needle: next(i for i, t in enumerate(r2.texts) if needle in t)        # noqa: E731
        self.assertEqual(at("shadow bots online"), 0)                                     # the oldest message goes out first
        self.assertLess(at(f"C9 CONDOR_1D | {self.d0} 16:20 ET"), at("C9 CONDOR_1D | settled"))    # then the entry, then its settlement
        self.assertEqual(sum("shadow bots online" in t for t in self.w.sender.texts[1:]), 1)       # delivered once (texts[0] was the failed try)
        self.assertEqual(len(r2.texts), len(set(r2.texts)), "no message was sent twice")

    def test_a_revoked_webhook_keeps_the_messages_and_the_job_green(self):
        self.w.sender = FakeSender(fail_with=[404, 404, 404, 404])
        r = self.w.run(f"{self.d0} 16:20", strategy="c9")
        self.assertEqual(r.code, 0)
        self.assertEqual(r.n_posts, 1)
        self.assertEqual(len(r.state["outbox"]), 2)
        self.assertEqual(len(r.state["strategies"]["C9"]["open_positions"]), 1)

    def test_without_a_webhook_secret_everything_is_kept_for_later(self):
        r = self.w.run(f"{self.d0} 16:20", strategy="c9", webhook=None)
        self.assertEqual((r.code, r.n_posts, len(r.state["outbox"])), (0, 0, 2))
        self.assertIn("SHADOW_DISCORD_WEBHOOK_URL is not set", r.stderr)
        r2 = self.w.run(f"{self.d1} 16:20", strategy="c9")
        self.assertEqual(r2.state["outbox"], [])
        self.assertTrue(any("shadow bots online" in t for t in r2.texts))

    def test_a_message_that_cannot_be_delivered_for_four_days_is_dropped(self):
        days = [self.d0]
        while len(days) < 5:
            days.append(nyse.next_td(days[-1]))
        for t in days[:-1]:
            self.w.run(f"{t} 16:20", strategy="c9", webhook=None)
        r = self.w.run(f"{days[-1]} 16:20", strategy="c9")
        self.assertEqual(r.state["outbox"], [])
        self.assertFalse(any("shadow bots online" in t for t in self.w.sender.texts), "the first day's hello is older than 3 days")
        self.assertTrue(any("dropped an undeliverable message" in e for e in r.diag["events"]))
        self.assertGreaterEqual(r.n_posts, 1)

    def test_one_data_notice_per_session_however_often_it_is_run(self):
        r1 = self.w.run(f"{self.d0} 16:20", strategy="c9", overrides={"options_SPY.json": None})
        self.assertEqual(sum("data problem" in t for t in r1.texts), 1)
        r2 = self.w.run(f"{self.d0} 16:26", strategy="c9", trigger="manual", overrides={"options_SPY.json": None})
        self.assertEqual(sum("data problem" in t for t in r2.texts), 0)
        d2 = self.w.run(f"{self.d1} 16:20", strategy="c9", overrides={"options_SPY.json": None})
        self.assertEqual(sum("data problem" in t for t in d2.texts), 1)                   # a new session: a new notice


class KillSwitchFlow(Flow):
    def test_a_weak_trailing_edge_flags_c9_once_and_logging_carries_on(self):
        w = self.world(n_sessions=45)
        sessions = w.sessions("2026-10-26", "2026-12-18")
        # The simulated condors earn about $90 a night; with the backtest edge set to $500 the floor is 25% x 500 = $125.
        with mock.patch.dict(cfg.BACKTEST["C9"], {"edge_base": 500.0}):
            runs = [w.run(f"{t} 16:20", strategy="c9") for t in sessions]
        kills = [(r.when, x) for r in runs for x in r.texts if "| FLAGGED:" in x]            # the alert, not the "[FLAGGED]" tag on later headlines
        self.assertEqual(len(kills), 1, kills)
        when, text = kills[0]
        self.assertIn("C9 CONDOR_1D | FLAGGED: edge", text)
        flagged_run = next(r for r in runs if r.when == when)
        self.assertEqual(flagged_run.state["strategies"]["C9"]["closed_n"], 20)           # the first run on which it could be judged
        final = runs[-1].state["strategies"]["C9"]
        self.assertEqual(final["status"], "flagged")
        self.assertGreater(final["closed_n"], 20)                                         # and it kept trading after the flag
        self.assertTrue(any("[FLAGGED]" in x for r in runs for x in r.texts))
        self.assertEqual(list(final["kill_alerts"]), ["edge"])

    def test_wide_markets_raise_the_quality_flag_after_five_bad_signals(self):
        w = self.world(n_sessions=45)
        w.set_floor(0.02)                                                                 # the shorts stay tight, the 1% wings go 60-100% wide
        sessions = w.sessions("2026-10-26", "2026-12-18")
        runs = [w.run(f"{t} 16:20", strategy="c9") for t in sessions]
        kills = [x for r in runs for x in r.texts if "| FLAGGED:" in x]
        self.assertEqual(len(kills), 1, kills)
        self.assertIn("FLAGGED: quality", kills[0])
        q = runs[-1].state["strategies"]["C9"]["quality"]
        self.assertGreater(sum(1 for x in q if x["flag"]), 4)
        self.assertEqual(runs[-1].state["strategies"]["C9"]["status"], "flagged")


class Replays(Flow):
    def test_a_fixture_directory_replay_gives_the_same_result_as_the_live_path(self):
        import shadow_job
        w = self.world(n_sessions=40)
        d0 = first_open_pair(w)
        w.seed_root(nyse.prev_td(d0))
        fx = self.tmp()
        names = w.write_fixture_dir(fx, f"{d0} 16:20")
        self.assertIn("options_SPY.json", names)
        root2 = self.tmp()
        import shutil
        shutil.copytree(w.path("data"), os.path.join(root2, "data"))
        snd = FakeSender()
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = shadow_job.main(["--root", root2, "--fixture-dir", fx, "--now", f"{d0}T16:20", "--strategy", "c9", "--trigger", "replay"],
                                   env={"SHADOW_DISCORD_WEBHOOK_URL": WEBHOOK}, sender=snd)
        self.assertEqual(code, 0)
        replay = state_mod.load(os.path.join(root2, cfg.STATE_FILE))["strategies"]["C9"]["open_positions"]
        live = w.run(f"{d0} 16:20", strategy="c9").state["strategies"]["C9"]["open_positions"]
        self.assertEqual(len(replay), 1)
        strip = lambda p: {k: v for k, v in p.items() if k not in ("opened_at", "quote_file_ts_utc")}      # noqa: E731
        self.assertEqual(strip(replay[0]), strip(live[0]))
        self.assertTrue(any("C9 CONDOR_1D" in t for t in snd.texts))

    def test_a_replay_with_a_missing_fixture_behaves_like_a_down_source(self):
        import shadow_job
        import contextlib
        import io
        w = self.world(n_sessions=40)
        d0 = first_open_pair(w)
        w.seed_root(nyse.prev_td(d0))
        fx = self.tmp()
        w.write_fixture_dir(fx, f"{d0} 16:20")
        os.remove(os.path.join(fx, "options_SPY.json"))
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = shadow_job.main(["--root", w.root, "--fixture-dir", fx, "--now", f"{d0}T16:20", "--strategy", "c9", "--dry-run"],
                                   env={}, sender=FakeSender())
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
