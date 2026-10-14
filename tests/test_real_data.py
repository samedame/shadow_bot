"""The real closing feed of Tuesday 2026-10-13, replayed.

`tests/fixtures/real_20261013/` holds what the three Cboe option files (SPY, GLD, TLT), the four index quotes, the index history CSVs,
the Treasury bill CSVs and the Nasdaq closes looked like when the job was first run against the live sources at 16:20 ET on that day
(its README says what was kept). Every other test in this suite runs on synthetic data; this one checks that the parser, the rules and
the messages hold up on the feeds as they really are: a feed 15 minutes behind its own timestamp, a closing print that differs from the
last price, zero-bid wings, and a Yahoo and a FRED that were refusing connections at the time.

The golden numbers below are the tickets the job produced that afternoon. The short strikes were also re-derived from the raw
contract list without using any of the bot's code (`Oracle`), and the C9 and C6 prices were read off the raw bid/ask by hand.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import gzip
import io
import json
import os
import shutil
import unittest
from unittest import mock

import chain as chain_mod
import config as cfg
import messages as M
import nyse_calendar as nyse
import rules_c3
import rules_c6
import rules_c9
import shadow_job
import state as state_mod
from tests.helpers import FIXTURES, UTC, WEBHOOK, Base, D, FakeSender

FIX = os.path.join(FIXTURES, "real_20261013")
SESSION = D("2026-10-13")
# A second after the SPY and GLD files were generated (20:19:32 UTC), thirty seconds before the real run's start.
NOW_UTC = dt.datetime(2026, 10, 13, 20, 20, 1, tzinfo=UTC)
NOW_ET = "2026-10-13T16:20:01"

# (cp, strike, expiry, qty) as in the rules' tickets; credits are dollars for one contract (a debit is negative), keyed by the fraction
# of the quoted spread given up (0 = mid, 0.25 = base, 0.5 = natural, 0.75 = x3).
GOLDEN = {
    "C9": dict(legs=[(+1, 783.0, D("2026-10-14"), -1), (+1, 791.0, D("2026-10-14"), +1),
                     (-1, 773.0, D("2026-10-14"), -1), (-1, 765.0, D("2026-10-14"), +1)],
               cf={0.0: 83.0, 0.25: 82.0, 0.5: 81.0, 0.75: 80.0}, units=4, max_loss=718.2),
    "C6_GLD": dict(legs=[(-1, 368.0, D("2026-11-13"), -1), (-1, 360.0, D("2026-11-13"), +1)],
                   cf={0.0: 152.0, 0.25: 145.5, 0.5: 139.0, 0.75: 132.5}, units=2, max_loss=654.6),
    "C6_TLT": dict(legs=[(-1, 76.5, D("2026-11-13"), -1), (-1, 74.5, D("2026-11-13"), +1)],
                   cf={0.0: 31.5, 0.25: 30.75, 0.5: 30.0, 0.75: 29.25}, units=2, max_loss=169.35),
    "C3": dict(legs=[(-1, 739.0, D("2026-12-18"), +1), (-1, 660.0, D("2026-12-18"), -1)],
               cf={0.0: -480.0, 0.25: -481.0, 0.5: -482.0, 0.75: -483.0}, units=2, max_loss=481.1),
}
CLOSE = {"SPY": 778.01, "GLD": 381.65, "TLT": 78.37}                     # the official closes, as the feed's `close` field had them
LAST_TRADE = {"SPY": "2026-10-13T16:00:00-04:00", "GLD": "2026-10-13T15:59:59-04:00", "TLT": "2026-10-13T15:59:59-04:00"}


def full_text(payload: dict) -> str:
    """What a reader sees: the content line plus every embed's description."""
    return "\n".join([payload.get("content") or ""] + [e.get("description", "") for e in payload.get("embeds") or []])


def load_raw(sym: str) -> dict:
    with gzip.open(os.path.join(FIX, f"options_{sym}.json.gz"), "rt") as f:
        return json.load(f)


def load_chain(sym: str) -> chain_mod.Chain:
    return chain_mod.from_cboe(load_raw(sym), sym, SESSION, now_utc=NOW_UTC)


