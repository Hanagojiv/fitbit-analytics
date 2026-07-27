"""Pull data from the Google Health API.

Two things learned from testing this live against a real account that
aren't obvious from the docs:

* The plain ``list`` endpoint returns nothing useful on its own. Wearable
  data needs ``reconcile`` scoped to the wearables data source family, or
  the response comes back with a ``nextPageToken`` and no ``dataPoints``.
* Every point's ``interval`` already carries ``civilStartTime`` /
  ``civilEndTime`` broken into local year/month/day/hour/minute -- Google
  does the UTC-to-local conversion server-side, unlike the Takeout export
  where that had to be done by hand (see parsers/google_health.py).

Only a handful of data types are wired up so far -- the ones verified live.
Expanding this list is mostly a matter of confirming each dataType string
against a real account before trusting it; see CLAUDE.md.
"""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator

import certifi
import pandas as pd

from .auth import get_access_token

API_BASE = "https://health.googleapis.com/v4"
WEARABLES_SOURCE = "users/me/dataSourceFamilies/google-wearables"
_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())

# Verified live against a real Fitbit Air account on 2026-07-27. Expand only
# after confirming a new dataType string the same way -- a guessed one fails
# silently with an empty dataPoints list, not an error.
DATA_TYPES: list[str] = [
    "steps",
    "heart-rate",
    "distance",
    "active-energy-burned",
    "sleep",
]


def _get(url: str, params: dict, token: str) -> dict:
    q = urllib.parse.urlencode(params)
    req = urllib.request.Request(f"{url}?{q}")
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, context=_SSL_CONTEXT) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{url} -> HTTP {e.code}: {e.read().decode()[:500]}") from e


def fetch_data_points(data_type: str, start: str, end: str, token: str | None = None) -> list[dict]:
    """All reconciled wearable data points for one data type in [start, end).

    ``start``/``end`` are UTC ISO 8601 strings, e.g. "2026-07-20T00:00:00Z".
    Follows pagination to exhaustion.

    No server-side time filter is sent: the filter field name isn't
    consistent across data type shapes (interval-based accumulators like
    steps vs. instantaneous samples like heart-rate use different field
    paths, confirmed live -- see module docstring) and guessing wrong fails
    the whole request rather than degrading gracefully. Trimmed client-side
    instead; ``reconcile`` returns newest-first, so this is a bounded scan,
    not a full-history one, as long as ``start`` is reasonably recent.
    """
    token = token or get_access_token()
    url = f"{API_BASE}/users/me/dataTypes/{data_type}/dataPoints:reconcile"

    points: list[dict] = []
    page_token = None
    while True:
        params = {"dataSourceFamily": WEARABLES_SOURCE, "pageSize": 1000}
        if page_token:
            params["pageToken"] = page_token
        body = _get(url, params, token)
        page = body.get("dataPoints", [])
        points.extend(page)
        page_token = body.get("nextPageToken")

        oldest_on_page = min((_point_start(p) for p in page), default="9999")
        if not page_token or (oldest_on_page and oldest_on_page < start):
            break

    return [p for p in points if start <= _point_start(p) < end]


def _payload(point: dict) -> dict:
    """The data-type-keyed payload dict, e.g. point["heartRate"].

    Some responses (sleep, confirmed live) prefix the point with metadata
    fields like ``dataPointName`` before the actual payload, so this can't
    just take the first value -- it has to specifically skip non-dict ones.
    """
    return next((v for v in point.values() if isinstance(v, dict)), {})


def _point_start(point: dict) -> str:
    """The instant a data point starts, regardless of interval vs. sample shape."""
    payload = _payload(point)
    interval = payload.get("interval")
    if interval:
        return interval.get("startTime", "")
    return (payload.get("sampleTime") or {}).get("physicalTime", "")


def _date_str(date: dict) -> str | None:
    if not date:
        return None
    return f"{date.get('year')}-{date.get('month', 0):02d}-{date.get('day', 0):02d}"


def to_dataframe(points: list[dict], data_type: str) -> pd.DataFrame:
    """Flatten the nested {dataType: {interval|sampleTime, <value>}} shape to a tidy frame."""
    rows = []
    for p in points:
        payload = _payload(p)
        interval = payload.get("interval")
        sample = payload.get("sampleTime")

        if interval:
            row = {
                "start_time_utc": interval.get("startTime"),
                "end_time_utc": interval.get("endTime"),
                "local_date": _date_str((interval.get("civilStartTime") or {}).get("date")),
            }
            extra = {k: v for k, v in payload.items() if k != "interval"}
        elif sample:
            row = {
                "start_time_utc": sample.get("physicalTime"),
                "end_time_utc": sample.get("physicalTime"),
                "local_date": _date_str((sample.get("civilTime") or {}).get("date")),
            }
            extra = {k: v for k, v in payload.items() if k != "sampleTime"}
        else:
            row = {"start_time_utc": None, "end_time_utc": None, "local_date": None}
            extra = dict(payload)

        row.update(extra)
        rows.append(row)

    df = pd.DataFrame(rows)
    if not df.empty:
        df["local_date"] = pd.to_datetime(df["local_date"], errors="coerce")
    return df


def sync_all(
    start: str, end: str, data_types: list[str] | None = None
) -> Iterator[tuple[str, pd.DataFrame]]:
    token = get_access_token()
    for data_type in data_types or DATA_TYPES:
        points = fetch_data_points(data_type, start, end, token)
        yield data_type, to_dataframe(points, data_type)
