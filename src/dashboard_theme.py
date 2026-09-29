"""Theme constants for the dashboard: colors, fonts, injected CSS."""

import streamlit as st

# ─── Palette: dark trading terminal ──────────────────────────────────────────

BG = "#0b0e14"
PANEL = "#11151d"
PANEL_BORDER = "#1c2230"
TEXT = "#d7dce6"
MUTED = "#8b93a7"
ACCENT = "#3b82f6"
GREEN = "#22c55e"
RED = "#ef4444"
AMBER = "#f59e0b"

# Positive/negative numbers, chart series
POS = GREEN
NEG = RED
CHART_COLORS = ["#3b82f6", "#22c55e", "#f59e0b", "#ef4444", "#a855f7", "#14b8a6"]


def inject() -> None:
    """Emit the global CSS block. Call once right after set_page_config."""
    st.markdown(
        f"""
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap');

        :root {{
            --bg: {BG}; --panel: {PANEL}; --border: {PANEL_BORDER};
            --text: {TEXT}; --muted: {MUTED}; --accent: {ACCENT};
            --green: {GREEN}; --red: {RED}; --amber: {AMBER};
        }}

        /* Streamlit chrome */
        #MainMenu, footer, header {{
            visibility: hidden;
        }}
        .stApp {{
            background: var(--bg);
            color: var(--text);
            font-family: 'Inter', -apple-system, sans-serif;
        }}
        [data-testid="stSidebar"] {{
            background: var(--panel);
            border-right: 1px solid var(--border);
        }}

        /* Typography */
        h1 {{ font-size: 1.35rem; font-weight: 700; letter-spacing: -0.01em; }}
        h2 {{ font-size: 1.05rem; font-weight: 600; }}
        h3 {{ font-size: 0.92rem; font-weight: 600; color: var(--text); }}
        p, span, label, li {{ font-size: 0.86rem; }}

        /* Metric cards */
        [data-testid="stMetric"] {{
            background: var(--panel);
            border: 1px solid var(--border);
            border-radius: 10px;
            padding: 12px 16px;
        }}
        [data-testid="stMetricLabel"] p {{ color: var(--muted); font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.06em; }}
        [data-testid="stMetricValue"] {{ font-family: 'JetBrains Mono', monospace; font-size: 1.3rem; font-weight: 600; }}

        /* Buttons */
        .stButton > button {{
            width: 100%;
            border-radius: 8px;
            border: 1px solid var(--border);
            background: #161b26;
            color: var(--text);
            font-weight: 600;
            font-size: 0.85rem;
            padding: 0.45rem 0.9rem;
            transition: border-color .15s, background .15s;
        }}
        .stButton > button:hover {{
            border-color: var(--accent);
            background: #1a2130;
            color: #fff;
        }}
        .stButton > button[kind="primary"] {{
            background: var(--accent);
            border-color: var(--accent);
            color: #fff;
        }}
        .stButton > button[kind="primary"]:hover {{ filter: brightness(1.12); }}

        /* Kill switch: red outline that fills on hover */
        button.kill-red {{
            background: transparent !important;
            border: 1.5px solid var(--red) !important;
            color: var(--red) !important;
        }}
        button.kill-red:hover {{
            background: var(--red) !important;
            color: #fff !important;
        }}

        /* Inputs / expanders / code */
        .stTextInput > div > div > input {{
            background: #0e1219; color: var(--text);
            border: 1px solid var(--border); border-radius: 8px;
            font-family: 'JetBrains Mono', monospace;
        }}
        .stTextInput > div > div > input:focus {{ border-color: var(--accent); }}
        [data-testid="stExpander"] {{
            background: var(--panel);
            border: 1px solid var(--border) !important;
            border-radius: 10px;
        }}
        [data-testid="stExpander"] details {{ border: none !important; }}
        pre, code, [data-testid="stCodeBlock"] {{
            font-family: 'JetBrains Mono', monospace !important;
            font-size: 0.78rem !important;
            background: #0d1117 !important;
            border-radius: 8px;
        }}
        [data-testid="stJson"] {{ font-family: 'JetBrains Mono', monospace; font-size: 0.78rem; }}

        /* Tabs */
        .stTabs [data-baseweb="tab-list"] {{ gap: 4px; border-bottom: 1px solid var(--border); }}
        .stTabs [data-baseweb="tab"] {{
            border-radius: 8px 8px 0 0;
            padding: 6px 14px;
            color: var(--muted);
            font-weight: 500;
            font-size: 0.85rem;
        }}
        .stTabs [aria-selected="true"] {{ color: #fff; background: #161b26; }}
        .stTabs [data-baseweb="tab-highlight"] {{ background: var(--accent); height: 2px; }}
        .stTabs [data-baseweb="tab-border"] {{ display: none; }}

        /* Alerts */
        [data-testid="stAlert"] {{
            background: var(--panel); border: 1px solid var(--border);
            border-radius: 10px; border-left-width: 3px;
            font-size: 0.85rem;
        }}
        div[data-testid="stAlert"]:has(.st-eb) {{ border-left-color: var(--green); }}

        /* Dataframe / charts container */
        [data-testid="stDataFrame"], [data-testid="stVegaScatterChart"], [data-testid="stArrowVegaLiteChart"] {{
            border-radius: 10px;
            border: 1px solid var(--border);
            overflow: hidden;
        }}

        /* Scrollbar */
        ::-webkit-scrollbar {{ width: 8px; height: 8px; }}
        ::-webkit-scrollbar-thumb {{ background: #232a38; border-radius: 4px; }}
        ::-webkit-scrollbar-track {{ background: transparent; }}

        /* Status pill */
        .pill {{
            display: inline-flex; align-items: center; gap: 6px;
            padding: 3px 10px; border-radius: 999px;
            font-size: 0.75rem; font-weight: 600;
            font-family: 'JetBrains Mono', monospace;
        }}
        .pill-green {{ background: rgba(34,197,94,.12); color: var(--green); border: 1px solid rgba(34,197,94,.35); }}
        .pill-red   {{ background: rgba(239,68,68,.12);  color: var(--red);  border: 1px solid rgba(239,68,68,.35); }}
        .pill-amber {{ background: rgba(245,158,11,.12); color: var(--amber); border: 1px solid rgba(245,158,11,.35); }}
        .pill-grey  {{ background: rgba(139,147,167,.12); color: var(--muted); border: 1px solid rgba(139,147,167,.3); }}
        .pill-blue  {{ background: rgba(59,130,246,.12); color: var(--accent); border: 1px solid rgba(59,130,246,.35); }}

        /* Clock */
        .clock {{ font-family: 'JetBrains Mono', monospace; color: var(--muted); font-size: .78rem; }}
        </style>
        """,
        unsafe_allow_html=True,
    )


def pill(text: str, tone: str = "grey") -> str:
    """Return a status pill span. tone: green|red|amber|grey|blue."""
    return f'<span class="pill pill-{tone}">{text}</span>'
