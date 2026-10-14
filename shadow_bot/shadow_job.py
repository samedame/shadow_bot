#!/usr/bin/env python3
"""shadow_job.py - the one daily job behind the three SHADOW bots (C9 CONDOR_1D, C6 NONEQ_VRP, C3 SPY_HEDGE).

SHADOW - NOT A TRADE. Nothing here places, changes or cancels an order; it logs would-be trades at mid / base / natural fills and
posts labelled messages to a separate Discord channel (webhook secret SHADOW_DISCORD_WEBHOOK_URL, never the live one).

Run it through run_guard.py (lateness + duplicate guard), see .github/workflows/shadow.yml:

    python run_guard.py --job shadow-daily --target 16:20 --usable-until 16:30 --on-time-min 10 --on-too-late run_flagged -- \\
        python shadow_job.py --trigger "$RUN_TRIGGER"

Order of work in a real run (phase "close": a trading day, at or after 16:05 ET):
    1 data      option chains (SPY, GLD, TLT) -> closing-snapshot check; raw closes; published + provisional index values; T-bill
    2 upkeep    C9 settlement at the official close; C3 exits and candidate confirmation; C6 real exits and the model path
    3 entries   C9 nightly condor; C6 weekly pair (real + model); C3 monthly candidate
    4 review    kill-switch flags, one data notice, weekly summary, first-run hello
    5 persist   log rows, state, market caches, diagnostics, export; THEN post the queued messages (state saved after each post)

Phase "intraday" (before the snapshot time) only records diagnostics; "closed_day" does nothing. Every state change is keyed to
dates, official closes and published index values, so a late or repeated run can delay a transition but never corrupt one.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
import traceback
from typing import Callable, Optional
from zoneinfo import ZoneInfo

import chain as chain_mod
import config as cfg
import data_feed
import discord_shadow
import export_state
import job_c3
import job_c6
import job_c9
import kill_switch
import market_data
import messages
import nyse_calendar as nyse
import state as state_mod
from jobctx import Ctx, compute_phase

ET = ZoneInfo("America/New_York")
UTC = dt.timezone.utc
CHAIN_SYMS = {"C9": ("SPY",), "C3": ("SPY",), "C6": ("GLD", "TLT")}
CLOSE_SYMS = {"C9": ("SPY",), "C3": ("SPY",), "C6": ("GLD", "TLT")}
INDEX_NAMES = {"C3": ("VIX", "VIX3M"), "C6": ("GVZ", "VXTLT")}


# ---------------------------------------------------------------------------------------------- args and context
def parse_args(argv=None):
    ap = argparse.ArgumentParser(prog="shadow_job.py", description="SHADOW - NOT A TRADE: daily job for the three shadow bots.")
    ap.add_argument("--trigger", default=os.environ.get("RUN_TRIGGER", "manual"))
    ap.add_argument("--strategy", default="all", help="all | c9 | c6 | c3 (comma separated)")
    ap.add_argument("--dry-run", action="store_true", help="print only: no files written, nothing posted")
    ap.add_argument("--root", default=".", help="directory holding the state files and data/ (default: current directory)")
    ap.add_argument("--now", default=None, help="override the clock, ISO-8601 (naive = New York time); for tests and replays")
    ap.add_argument("--fixture-dir", default=None, help="serve every network source from saved files in this directory")
    return ap.parse_args(argv)


def parse_only(text: str) -> set:
    parts = {p.strip().upper() for p in text.split(",") if p.strip()}
    if not parts or "ALL" in parts:
        return set(cfg.STRATEGIES)
    bad = parts - set(cfg.STRATEGIES)
    if bad:
        raise SystemExit(f"unknown strategy {sorted(bad)}; use all | c9 | c6 | c3")
    return parts


def parse_now(text: Optional[str]) -> dt.datetime:
    if not text:
        return dt.datetime.now(ET)
    t = dt.datetime.fromisoformat(text)
    return t.replace(tzinfo=ET) if t.tzinfo is None else t.astimezone(ET)


def build_ctx(args, env, feed, now_et: dt.datetime) -> Ctx:
    root = args.root
    session, phase, early, close_et = compute_phase(now_et)
    st = state_mod.load(os.path.join(root, cfg.STATE_FILE))
    mkt = market_data.load_market(root)
    rows = state_mod.read_log(os.path.join(root, cfg.LOG_FILE))
    try:
        late = float(env.get("RUN_GUARD_LATE_MIN", "")) if env.get("RUN_GUARD_LATE_MIN") else None
    except ValueError:
        late = None
    return Ctx(root=root, now_et=now_et, session=session, phase=phase, early_close=early, close_et=close_et,
               trigger=args.trigger, guard_status=env.get("RUN_GUARD_STATUS", ""), late_min=late, dry_run=args.dry_run,
               only=parse_only(args.strategy), feed=feed, st=st, mkt=mkt, log_rows=rows, now_utc=now_et.astimezone(UTC))


def needed(ctx: Ctx, table: dict) -> list:
    out: list = []
    for sid in cfg.STRATEGIES:
        if ctx.wants(sid):
            out += [x for x in table.get(sid, ()) if x not in out]
    return out


# ---------------------------------------------------------------------------------------------- 1. data
FEED_KEYS = ("spot_src", "und_last", "und_close", "und_last_trade_et", "file_ts_utc", "file_age_min", "newest_atm_trade_et",
             "n_raw", "n_clean", "n_two_sided", "n_zero_bid", "n_no_delta", "n_crossed_raw")


def usable_until(ctx: Ctx) -> dt.datetime:
    """The last moment (ET, on the session date) at which a snapshot still counts as the closing snapshot."""
    h, m = (int(x) for x in cfg.USABLE_UNTIL_ET.split(":"))
    return dt.datetime.combine(ctx.session, dt.time(h, m), tzinfo=ET)


def fetch_chains(ctx: Ctx) -> None:
    """Fetch every chain the selected strategies need.

    In the closing phase a chain that could not be read, or is not yet the closing snapshot (the closing print is not in the
    file, or the file is too old), is fetched again every WAIT_POLL_S seconds for at most WAIT_MAX_S seconds and never past
    USABLE_UNTIL_ET. What was waited for is recorded as diag["snapshot_wait"]. Outside the closing phase there is one fetch.
    """
    syms = needed(ctx, CHAIN_SYMS)
    info: dict = {}
    pending = list(syms)
    t_first = ctx.clock_now()
    deadline = min(t_first + dt.timedelta(seconds=cfg.WAIT_MAX_S), usable_until(ctx))
    polls = 0
    while pending:
        now = ctx.clock_now()
        for sym in list(pending):
            try:
                ch = ctx.feed.chain(sym, ctx.session, now.astimezone(UTC), close_et=ctx.close_et)
            except data_feed.FeedError as e:
                ctx.chain_err[sym] = str(e)
                info[sym] = {"ok": False, "error": str(e)[:300]}
                continue
            ctx.chain_err.pop(sym, None)
            why = chain_mod.snapshot_stale(ch.meta, ctx.session, True, ctx.close_et)
            ctx.chains[sym], ctx.stale[sym] = ch, why
            info[sym] = {"ok": True, "stale": why, **{k: ch.meta.get(k) for k in FEED_KEYS}}
            if not why:
                pending.remove(sym)
        if ctx.phase != "close" or not pending:
            break
        if ctx.clock_now() + dt.timedelta(seconds=cfg.WAIT_POLL_S) > deadline:
            break
        ctx.wait(cfg.WAIT_POLL_S)
        polls += 1
    ctx.now_et = ctx.clock_now()                       # the time the data was actually read (message headers show it)
    ctx.now_utc = ctx.now_et.astimezone(UTC)
    if ctx.phase == "close" and syms:
        ctx.diag["snapshot_wait"] = {"polls": polls, "waited_s": round((ctx.now_et - t_first).total_seconds(), 1),
                                     "still_stale": sorted(pending)}
    for sym in syms:
        if sym in ctx.chain_err:
            ctx.problems.append(f"{sym} option chain unavailable: {ctx.chain_err[sym][:160]}")
        elif ctx.stale.get(sym) and ctx.phase == "close":
            ctx.problems.append(f"{sym} snapshot is not the closing snapshot ({ctx.stale[sym]}); entries and exits on it wait")
    ctx.diag["feed"] = info


def closes_window(ctx: Ctx, sym: str) -> int:
    """Calendar days of closes to ask for: a long window on the first run (or when the store is nearly empty), otherwise the
    daily top-up, widened to cover any gap since the newest stored close."""
    s = ctx.mkt.closes.get(sym)
    if s is None or len(s) < cfg.CLOSES_MIN_ROWS:
        return cfg.CLOSES_BOOTSTRAP_DAYS
    return min(cfg.CLOSES_BOOTSTRAP_DAYS, max(cfg.CLOSES_FETCH_DAYS, (ctx.session - s.last_date()).days + 10))


def refresh_closes(ctx: Ctx) -> None:
    out = {}
    d = ctx.session
    for sym in needed(ctx, CLOSE_SYMS):
        rec: dict = {}
        ch = ctx.chains.get(sym)
        if ctx.phase == "close" and ch is not None and not ctx.stale.get(sym) and ch.meta.get("spot_src") == "close":
            market_data.merge_closes(ctx.mkt, sym, [(d, ch.spot)], "cboe")
            rec["cboe_close"] = ch.spot
        try:
            rows, src = ctx.feed.closes(sym, closes_window(ctx, sym), ctx.now_utc)
        except data_feed.FeedError as e:
            rec["error"] = str(e)[:200]
            if ctx.phase == "close" and ctx.mkt.close(sym, d) is None:
                ctx.problems.append(f"{sym} close for {d} unavailable from every source")
            out[sym] = rec
            continue
        # Yahoo's bar for the current day is live until the close: only keep it once the session's close is final.
        rows = [(x, v) for x, v in rows if x < d or (x == d and ctx.phase == "close")]
        rep = market_data.merge_closes(ctx.mkt, sym, rows, src)
        rec.update(src=src, added=[x.isoformat() for x in rep["added"]][-5:],
                   corrected=[(x.isoformat(), a, b) for x, a, b in rep["corrected"]][-5:],
                   mismatch=[(x.isoformat(), a, b) for x, a, b in rep["mismatch"]][-5:])
        recent = nyse.prev_td(d)                         # today's and yesterday's rows only, so one bad row is reported once or twice
        for (x, a, b) in rep["corrected"]:
            if x >= recent and a > 0 and abs(b / a - 1.0) > cfg.SPOT_VS_CLOSE_WARN:
                ctx.problems.append(f"{sym} close for {x}: stored {a:.2f} replaced by {b:.2f} from {src}")
        for (x, a, b) in rep["mismatch"]:
            if x >= recent and a > 0 and abs(b / a - 1.0) > cfg.SPOT_VS_CLOSE_WARN:
                ctx.problems.append(f"{sym} close for {x}: stored {a:.2f} KEPT, but {src} says {b:.2f}; check which is right")
        y = ctx.mkt.close(sym, d)
        if ch is not None and y:
            rec["spot_vs_close"] = round(ch.spot / y - 1.0, 6)
        out[sym] = rec
    ctx.diag["closes"] = out


def refresh_indices(ctx: Ctx) -> None:
    d = ctx.session
    out = {}
    diffs = ctx.st["notes"].setdefault("prov_vs_pub", [])
    for name in needed(ctx, INDEX_NAMES):
        info: dict = {}
        try:
            rows = ctx.feed.index_history(name)
            lo = d - dt.timedelta(days=cfg.HISTORY_KEEP_DAYS[name])
            rows = [(x, v) for x, v in rows if lo <= x <= d]
            for x, v in rows:
                pv = ctx.mkt.prov.get((name, x))
                if pv is not None:
                    diffs.append({"name": name, "date": x.isoformat(), "prov": pv, "pub": v})
            new = market_data.merge_published_index(ctx.mkt, name, rows)
            info["published_new"] = [x.isoformat() for x in new][-3:]
        except data_feed.FeedError as e:
            info["history_error"] = str(e)[:200]
        last = ctx.mkt.index_series(name).last_date()
        info["published_last"] = None if last is None else last.isoformat()
        if ctx.phase == "close":
            try:
                q = ctx.feed.index_quote(name)
                info["quote"] = q
                lt = q.get("last_trade_et")
                if lt and dt.datetime.fromisoformat(lt).date() == d:
                    ctx.mkt.prov[(name, d)] = q["value"]
                else:
                    info["quote_note"] = "quote is not from the session date; not stored as provisional"
            except data_feed.FeedError as e:
                info["quote_error"] = str(e)[:200]
        out[name] = info
    ctx.st["notes"]["prov_vs_pub"] = diffs[-200:]
    ctx.diag["indices"] = out


def refresh_tbill(ctx: Ctx) -> None:
    if not ctx.wants("C6"):
        return
    info: dict = {}
    try:
        rows, src = ctx.feed.tbill(ctx.session)
        lo = ctx.session - dt.timedelta(days=cfg.TBILL_KEEP_DAYS)
        info["src"] = src
        info["new"] = len(market_data.merge_tbill(ctx.mkt, [(x, v) for x, v in rows if x >= lo]))
    except data_feed.FeedError as e:
        info["error"] = str(e)[:200]
    last = ctx.mkt.tbill.last_date()
    info["last"] = None if last is None else last.isoformat()
    info["rate"] = ctx.mkt.rate(ctx.session)
    if ctx.phase == "close" and (last is None or (ctx.session - last).days > 14):
        ctx.problems.append(f"T-bill rate is stale (last {info['last']}); the C6 model keeps using it")
    ctx.diag["tbill"] = info


# ---------------------------------------------------------------------------------------------- review steps
def account_missed(ctx: Ctx) -> None:
    """Count the sessions between this strategy's last processed session and today as 'missed_run' skips."""
    d = ctx.session
    for sid in cfg.STRATEGIES:
        if not ctx.wants(sid):
            continue
        last = ctx.st["last_session"].get(sid)
        if not last:
            continue
        gap = [t for t in nyse.trading_days_between(dt.date.fromisoformat(last), d) if t < d]
        for t in gap:
            if sid == "C9":
                state_mod.record_skip(ctx.st, sid, t, "missed_run")
            elif sid == "C6" and nyse.is_first_td_of_week(t):
                for tk in cfg.C6["vol_index"]:
                    state_mod.record_skip(ctx.st, sid, t, "missed_run", sleeve=tk)
            elif sid == "C3" and nyse.is_first_td_of_month(t):
                state_mod.record_skip(ctx.st, sid, t, "missed_run")
        if gap:
            ctx.event(f"{sid}: {len(gap)} session(s) since {last} had no run")
            ctx.diag.setdefault("missed", {})[sid] = [t.isoformat() for t in gap]


