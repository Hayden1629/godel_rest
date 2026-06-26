"""Ticker-level signals built from the event stream.

  recent_buzz        attention spike: recent mention count vs its own baseline
  smart_sentiment    net direction weighted by each speaker's skill score
                     (the "smart money" signal — denoises the raw crowd)
"""
import sqlite3

import numpy as np
import pandas as pd

from .config import DB_PATH
from . import skill


def _events(conn) -> pd.DataFrame:
    df = pd.read_sql_query(
        "SELECT message_id, ticker, user_id, direction, ts FROM events", conn)
    df["ts"] = pd.to_datetime(df["ts"], errors="coerce")
    return df.dropna(subset=["ts"])


def recent_buzz(recent_days: int = 3, baseline_days: int = 30,
                min_mentions: int = 5) -> pd.DataFrame:
    conn = sqlite3.connect(str(DB_PATH))
    df = _events(conn)
    conn.close()
    if df.empty:
        return df
    tmax = df["ts"].max()
    recent_cut = tmax - pd.Timedelta(days=recent_days)
    base_cut = tmax - pd.Timedelta(days=baseline_days)

    recent = df[df["ts"] >= recent_cut].groupby("ticker").size()
    base = df[(df["ts"] >= base_cut) & (df["ts"] < recent_cut)]
    base_daily = base.groupby("ticker").size() / max(baseline_days - recent_days, 1)

    out = pd.DataFrame({"recent": recent}).fillna(0)
    out["baseline_per_day"] = base_daily.reindex(out.index).fillna(0)
    out["expected"] = out["baseline_per_day"] * recent_days
    out["excess"] = out["recent"] - out["expected"]
    # Poisson-ish z-score of the spike
    out["z"] = out["excess"] / np.sqrt(out["expected"].clip(lower=1.0))
    out = out[out["recent"] >= min_mentions]
    return out.sort_values("z", ascending=False).reset_index()


def smart_sentiment(horizon: int = 5, window_days: int = 7,
                    min_calls_for_weight: int = 5) -> pd.DataFrame:
    """Per ticker over the recent window: net crowd direction and
    skill-weighted direction (weight = each user's shrunk edge)."""
    lb = skill.leaderboard(horizon=horizon, min_calls=min_calls_for_weight)
    weight = (dict(zip(lb["user_id"], lb["mean_shrunk"]))
              if not lb.empty else {})

    conn = sqlite3.connect(str(DB_PATH))
    df = _events(conn)
    conn.close()
    if df.empty:
        return df
    df = df[df["direction"] != 0].copy()
    tmax = df["ts"].max()
    df = df[df["ts"] >= tmax - pd.Timedelta(days=window_days)]
    if df.empty:
        return df
    df["w"] = df["user_id"].map(weight).fillna(0.0)
    df["weighted"] = df["direction"] * df["w"]

    g = (df.groupby("ticker")
           .agg(mentions=("direction", "size"),
                net_direction=("direction", "sum"),
                bulls=("direction", lambda s: int((s > 0).sum())),
                bears=("direction", lambda s: int((s < 0).sum())),
                smart_score=("weighted", "sum"))
           .reset_index())
    g["smart_per_mention"] = g["smart_score"] / g["mentions"]
    return g.sort_values("smart_score", ascending=False).reset_index(drop=True)
