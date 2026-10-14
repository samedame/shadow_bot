"""Network layer (data_feed.py) with no network: parsers on samples in each source's real format, the HTTP retry wrapper, the
fallback orders (raw closes: Yahoo -> Nasdaq -> Stooq; T-bill: FRED -> Treasury) and the fixture server."""
import datetime as dt
import http.client
import io
import json
import os
import unittest
import urllib.error
from unittest import mock

from tests.helpers import (Base, D, ET, UTC, bs_rows, cboe_options_obj, cboe_quote_obj, et, history_csv, nasdaq_json,
                           treasury_csv)

import config as cfg
import data_feed as df

NOW = dt.datetime(2026, 10, 13, 20, 25, tzinfo=UTC)          # 16:25 ET
SESSION = D("2026-10-13")


def yahoo_json(rows) -> bytes:
    ts = [int(dt.datetime.combine(d, dt.time(9, 30), tzinfo=ET).timestamp()) for d, _c in rows]
    return json.dumps({"chart": {"result": [{"timestamp": ts, "indicators": {"quote": [{"close": [c for _d, c in rows]}]}}],
                                 "error": None}}).encode()


def stooq_csv(rows) -> str:
    out = io.StringIO()
    out.write("Date,Open,High,Low,Close,Volume\n")
    for d, c in rows:
        out.write(f"{d.isoformat()},1,1,1,{c},100\n")
    return out.getvalue()


def fred_csv(rows) -> str:
    return "observation_date,DTB3\n" + "".join(f"{d.isoformat()},{v}\n" for d, v in rows)


class FakeHttp:
    """http double. routes: [(url substring, outcome)], first match wins. An outcome is bytes / str (the body), an exception to
    raise, or a list of such (consumed one per call, the last one repeated)."""

    def __init__(self, *routes):
        self.routes = [[k, v] for k, v in routes]
        self.calls: list = []

    def __call__(self, url, **kw):
        self.calls.append((url, kw))
        for r in self.routes:
            if r[0] in url:
                out = r[1]
                if isinstance(out, list):
                    out = out.pop(0) if len(out) > 1 else out[0]
                if isinstance(out, BaseException):
                    raise out
                return out.encode() if isinstance(out, str) else out
        raise df.FeedError(f"no route for {url}")


def feed(*routes):
    http = FakeHttp(*routes)
    sleeps: list = []
    return df.Feed(http=http, sleep=sleeps.append), http, sleeps


