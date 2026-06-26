"""FinBERT direction backend — a finance-tuned BERT sentiment classifier
(ProsusAI/finbert). Same interface as the lexicon backend: text -> (direction,
score), but far better at nuanced messages.

Lightweight on a Mac: ~110M params, ~440MB download (once), runs on the Apple
Silicon GPU (MPS) if available. Loads lazily so importing this module is cheap.
"""
import re

_TICKER_TAG = re.compile(r"\[[A-Z.]+:\d+\]")
_MODEL = "ProsusAI/finbert"
_pipe = None
_LABEL = {"positive": 1, "negative": -1, "neutral": 0}


def _clean(text: str) -> str:
    return _TICKER_TAG.sub(" ", text or "").strip() or "."


def _load():
    global _pipe
    if _pipe is not None:
        return _pipe
    import torch
    from transformers import pipeline
    device = 0 if torch.backends.mps.is_available() else -1
    # transformers maps device=0 to the first accelerator; force mps explicitly
    dev = "mps" if torch.backends.mps.is_available() else "cpu"
    _pipe = pipeline("text-classification", model=_MODEL, top_k=None,
                     truncation=True, max_length=256, device=dev)
    return _pipe


def classify_many(texts: list[str], batch_size: int = 32) -> list[tuple[int, float]]:
    """Classify a batch. Returns (direction in {-1,0,1}, signed confidence)."""
    pipe = _load()
    cleaned = [_clean(t) for t in texts]
    results = pipe(cleaned, batch_size=batch_size)
    out = []
    for scores in results:
        # scores: list of {label, score} for positive/negative/neutral
        prob = {s["label"].lower(): s["score"] for s in scores}
        top = max(prob, key=prob.get)
        direction = _LABEL.get(top, 0)
        signed = prob.get("positive", 0.0) - prob.get("negative", 0.0)
        out.append((direction, round(signed, 4)))
    return out


def classify(text: str) -> tuple[int, float]:
    return classify_many([text])[0]
