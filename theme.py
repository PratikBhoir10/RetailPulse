"""
RetailPulse - visual theme: CSS, Plotly styling, number formats and small HTML components.

Contrast rules used throughout:
  * dark navy text (#0B1F4B / #334155) on white or very light cards
  * white text only on the navy/blue gradient header and sidebar
  * the sidebar widgets get a dark palette from [theme.sidebar] in .streamlit/config.toml,
    so Streamlit itself picks readable text/background colours for them
"""

from __future__ import annotations

import html
import os

import streamlit as st

# db.py loads .env before this module is imported by app.py
CURRENCY = os.getenv("RETAILPULSE_CURRENCY_SYMBOL", "£")
CURRENCY_CODE = os.getenv("RETAILPULSE_CURRENCY_CODE", "GBP")

NAVY = "#0B1F4B"
BLUE = "#2563EB"
TEAL = "#0D9488"
VIOLET = "#7C3AED"
SKY = "#38BDF8"
INK = "#334155"
MUTED = "#64748B"
GRID = "#E6ECF7"
COLORWAY = [BLUE, TEAL, VIOLET, SKY, "#1E3A8A", "#14B8A6", "#A78BFA", "#0EA5E9"]
FONT = "Inter, 'Segoe UI', system-ui, -apple-system, Roboto, Arial, sans-serif"

ACCENTS = {"blue": BLUE, "teal": TEAL, "violet": VIOLET, "navy": NAVY, "sky": SKY}


# --------------------------------------------------------------------------
# Formatting
# --------------------------------------------------------------------------

def esc(value) -> str:
    return html.escape(str(value), quote=True)


def money(value, decimals: int = 2) -> str:
    try:
        return f"{CURRENCY}{float(value):,.{decimals}f}"
    except (TypeError, ValueError):
        return "n/a"


def money_compact(value) -> str:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "n/a"
    a = abs(v)
    if a >= 1e9:
        return f"{CURRENCY}{v / 1e9:,.2f}B"
    if a >= 1e6:
        return f"{CURRENCY}{v / 1e6:,.2f}M"
    if a >= 1e4:
        return f"{CURRENCY}{v / 1e3:,.1f}K"
    return f"{CURRENCY}{v:,.0f}"


def number(value, decimals: int = 0) -> str:
    try:
        return f"{float(value):,.{decimals}f}"
    except (TypeError, ValueError):
        return "n/a"


def number_compact(value) -> str:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "n/a"
    if abs(v) >= 1e6:
        return f"{v / 1e6:,.2f}M"
    if abs(v) >= 1e4:
        return f"{v / 1e3:,.1f}K"
    return f"{v:,.0f}"


def pct(value, decimals: int = 1) -> str:
    try:
        return f"{float(value):,.{decimals}f}%"
    except (TypeError, ValueError):
        return "n/a"


# --------------------------------------------------------------------------
# CSS
# --------------------------------------------------------------------------

