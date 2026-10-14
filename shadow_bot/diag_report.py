"""SHADOW - NOT A TRADE. Summarise diag_log.jsonl: the "one-week data check" in one command. Standard library only.

    python diag_report.py                 # every logged run
    python diag_report.py --last 10       # the ten most recent closing-phase runs
    python diag_report.py --root path     # a checkout somewhere else

It answers, from what the job itself recorded at 16:20 ET: did the Cboe files hold the closing print when fetched or did the job
have to wait, how old were they, which spot source was used, were deltas and two-sided quotes present, did the last print agree
with the official close, how far were the provisional index quotes from the published closes, and what went wrong.
It reads files only; it never touches the network and never writes."""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from collections import Counter

import config as cfg

SYMS = ("SPY", "GLD", "TLT")


def load(root: str) -> list:
    path = os.path.join(root, cfg.DIAG_FILE)
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue                                    # a torn last line is not worth failing for
    return out


def med(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def fmt(x, nd=1, unit=""):
    return "n/a" if x is None else f"{x:.{nd}f}{unit}"


def analyse(records: list, last: int = 0) -> dict:
    """-> a plain dict with the numbers the report prints (also what the tests check)."""
    close = [r for r in records if r.get("phase") == "close" and not r.get("dry_run")]
    if last:
        close = close[-last:]
    res: dict = {"runs": len(close), "sessions": len({r["session"] for r in close}), "intraday_runs": sum(
        1 for r in records if r.get("phase") == "intraday"), "per_symbol": {}, "problems": Counter(), "waits": [], "spot_src": Counter()}
    for r in close:
        for p in r.get("problems") or []:
            res["problems"][p] += 1
        w = r.get("snapshot_wait")
        if w:
            res["waits"].append(w)
    for sym in SYMS:
        feeds = [r["feed"][sym] for r in close if sym in (r.get("feed") or {})]
        ok = [f for f in feeds if f.get("ok")]
        gaps = [abs(f["und_last"] / f["und_close"] - 1.0) for f in ok if f.get("und_last") and f.get("und_close")]
        share = [f["n_two_sided"] / f["n_clean"] for f in ok if f.get("n_clean")]
        for f in ok:
            res["spot_src"][f.get("spot_src")] += 1
        res["per_symbol"][sym] = {
            "fetched": len(feeds), "ok": len(ok), "failed": len(feeds) - len(ok),
            "stale": sum(1 for f in feeds if f.get("stale")),
            "file_age_min_median": med([f.get("file_age_min") for f in ok]),
            "file_age_min_max": max([f["file_age_min"] for f in ok if f.get("file_age_min") is not None], default=None),
            "no_delta_rows": sum(f.get("n_no_delta") or 0 for f in ok),
            "crossed_rows": sum(f.get("n_crossed_raw") or 0 for f in ok),
            "two_sided_share_median": med(share),
            "last_vs_close_max": max(gaps, default=None),
            "last_vs_close_warn": sum(1 for g in gaps if g > cfg.SPOT_VS_CLOSE_WARN),
            "last_trade_et_latest": max([f["und_last_trade_et"] for f in ok if f.get("und_last_trade_et")], default=None),
        }
    res["waited_runs"] = sum(1 for w in res["waits"] if w.get("polls"))
    res["never_fresh_runs"] = sum(1 for w in res["waits"] if w.get("still_stale"))
    res["wait_s_median"] = med([w.get("waited_s") for w in res["waits"] if w.get("polls")])
    return res


def prov_vs_pub(root: str) -> dict:
    """Mean |published - provisional| per index from the state file's notes (the weekly summary prints the same)."""
    path = os.path.join(root, cfg.STATE_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            diffs = (json.load(f).get("notes") or {}).get("prov_vs_pub") or []
    except (OSError, ValueError):
        return {}
    by: dict = {}
    for d in diffs:
        by.setdefault(d["name"], []).append(abs(d["pub"] - d["prov"]))
    return {k: (sum(v) / len(v), len(v)) for k, v in sorted(by.items())}


def render(res: dict, pvp: dict) -> str:
    L = [f"SHADOW - NOT A TRADE | data check from {cfg.DIAG_FILE}",
         f"closing-phase runs: {res['runs']} over {res['sessions']} session(s); intraday (diagnostic-only) runs: {res['intraday_runs']}"]
    if not res["runs"]:
        L.append("no closing-phase runs logged yet (a run before 16:05 ET is 'intraday' and only records diagnostics)")
        return "\n".join(L)
    n_wait = len(res["waits"])
    L.append("")
    L.append("Closing snapshot")
    L.append(f"  runs that had to wait for a fresher file : {res['waited_runs']} of {n_wait} (median wait {fmt(res['wait_s_median'], 0, ' s')})")
    L.append(f"  runs where the file never caught up      : {res['never_fresh_runs']} of {n_wait}")
    L.append(f"  spot source used                         : " + (", ".join(f"{k} x{v}" for k, v in sorted(res['spot_src'].items())) or "n/a"))
    for sym, s in res["per_symbol"].items():
        if not s["fetched"]:
            continue
        L.append(f"  {sym}: fetched {s['fetched']}, failed {s['failed']}, stale {s['stale']}; file age median {fmt(s['file_age_min_median'])} / max "
                 f"{fmt(s['file_age_min_max'])} min; two-sided quotes {fmt(None if s['two_sided_share_median'] is None else 100 * s['two_sided_share_median'], 0, '%')}"
                 f"; rows without delta {s['no_delta_rows']}; crossed {s['crossed_rows']}; last-vs-close max "
                 f"{fmt(None if s['last_vs_close_max'] is None else 100 * s['last_vs_close_max'], 2, '%')} ({s['last_vs_close_warn']} above "
                 f"{100 * cfg.SPOT_VS_CLOSE_WARN:.1f}%)")
    L.append("")
    L.append("Provisional index quote vs published close (mean |diff|): " + (
        ", ".join(f"{k} {v[0]:.2f} (n={v[1]})" for k, v in pvp.items()) or "none yet (needs the next day's publication)"))
    L.append("")
    L.append("Problems logged")
    if res["problems"]:
        for p, c in res["problems"].most_common(12):
            L.append(f"  x{c}  {p}")
    else:
        L.append("  none")
    L.append("")
    L.append("How to read it")
    L.append("  * 'had to wait' on most days, or any 'never caught up': the feed lags the 16:20 target; see docs/OPERATIONS.md (moving the target).")
    L.append("  * rows without delta: the REAL strikes would be picked on Black-Scholes deltas from the feed IV (alt-selection is logged per entry).")
    L.append("  * last-vs-close above the threshold on GLD / TLT: the closing print was missing; consider CLOSE_PRINT_TOL_MIN in config.py.")
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="diag_report.py", description="Summarise diag_log.jsonl (the one-week data check).")
    ap.add_argument("--root", default=".")
    ap.add_argument("--last", type=int, default=0, help="only the N most recent closing-phase runs")
    a = ap.parse_args(argv)
    print(render(analyse(load(a.root), a.last), prov_vs_pub(a.root)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
