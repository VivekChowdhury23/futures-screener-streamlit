import streamlit as st
import pandas as pd
from bs4 import BeautifulSoup
import requests
from datetime import datetime
import time
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import math

st.markdown("""
<style>
/* Base text */
html, body, [class*="css"] {
    font-size: 13px !important;
}

/* Headings */
h1 { font-size: 2 rem !important; }
h2 { font-size: 1.8rem !important; }
h3 { font-size: 1.5rem !important; }

/* Subheaders (st.subheader) */
[data-testid="stMarkdownContainer"] h2,
[data-testid="stMarkdownContainer"] h3 {
    font-size: 1.3rem !important;
    margin-top: 1 rem !important;
    margin-bottom: 0.8 rem !important;
}

/* Metric cards — labels and values */
[data-testid="stMetricLabel"]  { font-size: 0.75rem !important; }
[data-testid="stMetricValue"]  { font-size: 1.1rem  !important; }
[data-testid="stMetricDelta"]  { font-size: 0.7rem  !important; }

/* DataFrames */
[data-testid="stDataFrame"] { font-size: 0.8rem !important; }

/* Captions */
[data-testid="stCaptionContainer"] { font-size: 0.75rem !important; }

/* Sidebar */
[data-testid="stSidebar"] { font-size: 0.85rem !important; }

/* Expander headers */
[data-testid="stExpander"] summary { font-size: 0.9rem !important; }
</style>
""", unsafe_allow_html=True)

#####################################################################################
# Page Config
#####################################################################################

st.set_page_config(
    page_title="F&O Easy Screener",
    page_icon="📊",
    layout="wide"
)

#####################################################################################
# Constants
#####################################################################################

BASE_URL = "https://smartoptions.trendlyne.com"
PAGE_URL = "https://smartoptions.trendlyne.com/easy-screener/futures/"
API_URL  = "https://smartoptions.trendlyne.com/phoenix/api/fno/easy-screener/"

HEADERS_BASE = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Origin": BASE_URL,
    "Referer": PAGE_URL,
    "Broker": "phoenix",
}

#####################################################################################
# Session + API helpers (cached)
#####################################################################################

@st.cache_resource
def get_session():
    """Create a requests session with CSRF token. Cached as a resource so cookies persist."""
    session = requests.Session()
    headers = HEADERS_BASE.copy()

    page = session.get(PAGE_URL, headers=headers, timeout=20)
    csrf_token = session.cookies.get("csrftoken")

    if not csrf_token:
        soup = BeautifulSoup(page.text, "html.parser")
        tag = soup.find("input", {"name": "csrfmiddlewaretoken"})
        if tag:
            csrf_token = tag["value"]

    if csrf_token:
        headers["X-CSRFToken"] = csrf_token

    return session, headers


def parse_response(data):
    table_headers = data["body"]["tableHeaders"]
    table_data    = data["body"]["tableData"]

    cols = [
        h.get("title") or h.get("name") or f"col_{i}"
        for i, h in enumerate(table_headers)
    ]

    rows = []
    for row in table_data:
        cleaned_row = []
        for cell in row:
            if isinstance(cell, dict):
                val = cell.get("name") or cell.get("symbol") or cell.get("id")
                if val is None:
                    val = next(iter(cell.values()), None)
                cleaned_row.append(val)
            else:
                cleaned_row.append(cell)
        rows.append(cleaned_row)

    return pd.DataFrame(rows, columns=cols[:len(rows[0])] if rows else cols)


@st.cache_data(ttl=60, show_spinner=False)
def fetch_expiry_dates():
    session, headers = get_session()
    resp = session.get(
        f"{BASE_URL}/phoenix/api/fno/get-expiry-dates/",
        headers=headers,
        params={"mtype": "futures"},
        timeout=30
    )
    return resp.json()["body"]["expiryDates"]


