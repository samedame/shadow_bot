"""run_guard.py: the lateness / duplicate guard around the daily job.

Two dates are used: Tue 2026-10-13 (EDT, UTC-4: 20:20 UTC is 16:20 ET) and Mon 2026-12-14 (EST, UTC-5: 21:20 UTC is 16:20 ET).
The workflow's native cron fires at both 20:20 and 21:20 UTC; the guard has to make exactly one of them count."""
import contextlib
import datetime as dt
import io
import json
import os
import unittest
from types import SimpleNamespace
from unittest import mock

from tests.helpers import Base, D, WEBHOOK, et

import config as cfg
import discord_shadow
import run_guard as rg

CMD = ["python", "shadow_job.py", "--trigger", "x"]


class Parsing(unittest.TestCase):
    TODAY = D("2026-10-13")

    def test_hhmm(self):
        self.assertEqual(rg.parse_hhmm("16:20"), dt.time(16, 20))
        self.assertEqual(rg.parse_hhmm(" 9:05 "), dt.time(9, 5))

    def test_scheduled_for_formats(self):
        f = lambda s: rg.parse_scheduled_for(s, self.TODAY)                                  # noqa: E731
        self.assertEqual(f("2026-10-13T16:20:00-04:00"), et("2026-10-13 16:20"))
        self.assertEqual(f("2026-10-13T20:20:00Z"), et("2026-10-13 16:20"))
        self.assertEqual(f("2026-10-13T16:20:00"), et("2026-10-13 16:20"), "no offset means New York time")
        self.assertEqual(f("2026-10-13T16:20:00+00:00"), et("2026-10-13 12:20"))
        self.assertEqual(f("16:20"), et("2026-10-13 16:20"), "HH:MM means that time today")
        epoch = int(et("2026-10-13 16:20").timestamp())
        self.assertEqual(epoch, 1791922800)
        self.assertEqual(f(str(epoch)), et("2026-10-13 16:20"), "epoch seconds")
        self.assertEqual(f(f"{epoch}.5"), et("2026-10-13 16:20").replace(microsecond=500000))

    def test_scheduled_for_garbage_is_ignored(self):
        for s in (None, "", "   ", "soon", "25:99", "2026-13-45", "16:2x", "now"):
            self.assertIsNone(rg.parse_scheduled_for(s, self.TODAY), repr(s))

    def test_scheduled_for_result_is_new_york_time(self):
        t = rg.parse_scheduled_for("2026-12-14T21:20:00Z", D("2026-12-14"))
        self.assertEqual((t.hour, t.minute, t.utcoffset()), (16, 20, dt.timedelta(hours=-5)))


class Classify(unittest.TestCase):
    TARGET = et("2026-10-13 16:20")
    USABLE = et("2026-10-13 16:30")

    def c(self, now, sched=True, on_time=10.0):
        return rg.classify(et(now), self.TARGET, self.USABLE, on_time, sched)

    def test_statuses(self):
        self.assertEqual(self.c("2026-10-13 16:20")[0], "on_time")
        self.assertEqual(self.c("2026-10-13 16:20:00")[1], 0.0)
        self.assertEqual(self.c("2026-10-13 16:30")[0], "on_time")                 # exactly 10 minutes late, exactly the usable time
        self.assertEqual(self.c("2026-10-13 16:30:01")[0], "too_late")
        self.assertEqual(self.c("2026-10-13 16:25", on_time=2.0)[0], "late")      # past on-time, still usable
        self.assertEqual(self.c("2026-10-13 16:30", on_time=2.0)[0], "late")
        self.assertEqual(self.c("2026-10-13 18:00")[0], "too_late")
        self.assertAlmostEqual(self.c("2026-10-13 16:21:30")[1], 1.5)

    def test_early(self):
        """A scheduled run may start at most one minute before the target; earlier than that is skipped."""
        self.assertEqual(self.c("2026-10-13 16:19")[0], "on_time")                  # exactly 1.0 minute early is still allowed
        self.assertEqual(self.c("2026-10-13 16:18:59")[0], "too_early")
        self.assertEqual(self.c("2026-10-13 15:50")[0], "too_early")
        self.assertEqual(self.c("2026-10-13 15:20")[0], "too_early")
        self.assertEqual(self.c("2026-10-13 15:20", sched=False)[0], "on_time", "only scheduled runs can be too early")
        self.assertAlmostEqual(self.c("2026-10-13 15:20")[1], -60.0)


