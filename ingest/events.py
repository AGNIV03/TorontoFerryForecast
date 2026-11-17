"""City of Toronto Festivals & Events feed (current + upcoming only), archived daily.

The JSON feed is large (~240 MB, ~18 MB gzipped) and lists only current/upcoming
events, so the daily archive is the only way to build event history going forward.
A historical XML feed (Jan 2014 - Dec 2016) also exists and is archived once.

Parsed table `city_events`: one row per event-date at Island/waterfront locations,
with first_seen (the fetch at which we first saw it - this is what was knowable when).

Usage:
    python -m ingest.events fetch
    python -m ingest.events historical
    python -m ingest.events parse
"""

from __future__ import annotations

import argparse
import logging
import re

import pandas as pd

from common import db
from common.archive import iter_manifest, load_json
from common.config import CKAN
from common.http import fetch
from common.timeutil import setup_logging

log = logging.getLogger(__name__)
BASE = f"{CKAN}/dataset/9201059e-43ed-4369-885e-0b867652feac/resource"
FEED = f"{BASE}/8900fdb2-7f6c-4f50-8581-b463311ff05d/download/festivals-and-events-json-feed.json"
HIST = (f"{BASE}/74c4c0b7-51ef-4a67-83b5-5375842214e2/download/"
        "festivals-and-events-historical-xml-feed-jan-2014-dec-2016.xml")

ISLAND = re.compile(r"toronto island|centre island|center island|hanlan|ward'?s island|"
                    r"algonquin island|olympic island|island park|gibraltar point", re.I)
WATERFRONT = re.compile(r"harbourfront|queens quay|jack layton|ferry terminal|sugar beach|"
                        r"sherbourne common|harbour square|music garden|ontario place|"
                        r"exhibition place|trillium park", re.I)


def fetch_feed():
    return fetch("events_feed", FEED, extra={"kind": "json_feed"})


def fetch_historical():
    return fetch("events_feed_historical", HIST, ext="xml")


def _rows_from_feed(j: dict, fetched_at: str):
    for ev in j.get("value", []):
        locs = " | ".join(
            str(l.get("location_name", "")) + " " + str(l.get("location_address", ""))
            for l in (ev.get("event_locations") or []) if isinstance(l, dict))
        text = f"{ev.get('event_name', '')} {locs}"
        zone = "island" if ISLAND.search(text) else "waterfront" if WATERFRONT.search(text) else None
        if zone is None:
            continue
        for d in ev.get("event_dates") or []:
            ts = pd.to_datetime(d.get("date"), unit="ms", utc=True)
            yield {
                "event_id": ev.get("id"),
                "name": ev.get("event_name"),
                "zone": zone,
                "categories": ";".join(ev.get("event_category") or []),
                "date_local": ts.tz_convert("America/Toronto").date(),
                "start_utc": ts,
                "end_utc": pd.to_datetime(ev.get("event_enddate"), errors="coerce", utc=True),
                "free": ev.get("free_event"),
                "locations": locs[:500],
                "fetched_at": fetched_at,
            }


def _parse_entries(entries) -> pd.DataFrame:
    rows = []
    for e in entries:
        j = load_json(e)
        if j is not None:
            rows.extend(_rows_from_feed(j, e["fetched_at"]))
    df = pd.DataFrame(rows)
    if not df.empty:
        df["fetched_at"] = pd.to_datetime(df["fetched_at"], utc=True)
    return df


def _merge(old: pd.DataFrame | None, new: pd.DataFrame) -> pd.DataFrame:
    """Latest details win; first_seen = earliest fetch that listed the event-date.
    Event-dates that later vanish from the feed are kept (the feed only shows upcoming events)."""
    new = new.rename(columns={"fetched_at": "first_seen"})
    new["last_seen"] = new["first_seen"]
    new["date_local"] = pd.to_datetime(new["date_local"])
    if old is not None and not old.empty:
        old = old.copy()
        old["date_local"] = pd.to_datetime(old["date_local"])  # DuckDB returns timestamps
        for c in ("first_seen", "last_seen"):
            old[c] = pd.to_datetime(old[c], utc=True)
        both = pd.concat([old, new], ignore_index=True)
    else:
        both = new
    keys = ["event_id", "date_local"]
    agg = both.groupby(keys).agg(first_seen=("first_seen", "min"), last_seen=("last_seen", "max")).reset_index()
    latest = both.sort_values("last_seen").drop_duplicates(keys, keep="last").drop(columns=["first_seen", "last_seen"])
    return latest.merge(agg, on=keys).sort_values(["date_local", "name"]).reset_index(drop=True)


def load_db(full: bool = False) -> int:
    """Incremental by default: merge only the newest archived feed into city_events."""
    entries = [e for e in iter_manifest("events_feed") if e.get("status") == 200]
    if not entries:
        return 0
    with db.connect() as con:
        exists = con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name='city_events'").fetchone()[0]
        old = None if (full or not exists) else con.execute("SELECT * FROM city_events").df()
        todo = entries if (full or not exists) else entries[-1:]
        df = old
        for e in todo:
            df = _merge(df, _parse_entries([e]))
        con.execute("CREATE OR REPLACE TABLE city_events AS SELECT * FROM df")
    return len(df)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["fetch", "historical", "parse"])
    ap.add_argument("--full", action="store_true", help="re-parse every archived feed")
    a = ap.parse_args(argv)
    if a.cmd == "fetch":
        fetch_feed()
    elif a.cmd == "historical":
        fetch_historical()
        return
    print("city_events rows:", load_db(full=a.full))


if __name__ == "__main__":
    setup_logging()
    main()
