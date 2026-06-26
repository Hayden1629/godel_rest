"""Lightweight finance-direction classifier (lexicon baseline).

This is intentionally swappable: it turns a message into a signed direction in
{-1, 0, +1} plus a raw score. It is the weakest link in the whole pipeline — an
LLM/FinBERT pass can replace `classify()` later without touching anything else.
"""
import re

BULL = {
    "long", "longs", "calls", "call", "buy", "buying", "bought", "bull", "bullish",
    "moon", "mooning", "breakout", "squeeze", "ripping", "rip", "bid", "bidding",
    "accumulate", "undervalued", "cheap", "load", "loading", "send", "sending",
    "pump", "pumping", "green", "up", "higher", "rally", "rallying", "support",
    "bottom", "bottomed", "oversold", "beat", "beats", "upgrade", "upside",
    "added", "adding", "scooped", "scoop", "yolo", "printing", "prints", "winner",
    "strong", "strength", "gap", "gapping", "runner", "running", "all-time",
}
BEAR = {
    "short", "shorts", "shorting", "puts", "put", "sell", "selling", "sold",
    "bear", "bearish", "dump", "dumping", "dumped", "crash", "crashing",
    "overvalued", "expensive", "fade", "fading", "top", "topped", "breakdown",
    "rug", "rugged", "red", "down", "lower", "drop", "dropping", "tank",
    "tanking", "miss", "misses", "downgrade", "downside", "weak", "weakness",
    "resistance", "overbought", "bagholder", "bagholding", "rekt", "loser",
    "puke", "puking", "collapse", "collapsing", "bleeding", "bleed",
}
# negators flip the immediately-following sentiment word
NEGATORS = {"not", "no", "never", "isn't", "isnt", "aint", "ain't", "dont",
            "don't", "doesn't", "doesnt", "won't", "wont"}

TICKER_TAG = re.compile(r"\[[A-Z.]+:\d+\]")
WORD = re.compile(r"[a-z'\-]+")


def _tokens(text: str) -> list[str]:
    text = TICKER_TAG.sub(" ", text or "")
    return WORD.findall(text.lower())


def score(text: str) -> int:
    """Net bull(+)/bear(-) word count with simple negation handling."""
    toks = _tokens(text)
    s = 0
    negate = False
    for tok in toks:
        if tok in NEGATORS:
            negate = True
            continue
        val = 0
        if tok in BULL:
            val = 1
        elif tok in BEAR:
            val = -1
        if val:
            s += -val if negate else val
        negate = False
    return s


def classify(text: str) -> tuple[int, int]:
    """Return (direction in {-1,0,1}, raw_score)."""
    s = score(text)
    return ((1 if s > 0 else -1 if s < 0 else 0), s)


def classify_many(texts: list[str]) -> list[tuple[int, int]]:
    """Batch interface mirroring the FinBERT backend."""
    return [classify(t) for t in texts]
