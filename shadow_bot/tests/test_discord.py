"""Discord transport (discord_shadow.py): message building, the payload rules, the HTTP sender and the outbox flush.

Nothing here touches the network: `send` takes an `opener` and a `sleep`, `flush` takes a `sender`."""
import io
import json
import unittest
import urllib.error

from tests.helpers import Base, FakeSender, WEBHOOK

import config as cfg
import discord_shadow as ds
import state as S

NOW = "2026-10-13T16:21:00-04:00"


def queued(*keys, embed=True):
    st = S.new_state()
    for k in keys:
        msg = ds.make_message(f"headline {k}", [f"line for {k}"])
        S.enqueue(st, k, "notice", None, msg["embed"] if embed else None, msg["plain"], "2026-10-13T16:20:00-04:00")
    return st


class Building(unittest.TestCase):
    def test_the_banner_leads_every_message(self):
        m = ds.make_message("C9 CONDOR_1D | 2026-10-13 16:20 ET", ["Would-be ticket : SELL 1 604C", "Credit : 1.73"], "entry")
        self.assertTrue(m["plain"].startswith("SHADOW - NOT A TRADE | C9 CONDOR_1D"))
        self.assertEqual(m["embed"]["footer"]["text"], "SHADOW - NOT A TRADE")
        self.assertEqual(m["embed"]["title"], "C9 CONDOR_1D | 2026-10-13 16:20 ET")
        self.assertEqual(m["embed"]["description"], "```\nWould-be ticket : SELL 1 604C\nCredit : 1.73\n```")
        self.assertEqual(m["embed"]["color"], ds.COLORS["entry"])

    def test_headline_only(self):
        m = ds.make_message("online", [])
        self.assertEqual(m["plain"], "SHADOW - NOT A TRADE | online")
        self.assertEqual(m["embed"]["description"], "")

    def test_unknown_colour_falls_back_to_info(self):
        self.assertEqual(ds.make_message("x", ["y"], "puce")["embed"]["color"], ds.COLORS["info"])

    def test_long_text_is_cut_to_the_limits(self):
        m = ds.make_message("h" * 400, ["line " + "x" * 90 for _ in range(60)])
        self.assertLessEqual(len(m["plain"]), ds.LIM["plain"])
        self.assertTrue(m["plain"].startswith(cfg.BANNER))
        self.assertTrue(m["plain"].endswith("…"))
        self.assertIsNone(m["embed"], "an over-long description cannot be an embed: the plain twin is posted instead")
        m = ds.make_message("h" * 400, ["short"])
        self.assertLessEqual(len(m["embed"]["title"]), ds.LIM["title"])
        self.assertTrue(m["embed"]["title"].endswith("…"))

    def test_fields_are_cut_to_the_limits(self):
        m = ds.make_message("h", ["b"], fields=[("n" * 300, "v" * 2000)])
        f = m["embed"]["fields"][0]
        self.assertEqual((len(f["name"]), len(f["value"])), (256, 1024))
        self.assertTrue(f["name"].endswith("…") and f["value"].endswith("…"))

    def test_at_most_25_fields(self):
        m = ds.make_message("h", ["b"], fields=[(f"n{i}", "v") for i in range(30)])
        self.assertEqual(len(m["embed"]["fields"]), 25)

    def test_an_embed_over_the_total_limit_is_dropped(self):
        m = ds.make_message("h", ["b"], fields=[("n" * 250, "v" * 1000)] * 6)
        self.assertIsNone(m["embed"])
        self.assertTrue(m["plain"].startswith(cfg.BANNER))

    def test_embed_size_counts_title_description_footer_and_fields(self):
        e = {"title": "ab", "description": "cde", "footer": {"text": "f"}, "fields": [{"name": "gh", "value": "ijk"}]}
        self.assertEqual(ds.embed_size(e), 2 + 3 + 1 + 2 + 3)
        self.assertEqual(ds.embed_size({}), 0)

    def test_trunc(self):
        self.assertEqual(ds.trunc("abcdef", 6), "abcdef")
        self.assertEqual(ds.trunc("abcdefg", 6), "abcde…")
        self.assertEqual(ds.trunc("abc", 0), "…")
        self.assertEqual(ds.trunc(12345, 3), "12…")


