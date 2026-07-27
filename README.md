# fitbit-analytics

A local ELT pipeline and analytics layer for Google Takeout Fitbit exports.

Runs entirely on your machine. No data leaves it, nothing is uploaded, and
`data/` is gitignored so the repository stays committable while your health
record does not.

```
Takeout/  ──▶  catalog  ──▶  bronze/silver  ──▶  gold  ──▶  report.html
             (discover)      (ingest)          (transform)   (report)
```

---

## Quick start

```bash
git clone <your-remote> fitbit-analytics && cd fitbit-analytics

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

## Why it is shaped this way

**Discovery before parsing.** Fitbit's export schema drifts between account
types, device generations and export dates. Rather than assume a layout, the
pipeline catalogs what is actually on disk and reports anything it does not
recognise. If your export contains something this repo has not seen, you will
be told about it instead of silently losing it. Adding support is one
`DatasetSpec` in `discover.py`.

**Aggregate intraday on read.** Minute-grain heart rate and steps are the
overwhelming majority of an export's bytes and almost none of its insight. They
are downsampled to daily and hourly grains during ingest and the raw rows are
never persisted, which keeps a multi-gigabyte export comfortably in memory.
Set `intraday_file_limit` for a fast first pass.

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
├── ingest.py            orchestration, bronze/silver
├── transform.py         DuckDB join into gold/daily_facts
├── report.py            self-contained interactive HTML
├── cli.py               argparse entry point
├── parsers/
│   ├── common.py        timestamp conventions, the {dateTime,value} shape
│   ├── intraday.py      minute-grain JSON + downsampling + wear coverage
│   ├── sleep.py         sleep logs, stages, timing features
│   └── csv_features.py  HRV, SpO2, sleep score, stress, readiness
└── analytics/
    ├── trends.py        baselines, RHR drift, regularity, robust anomalies
    ├── relationships.py lagged correlations with FDR correction
    └── flags.py         rule-based observations
```

Datasets currently recognised: steps, calories, distance, altitude, heart rate,
resting heart rate, the four active-minute tiers, sleep, exercise, HRV (daily
and detail), respiratory rate, SpO2 (daily and minute), skin and device
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

This produces descriptive statistics about what a consumer wrist device
estimated. Such a device is a reasonable trend instrument and a poor absolute
one: optical heart rate degrades during movement, wrist SpO2 is not a medical
oximeter, and sleep staging from actigraphy plus heart rate agrees only
moderately with polysomnography. The thresholds in `flags.py` are traceable to
public health references where such references exist, and to your own baselines
where they do not.

Nothing here is a diagnosis or medical advice. If a pattern concerns you or
persists, bring it to a clinician who can weigh it against your history and an
actual measurement.
