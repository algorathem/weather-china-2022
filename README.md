# China, summer 2022

An interactive atlas of 379 weather stations across China for 1 May to 21 August 2022,
plus a SQL console that runs entirely in your browser.

Everything is static. There is no server, no API, and no analytics. The station data
is embedded in the page as a 146 KB Parquet file and queried with DuckDB compiled to
WebAssembly, so nothing you type is sent anywhere.

## What is here

```
index.html                    the whole site: atlas + SQL console, self-contained
data/weather.parquet          42,827 daily readings, the source for the console
data/weather.csv              the same readings as plain CSV
data/stations.csv             379 station summaries with normalised region names
data/station_thresholds.csv   each station's own summer percentiles, precomputed
data/weather_meta.json        row counts, date range, and known gaps
sql/views.sql                 five derived views, created when the engine boots
sql/events.sql                nine more objects: thresholds, episodes, catalogue
sql/saved_queries.sql         twenty-six ready-made queries behind the chips
build/                        the scripts that produce data/ and index.html
source/                       the original atlas before the console was added
```

## The console

Click **Start the query engine**. That downloads about 30 MB of DuckDB-WASM from a
CDN the first time and caches it afterwards. The engine then registers the embedded
Parquet file, builds the views in `sql/views.sql` and `sql/events.sql`, and hands you
a query box.

Three tables and thirteen views are available:

| name | what it is |
| --- | --- |
| `weather` | the raw readings, exactly as the workbook had them |
| `stations` | one row per physical station, with season summaries |
| `station_thresholds` | each station's own summer percentiles, precomputed |
| `wx` | readings joined to stations, with `region_norm` fixed up |
| `network_day` | one row per date, averaged over the whole network |
| `province_month` | region by month, with station counts |
| `altitude_bands` | four altitude bands and their summer averages |
| `station_extremes` | per-station hottest, coldest, wettest, driest |
| `heatwave_days` | station-days over 35 degC or over the station's own p90 |
| `heatwave_runs` | those days collapsed into per-station episodes |
| `cold_spell_runs` | episodes at or below each station's own p10 overnight low |
| `heavy_rain_runs` | episodes at or above each station's p95 rain |
| `dry_spell_runs` | episodes at or below each station's p25 rain |
| `event_catalogue` | all four families in one table: 13,911 events |
| `national_heatwave_days` | one row per date, over the whole reporting network |
| `national_events` | the 7 episodes when 100+ stations were at or above 35 degC |

`wx` is the one to start with. It has the station columns joined onto the readings,
so most questions need only one table. `event_catalogue` is the one to start with if
you want events rather than rows.

## How an event is defined

Consecutive days are collapsed into one episode with the usual
`date - ROW_NUMBER()` trick, per station, per definition. A station that misses a
day in the middle starts a new episode rather than having the gap papered over.

| family | a day qualifies when | notes |
| --- | --- | --- |
| heatwave | `tmax >= 35`, or `tmax >= ` that station's p90 | two bases, kept separate as `kind` |
| cold spell | `tmin <= ` that station's p10 | |
| heavy rain | `rain >= ` that station's p95, where p95 is at least 10 mm | floor removes 80 arid stations |
| dry spell | `rain <= ` that station's p25 | where p25 is 0, this means no measurable rain |

Three of these caveats matter, and they are all visible in the data rather than
buried here:

- **The percentiles describe this summer, not the climate.** They are computed from
  the 113 days in this workbook, so "this station's p90" means "as hot as its own
  hottest tenth of this particular summer". A station with a mild summer gets a low
  threshold and can register a relative heatwave without anything unusual happening.
  This is a description of one season, not a climatology.
- **The relative heatwave qualifies about 10% of station-days by construction**, so
  it fires everywhere. It is a way of ranking stations against themselves, not a
  claim that a tenth of the country was in a heatwave on any given day. Use the
  absolute 35 degC definition for that, and note that 146 stations had a relative
  heatwave but never reached 35 degC at all.
