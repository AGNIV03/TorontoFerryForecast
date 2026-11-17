"""Open-Meteo: historical reanalysis (gap-fill), archived past forecasts, live forecasts.

Sources (raw archive names):
  om_archive        archive-api (ERA5/ECMWF-IFS reanalysis) 2015 -> present, gap filling
  om_previous_runs  previous-runs-api: forecasts issued 0..7 days before each hour,
                    per model. Available from 2024-01-21 (GEM/GFS/ICON) and 2024-02-06
                    (ECMWF IFS 0.25). Used for model selection and honest backtests.
  om_forecast       live forecast for the terminal, all candidate models, saved every 3h.

Usage:
    python -m ingest.openmeteo archive --start 2015-01-01
    python -m ingest.openmeteo previous-runs --start 2024-01-15
    python -m ingest.openmeteo forecast
    python -m ingest.openmeteo parse
"""

from __future__ import annotations

import argparse
import logging
import time
from datetime import date, datetime, timedelta

import pandas as pd

from common import db
from common.archive import iter_manifest, load_json
from common.config import (OM_ARCHIVE, OM_FORECAST, OM_PREVIOUS_RUNS, TERMINAL_LAT,
                           TERMINAL_LON)
from common.http import fetch
from common.timeutil import setup_logging

log = logging.getLogger(__name__)
LOC = {"latitude": TERMINAL_LAT, "longitude": TERMINAL_LON, "timezone": "UTC"}

ARCHIVE_VARS = [
    "temperature_2m", "relative_humidity_2m", "dew_point_2m", "apparent_temperature",
    "precipitation", "rain", "snowfall", "cloud_cover", "wind_speed_10m", "wind_gusts_10m",
    "shortwave_radiation", "sunshine_duration", "weather_code",
]
FC_VARS = [
    "temperature_2m", "relative_humidity_2m", "dew_point_2m", "apparent_temperature",
    "precipitation", "cloud_cover", "wind_speed_10m", "wind_gusts_10m",
    "shortwave_radiation", "weather_code",
]
# variables pulled from the previous-runs API (x 8 lead days each)
PR_VARS = ["temperature_2m", "relative_humidity_2m", "dew_point_2m", "precipitation",
           "cloud_cover", "wind_speed_10m", "wind_gusts_10m", "shortwave_radiation"]
CANDIDATE_MODELS = ["ecmwf_ifs025", "gem_seamless", "gfs_seamless", "icon_seamless",
                    "gem_hrdps_continental", "ecmwf_ifs"]
LIVE_MODELS = CANDIDATE_MODELS + ["best_match"]
MAX_LEAD_DAYS = 7


def _chunks(start: date, end: date, days: int):
    d = start
    while d <= end:
        e = min(d + timedelta(days=days - 1), end)
        yield d, e
        d = e + timedelta(days=1)


def backfill_archive(start: date, end: date):
    for s, e in _chunks(start, end, 366):
        fetch("om_archive", OM_ARCHIVE, {**LOC, "start_date": s.isoformat(), "end_date": e.isoformat(),
                                         "hourly": ",".join(ARCHIVE_VARS)})
        time.sleep(6)


def backfill_previous_runs(start: date, end: date, models=CANDIDATE_MODELS, chunk_days=92):
    hourly = [v if d == 0 else f"{v}_previous_day{d}" for v in PR_VARS for d in range(MAX_LEAD_DAYS + 1)]
    done = {(e.get("model"), e["params"].get("start_date"), e["params"].get("end_date"))
            for e in iter_manifest("om_previous_runs", usable_only=True)}
    for m in models:
        for s, e in _chunks(start, end, chunk_days):
            if (m, s.isoformat(), e.isoformat()) in done and e < date.today() - timedelta(days=8):
                continue  # already archived and no longer changing
            fetch("om_previous_runs", OM_PREVIOUS_RUNS,
                  {**LOC, "models": m, "start_date": s.isoformat(), "end_date": e.isoformat(),
                   "hourly": ",".join(hourly)}, extra={"model": m})
            time.sleep(20)  # weighted call cost is high; stay far below the per-minute limit


def fetch_forecast():
    return fetch("om_forecast", OM_FORECAST,
                 {**LOC, "models": ",".join(LIVE_MODELS), "hourly": ",".join(FC_VARS),
                  "forecast_days": 9, "past_days": 1})


# ---------------------------------------------------------------- parsing

def parse_archive() -> pd.DataFrame:
    frames = []
    for e in iter_manifest("om_archive"):
        if e.get("status") != 200 or e.get("valid") is False:
            continue
        j = load_json(e)
        if j is None or "hourly" not in j:
            continue
        h = j["hourly"]
        df = pd.DataFrame(h)
        df["fetched_at"] = e["fetched_at"]
        frames.append(df)
    df = pd.concat(frames, ignore_index=True)
    df["ts_utc"] = pd.to_datetime(df.pop("time")).dt.tz_localize("UTC")
    df["fetched_at"] = pd.to_datetime(df["fetched_at"], utc=True)
    df = df.sort_values("fetched_at").drop_duplicates("ts_utc", keep="last")
    return df.sort_values("ts_utc").reset_index(drop=True)


