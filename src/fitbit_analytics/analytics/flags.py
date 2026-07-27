"""Turn the daily fact table into a short list of things worth noticing.

Scope note: these are descriptive observations about a consumer wearable's
estimates, not clinical findings. A wrist optical sensor is a decent trend
instrument and a poor absolute one. Nothing here diagnoses anything, and
anything in the ``discuss`` tier is a prompt to raise it with a clinician who
can put it next to your history and an actual measurement, not a conclusion.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import pandas as pd

from . import trends

# Reference points used below, with their sources, so every threshold in this
# file can be traced rather than taken on faith.
REFERENCES = {
    "sleep_duration": "7-9 h/night for adults (AASM/Sleep Research Society consensus)",
    "mvpa_weekly": "150-300 min/week moderate-to-vigorous activity (WHO 2020)",
    "sleep_regularity": "Night-to-night timing variability tracks outcomes independently "
                        "of duration (regularity literature)",
    "rhr_drift": "Sustained resting HR elevation vs personal baseline is a general "
                 "stress/fatigue/illness marker, not disease-specific",
    "spo2": "Consumer wrist SpO2 is not a medical oximeter; treat as a trend only",
}

TIERS = ("notice", "watch", "discuss")


@dataclass
class Flag:
    tier: str          # notice | watch | discuss
    topic: str
    headline: str
    detail: str
    evidence: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _recent(df: pd.DataFrame, days: int) -> pd.DataFrame:
    return df[df["date"] >= df["date"].max() - pd.Timedelta(days=days)]


def _mean(df: pd.DataFrame, col: str) -> float | None:
    if col not in df.columns:
        return None
    val = df[col].mean()
    return None if pd.isna(val) else float(val)


def evaluate(df: pd.DataFrame) -> list[Flag]:
    """Run every rule and return the flags that fired."""
    out: list[Flag] = []
    for rule in (
        _rule_data_coverage,
        _rule_sleep_duration,
        _rule_sleep_regularity,
        _rule_social_jetlag,
        _rule_rhr_drift,
        _rule_rhr_trend,
        _rule_activity_volume,
        _rule_sedentary,
        _rule_hrv_trend,
        _rule_spo2,
        _rule_respiratory,
    ):
        try:
            out.extend(rule(df))
        except Exception:  # a broken rule must not sink the report
            continue

    order = {t: i for i, t in enumerate(reversed(TIERS))}
    return sorted(out, key=lambda f: order.get(f.tier, 99))


# --- rules -----------------------------------------------------------------

def _rule_data_coverage(df: pd.DataFrame) -> list[Flag]:
    r90 = _recent(df, 90)
    wear = _mean(r90, "wear_fraction")
    if wear is None:
        return []
    if wear < 0.5:
        return [Flag(
            "notice", "Data quality",
            f"The watch was worn about {wear * 100:.0f}% of the time over the last 90 days.",
            "Below roughly half-time wear, daily averages start describing when you wore "
            "the device rather than how you lived. Sleep and resting heart rate are the "
            "first numbers to become unreliable.",
            f"mean wear_fraction = {wear:.2f}",
        )]
    return []


def _rule_sleep_duration(df: pd.DataFrame) -> list[Flag]:
    r90 = _recent(df, 90)
    mean_h = _mean(r90, "sleep_hours")
    if mean_h is None:
        return []

    nights = r90["sleep_hours"].notna().sum()
    short_share = (r90["sleep_hours"] < 6).mean()

    if mean_h < 6.5:
        return [Flag(
            "discuss", "Sleep duration",
            f"Average sleep is {mean_h:.1f} h/night across {nights} recorded nights.",
            f"That sits below the {REFERENCES['sleep_duration']} range, and "
            f"{short_share * 100:.0f}% of nights came in under 6 h. If this is chronic "
            "rather than a busy stretch, it is the single highest-leverage thing in this "
            "whole dataset, and worth raising with a doctor if it persists despite "
            "protecting the time.",
            f"mean={mean_h:.2f} h, nights={nights}",
        )]
    if mean_h < 7.0:
        return [Flag(
            "watch", "Sleep duration",
            f"Average sleep is {mean_h:.1f} h/night, just under the usual adult range.",
            f"Reference: {REFERENCES['sleep_duration']}. Worth watching whether this is a "
            "seasonal dip or a settled pattern.",
            f"mean={mean_h:.2f} h, nights={nights}",
        )]
    return []


def _rule_sleep_regularity(df: pd.DataFrame) -> list[Flag]:
    reg = trends.sleep_regularity(df)
    if reg.empty:
        return []
    sd = reg["midpoint_sd_min"].tail(28).mean()
    if pd.isna(sd):
        return []

    if sd > 90:
        return [Flag(
            "watch", "Sleep timing",
            f"Sleep midpoint varies by about {sd:.0f} minutes night to night.",
            f"{REFERENCES['sleep_regularity']}. Variability at this level is roughly the "
            "circadian equivalent of changing time zone every few days. Anchoring wake "
            "time tends to be easier to hold than anchoring bedtime.",
            f"28-day rolling SD of midpoint = {sd:.0f} min",
        )]
    if sd < 30:
        return [Flag(
            "notice", "Sleep timing",
            f"Sleep timing is very consistent, varying about {sd:.0f} minutes night to night.",
            "Worth knowing what is going right, not just what is going wrong.",
            f"28-day rolling SD of midpoint = {sd:.0f} min",
        )]
    return []


def _rule_social_jetlag(df: pd.DataFrame) -> list[Flag]:
    sj = trends.social_jetlag(df)
    if not sj:
        return []
    minutes = sj["social_jetlag_min"]
    if abs(minutes) < 60:
        return []
    return [Flag(
        "notice", "Weekend shift",
        f"Weekend sleep runs {abs(minutes):.0f} minutes "
        f"{'later' if minutes > 0 else 'earlier'} than weekdays.",
        "A shift of an hour or more each weekend is commonly described as social jetlag. "
        "It usually shows up as a rough Monday rather than anything dramatic.",
        f"weekday midpoint {sj['weekday_midpoint_hour']:.1f} h vs "
        f"weekend {sj['weekend_midpoint_hour']:.1f} h",
    )]


def _rule_rhr_drift(df: pd.DataFrame) -> list[Flag]:
    drift = trends.rhr_drift(df)
    if drift.empty:
        return []
    tail = drift.tail(14)
    elevated_days = int(tail["elevated"].sum())
    delta = tail["rhr_delta"].tail(7).mean()

    if elevated_days >= 7 and pd.notna(delta) and delta > 0:
        return [Flag(
            "watch", "Resting heart rate",
            f"Resting heart rate has run about {delta:.1f} bpm above your own baseline "
            f"on {elevated_days} of the last 14 days.",
            f"{REFERENCES['rhr_drift']}. Common everyday explanations are short sleep, "
            "alcohol, heat, a heavy training block, or something viral. If it stays "
            "elevated for a few weeks with no obvious cause, it is worth mentioning at "
            "your next appointment.",
            f"7-day mean delta = {delta:+.1f} bpm vs 60-day baseline",
        )]
    return []


def _rule_rhr_trend(df: pd.DataFrame) -> list[Flag]:
    if "resting_hr" not in df.columns:
        return []
    slope, p = trends.slope_per_month(df["resting_hr"], df["date"])
    if pd.isna(slope) or p > 0.01 or abs(slope) < 0.4:
        return []

    direction = "rising" if slope > 0 else "falling"
    tier = "watch" if slope > 0 else "notice"
    reading = ("Often tracks fitness gains, weight change, or reduced stress load."
               if slope < 0 else
               "Can reflect detraining, poorer sleep, weight gain, or rising stress load. "
               "It is a slow signal, so read it over months rather than days.")
    return [Flag(
        tier, "Resting heart rate",
        f"Resting heart rate is {direction} by roughly {abs(slope):.1f} bpm per month "
        "across the record.",
        reading,
        f"OLS slope = {slope:+.2f} bpm/30d, p = {p:.2g}",
    )]


def _rule_activity_volume(df: pd.DataFrame) -> list[Flag]:
    r90 = _recent(df, 90)
    if "mvpa_minutes" not in r90.columns:
        return []
    daily = _mean(r90, "mvpa_minutes")
    if daily is None:
        return []

    weekly = daily * 7
    if weekly < 150:
        return [Flag(
            "watch", "Activity",
            f"Moderate-to-vigorous activity averages about {weekly:.0f} min/week.",
            f"The common public health reference is {REFERENCES['mvpa_weekly']}. Fitbit's "
            "zone estimates run conservative, so treat this as a floor rather than a "
            "verdict — but the gap is large enough to be real.",
            f"mean MVPA = {daily:.1f} min/day over 90 days",
        )]
    if weekly >= 300:
        return [Flag(
            "notice", "Activity",
            f"Moderate-to-vigorous activity averages about {weekly:.0f} min/week.",
            f"Comfortably inside {REFERENCES['mvpa_weekly']}.",
            f"mean MVPA = {daily:.1f} min/day over 90 days",
        )]
    return []


def _rule_sedentary(df: pd.DataFrame) -> list[Flag]:
    r90 = _recent(df, 90)
    sed = _mean(r90, "sedentary_minutes")
    if sed is None:
        return []
    hours = sed / 60
    if hours > 12:
        return [Flag(
            "notice", "Sedentary time",
            f"Sedentary time averages about {hours:.1f} h/day.",
            "Fitbit counts sleep-adjacent stillness in this number, so it overstates "
            "waking sitting. The useful read is the trend and the shape of the day, not "
            "the absolute total. Breaking up long uninterrupted blocks matters more than "
            "the daily sum.",
            f"mean sedentary = {sed:.0f} min/day",
        )]
    return []


def _rule_hrv_trend(df: pd.DataFrame) -> list[Flag]:
    if "hrv_rmssd" not in df.columns or df["hrv_rmssd"].notna().sum() < 30:
        return []
    slope, p = trends.slope_per_month(df["hrv_rmssd"], df["date"])
    if pd.isna(slope) or p > 0.01:
        return []
    if slope >= 0:
        return []
    return [Flag(
        "notice", "Heart rate variability",
        f"HRV is drifting down by roughly {abs(slope):.1f} ms per month.",
        "HRV is highly individual and noisy; only the direction over months carries much "
        "information, and it moves with sleep, alcohol, training load and stress together. "
        "Read it alongside resting heart rate rather than on its own.",
        f"OLS slope = {slope:+.2f} ms/30d, p = {p:.2g}",
    )]


def _rule_spo2(df: pd.DataFrame) -> list[Flag]:
    if "spo2_avg" not in df.columns:
        return []
    r90 = _recent(df, 90)
    mean_spo2 = _mean(r90, "spo2_avg")
    if mean_spo2 is None:
        return []
    low_nights = int((r90["spo2_avg"] < 90).sum())
    if mean_spo2 < 92 or low_nights >= 5:
        return [Flag(
            "discuss", "Overnight oxygen saturation",
            f"Nightly average SpO2 is {mean_spo2:.1f}%, with {low_nights} nights below 90%.",
            f"{REFERENCES['spo2']} — wrist readings often read low by a few points from "
            "poor contact or cold hands, so this may well be measurement noise. That said, "
            "consistently low overnight readings are one of the things clinicians do want "
            "to know about, so it is worth mentioning rather than dismissing.",
            f"mean SpO2 = {mean_spo2:.1f}%, nights < 90% = {low_nights}",
        )]
    return []


def _rule_respiratory(df: pd.DataFrame) -> list[Flag]:
    if "respiratory_rate" not in df.columns:
        return []
    z = trends.robust_z(df["respiratory_rate"])
    recent_hits = int((z.tail(14).abs() >= 3).sum())
    if recent_hits >= 3:
        return [Flag(
            "notice", "Breathing rate",
            f"Overnight breathing rate has been unusual on {recent_hits} of the last 14 nights.",
            "Breathing rate is stable enough that departures usually mean something "
            "ordinary and temporary — a cold, a fever, alcohol, or a much warmer room.",
            f"{recent_hits} nights with |robust z| >= 3",
        )]
    return []


def to_frame(flags: list[Flag]) -> pd.DataFrame:
    if not flags:
        return pd.DataFrame(columns=["tier", "topic", "headline", "detail", "evidence"])
    return pd.DataFrame([f.as_dict() for f in flags])