def stage_fixture(dest: str) -> None:
    """What `shadow_job.py --fixture-dir` reads: the recorded bodies under the names data_feed.FixtureHttp looks for."""
    for fn in os.listdir(FIX):
        src = os.path.join(FIX, fn)
        if os.path.isdir(src) or fn == "README.md":
            continue
        if fn.endswith(".gz"):
            with gzip.open(src, "rb") as f, open(os.path.join(dest, fn[:-3]), "wb") as g:
                g.write(f.read())
        else:
            shutil.copy(src, dest)


class Oracle:
    """Short-strike pick from the raw contract list, with none of the bot's code: among the contracts of one expiry with a quote
    (ask above zero, bid and delta present), the strike whose |delta| is closest to the target."""

    @staticmethod
    def closest_delta_strike(raw: dict, yymmdd: str, cp: str, target: float) -> float:
        best = None
        for o in raw["data"]["options"]:
            name = o["option"]
            if name[3:9] != yymmdd or name[9] != cp:
                continue
            if o["bid"] is None or o["delta"] is None or not o["ask"] or o["ask"] <= 0:
                continue
            miss = abs(abs(o["delta"]) - target)
            if best is None or miss < best[0]:
                best = (miss, int(name[10:]) / 1000)
        return best[1]

    @staticmethod
    def nearest_listed_strike(raw: dict, yymmdd: str, cp: str, target: float) -> float:
        strikes = {int(o["option"][10:]) / 1000 for o in raw["data"]["options"] if o["option"][3:9] == yymmdd and o["option"][9] == cp}
        return min(strikes, key=lambda k: (abs(k - target), k))

    @staticmethod
    def quote(raw: dict, occ: str) -> tuple:
        for o in raw["data"]["options"]:
            if o["option"] == occ:
                return o["bid"], o["ask"]
        raise KeyError(occ)


class TheFilesParse(unittest.TestCase):
    def test_each_file_parses_with_the_production_parser_and_is_a_closing_snapshot(self):
        for sym in ("SPY", "GLD", "TLT"):
            with self.subTest(sym=sym):
                ch = load_chain(sym)
                self.assertEqual(ch.spot, CLOSE[sym])
                self.assertEqual(ch.meta["spot_src"], "close")                  # the official close, not the last price
                self.assertEqual(ch.meta["und_last_trade_et"], LAST_TRADE[sym])
                self.assertIsNone(chain_mod.snapshot_stale(ch.meta, SESSION))
                self.assertLess(ch.meta["file_age_min"], 1.0)
                self.assertEqual(ch.meta["n_crossed_raw"], 0)
                self.assertGreater(ch.n_rows, 50)

    def test_the_closing_print_differs_from_the_last_price_as_the_job_expects(self):
        # SPY: last 777.93 vs official close 778.01 when the file was generated; the parser must prefer the close once its print is in.
        raw = load_raw("SPY")["data"]
        self.assertNotEqual(raw["current_price"], raw["close"])
        self.assertEqual(raw["close"], 778.01)
        self.assertEqual(load_chain("SPY").spot, 778.01)

    def test_a_file_one_minute_before_the_closing_print_would_have_been_refused(self):
        # The same SPY file with the underlying's last trade moved back to 15:58 is a pre-close snapshot.
        raw = load_raw("SPY")
        raw["data"]["last_trade_time"] = "2026-10-13T15:58:00"
        ch = chain_mod.from_cboe(raw, "SPY", SESSION, now_utc=NOW_UTC)
        self.assertEqual(chain_mod.snapshot_stale(ch.meta, SESSION), "pre_close_snapshot_15:58")

    def test_the_labels_name_both_clocks(self):
        # Cboe's file timestamp is a generation time; the data inside is 15 minutes older, so a 16:19:32 ET file holds 16:04 quotes.
        for sym, hhmm in (("SPY", "16:04"), ("GLD", "16:04"), ("TLT", "16:04")):
            with self.subTest(sym=sym):
                meta = load_chain(sym).meta
                self.assertEqual(M.option_quote_hm(meta), hhmm)
                note = M.quote_note(meta, None)
                self.assertTrue(note.startswith(f"underlying last trade {LAST_TRADE[sym][11:16]}, option quotes ~{hhmm}, file "), note)
                self.assertTrue(note.endswith(" min old, stale: no"), note)

    def test_spy_option_quotes_are_newer_than_the_close_but_gld_and_tlt_trades_stop_at_it(self):
        spy, gld, tlt = (load_chain(s).meta for s in ("SPY", "GLD", "TLT"))
        self.assertGreater(spy["newest_atm_trade_et"], "2026-10-13T16:01")      # SPY options trade to 16:15 ET
        for meta in (gld, tlt):                                                 # GLD and TLT options stop at 16:00
            self.assertLess(meta["newest_atm_trade_et"], "2026-10-13T16:00:00")
            self.assertGreater(meta["newest_atm_trade_et"], "2026-10-13T15:55")