class HttpGet(unittest.TestCase):
    class Resp:
        def __init__(self, body):
            self.body = body

        def read(self):
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def run_get(self, outcomes, **kw):
        outcomes = list(outcomes)
        seen = []

        def opener(req, timeout=None):
            seen.append((req, timeout))
            o = outcomes.pop(0)
            if isinstance(o, BaseException):
                raise o
            return self.Resp(o)

        sleeps: list = []
        with mock.patch("urllib.request.urlopen", opener), mock.patch("sys.stderr", io.StringIO()):
            try:
                body = df.http_get("https://example.test/x", sleep=sleeps.append, **kw)
            except df.FeedError as e:
                body = e
        return body, seen, sleeps

    def test_success_sends_the_generic_browser_agent(self):
        body, seen, sleeps = self.run_get([b"hello"])
        self.assertEqual(body, b"hello")
        req, timeout = seen[0]
        h = {k.lower(): v for k, v in req.header_items()}
        self.assertEqual(h["user-agent"], cfg.USER_AGENT)
        self.assertEqual(h["accept-encoding"], "identity")
        self.assertEqual(timeout, cfg.FETCH_TIMEOUT_S)
        self.assertEqual(sleeps, [])
        self.assertNotIn("@", h["user-agent"])

    def test_extra_headers_are_added(self):
        _b, seen, _s = self.run_get([b"x"], headers={"Accept": "application/json"})
        self.assertEqual({k.lower(): v for k, v in seen[0][0].header_items()}["accept"], "application/json")

    def test_retries_with_backoff_then_succeeds(self):
        body, seen, sleeps = self.run_get([urllib.error.URLError("dns"), OSError("reset"), b"ok"])
        self.assertEqual(body, b"ok")
        self.assertEqual(len(seen), 3)
        self.assertEqual(sleeps, [1, 3])

    def test_http_errors_are_retried_too(self):
        err = urllib.error.HTTPError("https://example.test/x", 503, "unavailable", {}, io.BytesIO(b""))
        body, seen, sleeps = self.run_get([err, b"ok"])
        self.assertEqual((body, len(seen), sleeps), (b"ok", 2, [1]))

    def test_gives_up_with_a_feed_error(self):
        body, seen, sleeps = self.run_get([urllib.error.URLError("a"), urllib.error.URLError("b"), urllib.error.URLError("c")])
        self.assertIsInstance(body, df.FeedError)
        self.assertEqual((len(seen), sleeps), (3, [1, 3]))
        self.assertIn("example.test/x", str(body))

    def test_an_empty_body_counts_as_a_failure(self):
        body, seen, _s = self.run_get([b"", b"", b""])
        self.assertIsInstance(body, df.FeedError)
        self.assertEqual(len(seen), 3)

    def test_a_body_cut_short_is_retried_like_any_other_network_failure(self):
        """A 6 MB options file whose connection drops mid-transfer raises http.client.IncompleteRead from read(); it is not an
        OSError, so it used to escape the retry loop and crash the whole job."""
        cut = http.client.IncompleteRead(b"partial", 1000)
        body, seen, sleeps = self.run_get([cut, http.client.RemoteDisconnected("closed"), b"ok"])
        self.assertEqual((body, len(seen), sleeps), (b"ok", 3, [1, 3]))
        body, seen, _s = self.run_get([cut, cut, cut])
        self.assertIsInstance(body, df.FeedError)
        self.assertEqual(len(seen), 3)


