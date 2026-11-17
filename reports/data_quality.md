# Data quality report

_Generated 2026-11-16 21:15 EST by `python -m reports.make_data_quality`._

## 1. Ferry ticket counts (City of Toronto Open Data)

* Rows published: **286,202**, 2015-05-01 13:30:00 -> 2026-11-16 19:30:00 (15-minute intervals).
* Duplicate timestamps: **0**. Rows with both counts zero: **0**. Negative counts: **0**. All timestamps fall on :00/:15/:30/:45.
* Sort order: the datastore returns rows **newest first** (`_id` 1 = latest interval). We sort on parse.
* Revisions: every fetch is archived; on parse the most recent fetch wins per interval, and the parser logs how many intervals changed between fetches (0 so far).

### Timezone

`Timestamp` is **local wall-clock time (America/Toronto), naive**. Evidence:

1. The first boarding of the day (first interval with >= 5 redemptions after 05:00) has a median of **06:30 in the three weeks before and after every spring and fall DST change, 2016-2026**. If the timestamps were UTC or fixed-offset, the first boarding would jump by one hour at each change.
2. The newest row (17:45) was published at 23:18 UTC = 18:18 EST, i.e. a ~30-45 minute publication lag in local time.
3. No duplicate timestamps exist in the fall-back hour (01:00-01:59). The ferry carries almost nobody at that hour, so the ambiguity is immaterial; we map ambiguous local times to the first (DST) occurrence when joining with UTC weather.

### Zero-fill

Intervals with no activity are **absent** (no published row has both counts zero), so every 15-minute slot between the first record and the last published interval is zero-filled. An hour counts as complete only once its :45 interval is published.

### Coverage per year

|   ts_local |     rows |   slots_possible |   published_share |   redemptions |      sales |   sales/redemptions |
|-----------:|---------:|-----------------:|------------------:|--------------:|-----------:|--------------------:|
|       2015 | 14995.00 |         23520.00 |              0.64 |     978707.00 | 1189620.00 |                1.22 |
|       2016 | 23240.00 |         35136.00 |              0.66 |    1425779.00 | 1518428.00 |                1.06 |
|       2017 | 22101.00 |         35040.00 |              0.63 |     698861.00 |  682346.00 |                0.98 |
|       2018 | 24665.00 |         35040.00 |              0.70 |    1493560.00 | 1463589.00 |                0.98 |
|       2019 | 24670.00 |         35040.00 |              0.70 |    1278505.00 | 1249725.00 |                0.98 |
|       2020 | 22741.00 |         35136.00 |              0.65 |     374546.00 |  366606.00 |                0.98 |
|       2021 | 25231.00 |         35040.00 |              0.72 |     773040.00 |  782368.00 |                1.01 |
|       2022 | 25733.00 |         35040.00 |              0.73 |    1349778.00 | 1346659.00 |                1.00 |
|       2023 | 26295.00 |         35040.00 |              0.75 |    1502883.00 | 1491473.00 |                0.99 |
|       2024 | 26261.00 |         35136.00 |              0.75 |    1390358.00 | 1373563.00 |                0.99 |
|       2025 | 26240.00 |         35040.00 |              0.75 |    1525849.00 | 1513483.00 |                0.99 |
|       2026 | 24030.00 |         30720.00 |              0.78 |    1626537.00 | 1655431.00 |                1.02 |

`published_share` is low because nothing is published overnight (00:00-06:00) when there is no activity; it is not missing data.

### Bulk-batch intervals (repaired)

Isolated 15-minute intervals with more than **1,200** redemptions and more than 8x the median of the 4 neighbouring intervals (or more than twice the limit outright). The real peak boarding rate tops out around 1,100 per 15 minutes (summer-weekend 99.9th percentile = 963), and in every interval above 1,000, sales match redemptions to within 10% in the same quarter-hour. These are batch transactions (e.g. group or partner allocations processed at once), not people boarding at that moment. Exception: a spike right after an intra-day outage is a backlog upload and is handled below. **35 intervals** were replaced by the neighbouring median (the original values are kept in `red_raw`/`sales_raw`).