class TheTickets(unittest.TestCase):
    def check(self, ticket: dict, golden: dict) -> None:
        self.assertEqual([tuple(l) for l in ticket["legs"]], golden["legs"])
        self.assertEqual(ticket["pkg"]["units"], golden["units"])
        for k, v in golden["cf"].items():
            self.assertAlmostEqual(ticket["pkg"]["cf"][k], v, places=6, msg=f"cash flow at k={k}")
        self.assertAlmostEqual(ticket["max_loss"], golden["max_loss"], places=6)

    def test_c9_condor(self):
        raw, ch = load_raw("SPY"), load_chain("SPY")
        res = rules_c9.evaluate(ch, SESSION, D("2026-10-14"), True, alt_ctx={"r": 0.0409})
        self.assertIsNone(res["skip"])
        t = res["ticket"]
        self.check(t, GOLDEN["C9"])
        self.assertEqual(t["strikes"], {"k_call": 783.0, "k_put": 773.0, "k_call_wing": 791.0, "k_put_wing": 765.0})
        # shorts: re-derived from the raw contracts. Wings: 1% of 778.01 = 7.78 beyond the short, nearest listed strike.
        self.assertEqual(Oracle.closest_delta_strike(raw, "261014", "C", 0.16), 783.0)
        self.assertEqual(Oracle.closest_delta_strike(raw, "261014", "P", 0.16), 773.0)
        self.assertEqual(Oracle.nearest_listed_strike(raw, "261014", "C", 783.0 + 0.01 * 778.01), 791.0)
        self.assertEqual(Oracle.nearest_listed_strike(raw, "261014", "P", 773.0 - 0.01 * 778.01), 765.0)
        # prices: by hand from the raw bid/ask. Mid credit = 0.385 - 0.015 + 0.505 - 0.045 = 0.83 per share.
        quotes = {occ: Oracle.quote(raw, occ) for occ in ("SPY261014C00783000", "SPY261014C00791000", "SPY261014P00773000", "SPY261014P00765000")}
        self.assertEqual(quotes["SPY261014C00783000"], (0.38, 0.39))
        self.assertEqual(quotes["SPY261014C00791000"], (0.01, 0.02))
        self.assertEqual(quotes["SPY261014P00773000"], (0.5, 0.51))
        self.assertEqual(quotes["SPY261014P00765000"], (0.04, 0.05))
        mid = sum(-q * (b + a) / 2 for q, (b, a) in zip((-1, +1, -1, +1), quotes.values()))
        self.assertAlmostEqual(mid * 100, 83.0, places=6)
        # the feed's deltas and the Black-Scholes cross-check agree on the picks
        self.assertFalse(res["detail"].get("alt_differs", False))

    def test_c6_gld_and_tlt(self):
        for sym, key, shorts, longs in (("GLD", "C6_GLD", "GLD261113P00368000", "GLD261113P00360000"),
                                        ("TLT", "C6_TLT", "TLT261113P00076500", "TLT261113P00074500")):
            with self.subTest(sym=sym):
                raw, ch = load_raw(sym), load_chain(sym)
                res = rules_c6.real_entry(ch, SESSION, ch.spot)
                self.assertIsNone(res["skip"])
                self.check(res["ticket"], GOLDEN[key])
                self.assertEqual(res["ticket"]["expiry"], D("2026-11-13"))                  # 31 DTE: the listed expiry nearest 30
                (bs_, as_), (bl, al) = Oracle.quote(raw, shorts), Oracle.quote(raw, longs)
                self.assertAlmostEqual(((bs_ + as_) / 2 - (bl + al) / 2) * 100, GOLDEN[key]["cf"][0.0], places=6)
                short_delta = next(o["delta"] for o in raw["data"]["options"] if o["option"] == shorts)
                self.assertLess(abs(abs(short_delta) - 0.25), 0.03)

    def test_c3_candidate(self):
        ch = load_chain("SPY")
        res = rules_c3.build_ticket(ch, SESSION)
        self.assertIsNone(res["skip"])
        self.check(res["ticket"], GOLDEN["C3"])
        self.assertEqual((res["ticket"]["expiry"] - SESSION).days, 66)       # the only listed expiry between 50 and 75 DTE
        # 0.95 x 778.01 = 739.11 -> 739; 0.85 x 778.01 = 661.31 -> 660 (the grid is 5 wide there)
        self.assertEqual(res["ticket"]["strikes"], {"k_long": 739.0, "k_short": 660.0})


