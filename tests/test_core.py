import gzip
import json
from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

from api import logic
from features.calendar import build_calendar
from features.ferry_clean import clean_15min
from features.weather import humidex, qc_precip


def test_archive_is_append_only(tmp_path, monkeypatch):
    import common.archive as arch
    monkeypatch.setattr(arch, "RAW", tmp_path)
    t = datetime(2026, 1, 1, tzinfo=UTC)
    r = arch.save_raw("src", b"hello", url="http://x", fetched_at=t)
    assert gzip.decompress(r.path.read_bytes()) == b"hello"
    with pytest.raises(FileExistsError):
        arch.save_raw("src", b"hello", url="http://x", fetched_at=t)
    m = [json.loads(l) for l in (tmp_path / "src" / "manifest.jsonl").read_text().splitlines()]
    assert len(m) == 1 and m[0]["bytes"] == 5


def test_calendar_holidays_and_long_weekends():
    c = build_calendar("2025-01-01", "2025-12-31").set_index("date")
    assert c.loc["2025-08-04", "holiday_kind"] == "civic"
    assert c.loc["2025-11-11", "is_holiday"] == 0          # Remembrance Day not observed in Ontario
    assert c.loc["2025-04-21", "is_holiday"] == 0          # Easter Monday not observed
    assert list(c.loc["2025-08-02":"2025-08-04", "is_long_weekend"]) == [1, 1, 1]
    assert c.loc["2025-09-01", "is_peak_season"] == 1 and c.loc["2025-09-02", "is_peak_season"] == 0
    assert c.loc["2017-06-15" if False else "2025-06-15", "regime"] == "normal"
    c2 = build_calendar("2017-06-01", "2017-06-02").set_index("date")
    assert c2.loc["2017-06-01", "train_weight"] == 0


def test_bulk_batches_repaired_but_real_peaks_kept():
    ts = pd.date_range("2024-07-13 10:00", periods=12, freq="15min")
    red = [500, 600, 700, 900, 1050, 7000, 950, 800, 700, 600, 500, 400]
    raw = pd.DataFrame({"ts_local": ts, "redemptions": red, "sales": red})
    q = clean_15min(raw)
    assert q.loc["2024-07-13 11:15", "redemptions"] < 1200
    assert q.loc["2024-07-13 11:00", "redemptions"] == 1050
    assert q.bulk_flag.sum() == 1


def test_zero_fill():
    raw = pd.DataFrame({"ts_local": pd.to_datetime(["2024-01-01 10:00", "2024-01-01 11:00"]),
                        "redemptions": [5, 7], "sales": [1, 1]})
    q = clean_15min(raw)
    assert q.loc["2024-01-01 10:30", "redemptions"] == 0


def test_precip_qc_rejects_stuck_gauge():
    idx = pd.date_range("2026-10-01", periods=48, freq="h", tz="UTC")
    cc = pd.Series([610.0] * 24 + [0.2] * 24, index=idx)
    city = pd.Series([0.0] * 24 + [0.2] * 24, index=idx)
    out = qc_precip(cc, city)
    assert out.iloc[:24].isna().all() and (out.iloc[24:] == 0.2).all()


def test_humidex_reference_value():
    # Environment Canada: T=30C, dew point 20C -> humidex ~37
    assert abs(float(humidex(30, 20)) - 37.4) < 0.6


