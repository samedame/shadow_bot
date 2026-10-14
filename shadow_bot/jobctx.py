"""Run context shared by the strategy modules, plus the helpers every strategy uses to build a position, close it and write
its log row. No network here.

Phases (decided from the New York wall clock and the NYSE calendar):
    close       a trading day, at or after the close-snapshot time (16:05 ET; 13:05 on an early-close day): the real run.
    intraday    a trading day before that: diagnostics only. No entries, no settlements, no state changes except the diag line.
    closed_day  weekend / holiday: nothing to do.
All state transitions are keyed to DATES and to official closes / published index values, never to the wall clock, so a late or
repeated run cannot corrupt a position; it can only delay a transition.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Optional
from zoneinfo import ZoneInfo

import chain as chain_mod
import config as cfg
import fills
import nyse_calendar as nyse
import state as state_mod
from market_data import Market

ET = ZoneInfo("America/New_York")


def compute_phase(now_et: dt.datetime) -> tuple:
    """-> (session date, phase, early_close, close_time)."""
    d = now_et.date()
    if not nyse.is_trading_day(d):
        return d, "closed_day", False, chain_mod.REGULAR_CLOSE
    early = nyse.is_early_close(d)
    close_t = chain_mod.EARLY_CLOSE if early else chain_mod.REGULAR_CLOSE
    snap = (dt.datetime.combine(d, close_t) + dt.timedelta(minutes=5)).time()
    phase = "close" if now_et.time() >= snap else "intraday"
    return d, phase, early, close_t


@dataclass
class Ctx:
    root: str
    now_et: dt.datetime
    session: dt.date
    phase: str
    early_close: bool
    close_et: dt.time
    trigger: str
    guard_status: str
    late_min: Optional[float]
    dry_run: bool
    only: set
    feed: Any
    st: dict
    mkt: Market
    log_rows: list
    now_utc: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))
    log_ids: set = field(default_factory=set)
    new_log_rows: list = field(default_factory=list)
    chains: dict = field(default_factory=dict)          # symbol -> Chain
    chain_err: dict = field(default_factory=dict)       # symbol -> error text
    stale: dict = field(default_factory=dict)           # symbol -> reason | None
    problems: list = field(default_factory=list)        # data problems for the once-a-day notice
    diag: dict = field(default_factory=dict)
    events: list = field(default_factory=list)          # human-readable run log (printed at the end)
    clock: Any = None                                   # object with .now() -> aware ET datetime and .sleep(seconds); None = wall clock

    def __post_init__(self):
        self.log_ids = {r["id"] for r in self.log_rows}

    def clock_now(self) -> dt.datetime:
        return self.clock.now() if self.clock is not None else dt.datetime.now(ET)

    def wait(self, seconds: float) -> None:
        if self.clock is not None:
            self.clock.sleep(seconds)
        else:
            import time
            time.sleep(seconds)

    # ------------------------------------------------------------------ small helpers
    @property
    def now_iso(self) -> str:
        return self.now_et.isoformat(timespec="seconds")

    def block(self, sid: str) -> dict:
        return self.st["strategies"][sid]

    def event(self, text: str) -> None:
        self.events.append(text)

    def emit(self, key: str, kind: str, sid: Optional[str], msg: dict) -> bool:
        """Queue a message once (the key dedupes against the outbox and the sent ledger)."""
        return state_mod.enqueue(self.st, key, kind, sid, msg["embed"], msg["plain"], self.now_iso)

    def append_log(self, row: dict) -> bool:
        """Add a closed-trade row unless that id is already in the log (re-runs after a crash are harmless)."""
        if row["id"] in self.log_ids:
            return False
        self.log_ids.add(row["id"])
        self.new_log_rows.append(row)
        self.log_rows.append(row)
        return True

    def wants(self, sid: str) -> bool:
        return sid in self.only

    # ------------------------------------------------------------------ gating of NEW entries
    def entry_block_reason(self, sid: str) -> Optional[str]:
        """Why this run may not open new shadow positions for `sid` (None = allowed). Settlements and re-marks are not gated."""
        if self.block(sid)["status"] == "retired":
            return "retired"
        if self.phase != "close":
            return "not_close_phase"
        if self.early_close:
            return "early_close"
        if self.guard_status == "too_late":
            return "run_late"
        return None

    def chain_for(self, symbol: str) -> tuple:
        """-> (Chain | None, skip_reason | None). feed_down when the file could not be read, stale when the snapshot is not the
        session's closing snapshot."""
        if symbol in self.chain_err:
            return None, "feed_down"
        ch = self.chains.get(symbol)
        if ch is None:
            return None, "feed_down"
        if self.stale.get(symbol):
            return None, "stale"
        return ch, None


