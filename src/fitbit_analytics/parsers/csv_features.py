"""Parse the CSV feature exports (HRV, SpO2, sleep score, stress, readiness).

These files change column names between export vintages, so each parser picks
its value column by searching a list of aliases rather than indexing a fixed
name. Unmatched files return empty and are reported, not silently dropped.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import pandas as pd

from .common import find_timestamp_column, read_csv_files

# dataset -> (output column, [aliases in order of preference])
VALUE_ALIASES: dict[str, tuple[str, list[str]]] = {
    "hrv_daily": ("hrv_rmssd", ["rmssd", "daily_rmssd", "deep_rmssd"]),
    "respiratory_rate": ("respiratory_rate",
                         ["daily_respiratory_rate", "full_sleep_breathing_rate",
                          "breathing_rate", "respiratory_rate"]),
    "spo2_daily": ("spo2_avg", ["average_value", "avg", "daily_spo2_pct", "average"]),
    "temperature_computed": ("skin_temp_delta",
                             ["nightly_temperature", "temperature", "value"]),
    "sleep_score": ("sleep_score", ["overall_score", "sleep_score", "score"]),
    "stress_score": ("stress_score", ["stress_score", "score"]),
    "readiness": ("readiness_score", ["readiness_score", "score"]),
}

# Extra columns worth keeping when present.
EXTRA_COLUMNS: dict[str, dict[str, str]] = {
    "spo2_daily": {"lower_bound": "spo2_min", "upper_bound": "spo2_max"},
    "sleep_score": {"composition_score": "sleep_composition_score",
                    "revitalization_score": "sleep_revitalization_score",
                    "duration_score": "sleep_duration_score",
                    "resting_heart_rate": "sleep_resting_hr"},
    "readiness": {"activity_subcomponent": "readiness_activity",
                  "sleep_subcomponent": "readiness_sleep",
                  "hrv_subcomponent": "readiness_hrv"},
}


def _pick(df: pd.DataFrame, aliases: list[str]) -> str | None:
    for alias in aliases:
        if alias in df.columns:
            return alias
    for alias in aliases:  # substring fallback for e.g. "daily_spo2_pct_avg"
        for col in df.columns:
            if alias in col:
                return col
    return None


def parse(dataset: str, paths: Iterable[Path]) -> pd.DataFrame:
    """Parse one CSV dataset down to a daily frame keyed on ``date``."""
    paths = list(paths)
    if not paths or dataset not in VALUE_ALIASES:
        return pd.DataFrame()

    raw = read_csv_files(paths)
    if raw.empty:
        return pd.DataFrame()

    ts_col = find_timestamp_column(raw)
    value_name, aliases = VALUE_ALIASES[dataset]
    value_col = _pick(raw, aliases)

    if ts_col is None or value_col is None:
        return pd.DataFrame()

    out = pd.DataFrame(
        {
            "date": pd.to_datetime(raw[ts_col], errors="coerce", format="mixed").dt.normalize(),
            value_name: pd.to_numeric(raw[value_col], errors="coerce"),
        }
    )

    for src, dst in EXTRA_COLUMNS.get(dataset, {}).items():
        col = _pick(raw, [src])
        if col is not None:
            out[dst] = pd.to_numeric(raw[col], errors="coerce")

    out = out.dropna(subset=["date"])
    if out.empty:
        return out

    # Several rows per day can appear across overlapping export files.
    numeric_cols = [c for c in out.columns if c != "date"]
    return out.groupby("date", as_index=False)[numeric_cols].mean().sort_values("date")


def parse_azm(paths: Iterable[Path]) -> pd.DataFrame:
    """Active Zone Minutes ships at minute grain; roll it up to daily totals."""
    raw = read_csv_files(list(paths))
    if raw.empty:
        return pd.DataFrame()

    ts_col = find_timestamp_column(raw)
    if ts_col is None:
        return pd.DataFrame()

    zone_col = next((c for c in raw.columns if "zone" in c and c != ts_col), None)
    value_col = next((c for c in ("total_minutes", "value", "minutes") if c in raw.columns), None)
    if value_col is None:
        return pd.DataFrame()

    raw = raw.copy()
    raw["date"] = pd.to_datetime(raw[ts_col], errors="coerce", format="mixed").dt.normalize()
    raw[value_col] = pd.to_numeric(raw[value_col], errors="coerce")
    raw = raw.dropna(subset=["date"])

    if zone_col is not None:
        wide = (
            raw.pivot_table(index="date", columns=zone_col, values=value_col, aggfunc="sum")
            .reset_index()
        )
        wide.columns = ["date"] + [f"azm_{str(c).lower().replace(' ', '_')}"
                                   for c in wide.columns[1:]]
        azm_cols = [c for c in wide.columns if c.startswith("azm_")]
        wide["azm_total"] = wide[azm_cols].sum(axis=1, min_count=1)
        return wide

    return raw.groupby("date", as_index=False)[value_col].sum().rename(
        columns={value_col: "azm_total"}
    )