@st.cache_data(ttl=60, show_spinner=False)
def fetch_screener(mtype, expiry):
    session, headers = get_session()
    payload = {
        "columns": [],
        "mtype": mtype,
        "query": f"expdate = '{expiry}'",
    }
    if mtype == "futures":
        payload["order"]  = "desc"
        payload["sortBy"] = "volume"

    resp = session.post(API_URL, headers=headers, json=payload, timeout=30)
    return resp.json(), resp.status_code


@st.cache_data(ttl=60, show_spinner=False)
def fetch_buildup_data(symbol, expiry, mtype="futures"):
    session, headers = get_session()
    expiry_dt         = datetime.strptime(expiry, "%Y-%m-%d")
    expiry_url_format = expiry_dt.strftime("%d-%b-%Y").lower()

    url = f"{BASE_URL}/phoenix/api/fno/buildup-5/{expiry_url_format}-near/{symbol}/"
    try:
        resp = session.get(url, headers=headers, params={"fno_mtype": mtype}, timeout=30)
    except requests.RequestException:
        return None

    if resp.status_code // 100 != 2:
        return None

    data = resp.json()
    if data.get("head", {}).get("status") != "0":
        return None

    return data["body"].get("data_v2", [])


def buildup_to_df(records):
    if not records:
        return pd.DataFrame()

    df = pd.DataFrame(records)
    df["start_time"] = df["interval"].str.split(" TO ").str[0]
    df = df.sort_values("start_time").reset_index(drop=True)

    df["oi_change"]     = pd.to_numeric(df["oi_change"], errors="coerce").fillna(0)
    df["cum_oi_change"] = df["oi_change"].cumsum()

    if "volume_gross" in df.columns:
        df["volume_gross"]  = pd.to_numeric(df["volume_gross"], errors="coerce").fillna(0)
        df["cum_contracts"] = df["volume_gross"].cumsum()

    return df

#####################################################################################
# Scoring Engine
#####################################################################################

def score_options(df):
    """Returns (scored_df, top_row_or_None)."""
    if df is None or df.empty:
        return df, None

    scored    = df.copy()
    iv_median = pd.to_numeric(scored.get("IV", pd.Series(dtype=float)), errors="coerce").median()

    def score_row(row):
        score = 0
        notes = []

        # Delta
        delta = abs(pd.to_numeric(row.get("Delta"), errors="coerce") or 0)
        if 0.25 <= delta <= 0.45:
            score += 3
            notes.append("✅ Delta sweet spot")
        elif 0.15 <= delta < 0.25 or 0.45 < delta <= 0.55:
            score += 1
            notes.append("⚠️ Delta acceptable")
        else:
            notes.append("❌ Delta out of range")

        # OI Change%
        oi_chg = pd.to_numeric(row.get("OI Change%"), errors="coerce") or 0
        if oi_chg > 5000:
            score += 3
            notes.append("✅ OI surge very strong")
        elif oi_chg > 1000:
            score += 2
            notes.append("✅ OI surge strong")
        elif oi_chg > 500:
            score += 1
            notes.append("⚠️ OI surge moderate")
        else:
            notes.append("❌ OI surge weak")

        # Volume
        volume = pd.to_numeric(row.get("Volume"), errors="coerce") or 0
        if volume > 500000:
            score += 2
            notes.append("✅ High volume")
        elif volume > 100000:
            score += 1
            notes.append("⚠️ Moderate volume")
        else:
            notes.append("❌ Low volume")

        # Volume Change%
        vol_chg = pd.to_numeric(row.get("Volume Change%"), errors="coerce") or 0
        if vol_chg > 1000:
            score += 1
            notes.append("✅ Volume spike confirms OI")

        # Premium as % of Spot
        premium     = pd.to_numeric(row.get("Current Price"), errors="coerce") or 0
        spot        = pd.to_numeric(row.get("Spot"), errors="coerce") or 0
        premium_pct = (premium / spot) * 100 if spot > 0 else 0

        if 0.5 <= premium_pct <= 3:
            score += 2
            notes.append(f"✅ Premium {premium_pct:.1f}% of spot")
        elif premium_pct < 0.5:
            score -= 2
            notes.append(f"❌ Lottery ({premium_pct:.2f}% of spot)")
        elif premium_pct > 5:
            score -= 2
            notes.append(f"❌ Expensive ({premium_pct:.1f}% of spot)")
        else:
            score += 1
            notes.append(f"⚠️ Premium {premium_pct:.1f}% of spot")

        # Theta decay
        theta = pd.to_numeric(row.get("Theta"), errors="coerce") or 0
        if premium > 0:
            theta_ratio = abs(theta) / premium
            if theta_ratio < 0.10:
                score += 2
                notes.append("✅ Low theta decay")
            elif theta_ratio > 0.30:
                score -= 3
                notes.append(f"❌ Severe theta ({theta_ratio*100:.0f}%/day)")
            else:
                notes.append(f"⚠️ Theta {theta_ratio*100:.0f}%/day")

        # IV vs peer median
        iv = pd.to_numeric(row.get("IV"), errors="coerce") or 0
        if iv_median and iv_median > 0:
            if iv > iv_median * 1.3:
                score -= 2
                notes.append("❌ IV elevated vs peers")
            elif iv < iv_median * 1.1:
                score += 1
                notes.append("✅ IV reasonable vs peers")

        # OI absolute
        oi = pd.to_numeric(row.get("OI"), errors="coerce") or 0
        if oi > 500000:
            score += 1
            notes.append("✅ Deep open interest")

        return score, " | ".join(notes)

    scored[["Score", "Notes"]] = scored.apply(
        lambda row: pd.Series(score_row(row)), axis=1
    )
    scored = scored.sort_values(by="Score", ascending=False).reset_index(drop=True)

    return scored, scored.iloc[0]

