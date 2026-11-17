# Changelog

## v1.0.0 (2026-11-17)

First public release. Live at https://ferries.agniv.site.

**Forecasts**
- Hourly island-bound boardings for the next 7 days with a calibrated likely range (LightGBM), using a
  blend of weather-forecast models chosen by measured skill at the terminal.
- Six-month outlook from season, weekday, holidays and the multi-year trend.
- Busyness levels based on ferry capacity for the schedule in force.
- Backtest on 2024-2026 with real archived forecasts: 37% less hourly error than the best simple
  baseline one day ahead, 18% less at 4-7 days; the day-ahead total is typically within 13.5%.
  See `reports/model_report.md`.

**Site and API**
- Forecast view (today by hour, Day / Week / Month chart, 7-day list) and a Deep dive (hour-by-hour
  detail, months ahead, 11 years of history, CSV downloads).
- JSON API with interactive docs at `/docs`, `sitemap.xml` and `robots.txt`.

**Data and operations**
- Append-only raw archive of every fetch (City of Toronto ferry counts, ECCC observations back to
  1957, Open-Meteo reanalysis, archived and live forecasts, City events feed).
- Docker Compose deployment: scheduler with hourly ingest, weekly retrain with a no-regression gate,
  daily accuracy log, feed-health checks and alerts. All times in Toronto time.
