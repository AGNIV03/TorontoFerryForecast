"""Long-range outlook: daily boardings 8-180 days ahead (no weather).

Weather is unknowable that far out, so this model uses what is: the calendar (weekday,
season, school breaks, holidays and their history), the multi-year trend projection for the
date, the same days in previous years, and the year-over-year momentum at issue time.
A day's total is split into hours with that date's projected hourly profile
(Base.trend_hour, normalised), falling back to the historical profile for the period.

    python -m model.outlook backtest   -> data/processed/outlook_backtest.parquet + report section
    python -m model.outlook train      -> data/models/outlook/ (used by predict_live)
"""

from __future__ import annotations

import json
import logging
import sys

import lightgbm as lgb
import numpy as np
import pandas as pd

from common.config import MODELS, PROCESSED, REPORTS
from common.timeutil import setup_logging
from model.core import BASE_PARAMS, POINT_OBJECTIVE, mape, wape
from model.dataset import CAL_FEATURES, HOLIDAY_KINDS, Base

log = logging.getLogger(__name__)
LEADS = (8, 14, 21, 30, 45, 60, 90, 120, 150, 180)
FEATURES = (["lead"] + [c for c in CAL_FEATURES if c != "holiday_kind"] + ["holiday_kind"]
            + ["occ_ratio_wd", "occ_ratio_we", "occ_n", "trend_day", "trend_slope", "ly_day", "ly2_day",
               "ly3_day", "yoy90_issue", "yoy28_issue", "occ_exp_we_ly"])
CATEGORICAL = ["holiday_kind"]
ROUNDS = 300
OUT = MODELS / "outlook"


def rows(base: Base, target_start, target_end, leads=LEADS, require_target=True) -> pd.DataFrame:
    base.trends()
    t0, t1 = base.idx[pd.Timestamp(target_start)], base.idx[pd.Timestamp(target_end)]
    T = np.arange(t0, t1 + 1)
    tt, ll = np.meshgrid(T, np.array(leads), indexing="ij")
    tt, ll = tt.ravel(), ll.ravel()
    dd = tt - ll
    keep = dd > 800
    tt, ll, dd = tt[keep], ll[keep], dd[keep]
    D, Dly = base.Dsum, base.Dly
    cal = base.cal.reindex(base.dates)
    f = {"lead": ll}
    for c in CAL_FEATURES:
        f[c] = cal[c].values[tt]
    f["occ_ratio_wd"], f["occ_ratio_we"], f["occ_n"] = base.occ_ratio_wd[tt], base.occ_ratio_we[tt], base.occ_n[tt]
    f["trend_day"], f["trend_slope"] = base.trend_day[tt], base.trend_slope[tt]
    with np.errstate(all="ignore"):
        import warnings
        warnings.simplefilter("ignore", RuntimeWarning)
        f["ly_day"] = Dly[tt - 364]
        f["ly2_day"] = Dly[tt - 728]
        f["ly3_day"] = np.nanmean(np.stack([Dly[tt - k] for k in (357, 364, 371)]), axis=0)

        def mom(days):
            cur = np.nanmean(np.stack([Dly[dd - k] for k in range(1, days + 1)]), axis=0)
            prev = np.nanmean(np.stack([Dly[dd - 364 - k] for k in range(1, days + 1)]), axis=0)
            return np.clip(cur / np.maximum(prev, 50), 0, 5)
        f["yoy90_issue"] = mom(90)
        f["yoy28_issue"] = mom(28)
        we = (base.dates.weekday.values >= 5) & (base.occ_key == "")
        Dwe = np.where(we, Dly, np.nan)
        lvl = np.nanmean(np.stack([Dwe[tt - 364 + k] for k in range(-21, 22)]), axis=0)
        f["occ_exp_we_ly"] = f["occ_ratio_we"] * lvl          # last year's weekend level x uplift
    df = pd.DataFrame(f)
    df["holiday_kind"] = pd.Categorical(df["holiday_kind"], categories=HOLIDAY_KINDS)
    df.insert(0, "target_date", base.dates[tt])
    df.insert(0, "issue_date", base.dates[dd])
    df["y"] = D[tt]
    df["train_weight"] = cal.train_weight.values[tt]
    if require_target:
        df = df[np.isfinite(df.y)]
    return df.reset_index(drop=True)


