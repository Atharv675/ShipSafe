"""Visual components for the Streamlit app (2026-11-25 redesign after the owner's references).

The look: near-black page, slightly lighter cards with hairline borders, small muted labels,
large numbers, a thin coloured accent line on KPI cards, outlined "chips" with a coloured dot,
thin progress rows, donuts with the total in the centre and a legend list with counts, grouped
bars, and "stage" cards with chips and three small metric boxes.

Everything is drawn with native Streamlit elements (so tests and the dashboard check read the
same structure as before) plus small HTML fragments and Altair charts. Colours come only from
dashboard/theme.py: categorical slots for identity, status colours for state (always with an
icon or a label), never the other way round. Text never takes a series colour.
"""

from __future__ import annotations

import html
import zlib
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, cast

import altair as alt
import pandas as pd
import streamlit as st

from sentinel.dashboard import theme as th

ACCENTS = {  # KPI accent line per card (identity slots only)
    "blue": th.SERIES[0],
    "orange": th.SERIES[1],
    "aqua": th.SERIES[2],
    "yellow": th.SERIES[3],
    "magenta": th.SERIES[4],
    "green": th.SERIES[5],
    "violet": th.SERIES[6],
    "red": th.SERIES[7],
}

CSS = f"""
<style>
:root {{
  --sg-page: {th.PAGE}; --sg-card: {th.SURFACE}; --sg-border: {th.BORDER};
  --sg-ink: {th.INK}; --sg-ink2: {th.INK_2}; --sg-muted: {th.MUTED}; --sg-track: #1d1d21;
}}
.stApp, [data-testid="stAppViewContainer"] {{ background: var(--sg-page); }}
[data-testid="stSidebar"] {{ background: #0c0c0e; border-right: 1px solid var(--sg-border); }}
[data-testid="stSidebarNav"] [data-testid="stNavSectionHeader"],
[data-testid="stSidebarNav"] header {{
  text-transform: uppercase; letter-spacing: .08em; font-size: .68rem; color: var(--sg-muted);
}}
[data-testid="stSidebarNav"] a {{ border-radius: 6px; }}
h1 {{ font-size: 1.65rem !important; font-weight: 700 !important; letter-spacing: -.01em; }}
h3 {{ font-size: 1.02rem !important; font-weight: 650 !important; }}
[class*="st-key-kpi-"] [data-testid="stCaptionContainer"] {{ margin-top: -6px; }}
[data-testid="stVerticalBlockBorderWrapper"] {{
  background: var(--sg-card); border-color: var(--sg-border) !important; border-radius: 10px;
}}
[data-testid="stMetric"], [class*="st-key-kpi-"] [data-testid="stMetric"] > div {{
  background: transparent !important;
}}
[data-testid="stMetricLabel"] p {{ color: var(--sg-muted) !important; font-size: .78rem; }}
[data-testid="stMetricValue"] {{ font-weight: 650; }}
{
    "".join(
        f'[class*="st-key-kpi-{name}-"] {{ box-shadow: inset 0 2px 0 0 {c}; }}'
        for name, c in ACCENTS.items()
    )
}
.sg-card-title {{ font-weight: 650; font-size: .95rem; color: var(--sg-ink); margin: 0; }}
.sg-card-sub {{ color: var(--sg-muted); font-size: .76rem; margin: 2px 0 10px 0; }}
.sg-rule {{ border-top: 1px solid var(--sg-border); margin: 0 -1rem 12px -1rem; }}
.sg-chips {{ display: flex; flex-wrap: wrap; gap: 6px; margin: 4px 0 8px 0; }}
.sg-chip {{
  display: inline-flex; align-items: center; gap: 6px; padding: 3px 10px;
  border: 1px solid var(--sg-border); border-radius: 999px; font-size: .74rem;
  color: var(--sg-ink2); background: #161619; white-space: nowrap;
}}
.sg-chip b {{ color: var(--sg-ink); font-weight: 600; }}
.sg-dot {{ width: 7px; height: 7px; border-radius: 50%; display: inline-block; flex: none; }}
.sg-prog {{ margin: 2px 0 12px 0; }}
.sg-prog-head {{
  display: flex; justify-content: space-between; font-size: .8rem; color: var(--sg-ink2);
  margin-bottom: 5px;
}}
.sg-prog-head span:last-child {{ color: var(--sg-ink); font-variant-numeric: tabular-nums; }}
.sg-track {{ height: 6px; border-radius: 999px; background: var(--sg-track); overflow: hidden; }}
.sg-fill {{ height: 100%; border-radius: 999px; }}
.sg-legend {{ display: flex; flex-direction: column; gap: 9px; margin-top: 18px; }}
.sg-legend-row {{
  display: flex; align-items: center; gap: 10px; font-size: .82rem; color: var(--sg-ink2);
}}
.sg-legend-row .sg-sq {{ width: 9px; height: 9px; border-radius: 2px; flex: none; }}
.sg-legend-row span.sg-n {{
  margin-left: auto; color: var(--sg-ink); font-variant-numeric: tabular-nums;
}}
.sg-stage {{
  background: #141417; border: 1px solid var(--sg-border); border-radius: 10px;
  padding: 14px 14px 12px 14px; height: 100%; box-sizing: border-box;
  display: flex; flex-direction: column;
}}
.sg-stage.sg-active {{ border-color: #2f4f7a; box-shadow: inset 0 0 0 1px #1d3352; }}
.sg-stage-head {{ display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }}
.sg-stage-head .sg-name {{ font-weight: 650; color: var(--sg-ink); font-size: .92rem; }}
.sg-tag {{
  margin-left: auto; font-size: .68rem; color: var(--sg-ink2); border: 1px solid var(--sg-border);
  border-radius: 6px; padding: 1px 7px; background: #18181b;
}}
.sg-desc {{ color: var(--sg-muted); font-size: .76rem; line-height: 1.35; margin-bottom: 8px; }}
.sg-sect {{ color: var(--sg-muted); font-size: .68rem; margin: 6px 0 2px 0; }}
.sg-boxes {{ display: flex; gap: 6px; margin-top: auto; padding-top: 10px; }}
.sg-grid {{
  display: grid; grid-template-columns: repeat(12, minmax(0, 1fr)); gap: 12px;
  align-items: stretch; margin: 4px 0 6px 0;
}}
@media (max-width: 900px) {{ .sg-grid > div {{ grid-column: span 12 !important; }} }}
.sg-box {{
  flex: 1; border: 1px solid var(--sg-border); border-radius: 8px; padding: 6px 8px;
  background: #111114; min-width: 0;
}}
.sg-box .sg-bl {{ color: var(--sg-muted); font-size: .64rem; }}
.sg-box .sg-bv {{ color: var(--sg-ink); font-weight: 650; font-size: .92rem; white-space: nowrap; }}
.sg-code {{
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: .68rem;
  color: var(--sg-muted); margin-top: 8px; overflow: hidden; text-overflow: ellipsis;
  white-space: nowrap;
}}
.sg-live {{
  display: flex; align-items: center; gap: 8px; font-size: .78rem; color: var(--sg-ink2);
}}
</style>
"""


