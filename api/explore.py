"""Explore endpoints: history, aggregations, comparisons and downloads.

Reads data/serving/history_*.parquet (published by ops.publish), cached by file mtime."""

from __future__ import annotations

import io
import json
from datetime import date

import numpy as np
import pandas as pd
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse

from common.config import DATA

SERVING = DATA / "serving"
router = APIRouter(prefix="/api")
_cache: dict[str, tuple[float, pd.DataFrame]] = {}
AGG = {"hour": "h", "day": "D", "week": "W-MON", "month": "MS", "year": "YS"}
HOLIDAY_NAMES = {"new_year": "New Year's Day", "family": "Family Day", "good_friday": "Good Friday",
                 "victoria": "Victoria Day", "canada_day": "Canada Day", "civic": "Civic Holiday",
                 "labour": "Labour Day", "thanksgiving": "Thanksgiving", "christmas": "Christmas Day",
                 "boxing": "Boxing Day"}


def _load(name: str) -> pd.DataFrame:
    p = SERVING / f"{name}.parquet"
    if not p.exists():
        raise HTTPException(503, f"{name} not published yet")
    m = p.stat().st_mtime
    if name not in _cache or _cache[name][0] != m:
        _cache[name] = (m, pd.read_parquet(p))
    return _cache[name][1]


def _clean(o):
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, float):
        return None if (np.isnan(o) or np.isinf(o)) else round(o, 2)
    return o


def _json(data, max_age=900):
    return JSONResponse(_clean(json.loads(json.dumps(_clean(data), default=_default))),
                        headers={"Cache-Control": f"public, max-age={max_age}"})


