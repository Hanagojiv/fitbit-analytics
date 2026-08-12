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

## Live sync: the Fitbit Web API is gone, replaced by the Google Health API

Google is decommissioning the legacy Fitbit Web API in September 2026.
Everything going forward is `health.googleapis.com/v4`, authenticated with
standard Google OAuth 2.0 -- old Fitbit developer tokens don't transfer.
`src/fitbit_analytics/sync/` is the client for this, built and validated
live against the real account this session.

### OAuth setup, and what actually broke

- The Google Cloud project (`My First Project`) is under a Workspace
  account. Its OAuth consent screen was first set to **Internal**, which
  restricts authorization to accounts in that same Workspace org. The
  user's real Fitbit/Google Health data lives on a **personal Gmail
  account** (`vivekbhanagoji@gmail.com`), a different org entirely -- this
  failed hard with `Error 403: org_internal` on first attempt. Fixed by
  switching User type to **External**, which puts it in **Testing** status
  automatically; added the personal Gmail as a test user.
- Restricted-scope review (Google's slow third-party security assessment)
  is only required past **100 users** OR to move an app out of Testing to
  production. A single-user Testing-status app needs neither, regardless of
  scope sensitivity.
- **Testing-status apps get refresh tokens that expire in 7 days**
  (`refresh_token_expires_in: 604799` in the token response) -- this is a
  hard Google restriction on unverified apps, not a bug. Decided (with
  user): stay in Testing, re-run `fitbit sync-auth` manually about weekly
  rather than pursue verification for a single-user project. Any scheduled
  sync job must fail loudly on an expired refresh token, not retry
  silently, so this doesn't turn into a quiet week-long data gap.
- macOS's python.org build doesn't wire up the system CA store, so the
  token-exchange POST failed with `CERTIFICATE_VERIFY_FAILED` until
  `sync/auth.py` was pointed at `certifi`'s bundle explicitly
  (`ssl.create_default_context(cafile=certifi.where())`). Worth remembering
  if this ever runs somewhere other than this machine.

### API shape, confirmed live (not from docs -- the docs were incomplete)

- The plain `list` endpoint returns an empty `dataPoints` array for
  wearable data. Use `reconcile` with
  `dataSourceFamily=users/me/dataSourceFamilies/google-wearables`, or
  nothing comes back.
- **Response shape differs by data type** and this isn't documented
  anywhere obvious: accumulator types (`steps`, `distance`,
  `active-energy-burned`) key their payload as `{interval: {startTime,
  endTime, civilStartTime, civilEndTime}, <value>}`; instantaneous-sample
  types (`heart-rate`) use `{sampleTime: {physicalTime, civilTime},
  <value>}` instead. `sync/client.py`'s `_payload`/`_point_start` handle
  both. A third shape (`sleep`) additionally prefixes the point with a
  `dataPointName` string field before the payload dict -- broke a
  "first value in the dict" shortcut; fixed by specifically skipping
  non-dict values.
- Google does the UTC→local conversion server-side: every point's
  `civilStartTime`/`civilTime` already carries local year/month/day/hour/
  minute. Simpler than the Takeout CSV parser, which had to do this by hand
  (see `parsers/google_health.py`).
- No server-side time-range filter is used (see `fetch_data_points`
  docstring) -- the filter field name isn't consistent across the
  interval/sample split above, and a wrong guess fails the whole request
  rather than degrading. Time bounds are applied client-side after paging
  back through `reconcile`'s newest-first results.
- **Heart rate is high-volume**: ~2.6s cadence, confirmed ~99K points over
  a 3-day test window. A 7-day pull timed out in testing. Any real sync job
  needs to either request a short window (e.g. last 24-36h, run daily) or
  downsample during fetch the same way `intraday.py` does for Takeout data
  -- raw per-sample heart rate should not be the thing that lands in
  storage long-term.
- Verified working live: `steps`, `heart-rate`, `distance`,
  `active-energy-burned`, `sleep` (`sync/client.py DATA_TYPES`). Everything
  else from the docs' partial data-type list (HRV, SpO2, temperature,
  resting HR, weight, height, VO2 max, readiness) is unconfirmed -- expand
  the list only after testing each one live the same way, per the module
  docstring's warning about silent wrong-identifier failures.

