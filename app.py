"""
RetailPulse - Retail Intelligence & Sales Analytics (Streamlit + Databricks).

Run:  streamlit run app.py

Nothing here talks to Databricks at import time. A connection (and the one-time browser
sign-in) happens only when the user clicks "Connect to Databricks".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

st.set_page_config(
    page_title="RetailPulse | Retail Intelligence",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

import analytics as an  # noqa: E402
import db  # noqa: E402  (loads .env)
import theme as th  # noqa: E402

th.inject_css()

SECTIONS = [
    "Executive overview",
    "Sales trends",
    "Product analytics",
    "Country analytics",
    "Customer analytics",
    "Data quality & pipeline",
    "Detailed tables & exports",
]

TREND_METRICS = {
    "Revenue": ("total_revenue", "money"),
    "Invoices": ("invoice_count", "number"),
    "Average invoice value": ("avg_invoice_value", "money"),
    "Unique customers": ("unique_customers", "number"),
    "Transaction lines": ("transaction_lines", "number"),
}


# =========================================================================
# Small helpers
# =========================================================================

def csv_bytes(frame: pd.DataFrame) -> bytes:
    return frame.to_csv(index=False).encode("utf-8-sig")  # BOM so Excel reads UTF-8 correctly


def show_error(exc: Exception, retry_key: str) -> None:
    headline, advice, detail = db.describe_error(exc)
    st.markdown(th.callout("bad", headline), unsafe_allow_html=True)
    st.write(advice)
    with st.expander("Technical details"):
        st.code(detail)
    left, right = st.columns(2)
    with left:
        if st.button("Retry", key=f"retry_{retry_key}", width="stretch"):
            st.cache_data.clear()
            st.rerun()
    with right:
        if st.button("Reconnect", key=f"reconnect_{retry_key}", type="primary", width="stretch"):
            client.disconnect()
            st.cache_data.clear()
            st.rerun()


def search_frame(frame: pd.DataFrame, query: str) -> pd.DataFrame:
    query = (query or "").strip()
    if not query or frame.empty:
        return frame
    mask = frame.astype(str).apply(lambda col: col.str.contains(query, case=False, regex=False)).any(axis=1)
    return frame[mask]


def styled(frame: pd.DataFrame, money_cols=(), int_cols=(), dec_cols=(), pct_cols=()):
    fmt = {c: f"{th.CURRENCY}{{:,.2f}}" for c in money_cols}
    fmt.update({c: "{:,.0f}" for c in int_cols})
    fmt.update({c: "{:,.2f}" for c in dec_cols})
    fmt.update({c: "{:,.1f}%" for c in pct_cols})
    return frame.style.format(fmt, na_rep="–")


def clip(text, n=40) -> str:
    text = str(text)
    return text if len(text) <= n else text[: n - 1] + "…"


def file_stamp(ctx: "Ctx") -> str:
    return f"{ctx.months[0]}_to_{ctx.months[-1]}"


def plot(fig, key: str) -> None:
    st.plotly_chart(fig, key=key, config=th.PLOT_CONFIG, theme=None)


# =========================================================================
# Connection gate (no OAuth unless the user clicks)
# =========================================================================

hero_slot = st.empty()
client = db.get_client()

try:
    settings = db.load_settings()
except Exception as exc:  # invalid catalog/schema in .env
    hero_slot.markdown(th.hero(False, None, None), unsafe_allow_html=True)
    st.error(f"Configuration problem: {exc}")
    st.stop()

if not client.is_connected:
    hero_slot.markdown(th.hero(False, None, None), unsafe_allow_html=True)
    with st.sidebar:
        st.markdown(th.sidebar_brand(), unsafe_allow_html=True)
        st.markdown('<div class="rp-side-note">Connect to Databricks to load the dashboard.</div>',
                    unsafe_allow_html=True)

    _, centre, _ = st.columns([1, 2, 1])
    with centre:
        with th.card("connect"):
            st.markdown(th.card_title("Connect to your Databricks workspace",
                                      "Read-only access to the Gold and Silver tables of RetailPulse"),
                        unsafe_allow_html=True)
            if settings.missing:
                st.markdown(th.callout("bad", "Missing in .env: " + ", ".join(settings.missing)), unsafe_allow_html=True)
                st.write("Copy `.env.example` to `.env` (same folder as `app.py`) and fill in the two values "
                         "from SQL Warehouses → your warehouse → Connection details.")
            else:
                st.write("Click **Connect** once. If you are not already signed in, one browser tab opens for the "
                         "Databricks OAuth sign-in; close it when it says the connector received a response, "
                         "then return here.")
                st.caption(f"Catalog `{settings.catalog}` · schema `{settings.schema}` · credentials are never displayed")
                if st.button("Connect to Databricks", type="primary", key="connect_btn", width="stretch"):
                    try:
                        with st.spinner("Waiting for Databricks…"):
                            client.connect(settings)
                        st.cache_data.clear()
                        st.rerun()
                    except db.ConnectionInProgressError as exc:
                        st.markdown(th.callout("warn", str(exc)), unsafe_allow_html=True)
                    except Exception as exc:  # noqa: BLE001
                        headline, advice, detail = db.describe_error(exc)
                        st.markdown(th.callout("bad", headline), unsafe_allow_html=True)
                        st.write(advice)
                        with st.expander("Technical details"):
                            st.code(detail)
    st.stop()

# =========================================================================
# Reference data (cached 10 min; same cache as every other query -> never inconsistent)
# =========================================================================

try:
    with st.spinner("Loading RetailPulse tables…"):
        ref = db.load_reference(client)
except Exception as exc:  # noqa: BLE001
    hero_slot.markdown(th.hero(client.is_connected, None, None), unsafe_allow_html=True)
    show_error(exc, "ref")
    st.stop()

months_all = ref.months
countries_all = ref.country_names

# ---- filter state (initialised BEFORE widgets so there is no default/state conflict) -------------
if "f_period" in st.session_state and not set(st.session_state["f_period"]) <= set(months_all):
    del st.session_state["f_period"]
st.session_state.setdefault("f_period", (months_all[0], months_all[-1]))
st.session_state["f_countries"] = [c for c in st.session_state.get("f_countries", []) if c in countries_all]
st.session_state.setdefault("f_term", "")
st.session_state.setdefault("f_exclude_admin", True)
st.session_state.setdefault("nav", SECTIONS[0])


def reset_filters() -> None:
    st.session_state["f_period"] = (months_all[0], months_all[-1])
    st.session_state["f_countries"] = []
    st.session_state["f_term"] = ""
    st.session_state["f_exclude_admin"] = True


def refresh_data() -> None:
    st.cache_data.clear()


with st.sidebar:
    st.markdown(th.sidebar_brand(), unsafe_allow_html=True)
    st.markdown(th.side_label("Navigate"), unsafe_allow_html=True)
    st.radio("Section", SECTIONS, key="nav", label_visibility="collapsed")

    st.markdown(th.side_label("Filters"), unsafe_allow_html=True)
    st.select_slider(
        "Reporting period", options=months_all, key="f_period", format_func=an.month_label,
        help="Start and end month. Applies to every KPI, chart and table.",
    )
    st.multiselect(
        "Countries", options=countries_all, key="f_countries", placeholder="All countries",
        help="Leave empty for all countries. Applies to every KPI, chart and table.",
    )
    st.text_input(
        "Product search", key="f_term", placeholder="Description or stock code",
        help="Keeps only sales lines whose product description or stock code contains this text. "
             "It recalculates every KPI, chart and table from the matching lines.",
    )
    st.checkbox(
        "Exclude postage, fees and manual entries from product rankings", key="f_exclude_admin",
        help="Affects product rankings and product tables only. Revenue KPIs always include these lines.",
    )
    st.button("Reset filters", on_click=reset_filters, width="stretch", key="reset_btn")

    st.markdown(th.side_label("Data"), unsafe_allow_html=True)
    st.button("Refresh data", on_click=refresh_data, width="stretch", icon=":material/refresh:", key="refresh_btn",
              help="Clears cached results and re-queries Databricks.")
    if st.button("Disconnect", width="stretch", key="disconnect_btn"):
        client.disconnect()
        st.cache_data.clear()
        st.rerun()
    st.markdown(
        '<div class="rp-side-note">Query results are cached for 10 minutes. “Refresh data” forces a new query. '
        "The Databricks session stays connected until you disconnect or stop the app.</div>",
        unsafe_allow_html=True,
    )

# =========================================================================
# Load data for the current filters
# =========================================================================

lo, hi = st.session_state["f_period"]
selected_months = [m for m in months_all if lo <= m <= hi]
selected_countries = list(st.session_state["f_countries"])
term = st.session_state["f_term"]

try:
    with st.spinner("Updating dashboard…"):
        dash = db.load_dashboard(client, ref, selected_months, selected_countries, term)
except Exception as exc:  # noqa: BLE001
    hero_slot.markdown(th.hero(client.is_connected, None, None), unsafe_allow_html=True)
    show_error(exc, "dash")
    st.stop()

# Compatibility guard: some db.py revisions call this metric unique_customers.
# The UI consistently uses "customers", so normalize the key once here.
if "customers" not in dash.kpis:
    dash.kpis["customers"] = dash.kpis.get("unique_customers", 0)

period_label = (f"{an.month_label(lo)} – {an.month_label(hi)}" if lo != hi else an.month_label(lo))
last_refresh = f"{client.last_success_at:%d %b %Y, %H:%M:%S}" if client.last_success_at else None
hero_slot.markdown(th.hero(True, period_label, last_refresh), unsafe_allow_html=True)


@dataclass
class Ctx:
    dash: db.Dashboard
    ref: db.Reference
    months: list
    countries: list
    term: str
    exclude_admin: bool
    products: pd.DataFrame        # respects the "exclude administrative" toggle
    monthly: pd.DataFrame         # with avg_invoice_value
    period_label: str

    @property
    def kpis(self):
        return self.dash.kpis

    @property
    def partial(self):
        return self.ref.partial_month

    @property
    def restrict(self):
        return self.dash.restrict


all_products = dash.products.copy()
if "is_admin" not in all_products.columns:
    all_products["is_admin"] = False
all_products["Type"] = np.where(all_products["is_admin"], "Administrative", "Merchandise")
products_view = all_products[~all_products["is_admin"]] if st.session_state["f_exclude_admin"] else all_products

ctx = Ctx(
    dash=dash, ref=ref, months=selected_months, countries=selected_countries, term=term,
    exclude_admin=st.session_state["f_exclude_admin"], products=products_view.reset_index(drop=True),
    monthly=an.add_average_invoice_value(dash.monthly), period_label=period_label,
)


def filter_bar(ctx: Ctx, which=("kpis", "monthly", "products", "countries")) -> None:
    n_c = len(ctx.countries)
    country_text = ("All countries" if n_c in (0, len(countries_all)) else
                    ", ".join(ctx.countries) if n_c <= 3 else f"{n_c} selected")
    items = [("Period", ctx.period_label), ("Countries", country_text),
             ("Product search", ctx.term if ctx.term else "none"), ("Currency", th.CURRENCY_CODE)]
    labels = {"kpis": "KPIs", "monthly": "Trends", "products": "Products", "countries": "Countries"}
    sources = {labels[k]: ctx.dash.sources[k] for k in which}
    st.markdown(th.chips(items, sources), unsafe_allow_html=True)
    if ctx.kpis["transaction_lines"] == 0:
        st.markdown(th.callout("warn", "No sales lines match the current filters. Widen the period, clear the "
                                       "country selection, or change the product search."), unsafe_allow_html=True)


# =========================================================================
# Chart builders
# =========================================================================

def trend_figure(monthly: pd.DataFrame, column: str, label: str, kind: str, chart: str, partial: str | None):
    x = monthly["month_key"].map(an.month_label)
    y = monthly[column]
    fmt = f"{th.CURRENCY}%{{y:,.2f}}" if kind == "money" else "%{y:,.0f}"
    hover = f"<b>%{{x}}</b><br>{label}: {fmt}<extra></extra>"
    is_partial = (monthly["month_key"] == partial).to_numpy() if partial else np.zeros(len(monthly), bool)
    fig = go.Figure()
    if chart == "Bar":
        fig.add_bar(x=x, y=y, marker_color=np.where(is_partial, "#A5B4FC", th.BLUE), hovertemplate=hover,
                    marker_pattern_shape=np.where(is_partial, "/", ""))
    else:
        fig.add_scatter(x=x, y=y, mode="lines+markers", line=dict(color=th.BLUE, width=3),
                        marker=dict(size=7, color=th.BLUE), hovertemplate=hover,
                        fill="tozeroy" if chart == "Area" else None,
                        fillcolor="rgba(37,99,235,0.12)")
        if is_partial.any():
            fig.add_scatter(x=x[is_partial], y=y[is_partial], mode="markers", name="Partial month",
                            marker=dict(size=13, symbol="circle-open", color=th.VIOLET, line=dict(width=3)),
                            hovertemplate=f"<b>%{{x}}</b> (partial month)<br>{label}: {fmt}<extra></extra>")
    fig.update_xaxes(type="category", categoryorder="array", categoryarray=list(x), tickangle=-35)
    th.style_fig(fig, height=340, legend=bool(is_partial.any()),
                 currency_axis="y" if kind == "money" else None)
    return fig


def hbar(labels, values, custom, hovertemplate, color, value_fmt, height=340):
    fig = go.Figure(go.Bar(
        x=values, y=labels, orientation="h", marker_color=color, customdata=custom, hovertemplate=hovertemplate,
        text=[value_fmt(v) for v in values], textposition="outside", cliponaxis=False,
        textfont=dict(color=th.INK, size=11),
    ))
    fig.update_yaxes(autorange="reversed", automargin=True)
    fig.update_xaxes(showgrid=True, gridcolor=th.GRID, showticklabels=False, zeroline=False)
    th.style_fig(fig, height=height)
    fig.update_layout(margin=dict(l=6, r=70, t=10, b=6))
    return fig


def card_header(title: str, subtitle: str | None = None) -> None:
    st.markdown(th.card_title(title, subtitle), unsafe_allow_html=True)


# =========================================================================
# Section: Executive overview
# =========================================================================

def build_insights(ctx: Ctx, rs: dict) -> list[str]:
    out = []
    k, m = ctx.kpis, ctx.monthly
    if not m.empty and m["total_revenue"].sum() > 0:
        best = m.loc[m["total_revenue"].idxmax()]
        out.append(f"**{an.month_label(best['month_key'])}** was the strongest month: {th.money_compact(best['total_revenue'])} "
                   f"({best['total_revenue'] / m['total_revenue'].sum() * 100:.1f}% of revenue in the selected period).")
    c = ctx.dash.countries
    if not c.empty and c["total_revenue"].sum() > 0:
        c = c.sort_values("total_revenue", ascending=False)
        top = c.iloc[0]
        out.append(f"**{top['Country']}** is the largest market with {top['total_revenue'] / c['total_revenue'].sum() * 100:.1f}% of revenue.")
        if len(c) > 1:
            second = c.iloc[1]
            out.append(f"The next largest market is **{second['Country']}** ({th.money_compact(second['total_revenue'])}).")
    p = ctx.products
    if not p.empty:
        best_p = p.sort_values("total_revenue", ascending=False).iloc[0]
        out.append(f"Top product by revenue: **{clip(best_p['Description'], 48)}** ({th.money_compact(best_p['total_revenue'])}, "
                   f"{th.number(best_p['units_sold'])} units).")
        share = an.share_held_by_top(p, 0.10)
        if share is not None and len(p) >= 20:
            out.append(f"The top 10% of products generate **{share:.0f}%** of product revenue.")
    if rs.get("repeat_rate") is not None:
        out.append(f"**{rs['repeat_rate']:.1f}%** of identified customers placed two or more invoices.")
    admin = ctx.dash.products
    if "is_admin" in admin.columns and admin["total_revenue"].sum() > 0:
        a_share = admin.loc[admin["is_admin"], "total_revenue"].sum() / admin["total_revenue"].sum() * 100
        if a_share >= 0.1:
            out.append(f"Postage, fees and manual entries make up {a_share:.1f}% of revenue; they are kept in KPIs "
                       "but can be hidden from product rankings.")
    return out


def view_overview(ctx: Ctx) -> None:
    st.markdown(th.section("Executive overview", "Headline performance for the selected period and filters."),
                unsafe_allow_html=True)
    filter_bar(ctx)
    k = ctx.kpis
    freq = db.query_customer_frequency(client, *ctx.restrict)
    rs = an.repeat_stats(freq)
    aiv = k["revenue"] / k["invoices"] if k["invoices"] else None
    st.markdown(th.kpi_grid([
        {"label": "Total revenue", "value": th.money_compact(k["revenue"]), "sub": th.money(k["revenue"]), "accent": "blue",
         "help": "Sum of Quantity × UnitPrice over accepted sales lines"},
        {"label": "Invoices", "value": th.number(k["invoices"]), "sub": f"{th.number(k['transaction_lines'])} transaction lines",
         "accent": "teal", "help": "Distinct invoice numbers"},
        {"label": "Average invoice value", "value": th.money(aiv) if aiv is not None else "n/a",
         "sub": "Revenue ÷ invoices", "accent": "violet"},
        {"label": "Unique customers", "value": th.number(k.get("customers", k.get("unique_customers", 0))),
         "sub": "Distinct customer IDs (guest lines excluded)", "accent": "navy",
         "help": "Counted over the whole selection, never summed across months"},
        {"label": "Units sold", "value": th.number_compact(k["units"]), "sub": f"{th.number(k['units'])} items", "accent": "sky"},
        {"label": "Repeat customers", "value": th.pct(rs["repeat_rate"]) if rs["repeat_rate"] is not None else "n/a",
         "sub": "Customers with 2+ invoices", "accent": "teal"},
    ]), unsafe_allow_html=True)

    st.markdown(th.section("Month-over-month", "Latest complete month compared with the month before it."),
                unsafe_allow_html=True)
    mom = an.mom_changes(ctx.monthly, ctx.partial)
    if mom["ok"]:
        st.markdown(f"Comparing **{an.month_label(mom['current'])}** with **{an.month_label(mom['previous'])}**.")
        st.markdown(th.mom_grid(mom, an.month_label), unsafe_allow_html=True)
        if mom["note"]:
            st.caption(mom["note"])
    else:
        st.markdown(th.callout("info", mom["reason"]), unsafe_allow_html=True)

    left, right = st.columns([2, 1])
    with left:
        with th.card("ov_trend"):
            card_header("Monthly revenue", "Hover for exact values")
            if ctx.monthly.empty:
                st.info("No monthly data for these filters.")
            else:
                plot(trend_figure(ctx.monthly, "total_revenue", "Revenue", "money", "Area", ctx.partial), "ov_trend_fig")
    with right:
        with th.card("ov_country"):
            card_header("Revenue by country", "Top 6 markets")
            c = ctx.dash.countries.sort_values("total_revenue", ascending=False).head(6)
            if c.empty:
                st.info("No country data for these filters.")
            else:
                plot(hbar(c["Country"], c["total_revenue"], c[["invoice_count", "unique_customers"]].to_numpy(),
                          f"<b>%{{y}}</b><br>Revenue: {th.CURRENCY}%{{x:,.2f}}<br>Invoices: %{{customdata[0]:,.0f}}"
                          "<br>Customers: %{customdata[1]:,.0f}<extra></extra>",
                          th.TEAL, th.money_compact), "ov_country_fig")

    left, right = st.columns([3, 2])
    with left:
        with th.card("ov_products"):
            card_header("Top 10 products by revenue",
                        "Merchandise only" if ctx.exclude_admin else "Includes postage, fees and manual entries")
            p = ctx.products.sort_values("total_revenue", ascending=False).head(10)
            if p.empty:
                st.info("No products match these filters.")
            else:
                labels = [f"{clip(d, 38)} ({c})" for d, c in zip(p["Description"], p["StockCode"])]
                plot(hbar(labels, p["total_revenue"], p[["units_sold", "invoice_count"]].to_numpy(),
                          f"<b>%{{y}}</b><br>Revenue: {th.CURRENCY}%{{x:,.2f}}<br>Units: %{{customdata[0]:,.0f}}"
                          "<br>Invoices: %{customdata[1]:,.0f}<extra></extra>",
                          th.BLUE, th.money_compact, height=380), "ov_products_fig")
    with right:
        with th.card("ov_insights"):
            card_header("Key insights", "Generated from the figures on this page")
            insights = build_insights(ctx, rs)
            if insights:
                st.markdown("\n".join(f"- {line}" for line in insights))
            else:
                st.info("Not enough data for insights with these filters.")


# =========================================================================
# Section: Sales trends
# =========================================================================

def view_trends(ctx: Ctx) -> None:
    st.markdown(th.section("Sales trends", "Month-by-month performance, growth, unusual months and cancellations."),
                unsafe_allow_html=True)
    filter_bar(ctx, ("kpis", "monthly"))
    if ctx.monthly.empty:
        st.info("No monthly data for these filters.")
        return

    c1, c2, c3 = st.columns([2, 1.4, 1])
    with c1:
        metric = st.selectbox("Metric", list(TREND_METRICS), key="trend_metric")
    with c2:
        chart = st.radio("Chart type", ["Area", "Line", "Bar"], horizontal=True, key="trend_chart")
    column, kind = TREND_METRICS[metric]

    with th.card("tr_main"):
        card_header(f"Monthly {metric.lower()}",
                    "Unique customers are distinct within each month; months cannot be added together."
                    if metric == "Unique customers" else "Hover for exact values")
        plot(trend_figure(ctx.monthly, column, metric, kind, chart, ctx.partial), "tr_main_fig")
    if ctx.partial and ctx.partial in set(ctx.monthly["month_key"]):
        st.markdown(th.callout("info", f"{an.month_label(ctx.partial)} is only partly covered (the data ends "
                                       f"{ctx.ref.last_invoice_date:%d %b %Y}). It is marked on the chart and excluded "
                                       "from growth and anomaly calculations."), unsafe_allow_html=True)

    left, right = st.columns(2)
    with left:
        with th.card("tr_growth"):
            card_header(f"Month-over-month change in {metric.lower()}", "Only consecutive, complete months are compared")
            series = an.mom_series(ctx.monthly, column, ctx.partial)
            if series.notna().sum() == 0:
                st.info("Needs at least two consecutive complete months.")
            else:
                colors = np.where(series.fillna(0) >= 0, th.TEAL, "#DC2626")
                fig = go.Figure(go.Bar(x=ctx.monthly["month_key"].map(an.month_label), y=series, marker_color=colors,
                                       hovertemplate="<b>%{x}</b><br>Change: %{y:,.1f}%<extra></extra>"))
                fig.update_xaxes(type="category", tickangle=-35)
                fig.update_yaxes(ticksuffix="%")
                plot(th.style_fig(fig, height=300), "tr_growth_fig")
    with right:
        with th.card("tr_dual"):
            card_header("Invoices and average invoice value", "Bars: invoices · line: average invoice value")
            fig = go.Figure()
            x = ctx.monthly["month_key"].map(an.month_label)
            fig.add_bar(x=x, y=ctx.monthly["invoice_count"], name="Invoices", marker_color="#93B4F8",
                        hovertemplate="<b>%{x}</b><br>Invoices: %{y:,.0f}<extra></extra>")
            fig.add_scatter(x=x, y=ctx.monthly["avg_invoice_value"], name="Avg invoice value", yaxis="y2",
                            mode="lines+markers", line=dict(color=th.VIOLET, width=3),
                            hovertemplate=f"<b>%{{x}}</b><br>Avg invoice: {th.CURRENCY}%{{y:,.2f}}<extra></extra>")
            fig.update_layout(yaxis2=dict(overlaying="y", side="right", showgrid=False, tickprefix=th.CURRENCY,
                                          tickfont=dict(color=th.INK)))
            fig.update_xaxes(type="category", tickangle=-35)
            plot(th.style_fig(fig, height=300, legend=True), "tr_dual_fig")

    with th.card("tr_anom"):
        card_header("Unusual months (screening)",
                    "Robust modified z-score on the selected metric. A descriptive screen, not a forecast.")
        threshold = st.slider("Sensitivity threshold (|z| above this is flagged)", 2.0, 5.0, 3.5, 0.5, key="anom_thr")
        res = an.flag_anomalies(ctx.monthly, column, ctx.partial, threshold)
        if not res["ok"]:
            st.markdown(th.callout("info", res["reason"]), unsafe_allow_html=True)
        else:
            frame = res["frame"][["month_key", column, "z_score", "is_anomaly"]].copy()
            frame["month_key"] = frame["month_key"].map(an.month_label)
            frame.columns = ["Month", metric, "Modified z-score", "Flagged"]
            if res["count"]:
                st.markdown(th.callout("warn", f"{res['count']} month(s) exceed the threshold of {threshold}."),
                            unsafe_allow_html=True)
            else:
                st.markdown(th.callout("ok", f"No month exceeds the threshold of {threshold}."), unsafe_allow_html=True)
            fmt = {metric: th.CURRENCY + "{:,.2f}"} if kind == "money" else {metric: "{:,.0f}"}
            fmt["Modified z-score"] = "{:+.2f}"
            st.dataframe(frame.style.format(fmt), hide_index=True, height=min(420, 40 + 35 * len(frame)))
        st.caption("With about a year of monthly points this cannot separate seasonality from anomalies. "
                   "Treat flags as prompts to investigate. RetailPulse does not forecast: one seasonal cycle is "
                   "too little history for a defensible forecast.")

    with th.card("tr_cancel"):
        card_header("Returns and cancellations", "Cancellation invoices (invoice numbers starting with C), by month of cancellation")
        try:
            canc = db.load_optional_table(client, "gold_cancellations_monthly")
        except Exception as exc:  # noqa: BLE001
            show_error(exc, "canc")
            canc = None
        if canc is None:
            st.markdown(th.callout("info", "The table gold_cancellations_monthly does not exist yet. Run the pipeline "
                                           "notebook (pipeline/retailpulse_pipeline.py) once, then click Refresh data."),
                        unsafe_allow_html=True)
        else:
            view = an.cancellation_view(ctx.ref.monthly, canc, ctx.months)
            if ctx.restrict[1] or ctx.restrict[2]:
                st.markdown(th.callout("warn", "Cancellations are stored by month only. Country and product filters do "
                                               "not apply here; the percentage compares all-market cancellations "
                                               "with all-market revenue."), unsafe_allow_html=True)
            if view is None or view.empty:
                st.info("No cancellations recorded for the selected months.")
            else:
                x = view["month_key"].map(an.month_label)
                fig = go.Figure()
                fig.add_bar(x=x, y=view["cancelled_value"], name="Cancelled value", marker_color="#DC2626",
                            hovertemplate=f"<b>%{{x}}</b><br>Cancelled: {th.CURRENCY}%{{y:,.2f}}<extra></extra>")
                fig.add_scatter(x=x, y=view["cancelled_pct_of_sales"], name="% of sales revenue", yaxis="y2",
                                mode="lines+markers", line=dict(color=th.NAVY, width=3),
                                hovertemplate="<b>%{x}</b><br>% of sales: %{y:,.1f}%<extra></extra>")
                fig.update_layout(yaxis2=dict(overlaying="y", side="right", showgrid=False, ticksuffix="%",
                                              tickfont=dict(color=th.INK)))
                fig.update_xaxes(type="category", tickangle=-35)
                plot(th.style_fig(fig, height=300, legend=True, currency_axis="y"), "tr_cancel_fig")
                total_c, total_s = view["cancelled_value"].sum(), view["total_revenue"].sum()
                st.caption(f"Cancelled value in the period: {th.money(total_c)} "
                           f"({total_c / total_s * 100:.1f}% of sales revenue) across "
                           f"{th.number(view['cancellation_invoices'].sum())} cancellation invoices. "
                           "Cancellations are dated by the cancellation invoice, not the original sale.")


# =========================================================================
# Section: Product analytics
# =========================================================================

def view_products(ctx: Ctx) -> None:
    st.markdown(th.section("Product analytics", "Which products drive revenue and volume, and how concentrated sales are."),
                unsafe_allow_html=True)
    filter_bar(ctx, ("kpis", "products"))
    p = ctx.products
    if p.empty:
        st.info("No products match the current filters.")
        return

    all_p = ctx.dash.products
    admin_rev = all_p.loc[all_p["is_admin"], "total_revenue"].sum() if "is_admin" in all_p else 0.0
    total_rev = all_p["total_revenue"].sum()
    top_share = an.share_held_by_top(p, 0.10)
    st.markdown(th.kpi_grid([
        {"label": "Products listed", "value": th.number(len(p)), "sub": "Stock code + description combinations", "accent": "blue"},
        {"label": "Product revenue", "value": th.money_compact(p["total_revenue"].sum()), "sub": th.money(p["total_revenue"].sum()), "accent": "teal"},
        {"label": "Units sold", "value": th.number_compact(p["units_sold"].sum()), "sub": f"{th.number(p['units_sold'].sum())} items", "accent": "violet"},
        {"label": "Top 10% share", "value": th.pct(top_share, 0) if top_share is not None else "n/a",
         "sub": "Revenue from the best-selling 10% of products", "accent": "navy"},
        {"label": "Non-merchandise lines", "value": th.money_compact(admin_rev),
         "sub": f"{admin_rev / total_rev * 100:.1f}% of revenue (postage, fees, manual)" if total_rev else "", "accent": "sky"},
    ]), unsafe_allow_html=True)

    c1, c2, c3 = st.columns([1.3, 1.3, 1])
    with c1:
        metric = st.selectbox("Rank by", ["Revenue", "Units sold", "Invoices"], key="prod_metric")
    with c2:
        direction = st.radio("Show", ["Top", "Bottom"], horizontal=True, key="prod_dir")
    with c3:
        n = st.select_slider("Items", options=[5, 10, 15, 20, 25], value=10, key="prod_n")
    col = {"Revenue": "total_revenue", "Units sold": "units_sold", "Invoices": "invoice_count"}[metric]
    ranked = p[p[col] > 0].sort_values(col, ascending=(direction == "Bottom")).head(n)

    left, right = st.columns([3, 2])
    with left:
        with th.card("pr_rank"):
            card_header(f"{direction} {n} products by {metric.lower()}",
                        "Bottom lists show the smallest positive sellers; volumes may be tiny." if direction == "Bottom" else None)
            labels = [f"{clip(d, 38)} ({c})" for d, c in zip(ranked["Description"], ranked["StockCode"])]
            money_metric = col == "total_revenue"
            plot(hbar(labels, ranked[col], ranked[["total_revenue", "units_sold", "invoice_count"]].to_numpy(),
                      "<b>%{y}</b><br>Revenue: " + th.CURRENCY + "%{customdata[0]:,.2f}<br>Units: %{customdata[1]:,.0f}"
                      "<br>Invoices: %{customdata[2]:,.0f}<extra></extra>",
                      th.BLUE if direction == "Top" else th.VIOLET,
                      th.money_compact if money_metric else th.number, height=max(300, 28 * n + 40)), "pr_rank_fig")
    with right:
        with th.card("pr_pareto"):
            card_header("Revenue concentration", "Cumulative revenue vs share of products (best sellers first)")
            curve = an.pareto(p)
            if curve.empty:
                st.info("Not enough data.")
            else:
                fig = go.Figure(go.Scatter(x=curve["product_share"], y=curve["cumulative_share"], mode="lines",
                                           line=dict(color=th.TEAL, width=3), fill="tozeroy", fillcolor="rgba(13,148,136,0.12)",
                                           hovertemplate="Top %{x:.1f}% of products<br>= %{y:.1f}% of revenue<extra></extra>"))
                fig.update_xaxes(ticksuffix="%", title="Share of products")
                fig.update_yaxes(ticksuffix="%", title="Cumulative revenue", range=[0, 100])
                plot(th.style_fig(fig, height=max(300, 28 * n + 40)), "pr_pareto_fig")

    with th.card("pr_scatter"):
        card_header("Revenue vs units sold", "Each dot is a product (top 400 by revenue), log scales; hover for details")
        top = p.sort_values("total_revenue", ascending=False).head(400)
        top = top[(top["units_sold"] > 0) & (top["total_revenue"] > 0)]
        if top.empty:
            st.info("Not enough data.")
        else:
            fig = go.Figure(go.Scatter(
                x=top["units_sold"], y=top["total_revenue"], mode="markers",
                marker=dict(size=np.clip(np.sqrt(top["invoice_count"]) * 2.2, 5, 26), color=th.BLUE, opacity=0.55,
                            line=dict(width=0.5, color="#FFFFFF")),
                customdata=np.column_stack([top["Description"].map(lambda s: clip(s, 50)), top["StockCode"], top["invoice_count"]]),
                hovertemplate="<b>%{customdata[0]}</b> (%{customdata[1]})<br>Units: %{x:,.0f}<br>Revenue: "
                              + th.CURRENCY + "%{y:,.2f}<br>Invoices: %{customdata[2]}<extra></extra>"))
            fig.update_xaxes(type="log", title="Units sold (log scale)")
            fig.update_yaxes(type="log", title="Revenue (log scale)", tickprefix=th.CURRENCY)
            plot(th.style_fig(fig, height=380), "pr_scatter_fig")
            st.caption("Dot size reflects the number of invoices. Products above the cloud earn more per unit.")
    st.caption("Full searchable product table and CSV export: “Detailed tables & exports”.")


# =========================================================================
# Section: Country analytics
# =========================================================================

def view_countries(ctx: Ctx) -> None:
    st.markdown(th.section("Country analytics", "Where revenue, orders and customers come from."), unsafe_allow_html=True)
    filter_bar(ctx, ("kpis", "countries"))
    c = ctx.dash.countries.copy()
    if c.empty:
        st.info("No countries match the current filters.")
        return
    c["avg_invoice_value"] = np.where(c["invoice_count"] > 0, c["total_revenue"] / c["invoice_count"].replace(0, np.nan), np.nan)
    total = c["total_revenue"].sum()
    ranked_all = c.sort_values("total_revenue", ascending=False)
    top = ranked_all.iloc[0]
    exports = ranked_all.iloc[1] if len(ranked_all) > 1 else None
    st.markdown(th.kpi_grid([
        {"label": "Countries", "value": th.number(len(c)), "sub": "With sales in the selection", "accent": "blue"},
        {"label": "Largest market", "value": str(top["Country"]), "sub": f"{top['total_revenue'] / total * 100:.1f}% of revenue" if total else "", "accent": "teal"},
        {"label": "Next largest", "value": str(exports["Country"]) if exports is not None else "n/a",
         "sub": th.money_compact(exports["total_revenue"]) if exports is not None else "", "accent": "violet"},
        {"label": "Top 3 share", "value": th.pct(ranked_all["total_revenue"].head(3).sum() / total * 100, 0) if total else "n/a",
         "sub": "Revenue held by the three largest markets", "accent": "navy"},
    ]), unsafe_allow_html=True)

    c1, c2, c3, c4 = st.columns([1.5, 1, 1.2, 1.4])
    with c1:
        metric = st.selectbox("Metric", ["Revenue", "Invoices", "Unique customers", "Average invoice value"], key="cty_metric")
    with c2:
        n = st.select_slider("Countries", options=[5, 10, 15, 20], value=10, key="cty_n")
    with c3:
        exclude_home = st.checkbox("Hide United Kingdom", key="cty_hide_uk",
                                   help="The UK is the home market and dominates the scale; hide it to compare export markets.")
    with c4:
        min_inv = st.number_input("Min. invoices (for averages)", 1, 1000, 10, key="cty_min_inv",
                                  help="Average invoice value is unreliable for countries with very few invoices.")
    col = {"Revenue": "total_revenue", "Invoices": "invoice_count", "Unique customers": "unique_customers",
           "Average invoice value": "avg_invoice_value"}[metric]
    view = c[c["Country"] != "United Kingdom"] if exclude_home else c
    if metric == "Average invoice value":
        view = view[view["invoice_count"] >= min_inv]
    view = view.sort_values(col, ascending=False).head(n)

    left, right = st.columns([3, 2])
    with left:
        with th.card("cty_rank"):
            card_header(f"Top {len(view)} countries by {metric.lower()}")
            if view.empty:
                st.info("No countries meet these settings.")
            else:
                money_metric = col in ("total_revenue", "avg_invoice_value")
                hover = ("<b>%{y}</b><br>Revenue: " + th.CURRENCY + "%{customdata[0]:,.2f}<br>Invoices: %{customdata[1]:,.0f}"
                         "<br>Customers: %{customdata[2]:,.0f}<br>Avg invoice: " + th.CURRENCY + "%{customdata[3]:,.2f}<extra></extra>")
                plot(hbar(view["Country"], view[col], view[["total_revenue", "invoice_count", "unique_customers", "avg_invoice_value"]].to_numpy(),
                          hover, th.TEAL, th.money_compact if money_metric else th.number, height=max(300, 28 * len(view) + 40)),
                     "cty_rank_fig")
    with right:
        with th.card("cty_share"):
            card_header("Revenue share", "Top markets vs all others")
            head = ranked_all.head(6)
            others = ranked_all["total_revenue"].iloc[6:].sum()
            labels = list(head["Country"]) + (["All others"] if others > 0 else [])
            values = list(head["total_revenue"]) + ([others] if others > 0 else [])
            fig = go.Figure(go.Pie(labels=labels, values=values, hole=0.58, sort=False,
                                   marker=dict(colors=th.COLORWAY[: len(labels)], line=dict(color="#FFFFFF", width=2)),
                                   textinfo="percent", textfont=dict(color="#FFFFFF", size=12),
                                   hovertemplate=f"<b>%{{label}}</b><br>{th.CURRENCY}%{{value:,.2f}} (%{{percent}})<extra></extra>"))
            th.style_fig(fig, height=max(300, 28 * len(view) + 40), legend=True)
            fig.update_layout(legend=dict(orientation="h", y=-0.05, font=dict(color=th.INK)))
            plot(fig, "cty_share_fig")

    with th.card("cty_scatter"):
        card_header("Order size vs order count", "Each bubble is a country; bubble size = revenue")
        s = c[c["invoice_count"] >= max(1, int(min_inv))]
        if exclude_home:
            s = s[s["Country"] != "United Kingdom"]
        if s.empty:
            st.info("No countries meet the minimum invoice threshold.")
        else:
            fig = go.Figure(go.Scatter(
                x=s["invoice_count"], y=s["avg_invoice_value"], mode="markers+text", text=s["Country"],
                textposition="top center", textfont=dict(size=10, color=th.INK),
                marker=dict(size=np.clip(np.sqrt(s["total_revenue"]) / 12, 8, 50), color=th.VIOLET, opacity=0.55,
                            line=dict(width=1, color="#FFFFFF")),
                customdata=s[["total_revenue", "unique_customers"]].to_numpy(),
                hovertemplate="<b>%{text}</b><br>Invoices: %{x:,.0f}<br>Avg invoice: " + th.CURRENCY + "%{y:,.2f}"
                              "<br>Revenue: " + th.CURRENCY + "%{customdata[0]:,.2f}<br>Customers: %{customdata[1]:,.0f}<extra></extra>"))
            fig.update_xaxes(type="log", title="Invoices (log scale)")
            fig.update_yaxes(title="Average invoice value", tickprefix=th.CURRENCY)
            plot(th.style_fig(fig, height=380), "cty_scatter_fig")
    st.caption("Customer counts per country are distinct within each country; do not add them across countries. "
               "Full table and CSV export: “Detailed tables & exports”.")


# =========================================================================
# Section: Customer analytics
# =========================================================================

def view_customers(ctx: Ctx) -> None:
    st.markdown(th.section("Customer analytics", "Purchase frequency, repeat buying and customer concentration."),
                unsafe_allow_html=True)
    st.markdown(th.chips([("Period", ctx.period_label), ("Countries", "All" if not ctx.restrict[1] else f"{len(ctx.restrict[1])} selected"),
                          ("Product search", ctx.term or "none")], {"Customer data": "Silver"}), unsafe_allow_html=True)
    ident = db.query_identification(client, *ctx.restrict)
    freq = db.query_customer_frequency(client, *ctx.restrict)
    top = db.query_top_customers(client, *ctx.restrict)
    flow = db.query_customer_flow(client, *ctx.restrict)
    rs = an.repeat_stats(freq)
    if rs.get("customers", 0) == 0:
        st.markdown(th.callout("warn", "No identified customers in the current selection."), unsafe_allow_html=True)
        return

    total_rev = ident["identified_revenue"] + ident["anonymous_revenue"]
    ident_share = ident["identified_revenue"] / total_rev * 100 if total_rev else None
    top10_share = top["total_revenue"].head(10).sum() / ident["identified_revenue"] * 100 if ident["identified_revenue"] else None
    st.markdown(th.kpi_grid([
        {"label": "Identified customers", "value": th.number(rs.get("customers", 0)), "sub": "Distinct customer IDs", "accent": "blue"},
        {"label": "Repeat customers", "value": th.pct(rs["repeat_rate"]), "sub": f"{th.number(rs['repeat_customers'])} placed 2+ invoices", "accent": "teal"},
        {"label": "Invoices per customer", "value": th.number(rs.get("avg_invoices", 0), 2), "sub": "Average over identified customers", "accent": "violet"},
        {"label": "Revenue with customer ID", "value": th.pct(ident_share), "sub": f"{th.number(ident['anonymous_lines'])} guest lines have no ID", "accent": "navy"},
        {"label": "Top 10 customers", "value": th.pct(top10_share), "sub": "Share of identified revenue", "accent": "sky"},
    ]), unsafe_allow_html=True)
    st.markdown(th.callout("info", "Customer analytics use only sales lines that carry a CustomerID. Guest lines "
                                   "(no ID) still count in revenue, invoices and units, but cannot be tied to a customer."),
                unsafe_allow_html=True)

    left, right = st.columns(2)
    with left:
        with th.card("cu_freq"):
            card_header("How often do customers buy?", "Customers by number of invoices placed")
            b = an.bucket_frequency(freq, 10)
            fig = go.Figure(go.Bar(x=b["bucket"], y=b["customers"], marker_color=th.BLUE,
                                   hovertemplate="%{x} invoice(s)<br>%{y:,.0f} customers<extra></extra>",
                                   text=b["customers"].map(lambda v: f"{v:,.0f}"), textposition="outside", cliponaxis=False,
                                   textfont=dict(color=th.INK, size=11)))
            fig.update_xaxes(title="Invoices placed", type="category")
            fig.update_yaxes(title="Customers")
            plot(th.style_fig(fig, height=340), "cu_freq_fig")
    with right:
        with th.card("cu_flow"):
            card_header("New vs returning customers", "By month; “new” = first purchase in the whole dataset")
            if flow.empty:
                st.info("No data.")
            else:
                x = flow["month_key"].map(an.month_label)
                fig = go.Figure()
                fig.add_bar(x=x, y=flow["new_customers"], name="New", marker_color=th.TEAL,
                            hovertemplate="<b>%{x}</b><br>New: %{y:,.0f}<extra></extra>")
                fig.add_bar(x=x, y=flow["returning_customers"], name="Returning", marker_color=th.BLUE,
                            hovertemplate="<b>%{x}</b><br>Returning: %{y:,.0f}<extra></extra>")
                fig.update_layout(barmode="stack")
                fig.update_xaxes(type="category", tickangle=-35)
                plot(th.style_fig(fig, height=340, legend=True), "cu_flow_fig")
    first_key = ctx.ref.months[0]
    if first_key in set(flow["month_key"]):
        st.caption(f"Every customer active in {an.month_label(first_key)} counts as “new” because the dataset starts that month.")

    with th.card("cu_top"):
        card_header("Top 15 customers by revenue", "Customer IDs are pseudonymous identifiers from the source data")
        t = top.head(15)
        plot(hbar([f"Customer {c}" for c in t["CustomerID"]], t["total_revenue"], t[["invoice_count", "units_bought", "Country"]].to_numpy(),
                  "<b>%{y}</b><br>Revenue: " + th.CURRENCY + "%{x:,.2f}<br>Invoices: %{customdata[0]:,.0f}"
                  "<br>Units: %{customdata[1]:,.0f}<br>Country: %{customdata[2]}<extra></extra>",
                  th.VIOLET, th.money_compact, height=470), "cu_top_fig")


# =========================================================================
# Section: Data quality & pipeline monitoring
# =========================================================================

def view_quality(ctx: Ctx) -> None:
    st.markdown(th.section("Data quality & pipeline monitoring",
                           "Live checks of the Bronze, Silver and Gold tables. This page ignores the sidebar filters."),
                unsafe_allow_html=True)

    # 1. live row counts vs recorded
    counts = db.load_table_counts(client)
    ok_all = bool(counts["matches"].all())
    st.markdown(th.callout("ok" if ok_all else "warn",
                           "All five tables match the row counts recorded after the original pipeline run." if ok_all else
                           "At least one table differs from the recorded baseline. This is expected only if the source data "
                           "or pipeline changed on purpose."), unsafe_allow_html=True)
    with th.card("dq_counts"):
        card_header("Row counts: live vs recorded", "Counted now in Databricks")
        view = counts.rename(columns={"table_name": "Table", "live_rows": "Live rows", "recorded_rows": "Recorded rows"})
        view["Status"] = np.where(view["matches"], "✔ matches", "✖ differs")
        st.dataframe(view[["Table", "Live rows", "Recorded rows", "Status"]].style.format({"Live rows": "{:,.0f}", "Recorded rows": "{:,.0f}"}),
                     hide_index=True)

    # 2. reconciliation Gold vs Silver
    base = db.query_kpis(client, None, None, "")
    rec = an.reconcile(base, ref.monthly, ref.products, ref.countries)
    with th.card("dq_recon"):
        card_header("Gold vs Silver reconciliation", "Every Gold table must add up to the Silver totals")
        view = rec.rename(columns={"check": "Check", "silver": "Silver", "gold": "Gold", "difference": "Difference", "status": "Status"})
        st.dataframe(view[["Check", "Silver", "Gold", "Difference", "Status"]].style.format(
            {"Silver": "{:,.2f}", "Gold": "{:,.2f}", "Difference": "{:+,.2f}"}), hide_index=True)
        if (rec["status"] == "FAIL").any():
            st.markdown(th.callout("bad", "A Gold table does not match Silver. Re-run the pipeline notebook and inspect the "
                                          "data_quality_metrics table."), unsafe_allow_html=True)

    # 3. Silver rule profile
    prof = db.load_silver_profile(client)
    rows = [
        ("Silver lines", prof["rows_total"], "info", "Accepted sales lines"),
        ("Quantity ≤ 0", prof["non_positive_quantity"], "rule", "Must be 0 (returns are excluded from Silver)"),
        ("Revenue ≤ 0", prof["non_positive_revenue"], "rule", "Must be 0"),
        ("Invoices spanning two months", prof["invoices_spanning_months"], "rule", "Must be 0 so monthly invoice counts can be added"),
        ("Lines without CustomerID", prof["lines_no_customer"], "info", f"{prof['lines_no_customer'] / prof['rows_total'] * 100:.1f}% of lines (guest checkouts)"),
        ("Lines without description", prof["lines_no_description"], "info", "Informational"),
    ]
    with th.card("dq_profile"):
        card_header("Silver business-rule profile", "Computed live from silver_transactions")
        table = pd.DataFrame(rows, columns=["Check", "Count", "Type", "Meaning"])
        table["Status"] = [("PASS" if r[1] == 0 else "FAIL") if r[2] == "rule" else "INFO" for r in rows]
        st.dataframe(table[["Check", "Count", "Status", "Meaning"]].style.format({"Count": "{:,.0f}"}), hide_index=True)

    # 4. pipeline notebook results (optional tables)
    st.markdown(th.section("Pipeline run results", "Written by pipeline/retailpulse_pipeline.py (optional)."), unsafe_allow_html=True)
    try:
        dq = db.load_optional_table(client, "data_quality_metrics")
        log = db.load_optional_table(client, "pipeline_run_log")
        rejected = db.load_rejected_summary(client)
    except Exception as exc:  # noqa: BLE001
        show_error(exc, "dq")
        return
    if dq is None or dq.empty:
        st.markdown(th.callout("info", "No pipeline audit tables found yet. In Databricks, open the notebook "
                                       "pipeline/retailpulse_pipeline.py and Run all (default settings do not modify "
                                       "your existing tables), then click Refresh data."), unsafe_allow_html=True)
        return

    dq["checked_at"] = pd.to_datetime(dq["checked_at"], errors="coerce")
    run_id = dq.loc[dq["checked_at"].idxmax(), "pipeline_run_id"]
    latest = dq[dq["pipeline_run_id"] == run_id]
    n_pass, n_fail, n_info = (int((latest["status"] == s).sum()) for s in ("PASS", "FAIL", "INFO"))
    st.markdown(th.kpi_grid([
        {"label": "Latest run", "value": str(run_id), "sub": f"{latest['checked_at'].max():%d %b %Y %H:%M}", "accent": "blue"},
        {"label": "Checks passed", "value": th.number(n_pass), "sub": "Of rule-based checks", "accent": "teal"},
        {"label": "Checks failed", "value": th.number(n_fail), "sub": "Must be 0", "accent": "violet" if n_fail == 0 else "navy"},
        {"label": "Informational", "value": th.number(n_info), "sub": "Profiles and counts", "accent": "sky"},
    ]), unsafe_allow_html=True)
    if n_fail:
        st.markdown(th.callout("bad", f"{n_fail} check(s) failed in the latest run. See the table below."), unsafe_allow_html=True)

    left, right = st.columns([3, 2])
    with left:
        with th.card("dq_checks"):
            card_header("Quality checks (latest run)")
            shown = latest[["check_name", "category", "expected", "actual", "status", "detail"]].rename(columns={
                "check_name": "Check", "category": "Category", "expected": "Expected", "actual": "Actual",
                "status": "Status", "detail": "Detail"})
            st.dataframe(shown, hide_index=True, height=420)
    with right:
        with th.card("dq_rejected"):
            card_header("Rejected records by reason", "Kept in silver_rejected_records for audit")
            if rejected is None or rejected.empty:
                st.info("No rejected-record table yet.")
            else:
                bronze = int(counts.loc[counts["table_name"] == "bronze_transactions", "live_rows"].iloc[0])
                rejected = rejected.assign(share=rejected["records"] / bronze * 100)
                fig = go.Figure(go.Bar(x=rejected["records"], y=rejected["reject_reason"].str.replace("_", " ").str.title(),
                                       orientation="h", marker_color=th.VIOLET,
                                       customdata=rejected["share"], hovertemplate="<b>%{y}</b><br>%{x:,.0f} records (%{customdata:.2f}% of Bronze)<extra></extra>"))
                fig.update_yaxes(autorange="reversed")
                plot(th.style_fig(fig, height=300), "dq_rejected_fig")
                st.caption(f"Rejected total: {th.number(rejected['records'].sum())} of {th.number(bronze)} Bronze rows.")
    if log is not None and not log.empty:
        with th.card("dq_log"):
            card_header("Pipeline run log", "Stages of the most recent runs")
            st.dataframe(log.sort_values("started_at", ascending=False).head(30), hide_index=True, height=300)

    with st.expander("Metric definitions used everywhere in RetailPulse"):
        st.markdown(
            "- **Revenue** = Quantity × UnitPrice on accepted sales lines (cancellations, returns, zero/negative price lines and duplicates excluded).\n"
            "- **Invoices** = distinct InvoiceNo. Valid to add across months and countries because the check above shows an invoice never spans two.\n"
            "- **Unique customers** = distinct CustomerID within the selection. Never added across months or countries.\n"
            "- **Units sold** = sum of Quantity on accepted sales lines.\n"
            "- **Average invoice value** = Revenue ÷ Invoices.")


# =========================================================================
# Section: Detailed tables & exports
# =========================================================================

def view_tables(ctx: Ctx) -> None:
    st.markdown(th.section("Detailed tables & exports", "Search, sort (click a column header) and download the data behind the charts."),
                unsafe_allow_html=True)
    filter_bar(ctx)
    t_month, t_prod, t_country, t_cust = st.tabs(["Monthly", "Products", "Countries", "Top customers"])

    with t_month:
        m = ctx.monthly.rename(columns={"month_key": "Month", "transaction_lines": "Transaction lines", "total_revenue": "Revenue",
                                        "invoice_count": "Invoices", "unique_customers": "Unique customers",
                                        "avg_invoice_value": "Average invoice value"})
        m = m[["Month", "Revenue", "Invoices", "Average invoice value", "Unique customers", "Transaction lines"]]
        st.caption("Unique customers are distinct within each month and must not be added together.")
        st.dataframe(styled(m, money_cols=["Revenue", "Average invoice value"], int_cols=["Invoices", "Unique customers", "Transaction lines"]),
                     hide_index=True, height=min(520, 40 + 35 * max(len(m), 1)))
        st.download_button("Download monthly CSV", csv_bytes(m), f"retailpulse_monthly_{file_stamp(ctx)}.csv", "text/csv", key="dl_month")

    with t_prod:
        p = ctx.products.copy()
        p["Revenue per unit"] = np.where(p["units_sold"] > 0, p["total_revenue"] / p["units_sold"].replace(0, np.nan), np.nan)
        p = p.rename(columns={"StockCode": "Stock code", "Description": "Product", "units_sold": "Units sold",
                              "total_revenue": "Revenue", "invoice_count": "Invoices"})
        p = p[["Stock code", "Product", "Type", "Revenue", "Units sold", "Invoices", "Revenue per unit"]].sort_values("Revenue", ascending=False)
        q = st.text_input("Search products", key="tbl_prod_q", placeholder="Type part of a description or stock code")
        p = search_frame(p, q)
        st.caption(f"{len(p):,} products · {'merchandise only' if ctx.exclude_admin else 'all stock codes'}")
        st.dataframe(styled(p, money_cols=["Revenue", "Revenue per unit"], int_cols=["Units sold", "Invoices"]),
                     hide_index=True, height=460)
        st.download_button("Download products CSV (current search)", csv_bytes(p), f"retailpulse_products_{file_stamp(ctx)}.csv", "text/csv", key="dl_prod")

    with t_country:
        c = ctx.dash.countries.copy()
        c["Average invoice value"] = np.where(c["invoice_count"] > 0, c["total_revenue"] / c["invoice_count"].replace(0, np.nan), np.nan)
        total = c["total_revenue"].sum()
        c["Revenue share"] = c["total_revenue"] / total * 100 if total else np.nan
        c = c.rename(columns={"total_revenue": "Revenue", "invoice_count": "Invoices", "unique_customers": "Unique customers",
                              "transaction_lines": "Transaction lines"})
        c = c[["Country", "Revenue", "Revenue share", "Invoices", "Average invoice value", "Unique customers", "Transaction lines"]]
        c = c.sort_values("Revenue", ascending=False)
        q = st.text_input("Search countries", key="tbl_cty_q", placeholder="Type a country name")
        c = search_frame(c, q)
        st.dataframe(styled(c, money_cols=["Revenue", "Average invoice value"], int_cols=["Invoices", "Unique customers", "Transaction lines"],
                            pct_cols=["Revenue share"]), hide_index=True, height=460)
        st.download_button("Download countries CSV (current search)", csv_bytes(c), f"retailpulse_countries_{file_stamp(ctx)}.csv", "text/csv", key="dl_cty")

    with t_cust:
        t = db.query_top_customers(client, *ctx.restrict).rename(columns={
            "CustomerID": "Customer ID", "invoice_count": "Invoices", "units_bought": "Units", "total_revenue": "Revenue"})
        if t.empty:
            st.info("No identified customers in the current selection.")
        else:
            t = t[["Customer ID", "Country", "Revenue", "Invoices", "Units"]]
            st.caption("Top 200 customers by revenue. Customer IDs are pseudonymous identifiers from the source data.")
            q = st.text_input("Search customers", key="tbl_cust_q", placeholder="Customer ID or country")
            t = search_frame(t, q)
            st.dataframe(styled(t, money_cols=["Revenue"], int_cols=["Invoices", "Units"]), hide_index=True, height=460)
            st.download_button("Download customers CSV (current search)", csv_bytes(t), f"retailpulse_customers_{file_stamp(ctx)}.csv", "text/csv", key="dl_cust")


# =========================================================================
# Router
# =========================================================================

VIEWS = {
    "Executive overview": view_overview,
    "Sales trends": view_trends,
    "Product analytics": view_products,
    "Country analytics": view_countries,
    "Customer analytics": view_customers,
    "Data quality & pipeline": view_quality,
    "Detailed tables & exports": view_tables,
}

try:
    VIEWS[st.session_state["nav"]](ctx)
except Exception as exc:  # noqa: BLE001
    show_error(exc, "view")

st.markdown(
    f'<div class="rp-footer">RetailPulse · Big Data Analytics project · Databricks (Delta, Spark SQL) + Streamlit · '
    f'Source: UCI Online Retail dataset · Rendered {datetime.now():%d %b %Y, %H:%M}</div>',
    unsafe_allow_html=True,
)
