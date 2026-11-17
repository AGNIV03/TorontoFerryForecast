"""Issue the live forecast (hourly job).

* Weather: the most recent archived Open-Meteo forecast, blended per lead (N = whole days
  ahead) with the models chosen in data/models/weather_models.json.
* Features: identical to training (issue date = today), see model/dataset.py.
* Same-day adjustment (lead 0 only, once 06:00-11:59 are counted): scales today's remaining hours by the
  shrunk ratio of actual to predicted boardings so far (validated in the backtest, see
  reports/model_report.md).
* Outputs:
    data/serving/forecast.json          (read by the API)
    data/forecast_log/YYYY/MM/DD/<issued>.parquet   (append-only, for accuracy scoring)

    python -m model.predict_live
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

import numpy as np
import pandas as pd

from common import db
from common.config import DATA, MODELS, TZ
from common.timeutil import iso_local
from common.timeutil import setup_logging
from model import registry
from model.core import apply_interval_calibration, climatology_table, level_keys, predict, to_level
from model.dataset import HOURS, Base, build_rows
from features.weather import humidex

log = logging.getLogger(__name__)
SERVING = DATA / "serving"
FORECAST_LOG = DATA / "forecast_log"
NOWCAST_SHRINK = 400.0   # pseudo-count of boardings for the same-day ratio
NOWCAST_CLIP = (0.5, 1.8)
NOWCAST_MIN_HOURS = 6   # 06:00-11:59 counted; at 10:00 the adjustment slightly hurt in the backtest


def live_weather(models_by_n: dict[int, list[str]], now_utc: pd.Timestamp) -> tuple[pd.DataFrame, pd.Timestamp]:
    with db.connect(read_only=True) as con:
        issued = con.execute("SELECT max(issued_at) FROM om_forecasts").fetchone()[0]
        f = con.execute("SELECT * FROM om_forecasts WHERE issued_at = ?", [issued]).df()
    f["N"] = ((f.ts_utc - now_utc).dt.total_seconds() // 86400).clip(0, 7).astype(int)
    parts = []
    for n, g in f.groupby("N"):
        want = models_by_n.get(int(n), ["best_match"])
        sel = g[g.model.isin(want)]
        if sel.empty:
            sel = g[g.model == "best_match"]
        parts.append(sel)
    f = pd.concat(parts)
    num = ["temperature_2m", "relative_humidity_2m", "dew_point_2m", "precipitation", "cloud_cover",
           "wind_speed_10m", "wind_gusts_10m", "shortwave_radiation"]
    g = f.groupby("ts_utc")[num].mean().reset_index()
    wx = pd.DataFrame({"ts_utc": g.ts_utc, "temp_c": g.temperature_2m, "rh": g.relative_humidity_2m,
                       "precip_mm": g.precipitation, "wind_kmh": g.wind_speed_10m, "gust_kmh": g.wind_gusts_10m,
                       "cloud_cover": g.cloud_cover, "radiation": g.shortwave_radiation})
    wx["humidex"] = humidex(wx.temp_c, g.dew_point_2m)
    return wx, pd.Timestamp(issued)


def publish_outlook(base, today, thr, now_utc, horizon: int = 180):
    from model import outlook
    if not (MODELS / "outlook" / "model.txt").exists():
        log.warning("no outlook model yet: run `python -m model.outlook train`")
        return
    ol = outlook.predict(base, today, horizon=horizon, start_lead=8)
    keys = level_keys(ol.target_date.values, ol.is_school_summer.values)
    days = []
    for (_, r), key in zip(ol.iterrows(), keys):
        hrs = np.asarray(r.hourly)[6:24]
        lv = to_level(hrs, np.repeat(key, 18), np.arange(6, 24), thr)
        days.append({"date": str(r.target_date.date()), "lead": int(r.lead), "dow": int(r.dow),
                     "holiday": str(r.holiday_kind), "is_long_weekend": bool(r.is_long_weekend),
                     "period": key.split("|")[0], "total": round(float(r.pred)),
                     "total_p10": round(float(r.p10)), "total_p90": round(float(r.p90)),
                     "peak_hour": int(6 + np.argmax(hrs)), "peak_level": int(lv.max()),
                     "hours": [{"hour": 6 + i, "pred": round(float(v), 1), "level": int(x)}
                               for i, (v, x) in enumerate(zip(hrs, lv))]})
    out = {"generated_at": iso_local(now_utc), "issue_date": str(today.date()), "horizon_days": horizon,
           "note": ("Long-range outlook: typical boardings for each date given the season, weekday, holidays and the "
                    "multi-year trend. No weather is used, so any single day can be 40% higher or lower."),
           "days": days}
    tmp = SERVING / "outlook.json.tmp"
    tmp.write_text(json.dumps(out))
    tmp.replace(SERVING / "outlook.json")
    log.info("outlook issued: %d days", len(days))


def nowcast_factor(actual_so_far: float, pred_so_far: float) -> float:
    r = (actual_so_far + NOWCAST_SHRINK) / (pred_so_far + NOWCAST_SHRINK)
    return float(np.clip(r, *NOWCAST_CLIP))


def run(now: datetime | None = None) -> dict:
    now_local = pd.Timestamp(now or datetime.now(TZ)).tz_convert(TZ)
    now_utc = now_local.tz_convert("UTC")
    today = now_local.tz_localize(None).normalize()
    models, thr, meta, mdir = registry.load()
    wsel = json.loads((mdir / "weather_models.json").read_text())["by_n"]
    wsel = {int(k): v for k, v in wsel.items()}
    base = Base()
    wx, wx_issued = live_weather(wsel, now_utc)
    rows = build_rows(base, today, today, wx_obs=wx, require_target=False, wx_hist=db.table("weather_obs"))
    pr = predict(models, rows)
    rows = pd.concat([rows.reset_index(drop=True), pr.reset_index(drop=True)], axis=1)
    calp = MODELS / "interval_calibration.json"
    if calp.exists():  # widen the quantile ranges to their measured forecast-mode coverage
        rows["p10"], rows["p90"] = apply_interval_calibration(rows, json.loads(calp.read_text()))

    # same-day adjustment from today's complete hours (helps from ~noon on; see model report)
    t0 = rows.lead == 0
    done_hours = [h for h in HOURS if np.isfinite(base.R[base.idx[today], h])]
    factor = 1.0
    if t0.any() and len(done_hours) >= NOWCAST_MIN_HOURS:
        act = float(np.nansum(base.R[base.idx[today], done_hours]))
        prd = float(rows[t0 & rows.hour.isin(done_hours)].pred.sum())
        factor = nowcast_factor(act, prd)
        future = t0 & ~rows.hour.isin(done_hours)
        for c in ("pred", "p10", "p90"):
            rows.loc[future, c] *= factor

    seas = level_keys(rows.target_date.values, rows.is_school_summer.values)
    rows["level"] = to_level(rows.pred.values, seas, rows.hour.values, thr)
    clim = climatology_table(base, base.last_complete_date)
    rows = rows.merge(clim.rename(columns={"clim": "typical"}),
                      left_on=[np.minimum(rows.woy, 52), "dow", "hour"], right_on=["woy", "dow", "hour"],
                      how="left", suffixes=("", "_c"))
    dq = {}
    p = MODELS / "daily_error_quantiles.json"
    if p.exists():
        dq = json.loads(p.read_text())

    days = []
    for (tdate, lead), g in rows.groupby(["target_date", "lead"]):
        hours = []
        for _, r in g.sort_values("hour").iterrows():
            observed = base.R[base.idx[tdate], int(r.hour)] if lead == 0 else np.nan
            hours.append({
                "hour": int(r.hour), "pred": round(float(r.pred), 1), "p10": round(float(r.p10), 1),
                "p90": round(float(r.p90), 1), "level": int(r.level),
                "typical": None if pd.isna(r.typical) else round(float(r.typical), 1),
                "observed": None if not np.isfinite(observed) else int(observed),
                "temp_c": None if pd.isna(r.temp_c) else round(float(r.temp_c), 1),
                "precip_mm": None if pd.isna(r.precip_mm) else round(float(r.precip_mm), 1),
            })
        total = float(g.pred.sum())
        q = dq.get(str(int(lead)), {"q10": -0.25, "q90": 0.25})
        days.append({
            "date": str(tdate.date()), "lead": int(lead), "dow": int(g.dow.iloc[0]),
            "is_holiday": bool(g.is_holiday.iloc[0]), "holiday": str(g.holiday_kind.iloc[0]),
            "total": round(total), "total_p10": round(max(total * (1 + q["q10"]), 0)),
            "total_p90": round(total * (1 + q["q90"])),
            "typical_total": None if g.typical.isna().all() else round(float(g.typical.sum())),
            "tmax": None if pd.isna(g.d_tmax.iloc[0]) else round(float(g.d_tmax.iloc[0]), 1),
            "precip_mm": None if pd.isna(g.d_precip.iloc[0]) else round(float(g.d_precip.iloc[0]), 1),
            "hours": hours,
        })
    last_interval = db.table("ferry_15min").ts_local.max()
    out = {
        "generated_at": iso_local(now_utc), "issue_date": str(today.date()), "timezone": "America/Toronto (EST/EDT)",
        "model_version": meta["version"], "model_train_end": meta["train_end"],
        "weather_issued_at": iso_local(wx_issued), "weather_models": wsel,
        "ferry_last_interval_local": str(last_interval), "same_day_factor": round(factor, 3),
        "levels": ["Quiet", "Moderate", "Busy", "Very busy"],
        "days": days,
    }
    SERVING.mkdir(parents=True, exist_ok=True)
    tmp = SERVING / "forecast.json.tmp"
    tmp.write_text(json.dumps(out))
    tmp.replace(SERVING / "forecast.json")

    logdir = FORECAST_LOG / now_utc.strftime("%Y/%m/%d")
    logdir.mkdir(parents=True, exist_ok=True)
    flog = rows[["issue_date", "target_date", "lead", "hour", "pred", "p10", "p90", "level"]].copy()
    flog["issued_at"] = now_local
    flog["model_version"] = meta["version"]
    flog["same_day_factor"] = factor
    flog.to_parquet(logdir / f"{now_utc:%Y%m%dT%H%M%SZ}.parquet", index=False)
    log.info("forecast issued: %d days, same-day factor %.2f", len(days), factor)
    try:
        publish_outlook(base, today, thr, now_utc)
    except Exception:  # the 7-day forecast must not fail because of the long-range outlook
        log.exception("outlook failed")
    return out


if __name__ == "__main__":
    setup_logging()
    run()
