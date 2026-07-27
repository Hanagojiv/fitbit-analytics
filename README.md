# fitbit-analytics

An end-to-end data engineering pipeline for my own Fitbit Air data: ingestion,
a warehouse, transformations, ML-based predictions, and an interactive
dashboard — built the way a production pipeline would be, on a dataset of one.

```
Takeout export ──┐
                  ├──▶ ingest ──▶ warehouse (bronze/silver/gold) ──▶ predictions ──▶ dashboard
Fitbit Web API ───┘                     ▲
                              scheduled orchestration (GitHub Actions)
```

Repo: https://github.com/Hanagojiv/fitbit-analytics

---

## Current state (2026-07-27)

**Built and working:** a local ELT pipeline (discover → ingest → transform →
report) over Google Takeout Fitbit exports, using DuckDB for the
bronze/silver/gold layers and a self-contained HTML report. 15 tests passing
against synthetic fixtures. See `src/fitbit_analytics/` and the architecture
notes below — this part of the repo predates the cloud rebuild and still
works standalone if you just want a local-only pipeline.

**Just discovered, not yet handled:** the real Takeout export (267MB zipped,
1.4GB unzipped) uses Fitbit's newer **"Google Health"** export format
(post-Google-acquisition), not the older "Fitbit" layout the parsers above
were built against. Running `fitbit discover` against it found:

- The 20 dataset types already parsed (`Global Export Data/`) are present and
  read correctly — heart rate, steps, sleep, HRV, SpO2, temperature, etc.