- **A dry spell means different things at different stations.** For 374 of the 379
  stations rain_p25 is exactly 0, so the rule counts days with no measurable rain.
  Only 5 stations have a p25 above 0, where it is a genuine quartile. The longest
  dry spell, 55 days at Tikanlik in Xinjiang, is 55 consecutive days of zero rain.

`national_heatwave_days` divides by every station that reported a temperature that
day, not by the stations that had a heatwave. Those are very different numbers, and
using the wrong one inflates the worst national day from 31.7% to over 70%.

A few notes on the data, so a surprising result is not a bug:

- `weather` has no `station_id`. Join on `name`, `latitude`, and `longitude`; the
  four duplicated names (`Bugt`, `Jinghe`, `Ji'an`, `Yichun`) each cover two towns.
- `T'ai-pei` is spelled that way in the source. `wx.region_norm` normalises it to
  `Taiwan`; the raw `region` column is untouched.
- 791 readings have no temperature, all of them 7 Taiwanese stations across all
  113 days. `stations with incomplete records` lists them.
- The date range ends on 21 August, not 31. The network is not complete after that.

## Reproducing the build

Needs Python 3 with `duckdb`, `pandas`, `pyarrow`, and `openpyxl`.

```
python build/build_data.py    # workbook  -> data/
python build/verify.py        # 94 assertions against the built data
python build/report_events.py # what the event definitions found
python build/build_site.py    # source/   -> index.html, with a data cross-check
```

`build_site.py` fails the build if the atlas and the Parquet file disagree on any
station's coordinates or on the national daily series.

The browser suite needs Node, a copy of `duckdb` available to `npm install`, and
Edge or Chrome. It drives a real browser over the DevTools protocol, boots the
engine, runs all twenty-six saved queries, and re-derives every published figure:

```
npm install ws
node build/browser_test.js              # the local build
node build/split_test.js                # the SQL statement splitter
SITE_URL=https://example.github.io/repo node build/browser_test.js   # a deployment
```

Set `SITE_URL` to run the same suite against a published copy. It is the only way to
catch a problem that exists only after the upload, such as a payload that failed to
publish or a stale cached page. Uncaught page errors fail the run; the browser's
automatic favicon request is the only tolerated one.

## Headline numbers

All of these are re-derived by the browser test, not just asserted in the prose.

| | |
| --- | --- |
| hottest reading | Turpan, 46.6 degC on 23 June |
| coldest night | Wudaoliang, -15.7 degC on 1 May, at 4,613 m |
| heaviest daily rain | Jingdezhen, 241 mm on 19 June |
| warmest day nationwide | 25 July, mean high 31.8 degC |
| wettest day nationwide | 5 July, 10.0 mm per station |
| temperature vs altitude | correlation -0.76 |
| longest run at or above 35 degC | Turpan, 42 days |

## What the event definitions found

Counted by `build/report_events.py`, and re-derived in the browser.

| | |
| --- | --- |
| events in the catalogue | 13,911 across four families |
| national heatwave episodes | 7, on 9, 14-15, 25 July, 3-8, 12, 14-15 and 19 August |
| worst national day | 15 August, 118 of 372 stations at or above 35 degC, 31.7% |
| longest heatwave, relative | 12 days, five Guangdong stations from 22 July |
| longest cold spell | 12 days at Chengshantou, Shandong, 1-12 May |
| heaviest episode | Shaoguan, Guangdong, 505 mm over 4 days in mid-June |
| longest dry spell | 55 days at Tikanlik, Xinjiang, no measurable rain |
| stations with a relative but no absolute heatwave | 146 |
| stations with no temperature thresholds | 7, all in Taiwan |

## Source

Derived from `weather_data_May22.xlsx`. Values are reproduced as given, including
the gaps. Nothing has been filled in or interpolated.
