"""Cluster users by what they trade. Builds a user x ticker mention matrix,
TF-IDF weights it (so common mega-cap mentions don't dominate), reduces with
SVD, and clusters with KMeans. Returns a per-user frame with cluster label,
2-D coordinates for plotting, and the user's top tickers.
"""
import sqlite3

import numpy as np
import pandas as pd
from sklearn.cluster import KMeans
from sklearn.feature_extraction.text import TfidfTransformer
from sklearn.decomposition import TruncatedSVD

from .config import DB_PATH


def user_clusters(min_mentions: int = 15, n_clusters: int = 8,
                  n_components: int = 20) -> pd.DataFrame:
    conn = sqlite3.connect(str(DB_PATH))
    df = pd.read_sql_query(
        "SELECT user_id, username, ticker FROM events", conn)
    conn.close()
    if df.empty:
        return df

    counts = df.groupby("user_id").size()
    keep = counts[counts >= min_mentions].index
    df = df[df["user_id"].isin(keep)]
    if df["user_id"].nunique() < n_clusters:
        n_clusters = max(2, df["user_id"].nunique())

    pivot = (df.groupby(["user_id", "ticker"]).size()
               .unstack(fill_value=0))
    names = (df.drop_duplicates("user_id")
               .set_index("user_id")["username"].reindex(pivot.index))

    X = TfidfTransformer().fit_transform(pivot.values)
    k = min(n_components, min(X.shape) - 1)
    coords = TruncatedSVD(n_components=k, random_state=0).fit_transform(X)
    labels = KMeans(n_clusters=n_clusters, n_init=10,
                    random_state=0).fit_predict(coords)

    # top tickers per user (by raw mention count)
    top = {}
    for uid, row in pivot.iterrows():
        top[uid] = ", ".join(row.sort_values(ascending=False).head(5).index)

    out = pd.DataFrame({
        "user_id": pivot.index,
        "username": names.values,
        "cluster": labels,
        "mentions": pivot.sum(axis=1).values,
        "x": coords[:, 0],
        "y": coords[:, 1] if coords.shape[1] > 1 else np.zeros(len(coords)),
        "top_tickers": [top[u] for u in pivot.index],
    })
    return out.sort_values(["cluster", "mentions"], ascending=[True, False]) \
              .reset_index(drop=True)


def cluster_themes(clusters: pd.DataFrame, top_n: int = 8) -> pd.DataFrame:
    """Most distinctive tickers per cluster (by total mentions in cluster)."""
    if clusters.empty:
        return clusters
    conn = sqlite3.connect(str(DB_PATH))
    ev = pd.read_sql_query("SELECT user_id, ticker FROM events", conn)
    conn.close()
    ev = ev.merge(clusters[["user_id", "cluster"]], on="user_id", how="inner")
    rows = []
    for cl, g in ev.groupby("cluster"):
        tickers = g["ticker"].value_counts().head(top_n)
        rows.append({"cluster": cl, "users": clusters[clusters.cluster == cl].shape[0],
                     "top_tickers": ", ".join(f"{t}({n})" for t, n in tickers.items())})
    return pd.DataFrame(rows)