def _train_mask(df, base):
    exc = pd.Series(base.excluded.astype(int), index=base.dates)
    recent = exc.rolling(35, min_periods=1).max().shift(1).fillna(0)
    return (df.train_weight.values > 0) & (recent.reindex(df.issue_date).values == 0)


def base_level(df: pd.DataFrame) -> np.ndarray:
    """Log of the multi-year trend projection (fallback: last year's same days); the model
    learns multiplicative deviations from it (validated: better WAPE and less bias than a
    plain model on the 2024-26 holdout)."""
    v = df.trend_day.fillna(df.ly3_day).fillna(df.ly_day).fillna(1000.0)
    return np.log(np.maximum(v.values, 1.0))


def fit(train: pd.DataFrame) -> lgb.Booster:
    ds = lgb.Dataset(train[FEATURES], train.y, weight=train.train_weight, init_score=base_level(train),
                     categorical_feature=CATEGORICAL)
    return lgb.train({**BASE_PARAMS, **POINT_OBJECTIVE}, ds, ROUNDS)


def predict_rows(m: lgb.Booster, df: pd.DataFrame) -> np.ndarray:
    return np.exp(m.predict(df[FEATURES], raw_score=True) + base_level(df))


def hourly_profile(base: Base, t_idx: int) -> np.ndarray:
    """Share of the day's boardings per hour 0-23 for a date (trend-projected profile)."""
    base.trends()
    p = base.trend_hour[t_idx].copy()
    if not np.isfinite(p).all() or np.nansum(p) <= 0:
        # fallback: mean profile of the same weekday over the last 3 years' +-2 weeks
        rows_ = [base.R[t_idx - 364 * k + o] for k in (1, 2, 3) for o in (-14, -7, 0, 7, 14)
                 if 0 <= t_idx - 364 * k + o < len(base.dates)]
        p = np.nanmean(np.stack(rows_), axis=0) if rows_ else np.ones(24)
    p = np.nan_to_num(p, nan=0.0)
    return p / p.sum() if p.sum() > 0 else np.full(24, 1 / 24)


def backtest():
    base = Base()
    last = base.last_complete_date
    allr = rows(base, "2016-01-01", last)
    out = []
    for train_end, ts, te in [("2023-12-31", "2024-01-01", "2024-12-31"), ("2024-12-31", "2025-01-01", "2025-12-31"),
                              ("2025-12-31", "2026-01-01", str(last.date()))]:
        tr = allr[(allr.target_date <= train_end)]
        tr = tr[_train_mask(tr, base)]
        m = fit(tr)
        # test: issue dates inside the test year only (so nothing after train_end was known)
        te_rows = allr[(allr.issue_date > train_end) & (allr.target_date >= ts) & (allr.target_date <= te)].copy()
        te_rows["pred"] = predict_rows(m, te_rows)
        te_rows["fold"] = ts[:4]
        # climatology baseline (week x weekday mean of training years, normal days)
        tr_days = pd.DataFrame({"date": base.dates, "y": base.Dly})
        tr_days = tr_days[(tr_days.date <= train_end) & tr_days.y.notna()]
        tr_days["woy"] = np.minimum(tr_days.date.dt.isocalendar().week.values, 52)
        tr_days["dow"] = tr_days.date.dt.weekday.values
        clim = tr_days.groupby(["woy", "dow"]).y.mean()
        k = pd.MultiIndex.from_arrays([np.minimum(te_rows.woy.values, 52), te_rows.dow.values])
        te_rows["bl_climatology"] = clim.reindex(k).values
        te_rows["bl_trend"] = te_rows.trend_day
        te_rows["bl_last_year"] = te_rows.ly_day
        out.append(te_rows)
        log.info("outlook fold %s: %d rows", ts[:4], len(te_rows))
    res = pd.concat(out, ignore_index=True)
    res.to_parquet(PROCESSED / "outlook_backtest.parquet")
    report(res)
    return res


