"""Parse the newer "Google Health" per-source export.

Unlike the old Global Export Data layout, every file here already has a
self-describing header: ``timestamp, <value column(s)>, data source``. That
regularity means one generic, config-driven parser covers ~25 datasets
instead of a bespoke one each. ``data source`` records which device/app
wrote the row -- the Fitbit Air identifies itself as "Radiance", its
internal codename, versus "Fitbit App" for phone-derived or manually
entered rows.

Timestamps here are UTC ISO 8601 instants (``2026-07-01T00:18:00Z``),
unlike Global Export Data's device-local convention -- the two layers do
not share a clock and must not be joined without an explicit conversion.

Daily summary files are a partial exception: many stamp every row
``T00:00:00Z`` (or a bare date with no time at all) as a calendar-date
label, not a real instant. Converting those through a timezone would shift
the date by one day near the UTC offset boundary, so they are read
literally instead -- see ``_daily_date``.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import pandas as pd

from .common import normalize_column

# --- minute-grain numeric signals, downsampled like parsers/intraday.py ---
# agg: "sum" for accumulating counters, "mean" for continuous signals,
# "hr_stats" for the richer heart-rate-shaped summary.
INTRADAY_SPECS: dict[str, dict[str, str]] = {
    "gh_heart_rate": {"value_col": "beats_per_minute", "agg": "hr_stats"},
    "gh_steps": {"value_col": "steps", "agg": "sum"},
    "gh_calories": {"value_col": "calories", "agg": "sum"},
    "gh_active_energy_burned": {"value_col": "kilocalories", "agg": "sum",
                                 "out": "active_energy_kcal"},
    "gh_distance": {"value_col": "distance", "agg": "sum"},
    "gh_body_temperature": {"value_col": "temperature_celsius", "agg": "mean",
                             "out": "body_temp_c"},
    "gh_oxygen_saturation": {"value_col": "oxygen_saturation_percentage", "agg": "mean",
                              "out": "spo2_pct"},
    "gh_speed": {"value_col": "speed", "agg": "mean"},
    "gh_cardio_load": {"value_col": "total", "agg": "mean", "out": "cardio_load"},
}

# --- categorical rows, pivoted to one column per category -----------------
# value_col=None means each row is one minute in that category: count rows.
PIVOT_SPECS: dict[str, dict[str, str | None]] = {
    "gh_activity_level": {"category_col": "level", "value_col": None, "prefix": "activity_min"},
    "gh_active_zone_minutes": {"category_col": "heart_rate_zone", "value_col": "total_minutes",
                                "prefix": "azm2"},
    "gh_calories_in_heart_rate_zone": {"category_col": "heart_rate_zone_type",
                                        "value_col": "kcal", "prefix": "kcal_zone"},
    "gh_time_in_heart_rate_zone": {"category_col": "heart_rate_zone_type", "value_col": None,
                                    "prefix": "hr_zone_min"},
}

# --- already ~daily grain: rename columns, no downsampling needed ---------
# "instant": True means the timestamp is a real clock time (rare updates
# like a weigh-in) and should go through the configured timezone before
# taking the date. False (default) means it is a T00:00:00Z / bare-date
# calendar label and must be read literally -- see module docstring.
DAILY_PASSTHROUGH_SPECS: dict[str, dict] = {
    "gh_daily_heart_rate_variability": {
        "instant": False,
        "columns": {
            "average_heart_rate_variability_milliseconds": "hrv_avg_ms_gh",
            "non_rem_heart_rate_beats_per_minute": "non_rem_hr_bpm_gh",
            "entropy": "hrv_entropy_gh",
            "deep_sleep_root_mean_square_of_successive_differences_milliseconds":
                "hrv_deep_rmssd_ms_gh",
        },
    },
    "gh_heart_rate_variability": {
        "instant": False,
        "columns": {
            "root_mean_square_of_successive_differences_milliseconds": "hrv_rmssd_ms_gh",
            "standard_deviation_milliseconds": "hrv_sdnn_ms_gh",
        },
    },
    "gh_daily_oxygen_saturation": {
        "instant": False,
        "columns": {
            "average_percentage": "spo2_avg_pct_gh",
            "lower_bound_percentage": "spo2_min_pct_gh",
            "upper_bound_percentage": "spo2_max_pct_gh",
            "baseline_percentage": "spo2_baseline_pct_gh",
            "standard_deviation_percentage": "spo2_std_pct_gh",
        },
    },
    "gh_daily_readiness": {
        "instant": False,
        "columns": {
            "score": "readiness_score_gh",
            "readiness_level": "readiness_level_gh",
            "sleep_readiness": "readiness_sleep_gh",
            "heart_rate_variability_readiness": "readiness_hrv_gh",
            "resting_heart_rate_readiness": "readiness_rhr_gh",
        },
    },
    "gh_daily_respiratory_rate": {
        "instant": False,
        "columns": {"breaths_per_minute": "respiratory_rate_gh"},
    },
    "gh_daily_resting_heart_rate": {
        "instant": False,
        "columns": {"beats_per_minute": "resting_hr_gh"},
    },
    "gh_daily_sleep_temperature_derivations": {
        "instant": False,
        "columns": {
            "nightly_temperature_celsius": "sleep_temp_nightly_c_gh",
            "baseline_temperature_celsius": "sleep_temp_baseline_c_gh",
            "relative_nightly_stddev_30d_celsius": "sleep_temp_stddev_30d_c_gh",
        },
    },
    "gh_height": {
        "instant": True,
        "columns": {"height_millimeters": "height_mm_gh"},
    },
    "gh_weight": {
        "instant": True,
        "columns": {"weight_grams": "weight_g_gh"},
    },
    "gh_cardio_acute_chronic_workload_ratio": {
        "instant": False,
        "columns": {"ratio": "cardio_acwr_gh"},
    },
    "gh_cardio_load_observed_interval": {
        "instant": False,
        "columns": {
            "min_observed_load": "cardio_load_min_gh",
            "max_observed_load": "cardio_load_max_gh",
        },
    },
}


def _read_one(path: Path) -> pd.DataFrame:
    try:
        df = pd.read_csv(path)
    except (OSError, pd.errors.ParserError, pd.errors.EmptyDataError, UnicodeDecodeError):
        return pd.DataFrame()
    if df.empty:
        return df
    df.columns = [normalize_column(c) for c in df.columns]
    return df


def _load(paths: Iterable[Path]) -> pd.DataFrame:
    """Concatenate files sharing this format's schema, keyed on a parsed ``ts``."""
    frames = [df for df in (_read_one(Path(p)) for p in paths) if not df.empty]
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    if "timestamp" not in df.columns:
        return pd.DataFrame()
    df["ts"] = pd.to_datetime(df["timestamp"], errors="coerce", utc=True, format="mixed")
    return df.dropna(subset=["ts"])


