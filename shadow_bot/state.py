"""JSON state (shadow_state.json), the append-only trade log (shadow_log.csv) and the message outbox.

Shadow state holds NO money: positions are would-be trades. It is deliberately separate from the live condor bot's
state.json / pipeline_state.json and is never read by the live combined-drawdown monitor.

Idempotency lives here:
  * entries_done      - position ids already created (a re-run the same day cannot open a second copy)
  * exit_alerts       - "<position id>:<reason>" -> when it was sent (exit alerts once per position per reason)
  * outbox / sent     - messages are queued with a deterministic key BEFORE posting, posted, then moved to `sent`; a failed
                        post stays queued for the next run, a posted one is never re-sent
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import os
from typing import Optional

import config as cfg

KNAME = {0.0: "mid", 0.25: "base", 0.5: "nat", 0.75: "x3"}
NAME_K = {v: k for k, v in KNAME.items()}
KEEP_ENTRIES = 600
KEEP_SENT_DAYS = 45
KEEP_WEEKS = 16


def named_cf(cf: dict) -> dict:
    return {KNAME[k]: round(float(v), 8) for k, v in cf.items()}


def unnamed_cf(cf: dict) -> dict:
    return {NAME_K[k]: float(v) for k, v in cf.items()}


# ----------------------------------------------------------------------------- blocks
def new_strategy_block(sid: str) -> dict:
    b = {
        "status": "active", "primed": False,
        "closed_n": 0, "unpriced_n": 0,
        "cum_pnl_mid": 0.0, "cum_pnl_base": 0.0, "cum_pnl_nat": 0.0,
        "peak": 0.0, "drawdown": 0.0, "max_drawdown": 0.0,
        "recent_pnl_base": [], "recent_pnl_nat": [],
        "open_positions": [], "exit_alerts": {}, "entries_done": [],
        "quality": [], "kill_alerts": {}, "started": None,
    }
    if sid == "C6":
        b["sleeves"] = {tk: {"closed_n": 0, "cum_pnl_base": 0.0, "cum_pnl_nat": 0.0} for tk in cfg.C6["vol_index"]}
        b["model"] = {"open": [], "closed_n": 0, "cum_pnl_base": 0.0, "cum_pnl_nat": 0.0, "recent_pnl_base": [],
                      "sleeves": {tk: {"closed_n": 0, "cum_pnl_base": 0.0} for tk in cfg.C6["vol_index"]}}
        b["pairs"] = []
    if sid == "C3":
        b.update({"premium_paid_since_reset": 0.0, "premium_reset_date": None, "gate_log": [], "candidate": None,
                  "spy_peak": None, "spy_peak_date": None, "episode": None, "hedge_ledger": []})
    return b


def new_state() -> dict:
    return {
        "schema": cfg.SCHEMA,
        "strategies": {sid: new_strategy_block(sid) for sid in cfg.STRATEGIES},
        "last_run": None,
        "outbox": [], "sent": {},
        "skips": {}, "total_skips": {sid: {} for sid in cfg.STRATEGIES},
        "skip_keys": {},              # "<date>|<strategy>|<reason>" -> True: one count per session, however often it is re-run
        "runs": {},                   # ISO week -> number of completed runs (weekly summary: runs this week)
        "last_session": {},           # strategy id -> last session date a closing-phase run processed (missed-run accounting)
        "notes": {},                  # free-form flags, e.g. once-per-day data notices
    }


def load(path: str = cfg.STATE_FILE) -> dict:
    if not os.path.exists(path):
        return new_state()
    with open(path) as f:
        st = json.load(f)
    base = new_state()
    for k, v in base.items():
        st.setdefault(k, v)
    for sid in cfg.STRATEGIES:
        blk = st["strategies"].setdefault(sid, new_strategy_block(sid))
        for k, v in new_strategy_block(sid).items():
            blk.setdefault(k, v)
        st["total_skips"].setdefault(sid, {})
    return st


def save(st: dict, path: str = cfg.STATE_FILE) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=1, sort_keys=False, default=str)
        f.write("\n")
    os.replace(tmp, path)


# ----------------------------------------------------------------------------- positions
def legs_to_json(legs: list, quotes: list, chain=None) -> list:
    out = []
    for (cp, K, ex, q), (bid, ask) in zip(legs, quotes):
        row = chain.row(ex, cp, K) if chain is not None else None
        out.append({
            "label": f"{'BUY' if q > 0 else 'SELL'} {abs(q)} {K:g}{'C' if cp > 0 else 'P'} {ex.isoformat()}",
            "cp": cp, "strike": K, "expiry": ex.isoformat(), "qty": q,
            "bid": bid, "ask": ask, "mid": round(0.5 * (bid + ask), 6),
            "iv": None if row is None else row.get("iv"),
            "delta": None if row is None else row.get("delta"),
            "delta_src": "feed" if (row is not None and row.get("delta") is not None) else "none",
        })
    return out


def legs_from_json(items: list) -> list:
    return [(it["cp"], float(it["strike"]), dt.date.fromisoformat(it["expiry"]), int(it["qty"])) for it in items]


def find_position(block: dict, pid: str) -> Optional[dict]:
    for p in block["open_positions"]:
        if p["id"] == pid:
            return p
    return None


def entry_exists(block: dict, pid: str) -> bool:
    return pid in block["entries_done"] or find_position(block, pid) is not None


def register_entry(block: dict, pid: str) -> None:
    if pid not in block["entries_done"]:
        block["entries_done"].append(pid)
        del block["entries_done"][:-KEEP_ENTRIES]


def mark_exit_alert(block: dict, pid: str, reason: str, at_iso: str) -> bool:
    """True the first time (pid, reason) is marked; False if it was already alerted."""
    key = f"{pid}:{reason}"
    if key in block["exit_alerts"]:
        return False
    block["exit_alerts"][key] = at_iso
    if len(block["exit_alerts"]) > 800:
        for k in sorted(block["exit_alerts"], key=block["exit_alerts"].get)[:200]:
            del block["exit_alerts"][k]
    return True


def update_stats(block: dict, pnl_base: float, pnl_nat: float, pnl_mid: float, sleeve: Optional[str] = None) -> None:
    """Fold one closed trade (by exit date) into the running counters, peak and drawdown."""
    block["closed_n"] += 1
    block["cum_pnl_base"] = round(block["cum_pnl_base"] + pnl_base, 6)
    block["cum_pnl_nat"] = round(block["cum_pnl_nat"] + pnl_nat, 6)
    block["cum_pnl_mid"] = round(block["cum_pnl_mid"] + pnl_mid, 6)
    block["peak"] = max(block["peak"], block["cum_pnl_base"])
    block["drawdown"] = round(block["peak"] - block["cum_pnl_base"], 6)
    block["max_drawdown"] = max(block["max_drawdown"], block["drawdown"])
    block["recent_pnl_base"] = (block["recent_pnl_base"] + [round(pnl_base, 6)])[-cfg.TRAILING_WINDOW:]
    block["recent_pnl_nat"] = (block["recent_pnl_nat"] + [round(pnl_nat, 6)])[-cfg.TRAILING_WINDOW:]
    block["primed"] = block["closed_n"] >= cfg.PRIMED_MIN_TRADES
    if sleeve and "sleeves" in block:
        s = block["sleeves"].setdefault(sleeve, {"closed_n": 0, "cum_pnl_base": 0.0, "cum_pnl_nat": 0.0})
        s["closed_n"] += 1
        s["cum_pnl_base"] = round(s["cum_pnl_base"] + pnl_base, 6)
        s["cum_pnl_nat"] = round(s["cum_pnl_nat"] + pnl_nat, 6)


def update_model_stats(block: dict, pnl_base: float, pnl_nat: float, sleeve: str) -> None:
    m = block["model"]
    m["closed_n"] += 1
    m["cum_pnl_base"] = round(m["cum_pnl_base"] + pnl_base, 6)
    m["cum_pnl_nat"] = round(m["cum_pnl_nat"] + pnl_nat, 6)
    m["recent_pnl_base"] = (m["recent_pnl_base"] + [round(pnl_base, 6)])[-cfg.TRAILING_WINDOW:]
    s = m["sleeves"].setdefault(sleeve, {"closed_n": 0, "cum_pnl_base": 0.0})
    s["closed_n"] += 1
    s["cum_pnl_base"] = round(s["cum_pnl_base"] + pnl_base, 6)


def add_quality(block: dict, date_iso: str, flag: bool, med_rel_spread: Optional[float], why: str = "") -> None:
    block["quality"] = (block["quality"] + [{"date": date_iso, "flag": bool(flag), "med_rel_spread": med_rel_spread, "why": why}])[
        -cfg.QUALITY_WINDOW:]


# ----------------------------------------------------------------------------- skip ledger
def week_key(d: dt.date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def record_skip(st: dict, sid: str, d: dt.date, reason: str, sleeve: Optional[str] = None) -> bool:
    """Count one skipped session. Keyed by (session date, strategy, reason), so a second run of the same session (a manual
    re-run, a backup trigger) cannot count it twice. -> True when it was counted now."""
    label = f"{sleeve}:{reason}" if sleeve else reason
    seen = st.setdefault("skip_keys", {})
    key = f"{d.isoformat()}|{sid}|{label}"
    if key in seen:
        return False
    seen[key] = True
    horizon = (d - dt.timedelta(days=7 * KEEP_WEEKS + 14)).isoformat()
    for k in [k for k in seen if k[:10] < horizon]:
        del seen[k]
    wk = st["skips"].setdefault(week_key(d), {})
    wk.setdefault(sid, {})
    wk[sid][label] = wk[sid].get(label, 0) + 1
    tot = st["total_skips"].setdefault(sid, {})
    tot[label] = tot.get(label, 0) + 1
    for k in sorted(st["skips"])[:-cfg_keep_weeks()]:
        del st["skips"][k]
    return True


def cfg_keep_weeks() -> int:
    return KEEP_WEEKS


def bump_run(st: dict, d: dt.date) -> None:
    wk = week_key(d)
    st["runs"][wk] = st["runs"].get(wk, 0) + 1
    for k in sorted(st["runs"])[:-KEEP_WEEKS]:
        del st["runs"][k]


# ----------------------------------------------------------------------------- outbox
def enqueue(st: dict, key: str, kind: str, strategy: Optional[str], embed: Optional[dict], plain: str, created_iso: str) -> bool:
    """Queue a message. False when the key was already sent or is already queued (so callers can enqueue idempotently)."""
    if key in st["sent"] or any(m["key"] == key for m in st["outbox"]):
        return False
    st["outbox"].append({"key": key, "kind": kind, "strategy": strategy, "embed": embed, "plain": plain, "created": created_iso})
    return True


def mark_sent(st: dict, key: str, at_iso: str) -> None:
    st["outbox"] = [m for m in st["outbox"] if m["key"] != key]
    st["sent"][key] = at_iso


def prune_sent(st: dict, today: dt.date) -> None:
    cutoff = (today - dt.timedelta(days=KEEP_SENT_DAYS)).isoformat()
    st["sent"] = {k: v for k, v in st["sent"].items() if v[:10] >= cutoff}


def drop_stale_outbox(st: dict, today: dt.date, max_age_days: int = 3) -> list:
    """Messages that could not be delivered for several days are dropped (and returned for logging), not retried forever."""
    cutoff = (today - dt.timedelta(days=max_age_days)).isoformat()
    dropped = [m for m in st["outbox"] if m["created"][:10] < cutoff]
    st["outbox"] = [m for m in st["outbox"] if m["created"][:10] >= cutoff]
    return dropped


# ----------------------------------------------------------------------------- trade log
LOG_COLUMNS = [
    "id", "strategy", "sleeve", "version", "pair_id", "entry_date", "exit_date", "exit_reason", "expiry", "dte_in", "dte_out",
    "spot_in", "legs", "credit_mid", "credit_base", "credit_nat", "entry_fees", "exit_cf_mid", "exit_cf_base", "exit_cf_nat",
    "exit_fees", "units", "max_loss", "pnl_mid", "pnl_base", "pnl_nat", "pnl_x3", "pnl_f65", "med_rel_spread", "stale",
    "flags", "quote_file_ts_utc", "model_credit_base", "real_over_model", "notes",
]


def init_log(path: str = cfg.LOG_FILE) -> None:
    if not os.path.exists(path):
        with open(path, "w", newline="") as f:
            csv.writer(f, lineterminator="\n").writerow(LOG_COLUMNS)


def append_log(rows: list, path: str = cfg.LOG_FILE) -> None:
    init_log(path)
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=LOG_COLUMNS, lineterminator="\n", extrasaction="ignore")
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in LOG_COLUMNS})


def read_log(path: str = cfg.LOG_FILE) -> list:
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def log_ids(path: str = cfg.LOG_FILE) -> set:
    return {r["id"] for r in read_log(path)}
