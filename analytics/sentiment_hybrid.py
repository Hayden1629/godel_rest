"""Hybrid backend: trust the fast lexicon where it has a clear opinion, and only
escalate the messages it marks NEUTRAL to the Ollama LLM. The lexicon catches
the obvious calls cheaply; the LLM resolves the ambiguous ones — cutting LLM
volume to roughly the neutral fraction while keeping the best of both.
"""
import os

from . import sentiment as lex


def _llm():
    """LLM backend for the neutral escalation. Defaults to OpenRouter (cheap
    cloud Kimi); set HYBRID_LLM=ollama to use a local model instead."""
    if os.environ.get("HYBRID_LLM", "openrouter") == "ollama":
        from . import sentiment_ollama as m
    else:
        from . import sentiment_openrouter as m
    return m


def classify_many(texts: list[str], batch_size: int = 20,
                  model: str | None = None, log=None) -> list[tuple[int, float]]:
    base = lex.classify_many(texts)
    neutral_idx = [i for i, (d, _) in enumerate(base) if d == 0]
    if log:
        log(f"[hybrid] lexicon labelled {len(texts) - len(neutral_idx)} directly; "
            f"escalating {len(neutral_idx)} neutrals to LLM")
    if neutral_idx:
        llm = _llm().classify_many([texts[i] for i in neutral_idx], log=log)
        for i, res in zip(neutral_idx, llm):
            base[i] = res
    return [(d, float(s)) for d, s in base]


def classify(text: str) -> tuple[int, float]:
    return classify_many([text])[0]