def evaluate_kill(ctx: Ctx) -> None:
    for sid in cfg.STRATEGIES:
        if not ctx.wants(sid):
            continue
        blk = ctx.block(sid)
        prev = blk["status"]
        res = kill_switch.evaluate(sid, blk, ctx.now_iso)
        for reason, text in res["new"].items():
            ctx.event(f"{sid} FLAGGED ({reason})")
            ctx.emit(f"kill:{sid}:{reason}:{ctx.now_iso}", "kill", sid, messages.kill_alert(sid, blk, reason, text, prev))


def data_notice(ctx: Ctx) -> None:
    if not ctx.problems:
        return
    lines = [f"Session {ctx.session}: {len(ctx.problems)} problem(s)"] + [f"- {p}" for p in ctx.problems[:12]]
    if len(ctx.problems) > 12:
        lines.append(f"- ... and {len(ctx.problems) - 12} more (diag_log.jsonl)")
    lines.append("Affected strategies skip or wait; nothing was traded. This is the only notice for today.")
    ctx.emit(f"notice:data:{ctx.session}", "notice", None, messages.notice("data problem", lines))


def weekly_summary(ctx: Ctx) -> None:
    d = ctx.session
    if ctx.only != set(cfg.STRATEGIES) or not nyse.is_last_td_of_week(d):
        return
    wk = state_mod.week_key(d)
    monday = d - dt.timedelta(days=d.weekday())
    days = [t for t in nyse.trading_days_between(monday - dt.timedelta(days=1), d)]
    ctx.emit(f"weekly:{wk}", "weekly", None,
             messages.weekly(ctx.st, ctx.log_rows, wk, [t.isoformat() for t in days], len(days)))


