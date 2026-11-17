"""Clean 15-min ferry counts -> hourly target table.

Decisions (documented in reports/data_quality.md):
* Zero-fill: the City publishes no row for an interval with no activity (there are no
  rows with both counts zero). We zero-fill every 15-min slot from the first record to
  the last published one.
* Bulk-batch repair: isolated intervals far above the physical boarding capacity
  (> BULK_ABS boardings in 15 min, > BULK_RATIO x the neighbouring median) are batch
  transactions (redemptions ~= sales in the same interval). Both redemptions and sales
  are replaced by the median of the 4 neighbouring intervals; the original is kept.
* Outage days: a calendar day with zero redemptions is treated as missing (feed outage
  or full suspension), not as a true zero, and excluded from training/evaluation.
* Intra-day outages: >= 1.5 h of zeros between 09:00 and 19:00 on a day with >= 1,500
  boardings, plus any above-capacity backlog spike within 3 h after, are flagged
  (`gap_flag`) and treated as missing. The backlog is NOT repaired as a group booking.
"""

from __future__ import annotations

import pandas as pd

from common import db

BULK_ABS = 1200     # real peak 15-min boardings top out ~1,100 (summer-weekend p99.9 = 963)
BULK_RATIO = 8.0
GAP_MIN_INTERVALS = 6   # 1.5 h of zero boardings in the middle of a busy day = outage
GAP_MIN_DAY = 1500
OPERATING_HOURS = list(range(6, 24))  # first boarding 06:30; last departures ~23:45


def clean_15min(raw: pd.DataFrame, end: pd.Timestamp | None = None) -> pd.DataFrame:
    raw = raw.set_index("ts_local").sort_index()
    last = raw.index.max() if end is None else max(raw.index.max(), end - pd.Timedelta(minutes=15))
    grid = pd.date_range(raw.index.min().floor("D"), last, freq="15min")
    df = raw[["redemptions", "sales"]].reindex(grid, fill_value=0)
    df.index.name = "ts_local"
    df["red_raw"] = df.redemptions
    df["sales_raw"] = df.sales
    r = df.redemptions.astype(float)
    nb = pd.concat([r.shift(k) for k in (-2, -1, 1, 2)], axis=1).median(axis=1)
    bulk = (r > BULK_ABS) & (r > BULK_RATIO * nb.clip(lower=10))
    # also catch above-capacity intervals whose neighbours are busy (e.g. 2018-07-08 11:30)
    bulk |= r > 2 * BULK_ABS
    s = df.sales.astype(float)
    nbs = pd.concat([s.shift(k) for k in (-2, -1, 1, 2)], axis=1).median(axis=1)
    sales_bulk = (s > BULK_ABS) & (s > BULK_RATIO * nbs.clip(lower=10))
    # intra-day outages: >= GAP_MIN_INTERVALS consecutive zero intervals between 09:00 and 19:00
    # on a day with real traffic. When a backlog spike follows within 3 h (e.g. 2026-08-22:
    # zero 11:00-15:59, then 4,889 at 16:30) the scans are real boardings uploaded late:
    # we REFILL them, spreading the backlog's excess over the outage window in proportion to
    # the same window one week earlier. Outages with no backlog stay missing (gap_flag).
    day = df.index.normalize()
    daytot = r.groupby(day).transform("sum")
    z = (r == 0) & (df.index.hour >= 9) & (df.index.hour < 19) & (daytot >= GAP_MIN_DAY)
    run = (z != z.shift()).cumsum()
    runlen = z.groupby(run).transform("size")
    gap = z & (runlen >= GAP_MIN_INTERVALS)
    df["refill_flag"] = 0
    backlog_all = pd.Series(False, index=df.index)
    week_ago = r.shift(7 * 96)
    for rid in run[gap].unique():
        idx = df.index[gap & (run == rid)]
        end = idx.max()
        win = (df.index > end) & (df.index <= end + pd.Timedelta(hours=3))
        spikes = df.index[win & bulk]
        if len(spikes) == 0:
            continue
        excess = float(sum(r[t] - nb[t] for t in spikes))
        target = idx.append(spikes)
        w = week_ago.reindex(target).fillna(0).clip(lower=1.0)
        alloc = (excess * w / w.sum()).round()
        alloc[spikes] += nb[spikes].round()        # spike intervals keep their normal level
        df.loc[target, "redemptions"] = alloc.astype(int).values
        df.loc[target, "refill_flag"] = 1
        gap[idx] = False
        backlog_all[spikes] = True
    df["gap_flag"] = gap.astype(int)
    df["bulk_flag"] = (bulk & ~backlog_all).astype(int)
    df["sales_bulk_flag"] = sales_bulk.astype(int)
    rep = bulk & ~backlog_all
    df.loc[rep, "redemptions"] = nb[rep].round().astype(int)
    df.loc[bulk | sales_bulk, "sales"] = nbs[bulk | sales_bulk].round().astype(int)
    return df


def hourly_table() -> pd.DataFrame:
    raw = db.table("ferry_15min")
    # The City publishes hourly (~:18) through the top of that hour, and writes no row for a
    # quiet interval. So anything older than (latest fetch - 80 min, floored to the hour) is
    # known even if no later row exists; otherwise last night's final hours and the overnight
    # sales stay "incomplete" until the first boat of the morning.
    fetched = pd.Timestamp(raw.fetched_at.max()).tz_convert("America/Toronto").tz_localize(None)
    known_through = (fetched - pd.Timedelta(minutes=80)).floor("h")
    q = clean_15min(raw, end=fetched.floor("15min"))
    last_ts = raw.ts_local.max()
    h = q.resample("h").agg({"redemptions": "sum", "sales": "sum", "red_raw": "sum",
                             "sales_raw": "sum", "bulk_flag": "sum", "sales_bulk_flag": "sum",
                             "gap_flag": "sum", "refill_flag": "sum"})
    # an hour is complete only when its :45 interval has been published
    h["complete"] = (((h.index + pd.Timedelta(minutes=45)) <= last_ts)
                     | ((h.index + pd.Timedelta(hours=1)) <= known_through))
    h = h.reset_index()
    h["date"] = h.ts_local.dt.normalize()
    h["hour"] = h.ts_local.dt.hour
    daily = h.groupby("date").agg(day_red=("redemptions", "sum"), day_complete=("complete", "all"))
    outage = daily.index[(daily.day_red == 0) & daily.day_complete]
    h["outage_day"] = h.date.isin(outage).astype(int)
    return h


def build() -> pd.DataFrame:
    h = hourly_table()
    with db.connect() as con:
        con.execute("CREATE OR REPLACE TABLE ferry_hourly AS SELECT * FROM h")
    return h


if __name__ == "__main__":
    h = build()
    print(h.tail(), len(h), "bulk intervals repaired:", int(h.bulk_flag.sum()),
          "outage days:", h[h.outage_day == 1].date.nunique())