def _local_date_key(ts: pd.Series, tz: str, freq: str) -> pd.Series:
    """UTC instant -> local calendar bucket. For real per-minute clock time."""
    return ts.dt.tz_convert(tz).dt.tz_localize(None).dt.floor(freq)


def parse_intraday(
    dataset: str, paths: Iterable[Path], grains: Iterable[str], tz: str
) -> dict[str, pd.DataFrame]:
    spec = INTRADAY_SPECS[dataset]
    df = _load(paths)
    value_col = spec["value_col"]
    if df.empty or value_col not in df.columns:
        return {}

    df = df.copy()
    df[value_col] = pd.to_numeric(df[value_col], errors="coerce")
    df = df.dropna(subset=[value_col])
    if df.empty:
        return {}

    out_name = spec.get("out", value_col)
    out: dict[str, pd.DataFrame] = {}
    for grain in grains:
        freq = {"daily": "D", "hourly": "h"}.get(grain)
        if freq is None:
            continue
        keys = _local_date_key(df["ts"], tz, freq).rename(
            "date" if grain == "daily" else "ts_hour"
        )
        grouped = df.groupby(keys)

        if spec["agg"] == "hr_stats":
            agg = grouped[value_col].agg(
                hr_mean_gh="mean", hr_min_gh="min", hr_max_gh="max", hr_std_gh="std",
                hr_p05_gh=lambda s: s.quantile(0.05), hr_p50_gh="median",
                hr_p95_gh=lambda s: s.quantile(0.95), hr_samples_gh="count",
            ).reset_index()
        elif spec["agg"] == "sum":
            agg = grouped[value_col].sum().reset_index(name=f"{out_name}_gh")
        else:  # mean
            agg = grouped[value_col].mean().reset_index(name=f"{out_name}_gh")

        out[grain] = agg

    return out