class Payloads(unittest.TestCase):
    def test_mentions_are_always_disabled(self):
        m = ds.make_message("x", ["@everyone @here <@123456>"])
        for use_embed in (True, False):
            p = ds.payload_for(m, use_embed)
            self.assertEqual(p["allowed_mentions"], {"parse": []})

    def test_embed_payload(self):
        m = ds.make_message("headline", ["a", "b"])
        p = ds.payload_for(m, True)
        self.assertEqual(p["content"], "SHADOW - NOT A TRADE | headline", "the content line is the banner + headline only")
        self.assertEqual(p["embeds"], [m["embed"]])

    def test_plain_payload(self):
        m = ds.make_message("headline", ["a", "b"])
        p = ds.payload_for(m, False)
        self.assertNotIn("embeds", p)
        self.assertEqual(p["content"], m["plain"])

    def test_a_message_without_an_embed_is_posted_plain_even_when_an_embed_is_wanted(self):
        m = {"embed": None, "plain": "SHADOW - NOT A TRADE | x\nbody"}
        p = ds.payload_for(m, True)
        self.assertNotIn("embeds", p)
        self.assertEqual(p["content"], m["plain"])

    def test_the_banner_is_added_if_a_stored_message_lost_it(self):
        self.assertTrue(ds.payload_for({"embed": None, "plain": "no banner here"})["content"].startswith(cfg.BANNER))
        self.assertTrue(ds.payload_for({"embed": None, "plain": ""})["content"].startswith(cfg.BANNER))
        self.assertTrue(ds.payload_for({"embed": None})["content"].startswith(cfg.BANNER))

    def test_content_never_exceeds_discords_limit(self):
        m = {"embed": None, "plain": cfg.BANNER + " | " + "x" * 5000}
        self.assertLessEqual(len(ds.payload_for(m)["content"]), 2000)


class WebhookValidation(unittest.TestCase):
    def test_accepted(self):
        for url in ("https://discord.com/api/webhooks/1/abc", "https://discordapp.com/api/webhooks/1/abc",
                    "https://canary.discord.com/api/webhooks/1/abc", "https://ptb.discord.com/api/webhooks/1/abc", WEBHOOK):
            self.assertTrue(ds.valid_webhook(url), url)

    def test_refused(self):
        for url in (None, "", "   ", "http://discord.com/api/webhooks/1/abc", "https://example.com/api/webhooks/1/abc",
                    "https://discord.com.evil.example/api/webhooks/1/abc", "https://discord.com/api/webhooks",
                    "https://evil.example/https://discord.com/api/webhooks/1/abc", "discord.com/api/webhooks/1/abc",
                    "not a url", "https://hooks.slack.com/services/T/B/x"):
            self.assertFalse(ds.valid_webhook(url), url)

    def test_the_env_name_is_the_shadow_one(self):
        self.assertEqual(ds.ENV_NAME, "SHADOW_DISCORD_WEBHOOK_URL")


class Response:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def getcode(self):
        return self.status


def http_error(code, body=""):
    return urllib.error.HTTPError(WEBHOOK, code, "msg", {}, io.BytesIO(body.encode()))


