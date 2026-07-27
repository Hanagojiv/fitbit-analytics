"""Render the analysis as a single self-contained interactive HTML file.

Visual direction: an instrument readout, not a wellness dashboard. Numbers are
set in a monospaced face at a size that lets you read a column of them
vertically; prose is set in a sans and kept out of the way. The signature
element is the coverage strip at the top, because in a Takeout export the gaps
are as informative as the values, and every other chart should be read in
light of them.
"""

from __future__ import annotations

import html
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import plotly.io as pio

from .analytics import flags as flags_mod
from .analytics import relationships, trends
from .config import Config

INK = "#14181D"
PAPER = "#FCFBF8"
RULE = "#DDD9CF"
MUTED = "#5F6670"
SIGNAL = "#1E7A63"   # steady
WATCH = "#B07514"    # amber
ALERT = "#A32E22"    # flag
SLEEP = "#3B4A8C"    # indigo

TIER_COLOR = {"notice": SIGNAL, "watch": WATCH, "discuss": ALERT}
MONO = "ui-monospace, SFMono-Regular, 'SF Mono', Menlo, Consolas, monospace"
SANS = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Inter, Helvetica, Arial, sans-serif"

LAYOUT = dict(
    paper_bgcolor="rgba(0,0,0,0)",
    plot_bgcolor="rgba(0,0,0,0)",
    font=dict(family=MONO, size=12, color=INK),
    margin=dict(l=52, r=24, t=44, b=40),
    hovermode="x unified",
    xaxis=dict(gridcolor=RULE, zeroline=False, linecolor=RULE),
    yaxis=dict(gridcolor=RULE, zeroline=False, linecolor=RULE),
    legend=dict(orientation="h", y=1.12, x=0, bgcolor="rgba(0,0,0,0)"),
    title=dict(font=dict(size=13, family=MONO), x=0, xanchor="left"),
)


def _fig(title: str) -> go.Figure:
    fig = go.Figure()
    fig.update_layout(**LAYOUT)
    fig.update_layout(title=dict(text=title.upper(), font=dict(size=12, family=MONO)))
    return fig


def _div(fig: go.Figure, height: int = 320) -> str:
    fig.update_layout(height=height)
    return pio.to_html(fig, include_plotlyjs=False, full_html=False,
                       config={"displayModeBar": False, "responsive": True})


# --- individual charts ------------------------------------------------------

def chart_coverage(df: pd.DataFrame) -> str:
    """The signature: which signals exist on which days."""
    metrics = [m for m in ("sleep_hours", "resting_hr", "steps", "hr_mean", "hrv_rmssd",
                           "spo2_avg", "respiratory_rate", "sleep_score", "mvpa_minutes")
               if m in df.columns and df[m].notna().any()]
    fig = _fig("signal coverage")
    if not metrics:
        return _div(fig, 160)

    z = [[1 if pd.notna(v) else 0 for v in df[m]] for m in metrics]
    fig.add_trace(
        go.Heatmap(
            z=z,
            x=df["date"],
            y=[m.replace("_", " ") for m in metrics],
            colorscale=[[0, "#EFEDE6"], [1, INK]],
            showscale=False,
            hovertemplate="%{y}<br>%{x|%Y-%m-%d}<extra></extra>",
            xgap=0, ygap=3,
        )
    )
    fig.update_layout(yaxis=dict(autorange="reversed", gridcolor="rgba(0,0,0,0)"),
                      hovermode="closest")
    return _div(fig, 60 + 26 * len(metrics))


def chart_sleep(df: pd.DataFrame) -> str:
    fig = _fig("sleep duration vs 28-day baseline")
    if "sleep_hours" not in df.columns:
        return ""
    fig.add_trace(go.Scatter(x=df["date"], y=df["sleep_hours"], mode="markers",
                             name="nightly", marker=dict(size=3.5, color=SLEEP, opacity=0.45)))
    ma = df["sleep_hours"].rolling(28, min_periods=7).mean()
    fig.add_trace(go.Scatter(x=df["date"], y=ma, mode="lines", name="28-day mean",
                             line=dict(color=SLEEP, width=2)))
    fig.add_hrect(y0=7, y1=9, fillcolor=SIGNAL, opacity=0.07, line_width=0,
                  annotation_text="7-9 h reference", annotation_position="top left",
                  annotation_font=dict(size=10, color=MUTED))
    fig.update_layout(yaxis_title="hours")
    return _div(fig)


