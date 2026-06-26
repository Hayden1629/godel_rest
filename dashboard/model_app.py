"""Interactive financial-model dashboard.

Type a ticker, get the full deterministic model: quarterly statements pulled live
from Gödel Terminal, trend charts (revenue growth, margins, real free cash flow),
and an EV-based reverse DCF showing the FCF growth the market is pricing in.

Run (bare `streamlit` shebang is broken on this Mac → use the module form):
    python3 -m streamlit run dashboard/model_app.py
"""
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import PercentFormatter
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analytics import financials as F  # noqa: E402
from analytics import model as M  # noqa: E402
from server import api_client  # noqa: E402

st.set_page_config(page_title="Reverse-DCF Model", layout="wide")

# metrics rendered as percentages (vs absolute dollars)
_PCT = F._IS_PCT


@st.cache_data(ttl=900, show_spinner=False)
def _load(ticker: str, period: str):
    """Pull statements, valuation inputs and company profile in one shot, and
    snapshot every API call made along the way (cached 15 min). All the network
    I/O happens here so the under-the-hood log reflects exactly this ticker."""
    api_client.clear_calls()
    frames = M.download_statements(ticker, period)
    model_df = M.build_model_frame(frames)
    val = M.build_valuation(ticker, model_df)
    card = M.company_card(ticker)
    chart = M.price_chart(ticker)
    calls = api_client.recent_calls(300)
    return frames, model_df, val, card, chart, calls


def _metric_fig(model_df, item, forecast_periods):
    """One actual+forecast chart for a single metric (compact)."""
    s = model_df[item].dropna()
    fig, ax = plt.subplots(figsize=(3.6, 2.2))
    ax.plot(s.index, s.values, color="#1f77b4", lw=1.3, label="actual")
    if forecast_periods:
        fc, std = F.forecast(s, forecast_periods)
        if len(fc):
            ax.plot(fc.index, fc.values, color="#d62728", lw=1.3, ls="--",
                    label="forecast")
            ax.fill_between(fc.index, fc.values - std, fc.values + std,
                           color="#d62728", alpha=0.15)
    ax.set_title(F._PRETTY.get(item, item), fontsize=9)
    ax.grid(alpha=0.3)
    ax.axhline(0, color="#888", lw=0.6)
    ax.tick_params(labelsize=7)
    if item in _PCT:
        ax.yaxis.set_major_formatter(PercentFormatter(1.0))
    ax.legend(loc="best", fontsize=6)
    fig.tight_layout()
    return fig


def _fmt_usd(x, scale=1.0, suffix="B"):
    return "—" if x is None else f"${x/scale:,.1f}{suffix}"


# ── sidebar controls ──────────────────────────────────────────────────────────

st.sidebar.title("📊 Reverse-DCF Model")
ticker = st.sidebar.text_input("Ticker", value="BSX").strip().upper()
period = st.sidebar.selectbox("Period", ["QTR", "ANN"], index=0)
forecast = st.sidebar.slider("Forecast quarters", 0, 16, 8)
st.sidebar.markdown("**Reverse-DCF assumptions**")
st.sidebar.caption(
    "The reverse DCF *solves for the growth rate*. But you still pick the other "
    "knobs: the discount rate (your required return) and terminal growth (steady "
    "long-run rate). Given those, it finds the FCF growth that justifies today's "
    "EV. Higher discount or lower terminal ⇒ higher implied growth.")
discount = st.sidebar.slider("Discount rate", 0.05, 0.20, 0.10, 0.005,
                             help="Required annual return / WACC used to discount "
                                  "future cash flows.")
terminal = st.sidebar.slider("Terminal growth", 0.00, 0.05, 0.025, 0.005,
                             help="Perpetual growth after the explicit forecast "
                                  "window (~GDP/inflation, e.g. 2.5%).")
years = st.sidebar.slider("Explicit years", 5, 15, 10,
                          help="Length of the explicit high-growth window before "
                               "the terminal value kicks in.")
go = st.sidebar.button("Run model", type="primary")

if not (go or ticker):
    st.info("Enter a ticker in the sidebar and hit **Run model**.")
    st.stop()

try:
    with st.spinner(f"Pulling {ticker} from Gödel + Massive…"):
        frames, model_df, val, card, price_chart, calls = _load(ticker, period)
    if model_df.empty:
        st.error(f"No financial data returned for {ticker!r}.")
        st.stop()
    # reverse DCF is pure math on already-fetched data — recompute live as the
    # assumption sliders move, no refetch needed.
    rdcf = M.reverse_dcf(val, model_df, discount, terminal, years)
except Exception as e:  # surface a clean message instead of a stack trace
    st.error(f"Failed to load {ticker!r}: {type(e).__name__}: {e}")
    st.stop()

# ── header: company + valuation + reverse DCF ─────────────────────────────────

st.title(f"{ticker} — {card.get('name') or 'financial model'}")
meta = " · ".join(str(x) for x in (card.get("sector"), card.get("exchange"),
                  f"{card['employees']:,} employees" if card.get("employees")
                  else None) if x)
if meta:
    st.caption(meta)
if card.get("description"):
    with st.expander("Company summary", expanded=True):
        st.write(card["description"])
        if card.get("homepage"):
            st.markdown(f"[{card['homepage']}]({card['homepage']})")

# market snapshot row (the DES-style stats)
def _pct(v):
    return "—" if v is None else f"{v:+.1%}"

m = st.columns(5)
beta = card.get("beta")
beta_txt = "—" if beta is None else f"{beta:.2f}"
if card.get("low_confidence_beta"):
    beta_txt += " ⚠"