class Parsers(unittest.TestCase):
    def test_cboe_history_vix_style(self):
        text = "DATE,OPEN,HIGH,LOW,CLOSE\n10/09/2026,16.1,16.5,15.9,16.30\n10/12/2026,16.3,17.0,16.2,16.85\n"
        self.assertEqual(df.parse_cboe_history(text), [(D("2026-10-09"), 16.30), (D("2026-10-12"), 16.85)])

    def test_cboe_history_single_column_style(self):
        text = "DATE,GVZ\n10/12/2026,17.97\n10/09/2026,17.5\n"
        self.assertEqual(df.parse_cboe_history(text), [(D("2026-10-09"), 17.5), (D("2026-10-12"), 17.97)], "sorted ascending")

    def test_cboe_history_skips_bad_rows_and_keeps_the_last_duplicate(self):
        text = "DATE,OPEN,HIGH,LOW,CLOSE\n10/09/2026,1,1,1,16.0\nnot a date,1,1,1,1\n10/10/2026,1,1,1,\n10/09/2026,1,1,1,16.2\n"
        self.assertEqual(df.parse_cboe_history(text), [(D("2026-10-09"), 16.2)])

    def test_cboe_history_wrong_header(self):
        with self.assertRaises(ValueError):
            df.parse_cboe_history("Day,Value\n1,2\n")
        with self.assertRaises(ValueError):
            df.parse_cboe_history("")

    def test_fred(self):
        text = "observation_date,DTB3\n2026-10-08,4.07\n2026-10-09,4.06\n2026-10-12,.\n2026-10-13,\nbad,row\n"
        self.assertEqual(df.parse_fred(text), [(D("2026-10-08"), 4.07), (D("2026-10-09"), 4.06)])
        with self.assertRaises(ValueError):
            df.parse_fred("onlyone\n")
        self.assertEqual(df.parse_fred("observation_date,DTB3\n"), [])

    def test_treasury_uses_the_bank_discount_column_not_the_coupon_equivalent(self):
        text = treasury_csv([(D("2026-10-08"), 4.05), (D("2026-10-09"), 4.06)])
        self.assertIn("13 WEEKS COUPON EQUIVALENT", text)
        self.assertEqual(df.parse_treasury_bills(text), [(D("2026-10-08"), 4.05), (D("2026-10-09"), 4.06)])

    def test_treasury_header_match_is_case_insensitive_and_rows_sorted(self):
        text = 'date,"13 weeks bank discount"\n10/09/2026,4.06\n10/08/2026,4.05\n10/07/2026,N/A\n'
        self.assertEqual(df.parse_treasury_bills(text), [(D("2026-10-08"), 4.05), (D("2026-10-09"), 4.06)])

    def test_treasury_without_the_column_is_an_error(self):
        with self.assertRaises(ValueError):
            df.parse_treasury_bills('"Date","13 WEEKS COUPON EQUIVALENT"\n"10/09/2026","4.25"\n')
        with self.assertRaises(ValueError):
            df.parse_treasury_bills("")

    def test_yahoo(self):
        rows = [(D("2026-10-08"), 780.1), (D("2026-10-09"), None), (D("2026-10-12"), 778.1)]
        self.assertEqual(df.parse_yahoo_chart(yahoo_json(rows)), [(D("2026-10-08"), 780.1), (D("2026-10-12"), 778.1)])

    def test_yahoo_bar_date_is_the_new_york_date(self):
        ts = int(dt.datetime(2026, 10, 13, 1, 0, tzinfo=UTC).timestamp())               # 21:00 ET on the 12th
        raw = json.dumps({"chart": {"result": [{"timestamp": [ts], "indicators": {"quote": [{"close": [5.0]}]}}]}}).encode()
        self.assertEqual(df.parse_yahoo_chart(raw), [(D("2026-10-12"), 5.0)])

    def test_yahoo_errors(self):
        for raw in (b"{}", b'{"chart": {"result": null}}', b"not json", b'{"chart": {"result": []}}'):
            with self.assertRaises((ValueError, KeyError, TypeError, IndexError)):
                df.parse_yahoo_chart(raw)

    def test_nasdaq(self):
        raw = nasdaq_json([(D("2026-10-12"), 1234.5), (D("2026-10-09"), 778.1)]).encode()
        self.assertEqual(df.parse_nasdaq_hist(raw), [(D("2026-10-09"), 778.1), (D("2026-10-12"), 1234.5)])

    def test_nasdaq_skips_junk_and_non_positive_closes(self):
        raw = json.dumps({"data": {"tradesTable": {"rows": [
            {"date": "10/12/2026", "close": "$778.10"}, {"date": "10/09/2026", "close": "N/A"}, {"date": "bad", "close": "$1"},
            {"date": "10/08/2026", "close": "$0.00"}, {"date": "10/07/2026"}]}}}).encode()
        self.assertEqual(df.parse_nasdaq_hist(raw), [(D("2026-10-12"), 778.10)])

    def test_nasdaq_no_rows(self):
        for raw in (b'{"data": null}', b'{"data": {"tradesTable": {"rows": []}}}', b'{"data": {"tradesTable": null}}', b"{}"):
            with self.assertRaises(ValueError):
                df.parse_nasdaq_hist(raw)

    def test_stooq(self):
        text = stooq_csv([(D("2026-10-09"), 778.1), (D("2026-10-08"), 780.0)]) + "garbage,row\nN/D\n"
        self.assertEqual(df.parse_stooq(text), [(D("2026-10-08"), 780.0), (D("2026-10-09"), 778.1)])
        self.assertEqual(df.parse_stooq("No data"), [])