_CSS = f"""
<style>
:root {{ color-scheme: light; }}
html, body, .stApp {{ font-family: {FONT}; }}
.stApp {{ background: #F3F6FC; }}
.block-container {{ padding-top: 1.0rem; padding-bottom: 2.5rem; max-width: 1520px; }}
[data-testid="stHeader"] {{ background: transparent; }}

/* ---------- sidebar: gradient on top of the dark [theme.sidebar] palette ---------- */
[data-testid="stSidebar"] > div:first-child {{
  background: linear-gradient(185deg, #0B1F4B 0%, #102F78 55%, #14408F 100%);
}}
[data-testid="stSidebar"] hr {{ border-color: rgba(255,255,255,.16); }}
.rp-side-brand {{ display:flex; align-items:center; gap:10px; padding:2px 0 6px; }}
.rp-side-brand .name {{ color:#FFFFFF !important; font-size:1.35rem; font-weight:800; letter-spacing:-.02em; }}
.rp-side-brand .tag  {{ color:#BAE6FD !important; font-size:.72rem; letter-spacing:.12em; text-transform:uppercase; }}
.rp-side-label {{ color:#93C5FD !important; font-size:.72rem; font-weight:700; letter-spacing:.12em;
                  text-transform:uppercase; margin:.4rem 0 .15rem; }}
.rp-side-note {{ color:#C7D7F5 !important; font-size:.78rem; line-height:1.35; }}

/* ---------- hero ---------- */
.rp-hero {{
  background: linear-gradient(118deg, #0B1F4B 0%, #1740A8 52%, #0D9488 100%);
  border-radius: 18px; padding: 22px 28px; margin-bottom: 14px;
  display:flex; flex-wrap:wrap; align-items:center; justify-content:space-between; gap:16px;
  box-shadow: 0 10px 28px rgba(11,31,75,.22);
}}
.rp-hero-left {{ display:flex; align-items:center; gap:14px; }}
.rp-hero .title {{ color:#FFFFFF !important; font-size:1.9rem; font-weight:800; letter-spacing:-.03em; line-height:1.1; }}
.rp-hero .subtitle {{ color:#D6E6FF !important; font-size:.98rem; margin-top:3px; }}
.rp-hero-right {{ display:flex; flex-wrap:wrap; gap:8px; justify-content:flex-end; }}
.rp-pill {{
  display:inline-flex; align-items:center; gap:7px; padding:6px 12px; border-radius:999px;
  background: rgba(255,255,255,.14); border:1px solid rgba(255,255,255,.28);
  color:#FFFFFF !important; font-size:.8rem; font-weight:600; white-space:nowrap;
}}
.rp-dot {{ width:9px; height:9px; border-radius:50%; display:inline-block; }}
.rp-dot.ok {{ background:#4ADE80; box-shadow:0 0 0 3px rgba(74,222,128,.25); }}
.rp-dot.off {{ background:#FBBF24; box-shadow:0 0 0 3px rgba(251,191,36,.25); }}

/* ---------- section headers ---------- */
.rp-sec {{ margin: 6px 0 10px; }}
.rp-sec .t {{ color:{NAVY} !important; font-size:1.3rem; font-weight:800; letter-spacing:-.02em; }}
.rp-sec .d {{ color:{INK} !important; font-size:.92rem; margin-top:2px; }}

/* ---------- filter chips ---------- */
.rp-chips {{ display:flex; flex-wrap:wrap; gap:8px; margin: 2px 0 14px; align-items:center; }}
.rp-chip {{ background:#FFFFFF; border:1px solid #D5DFF3; color:{NAVY} !important; border-radius:999px;
            padding:5px 12px; font-size:.8rem; font-weight:600; }}
.rp-chip b {{ color:{BLUE} !important; font-weight:700; }}
.rp-src {{ border-radius:6px; padding:3px 8px; font-size:.7rem; font-weight:700; letter-spacing:.06em; text-transform:uppercase; }}
.rp-src.gold   {{ background:#CCFBF1; color:#115E59 !important; }}
.rp-src.silver {{ background:#EDE9FE; color:#5B21B6 !important; }}

/* ---------- KPI cards ---------- */
.rp-kpis {{ display:grid; grid-template-columns: repeat(auto-fit, minmax(205px, 1fr)); gap:14px; margin-bottom:6px; }}
.rp-kpi {{
  background:#FFFFFF; border:1px solid #DCE5F5; border-radius:14px; padding:15px 17px 14px;
  box-shadow:0 2px 8px rgba(11,31,75,.05); position:relative; overflow:hidden;
}}
.rp-kpi::before {{ content:""; position:absolute; left:0; top:0; bottom:0; width:5px; background:var(--accent); }}
.rp-kpi .l {{ color:#475569 !important; font-size:.74rem; font-weight:700; letter-spacing:.08em; text-transform:uppercase; }}
.rp-kpi .v {{ color:{NAVY} !important; font-size:1.75rem; font-weight:800; letter-spacing:-.02em; margin:5px 0 2px; line-height:1.15; }}
.rp-kpi .s {{ color:{MUTED} !important; font-size:.78rem; line-height:1.3; }}

/* ---------- delta pills ---------- */
.rp-delta {{ display:inline-block; border-radius:999px; padding:2px 9px; font-size:.78rem; font-weight:700; }}
.rp-delta.up   {{ background:#D1FAE5; color:#065F46 !important; }}
.rp-delta.down {{ background:#FEE2E2; color:#991B1B !important; }}
.rp-delta.flat {{ background:#E2E8F0; color:#334155 !important; }}

/* ---------- callouts ---------- */
.rp-callout {{ border-radius:12px; padding:11px 15px; font-size:.88rem; line-height:1.45; margin:6px 0 10px;
               border:1px solid; }}
.rp-callout.info {{ background:#EFF6FF; border-color:#BFDBFE; color:#1E3A8A !important; }}
.rp-callout.warn {{ background:#FFFBEB; border-color:#FDE68A; color:#78350F !important; }}
.rp-callout.ok   {{ background:#ECFDF5; border-color:#A7F3D0; color:#065F46 !important; }}
.rp-callout.bad  {{ background:#FEF2F2; border-color:#FECACA; color:#991B1B !important; }}

/* ---------- cards (st.container(key="card_*")) ---------- */
[class*="st-key-card"] {{
  background:#FFFFFF; border:1px solid #DCE5F5; border-radius:14px; padding:14px 16px 8px;
  box-shadow:0 2px 8px rgba(11,31,75,.05);
}}
.rp-card-title {{ color:{NAVY} !important; font-weight:700; font-size:.98rem; }}
.rp-card-sub {{ color:{MUTED} !important; font-size:.8rem; margin-bottom:4px; }}

/* ---------- status badge ---------- */
.rp-badge {{ display:inline-block; border-radius:6px; padding:2px 8px; font-size:.72rem; font-weight:800; letter-spacing:.05em; }}
.rp-badge.pass {{ background:#D1FAE5; color:#065F46 !important; }}
.rp-badge.fail {{ background:#FEE2E2; color:#991B1B !important; }}
.rp-badge.info {{ background:#DBEAFE; color:#1E40AF !important; }}

/* ---------- misc ---------- */
[data-testid="stTabs"] [role="tab"] {{ font-weight:600; }}
.rp-footer {{ color:{MUTED}; font-size:.78rem; padding-top:10px; }}

/* ---------- sidebar contrast fix: readable labels and controls ---------- */
[data-testid="stSidebar"] > div:first-child {{
  background: linear-gradient(185deg, #0B1F4B 0%, #102F78 55%, #14408F 100%) !important;
}}

/* Streamlit sidebar labels, navigation, helper text and markdown */
[data-testid="stSidebar"] label,
[data-testid="stSidebar"] label p,
[data-testid="stSidebar"] [data-testid="stMarkdownContainer"],
[data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p,
[data-testid="stSidebar"] [data-testid="stCaptionContainer"],
[data-testid="stSidebar"] [data-testid="stCaptionContainer"] p,
[data-testid="stSidebar"] [data-testid="stWidgetLabel"],
[data-testid="stSidebar"] [data-testid="stWidgetLabel"] p,
[data-testid="stSidebar"] [data-testid="stRadio"] label,
[data-testid="stSidebar"] [data-testid="stCheckbox"] label {{
  color: #F8FAFC !important;
}}

/* Radio options and checkboxes can have nested text wrappers */
[data-testid="stSidebar"] [role="radiogroup"] *,
[data-testid="stSidebar"] [data-testid="stCheckbox"] * {{
  color: #F8FAFC !important;
}}

/* Keep input fields and dropdowns light with dark, readable text */
[data-testid="stSidebar"] input,
[data-testid="stSidebar"] textarea,
[data-testid="stSidebar"] [data-baseweb="select"] *,
[data-testid="stSidebar"] [data-baseweb="select"] input {{
  color: #172554 !important;
}}

[data-testid="stSidebar"] [data-baseweb="select"] > div {{
  background-color: #FFFFFF !important;
  border-color: #CBD5E1 !important;
}}

[data-testid="stSidebar"] input::placeholder,
[data-testid="stSidebar"] textarea::placeholder {{
  color: #64748B !important;
  opacity: 1 !important;
}}

/* Ensure selected radio/checkbox labels remain readable */
[data-testid="stSidebar"] [data-testid="stRadio"] [aria-checked="true"] *,
[data-testid="stSidebar"] [data-testid="stCheckbox"] [aria-checked="true"] * {{
  color: #FFFFFF !important;
}}


/* Sidebar action buttons: high-contrast text and icons */
[data-testid="stSidebar"] .stButton > button {{
  background: #2563EB !important;
  color: #FFFFFF !important;
  border: 1px solid #60A5FA !important;
  border-radius: 10px !important;
  font-weight: 700 !important;
  opacity: 1 !important;
}}
[data-testid="stSidebar"] .stButton > button p,
[data-testid="stSidebar"] .stButton > button span,
[data-testid="stSidebar"] .stButton > button [data-testid="stMarkdownContainer"],
[data-testid="stSidebar"] .stButton > button [data-testid="stMarkdownContainer"] p {{
  color: #FFFFFF !important;
}}
[data-testid="stSidebar"] .stButton > button svg {{
  color: #FFFFFF !important;
  stroke: #FFFFFF !important;
}}
[data-testid="stSidebar"] .stButton > button:hover {{
  background: #1D4ED8 !important;
  border-color: #BFDBFE !important;
}}
[data-testid="stSidebar"] .stButton > button:focus-visible {{
  outline: 3px solid #93C5FD !important;
  outline-offset: 2px;
}}
[data-testid="stSidebar"] .stButton > button:disabled {{
  background: #334E80 !important;
  background-color: #334E80 !important;
  border: 1px solid #93C5FD !important;
  color: #FFFFFF !important;
  -webkit-text-fill-color: #FFFFFF !important;
  opacity: 1 !important;
  cursor: not-allowed !important;
}}
/* Force readable text/icons for all Streamlit button states, including disabled */
[data-testid="stSidebar"] [data-testid="stButton"] button,
[data-testid="stSidebar"] [data-testid="stButton"] button:disabled,
[data-testid="stSidebar"] [data-testid="stButton"] button[kind="secondary"] {{
  background: #2563EB !important;
  background-color: #2563EB !important;
  color: #FFFFFF !important;
  -webkit-text-fill-color: #FFFFFF !important;
  opacity: 1 !important;
  box-shadow: none !important;
}}
[data-testid="stSidebar"] [data-testid="stButton"] button:disabled {{
  background: #334E80 !important;
  background-color: #334E80 !important;
  border-color: #93C5FD !important;
}}
[data-testid="stSidebar"] [data-testid="stButton"] button *,
[data-testid="stSidebar"] [data-testid="stButton"] button:disabled * {{
  color: #FFFFFF !important;
  -webkit-text-fill-color: #FFFFFF !important;
  fill: #FFFFFF !important;
  opacity: 1 !important;
}}

</style>
"""


