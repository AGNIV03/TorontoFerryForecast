# Hand-editable configuration

## `events.csv`

One row per event or disruption that can change ferry demand. Multi-day events use a
date range. The model reads this file at training *and* at inference time, so only add
an event once it is publicly known (and before the date, for future events).

| column | type | meaning |
|---|---|---|
| `date_start` | `YYYY-MM-DD` | first local date affected (inclusive) |
| `date_end` | `YYYY-MM-DD` | last local date affected (inclusive) |
| `name` | text | human-readable name |
| `zone` | `island` \| `waterfront` \| `city` | where it happens |
| `kind` | `closure` \| `capacity_limit` \| `service_disruption` \| `event` | what it is |
| `expected_effect` | `up` \| `down` \| `unknown` | expected direction on island-bound boardings |
| `source` | text | where the information came from (URL, news release, data) |
| `notes` | text | anything else |
| `series` | text, optional | name of a recurring event (e.g. `dragon-boat-festival`). All dates with the same series share their history: the model learns how busy past occurrences were (see `occ_*` features) |

Rows with `kind` = `closure` or `capacity_limit` are treated as anomaly regimes (see
`reports/data_quality.md`). Rows with `kind` = `event` become the `event_island` /
`event_waterfront` features (1 on affected dates).

The City's Festivals & Events feed is archived daily (`ingest/events.py`) and parsed
into the `city_events` table. It only has a short history (mid-2025 onwards), so
recurring events that matter for training must be added here by hand.

## `school_calendar.csv`

Approximate Toronto District School Board dates per school year: March Break and the
summer break (last day of school in June, first day in September). Update each year
when the boards publish their calendars.