class Chains(Base):
    def spy_obj(self):
        rows = bs_rows(700.0, SESSION, [SESSION + dt.timedelta(days=1)], [float(k) for k in range(680, 721)], vol=0.14)
        return cboe_options_obj("SPY", rows, 700.0, last_trade=et("2026-10-13 16:00"), file_ts_utc=NOW, close=700.0)

    def test_chain_is_parsed_and_cleaned(self):
        f, http, sleeps = feed(("options/SPY.json", json.dumps(self.spy_obj())))
        ch = f.chain("SPY", SESSION, NOW)
        self.assertEqual((ch.symbol, ch.spot, ch.asof), ("SPY", 700.0, SESSION))
        self.assertGreater(ch.n_rows, 50)
        self.assertEqual(http.calls[0][0], cfg.CBOE_CHAIN_URL.format(symbol="SPY"))
        self.assertEqual(sleeps, [])

    def test_a_network_failure_is_not_retried_here_because_the_http_layer_already_did(self):
        f, http, sleeps = feed(("options/SPY.json", df.FeedError("down")))
        with self.assertRaises(df.FeedError) as cm:
            f.chain("SPY", SESSION, NOW)
        self.assertIn("chain SPY", str(cm.exception))
        self.assertEqual((len(http.calls), sleeps), (1, []))

    def test_an_unusable_file_is_fetched_again_then_reported(self):
        f, http, sleeps = feed(("options/SPY.json", '{"timestamp": "2026-10-13 20:25:00", "data": {"options": [], "current_price": 700}}'))
        with mock.patch("sys.stderr", io.StringIO()), self.assertRaises(df.FeedError):
            f.chain("SPY", SESSION, NOW)
        self.assertEqual((len(http.calls), sleeps), (3, [2, 5]))

    def test_garbage_json_is_retried_and_can_recover(self):
        f, http, sleeps = feed(("options/SPY.json", ["{not json", json.dumps(self.spy_obj())]))
        with mock.patch("sys.stderr", io.StringIO()):
            ch = f.chain("SPY", SESSION, NOW)
        self.assertEqual((ch.symbol, len(http.calls), sleeps), ("SPY", 2, [2]))

    def test_wrong_shape_is_a_clean_error(self):
        f, _http, _s = feed(("options/SPY.json", '{"data": []}'))
        with mock.patch("sys.stderr", io.StringIO()), self.assertRaises(df.FeedError):
            f.chain("SPY", SESSION, NOW)


class IndexQuotes(unittest.TestCase):
    def test_vix_quote(self):
        obj = cboe_quote_obj("VIX", 16.42, et("2026-10-13 16:15"), NOW)
        f, http, _s = feed(("quotes/_VIX.json", json.dumps(obj)))
        q = f.index_quote("VIX")
        self.assertEqual(q["value"], 16.42)
        self.assertEqual(q["last_trade_et"], "2026-10-13T16:15:00-04:00")
        self.assertEqual(q["file_ts_utc"], "2026-10-13 20:25:00")
        self.assertEqual(http.calls[0][0], cfg.CBOE_QUOTE_URL.format(name="VIX"))

    def test_central_time_offsets_are_converted_to_eastern(self):
        """GVZ and VXTLT quotes carry an offset ('...T15:15:00.402000-05:00' is 16:15 in New York)."""
        lt = dt.datetime(2026, 10, 13, 15, 15, 0, 402000, tzinfo=dt.timezone(dt.timedelta(hours=-5)))
        obj = cboe_quote_obj("GVZ", 22.5, lt, NOW, offset_style=True)
        f, _h, _s = feed(("quotes/_GVZ.json", json.dumps(obj)))
        self.assertEqual(f.index_quote("GVZ")["last_trade_et"], "2026-10-13T16:15:00-04:00")

    def test_no_price_is_an_error(self):
        for body in ('{"data": {"current_price": 0}}', '{"data": {}}', '{"data": {"current_price": "x"}}', "[]", "not json"):
            f, _h, _s = feed(("quotes/_VIX.json", body))
            with self.assertRaises(df.FeedError, msg=body):
                f.index_quote("VIX")

    def test_a_missing_trade_time_is_tolerated(self):
        f, _h, _s = feed(("quotes/_VIX.json", '{"data": {"current_price": 16.4, "close": 16.0}}'))
        q = f.index_quote("VIX")
        self.assertEqual((q["value"], q["close"], q["last_trade_et"]), (16.4, 16.0, None))


