#!/usr/bin/env python3
"""Shrink a saved Cboe delayed-quotes options file to the contracts the three shadow strategies can ever look at.

    python trim_cboe_file.py SYMBOL SESSION_DATE IN.json[.gz] OUT.json.gz

Keeps every top-level field exactly as published (timestamp, current_price, close, last_trade_time, ...) and drops contracts that
no selection rule can reach:

    SPY  expiries 0-5 days out   (C9: next-trading-day condor)      strikes within 6% of spot, calls and puts
    SPY  expiries 45-80 days out (C3: 50-75 DTE put spread)         puts only, strikes 0.78-1.00 x spot
    GLD / TLT  expiries 20-40 days out (C6: 23-37 DTE put spread)   puts only, strikes 0.75-1.02 x spot

The windows are wider than the rules' own (C9 shorts sit about 1% out and the wings 1% beyond; C3 uses 0.95 and 0.85 x spot; C6
uses a 0.25-delta put and a 2%-wide wing), so the selections on the trimmed file equal the selections on the full file;
tests/test_real_data.py checks that against the numbers recorded when the fixture was made. Standard library only.
"""
import datetime as dt
import gzip
import json
import re
import sys

OCC = re.compile(r"^([A-Z]+\d?)(\d{6})([CP])(\d{8})$")


def keep(symbol: str, session: dt.date, spot: float, name: str) -> bool:
    m = OCC.match(name or "")
    if not m or m.group(1) != symbol:
        return False
    exp = dt.datetime.strptime(m.group(2), "%y%m%d").date()
    cp, k = m.group(3), int(m.group(4)) / 1000.0
    dte = (exp - session).days
    if symbol == "SPY":
        if 0 <= dte <= 5:
            return abs(k / spot - 1.0) <= 0.06
        if 45 <= dte <= 80:
            return cp == "P" and 0.78 * spot <= k <= 1.00 * spot
        return False
    if symbol in ("GLD", "TLT"):
        return 20 <= dte <= 40 and cp == "P" and 0.75 * spot <= k <= 1.02 * spot
    return False


def trim(obj: dict, symbol: str, session: dt.date) -> dict:
    data = obj["data"]
    spot = float(data["current_price"])
    out = dict(obj)
    out["data"] = dict(data)
    out["data"]["options"] = [o for o in data["options"] if keep(symbol, session, spot, o.get("option"))]
    return out


def read_json(path: str) -> dict:
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rb") as f:
        return json.loads(f.read())


def main(argv) -> int:
    if len(argv) != 4:
        print(__doc__)
        return 2
    symbol, session, src, dst = argv[0], dt.date.fromisoformat(argv[1]), argv[2], argv[3]
    obj = read_json(src)
    small = trim(obj, symbol, session)
    with gzip.open(dst, "wb", compresslevel=9) as f:
        f.write(json.dumps(small, separators=(",", ":")).encode())
    print(f"{symbol}: {len(obj['data']['options'])} -> {len(small['data']['options'])} contracts, written to {dst}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
