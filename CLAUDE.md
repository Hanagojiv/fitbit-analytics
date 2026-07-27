# CLAUDE.md

Context for continuing work on this repo. Read `README.md` first for
architecture and rationale; this file covers state, conventions, and what to do
next.

## What this is

A local ELT pipeline turning a Google Takeout Fitbit export into a daily fact
table, an HTML report, and a set of rule-based observations. Owner is a data
engineer; assume familiarity with parquet, DuckDB, and medallion layering.
Explain decisions, not syntax.

## Current state

The local pipeline (discover/ingest/transform/report) is built, passing 15
tests against synthetic fixtures, and **has now been run once against the
real export** (`fitbit discover` only, on 2026-07-27). Ingest/transform/report
have not yet been run on real data — do that next, but expect it to need
parser work first (see below).

- 15 tests passing, `ruff check src tests` clean
- Pipeline verified end to end on synthetic fixtures: 270 days, 24 silver
  tables, 62 gold columns
- 22 `DatasetSpec`s registered in `discover.py`, all matching against the old
  "Global Export Data" file layout only

Real export stats from `fitbit discover` (`data/catalog.parquet`): 1,082
files, 1,444.7 MB, under `data_raw/Takeout/Google Health/` (gitignored,
unzipped from `~/Downloads/takeout-20260727T111240Z-1-001.zip`). 293 files
matched a known `DatasetSpec`; **789 are unclassified**.

## The real export uses a different, newer layout than this pipeline was built for

Fitbit's export is now branded **"Google Health"**, not "Fitbit", and ships
two overlapping data trees:

1. `Global Export Data/` — the old layout, still present, still what the 22
   existing `DatasetSpec`s match. This is why discovery finds real data at
   all.
2. `Physical Activity_GoogleData/` (311 files, 96MB) and
   `Health Fitness Data_GoogleData/` (89 files, 1.19GB) — a **new, richer**
   per-source CSV format, one file per dataset per day, columns like
   `timestamp, <value>, data source`. `data source` is the device/app that
   wrote the row — the Fitbit Air shows up as **"Radiance"** (its internal
   codename), vs. `"Fitbit App"` for manually-entered or phone-derived data.
   This is genuinely a superset of the old format: it includes GPS location,
   micro-motion, swim lengths, cardio load, live pace, and speed, none of
   which the old format carries. See the full unique dataset-name list by
   running the `top2` groupby in the discovery session transcript, or just
   `ls "data_raw/Takeout/Google Health/Physical Activity_GoogleData" | sed -E
   's/_[0-9]{4}-[0-9]{2}-[0-9]{2}\.csv$//' | sort -u`.

`Biometrics/` (231 files, ~0MB total) is template/placeholder CSVs Fitbit
generates regardless of whether you use the feature (e.g. `Glucose
200706.csv` with header only) — noise, not signal. Add a pattern to
`IGNORE_PATTERNS` rather than a `DatasetSpec`.

`UserActivityProbabilities_*.csv` (in `Health Fitness Data_GoogleData/`) is
~45MB **per day** — sub-2-second-cadence activity-classification
probabilities. This is the intraday-aggregation problem `intraday.py` already
solves for heart rate/steps, applied to a new, much higher-volume source.
Needs the same treatment: downsample on read, never persist raw.

`gps_location_*.csv` contains real lat/lon/altitude. Flagged in README as
needing an explicit privacy decision before any cloud sync touches it — do
not add this to a default sync path without that decision being made first.

## Immediate next step

Extend `discover.py`'s `DATASET_SPECS` to also match the
`Physical Activity_GoogleData` / `Health Fitness Data_GoogleData` filename
shape (`<dataset_name>_<date>.csv` or `<dataset_name>.csv` for
non-daily-partitioned ones like `weight.csv`, `height.csv`,
`daily_readiness.csv`). These files have consistent, self-describing headers
(see samples above) which makes them easier to parse than the old CSV
exports' drifting column names — likely a single generic parser keyed off the
filename-derived dataset name, rather than one bespoke parser per dataset.

Then re-run `fitbit discover` to confirm the unclassified count drops, add
`IGNORE_PATTERNS` for `Biometrics/`-style placeholder noise, and only then
move on to `fitbit ingest`.

Set `intraday_file_limit: 20` in `config.local.yaml` while iterating —
already set in `config.local.yaml` (gitignored, present locally).

## Known unknowns about the real data

- Device is a **Fitbit Air**, confirmed as `data source: Radiance` in the new
  CSV format. It does write HRV, SpO2, temperature, and readiness data — all
  present in the real export.
- Export vintage is 2026-07, in the new "Google Health" format. Column names
  in the old-format CSV feature exports (`csv_features.py`,
  `VALUE_ALIASES`) have not yet been checked against real files — do that
  when running `fitbit ingest` for the first time.
- Timezone handling is deliberately minimal. New-format timestamps are UTC
  ISO 8601 with no offset column on most files (an exception:
  `UserActivityProbabilities` includes `utc_offset`). Old-format Global
  Export Data timestamps are device-local. These two conventions will need to
  be reconciled explicitly when both feed the same `daily_facts` table.

## Conventions

- Trailing rolling windows only, never centred. Every value must use only
  information available on that day.
- Missing days stay on the date spine as nulls. Never drop them.
- Per-dataset failure isolation: one malformed export must not sink the run.
  The blind excepts in `ingest.py` and `flags.py` are deliberate.
- Tests assert that planted signals are **recovered**, not merely that code
  runs. When adding analytics, plant the effect in `make_fixtures.py` and
  assert on it.
- New correlation hypotheses go into `relationships.HYPOTHESES` *before*
  looking at the result. The list being fixed is the point.
- `data/`, `reports/`, `fixtures/` and `config.local.yaml` are gitignored.
  Never commit health data.

## Tone for health-adjacent output

`analytics/flags.py` produces observations, not findings. Thresholds trace to
public health references (WHO activity, AASM sleep) or to the user's own
baseline. A consumer wrist device is a reasonable trend instrument and a poor
absolute one. Do not add rules that diagnose, and keep the `discuss` tier
meaning "worth raising with a clinician", not "you have X".

## Ideas not yet built

- Exercise session parsing (`exercise-*.json` is cataloged but unparsed)
- Intraday HR zone-time and workout heart rate curves
- Change-point detection on resting HR rather than threshold rules
- Seasonality decomposition once >1 year of data is confirmed present
- A `fitbit export` command emitting a clinician-friendly one-page PDF