#####################################################################################
# Plot Helper
#####################################################################################

def make_buildup_chart(buildup_dfs, build_up_label, expiry, cols=2):
    BULLISH = {"Long Build Up", "Short Covering"}
    BEARISH = {"Short Build Up", "Long Unwinding"}

    def buildup_color(b):
        if b in BULLISH: return "#22c55e"
        if b in BEARISH: return "#ef4444"
        return "#9ca3af"

    def color_label(c):
        return ("Bullish OI" if c == "#22c55e"
                else "Bearish OI" if c == "#ef4444"
                else "Neutral OI")

    n    = len(buildup_dfs)
    rows = math.ceil(n / cols)

    # Build specs grid — every cell needs secondary_y, padding with None for empty cells
    specs = []
    for r in range(rows):
        row_specs = []
        for c in range(cols):
            idx = r * cols + c
            row_specs.append({"secondary_y": True} if idx < n else None)
        specs.append(row_specs)

    fig = make_subplots(
        rows=rows, cols=cols,
        specs=specs,
        subplot_titles=list(buildup_dfs.keys()),
        horizontal_spacing=0.10,
        vertical_spacing=0.18,
    )

    # Track which legend groups have already shown a label
    legend_seen = {"Bullish OI": False, "Bearish OI": False,
                   "Neutral OI": False, "Cum Contracts": False}

    for idx, (symbol, df) in enumerate(buildup_dfs.items()):
        r = idx // cols + 1
        c = idx % cols + 1

        if df.empty:
            continue

        x        = df["start_time"].tolist()
        y_oi     = df["cum_oi_change"].tolist()
        buildups = df["buildup"].tolist()

        # --- Coloured line segments (one trace per segment, grouped in legend) ---
        for i in range(len(df) - 1):
            color = buildup_color(buildups[i + 1])
            label = color_label(color)
            show_leg = not legend_seen[label]
            legend_seen[label] = True

            fig.add_trace(
                go.Scatter(
                    x=[x[i], x[i + 1]],
                    y=[y_oi[i], y_oi[i + 1]],
                    mode="lines+markers",
                    line=dict(color=color, width=2.5),
                    marker=dict(
                        color=color, size=6,
                        line=dict(color="black", width=0.5),
                    ),
                    name=label,
                    legendgroup=label,
                    showlegend=show_leg,
                    hovertemplate=(
                        f"<b>{symbol}</b><br>"
                        "Time: %{x}<br>"
                        "Cum OI Δ%: %{y:.2f}<br>"
                        f"Buildup: {buildups[i + 1]}"
                        "<extra></extra>"
                    ),
                ),
                row=r, col=c, secondary_y=False,
            )

        # --- Secondary axis: cumulative traded contracts ---
        if "cum_contracts" in df.columns:
            show_leg = not legend_seen["Cum Contracts"]
            legend_seen["Cum Contracts"] = True

            fig.add_trace(
                go.Scatter(
                    x=x,
                    y=(df["cum_contracts"] / 1000).tolist(),
                    mode="lines+markers",
                    line=dict(color="#3b82f6", width=1.8, dash="dash"),
                    marker=dict(symbol="square", size=5, color="#3b82f6"),
                    name="Cum Contracts ('000)",
                    legendgroup="Cum Contracts",
                    showlegend=show_leg,
                    hovertemplate=(
                        f"<b>{symbol}</b><br>"
                        "Time: %{x}<br>"
                        "Cum Contracts: %{y:,.1f}k"
                        "<extra></extra>"
                    ),
                ),
                row=r, col=c, secondary_y=True,
            )

        fig.update_xaxes(tickangle=-45, row=r, col=c, showgrid=True, gridcolor="rgba(0,0,0,0.06)")
        fig.update_yaxes(
            title_text="Cum OI Δ%", row=r, col=c, secondary_y=False,
            color="#374151", showgrid=True, gridcolor="rgba(0,0,0,0.06)",
            zeroline=True, zerolinecolor="black", zerolinewidth=0.7,
        )
        fig.update_yaxes(
            title_text="Cum Contracts ('000)", row=r, col=c, secondary_y=True,
            color="#3b82f6", showgrid=False,
        )

    fig.update_layout(
        title=dict(
            text=f"5-Min Buildup — {build_up_label} Shortlist  |  Expiry: {expiry}",
            x=0.5, xanchor="center",
            font=dict(size=15),
        ),
        height=380 * rows + 120,
        hovermode="closest",
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.02,
            xanchor="center",
            x=0.5,
        ),
        margin=dict(t=140, b=40, l=40, r=40),
        plot_bgcolor="white",
    )

    return fig

