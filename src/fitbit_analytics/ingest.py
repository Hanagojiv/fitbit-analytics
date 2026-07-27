"""Drive every parser from the catalog and write the silver layer.

Each dataset becomes one parquet file at its natural grain. Failures are
isolated per dataset: a malformed sleep export should not cost you the heart
rate pipeline.
"""

from __future__ import annotations

import traceback
from pathlib import Path

import pandas as pd

from .config import Config
from .parsers import csv_features, intraday, sleep

INTRADAY_DATASETS = [
    "heart_rate", "steps", "calories", "distance", "altitude",
    "sedentary_minutes", "lightly_active_minutes",
    "moderately_active_minutes", "very_active_minutes",
]

CSV_DAILY_DATASETS = [
    "hrv_daily", "respiratory_rate", "spo2_daily",
    "temperature_computed", "sleep_score", "stress_score", "readiness",
]


def _paths(catalog: pd.DataFrame, dataset: str, limit: int | None = None) -> list[Path]:
    sub = catalog[catalog["dataset"] == dataset].sort_values("path")
    if limit is not None:
        sub = sub.head(limit)
    return [Path(p) for p in sub["path"]]


def _write(df: pd.DataFrame, path: Path, label: str) -> int:
    if df is None or df.empty:
        print(f"  {label:<28} no rows")
        return 0
    df.to_parquet(path, index=False)
    print(f"  {label:<28} {len(df):>8,} rows -> {path.name}")
    return len(df)


def run(cfg: Config, catalog: pd.DataFrame | None = None) -> dict[str, int]:
    cfg.ensure_dirs()
    if catalog is None:
        if not cfg.catalog_path.exists():
            raise FileNotFoundError("No catalog. Run `fitbit discover` first.")
        catalog = pd.read_parquet(cfg.catalog_path)

    counts: dict[str, int] = {}
    limit = cfg.intraday_file_limit

    print("\nIngesting to silver layer...\n")

    # --- resting heart rate (daily JSON) ------------------------------
    try:
        rhr = intraday.resting_hr_from_json(_paths(catalog, "resting_heart_rate"))
        counts["resting_heart_rate"] = _write(
            rhr, cfg.silver / "resting_heart_rate.parquet", "resting_heart_rate"
        )
    except Exception:
        print("  resting_heart_rate FAILED")
        traceback.print_exc(limit=2)

    # --- sleep sessions and daily rollup ------------------------------
    try:
        sessions = sleep.parse(_paths(catalog, "sleep"))
        counts["sleep_sessions"] = _write(
            sessions, cfg.silver / "sleep_sessions.parquet", "sleep_sessions"
        )
        if not sessions.empty:
            counts["sleep_daily"] = _write(
                sleep.to_daily(sessions), cfg.silver / "sleep_daily.parquet", "sleep_daily"
            )
    except Exception:
        print("  sleep FAILED")
        traceback.print_exc(limit=2)

    # --- intraday datasets --------------------------------------------
    for dataset in INTRADAY_DATASETS:
        paths = _paths(catalog, dataset, limit)
        if not paths:
            continue
        try:
            frames = intraday.parse_and_downsample(dataset, paths, cfg.intraday_grains)
            for grain, df in frames.items():
                name = f"{dataset}_{grain}"
                counts[name] = _write(df, cfg.silver / f"{name}.parquet", name)
        except Exception:
            print(f"  {dataset} FAILED")
            traceback.print_exc(limit=2)

    # --- wear coverage, derived from hourly heart rate -----------------
    hr_hourly_path = cfg.silver / "heart_rate_hourly.parquet"
    if hr_hourly_path.exists():
        try:
            cov = intraday.wear_coverage(pd.read_parquet(hr_hourly_path))
            counts["wear_coverage"] = _write(
                cov, cfg.silver / "wear_coverage.parquet", "wear_coverage"
            )
        except Exception:
            print("  wear_coverage FAILED")
            traceback.print_exc(limit=2)

    # --- CSV feature exports -------------------------------------------
    for dataset in CSV_DAILY_DATASETS:
        paths = _paths(catalog, dataset)
        if not paths:
            continue
        try:
            df = csv_features.parse(dataset, paths)
            if df.empty:
                cols = _peek_columns(paths[0])
                print(f"  {dataset:<28} matched no known columns; saw {cols}")
                continue
            counts[dataset] = _write(df, cfg.silver / f"{dataset}.parquet", dataset)
        except Exception:
            print(f"  {dataset} FAILED")
            traceback.print_exc(limit=2)

    azm_paths = _paths(catalog, "azm")
    if azm_paths:
        try:
            counts["azm"] = _write(
                csv_features.parse_azm(azm_paths), cfg.silver / "azm.parquet", "azm"
            )
        except Exception:
            print("  azm FAILED")
            traceback.print_exc(limit=2)

    total = sum(counts.values())
    print(f"\nSilver layer complete: {len(counts)} tables, {total:,} rows total.")
    return counts


def _peek_columns(path: Path) -> list[str]:
    """Show a file's real columns when alias matching fails, so specs can be fixed."""
    try:
        return list(pd.read_csv(path, nrows=1).columns)[:12]
    except Exception:
        return []
