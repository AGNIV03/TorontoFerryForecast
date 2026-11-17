"""City of Toronto ferry ticket counts.

Raw archive sources:
  ferry_dump   full datastore CSV dump (backfill; weekly refresh to catch revisions)
  ferry_api    datastore_search pages of the newest rows (hourly incremental)

Parsed table `ferry_15min`: one row per 15-min interval that the City published, with
the value from the most recent fetch (so later revisions win). Timestamps are local wall
clock time, America/Toronto (verified: first boarding stays at 06:30 across every DST
change), stored naive as `ts_local`.

Usage:
    python -m ingest.ferry dump      # full CSV dump
    python -m ingest.ferry update    # newest ~2000 rows via the API
    python -m ingest.ferry parse
"""

from __future__ import annotations

import argparse
import io
import logging

import pandas as pd

from common import db
from common.archive import iter_manifest, load_json, load_raw
from common.config import CKAN, FERRY_RESOURCE
from common.http import fetch
from common.timeutil import setup_logging

log = logging.getLogger(__name__)
EXPECTED = ["_id", "Timestamp", "Redemption Count", "Sales Count"]


class SchemaChanged(RuntimeError):
    pass


def fetch_dump():
    return fetch("ferry_dump", f"{CKAN}/datastore/dump/{FERRY_RESOURCE}", ext="csv",
                 extra={"kind": "full_dump"})


def fetch_recent(limit: int = 2000):
    return fetch("ferry_api", f"{CKAN}/api/3/action/datastore_search",
                 {"resource_id": FERRY_RESOURCE, "limit": limit, "sort": "Timestamp desc"})


def _check(cols: list[str], where: str):
    if [c for c in EXPECTED if c not in cols]:
        raise SchemaChanged(f"ferry schema changed in {where}: got {cols}")


def parse_all() -> pd.DataFrame:
    """Latest full dump + every incremental API page fetched after it (later fetch wins).

    Older dumps and pages stay in the raw archive but are superseded by the latest dump,
    which keeps the hourly rebuild fast (a dump is refreshed weekly).
    """
    frames = []
    dumps = [e for e in iter_manifest("ferry_dump") if e.get("status") == 200]
    if not dumps:
        raise RuntimeError("no ferry dump archived yet: run `python -m ingest.ferry dump`")
    e = dumps[-1]
    since = pd.Timestamp(e["fetched_at"])
    df = pd.read_csv(io.BytesIO(load_raw(e)))
    _check(list(df.columns), e["file"])
    df["fetched_at"] = e["fetched_at"]
    frames.append(df)
    for e in iter_manifest("ferry_api"):
        if e.get("status") != 200 or e.get("valid") is False or pd.Timestamp(e["fetched_at"]) <= since:
            continue
        j = load_json(e)
        if j is None:
            continue
        recs = j["result"]["records"]
        if not recs:
            continue
        df = pd.DataFrame(recs)
        _check(list(df.columns), e["file"])
        df["fetched_at"] = e["fetched_at"]
        frames.append(df)
    raw = pd.concat(frames, ignore_index=True)
    out = pd.DataFrame({
        "ts_local": pd.to_datetime(raw["Timestamp"]),
        "redemptions": pd.to_numeric(raw["Redemption Count"]).astype("int64"),
        "sales": pd.to_numeric(raw["Sales Count"]).astype("int64"),
        "fetched_at": pd.to_datetime(raw["fetched_at"], utc=True),
    })
    if (out.ts_local.dt.minute % 15 != 0).any():
        raise SchemaChanged("ferry timestamps no longer on 15-minute boundaries")
    out = out.sort_values("fetched_at", kind="stable")
    n_versions = out.groupby("ts_local").agg(nv=("redemptions", "nunique"), ns=("sales", "nunique"))
    revised = int(((n_versions.nv > 1) | (n_versions.ns > 1)).sum())
    out = out.drop_duplicates("ts_local", keep="last").sort_values("ts_local").reset_index(drop=True)
    log.info("ferry_15min rows=%d (intervals revised across fetches: %d)", len(out), revised)
    return out


def load_db() -> int:
    df = parse_all()
    with db.connect() as con:
        con.execute("CREATE OR REPLACE TABLE ferry_15min AS SELECT * FROM df")
    return len(df)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["dump", "update", "parse"])
    a = ap.parse_args(argv)
    if a.cmd == "dump":
        fetch_dump()
    elif a.cmd == "update":
        fetch_recent()
    print("ferry_15min rows:", load_db())


if __name__ == "__main__":
    setup_logging()
    main()