def report(res: pd.DataFrame):
    res = res.dropna(subset=["bl_climatology"]).copy()
    res["lead_bucket"] = pd.cut(res.lead, [0, 30, 90, 180], labels=["8-30 d", "31-90 d", "91-180 d"])
    res["season"] = np.where(res.is_peak_season == 1, "peak", "off-season")
    lines = ["# Long-range outlook: backtest\n",
             "_Generated by `python -m model.outlook backtest`. Daily totals, 2024-2026 holdout, issue dates inside the "
             "test year only (rolling origin). No weather is used, so these are 'typical day for this date' forecasts "
             "with trend and holidays, not weather forecasts._\n"]
    rows_ = []
    for (b, s), g in res.groupby(["lead_bucket", "season"], observed=True):
        r = {"lead": b, "season": s, "days": len(g)}
        for c, name in [("pred", "outlook model"), ("bl_trend", "trend projection"),
                        ("bl_climatology", "climatology"), ("bl_last_year", "same day last year")]:
            gg = g.dropna(subset=[c])
            r[f"MAPE {name}"] = mape(gg.y, gg[c])
            r[f"WAPE {name}"] = wape(gg.y, gg[c])
        rows_.append(r)
    t = pd.DataFrame(rows_)
    lines += ["## Daily-total error by lead and season\n", t.to_markdown(index=False, floatfmt=".3f")]
    q = {}
    for b, g in res.groupby("lead_bucket", observed=True):
        e = g.y / g.pred.clip(lower=1)
        q[str(b)] = {"q10": float(e.quantile(0.1)), "q90": float(e.quantile(0.9))}
    lines += ["\n## Ratio actual / forecast (used for the published ranges)\n", pd.DataFrame(q).T.to_markdown(floatfmt=".3f")]
    bias = res.groupby("fold").apply(lambda g: g.pred.sum() / g.y.sum() - 1)
    lines += ["\n## Bias by test year (forecast / actual - 1)\n", bias.to_frame("bias").to_markdown(floatfmt="+.3f")]
    (REPORTS / "outlook_report.md").write_text("\n".join(lines), encoding="utf-8")
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "ranges.json").write_text(json.dumps(q, indent=1))


def train():
    base = Base()
    allr = rows(base, "2016-01-01", base.last_complete_date)
    allr = allr[_train_mask(allr, base)]
    m = fit(allr)
    OUT.mkdir(parents=True, exist_ok=True)
    m.save_model(str(OUT / "model.txt"))
    log.info("outlook model trained on %d rows", len(allr))


def predict(base: Base, issue_date: pd.Timestamp, horizon: int = 180, start_lead: int = 8) -> pd.DataFrame:
    """Daily outlook for issue_date + start_lead .. + horizon, with p10/p90 and hourly split."""
    m = lgb.Booster(model_file=str(OUT / "model.txt"))
    ranges = json.loads((OUT / "ranges.json").read_text()) if (OUT / "ranges.json").exists() else {}
    i = base.idx[pd.Timestamp(issue_date)]
    t_end = min(i + horizon, len(base.dates) - 1)
    out = []
    for t in range(i + start_lead, t_end + 1):
        out.append((t, t - i))
    tt = np.array([o[0] for o in out])
    # build one row per target with its own lead
    frames = []
    for lead in sorted({o[1] for o in out}):
        r = rows(base, base.dates[i + lead], base.dates[i + lead], leads=(lead,), require_target=False)
        frames.append(r)
    df = pd.concat(frames, ignore_index=True)
    df["holiday_kind"] = pd.Categorical(df["holiday_kind"].astype(str), categories=HOLIDAY_KINDS)
    df["pred"] = predict_rows(m, df)
    bucket = pd.cut(df.lead, [0, 30, 90, 180], labels=["8-30 d", "31-90 d", "91-180 d"]).astype(str)
    df["p10"] = df.pred * bucket.map(lambda b: ranges.get(b, {}).get("q10", 0.7))
    df["p90"] = df.pred * bucket.map(lambda b: ranges.get(b, {}).get("q90", 1.4))
    prof = np.stack([hourly_profile(base, base.idx[t]) for t in df.target_date])
    df["hourly"] = list(np.round(prof * df.pred.values[:, None], 1))
    _ = tt
    return df


if __name__ == "__main__":
    setup_logging()
    {"backtest": backtest, "train": train}[sys.argv[1]]()
