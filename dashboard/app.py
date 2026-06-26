"""Gödel chat alpha dashboard.

Run:  streamlit run dashboard/app.py
Refresh data (incremental) from the sidebar button or:
      python3 -m analytics.run refresh
"""
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analytics import (clustering, network, pipeline, signals, skill,  # noqa: E402
                       userstats)
from analytics.config import DB_PATH  # noqa: E402

st.set_page_config(page_title="Gödel Chat Alpha", layout="wide")


@st.cache_data(ttl=300)
def _overview():
    return userstats.overview()


@st.cache_data(ttl=300)
def _skill(horizon, min_calls):
    return skill.leaderboard(horizon=horizon, min_calls=min_calls), \
        skill.summary(horizon=horizon)


@st.cache_data(ttl=300)
def _users_index():
    return skill.users_index()


@st.cache_data(ttl=300)
def _user_calls(user_id, horizon):
    return skill.user_calls(user_id, horizon), skill.user_stats(user_id, horizon)


@st.cache_data(ttl=300)
def _smart(horizon, window):
    return signals.smart_sentiment(horizon=horizon, window_days=window)


@st.cache_data(ttl=300)
def _buzz(recent, baseline):
    return signals.recent_buzz(recent_days=recent, baseline_days=baseline)


@st.cache_data(ttl=300)
def _clusters(min_mentions, n_clusters):
    c = clustering.user_clusters(min_mentions=min_mentions, n_clusters=n_clusters)
    return c, clustering.cluster_themes(c)


@st.cache_data(ttl=300)
def _network(window):
    return network.influence(window_minutes=window)


@st.cache_data(ttl=300)
def _activity():
    return (userstats.top_senders(), userstats.messages_per_day(),
            userstats.hour_histogram(), userstats.dow_histogram())


# ── sidebar ──────────────────────────────────────────────────────────────────
st.sidebar.title("Gödel Chat Alpha")
st.sidebar.caption(f"DB: {DB_PATH.name}")
horizon = st.sidebar.selectbox("Forward-return horizon (trading days)",
                               [1, 5, 20], index=1)
min_calls = st.sidebar.slider("Min calls per user", 3, 50, 8)

if st.sidebar.button("↻ Refresh data (incremental)"):
    with st.spinner("Refreshing events → prices → returns…"):
        logs = []
        pipeline.refresh(log=logs.append)
        st.cache_data.clear()
    st.sidebar.success("Refreshed")
    st.sidebar.code("\n".join(logs[-12:]))

st.sidebar.markdown("---")
st.sidebar.caption("⚠️ Direction labels are lexicon-based (noisy). Treat skill "
                   "scores as hypotheses, not P&L.")

# ── header ───────────────────────────────────────────────────────────────────
ov = _overview()
st.title("Gödel Chat Alpha")
if ov.get("messages"):
    c = st.columns(5)
    c[0].metric("Messages", f"{ov['messages']:,}")
    c[1].metric("Users", f"{ov['users']:,}")
    c[2].metric("Days", ov["days"])
    c[3].metric("Avg msgs/day", f"{ov['avg_msgs_per_day']:.0f}")
    c[4].metric("Busiest day", ov["busiest_day_count"], ov["busiest_day"])
    st.caption(f"Range: {ov['date_range']}")

def show_user(user_id, username, horizon):
    """Drill-down panel: a single user's calls + their forward returns."""
    calls, stats = _user_calls(user_id, horizon)
    st.markdown(f"### 👤 {username}")
    if not stats.get("scored"):
        st.info("No scored directional calls for this user yet.")
    else:
        m = st.columns(6)
        m[0].metric("Mentions", stats["total_mentions"])
        m[1].metric("Directional", stats["directional"])
        m[2].metric("Scored", stats["scored"])
        m[3].metric(f"Hit rate ({horizon}d)", f"{stats['hit_rate']:.1%}")
        m[4].metric("Mean abn", f"{stats['mean_abn']:+.2%}")
        m[5].metric("Best / worst", stats["best"], stats["worst"],
                    delta_color="off")
    if calls is None or calls.empty:
        return
    only_dir = st.checkbox("Directional calls only", value=True,
                           key=f"od_{user_id}")
    view = calls[calls["direction"] != 0] if only_dir else calls
    disp = view[["ts", "ticker", "dir", "entry_date",
                 f"abn_{horizon}d", "correct", "content"]].copy()
    disp = disp.rename(columns={"ts": "time", f"abn_{horizon}d": f"abn_{horizon}d",
                                "dir": "call", "content": "message"})
    disp[f"abn_{horizon}d"] = view[f"abn_{horizon}d"].map(
        lambda v: "" if pd.isna(v) else f"{v:+.1%}")
    st.dataframe(disp, use_container_width=True, hide_index=True, height=460)
    st.caption("`abn` = market-adjusted forward return from the entry day. "
               "`✓` = the call's direction was right.")


tabs = st.tabs(["🏆 Skill", "📡 Signals", "🧩 Clusters", "🕸 Influence",
                "📊 Activity", "👤 User lookup"])

