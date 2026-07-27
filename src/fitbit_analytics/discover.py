"""Walk a Takeout export and classify every file into a known dataset.

Fitbit's export schema drifts between account types, device generations and
export dates. Rather than assume a layout, we catalog what is actually present
and let the ingest layer work from that manifest. Anything unrecognised lands
in the ``unclassified`` bucket so it stays visible instead of silently ignored.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from .config import Config


@dataclass(frozen=True)
class DatasetSpec:
    name: str
    pattern: re.Pattern[str]
    fmt: str  # "json" | "csv"
    grain: str  # "intraday" | "daily" | "event"
    notes: str = ""


def _p(rx: str) -> re.Pattern[str]:
    return re.compile(rx, re.IGNORECASE)


# Ordered: first match wins, so put specific patterns before general ones.
DATASET_SPECS: list[DatasetSpec] = [
    # --- Global Export Data, minute-grain JSON -----------------------
    DatasetSpec("resting_heart_rate", _p(r"resting_heart_rate-\d{4}-\d{2}-\d{2}\.json$"),
                "json", "daily", "one value per day, with an error estimate"),
    DatasetSpec("heart_rate", _p(r"heart_rate-\d{4}-\d{2}-\d{2}\.json$"),
                "json", "intraday", "value.bpm + value.confidence, ~5s cadence"),
    DatasetSpec("steps", _p(r"steps-\d{4}-\d{2}-\d{2}\.json$"), "json", "intraday"),
    DatasetSpec("calories", _p(r"calories-\d{4}-\d{2}-\d{2}\.json$"), "json", "intraday"),
    DatasetSpec("distance", _p(r"distance-\d{4}-\d{2}-\d{2}\.json$"), "json", "intraday",
                "centimetres per minute"),
    DatasetSpec("altitude", _p(r"altitude-\d{4}-\d{2}-\d{2}\.json$"), "json", "intraday"),
    DatasetSpec("sedentary_minutes", _p(r"sedentary_minutes-\d{4}-\d{2}-\d{2}\.json$"),
                "json", "intraday"),
    DatasetSpec("lightly_active_minutes", _p(r"lightly_active_minutes-\d{4}-\d{2}-\d{2}\.json$"),
                "json", "intraday"),
    DatasetSpec("moderately_active_minutes",
                _p(r"moderately_active_minutes-\d{4}-\d{2}-\d{2}\.json$"), "json", "intraday"),
    DatasetSpec("very_active_minutes", _p(r"very_active_minutes-\d{4}-\d{2}-\d{2}\.json$"),
                "json", "intraday"),
    DatasetSpec("sleep", _p(r"sleep-\d{4}-\d{2}-\d{2}\.json$"), "json", "event",
                "one record per sleep log, with stage summary and detail"),
    DatasetSpec("exercise", _p(r"exercise-\d+\.json$"), "json", "event"),

    # --- CSV feature exports -----------------------------------------
    DatasetSpec("hrv_daily", _p(r"Daily Heart Rate Variability Summary.*\.csv$"), "csv", "daily"),
    DatasetSpec("hrv_details", _p(r"Heart Rate Variability Details.*\.csv$"), "csv", "intraday"),
    DatasetSpec("respiratory_rate", _p(r"Daily Respiratory Rate Summary.*\.csv$"), "csv", "daily"),
    DatasetSpec("spo2_daily", _p(r"Daily SpO2.*\.csv$"), "csv", "daily"),
    DatasetSpec("spo2_intraday", _p(r"Minute SpO2.*\.csv$"), "csv", "intraday"),
    DatasetSpec("temperature_computed", _p(r"Computed Temperature.*\.csv$"), "csv", "daily"),
    DatasetSpec("temperature_device", _p(r"Device Temperature.*\.csv$"), "csv", "intraday"),
    DatasetSpec("sleep_score", _p(r"sleep_score\.csv$"), "csv", "daily"),
    DatasetSpec("stress_score", _p(r"Stress Score\.csv$"), "csv", "daily"),
    DatasetSpec("readiness", _p(r"Daily Readiness Score.*\.csv$"), "csv", "daily"),
    DatasetSpec("azm", _p(r"Active Zone Minutes.*\.csv$"), "csv", "intraday"),
]

# Files we know about but have no analytical use for — excluded from the
# unclassified warning so it stays a signal rather than noise.
IGNORE_PATTERNS = [
    _p(r"archive_browser\.html$"),
    _p(r"\.pdf$"),
    _p(r"badge.*\.json$"),
    _p(r"trophy.*\.json$"),
    _p(r"social.*\.json$"),
    _p(r"profile\.(json|csv)$"),
    _p(r"^\..*"),  # dotfiles
]


def classify(path: Path) -> tuple[str, str, str]:
    """Return (dataset, fmt, grain) for a file path."""
    name = path.name
    for spec in DATASET_SPECS:
        if spec.pattern.search(name):
            return spec.name, spec.fmt, spec.grain
    for rx in IGNORE_PATTERNS:
        if rx.search(name):
            return "ignored", path.suffix.lstrip("."), "none"
    return "unclassified", path.suffix.lstrip("."), "unknown"


def _date_from_name(name: str) -> str | None:
    m = re.search(r"(\d{4}-\d{2}-\d{2})", name)
    return m.group(1) if m else None


def build_catalog(cfg: Config) -> pd.DataFrame:
    """Walk takeout_root and return a manifest of every file found."""
    root = cfg.takeout_root
    if not root.exists():
        raise FileNotFoundError(
            f"takeout_root does not exist: {root}\n"
            "Unzip your Takeout archive and point config.local.yaml at the folder "
            "containing 'Fitbit'."
        )

    rows = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        dataset, fmt, grain = classify(path)
        rows.append(
            {
                "dataset": dataset,
                "format": fmt,
                "grain": grain,
                "path": str(path),
                "relpath": str(path.relative_to(root)),
                "file_date": _date_from_name(path.name),
                "size_bytes": path.stat().st_size,
            }
        )

    if not rows:
        raise ValueError(f"No files found under {root}")

    df = pd.DataFrame(rows)
    df["file_date"] = pd.to_datetime(df["file_date"], errors="coerce")
    return df


def summarize(catalog: pd.DataFrame) -> pd.DataFrame:
    """One row per dataset: file count, size, and observed date range."""
    g = (
        catalog.groupby("dataset")
        .agg(
            files=("path", "count"),
            size_mb=("size_bytes", lambda s: round(s.sum() / 1_048_576, 1)),
            grain=("grain", "first"),
            first_date=("file_date", "min"),
            last_date=("file_date", "max"),
        )
        .reset_index()
        .sort_values("size_mb", ascending=False)
    )
    return g


def run(cfg: Config) -> pd.DataFrame:
    cfg.ensure_dirs()
    catalog = build_catalog(cfg)
    catalog.to_parquet(cfg.catalog_path, index=False)

    summary = summarize(catalog)
    known = summary[~summary["dataset"].isin(["ignored", "unclassified"])]
    total_mb = round(catalog["size_bytes"].sum() / 1_048_576, 1)

    print(f"\nCataloged {len(catalog):,} files ({total_mb:,} MB) under {cfg.takeout_root}\n")
    if not known.empty:
        print(known.to_string(index=False))

    unknown = catalog[catalog["dataset"] == "unclassified"]
    if not unknown.empty:
        by_ext = unknown["relpath"].str.rsplit(".", n=1).str[-1].value_counts()
        print(f"\n{len(unknown):,} unclassified files. Extensions: {dict(by_ext)}")
        print("Sample:")
        for p in unknown["relpath"].head(10):
            print(f"  {p}")
        print("\nIf any of these look useful, add a DatasetSpec in discover.py.")

    print(f"\nCatalog written to {cfg.catalog_path}")
    return catalog