def inject_css() -> None:
    """Once per page run (Streamlit re-renders the whole script)."""
    st.markdown(CSS, unsafe_allow_html=True)


def _e(s: Any) -> str:
    return html.escape(str(s))


# ---------------------------------------------------------------------------- cards


@contextmanager
def card(title: str, subtitle: str = "", key: str | None = None) -> Iterator[None]:
    """Bordered section card: bold title, muted subtitle, hairline rule (the reference look).
    The title is a native subheader-sized markdown line, so page text stays searchable."""
    with st.container(border=True, key=key):
        st.markdown(
            f'<p class="sg-card-title">{_e(title)}</p>'
            + (f'<p class="sg-card-sub">{_e(subtitle)}</p>' if subtitle else "")
            + '<div class="sg-rule"></div>',
            unsafe_allow_html=True,
        )
        yield


def kpi(
    col: Any,
    label: str,
    value: str,
    caption: str,
    *,
    icon: str = "",
    accent: str = "blue",
    trend: Sequence[float] | None = None,
    delta: str | None = None,
    higher_is_bad: bool = False,
    chart: str = "area",
    help: str | None = None,
) -> None:
    """KPI card: icon + muted label, big value, caption, thin accent line, optional sparkline.
    The accent line comes from the keyed container's class (CSS rule per accent)."""
    key = f"kpi-{accent}-{zlib.crc32(label.encode('utf-8'))}"
    with col, st.container(key=key, border=True):
        st.metric(
            f"{icon} {label}".strip(),
            value,
            delta=delta,
            delta_color="inverse" if higher_is_bad else "normal",
            help=help or caption,
            border=False,
            chart_data=list(trend) if trend else None,
            chart_type=chart,  # type: ignore[arg-type]
        )
        if caption:
            st.caption(caption)