def inject_css() -> None:
    st.markdown(_CSS, unsafe_allow_html=True)


# --------------------------------------------------------------------------
# HTML components
# --------------------------------------------------------------------------

_LOGO = (
    '<svg width="46" height="46" viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg" aria-label="RetailPulse logo">'
    '<rect width="48" height="48" rx="13" fill="rgba(255,255,255,0.16)" stroke="rgba(255,255,255,0.5)"/>'
    '<polyline points="6,27 15,27 20,13 27,36 32,22 36,27 42,27" fill="none" stroke="#FFFFFF" '
    'stroke-width="3.2" stroke-linecap="round" stroke-linejoin="round"/></svg>'
)


def hero(connected: bool, period: str | None, last_refresh: str | None) -> str:
    status = ('<span class="rp-pill"><span class="rp-dot ok"></span>Connected to Databricks</span>' if connected
              else '<span class="rp-pill"><span class="rp-dot off"></span>Not connected</span>')
    pills = [status]
    if period:
        pills.append(f'<span class="rp-pill">Period: {esc(period)}</span>')
    pills.append(f'<span class="rp-pill">Currency: {esc(CURRENCY_CODE)} ({esc(CURRENCY)})</span>')
    if last_refresh:
        pills.append(f'<span class="rp-pill">Last data refresh: {esc(last_refresh)}</span>')
    return (
        '<div class="rp-hero"><div class="rp-hero-left">' + _LOGO +
        '<div><div class="title">RetailPulse</div>'
        '<div class="subtitle">Retail Intelligence &amp; Sales Analytics</div></div></div>'
        '<div class="rp-hero-right">' + "".join(pills) + '</div></div>'
    )


