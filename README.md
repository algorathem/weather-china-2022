# China, summer 2022

An interactive atlas of 379 weather stations across China for 1 May to 21 August 2022,
plus a SQL console that runs entirely in your browser.

Everything is static. There is no server, no API, and no analytics. The station data
is embedded in the page as a 146 KB Parquet file and queried with DuckDB compiled to
WebAssembly, so nothing you type is sent anywhere.

## What is here

```
index.html              the whole site: atlas + SQL console, self-contained
data/weather.parquet    42,827 daily readings, the source for the console
data/weather.csv        the same readings as plain CSV
data/stations.csv       379 station summaries with normalised region names
data/weather_meta.json  row counts, date range, and known gaps
sql/views.sql           five derived views, created when the engine boots
sql/saved_queries.sql   nineteen ready-made queries behind the chips
build/                  the scripts that produce data/ and index.html
source/                 the original atlas before the console was added
```

## The console

Click **Start the query engine**. That downloads about 30 MB of DuckDB-WASM from a
CDN the first time and caches it afterwards. The engine then registers the embedded
Parquet file, builds the views in `sql/views.sql`, and hands you a query box.

Two tables and five views are available:

| name | what it is |
| --- | --- |
| `weather` | the raw readings, exactly as the workbook had them |
| `stations` | one row per physical station, with season summaries |
| `wx` | readings joined to stations, with `region_norm` fixed up |
| `network_day` | one row per date, averaged over the whole network |
| `province_month` | region by month, with station counts |
| `altitude_bands` | four altitude bands and their summer averages |
| `station_extremes` | per-station hottest, coldest, wettest, driest |

`wx` is the one to start with. It has the station columns joined onto the readings,
so most questions need only one table.

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
python build/verify.py        # 69 assertions against the built data
python build/build_site.py    # source/   -> index.html, with a data cross-check
```

`build_site.py` fails the build if the atlas and the Parquet file disagree on any
station's coordinates or on the national daily series.

The browser suite needs Node, a copy of `duckdb` available to `npm install`, and
Edge or Chrome. It drives a real browser over the DevTools protocol, boots the
engine, runs all nineteen saved queries, and re-derives every published figure:

```
npm install ws
node build/browser_test.js
```

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

## Source

Derived from `weather_data_May22.xlsx`. Values are reproduced as given, including
the gaps. Nothing has been filled in or interpolated.
