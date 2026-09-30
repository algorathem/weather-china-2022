# Data dictionary

Everything the SQL console exposes, in the order the console creates it.

## `weather` — the raw readings

42,827 rows, one per station per day. Reproduced from the workbook without changes.

| column | type | notes |
| --- | --- | --- |
| `date` | `DATE` | 2022-05-01 to 2022-08-21, 113 distinct days |
| `name` | `VARCHAR` | station name as written in the source; 375 distinct |
| `region` | `VARCHAR` | 32 distinct values, includes the alias `T'ai-pei` |
| `latitude` | `DOUBLE` | |
| `longitude` | `DOUBLE` | |
| `altitude` | `DOUBLE` | metres, 3 to 4,701 |
| `tmax` | `DOUBLE` | daily high, degC; null for 791 rows |
| `tmin` | `DOUBLE` | daily low, degC; null for the same 791 rows |
| `rain` | `DOUBLE` | daily total, mm, 0 to 241 |
| `wind` | `DOUBLE` | as supplied in the workbook |

This table has no `station_id`. Four names each refer to two distinct towns, so a
join must carry `latitude` and `longitude` as well as `name` — see `wx`.

## `stations` — one row per physical location

379 rows. Aggregated over the whole season.

| column | type | notes |
| --- | --- | --- |
| `station_id` | `VARCHAR` | `name` at `latitude, longitude`; unique |
| `name` | `VARCHAR` | |
| `region_norm` | `VARCHAR` | `T'ai-pei` folded into `Taiwan`; 15 regions |
| `latitude`, `longitude`, `altitude` | `DOUBLE` | |
| `days` | `BIGINT` | days present, 113 unless incomplete |
| `mean_high`, `mean_low` | `DOUBLE` | season averages, ignoring nulls |
| `max_high`, `min_low` | `DOUBLE` | |
| `rain_total`, `wet_days` | `DOUBLE`, `BIGINT` | |
| `hottest_date`, `coldest_date`, `wettest_date` | `DATE` | |

## `wx` — readings joined to stations

`weather` left-joined to `stations` on `name`, `latitude`, and `longitude`. All
`weather` columns, plus `station_id`, `region_norm`, and the station summaries.
This is the view to write most queries against.

## `network_day` — one row per date

| column | type | notes |
| --- | --- | --- |
| `date` | `DATE` | 113 rows |
| `stations` | `BIGINT` | stations reporting that day |
| `mean_high`, `mean_low` | `DOUBLE` | network-wide daily means |
| `mean_rain` | `DOUBLE` | total rain divided by stations, not the mean of totals |
| `hot_stations` | `BIGINT` | stations at or above 35 degC |

## `province_month` — region by month

| column | type | notes |
| --- | --- | --- |
| `region_norm` | `VARCHAR` | |
| `month_num` | `INTEGER` | 5, 6, 7, 8 |
| `stations` | `BIGINT` | distinct stations in that cell |
| `mean_high`, `mean_low` | `DOUBLE` | |
| `rain_total` | `DOUBLE` | |

## `altitude_bands` — four fixed bands

`band`, `stations`, `mean_high`, `mean_low`, ordered `1.` to `4.`. The boundaries
are under 200 m, 200 to 800 m, 800 to 2,000 m, and over 2,000 m.

| band | stations | mean high |
| --- | --- | --- |
| under 200 m | 138 | 30.1 degC |
| 200 to 800 m | 88 | 29.3 degC |
| 800 to 2,000 m | 93 | 27.6 degC |
| over 2,000 m | 53 | 20.0 degC |

## `station_extremes` — one row per station

Like `stations`, plus `mean_high` and `mean_rain` aligned per station so they can be
correlated against `altitude`. `CORR(altitude, mean_high)` is -0.76.

## Known gaps and quirks

- 791 rows have null `tmax` and `tmin`: 7 Taiwanese stations, all 113 days.
  `wind` is populated. Filter with `WHERE tmax IS NOT NULL` when averaging.
- `region` contains the Wade-Giles alias `T'ai-pei`. Only `region_norm` is fixed.
- 4 names cover 2 towns each, so 375 names for 379 stations: `Bugt`, `Jinghe`,
  `Ji'an`, `Yichun`. Grouping by `name` alone merges the pairs.
- The series stops on 21 August. Monthly figures for August cover 21 days, and
  `province_month` marks this with a `stations` count you can filter on.
- `Fezxzan` appears to be a mangled station name. It is left as found.
