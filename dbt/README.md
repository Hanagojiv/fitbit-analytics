# fitbit_analytics dbt project

## Current state

45 staging models, one per table in `transform.DAILY_TABLES` (the Python
pipeline's own list of "this is daily-grain and matters for the gold join" —
reused here rather than re-deciding it by hand). Each is a thin
`select * from {{ source('raw', '<table>') }}` — no typing or renaming yet.

Sources (`models/sources.yml`) point at the `raw` schema, populated by
`fitbit warehouse-load` (see `src/fitbit_analytics/warehouse.py`) from the
local pipeline's silver-layer parquet output. Run that before `dbt run`, or
the sources won't exist yet.

## Not yet built: the marts layer

`transform.py`'s `build_daily_facts` does a 45-way left join of every daily
table onto a continuous date spine, with runtime column-collision detection
(two tables can legitimately produce a column with the same name — see
CLAUDE.md's "Column collisions" section). Reimplementing that as static dbt
SQL means either hand-writing 45 join clauses with manually-verified column
lists (tedious and drifts out of sync the moment a staging model's source
schema changes), or using dbt's `adapter.get_columns_in_relation()` inside a
macro to generate the join dynamically at compile time, mirroring what
`transform.py` already does in Python.

Deliberately not built blind: this needs a live Postgres connection to
actually run `dbt run` against and iterate on real compile/runtime errors,
the same way `sync/client.py` was built by testing against the real Google
Health API rather than trusting its docs. Do this once `SUPABASE_DB_*` env
vars are set and `dbt run` (staging only) succeeds first.

Until then, `gold.daily_facts` in Postgres is populated directly by
`fitbit warehouse-load` from the already-computed local
`data/gold/daily_facts.parquet` — the dashboard has something to query today,
it's just not (yet) coming out of dbt.

## Running it

```bash
cd dbt
cp profiles.yml.example profiles.yml   # profiles.yml itself is gitignored
export SUPABASE_DB_HOST=...
export SUPABASE_DB_PASSWORD=...
dbt run --profiles-dir .
dbt test --profiles-dir .
```
