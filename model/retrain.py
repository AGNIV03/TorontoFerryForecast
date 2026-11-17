"""Weekly retrain with a no-regression promotion gate.

1. Candidate check: train on everything up to (last complete day - 28 d), predict the last
   28 days (observed weather) -> WAPE_candidate.
2. The current production model predicts the same 28 days -> WAPE_current. (It may
   have seen some of those days in training, which favours it: a conservative gate.)
3. If WAPE_candidate <= WAPE_current * (1 + TOLERANCE) and sanity checks pass, retrain on
   all data and promote. Otherwise keep the current model and log why.
Every version is kept in data/models/<version>/.

    python -m model.retrain            # weekly job
    python -m model.retrain --force    # train + promote unconditionally (first run)
"""

from __future__ import annotations

import argparse
import json
import logging

import numpy as np
import pandas as pd

from common.config import MODELS
from common.timeutil import now_local, setup_logging
from model import registry
from model.backtest import HIST_START, ROUNDS, fc_training_rows, selected_weather_models, with_fc_rows
from model.core import fit_models, level_thresholds, predict, training_mask, wape
from model.dataset import FEATURES, Base, build_rows, fc_weather_versions, obs_weather

log = logging.getLogger(__name__)
TOLERANCE = 0.05
WINDOW_DAYS = 28


def _sane(pred: pd.DataFrame, rows: pd.DataFrame) -> list[str]:
    problems = []
    if not np.isfinite(pred.pred).all():
        problems.append("non-finite predictions")
    daily = rows.assign(p=pred.pred.values).groupby("target_date").p.sum()
    if (daily > 60000).any():
        problems.append(f"implausible daily total {daily.max():.0f}")
    return problems


def run(force: bool = False) -> dict:
    base = Base()
    last = base.last_complete_date
    wobs = obs_weather()
    rows = build_rows(base, HIST_START, last, wx_obs=wobs)
    try:   # rows with archived real forecasts as weather (see model/backtest.py::fc_training_rows)
        fc_rows = fc_training_rows(base, fc_weather_versions(selected_weather_models()), wobs, last)
    except Exception:
        log.exception("forecast-weather training rows unavailable; training on observed weather only")
        fc_rows = pd.DataFrame()
    cut = last - pd.Timedelta(days=WINDOW_DAYS)
    result = {"last_complete_date": str(last.date()), "window_start": str((cut + pd.Timedelta(days=1)).date())}
    if not force and registry.current_version():
        tr = rows[rows.target_date <= cut]
        tr = with_fc_rows(tr[training_mask(tr, base)], fc_rows, cut)
        ev = rows[(rows.issue_date > cut) & (rows.target_date <= last)]
        cand = fit_models(tr, ROUNDS, quantiles=())
        pc = predict(cand, ev)
        cur, _, cur_meta, _ = registry.load()
        if cur_meta.get("features") != FEATURES:
            log.warning("feature set changed since current model; comparing anyway with the candidate's features")
        try:
            wape_current = wape(ev.y, predict({"point": cur["point"]}, ev).pred)
        except Exception as exc:  # e.g. the current model needs a feature the code no longer builds
            log.warning("current model can't be scored (%s); gate uses sanity checks only", exc)
            wape_current = float("inf")
        result.update(wape_candidate=wape(ev.y, pc.pred), wape_current=wape_current, eval_rows=len(ev))
        problems = _sane(pc, ev)
        ok = result["wape_candidate"] <= result["wape_current"] * (1 + TOLERANCE) and not problems
        result.update(problems=problems, promote=bool(ok))
        log.info("retrain gate: %s", result)
        if not ok:
            _log(result)
            return result
    tr = with_fc_rows(rows[training_mask(rows, base)], fc_rows, last)
    models = fit_models(tr, ROUNDS)
    thr = level_thresholds(base, last)
    version = registry.save(models, thr, {"train_end": str(last.date()), "train_rows": len(tr),
                                          "features": FEATURES, "rounds": ROUNDS, "gate": result})
    registry.promote(version)
    result.update(version=version, promote=True)
    _log(result)
    log.info("promoted %s", version)
    return result


def _log(result: dict):
    MODELS.mkdir(parents=True, exist_ok=True)
    with open(MODELS / "retrain_log.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"at": now_local().isoformat(), **result}, default=str) + "\n")


if __name__ == "__main__":
    setup_logging()
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    run(ap.parse_args().force)
