"""Walk a channel's full history backward via the beforeCursor pagination and
persist every message to SQLite.

Pagination facts (confirmed live):
  * GET /api/chat/channels/{id}/messages?size=50[&beforeCursor=<id>]
  * `content` is OLDEST-FIRST (id ascending); newest message is content[-1].
  * A page's `prevCursor` == content[0].id (oldest id in the page).
  * To page older, pass that prevCursor as the next `beforeCursor`.
  * The walk ends when a page comes back empty (reached the first message).

Resumable: by default it continues from the oldest id already stored for the
channel, so a killed background run picks up where it left off. Idempotent:
every message is INSERT OR REPLACE on its int64 id, so overlap never duplicates.
"""
import time

from . import api_client
from .chat import fetch_page, parse_tickers
from .sink import Sink


def backfill_channel(channel_id: str, title: str = "", sink: Sink | None = None,
                     page_size: int = 50, max_pages: int | None = None,
                     delay: float = 0.25, resume: bool = True,
                     log_every: int = 10) -> dict:
    """Backfill one channel. Returns a summary dict."""
    own_sink = sink is None
    sink = sink or Sink()
    label = f"#{title}" if title else channel_id
    cursor = None
    if resume:
        oldest = sink.min_chat_id(channel_id)
        if oldest is not None:
            cursor = oldest  # continue older than what we already have
    start_count = sink.channel_count(channel_id)
    saved = 0
    pages = 0
    oldest_ts = None
    print(f"[backfill] {label} starting (resume from id {cursor}, "
          f"{start_count} already stored)", flush=True)
    try:
        while True:
            try:
                page = fetch_page(channel_id, size=page_size, cursor=cursor,
                                  cursor_param="beforeCursor")
            except api_client.TokenExpired as e:
                print(f"[backfill] {label} {e}; pausing 30s", flush=True)
                time.sleep(30)
                continue
            except api_client.RateLimited as e:
                wait = e.retry_after * 2  # back off generously
                print(f"[backfill] {label} rate limited; backing off {wait}s",
                      flush=True)
                time.sleep(wait)
                continue
            content = page.get("content") or []
            if not content:
                print(f"[backfill] {label} reached beginning of history", flush=True)
                break
            for m in content:
                sink.save_chat_message(m, parse_tickers(m))
                saved += 1
            pages += 1
            oldest_ts = content[0].get("createdAt")
            prev = page.get("prevCursor")
            if pages % log_every == 0:
                print(f"[backfill] {label} page {pages}, {saved} saved, "
                      f"back to {oldest_ts}", flush=True)
            if prev is None or prev == cursor:
                print(f"[backfill] {label} no further cursor", flush=True)
                break
            cursor = prev
            if max_pages and pages >= max_pages:
                print(f"[backfill] {label} hit max_pages={max_pages}", flush=True)
                break
            time.sleep(delay)
    except KeyboardInterrupt:
        print(f"\n[backfill] {label} interrupted", flush=True)
    finally:
        if own_sink:
            sink.close()
    summary = {
        "channel": title or channel_id,
        "pages": pages,
        "saved": saved,
        "oldest_ts": oldest_ts,
        "total_now": start_count + saved,
    }
    print(f"[backfill] {label} done: {summary}", flush=True)
    return summary


def backfill_many(channels: list[tuple[str, str]], **kw) -> list[dict]:
    """Backfill several channels sequentially, sharing one sink/connection."""
    sink = Sink()
    results = []
    try:
        for cid, title in channels:
            results.append(backfill_channel(cid, title, sink=sink, **kw))
    finally:
        sink.close()
    return results
