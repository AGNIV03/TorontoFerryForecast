"""Daily calendar table: holidays, long weekends, school breaks, DST, events, anomaly regimes."""

from __future__ import annotations

from datetime import date, timedelta

import holidays
import numpy as np
import pandas as pd

from common.config import CONFIG_DIR, TZ

# Data-quality regimes (see reports/data_quality.md). weight 0 = excluded from training.
REGIMES = [
    # start, end (inclusive), name, training weight
    ("2015-05-01", "2015-07-12", "ticketing_rollout", 0.0),
    ("2017-05-01", "2017-07-30", "flood_closure", 0.0),
    ("2020-03-14", "2021-06-30", "covid_closure", 0.0),
    ("2021-07-01", "2021-12-31", "covid_recovery", 0.5),
]


def ontario_holidays(years) -> dict[date, str]:
    # Ontario statutory holidays + Civic Holiday (first Monday of August, not statutory
    # but widely observed) + Easter Monday is NOT included (not observed in Ontario).
    h = holidays.Canada(subdiv="ON", years=years, categories=("public", "optional"))
    out = {}
    for d, name in h.items():
        if "Easter Monday" in name or "Remembrance" in name:
            continue
        out[d] = name
    for y in years:  # make sure Civic Holiday is there
        aug1 = date(y, 8, 1)
        civic = aug1 + timedelta(days=(0 - aug1.weekday()) % 7)
        out.setdefault(civic, "Civic Holiday")
    return out


def _holiday_kind(name: str) -> str:
    n = name.lower()
    for key in ["canada day", "civic", "labour", "victoria", "thanksgiving", "good friday",
                "family", "christmas", "boxing", "new year"]:
        if key in n:
            return key.replace(" ", "_")
    return "other"


def build_calendar(start="2015-01-01", end="2028-12-31") -> pd.DataFrame:
    days = pd.date_range(start, end, freq="D")
    df = pd.DataFrame({"date": days.date})
    d = pd.to_datetime(df["date"])
    years = range(days.min().year - 1, days.max().year + 2)
    hol = ontario_holidays(years)

    df["dow"] = d.dt.weekday
    df["month"] = d.dt.month
    df["doy"] = d.dt.day_of_year
    df["woy"] = d.dt.isocalendar().week.astype(int).values
    df["year"] = d.dt.year
    df["is_weekend"] = (df.dow >= 5).astype(int)
    df["is_holiday"] = df["date"].map(lambda x: int(x in hol))
    df["holiday_kind"] = df["date"].map(lambda x: _holiday_kind(hol[x]) if x in hol else "none")
    off = (df.is_weekend == 1) | (df.is_holiday == 1)
    # long weekend: a run of >=3 consecutive days off that contains a holiday
    run_id = (off != off.shift()).cumsum()
    run_len = off.groupby(run_id).transform("size")
    run_has_hol = df.is_holiday.groupby(run_id).transform("max")
    df["is_long_weekend"] = (off & (run_len >= 3) & (run_has_hol == 1)).astype(int)
    df["is_day_off"] = off.astype(int)
    # day position within a long weekend (1..n), 0 otherwise; and the day before one
    pos = off.groupby(run_id).cumcount() + 1
    df["long_weekend_pos"] = np.where(df.is_long_weekend == 1, pos, 0)
    df["is_eve_of_long_weekend"] = df.is_long_weekend.shift(-1, fill_value=0).astype(bool) & (df.is_long_weekend == 0)
    df["is_eve_of_long_weekend"] = df["is_eve_of_long_weekend"].astype(int)

    hol_dates = np.array(sorted(pd.to_datetime(list(hol))), dtype="datetime64[D]")
    dd = d.values.astype("datetime64[D]")
    idx = np.searchsorted(hol_dates, dd)
    nxt = hol_dates[np.minimum(idx, len(hol_dates) - 1)]
    prv = hol_dates[np.maximum(idx - 1, 0)]
    df["days_to_holiday"] = np.clip((nxt - dd).astype(int), 0, 30)
    df["days_since_holiday"] = np.clip((dd - prv).astype(int), 0, 30)

    # school calendar
    sc = pd.read_csv(CONFIG_DIR / "school_calendar.csv", parse_dates=[
        "march_break_start", "march_break_end", "last_school_day", "first_school_day_fall"])
    df["is_march_break"] = 0
    df["is_school_summer"] = 0
    for _, r in sc.iterrows():
        m = (d >= r.march_break_start) & (d <= r.march_break_end)
        df.loc[m, "is_march_break"] = 1
        m = (d > r.last_school_day) & (d < r.first_school_day_fall)
        df.loc[m, "is_school_summer"] = 1
    # weekday when school is out (summer, march break, holidays) - family trips
    df["is_school_day"] = ((df.is_day_off == 0) & (df.is_march_break == 0) & (df.is_school_summer == 0)
                           & ~((df.month == 12) & (d.dt.day >= 24)) & ~((df.month == 1) & (d.dt.day <= 3))).astype(int)

    # DST in effect (at noon local)
    noon = pd.to_datetime(df["date"]).dt.tz_localize(TZ, ambiguous=True, nonexistent="shift_forward") + pd.Timedelta(hours=12)
    df["is_dst"] = noon.map(lambda t: int(bool(t.dst()))).values

    # Labour Day of the date's year -> peak-season flag (June 1 .. Labour Day)
    labour = {y: next(k for k, v in hol.items() if k.year == y and "Labour" in v) for y in years}
    ld = df["year"].map(labour)
    df["is_peak_season"] = ((d.dt.month >= 6) & (pd.to_datetime(ld) >= d)).astype(int)
    df["days_to_labour_day"] = (pd.to_datetime(ld) - d).dt.days.clip(-120, 120)

    # anomaly regimes
    df["regime"] = "normal"
    df["train_weight"] = 1.0
    for s, e, name, w in REGIMES:
        m = (d >= pd.Timestamp(s)) & (d <= pd.Timestamp(e))
        df.loc[m, "regime"] = name
        df.loc[m, "train_weight"] = w
    df["covid_recovery"] = (df.regime == "covid_recovery").astype(int)

    # hand-curated events
    ev = pd.read_csv(CONFIG_DIR / "events.csv", parse_dates=["date_start", "date_end"])
    df["event_island"] = 0
    df["event_waterfront"] = 0
    for _, r in ev[ev.kind == "event"].iterrows():
        m = (d >= r.date_start) & (d <= r.date_end)
        col = "event_island" if r.zone == "island" else "event_waterfront"
        df.loc[m, col] = 1
    df["date"] = pd.to_datetime(df["date"])
    return df


def season_of(month: pd.Series, is_peak: pd.Series) -> pd.Series:
    """peak (Jun 1-Labour Day) / shoulder (Apr, May, rest of Sep, Oct) / off (Nov-Mar)."""
    s = pd.Series("off", index=month.index)
    s[month.isin([4, 5, 9, 10])] = "shoulder"
    s[(month.isin([9])) & (is_peak == 1)] = "peak"
    s[is_peak == 1] = "peak"
    return s
