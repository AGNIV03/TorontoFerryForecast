"""Observed weather table (training) and forecast weather (inference / honest backtest).

Observed, hourly, keyed on UTC hour. Priority per variable:
  temp, dewpoint, rh, precip, wind : ECCC Toronto City Centre (6158359, Billy Bishop)
                                     -> ECCC Toronto City (6158355) -> Open-Meteo archive
  cloud, gusts, radiation          : Open-Meteo archive only (ECCC hourly has none)
Each filled variable gets a `<var>_src` column: eccc_cc / eccc_city / om_archive.

Humidex is computed from temperature and dew point for every hour (ECCC only publishes
it when it is >= 25), so observed and forecast humidex share one definition.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from common import db

OBS_VARS = ["temp_c", "dewpoint_c", "rh", "precip_mm", "wind_kmh"]
OM_MAP = {"temp_c": "temperature_2m", "dewpoint_c": "dew_point_2m", "rh": "relative_humidity_2m",
          "precip_mm": "precipitation", "wind_kmh": "wind_speed_10m"}
OM_ONLY = {"cloud_cover": "cloud_cover", "gust_kmh": "wind_gusts_10m", "radiation": "shortwave_radiation"}
WEATHER_FEATURES = ["temp_c", "humidex", "rh", "precip_mm", "wind_kmh", "gust_kmh", "cloud_cover", "radiation"]


def humidex(t, td):
    t, td = np.asarray(t, float), np.asarray(td, float)
    e = 6.11 * np.exp(5417.7530 * (1 / 273.16 - 1 / (273.15 + td)))
    return t + 0.5555 * (e - 10.0)


PRECIP_HOURLY_MAX = 100.0  # mm/h; anything above is a gauge fault in this climate


def qc_precip(cc: pd.Series, city: pd.Series) -> pd.Series:
    """Blank Billy Bishop precip on days it is implausible against the downtown gauge.

    The Billy Bishop gauge failed on 2026-09-30 (stuck at ~610 mm every hour). A UTC day
    is rejected if any hour exceeds PRECIP_HOURLY_MAX, or its daily total exceeds
    max(3 x downtown, downtown + 25 mm). Rejected hours fall back to the downtown station.
    """
    day = cc.index.floor("D")
    cc_d = cc.groupby(day).transform("sum")
    ct_d = city.groupby(day).transform(lambda s: s.sum(min_count=1))
    cc_max = cc.groupby(day).transform("max")
    bad = (cc_max > PRECIP_HOURLY_MAX) | ((cc_d > np.maximum(3 * ct_d, ct_d + 25)) & ct_d.notna())
    return cc.mask(bad)


def observed_hourly() -> pd.DataFrame:
    ec = db.table("eccc_hourly")
    om = db.table("om_archive").set_index("ts_utc")
    cc = ec[ec.climate_id == "6158359"].set_index("ts_utc")
    ct = ec[ec.climate_id == "6158355"].set_index("ts_utc")
    idx = pd.date_range(min(cc.index.min(), om.index.min()), max(cc.index.max(), om.index.max()),
                        freq="h", name="ts_utc")
    out = pd.DataFrame(index=idx)
    cc, ct, om = cc.reindex(idx), ct.reindex(idx), om.reindex(idx)
    cc = cc.copy()
    cc["precip_mm"] = qc_precip(cc.precip_mm.astype(float), ct.precip_mm.astype(float))
    for v in OBS_VARS:
        a = cc[v].astype(float)
        src = pd.Series(np.where(a.notna(), "eccc_cc", None), index=idx, dtype=object)
        if v in ct:
            b = ct[v].astype(float)
            fill = a.isna() & b.notna()
            a = a.where(~fill, b)
            src[fill] = "eccc_city"
        c = om[OM_MAP[v]].astype(float)
        fill = a.isna() & c.notna()
        a = a.where(~fill, c)
        src[fill] = "om_archive"
        out[v] = a
        out[v + "_src"] = src
    for v, k in OM_ONLY.items():
        out[v] = om[k].astype(float)
        out[v + "_src"] = np.where(out[v].notna(), "om_archive", None)
    out["humidex"] = humidex(out.temp_c, out.dewpoint_c)
    return out.reset_index()


def build():
    w = observed_hourly()
    with db.connect() as con:
        con.execute("CREATE OR REPLACE TABLE weather_obs AS SELECT * FROM w")
    return w


if __name__ == "__main__":
    w = build()
    w["year"] = w.ts_utc.dt.year
    src_cols = [c for c in w.columns if c.endswith("_src")]
    for c in src_cols:
        print(c, w.groupby("year")[c].value_counts(dropna=False).unstack().fillna(0).astype(int).to_string(), sep="\n")
