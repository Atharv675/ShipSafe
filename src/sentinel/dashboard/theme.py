"""Visual system for the Streamlit app: one validated palette, one Altair theme.

Colours follow the dataviz method (validated with its palette script, dark mode, surface
#1a1a19: lightness band, chroma floor, CVD separation, normal-vision floor and contrast all
pass for the four categorical slots used here):
- categorical slots, fixed order, never cycled: identity (ring, model version, group);
- status colours (good/warning/serious/critical): only for state, always with icon + label;
- one sequential blue ramp for magnitude (heatmaps).
The Streamlit chrome itself is themed in .streamlit/config.toml with the same values.
"""

from __future__ import annotations

from typing import Any, Literal, cast

import altair as alt

# --- surfaces and ink (dark) ---------------------------------------------------------------
PAGE = "#09090b"  # 2026-11-25 redesign: near-black page, darker cards (owner references)
SURFACE = "#121214"  # all 8 slots re-validated on this surface (dataviz validator)
INK = "#f0efec"
INK_2 = "#c3c2b7"
MUTED = "#898781"
GRID = "#1f1f23"
BORDER = "#232327"
AXIS = "#2e2e33"
FONT = 'system-ui, -apple-system, "Segoe UI", sans-serif'

# --- categorical (identity), dark steps, fixed order ----------------------------------------
# blue, orange, aqua, yellow, magenta, green, violet, red (dataviz dark slots 1-8; worst
# adjacent CVD dE 8.4, normal-vision floor 19.3 on #121214: secondary encoding = labels)
SERIES = [
    "#3987e5",
    "#d95926",
    "#199e70",
    "#c98500",
    "#d55181",
    "#0ca30c",
    "#9085e9",
    "#e66767",
]
# stable identity for things that recur on several charts (colour follows the entity)
RING_COLORS = {"ring0": SERIES[0], "ring1": SERIES[1], "ring2": SERIES[2], "none": MUTED}

# --- status (state only; never a series colour) ----------------------------------------------
STATUS = {"good": "#0ca30c", "warning": "#fab219", "serious": "#ec835a", "critical": "#d03b3b"}
STATUS_ICON = {
    "good": ":material/check_circle:",
    "warning": ":material/warning:",
    "serious": ":material/error:",
    "critical": ":material/dangerous:",
}
STATUS_LABEL = {
    "good": "Healthy",
    "warning": "Degraded",
    "serious": "Serious",
    "critical": "Critical",
}
SEVERITY_STATUS = {"page": "critical", "ticket": "warning"}
BadgeColor = Literal["green", "orange", "red"]
BADGE_COLOR: dict[str, BadgeColor] = {
    "good": "green",
    "warning": "orange",
    "serious": "orange",
    "critical": "red",
}
ROLLOUT_STATE_COLORS = {
    "APPROVED": MUTED,
    "RING0": SERIES[0],
    "RING1": SERIES[1],
    "RING2": SERIES[2],
    "COMPLETE": STATUS["good"],
    "ROLLED_BACK": STATUS["critical"],
}

# --- sequential blue ramp (magnitude), light -> dark ------------------------------------------
BLUE_RAMP = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"]

THEME_NAME = "sentinel_dark"


def _theme() -> dict[str, Any]:
    axis = {
        "domainColor": AXIS,
        "gridColor": GRID,
        "gridWidth": 1,
        "gridDash": [],  # solid hairlines, never dashed
        "tickColor": AXIS,
        "labelColor": MUTED,
        "titleColor": INK_2,
        "labelFont": FONT,
        "titleFont": FONT,
        "labelFontSize": 11,
        "titleFontSize": 11,
        "titleFontWeight": "normal",
    }
    return {
        "config": {
            "background": "transparent",  # charts sit on the card surface (redesign)
            "font": FONT,
            "padding": {"left": 8, "right": 12, "top": 12, "bottom": 8},
            "view": {"stroke": None},
            "axis": axis,
            "axisX": {"grid": False},
            "legend": {
                "labelColor": INK_2,
                "titleColor": INK_2,
                "labelFont": FONT,
                "titleFont": FONT,
                "orient": "top",
                "symbolType": "circle",
            },
            "title": {"color": INK, "font": FONT, "fontSize": 13, "anchor": "start"},
            "line": {"strokeWidth": 2, "invalid": "break-paths-show-domains"},
            "point": {"size": 64, "filled": True},
            "bar": {"cornerRadiusEnd": 4},
            "area": {"opacity": 0.85, "invalid": "break-paths-show-domains"},
            "range": {"category": SERIES, "ramp": BLUE_RAMP},
        }
    }


def register() -> None:
    """Register and enable the Altair theme (idempotent)."""
    alt.theme.register(THEME_NAME, enable=True)(cast(Any, _theme))