class TheWholeJob(Base):
    """shadow_job.main on the recorded day: every network source served from the saved files, the clock virtual."""

    def run_job(self, root: str, fx: str, snd: FakeSender) -> int:
        env = {"SHADOW_DISCORD_WEBHOOK_URL": WEBHOOK, "RUN_GUARD_STATUS": "on_time"}
        # 2026-10-13 is a Tuesday in the middle of the month: make it count as the first trading day of its week and month so the
        # C6 and C3 entry paths run too.
        with mock.patch.object(nyse, "is_first_td_of_week", return_value=True), \
                mock.patch.object(nyse, "is_first_td_of_month", return_value=True), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return shadow_job.main(["--root", root, "--fixture-dir", fx, "--now", NOW_ET, "--trigger", "replay"], env=env, sender=snd)

    def setUp(self):
        self.root, self.fx = self.tmp(), self.tmp()
        shutil.copytree(os.path.join(FIX, "data"), os.path.join(self.root, "data"))
        stage_fixture(self.fx)

    def test_the_day_end_to_end_and_a_repeat_run_changes_nothing(self):
        snd = FakeSender()
        self.assertEqual(self.run_job(self.root, self.fx, snd), 0)
        st = state_mod.load(os.path.join(self.root, cfg.STATE_FILE))
        # positions
        c9 = st["strategies"]["C9"]["open_positions"]
        c6 = st["strategies"]["C6"]["open_positions"]
        self.assertEqual([p["id"] for p in c9], ["C9-2026-10-13"])
        self.assertEqual(sorted(p["id"] for p in c6), ["C6-GLD-2026-10-13", "C6-TLT-2026-10-13"])
        self.assertEqual([(l["cp"], l["strike"], l["qty"]) for l in c9[0]["legs"]],
                         [(+1, 783.0, -1), (+1, 791.0, +1), (-1, 773.0, -1), (-1, 765.0, +1)])
        self.assertEqual(c9[0]["entry_cf"], {"mid": 83.0, "base": 82.0, "nat": 81.0, "x3": 80.0})
        self.assertEqual(sorted(r["id"] for r in st["strategies"]["C6"]["model"]["open"]), ["C6M-GLD-2026-10-13", "C6M-TLT-2026-10-13"])
        cand = st["strategies"]["C3"]["candidate"]
        self.assertEqual(cand["entry_date"], "2026-10-13")
        self.assertEqual([(l["strike"], l["qty"]) for l in cand["position"]["legs"]], [(739.0, +1), (660.0, -1)])
        self.assertTrue(cand["prov"]["passes"])                                   # VIX 15.27 <= its 25th percentile 15.805; ratio 0.85 < 0.90
        self.assertEqual(st["strategies"]["C3"]["open_positions"], [])             # confirmed only once the published closes arrive
        # messages: online + one condor + GLD + TLT, each starting with the banner
        self.assertEqual(len(snd.posts), 4, snd.texts)
        for t in snd.texts:
            self.assertTrue(t.startswith(cfg.BANNER), t)
        texts = [full_text(p) for _u, p in snd.posts]
        def entry(head: str) -> str:
            return next(t for t in texts if t.split("\n")[0].startswith(f"{cfg.BANNER} | {head} |"))

        c9_text = entry("C9 CONDOR_1D")
        self.assertIn("(underlying last trade 16:00, option quotes ~16:04, file", c9_text.split("\n")[0])
        self.assertIn("Would-be ticket : SELL 1 783C / BUY 1 791C / SELL 1 773P / BUY 1 765P   exp 2026-10-14", c9_text)
        self.assertIn("Credit          : mid $0.83 | base fill (25%) $0.82 | natural $0.81   Max loss $718", c9_text)
        self.assertIn("Underlying      : SPY 778.01 (close) | shorts 783C delta +0.16, 773P delta -0.17 (feed)", c9_text)
        gld = entry("C6 NONEQ_VRP GLD")
        self.assertIn("SELL 1 368P / BUY 1 360P   exp 2026-11-13 (31 DTE) | GLD 381.65, width $8", gld)
        self.assertIn("Credit (real)   : mid $1.52 | base fill (25%) $1.46 | natural $1.39   Max loss $655", gld)
        self.assertIn("Credit (model)  : base $1.65 per share, provisional (GVZ quote 22.09; the published close replaces it) | real / model 0.88", gld)
        tlt = entry("C6 NONEQ_VRP TLT")
        self.assertIn("SELL 1 76.5P / BUY 1 74.5P   exp 2026-11-13 (31 DTE) | TLT 78.37, width $2", tlt)
        self.assertIn("Credit (real)   : mid $0.32 | base fill (25%) $0.31 | natural $0.30   Max loss $169", tlt)
        self.assertIn("Credit (model)  : base $0.24 per share, provisional (VXTLT quote 14.54; the published close replaces it) | real / model 1.27", tlt)
        # a second run of the same session: no new position, no new message
        before = json.dumps(st["strategies"]["C9"]["open_positions"], sort_keys=True)
        snd2 = FakeSender()
        self.assertEqual(self.run_job(self.root, self.fx, snd2), 0)
        st2 = state_mod.load(os.path.join(self.root, cfg.STATE_FILE))
        self.assertEqual(snd2.texts, [])
        self.assertEqual(json.dumps(st2["strategies"]["C9"]["open_positions"], sort_keys=True), before)
        self.assertEqual(len(st2["strategies"]["C6"]["open_positions"]), 2)

    def test_the_fallbacks_carried_the_day_because_yahoo_and_fred_were_down(self):
        # No Yahoo and no FRED file was recorded (HTTP 429 and a closed connection at the time), so every number below came from the
        # fallbacks: closes from the chain's own closing print (Nasdaq has no 10-13 row yet), the bill rate from the Treasury CSV.
        self.assertFalse([f for f in os.listdir(self.fx) if f.startswith(("yahoo_", "fred_"))])
        self.assertEqual(self.run_job(self.root, self.fx, FakeSender()), 0)
        with open(os.path.join(self.root, "diag_log.jsonl")) as f:
            diag = json.loads(f.read().strip().splitlines()[-1])
        self.assertEqual(diag["problems"], [])
        self.assertEqual(diag["tbill"]["src"], "treasury")
        self.assertEqual(diag["tbill"]["last"], "2026-10-13")
        self.assertEqual(diag["tbill"]["rate"], 0.0409)                            # 13-week bank discount 4.09 -> 0.0409
        self.assertEqual({k: v["src"] for k, v in diag["closes"].items()}, {"SPY": "nasdaq", "GLD": "nasdaq", "TLT": "nasdaq"})
        self.assertEqual({k: v["spot_src"] for k, v in diag["feed"].items()}, {"SPY": "close", "GLD": "close", "TLT": "close"})
        self.assertEqual(diag["c9"]["result"], "entered")
        self.assertTrue(diag["c9"]["gate"])
        self.assertEqual(diag["c3"]["result"], "candidate_stored")
        self.assertEqual(diag["messages"]["failed"], 0)


if __name__ == "__main__":
    unittest.main()