def housekeeping(ctx: Ctx) -> None:
    d = ctx.session
    st = ctx.st
    state_mod.prune_sent(st, d)
    for m in state_mod.drop_stale_outbox(st, d):
        ctx.event(f"dropped an undeliverable message after 3 days: {m['key']}")
    market_data.prune_prov(ctx.mkt, d)
    market_data.trim_market(ctx.mkt, d)
    cutoff = (d - dt.timedelta(days=30)).isoformat()
    st["notes"] = {k: v for k, v in st["notes"].items() if k == "prov_vs_pub" or not k.startswith("crash:") or k.split(":")[1] >= cutoff}


def run_close_phase(ctx: Ctx) -> None:
    st = ctx.st
    first_ever = st["last_run"] is None
    if ctx.guard_status == "too_late":
        late = "n/a" if ctx.late_min is None else f"{ctx.late_min:.0f}"
        ctx.problems.append(f"the run started {late} min after its {cfg.TARGET_ET} ET target (usable until {cfg.USABLE_UNTIL_ET}): "
                            f"no new shadow positions today; settlements and exits still ran")
    if first_ever and not ctx.dry_run:
        ctx.emit("hello", "hello", None, messages.hello(ctx.now_et))          # queued first, so it is also posted first
    prev_session = (st["last_run"] or {}).get("session")
    if prev_session != ctx.session.isoformat():
        state_mod.bump_run(st, ctx.session)          # before the weekly summary, so Friday's own run is in its "runs this week"
    account_missed(ctx)
    job_c9.settle(ctx)
    job_c3.process(ctx)
    job_c6.process(ctx)
    job_c9.enter(ctx)
    job_c6.enter(ctx)
    job_c3.enter(ctx)
    evaluate_kill(ctx)
    if d_year_warning(ctx.session):
        ctx.emit("notice:calendar:2028", "notice", None, messages.notice("calendar table ends 2028-12-31", [
            "nyse_calendar.py has explicit holiday tables only through 2028. Extend EXPLICIT_HOLIDAYS / EXPLICIT_EARLY_CLOSES",
            "(or add the generated rules' output for 2029+) before the end of 2028; later dates raise CalendarRange."]))
    data_notice(ctx)
    weekly_summary(ctx)
    housekeeping(ctx)
    for sid in cfg.STRATEGIES:
        if ctx.wants(sid):
            st["last_session"][sid] = ctx.session.isoformat()
    st["last_run"] = {"trigger": ctx.trigger, "at_et": ctx.now_iso, "session": ctx.session.isoformat(), "phase": ctx.phase,
                      "guard": ctx.guard_status or None, "late_min": ctx.late_min, "data_ok": not ctx.problems,
                      "strategies": sorted(ctx.only)}


