"""ECCC hourly climate observations (MSC GeoMet OGC API).

Station: TORONTO CITY CENTRE (Billy Bishop), climate ID 6158359, hourly 2009-12 -> present.
Fallback: TORONTO CITY (6158355, downtown).

NOTE: ECCC LOCAL_DATE is Local *Standard* Time year-round (no DST). We key on UTC_DATE.

Usage:
    python -m ingest.eccc backfill --start 2015 --end 2026
    python -m ingest.eccc update            # last ~10 days, both stations
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import UTC, datetime, timedelta

import pandas as pd

from common import db
from common.archive import iter_manifest, load_json
from common.config import ECCC_API
from common.http import fetch
from common.timeutil import setup_logging

log = logging.getLogger(__name__)
SOURCE = "eccc_hourly"
STATIONS = {"6158359": "city_centre", "6158355": "city",
            # archive-only: older station records at the same site, kept for completeness
            "6158667": "city_centre_2006", "6158665": "toronto_island_a"}
# full hourly record per station (first year, last year or None = still reporting)
STATION_YEARS = {"6158359": (2009, None), "6158355": (2002, None), "6158667": (2006, 2010), "6158665": (1957, 2006)}
LIVE_STATIONS = [k for k, (_, last) in STATION_YEARS.items() if last is None]
FIELDS = {
    "TEMP": "temp_c",
    "DEW_POINT_TEMP": "dewpoint_c",
    "HUMIDEX": "humidex",
    "PRECIP_AMOUNT": "precip_mm",
    "RELATIVE_HUMIDITY": "rh",
    "WIND_SPEED": "wind_kmh",
    "WIND_DIRECTION": "wind_dir_10deg",
    "VISIBILITY": "visibility_km",
    "STATION_PRESSURE": "pressure_kpa",
    "WINDCHILL": "windchill",
    "WEATHER_ENG_DESC": "weather_desc",
}
PAGE = 10000


def fetch_range(climate_id: str, start: datetime, end: datetime) -> int:
    """Fetch [start, end) for one station, paging. Returns rows fetched."""
    offset, total = 0, 0
    dt = f"{start:%Y-%m-%dT%H:%M:%SZ}/{end:%Y-%m-%dT%H:%M:%SZ}"
    while True:
        params = {"f": "json", "CLIMATE_IDENTIFIER": climate_id, "datetime": dt,
                  "limit": PAGE, "offset": offset, "sortby": "UTC_DATE"}
        rec = fetch(SOURCE, f"{ECCC_API}/collections/climate-hourly/items", params,
                    extra={"climate_id": climate_id})
        j = json.loads(rec.read())
        n = j.get("numberReturned", len(j.get("features", [])))
        total += n
        matched = j.get("numberMatched")
        log.info("%s %s offset=%d got=%d matched=%s", climate_id, dt, offset, n, matched)
        if n < PAGE:
            break
        offset += n
    return total


def parse_all() -> pd.DataFrame:
    """Parse every archived page into one de-duplicated observation table (latest fetch wins)."""
    frames = []
    for e in iter_manifest(SOURCE):
        if e.get("status") != 200 or e.get("valid") is False:
            continue
        j = load_json(e)
        if j is None:
            continue
        rows = [f["properties"] for f in j.get("features", [])]
        if not rows:
            continue
        df = pd.DataFrame(rows)
        df["fetched_at"] = e["fetched_at"]
        frames.append(df)
    if not frames:
        return pd.DataFrame()
    raw = pd.concat(frames, ignore_index=True)
    missing = set(FIELDS) - set(raw.columns)
    if missing:
        raise RuntimeError(f"ECCC schema changed: missing fields {missing}")
    out = pd.DataFrame({
        "climate_id": raw["CLIMATE_IDENTIFIER"].astype(str),
        "ts_utc": pd.to_datetime(raw["UTC_DATE"]).dt.tz_localize("UTC"),
        "fetched_at": pd.to_datetime(raw["fetched_at"], utc=True),
    })
    for k, v in FIELDS.items():
        out[v] = raw[k]
    for k in FIELDS:
        if k + "_FLAG" in raw:
            out[FIELDS[k] + "_flag"] = raw[k + "_FLAG"]
    out["weather_desc"] = out["weather_desc"].replace({"NA": None})
    out = (out.sort_values("fetched_at").drop_duplicates(["climate_id", "ts_utc"], keep="last")
              .sort_values(["climate_id", "ts_utc"]).reset_index(drop=True))
    out["station"] = out["climate_id"].map(STATIONS)
    return out


def load_db() -> int:
    df = parse_all()
    with db.connect() as con:
        con.execute("CREATE OR REPLACE TABLE eccc_hourly AS SELECT * FROM df")
    return len(df)


def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backfill")
    b.add_argument("--start", type=int, default=None, help="default: each station's first year")
    b.add_argument("--end", type=int, default=None, help="default: each station's last year")
    b.add_argument("--stations", nargs="*", default=list(STATIONS))
    b.add_argument("--skip-archived", action="store_true", help="skip station-years already archived")
    sub.add_parser("update")
    sub.add_parser("parse")
    a = ap.parse_args(argv)
    if a.cmd == "backfill":
        done = set()
        if a.skip_archived:
            for e in iter_manifest(SOURCE, usable_only=True):
                dt = e["params"].get("datetime", "")
                if dt.endswith("-01-01T00:00:00Z") and e["params"].get("offset", 0) == 0:
                    done.add((e.get("climate_id"), int(dt[:4])))
        this_year = datetime.now().year
        for cid in a.stations:
            first, last = STATION_YEARS.get(cid, (2015, None))
            for y in range(a.start or first, (a.end or last or this_year) + 1):
                if (cid, y) in done and y < this_year:
                    continue
                fetch_range(cid, datetime(y, 1, 1, tzinfo=UTC), datetime(y + 1, 1, 1, tzinfo=UTC))
    elif a.cmd == "update":
        now = datetime.now(UTC)
        for cid in LIVE_STATIONS:
            fetch_range(cid, now - timedelta(days=10), now + timedelta(hours=1))
    print("eccc_hourly rows:", load_db())


if __name__ == "__main__":
    setup_logging()
    main()
