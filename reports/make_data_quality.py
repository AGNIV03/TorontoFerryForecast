"""Generate reports/data_quality.md from the processed tables (python -m reports.make_data_quality)."""

from __future__ import annotations


import numpy as np
import pandas as pd

from common import db
from common.archive import iter_manifest
from common.config import REPORTS
from common.timeutil import label
from features.calendar import REGIMES
from features.ferry_clean import BULK_ABS, BULK_RATIO


def md(df: pd.DataFrame, index=True, floatfmt=".2f") -> str:
    return df.to_markdown(index=index, floatfmt=floatfmt)


def main():
    raw = db.table("ferry_15min")
    h = db.table("ferry_hourly")
    w = db.table("weather_obs")
    ec = db.table("eccc_hourly")
    out = []
    p = out.append
    p("# Data quality report\n")
    p(f"_Generated {label()} by `python -m reports.make_data_quality`._\n")

    # ---------------------------------------------------------------- ferry
    p("## 1. Ferry ticket counts (City of Toronto Open Data)\n")
    p(f"* Rows published: **{len(raw):,}**, {raw.ts_local.min()} -> {raw.ts_local.max()} (15-minute intervals).")
    dup = raw.ts_local.duplicated().sum()
    p(f"* Duplicate timestamps: **{dup}**. Rows with both counts zero: **{int(((raw.redemptions == 0) & (raw.sales == 0)).sum())}**. "
      "Negative counts: **0**. All timestamps fall on :00/:15/:30/:45.")
    p("* Sort order: the datastore returns rows **newest first** (`_id` 1 = latest interval). We sort on parse.")
    p("* Revisions: every fetch is archived; on parse the most recent fetch wins per interval, and the parser "
      "logs how many intervals changed between fetches (0 so far).\n")

    p("### Timezone\n")
    p("`Timestamp` is **local wall-clock time (America/Toronto), naive**. Evidence:\n")
    p("1. The first boarding of the day (first interval with >= 5 redemptions after 05:00) has a median of "
      "**06:30 in the three weeks before and after every spring and fall DST change, 2016-2026**. If the "
      "timestamps were UTC or fixed-offset, the first boarding would jump by one hour at each change.")
    p("2. The newest row (17:45) was published at 23:18 UTC = 18:18 EST, i.e. a ~30-45 minute publication lag in local time.")
    p("3. No duplicate timestamps exist in the fall-back hour (01:00-01:59). The ferry carries almost nobody "
      "at that hour, so the ambiguity is immaterial; we map ambiguous local times to the first (DST) "
      "occurrence when joining with UTC weather.\n")

    p("### Zero-fill\n")
    p("Intervals with no activity are **absent** (no published row has both counts zero), so every 15-minute "
      "slot between the first record and the last published interval is zero-filled. An hour counts as "
      "complete only once its :45 interval is published.\n")

    yr = raw.groupby(raw.ts_local.dt.year).agg(rows=("ts_local", "size"), redemptions=("redemptions", "sum"),
                                                sales=("sales", "sum"))
    days = h.groupby(h.ts_local.dt.year).agg(days=("date", "nunique"))
    yr = yr.join(days)
    yr["slots_possible"] = yr.days * 96
    yr["published_share"] = yr.rows / yr.slots_possible
    yr["sales/redemptions"] = yr.sales / yr.redemptions
    p("### Coverage per year\n")
    p(md(yr[["rows", "slots_possible", "published_share", "redemptions", "sales", "sales/redemptions"]]))
    p("\n`published_share` is low because nothing is published overnight (00:00-06:00) when there is no activity; "
      "it is not missing data.\n")

    p("### Bulk-batch intervals (repaired)\n")
    p(f"Isolated 15-minute intervals with more than **{BULK_ABS:,}** redemptions and more than {BULK_RATIO:g}x the "
      "median of the 4 neighbouring intervals (or more than twice the limit outright). The real peak "
      "boarding rate tops out around 1,100 per 15 minutes (summer-weekend 99.9th percentile = 963), "
      "and in every interval above 1,000, sales match redemptions to within 10% in the same quarter-hour. These are "
      "batch transactions (e.g. group or partner allocations processed at once), not people boarding at that "
      "moment. Exception: a spike right after an intra-day outage is a backlog upload and is handled below. "
      f"**{int(h.bulk_flag.sum())} intervals** were replaced by the neighbouring median (the original values are kept "
      "in `red_raw`/`sales_raw`).\n")
    q = raw.set_index("ts_local")
    top = q.nlargest(10, "redemptions")[["redemptions", "sales"]]
    p("Ten largest raw intervals:\n")
    p(md(top))
    p(f"\nRedemptions removed by the repair: **{int((h.red_raw - h.redemptions).sum()):,}** "
      f"({(h.red_raw - h.redemptions).sum() / h.red_raw.sum():.2%} of all redemptions). Smaller isolated "
      "spikes (300-1,000 in one interval, mostly weekday mornings in May-June) are **kept**: they are "
      "plausible school and group boardings.\n")

    p("### Outage days\n")
    od = h[h.outage_day == 1].date.drop_duplicates()
    p(f"Days with zero redemptions all day: **{len(od)}** ({', '.join(d.strftime('%Y-%m-%d') for d in od)}). "
      "We can't tell a feed outage from a full service suspension (e.g. ice), so these are treated as "
      "missing and excluded from training and evaluation.\n")

    p("### Intra-day outages and backlog uploads\n")
    p("A run of >= 1.5 h of zero boardings between 09:00 and 19:00, on a day with >= 1,500 boardings, is treated as a "
      "feed/scanner outage, not as nobody travelling. When an above-capacity spike follows within 3 h, it is the "
      "backlog being uploaded (e.g. **2026-08-22**: zero from 11:00 to 15:59 on a summer Saturday, then 4,889 scans "
      "at 16:30). Those boardings are real, so they are **refilled**: the backlog's excess over the normal level is "
      "spread back over the outage intervals in proportion to the same intervals one week earlier (`refill_flag`). "
      "The day total is unchanged. Outages with no backlog after them stay **missing** (`gap_flag`), excluded from "
      "training and evaluation.\n")
    rf = h[h.refill_flag > 0].groupby("date").agg(hours_refilled=("hour", "nunique"),
                                                  boardings_in_refilled_hours=("redemptions", "sum")).reset_index()
    gp = h[h.gap_flag > 0].groupby("date").agg(hours_missing=("hour", "nunique")).reset_index()
    t = rf.merge(gp, on="date", how="outer").fillna(0)
    t["date"] = t.date.dt.date
    t = t[t.date.astype(str) >= "2015-07-13"].sort_values("date")
    p(md(t, index=False, floatfmt=".0f"))
    p("\n2024-07-16 is the day of the Toronto flash-flood storm: the zeros may be a genuine suspension of service. "
      "Either way it is unforecastable from these inputs, so leaving it missing is the right call. Gaps in the 2015 "
      "roll-out period are excluded anyway.\n")

    p("### Anomaly periods and structural breaks\n")
    d = h[h.complete].groupby("date").redemptions.sum()
    dfw = pd.DataFrame({"red": d})
    dfw["yr"] = dfw.index.year
    dfw["woy"] = dfw.index.isocalendar().week.values
    wk = dfw.groupby(["yr", "woy"]).red.sum()
    base = wk.loc[[2018, 2019, 2022, 2023, 2024, 2025]].groupby("woy").median()
    rel = (wk / wk.index.get_level_values(1).map(base)).rename("rel")
    reg = pd.DataFrame(REGIMES, columns=["start", "end", "regime", "training_weight"])

    def rel_between(s, e):
        m = (dfw.index >= s) & (dfw.index <= e)
        sub = dfw[m].groupby(["yr", "woy"]).red.sum()
        r = sub / sub.index.get_level_values(1).map(base)
        return f"{r.min():.0%}-{r.max():.0%}"
    reg["weekly volume vs normal years"] = [rel_between(s, e) for s, e in zip(reg.start, reg.end)]
    reg["decision"] = ["exclude", "exclude", "exclude", "keep with indicator `covid_recovery`, weight 0.5"]
    p(md(reg, index=False))
    p("""
"Normal years" = median weekly redemptions over 2018, 2019 and 2022-2025.

* **2015 ticketing roll-out (May 1 - Jul 12, 2015).** Weekly sales exceeded redemptions by 15-56% (up to
  7x in early July), against ~5% in every later summer. Redemption scanning was evidently not yet
  recording all boardings. From the week of Jul 13 the ratio is normal. **Excluded.**
* **2017 Lake Ontario flooding (May 1 - Jul 30, 2017).** Volume fell to 3-8% of normal (Island open to
  residents only) and recovered from the week of Jul 31. **Excluded.**
* **COVID-19 (Mar 14, 2020 - Jun 30, 2021).** Closures, residents-only service, then capacity limits:
  3-35% of normal. **Excluded.**
* **COVID recovery (Jul - Dec 2021).** 50-90% of normal, recovering steadily. **Kept** with an indicator
  feature (always 0 at prediction time) and half training weight. 2022 onwards is back to normal levels.
* **Ticket-product change (from ~Nov 2017).** Off-season sales fall to ~70-80% of redemptions every winter
  since Nov 2017 (they were ~100% in winter 2016/17); summers stay at ~100%. Most likely multi-ride or
  pass products (e.g. for Island residents and workers) that are redeemed without a matching same-period
  sale. **Redemptions (the target) show no level break**, so no action is needed for the target; the
  model's sales-based features are relative to the same period last year.
* **Online sales.** Overnight (00:00-06:00) sales are necessarily online. They rose from 0.2% of sales in
  2015 to 0.5-0.8% in 2016-2019, jumped to ~2.2% in 2020-21 (online-only timed tickets during the pandemic),
  and settled at ~0.8-0.9% from 2022. A 30k overnight batch in Aug 2026 is a bulk sale and was repaired
  by the rule above. The exact start date of online sales can't be identified from these data.
""")

    p("Weekly volume relative to normal years, anomaly years:\n")
    rel_t = rel.unstack(0)[[2015, 2017, 2020, 2021]].round(2)
    rel_t = rel_t.loc[(rel_t.index >= 14) & (rel_t.index <= 44)]
    p(md(rel_t.T, floatfmt=".2f"))
    p("")

    # ---------------------------------------------------------------- weather
    p("## 2. Weather observations\n")
    st = pd.DataFrame([
        ("6158359", "TORONTO CITY CENTRE (Billy Bishop)", "2009-12-10 -> present", "primary"),
        ("6158667", "TORONTO CITY CENTRE (old ID)", "2006-01-01 -> 2010-03-02", "not needed: ends before 2015"),
        ("6158665", "TORONTO ISLAND A", "1957 -> 2006-03-27", "not needed"),
        ("6158355", "TORONTO CITY (downtown)", "2002-06-04 -> present", "fallback for temperature, dew point, RH, precipitation"),
    ], columns=["climate ID", "station", "hourly record", "use"])
    p(md(st, index=False))
    p("""
* **No stitching across station IDs is needed for 2015 onwards.** The current Billy Bishop ID (6158359)
  covers the whole period.
* **ECCC `LOCAL_DATE` is Local *Standard* Time all year** (in July, 14:00 local = 19:00 UTC). We key on
  `UTC_DATE`. Using `LOCAL_DATE` would shift every summer observation by one hour.
* ECCC hourly data for these stations has **no wind gusts and no cloud cover**. `WEATHER_ENG_DESC` is filled
  only when a phenomenon is present (rain, snow, fog: ~15% of hours) and is blank otherwise.
  **Humidex** is published only when it is >= 25 (temperature >= 20 C). We compute humidex from temperature and
  dew point for every hour, so observed and forecast humidex share one definition.
* The downtown station (6158355) reports no wind.
* Cloud cover, wind gusts and shortwave radiation come from the **Open-Meteo historical archive**
  (ERA5 / ECMWF IFS reanalysis) for every hour. That is model data, not an observation, and is labelled as such.
""")
    w["year"] = w.ts_utc.dt.year
    p("### Coverage per variable per year (Billy Bishop, before gap filling)\n")
    cc = ec[ec.climate_id == "6158359"].copy()
    cc["year"] = cc.ts_utc.dt.year
    hours = w.groupby("year").size().rename("hours in year")
    cov = cc.groupby("year")[["temp_c", "dewpoint_c", "rh", "precip_mm", "wind_kmh", "humidex", "weather_desc"]].count()
    cov = cov.div(hours, axis=0).clip(upper=1)
    p(md(cov, floatfmt=".3f"))
    p("\n(`humidex` and `weather_desc` are sparse by design, see above.)\n")
    p("### Gap filling: source of each value after stitching (count of hours)\n")
    for v in ["temp_c", "rh", "precip_mm", "wind_kmh"]:
        t = w.groupby("year")[v + "_src"].value_counts().unstack().fillna(0).astype(int)
        p(f"**{v}**\n")
        p(md(t))
        p("")
    # agreement eccc vs om archive
    om = db.table("om_archive").set_index("ts_utc")
    j = cc.set_index("ts_utc").join(om.drop(columns="fetched_at"), how="inner")
    p("""### Billy Bishop precipitation gauge failure (Sep 30, 2026 -> present)

From **2026-09-30 19:00 UTC** the Billy Bishop gauge reports a stuck value of ~604-611 mm *every hour*
(a raw 2026 total of 168,000 mm), while the downtown gauge reads normally (33 mm in October 2026). Earlier
years are plausible (590-875 mm/year). QC rule (`features/weather.py::qc_precip`): a UTC day of Billy
Bishop precipitation is rejected if any hour exceeds 100 mm, or the day's total exceeds
max(3 x downtown, downtown + 25 mm). Rejected hours fall back to the downtown station. The rule
rejects the whole failure period plus a handful of earlier days. **This affects training labels and weather
scoring only; live predictions use forecasts.** The feed-health check should alert on this kind of
failure (Milestone 4).
""")
    yrp = w.groupby("year").precip_mm.sum().round(0).to_frame("precip after QC (mm)")
    yrp["raw Billy Bishop (mm)"] = cc.groupby("year").precip_mm.sum().round(0)
    p(md(yrp, floatfmt=".0f"))
    p("")
    jj = j[j.index < "2026-09-30"]
    agree = pd.DataFrame({
        "temperature MAE (C)": [np.nanmean(np.abs(j.temp_c - j.temperature_2m))],
        "wind MAE (km/h)": [np.nanmean(np.abs(j.wind_kmh - j.wind_speed_10m))],
        "annual precip ECCC (mm)": [jj.groupby(jj.index.year).precip_mm.sum().mean()],
        "annual precip reanalysis (mm)": [jj.groupby(jj.index.year).precipitation.sum().mean()],
    })
    p("Agreement between Billy Bishop observations and the reanalysis used for gap filling (all hours since 2015):\n")
    p(md(agree, index=False))
    p("""
The reanalysis is a good stand-in for the rare gaps in temperature and wind. Its precipitation is smoother
than gauge data (more drizzle hours, lower peaks), which is why it is used only as a last resort for
precipitation.
""")

    # ---------------------------------------------------------------- forecasts & events
    p("## 3. Weather forecasts\n")
    pr_files = sum(1 for _ in iter_manifest("om_previous_runs"))
    fc_files = sum(1 for _ in iter_manifest("om_forecast"))
    p(f"""* **Archived past forecasts exist.** Open-Meteo's Previous Runs API returns, for each valid hour, the value
  predicted 24 x N hours earlier (N = 1..7). Coverage starts **2024-01-21** (GEM, GFS, ICON) and **2024-02-06** (ECMWF IFS
  0.25; radiation and RH from early March 2024). HRDPS and GEM regional are only available for N <= 1-2 from Sep 2024, and
  ECMWF IFS 9 km from 2025. That is enough for an honest backtest of the 2024-2026 holdout seasons.
  {pr_files} raw responses archived.
* **Live forecasts** (7 models, 9 days ahead, at the terminal) are archived every 3 hours from 2026-11-16:
  {fc_files} snapshots so far.
* Terminal location: 43.6405, -79.3755 (Jack Layton Ferry Terminal building). Grid points used: ECMWF 0.25 -> 43.75, -79.50
  (~14 km away, a coarse grid); GEM -> 43.66, -79.40; GFS -> 43.65, -79.38.
""")
    p("## 4. Events\n")
    p("""* The City's Festivals & Events JSON feed is **~240 MB per download (~18 MB gzipped)**, ~29k event records.
  A daily archive costs ~6.5 GB/year of disk. It contains current and upcoming events, plus past dates of
  recurring events back to mid-2025. Only ~20 rows are on the Island; tens of thousands are at
  Harbourfront / Queens Quay (mostly long-running exhibitions).
* A separate **historical XML feed (Jan 2014 - Dec 2016)** exists and was archived once.
* The feed is too short to train on. Events that matter go in the hand-editable `config/events.csv`
  (schema in `config/README.md`).
""")
    p("## 5. Calendar\n")
    p("Ontario statutory holidays plus the Civic Holiday (Easter Monday and Remembrance Day are not observed), long weekends "
      "(a run of >= 3 days off containing a holiday), approximate TDSB March Break and summer break (`config/school_calendar.csv`), "
      "DST, and the peak season (June 1 - Labour Day).\n")
    (REPORTS / "data_quality.md").write_text("\n".join(out), encoding="utf-8")
    print("wrote", REPORTS / "data_quality.md")


if __name__ == "__main__":
    main()
