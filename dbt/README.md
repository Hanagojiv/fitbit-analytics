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

## The marts layer

`models/marts/fct_daily_facts.sql` + `macros/daily_facts.sql` reimplement
`transform.py`'s `build_daily_facts`: a continuous date spine, every daily
table left-joined in, with automatic column-collision suffixing (two
sources can legitimately produce the same column name — see CLAUDE.md
"Column collisions"). Built and validated live against Supabase on
2026-07-27: 44 tables joined into 117 columns × 60 days, cross-checked
row-for-row against `data/gold/daily_facts.parquet` and matched exactly.
`dbt test` passes (`not_null` + `unique` on `date`).

The macro uses `adapter.get_columns_in_relation()` at compile time rather
than a hand-written column list per table, so it stays correct as staging
models' underlying schemas change. It does need `models/staging/*.sql` to
exist for every entry in its static model list (`daily_facts_models()`) --
if a staging model is added or removed, that list needs regenerating too
(see the macro's own comment for how it was generated).

One real gap found this way, not guessed: `stg_readiness.sql` originally
referenced `raw.readiness`, which never gets created because this account's
export has no old-format `Daily Readiness Score.csv` (only the new-format
`gh_daily_readiness` has real data — matches what CLAUDE.md already
documented about the two export formats). Removed the model and its source
entry rather than ship a staging model that always errors for this dataset.

`gold.daily_facts` (from `fitbit warehouse-load`) and `dbt_marts.fct_daily_facts`
now contain the same data by two different paths -- the former a direct
parquet upload, the latter dbt's own computation. Worth deciding which the
dashboard should actually read from; leaning `dbt_marts.fct_daily_facts`
since it's the one that's actually tested and rebuildable, but `gold.*`
hasn't been removed yet in case that's wrong.

## Running it

```bash
cd dbt
cp profiles.yml.example profiles.yml   # profiles.yml itself is gitignored
export SUPABASE_DB_HOST=...
export SUPABASE_DB_PASSWORD=...
dbt run --profiles-dir .
dbt test --profiles-dir .
```
