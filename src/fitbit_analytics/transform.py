"""Build the gold layer: one wide, analysis-ready row per calendar day.

Every daily silver table is left-joined onto a continuous date spine. The
spine matters — a missing day must appear as a row of nulls, not vanish, or
every rolling window downstream silently changes length.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd

from .config import Config

# Tables joined into daily_facts, in join order. Anything not listed stays in
# silver and can still be queried directly.
DAILY_TABLES = [
    "sleep_daily",
    "resting_heart_rate",
    "heart_rate_daily",
    "steps_daily",
    "calories_daily",
    "distance_daily",
    "very_active_minutes_daily",
    "moderately_active_minutes_daily",
    "lightly_active_minutes_daily",
    "sedentary_minutes_daily",
    "wear_coverage",
    "hrv_daily",
    "respiratory_rate",
    "spo2_daily",
    "temperature_computed",
    "sleep_score",
    "stress_score",
    "readiness",
    "azm",
    "vo2max",
    "hr_zone_minutes",
    # "Google Health" format (see parsers/google_health.py). "_gh" column
    # suffixes mark these as the new-format equivalent of a metric that may
    # already exist above from the old format; nothing here overwrites it.
    "gh_heart_rate_daily",
    "gh_steps_daily",
    "gh_calories_daily",
    "gh_active_energy_burned_daily",
    "gh_distance_daily",
    "gh_body_temperature_daily",
    "gh_oxygen_saturation_daily",
    "gh_speed_daily",
    "gh_cardio_load_daily",
    "gh_activity_level_daily",
    "gh_active_zone_minutes_daily",
    "gh_calories_in_heart_rate_zone_daily",
    "gh_time_in_heart_rate_zone_daily",
    "gh_daily_heart_rate_variability",
    "gh_heart_rate_variability",
    "gh_daily_oxygen_saturation",
    "gh_daily_readiness",
    "gh_daily_respiratory_rate",
    "gh_daily_resting_heart_rate",
    "gh_daily_sleep_temperature_derivations",
    "gh_height",
    "gh_weight",
    "gh_cardio_acute_chronic_workload_ratio",
    "gh_cardio_load_observed_interval",
]


def available_tables(cfg: Config) -> list[tuple[str, Path]]:
    found = []
    for name in DAILY_TABLES:
        path = cfg.silver / f"{name}.parquet"
        if path.exists():
            found.append((name, path))
    return found


def build_daily_facts(cfg: Config) -> pd.DataFrame:
    tables = available_tables(cfg)
    if not tables:
        raise FileNotFoundError("No daily silver tables found. Run `fitbit ingest` first.")

    con = duckdb.connect()
    for name, path in tables:
        con.execute(f"CREATE VIEW {name} AS SELECT * FROM read_parquet('{path.as_posix()}')")

    bounds = " UNION ALL ".join(
        f"SELECT min(date) AS lo, max(date) AS hi FROM {n}" for n, _ in tables
    )
    lo, hi = con.execute(
        f"SELECT min(lo), max(hi) FROM ({bounds})"
    ).fetchone()

    if lo is None:
        raise ValueError("Silver tables contain no dated rows.")

    con.execute(
        f"""
        CREATE VIEW spine AS
        SELECT CAST(UNNEST(generate_series(
            DATE '{lo:%Y-%m-%d}', DATE '{hi:%Y-%m-%d}', INTERVAL 1 DAY
        )) AS DATE) AS date
        """
    )

    # Column names are not guaranteed unique across tables -- as more
    # datasets land here (old format + "Google Health" format + future
    # sources), two tables producing the same column name is expected, not
    # a bug. Keep the first table's column under its plain name and
    # disambiguate every later collision with a table-name suffix, loudly,
    # rather than letting DuckDB silently pick one.
    select_parts = ["spine.date"]
    join_parts = []
    seen_columns: set[str] = set()
    for name, _ in tables:
        cols = [c for c in con.execute(f"DESCRIBE {name}").df()["column_name"] if c != "date"]
        for c in cols:
            if c in seen_columns:
                alias = f"{c}__{name}"
                print(f"  note: column '{c}' in '{name}' collides; joined as '{alias}'")
                select_parts.append(f"{name}.{c} AS {alias}")
                seen_columns.add(alias)
            else:
                select_parts.append(f"{name}.{c}")
                seen_columns.add(c)
        join_parts.append(f"LEFT JOIN {name} ON {name}.date = spine.date")

    sql = (
        "SELECT " + ", ".join(select_parts)
        + " FROM spine " + " ".join(join_parts)
        + " ORDER BY spine.date"
    )
    df = con.execute(sql).df()
    con.close()

    df["date"] = pd.to_datetime(df["date"])
    return _add_derived(df)


def _add_derived(df: pd.DataFrame) -> pd.DataFrame:
    """Columns that only make sense once everything is on one row."""
    df = df.copy()

    df["dow"] = df["date"].dt.day_name()
    df["is_weekend"] = df["date"].dt.dayofweek >= 5

    active_cols = [c for c in ("very_active_minutes", "moderately_active_minutes")
                   if c in df.columns]
    if active_cols:
        df["mvpa_minutes"] = df[active_cols].sum(axis=1, min_count=1)

    if {"sleep_minutes", "time_in_bed"} <= set(df.columns):
        df["sleep_efficiency_calc"] = (
            df["sleep_minutes"] / df["time_in_bed"].where(df["time_in_bed"] > 0) * 100
        )

    # Yesterday's load against today's recovery — the pairing most worth testing.
    for src, dst in (("steps", "steps_prev"),
                     ("mvpa_minutes", "mvpa_prev"),
                     ("sleep_hours", "sleep_hours_prev")):
        if src in df.columns:
            df[dst] = df[src].shift(1)

    if "wear_fraction" not in df.columns:
        df["wear_fraction"] = pd.NA
        df["is_full_day"] = pd.NA

    return df


def run(cfg: Config) -> pd.DataFrame:
    cfg.ensure_dirs()
    facts = build_daily_facts(cfg)
    out = cfg.gold / "daily_facts.parquet"
    facts.to_parquet(out, index=False)

    span = f"{facts['date'].min():%Y-%m-%d} to {facts['date'].max():%Y-%m-%d}"
    covered = facts.notna().sum(axis=1).gt(3).sum()
    print(f"\nGold: daily_facts   {len(facts):,} days ({span})")
    print(f"      {facts.shape[1]} columns, {covered:,} days with data")
    print(f"      -> {out}")
    return facts
