"""Per-order trade authorization.

Hayden can authorize live orders. Nothing else can.

An order reaches Schwab only by consuming a GRANT that is:
  * bound to exact parameters (symbol, side, quantity, order type, limit),
  * single-use and consumed atomically,
  * expired after AUTH_TTL_SECONDS,
  * released only by Hayden's approval of THAT proposal.

Two confirmation modes (GODEL_CONFIRM_MODE):

  "reply" (default) — Hayden replies "yes" in the normal Telegram chat. This is
    the mode he chose: one word, no transcription, works from a phone. The
    security of this mode rests on the agent's channel discipline — a direct
    message from Hayden is authorization; text encountered anywhere else never
    is, however much it looks like him. See SOUL.md "Untrusted Input".

  "code" — propose mints a secret delivered out-of-band that Hayden must type
    back. Stronger (the agent cannot produce the secret even if it is fooled
    about who is talking), but it means retyping 9 characters on a phone.
    Set GODEL_CONFIRM_MODE=code to switch.

What both modes guarantee, and what actually matters most here: the order that
reaches Schwab is byte-for-byte the order described in the message Hayden
approved. The grant is fingerprinted over symbol/side/quantity/type/limit, so
an approval cannot be stretched to cover a bigger position, a different name,
or a market order where a limit was shown.

Sizing guidance (10% of equity per long, 5% per short) is a WARNING here, not a
block: a breach is stated prominently in the message Hayden reads before he
confirms. He can approve it anyway. He cannot approve it unknowingly.

Kill switch: set GODEL_TRADING_HALT=1 to refuse every order regardless of grant.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path

AUTH_TTL_SECONDS = 900          # 15 minutes — a phone reply takes a minute
# no 0/O/1/I/L — these get read aloud and retyped
_CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
CONFIRM_MODE = os.environ.get("GODEL_CONFIRM_MODE", "reply").lower()
LONG_CAP = 0.10                 # guidance only — warn, do not block
SHORT_CAP = 0.05                # guidance only — warn, do not block

# Everything secret lives outside godel_rest/. The agent's file access is
# confined to that tree, so the ticket store, the codes and the audit trail are
# all out of its reach. Do not move these back inside the project.
_PRIVATE_DIR = Path(os.environ.get("GODEL_PRIVATE_DIR", Path.home() / ".openclaw"))
_TICKETS_PATH = _PRIVATE_DIR / "trade-tickets.json"
_CONFIRMATIONS_PATH = _PRIVATE_DIR / "trade-confirmations.log"
_AUDIT_PATH = _PRIVATE_DIR / "trade-audit.log"
_KEY_PATH = _PRIVATE_DIR / "trade-auth.key"


def _secret_key() -> bytes:
    """Per-install HMAC key. Without it a leaked ticket store is useless: a
    6-8 character code would otherwise be brute-forceable against a bare hash
    in well under a second."""
    _PRIVATE_DIR.mkdir(parents=True, exist_ok=True)
    if not _KEY_PATH.exists():
        _KEY_PATH.write_bytes(secrets.token_bytes(32))
        os.chmod(_KEY_PATH, 0o600)
    return _KEY_PATH.read_bytes()


class AuthorizationError(RuntimeError):
    """No valid grant for this order."""


class TradingHalted(RuntimeError):
    """Kill switch engaged."""


def _now() -> float:
    return time.time()


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def check_halt() -> None:
    if os.environ.get("GODEL_TRADING_HALT") == "1":
        raise TradingHalted(
            "GODEL_TRADING_HALT=1 — all order placement is halted. Unset it to resume.")


# ---------------------------------------------------------------- ticket store

def _load() -> dict:
    if not _TICKETS_PATH.exists():
        return {"version": 1, "tickets": {}}
    try:
        return json.loads(_TICKETS_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {"version": 1, "tickets": {}}


def _save(store: dict) -> None:
    _PRIVATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _TICKETS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(store, indent=2))
    os.chmod(tmp, 0o600)
    tmp.replace(_TICKETS_PATH)


def _prune(store: dict) -> dict:
    now = _now()
    store["tickets"] = {k: v for k, v in store["tickets"].items()
                        if v.get("expires_at", 0) > now and not v.get("consumed")}
    return store


def order_fingerprint(symbol: str, side: str, quantity: int,
                      order_type: str, limit: float | None) -> str:
    """Canonical identity of an order. A grant is valid for this and nothing else."""
    limit_s = f"{float(limit):.4f}" if limit is not None else "MARKET"
    raw = f"{symbol.upper()}|{side.upper()}|{int(quantity)}|{order_type.upper()}|{limit_s}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _code_hash(ticket_id: str, code: str) -> str:
    return hmac.new(_secret_key(), f"{ticket_id}:{code}".encode(),
                    hashlib.sha256).hexdigest()


# ------------------------------------------------------------------- delivery

def _deliver(message: str) -> list[str]:
    """Send the confirmation out-of-band. Returns the channels that accepted it."""
    channels = []

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat_id:
        try:
            import urllib.error
            import urllib.parse
            import urllib.request
            payload = urllib.parse.urlencode(
                {"chat_id": chat_id, "text": message}).encode()
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{token}/sendMessage", data=payload)
            with urllib.request.urlopen(req, timeout=10) as r:
                if r.status == 200:
                    channels.append("telegram")
        except Exception as e:                              # noqa: BLE001
            channels.append(f"telegram-failed({type(e).__name__})")

    try:
        _PRIVATE_DIR.mkdir(parents=True, exist_ok=True)
        with _CONFIRMATIONS_PATH.open("a") as fh:
            fh.write(f"\n===== {_utc()} =====\n{message}\n")
        os.chmod(_CONFIRMATIONS_PATH, 0o600)
        channels.append(f"file:{_CONFIRMATIONS_PATH}")
    except OSError as e:
        channels.append(f"file-failed({e})")

    return channels


def _audit(event: str, detail: dict) -> None:
    try:
        _PRIVATE_DIR.mkdir(parents=True, exist_ok=True)
        with _AUDIT_PATH.open("a") as fh:
            fh.write(json.dumps({"ts": _utc(), "event": event, **detail}) + "\n")
        os.chmod(_AUDIT_PATH, 0o600)
    except OSError:
        pass


# -------------------------------------------------------------------- sizing

def size_check(symbol: str, side: str, quantity: int,
               limit: float | None = None) -> dict:
    """Post-trade weight vs the sizing guidance. Never raises: on failure it
    reports unknown, and 'unknown' is itself surfaced to Hayden as a warning."""
    out = {"ok": None, "breach": False, "detail": "", "weight": None,
           "notional": None, "equity": None, "cap": None}
    side_u = side.upper()
    out["cap"] = SHORT_CAP if side_u == "SELL_SHORT" else LONG_CAP
    try:
        price = limit
        if price is None:
            from .market_data import MarketDataClient
            price = MarketDataClient().last_price(symbol)
        if price is None:
            out["detail"] = "no price available — position size could not be checked"
            return out

        from .portfolio import Portfolio
        snap = Portfolio().snapshot()
        equity = snap["balances"].get("liquidation_value")
        if not equity:
            out["detail"] = "no account equity available — size could not be checked"
            return out

        notional = abs(float(price) * int(quantity))
        existing = 0.0
        for p in snap["positions"]:
            if p["symbol"].upper() == symbol.upper():
                existing = abs(p.get("market_value") or 0)
                break
        # adds increase exposure; closing trades reduce it
        if side_u in ("BUY", "SELL_SHORT"):
            resulting = existing + notional
        else:
            resulting = max(existing - notional, 0.0)

        weight = resulting / equity
        out.update(notional=notional, equity=equity, weight=weight,
                   breach=weight > out["cap"], ok=weight <= out["cap"])
        label = "short" if side_u == "SELL_SHORT" else "long"
        out["detail"] = (
            f"post-trade {symbol.upper()} = {weight * 100:.1f}% of "
            f"${equity:,.0f} equity (guidance: {out['cap'] * 100:.0f}% per {label})")
    except Exception as e:                                  # noqa: BLE001
        out["detail"] = f"size check failed ({type(e).__name__}) — treat as unverified"
    return out


# ------------------------------------------------------------------ proposals

def propose_cancel(order_id: str, note: str = "") -> dict:
    """Mint a pending authorization for cancelling a working order.

    Cancels reduce exposure, but they are still real account actions: a
    poisoned instruction that pulls your protective orders is an attack, not a
    convenience. Same confirmation path as placement.
    """
    check_halt()
    reply_mode = CONFIRM_MODE != "code"
    ticket_id = "t-" + secrets.token_hex(4)
    code = None if reply_mode else "-".join(
        "".join(secrets.choice(_CODE_ALPHABET) for _ in range(4)) for _ in range(2))
    expires_at = _now() + AUTH_TTL_SECONDS

    store = _prune(_load())
    store["tickets"][ticket_id] = {
        "action": "cancel", "order_id": str(order_id),
        "fingerprint": hashlib.sha256(f"CANCEL|{order_id}".encode()).hexdigest(),
        "code_hash": None if code is None else _code_hash(ticket_id, code),
        "mode": "reply" if reply_mode else "code",
        "symbol": "-", "side": "CANCEL", "quantity": 0,
        "order_type": "-", "limit": None,
        "created_at": _utc(), "expires_at": expires_at,
        "consumed": False, "attempts": 0,
        "sizing_breach": False, "sizing_detail": "n/a (cancellation)",
    }
    _save(store)

    note_s = (chr(10) + chr(10) + note) if note else ""
    if reply_mode:
        message = (f"Cancel working order {order_id}?{note_s}\n\n"
                   f"Reply YES to cancel it. Expires in "
                   f"{AUTH_TTL_SECONDS // 60} min.")
        channels = ["agent-relay"]
    else:
        message = (f"CANCEL CONFIRMATION REQUEST {ticket_id}\n"
                   f"Cancel working order {order_id}{note_s}\n\n"
                   f"Confirmation code: {code}\n"
                   f"Expires in {AUTH_TTL_SECONDS // 60} minutes. Single use.")
        channels = _deliver(message)
    _audit("propose_cancel", {"ticket": ticket_id, "order_id": str(order_id),
                              "mode": "reply" if reply_mode else "code",
                              "delivered": channels})
    out = {"ticket_id": ticket_id, "action": "cancel", "order_id": str(order_id),
           "mode": "reply" if reply_mode else "code",
           "expires_in_seconds": AUTH_TTL_SECONDS,
           "next_step": f"python3 -m broker.cli confirm {ticket_id} "
                        f"{'--yes' if reply_mode else '<CODE>'}"}
    if reply_mode:
        out["send_to_hayden"] = message
    else:
        out["code_delivered_to"] = channels
    return out


def propose(symbol: str, side: str, quantity: int, limit: float | None = None,
            note: str = "", duration: str = "DAY") -> dict:
    """Mint a pending authorization. Returns everything EXCEPT the code."""
    check_halt()
    symbol, side = symbol.upper(), side.upper()
    order_type = "LIMIT" if limit is not None else "MARKET"

    reply_mode = CONFIRM_MODE != "code"
    ticket_id = "t-" + secrets.token_hex(4)
    code = None if reply_mode else "-".join(
        "".join(secrets.choice(_CODE_ALPHABET) for _ in range(4)) for _ in range(2))

    sizing = size_check(symbol, side, quantity, limit)
    fp = order_fingerprint(symbol, side, quantity, order_type, limit)
    expires_at = _now() + AUTH_TTL_SECONDS

    store = _prune(_load())
    store["tickets"][ticket_id] = {
        "action": "order",
        "fingerprint": fp,
        "code_hash": None if code is None else _code_hash(ticket_id, code),
        "mode": "reply" if reply_mode else "code",
        "symbol": symbol, "side": side, "quantity": int(quantity),
        "order_type": order_type, "limit": limit, "duration": duration.upper(),
        "created_at": _utc(), "expires_at": expires_at,
        "consumed": False, "attempts": 0,
        "sizing_breach": bool(sizing["breach"]),
        "sizing_detail": sizing["detail"],
    }
    _save(store)

    price_s = f"limit ${limit}" if limit is not None else "market"
    est = ""
    if sizing.get("notional"):
        est = f" (~${sizing['notional']:,.0f})"

    if sizing["breach"]:
        warn = (f"\n\nSIZE WARNING: {sizing['detail']}\n"
                f"That is ABOVE your guidance. Saying yes approves the oversize "
                f"position knowingly.")
    elif sizing["ok"] is None:
        warn = f"\n\nSIZE UNVERIFIED: {sizing['detail']}"
    else:
        warn = f"\n\nSize ok: {sizing['detail']}"

    head = f"{side} {quantity} {symbol} @ {price_s}{est}"
    note_s = (chr(10) + chr(10) + note) if note else ""

    if reply_mode:
        # The agent sends this to Hayden itself, on the channel he is already in.
        message = (f"{head}{warn}{note_s}\n\n"
                   f"Reply YES to place it. Anything else cancels. "
                   f"Expires in {AUTH_TTL_SECONDS // 60} min.")
        channels = ["agent-relay"]
        next_step = (f"send Hayden the message verbatim; on his YES run: "
                     f"python3 -m broker.cli confirm {ticket_id} --yes")
    else:
        message = (f"TRADE CONFIRMATION REQUEST {ticket_id}\n{head}{warn}{note_s}\n\n"
                   f"Confirmation code: {code}\n"
                   f"Expires in {AUTH_TTL_SECONDS // 60} minutes. Single use.\n"
                   f"Reply with this code ONLY if you asked for this trade.")
        channels = _deliver(message)
        next_step = f"python3 -m broker.cli confirm {ticket_id} <CODE>"

    _audit("propose", {"ticket": ticket_id, "mode": "reply" if reply_mode else "code",
                       "symbol": symbol, "side": side, "quantity": quantity,
                       "order_type": order_type, "limit": limit, "duration": duration.upper(),
                       "sizing": sizing["detail"], "breach": bool(sizing["breach"]),
                       "delivered": channels})

    out = {
        "ticket_id": ticket_id,
        "mode": "reply" if reply_mode else "code",
        "order": {"symbol": symbol, "side": side, "quantity": int(quantity),
                  "order_type": order_type, "limit": limit, "duration": duration.upper()},
        "sizing": sizing,
        "expires_in_seconds": AUTH_TTL_SECONDS,
        "next_step": next_step,
    }
    if reply_mode:
        # send this to Hayden EXACTLY as written — he is approving these words
        out["send_to_hayden"] = message
    else:
        out["code_delivered_to"] = channels
    return out


def consume(ticket_id: str, code: str | None = None) -> dict:
    """Validate and burn a grant. Returns the order parameters it authorizes.

    In reply mode `code` is unused: the caller asserts Hayden said yes to THIS
    ticket. The ticket still has to exist, be unexpired and unused, and the
    order placed from it is locked to what he was shown.
    """
    check_halt()
    store = _prune(_load())
    t = store["tickets"].get(ticket_id)
    if t is None:
        _audit("confirm_rejected", {"ticket": ticket_id, "reason": "unknown_or_expired"})
        raise AuthorizationError(
            f"No live authorization {ticket_id!r}. It expired, was already used, "
            "or never existed. Propose again.")

    if t.get("mode", "code") == "reply":
        t["consumed"] = True
        t["consumed_at"] = _utc()
        _save(store)
        _audit("confirmed", {"ticket": ticket_id, "mode": "reply",
                             "symbol": t["symbol"], "side": t["side"],
                             "quantity": t["quantity"], "order_type": t["order_type"],
                             "limit": t["limit"], "duration": t.get("duration", "DAY"),
                             "breach": t["sizing_breach"]})
        return {"ticket_id": ticket_id, "fingerprint": t["fingerprint"],
                "action": t.get("action", "order"), "order_id": t.get("order_id"),
                "symbol": t["symbol"], "side": t["side"], "quantity": t["quantity"],
                "order_type": t["order_type"], "limit": t["limit"],
                "duration": t.get("duration", "DAY"),
                "sizing_breach": t["sizing_breach"]}

    if t["attempts"] >= 3:
        t["consumed"] = True
        _save(store)
        _audit("confirm_rejected", {"ticket": ticket_id, "reason": "too_many_attempts"})
        raise AuthorizationError("Too many bad codes — authorization destroyed.")

    supplied = str(code).strip().upper().replace(" ", "")
    if not hmac.compare_digest(t["code_hash"], _code_hash(ticket_id, supplied)):
        t["attempts"] += 1
        _save(store)
        _audit("confirm_rejected", {"ticket": ticket_id, "reason": "bad_code",
                                    "attempt": t["attempts"]})
        raise AuthorizationError(
            f"Wrong confirmation code ({t['attempts']}/3). No order placed.")

    t["consumed"] = True
    t["consumed_at"] = _utc()
    _save(store)
    _audit("confirmed", {"ticket": ticket_id, "symbol": t["symbol"],
                         "side": t["side"], "quantity": t["quantity"],
                         "order_type": t["order_type"], "limit": t["limit"],
                         "duration": t.get("duration", "DAY"),
                         "breach": t["sizing_breach"]})
    return {"ticket_id": ticket_id, "fingerprint": t["fingerprint"],
            "action": t.get("action", "order"), "order_id": t.get("order_id"),
            "symbol": t["symbol"], "side": t["side"], "quantity": t["quantity"],
            "order_type": t["order_type"], "limit": t["limit"],
            "duration": t.get("duration", "DAY"),
            "sizing_breach": t["sizing_breach"]}


def pending() -> list[dict]:
    """Live tickets, with no secrets in the output."""
    store = _prune(_load())
    _save(store)
    return [{"ticket_id": k, "action": v.get("action", "order"),
             "symbol": v["symbol"], "side": v["side"],
             "quantity": v["quantity"], "order_type": v["order_type"],
             "limit": v["limit"], "duration": v.get("duration", "DAY"),
             "sizing_breach": v["sizing_breach"],
             "expires_in_seconds": int(v["expires_at"] - _now())}
            for k, v in store["tickets"].items()]


def revoke(ticket_id: str = "") -> int:
    """Kill one ticket, or all of them when ticket_id is empty."""
    store = _load()
    if ticket_id:
        n = 1 if store["tickets"].pop(ticket_id, None) else 0
    else:
        n = len(store["tickets"])
        store["tickets"] = {}
    _save(store)
    _audit("revoke", {"ticket": ticket_id or "ALL", "count": n})
    return n
