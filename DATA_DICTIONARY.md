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

## `station_thresholds` — each station's own summer percentiles

379 rows. A table rather than a view, and the only object precomputed at build time:
it is a pure function of the immutable Parquet, and computing a window quantile over
42,827 rows inside duckdb-wasm added roughly 35 seconds to every page load. Loading
the file instead brings all thirteen views up in under a second.
`build/verify.py` re-derives all five percentiles from the raw readings and fails on
any disagreement, so the file cannot drift from `weather.parquet`.

| column | type | notes |
| --- | --- | --- |
| `station_id`, `name`, `region_norm` | `VARCHAR` | |
| `tmax_p90`, `tmax_p95` | `DOUBLE` | the station's own hottest tenth and twentieth |
| `tmin_p10` | `DOUBLE` | its own coldest tenth of nights |
| `rain_p95`, `rain_p25` | `DOUBLE` | its own wettest twentieth, and driest quarter |

Each percentile excludes nulls in its own column, so the 7 stations with no
temperature get a null `tmax_p90` and `tmax_p95` while still having usable rain
thresholds.

**These are summer percentiles, not climate normals.** They are computed from the 113
days in this workbook, so `tmax_p90` means "as hot as this station's own hottest
tenth of this particular summer", not "as hot as this station normally gets". A
station with a mild summer therefore gets a low threshold and can register a relative
heatwave without anything unusual happening.

## The event views

Four families, each defined by a per-day rule and then collapsed into consecutive
episodes. Days are grouped with `date - ROW_NUMBER() OVER (PARTITION BY station_id
ORDER BY date)`, so a station missing a day in the middle starts a new episode rather
than having the gap closed for it.

| view | a day qualifies when |
| --- | --- |
| `heatwave_days`, `heatwave_runs` | `tmax >= 35` (`kind = 'absolute'`) or `tmax >= tmax_p90` (`kind = 'relative'`) |
| `cold_spell_runs` | `tmin <= tmin_p10` |
| `heavy_rain_runs` | `rain >= rain_p95`, and only where `rain_p95 >= 10.0` |
| `dry_spell_runs` | `rain <= rain_p25` |

Two floors are load-bearing rather than cosmetic:

- The `rain_p95 >= 10.0` floor keeps the 80 driest stations out of
  `heavy_rain_runs`. Without it Dunhuang, whose p95 is 0.2 mm, produces four-day
  "heavy rain episodes" totalling 0.2 mm. The smallest peak that survives the floor
  is 10.3 mm.
- The dry-spell comparison is `<=`, not `<`. For 374 of the 379 stations `rain_p25`
  is exactly 0, and no measurement is below zero, so `<` can never fire there and the
  family collapses to 86 episodes at 5 stations. `<=` makes "no measurable rain" count
  as dry, which is what the word means to a reader, and still does relative work at
  the 5 stations whose p25 is above zero. `build/verify.py` fails if any station's
  dry rule selects zero days.

### `event_catalogue` — every event in one table

13,911 rows: the union of the four families. Start here rather than guessing which
view holds what.

| column | type | notes |
| --- | --- | --- |
| `family` | `VARCHAR` | `heatwave`, `cold_spell`, `heavy_rain`, `dry_spell` |
| `basis` | `VARCHAR` | which rule, in words |
| `started`, `ended`, `days` | `DATE`, `DATE`, `BIGINT` | |
| `headline` | `VARCHAR` | pre-formatted for a human |
| `magnitude` | `DOUBLE` | degC for heat and cold, mm for rain. **Not** a cross-family score; do not sort the table by it |
| `peak_date` | `DATE` | |

### `national_heatwave_days` — one row per date

| column | type | notes |
| --- | --- | --- |
| `date` | `DATE` | 113 rows |
| `stations` | `BIGINT` | every station reporting a temperature that day, 372 throughout |
| `over_35c`, `over_own_p90` | `BIGINT` | stations over each threshold, counted once each |
| `pct_absolute`, `pct_relative` | `DOUBLE` | share of `stations` |
| `worst_excess` | `DOUBLE` | degC above the threshold, worst station |
| `significant` | `BOOLEAN` | `stations >= 100`; true on all 113 days here |

The denominator is the reporting network, taken from `wx`, not from `heatwave_days`.
Using `heatwave_days` makes each percentage "of the stations that had a heatwave",
which is a different and much larger number: it puts the worst national day above 70%
when the true share is 31.7%. `build/verify.py` checks the denominator against `wx`
for every day and pins the worst share to 31.7%.

`significant` is true for every day in this dataset, because the same 372 stations
report from 1 May to 21 August. It is kept because it costs nothing and a partial
dataset would need it; do not read anything into it for this summer.

### `national_events` — the 7 named episodes

Days on which at least 100 stations were at or above 35 degC, collapsed into
consecutive runs. A station count rather than a percentage, deliberately: a share
moves with network size, a floor of 100 stations cannot be reached by accident.
Columns: `started`, `ended`, `days`, `peak_stations`, `headline`, `peak_date`,
`mean_pct_absolute`, `mean_stations_relative`.

## Known gaps and quirks

- 791 rows have null `tmax` and `tmin`: 7 Taiwanese stations, all 113 days.
  `wind` is populated. Filter with `WHERE tmax IS NOT NULL` when averaging.
- `region` contains the Wade-Giles alias `T'ai-pei`. Only `region_norm` is fixed.
- 4 names cover 2 towns each, so 375 names for 379 stations: `Bugt`, `Jinghe`,
  `Ji'an`, `Yichun`. Grouping by `name` alone merges the pairs.
- The series stops on 21 August. Monthly figures for August cover 21 days, and
  `province_month` marks this with a `stations` count you can filter on.
- `Fezxzan` appears to be a mangled station name. It is left as found.
- The relative heatwave definition qualifies roughly 10% of station-days by
  construction, so it fires nearly everywhere. It compares stations against
  themselves; it is not evidence that a tenth of the country was in a heatwave on
  any given day. 146 stations had a relative heatwave and never once reached 35 degC.
