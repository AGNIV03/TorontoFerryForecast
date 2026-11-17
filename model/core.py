"""Model fitting, baselines, busyness levels and metrics."""

from __future__ import annotations

import lightgbm as lgb
import numpy as np
import pandas as pd

from model.capacity import capacity_thresholds, period_keys
from model.dataset import CATEGORICAL, FEATURES, Base

QUANTILES = (0.1, 0.9)
LEVELS = ["Quiet", "Moderate", "Busy", "Very busy"]
LEVEL_Q = (0.5, 0.8, 0.95)   # Quiet < p50 <= Moderate < p80 <= Busy < p95 <= Very busy

BASE_PARAMS = dict(
    learning_rate=0.05, num_leaves=63, min_data_in_leaf=200, feature_fraction=0.8,
    bagging_fraction=0.8, bagging_freq=1, lambda_l2=1.0, verbose=-1,
    num_threads=int(__import__("os").environ.get("FERRY_THREADS", "0")), seed=7,
    max_bin=255, cat_smooth=10,
)
POINT_OBJECTIVE = dict(objective="tweedie", tweedie_variance_power=1.3)
N_ROUNDS = 1200


def training_mask(df: pd.DataFrame, base: Base) -> np.ndarray:
    """Usable training rows: positive weight and no excluded days in the 35 days before issue."""
    exc = pd.Series(base.excluded.astype(int), index=base.dates)
    recent_exc = exc.rolling(35, min_periods=1).max().shift(1).fillna(0)
    contaminated = recent_exc.reindex(df.issue_date).values > 0
    return (df.train_weight.values > 0) & ~contaminated


def _ds(df, weight=None):
    return lgb.Dataset(df[FEATURES], label=df.y.values, weight=weight,
                       categorical_feature=CATEGORICAL, free_raw_data=False)


def fit_models(train: pd.DataFrame, rounds: int = N_ROUNDS, point_params: dict | None = None,
               quantiles=QUANTILES) -> dict:
    w = train.train_weight.values
    ds = _ds(train, w)
    models = {"point": lgb.train({**BASE_PARAMS, **(point_params or POINT_OBJECTIVE)}, ds, rounds)}
    for q in quantiles:
        models[q] = lgb.train({**BASE_PARAMS, "objective": "quantile", "alpha": q,
                               "learning_rate": 0.08}, ds, int(rounds * 0.5))
    return models


def predict(models: dict, df: pd.DataFrame) -> pd.DataFrame:
    def run(m):
        return np.clip(m.predict(df[m.feature_name()]), 0, None)  # the model's own feature list

    out = pd.DataFrame(index=df.index)
    out["pred"] = run(models["point"])
    qs = sorted(k for k in models if k != "point")
    if qs:
        qp = np.column_stack([run(models[q]) for q in qs])
        qp = np.sort(qp, axis=1)
        out["p10"] = np.minimum(qp[:, 0], out.pred)
        out["p90"] = np.maximum(qp[:, -1], out.pred)
    return out


# ------------------------------------------------------------------ interval calibration
# The quantile models are trained on observed weather, so with forecast weather their
# p10-p90 range is too narrow and barely widens with lead time. Split-conformal fix:
# per (lead, peak-season flag), scale the distance from the point forecast to each bound
# so that 10% of calibration actuals fall below and 10% above.

def fit_interval_calibration(df: pd.DataFrame, target_tail=0.10) -> dict:
    cal = {}
    for (lead, peak), g in df.groupby(["lead", "is_peak_season"]):
        up = (g.y - g.pred) / np.maximum(g.p90 - g.pred, 1.0)
        lo = (g.pred - g.y) / np.maximum(g.pred - g.p10, 1.0)
        cal[f"{int(lead)}|{int(peak)}"] = {"s_hi": float(max(np.quantile(up, 1 - target_tail), 1.0)),
                                            "s_lo": float(max(np.quantile(lo, 1 - target_tail), 1.0)),
                                            "n": int(len(g))}
    return cal


def apply_interval_calibration(df: pd.DataFrame, cal: dict) -> tuple[np.ndarray, np.ndarray]:
    keys = df.lead.astype(int).astype(str) + "|" + df.is_peak_season.astype(int).astype(str)
    s_hi = keys.map(lambda k: cal.get(k, {}).get("s_hi", 1.0)).values
    s_lo = keys.map(lambda k: cal.get(k, {}).get("s_lo", 1.0)).values
    p10 = np.clip(df.pred.values - s_lo * (df.pred.values - df.p10.values), 0, None)
    p90 = df.pred.values + s_hi * (df.p90.values - df.pred.values)
    return p10, p90


# ------------------------------------------------------------------ baselines

