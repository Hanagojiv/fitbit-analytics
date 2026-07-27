"""Correlations between daily metrics, including lagged pairs.

Everything here is observational. Same-day correlations in wearable data are
especially prone to reversed causality — a hard workout raises tonight's
resting heart rate, and a bad night's sleep suppresses tomorrow's step count.
The lag structure is reported so direction can at least be reasoned about,
never asserted.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy import stats

# Pairs worth testing explicitly: (driver, outcome, lag_days)
# lag=1 means the driver is measured the day before the outcome.
HYPOTHESES: list[tuple[str, str, int]] = [
    ("steps", "sleep_hours", 1),
    ("mvpa_minutes", "sleep_hours", 1),
    ("mvpa_minutes", "resting_hr", 1),
    ("steps", "resting_hr", 1),
    ("sleep_hours", "steps", 1),
    ("sleep_hours", "resting_hr", 0),
    ("sleep_efficiency_calc", "resting_hr", 0),
    ("midpoint_hour", "sleep_hours", 0),
    ("hrv_rmssd", "resting_hr", 0),
    ("sleep_hours", "hrv_rmssd", 0),
    ("mvpa_minutes", "hrv_rmssd", 1),
    ("sleep_hours", "sleep_score", 0),
    ("respiratory_rate", "resting_hr", 0),
]

MIN_PAIRS = 30


def _pearson(x: pd.Series, y: pd.Series) -> tuple[float, float, int]:
    mask = x.notna() & y.notna()
    n = int(mask.sum())
    if n < MIN_PAIRS:
        return (np.nan, np.nan, n)
    r, p = stats.pearsonr(x[mask], y[mask])
    return (float(r), float(p), n)


def hypothesis_table(df: pd.DataFrame,
                     pairs: list[tuple[str, str, int]] | None = None) -> pd.DataFrame:
    """Test the pre-registered pairs above, with Benjamini-Hochberg correction.

    Correcting matters: testing a dozen pairs on one person's noisy data will
    otherwise hand you a 'significant' result by construction.
    """
    pairs = pairs or HYPOTHESES
    rows = []
    for driver, outcome, lag in pairs:
        if driver not in df.columns or outcome not in df.columns:
            continue
        x = df[driver].shift(lag) if lag else df[driver]
        r, p, n = _pearson(x, df[outcome])
        rows.append(
            {
                "driver": driver,
                "outcome": outcome,
                "lag_days": lag,
                "r": r,
                "p_value": p,
                "n": n,
            }
        )

    out = pd.DataFrame(rows)
    if out.empty:
        return out

    tested = out["p_value"].notna()
    out["q_value"] = np.nan
    if tested.sum() > 0:
        out.loc[tested, "q_value"] = _bh(out.loc[tested, "p_value"].to_numpy())

    out["significant"] = out["q_value"] < 0.05
    out["strength"] = pd.cut(
        out["r"].abs(),
        bins=[-0.01, 0.1, 0.3, 0.5, 1.0],
        labels=["negligible", "weak", "moderate", "strong"],
    )
    return out.sort_values("q_value", na_position="last").reset_index(drop=True)


def _bh(pvals: np.ndarray) -> np.ndarray:
    """Benjamini-Hochberg false discovery rate adjustment."""
    n = len(pvals)
    order = np.argsort(pvals)
    ranked = pvals[order] * n / (np.arange(n) + 1)
    ranked = np.minimum.accumulate(ranked[::-1])[::-1]
    out = np.empty(n)
    out[order] = np.clip(ranked, 0, 1)
    return out


def correlation_matrix(df: pd.DataFrame, metrics: list[str] | None = None) -> pd.DataFrame:
    """Same-day Pearson matrix over whichever metrics have coverage."""
    from .trends import present_metrics

    cols = present_metrics(df, metrics)
    if len(cols) < 2:
        return pd.DataFrame()
    return df[cols].corr(method="pearson", min_periods=MIN_PAIRS)


def weekday_profile(df: pd.DataFrame, metrics: list[str] | None = None) -> pd.DataFrame:
    """Mean of each metric by day of week, ordered Monday first."""
    from .trends import present_metrics

    cols = present_metrics(df, metrics)
    if not cols or "dow" not in df.columns:
        return pd.DataFrame()

    order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    out = df.groupby("dow")[cols].mean()
    return out.reindex([d for d in order if d in out.index])