def chart_rhr(df: pd.DataFrame) -> str:
    drift = trends.rhr_drift(df)
    if drift.empty or drift["resting_hr"].notna().sum() < 10:
        return ""
    fig = _fig("resting heart rate: 7-day vs 60-day baseline")
    fig.add_trace(go.Scatter(x=drift["date"], y=drift["resting_hr"], mode="markers",
                             name="daily", marker=dict(size=3, color=MUTED, opacity=0.35)))
    fig.add_trace(go.Scatter(x=drift["date"], y=drift["rhr_base"], mode="lines",
                             name="60-day baseline", line=dict(color=INK, width=1.5, dash="dot")))
    fig.add_trace(go.Scatter(x=drift["date"], y=drift["rhr_short"], mode="lines",
                             name="7-day mean", line=dict(color=ALERT, width=2)))
    fig.update_layout(yaxis_title="bpm")
    return _div(fig)


def chart_activity(df: pd.DataFrame) -> str:
    if "steps" not in df.columns:
        return ""
    fig = _fig("daily steps")
    fig.add_trace(go.Bar(x=df["date"], y=df["steps"], name="steps",
                         marker=dict(color=RULE), hovertemplate="%{y:,.0f}<extra></extra>"))
    ma = df["steps"].rolling(28, min_periods=7).mean()
    fig.add_trace(go.Scatter(x=df["date"], y=ma, mode="lines", name="28-day mean",
                             line=dict(color=SIGNAL, width=2)))
    fig.update_layout(yaxis_title="steps", bargap=0.1)
    return _div(fig)


def chart_weekday(df: pd.DataFrame) -> str:
    prof = relationships.weekday_profile(df, ["sleep_hours", "steps", "resting_hr"])
    if prof.empty:
        return ""
    fig = _fig("weekday profile (z-scored, so metrics share an axis)")
    colors = {"sleep_hours": SLEEP, "steps": SIGNAL, "resting_hr": ALERT}
    for col in prof.columns:
        z = (prof[col] - prof[col].mean()) / (prof[col].std() or 1)
        fig.add_trace(go.Scatter(x=[d[:3] for d in prof.index], y=z, mode="lines+markers",
                                 name=col.replace("_", " "),
                                 line=dict(color=colors.get(col, MUTED), width=2)))
    fig.update_layout(yaxis_title="sd from own mean", hovermode="x")
    return _div(fig, 300)


def chart_correlations(df: pd.DataFrame) -> str:
    corr = relationships.correlation_matrix(df)
    if corr.empty:
        return ""
    labels = [c.replace("_", " ") for c in corr.columns]
    fig = _fig("same-day correlations")
    fig.add_trace(go.Heatmap(
        z=corr.to_numpy(), x=labels, y=labels,
        colorscale=[[0, ALERT], [0.5, "#F0EEE7"], [1, SIGNAL]],
        zmid=0, zmin=-1, zmax=1,
        hovertemplate="%{y} / %{x}<br>r = %{z:.2f}<extra></extra>",
        colorbar=dict(thickness=10, len=0.7, tickfont=dict(size=10)),
    ))
    fig.update_layout(hovermode="closest", height=420,
                      xaxis=dict(tickangle=-40, gridcolor="rgba(0,0,0,0)"),
                      yaxis=dict(autorange="reversed", gridcolor="rgba(0,0,0,0)"))
    return _div(fig, 440)


# --- HTML assembly ----------------------------------------------------------

def _flag_cards(flag_list: list[flags_mod.Flag]) -> str:
    if not flag_list:
        return ('<p class="empty">No rules fired. Either things look steady, or there '
                'is not yet enough history for the thresholds to apply.</p>')
    out = []
    for f in flag_list:
        color = TIER_COLOR.get(f.tier, MUTED)
        out.append(
            f'<article class="flag" style="--tier:{color}">'
            f'<div class="flag-tier">{html.escape(f.tier)}</div>'
            f'<div class="flag-body">'
            f'<div class="flag-topic">{html.escape(f.topic)}</div>'
            f'<h3>{html.escape(f.headline)}</h3>'
            f'<p>{html.escape(f.detail)}</p>'
            f'<code>{html.escape(f.evidence)}</code>'
            f"</div></article>"
        )
    return "\n".join(out)