m[0].metric("Beta (1y vs SPY)", beta_txt)
m[1].metric("YTD return", _pct(card.get("ytd")))
m[2].metric("1Y return", _pct(card.get("ret_1y")))
m[3].metric("Annual vol", _pct(card.get("annual_vol")))
rng = (f"${card['low_52w']:.0f}–${card['high_52w']:.0f}"
       if card.get("low_52w") and card.get("high_52w") else "—")
m[4].metric("52-week range", rng)

if price_chart and Path(price_chart).exists():
    st.image(price_chart, caption=f"{ticker} price + SMA50/200", width=720)

st.subheader("Valuation")
c = st.columns(4)
c[0].metric("Price (last close)", _fmt_usd(val.price, 1, ""))
c[1].metric("Market cap", _fmt_usd(val.market_cap, 1e9))
c[2].metric("Enterprise value", _fmt_usd(val.enterprise_value, 1e9))
c[3].metric("TTM free cash flow",
            _fmt_usd(val.fcf_ttm * 1e6 if val.fcf_ttm else None, 1e9))

if "error" in rdcf:
    st.warning(f"Reverse DCF: {rdcf['error']}")
else:
    d = st.columns(3)
    d[0].metric("EV / FCF", f"{rdcf['ev_to_fcf']}x")
    d[1].metric("Implied FCF growth (priced in)",
                f"{rdcf['implied_fcf_growth']:.1%}")
    hist = rdcf.get("historical_fcf_cagr")
    d[2].metric("Historical FCF CAGR",
                "—" if hist is None else f"{hist:.1%}")
    st.info(rdcf["read"])

st.caption(f"EV = market cap + net debt (${(val.net_debt or 0)/1e3:,.1f}B). "
           f"Price source: {val.source_price}. FCF = operating cash flow − capex.")

# ── trend charts ──────────────────────────────────────────────────────────────

st.subheader("Trends & forecast")
income_items = M.chartable(model_df, M.MODEL_ITEMS)
balance_items = M.chartable(model_df, M.BALANCE_ITEMS)

if not M.has_income_data(model_df):
    st.warning(
        f"No income-statement or cash-flow data is available for {ticker} from "
        "Gödel or Massive — only a balance sheet. Revenue, margins, FCF and the "
        "reverse DCF can't be computed. Showing balance-sheet trends instead.")

# offer income+cash-flow metrics if present, else fall back to balance sheet
options = income_items or balance_items
if not options:
    st.error("No chartable financial data for this ticker.")
else:
    chosen = st.multiselect("Metrics", options=options, default=options,
                            format_func=lambda i: F._PRETTY.get(i, i))
    ncol = 3
    cols = st.columns(ncol)
    for n, item in enumerate(chosen):
        with cols[n % ncol]:
            st.pyplot(_metric_fig(model_df, item, forecast), clear_figure=True)
    # if we showed income metrics but a balance sheet also exists, offer it too
    if income_items and balance_items:
        with st.expander("Balance-sheet trends"):
            bcols = st.columns(ncol)
            for n, item in enumerate(balance_items):
                with bcols[n % ncol]:
                    st.pyplot(_metric_fig(model_df, item, forecast),
                              clear_figure=True)

# ── raw statements + downloads ────────────────────────────────────────────────

st.subheader("Statements")
tabs = st.tabs(["Income", "Balance sheet", "Cash flow", "Merged model"])
mapping = [("income_statement", tabs[0]), ("balance_sheet", tabs[1]),
           ("cash_flow", tabs[2])]
for key, tab in mapping:
    with tab:
        df = frames.get(key)
        if df is None or df.empty:
            st.write("No data.")
            continue
        disp = df.sort_index(ascending=False).round(2)
        st.dataframe(disp, use_container_width=True)
        st.download_button(f"Download {key}.csv", df.to_csv(),
                           file_name=f"{ticker}_{key}_{period}.csv",
                           mime="text/csv", key=f"dl_{key}")
with tabs[3]:
    st.dataframe(model_df.sort_index(ascending=False).round(3),
                 use_container_width=True)
    st.download_button("Download merged_model.csv", model_df.to_csv(),
                       file_name=f"{ticker}_model_{period}.csv",
                       mime="text/csv", key="dl_merged")

# ── under the hood: how it works + the actual API calls ───────────────────────

with st.expander("🔧 Under the hood — what's executing"):
    st.markdown(
        "**How this page is built (no DES, no browser scraping):**\n"
        "1. `GET /api/v1/search?query=TICKER` → Gödel **series id** + last close\n"
        "2. `GET /api/v1/consolidated_financials/{income_statement|balance_sheet|"
        "cash_flow}/{series_id}/{QTR|ANN}` → the three statements\n"
        "3. Massive `GET /v3/reference/tickers/{TICKER}` → name, summary, sector, "
        "shares outstanding\n"
        "4. Massive `GET /v2/aggs/.../range/1/day/...` (ticker **and** SPY) → "
        "price history → beta, YTD, vol, the price chart\n"
        "5. **Enterprise value** = price×shares + net debt (debt − cash from the "
        "balance sheet). **FCF** = operating cash flow − capex. The **reverse "
        "DCF** then solves the FCF growth rate that makes the discounted stream "
        "equal EV.\n\n"
        f"Every call is logged with [loguru](https://loguru.readthedocs.io) to "
        f"`{api_client.log_path()}` and kept in a ring buffer shown below.")
    if calls:
        cdf = pd.DataFrame(calls)[["ts", "host", "method", "path", "status", "ms"]]
        st.dataframe(cdf, use_container_width=True, hide_index=True)
        st.caption(f"{len(calls)} API calls for this load "
                   f"({sum(c['ms'] for c in calls)/1000:.1f}s total). "
                   "Cached for 15 min — a cached load makes zero calls.")
    else:
        st.caption("No calls captured (served from cache).")
