"""Fetch from the Google Health API, downsample, and persist incrementally.

This is the one part of the pipeline that isn't full-refresh. Everything
else (ingest, transform, warehouse-load) recomputes from a complete source
of truth every run, which is simple and correct as long as the source
actually has full history. The live API doesn't: each run only asks for
data since the last watermark, so results here are *appended* to whatever
was already on disk, deduplicated on date, not overwritten. Worth being
explicit about since it's a real deviation from how the rest of this
project works, not an oversight.

Output lands in the silver layer under a `sync_` prefix
(`sync_steps_daily.parquet`, etc.) rather than merging into the `gh_*`
tables the Takeout parser produces for the same concepts. Reconciling
"live sync says 7401 steps today, Takeout backfill said something else
during file overlap" is a real decision deferred here the same way the old
vs. new Takeout format columns were -- not built blind. `fitbit
warehouse-load` picks these up automatically since it just globs
`data/silver/*.parquet`; they are not (yet) wired into
`transform.DAILY_TABLES` or the dbt staging layer for the same reason.

Heart rate is downsampled to daily aggregate stats during fetch, never
persisted raw -- confirmed live (see CLAUDE.md) that a week of raw heart
rate is ~283K points; the whole point of this module existing separately
from a naive "just save what the API returns" script is to not do that.
"""

from __future__ import annotations

import traceback
from datetime import datetime, timedelta, timezone

import pandas as pd
import sqlalchemy

from . import client
from .auth import get_access_token

DEFAULT_LOOKBACK = timedelta(days=3)  # bootstrap window when no watermark exists yet
OVERLAP = timedelta(hours=6)  # re-fetch a little before the watermark; dedup handles overlap


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ensure_watermark_table(engine: sqlalchemy.Engine) -> None:
    with engine.begin() as conn:
        conn.execute(sqlalchemy.text("CREATE SCHEMA IF NOT EXISTS meta"))
        conn.execute(
            sqlalchemy.text(
                """
                CREATE TABLE IF NOT EXISTS meta.sync_watermark (
                    data_type TEXT PRIMARY KEY,
                    last_synced_utc TIMESTAMPTZ NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
        )


def get_watermark(engine: sqlalchemy.Engine, data_type: str) -> str:
    with engine.begin() as conn:
        row = conn.execute(
            sqlalchemy.text(
                "SELECT last_synced_utc FROM meta.sync_watermark WHERE data_type = :dt"
            ),
            {"dt": data_type},
        ).fetchone()
    if row is None:
        start = datetime.now(timezone.utc) - DEFAULT_LOOKBACK
        return start.strftime("%Y-%m-%dT%H:%M:%SZ")
    return (row[0] - OVERLAP).strftime("%Y-%m-%dT%H:%M:%SZ")


def set_watermark(engine: sqlalchemy.Engine, data_type: str, ts_iso: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            sqlalchemy.text(
                """
                INSERT INTO meta.sync_watermark (data_type, last_synced_utc, updated_at)
                VALUES (:dt, :ts, now())
                ON CONFLICT (data_type)
                DO UPDATE SET last_synced_utc = :ts, updated_at = now()
                """
            ),
            {"dt": data_type, "ts": ts_iso},
        )


def _daily_sum(df: pd.DataFrame, value_col: str, out_col: str) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=["date", out_col])
    df = df.copy()
    df[value_col] = pd.to_numeric(df[value_col], errors="coerce")
    out = df.groupby("local_date", as_index=False)[value_col].sum()
    return out.rename(columns={"local_date": "date", value_col: out_col})


def _heart_rate_daily(df: pd.DataFrame) -> pd.DataFrame:
    """Downsample to daily stats -- see module docstring. Never persist raw."""
    if df.empty:
        return pd.DataFrame(columns=["date", "hr_mean", "hr_min", "hr_max", "hr_std", "hr_samples"])
    df = df.copy()
    df["beatsPerMinute"] = pd.to_numeric(df["beatsPerMinute"], errors="coerce")
    agg = df.groupby("local_date")["beatsPerMinute"].agg(
        hr_mean="mean", hr_min="min", hr_max="max", hr_std="std", hr_samples="count"
    )
    return agg.reset_index().rename(columns={"local_date": "date"})


def _sleep_sessions(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=["start_time_utc", "end_time_utc", "local_date", "type"])
    keep = [c for c in ("start_time_utc", "end_time_utc", "local_date", "type") if c in df.columns]
    return df[keep].copy()


# (client data type, transform fn, output silver table name, date-keyed dedup column)
_JOBS = [
    ("steps", lambda df: _daily_sum(df, "count", "steps"), "sync_steps_daily", "date"),
    (
        "distance",
        lambda df: _daily_sum(df, "millimeters", "distance_mm"),
        "sync_distance_daily",
        "date",
    ),
    (
        "active-energy-burned",
        lambda df: _daily_sum(df, "kcal", "active_energy_kcal"),
        "sync_active_energy_daily",
        "date",
    ),
    ("heart-rate", _heart_rate_daily, "sync_heart_rate_daily", "date"),
    ("sleep", _sleep_sessions, "sync_sleep_sessions", "start_time_utc"),
]


def _merge_and_write(path, new_df: pd.DataFrame, dedup_col: str) -> int:
    if path.exists():
        existing = pd.read_parquet(path)
        combined = pd.concat([existing, new_df], ignore_index=True)
    else:
        combined = new_df
    if combined.empty:
        return 0
    combined = combined.drop_duplicates(subset=[dedup_col], keep="last").sort_values(dedup_col)
    combined.to_parquet(path, index=False)
    return len(combined)


def run(cfg) -> dict[str, int]:
    from .. import warehouse  # local import: avoid a hard dependency for pure-fetch use

    engine = sqlalchemy.create_engine(warehouse._with_psycopg_dialect(warehouse.load_pg_url()))
    _ensure_watermark_table(engine)

    # Deliberately outside the per-job try/except below and not caught: an
    # expired refresh token (the 7-day Testing-mode limit, see CLAUDE.md)
    # must fail the whole job loudly right here, not get silently absorbed
    # by per-dataset error isolation further down.
    token = get_access_token()

    counts: dict[str, int] = {}
    now = _now_iso()

    print("\nSyncing from the Google Health API...\n")
    for data_type, transform_fn, table_name, dedup_col in _JOBS:
        start = get_watermark(engine, data_type)
        try:
            points = client.fetch_data_points(data_type, start, now, token)
            raw_df = client.to_dataframe(points, data_type)
            out_df = transform_fn(raw_df)
            path = cfg.silver / f"{table_name}.parquet"
            total_rows = _merge_and_write(path, out_df, dedup_col)
            counts[table_name] = total_rows
            set_watermark(engine, data_type, now)
            print(f"  {table_name:<28} +{len(out_df):>5} new rows fetched, {total_rows:>6} total")
        except Exception:
            # Per-dataset failure isolation, same convention as ingest.py: a
            # bad response for one data type shouldn't cost the others their
            # sync, or their watermark update.
            print(f"  {table_name:<28} FAILED")
            traceback.print_exc(limit=2)

    engine.dispose()
    print(f"\nSync complete: {len(counts)} tables updated.")
    return counts