def _stat_grid(df: pd.DataFrame) -> str:
    r90 = df[df["date"] >= df["date"].max() - pd.Timedelta(days=90)]
    specs = [
        ("sleep_hours", "sleep", "{:.1f} h"),
        ("resting_hr", "resting hr", "{:.0f} bpm"),
        ("steps", "steps", "{:,.0f}"),
        ("mvpa_minutes", "mvpa", "{:.0f} min"),
        ("hrv_rmssd", "hrv", "{:.0f} ms"),
        ("sleep_score", "sleep score", "{:.0f}"),
    ]
    cells = []
    for col, label, fmt in specs:
        if col not in r90.columns or r90[col].notna().sum() == 0:
            continue
        val = r90[col].mean()
        cells.append(
            f'<div class="stat"><div class="stat-val">{fmt.format(val)}</div>'
            f'<div class="stat-label">{label} · 90d avg</div></div>'
        )
    return "".join(cells)


def _table(df: pd.DataFrame, cols: dict[str, str], limit: int = 25) -> str:
    if df.empty:
        return '<p class="empty">Nothing to show.</p>'
    sub = df.head(limit)
    head = "".join(f"<th>{html.escape(v)}</th>" for v in cols.values())
    rows = []
    for _, r in sub.iterrows():
        cells = []
        for c in cols:
            v = r.get(c)
            if isinstance(v, float):
                v = "—" if pd.isna(v) else f"{v:,.3g}"
            elif isinstance(v, pd.Timestamp):
                v = v.strftime("%Y-%m-%d")
            cells.append(f"<td>{html.escape(str(v))}</td>")
        rows.append("<tr>" + "".join(cells) + "</tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(rows)}</tbody></table>"


CSS = f"""
*{{box-sizing:border-box}}
body{{margin:0;background:{PAPER};color:{INK};font-family:{SANS};line-height:1.55;
     -webkit-font-smoothing:antialiased}}
.wrap{{max-width:1080px;margin:0 auto;padding:56px 28px 96px}}
header{{border-bottom:2px solid {INK};padding-bottom:20px;margin-bottom:8px}}
.eyebrow{{font-family:{MONO};font-size:11px;letter-spacing:.16em;text-transform:uppercase;
         color:{MUTED};margin:0 0 10px}}
h1{{font-family:{MONO};font-size:30px;font-weight:600;letter-spacing:-.02em;margin:0 0 6px}}
.sub{{font-family:{MONO};font-size:12px;color:{MUTED};margin:0}}
.stats{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));
       gap:1px;background:{RULE};border:1px solid {RULE};margin:28px 0 44px}}
.stat{{background:{PAPER};padding:16px 14px}}
.stat-val{{font-family:{MONO};font-size:23px;font-weight:600;letter-spacing:-.02em}}
.stat-label{{font-family:{MONO};font-size:10px;letter-spacing:.08em;text-transform:uppercase;
           color:{MUTED};margin-top:4px}}
section{{margin:0 0 52px}}
h2{{font-family:{MONO};font-size:12px;letter-spacing:.16em;text-transform:uppercase;
   color:{INK};border-bottom:1px solid {RULE};padding-bottom:8px;margin:0 0 6px;
   display:flex;justify-content:space-between;align-items:baseline}}
h2 span{{color:{MUTED};letter-spacing:0;text-transform:none;font-size:11px}}
.note{{font-size:13.5px;color:{MUTED};margin:10px 0 18px;max-width:62ch}}
.flag{{display:flex;gap:16px;border-left:3px solid var(--tier);background:#fff;
      padding:16px 18px;margin-bottom:10px}}
.flag-tier{{font-family:{MONO};font-size:10px;letter-spacing:.1em;text-transform:uppercase;
          color:var(--tier);min-width:58px;padding-top:3px}}
.flag-topic{{font-family:{MONO};font-size:10px;letter-spacing:.1em;text-transform:uppercase;
           color:{MUTED};margin-bottom:3px}}
.flag h3{{font-size:15.5px;margin:0 0 6px;font-weight:600;line-height:1.4}}
.flag p{{font-size:13.5px;margin:0 0 8px;max-width:70ch;color:#33383F}}
.flag code{{font-family:{MONO};font-size:11px;color:{MUTED}}}
table{{width:100%;border-collapse:collapse;font-family:{MONO};font-size:11.5px}}
th{{text-align:left;font-weight:600;border-bottom:1px solid {INK};padding:7px 8px;
   letter-spacing:.05em;text-transform:uppercase;font-size:10px}}
td{{padding:6px 8px;border-bottom:1px solid {RULE}}}
tr:hover td{{background:#F4F2EB}}
.empty{{font-size:13.5px;color:{MUTED};font-style:italic}}
footer{{border-top:1px solid {RULE};padding-top:20px;font-size:12px;color:{MUTED};
       max-width:70ch}}
@media (max-width:640px){{.wrap{{padding:32px 16px 64px}}h1{{font-size:23px}}
  .flag{{flex-direction:column;gap:6px}}}}
"""


def build_html(df: pd.DataFrame, flag_list: list[flags_mod.Flag]) -> str:
    span = f"{df['date'].min():%d %b %Y} — {df['date'].max():%d %b %Y}"
    days = len(df)
    trend_tbl = trends.trend_table(df)
    corr_tbl = relationships.hypothesis_table(df)
    anom = trends.anomalies(df).sort_values("z", key=abs, ascending=False)

    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Fitbit analysis</title>
<script src="https://cdn.plot.ly/plotly-2.32.0.min.js" charset="utf-8"></script>
<style>{CSS}</style></head>
<body><div class="wrap">

<header>
  <p class="eyebrow">Takeout export · local analysis</p>
  <h1>Wearable record</h1>
  <p class="sub">{span} · {days:,} days on the spine · {df.shape[1]} columns</p>
</header>

<div class="stats">{_stat_grid(df)}</div>

<section>
  <h2>Coverage <span>what exists, and when</span></h2>
  <p class="note">Dark means the signal was recorded that day. Read every chart below
  against this one: a flat stretch in steps is a different thing from a gap in wear.</p>
  {chart_coverage(df)}
</section>

<section>
  <h2>Worth noticing</h2>
  <p class="note">Rules evaluated against your own baselines, not population norms.
  Descriptive only — see the note at the bottom.</p>
  {_flag_cards(flag_list)}
</section>

<section>
  <h2>Sleep</h2>
  {chart_sleep(df)}
</section>

<section>
  <h2>Resting heart rate</h2>
  <p class="note">The comparison that carries information is the fast line against the
  slow one. Absolute values from a wrist sensor drift between devices; your own
  baseline does not.</p>
  {chart_rhr(df)}
</section>

<section>
  <h2>Activity</h2>
  {chart_activity(df)}
</section>

<section>
  <h2>Weekly shape</h2>
  {chart_weekday(df)}
</section>

<section>
  <h2>Relationships <span>observational, not causal</span></h2>
  <p class="note">Pre-registered pairs, tested once, with a Benjamini-Hochberg correction
  so that testing a dozen hypotheses does not manufacture a significant one. Lag 1 means
  the driver was measured the day before the outcome.</p>
  {_table(corr_tbl, {"driver": "driver", "outcome": "outcome", "lag_days": "lag",
                     "r": "r", "q_value": "q", "n": "n", "strength": "strength"})}
  {chart_correlations(df)}
</section>

<section>
  <h2>Trends <span>last 180 days</span></h2>
  {_table(trend_tbl, {"metric": "metric", "n_days": "days", "mean": "mean",
                      "last_28d": "last 28d", "prior_28d": "prior 28d",
                      "slope_per_month": "slope/mo", "p_value": "p"})}
</section>

<section>
  <h2>Outlier days <span>|robust z| ≥ 3.5</span></h2>
  <p class="note">Scored against a trailing 60-day median and MAD, so a single strange
  day cannot hide inside its own baseline.</p>
  {_table(anom, {"date": "date", "metric": "metric", "value": "value",
                 "z": "z", "direction": "direction"}, limit=30)}
</section>

<footer>
  Generated locally from a Google Takeout export. Everything here describes what a
  consumer wrist device estimated, which is a reasonable trend instrument and a poor
  absolute one. Nothing in this report is a diagnosis or medical advice. If something
  here concerns you, or a pattern persists, bring it to a clinician who can weigh it
  against your history and a real measurement.
</footer>

</div></body></html>"""


def run(cfg: Config, out_path: Path | None = None) -> Path:
    facts_path = cfg.gold / "daily_facts.parquet"
    if not facts_path.exists():
        raise FileNotFoundError("No daily_facts. Run `fitbit transform` first.")

    df = pd.read_parquet(facts_path)
    df["date"] = pd.to_datetime(df["date"])

    flag_list = flags_mod.evaluate(df)
    out_path = out_path or (Path("reports") / "fitbit_report.html")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(build_html(df, flag_list), encoding="utf-8")

    flags_mod.to_frame(flag_list).to_csv(out_path.with_suffix(".flags.csv"), index=False)

    print(f"\nReport   -> {out_path}")
    print(f"Flags    -> {len(flag_list)} "
          f"({', '.join(sorted({f.tier for f in flag_list})) or 'none'})")
    return out_path
