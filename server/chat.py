"""Chat message parsing + the polling loop.

Endpoint (newest-first, cursor-paginated):
    GET /api/chat/channels/{channelId}/messages?size=50
"""
import re
import time

from . import api_client
from .sink import Sink

TAG = re.compile(r"\[([A-Z.]+):(\d+)\]")
DEFAULT_CHANNEL = "13724041-32f0-4886-88ee-e38f6dd79d5e"  # "options"


def list_channels() -> list[dict]:
    """All channels the user can see: id, title, type, lastMessageCreatedAt."""
    return api_client.get("/api/chat/channels")


def resolve_channels(names_or_ids: list[str]) -> list[tuple[str, str]]:
    """Map a mix of channel titles and raw UUIDs to (id, title) pairs."""
    catalog = {c["id"]: c.get("title", "?") for c in list_channels()}
    by_title = {}
    for cid, title in catalog.items():
        by_title.setdefault(title.lower(), cid)
    out = []
    for token in names_or_ids:
        if token in catalog:  # already a uuid
            out.append((token, catalog[token]))
        elif token.lower() in by_title:
            cid = by_title[token.lower()]
            out.append((cid, catalog[cid]))
        else:
            raise ValueError(f"unknown channel: {token!r}")
    return out


def parse_tickers(msg: dict) -> list[dict]:
    """Expand inline [TICKER:seriesId] tags into structured rows by joining
    against the message's seriesContexts."""
    contexts = msg.get("seriesContexts") or {}
    out = []
    for ticker, sid in TAG.findall(msg.get("content") or ""):
        ctx = contexts.get(sid) or {}
        instrument = ctx.get("instrument") or {}
        legal = ctx.get("legalEntity") or {}
        series = ctx.get("series") or {}
        series_type = (series.get("seriesType") or {}).get("name")
        out.append(
            {
                "ticker": ticker,
                "series_id": int(sid),
                "name": instrument.get("name"),
                "figi": instrument.get("figi"),
                "isin": instrument.get("isin"),
                "cik": legal.get("cik"),
                "series_type": series_type,
            }
        )
    return out


def fetch_page(channel_id: str, size: int = 50, cursor: str | None = None,
               cursor_param: str = "cursor") -> dict:
    params = {"size": size}
    if cursor:
        params[cursor_param] = cursor
    return api_client.get(f"/api/chat/channels/{channel_id}/messages", params=params)


def poll(channel_id: str = DEFAULT_CHANNEL, interval: float = 3.0,
         size: int = 50, sink: Sink | None = None, on_message=None) -> None:
    """Poll forever. Emits each new message (oldest-first within a page) to
    `on_message(msg, tickers)` and/or persists to `sink`. Dedup is by max id."""
    own_sink = sink is None
    sink = sink or Sink()
    max_id = sink.max_chat_id(channel_id)
    print(f"[chat] polling channel {channel_id} every {interval}s "
          f"(resuming after id {max_id})", flush=True)
    try:
        while True:
            try:
                page = fetch_page(channel_id, size=size)
                # content is oldest-first (id ascending); emit in that order.
                content = page.get("content") or []
                fresh = [m for m in content if m["id"] > max_id]
                for m in fresh:
                    max_id = max(max_id, m["id"])
                    tickers = parse_tickers(m)
                    sink.save_chat_message(m, tickers)
                    if on_message:
                        on_message(m, tickers)
                    else:
                        _print_message(m, tickers)
            except api_client.TokenExpired as e:
                print(f"[chat] {e}; waiting for fresh token…", flush=True)
            except Exception as e:
                print(f"[chat] error: {e}", flush=True)
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n[chat] stopped", flush=True)
    finally:
        if own_sink:
            sink.close()


def poll_multi(channels: list[tuple[str, str]], interval: float = 3.0,
               size: int = 50, sink: Sink | None = None, on_message=None) -> None:
    """Poll several channels in one loop. `channels` is a list of (id, title).
    Each channel keeps its own max-id watermark; dedup is per channel."""
    own_sink = sink is None
    sink = sink or Sink()
    watermark = {cid: sink.max_chat_id(cid) for cid, _ in channels}
    label = ", ".join(f"#{t}" for _, t in channels)
    print(f"[chat] polling {len(channels)} channels every {interval}s: {label}",
          flush=True)
    try:
        while True:
            for cid, title in channels:
                try:
                    page = fetch_page(cid, size=size)
                    content = page.get("content") or []  # oldest-first
                    fresh = [m for m in content if m["id"] > watermark[cid]]
                    for m in fresh:
                        watermark[cid] = max(watermark[cid], m["id"])
                        tickers = parse_tickers(m)
                        sink.save_chat_message(m, tickers)
                        if on_message:
                            on_message(m, tickers)
                        else:
                            _print_message(m, tickers, title)
                except api_client.TokenExpired as e:
                    print(f"[chat] {e}; waiting for fresh token…", flush=True)
                except Exception as e:
                    print(f"[chat:{title}] error: {e}", flush=True)
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n[chat] stopped", flush=True)
    finally:
        if own_sink:
            sink.close()


def _print_message(m: dict, tickers: list[dict], channel: str | None = None) -> None:
    tags = " ".join(f"{t['ticker']}({t['name'] or '?'})" for t in tickers)
    chan = f"#{channel} " if channel else ""
    line = f"{chan}[{m.get('createdAt')}] {m.get('username')}: {m.get('content')}"
    if tags:
        line += f"   <{tags}>"
    print(line, flush=True)