# ---------------------------------------------------------------------------------------------- positions
def build_position(ctx: Ctx, sid: str, sleeve: str, pid: str, ticket: dict, chain: chain_mod.Chain, version: str = "real",
                   extra: Optional[dict] = None) -> dict:
    pkg = ticket["pkg"]
    meta = chain.meta
    pos = {
        "id": pid, "strategy": sid, "sleeve": sleeve, "version": version,
        "entry_date": ctx.session.isoformat(), "quote_time": meta.get("und_last_trade_et"),
        "quote_file_ts_utc": meta.get("file_ts_utc"), "opened_at": ctx.now_iso,
        "expiry": ticket["expiry"].isoformat(), "dte_in": (ticket["expiry"] - ctx.session).days, "spot_in": ticket["spot"],
        "legs": state_mod.legs_to_json(ticket["legs"], pkg["quotes"], chain),
        "entry_cf": state_mod.named_cf(pkg["cf"]), "entry_units": pkg["units"],
        "max_loss": round(float(ticket["max_loss"]), 4), "med_rel_spread": ticket.get("med_rel_spread"),
        "flags": {"stale": False}, "exit_attempts": 0,
    }
    pos.update(extra or {})
    return pos


def entry_pkg(pos: dict) -> dict:
    return {"cf": state_mod.unnamed_cf(pos["entry_cf"]), "units": pos["entry_units"]}


def log_row(pos: dict, rec: dict, ext: dict, d_exit: dt.date, reason: str, notes: str = "") -> dict:
    ent = pos["entry_cf"]
    exc = state_mod.named_cf(ext["cf"])
    expiry = dt.date.fromisoformat(pos["expiry"])
    flags = ",".join(k for k, v in (pos.get("flags") or {}).items() if v)
    return {
        "id": pos["id"], "strategy": pos["strategy"], "sleeve": pos["sleeve"], "version": pos.get("version", "real"),
        "pair_id": pos.get("pair_id") or "", "entry_date": pos["entry_date"], "exit_date": d_exit.isoformat(),
        "exit_reason": reason, "expiry": pos["expiry"], "dte_in": pos["dte_in"], "dte_out": (expiry - d_exit).days,
        "spot_in": pos["spot_in"], "legs": " ; ".join(l["label"] for l in pos["legs"]),
        "credit_mid": round(ent["mid"], 4), "credit_base": round(ent["base"], 4), "credit_nat": round(ent["nat"], 4),
        "entry_fees": round(cfg.FEE_BASE * pos["entry_units"], 4),
        "exit_cf_mid": round(exc["mid"], 4), "exit_cf_base": round(exc["base"], 4), "exit_cf_nat": round(exc["nat"], 4),
        "exit_fees": round(cfg.FEE_BASE * ext["units"], 4), "units": rec["units"], "max_loss": round(rec["max_loss"], 4),
        "pnl_mid": round(rec["pnl_mid"], 4), "pnl_base": round(rec["pnl"], 4), "pnl_nat": round(rec["pnl_nat"], 4),
        "pnl_x3": round(rec["pnl_x3"], 4), "pnl_f65": round(rec["pnl_f65"], 4),
        "med_rel_spread": pos.get("med_rel_spread"), "stale": "yes" if (pos.get("flags") or {}).get("stale") else "no",
        "flags": flags, "quote_file_ts_utc": pos.get("quote_file_ts_utc"),
        "model_credit_base": pos.get("model_credit_base"), "real_over_model": pos.get("real_over_model"), "notes": notes,
    }


def close_out(ctx: Ctx, sid: str, pos: dict, ext: dict, d_exit: dt.date, reason: str, notes: str = "",
              count_sleeve: bool = False) -> tuple:
    """Close one shadow position: P&L record, log row, running stats, removal from the open list.
    -> (log row, assemble record). The caller queues the exit message (retired strategies stay silent)."""
    blk = ctx.block(sid)
    legs = state_mod.legs_from_json(pos["legs"])
    rec = fills.assemble(legs, entry_pkg(pos), ext)
    row = log_row(pos, rec, ext, d_exit, reason, notes)
    ctx.append_log(row)
    state_mod.update_stats(blk, rec["pnl"], rec["pnl_nat"], rec["pnl_mid"], sleeve=pos["sleeve"] if count_sleeve else None)
    blk["open_positions"] = [p for p in blk["open_positions"] if p["id"] != pos["id"]]
    state_mod.mark_exit_alert(blk, pos["id"], reason, ctx.now_iso)
    return row, rec


def unpriced_out(ctx: Ctx, sid: str, pos: dict, d_exit: dt.date, why: str) -> dict:
    """An exit that could not be priced within the drift window: logged without P&L, counted, removed."""
    blk = ctx.block(sid)
    expiry = dt.date.fromisoformat(pos["expiry"])
    row = {k: "" for k in state_mod.LOG_COLUMNS}
    row.update(id=pos["id"], strategy=pos["strategy"], sleeve=pos["sleeve"], version=pos.get("version", "real"),
               pair_id=pos.get("pair_id") or "", entry_date=pos["entry_date"], exit_date=d_exit.isoformat(),
               exit_reason="unpriced", expiry=pos["expiry"], dte_in=pos["dte_in"], dte_out=(expiry - d_exit).days,
               spot_in=pos["spot_in"], legs=" ; ".join(l["label"] for l in pos["legs"]), notes=why,
               credit_mid=pos["entry_cf"]["mid"], credit_base=pos["entry_cf"]["base"], credit_nat=pos["entry_cf"]["nat"])
    ctx.append_log(row)
    blk["unpriced_n"] += 1
    blk["open_positions"] = [p for p in blk["open_positions"] if p["id"] != pos["id"]]
    state_mod.mark_exit_alert(blk, pos["id"], "unpriced", ctx.now_iso)
    return row
