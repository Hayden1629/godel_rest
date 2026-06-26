"""Influence graph from 'mentioned-after' relationships.

For each ticker, mentions are ordered in time; when user B mentions it within a
short window after user A, we add a directed edge A -> B (A may have seeded the
idea). Aggregated across all tickers, PageRank on this graph surfaces users
whose mentions tend to precede others' — an influence/lead proxy.

Caveat: precedence is correlation, not proof of idea origination; treat as a
ranking heuristic, not causal attribution.
"""
import sqlite3
from collections import defaultdict

import pandas as pd

from .config import DB_PATH


def influence(window_minutes: int = 90, max_followers: int = 8,
              min_edges: int = 3) -> tuple[pd.DataFrame, list]:
    import networkx as nx

    conn = sqlite3.connect(str(DB_PATH))
    df = pd.read_sql_query(
        "SELECT ticker, user_id, username, ts FROM events", conn)
    conn.close()
    if df.empty:
        return pd.DataFrame(), []

    df["ts"] = pd.to_datetime(df["ts"], errors="coerce")
    df = df.dropna(subset=["ts"]).sort_values("ts")
    names = dict(zip(df["user_id"], df["username"]))

    window = pd.Timedelta(minutes=window_minutes)
    weights: dict[tuple, float] = defaultdict(float)

    for _, g in df.groupby("ticker"):
        recs = g[["ts", "user_id"]].to_records(index=False)
        n = len(recs)
        for i in range(n):
            ts_i, u_i = recs[i]
            followers = 0
            for j in range(i + 1, n):
                ts_j, u_j = recs[j]
                if ts_j - ts_i > window:
                    break
                if u_j == u_i:
                    continue
                weights[(u_i, u_j)] += 1.0
                followers += 1
                if followers >= max_followers:
                    break

    G = nx.DiGraph()
    for (a, b), w in weights.items():
        if w >= min_edges:
            G.add_edge(a, b, weight=w)
    if G.number_of_nodes() == 0:
        return pd.DataFrame(), []

    pr = nx.pagerank(G, weight="weight")
    out_inf = {n: sum(d["weight"] for _, _, d in G.out_edges(n, data=True))
               for n in G.nodes}
    in_inf = {n: sum(d["weight"] for _, _, d in G.in_edges(n, data=True))
              for n in G.nodes}
    nodes = pd.DataFrame({
        "user_id": list(G.nodes),
        "username": [names.get(n, n) for n in G.nodes],
        "pagerank": [pr[n] for n in G.nodes],
        "led_others": [out_inf[n] for n in G.nodes],
        "followed_others": [in_inf[n] for n in G.nodes],
    })
    nodes["lead_ratio"] = (nodes["led_others"]
                           / (nodes["followed_others"] + 1.0)).round(2)
    nodes = nodes.sort_values("pagerank", ascending=False).reset_index(drop=True)
    edges = [(names.get(a, a), names.get(b, b), w) for (a, b), w in weights.items()
             if w >= min_edges]
    return nodes, edges
