"""Shared helpers for reading Fitbit's several timestamp and value conventions."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pandas as pd

# Global Export Data uses US-style short dates: "01/15/24 13:45:00".
# The CSV feature exports use ISO. Try both, in that order.
_TS_FORMATS = ("%m/%d/%y %H:%M:%S", "%m/%d/%Y %H:%M:%S", "ISO8601")


def parse_timestamps(s: pd.Series) -> pd.Series:
    """Parse a Fitbit timestamp column, trying each known convention."""
    s = s.astype("string")
    for fmt in _TS_FORMATS:
        try:
            if fmt == "ISO8601":
                out = pd.to_datetime(s, format="ISO8601", errors="raise")
            else:
                out = pd.to_datetime(s, format=fmt, errors="raise")
            return out
        except (ValueError, TypeError):
            continue
    # Last resort: let pandas infer per-element, tolerating a mixed file.
    return pd.to_datetime(s, errors="coerce", format="mixed")


def read_json_records(path: Path) -> list[dict[str, Any]]:
    """Read one Fitbit JSON file. Returns [] for empty or malformed files."""
    try:
        text = path.read_text(encoding="utf-8-sig").strip()
    except (OSError, UnicodeDecodeError):
        return []
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        return [data]
    return [r for r in data if isinstance(r, dict)]


def load_datetime_value_files(paths: Iterable[Path], value_name: str) -> pd.DataFrame:
    """Parse the ``[{"dateTime": ..., "value": ...}]`` shape.

    ``value`` may be a scalar (steps, calories) or a nested object
    (heart_rate -> {bpm, confidence}). Nested keys are flattened to columns
    prefixed with ``value_name``.
    """
    frames: list[pd.DataFrame] = []
    for path in paths:
        records = read_json_records(Path(path))
        if not records:
            continue
        df = pd.json_normalize(records, sep="_")
        if "dateTime" not in df.columns:
            continue
        frames.append(df)

    if not frames:
        return pd.DataFrame(columns=["ts", value_name])

    df = pd.concat(frames, ignore_index=True)
    df["ts"] = parse_timestamps(df["dateTime"])
    df = df.drop(columns=["dateTime"])

    # Scalar payload: a single "value" column.
    if "value" in df.columns:
        df = df.rename(columns={"value": value_name})
        df[value_name] = pd.to_numeric(df[value_name], errors="coerce")
    else:
        # Nested payload: value_bpm, value_confidence, ...
        rename = {c: f"{value_name}_{c.removeprefix('value_')}"
                  for c in df.columns if c.startswith("value_")}
        df = df.rename(columns=rename)
        for c in rename.values():
            df[c] = pd.to_numeric(df[c], errors="coerce")

    df = df.dropna(subset=["ts"]).drop_duplicates(subset=["ts"]).sort_values("ts")
    return df.reset_index(drop=True)


def read_csv_files(paths: Iterable[Path]) -> pd.DataFrame:
    """Concatenate CSV exports that share a schema, tolerating minor drift."""
    frames = []
    for path in paths:
        try:
            df = pd.read_csv(path)
        except (OSError, pd.errors.ParserError, pd.errors.EmptyDataError, UnicodeDecodeError):
            continue
        if df.empty:
            continue
        df.columns = [normalize_column(c) for c in df.columns]
        frames.append(df)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def normalize_column(name: str) -> str:
    """``Daily SpO2 (%)`` -> ``daily_spo2_pct``."""
    out = name.strip().lower()
    out = out.replace("%", "pct").replace("(", " ").replace(")", " ")
    out = "".join(ch if ch.isalnum() else "_" for ch in out)
    while "__" in out:
        out = out.replace("__", "_")
    return out.strip("_")


def find_timestamp_column(df: pd.DataFrame) -> str | None:
    """Best-effort identification of the timestamp column in a CSV export."""
    candidates = ["timestamp", "date", "datetime", "date_time", "sleep_start",
                  "start_time", "recorded_time", "day"]
    for c in candidates:
        if c in df.columns:
            return c
    for c in df.columns:
        if "date" in c or "time" in c:
            return c
    return None
