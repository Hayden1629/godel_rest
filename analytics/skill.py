"""User skill leaderboard.

For each user we score their directional calls against forward ABNORMAL
(market-adjusted) returns. A call's signed return = direction * abn_ret, so a
correct bullish or bearish call is positive. We report:

  n_calls        directional mentions with a usable forward return
  hit_rate       raw fraction of calls that were directionally right
  hit_shrunk     beta-binomial shrink of hit_rate toward the global base rate
  mean_abn       mean signed abnormal return per call
  mean_shrunk    mean_abn shrunk toward the global mean (Empirical-Bayes-ish)
  t_stat         mean_abn / standard error  (significance; |t|>~2 ≈ p<0.05)
  skill          ranking score = mean_shrunk scaled by significance

Shrinkage matters: a user with 3 lucky calls must not outrank a user with 200
consistent ones. We pull small samples toward the population.
"""
import sqlite3

import numpy as np
import pandas as pd

from .config import DB_PATH

# pseudo-observations of the prior — higher = more aggressive shrink
PRIOR_STRENGTH = 25.0


def _load(conn, horizon: int) -> pd.DataFrame:
    col = f"abn_{horizon}d"
    q = f"""
        SELECT e.user_id, e.username, e.direction, e.sent_score,
               r.{col} AS abn
        FROM events e
        JOIN event_returns r
          ON r.message_id = e.message_id AND r.series_id = e.series_id
        WHERE e.direction != 0 AND r.{col} IS NOT NULL
    """
    return pd.read_sql_query(q, conn)


def leaderboard(horizon: int = 5, min_calls: int = 5) -> pd.DataFrame:
    conn = sqlite3.connect(str(DB_PATH))
    df = _load(conn, horizon)
    conn.close()
    if df.empty:
        return df

    df["signed"] = df["direction"] * df["abn"]
    df["correct"] = (df["signed"] > 0).astype(int)

    global_mean = df["signed"].mean()
    global_hit = df["correct"].mean()
    k = PRIOR_STRENGTH

    def agg(g):
        n = len(g)
        mean_abn = g["signed"].mean()
        std = g["signed"].std(ddof=1) if n > 1 else np.nan
        se = std / np.sqrt(n) if n > 1 else np.nan
        hits = g["correct"].sum()
        return pd.Series({
            "username": g["username"].iloc[-1],
            "n_calls": n,
            "hit_rate": hits / n,
            "hit_shrunk": (hits + k * global_hit) / (n + k),
            "mean_abn": mean_abn,
            "mean_shrunk": (n * mean_abn + k * global_mean) / (n + k),
            "t_stat": (mean_abn / se) if se and se > 0 else np.nan,
        })

    out = df.groupby("user_id").apply(agg, include_groups=False).reset_index()
    out = out[out["n_calls"] >= min_calls].copy()
    if out.empty:
        return out
    # ranking score: shrunk edge weighted by how much data backs it
    conf = np.sqrt(out["n_calls"] / (out["n_calls"] + k))
    out["skill"] = out["mean_shrunk"] * conf
    out = out.sort_values("skill", ascending=False).reset_index(drop=True)
    out.insert(0, "rank", out.index + 1)
    return out


def user_calls(user_id: str, horizon: int = 5,
               directional_only: bool = False) -> pd.DataFrame:
    """Every ticker mention by a user, with its forward returns and the message
    text. Ordered newest-first."""
    conn = sqlite3.connect(str(DB_PATH))
    df = pd.read_sql_query(
        """SELECT e.ts, e.ticker, e.direction, e.sent_score,
                  r.entry_date, r.ret_1d, r.ret_5d, r.ret_20d,
                  r.abn_1d, r.abn_5d, r.abn_20d,
                  m.content, m.channel_id
           FROM events e
           LEFT JOIN event_returns r
             ON r.message_id = e.message_id AND r.series_id = e.series_id
           JOIN chat_messages m ON m.id = e.message_id
           WHERE e.user_id = ?
           ORDER BY e.ts DESC""", conn, params=(user_id,))
    conn.close()
    if directional_only:
        df = df[df["direction"] != 0]
    if not df.empty:
        col = f"abn_{horizon}d"
        df["signed_abn"] = df["direction"] * df[col]
        df["correct"] = df["signed_abn"].apply(
            lambda v: "" if pd.isna(v) else ("✓" if v > 0 else "✗"))
        df["dir"] = df["direction"].map({1: "▲ bull", -1: "▼ bear", 0: "· none"})
    return df


def user_stats(user_id: str, horizon: int = 5) -> dict:
    df = user_calls(user_id, horizon, directional_only=True)
    col = f"abn_{horizon}d"
    scored = df.dropna(subset=[col]) if not df.empty else df
    if scored.empty:
        return {"calls": 0, "scored": 0}
    signed = scored["direction"] * scored[col]
    return {
        "total_mentions": len(user_calls(user_id, horizon)),
        "directional": len(df),
        "scored": len(scored),
        "hit_rate": float((signed > 0).mean()),
        "mean_abn": float(signed.mean()),
        "best": f"{scored.loc[signed.idxmax(),'ticker']} {signed.max():+.1%}",
        "worst": f"{scored.loc[signed.idxmin(),'ticker']} {signed.min():+.1%}",
    }


def users_index() -> pd.DataFrame:
    """username -> user_id map with mention counts, for lookup widgets."""
    conn = sqlite3.connect(str(DB_PATH))
    df = pd.read_sql_query(
        """SELECT user_id, username, COUNT(*) mentions
           FROM events GROUP BY user_id ORDER BY mentions DESC""", conn)
    conn.close()
    return df


def summary(horizon: int = 5) -> dict:
    conn = sqlite3.connect(str(DB_PATH))
    df = _load(conn, horizon)
    conn.close()
    if df.empty:
        return {"events": 0}
    signed = df["direction"] * df["abn"]
    return {
        "events": len(df),
        "users": df["user_id"].nunique(),
        "global_hit_rate": float((signed > 0).mean()),
        "global_mean_abn": float(signed.mean()),
        "horizon": horizon,
    }
