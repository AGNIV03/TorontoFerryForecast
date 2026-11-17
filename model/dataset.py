"""Build the (issue date, lead, target hour) modelling table.

Forecast protocol: a forecast is issued at 06:00 local on issue date D, for target date
T = D + lead (lead = 0..7 days) and target hours 06..23. Only information available at
06:00 on D is used:

* ferry counts strictly before D (whole days D-1 and earlier), plus the overnight online
  sales on D between 00:00 and 06:00 (ticket booths are closed, so these are online);
* calendar facts about T;
* weather for T: observed (mode "obs": the perfect-forecast ceiling) or the archived real
  forecast issued >= (lead+1)*24h before the valid time (mode "fc": honest).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from common import db
from common.config import CONFIG_DIR
from features.calendar import build_calendar
from features.weather import humidex

HOURS = np.arange(6, 24)
MAX_LEAD = 7

CAL_FEATURES = [
    "dow", "month", "doy", "woy", "is_weekend", "is_holiday", "holiday_kind", "is_long_weekend",
    "long_weekend_pos", "is_eve_of_long_weekend", "days_to_holiday", "days_since_holiday",
    "is_march_break", "is_school_summer", "is_school_day", "is_dst", "is_peak_season",
    "days_to_labour_day", "covid_recovery", "event_island", "event_waterfront",
]
OCC_FEATURES = ["occ_ratio_wd", "occ_ratio_we", "occ_n", "occ_exp_we", "occ_exp_wd"]
HIST_FEATURES = [
    "sn4_hour", "sn4_day", "ly_hour", "ly3_hour", "ly_day", "ly2_day", "r7_hour", "yday",
    "r7", "r28", "yoy7", "yoy28", "ly3_hour_adj", "overnight_sales", "overnight_sales_ly",
    "adv_sales_3d",
]
TREND_FEATURES = ["trend_day", "trend_hour", "trend_slope", "yoy90"]
WX_HOURLY = ["temp_c", "humidex", "rh", "precip_mm", "wind_kmh", "gust_kmh", "cloud_cover", "radiation"]
WX_DAILY = ["d_tmax", "d_humidex_max", "d_precip", "d_precip_am", "d_precip_pm", "d_precip_hours",
            "d_cloud", "d_wind", "d_gust_max", "d_radiation", "p_precip_3h"]
# weather in context: the day's high against the seasonal normal and against the last two
# weeks (the first warm day draws a crowd). Neighbouring-day weather and daylight were tested
# on 2019/2023 and did not help.
WX_CONTEXT = ["tmax_anom", "tmax_vs_recent"]
FEATURES = (["lead", "hour"] + CAL_FEATURES + OCC_FEATURES + HIST_FEATURES + TREND_FEATURES + WX_HOURLY + WX_DAILY
            + WX_CONTEXT)
CATEGORICAL = ["holiday_kind"]
HOLIDAY_KINDS = ["none", "new_year", "family", "good_friday", "victoria", "canada_day", "civic", "labour",
                 "thanksgiving", "christmas", "boxing", "other"]


class Base:
    """Day x hour arrays of counts, calendar and weather, indexed by local date."""

    def __init__(self, end_date=None):
        h = db.table("ferry_hourly")
        cal = build_calendar(start="2014-01-01", end="2028-12-31")
        self.dates = pd.DatetimeIndex(cal.date)
        self.cal = cal.set_index("date")
        n = len(self.dates)
        self.idx = {d: i for i, d in enumerate(self.dates)}
        di = h.date.map(self.idx).values
        hr = h.hour.values
        self.R = np.full((n, 24), np.nan)
        self.S = np.full((n, 24), np.nan)
        ok = h.complete.values & (h.outage_day.values == 0) & (h.gap_flag.values == 0)
        self.R[di[ok], hr[ok]] = h.redemptions.values[ok]
        self.S[di[ok], hr[ok]] = h.sales.values[ok]
        # day totals only for complete days
        full = np.isfinite(self.R).all(axis=1)
        self.Dsum = np.where(full, np.nansum(self.R, axis=1), np.nan)
        # mask for "history is representative" (exclude weight-0 regimes from ly features)
        w = self.cal.train_weight.reindex(self.dates).values
        self.excluded = w == 0
        self.Rly = self.R.copy()
        self.Rly[self.excluded] = np.nan
        self.Dly = self.Dsum.copy()
        self.Dly[self.excluded] = np.nan
        self.last_complete_date = self.dates[np.where(full)[0].max()]
        self._occasions()

    # ---------------------------------------------------------------- occasions
    def _occasions(self, lookback_days: int = 180, last_k: int = 3):
        """Holiday / event history: how much busier each past occurrence of the same occasion
        was than normal days around it, averaged over the last `last_k` occurrences that are
        at least `lookback_days` before the target (so always known at issue time)."""
        cal = self.cal.reindex(self.dates)
        n = len(self.dates)
        key = np.array([k if k not in ("none", "other") else "" for k in cal.holiday_kind.values], dtype=object)
        # non-holiday days of a holiday long weekend: '<holiday>_lw'
        lw = cal.is_long_weekend.values == 1
        i = 0
        while i < n:
            if lw[i]:
                j = i
                while j < n and lw[j]:
                    j += 1
                kinds = [key[k] for k in range(i, j) if key[k]]
                if kinds:
                    for k in range(i, j):
                        if not key[k]:
                            key[k] = kinds[0] + "_lw"
                i = j
            else:
                i += 1
        # recurring events with a `series` name in config/events.csv
        ev = pd.read_csv(CONFIG_DIR / "events.csv", parse_dates=["date_start", "date_end"])
        if "series" in ev:
            for _, r in ev[(ev.kind == "event") & ev.series.notna()].iterrows():
                for d in pd.date_range(r.date_start, r.date_end):
                    if d in self.idx:
                        key[self.idx[d]] = f"event:{r.series}"
        special = np.array([bool(k) for k in key])
        D = self.Dly.copy()
        normal = np.isfinite(D) & ~special
        dow = self.dates.weekday.values
        with np.errstate(all="ignore"):
            import warnings
            warnings.simplefilter("ignore", RuntimeWarning)
            same_wd = np.full((6, n), np.nan)
            for j, off in enumerate((-21, -14, -7, 7, 14, 21)):
                src = np.arange(n) + off
                okk = (src >= 0) & (src < n)
                vals = np.full(n, np.nan)
                vals[okk] = np.where(normal[src[okk]], D[src[okk]], np.nan)
                same_wd[j] = vals
            base_wd = np.nanmean(same_wd, axis=0)
            wk = np.full((43, n), np.nan)
            for j, off in enumerate(range(-21, 22)):
                src = np.arange(n) + off
                okk = (src >= 0) & (src < n) & (off != 0)
                vals = np.full(n, np.nan)
                s = src[okk]
                vals[okk] = np.where(normal[s] & (dow[s] >= 5), D[s], np.nan)
                wk[j] = vals
            base_we = np.nanmean(wk, axis=0)
            r_wd = D / base_wd
            r_we = D / base_we
        self.occ_key = key
        self.occ_ratio_wd = np.full(n, np.nan)
        self.occ_ratio_we = np.full(n, np.nan)
        self.occ_n = np.zeros(n)
        by_key: dict[str, list[int]] = {}
        for t in range(n):
            if key[t]:
                by_key.setdefault(key[t], []).append(t)
        for idxs in by_key.values():
            for t in idxs:
                prior = [p for p in idxs if p <= t - lookback_days and np.isfinite(r_wd[p])][-last_k:]
                if prior:
                    self.occ_ratio_wd[t] = float(np.nanmean(r_wd[prior]))
                    self.occ_ratio_we[t] = float(np.nanmean(r_we[prior]))
                    self.occ_n[t] = len(prior)

    # ---------------------------------------------------------------- weather
    # ---------------------------------------------------------------- multi-year trend
    def trends(self, max_years: int = 6, decay: float = 0.7, min_points: int = 3, slope_clip: float = 0.3):
        """Project each date's level from the same weekday-aligned window (+-1 week) in each of
        the previous `max_years` normal years: a recency-weighted log-linear fit through the
        yearly values, extrapolated to the target year. Uses only data >= 357 days before the
        target, so it is known at any issue time up to ~1 year ahead.

        Sets: trend_day [n], trend_hour [n, 24], trend_slope [n] (growth per year, e.g. 0.08)."""
        if hasattr(self, "trend_day"):
            return
        n = len(self.dates)
        cal = self.cal.reindex(self.dates)
        clean = ~self.excluded & (cal.regime.values != "covid_recovery") & (self.occ_key == "")
        D = np.where(clean, self.Dsum, np.nan)
        R = np.where(clean[:, None], self.R, np.nan)

        def window(arr, k):
            out = []
            for off in (-7, 0, 7):
                src = np.arange(n) - 364 * k + off
                ok = src >= 0
                v = np.full((n,) + arr.shape[1:], np.nan)
                v[ok] = arr[src[ok]]
                out.append(v)
            with np.errstate(all="ignore"):
                import warnings
                warnings.simplefilter("ignore", RuntimeWarning)
                return np.nanmean(np.stack(out), axis=0)

        def fit(arr):
            sums = {k: 0.0 for k in ("w", "wx", "wy", "wxx", "wxy", "n")}
            for k in range(1, max_years + 1):
                v = window(arr, k)
                ok = np.isfinite(v) & (v > 0)
                w = np.where(ok, decay ** (k - 1), 0.0)
                y = np.where(ok, np.log(np.where(ok, v, 1.0)), 0.0)
                x = -float(k)
                sums["w"] = sums["w"] + w
                sums["wx"] = sums["wx"] + w * x
                sums["wy"] = sums["wy"] + w * y
                sums["wxx"] = sums["wxx"] + w * x * x
                sums["wxy"] = sums["wxy"] + w * x * y
                sums["n"] = sums["n"] + ok
            with np.errstate(all="ignore"):
                den = sums["w"] * sums["wxx"] - sums["wx"] ** 2
                b = np.where(den > 0, (sums["w"] * sums["wxy"] - sums["wx"] * sums["wy"]) / den, np.nan)
                b = np.clip(b, np.log(1 - slope_clip), np.log(1 + slope_clip))
                a = (sums["wy"] - b * sums["wx"]) / sums["w"]
                proj = np.where(sums["n"] >= min_points, np.exp(a), np.nan)  # x = 0: target year
            return proj, np.where(sums["n"] >= min_points, np.exp(b) - 1, np.nan)

        self.trend_day, self.trend_slope = fit(D)
        self.trend_hour, _ = fit(R)

    def weather_arrays(self, wx: pd.DataFrame) -> dict[str, np.ndarray]:
        """wx: hourly frame with ts_utc + WX_HOURLY-ish columns -> {var: [ndays, 24]} local."""
        loc = wx.ts_utc.dt.tz_convert("America/Toronto")
        d = loc.dt.tz_localize(None).dt.normalize().map(self.idx)
        keep = d.notna().values
        di = d[keep].astype(int).values
        hr = loc.dt.hour.values[keep]
        out = {}
        for v in WX_HOURLY:
            a = np.full((len(self.dates), 24), np.nan)
            if v in wx:
                a[di, hr] = wx[v].values[keep]
            out[v] = a
        return out

    @staticmethod
    def daily_weather(A: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        day = slice(9, 21)
        with np.errstate(all="ignore"):
            import warnings
            warnings.simplefilter("ignore", RuntimeWarning)
            return {
                "d_tmax": np.nanmax(A["temp_c"][:, day], axis=1),
                "d_humidex_max": np.nanmax(A["humidex"][:, day], axis=1),
                "d_precip": np.nansum(A["precip_mm"][:, 8:21], axis=1),
                "d_precip_am": np.nansum(A["precip_mm"][:, 6:12], axis=1),
                "d_precip_pm": np.nansum(A["precip_mm"][:, 12:21], axis=1),
                "d_precip_hours": np.nansum(A["precip_mm"][:, 8:21] >= 0.2, axis=1).astype(float),
                "d_cloud": np.nanmean(A["cloud_cover"][:, day], axis=1),
                "d_wind": np.nanmean(A["wind_kmh"][:, day], axis=1),
                "d_gust_max": np.nanmax(A["gust_kmh"][:, day], axis=1),
                "d_radiation": np.nansum(A["radiation"][:, day], axis=1),
            }


def obs_weather() -> pd.DataFrame:
    return db.table("weather_obs")


def fc_weather_versions(models_by_n: dict[int, list[str]]) -> dict[int, pd.DataFrame]:
    """Archived real forecasts from the previous-runs API, one frame per N (days before
    valid time). models_by_n maps N -> list of models to average (a simple blend)."""
    pr = db.table("om_previous_runs")
    out = {}
    for n, models in models_by_n.items():
        p = pr[(pr.lead_day == n) & pr.model.isin(models)]
        num = ["temperature_2m", "relative_humidity_2m", "dew_point_2m", "precipitation", "cloud_cover",
               "wind_speed_10m", "wind_gusts_10m", "shortwave_radiation"]
        g = p.groupby("ts_utc")[num].mean().reset_index()
        f = pd.DataFrame({
            "ts_utc": g.ts_utc, "temp_c": g.temperature_2m, "rh": g.relative_humidity_2m,
            "precip_mm": g.precipitation, "wind_kmh": g.wind_speed_10m, "gust_kmh": g.wind_gusts_10m,
            "cloud_cover": g.cloud_cover, "radiation": g.shortwave_radiation,
        })
        f["humidex"] = humidex(f.temp_c, g.dew_point_2m)
        out[n] = f
    return out


def build_rows(base: Base, issue_start, issue_end, weather: str | dict = "obs",
               wx_obs: pd.DataFrame | None = None, fc_versions: dict | None = None,
               require_target=True, wx_hist: pd.DataFrame | None = None) -> pd.DataFrame:
    """wx_hist: observed weather history (defaults to wx_obs) for the recent-weather and
    seasonal-normal context features; pass it explicitly in "fc" mode and for live runs."""
    """Rows for issue dates in [issue_start, issue_end] x lead 0..7 x hours 6..23."""
    i0, i1 = base.idx[pd.Timestamp(issue_start)], base.idx[pd.Timestamp(issue_end)]
    D = np.arange(i0, i1 + 1)
    L = np.arange(MAX_LEAD + 1)
    Hh = HOURS
    dd, ll, hh = np.meshgrid(D, L, Hh, indexing="ij")
    dd, ll, hh = dd.ravel(), ll.ravel(), hh.ravel()
    tt = dd + ll
    ok = tt < len(base.dates)
    dd, ll, hh, tt = dd[ok], ll[ok], hh[ok], tt[ok]
    R, Dsum, S = base.R, base.Dsum, base.S
    f = {}
    f["lead"] = ll
    f["hour"] = hh
    y = R[tt, hh]

    with np.errstate(all="ignore"):
        import warnings
        warnings.simplefilter("ignore", RuntimeWarning)
        kmin = np.where(ll < 7, 1, 2)
        sn_h = np.stack([R[tt - 7 * (kmin + j), hh] for j in range(4)])
        sn_d = np.stack([Dsum[tt - 7 * (kmin + j)] for j in range(4)])
        f["sn4_hour"] = np.nanmean(sn_h, axis=0)
        f["sn4_day"] = np.nanmean(sn_d, axis=0)
        Rly, Dly = base.Rly, base.Dly
        f["ly_hour"] = Rly[tt - 364, hh]
        f["ly3_hour"] = np.nanmean(np.stack([Rly[tt - k, hh] for k in (357, 364, 371)]), axis=0)
        f["ly_day"] = Dly[tt - 364]
        f["ly2_day"] = Dly[tt - 728]
        f["r7_hour"] = np.nanmean(np.stack([R[dd - k, hh] for k in range(1, 8)]), axis=0)
        f["yday"] = Dsum[dd - 1]
        r7 = np.nanmean(np.stack([Dsum[dd - k] for k in range(1, 8)]), axis=0) * 7
        r28 = np.nanmean(np.stack([Dsum[dd - k] for k in range(1, 29)]), axis=0) * 28
        r7ly = np.nanmean(np.stack([Dly[dd - 364 - k] for k in range(1, 8)]), axis=0) * 7
        r28ly = np.nanmean(np.stack([Dly[dd - 364 - k] for k in range(1, 29)]), axis=0) * 28
        f["r7"], f["r28"] = r7, r28
        f["yoy7"] = np.clip(r7 / np.maximum(r7ly, 50), 0, 5)
        f["yoy28"] = np.clip(r28 / np.maximum(r28ly, 200), 0, 5)
        f["ly3_hour_adj"] = f["ly3_hour"] * f["yoy28"]
        f["overnight_sales"] = np.nansum(S[dd, 0:6], axis=1)
        f["overnight_sales_ly"] = np.nansum(S[dd - 364, 0:6], axis=1)
        f["adv_sales_3d"] = np.nansum(np.stack([S[dd - k].sum(axis=1) - R[dd - k].sum(axis=1)
                                                 for k in (1, 2, 3)]), axis=0)

    f["occ_ratio_wd"] = base.occ_ratio_wd[tt]
    f["occ_ratio_we"] = base.occ_ratio_we[tt]
    f["occ_n"] = base.occ_n[tt]
    # expected day total for the occasion = its historical uplift x the current level of
    # normal weekend days (last 28 days before issue) / of the same weekday (sn4_day)
    with np.errstate(all="ignore"):
        import warnings
        warnings.simplefilter("ignore", RuntimeWarning)
        we_mask = (base.dates.weekday.values >= 5) & (base.occ_key == "")
        Dwe = np.where(we_mask, base.Dsum, np.nan)
        we_level = np.nanmean(np.stack([Dwe[dd - k] for k in range(1, 29)]), axis=0)
    f["occ_exp_we"] = f["occ_ratio_we"] * we_level
    f["occ_exp_wd"] = f["occ_ratio_wd"] * f["sn4_day"]

    # multi-year trend projection for the target date, and recent year-over-year momentum
    base.trends()
    f["trend_day"] = base.trend_day[tt]
    f["trend_hour"] = base.trend_hour[tt, hh]
    f["trend_slope"] = base.trend_slope[tt]
    with np.errstate(all="ignore"):
        import warnings
        warnings.simplefilter("ignore", RuntimeWarning)
        Dn = base.Dly
        cur = np.nanmean(np.stack([Dn[dd - k] for k in range(1, 91)]), axis=0)
        prev = np.nanmean(np.stack([Dn[dd - 364 - k] for k in range(1, 91)]), axis=0)
        f["yoy90"] = np.clip(cur / np.maximum(prev, 50), 0, 5)

    cal = base.cal.reindex(base.dates)
    for c in CAL_FEATURES:
        v = cal[c].values
        f[c] = v[tt]

    # weather
    hist = wx_hist if wx_hist is not None else wx_obs
    Hd = base.daily_weather(base.weather_arrays(hist))
    htmax = Hd["d_tmax"].copy()
    htmax[np.isnan(base.weather_arrays(hist)["temp_c"][:, 9:21]).all(axis=1)] = np.nan
    # seasonal normal of the daily high: 2-harmonic fit on observed days before 2024 (fixed)
    doy_all = base.dates.dayofyear.values
    X = np.column_stack([np.ones(len(doy_all))] + [fn(2 * np.pi * k * doy_all / 365.25) for k in (1, 2) for fn in (np.sin, np.cos)])
    fit_m = np.isfinite(htmax) & (base.dates < pd.Timestamp("2024-01-01"))
    coef = np.linalg.lstsq(X[fit_m], htmax[fit_m], rcond=None)[0]
    normal_tmax = X @ coef
    with np.errstate(all="ignore"):
        import warnings
        warnings.simplefilter("ignore", RuntimeWarning)
        recent_tmax = np.nanmean(np.stack([htmax[dd - k] for k in range(1, 15)]), axis=0)

    def context(Dwv, m, Dw_next=None):
        """prev/next-day weather and anomalies for rows m from a daily-weather dict."""
        return {"tmax_anom": Dwv["d_tmax"][tt[m]] - normal_tmax[tt[m]],
                "tmax_vs_recent": Dwv["d_tmax"][tt[m]] - recent_tmax[m]}

    def _valid(A, Dwv):
        """daily aggregates are NaN (not 0) on days with no weather data at all"""
        empty = np.isnan(A["temp_c"][:, 9:21]).all(axis=1)
        return {k: np.where(empty, np.nan, v) for k, v in Dwv.items()}

    if weather == "obs":
        A = base.weather_arrays(wx_obs)
        Dw = _valid(A, base.daily_weather(A))
        allm = np.ones(len(tt), bool)
        for k, v in context(Dw, allm).items():
            f[k] = v
        for v in WX_HOURLY:
            f[v] = A[v][tt, hh]
        for v, a in Dw.items():
            f[v] = a[tt]
        pr = A["precip_mm"]
        f["p_precip_3h"] = np.nansum(np.stack([pr[tt, np.maximum(hh - k, 0)] for k in (1, 2, 3)]), axis=0)
    else:  # archived forecasts; N = min(lead + 1, 7)
        for v in WX_HOURLY + WX_DAILY + WX_CONTEXT:
            f[v] = np.full(len(tt), np.nan)
        daily_v = {}
        for n, wx in fc_versions.items():
            A = base.weather_arrays(wx)
            daily_v[n] = (A, _valid(A, base.daily_weather(A)))
        for n, (A, Dw) in daily_v.items():
            m = np.minimum(ll + 1, 7) == n
            if not m.any():
                continue
            # the next day's forecast must be one day older to be equally available at issue time
            for k, v in context(Dw, m, daily_v.get(min(n + 1, 7), (None, Dw))[1]).items():
                f[k][m] = v
            for v in WX_HOURLY:
                f[v][m] = A[v][tt[m], hh[m]]
            for v, a in Dw.items():
                f[v][m] = a[tt[m]]
            pr = A["precip_mm"]
            f["p_precip_3h"][m] = np.nansum(np.stack([pr[tt[m], np.maximum(hh[m] - k, 0)] for k in (1, 2, 3)]), axis=0)

    df = pd.DataFrame(f)
    df.insert(0, "target_date", base.dates[tt])
    df.insert(0, "issue_date", base.dates[dd])
    df["y"] = y
    df["train_weight"] = cal.train_weight.values[tt]
    df["regime"] = cal.regime.values[tt]
    df["holiday_kind"] = pd.Categorical(df["holiday_kind"], categories=HOLIDAY_KINDS)
    if require_target:
        df = df[np.isfinite(df.y)]
    return df.reset_index(drop=True)