Ten largest raw intervals:

| ts_local            |   redemptions |   sales |
|:--------------------|--------------:|--------:|
| 2023-08-15 20:15:00 |          7216 |    7229 |
| 2023-10-10 14:00:00 |          5535 |    5518 |
| 2024-06-19 19:00:00 |          5341 |    5342 |
| 2025-09-01 17:30:00 |          5256 |    5218 |
| 2024-08-05 22:00:00 |          4973 |    4995 |
| 2024-09-07 17:00:00 |          4966 |    4974 |
| 2026-08-22 16:30:00 |          4889 |    4819 |
| 2026-07-16 19:15:00 |          4823 |    4836 |
| 2026-08-08 18:45:00 |          4359 |    4381 |
| 2025-09-24 22:30:00 |          4020 |    4015 |

Redemptions removed by the repair: **101,159** (0.70% of all redemptions). Smaller isolated spikes (300-1,000 in one interval, mostly weekday mornings in May-June) are **kept**: they are plausible school and group boardings.

### Outage days

Days with zero redemptions all day: **13** (2015-05-02, 2015-05-03, 2018-01-03, 2018-01-04, 2018-01-05, 2018-01-06, 2021-02-21, 2021-02-22, 2022-02-11, 2022-02-12, 2022-12-24, 2025-01-26, 2025-01-27). We can't tell a feed outage from a full service suspension (e.g. ice), so these are treated as missing and excluded from training and evaluation.

### Intra-day outages and backlog uploads

A run of >= 1.5 h of zero boardings between 09:00 and 19:00, on a day with >= 1,500 boardings, is treated as a feed/scanner outage, not as nobody travelling. When an above-capacity spike follows within 3 h, it is the backlog being uploaded (e.g. **2026-08-22**: zero from 11:00 to 15:59 on a summer Saturday, then 4,889 scans at 16:30). Those boardings are real, so they are **refilled**: the backlog's excess over the normal level is spread back over the outage intervals in proportion to the same intervals one week earlier (`refill_flag`). The day total is unchanged. Outages with no backlog after them stay **missing** (`gap_flag`), excluded from training and evaluation.

| date       |   hours_refilled |   boardings_in_refilled_hours |   hours_missing |
|:-----------|-----------------:|------------------------------:|----------------:|
| 2017-04-23 |                4 |                          1991 |               0 |
| 2019-08-29 |                6 |                          2559 |               0 |
| 2024-07-16 |                0 |                             0 |               2 |
| 2025-05-27 |                0 |                             0 |               6 |
| 2026-08-22 |                6 |                          5994 |               0 |

2024-07-16 is the day of the Toronto flash-flood storm: the zeros may be a genuine suspension of service. Either way it is unforecastable from these inputs, so leaving it missing is the right call. Gaps in the 2015 roll-out period are excluded anyway.

### Anomaly periods and structural breaks

| start      | end        | regime            |   training_weight | weekly volume vs normal years   | decision                                         |
|:-----------|:-----------|:------------------|------------------:|:--------------------------------|:-------------------------------------------------|
| 2015-05-01 | 2015-07-12 | ticketing_rollout |              0.00 | 0%-79%                          | exclude                                          |
| 2017-05-01 | 2017-07-30 | flood_closure     |              0.00 | 3%-25%                          | exclude                                          |
| 2020-03-14 | 2021-06-30 | covid_closure     |              0.00 | 3%-93%                          | exclude                                          |
| 2021-07-01 | 2021-12-31 | covid_recovery    |              0.50 | 27%-100%                        | keep with indicator `covid_recovery`, weight 0.5 |

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

Weekly volume relative to normal years, anomaly years:

