"""Generate a synthetic Takeout tree that mimics Fitbit's real export shapes.

Used by the test suite and by anyone wanting to exercise the pipeline without
handing it real health data. The generator deliberately bakes in structure the
analytics should find: a resting heart rate that rises when sleep is short, a
two week period of elevated resting heart rate, weekend sleep that runs late,
and a stretch of missing wear.
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

SEED = 7
FMT = "%m/%d/%y %H:%M:%S"


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1))


def build(root: Path, days: int = 270, start: str = "2025-09-01") -> Path:
    """Write a synthetic export under ``root`` and return the Takeout dir."""
    rng = np.random.default_rng(SEED)
    random.seed(SEED)

    ged = root / "Takeout" / "Fitbit" / "Global Export Data"
    other = root / "Takeout" / "Fitbit"
    ged.mkdir(parents=True, exist_ok=True)

    d0 = datetime.fromisoformat(start)
    dates = [d0 + timedelta(days=i) for i in range(days)]

    # --- latent daily state ------------------------------------------
    sleep_h = np.clip(rng.normal(7.1, 0.9, days), 3.5, 10.5)
    weekend = np.array([d.weekday() >= 5 for d in dates])
    sleep_h[weekend] += 0.6                       # longer weekend sleep
    bed_hour = np.where(weekend, 0.9, -0.4) + rng.normal(0, 0.55, days)  # decimal, <0 = pre-midnight

    base_rhr = 58 + np.linspace(0, 1.8, days)     # slow upward drift
    rhr = base_rhr - 0.8 * (sleep_h - 7.1) + rng.normal(0, 1.6, days)
    rhr[200:214] += 4.5                           # a two-week elevated block

    steps = np.clip(rng.normal(8200, 3000, days), 300, 26000)
    steps[weekend] *= 1.15
    mvpa = np.clip(rng.normal(24, 16, days), 0, 130)

    # A gap: device off the wrist for 11 days.
    worn = np.ones(days, dtype=bool)
    worn[120:131] = False

    # --- Global Export Data, one file per month -----------------------
    buckets: dict[str, dict[str, list]] = {}
    for i, d in enumerate(dates):
        key = d.strftime("%Y-%m-01")
        b = buckets.setdefault(key, {k: [] for k in
                                     ("steps", "calories", "distance", "heart_rate",
                                      "very_active_minutes", "moderately_active_minutes",
                                      "lightly_active_minutes", "sedentary_minutes")})
        if not worn[i]:
            continue

        # Hourly step/calorie/distance rows.
        shape = np.array([0.2, .1, .1, .1, .2, .6, 1.2, 1.6, 1.4, 1.2, 1.1, 1.3,
                          1.5, 1.3, 1.1, 1.2, 1.4, 1.7, 1.5, 1.2, .9, .7, .5, .3])
        shape = shape / shape.sum()
        for h in range(24):
            ts = (d + timedelta(hours=h)).strftime(FMT)
            b["steps"].append({"dateTime": ts, "value": str(int(steps[i] * shape[h]))})
            b["calories"].append({"dateTime": ts, "value": str(round(60 + 110 * shape[h] * 8, 2))})
            b["distance"].append({"dateTime": ts, "value": str(int(steps[i] * shape[h] * 76))})
            bpm = float(np.clip(rhr[i] + 14 * shape[h] * 8 + rng.normal(0, 4), 42, 178))
            b["heart_rate"].append(
                {"dateTime": ts, "value": {"bpm": round(bpm, 1), "confidence": 2}}
            )

        day_ts = d.strftime(FMT)
        vig = float(np.clip(mvpa[i] * 0.35, 0, 60))
        b["very_active_minutes"].append({"dateTime": day_ts, "value": str(int(vig))})
        b["moderately_active_minutes"].append(
            {"dateTime": day_ts, "value": str(int(mvpa[i] - vig))})
        b["lightly_active_minutes"].append(
            {"dateTime": day_ts, "value": str(int(np.clip(rng.normal(210, 55), 40, 420)))})
        b["sedentary_minutes"].append(
            {"dateTime": day_ts, "value": str(int(np.clip(rng.normal(690, 90), 380, 1100)))})

    for month, series in buckets.items():
        for name, rows in series.items():
            if rows:
                _write_json(ged / f"{name}-{month}.json", rows)

    # --- resting heart rate -------------------------------------------
    rhr_rows = [
        {"dateTime": d.strftime(FMT),
         "value": {"date": d.strftime("%m/%d/%y"), "value": round(float(rhr[i]), 2),
                   "error": 5.5}}
        for i, d in enumerate(dates) if worn[i]
    ]
    _write_json(ged / "resting_heart_rate-2025-09-01.json", rhr_rows)

    # --- sleep logs ----------------------------------------------------
    sleep_rows = []
    for i, d in enumerate(dates):
        if not worn[i]:
            continue
        start_dt = d - timedelta(hours=1) + timedelta(hours=float(bed_hour[i]))
        asleep = int(sleep_h[i] * 60)
        awake = int(np.clip(rng.normal(38, 14), 8, 95))
        end_dt = start_dt + timedelta(minutes=asleep + awake)
        deep = int(asleep * float(np.clip(rng.normal(0.17, 0.04), 0.06, 0.30)))
        rem = int(asleep * float(np.clip(rng.normal(0.21, 0.05), 0.08, 0.35)))
        light = asleep - deep - rem
        sleep_rows.append({
            "logId": 100000 + i,
            "dateOfSleep": d.strftime("%Y-%m-%d"),
            "startTime": start_dt.strftime("%Y-%m-%dT%H:%M:%S.000"),
            "endTime": end_dt.strftime("%Y-%m-%dT%H:%M:%S.000"),
            "duration": (asleep + awake) * 60000,
            "minutesToFallAsleep": 0,
            "minutesAsleep": asleep,
            "minutesAwake": awake,
            "minutesAfterWakeup": 1,
            "timeInBed": asleep + awake,
            "efficiency": int(round(asleep / (asleep + awake) * 100)),
            "type": "stages",
            "infoCode": 0,
            "mainSleep": True,
            "levels": {"summary": {
                "deep": {"count": 4, "minutes": deep, "thirtyDayAvgMinutes": 70},
                "light": {"count": 22, "minutes": light, "thirtyDayAvgMinutes": 230},
                "rem": {"count": 8, "minutes": rem, "thirtyDayAvgMinutes": 95},
                "wake": {"count": 19, "minutes": awake, "thirtyDayAvgMinutes": 55},
            }},
        })
    _write_json(ged / "sleep-2025-09-01.json", sleep_rows)

    # --- CSV feature exports -------------------------------------------
    def _csv(path: Path, header: str, lines: list[str]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(header + "\n" + "\n".join(lines) + "\n")

    hrv = 42 - 0.006 * np.arange(days) + 1.9 * (sleep_h - 7.1) + rng.normal(0, 4.5, days)
    _csv(other / "Heart Rate Variability" / "Daily Heart Rate Variability Summary - 2025-09-01.csv",
         "timestamp,rmssd,nremhr,entropy",
         [f"{d.strftime('%Y-%m-%dT00:00:00')},{hrv[i]:.2f},{rhr[i] - 2:.1f},2.4"
          for i, d in enumerate(dates) if worn[i]])

    spo2 = np.clip(rng.normal(95.6, 1.3, days), 88, 99.5)
    _csv(other / "Oxygen Saturation (SpO2)" / "Daily SpO2 - 2025-09-01.csv",
         "timestamp,average_value,lower_bound,upper_bound",
         [f"{d.strftime('%Y-%m-%d')} 00:00:00,{spo2[i]:.1f},{spo2[i] - 3.2:.1f},{min(spo2[i] + 2, 100):.1f}"
          for i, d in enumerate(dates) if worn[i]])

    rr = np.clip(rng.normal(14.6, 1.0, days), 10, 22)
    _csv(other / "Heart Rate Variability" / "Daily Respiratory Rate Summary - 2025-09-01.csv",
         "timestamp,daily_respiratory_rate",
         [f"{d.strftime('%Y-%m-%dT00:00:00')},{rr[i]:.2f}"
          for i, d in enumerate(dates) if worn[i]])

    score = np.clip(52 + 4.2 * (sleep_h - 6) + rng.normal(0, 5, days), 20, 99)
    _csv(other / "Sleep Score" / "sleep_score.csv",
         "sleep_log_entry_id,timestamp,overall_score,composition_score,revitalization_score,"
         "duration_score,deep_sleep_in_minutes,resting_heart_rate,restlessness",
         [f"{100000 + i},{d.strftime('%Y-%m-%dT06:30:00Z')},{score[i]:.0f},21,18,"
          f"{score[i] * 0.55:.0f},68,{rhr[i]:.0f},0.11"
          for i, d in enumerate(dates) if worn[i]])

    # A file the catalog should not recognise, to prove unclassified reporting works.
    _write_json(other / "Other" / "badge-records.json", [{"badgeType": "DAILY_STEPS"}])
    (root / "Takeout" / "archive_browser.html").write_text("<html>index</html>")

    return root / "Takeout"


if __name__ == "__main__":
    out = build(Path("./fixtures"))
    print(f"Fixtures written to {out}")
