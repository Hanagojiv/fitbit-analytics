"""End-to-end and unit tests, run against synthetic fixtures.

The fixtures bake in known structure (a resting heart rate elevation, a wear
gap, a sleep/RHR coupling), so these tests assert the analytics *recover* what
was planted rather than merely running without raising.
"""

from __future__ import annotations

import pandas as pd
import pytest

from fitbit_analytics import discover, ingest, transform
from fitbit_analytics.analytics import flags, relationships, trends
from fitbit_analytics.config import Config
from fitbit_analytics.parsers import common, sleep

from .make_fixtures import build


@pytest.fixture(scope="session")
def cfg(tmp_path_factory) -> Config:
    root = tmp_path_factory.mktemp("export")
    takeout = build(root, days=270)
    return Config(takeout_root=takeout, data_dir=root / "data")


@pytest.fixture(scope="session")
def facts(cfg: Config) -> pd.DataFrame:
    catalog = discover.build_catalog(cfg)
    cfg.ensure_dirs()
    catalog.to_parquet(cfg.catalog_path, index=False)
    ingest.run(cfg, catalog)
    return transform.build_daily_facts(cfg)


# --- discovery ---------------------------------------------------------

def test_catalog_classifies_known_datasets(cfg: Config):
    catalog = discover.build_catalog(cfg)
    found = set(catalog["dataset"])
    for expected in ("steps", "heart_rate", "sleep", "resting_heart_rate",
                     "hrv_daily", "spo2_daily", "sleep_score"):
        assert expected in found, f"{expected} not classified"


def test_unrecognised_files_are_surfaced_not_dropped(cfg: Config):
    catalog = discover.build_catalog(cfg)
    # archive_browser.html is explicitly ignored; nothing should silently vanish.
    assert (catalog["dataset"] == "ignored").any()
    assert len(catalog) > 50


# --- parsers -----------------------------------------------------------

def test_timestamp_parser_handles_both_conventions():
    us = pd.Series(["01/15/24 13:45:00", "01/16/24 09:00:00"])
    iso = pd.Series(["2024-01-15T13:45:00.000", "2024-01-16T09:00:00.000"])
    assert common.parse_timestamps(us).dt.year.tolist() == [2024, 2024]
    assert common.parse_timestamps(iso).dt.hour.tolist() == [13, 9]


def test_column_normalisation():
    assert common.normalize_column("Daily SpO2 (%)") == "daily_spo2_pct"
    assert common.normalize_column("  Overall Score ") == "overall_score"


def test_bedtime_hour_wraps_around_midnight():
    """23:30 and 00:30 must be half an hour apart, not 23 hours."""
    ts = pd.Series(pd.to_datetime(["2024-01-01 23:30", "2024-01-02 00:30"]))
    hours = sleep._decimal_hour(ts)
    assert abs(hours[1] - hours[0]) == pytest.approx(1.0, abs=0.01)


# --- pipeline ----------------------------------------------------------

def test_daily_facts_has_continuous_spine(facts: pd.DataFrame):
    """Missing days must appear as null rows, never disappear."""
    gaps = facts["date"].diff().dropna().dt.days
    assert (gaps == 1).all(), "date spine is not continuous"
    assert len(facts) == 270


def test_wear_gap_survives_as_nulls(facts: pd.DataFrame):
    """The fixture removes 11 days of wear; they must be present but empty."""
    missing = facts["sleep_hours"].isna().sum()
    assert missing == 11, f"expected 11 null nights, got {missing}"


def test_expected_columns_present(facts: pd.DataFrame):
    for col in ("sleep_hours", "resting_hr", "steps", "hrv_rmssd",
                "spo2_avg", "mvpa_minutes", "wear_fraction", "is_weekend"):
        assert col in facts.columns, f"missing {col}"


# --- analytics ---------------------------------------------------------

def test_rhr_drift_detects_injected_elevation(facts: pd.DataFrame):
    """Fixtures add +4.5 bpm for a fortnight; the drift metric must see it."""
    drift = trends.rhr_drift(facts)
    assert drift["rhr_delta"].max() > 3.0
    assert drift["elevated"].sum() >= 5


def test_sleep_rhr_coupling_recovered(facts: pd.DataFrame):
    """Fixtures set RHR = base - 0.8*(sleep - mean); sign must come back negative."""
    table = relationships.hypothesis_table(facts)
    row = table[(table["driver"] == "sleep_hours") & (table["outcome"] == "resting_hr")]
    assert not row.empty
    assert row.iloc[0]["r"] < -0.15
    assert bool(row.iloc[0]["significant"])


def test_uncorrelated_pairs_stay_insignificant(facts: pd.DataFrame):
    """Guard against the correction being too lenient and inventing findings."""
    table = relationships.hypothesis_table(facts)
    noise = table[(table["driver"] == "steps") & (table["outcome"] == "sleep_hours")]
    assert not noise.empty
    assert not bool(noise.iloc[0]["significant"])


def test_social_jetlag_matches_injected_shift(facts: pd.DataFrame):
    sj = trends.social_jetlag(facts)
    assert sj["social_jetlag_min"] > 45


def test_robust_z_is_not_fooled_by_outliers():
    s = pd.Series([10.0] * 60 + [10.5, 9.5, 40.0])
    z = trends.robust_z(s)
    assert abs(z.iloc[-1]) > 5, "a 4x spike should score as a strong outlier"


def test_flags_run_and_are_well_formed(facts: pd.DataFrame):
    result = flags.evaluate(facts)
    assert isinstance(result, list)
    for f in result:
        assert f.tier in flags.TIERS
        assert f.headline and f.detail


def test_a_broken_metric_does_not_sink_the_report(facts: pd.DataFrame):
    """One corrupt column must not stop the other rules from firing."""
    broken = facts.copy()
    broken["resting_hr"] = "not a number"
    result = flags.evaluate(broken)
    assert isinstance(result, list)