def parse_previous_runs() -> pd.DataFrame:
    """Long table: model, ts_utc, lead_day (0..7), variable columns."""
    frames = []
    for e in iter_manifest("om_previous_runs"):
        if e.get("status") != 200 or e.get("valid") is False:
            continue
        j = load_json(e)
        if j is None:
            continue
        h = j["hourly"]
        t = pd.to_datetime(h["time"]).tz_localize("UTC")
        for d in range(MAX_LEAD_DAYS + 1):
            cols = {v: h.get(v if d == 0 else f"{v}_previous_day{d}") for v in PR_VARS}
            df = pd.DataFrame({k: v for k, v in cols.items() if v is not None})
            df.insert(0, "ts_utc", t)
            df.insert(0, "lead_day", d)
            df.insert(0, "model", e["model"])
            df["fetched_at"] = e["fetched_at"]
            frames.append(df)
    df = pd.concat(frames, ignore_index=True)
    df = df.dropna(subset=["temperature_2m"])
    df["fetched_at"] = pd.to_datetime(df["fetched_at"], utc=True)
    df = df.sort_values("fetched_at").drop_duplicates(["model", "ts_utc", "lead_day"], keep="last")
    return df.sort_values(["model", "lead_day", "ts_utc"]).reset_index(drop=True)


def parse_forecasts(skip_files: set[str] = frozenset()) -> pd.DataFrame:
    """Archived live forecasts not yet parsed: issued_at (fetch time), model, ts_utc, vars."""
    frames = []
    for e in iter_manifest("om_forecast"):
        if e.get("status") != 200 or e.get("valid") is False or e["file"] in skip_files:
            continue
        j = load_json(e)
        if j is None or "hourly" not in j:
            continue
        h = j["hourly"]
        t = pd.to_datetime(h["time"]).tz_localize("UTC")
        for m in LIVE_MODELS:
            cols = {v: h.get(f"{v}_{m}") for v in FC_VARS}
            if all(c is None for c in cols.values()):
                continue
            df = pd.DataFrame({k: v for k, v in cols.items() if v is not None})
            df.insert(0, "ts_utc", t)
            df.insert(0, "model", m)
            df.insert(0, "issued_at", pd.Timestamp(e["fetched_at"]))
            df["file"] = e["file"]
            frames.append(df)
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    for v in FC_VARS:  # stable schema even if a model drops a variable
        if v not in out:
            out[v] = float("nan")
    return out[["issued_at", "model", "ts_utc", *FC_VARS, "file"]]


def load_db(which=("archive", "previous_runs", "forecasts")):
    with db.connect() as con:
        if "archive" in which and any(True for _ in iter_manifest("om_archive")):
            a = parse_archive()
            con.execute("CREATE OR REPLACE TABLE om_archive AS SELECT * FROM a")
            log.info("om_archive rows=%d", len(a))
        if "previous_runs" in which and any(True for _ in iter_manifest("om_previous_runs")):
            p = parse_previous_runs()
            con.execute("CREATE OR REPLACE TABLE om_previous_runs AS SELECT * FROM p")
            log.info("om_previous_runs rows=%d", len(p))
        if "forecasts" in which and any(True for _ in iter_manifest("om_forecast")):
            exists = con.execute("SELECT count(*) FROM information_schema.tables "
                                 "WHERE table_name='om_forecasts'").fetchone()[0]
            cols = [r[0] for r in con.execute("DESCRIBE om_forecasts").fetchall()] if exists else []
            if exists and "file" in cols:
                done = {r[0] for r in con.execute("SELECT DISTINCT file FROM om_forecasts").fetchall()}
                f = parse_forecasts(done)
                if len(f):
                    con.execute("INSERT INTO om_forecasts SELECT * FROM f")
            else:
                f = parse_forecasts()
                con.execute("CREATE OR REPLACE TABLE om_forecasts AS SELECT * FROM f")
            log.info("om_forecasts new rows=%d", len(f))


def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    a1 = sub.add_parser("archive")
    a1.add_argument("--start", default="2015-01-01")
    a1.add_argument("--end", default=None)
    a2 = sub.add_parser("previous-runs")
    a2.add_argument("--start", default="2024-01-15")
    a2.add_argument("--end", default=None)
    a2.add_argument("--models", nargs="*", default=CANDIDATE_MODELS)
    sub.add_parser("forecast")
    sub.add_parser("parse")
    sub.add_parser("update", help="daily: last 10 days of reanalysis + archived past forecasts")
    a = ap.parse_args(argv)
    today = datetime.now().date()
    if a.cmd == "update":
        backfill_archive(today - timedelta(days=12), today - timedelta(days=2))
        backfill_previous_runs(today - timedelta(days=10), today - timedelta(days=1), chunk_days=10)
        load_db(("archive", "previous_runs"))
        return
    if a.cmd == "archive":
        # the archive lags real time by a few days
        backfill_archive(date.fromisoformat(a.start), date.fromisoformat(a.end) if a.end else today - timedelta(days=2))
        load_db(("archive",))
    elif a.cmd == "previous-runs":
        backfill_previous_runs(date.fromisoformat(a.start),
                               date.fromisoformat(a.end) if a.end else today - timedelta(days=1), a.models)
        load_db(("previous_runs",))
    elif a.cmd == "forecast":
        fetch_forecast()
        load_db(("forecasts",))
    else:
        load_db()


if __name__ == "__main__":
    setup_logging()
    main()