|   yr | 14   | 15   | 16   | 17   |   18 |   19 |   20 |   21 |   22 |   23 |   24 |   25 |   26 |   27 |   28 |   29 |   30 |   31 |   32 |   33 |   34 |   35 |   36 |   37 |   38 |   39 |   40 |   41 |   42 |   43 |   44 |
|-----:|:-----|:-----|:-----|:-----|-----:|-----:|-----:|-----:|-----:|-----:|-----:|-----:|-----:|-----:|-----:|-----:|-----:|-----:|-----:|-----:|-----:|-----:|-----:|-----:|-----:|-----:|-----:|-----:|-----:|-----:|-----:|
| 2015 | <NA> | <NA> | <NA> | <NA> | 0.00 | 0.53 | 0.56 | 0.67 | 0.47 | 0.79 | 0.39 | 0.78 | 0.45 | 0.15 | 0.10 | 0.93 | 0.97 | 1.01 | 1.13 | 0.98 | 1.06 | 0.86 | 1.83 | 0.82 | 0.79 | 1.12 | 0.42 | 0.76 | 0.64 | 0.40 | 0.43 |
| 2017 | 0.93 | 1.64 | 1.08 | 0.97 | 0.25 | 0.13 | 0.08 | 0.05 | 0.06 | 0.05 | 0.04 | 0.05 | 0.03 | 0.04 | 0.05 | 0.06 | 0.06 | 0.66 | 0.82 | 0.91 | 0.88 | 0.81 | 0.96 | 1.17 | 1.29 | 1.07 | 1.08 | 0.65 | 1.12 | 0.58 | 0.62 |
| 2020 | 0.12 | 0.08 | 0.08 | 0.08 | 0.06 | 0.05 | 0.04 | 0.03 | 0.04 | 0.05 | 0.03 | 0.05 | 0.12 | 0.30 | 0.19 | 0.23 | 0.26 | 0.21 | 0.27 | 0.28 | 0.33 | 0.28 | 0.56 | 0.32 | 0.30 | 0.54 | 0.26 | 0.34 | 0.35 | 0.34 | 0.39 |
| 2021 | 0.58 | 0.31 | 0.31 | 0.23 | 0.21 | 0.35 | 0.47 | 0.40 | 0.58 | 0.56 | 0.37 | 0.38 | 0.45 | 0.27 | 0.33 | 0.57 | 0.48 | 0.68 | 0.56 | 0.68 | 0.68 | 0.83 | 0.67 | 0.64 | 0.38 | 0.63 | 0.53 | 0.72 | 0.65 | 0.53 | 0.90 |

## 2. Weather observations

|   climate ID | station                            | hourly record            | use                                                    |
|-------------:|:-----------------------------------|:-------------------------|:-------------------------------------------------------|
|      6158359 | TORONTO CITY CENTRE (Billy Bishop) | 2009-12-10 -> present    | primary                                                |
|      6158667 | TORONTO CITY CENTRE (old ID)       | 2006-01-01 -> 2010-03-02 | not needed: ends before 2015                           |
|      6158665 | TORONTO ISLAND A                   | 1957 -> 2006-03-27       | not needed                                             |
|      6158355 | TORONTO CITY (downtown)            | 2002-06-04 -> present    | fallback for temperature, dew point, RH, precipitation |

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

### Coverage per variable per year (Billy Bishop, before gap filling)

