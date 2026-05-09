"""News Agent dashboard — read-only view of seen.db.

Run with:
    .venv/bin/streamlit run dashboard.py
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd
import streamlit as st

DB_PATH = Path("seen.db")


# ---------------------------------------------------------------- Page setup
st.set_page_config(
    page_title="News Agent — Insurance/Reinsurance",
    page_icon="📰",
    layout="wide",
)


# ---------------------------------------------------------------- Data load
@st.cache_data(ttl=30)
def load_data() -> pd.DataFrame:
    if not DB_PATH.exists():
        return pd.DataFrame()
    with sqlite3.connect(DB_PATH) as conn:
        df = pd.read_sql_query(
            """
            SELECT url_hash, url, title, source, priority,
                   collected_at, published_at, summary,
                   emailed_at, dropped_reason
            FROM seen
            ORDER BY collected_at DESC
            """,
            conn,
        )
    if not df.empty:
        df["collected_at"] = pd.to_datetime(df["collected_at"], errors="coerce", utc=True)
        df["published_at"] = pd.to_datetime(df["published_at"], errors="coerce", utc=True)
        df["emailed_at"] = pd.to_datetime(df["emailed_at"], errors="coerce", utc=True)
    return df


# ---------------------------------------------------------------- Header
col_title, col_refresh = st.columns([4, 1])
with col_title:
    st.title("📰 News Agent")
    st.caption("Insurance / reinsurance news pipeline — read-only view of `seen.db`")
with col_refresh:
    st.write("")
    if st.button("🔄 Refresh", use_container_width=True):
        load_data.clear()
        st.rerun()

df = load_data()

if df.empty:
    st.warning(
        "`seen.db` is empty or missing. Run `python -m news_agent --once` first to populate it."
    )
    st.stop()


# ---------------------------------------------------------------- Sidebar filters
st.sidebar.header("Filters")

priority_filter = st.sidebar.multiselect(
    "Priority",
    options=["P1", "P2", "P3", "DROPPED"],
    default=["P1", "P2", "P3"],
)

email_status_filter = st.sidebar.multiselect(
    "Email status",
    options=["Sent", "Pending", "Dropped (never emailed)"],
    default=["Sent", "Pending"],
)

sources = sorted(df["source"].dropna().unique().tolist())
source_filter = st.sidebar.multiselect("Source", options=sources)

search = st.sidebar.text_input("Search title (case-insensitive)")

# Date range
min_date = df["collected_at"].min().date()
max_date = df["collected_at"].max().date()
date_range = st.sidebar.date_input(
    "Collected between",
    value=(min_date, max_date),
    min_value=min_date,
    max_value=max_date,
)


# ---------------------------------------------------------------- Apply filters
mask = df["priority"].isin(priority_filter)

if source_filter:
    mask &= df["source"].isin(source_filter)

if search:
    mask &= df["title"].str.contains(search, case=False, na=False)

if isinstance(date_range, tuple) and len(date_range) == 2:
    start, end = date_range
    mask &= (df["collected_at"].dt.date >= start) & (df["collected_at"].dt.date <= end)

# Email-status filter
def email_status_label(row: pd.Series) -> str:
    if row["priority"] == "DROPPED":
        return "Dropped (never emailed)"
    if pd.notna(row["emailed_at"]):
        return "Sent"
    return "Pending"

df["_status"] = df.apply(email_status_label, axis=1)
mask &= df["_status"].isin(email_status_filter)

filtered = df[mask].copy()


# ---------------------------------------------------------------- Top metrics
m1, m2, m3, m4, m5, m6 = st.columns(6)
m1.metric("Total shown", len(filtered))
m2.metric("P1", int((filtered.priority == "P1").sum()))
m3.metric("P2", int((filtered.priority == "P2").sum()))
m4.metric("P3", int((filtered.priority == "P3").sum()))
m5.metric("Emailed", int(filtered.emailed_at.notna().sum()))
m6.metric("Dropped", int((filtered.priority == "DROPPED").sum()))


# ---------------------------------------------------------------- Status icon
def status_icon(row: pd.Series) -> str:
    if row["priority"] == "DROPPED":
        return "🚫"
    if pd.notna(row["emailed_at"]):
        return "✅"
    if row["priority"] == "P1":
        return "⏳"
    return "🕐"  # awaiting digest


filtered["status"] = filtered.apply(status_icon, axis=1)


# ---------------------------------------------------------------- Table
display_cols = ["status", "priority", "source", "title", "url", "published_at", "collected_at", "emailed_at", "dropped_reason"]
display = filtered[display_cols].rename(
    columns={
        "status": "📧",
        "priority": "Pri",
        "source": "Source",
        "title": "Title",
        "url": "🔗",
        "published_at": "Published",
        "collected_at": "Collected",
        "emailed_at": "Emailed",
        "dropped_reason": "Drop reason",
    }
)

st.dataframe(
    display,
    use_container_width=True,
    hide_index=True,
    height=420,
    column_config={
        "🔗": st.column_config.LinkColumn(label="🔗", display_text="open"),
        "Published": st.column_config.DatetimeColumn(format="YYYY-MM-DD HH:mm"),
        "Collected": st.column_config.DatetimeColumn(format="YYYY-MM-DD HH:mm"),
        "Emailed": st.column_config.DatetimeColumn(format="YYYY-MM-DD HH:mm"),
        "Title": st.column_config.TextColumn(width="large"),
    },
)


# ---------------------------------------------------------------- Drilldown
st.divider()
st.subheader("Story detail")

if len(filtered) == 0:
    st.info("No stories match the current filters.")
else:
    selected_idx = st.selectbox(
        "Pick a story to inspect",
        options=range(len(filtered)),
        format_func=lambda i: f"[{filtered.iloc[i]['priority']}] {filtered.iloc[i]['title'][:90]}",
    )
    row = filtered.iloc[selected_idx]

    st.markdown(f"### {row['title']}")
    st.caption(
        f"**{row['source']}** · Priority: **{row['priority']}** · Collected: {row['collected_at']:%Y-%m-%d %H:%M UTC}"
    )

    if row["priority"] == "DROPPED":
        st.warning(f"🚫 Dropped — reason: `{row['dropped_reason']}`")
    elif pd.notna(row["emailed_at"]):
        st.success(f"✅ Emailed at {row['emailed_at']:%Y-%m-%d %H:%M UTC}")
    elif row["priority"] == "P1":
        st.error("⏳ P1 not yet emailed (summarizer or mailer may be disabled — check `.env`)")
    else:
        st.info("🕐 Awaiting daily digest (07:00 JST)")

    st.markdown(f"[Open source article ↗]({row['url']})")

    if pd.notna(row["summary"]) and row["summary"]:
        st.markdown("---")
        st.markdown("**Summary**")
        st.text(row["summary"])
    else:
        st.caption("No summary yet — P2/P3 stories are summarized at digest time.")
