"""Ferry capacity per schedule period (config/capacity.json) -> busyness keys and thresholds."""

from __future__ import annotations

import json
from functools import lru_cache

import numpy as np
import pandas as pd

from common.config import CONFIG_DIR


@lru_cache(maxsize=1)
def config() -> dict:
    return json.loads((CONFIG_DIR / "capacity.json").read_text())


def _md(s: str) -> int:
    m, d = s.split("-")
    return int(m) * 100 + int(d)


def period_keys(dates, school_summer, overrides: bool = True) -> np.ndarray:
    """Schedule-period key per date ('winter', 'spring', 'summer', 'summer_peak', 'fall',
    or a dated override key such as a vessel being out of service)."""
    c = config()["periods"]
    d = pd.DatetimeIndex(pd.to_datetime(dates))
    md = d.month * 100 + d.day
    ss = np.asarray(school_summer).astype(bool)
    w, sp, su, fa = (c["winter"], c["spring"], c["summer"], c["fall"])
    key = np.where((md >= _md(w["from"])) | (md <= _md(w["to"])), "winter",
          np.where(md <= _md(sp["to"]), "spring",
          np.where(md <= _md(su["to"]), "summer",
          np.where(md <= _md(fa["to"]), "fall", "winter")))).astype(object)
    # school summer break runs to Labour Day, which can fall after summer's nominal end
    key[ss & np.isin(key, ["summer", "fall"])] = "summer_peak"
    for o in (config().get("overrides", []) if overrides else []):
        m = (d >= pd.Timestamp(o["start"])) & (d <= pd.Timestamp(o["end"]))
        key[np.asarray(m)] = o["key"]
    return key


def capacities() -> dict[str, float]:
    c = config()
    lf = c["load_factor"]
    out = {k: v["nominal_per_hour"] * lf for k, v in c["periods"].items()}
    for o in c.get("overrides", []):
        out[o["key"]] = o["nominal_per_hour"] * lf
    return out


def capacity_thresholds() -> pd.DataFrame:
    s = config()["level_shares"]
    rows = []
    for key, cap in capacities().items():
        for h in range(6, 24):
            rows.append({"season": key, "hour": h, "q50": cap * s["moderate"], "q80": cap * s["busy"],
                         "q95": cap * s["very_busy"], "capacity": cap})
    return pd.DataFrame(rows).assign(scheme="capacity")