- **789 files are unclassified**, almost entirely under a parallel,
  higher-fidelity export tree (`Physical Activity_GoogleData/`,
  `Health Fitness Data_GoogleData/`) with ~30 additional per-minute datasets:
  cardio load, GPS location, micro-motion, swim lengths, daily readiness,
  weight, height, and more, each tagged with a `data source` device name
  ("Radiance" — the Fitbit Air's internal codename — vs. "Fitbit App").
- One file, `UserActivityProbabilities`, is ~1.1GB on its own — a
  sub-2-second-cadence activity-classifier stream that needs the same
  aggregate-on-read treatment the old intraday heart rate/steps data already
  gets, not raw storage.
- `gps_location_*.csv` contains real lat/lon/altitude. This needs an explicit
  privacy decision before anything cloud-hosted touches it — see **Privacy**
  below.

**Not yet built:** everything past the local pipeline — the Fitbit Web API
sync, the Postgres warehouse, dbt models, scheduled orchestration, ML
predictions, and the dashboard. This README describes the target shape;
treat anything below marked with a stage as aspirational until it has code
behind it.

---

## Target architecture

| Layer | Choice | Status |
|---|---|---|
| Historical backfill | Google Takeout export (this repo's existing parser layer) | ⚠️ needs extending for the new export format |
| Ongoing sync | Fitbit Web API (OAuth2, daily incremental) | ⬜ not started |
| Warehouse | Supabase (managed Postgres) | ⬜ not started |
| Transform | dbt-core models, bronze/silver/gold on Postgres | ⬜ not started |
| Orchestration | GitHub Actions scheduled workflows | ⬜ not started |
| Predictions | scikit-learn / statsmodels, extending `analytics/` | ⬜ not started |
| Dashboard | Next.js + Tailwind + Recharts/Nivo, deployed on Vercel | ⬜ not started |
| CI | GitHub Actions: lint, pytest, dbt tests, deploy on merge | ⬜ not started |

This is a deliberate change from the pipeline's original design (see
"Local-pipeline design notes" below): real Fitbit data will live on Supabase's
servers, not only on this machine, so a real dashboard can be reached from a
phone. That tradeoff was made explicitly, not by default.

---

## Privacy

- `data/`, `data_raw/`, `reports/`, `config.local.yaml`, and all Takeout
  zips/exports are gitignored. Nothing under those paths has ever been
  committed.
- Once a cloud warehouse exists, **GPS location will not be synced by
  default.** Exact coordinates are materially more sensitive than the rest of
  this dataset (they can reveal home address, workplace, routes). If a
  location-based feature is ever built (e.g. a route map for a single
  workout), it will be opt-in and scoped per-activity, not a bulk sync of
  `gps_location`.
- The dashboard is a personal tool. Nothing here is designed for, or should be
  read as, medical advice — see Scope below.

---

## Quick start (local pipeline only)

```bash
git clone https://github.com/Hanagojiv/fitbit-analytics.git && cd fitbit-analytics

python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Unzip your Takeout archive somewhere, then:
cp config.example.yaml config.local.yaml
$EDITOR config.local.yaml          # set takeout_root

fitbit all                         # discover -> ingest -> transform -> report
open reports/fitbit_report.html
```

Each stage also runs on its own, which is what you want while iterating:

```bash
fitbit discover     # catalog every file, report what was not recognised
fitbit ingest       # parse into silver parquet
fitbit transform    # build gold/daily_facts.parquet
fitbit report       # render the HTML
```

---

## Local-pipeline design notes

These decisions predate the cloud rebuild but still hold for the local
ingestion layer, which remains the backfill path either way.

**Discovery before parsing.** Fitbit's export schema drifts between account
types, device generations and export dates — confirmed firsthand: the real
export uses a different layout than either of us expected. Rather than assume
a layout, the pipeline catalogs what is actually on disk and reports anything
it does not recognise. Adding support is one `DatasetSpec` in `discover.py`.

**Aggregate intraday on read.** Minute-grain (or sub-second, in the new
export) heart rate, steps, and activity-probability data are the overwhelming
majority of an export's bytes and almost none of its insight. They are
downsampled to daily and hourly grains during ingest and the raw rows are
never persisted.

**A continuous date spine.** Missing days appear in `daily_facts` as rows of
nulls rather than vanishing. Without this, every rolling window downstream
silently changes length and a two-week gap quietly becomes a two-week average.

**Coverage is a first-class metric.** A day with four hours of heart rate data
is not a low-activity day, it is a day the watch sat on a charger.
`wear_fraction` is derived from heart-rate sample density so analysis can
exclude those days rather than average them in.

**Per-dataset failure isolation.** A malformed sleep export should not cost you
the heart rate pipeline. Each parser is wrapped independently and reports what
it saw when column matching fails.

---

## Layout

```
src/fitbit_analytics/
├── config.py            config + layer paths
├── discover.py          file classification, the DatasetSpec table
├── ingest.py             orchestration, bronze/silver
├── transform.py         DuckDB join into gold/daily_facts
├── report.py             self-contained interactive HTML
├── cli.py                argparse entry point
├── parsers/
│   ├── common.py         timestamp conventions, the {dateTime,value} shape
│   ├── intraday.py       minute-grain JSON + downsampling + wear coverage
│   ├── sleep.py           sleep logs, stages, timing features
│   └── csv_features.py   HRV, SpO2, sleep score, stress, readiness
└── analytics/
    ├── trends.py          baselines, RHR drift, regularity, robust anomalies
    ├── relationships.py   lagged correlations with FDR correction
    └── flags.py           rule-based observations
```

Cloud-stack code (Fitbit API client, dbt project, predictions, dashboard) will
land in new top-level directories as it's built; this section will be updated
as that happens rather than left stale.

Datasets currently recognised (old "Global Export Data" format only — the new
`Physical Activity_GoogleData` / `Health Fitness Data_GoogleData` datasets
are not yet parsed): steps, calories, distance, altitude, heart rate, resting
heart rate, the four active-minute tiers, sleep, exercise, HRV (daily and
detail), respiratory rate, SpO2 (daily and minute), skin and device
temperature, sleep score, stress score, daily readiness, and Active Zone
Minutes.

---

## The analytics

**Baselines and drift.** Trailing rolling windows only, never centred, so every
value uses information available on that day. Resting heart rate is tracked as
a 7-day mean against a 60-day baseline, expressed in standard deviations of
your own variability.

**Anomalies.** Modified z-score against a trailing median and MAD, so a handful
of genuine outliers cannot inflate the baseline enough to hide themselves. Very
stable metrics fall back to a mean-absolute-deviation scale, because a zero MAD
would otherwise make spikes in your steadiest signals undetectable.

**Relationships.** A fixed list of pre-registered pairs in
`relationships.HYPOTHESES`, each with an explicit lag, tested once with a
Benjamini-Hochberg correction. Testing a dozen hypotheses against one person's
noisy data will otherwise hand you a "significant" result by construction.

**Sleep regularity.** Rolling standard deviation of the sleep midpoint. Bedtime
is mapped to a signed decimal hour so 23:30 and 00:30 read as half an hour
apart rather than twenty-three.

**Predictions (planned).** Forecast next-day resting heart rate and sleep
score from trailing features; surface as a range, not a point estimate.
Anomaly and relationship logic above feed directly into the suggestions
layer on the dashboard.

---

## Testing

```bash
pytest -q
```

`tests/make_fixtures.py` generates a synthetic Takeout tree with known
structure planted in it: a fortnight of elevated resting heart rate, an
eleven-day wear gap, a sleep-to-RHR coupling, and a weekend phase shift. The
tests assert the analytics *recover* those signals, and that uncorrelated pairs
stay insignificant — a suite that only checked for absence of exceptions would
pass on a pipeline that computed nothing.

You can also run the whole thing against fixtures without touching real data:

```bash
python tests/make_fixtures.py
# set takeout_root: "./fixtures/Takeout" in config.local.yaml
fitbit all
```

---

## Extending it

*New dataset:* add a `DatasetSpec` to `DATASET_SPECS` in `discover.py`, write a
parser, register it in `ingest.py`, and add it to `DAILY_TABLES` in
`transform.py` if it is daily-grain.

*New CSV export whose columns do not match:* `fitbit ingest` prints the real
column names it saw. Add them to `VALUE_ALIASES` in `parsers/csv_features.py`.

*New hypothesis:* append to `HYPOTHESES` in `analytics/relationships.py`. Add
it before you look at the result, not after — that is the entire point of the
list being fixed.

*New rule:* write a `_rule_*` function in `analytics/flags.py` returning a list
of `Flag`, and add it to the tuple in `evaluate()`.

---

## Scope

This produces descriptive statistics and trend predictions about what a
consumer wrist device estimated. Such a device is a reasonable trend
instrument and a poor absolute one: optical heart rate degrades during
movement, wrist SpO2 is not a medical oximeter, and sleep staging from
actigraphy plus heart rate agrees only moderately with polysomnography. The
thresholds in `flags.py` are traceable to public health references where such
references exist, and to your own baselines where they do not.

Nothing here is a diagnosis or medical advice. If a pattern concerns you or
persists, bring it to a clinician who can weigh it against your history and an
actual measurement.