class IndexHistory(unittest.TestCase):
    def test_ok(self):
        f, http, _s = feed(("VIX_History.csv", history_csv([(D("2026-10-09"), 16.3), (D("2026-10-12"), 16.85)])))
        self.assertEqual(f.index_history("VIX"), [(D("2026-10-09"), 16.3), (D("2026-10-12"), 16.85)])
        self.assertEqual(http.calls[0][0], cfg.CBOE_HISTORY_URL.format(name="VIX"))

    def test_single_column_history(self):
        f, _h, _s = feed(("GVZ_History.csv", history_csv([(D("2026-10-12"), 17.97)], name="GVZ")))
        self.assertEqual(f.index_history("GVZ"), [(D("2026-10-12"), 17.97)])

    def test_failures(self):
        for out in (df.FeedError("down"), "DATE,OPEN,HIGH,LOW,CLOSE\n", "<html>blocked</html>"):
            f, _h, _s = feed(("VIX_History.csv", out))
            with self.assertRaises(df.FeedError):
                f.index_history("VIX")


class TBill(unittest.TestCase):
    FRED = ("fredgraph.csv", fred_csv([(D("2026-10-08"), 4.07), (D("2026-10-09"), 4.06)]))

    def test_fred_first(self):
        f, http, _s = feed(self.FRED)
        rows, src = f.tbill(SESSION)
        self.assertEqual((src, rows[-1]), ("fred", (D("2026-10-09"), 4.06)))
        self.assertEqual(len(http.calls), 1)

    def test_treasury_when_fred_is_down_reads_this_year_and_last(self):
        t25 = treasury_csv([(D("2025-12-30"), 4.30)])
        t26 = treasury_csv([(D("2026-10-08"), 4.05), (D("2026-10-09"), 4.06)])
        f, http, _s = feed(("fredgraph.csv", df.FeedError("down")), ("treasury_bill_rates&field_tdr_date_value=2025", t25),
                           ("treasury_bill_rates&field_tdr_date_value=2026", t26))
        rows, src = f.tbill(SESSION)
        self.assertEqual(src, "treasury")
        self.assertEqual(rows, [(D("2025-12-30"), 4.30), (D("2026-10-08"), 4.05), (D("2026-10-09"), 4.06)])
        self.assertEqual(rows[-1][1], 4.06, "the bank-discount value, not the +0.19 coupon-equivalent one")
        self.assertEqual(len(http.calls), 3)

    def test_fred_with_no_rows_falls_back(self):
        f, _h, _s = feed(("fredgraph.csv", "observation_date,DTB3\n"),
                         ("treasury_bill_rates&field_tdr_date_value=2026", treasury_csv([(D("2026-10-09"), 4.06)])),
                         ("treasury_bill_rates&field_tdr_date_value=2025", df.FeedError("no file")))
        rows, src = f.tbill(SESSION)
        self.assertEqual((src, rows), ("treasury", [(D("2026-10-09"), 4.06)]))

    def test_last_years_file_may_be_missing_but_this_years_may_not(self):
        f, _h, _s = feed(("fredgraph.csv", df.FeedError("down")), ("treasury_bill_rates&field_tdr_date_value=2025", df.FeedError("x")),
                         ("treasury_bill_rates&field_tdr_date_value=2026", df.FeedError("y")))
        with self.assertRaises(df.FeedError) as cm:
            f.tbill(SESSION)
        self.assertIn("FRED:", str(cm.exception))
        self.assertIn("Treasury:", str(cm.exception))

    def test_both_down(self):
        f, _h, _s = feed()
        with self.assertRaises(df.FeedError):
            f.tbill(SESSION)

    def test_a_bad_fred_file_falls_back_too(self):
        f, _h, _s = feed(("fredgraph.csv", "<html>"), ("treasury_bill_rates&field_tdr_date_value=2026", treasury_csv([(D("2026-10-09"), 4.06)])),
                         ("treasury_bill_rates&field_tdr_date_value=2025", df.FeedError("x")))
        self.assertEqual(f.tbill(SESSION)[1], "treasury")


