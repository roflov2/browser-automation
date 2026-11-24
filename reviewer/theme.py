"""Material Design styling in the NASC / Scamwatch palette.

Colours, fonts and radii come from ``.streamlit/config.toml``; this adds what the theme cannot express:
an app bar, elevated cards, contained uppercase buttons, Material tabs and chip-shaped badges.
"""

from __future__ import annotations

import html

import streamlit as st

ROYAL_BLUE = "#0038E6"
NAVY = "#0A1C40"
ALERT_ORANGE = "#FF5E00"
EMERALD = "#00C853"
CYAN = "#00B4D8"
WHITE = "#FFFFFF"
SURFACE = "#F4F6F8"
CHARCOAL = "#1A1A1A"

# Material elevation 1 and 3 (the shadows MUI's Paper uses).
_ELEVATION_1 = "0 2px 1px -1px rgba(10,28,64,.20), 0 1px 1px 0 rgba(10,28,64,.14), 0 1px 3px 0 rgba(10,28,64,.12)"
_ELEVATION_3 = "0 3px 3px -2px rgba(10,28,64,.20), 0 3px 4px 0 rgba(10,28,64,.14), 0 1px 8px 0 rgba(10,28,64,.12)"

_CSS = f"""
<style>
:root {{
  --nasc-color-royal-blue: {ROYAL_BLUE};
  --nasc-color-authority-navy: {NAVY};
  --nasc-color-alert-orange: {ALERT_ORANGE};
  --nasc-color-emerald-green: {EMERALD};
  --nasc-color-progress-cyan: {CYAN};
  --nasc-color-bg-white: {WHITE};
  --nasc-color-bg-grey: {SURFACE};
  --nasc-color-text-charcoal: {CHARCOAL};
}}

/* Streamlit's own header is replaced by the app bar below. */
header[data-testid="stHeader"] {{ background: transparent; }}
.block-container {{ padding-top: 1.5rem; }}

/* App bar */
.nasc-appbar {{
  display: flex; align-items: center; gap: 14px;
  background: var(--nasc-color-authority-navy); color: #fff;
  padding: 14px 24px; margin: 0 0 24px; border-radius: 4px;
  border-bottom: 4px solid var(--nasc-color-royal-blue);
  box-shadow: {_ELEVATION_3};
}}
.nasc-appbar .nasc-mark {{
  width: 36px; height: 36px; border-radius: 50%; flex: none;
  background: var(--nasc-color-emerald-green); color: var(--nasc-color-authority-navy);
  display: grid; place-items: center; font-weight: 800; font-size: 15px;
}}
.nasc-appbar .nasc-title {{ font-size: 1.15rem; font-weight: 700; letter-spacing: .01em; line-height: 1.2; }}
.nasc-appbar .nasc-subtitle {{ font-size: .85rem; color: #C7D0E0; }}
.nasc-appbar .nasc-page {{
  margin-left: auto; font-size: .78rem; font-weight: 600; text-transform: uppercase; letter-spacing: .08em;
  color: var(--nasc-color-progress-cyan);
}}

/* Headings */
h1, h2, h3 {{ color: var(--nasc-color-authority-navy) !important; }}
h1 {{ letter-spacing: -.01em; }}

/* Paper: bordered containers, metrics, dataframes and expanders become elevated white cards. */
/* Bordered containers (the timeline's step cards) have no test id of their own; inside a tab they are
   the vertical blocks directly under a layout wrapper. */
div[data-testid="stTabPanel"] div[data-testid="stLayoutWrapper"] > div[data-testid="stVerticalBlock"],
div[data-testid="stForm"] {{
  box-shadow: {_ELEVATION_1}; border-color: transparent !important; background: var(--nasc-color-bg-white);
}}
section[data-testid="stSidebar"] div[data-testid="stForm"] {{ background: #13254D; box-shadow: none; border-color: #3A4E7A !important; }}
div[data-testid="stMetric"] {{
  background: var(--nasc-color-bg-white); border-radius: 4px; padding: 14px 18px;
  box-shadow: {_ELEVATION_1}; border-top: 3px solid var(--nasc-color-royal-blue);
}}
div[data-testid="stMetricLabel"] p {{
  font-size: .75rem; font-weight: 600; text-transform: uppercase; letter-spacing: .06em; color: #5C6470;
}}
div[data-testid="stDataFrame"], div[data-testid="stExpander"] details {{
  box-shadow: {_ELEVATION_1}; border-radius: 4px;
}}
div[data-testid="stExpander"] details {{ border-color: transparent; }}

/* Buttons: contained (primary, navy) and outlined (secondary), uppercase like Material. */
button[data-testid^="stBaseButton"] p {{ text-transform: uppercase; letter-spacing: .05em; font-weight: 600; font-size: .85rem; }}
button[data-testid="stBaseButton-tertiary"] p {{ color: var(--nasc-color-royal-blue); }}
button[data-testid="stBaseButton-primary"],
button[data-testid="stBaseButton-primaryFormSubmit"] {{
  background: var(--nasc-color-authority-navy); border-color: var(--nasc-color-authority-navy); box-shadow: {_ELEVATION_1};
}}
button[data-testid="stBaseButton-primary"]:hover,
button[data-testid="stBaseButton-primaryFormSubmit"]:hover {{
  background: var(--nasc-color-royal-blue); border-color: var(--nasc-color-royal-blue); box-shadow: {_ELEVATION_3};
}}
button[data-testid="stBaseButton-secondary"] {{ border-color: var(--nasc-color-royal-blue); color: var(--nasc-color-royal-blue); }}
section[data-testid="stSidebar"] button[data-testid="stBaseButton-primaryFormSubmit"] {{
  background: var(--nasc-color-progress-cyan); border-color: var(--nasc-color-progress-cyan);
}}
section[data-testid="stSidebar"] button[data-testid="stBaseButton-primaryFormSubmit"] p {{ color: var(--nasc-color-authority-navy); }}
section[data-testid="stSidebar"] button[data-testid="stBaseButton-tertiary"] p {{ color: var(--nasc-color-progress-cyan); }}
div[data-testid="stDownloadButton"] button {{ border-color: var(--nasc-color-royal-blue); }}

/* Tabs: Material's uppercase labels with a 3px indicator. */
div[data-testid="stTabs"] [role="tablist"] {{ border-bottom: 1px solid #DDE2E8; gap: 8px; }}
div[data-testid="stTab"] p {{ text-transform: uppercase; letter-spacing: .06em; font-weight: 600; font-size: .82rem; }}
div[data-testid="stTab"][aria-selected="true"] p {{ color: var(--nasc-color-royal-blue); }}
div[data-testid="stTab"] .react-aria-SelectionIndicator {{ height: 3px; background: var(--nasc-color-royal-blue); }}

/* Chips: badges as rounded Material chips. */
span[data-testid="stBadge"], div[data-testid="stMarkdownContainer"] span[class*="badge"] {{
  border-radius: 16px !important; padding: 4px 12px !important; font-weight: 600;
}}

/* Sidebar navigation: cyan marks the active page, as a progress indicator would. */
section[data-testid="stSidebar"] a[data-testid="stSidebarNavLink"] {{ border-radius: 4px; }}
section[data-testid="stSidebar"] a[data-testid="stSidebarNavLink"][aria-current="page"] {{
  background: rgba(0,180,216,.16); box-shadow: inset 3px 0 0 var(--nasc-color-progress-cyan);
}}
section[data-testid="stSidebar"] a[data-testid="stSidebarNavLink"] span {{ color: #fff; }}
section[data-testid="stSidebar"] h2, section[data-testid="stSidebar"] h3 {{ color: #fff !important; }}

/* Dialog (enlarged screenshot) */
div[role="dialog"] {{ box-shadow: 0 11px 15px -7px rgba(10,28,64,.2), 0 24px 38px 3px rgba(10,28,64,.14); }}
</style>
"""


def apply() -> None:
    st.html(_CSS)


def app_bar(page_title: str) -> None:
    st.html(
        '<div class="nasc-appbar">'
        '<div class="nasc-mark" aria-hidden="true">ER</div>'
        '<div><div class="nasc-title">Evidence Reviewer</div>'
        '<div class="nasc-subtitle">Drip-pricing audit evidence and analyst verdicts</div></div>'
        f'<div class="nasc-page">{html.escape(page_title)}</div>'
        "</div>"
    )