class Opener:
    """urlopen double: a list of outcomes, each a status int (a response), an exception to raise, or a callable."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.requests: list = []

    def __call__(self, req, timeout=None):
        self.requests.append((req, timeout))
        o = self.outcomes.pop(0) if self.outcomes else 204
        if isinstance(o, BaseException):
            raise o
        return Response(o)


class Sleeps:
    def __init__(self):
        self.calls: list = []

    def __call__(self, s):
        self.calls.append(s)


class Sending(unittest.TestCase):
    PAYLOAD = {"content": "SHADOW - NOT A TRADE | x", "allowed_mentions": {"parse": []}}

    def send(self, *outcomes, retries=3):
        op, sl = Opener(*outcomes), Sleeps()
        res = ds.send(WEBHOOK, self.PAYLOAD, retries=retries, sleep=sl, opener=op)
        return res, op, sl

    def test_request_shape(self):
        res, op, sl = self.send(204)
        self.assertEqual(res, (True, 204, ""))
        req, timeout = op.requests[0]
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.full_url, WEBHOOK)
        self.assertEqual(json.loads(req.data), self.PAYLOAD)
        h = {k.lower(): v for k, v in req.header_items()}
        self.assertEqual(h["content-type"], "application/json")
        self.assertEqual(h["user-agent"], ds.USER_AGENT)
        self.assertEqual(timeout, 15)
        self.assertEqual(sl.calls, [])

    def test_the_user_agent_is_not_the_python_default_and_carries_no_account_details(self):
        self.assertTrue(ds.USER_AGENT.startswith("DiscordBot ("), "Discord's documented 'DiscordBot ($url, $version)' form")
        self.assertNotIn("python", ds.USER_AGENT.lower())
        self.assertNotIn("@", ds.USER_AGENT)

    def test_200_is_a_success_too(self):
        self.assertEqual(self.send(200)[0], (True, 200, ""))

    def test_client_errors_are_final(self):
        for code in (400, 401, 403, 404, 413):
            res, op, sl = self.send(http_error(code, "bad"), 204)
            self.assertEqual(res[:2], (False, code), code)
            self.assertIn(f"HTTP {code}", res[2])
            self.assertEqual(len(op.requests), 1, f"{code} must not be retried")
            self.assertEqual(sl.calls, [])

    def test_the_error_body_is_kept_for_the_log_but_cut(self):
        res, _op, _sl = self.send(http_error(400, "x" * 1000))
        self.assertLessEqual(len(res[2]), len("HTTP 400 ") + 200)

    def test_server_errors_retry_with_backoff_and_can_recover(self):
        res, op, sl = self.send(http_error(502), http_error(503), 204)
        self.assertEqual(res, (True, 204, ""))
        self.assertEqual(len(op.requests), 3)
        self.assertEqual(sl.calls, [1, 3])

    def test_server_errors_give_up_after_the_last_attempt(self):
        res, op, sl = self.send(http_error(500), http_error(500), http_error(500))
        self.assertEqual(res[:2], (False, 500))
        self.assertEqual(len(op.requests), 3)
        self.assertEqual(sl.calls, [1, 3], "no sleep after the last attempt")

    def test_network_errors_retry_then_report_no_status(self):
        err = urllib.error.URLError("name resolution failed")
        res, op, sl = self.send(err, err, err)
        self.assertEqual(res[0:2], (False, None))
        self.assertTrue(res[2].startswith("network:"))
        self.assertEqual(len(op.requests), 3)
        res, op, _ = self.send(OSError("reset"), 204)
        self.assertEqual(res, (True, 204, ""))

    def test_rate_limit_honours_retry_after(self):
        res, op, sl = self.send(http_error(429, json.dumps({"retry_after": 1.5})), 204)
        self.assertEqual(res, (True, 204, ""))
        self.assertEqual(sl.calls, [1.5])
        self.assertEqual(len(op.requests), 2)

    def test_rate_limit_waits_are_bounded(self):
        _r, _o, sl = self.send(http_error(429, json.dumps({"retry_after": 99})), 204)
        self.assertEqual(sl.calls, [10.0])
        _r, _o, sl = self.send(http_error(429, json.dumps({"retry_after": 0})), 204)
        self.assertEqual(sl.calls, [0.5])
        _r, _o, sl = self.send(http_error(429, "not json"), 204)
        self.assertEqual(sl.calls, [2.0])
        _r, _o, sl = self.send(http_error(429, json.dumps({"retry_after": "soon"})), 204)
        self.assertEqual(sl.calls, [2.0])
        _r, _o, sl = self.send(http_error(429, json.dumps([1, 2])), 204)
        self.assertEqual(sl.calls, [2.0])

    def test_rate_limited_to_the_end(self):
        res, op, sl = self.send(*[http_error(429, json.dumps({"retry_after": 1})) for _ in range(3)])
        self.assertEqual(res[:2], (False, 429))
        self.assertEqual(len(op.requests), 3)
        self.assertEqual(sl.calls, [1.0, 1.0], "no sleep after the final 429")

    def test_one_attempt_only(self):
        res, op, sl = self.send(http_error(500), retries=1)
        self.assertEqual(res[:2], (False, 500))
        self.assertEqual((len(op.requests), sl.calls), (1, []))


class Flushing(Base):
    def flush(self, st, sender, url=WEBHOOK, **kw):
        saves = []
        out = []
        res = ds.flush(st, url, NOW, save=lambda: saves.append(json.dumps(st["sent"], sort_keys=True)), sender=sender,
                       out=out.append, **kw)
        return res, saves, out

    def test_posts_oldest_first_and_moves_to_sent(self):
        st = queued("a", "b", "c")
        snd = FakeSender()
        res, saves, _ = self.flush(st, snd)
        self.assertEqual(res, {"sent": 3, "fallback": 0, "failed": 0, "kept": 0, "dry": 0})
        self.assertEqual([p["content"].split("headline ")[1] for _u, p in snd.posts], ["a", "b", "c"])
        self.assertEqual(st["outbox"], [])
        self.assertEqual(set(st["sent"]), {"a", "b", "c"})
        self.assertTrue(all(v == NOW for v in st["sent"].values()))
        self.assertTrue(all(u == WEBHOOK for u, _p in snd.posts))

    def test_state_is_saved_after_every_post(self):
        st = queued("a", "b", "c")
        _res, saves, _ = self.flush(st, FakeSender())
        self.assertEqual(len(saves), 3)
        self.assertEqual([len(json.loads(x)) for x in saves], [1, 2, 3], "a crash after post n cannot lose the record of the first n")

    def test_a_second_flush_sends_nothing(self):
        st = queued("a", "b")
        self.flush(st, FakeSender())
        snd = FakeSender()
        res, _s, _o = self.flush(st, snd)
        self.assertEqual((res["sent"], snd.posts), (0, []))

    def test_every_post_is_banner_first_and_mention_free(self):
        st = queued("a", "b")
        snd = FakeSender()
        self.flush(st, snd)
        for _u, p in snd.posts:
            self.assertTrue(p["content"].startswith(cfg.BANNER))
            self.assertEqual(p["allowed_mentions"], {"parse": []})

    def test_http_400_retries_once_as_plain_text(self):
        st = queued("a")
        snd = FakeSender(fail_with=[400])
        res, _s, _o = self.flush(st, snd)
        self.assertEqual((res["sent"], res["fallback"], res["failed"]), (1, 1, 0))
        self.assertEqual(len(snd.posts), 2)
        self.assertIn("embeds", snd.posts[0][1])
        self.assertNotIn("embeds", snd.posts[1][1])
        self.assertTrue(snd.posts[1][1]["content"].startswith(cfg.BANNER))
        self.assertEqual(st["outbox"], [])

    def test_400_twice_leaves_it_queued_and_goes_on_to_the_next(self):
        st = queued("a", "b")
        snd = FakeSender(fail_with=[400, 400])
        res, _s, _o = self.flush(st, snd)
        self.assertEqual((res["sent"], res["failed"], res["kept"]), (1, 1, 1))
        self.assertEqual([m["key"] for m in st["outbox"]], ["a"])
        self.assertEqual(set(st["sent"]), {"b"})

    def test_stops_when_the_endpoint_is_down_or_refuses_us(self):
        for fail in (500, 503, "net", 401, 403, 404, 429):
            st = queued("a", "b", "c")
            snd = FakeSender(fail_with=[fail])
            res, _s, _o = self.flush(st, snd)
            self.assertEqual(len(snd.posts), 1, f"after {fail} the other messages are not attempted")
            self.assertEqual((res["sent"], res["failed"], res["kept"]), (0, 1, 3), fail)
            self.assertEqual([m["key"] for m in st["outbox"]], ["a", "b", "c"])

    def test_failed_messages_are_retried_by_the_next_run(self):
        st = queued("a", "b")
        self.flush(st, FakeSender(fail_with=[503]))
        snd = FakeSender()
        res, _s, _o = self.flush(st, snd)
        self.assertEqual((res["sent"], st["outbox"]), (2, []))

    def test_no_webhook_keeps_everything_queued(self):
        for url in (None, "", "https://example.com/hook"):
            st = queued("a", "b")
            snd = FakeSender()
            res, saves, _ = self.flush(st, snd, url=url)
            self.assertEqual((res["kept"], res["sent"], snd.posts, saves), (2, 0, [], []))
            self.assertEqual(len(st["outbox"]), 2)

    def test_an_empty_outbox_needs_no_webhook(self):
        st = S.new_state()
        self.assertEqual(self.flush(st, FakeSender(), url=None)[0], {"sent": 0, "fallback": 0, "failed": 0, "kept": 0, "dry": 0})

    def test_dry_run_prints_and_changes_nothing(self):
        st = queued("a", "b")
        before = json.dumps(st, sort_keys=True)
        snd = FakeSender()
        res, saves, out = self.flush(st, snd, dry_run=True)
        self.assertEqual((res["dry"], res["sent"], snd.posts, saves), (2, 0, [], []))
        self.assertEqual(json.dumps(st, sort_keys=True), before)
        self.assertEqual(len(out), 4)
        self.assertIn("a", out[0])
        self.assertTrue(out[1].startswith(cfg.BANNER))

    def test_dry_run_needs_no_webhook(self):
        st = queued("a")
        res, _s, out = self.flush(st, FakeSender(), url=None, dry_run=True)
        self.assertEqual(res["dry"], 1)

    def test_default_sender_is_the_real_one_and_never_called_here(self):
        import inspect
        self.assertIs(inspect.signature(ds.flush).parameters["sender"].default, ds.send)


if __name__ == "__main__":
    unittest.main()