`fitbit sync`, heart-rate downsampling, watermark persistence, and the
fail-loudly behavior are all built now -- see "Orchestrated sync pipeline"
further down for what that actually looked like live. Only the GitHub
Actions schedule itself remains from this section's original list.

## Postgres warehouse + dbt (Supabase)

Live and validated. `secrets.local.yaml`'s `postgres.connection_string`
points at a Supabase project (direct connection, `db.<ref>.supabase.co` --
switch to the session pooler string if this ever fails from a network
without good IPv6, e.g. GitHub Actions runners, untested there yet).

- `fitbit warehouse-load` pushes all 68 silver parquet tables to `raw.*`
  plus the local `data/gold/daily_facts.parquet` to `gold.daily_facts`
  (full-refresh every run).
- `dbt/` has 44 staging models (thin passthroughs) and one real mart,
  `dbt_marts.fct_daily_facts` -- a dynamic 44-way collision-safe join built
  via `adapter.get_columns_in_relation()`, mirroring `transform.py`'s
  `build_daily_facts`. **Validated 2026-07-27**: 117 columns × 60 days,
  cross-checked row-for-row against the local parquet gold table, matched
  exactly. `dbt test` passes.
- Supabase's Table Editor only shows the `public` schema by default --
  everything here lives in `raw`/`gold`/`dbt_staging`/`dbt_marts`, need the
  schema dropdown switched to see them. Cost the user a "why can't I see my
  tables" moment; worth remembering if this comes up again.
- `raw.readiness` never gets created (old-format readiness absent from this
  export -- see "Live sync" section above), which broke the first `dbt run`
  until `stg_readiness.sql` was removed. Not a bug, a real data gap.
- Open decision, not yet made: `gold.daily_facts` (raw parquet upload) and
  `dbt_marts.fct_daily_facts` (dbt's own computation) currently both exist
  with the same data. The dashboard should read one of them, and it should
  probably be `dbt_marts.fct_daily_facts` since that's the one with tests
  and a rebuildable lineage -- but `gold.*` hasn't been removed in case
  that's wrong. Decide before wiring up the dashboard.
- macOS's python.org build needs `SSL_CERT_FILE` set to certifi's bundle
  for `pip install` itself to work, not just this project's own HTTPS
  calls -- same root cause as the OAuth token exchange issue, different
  symptom. Export it before any `pip install` on this machine:
  `export SSL_CERT_FILE=$(python3 -c "import certifi; print(certifi.where())")`.
  (Only works once certifi is already installed somewhere `python3` can
  import it from -- chicken-and-egg on a completely fresh environment.)
- **The Supabase direct-connection host stopped resolving.**
  `db.emmobuzexhpzntbkvags.supabase.co` worked when the warehouse/dbt work
  above was built and validated, then came back `NXDOMAIN` days later with
  no config change on this end. Switched to the **session pooler**
  (`aws-0-us-east-2.pooler.supabase.com:5432`) instead, which is what
  Supabase actually recommends for external tools -- the direct host was
  the wrong default to reach for originally, not just a fallback for when
  it breaks. `secrets.local.yaml`'s `postgres.connection_string` reflects
  this now.

## Orchestrated sync pipeline (`sync/pipeline.py`)

Built, live-tested, and this is the piece of the whole project that most
directly validated the "fail loudly on an expired refresh token" design
decision -- not hypothetically, it actually happened: the token from the
warehouse/dbt session had expired (14 days old, 7-day limit) when `fitbit
sync` was first run here, and it failed with a raw `HTTP 400`. That surfaced
a real gap -- the failure wasn't actually *loud* in a useful sense, just an
ugly traceback -- so `sync/auth.py`'s `_post_token` now detects
`invalid_grant` specifically and raises a message that says what to do
(`fitbit sync-auth`) instead of a bare HTTP error. Re-ran `sync-auth`, then
`fitbit sync` succeeded.

- `fitbit sync` fetches steps/distance/active-energy-burned/heart-rate/sleep
  since each data type's watermark (stored in `meta.sync_watermark` in
  Postgres, not a local file -- survives ephemeral CI runners), downsamples
  heart-rate to daily stats during fetch (never persists raw), and
  **appends/dedups** into `data/silver/sync_*.parquet` rather than
  overwriting -- this is the one part of the pipeline that isn't
  full-refresh, since the API only ever returns what's new. See the
  module docstring for why that's a deliberate deviation.
