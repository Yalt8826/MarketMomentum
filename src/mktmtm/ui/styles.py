"""Shared CSS + small HTML builders for the MktMtm control panel.

Streamlit-free by design: every helper here returns plain strings so the
styling layer is trivially testable and reusable across pages.
"""

# ── palette (dark trading-terminal theme) ────────────────────────────────
ACCENT = "#00D4AA"      # teal — primary accent
ACCENT_DIM = "#00A888"
GREEN = "#3FB950"       # profit / success
RED = "#F85149"         # loss / failure
AMBER = "#D29922"       # running / warning
MUTED = "#8B949E"
TEXT = "#E6EDF3"
BG = "#0E1117"
CARD_BG = "#131A23"
BORDER = "#21262D"

FONT_STACK = "'Inter', -apple-system, 'Segoe UI', Roboto, sans-serif"
MONO_STACK = "'JetBrains Mono', ui-monospace, 'SF Mono', Menlo, monospace"

# ── global stylesheet ────────────────────────────────────────────────────
CSS = f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;600&display=swap');

html, body, [class*="css"] {{
    font-family: {FONT_STACK};
}}

#MainMenu, footer {{ visibility: hidden; }}

/* ── sidebar ── */
[data-testid="stSidebar"] {{
    background: linear-gradient(180deg, #0B0F14 0%, {BG} 100%);
    border-right: 1px solid {BORDER};
}}
[data-testid="stSidebar"] .block-container {{ padding-top: 1.4rem; }}
[data-testid="stSidebar"] [data-testid="stRadio"] label {{
    padding: 0.42rem 0.7rem;
    border-radius: 10px;
    transition: background .15s ease;
}}
[data-testid="stSidebar"] [data-testid="stRadio"] label:hover {{
    background: {CARD_BG};
}}
[data-testid="stSidebar"] [data-testid="stRadio"] label:has(input:checked) {{
    background: rgba(0, 212, 170, 0.10);
    color: {ACCENT};
    font-weight: 600;
}}

/* ── generic blocks ── */
.block-container {{ padding-top: 1.2rem; max-width: 1400px; }}

/* ── hero header ── */
.mkt-hero {{
    padding: 0.4rem 0 1.1rem 0;
    border-bottom: 1px solid {BORDER};
    margin-bottom: 1.1rem;
}}
.mkt-hero .title {{
    font-size: 1.85rem;
    font-weight: 800;
    letter-spacing: -0.02em;
    background: linear-gradient(92deg, {TEXT} 20%, {ACCENT} 90%);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
    margin: 0;
}}
.mkt-hero .subtitle {{ color: {MUTED}; font-size: 0.92rem; margin-top: 0.2rem; }}

/* ── metric cards ── */
.mkt-card {{
    background: linear-gradient(180deg, {CARD_BG} 0%, #10151D 100%);
    border: 1px solid {BORDER};
    border-radius: 14px;
    padding: 0.85rem 1rem;
    position: relative;
    overflow: hidden;
    transition: transform .15s ease, border-color .15s ease;
}}
.mkt-card:hover {{ transform: translateY(-2px); border-color: #2F3A4A; }}
.mkt-card::before {{
    content: "";
    position: absolute;
    top: 0; left: 0; right: 0;
    height: 2px;
    background: linear-gradient(90deg, {ACCENT}, transparent 70%);
    opacity: 0.85;
}}
.mkt-card .label {{
    font-size: 0.72rem;
    text-transform: uppercase;
    letter-spacing: 0.09em;
    color: {MUTED};
    display: flex;
    align-items: center;
    gap: 0.4rem;
}}
.mkt-card .value {{
    font-size: 1.5rem;
    font-weight: 700;
    color: {TEXT};
    margin: 0.28rem 0 0.12rem 0;
    letter-spacing: -0.01em;
    font-variant-numeric: tabular-nums;
}}
.mkt-card .sub {{ font-size: 0.78rem; color: {MUTED}; }}
.mkt-card .value.pos {{ color: {GREEN}; }}
.mkt-card .value.neg {{ color: {RED}; }}

/* ── status pills ── */
.mkt-pill {{
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    padding: 0.14rem 0.6rem;
    border-radius: 999px;
    font-size: 0.72rem;
    font-weight: 600;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    border: 1px solid transparent;
}}
.mkt-pill .dot {{ width: 7px; height: 7px; border-radius: 50%; background: currentColor; }}
.mkt-pill.running  {{ color: {AMBER}; background: rgba(210, 153, 34, 0.10); border-color: rgba(210,153,34,.35); }}
.mkt-pill.running .dot {{ animation: pulse 1.2s ease-in-out infinite; }}
.mkt-pill.done     {{ color: {GREEN}; background: rgba(63, 185, 80, 0.10);  border-color: rgba(63,185,80,.35); }}
.mkt-pill.failed   {{ color: {RED};   background: rgba(248, 81, 73, 0.10);  border-color: rgba(248,81,73,.35); }}
.mkt-pill.killed   {{ color: {MUTED}; background: rgba(139, 148, 158, 0.12); border-color: rgba(139,148,158,.35); }}
@keyframes pulse {{ 0%,100% {{ opacity: 1; }} 50% {{ opacity: 0.25; }} }}

/* ── section header ── */
.mkt-section {{
    display: flex;
    align-items: center;
    gap: 0.6rem;
    margin: 1.5rem 0 0.7rem 0;
}}
.mkt-section::before {{
    content: "";
    width: 4px; height: 18px;
    border-radius: 2px;
    background: {ACCENT};
}}
.mkt-section h3 {{
    font-size: 1.05rem;
    font-weight: 700;
    margin: 0;
    color: {TEXT};
}}

/* ── code / log blocks ── */
[data-testid="stCodeBlock"] pre {{
    background: #0B0F14 !important;
    border: 1px solid {BORDER};
    border-radius: 10px;
    font-family: {MONO_STACK};
    font-size: 0.8rem;
}}

/* ── buttons ── */
.stButton > button, .stDownloadButton > button {{
    border-radius: 10px;
    font-weight: 600;
    border: 1px solid {BORDER};
    transition: all .15s ease;
}}
.stButton > button:hover {{ border-color: {ACCENT}; color: {ACCENT}; }}
.stButton > button[kind="primary"] {{
    background: linear-gradient(180deg, {ACCENT} 0%, {ACCENT_DIM} 100%);
    color: #04131A;
    border: none;
}}
.stButton > button[kind="primary"]:hover {{
    filter: brightness(1.08);
    color: #04131A;
}}

/* ── inputs ── */
[data-testid="stTextInput"] input,
[data-testid="stNumberInput"] input,
[data-testid="stTextArea"] textarea,
[data-testid="stDateInput"] input {{
    border-radius: 10px;
    background: {CARD_BG};
    border-color: {BORDER};
}}
[data-testid="stTextArea"] textarea {{ font-family: {MONO_STACK}; font-size: 0.82rem; }}

/* ── tables ── */
[data-testid="stDataFrame"] {{
    border: 1px solid {BORDER};
    border-radius: 12px;
    overflow: hidden;
}}

/* ── expanders ── */
[data-testid="stExpander"] {{
    border: 1px solid {BORDER};
    border-radius: 12px;
    background: rgba(19, 26, 35, 0.55);
}}
[data-testid="stExpander"] summary {{
    font-weight: 600;
}}
</style>
"""


def pill(status: str) -> str:
    """HTML for a coloured status pill (running/done/failed/killed)."""
    key = status if status in {"running", "done", "failed", "killed"} else "muted"
    label = {"running": "● running", "done": "done", "failed": "failed",
             "killed": "killed"}.get(status, status)
    return (
        f'<span class="mkt-pill {key}"><span class="dot"></span>{label}</span>'
    )


def metric_card(label: str, value: str, sub: str = "", icon: str = "",
                tone: str = "neutral") -> str:
    """HTML for a metric card. ``tone``: neutral | pos | neg."""
    cls = "pos" if tone == "pos" else "neg" if tone == "neg" else ""
    icon_html = f"<span>{icon}</span>" if icon else ""
    sub_html = f'<div class="sub">{sub}</div>' if sub else ""
    return (
        f'<div class="mkt-card"><div class="label">{icon_html}{label}</div>'
        f'<div class="value {cls}">{value}</div>{sub_html}</div>'
    )


def hero(title: str, subtitle: str) -> str:
    """HTML for the page hero header."""
    return (
        f'<div class="mkt-hero"><div class="title">{title}</div>'
        f'<div class="subtitle">{subtitle}</div></div>'
    )


def section(title: str) -> str:
    """HTML for a styled section header."""
    return f'<div class="mkt-section"><h3>{title}</h3></div>'