# ---------------------------------------------------------------------------- chips, rows


@dataclass(frozen=True)
class Chip:
    label: str
    color: str | None = None  # dot colour (identity or status); None = no dot
    count: Any = None


def chips_html(items: Sequence[Chip]) -> str:
    parts = []
    for c in items:
        dot = f'<span class="sg-dot" style="background:{c.color}"></span>' if c.color else ""
        n = f" <b>{_e(c.count)}</b>" if c.count is not None else ""
        parts.append(f'<span class="sg-chip">{dot}{_e(c.label)}{n}</span>')
    return f'<div class="sg-chips">{"".join(parts)}</div>'


def chips(items: Sequence[Chip]) -> None:
    st.markdown(chips_html(items), unsafe_allow_html=True)


@dataclass(frozen=True)
class Progress:
    label: str
    share: float  # 0..1, the bar length
    color: str
    right: str = ""  # text on the right (defaults to the share as a percentage)


def progress_rows(rows: Sequence[Progress]) -> None:
    """Thin bars on a dark track, label left, value right (the 'outcome by class' look)."""
    out = []
    for r in rows:
        w = max(0.0, min(1.0, float(r.share))) * 100
        out.append(
            f'<div class="sg-prog"><div class="sg-prog-head"><span>{_e(r.label)}</span>'
            f"<span>{_e(r.right or f'{w:.0f}%')}</span></div>"
            f'<div class="sg-track"><div class="sg-fill" style="width:{w:.2f}%;'
            f'background:{r.color}"></div></div></div>'
        )
    st.markdown("".join(out), unsafe_allow_html=True)


# ---------------------------------------------------------------------------- donut


def donut(
    df: pd.DataFrame,
    label: str,
    value: str,
    colors: dict[str, str],
    center_value: str,
    center_caption: str,
    *,
    height: int = 220,
    key: str = "donut",
) -> None:
    """Donut with the total in the centre and a legend list with counts (two columns).
    The legend is the secondary encoding: every slice is named with its count."""
    if df.empty or df[value].sum() == 0:
        st.info("Nothing to show yet.", icon=":material/hourglass_empty:")
        return
    d = df[[label, value]].copy()
    d = d[d[value] > 0]
    dom = [k for k in colors if k in set(d[label])] + sorted(set(d[label]) - set(colors))
    rng = [colors.get(k, th.MUTED) for k in dom]
    base = alt.Chart(d).encode(
        theta=alt.Theta(f"{value}:Q", stack=True),
        color=alt.Color(f"{label}:N", scale=alt.Scale(domain=dom, range=rng), legend=None),
        order=alt.Order(f"{value}:Q", sort="descending"),
        tooltip=[alt.Tooltip(f"{label}:N"), alt.Tooltip(f"{value}:Q", format=",")],
    )
    ring = base.mark_arc(innerRadius=64, outerRadius=92, stroke=th.SURFACE, strokeWidth=2)
    centre = pd.DataFrame({"t": [center_value], "c": [center_caption]})
    big = (
        alt.Chart(centre)
        .mark_text(fontSize=24, fontWeight=600, color=th.INK, dy=-6)
        .encode(text="t:N")
    )
    small = alt.Chart(centre).mark_text(fontSize=11, color=th.MUTED, dy=14).encode(text="c:N")
    c1, c2 = st.columns([1, 1], vertical_alignment="center")
    with c1:
        st.altair_chart(
            alt.layer(ring, big, small).properties(height=height), theme=None, width="stretch"
        )
    total = float(d[value].sum())
    rows = "".join(
        f'<div class="sg-legend-row"><span class="sg-sq" style="background:'
        f'{colors.get(str(r[label]), th.MUTED)}"></span>{_e(r[label])}'
        f'<span class="sg-n">{int(r[value]):,} '
        f'<span style="color:var(--sg-muted)">({100 * r[value] / total:.0f}%)</span></span></div>'
        for _, r in d.sort_values(value, ascending=False).iterrows()
    )
    c2.markdown(f'<div class="sg-legend">{rows}</div>', unsafe_allow_html=True)


