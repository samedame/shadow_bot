"""A simulated market and a bot root, for the end-to-end tests (tests/test_flows.py).

The World owns deterministic price and index paths for SPY, GLD, TLT, VIX, VIX3M, GVZ and VXTLT over a stretch of real NYSE
trading days, and serves them in the exact formats of the live sources (Cboe delayed-quote chains and index quotes, Cboe history
CSVs, a Nasdaq-style close history, FRED DTB3). What a source returns depends on the VIRTUAL CLOCK of the run that asks, so a
test can say "the closing print reaches the file at 16:23" or "the index history is published at 19:00" and watch the job react:

    w = World(root)                         # paths, seeds in root/data/
    r = w.run("2026-11-02 16:20")           # shadow_job.main() end to end, Discord replaced by a recording double
    r.state, r.texts, w.log()               # what the run left behind

Nothing here touches the network. Chains are Black-Scholes with a mild skew and realistic spreads (half-spread max($0.005, 2% of
price)); the near SPY expiries carry both calls and puts, the far ones puts only (all C3 needs), to keep the files small.
"""
from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import io
import json
import math
import os
import random
import statistics
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from tests import helpers as h

import config as cfg
import data_feed
import discord_shadow
import market_data as md
import nyse_calendar as nyse
import shadow_job
import state as state_mod

ET, UTC, CT = h.ET, h.UTC, ZoneInfo("America/Chicago")
SYMS = ("SPY", "GLD", "TLT")
IDX = ("VIX", "VIX3M", "GVZ", "VXTLT")
CHAIN_VOL_INDEX = {"SPY": "VIX", "GLD": "GVZ", "TLT": "VXTLT"}


def at(d: dt.date, hhmm: str) -> dt.datetime:
    hh, mm = hhmm.split(":")
    return dt.datetime.combine(d, dt.time(int(hh), int(mm)), tzinfo=ET)


def _time(hhmm: str) -> dt.time:
    hh, mm = hhmm.split(":")
    return dt.time(int(hh), int(mm))


@dataclasses.dataclass
class Run:
    when: str
    code: int
    stdout: str
    n_posts: int
    texts: list                   # the `content` lines posted during this run
    stderr: str                   # what the run printed to stderr (retry notes, discord warnings)
    state: dict                   # shadow_state.json after the run
    diag: dict                    # the diagnostics line this run wrote ({} if none)


