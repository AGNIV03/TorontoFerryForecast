"""Versioned model storage: data/models/<version>/ + data/models/current.json pointer."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import lightgbm as lgb
import pandas as pd

from common.config import MODELS
from common.timeutil import now_local


def save(models: dict, thresholds: pd.DataFrame, meta: dict) -> str:
    t = now_local()
    version = t.strftime("%Y%m%dT%H%M%S") + t.strftime("%Z")  # Eastern, e.g. 20261116T195602EST
    d = MODELS / version
    d.mkdir(parents=True, exist_ok=False)
    for k, m in models.items():
        name = "point" if k == "point" else f"q{int(round(k * 100)):02d}"
        m.save_model(str(d / f"{name}.txt"))
    thresholds.to_csv(d / "thresholds.csv", index=False)
    wm = MODELS / "weather_models.json"
    if wm.exists():
        shutil.copy(wm, d / "weather_models.json")
    (d / "meta.json").write_text(json.dumps({"version": version, **meta}, indent=2, default=str))
    return version


def promote(version: str):
    tmp = MODELS / "current.json.tmp"
    tmp.write_text(json.dumps({"version": version, "promoted_at": now_local().isoformat()}))
    tmp.replace(MODELS / "current.json")


def current_version() -> str | None:
    p = MODELS / "current.json"
    return json.loads(p.read_text())["version"] if p.exists() else None


def load(version: str | None = None) -> tuple[dict, pd.DataFrame, dict, Path]:
    version = version or current_version()
    if version is None:
        raise FileNotFoundError("no promoted model: run `python -m model.retrain --force`")
    d = MODELS / version
    models = {"point": lgb.Booster(model_file=str(d / "point.txt"))}
    for f in d.glob("q*.txt"):
        models[int(f.stem[1:]) / 100] = lgb.Booster(model_file=str(f))
    thr = pd.read_csv(d / "thresholds.csv")
    meta = json.loads((d / "meta.json").read_text())
    return models, thr, meta, d