def d_year_warning(d: dt.date) -> bool:
    return d >= dt.date(2028, 6, 1)


# ---------------------------------------------------------------------------------------------- persistence
def write_diag(ctx: Ctx) -> None:
    rec = {"v": 1, "run_at_et": ctx.now_iso, "session": ctx.session.isoformat(), "phase": ctx.phase,
           "early_close": ctx.early_close, "trigger": ctx.trigger, "guard": ctx.guard_status or None, "late_min": ctx.late_min,
           "dry_run": ctx.dry_run, "strategies": sorted(ctx.only), "problems": ctx.problems, "events": ctx.events, **ctx.diag}
    path = os.path.join(ctx.root, cfg.DIAG_FILE)
    with open(path, "a") as f:
        f.write(json.dumps(rec, default=str, separators=(",", ":")) + "\n")
    try:
        with open(path) as f:
            lines = f.readlines()
        if len(lines) > cfg.DIAG_MAX_LINES:
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                f.writelines(lines[-cfg.DIAG_KEEP_LINES:])
            os.replace(tmp, path)
    except OSError:
        pass


def persist(ctx: Ctx) -> None:
    """Order matters: log rows first (deduped by id), then state (positions, stats, outbox), then the caches and the export."""
    r = ctx.root
    if ctx.phase == "close":
        state_mod.append_log(ctx.new_log_rows, os.path.join(r, cfg.LOG_FILE))
        state_mod.save(ctx.st, os.path.join(r, cfg.STATE_FILE))
        market_data.save_market(ctx.mkt, r)
        export_state.export(os.path.join(r, cfg.STATE_FILE), os.path.join(r, cfg.LOG_FILE), os.path.join(r, cfg.EXPORT_FILE))


