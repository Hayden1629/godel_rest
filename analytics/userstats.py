"""General activity statistics over chat_messages. Secondary to alpha, but
cheap and useful for context. All timestamps are the raw createdAt (UTC)."""
import sqlite3

import pandas as pd

from .config import DB_PATH


def _messages(channel_ids=None) -> pd.DataFrame:
    conn = sqlite3.connect(str(DB_PATH))
    q = "SELECT id, channel_id, username, created_at FROM chat_messages"
    params = ()
    if channel_ids:
        q += " WHERE channel_id IN (%s)" % ",".join("?" * len(channel_ids))
        params = tuple(channel_ids)
    df = pd.read_sql_query(q, conn, params=params)
    conn.close()
    df["ts"] = pd.to_datetime(df["created_at"], errors="coerce")
    df = df.dropna(subset=["ts"])
    df["date"] = df["ts"].dt.date
    df["hour"] = df["ts"].dt.hour
    df["dow"] = df["ts"].dt.day_name()
    return df


def top_senders(channel_ids=None, limit: int = 30) -> pd.DataFrame:
    df = _messages(channel_ids)
    g = (df.groupby("username")
           .agg(messages=("id", "count"),
                active_days=("date", "nunique"),
                first_seen=("ts", "min"),
                last_seen=("ts", "max"))
           .reset_index())
    g["msgs_per_active_day"] = (g["messages"] / g["active_days"]).round(1)
    return g.sort_values("messages", ascending=False).head(limit).reset_index(drop=True)


def messages_per_day(channel_ids=None) -> pd.DataFrame:
    df = _messages(channel_ids)
    g = (df.groupby("date")
           .agg(messages=("id", "count"), active_users=("username", "nunique"))
           .reset_index())
    return g.sort_values("date")


def hour_histogram(channel_ids=None) -> pd.DataFrame:
    df = _messages(channel_ids)
    g = df.groupby("hour").size().reindex(range(24), fill_value=0)
    return g.rename("messages").reset_index()


def dow_histogram(channel_ids=None) -> pd.DataFrame:
    order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
             "Saturday", "Sunday"]
    df = _messages(channel_ids)
    g = df.groupby("dow").size().reindex(order, fill_value=0)
    return g.rename("messages").reset_index()


def overview(channel_ids=None) -> dict:
    df = _messages(channel_ids)
    if df.empty:
        return {"messages": 0}
    per_day = df.groupby("date").size()
    return {
        "messages": int(len(df)),
        "users": int(df["username"].nunique()),
        "days": int(df["date"].nunique()),
        "avg_msgs_per_day": float(per_day.mean()),
        "busiest_day": str(per_day.idxmax()),
        "busiest_day_count": int(per_day.max()),
        "date_range": f"{df['ts'].min():%Y-%m-%d} → {df['ts'].max():%Y-%m-%d}",
    }