def _default(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return None if np.isnan(o) else round(float(o), 2)
    if isinstance(o, (pd.Timestamp, date)):
        return str(o)[:19]
    return str(o)


def _date(v: str | None, name: str):
    if not v:
        return None
    try:
        return pd.Timestamp(date.fromisoformat(v))
    except ValueError:
        raise HTTPException(400, f"{name} must be a date like 2026-07-01") from None


def _years(v: str | None) -> list[int] | None:
    if not v:
        return None
    try:
        ys = [int(x) for x in v.split(",")]
    except ValueError:
        raise HTTPException(400, "years must be comma-separated years, e.g. 2024,2025") from None
    if len(ys) > 30 or any(y < 2000 or y > 2100 for y in ys):
        raise HTTPException(400, "years out of range")
    return ys


def _range(df, col, start, end):
    s, e = _date(start, "start"), _date(end, "end")
    if s is not None:
        df = df[df[col] >= s]
    if e is not None:
        df = df[df[col] < e + pd.Timedelta(days=1)]
    return df


@router.get("/history")
def history(start: str | None = None, end: str | None = None,
            agg: str = Query("day", pattern="^(hour|day|week|month|year)$"),
            include_excluded: bool = False):
    """Boardings (island-bound), sales and weather aggregated by hour/day/week/month/year.
    Days in excluded periods (2015 roll-out, 2017 flood, 2020-21 pandemic) are flagged."""
    if agg == "hour":
        h = _range(_load("history_hourly"), "ts_local", start, end)
        if len(h) > 24 * 400:
            raise HTTPException(400, "hourly view is limited to 400 days; narrow the range")
        out = h[["ts_local", "boardings", "sales", "valid", "regime", "level", "temp_c", "precip_mm"]].rename(
            columns={"ts_local": "t"})
        return _json({"agg": agg, "series": out.to_dict("records")})
    d = _range(_load("history_daily"), "date", start, end).copy()
    d["excluded"] = d.regime.isin(["ticketing_rollout", "flood_closure", "covid_closure"])
    if not include_excluded:
        d.loc[d.excluded, ["boardings", "sales"]] = np.nan
    if agg == "day":
        out = d[["date", "boardings", "sales", "tmax", "precip_mm", "holiday", "occasion", "regime",
                 "peak_hour_boardings", "busy_hours", "very_busy_hours", "valid"]].rename(columns={"date": "t"})
        return _json({"agg": agg, "series": out.to_dict("records")})
    g = d.set_index("date").resample(AGG[agg])
    out = pd.DataFrame({"boardings": g.boardings.sum(min_count=1), "sales": g.sales.sum(min_count=1),
                        "days": g.boardings.count(), "tmax_mean": g.tmax.mean(), "precip_mm": g.precip_mm.sum(),
                        "busy_hours": g.busy_hours.sum(), "peak_day": g.boardings.max()}).reset_index()
    out = out.rename(columns={"date": "t"})
    return _json({"agg": agg, "series": out.to_dict("records")})


@router.get("/years")
def years():
    """Yearly totals with growth vs the previous year (excluded periods noted)."""
    d = _load("history_daily")
    d = d.assign(year=d.date.dt.year)
    y = d.groupby("year").agg(boardings=("boardings", "sum"), days=("boardings", "size"),
                              busiest_day=("boardings", "max"),
                              excluded_days=("regime", lambda s: int(s.isin(["ticketing_rollout", "flood_closure",
                                                                              "covid_closure"]).sum()))).reset_index()
    y["growth"] = y.boardings.pct_change()
    peak = d.loc[d.groupby("year").boardings.idxmax(), ["year", "date"]].rename(columns={"date": "busiest_date"})
    return _json({"years": y.merge(peak, on="year").to_dict("records")})


@router.get("/compare-years")
def compare_years(agg: str = Query("week", pattern="^(day|week|month)$"), years: str | None = None):
    """Boardings by week-of-year / month / day-of-year, one series per year, for overlaying."""
    d = _load("history_daily").copy()
    d.loc[d.regime.isin(["ticketing_rollout", "flood_closure", "covid_closure"]), "boardings"] = np.nan
    d["year"] = d.date.dt.year
    if agg == "week":
        d["x"] = np.minimum(d.date.dt.isocalendar().week.values, 52)
    elif agg == "month":
        d["x"] = d.date.dt.month
    else:
        d["x"] = d.date.dt.dayofyear
    t = d.groupby(["year", "x"]).boardings.sum(min_count=1).unstack(0)
    ys = _years(years) or list(t.columns)
    t = t[[c for c in t.columns if c in ys]]
    return _json({"agg": agg, "x": list(map(int, t.index)),
                  "series": {str(c): [None if pd.isna(v) else float(v) for v in t[c]] for c in t.columns}})


@router.get("/heatmap")
def heatmap(period: str | None = None, years: str | None = None, metric: str = "mean"):
    """Average boardings by weekday x hour, optionally for one schedule period and set of years."""
    h = _load("history_hourly")
    h = h[h.valid & h.hour.between(6, 23)]
    if period:
        h = h[h.level_key.str.split("|").str[0] == period]
    if years:
        h = h[h.ts_local.dt.year.isin(_years(years))]
    g = h.groupby([h.ts_local.dt.weekday, "hour"]).boardings
    t = (g.mean() if metric == "mean" else g.max()).unstack().reindex(index=range(7), columns=range(6, 24))
    return _json({"period": period, "rows": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
                  "cols": list(range(6, 24)), "values": [[None if pd.isna(v) else round(float(v)) for v in r]
                                                          for r in t.values]})


@router.get("/calendar")
def calendar(year: int):
    """Daily totals for one year (calendar heatmap)."""
    d = _load("history_daily")
    d = d[d.date.dt.year == year]
    return _json({"year": year, "days": d[["date", "boardings", "holiday", "regime", "tmax", "precip_mm"]]
                  .assign(date=d.date.dt.strftime("%Y-%m-%d")).to_dict("records")})


@router.get("/holidays")
def holidays():
    """Boardings on each holiday, by year, with the average of normal days around it for scale."""
    d = _load("history_daily").copy()
    d.loc[d.regime.isin(["ticketing_rollout", "flood_closure", "covid_closure"]), "boardings"] = np.nan
    hol = d[d.holiday.isin(list(HOLIDAY_NAMES))].copy()
    hol["year"] = hol.date.dt.year
    normal = d[(d.occasion == "") & d.boardings.notna()].set_index("date").boardings
    def around(t):
        w = normal[(normal.index >= t - pd.Timedelta(days=21)) & (normal.index <= t + pd.Timedelta(days=21))]
        return float(w.mean()) if len(w) else np.nan
    hol["normal_day_nearby"] = [around(t) for t in hol.date]
    hol["name"] = hol.holiday.map(HOLIDAY_NAMES)
    return _json({"holidays": hol[["name", "holiday", "year", "date", "boardings", "normal_day_nearby", "tmax",
                                   "precip_mm"]].assign(date=hol.date.dt.strftime("%Y-%m-%d (%a)"))
                  .to_dict("records")})


@router.get("/weather-effect")
def weather_effect(period: str | None = "summer_peak", daytype: str = Query("all", pattern="^(all|weekday|weekend)$")):
    """One point per day: max temperature, rain, total boardings (normal days, 2016+)."""
    d = _load("history_daily")
    d = d[(d.date >= "2016-01-01") & d.valid & ~d.regime.isin(["ticketing_rollout", "flood_closure", "covid_closure",
                                                                 "covid_recovery"])]
    if period:
        d = d[d.level_key.str.split("|").str[0] == period]
    if daytype == "weekday":
        d = d[(d.dow < 5) & (d.holiday == "none")]
    elif daytype == "weekend":
        d = d[(d.dow >= 5) | (d.holiday != "none")]
    return _json({"period": period, "daytype": daytype,
                  "points": d[["date", "tmax", "precip_mm", "boardings", "dow", "holiday"]]
                  .assign(date=d.date.dt.strftime("%Y-%m-%d")).to_dict("records")})


@router.get("/download")
def download(dataset: str = Query("daily", pattern="^(daily|hourly)$"), start: str | None = None,
             end: str | None = None):
    """CSV download of the cleaned history (Open Government Licence - Toronto applies)."""
    if dataset == "hourly":
        # hourly is ~100k rows in total: serve at most two years per request (default: the last year)
        full = _load("history_hourly")
        e = _date(end, "end") or full.ts_local.max().normalize()
        s = _date(start, "start") or e - pd.Timedelta(days=365)
        if (e - s).days > 731:
            raise HTTPException(400, "hourly downloads are limited to two years per request; narrow the range")
        start, end = str(s.date()), str(e.date())
        df = _range(full, "ts_local", start, end)
    else:
        df = _range(_load("history_daily"), "date", start, end)
    buf = io.StringIO()
    buf.write("# Island-bound ferry boardings, cleaned (see reports/data_quality.md). Contains information licensed "
              "under the Open Government Licence - Toronto. Weather: Environment and Climate Change Canada; "
              "Open-Meteo.com (CC BY 4.0). Times are Toronto local time.\n")
    df.to_csv(buf, index=False)
    buf.seek(0)
    name = f"toronto-island-ferry-{dataset}-{start or 'all'}-{end or 'latest'}.csv"
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{name}"',
                                      "Cache-Control": "public, max-age=3600"})