|   year |   temp_c |   dewpoint_c |    rh |   precip_mm |   wind_kmh |   humidex |   weather_desc |
|-------:|---------:|-------------:|------:|------------:|-----------:|----------:|---------------:|
|   2015 |    0.997 |        0.985 | 0.985 |       0.998 |      0.997 |     0.122 |          0.163 |
|   2016 |    0.999 |        0.999 | 0.999 |       0.999 |      0.998 |     0.186 |          0.138 |
|   2017 |    0.999 |        0.999 | 0.999 |       0.999 |      0.999 |     0.133 |          0.157 |
|   2018 |    1.000 |        1.000 | 1.000 |       1.000 |      1.000 |     0.203 |          0.162 |
|   2019 |    0.989 |        0.983 | 0.983 |       0.998 |      0.997 |     0.132 |          0.177 |
|   2020 |    0.999 |        0.999 | 0.999 |       0.999 |      0.998 |     0.179 |          0.147 |
|   2021 |    0.997 |        0.994 | 0.995 |       0.998 |      0.998 |     0.166 |          0.129 |
|   2022 |    0.971 |        0.950 | 0.950 |       0.998 |      0.997 |     0.144 |          0.126 |
|   2023 |    0.999 |        0.999 | 0.999 |       0.999 |      0.999 |     0.155 |          0.154 |
|   2024 |    0.997 |        0.997 | 0.997 |       0.997 |      0.996 |     0.185 |          0.138 |
|   2025 |    0.998 |        0.998 | 0.998 |       0.997 |      0.996 |     0.159 |          0.142 |
|   2026 |    0.999 |        0.995 | 0.995 |       0.961 |      0.999 |     0.179 |          0.165 |

(`humidex` and `weather_desc` are sparse by design, see above.)

### Gap filling: source of each value after stitching (count of hours)

**temp_c**

|   year |   eccc_cc |   eccc_city |   om_archive |
|-------:|----------:|------------:|-------------:|
|   2015 |      8737 |          18 |            5 |
|   2016 |      8778 |           6 |            0 |
|   2017 |      8749 |          11 |            0 |
|   2018 |      8756 |           4 |            0 |
|   2019 |      8665 |          95 |            0 |
|   2020 |      8771 |          11 |            2 |
|   2021 |      8734 |          25 |            1 |
|   2022 |      8509 |         248 |            3 |
|   2023 |      8753 |           6 |            1 |
|   2024 |      8755 |          29 |            0 |
|   2025 |      8740 |          19 |            1 |
|   2026 |      7654 |           7 |            0 |

**rh**

|   year |   eccc_cc |   eccc_city |   om_archive |
|-------:|----------:|------------:|-------------:|
|   2015 |      8628 |         127 |            5 |
|   2016 |      8778 |           6 |            0 |
|   2017 |      8750 |          10 |            0 |
|   2018 |      8758 |           2 |            0 |
|   2019 |      8615 |         145 |            0 |
|   2020 |      8777 |           7 |            0 |
|   2021 |      8713 |          45 |            2 |
|   2022 |      8324 |         433 |            3 |
|   2023 |      8753 |           6 |            1 |
|   2024 |      8755 |          29 |            0 |
|   2025 |      8740 |          19 |            1 |
|   2026 |      7624 |          37 |            0 |

**precip_mm**

|   year |   eccc_cc |   eccc_city |   om_archive |
|-------:|----------:|------------:|-------------:|
|   2015 |      8741 |          14 |            5 |
|   2016 |      8755 |          29 |            0 |
|   2017 |      8750 |          10 |            0 |
|   2018 |      8758 |           2 |            0 |
|   2019 |      8744 |          16 |            0 |
|   2020 |      8778 |           6 |            0 |
|   2021 |      8746 |          14 |            0 |
|   2022 |      8739 |          18 |            3 |
|   2023 |      8753 |           6 |            1 |
|   2024 |      8755 |          29 |            0 |
|   2025 |      8733 |          26 |            1 |
|   2026 |      6843 |         818 |            0 |

**wind_kmh**

|   year |   eccc_cc |   om_archive |
|-------:|----------:|-------------:|
|   2015 |      8732 |           28 |
|   2016 |      8765 |           19 |
|   2017 |      8751 |            9 |
|   2018 |      8758 |            2 |
|   2019 |      8737 |           23 |
|   2020 |      8770 |           14 |
|   2021 |      8739 |           21 |
|   2022 |      8736 |           24 |
|   2023 |      8752 |            8 |
|   2024 |      8752 |           32 |
|   2025 |      8725 |           35 |
|   2026 |      7651 |           10 |

