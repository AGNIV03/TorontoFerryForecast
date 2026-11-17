"""Rolling-origin backtest on the 2024, 2025 and 2026-to-date holdout.

For each fold the models are trained only on targets up to the end of the previous year,
then forecast every issue date in the test year (06:00 issue, leads 0-7, hours 06-23).
Two weather modes on the same trained model:
  obs : observed weather for the target hours ("perfect forecast" ceiling)
  fc  : archived real forecasts issued >= (lead+1)*24 h before valid time (honest)

    python -m model.backtest            -> data/processed/backtest.parquet
"""

from __future__ import annotations

import json
import logging
import time

import pandas as pd

from common.config import MODELS, PROCESSED
from common.timeutil import setup_logging
from model.core import (add_baselines, level_keys, climatology_table, fit_models, level_thresholds, predict,
                        season_key, to_level, training_mask)
from model.dataset import Base, build_rows, fc_weather_versions, obs_weather

log = logging.getLogger(__name__)
HIST_START = "2015-08-20"
FOLDS = [("2023-12-31", "2024-01-01", "2024-12-31"),
         ("2024-12-31", "2025-01-01", "2025-12-31"),
         ("2025-12-31", "2026-01-01", "2026-12-31")]
ROUNDS = 700


def selected_weather_models() -> dict[int, list[str]]:
    p = MODELS / "weather_models.json"
    sel = json.loads(p.read_text())
    return {int(k): v for k, v in sel["by_n"].items()}


FC_ROWS_START = "2024-02-10"   # first date with archived forecasts from every selected model


def fc_training_rows(base: Base, fcv: dict, wobs: pd.DataFrame, end) -> pd.DataFrame:
    """Rows whose weather is the archived REAL forecast for that lead (not the observation).

    Training on observed weather alone teaches the model to trust a 6-day forecast as much as
    a measurement. Adding these rows (same targets, forecast weather, the `lead` feature) lets
    it learn how much weather is worth at each lead. Tested rolling on 2025 and 2026: hourly
    WAPE 0.264 -> 0.251 and 0.312 -> 0.307, better at every lead; up-weighting them was worse."""
    if pd.Timestamp(end) < pd.Timestamp(FC_ROWS_START):
        return pd.DataFrame()
    r = build_rows(base, FC_ROWS_START, end, weather="fc", fc_versions=fcv, wx_hist=wobs)
    return r[r.temp_c.notna() & r.precip_mm.notna() & (r.train_weight > 0)]


def with_fc_rows(tr_obs: pd.DataFrame, fc_rows: pd.DataFrame, train_end) -> pd.DataFrame:
    if fc_rows.empty:
        return tr_obs
    extra = fc_rows[fc_rows.target_date <= pd.Timestamp(train_end)]
    return pd.concat([tr_obs, extra], ignore_index=True) if len(extra) else tr_obs


def run(rounds=ROUNDS):
    base = Base()
    wobs = obs_weather()
    fcv = fc_weather_versions(selected_weather_models())
    last = base.last_complete_date
    allrows = build_rows(base, HIST_START, last, wx_obs=wobs)
    allfc = fc_training_rows(base, fcv, wobs, last)
    out = []
    for train_end, ts, te in FOLDS:
        t0 = time.time()
        te = min(pd.Timestamp(te), last)
        tr = allrows[allrows.target_date <= train_end]
        tr = with_fc_rows(tr[training_mask(tr, base)], allfc, train_end)
        models = fit_models(tr, rounds)
        clim = climatology_table(base, train_end)
        thr = level_thresholds(base, train_end)
        thr_h = level_thresholds(base, train_end, scheme="season_hour")
        thr_s = level_thresholds(base, train_end, scheme="all")
        for mode in ("obs", "fc"):
            if mode == "obs":
                te_rows = allrows[(allrows.issue_date >= ts) & (allrows.issue_date <= te) & (allrows.target_date <= te)]
            else:
                te_rows = build_rows(base, ts, te, weather="fc", fc_versions=fcv, wx_hist=wobs)
                te_rows = te_rows[(te_rows.target_date <= te) & te_rows.temp_c.notna() & te_rows.precip_mm.notna()]
            te_rows = add_baselines(te_rows, clim)
            p = predict(models, te_rows)
            r = te_rows[["issue_date", "target_date", "lead", "hour", "y", "dow", "month", "is_holiday",
                         "is_weekend", "is_long_weekend", "is_peak_season", "is_school_summer", "holiday_kind",
                         "bl_seasonal_naive", "bl_last_year", "bl_climatology",
                         "temp_c", "precip_mm", "d_precip", "d_tmax"]].copy()
            r[["pred", "p10", "p90"]] = p[["pred", "p10", "p90"]].values
            r["season"] = season_key(r.month.values, r.is_peak_season.values)  # for reporting slices
            seas = level_keys(r.target_date.values, te_rows.is_school_summer.values)
            r["level_key"] = seas
            r["level_true"] = to_level(r.y.values, seas, r.hour.values, thr)
            r["level_pred"] = to_level(r.pred.values, seas, r.hour.values, thr)
            r["level_sn"] = to_level(r.bl_seasonal_naive.values, seas, r.hour.values, thr)
            r["level_clim"] = to_level(r.bl_climatology.values, seas, r.hour.values, thr)
            r["level_true_h"] = to_level(r.y.values, seas, r.hour.values, thr_h)
            r["level_pred_h"] = to_level(r.pred.values, seas, r.hour.values, thr_h)
            r["level_clim_h"] = to_level(r.bl_climatology.values, seas, r.hour.values, thr_h)
            r["level_true_s"] = to_level(r.y.values, seas, r.hour.values, thr_s)
            r["level_pred_s"] = to_level(r.pred.values, seas, r.hour.values, thr_s)
            r["level_clim_s"] = to_level(r.bl_climatology.values, seas, r.hour.values, thr_s)
            r["fold"] = ts[:4]
            r["mode"] = mode
            out.append(r)
        imp = pd.DataFrame({"feature": models["point"].feature_name(),
                            "gain": models["point"].feature_importance("gain")})
        imp["fold"] = ts[:4]
        imp.to_csv(PROCESSED / f"importance_{ts[:4]}.csv", index=False)
        log.info("fold %s done in %.0fs (train rows %d)", ts[:4], time.time() - t0, len(tr))
    res = pd.concat(out, ignore_index=True)
    res["holiday_kind"] = res.holiday_kind.astype(str)
    res.to_parquet(PROCESSED / "backtest.parquet")
    return res


if __name__ == "__main__":
    setup_logging()
    run()