#####################################################################################
# UI — Sidebar
#####################################################################################

st.sidebar.title("⚙️ Settings")

try:
    with st.spinner("Loading expiry dates..."):
        expiry_dates = fetch_expiry_dates()
except Exception as e:
    st.sidebar.error(f"Failed to load expiries: {e}")
    st.stop()

selected_expiry = st.sidebar.selectbox(
    "Expiry Date",
    expiry_dates,
    index=0,
    help="Choose F&O expiry to analyze",
)

st.sidebar.divider()

SENTIMENT_THRESHOLD_STRONG = st.sidebar.slider(
    "Sentiment Threshold (%)",
    0.0, 20.0, 8.0, 0.5,
    help="Minimum gap between Long% and Short% to consider signal strong",
)

MIN_ACCEPTABLE_SCORE = st.sidebar.slider(
    "Min Acceptable Score",
    0, 15, 6,
    help="Top pick must clear this — otherwise stay out",
)

TOP_N = st.sidebar.slider(
    "Top stocks to shortlist",
    3, 10, 5,
)

st.sidebar.divider()

if st.sidebar.button("🔄 Refresh All Data", use_container_width=True):
    st.cache_data.clear()
    st.rerun()

st.sidebar.caption(f"Data cached for 60s. Last refresh on rerun.")

#####################################################################################
# UI — Main
#####################################################################################

st.title("📊 F&O Easy Screener")
st.caption(f"Trendlyne Smart Options analysis — Expiry **{selected_expiry}**")

# --- Fetch core data ---
with st.spinner("Fetching futures data..."):
    futures_data, fut_status = fetch_screener(mtype="futures", expiry=selected_expiry)
if fut_status // 100 != 2:
    st.error(f"Futures fetch failed (HTTP {fut_status})")
    st.stop()
futures_df_full = parse_response(futures_data)