class Marker(Base):
    def test_missing_corrupt_and_wrong_shape_files_are_empty(self):
        d = self.tmp()
        self.assertEqual(rg.load_marker(os.path.join(d, "none.json")), {})
        p = os.path.join(d, "bad.json")
        for text in ("{not json", "[1, 2]", "", "null", '"x"'):
            with open(p, "w") as f:
                f.write(text)
            self.assertEqual(rg.load_marker(p), {}, repr(text))

    def test_save_prunes_old_days_and_leaves_no_temp_file(self):
        p = os.path.join(self.tmp(), "m.json")
        data = {f"shadow-daily:{d}": {"status": "on_time"} for d in ("2026-09-01", "2026-09-12", "2026-09-13", "2026-10-13")}
        rg.save_marker(p, data, D("2026-10-13"))
        self.assertFalse(os.path.exists(p + ".tmp"))
        self.assertEqual(sorted(rg.load_marker(p)), ["shadow-daily:2026-09-13", "shadow-daily:2026-10-13"])

    def test_defaults_match_the_rest_of_the_bot(self):
        self.assertEqual(rg.DEFAULT_MARKER, cfg.GUARD_FILE)
        self.assertEqual(rg.WEBHOOK_ENV, discord_shadow.ENV_NAME)
        self.assertEqual(rg.SCHEDULED_TRIGGERS, ("schedule", "cf-scheduler"))


