"""Ollama LLM direction backend — understands trader slang ("BUY QBTS!!!", "re
short MU", "loading calls") that the lexicon and FinBERT both miss.

Batched + JSON-schema-constrained so each request labels many messages at once.
Model/host are configurable; pick a small fast model (llama3.1:8b, qwen2.5:7b)
for the full corpus, or a big one (gemma4:26b) for a small high-value subset.
"""
import json
import os
import re

import requests

from .config import REPO_ROOT

_TAG = re.compile(r"\[[A-Z.]+:\d+\]")
_DIR = {"bull": 1, "bear": -1, "neutral": 0}

# JSON schema forces the model to return one label per input message.
_SCHEMA = {
    "type": "object",
    "properties": {
        "labels": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "n": {"type": "integer"},
                    "dir": {"type": "string", "enum": ["bull", "bear", "neutral"]},
                },
                "required": ["n", "dir"],
            },
        }
    },
    "required": ["labels"],
}

_PROMPT = """You label stock-trader chat messages by the AUTHOR'S directional stance on the stock they mention.
- bull = author is long / bullish / expects it up (buy, calls, "loading up", "rips", "squeeze").
- bear = author is short / bearish / expects it down (short, puts, "dumping", "going to zero", "selling").
- neutral = a question, news with no stance, or no clear directional opinion.
Judge intent, not just keywords. Return one label per numbered message.

Messages:
{body}"""


def _host() -> str:
    return os.environ.get("OLLAMA_HOST", "http://localhost:11434")


def _model() -> str:
    return os.environ.get("OLLAMA_MODEL", "llama3.1:8b")


def _clean(text: str) -> str:
    return _TAG.sub(" ", text or "").strip() or "."


def _post(body: str, model: str, timeout: int):
    return requests.post(
        f"{_host()}/api/generate",
        json={"model": model, "prompt": _PROMPT.format(body=body),
              "stream": False, "format": _SCHEMA, "keep_alive": "30m",
              "options": {"temperature": 0}},
        timeout=timeout)


def _classify_batch(texts: list[str], model: str, timeout: int,
                    log=None) -> list[int]:
    """Classify one batch. A slow/failed call must never kill a long run, so we
    retry once with a longer timeout and then fall back to neutral (0)."""
    body = "\n".join(f"{i + 1}. {_clean(t)}" for i, t in enumerate(texts))
    for attempt, t in enumerate((timeout, timeout * 2)):
        try:
            r = _post(body, model, t)
            r.raise_for_status()
            out = [0] * len(texts)
            labels = json.loads(r.json()["response"]).get("labels", [])
            for item in labels:
                n = int(item.get("n", 0)) - 1
                if 0 <= n < len(texts):
                    out[n] = _DIR.get(str(item.get("dir", "")).lower(), 0)
            return out
        except Exception as e:
            if log:
                log(f"[ollama] batch error (attempt {attempt + 1}): {e}")
            continue
    if log:
        log(f"[ollama] batch failed twice, defaulting {len(texts)} to neutral")
    return [0] * len(texts)


def warmup(model: str | None = None, timeout: int = 600) -> None:
    """Force the model to load into memory so the first real batch doesn't time
    out (loading a 26B model can take a while)."""
    try:
        _post("1. warmup", model or _model(), timeout)
    except Exception:
        pass


def classify_many(texts: list[str], batch_size: int = 15,
                  model: str | None = None, timeout: int = 300,
                  log=None) -> list[tuple[int, float]]:
    model = model or _model()
    out: list[tuple[int, float]] = []
    total = len(texts)
    for i in range(0, total, batch_size):
        chunk = texts[i:i + batch_size]
        dirs = _classify_batch(chunk, model, timeout, log=log)
        out.extend((d, float(d)) for d in dirs)
        if log and (i // batch_size) % 20 == 0:
            log(f"[ollama] {min(i + batch_size, total)}/{total} classified")
    return out


def classify(text: str) -> tuple[int, float]:
    return classify_many([text])[0]