# ---------------------------------------------------------------------------- stage cards


@dataclass(frozen=True)
class Stage:
    name: str
    dot: str
    tag: str
    description: str
    tags: list[Chip] = field(default_factory=list)
    section: str = ""
    section_chips: list[Chip] = field(default_factory=list)
    boxes: list[tuple[str, str]] = field(default_factory=list)  # (label, value), up to 3
    code: str = ""
    active: bool = False


def stage_html(s: Stage) -> str:
    boxes = "".join(
        f'<div class="sg-box"><div class="sg-bl">{_e(k)}</div><div class="sg-bv">{_e(v)}</div>'
        "</div>"
        for k, v in s.boxes[:3]
    )
    return (
        f'<div class="sg-stage{" sg-active" if s.active else ""}">'
        f'<div class="sg-stage-head"><span class="sg-dot" style="background:{s.dot}"></span>'
        f'<span class="sg-name">{_e(s.name)}</span><span class="sg-tag">{_e(s.tag)}</span></div>'
        f'<div class="sg-desc">{_e(s.description)}</div>'
        f"{chips_html(s.tags) if s.tags else ''}"
        + (f'<div class="sg-sect">{_e(s.section)}</div>' if s.section else "")
        + (chips_html(s.section_chips) if s.section_chips else "")
        + (f'<div class="sg-boxes">{boxes}</div>' if boxes else "")
        + (f'<div class="sg-code">{_e(s.code)}</div>' if s.code else "")
        + "</div>"
    )


def grid_spans(n: int, per_row: int) -> list[int]:
    """Column spans on a 12-column grid: full rows of `per_row`, and a shorter last row
    stretched to the full width (5 cards at 3 per row -> 4,4,4 then 6,6), so no gap."""
    per_row = max(1, min(per_row, 12))
    spans: list[int] = []
    for i in range(0, n, per_row):
        k = min(per_row, n - i)
        spans += [12 // k] * k
    return spans


def stage_grid(stages: Sequence[Stage], per_row: int = 4) -> None:
    """One CSS grid (not Streamlit columns): cards in a row share one height, their metric
    boxes line up at the bottom, and the last row fills the width."""
    cells = "".join(
        f'<div style="grid-column: span {span}">{stage_html(s)}</div>'
        for s, span in zip(stages, grid_spans(len(stages), per_row), strict=True)
    )
    st.markdown(f'<div class="sg-grid">{cells}</div>', unsafe_allow_html=True)


# ---------------------------------------------------------------------------- bars


def grouped_bars(
    df: pd.DataFrame,
    x: str,
    group: str,
    y: str,
    colors: dict[str, str],
    *,
    y_title: str,
    y_format: str = ".1%",
    height: int = 260,
    x_title: str | None = None,
) -> alt.Chart:
    """Grouped vertical bars (the 'every candidate across every stage' comparison), rounded
    tops, a 2px surface gap between neighbours, legend at the bottom, tooltip on every bar."""
    dom = [k for k in colors if k in set(df[group])]
    chart = (
        alt.Chart(df)
        .mark_bar(cornerRadiusTopLeft=3, cornerRadiusTopRight=3, stroke=th.SURFACE, strokeWidth=1)
        .encode(
            x=alt.X(f"{x}:N", title=x_title, axis=alt.Axis(labelAngle=0, labelLimit=140)),
            xOffset=alt.XOffset(f"{group}:N", sort=dom),
            y=alt.Y(f"{y}:Q", title=y_title, axis=alt.Axis(format=y_format)),
            color=alt.Color(
                f"{group}:N",
                scale=alt.Scale(domain=dom, range=[colors[k] for k in dom]),
                legend=alt.Legend(orient="bottom", title=None),
            ),
            tooltip=[
                alt.Tooltip(f"{x}:N"),
                alt.Tooltip(f"{group}:N"),
                alt.Tooltip(f"{y}:Q", format=y_format),
            ],
        )
        .properties(height=height)
    )
    return cast(alt.Chart, chart)


def live_indicator(text: str, ok: bool = True) -> None:
    color = th.STATUS["good"] if ok else th.STATUS["warning"]
    st.markdown(
        f'<div class="sg-live"><span class="sg-dot" style="background:{color}"></span>'
        f"{_e(text)}</div>",
        unsafe_allow_html=True,
    )