with st.spinner("Fetching options data..."):
    options_data, opt_status = fetch_screener(mtype="options", expiry=selected_expiry)
if opt_status // 100 != 2:
    st.error(f"Options fetch failed (HTTP {opt_status})")
    st.stop()
options_df = parse_response(options_data)

# --- Compute sentiment ---
total_symbols = len(futures_df_full)
counts = futures_df_full["Build Up"].value_counts().to_dict()
long_build_up_count  = counts.get("Long Build Up", 0)
short_build_up_count = counts.get("Short Build Up", 0)
short_covering_count = counts.get("Short Covering", 0)
long_covering_count  = counts.get("Long Covering", 0)

long_percent  = ((long_build_up_count + short_covering_count) / total_symbols) * 100 if total_symbols else 0
short_percent = ((short_build_up_count + long_covering_count) / total_symbols) * 100 if total_symbols else 0
sentiment_gap = abs(long_percent - short_percent)

if long_percent > short_percent:
    build_up, sort_order = "Long Build Up", False
elif short_percent > long_percent:
    build_up, sort_order = "Short Build Up", True
else:
    build_up, sort_order = "", True

# --- Compute shortlist ---
shortlisted_stocks  = []
options_filtered_df = pd.DataFrame()
futures_top         = pd.DataFrame()

if build_up == "Long Build Up":
    futures_top = (
        futures_df_full[futures_df_full["Build Up"] == "Long Build Up"]
        .sort_values(by="Day Change%", ascending=False)
        .head(TOP_N)
    )
    shortlisted_stocks = futures_top.SYMBOL.to_list()
    options_filtered_df = (
        options_df[
            (options_df["SYMBOL"].isin(shortlisted_stocks)) &
            (options_df["Type"] == "Call") &
            (options_df["Moneyness"] == "OTM") &
            (options_df["Build Up"] == "Long Build Up")
        ]
        .sort_values(by="OI Change%", ascending=False)
        .reset_index(drop=True)
    )
elif build_up == "Short Build Up":
    futures_top = (
        futures_df_full[futures_df_full["Build Up"] == "Short Build Up"]
        .sort_values(by="Day Change%", ascending=True)
        .head(TOP_N)
    )
    shortlisted_stocks = futures_top.SYMBOL.to_list()
    options_filtered_df = (
        options_df[
            (options_df["SYMBOL"].isin(shortlisted_stocks)) &
            (options_df["Type"] == "Put") &
            (options_df["Moneyness"] == "OTM") &
            (options_df["Build Up"] == "Short Build Up")
        ]
        .sort_values(by="OI Change%", ascending=False)
        .reset_index(drop=True)
    )

# --- Fetch 5-min buildup data (needs to happen before splitting columns) ---
buildup_dfs = {}
if shortlisted_stocks:
    progress = st.progress(0.0, text="Fetching buildup data...")
    for idx, symbol in enumerate(shortlisted_stocks, start=1):
        progress.progress(idx / len(shortlisted_stocks), text=f"Fetching {symbol}...")
        records = fetch_buildup_data(symbol, selected_expiry)
        if records:
            buildup_dfs[symbol] = buildup_to_df(records)
        time.sleep(0.3)
    progress.empty()

#####################################################################################
# Two-Column Page Layout
#####################################################################################

left_col, right_col = st.columns([1, 1.3], gap="large")

