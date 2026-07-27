"""Load the local parquet layers into the Postgres warehouse (Supabase).

This is a bridge, not the final transform layer: it pushes every silver
table up as-is (``raw.<table>``) plus the already-computed gold table
(``gold.daily_facts``) so the warehouse is useful immediately. Once the dbt
project's marts replace the DuckDB join in `transform.py`, `gold.daily_facts`
here should become a dbt output instead of a raw upload -- see dbt/README.

Full-refresh (``if_exists="replace"``) on every run, matching how
`transform.py` already rebuilds `daily_facts` from scratch each time rather
than doing incremental upserts -- appropriate at this data volume (tens of
thousands of rows, not millions), and it means a bad local run can't
accumulate stale rows in Postgres that a fresh run should have superseded.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import sqlalchemy
import yaml

from .config import Config

DEFAULT_SECRETS_PATH = Path("secrets.local.yaml")


def load_pg_url(path: Path = DEFAULT_SECRETS_PATH) -> str:
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Add a `postgres:` block with `connection_string`."
        )
    raw = yaml.safe_load(path.read_text()) or {}
    pg = raw.get("postgres")
    if not pg or "connection_string" not in pg:
        raise ValueError(f"{path} has no `postgres.connection_string`.")
    return pg["connection_string"]


def _table_name(path: Path) -> str:
    return path.stem


def load_raw(cfg: Config, pg_url: str) -> dict[str, int]:
    engine = sqlalchemy.create_engine(pg_url)
    counts: dict[str, int] = {}

    with engine.begin() as conn:
        conn.execute(sqlalchemy.text("CREATE SCHEMA IF NOT EXISTS raw"))
        conn.execute(sqlalchemy.text("CREATE SCHEMA IF NOT EXISTS gold"))

    print("\nLoading silver layer into raw schema...\n")
    for path in sorted(cfg.silver.glob("*.parquet")):
        table = _table_name(path)
        df = pd.read_parquet(path)
        df.to_sql(table, engine, schema="raw", if_exists="replace", index=False)
        counts[f"raw.{table}"] = len(df)
        print(f"  raw.{table:<45} {len(df):>8,} rows")

    gold_path = cfg.gold / "daily_facts.parquet"
    if gold_path.exists():
        df = pd.read_parquet(gold_path)
        df.to_sql("daily_facts", engine, schema="gold", if_exists="replace", index=False)
        counts["gold.daily_facts"] = len(df)
        print(f"  gold.daily_facts{'':<32} {len(df):>8,} rows")

    engine.dispose()
    total = sum(counts.values())
    print(f"\nWarehouse load complete: {len(counts)} tables, {total:,} rows total.")
    return counts


def run(cfg: Config) -> dict[str, int]:
    pg_url = load_pg_url()
    return load_raw(cfg, pg_url)
