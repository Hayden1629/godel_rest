"""OpenRouter LLM direction backend (default: a cheap Kimi). Cloud-hosted, so it
is fast and inexpensive compared with a big local model — the practical way to
label the whole chat corpus. Batched + lightly concurrent.

Same interface as the other backends: classify_many(texts) -> [(dir, score)].
Set OPENROUTER_KEY in godel/.env; OPENROUTER_MODEL overrides the model.
"""
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor

import requests

from .config import _load_env

_TAG = re.compile(r"\[[A-Z.]+:\d+\]")
_DIR = {"bull": 1, "bear": -1, "neutral": 0}
ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "moonshotai/kimi-k2.5"

_PROMPT = """Label each numbered stock-chat message by the AUTHOR'S directional stance on the stock they mention: bull (long/bullish/expects up), bear (short/bearish/expects down), or neutral (question, news, or no clear opinion). Judge intent, not just keywords.
Return ONLY JSON: {"labels":[{"n":1,"dir":"bull|bear|neutral"}, ...]} with one entry per message.

Messages:
%s"""


def _key() -> str:
    _load_env()
    k = os.environ.get("OPENROUTER_KEY") or os.environ.get("OPENROUTER_API_KEY")
    if not k:
        raise RuntimeError("OPENROUTER_KEY missing from godel/.env")
    return k


def _model() -> str:
    _load_env()
    return os.environ.get("OPENROUTER_MODEL", DEFAULT_MODEL)


def _clean(t: str) -> str:
    return _TAG.sub(" ", t or "").strip() or "."


def _classify_batch(texts, model, key, timeout=90):
    body = "\n".join(f"{i + 1}. {_clean(t)}" for i, t in enumerate(texts))
    out = [0] * len(texts)
    for attempt in range(3):
        try:
            r = requests.post(
                ENDPOINT, headers={"Authorization": f"Bearer {key}"},
                json={"model": model,
                      "messages": [{"role": "user", "content": _PROMPT % body}],
                      "response_format": {"type": "json_object"},
                      "temperature": 0}, timeout=timeout)
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
            labels = json.loads(content).get("labels", [])
            for item in labels:
                n = int(item.get("n", 0)) - 1
                if 0 <= n < len(texts):
                    out[n] = _DIR.get(str(item.get("dir", "")).lower(), 0)
            return out
        except Exception:
            continue
    return out  # neutral fallback on persistent failure


def classify_many(texts, batch_size: int = 20, workers: int = 6,
                  model: str | None = None, log=None) -> list[tuple[int, float]]:
    model = model or _model()
    key = _key()
    batches = [texts[i:i + batch_size] for i in range(0, len(texts), batch_size)]
    results = [None] * len(batches)

    def work(idx):
        results[idx] = _classify_batch(batches[idx], model, key)
        return idx

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for _ in ex.map(work, range(len(batches))):
            done += 1
            if log and done % 20 == 0:
                log(f"[openrouter] {min(done * batch_size, len(texts))}/{len(texts)}")
    out = []
    for r in results:
        out.extend((d, float(d)) for d in r)
    return out


def classify(text: str) -> tuple[int, float]:
    return classify_many([text], workers=1)[0]