def climatology_table(base: Base, train_end) -> pd.DataFrame:
    """Mean redemptions by ISO week x weekday x hour over usable training days."""
    n = base.idx[pd.Timestamp(train_end)] + 1
    dates = base.dates[:n]
    R = base.R[:n]
    ok = ~base.excluded[:n]
    rows = []
    woy = dates.isocalendar().week.values.astype(int)
    woy = np.minimum(woy, 52)
    dow = dates.weekday.values
    for h in range(24):
        v = R[:, h]
        m = ok & np.isfinite(v)
        rows.append(pd.DataFrame({"woy": woy[m], "dow": dow[m], "hour": h, "y": v[m]}))
    t = pd.concat(rows).groupby(["woy", "dow", "hour"]).y.mean().rename("clim").reset_index()
    return t


def add_baselines(df: pd.DataFrame, clim: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["bl_seasonal_naive"] = df.sn4_hour
    df["bl_last_year"] = df.ly_hour.fillna(df.sn4_hour)
    k = pd.DataFrame({"woy": np.minimum(df.woy.values, 52), "dow": df.dow.values, "hour": df.hour.values})
    df["bl_climatology"] = k.merge(clim, on=["woy", "dow", "hour"], how="left").clim.values
    df["bl_climatology"] = df.bl_climatology.fillna(df.sn4_hour)
    return df


# ------------------------------------------------------------------ busyness levels

def season_key(month: np.ndarray, is_peak: np.ndarray) -> np.ndarray:
    s = np.where(np.isin(month, [4, 5, 9, 10]), "shoulder", "off")
    return np.where(is_peak == 1, "peak", s)


def level_scheme() -> str:
    import json

    from common.config import CONFIG_DIR
    p = CONFIG_DIR / "busyness.json"
    return json.loads(p.read_text())["scheme"] if p.exists() else "season"


def level_thresholds(base: Base, train_end, scheme: str | None = None) -> pd.DataFrame:
    """Hourly redemption percentiles over usable days up to train_end, per season (pooled
    over operating hours) or per season x hour; always returned as one row per season x hour."""
    scheme = scheme or level_scheme()
    if scheme == "capacity":
        return capacity_thresholds()
    n = base.idx[pd.Timestamp(train_end)] + 1
    cal = base.cal.reindex(base.dates[:n])
    seas = level_keys(base.dates[:n], cal.is_school_summer.values, overrides=False)
    ok = ~base.excluded[:n]
    rows = []
    for h in range(6, 24):
        v = base.R[:n, h]
        m = ok & np.isfinite(v)
        rows.append(pd.DataFrame({"season": seas[m], "hour": h, "y": v[m]}))
    d = pd.concat(rows)
    if scheme == "all":  # year-round: one absolute scale for every season and hour
        q = d.y.quantile(list(LEVEL_Q)).values
        t = pd.DataFrame([(s, h) for s in ("winter", "spring", "summer", "summer_peak", "fall") for h in range(6, 24)],
                         columns=["season", "hour"])
        t[["q50", "q80", "q95"]] = q
        return t.assign(scheme=scheme)
    if scheme == "season_hour":
        t = d.groupby(["season", "hour"]).y.quantile(list(LEVEL_Q)).unstack()
        t.columns = ["q50", "q80", "q95"]
        return t.reset_index().assign(scheme=scheme)
    t = d.groupby("season").y.quantile(list(LEVEL_Q)).unstack()
    t.columns = ["q50", "q80", "q95"]
    t = t.reset_index().merge(pd.DataFrame({"hour": range(6, 24)}), how="cross")
    return t.assign(scheme=scheme)


def level_keys(dates, school_summer, overrides: bool = True) -> np.ndarray:
    """Schedule-period key per date (see model/capacity.py); busyness thresholds are keyed by it."""
    return period_keys(dates, school_summer, overrides)


def to_level(values: np.ndarray, season: np.ndarray, hour: np.ndarray, thr: pd.DataFrame) -> np.ndarray:
    keys = pd.Series(np.asarray(season, dtype=object))
    known = set(thr.season)
    # an override key ("summer_peak|minus_mcbride") falls back to its base period if a
    # percentile scheme has no thresholds for it
    keys = keys.where(keys.isin(known), keys.str.split("|").str[0])
    k = pd.DataFrame({"season": keys.values, "hour": hour}).merge(thr, on=["season", "hour"], how="left")
    v = np.asarray(values, float)
    return (v >= k.q50.values).astype(int) + (v >= k.q80.values) + (v >= k.q95.values)


# ------------------------------------------------------------------ metrics

def wape(y, p):
    y, p = np.asarray(y, float), np.asarray(p, float)
    return np.abs(y - p).sum() / max(y.sum(), 1e-9)


def mae(y, p):
    return float(np.mean(np.abs(np.asarray(y, float) - np.asarray(p, float))))


def mape(y, p):
    y, p = np.asarray(y, float), np.asarray(p, float)
    m = y > 0
    return float(np.mean(np.abs(y[m] - p[m]) / y[m]))