# ── Skill leaderboard ────────────────────────────────────────────────────────
with tabs[0]:
    st.subheader(f"User skill — {horizon}-day market-adjusted returns")
    lb, summ = _skill(horizon, min_calls)
    if summ.get("events"):
        c = st.columns(3)
        c[0].metric("Scored calls", f"{summ['events']:,}")
        c[1].metric("Global hit rate", f"{summ['global_hit_rate']:.1%}")
        c[2].metric("Global mean abn", f"{summ['global_mean_abn']:+.2%}")
    if lb is None or lb.empty:
        st.info("No scored calls yet — run a refresh so prices/returns exist.")
    else:
        show = lb.copy()
        for col in ("hit_rate", "hit_shrunk", "mean_abn", "mean_shrunk", "skill"):
            show[col] = show[col].map(lambda v: f"{v:+.2%}" if "mean" in col
                                      or col == "skill" else f"{v:.1%}")
        show["t_stat"] = lb["t_stat"].map(lambda v: f"{v:.2f}")
        st.caption("👇 **Click a row to see that user's calls.** "
                   "`mean_shrunk` = edge after shrinking small samples toward the "
                   "crowd. `t_stat` > ~2 ≈ statistically notable.")
        event = st.dataframe(
            show[["rank", "username", "n_calls", "hit_rate", "hit_shrunk",
                  "mean_abn", "mean_shrunk", "t_stat", "skill"]],
            use_container_width=True, hide_index=True, height=480,
            on_select="rerun", selection_mode="single-row", key="lb_select")
        rows = event.selection.rows if event and event.selection else []
        if rows:
            sel = lb.iloc[rows[0]]
            st.markdown("---")
            show_user(sel["user_id"], sel["username"], horizon)

# ── Signals ──────────────────────────────────────────────────────────────────
with tabs[1]:
    col1, col2 = st.columns(2)
    with col1:
        st.subheader("Smart-money sentiment (recent)")
        window = st.slider("Window (days)", 1, 30, 7, key="smart_win")
        sm = _smart(horizon, window)
        if sm is None or sm.empty:
            st.info("No data yet.")
        else:
            st.dataframe(sm.head(40), use_container_width=True, hide_index=True,
                         height=480)
            st.caption("`smart_score` = Σ direction × speaker-skill. Positive = "
                       "skilled users net-bullish.")
    with col2:
        st.subheader("Attention spikes (buzz)")
        rd = st.slider("Recent days", 1, 7, 3, key="buzz_recent")
        bl = st.slider("Baseline days", 10, 60, 30, key="buzz_base")
        bz = _buzz(rd, bl)
        if bz is None or bz.empty:
            st.info("No data yet.")
        else:
            st.dataframe(bz.head(40), use_container_width=True, hide_index=True,
                         height=480)
            st.caption("`z` = how unusual recent mention volume is vs the "
                       "ticker's own baseline.")

# ── Clusters ─────────────────────────────────────────────────────────────────
with tabs[2]:
    st.subheader("User clusters — who trades alike")
    cc1, cc2 = st.columns(2)
    minm = cc1.slider("Min mentions", 5, 50, 15, key="cl_min")
    ncl = cc2.slider("Clusters", 3, 15, 8, key="cl_n")
    cl, themes = _clusters(minm, ncl)
    if cl is None or cl.empty:
        st.info("Not enough data yet.")
    else:
        try:
            import plotly.express as px
            fig = px.scatter(cl, x="x", y="y", color=cl["cluster"].astype(str),
                             size="mentions", hover_name="username",
                             hover_data=["top_tickers"],
                             labels={"color": "cluster"}, height=480)
            st.plotly_chart(fig, use_container_width=True)
        except Exception:
            st.dataframe(cl, use_container_width=True, hide_index=True)
        st.markdown("**Cluster themes**")
        st.dataframe(themes, use_container_width=True, hide_index=True)

# ── Influence ────────────────────────────────────────────────────────────────
with tabs[3]:
    st.subheader("Influence — who mentions tickers before others")
    win = st.slider("Follow window (minutes)", 15, 240, 90, key="net_win")
    nodes, edges = _network(win)
    if nodes is None or nodes.empty:
        st.info("Not enough data yet.")
    else:
        st.dataframe(nodes.head(40), use_container_width=True, hide_index=True,
                     height=480)
        st.caption("`pagerank` = influence in the mentioned-after graph. "
                   "`lead_ratio` > 1 = leads more than follows.")

# ── Activity ─────────────────────────────────────────────────────────────────
with tabs[4]:
    st.subheader("General activity")
    senders, per_day, hours, dow = _activity()
    a1, a2 = st.columns(2)
    with a1:
        st.markdown("**Messages per day**")
        st.line_chart(per_day.set_index("date")[["messages", "active_users"]])
        st.markdown("**Messages by hour (UTC)**")
        st.bar_chart(hours.set_index("hour"))
    with a2:
        st.markdown("**Messages by weekday**")
        st.bar_chart(dow.set_index("dow"))
        st.markdown("**Top senders**")
        st.dataframe(senders, use_container_width=True, hide_index=True,
                     height=300)

# ── User lookup ──────────────────────────────────────────────────────────────
with tabs[5]:
    st.subheader("Look up any user's calls")
    idx = _users_index()
    if idx.empty:
        st.info("No events yet — run a refresh.")
    else:
        labels = [f"{r.username}  ({r.mentions} mentions)"
                  for r in idx.itertuples()]
        choice = st.selectbox("User", options=range(len(idx)),
                              format_func=lambda i: labels[i])
        row = idx.iloc[choice]
        st.markdown("---")
        show_user(row["user_id"], row["username"], horizon)
