"""Parse minute-grain JSON files and downsample them.

The intraday files are the overwhelming majority of an export's bytes and
almost none of its insight. We aggregate on read and never persist the raw
rows, which keeps the whole pipeline comfortably in memory.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import numpy as np
import pandas as pd

from .common import load_datetime_value_files, parse_timestamps, read_json_records

# How each dataset collapses to a coarser grain.
# "sum" for accumulating counters, richer stats for continuous signals.
AGG_SPEC: dict[str, dict[str, str]] = {
    "steps": {"steps": "sum"},
    "calories": {"calories": "sum"},
    "distance": {"distance": "sum"},
    "sedentary_minutes": {"sedentary_minutes": "sum"},
    "lightly_active_minutes": {"lightly_active_minutes": "sum"},
    "moderately_active_minutes": {"moderately_active_minutes": "sum"},
    "very_active_minutes": {"very_active_minutes": "sum"},
    "altitude": {"altitude": "max"},
}

# Datasets whose payload is a nested object rather than a scalar.
NESTED_VALUE_COLUMN = {"heart_rate": "heart_rate_bpm"}


def _hr_aggregates(g: pd.core.groupby.DataFrameGroupBy) -> pd.DataFrame:
    """Heart rate deserves more than a mean.

    The 5th percentile approximates true resting tone better than the minimum,
    which is noisy, and time-above-threshold is a cheap intensity proxy that
    does not depend on Fitbit's own zone definitions.
    """
    out = g["heart_rate_bpm"].agg(
        hr_mean="mean",
        hr_min="min",
        hr_max="max",
        hr_std="std",
        hr_p05=lambda s: s.quantile(0.05),
        hr_p50="median",
        hr_p95=lambda s: s.quantile(0.95),
        hr_samples="count",
    )
    return out.reset_index()


def parse_and_downsample(
    dataset: str,
    paths: Iterable[Path],
    grains: Iterable[str] = ("daily",),
) -> dict[str, pd.DataFrame]:
    """Return {grain: DataFrame} for one intraday dataset."""
    paths = list(paths)
    if not paths:
        return {}

    value_col = NESTED_VALUE_COLUMN.get(dataset, dataset)
    raw = load_datetime_value_files(paths, dataset if dataset not in NESTED_VALUE_COLUMN
                                    else "heart_rate")
    if raw.empty or value_col not in raw.columns:
        return {}

    raw = raw.dropna(subset=[value_col])
    if raw.empty:
        return {}

    out: dict[str, pd.DataFrame] = {}
    for grain in grains:
        freq = {"daily": "D", "hourly": "h"}.get(grain)
        if freq is None:
            continue
        keys = raw["ts"].dt.floor(freq).rename("date" if grain == "daily" else "ts_hour")
        grouped = raw.groupby(keys)

        if dataset == "heart_rate":
            agg = _hr_aggregates(grouped)
        else:
            spec = AGG_SPEC.get(dataset, {value_col: "sum"})
            agg = grouped.agg(**{k: (value_col, v) for k, v in spec.items()}).reset_index()
            # Coverage: how many minute-samples backed each bucket.
            counts = grouped[value_col].count().rename(f"{dataset}_samples").reset_index()
            agg = agg.merge(counts, on=agg.columns[0])

        out[grain] = agg

    return out


def wear_coverage(hr_hourly: pd.DataFrame) -> pd.DataFrame:
    """Estimate daily wear time from heart-rate sample density.

    Gaps matter: a day with four hours of heart rate data is not a low
    activity day, it is a day the watch sat on a charger. Downstream analysis
    uses this to exclude days rather than average them in.
    """
    if hr_hourly.empty or "ts_hour" not in hr_hourly.columns:
        return pd.DataFrame()

    df = hr_hourly.copy()
    df["date"] = df["ts_hour"].dt.normalize()
    cov = (
        df.assign(has_data=(df["hr_samples"] > 0).astype(int))
        .groupby("date")
        .agg(hours_with_hr=("has_data", "sum"), hr_samples_day=("hr_samples", "sum"))
        .reset_index()
    )
    cov["wear_fraction"] = (cov["hours_with_hr"] / 24.0).clip(upper=1.0)
    cov["is_full_day"] = cov["wear_fraction"] >= 0.5
    return cov


def resting_hr_from_json(paths: Iterable[Path]) -> pd.DataFrame:
    """resting_heart_rate-*.json has a nested {date, value, error} payload."""
    df = load_datetime_value_files(paths, "rhr")
    if df.empty:
        return pd.DataFrame()

    value_col = next((c for c in ("rhr_value", "rhr") if c in df.columns), None)
    if value_col is None:
        return pd.DataFrame()

    out = pd.DataFrame(
        {
            "date": df["ts"].dt.normalize(),
            "resting_hr": pd.to_numeric(df[value_col], errors="coerce"),
        }
    )
    if "rhr_error" in df.columns:
        out["resting_hr_error"] = pd.to_numeric(df["rhr_error"], errors="coerce")

    # Fitbit writes 0.0 when it could not compute a value for the day.
    out.loc[out["resting_hr"] <= 0, "resting_hr"] = np.nan
    return out.dropna(subset=["resting_hr"]).drop_duplicates("date").sort_values("date")


def vo2max_from_json(paths: Iterable[Path]) -> pd.DataFrame:
    """demographic_vo2_max-*.json: nested {demographicVO2Max, ...} per day."""
    df = load_datetime_value_files(paths, "vo2max")
    if df.empty:
        return pd.DataFrame()

    out = pd.DataFrame({"date": df["ts"].dt.normalize()})
    for src, dst in (
        ("vo2max_demographicVO2Max", "vo2max"),
        ("vo2max_filteredDemographicVO2Max", "vo2max_filtered"),
    ):
        if src in df.columns:
            out[dst] = pd.to_numeric(df[src], errors="coerce")

    return out.dropna(subset=["date"]).drop_duplicates("date").sort_values("date")


def hr_zone_minutes_from_json(paths: Iterable[Path]) -> pd.DataFrame:
    """time_in_heart_rate_zones-*.json: nested {valuesInZones: {zone: minutes}} per day."""
    frames: list[pd.DataFrame] = []
    for path in paths:
        for rec in read_json_records(Path(path)):
            date = rec.get("dateTime")
            zones = ((rec.get("value") or {}).get("valuesInZones")) or {}
            if date is None or not zones:
                continue
            row = {"date": date, **{f"hr_zone_{k.lower()}": v for k, v in zones.items()}}
            frames.append(pd.DataFrame([row]))

    if not frames:
        return pd.DataFrame()

    out = pd.concat(frames, ignore_index=True)
    out["date"] = parse_timestamps(out["date"]).dt.normalize()
    zone_cols = [c for c in out.columns if c != "date"]
    for c in zone_cols:
        out[c] = pd.to_numeric(out[c], errors="coerce")
    return out.dropna(subset=["date"]).drop_duplicates("date").sort_values("date")
