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

Built and passing, but **never yet run against real data**. Everything was
developed and verified against synthetic fixtures (`tests/make_fixtures.py`).

- 15 tests passing, `ruff check src tests` clean
- Pipeline verified end to end: 270 synthetic days, 24 silver tables, 62 gold columns
- 22 `DatasetSpec`s registered in `discover.py`

The single largest open risk: the real export almost certainly contains file
shapes the parsers have not seen. This is expected and designed for, not a bug.

## Immediate next step

```bash
fitbit discover
```

Run this against the real Takeout before anything else. It prints an
unclassified-file report. Work through that list:

- Genuinely useful file → add a `DatasetSpec` to `DATASET_SPECS` in
  `discover.py`, write or extend a parser, register it in `ingest.py`, and add
  it to `DAILY_TABLES` in `transform.py` if it is daily-grain.
- Noise (badges, social, profile) → add to `IGNORE_PATTERNS`.

Then `fitbit ingest`. When a CSV parser matches no known columns it prints the
real column names it saw — feed those into `VALUE_ALIASES` in
`parsers/csv_features.py`.

Set `intraday_file_limit: 20` in `config.local.yaml` for the first pass. The
real export is ~266 MB and mostly minute-grain heart rate; get the shape right
on a subset before parsing all of it.

## Known unknowns about the real data

- Device is a **Fitbit Air**. Its exact feature set is unconfirmed — whether it
  writes HRV, SpO2, skin temperature, readiness or stress scores is not yet
  known. Parsers exist for all of these and will simply produce nothing if the
  files are absent.
- Export vintage is 2026-07. Column names in the CSV feature exports drift
  between vintages; `VALUE_ALIASES` uses alias lists plus substring fallback
  for this reason.
- Timezone handling is deliberately minimal. Global Export Data timestamps are
  device-local; the `timezone` config value is currently unused beyond
  documentation. If daylight-saving artifacts show up in `midpoint_hour`, that
  is the place to look.

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