### Billy Bishop precipitation gauge failure (Sep 30, 2026 -> present)

From **2026-09-30 19:00 UTC** the Billy Bishop gauge reports a stuck value of ~604-611 mm *every hour*
(a raw 2026 total of 168,000 mm), while the downtown gauge reads normally (33 mm in October 2026). Earlier
years are plausible (590-875 mm/year). QC rule (`features/weather.py::qc_precip`): a UTC day of Billy
Bishop precipitation is rejected if any hour exceeds 100 mm, or the day's total exceeds
max(3 x downtown, downtown + 25 mm). Rejected hours fall back to the downtown station. The rule
rejects the whole failure period plus a handful of earlier days. **This affects training labels and weather
scoring only; live predictions use forecasts.** The feed-health check should alert on this kind of
failure (Milestone 4).

|   year |   precip after QC (mm) |   raw Billy Bishop (mm) |
|-------:|-----------------------:|------------------------:|
|   2015 |                    682 |                     682 |
|   2016 |                    610 |                     635 |
|   2017 |                    790 |                     790 |
|   2018 |                    875 |                     875 |
|   2019 |                    875 |                     875 |
|   2020 |                    728 |                     728 |
|   2021 |                    651 |                     651 |
|   2022 |                    593 |                     593 |
|   2023 |                    734 |                     734 |
|   2024 |                    863 |                     863 |
|   2025 |                    605 |                     591 |
|   2026 |                    754 |                  168144 |

Agreement between Billy Bishop observations and the reanalysis used for gap filling (all hours since 2015):

|   temperature MAE (C) |   wind MAE (km/h) |   annual precip ECCC (mm) |   annual precip reanalysis (mm) |
|----------------------:|------------------:|--------------------------:|--------------------------------:|
|                  1.44 |              6.21 |                    725.50 |                          862.56 |

The reanalysis is a good stand-in for the rare gaps in temperature and wind. Its precipitation is smoother
than gauge data (more drizzle hours, lower peaks), which is why it is used only as a last resort for
precipitation.

## 3. Weather forecasts

* **Archived past forecasts exist.** Open-Meteo's Previous Runs API returns, for each valid hour, the value
  predicted 24 x N hours earlier (N = 1..7). Coverage starts **2024-01-21** (GEM, GFS, ICON) and **2024-02-06** (ECMWF IFS
  0.25; radiation and RH from early March 2024). HRDPS and GEM regional are only available for N <= 1-2 from Sep 2024, and
  ECMWF IFS 9 km from 2025. That is enough for an honest backtest of the 2024-2026 holdout seasons.
  74 raw responses archived.
* **Live forecasts** (7 models, 9 days ahead, at the terminal) are archived every 3 hours from 2026-11-16:
  2 snapshots so far.
* Terminal location: 43.6405, -79.3755 (Jack Layton Ferry Terminal building). Grid points used: ECMWF 0.25 -> 43.75, -79.50
  (~14 km away, a coarse grid); GEM -> 43.66, -79.40; GFS -> 43.65, -79.38.

## 4. Events

* The City's Festivals & Events JSON feed is **~240 MB per download (~18 MB gzipped)**, ~29k event records.
  A daily archive costs ~6.5 GB/year of disk. It contains current and upcoming events, plus past dates of
  recurring events back to mid-2025. Only ~20 rows are on the Island; tens of thousands are at
  Harbourfront / Queens Quay (mostly long-running exhibitions).
* A separate **historical XML feed (Jan 2014 - Dec 2016)** exists and was archived once.
* The feed is too short to train on. Events that matter go in the hand-editable `config/events.csv`
  (schema in `config/README.md`).

## 5. Calendar

Ontario statutory holidays plus the Civic Holiday (Easter Monday and Remembrance Day are not observed), long weekends (a run of >= 3 days off containing a holiday), approximate TDSB March Break and summer break (`config/school_calendar.csv`), DST, and the peak season (June 1 - Labour Day).
