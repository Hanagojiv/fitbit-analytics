"""Trend, baseline and anomaly primitives over the daily fact table."""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy import stats

# Metrics where a rolling baseline is meaningful.
DEFAULT_METRICS = [
    "resting_hr", "sleep_hours", "steps", "hr_mean", "hrv_rmssd",
    "spo2_avg", "respiratory_rate", "sleep_score", "mvpa_minutes",
    "sleep_efficiency_calc", "midpoint_hour",
]


def present_metrics(df: pd.DataFrame, metrics: list[str] | None = None) -> list[str]:
    """Metrics that exist and have enough non-null values to be worth modelling."""
    candidates = metrics or DEFAULT_METRICS
    return [m for m in candidates if m in df.columns and df[m].notna().sum() >= 14]


def add_baselines(df: pd.DataFrame, metrics: list[str] | None = None,
                  windows: tuple[int, ...] = (7, 28)) -> pd.DataFrame:
    """Attach centred-free rolling means and an EWMA per metric.

    Windows are trailing, not centred, so every value uses only information
    available on that day. That keeps the columns honest if you ever replay
    this as a stream.
    """
    out = df.copy()
    for metric in present_metrics(df, metrics):
        s = out[metric]
        for w in windows:
            out[f"{metric}_ma{w}"] = s.rolling(w, min_periods=max(3, w // 3)).mean()
        out[f"{metric}_ewma"] = s.ewm(span=14, min_periods=5).mean()
    return out


def slope_per_month(s: pd.Series, dates: pd.Series) -> tuple[float, float]:
    """Ordinary least squares slope in units per 30 days, with its p-value."""
    mask = s.notna()
    if mask.sum() < 10:
        return (np.nan, np.nan)
    x = (dates[mask] - dates[mask].min()).dt.days.to_numpy(dtype=float)
    y = s[mask].to_numpy(dtype=float)
    if np.ptp(x) < 14:
        return (np.nan, np.nan)
    res = stats.linregress(x, y)
    return (float(res.slope) * 30.0, float(res.pvalue))


def trend_table(df: pd.DataFrame, metrics: list[str] | None = None,
                lookback_days: int = 180) -> pd.DataFrame:
    """Recent direction and level for each metric."""
    recent = df[df["date"] >= df["date"].max() - pd.Timedelta(days=lookback_days)]
    rows = []
    for metric in present_metrics(df, metrics):
        s = recent[metric]
        slope, pval = slope_per_month(s, recent["date"])
        last28 = s.tail(28).mean()
        prev28 = s.tail(56).head(28).mean()
        rows.append(
            {
                "metric": metric,
                "n_days": int(s.notna().sum()),
                "mean": s.mean(),
                "std": s.std(),
                "last_28d": last28,
                "prior_28d": prev28,
                "delta_28d": last28 - prev28 if pd.notna(prev28) else np.nan,
                "slope_per_month": slope,
                "p_value": pval,
            }
        )
    return pd.DataFrame(rows).sort_values("metric").reset_index(drop=True)


def robust_z(s: pd.Series, window: int = 60) -> pd.Series:
    """Modified z-score against a trailing median and MAD.

    Median and MAD rather than mean and standard deviation, because a handful
    of genuine outliers would otherwise inflate the baseline enough to hide
    themselves.

    A very stable metric can drive the MAD to exactly zero, which would make
    every score undefined and hide spikes in precisely the signals where a
    spike matters most. Fall back to the mean absolute deviation, and if the
    window is perfectly constant, treat any departure as a strong outlier
    (Iglewicz & Hoaglin, 1993).
    """
    med = s.rolling(window, min_periods=14).median()
    dev = s - med

    mad = dev.abs().rolling(window, min_periods=14).median()
    mean_ad = dev.abs().rolling(window, min_periods=14).mean()

    scale = mad * 1.4826
    scale = scale.where(scale > 0, mean_ad * 1.253314)

    z = dev / scale.replace(0, np.nan)
    # Constant window: scale is 0 or NaN, but a nonzero deviation is still real.
    degenerate = scale.isna() | (scale == 0)
    return z.where(~(degenerate & dev.notna() & (dev != 0)),
                   np.sign(dev) * 999.0)


def anomalies(df: pd.DataFrame, metrics: list[str] | None = None,
              threshold: float = 3.5) -> pd.DataFrame:
    """Days where a metric departed sharply from its own recent baseline."""
    rows = []
    for metric in present_metrics(df, metrics):
        z = robust_z(df[metric])
        hits = df.loc[z.abs() >= threshold, ["date", metric]].copy()
        if hits.empty:
            continue
        hits["metric"] = metric
        hits["value"] = hits[metric]
        hits["z"] = z[z.abs() >= threshold].to_numpy()
        hits["direction"] = np.where(hits["z"] > 0, "high", "low")
        rows.append(hits[["date", "metric", "value", "z", "direction"]])

    if not rows:
        return pd.DataFrame(columns=["date", "metric", "value", "z", "direction"])
    return pd.concat(rows).sort_values("date").reset_index(drop=True)


def rhr_drift(df: pd.DataFrame, short: int = 7, long: int = 60) -> pd.DataFrame:
    """Short-window resting heart rate against a long baseline.

    A sustained elevation of the 7 day mean over the 60 day mean is the
    classic early marker of accumulated fatigue, poor sleep, alcohol, or an
    infection incubating. It is not diagnostic of anything on its own.
    """
    if "resting_hr" not in df.columns:
        return pd.DataFrame()

    out = df[["date", "resting_hr"]].copy()
    out["rhr_short"] = out["resting_hr"].rolling(short, min_periods=3).mean()
    out["rhr_base"] = out["resting_hr"].rolling(long, min_periods=21).mean()
    out["rhr_delta"] = out["rhr_short"] - out["rhr_base"]
    sd = out["resting_hr"].rolling(long, min_periods=21).std()
    out["rhr_delta_sd"] = out["rhr_delta"] / sd.replace(0, np.nan)
    out["elevated"] = out["rhr_delta_sd"] >= 1.0
    return out


def sleep_regularity(df: pd.DataFrame, window: int = 28) -> pd.DataFrame:
    """Consistency of sleep timing, which tracks outcomes as well as duration.

    Reported as the rolling standard deviation of the sleep midpoint in
    minutes. Under ~30 minutes is very regular; over ~90 minutes is the
    signature of a rotating or socially jetlagged schedule.
    """
    if "midpoint_hour" not in df.columns:
        return pd.DataFrame()

    out = df[["date", "midpoint_hour", "bedtime_hour", "sleep_hours"]].copy()
    out["midpoint_sd_min"] = out["midpoint_hour"].rolling(
        window, min_periods=max(7, window // 3)
    ).std() * 60
    out["bedtime_sd_min"] = out["bedtime_hour"].rolling(
        window, min_periods=max(7, window // 3)
    ).std() * 60
    out["duration_sd_min"] = out["sleep_hours"].rolling(
        window, min_periods=max(7, window // 3)
    ).std() * 60
    return out


def social_jetlag(df: pd.DataFrame) -> dict[str, float]:
    """Weekend minus weekday sleep midpoint, in minutes."""
    if "midpoint_hour" not in df.columns or "is_weekend" not in df.columns:
        return {}
    wk = df.loc[~df["is_weekend"].astype(bool), "midpoint_hour"].mean()
    we = df.loc[df["is_weekend"].astype(bool), "midpoint_hour"].mean()
    if pd.isna(wk) or pd.isna(we):
        return {}
    return {
        "weekday_midpoint_hour": float(wk),
        "weekend_midpoint_hour": float(we),
        "social_jetlag_min": float((we - wk) * 60),
    }