class Closes(unittest.TestCase):
    YAHOO = ("v8/finance/chart/SPY", yahoo_json([(D("2026-10-12"), 778.1), (D("2026-10-13"), 780.4)]))
    NASDAQ = ("api.nasdaq.com/api/quote/SPY/historical", nasdaq_json([(D("2026-10-12"), 778.1), (D("2026-10-13"), 780.4)]))
    STOOQ = ("stooq.com/q/d/l/?s=spy.us", stooq_csv([(D("2026-09-01"), 650.0), (D("2026-10-12"), 778.1), (D("2026-10-13"), 780.4)]))

    def test_yahoo_first(self):
        f, http, _s = feed(self.YAHOO, self.NASDAQ, self.STOOQ)
        rows, src = f.closes("SPY", 20, NOW)
        self.assertEqual((src, rows), ("yahoo", [(D("2026-10-12"), 778.1), (D("2026-10-13"), 780.4)]))
        self.assertEqual(len(http.calls), 1)
        url = http.calls[0][0]
        p2 = int(NOW.timestamp()) + 86400
        self.assertIn(f"period2={p2}", url)
        self.assertIn(f"period1={p2 - 23 * 86400}", url)

    def test_nasdaq_when_yahoo_is_down(self):
        f, http, _s = feed(("v8/finance/chart", df.FeedError("429")), self.NASDAQ, self.STOOQ)
        rows, src = f.closes("SPY", 20, NOW)
        self.assertEqual(src, "nasdaq")
        self.assertEqual(len(http.calls), 2)
        url, kw = http.calls[1]
        self.assertIn("fromdate=2026-09-20&", url)          # 20 + 3 days before the New York date
        self.assertIn("todate=2026-10-13", url)
        self.assertEqual(kw["headers"], {"Accept": "application/json, text/plain, */*"})

    def test_stooq_last_and_trimmed_to_the_window(self):
        f, http, _s = feed(("v8/finance/chart", df.FeedError("429")), ("api.nasdaq.com", df.FeedError("blocked")), self.STOOQ)
        rows, src = f.closes("SPY", 20, NOW)
        self.assertEqual(src, "stooq")
        self.assertEqual(rows, [(D("2026-10-12"), 778.1), (D("2026-10-13"), 780.4)], "September 1st is outside the 23-day window")
        self.assertEqual(len(http.calls), 3)

    def test_empty_answers_fall_through(self):
        f, _h, _s = feed(("v8/finance/chart", yahoo_json([(D("2026-10-12"), None)])), ("api.nasdaq.com", '{"data": null}'), self.STOOQ)
        self.assertEqual(f.closes("SPY", 20, NOW)[1], "stooq")

    def test_all_three_down_names_each_source(self):
        f, _h, _s = feed()
        with self.assertRaises(df.FeedError) as cm:
            f.closes("SPY", 20, NOW)
        msg = str(cm.exception)
        for name in ("yahoo", "nasdaq", "stooq"):
            self.assertIn(name, msg)

    def test_stooq_with_only_old_rows_counts_as_no_rows(self):
        f, _h, _s = feed(("stooq.com", stooq_csv([(D("2026-01-02"), 600.0)])))
        with self.assertRaises(df.FeedError) as cm:
            f.closes("SPY", 20, NOW)
        self.assertIn("stooq: no rows", str(cm.exception))

    def test_symbols_are_substituted_per_source(self):
        f, http, _s = feed(("v8/finance/chart/GLD", df.FeedError("x")), ("api.nasdaq.com/api/quote/GLD/historical", df.FeedError("x")),
                           ("stooq.com/q/d/l/?s=gld.us", stooq_csv([(D("2026-10-13"), 380.0)])))
        self.assertEqual(f.closes("GLD", 20, NOW)[1], "stooq")
        self.assertTrue(all("GLD" in u or "gld" in u for u, _k in http.calls))


