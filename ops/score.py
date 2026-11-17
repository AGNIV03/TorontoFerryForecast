"""Daily: score past forecasts against actuals -> public accuracy log.

For each recently completed day Y and each lead 0..7, take the first forecast issued at or
after 06:00 local on Y - lead (the one users saw that morning) from data/forecast_log, and
compare it with the actual hourly redemptions (06:00-23:59). Appends one row per (date, lead) to
data/serving/accuracy_log.csv (append-only; each (date, lead) is scored once) and writes a
30-day summary to data/serving/accuracy.json.

    python -m ops.score
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

import numpy as np
import pandas as pd

from common import db
from common.config import DATA, TZ
from common.timeutil import setup_logging
from features.calendar import build_calendar
from model.core import level_keys, to_level
from model import registry

log = logging.getLogger(__name__)
SERVING = DATA / "serving"
FORECAST_LOG = DATA / "forecast_log"
LOG_CSV = SERVING / "accuracy_log.csv"


def _load_forecasts(since: pd.Timestamp) -> pd.DataFrame:
    files = [f for f in FORECAST_LOG.rglob("*.parquet") if f.stem >= since.strftime("%Y%m%dT")]
    if not files:
        return pd.DataFrame()
    return pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)


def run(days_back: int = 3) -> pd.DataFrame:
    now = pd.Timestamp(datetime.now(TZ)).tz_localize(None)
    today = now.normalize()
    fc = _load_forecasts(today - pd.Timedelta(days=days_back + 9))
    if fc.empty:
        log.info("no forecasts logged yet")
        return pd.DataFrame()
    fc["issued_local"] = pd.to_datetime(fc.issued_at, utc=True).dt.tz_convert(TZ).dt.tz_localize(None)
    fc["issue_day"] = fc.issued_local.dt.normalize()
    h = db.table("ferry_hourly")
    h = h[h.complete & (h.outage_day == 0) & (h.gap_flag == 0)]
    done = set()
    if LOG_CSV.exists():
        old = pd.read_csv(LOG_CSV)
        done = set(zip(old.date, old.lead))
    _, thr, _, _ = registry.load()
    cal = build_calendar().set_index("date")
    rows = []
    for k in range(1, days_back + 1):
        y = today - pd.Timedelta(days=k)
        act = h[(h.date == y) & (h.hour >= 6)].set_index("hour").redemptions
        if len(act) < 18:
            continue
        for lead in range(8):
            if (str(y.date()), lead) in done:
                continue
            day_issue = y - pd.Timedelta(days=lead)
            cand = fc[(fc.issue_day == day_issue) & (fc.target_date == y) & (fc.issued_local.dt.hour >= 6)]
            if cand.empty:
                continue
            first = cand.issued_local.min()
            f = cand[cand.issued_local == first].set_index("hour").reindex(act.index)
            c = cal.loc[y]
            seas = level_keys([y], [c.is_school_summer])[0]
            lt = to_level(act.values, np.repeat(seas, len(act)), act.index.values, thr)
            lp = to_level(f.pred.values, np.repeat(seas, len(act)), act.index.values, thr)
            rows.append({
                "date": str(y.date()), "lead": lead, "issued_at_local": str(first),
                "model_version": f.model_version.iloc[0],
                "actual_total": int(act.sum()), "pred_total": round(float(f.pred.sum())),
                "daily_pct_error": round(float((f.pred.sum() - act.sum()) / max(act.sum(), 1)), 4),
                "hourly_wape": round(float(np.abs(f.pred - act).sum() / max(act.sum(), 1)), 4),
                "hourly_mae": round(float(np.abs(f.pred - act).mean()), 1),
                "interval_coverage": round(float(((act >= f.p10) & (act <= f.p90)).mean()), 3),
                "level_accuracy": round(float((lt == lp).mean()), 3),
            })
    new = pd.DataFrame(rows)
    if len(new):
        SERVING.mkdir(parents=True, exist_ok=True)
        new.to_csv(LOG_CSV, mode="a", header=not LOG_CSV.exists(), index=False)
    summarize()
    log.info("scored %d (date, lead) pairs", len(new))
    return new


def summarize():
    if not LOG_CSV.exists():
        return
    a = pd.read_csv(LOG_CSV)
    a["date"] = pd.to_datetime(a.date)
    recent = a[a.date >= a.date.max() - pd.Timedelta(days=29)]
    bucket = recent.lead.map(lambda l: "same/next day" if l <= 1 else "2-3 days" if l <= 3 else "4-7 days")
    s = {}
    for b, g in recent.groupby(bucket):
        s[b] = {"days_scored": int(g.date.nunique()),
                "median_daily_abs_pct_error": round(float(g.daily_pct_error.abs().median()), 3),
                "hourly_wape": round(float((g.hourly_wape * g.actual_total).sum() / g.actual_total.sum()), 3),
                "level_accuracy": round(float(g.level_accuracy.mean()), 3),
                "interval_coverage": round(float(g.interval_coverage.mean()), 3)}
    last = a[a.lead == 1].sort_values("date").tail(14)
    out = {"window_days": 30, "by_lead": s,
           "recent_next_day": last[["date", "actual_total", "pred_total", "daily_pct_error"]]
           .assign(date=lambda d: d.date.dt.strftime("%Y-%m-%d")).to_dict("records")}
    tmp = SERVING / "accuracy.json.tmp"
    tmp.write_text(json.dumps(out, indent=1))
    tmp.replace(SERVING / "accuracy.json")


if __name__ == "__main__":
    setup_logging()
    run()