# ============================ LEFT COLUMN ============================
with left_col:

    # --- 1. Market Sentiment ---
    st.subheader("🧭 Market Sentiment")

    m1, m2 = st.columns(2)
    m1.metric("Total Symbols", total_symbols)

    direction_label = (
        "📈 BULLISH" if build_up == "Long Build Up"
        else "📉 BEARISH" if build_up == "Short Build Up"
        else "🔁 NEUTRAL"
    )
    m2.metric("Direction", direction_label)

    m3, m4, m5 = st.columns(3)
    m3.metric("Long %",  f"{long_percent:.1f}%",  delta=f"{long_percent - short_percent:+.1f}%")
    m4.metric("Short %", f"{short_percent:.1f}%")
    m5.metric("Gap",     f"{sentiment_gap:.1f}%")

    with st.expander("Build-up category breakdown"):
        breakdown_df = pd.DataFrame({
            "Category": ["Long Build Up", "Short Covering", "Short Build Up", "Long Covering"],
            "Count":    [long_build_up_count, short_covering_count, short_build_up_count, long_covering_count],
            "Side":     ["Bullish", "Bullish", "Bearish", "Bearish"],
        })
        breakdown_df["%"] = (breakdown_df["Count"] / total_symbols * 100).round(1)
        st.dataframe(breakdown_df, hide_index=True, use_container_width=True)

    st.divider()

    # --- 2. Top Shortlisted Stocks ---
    st.subheader(f"🎯 Top {TOP_N} Shortlisted Stocks")
    if shortlisted_stocks:
        st.write("  •  ".join(f"**{s}**" for s in shortlisted_stocks))
        with st.expander("View full futures rows for shortlist"):
            st.dataframe(futures_top.reset_index(drop=True), use_container_width=True)
    else:
        st.info("No clear directional shortlist — sentiment is tied or neutral.")

    st.divider()

    # --- 3. Trade Recommendation Engine ---
    st.subheader("🏆 Trade Recommendation Engine")

    if options_filtered_df.empty:
        st.warning("No options passed the initial filter. Nothing to score.")
    elif sentiment_gap < SENTIMENT_THRESHOLD_STRONG:
        st.warning(
            f"⚠️ **SIGNAL TOO WEAK** — Sentiment gap is only {sentiment_gap:.1f}% "
            f"(need ≥ {SENTIMENT_THRESHOLD_STRONG:.1f}%).  Recommendation: **STAY OUT**."
        )
    else:
        scored_df, top = score_options(options_filtered_df)

        direction = "BULLISH 📈" if build_up == "Long Build Up" else "BEARISH 📉"
        st.success(
            f"Direction: **{direction}**  |  Gap: **{sentiment_gap:.1f}%**  |  Signal: **STRONG ✅**"
        )

        if top is None or top["Score"] < MIN_ACCEPTABLE_SCORE:
            st.warning(
                f"⚠️ Best candidate scored only **{top['Score']}** "
                f"(min acceptable {MIN_ACCEPTABLE_SCORE}). **STAY OUT**."
            )
        else:
            premium = pd.to_numeric(top.get("Current Price"), errors="coerce") or 0

            c1, c2 = st.columns(2)
            c1.metric("Symbol",  top["SYMBOL"])
            c2.metric("Strike",  top["Strike"])

            c3, c4 = st.columns(2)
            c3.metric("Type",    top["Type"])
            c4.metric("Score",   top["Score"])

            c5, c6 = st.columns(2)
            c5.metric("Premium",  f"₹{premium:.2f}")
            c6.metric("Delta",    f"{top.get('Delta', 'N/A')}")

            st.metric("Max Loss", f"₹{premium:.2f}/share")

            st.info(f"**Reasons:** {top['Notes']}")
            st.caption("⚠️ Position sizing: risk no more than 1-2% of capital on this trade.")

        with st.expander("View full scored options table"):
            display_cols = [c for c in [
                "SYMBOL", "Strike", "Type", "Current Price", "Spot",
                "Delta", "Theta", "IV", "OI Change%", "Volume", "OI",
                "Score", "Notes",
            ] if c in scored_df.columns]
            st.dataframe(scored_df[display_cols], use_container_width=True, hide_index=True)

# ============================ RIGHT COLUMN ============================
with right_col:
    st.subheader("📈 Cumulative OI % Change + Traded Contracts")

    if buildup_dfs:
        with st.spinner("Rendering chart..."):
            fig = make_buildup_chart(buildup_dfs, build_up, selected_expiry, cols=2)
            st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("No buildup data to display.")

#####################################################################################
# Footer (full width)
#####################################################################################

st.divider()
st.caption(
    "Disclaimer: This is a screener for idea generation, not financial advice. "
    "All numbers are descriptive analytics — paper-trade signals before risking capital."
)