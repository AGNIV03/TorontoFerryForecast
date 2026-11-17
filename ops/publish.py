"""Publish data for the API's explore views (daily job + after each retrain).

Writes to data/serving/ (the API never opens DuckDB):
  history_hourly.parquet  every hour since 2015: boardings (cleaned), sales, raw, flags, observed weather
  history_daily.parquet   every day: totals, weather summary, holiday/occasion, regime, level counts
  schedule.json           schedule periods (config/schedule.json) + capacity + today's period

    python -m ops.publish
"""

from __future__ import annotations

import json
import logging

import numpy as np
import pandas as pd

from common import db
from common.config import CONFIG_DIR, DATA
from common.timeutil import iso_local, now_local, setup_logging
from features.calendar import build_calendar
from model.capacity import capacities, config as cap_config, period_keys
from model.core import to_level, level_thresholds, level_scheme
from model.dataset import Base

log = logging.getLogger(__name__)
SERVING = DATA / "serving"


def _write(df: pd.DataFrame, name: str):
    tmp = SERVING / (name + ".tmp")
    df.to_parquet(tmp, index=False)
    tmp.replace(SERVING / name)   # atomic: the API never sees a half-written file


def history(base: Base):
    h = db.table("ferry_hourly")
    w = db.table("weather_obs")[["ts_utc", "temp_c", "humidex", "precip_mm", "wind_kmh", "cloud_cover"]]
    w["ts_local"] = w.ts_utc.dt.tz_convert("America/Toronto").dt.tz_localize(None).dt.floor("h")
    w = w.drop(columns="ts_utc").drop_duplicates("ts_local")
    cal = build_calendar(start=str(h.date.min().date()), end=str(h.date.max().date())).set_index("date")
    h = h.merge(w, on="ts_local", how="left")
    h["valid"] = h.complete & (h.outage_day == 0) & (h.gap_flag == 0)
    h["regime"] = cal.regime.reindex(h.date).values
    h["holiday"] = cal.holiday_kind.reindex(h.date).values
    h["school_summer"] = cal.is_school_summer.reindex(h.date).values
    h["level_key"] = period_keys(h.date.values, h.school_summer.values)
    thr = level_thresholds(base, base.last_complete_date)
    h["level"] = np.where(h.valid & h.hour.between(6, 23),
                          to_level(h.redemptions.values, h.level_key.values, h.hour.values.clip(6, 23), thr), -1)
    hourly = h[["ts_local", "date", "hour", "redemptions", "sales", "red_raw", "valid", "refill_flag", "bulk_flag",
                "gap_flag", "regime", "holiday", "level_key", "level", "temp_c", "humidex", "precip_mm",
                "wind_kmh", "cloud_cover"]].rename(columns={"redemptions": "boardings", "red_raw": "boardings_raw"})
    _write(hourly, "history_hourly.parquet")

    occ = pd.Series(base.occ_key, index=base.dates)
    day = h[h.hour.between(6, 23)].groupby("date").agg(
        boardings=("redemptions", "sum"), sales=("sales", "sum"), valid=("valid", "all"),
        tmax=("temp_c", "max"), precip_mm=("precip_mm", "sum"), cloud=("cloud_cover", "mean"),
        regime=("regime", "first"), holiday=("holiday", "first"), level_key=("level_key", "first"),
        peak_hour_boardings=("redemptions", "max"), busy_hours=("level", lambda s: int((s >= 2).sum())),
        very_busy_hours=("level", lambda s: int((s >= 3).sum()))).reset_index()
    day["occasion"] = occ.reindex(day.date).fillna("").values
    day["dow"] = day.date.dt.weekday
    _write(day, "history_daily.parquet")
    log.info("published history: %d hours, %d days", len(hourly), len(day))


def schedule():
    s = json.loads((CONFIG_DIR / "schedule.json").read_text())
    caps = capacities()
    today = pd.Timestamp(now_local().date())
    cal = build_calendar(start=str(today.date()), end=str((today + pd.Timedelta(days=400)).date())).set_index("date")
    keys = period_keys(cal.index, cal.is_school_summer.values)
    s["capacity_per_hour"] = {k: round(v) for k, v in caps.items()}
    s["level_shares"] = cap_config()["level_shares"]
    s["level_scheme"] = level_scheme()
    s["today"] = {"date": str(today.date()), "period": keys[0].split("|")[0], "period_key": keys[0]}
    changes = []
    for i in range(1, len(keys)):
        if keys[i].split("|")[0] != keys[i - 1].split("|")[0]:
            changes.append({"date": str(cal.index[i].date()), "period": keys[i].split("|")[0]})
    s["upcoming_changes"] = changes[:4]
    s["generated_at"] = iso_local(now_local())
    tmp = SERVING / "schedule.json.tmp"
    tmp.write_text(json.dumps(s, indent=1))
    tmp.replace(SERVING / "schedule.json")


def run():
    SERVING.mkdir(parents=True, exist_ok=True)
    base = Base()
    history(base)
    schedule()


if __name__ == "__main__":
    setup_logging()
    run()