class World:
    def __init__(self, root: str, first_session: str = "2026-11-02", n_sessions: int = 45, hist_sessions: int = 330,
                 seed: int = 7, seeded: bool = True):
        self.root = root
        self.first = h.D(first_session)
        if not nyse.is_trading_day(self.first):
            raise ValueError(f"{first_session} is not a trading day")
        hist = [nyse.td_offset(self.first, -k) for k in range(hist_sessions, 0, -1)]
        self.days = hist + h.trading_days_from(self.first, n_sessions)
        self.sender = h.FakeSender()
        self.calls: list = []                              # (virtual time, fixture name) for every request the job made
        # --- when the sources catch up (all ET, same day) and how old the chain file is
        self.ready_at: dict = {}                           # symbol -> "HH:MM" the closing print is in the file (default close + 3 min)
        self.file_lag_min = 4.0
        self.floor = 0.005                                 # half-spread floor of the chains; 0.01 makes the SPY wings 40-70% wide
        self.publish_at = "19:00"                          # Cboe index history for the day appears
        self.closes_at = "20:00"                           # the Nasdaq-style close history for the day appears
        self.prov_noise = 0.10                             # delayed index quote minus the later published close
        self._rows: dict = {}
        self._build_paths(seed)
        if seeded:
            self.seed_root(nyse.prev_td(self.first))

    # ------------------------------------------------------------------ paths
    def _build_paths(self, seed: int) -> None:
        rng = random.Random(seed)
        self.px = {s: {} for s in SYMS}
        self.idx = {n: {} for n in IDX}
        spy, gld, tlt = 600.0, 300.0, 90.0
        for i, d in enumerate(self.days):
            # SPY: loud and quiet blocks of ~35 sessions, so the realised-vol gate is open on some nights and shut on others.
            # A gentle pull toward the starting level keeps the three prices (and so the strikes) in a familiar range.
            amp = 0.012 if (i // 35) % 2 == 0 else 0.004
            spy *= 1.0 + (rng.random() - 0.5) * 2.0 * amp + 0.04 * (600.0 / spy - 1.0)
            gld *= 1.0 + (rng.random() - 0.5) * 2.0 * 0.007 + 0.04 * (300.0 / gld - 1.0)
            tlt *= 1.0 + (rng.random() - 0.5) * 2.0 * 0.005 + 0.04 * (90.0 / tlt - 1.0)
            self.px["SPY"][d], self.px["GLD"][d], self.px["TLT"][d] = round(spy, 2), round(gld, 2), round(tlt, 2)
            vix = 15.0 + 6.0 * rng.random()
            self.idx["VIX"][d] = round(vix, 2)
            self.idx["VIX3M"][d] = round(vix * 1.15, 2)
            self.idx["GVZ"][d] = round(17.0 + 4.0 * rng.random(), 2)
            self.idx["VXTLT"][d] = round(13.0 + 3.0 * rng.random(), 2)
        self.rate_pct = {d: 3.60 for d in self.days}

    def _d(self, d) -> dt.date:
        return h.D(d) if isinstance(d, str) else d

    def set_close(self, sym: str, d, value: float) -> None:
        self.px[sym][self._d(d)] = round(float(value), 2)
        self._rows.clear()

    def set_index(self, name: str, d, value: float) -> None:
        self.idx[name][self._d(d)] = round(float(value), 2)

    def set_floor(self, floor: float) -> None:
        self.floor = floor
        self._rows.clear()

    def sessions(self, a, b) -> list:
        a, b = self._d(a), self._d(b)
        return [d for d in self.days if a <= d <= b]

    # ------------------------------------------------------------------ seeds
    def seed_root(self, through: dt.date) -> None:
        """What the repository's data/ holds before the first run: closes and index history through `through`."""
        m = md.Market()
        for s in SYMS:
            m.closes[s] = md.Series([(d, v) for d, v in self.px[s].items() if d <= through])
            for d in m.closes[s].dates:
                m.close_src[(s, d)] = "seed"
        for n in IDX:
            keep = through - dt.timedelta(days=430 if n == "VIX" else 150)
            m.indices[n] = md.Series([(d, v) for d, v in self.idx[n].items() if keep <= d <= through])
        m.tbill = md.Series([(d, v / 100.0) for d, v in self.rate_pct.items() if through - dt.timedelta(days=150) <= d <= through])
        md.save_market(m, self.root)

    # ------------------------------------------------------------------ the market at a moment
    def close_time(self, d: dt.date) -> dt.time:
        return dt.time(13, 0) if nyse.is_early_close(d) else dt.time(16, 0)

    def last_session(self, now: dt.datetime, after: str) -> dt.date:
        """Latest session whose data is out at `now`, for a source that publishes a session's data at `after` ET that day."""
        d = now.date()
        s = nyse.last_session_on_or_before(d)
        if s == d and now.time() < _time(after):
            s = nyse.prev_td(d)
        return s

    def listed_expiries(self, sym: str, d: dt.date) -> list:
        out = set()
        if sym == "SPY":
            t = d
            for _ in range(3):                              # daily expiries: the next three sessions
                t = nyse.next_td(t)
                out.add(t)
        for k in range(1, 91 if sym == "SPY" else 61):      # weekly Fridays (the Thursday before a Friday holiday)
            day = d + dt.timedelta(days=k)
            if day.weekday() != 4:
                continue
            ex = day if nyse.is_trading_day(day) else nyse.prev_td(day)
            if sym == "SPY" and (ex - d).days > 80:
                continue                                    # SPY: nothing beyond what C3 reads
            out.add(ex)
        return sorted(out)

    def spot(self, sym: str, d: dt.date, kind: str) -> float:
        base = self.px[sym][d]
        return round(base * {"final": 1.0, "pre_close": 1.0015, "intraday": 0.997}[kind], 2)

    def rows(self, sym: str, d: dt.date, kind: str = "final") -> list:
        """Chain rows (expiry, cp, strike, bid, ask, delta, iv) for `sym` on session `d`."""
        key = (sym, d, kind)
        if key not in self._rows:
            spot = self.spot(sym, d, kind)
            vol = self.idx[CHAIN_VOL_INDEX[sym]][d] / 100.0
            rows = []
            for ex in self.listed_expiries(sym, d):
                dte = (ex - d).days
                near = sym == "SPY" and dte <= 7
                pct = 0.09 if near else 0.22
                strikes = [float(k) for k in range(math.floor(spot * (1 - pct)), math.ceil(spot * (1 + pct)) + 1)]
                rs = h.bs_rows(spot, d, [ex], strikes, vol=vol, r=0.04, rel=0.02, floor=self.floor,
                               vol_slope=0.25 if sym == "SPY" else 0.10)
                rows += rs if (near or sym != "SPY") else [r for r in rs if r[1] < 0]
            self._rows[key] = rows
        return self._rows[key]

    def chain_kind(self, sym: str, now: dt.datetime) -> tuple:
        """-> (kind, underlying last-trade time) of the chain file as it stands at `now`."""
        d = now.date()
        close_dt = at(d, self.close_time(d).strftime("%H:%M"))
        ready = at(d, self.ready_at.get(sym) or (close_dt + dt.timedelta(minutes=3)).strftime("%H:%M"))
        if now < close_dt - dt.timedelta(minutes=15):
            return "intraday", now - dt.timedelta(minutes=15)
        if now < ready:
            return "pre_close", close_dt - dt.timedelta(minutes=10)
        return "final", close_dt

    # ------------------------------------------------------------------ rendering the sources
    def options_obj(self, sym: str, now: dt.datetime) -> dict:
        d = now.date()
        kind, last = self.chain_kind(sym, now)
        spot = self.spot(sym, d, kind)
        return h.cboe_options_obj(sym, self.rows(sym, d, kind), spot, last_trade=last,
                                  file_ts_utc=now - dt.timedelta(minutes=self.file_lag_min))

    def quote_obj(self, name: str, now: dt.datetime) -> dict:
        d = now.date()
        stops = at(d, "16:15" if name in ("VIX", "VIX3M") else "16:00")
        last = min(now - dt.timedelta(minutes=15), stops)
        value = round(self.idx[name][d] + self.prov_noise, 2)
        offset = name in ("GVZ", "VXTLT")                   # these two carry a Central-time offset in the live feed
        return h.cboe_quote_obj(name, value, last.astimezone(CT) if offset else last,
                                now - dt.timedelta(minutes=self.file_lag_min), offset_style=offset)

    def history_text(self, name: str, now: dt.datetime) -> str:
        last = self.last_session(now, self.publish_at)
        return h.history_csv([(d, v) for d, v in sorted(self.idx[name].items()) if d <= last], name)

    def nasdaq_text(self, sym: str, now: dt.datetime) -> str:
        last = self.last_session(now, self.closes_at)
        return h.nasdaq_json([(d, v) for d, v in self.px[sym].items() if d <= last])

    def fred_text(self, now: dt.datetime) -> str:
        last = self.last_session(now, "09:00")
        return "observation_date,DTB3\n" + "".join(f"{d.isoformat()},{v:.2f}\n" for d, v in sorted(self.rate_pct.items()) if d <= last)

    def render(self, name: str, now: dt.datetime) -> Optional[bytes]:
        kind, _, rest = name.partition("_")
        key = rest.rsplit(".", 1)[0]
        if kind == "options" and key in SYMS:
            return json.dumps(self.options_obj(key, now)).encode()
        if kind == "quote" and key in IDX:
            return json.dumps(self.quote_obj(key, now)).encode()
        if kind == "history" and key in IDX:
            return self.history_text(key, now).encode()
        if kind == "nasdaq" and key in SYMS:
            return self.nasdaq_text(key, now).encode()
        if kind == "fred" and key == "DTB3":
            return self.fred_text(now).encode()
        return None                                         # yahoo, stooq, treasury: "down", so the fallbacks are exercised

    def write_fixture_dir(self, path: str, when: str) -> list:
        """The saved files a `--fixture-dir` replay of `when` would read."""
        os.makedirs(path, exist_ok=True)
        now = h.et(when)
        names = ([f"options_{s}.json" for s in SYMS] + [f"nasdaq_{s}.json" for s in SYMS] + [f"quote_{n}.json" for n in IDX]
                 + [f"history_{n}.csv" for n in IDX] + ["fred_DTB3.csv"])
        for n in names:
            with open(os.path.join(path, n), "wb") as f:
                f.write(self.render(n, now))
        return names

    def http(self, clock, overrides: dict) -> Callable:
        """The `http` callable of a Feed: renders from the world at the clock's current time. `overrides` maps a fixture name to
        bytes / str / dict, or to a function of the time returning one of those or None (None = that source is down)."""
        namer = data_feed.FixtureHttp("/nonexistent")

        def get(url: str, **_kw) -> bytes:
            name = namer.name_for(url)
            now = clock.now()
            self.calls.append((now, name or url))
            if name in overrides:
                v = overrides[name]
                v = v(now) if callable(v) else v
            else:
                v = self.render(name, now) if name else None
            if v is None:
                raise data_feed.FeedError(f"fixture missing for {url} ({name})")
            if isinstance(v, dict):
                v = json.dumps(v)
            return v.encode() if isinstance(v, str) else v
        return get

    # ------------------------------------------------------------------ running the job
    def run(self, when: str, *, strategy: str = "all", trigger: str = "schedule", guard: str = "", late_min=None,
            dry_run: bool = False, overrides: Optional[dict] = None, webhook: Optional[str] = h.WEBHOOK, sender=None,
            extra_args: tuple = ()) -> Run:
        clock = shadow_job.Clock(h.et(when), virtual=True)
        feed = data_feed.Feed(http=self.http(clock, overrides or {}), sleep=lambda s: None)
        env = {"RUN_TRIGGER": trigger}
        if guard:
            env["RUN_GUARD_STATUS"] = guard
        if late_min is not None:
            env["RUN_GUARD_LATE_MIN"] = str(late_min)
        if webhook:
            env[discord_shadow.ENV_NAME] = webhook
        snd = sender or self.sender
        before = len(snd.posts)
        argv = ["--root", self.root, "--trigger", trigger, "--strategy", strategy] + (["--dry-run"] if dry_run else []) + list(extra_args)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = shadow_job.main(argv, feed=feed, env=env, sender=snd, clock=clock)
        return Run(when, code, out.getvalue(), len(snd.posts) - before, [p["content"] for _u, p in snd.posts[before:]],
                   err.getvalue(), self.state(), (self.diag_lines() or [{}])[-1])

    # ------------------------------------------------------------------ what the job left behind
    def path(self, *p) -> str:
        return os.path.join(self.root, *p)

    def state(self) -> dict:
        return state_mod.load(self.path(cfg.STATE_FILE))

    def log(self) -> list:
        return state_mod.read_log(self.path(cfg.LOG_FILE))

    def diag_lines(self) -> list:
        p = self.path(cfg.DIAG_FILE)
        if not os.path.exists(p):
            return []
        with open(p) as f:
            return [json.loads(x) for x in f if x.strip()]

    def market(self) -> md.Market:
        return md.load_market(self.root)

    # ------------------------------------------------------------------ independent oracles
    def spy_gate(self, d: dt.date) -> bool:
        """The C9 gate, restated with the standard library's statistics module (an implementation independent of market_data)."""
        p = nyse.prev_td(d)

        def rv(x: dt.date) -> float:
            c = [self.px["SPY"][nyse.td_offset(x, k)] for k in range(-20, 1)]
            return statistics.stdev([c[i + 1] / c[i] - 1.0 for i in range(20)])

        vals = [rv(nyse.td_offset(p, -k)) for k in range(20)]
        return rv(p) < statistics.median(vals)