def _day(preds, lead=0):
    return {"date": "2026-07-11", "lead": lead,
            "hours": [{"hour": 6 + i, "pred": p, "p10": p * .7, "p90": p * 1.3, "level": min(3, int(p // 400)),
                       "typical": p, "observed": None} for i, p in enumerate(preds)]}


def test_best_times_and_return_estimate():
    preds = [20, 60, 150, 400, 900, 1300, 1500, 1400, 1200, 900, 700, 500, 300, 200, 120, 80, 40, 20]
    d = _day(preds)
    b = logic.best_times(d)
    assert b["best"][0]["start"] == "18:00"            # quietest departure hour within 09-18
    assert b["worst"][0]["start"] == "12:00"
    r = logic.return_estimate(d)
    w = r["heaviest_window"]
    assert w and 15 <= int(w["start"][:2]) <= 18     # returns lag boardings by a few hours
    assert r["estimate"] is True and "caveat" in r


@pytest.mark.skipif(not __import__("common.config", fromlist=["DB_PATH"]).DB_PATH.exists(),
                    reason="needs the processed database")
def test_no_leakage_from_future_counts():
    """Features for an issue date must not change when data on/after that date changes."""
    from model.dataset import FEATURES, Base, build_rows, obs_weather
    b = Base()
    w = obs_weather()
    a = build_rows(b, "2024-07-10", "2024-07-10", wx_obs=w, require_target=False)
    i = b.idx[pd.Timestamp("2024-07-10")]
    b.R[i:, :] = 1e6
    b.Dsum[i:] = 1e7
    b.S[i:, 6:] = 1e6          # sales after 06:00 on the issue day are not yet known
    b.Rly[i:, :] = 1e6
    b.Dly[i:] = 1e7
    c = build_rows(b, "2024-07-10", "2024-07-10", wx_obs=w, require_target=False)
    c = c.iloc[: len(a)]
    num = [f for f in FEATURES if f != "holiday_kind"]
    np.testing.assert_allclose(a[num].to_numpy(float), c[num].to_numpy(float), equal_nan=True)


def test_intraday_outage_backlog_is_refilled():
    ts = pd.date_range("2026-08-08 06:00", "2026-08-22 23:45", freq="15min")  # a week of history for the profile
    s = pd.Series(300, index=ts)
    s["2026-08-22 11:00":"2026-08-22 15:45"] = 0
    s["2026-08-22 16:30"] = 4889
    raw = pd.DataFrame({"ts_local": ts, "redemptions": s.values, "sales": s.values})
    q = clean_15min(raw)
    day = q.loc["2026-08-22"]
    assert day.loc["2026-08-22 12:00", "refill_flag"] == 1 and day.loc["2026-08-22 12:00", "redemptions"] > 0
    assert day.gap_flag.sum() == 0 and day.bulk_flag.sum() == 0
    # boardings are moved in time, not created or destroyed (up to rounding)
    assert abs(day.redemptions.sum() - day.red_raw.sum()) <= 30
    assert day.loc["2026-08-22 16:30", "redemptions"] < 1200


def test_outage_without_backlog_stays_missing():
    ts = pd.date_range("2024-07-16 06:00", "2024-07-16 23:45", freq="15min")
    s = pd.Series(300, index=ts)
    s["2024-07-16 13:00":"2024-07-16 14:45"] = 0
    q = clean_15min(pd.DataFrame({"ts_local": ts, "redemptions": s.values, "sales": s.values}))
    assert q.loc["2024-07-16 13:30", "gap_flag"] == 1 and q.refill_flag.sum() == 0


def test_capacity_levels_by_schedule_period():
    from model.capacity import capacity_thresholds, period_keys
    from model.core import to_level
    keys = period_keys(["2025-07-12", "2025-07-12", "2025-01-15", "2022-08-25"], [1, 1, 0, 1])
    assert list(keys) == ["summer_peak", "summer_peak", "winter", "summer_peak|minus_mcbride"]
    thr = capacity_thresholds()
    lv = to_level(np.array([3000.0, 300.0, 450.0, 2000.0]), keys, np.array([13, 13, 13, 13]), thr)
    assert lv[0] == 3 and lv[1] == 0          # 3,000/h in peak summer = very busy; 300 = quiet
    assert lv[2] >= 2                         # 450/h with only the winter boat = busy or worse
    assert lv[3] == 3                         # same crowd with a ferry out of service = very busy


@pytest.mark.skipif(not __import__("common.config", fromlist=["DB_PATH"]).DB_PATH.exists(),
                    reason="needs the processed database")
def test_trend_projection_uses_only_past_years():
    """Trend features for a target must not change when data from the issue date onward changes
    (they only look >= 357 days back, so they are valid for leads up to ~1 year)."""
    from model.dataset import Base
    a = Base()
    a.trends()
    i = a.idx[pd.Timestamp("2025-03-01")]
    t = i + 200
    before = (a.trend_day[t], a.trend_hour[t, 13], a.trend_slope[t])
    b = Base()
    b.R[i:, :] = 1e6
    b.Dsum[i:] = 1e7
    b.trends()
    after = (b.trend_day[t], b.trend_hour[t, 13], b.trend_slope[t])
    np.testing.assert_allclose(before, after, equal_nan=True)


def test_qc_precip_keeps_heavy_rain_when_downtown_gauge_is_missing():
    idx = pd.date_range("2025-07-01", periods=24, freq="h", tz="UTC")
    cc = pd.Series([0.0] * 20 + [10.0] * 4, index=idx)          # a real 40 mm day
    city = pd.Series([np.nan] * 24, index=idx)
    assert qc_precip(cc, city).sum() == 40.0


def test_api_rejects_bad_dates_and_years():
    from fastapi import HTTPException

    from api.explore import _date, _years
    assert _date(None, "start") is None and _years(None) is None
    assert _years("2024,2025") == [2024, 2025]
    for bad in (lambda: _date("yesterday", "start"), lambda: _years("20x4"), lambda: _years("1")):
        with pytest.raises(HTTPException) as e:
            bad()
        assert e.value.status_code == 400


def test_quiet_hours_count_as_complete_once_the_feed_has_passed_them(monkeypatch):
    """No row is published for a quiet interval, so the last hours of the night must become
    complete from the fetch time, not from a later row."""
    import features.ferry_clean as fcn
    raw = pd.DataFrame({"ts_local": pd.to_datetime(["2026-11-16 21:15", "2026-11-16 22:30"]),
                        "redemptions": [5, 3], "sales": [1, 1],
                        "fetched_at": pd.Timestamp("2026-11-17 06:25", tz="America/Toronto").tz_convert("UTC")})
    monkeypatch.setattr(fcn.db, "table", lambda name: raw)
    h = fcn.hourly_table().set_index("ts_local")
    assert h.loc["2026-11-16 23:00", "complete"]            # nobody boarded after 22:45: known, not missing
    assert h.loc["2026-11-17 04:00", "complete"]            # overnight hours before the fetch are known
    assert not h.loc["2026-11-17 06:00", "complete"]        # the hour in progress is not
