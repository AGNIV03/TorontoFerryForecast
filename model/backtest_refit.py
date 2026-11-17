"""Sensitivity check: point-model backtest with QUARTERLY refits (closer to production's
weekly retraining than the yearly folds). fc-mode weather. Writes
data/processed/backtest_quarterly.parquet.  python -m model.backtest_refit"""
import logging

import pandas as pd

from common.config import PROCESSED
from common.timeutil import setup_logging
from model.backtest import HIST_START, ROUNDS, selected_weather_models
from model.core import fit_models, predict, training_mask
from model.dataset import Base, build_rows, fc_weather_versions, obs_weather

setup_logging()
base = Base()
wobs = obs_weather()
rows = build_rows(base, HIST_START, base.last_complete_date, wx_obs=wobs)
fcv = fc_weather_versions(selected_weather_models())
out = []
for q in pd.period_range("2024Q1", pd.Timestamp(base.last_complete_date).to_period("Q"), freq="Q"):
    start, end = q.start_time.normalize(), min(q.end_time.normalize(), base.last_complete_date)
    train_end = start - pd.Timedelta(days=1)
    tr = rows[rows.target_date <= train_end]
    tr = tr[training_mask(tr, base)]
    m = fit_models(tr, ROUNDS, quantiles=())
    te = build_rows(base, start, end, weather="fc", fc_versions=fcv, wx_hist=wobs)
    te = te[(te.target_date <= end) & te.temp_c.notna()]
    p = predict(m, te)
    out.append(te[["issue_date", "target_date", "lead", "hour", "y", "is_peak_season"]].assign(pred=p.pred.values, quarter=str(q)))
    logging.info("%s done", q)
pd.concat(out).to_parquet(PROCESSED / "backtest_quarterly.parquet")
