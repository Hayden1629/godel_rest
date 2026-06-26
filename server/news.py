"""News + trending feeds on api.godelterminal.com (text-digestible only).

    GET /api/v1/top-news-items[?important=true]   curated news feed (Reuters …)
    GET /api/v1/trending?timeframe=24H            trending tickers w/ mention counts

Schemas confirmed live 2026-06-19. `fetchBreaking` from the old HAR is gone
(404); discover any replacement with `python -m server.discover` (extension
endpoint logger) then add it here.
"""
import time

from . import api_client
from .sink import Sink


# ── top news ──────────────────────────────────────────────────────────────────

def _norm_top(item: dict) -> dict:
    """Normalize a top-news item into the news_items sink shape."""
    enriched = dict(item)
    enriched["_source"] = "top"
    enriched["type"] = item.get("providerName") or item.get("feedId")
    enriched["time"] = item.get("receivedTime") or item.get("publicationTime")
    title = item.get("title") or ""
    desc = item.get("description") or ""
    enriched["_content"] = f"{title} — {desc}" if desc else title
    enriched["_symbols"] = [str(s) for s in (item.get("securityIds") or [])]
    enriched["important"] = item.get("important", False)
    return enriched


def fetch_top_news(important: bool = False) -> list[dict]:
    params = {"important": "true"} if important else None
    payload = api_client.get("/api/v1/top-news-items", params=params)
    items = payload.get("items", []) if isinstance(payload, dict) else (payload or [])
    return [_norm_top(i) for i in items]


def poll_news(interval: float = 30.0, important: bool = False,
              sink: Sink | None = None, on_item=None) -> None:
    """Poll the top-news feed, emitting only ids not seen before."""
    own_sink = sink is None
    sink = sink or Sink()
    print(f"[news] polling top-news-items every {interval}s "
          f"(important_only={important})", flush=True)
    try:
        while True:
            try:
                for item in fetch_top_news(important=important):
                    if sink.save_news_item(item):
                        (on_item or _print_item)(item)
            except api_client.TokenExpired as e:
                print(f"[news] {e}; waiting for fresh token…", flush=True)
            except Exception as e:
                print(f"[news] error: {e}", flush=True)
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n[news] stopped", flush=True)
    finally:
        if own_sink:
            sink.close()


def _print_item(item: dict) -> None:
    flag = "!" if item.get("important") else " "
    syms = ",".join(item.get("_symbols") or [])
    head = f"[{item.get('type','news')}]{flag} {item.get('time','')}"
    print(f"{head} {item.get('_content','')}" + (f"  ({syms})" if syms else ""),
          flush=True)


# ── trending ──────────────────────────────────────────────────────────────────

def fetch_trending(timeframe: str = "24H") -> list[dict]:
    """Return trending tickers ranked by total mentions over the timeframe."""
    rows = api_client.get("/api/v1/trending", params={"timeframe": timeframe})
    out = []
    for r in rows:
        ctx = r.get("seriesContext") or {}
        instrument = ctx.get("instrument") or {}
        out.append(
            {
                "ticker": r.get("ticker"),
                "total": r.get("totalCount"),
                "series_id": r.get("seriesId"),
                "name": instrument.get("name"),
            }
        )
    out.sort(key=lambda x: x["total"] or 0, reverse=True)
    return out