def flush_messages(ctx: Ctx, env, sender=None) -> dict:
    """Post the queued messages (state is saved after every post). Only the closing phase posts: a diagnostics-only run
    changes no state, so a post made from it could not be recorded as sent and would be repeated by the next run."""
    if ctx.dry_run:
        return discord_shadow.flush(ctx.st, "", ctx.now_iso, dry_run=True)
    if ctx.phase != "close":
        return {"skipped": ctx.phase, "queued": len(ctx.st["outbox"])}
    url = env.get(discord_shadow.ENV_NAME, "")
    save = lambda: state_mod.save(ctx.st, os.path.join(ctx.root, cfg.STATE_FILE))          # noqa: E731
    kw = {} if sender is None else {"sender": sender}
    return discord_shadow.flush(ctx.st, url, ctx.now_iso, save=save, **kw)


# ---------------------------------------------------------------------------------------------- main
def execute(ctx: Ctx) -> None:
    if ctx.phase == "closed_day":
        ctx.diag["note"] = "not a trading day: nothing to do"
        return
    fetch_chains(ctx)
    refresh_closes(ctx)
    refresh_indices(ctx)
    refresh_tbill(ctx)
    if ctx.phase == "close":
        run_close_phase(ctx)
    else:
        ctx.diag["note"] = f"{ctx.phase}: diagnostics only (no entries, no settlements, no state changes)"