def parse_pivot(
    dataset: str, paths: Iterable[Path], grains: Iterable[str], tz: str
) -> dict[str, pd.DataFrame]:
    spec = PIVOT_SPECS[dataset]
    df = _load(paths)
    cat_col = spec["category_col"]
    if df.empty or cat_col not in df.columns:
        return {}

    value_col = spec["value_col"]
    out: dict[str, pd.DataFrame] = {}
    for grain in grains:
        freq = {"daily": "D", "hourly": "h"}.get(grain)
        if freq is None:
            continue
        date_key = _local_date_key(df["ts"], tz, freq)

        if value_col and value_col in df.columns:
            vals = pd.to_numeric(df[value_col], errors="coerce")
            wide = pd.DataFrame({"key": date_key, "cat": df[cat_col], "val": vals}).pivot_table(
                index="key", columns="cat", values="val", aggfunc="sum"
            )
        else:
            wide = pd.DataFrame({"key": date_key, "cat": df[cat_col]}).pivot_table(
                index="key", columns="cat", values="cat", aggfunc="count"
            )

        wide.columns = [
            f"{spec['prefix']}_{str(c).strip().lower().replace(' ', '_')}_gh"
            for c in wide.columns
        ]
        key_name = "date" if grain == "daily" else "ts_hour"
        out[grain] = wide.reset_index().rename(columns={"key": key_name})

    return out


def _daily_date(ts: pd.Series, tz: str, instant: bool) -> pd.Series:
    if instant:
        return ts.dt.tz_convert(tz).dt.tz_localize(None).dt.normalize()
    # T00:00:00Z / bare-date rows are a calendar label, not a real instant --
    # take the UTC date exactly as written rather than shifting it.
    return ts.dt.tz_localize(None).dt.normalize()


def parse_daily_passthrough(dataset: str, paths: Iterable[Path], tz: str) -> pd.DataFrame:
    spec = DAILY_PASSTHROUGH_SPECS[dataset]
    df = _load(paths)
    if df.empty:
        return pd.DataFrame()

    out = pd.DataFrame({"date": _daily_date(df["ts"], tz, spec["instant"])})
    numeric_cols: list[str] = []
    text_cols: list[str] = []
    for src, dst in spec["columns"].items():
        if src not in df.columns:
            continue
        numeric = pd.to_numeric(df[src], errors="coerce")
        if numeric.notna().sum() >= df[src].notna().sum() * 0.5:
            out[dst] = numeric
            numeric_cols.append(dst)
        else:
            out[dst] = df[src].astype("string")
            text_cols.append(dst)

    out = out.dropna(subset=["date"])
    if out.empty or not (numeric_cols or text_cols):
        return pd.DataFrame()

    agg = {c: "mean" for c in numeric_cols} | {c: "first" for c in text_cols}
    return out.groupby("date", as_index=False).agg(agg).sort_values("date")


def parse_event(dataset: str, paths: Iterable[Path]) -> pd.DataFrame:
    """Pass through an event-grain file as-is (sleep scores, personal records).

    Unlike the rest of this module these files are keyed on their own event
    timestamp column (``score_time``, ``achieve_time``, ...), not a shared
    ``timestamp`` column, so this skips ``_load``'s ts-parsing rather than
    forcing a fake one.

    Not downsampled or joined into gold -- these are queryable in silver but
    a daily rollup needs a real decision about how they interact with the
    existing sleep/exercise pipelines. See CLAUDE.md.
    """
    frames = [df for df in (_read_one(Path(p)) for p in paths) if not df.empty]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
