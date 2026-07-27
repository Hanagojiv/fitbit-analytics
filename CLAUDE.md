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

The full local pipeline (discover → ingest → transform → report) has now run
end to end against the **real** export, not just synthetic fixtures.

- 13 of 15 synthetic-fixture tests passing; 2 pre-existing failures in
  `test_daily_facts_has_continuous_spine` / `test_wear_gap_survives_as_nulls`
  (`expected 11 null nights, got 12`) predate this session's changes —
  confirmed via `git stash` against the prior commit. Not yet root-caused;
  likely a fixture date-boundary issue, not a pipeline bug. Next person to
  touch `tests/make_fixtures.py` should start there.
- `ruff check src tests` clean
- Real run: `fitbit discover` → 293/1,082 files classified against 47
  `DatasetSpec`s (up from 22); `fitbit ingest` → 68 silver tables, 14,042
  rows; `fitbit transform` → `daily_facts` at 60 days × 124 columns;
  `fitbit report` → renders, 4 flags raised.
- Remaining 34 unclassified files are genuinely low-value: account change
  logs, device charger/power events, notification preferences, and a few
  old-format duplicates (`height-*.json`, `swim_lengths_data-*.json`) of
  data the new-format parser already covers.

Real export: 1,082 files, 1,444.7 MB, under
`data_raw/Takeout/Google Health/` (gitignored, unzipped from
`~/Downloads/takeout-20260727T111240Z-1-001.zip`).

## The real export uses a different, newer layout than this pipeline was built for

Fitbit's export is now branded **"Google Health"**, not "Fitbit", and ships
two overlapping data trees. Both are now parsed:

1. `Global Export Data/` — the old layout. 22 original `DatasetSpec`s, plus
   two added this session (`demographic_vo2_max`, `hr_zone_minutes` — nested
   JSON, parsed in `intraday.py`).
2. `Physical Activity_GoogleData/` and `Health Fitness Data_GoogleData/` — a
   **new, richer** per-source CSV format, one file per dataset per day,
   self-describing columns: `timestamp, <value>, data source`. `data source`
   is the device/app that wrote the row — the Fitbit Air shows up as
   **"Radiance"** (its internal codename), vs. `"Fitbit App"` for
   manually-entered or phone-derived data. Parsed generically in the new
   `parsers/google_health.py` — one module covers ~25 datasets because the
   headers are consistent, unlike the old format's drifting column names.
   Every output column from this module is registered under a `gh_` table
   prefix / `_gh` column suffix, so it's always traceable to this source and
   never silently collides with an old-format column of a similar name (see
   "Column collisions" below).

`google_health.py` groups those ~25 datasets into three generic parsers:
`INTRADAY_SPECS` (minute-grain numeric, downsampled like `intraday.py`
already does for the old format), `PIVOT_SPECS` (categorical rows pivoted to
minutes-per-category, e.g. `gh_activity_level` → `activity_min_sedentary_gh`),
and `DAILY_PASSTHROUGH_SPECS` (already ~daily grain, just renamed/typed).
`gh_sleep_scores` and `gh_personal_records` are parsed but silver-only, not
joined to `daily_facts` — see "Deliberately deferred" below.

### Deliberately deferred, not silently dropped

Each of these is a real `IGNORE_PATTERNS` entry in `discover.py` with a
comment explaining why, not an oversight:

- **`gps_location_*.csv`** — real lat/lon/altitude. Needs an explicit
  privacy decision before any cloud sync touches it (see README Privacy
  section). Do not add to a default sync path.
- **`UserActivityProbabilities_*.csv`** — ~45MB/day, sub-2-second-cadence
  activity-classifier output. Same problem `intraday.py` solves for heart
  rate/steps, just not solved for this source yet; needs its own streaming
  downsample rather than a full-file read.
- **`micro_motion`, `micro_stillness`, `live_pace`, `swim_lengths_data`,
  `sedentary_period`, `respiratory_rate_sleep_summary`** — event/array-shaped,
  each needs bespoke aggregation the generic parser can't give it for free.
- **`UserSleepStages`, `UserExercises`, `WorkoutSummariesAndRounds`** —
  genuinely useful (richer sleep staging, workout sets/reps) but event-grain
  and would need a real decision about how they reconcile with the existing
  `sleep.py` / `exercise-*.json` pipelines before joining to gold.
- **`Biometrics/Glucose *.csv`** (231 files) — confirmed empty (0 bytes),
  template placeholders Fitbit ships regardless of whether the feature is
  used.
- **`menstrual_health_*.csv`** — not applicable to this user; excluded
  outright rather than parsed-then-ignored.
- Settings/account-metadata noise (`UserAppSettingData`,
  `UserDemographicData`, `AppContentHistory`, etc.) — not health data.

## Column collisions across silver tables (now handled automatically)

With two parallel formats producing similar metrics (e.g. old `resting_hr`
vs. new `resting_hr_gh`), `transform.build_daily_facts` now tracks every
column name it has already selected and auto-suffixes any repeat with
`__<table_name>`, printing a note when it does. This was added this session
because it was about to matter for real, not preemptively — check the
`fitbit transform` output for `note: column '...' collides` if a future
dataset addition trips it.

## Immediate next steps

1. **Reconcile duplicate signals.** Old-format `resting_hr` and new-format
   `resting_hr_gh` (similarly for SpO2, respiratory rate, HRV) now both flow
   into `daily_facts` under different names. Decide whether one is strictly
   better (the new format is likely more accurate — it's the format the
   Fitbit app itself now reads from) and either prefer it in `analytics/` or
   average/reconcile the two.
2. **`UserActivityProbabilities` downsampling** — the single largest deferred
   item by data volume.
3. Fitbit Web API OAuth sync (task #4 in this session's plan) — turns this
   from a one-time backfill into an actual pipeline.

Set `intraday_file_limit: 20` in `config.local.yaml` while iterating —
already set in `config.local.yaml` (gitignored, present locally). Unset
(`null`) once ready for a full ingest.

## Known facts about the real data

- Device is a **Fitbit Air**, confirmed as `data source: Radiance` in the new
  CSV format. It writes HRV, SpO2, temperature, and readiness data — all
  present in the real export, all now parsed from both formats.
- Export vintage is 2026-07.
- Timezone handling: new-format minute-grain files (`INTRADAY_SPECS`,
  `PIVOT_SPECS`) are real UTC instants, converted to `config.timezone` before
  bucketing into local days — see `google_health._local_date_key`. New-format
  *daily* files mostly stamp every row `T00:00:00Z` (or a bare date) as a
  calendar-date label, not a real instant; converting those through a
  timezone would shift the date near the UTC offset boundary, so
  `google_health._daily_date` reads them literally instead unless a spec
  marks `"instant": True` (currently just `gh_height`/`gh_weight`, which
  carry real clock times). Old-format Global Export Data timestamps are
  device-local already. Three different conventions in one pipeline — if a
  date looks off by one, check which of the three applies to that file
  first.

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

- Exercise session parsing (`exercise-*.json` and now `UserExercises_*.csv`
  are both cataloged but unparsed — same underlying gap, two formats)
- `UserActivityProbabilities` downsampling (see above)
- Reconciling old-format vs. `_gh` duplicate metrics into one canonical
  column per concept
- Change-point detection on resting HR rather than threshold rules
- Seasonality decomposition once >1 year of data is confirmed present
- A `fitbit export` command emitting a clinician-friendly one-page PDF
- GPS-based per-workout route maps, opt-in only (see Privacy in README)