def crash_notice(root: str, session: dt.date, exc: BaseException, env) -> None:
    """Best effort, once per day and exception type: tell the channel the job crashed (the guard then lets the backup cron retry)."""
    try:
        path = os.path.join(root, cfg.STATE_FILE)
        st = state_mod.load(path)
        key = f"crash:{session.isoformat()}:{type(exc).__name__}"
        if st["notes"].get(key):
            return
        st["notes"][key] = True
        state_mod.save(st, path)
        import run_guard
        run_guard.discord_post(f"shadow job crashed on {session}: {type(exc).__name__}: {str(exc)[:300]}. "
                               f"Nothing was traded. The backup trigger may retry; see the Actions log.",
                               webhook_url=env.get(discord_shadow.ENV_NAME) or None)
    except Exception:                                    # noqa: BLE001 - a failing crash report must not mask the crash
        traceback.print_exc()


class Clock:
    """The job's clock. A normal run reads the wall clock. `--now` starts the clock at that time and lets real time pass.
    A fixture replay (`virtual=True`) never waits: sleeping only moves the clock forward, so tests and dry replays are instant."""

    def __init__(self, start: dt.datetime, virtual: bool = False):
        self._t = start
        self._virtual = virtual
        self._mono0 = time.monotonic()

    def now(self) -> dt.datetime:
        if self._virtual:
            return self._t
        return self._t + dt.timedelta(seconds=time.monotonic() - self._mono0)

    def sleep(self, seconds: float) -> None:
        if self._virtual:
            self._t += dt.timedelta(seconds=seconds)
        else:
            time.sleep(seconds)


def main(argv=None, *, feed=None, env=None, sender=None, clock=None) -> int:
    args = parse_args(argv)
    env = os.environ if env is None else env
    if clock is None:
        clock = Clock(parse_now(args.now), virtual=bool(args.fixture_dir))
    now_et = clock.now()
    if feed is None:
        if args.fixture_dir:
            feed = data_feed.Feed(http=data_feed.FixtureHttp(args.fixture_dir), sleep=lambda s: None)
        else:
            feed = data_feed.Feed()
    ctx = build_ctx(args, env, feed, now_et)
    ctx.clock = clock
    t0 = time.time()
    print(f"[shadow] {cfg.BANNER} | session {ctx.session} phase {ctx.phase} trigger {ctx.trigger} guard "
          f"{ctx.guard_status or '-'} strategies {sorted(ctx.only)}{' DRY-RUN' if ctx.dry_run else ''}")
    res: dict = {}
    try:
        execute(ctx)
        ctx.diag["elapsed_s"] = round(time.time() - t0, 1)
        if not ctx.dry_run:
            persist(ctx)
        res = flush_messages(ctx, env, sender)
        ctx.diag["messages"] = res
        if not ctx.dry_run and ctx.phase == "close":
            state_mod.save(ctx.st, os.path.join(ctx.root, cfg.STATE_FILE))
    except Exception as exc:                             # noqa: BLE001
        traceback.print_exc()
        ctx.diag["crashed"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        if not ctx.dry_run:
            crash_notice(ctx.root, ctx.session, exc, env)
        raise
    finally:
        if not ctx.dry_run:
            try:
                write_diag(ctx)
            except OSError:
                traceback.print_exc()
    for e in ctx.events:
        print(f"[shadow] {e}")
    for p in ctx.problems:
        print(f"[shadow] PROBLEM: {p}")
    print(f"[shadow] done in {time.time() - t0:.1f}s; messages: {json.dumps(res)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