- Output lands in `sync_*`-prefixed silver tables, separate from `gh_*`
  (Takeout new-format) and the plain old-format ones -- a third source for
  the same reconciliation decision already deferred twice. Not wired into
  `transform.DAILY_TABLES` or the dbt staging layer yet for that reason.
  `fitbit warehouse-load` does pick them up automatically though, since it
  just globs `data/silver/*.parquet`.
- **Found live, not guessed**: `sleep`'s `interval` has no `civilStartTime`
  at all (unlike steps/distance/heart-rate/etc, which do) -- just
  `startTime` + `startUtcOffset` ("-14400s"-style signed seconds). First
  sync run produced `sync_sleep_sessions` with `local_date` entirely null.
  Fixed with `_local_date_from_offset` in `client.py`, applied whenever
  `civilStartTime` is absent rather than assumed present. Worth checking
  for the same gap before trusting `local_date` on any new data type added
  to `DATA_TYPES`.
- CI-friendly auth: `get_access_token()` and `warehouse.load_pg_url()` both
  check env vars first (`GOOGLE_HEALTH_CLIENT_ID/SECRET/REFRESH_TOKEN`,
  `SUPABASE_CONNECTION_STRING`) before falling back to local files, so
  GitHub Actions never needs `secrets.local.yaml` on the runner at all.

### Orchestration: `.github/workflows/sync.yml`

Built and validated before it ever ran on real GitHub infrastructure: cloned
the repo fresh into a scratch dir (no `data/`, no `secrets.local.yaml`,
nothing gitignored) and ran the exact `sync` -> `warehouse-load` -> `dbt
run`/`test` chain there with only env vars set, catching two real bugs
first --

- Neither `sync/pipeline.py` nor `warehouse.py` called `cfg.ensure_dirs()`,
  which worked on this machine (the directory already existed from earlier
  runs) but would have crashed instantly on an actual fresh CI checkout.
  Fixed before the workflow file was even written.
- dbt needs separate host/user/password fields, not the single URI Python
  uses. Deriving those from `SUPABASE_CONNECTION_STRING` inside the
  workflow would re-encode the password, and GitHub's log masking only
  matches the *literal* registered secret string -- a re-encoded
  derivative wouldn't be masked if it ever leaked into a log. Used a
  second, independent `SUPABASE_DB_PASSWORD` secret (raw, unencoded) for
  dbt instead of deriving it, so masking stays correct by construction
  rather than by hoping nothing logs the connection string.

5 repo secrets, set via `gh secret set` (piped through stdin, never a CLI
argument): `GOOGLE_HEALTH_CLIENT_ID`, `GOOGLE_HEALTH_CLIENT_SECRET`,
`GOOGLE_HEALTH_REFRESH_TOKEN`, `SUPABASE_CONNECTION_STRING`,
`SUPABASE_DB_PASSWORD`. Host/user for dbt are hardcoded plain values in the
workflow (not sensitive -- visible in the Supabase dashboard URL anyway).

Runs daily at 12:00 UTC, plus `workflow_dispatch` for manual triggering.
Deliberately nothing catches the `invalid_grant` error from an expired
refresh token (see "Live sync" above) -- the run goes red, GitHub's own
failure notification is the alert. When that happens (expected roughly
weekly): run `fitbit sync-auth` locally, then
`gh secret set GOOGLE_HEALTH_REFRESH_TOKEN --repo Hanagojiv/fitbit-analytics`
with the new token from `.google_health_token.json`.

### Not yet built
- Predictions (task #7) and the dashboard (task #8) still don't exist.

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