def sidebar_brand() -> str:
    return (
        '<div class="rp-side-brand"><svg width="34" height="34" viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg">'
        '<rect width="48" height="48" rx="13" fill="rgba(255,255,255,0.16)" stroke="rgba(255,255,255,0.5)"/>'
        '<polyline points="6,27 15,27 20,13 27,36 32,22 36,27 42,27" fill="none" stroke="#FFFFFF" stroke-width="3.2" '
        'stroke-linecap="round" stroke-linejoin="round"/></svg>'
        '<div><div class="name">RetailPulse</div><div class="tag">Sales intelligence</div></div></div>'
    )


def side_label(text: str) -> str:
    return f'<div class="rp-side-label">{esc(text)}</div>'


def section(title: str, description: str | None = None) -> str:
    d = f'<div class="d">{esc(description)}</div>' if description else ""
    return f'<div class="rp-sec"><div class="t">{esc(title)}</div>{d}</div>'


def kpi_grid(cards: list[dict]) -> str:
    parts = []
    for c in cards:
        accent = ACCENTS.get(c.get("accent", "blue"), BLUE)
        parts.append(
            f'<div class="rp-kpi" style="--accent:{accent}" title="{esc(c.get("help", ""))}">'
            f'<div class="l">{esc(c["label"])}</div><div class="v">{esc(c["value"])}</div>'
            f'<div class="s">{esc(c.get("sub", ""))}</div></div>'
        )
    return '<div class="rp-kpis">' + "".join(parts) + "</div>"


