#!/usr/bin/env python3
"""run_guard.py - lateness and duplicate guard around a scheduled job. Standard library only.

    python run_guard.py --job shadow-daily --target 16:20 --usable-until 16:30 --on-time-min 10 --on-too-late run_flagged -- \\
        python shadow_job.py --trigger "$RUN_TRIGGER"

Same contract as the guard in the live bots (see claude/bot-schedule-incident-2026-09-25 and claude/condor-pipeline-worker-
wiring-2026-10-08 in the project docs): the same flags, the same SCHEDULED_FOR_ET / RUN_TRIGGER environment variables and the
same marker format. This copy was written from that description (the live repos' file was not available), so it is the
shadow repo's own; it never touches the live bots' marker files.

What it decides, from the New York wall clock and the intended fire time:

    too_early   a scheduled run that starts more than EARLY_SKIP_MIN before the target: the wrong-season copy of the two-hour
                native cron (20:20 UTC is 15:20 ET in winter), or a copy GitHub delayed into the minutes before the target.
                Skipped silently, nothing runs and no marker is written, so an early copy can never use up the day before the
                real run (the Worker's, at the target) gets to do its work.
    on_time     at most --on-time-min minutes after the target.
    late        after that, but still inside --usable-until.
    too_late    after --usable-until. --on-too-late skip: do not run. --on-too-late run_flagged: run the job anyway with
                RUN_GUARD_STATUS=too_late (the shadow job then opens nothing new but still settles, exits and re-marks).
    duplicate   a scheduled run when today's marker already exists. Skipped silently.
    manual      a run whose trigger is not a scheduled one (a person pressing "Run workflow", tests): it ignores and never
                writes the marker, so it cannot block or consume the real run; lateness is still reported to the job.

The marker file (default .run_guard_shadow.json) maps "<job>:<YYYY-MM-DD>" to
{"done_at", "late_min", "status", "trigger"} and is written only after the job exits 0, so a crash can be retried by the
backup trigger. The child sees RUN_GUARD_STATUS, RUN_GUARD_LATE_MIN and RUN_TRIGGER in its environment.

SCHEDULED_FOR_ET (set by the Cloudflare Worker's dispatch) is read as an ISO-8601 time (with or without an offset; no offset
means New York time), an "HH:MM" time today, or epoch seconds. Anything unparseable is ignored and the target time on today's
New York date is used.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
from typing import Optional
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
SCHEDULED_TRIGGERS = ("schedule", "cf-scheduler")
EARLY_SKIP_MIN = 1.0          # minutes; the Worker never fires before the target, so one minute covers clock jitter
KEEP_MARKER_DAYS = 30
DEFAULT_MARKER = ".run_guard_shadow.json"
WEBHOOK_ENV = "SHADOW_DISCORD_WEBHOOK_URL"


# ---------------------------------------------------------------------------------------------- time helpers
def parse_hhmm(text: str) -> dt.time:
    h, m = text.strip().split(":")
    return dt.time(int(h), int(m))


def parse_scheduled_for(text: Optional[str], today: dt.date) -> Optional[dt.datetime]:
    """SCHEDULED_FOR_ET -> aware ET datetime, or None when empty or unparseable."""
    if not text or not text.strip():
        return None
    s = text.strip()
    try:
        if s.replace(".", "", 1).isdigit() and len(s) >= 9:                       # epoch seconds
            return dt.datetime.fromtimestamp(float(s), tz=dt.timezone.utc).astimezone(ET)
        if len(s) <= 5 and ":" in s:                                              # "16:20"
            return dt.datetime.combine(today, parse_hhmm(s), tzinfo=ET)
        t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        return t.replace(tzinfo=ET) if t.tzinfo is None else t.astimezone(ET)
    except (ValueError, OverflowError, OSError):
        return None


def classify(now_et: dt.datetime, target_et: dt.datetime, usable_until_et: dt.datetime, on_time_min: float,
             scheduled: bool) -> tuple:
    """-> (status, late_min). late_min is negative when the run started before the target."""
    late = (now_et - target_et).total_seconds() / 60.0
    if scheduled and late < -EARLY_SKIP_MIN:
        return "too_early", late
    if now_et > usable_until_et:
        return "too_late", late
    if late <= on_time_min:
        return "on_time", late
    return "late", late


# ---------------------------------------------------------------------------------------------- marker
def load_marker(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_marker(path: str, data: dict, today: dt.date) -> None:
    cutoff = (today - dt.timedelta(days=KEEP_MARKER_DAYS)).isoformat()
    kept = {k: v for k, v in data.items() if k.rsplit(":", 1)[-1] >= cutoff}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(kept, f, indent=1, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------------------------- Discord
def discord_post(text: str, webhook_url: Optional[str] = None) -> bool:
    """Post one plain line, banner first, mentions disabled. Best effort: returns False instead of raising. The webhook is
    SHADOW_DISCORD_WEBHOOK_URL (never the live DISCORD_WEBHOOK_URL) unless one is passed in."""
    try:
        import config as cfg
        import discord_shadow
        url = webhook_url or os.environ.get(WEBHOOK_ENV)
        if not discord_shadow.valid_webhook(url):
            print(f"[run_guard] discord_post skipped: {WEBHOOK_ENV} is not set to a Discord webhook", file=sys.stderr)
            return False
        body = text if text.startswith(cfg.BANNER) else f"{cfg.BANNER} | {text}"
        payload = {"content": body[:1900], "allowed_mentions": {"parse": []}}
        ok, _code, detail = discord_shadow.send(url, payload)
        if not ok:
            print(f"[run_guard] discord_post failed: {detail}", file=sys.stderr)
        return ok
    except Exception as exc:                                    # noqa: BLE001 - never let a notice break the guard
        print(f"[run_guard] discord_post error: {exc}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------------------------- main
def split_command(argv: list) -> tuple:
    if "--" in argv:
        i = argv.index("--")
        return argv[:i], argv[i + 1:]
    return argv, []


def run(argv: Optional[list] = None, env: Optional[dict] = None, runner=subprocess.run) -> int:
    own, cmd = split_command(list(sys.argv[1:] if argv is None else argv))
    ap = argparse.ArgumentParser(prog="run_guard.py", description="Lateness and duplicate guard for a scheduled job.")
    ap.add_argument("--job", required=True)
    ap.add_argument("--target", required=True, help="intended fire time, HH:MM New York time")
    ap.add_argument("--usable-until", required=True, help="last HH:MM at which a run is still useful")
    ap.add_argument("--on-time-min", type=float, default=10.0)
    ap.add_argument("--on-too-late", choices=("skip", "run_flagged"), default="skip")
    ap.add_argument("--marker", default=DEFAULT_MARKER)
    ap.add_argument("--now", default=None, help="override the clock, ISO-8601 (naive = New York time); for tests")
    a = ap.parse_args(own)
    if not cmd:
        ap.error("no command given (put it after --)")
    env = dict(os.environ if env is None else env)

    now = dt.datetime.fromisoformat(a.now) if a.now else dt.datetime.now(ET)
    now = now.replace(tzinfo=ET) if now.tzinfo is None else now.astimezone(ET)
    today = now.date()
    trigger = env.get("RUN_TRIGGER") or "manual"
    scheduled = trigger in SCHEDULED_TRIGGERS
    target = parse_scheduled_for(env.get("SCHEDULED_FOR_ET"), today) or dt.datetime.combine(today, parse_hhmm(a.target), tzinfo=ET)
    usable_until = dt.datetime.combine(target.date(), parse_hhmm(a.usable_until), tzinfo=ET)
    status, late = classify(now, target, usable_until, a.on_time_min, scheduled)
    key = f"{a.job}:{target.date().isoformat()}"
    marker = load_marker(a.marker)
    print(f"[run_guard] {a.job} {target.date()}: target {target:%H:%M} ET, now {now:%H:%M:%S} ET, late {late:+.1f} min -> "
          f"{status if scheduled else 'manual (' + status + ')'} (trigger {trigger})")

    if scheduled:
        if status == "too_early":
            print("[run_guard] wrong-season copy of the native cron: skipped silently")
            return 0
        if key in marker:
            print(f"[run_guard] {key} already done at {marker[key].get('done_at')} by {marker[key].get('trigger')}: "
                  f"duplicate, skipped silently")
            return 0
        if status == "too_late" and a.on_too_late == "skip":
            print("[run_guard] too late and --on-too-late skip: nothing runs")
            return 0

    child_env = dict(env, RUN_GUARD_STATUS=status, RUN_GUARD_LATE_MIN=f"{late:.1f}", RUN_TRIGGER=trigger)
    rc = runner(cmd, env=child_env).returncode
    if rc == 0 and scheduled:
        marker[key] = {"done_at": dt.datetime.now(ET).isoformat(timespec="seconds") if not a.now else now.isoformat(timespec="seconds"),
                       "late_min": round(late, 1), "status": status, "trigger": trigger}
        save_marker(a.marker, marker, today)
    elif rc != 0:
        print(f"[run_guard] the job exited {rc}: no marker written, so a backup trigger may retry", file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(run())
