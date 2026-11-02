"""FRED/ALFRED vintage dates and paginated point-in-time observation snapshots."""

import os
import re
from datetime import datetime, timedelta

from .base import in_window, interval, permissions, record, result
from .transport import SourceError


def _pages(client, path, params, field):
    offset = 0
    expected = None
    collected = []
    while True:
        payload = client.json("api.stlouisfed.org", path, params={**params, "file_type": "json", "limit": 1000, "offset": offset})
        if not isinstance(payload, dict) or not isinstance(payload.get(field), list):
            raise SourceError("fred_invalid_paginated_response")
        count = payload.get("count")
        if type(count) is not int or count < 0 or payload.get("offset") != offset:
            raise SourceError("fred_invalid_pagination_receipt")
        if expected is not None and expected != count:
            raise SourceError("fred_pagination_changed_during_collection")
        expected = count
        batch = payload[field]
        if offset + len(batch) > count or (not batch and offset < count):
            raise SourceError("fred_incomplete_pagination")
        collected.extend(batch)
        offset += len(batch)
        if offset == count:
            return collected


def collect(client, item, start, end):
    token = os.environ.get("FRED_API_KEY", "").strip()
    if not token or any(ord(char) < 32 for char in token):
        raise SourceError("fred_api_key_not_configured", unavailable=True)
    series = item.get("series_id", "")
    if not isinstance(series, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,80}", series):
        raise SourceError("fred_series_id_required", unavailable=True)
    permission = permissions(item)
    params = {"api_key": token, "series_id": series}
    vintages = _pages(client, "/fred/series/vintagedates", {
        **params, "realtime_start": start.date().isoformat(), "realtime_end": end.date().isoformat(), "sort_order": "asc",
    }, "vintage_dates")
    metadata = client.json("api.stlouisfed.org", "/fred/series", params={
        **params, "file_type": "json", "realtime_start": end.date().isoformat(), "realtime_end": end.date().isoformat(),
    })
    if not isinstance(metadata, dict) or not isinstance(metadata.get("seriess"), list) or not metadata["seriess"]:
        raise SourceError("fred_series_metadata_missing")
    info = metadata["seriess"][0]
    if not isinstance(info, dict) or info.get("id") != series or not isinstance(info.get("units"), str):
        raise SourceError("fred_invalid_series_metadata")
    rows = []
    # This is an explicit bounded observation domain, not the entire economic series history.
    observation_start = (start - timedelta(days=366)).date().isoformat()
    for vintage in vintages:
        try:
            datetime.strptime(vintage, "%Y-%m-%d")
            if not in_window(vintage, start, end):
                raise SourceError("fred_vintage_outside_query")
            observations = _pages(client, "/fred/series/observations", {
                **params, "realtime_start": vintage, "realtime_end": vintage,
                "observation_start": observation_start, "observation_end": vintage,
                "sort_order": "asc", "output_type": 1,
            }, "observations")
            snippets = []
            for observation in observations:
                if not isinstance(observation, dict) or not isinstance(observation.get("date"), str) or not isinstance(observation.get("value"), str):
                    raise SourceError("fred_invalid_observation")
                datetime.strptime(observation["date"], "%Y-%m-%d")
                if not observation_start <= observation["date"] <= vintage:
                    raise SourceError("fred_observation_outside_query")
                snippets.append(f"{observation['date']}: {observation['value']}")
            text = (f"{series}; units: {info['units']}; frequency: {info.get('frequency', 'not supplied')}. "
                    f"As-filed vintage available on {vintage}; observation periods are not release dates. "
                    f"Observation scope starts {observation_start}. " + "; ".join(snippets)) if permission["store_text"] else ""
            rows.append(record(
                "fred", f"{series}:{vintage}", title=f"{series}: official vintage {vintage}",
                url=f"https://fred.stlouisfed.org/series/{series}", published_at=vintage, timezone="America/Chicago",
                category="macro", event_key=f"fred:{series}:vintage:{vintage}", origin_id=f"fred:{series}:{vintage}",
                text=text, text_scope="official_excerpt" if text else "headline_only", permissions=permission,
                evidence_locator=f"series={series}; realtime_start={vintage}; realtime_end={vintage}",
            ))
        except (SourceError, TypeError, ValueError) as exc:  # noqa: PERF203 -- retain earlier vintages on a later request failure
            reason = exc.reason if isinstance(exc, SourceError) else "fred_invalid_vintage"
            return result(rows, failed=[interval(start, end, reason)], cursor={"vintage": vintage},
                          cap_reached=getattr(exc, "cap_reached", False))
    return result(rows, successful=[interval(start, end)], exhausted=True, limitations=[
        "FRED coverage is limited to the configured series vintage index, not all macro releases. Vintage precision is date, not intraday.",
        f"Observation snapshots begin {observation_start}; missing values '.' are not zeros. Storage requires series-specific permission.",
    ])
