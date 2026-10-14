"""Discord transport for the shadow bots. Standard library only.

  * Every message starts with the banner "SHADOW - NOT A TRADE" (the `content` line and the plain fallback both begin with it).
  * `allowed_mentions: {"parse": []}` on every post, so nothing in a message can ping anyone (no @here, no @everyone).
  * The webhook is read from SHADOW_DISCORD_WEBHOOK_URL only. It is never the live DISCORD_WEBHOOK_URL, and a URL that is not a
    Discord webhook is refused.
  * A message is an embed (title / description / footer) plus a plain-text twin. HTTP 400 (the usual sign of an over-long or
    malformed embed) retries ONCE as the plain twin. Other failures leave the message in the outbox for the next run.
  * Idempotency lives in state.py (outbox / sent ledger); this module only moves bytes.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from typing import Callable, Optional

import config as cfg
import state as state_mod

BANNER = cfg.BANNER
LIM = dict(title=256, description=4096, field_name=256, field_value=1024, footer=2048, fields=25, total=6000,
           content=2000, plain=1900)
COLORS = {"info": 0x95A5A6, "entry": 0x3498DB, "win": 0x2ECC71, "loss": 0xE67E22, "warn": 0xF1C40F, "error": 0xE74C3C}
WEBHOOK_PREFIXES = ("https://discord.com/api/webhooks/", "https://discordapp.com/api/webhooks/",
                    "https://canary.discord.com/api/webhooks/", "https://ptb.discord.com/api/webhooks/")
ENV_NAME = "SHADOW_DISCORD_WEBHOOK_URL"
# Discord's edge (Cloudflare) refuses the default "Python-urllib/x.y" agent with HTTP 403 "error code: 1010". Discord's own
# documented form for API clients is "DiscordBot ($url, $versionNumber)"; the URL is this repo's (it holds no account details).
USER_AGENT = "DiscordBot (https://github.com/samedame/shadow_bot, 1.0)"


# ---------------------------------------------------------------------------------------------- building
def trunc(text: str, n: int) -> str:
    text = str(text)
    return text if len(text) <= n else text[: max(0, n - 1)] + "…"


def embed_size(e: dict) -> int:
    n = len(e.get("title", "")) + len(e.get("description", "")) + len((e.get("footer") or {}).get("text", ""))
    for f in e.get("fields", []) or []:
        n += len(f.get("name", "")) + len(f.get("value", ""))
    return n


def make_message(headline: str, body_lines: list, color: str = "info", fields: Optional[list] = None) -> dict:
    """-> {"embed": dict | None, "plain": str}. `headline` is everything after the banner on the first line.

    The embed is None when it cannot be made to fit Discord's limits (the sender then uses the plain twin). The plain twin is
    always <= 1900 characters and starts with the banner."""
    body = "\n".join(body_lines)
    plain = f"{BANNER} | {headline}\n{body}" if body else f"{BANNER} | {headline}"
    plain = trunc(plain, LIM["plain"])
    desc = "```\n" + body + "\n```" if body else ""
    embed = {"title": trunc(headline, LIM["title"]), "description": desc, "color": COLORS.get(color, COLORS["info"]),
             "footer": {"text": BANNER}}
    if fields:
        embed["fields"] = [{"name": trunc(n, LIM["field_name"]), "value": trunc(v, LIM["field_value"]), "inline": False}
                           for n, v in fields[: LIM["fields"]]]
    if len(desc) > LIM["description"] or embed_size(embed) > LIM["total"]:
        embed = None
    return {"embed": embed, "plain": plain}


def payload_for(msg: dict, use_embed: bool = True) -> dict:
    """The JSON body to POST for an outbox message."""
    plain = msg.get("plain") or f"{BANNER} | (empty message)"
    if not plain.startswith(BANNER):
        plain = f"{BANNER} | {plain}"
    base = {"allowed_mentions": {"parse": []}}
    if use_embed and msg.get("embed"):
        head = plain.split("\n", 1)[0]
        return dict(base, content=trunc(head, LIM["content"]), embeds=[msg["embed"]])
    return dict(base, content=trunc(plain, LIM["plain"]))


# ---------------------------------------------------------------------------------------------- sending
def valid_webhook(url: Optional[str]) -> bool:
    return bool(url) and any(url.startswith(p) for p in WEBHOOK_PREFIXES)


def send(url: str, payload: dict, retries: int = 3, sleep: Callable[[float], None] = time.sleep,
         opener: Callable = urllib.request.urlopen) -> tuple:
    """POST one payload. -> (ok, http_status | None, detail). 4xx other than 429 is final (no retry); 429 honours retry_after;
    5xx and network errors retry with a short back-off."""
    body = json.dumps(payload).encode()
    status, detail = None, ""
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=body, method="POST",
                                         headers={"Content-Type": "application/json", "User-Agent": USER_AGENT})
            with opener(req, timeout=15) as r:
                status = getattr(r, "status", None) or r.getcode()
                if 200 <= status < 300:
                    return True, status, ""
                detail = f"HTTP {status}"
        except urllib.error.HTTPError as e:
            status = e.code
            try:
                raw = e.read().decode("utf-8", "replace")
            except Exception:                                   # noqa: BLE001 - best effort body for the log
                raw = ""
            detail = f"HTTP {e.code} {raw[:200]}"
            if e.code == 429:
                try:
                    wait = float(json.loads(raw).get("retry_after", 2))
                except (ValueError, TypeError, AttributeError):
                    wait = 2.0
                if attempt < retries - 1:
                    sleep(min(max(wait, 0.5), 10.0))
                continue
            if 400 <= e.code < 500:
                return False, e.code, detail
        except (urllib.error.URLError, OSError) as e:
            status, detail = None, f"network: {e}"
        if attempt < retries - 1:
            sleep(1 + 2 * attempt)
    return False, status, detail


def flush(st: dict, url: Optional[str], now_iso: str, save: Optional[Callable[[], None]] = None,
          sender: Callable = send, dry_run: bool = False, out: Callable[[str], None] = print) -> dict:
    """Post every queued message, oldest first. Each success is moved to `sent` and (if `save` is given) persisted at once,
    so a crash right after a post cannot cause a repost. Returns counts."""
    res = {"sent": 0, "fallback": 0, "failed": 0, "kept": 0, "dry": 0}
    queue = list(st["outbox"])
    if dry_run:
        for m in queue:
            out("---- would post: " + m["key"] + " ----")
            out(m.get("plain") or "")
        res["dry"] = len(queue)
        return res
    if not queue:
        return res
    if not valid_webhook(url):
        why = f"{ENV_NAME} is not set" if not url else f"{ENV_NAME} is not a Discord webhook URL"
        print(f"[discord] {why}; {len(queue)} message(s) stay queued", file=sys.stderr)
        res["kept"] = len(queue)
        return res
    for m in queue:
        ok, code, detail = sender(url, payload_for(m, True))
        if not ok and code == 400:
            ok, code, detail = sender(url, payload_for(m, False))        # plain-message fallback
            if ok:
                res["fallback"] += 1
        if ok:
            state_mod.mark_sent(st, m["key"], now_iso)
            res["sent"] += 1
            if save is not None:
                save()
        else:
            res["failed"] += 1
            print(f"[discord] {m['key']}: not delivered ({detail}); it stays queued", file=sys.stderr)
            if code is None or code >= 500 or code in (401, 403, 404, 429):
                break                                                 # down, refusing us or rate-limited: stop hammering
    res["kept"] = len(st["outbox"])
    return res
