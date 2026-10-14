# Real feed recording, Tuesday 2026-10-13, 16:20 ET

What the live sources returned when the job was first run against them: the day the bots were built, at 16:20:01 ET, one
session after the seeds in `data/` end. Nothing here is synthetic. It is read by `tests/test_real_data.py` and by nothing else.

| File | Source | Kept as |
|---|---|---|
| `options_SPY.json.gz`, `options_GLD.json.gz`, `options_TLT.json.gz` | `cdn.cboe.com/api/global/delayed_quotes/options/<SYM>.json` | the full file, reduced by `trim_cboe_file.py` (13,172 / 7,920 / 3,022 contracts down to 1,174 / 136 / 71) to what the three strategies read: the next-day, 30-day and 60-day expiries, around the strikes they pick. The full files and the trimmed ones gave identical tickets when this fixture was built. |
| `quote_VIX.json`, `quote_VIX3M.json`, `quote_GVZ.json`, `quote_VXTLT.json` | `.../delayed_quotes/quotes/_<NAME>.json` | unchanged |
| `history_<NAME>.csv` | `cdn.cboe.com/api/global/delayed_quotes/..` daily history CSVs | header plus the last 400 rows (the newest is 2026-10-12) |
| `nasdaq_SPY.json`, `nasdaq_GLD.json`, `nasdaq_TLT.json` | `api.nasdaq.com/api/quote/<SYM>/historical` | unchanged (newest row 2026-10-12) |
| `treasury_bills_2025.csv`, `treasury_bills_2026.csv` | `home.treasury.gov` daily bill rates | unchanged (the 2026-10-13 row is already there at 16:00: 13-week bank discount 4.09) |
| `data/` | this repository's `data/` seeds | a copy as of the close of 2026-10-12, so the replay does not move when the deployed bot extends its own seeds |

Not recorded, because the sources refused at the time: Yahoo Finance (HTTP 429 on every try) and FRED (connection closed with no
answer). The replay therefore also exercises the fallbacks (Nasdaq for closes, the Treasury CSV for the bill rate).

What the recording shows about the feeds (details in `docs/OPERATIONS.md`, section 10): a file's `timestamp` is its generation time and
the data inside is 15 minutes older; the closing print (underlying `last_trade_time` at 16:00:00 for SPY, 15:59:59 for GLD and TLT, and
a `close` that differs from the last price) is in files generated from about 16:15 ET; SPY options keep trading to 16:15 ET while GLD
and TLT options stop at 16:00.

## Refreshing it

Only if a feed's format changes (the test then fails on the parser, which is the point). Run the job once after 16:15 ET on a trading
day with every HTTP body saved under these names (`data_feed.FixtureHttp.name_for` gives the name for a URL), trim the three options
files with `trim_cboe_file.py`, replace the numbers in `GOLDEN` in `tests/test_real_data.py`, and check by hand that each ticket still
follows the frozen rules (`docs/DESIGN.md` states them) before accepting the new numbers.