class Runs(Base):
    """run() with a fake child process and a fixed clock (--now)."""

    def go(self, now, env=None, args=(), marker=None, rc=0, cmd=CMD):
        d = self.tmp()
        mpath = os.path.join(d, "marker.json")
        if marker is not None:
            with open(mpath, "w") as f:
                json.dump(marker, f)
        calls = []

        def runner(c, env=None):
            calls.append((c, dict(env)))
            return SimpleNamespace(returncode=rc)

        argv = ["--job", "shadow-daily", "--target", "16:20", "--usable-until", "16:30", "--on-time-min", "10",
                "--on-too-late", "run_flagged", "--marker", mpath, "--now", now, *args, "--", *cmd]
        out, err = io.StringIO(), io.StringIO()
        env = dict(env or {})
        before = dict(env)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = rg.run(argv, env=env, runner=runner)
        self.assertEqual(env, before, "the caller's environment must not be modified")
        saved = {}
        if os.path.exists(mpath):
            with open(mpath) as f:
                saved = json.load(f)
        return SimpleNamespace(code=code, calls=calls, marker=saved, out=out.getvalue(), err=err.getvalue())

    # ------------------------------------------------------------------ the normal day
    def test_worker_run_on_time(self):
        r = self.go("2026-10-13T16:21:30", {"RUN_TRIGGER": "cf-scheduler", "SCHEDULED_FOR_ET": "2026-10-13T16:20:00-04:00",
                                            "SHADOW_DISCORD_WEBHOOK_URL": WEBHOOK})
        self.assertEqual(r.code, 0)
        self.assertEqual(len(r.calls), 1)
        cmd, env = r.calls[0]
        self.assertEqual(cmd, CMD)
        self.assertEqual((env["RUN_GUARD_STATUS"], env["RUN_GUARD_LATE_MIN"], env["RUN_TRIGGER"]), ("on_time", "1.5", "cf-scheduler"))
        self.assertEqual(env["SHADOW_DISCORD_WEBHOOK_URL"], WEBHOOK, "the rest of the environment reaches the job")
        self.assertEqual(r.marker, {"shadow-daily:2026-10-13": {"done_at": "2026-10-13T16:21:30-04:00", "late_min": 1.5,
                                                                 "status": "on_time", "trigger": "cf-scheduler"}})
        self.assertIn("late +1.5 min -> on_time (trigger cf-scheduler)", r.out)

    def test_native_cron_run_on_time(self):
        r = self.go("2026-10-13T16:20:40", {"RUN_TRIGGER": "schedule"})
        self.assertEqual(len(r.calls), 1)
        self.assertEqual(r.marker["shadow-daily:2026-10-13"]["trigger"], "schedule")

    def test_a_second_copy_the_same_day_is_a_silent_duplicate(self):
        marker = {"shadow-daily:2026-10-13": {"done_at": "2026-10-13T16:21:30-04:00", "late_min": 1.5, "status": "on_time",
                                              "trigger": "cf-scheduler"}}
        r = self.go("2026-10-13T16:25:00", {"RUN_TRIGGER": "schedule"}, marker=marker)
        self.assertEqual((r.code, r.calls), (0, []))
        self.assertEqual(r.marker, marker, "the marker is untouched")
        self.assertIn("duplicate, skipped silently", r.out)

    def test_yesterdays_marker_does_not_block_today(self):
        marker = {"shadow-daily:2026-10-12": {"done_at": "2026-10-12T16:21:30-04:00", "late_min": 1.5, "status": "on_time",
                                              "trigger": "cf-scheduler"}}
        r = self.go("2026-10-13T16:21:00", {"RUN_TRIGGER": "cf-scheduler"}, marker=marker)
        self.assertEqual(len(r.calls), 1)
        self.assertEqual(sorted(r.marker), ["shadow-daily:2026-10-12", "shadow-daily:2026-10-13"])

    # ------------------------------------------------------------------ lateness
    def test_too_late_runs_flagged_so_the_job_can_still_settle(self):
        r = self.go("2026-10-13T19:05:00", {"RUN_TRIGGER": "schedule"})
        self.assertEqual(len(r.calls), 1)
        env = r.calls[0][1]
        self.assertEqual((env["RUN_GUARD_STATUS"], env["RUN_GUARD_LATE_MIN"]), ("too_late", "165.0"))
        self.assertEqual(r.marker["shadow-daily:2026-10-13"]["status"], "too_late")

    def test_too_late_with_skip_runs_nothing(self):
        r = self.go("2026-10-13T19:05:00", {"RUN_TRIGGER": "schedule"}, args=("--on-too-late", "skip"))
        self.assertEqual((r.code, r.calls, r.marker), (0, [], {}))
        self.assertIn("too late and --on-too-late skip", r.out)

    def test_on_too_late_defaults_to_skip(self):
        d = self.tmp()
        calls = []
        argv = ["--job", "j", "--target", "16:20", "--usable-until", "16:30", "--marker", os.path.join(d, "m.json"),
                "--now", "2026-10-13T19:00:00", "--", "x"]
        with contextlib.redirect_stdout(io.StringIO()):
            rg.run(argv, env={"RUN_TRIGGER": "schedule"}, runner=lambda c, env=None: calls.append(c) or SimpleNamespace(returncode=0))
        self.assertEqual(calls, [])

    def test_worker_run_dispatched_for_a_time_uses_that_time(self):
        """The Worker says the intended time; lateness is measured from it, not from the --target default."""
        r = self.go("2026-10-13T16:41:00", {"RUN_TRIGGER": "cf-scheduler", "SCHEDULED_FOR_ET": "2026-10-13T16:40:00-04:00"})
        self.assertEqual(r.calls[0][1]["RUN_GUARD_LATE_MIN"], "1.0")
        self.assertEqual(r.calls[0][1]["RUN_GUARD_STATUS"], "too_late", "usable-until is still 16:30 on the target's date")

    def test_scheduled_for_given_as_hhmm_or_garbage(self):
        r = self.go("2026-10-13T16:23:00", {"RUN_TRIGGER": "cf-scheduler", "SCHEDULED_FOR_ET": "16:20"})
        self.assertEqual(r.calls[0][1]["RUN_GUARD_LATE_MIN"], "3.0")
        r = self.go("2026-10-13T16:23:00", {"RUN_TRIGGER": "cf-scheduler", "SCHEDULED_FOR_ET": "garbage"})
        self.assertEqual(r.calls[0][1]["RUN_GUARD_LATE_MIN"], "3.0", "unparseable: the --target time on today's date")

    # ------------------------------------------------------------------ the two native crons across the clock change
    def test_summer_20_20_utc_runs_and_21_20_utc_is_a_duplicate(self):
        first = self.go("2026-10-13T20:20:30+00:00", {"RUN_TRIGGER": "schedule"})            # 16:20:30 EDT
        self.assertEqual(first.calls[0][1]["RUN_GUARD_STATUS"], "on_time")
        second = self.go("2026-10-13T21:20:30+00:00", {"RUN_TRIGGER": "schedule"}, marker=first.marker)       # 17:20:30 EDT
        self.assertEqual((second.code, second.calls), (0, []))

    def test_winter_20_20_utc_is_the_wrong_season_copy_and_21_20_utc_runs(self):
        early = self.go("2026-12-14T20:20:30+00:00", {"RUN_TRIGGER": "schedule"})            # 15:20:30 EST
        self.assertEqual((early.code, early.calls, early.marker), (0, [], {}))
        self.assertIn("wrong-season copy", early.out)
        on = self.go("2026-12-14T21:20:30+00:00", {"RUN_TRIGGER": "schedule"}, marker=early.marker)      # 16:20:30 EST
        self.assertEqual(on.calls[0][1]["RUN_GUARD_STATUS"], "on_time")
        self.assertEqual(list(on.marker), ["shadow-daily:2026-12-14"])

    def test_a_delayed_wrong_season_copy_cannot_use_up_the_day(self):
        """Winter: GitHub runs the 20:20 UTC copy 50 minutes late, i.e. at 16:10 EST, ten minutes before the Worker's run. If it
        ran (finding no closing snapshot yet) and wrote the marker, the Worker's 16:20 run would be skipped as a duplicate and the
        day's entries lost. It is skipped instead, with no marker, and the Worker's run goes ahead."""
        early = self.go("2026-12-14T21:10:00+00:00", {"RUN_TRIGGER": "schedule"})             # 16:10 EST
        self.assertEqual((early.calls, early.marker), ([], {}))
        worker = self.go("2026-12-14T21:21:00+00:00", {"RUN_TRIGGER": "cf-scheduler", "SCHEDULED_FOR_ET": "2026-12-14T16:20:00-05:00"},
                         marker=early.marker)
        self.assertEqual(len(worker.calls), 1)
        self.assertEqual(worker.calls[0][1]["RUN_GUARD_STATUS"], "on_time")

    def test_summer_21_20_utc_alone_is_too_late_but_still_runs_flagged(self):
        """The 20:20 copy never ran (GitHub skipped it): the 21:20 copy still settles and re-marks, with no new entries."""
        r = self.go("2026-10-13T21:20:30+00:00", {"RUN_TRIGGER": "schedule"})
        self.assertEqual(r.calls[0][1]["RUN_GUARD_STATUS"], "too_late")

    def test_a_missed_worker_run_is_picked_up_by_the_native_backup_on_time_in_the_right_season(self):
        r = self.go("2026-12-14T21:21:00+00:00", {"RUN_TRIGGER": "schedule"})
        self.assertEqual(r.calls[0][1]["RUN_GUARD_STATUS"], "on_time")

    # ------------------------------------------------------------------ manual runs and failures
    def test_manual_runs_ignore_and_never_write_the_marker(self):
        marker = {"shadow-daily:2026-10-13": {"done_at": "x", "late_min": 0, "status": "on_time", "trigger": "schedule"}}
        r = self.go("2026-10-13T16:40:00", {}, marker=marker)
        self.assertEqual(len(r.calls), 1, "a person pressing Run workflow always gets a run")
        self.assertEqual(r.calls[0][1]["RUN_TRIGGER"], "manual")
        self.assertEqual(r.calls[0][1]["RUN_GUARD_STATUS"], "too_late", "lateness is still reported to the job")
        self.assertEqual(r.marker, marker)
        self.assertIn("manual (too_late)", r.out)

    def test_manual_run_before_the_target_is_on_time(self):
        r = self.go("2026-10-13T11:00:00", {"RUN_TRIGGER": "manual"})
        self.assertEqual(r.calls[0][1]["RUN_GUARD_STATUS"], "on_time")
        self.assertEqual(r.marker, {})

    def test_a_manual_run_does_not_consume_the_real_run(self):
        r = self.go("2026-10-13T16:21:00", {"RUN_TRIGGER": "manual"})
        self.assertEqual(r.marker, {})
        r2 = self.go("2026-10-13T16:22:00", {"RUN_TRIGGER": "cf-scheduler"}, marker=r.marker)
        self.assertEqual(len(r2.calls), 1)

    def test_unknown_trigger_names_are_manual(self):
        for t in ("workflow_dispatch", "push", "pull_request", "something"):
            r = self.go("2026-10-13T16:21:00", {"RUN_TRIGGER": t})
            self.assertEqual(r.marker, {}, t)
            self.assertEqual(r.calls[0][1]["RUN_TRIGGER"], t)

    def test_a_failing_job_leaves_no_marker_so_the_backup_can_retry(self):
        r = self.go("2026-10-13T16:21:00", {"RUN_TRIGGER": "cf-scheduler"}, rc=3)
        self.assertEqual((r.code, r.marker), (3, {}))
        self.assertIn("exited 3", r.err)
        retry = self.go("2026-10-13T16:26:00", {"RUN_TRIGGER": "schedule"}, marker=r.marker)
        self.assertEqual(len(retry.calls), 1)

    def test_a_corrupt_marker_file_does_not_stop_the_run(self):
        d = self.tmp()
        mpath = os.path.join(d, "m.json")
        with open(mpath, "w") as f:
            f.write("{oops")
        argv = ["--job", "shadow-daily", "--target", "16:20", "--usable-until", "16:30", "--marker", mpath,
                "--now", "2026-10-13T16:21:00", "--", "x"]
        with contextlib.redirect_stdout(io.StringIO()):
            code = rg.run(argv, env={"RUN_TRIGGER": "schedule"}, runner=lambda c, env=None: SimpleNamespace(returncode=0))
        self.assertEqual(code, 0)
        self.assertEqual(list(rg.load_marker(mpath)), ["shadow-daily:2026-10-13"])

    def test_another_job_name_has_its_own_marker_key(self):
        marker = {"shadow-daily:2026-10-13": {"done_at": "x", "late_min": 0, "status": "on_time", "trigger": "schedule"}}
        d = self.tmp()
        mpath = os.path.join(d, "m.json")
        with open(mpath, "w") as f:
            json.dump(marker, f)
        calls = []
        argv = ["--job", "other-job", "--target", "16:20", "--usable-until", "16:30", "--marker", mpath,
                "--now", "2026-10-13T16:21:00", "--", "x"]
        with contextlib.redirect_stdout(io.StringIO()):
            rg.run(argv, env={"RUN_TRIGGER": "schedule"}, runner=lambda c, env=None: calls.append(c) or SimpleNamespace(returncode=0))
        self.assertEqual(len(calls), 1)

    def test_a_command_is_required(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
            rg.run(["--job", "j", "--target", "16:20", "--usable-until", "16:30"], env={})
        self.assertEqual(cm.exception.code, 2)

    def test_split_command(self):
        self.assertEqual(rg.split_command(["--a", "1", "--", "python", "x.py", "--b"]), (["--a", "1"], ["python", "x.py", "--b"]))
        self.assertEqual(rg.split_command(["--a"]), (["--a"], []))

    def test_the_real_subprocess_runner_passes_the_environment_to_the_child(self):
        """No fake: a tiny real child process proves the env vars arrive and a non-zero exit is returned."""
        import subprocess
        import sys
        d = self.tmp()
        out_file = os.path.join(d, "seen.txt")
        code = ("import os,sys; open(sys.argv[1],'w').write(os.environ['RUN_GUARD_STATUS']+'|'+os.environ['RUN_GUARD_LATE_MIN']"
                "+'|'+os.environ['RUN_TRIGGER']); sys.exit(0)")
        argv = ["--job", "shadow-daily", "--target", "16:20", "--usable-until", "16:30", "--marker", os.path.join(d, "m.json"),
                "--now", "2026-10-13T16:21:00", "--", sys.executable, "-c", code, out_file]
        with contextlib.redirect_stdout(io.StringIO()):
            rc = rg.run(argv, env=dict(os.environ, RUN_TRIGGER="cf-scheduler"))
        self.assertEqual(rc, 0)
        with open(out_file) as f:
            self.assertEqual(f.read(), "on_time|1.0|cf-scheduler")
        argv[-4:] = [sys.executable, "-c", "import sys; sys.exit(7)", "x"]
        argv[argv.index("--marker") + 1] = os.path.join(d, "m2.json")          # the first call already marked the day in m.json
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(rg.run(argv, env=dict(os.environ, RUN_TRIGGER="cf-scheduler")), 7)


class DiscordPost(unittest.TestCase):
    def test_not_configured(self):
        err = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=False), contextlib.redirect_stderr(err):
            os.environ.pop("SHADOW_DISCORD_WEBHOOK_URL", None)
            self.assertFalse(rg.discord_post("hello"))
        self.assertIn("SHADOW_DISCORD_WEBHOOK_URL is not set", err.getvalue())

    def test_refuses_a_non_discord_url_without_sending(self):
        with mock.patch.object(discord_shadow, "send") as send, contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(rg.discord_post("hello", webhook_url="https://example.com/hook"))
        send.assert_not_called()

    def test_payload(self):
        with mock.patch.object(discord_shadow, "send", return_value=(True, 204, "")) as send:
            self.assertTrue(rg.discord_post("job crashed", webhook_url=WEBHOOK))
        url, payload = send.call_args[0]
        self.assertEqual(url, WEBHOOK)
        self.assertEqual(payload, {"content": "SHADOW - NOT A TRADE | job crashed", "allowed_mentions": {"parse": []}})

    def test_banner_not_doubled_and_long_text_cut(self):
        with mock.patch.object(discord_shadow, "send", return_value=(True, 204, "")) as send:
            rg.discord_post(f"{cfg.BANNER} | already", webhook_url=WEBHOOK)
            self.assertEqual(send.call_args[0][1]["content"], "SHADOW - NOT A TRADE | already")
            rg.discord_post("x" * 5000, webhook_url=WEBHOOK)
            self.assertLessEqual(len(send.call_args[0][1]["content"]), 1900)

    def test_environment_webhook_is_used_when_none_is_passed(self):
        with mock.patch.dict(os.environ, {"SHADOW_DISCORD_WEBHOOK_URL": WEBHOOK}), \
                mock.patch.object(discord_shadow, "send", return_value=(True, 204, "")) as send:
            self.assertTrue(rg.discord_post("x"))
        self.assertEqual(send.call_args[0][0], WEBHOOK)

    def test_failures_never_raise(self):
        with mock.patch.object(discord_shadow, "send", return_value=(False, 500, "HTTP 500")), contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(rg.discord_post("x", webhook_url=WEBHOOK))
        with mock.patch.object(discord_shadow, "send", side_effect=RuntimeError("boom")), contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(rg.discord_post("x", webhook_url=WEBHOOK))


if __name__ == "__main__":
    unittest.main()