def delta_pill(pct_value) -> str:
    if pct_value is None:
        return '<span class="rp-delta flat">n/a</span>'
    if abs(pct_value) < 0.05:
        return '<span class="rp-delta flat">0.0%</span>'
    if pct_value > 0:
        return f'<span class="rp-delta up">▲ {pct_value:,.1f}%</span>'
    return f'<span class="rp-delta down">▼ {abs(pct_value):,.1f}%</span>'


def mom_grid(result: dict, month_label) -> str:
    cards = []
    for label, m in result["metrics"].items():
        value = money(m["current"]) if m["kind"] == "money" else number(m["current"])
        prev = money(m["previous"]) if m["kind"] == "money" else number(m["previous"])
        cards.append(
            '<div class="rp-kpi" style="--accent:#7C3AED">'
            f'<div class="l">{esc(label)}</div><div class="v" style="font-size:1.35rem">{esc(value)}</div>'
            f'<div class="s">{delta_pill(m["pct"])} &nbsp;vs {esc(prev)} in {esc(month_label(result["previous"]))}</div></div>'
        )
    return '<div class="rp-kpis">' + "".join(cards) + "</div>"


def callout(kind: str, text: str) -> str:
    return f'<div class="rp-callout {kind}">{esc(text)}</div>'


def chips(items: list[tuple[str, str]], sources: dict | None = None) -> str:
    out = [f'<span class="rp-chip">{esc(k)}: <b>{esc(v)}</b></span>' for k, v in items]
    for name, src in (sources or {}).items():
        out.append(f'<span class="rp-chip">{esc(name)} <span class="rp-src {esc(src.lower())}">{esc(src)}</span></span>')
    return '<div class="rp-chips">' + "".join(out) + "</div>"


def badge(status: str) -> str:
    kind = {"PASS": "pass", "FAIL": "fail"}.get(status, "info")
    return f'<span class="rp-badge {kind}">{esc(status)}</span>'


def card_title(title: str, subtitle: str | None = None) -> str:
    s = f'<div class="rp-card-sub">{esc(subtitle)}</div>' if subtitle else ""
    return f'<div class="rp-card-title">{esc(title)}</div>{s}'


def card(key: str):
    """A white card. `key` must be unique on the page."""
    return st.container(key=f"card_{key}")


# --------------------------------------------------------------------------
# Plotly
# --------------------------------------------------------------------------

PLOT_CONFIG = {"displayModeBar": False, "responsive": True}


def style_fig(fig, height: int = 340, legend: bool = False, currency_axis: str | None = None):
    """Apply the shared chart look. currency_axis: 'x' or 'y' to prefix ticks with the currency symbol."""
    fig.update_layout(
        template="plotly_white", height=height, colorway=COLORWAY,
        margin=dict(l=6, r=10, t=14, b=6),
        font=dict(family=FONT, size=12, color=INK),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        hoverlabel=dict(bgcolor=NAVY, font=dict(color="#FFFFFF", family=FONT, size=12), bordercolor=NAVY),
        showlegend=legend,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0, font=dict(color=INK)),
    )
    fig.update_xaxes(showgrid=False, linecolor="#CBD5E1", tickfont=dict(color=INK), title_font=dict(color=INK),
                     automargin=True)
    fig.update_yaxes(gridcolor=GRID, zeroline=False, tickfont=dict(color=INK), title_font=dict(color=INK),
                     automargin=True)
    if currency_axis == "y":
        fig.update_yaxes(tickprefix=CURRENCY, tickformat=",.0f")
    elif currency_axis == "x":
        fig.update_xaxes(tickprefix=CURRENCY, tickformat=",.0f")
    return fig