class Fixtures(Base):
    URLS = {
        "options_SPY.json": cfg.CBOE_CHAIN_URL.format(symbol="SPY"),
        "options_GLD.json": cfg.CBOE_CHAIN_URL.format(symbol="GLD"),
        "quote_VIX.json": cfg.CBOE_QUOTE_URL.format(name="VIX"),
        "quote_VIX3M.json": cfg.CBOE_QUOTE_URL.format(name="VIX3M"),
        "quote_VXTLT.json": cfg.CBOE_QUOTE_URL.format(name="VXTLT"),
        "history_VIX.csv": cfg.CBOE_HISTORY_URL.format(name="VIX"),
        "history_GVZ.csv": cfg.CBOE_HISTORY_URL.format(name="GVZ"),
        "fred_DTB3.csv": cfg.FRED_DTB3_URL,
        "treasury_bills_2026.csv": cfg.TREASURY_BILLS_URL.format(year=2026),
        "yahoo_SPY.json": cfg.YAHOO_CHART_URL.format(symbol="SPY", period1=1, period2=2),
        "nasdaq_TLT.json": cfg.NASDAQ_HIST_URL.format(symbol="TLT", fromdate="2026-09-01", todate="2026-10-13"),
        "stooq_GLD.csv": cfg.STOOQ_URL.format(symbol="gld"),
    }

    def test_every_source_url_maps_to_its_file_name(self):
        fx = df.FixtureHttp(self.tmp())
        for name, url in self.URLS.items():
            self.assertEqual(fx.name_for(url), name, url)

    def test_unknown_urls(self):
        self.assertIsNone(df.FixtureHttp(self.tmp()).name_for("https://example.com/other"))

    def test_serves_files_and_records_calls(self):
        d = self.tmp()
        with open(os.path.join(d, "fred_DTB3.csv"), "w") as f:
            f.write(fred_csv([(D("2026-10-09"), 4.06)]))
        fx = df.FixtureHttp(d)
        fd = df.Feed(http=fx, sleep=lambda s: None)
        self.assertEqual(fd.tbill(SESSION)[0], [(D("2026-10-09"), 4.06)])
        self.assertEqual(fx.calls, ["fred_DTB3.csv"])

    def test_a_missing_file_behaves_like_a_source_that_is_down(self):
        fx = df.FixtureHttp(self.tmp())
        fd = df.Feed(http=fx, sleep=lambda s: None)
        with self.assertRaises(df.FeedError):
            fd.index_history("VIX")
        with self.assertRaises(df.FeedError):
            fd.closes("SPY", 20, NOW)
        self.assertEqual(fx.calls[0], "history_VIX.csv")
        self.assertEqual(fx.calls[1:], ["yahoo_SPY.json", "nasdaq_SPY.json", "stooq_SPY.csv"])

    def test_http_keyword_arguments_are_accepted(self):
        d = self.tmp()
        with open(os.path.join(d, "nasdaq_SPY.json"), "w") as f:
            f.write(nasdaq_json([(D("2026-10-12"), 778.1)]))
        fd = df.Feed(http=df.FixtureHttp(d), sleep=lambda s: None)
        self.assertEqual(fd.closes("SPY", 20, NOW)[1], "nasdaq")


if __name__ == "__main__":
    unittest.main()
