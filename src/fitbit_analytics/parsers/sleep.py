"""Parse Fitbit sleep logs into one tidy row per sleep session.

Fitbit writes two shapes depending on whether the device resolved sleep stages:

* ``type == "stages"``  -> levels.summary has deep / light / rem / wake
* ``type == "classic"`` -> levels.summary has asleep / restless / awake

Both are normalised to the same columns; stage columns are null for classic
logs so downstream aggregation can distinguish "no stages recorded" from zero.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .common import read_json_records

STAGE_KEYS = ("deep", "light", "rem", "wake")
CLASSIC_KEYS = ("asleep", "restless", "awake")


def _summary_minutes(levels: dict[str, Any], key: str) -> float | None:
    summary = (levels or {}).get("summary") or {}
    entry = summary.get(key)
    if isinstance(entry, dict) and "minutes" in entry:
        return float(entry["minutes"])
    return None


def _flatten_log(rec: dict[str, Any]) -> dict[str, Any]:
    levels = rec.get("levels") or {}
    row: dict[str, Any] = {
        "log_id": rec.get("logId"),
        "date_of_sleep": rec.get("dateOfSleep"),
        "start_time": rec.get("startTime"),
        "end_time": rec.get("endTime"),
        "duration_ms": rec.get("duration"),
        "minutes_asleep": rec.get("minutesAsleep"),
        "minutes_awake": rec.get("minutesAwake"),
        "minutes_to_fall_asleep": rec.get("minutesToFallAsleep"),
        "time_in_bed": rec.get("timeInBed"),
        "efficiency": rec.get("efficiency"),
        "sleep_type": rec.get("type"),
        "is_main_sleep": rec.get("mainSleep"),
        "info_code": rec.get("infoCode"),
    }
    for key in STAGE_KEYS:
        row[f"minutes_{key}"] = _summary_minutes(levels, key)
    for key in CLASSIC_KEYS:
        row[f"minutes_classic_{key}"] = _summary_minutes(levels, key)
    return row


def _decimal_hour(ts: pd.Series) -> pd.Series:
    """Clock time as a decimal hour, shifted so late-night values are negative.

    A 23:30 bedtime and a 00:30 bedtime are half an hour apart, not 23 hours.
    Mapping anything after noon to a negative offset keeps ordinary bedtimes
    contiguous around zero so a standard deviation is meaningful.
    """
    hours = ts.dt.hour + ts.dt.minute / 60.0
    return np.where(hours >= 12, hours - 24.0, hours)


def parse(paths: Iterable[Path]) -> pd.DataFrame:
    records: list[dict[str, Any]] = []
    for path in paths:
        for rec in read_json_records(Path(path)):
            records.append(_flatten_log(rec))

    if not records:
        return pd.DataFrame()

    df = pd.DataFrame(records)
    df["date_of_sleep"] = pd.to_datetime(df["date_of_sleep"], errors="coerce")
    for col in ("start_time", "end_time"):
        df[col] = pd.to_datetime(df[col], errors="coerce", format="mixed")

    numeric = [
        "duration_ms", "minutes_asleep", "minutes_awake", "minutes_to_fall_asleep",
        "time_in_bed", "efficiency",
        *[f"minutes_{k}" for k in STAGE_KEYS],
        *[f"minutes_classic_{k}" for k in CLASSIC_KEYS],
    ]
    for col in numeric:
        df[col] = pd.to_numeric(df.get(col), errors="coerce")

    df = df.dropna(subset=["date_of_sleep", "start_time"])
    df = df.drop_duplicates(subset=["log_id"]) if "log_id" in df else df

    # Derived timing features
    df["bedtime_hour"] = _decimal_hour(df["start_time"])
    df["waketime_hour"] = df["end_time"].dt.hour + df["end_time"].dt.minute / 60.0
    midpoint = df["start_time"] + (df["end_time"] - df["start_time"]) / 2
    df["midpoint_hour"] = _decimal_hour(midpoint)

    # Stage shares, only meaningful when stages were recorded.
    stage_total = df[[f"minutes_{k}" for k in STAGE_KEYS]].sum(axis=1, min_count=1)
    for key in STAGE_KEYS:
        df[f"pct_{key}"] = np.where(
            stage_total > 0, df[f"minutes_{key}"] / stage_total * 100, np.nan
        )

    df["date"] = df["date_of_sleep"].dt.normalize()
    return df.sort_values("start_time").reset_index(drop=True)


def to_daily(sessions: pd.DataFrame) -> pd.DataFrame:
    """Collapse sessions to one row per calendar day.

    Naps are folded into the daily totals, but timing features come from the
    main sleep only — a 20 minute afternoon nap should not move your midpoint.
    """
    if sessions.empty:
        return pd.DataFrame()

    totals = (
        sessions.groupby("date")
        .agg(
            sleep_minutes=("minutes_asleep", "sum"),
            time_in_bed=("time_in_bed", "sum"),
            awake_minutes=("minutes_awake", "sum"),
            minutes_deep=("minutes_deep", "sum"),
            minutes_light=("minutes_light", "sum"),
            minutes_rem=("minutes_rem", "sum"),
            minutes_wake=("minutes_wake", "sum"),
            sleep_sessions=("log_id", "count"),
        )
        .reset_index()
    )

    main = sessions[sessions["is_main_sleep"].fillna(False).astype(bool)]
    if main.empty:  # fall back to the longest session of each day
        main = sessions.sort_values("minutes_asleep").groupby("date", as_index=False).tail(1)

    timing = (
        main.sort_values("minutes_asleep")
        .groupby("date", as_index=False)
        .tail(1)[["date", "bedtime_hour", "waketime_hour", "midpoint_hour",
                  "efficiency", "minutes_to_fall_asleep", "pct_deep", "pct_rem"]]
    )

    daily = totals.merge(timing, on="date", how="left")
    daily["sleep_hours"] = daily["sleep_minutes"] / 60.0
    return daily
