-- ---------------------------------------------------------------------------
-- Event definitions.
--
-- The rows in `weather` are daily readings. The point of these views is to turn
-- them into EVENTS: episodes with a start, an end, a length, and a magnitude a
-- reader can cite.
--
-- Two threshold styles are offered, and they answer different questions:
--
--   absolute   a fixed cut-off, e.g. tmax >= 35 degC. Comparable across
--               stations, but it only means something in the hot lowlands.
--               Coastal Zhejiang never reaches 35, so under this definition it
--               registers no heatwave at all.
--
--   relative   each station against its own summer, e.g. tmax above the 90th
--               percentile of that station's 113 days. Finds the events that
--               matter locally, and is the one to use for "when did it get
--               unusually hot here".
--
-- A run of consecutive qualifying days is one event. Runs are found with the
-- standard gaps-and-islands trick: subtracting a row number from the date gives
-- a constant value inside a run and a different one across every gap.
--
-- Caveat worth keeping in mind: with 113 days, the "90th percentile" is roughly
-- the 11th hottest day of that one summer, not a climatological normal. These
-- are relative extremes within the observed period, not departures from a
-- 30-year baseline, and must not be described as the latter.
-- ---------------------------------------------------------------------------

-- ---------------------------------------------------------------------------
-- station_thresholds : each station's own summer percentiles.
--
-- This is a registered TABLE, not a view, and it is produced by
-- build/build_data.py rather than here. It is a pure function of the immutable
-- Parquet, so computing it in the browser would mean paying for it on every
-- page load: doing so pushed the console's startup from about 11 seconds to
-- about 45, because a window quantile over 42,827 rows is not free in wasm.
--
-- build/verify.py re-derives all seven columns from the raw data and fails if
-- any of them disagrees, so this table cannot drift from weather.parquet.
--
-- NULLs are excluded per column, so a station with no temperature (the 7 in
-- Taiwan) gets a NULL tmax_p90 rather than a misleading zero.
-- ---------------------------------------------------------------------------

-- (No CREATE here: station_thresholds is registered from
--  data/station_thresholds.csv, in the same way weather.parquet is.)

-- ---------------------------------------------------------------------------
-- heatwave_days : one row per station-day that qualifies, tagged with which
-- definition flagged it. 'kind' lets both be compared in a single place.
-- A station can appear twice on one date if it qualifies on both counts, which
-- is why every summary of this view counts DISTINCT station_id.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW heatwave_days AS
SELECT
    'absolute' AS kind,
    date, station_id, name, region_norm, altitude, tmax,
    tmax - 35.0  AS excess,
    35.0         AS threshold
FROM wx
WHERE tmax >= 35.0
UNION ALL
SELECT
    'relative',
    x.date, x.station_id, x.name, x.region_norm, x.altitude, x.tmax,
    x.tmax - t.tmax_p90,
    t.tmax_p90
FROM wx x
JOIN station_thresholds t USING (station_id)
WHERE x.tmax >= t.tmax_p90;

-- ---------------------------------------------------------------------------
-- heatwave_runs : consecutive qualifying days collapsed into single events.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW heatwave_runs AS
WITH islands AS (
    SELECT
        kind, station_id, name, region_norm, altitude, date, tmax, excess,
        CAST(date AS DATE)
          - CAST(ROW_NUMBER() OVER (PARTITION BY kind, station_id ORDER BY date) AS INTEGER) AS grp
    FROM heatwave_days
)
SELECT
    kind,
    station_id, name, region_norm, altitude,
    MIN(date)            AS started,
    MAX(date)            AS ended,
    COUNT(*)             AS days,
    ROUND(MAX(tmax), 1)  AS peak_tmax,
    ROUND(MAX(excess), 1) AS peak_excess,
    arg_max(date, tmax)  AS peak_date
FROM islands
GROUP BY kind, station_id, name, region_norm, altitude, grp;

-- ---------------------------------------------------------------------------
-- cold_spell_runs : nights at or below each station's own 10th percentile.
-- Relative only. There is no meaningful absolute "cold" for a country-wide
-- network: Wudaoliang at -15.7 degC and Turpan at 40 degC can share a night,
-- and a fixed cut-off would report one and ignore the other.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW cold_spell_runs AS
WITH cold AS (
    SELECT
        x.station_id, x.name, x.region_norm, x.altitude, x.date, x.tmin,
        CAST(x.date AS DATE)
          - CAST(ROW_NUMBER() OVER (PARTITION BY x.station_id ORDER BY x.date) AS INTEGER) AS grp
    FROM wx x
    JOIN station_thresholds t USING (station_id)
    WHERE x.tmin <= t.tmin_p10
)
SELECT
    station_id, name, region_norm, altitude,
    MIN(date)            AS started,
    MAX(date)            AS ended,
    COUNT(*)             AS days,
    ROUND(MIN(tmin), 1)  AS coldest,
    arg_min(date, tmin)  AS coldest_date
FROM cold
GROUP BY station_id, name, region_norm, altitude, grp;

-- ---------------------------------------------------------------------------
-- heavy_rain_runs : days at or above each station's own 95th percentile of rain.
-- Relative, because a "heavy" day in the Taklamakan is a different quantity
-- from one in coastal Fujian.
--
-- A station is only included if that percentile is itself a meaningful amount,
-- i.e. at least 10 mm. Without this floor the driest stations qualify: Dunhuang
-- has a p95 of 0.2 mm, so a "5-day heavy rain episode totalling 0.2 mm" would
-- appear in the catalogue and mean nothing. The floor costs 80 of the 379
-- stations. To see which ones, compare station_thresholds.rain_p95 to 10.0.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW heavy_rain_runs AS
WITH wet AS (
    SELECT
        x.station_id, x.name, x.region_norm, x.altitude, x.date, x.rain,
        CAST(x.date AS DATE)
          - CAST(ROW_NUMBER() OVER (PARTITION BY x.station_id ORDER BY x.date) AS INTEGER) AS grp
    FROM wx x
    JOIN station_thresholds t USING (station_id)
    WHERE t.rain_p95 >= 10.0
      AND x.rain >= t.rain_p95
)
SELECT
    station_id, name, region_norm, altitude,
    MIN(date)              AS started,
    MAX(date)              AS ended,
    COUNT(*)               AS days,
    ROUND(SUM(rain), 1)    AS rain_total,
    ROUND(MAX(rain), 1)    AS peak_rain,
    arg_max(date, rain)    AS peak_date
FROM wet
GROUP BY station_id, name, region_norm, altitude, grp;

-- ---------------------------------------------------------------------------
-- dry_spell_runs : consecutive days in each station's own dry quarter, i.e. at
-- or below its 25th percentile of rain. Kept separate from heavy_rain_runs
-- because a station can have both in one summer.
--
-- The comparison is `<=`, not `<`, and that is load-bearing. China's summer
-- rain is intermittent, so for 374 of the 379 stations rain_p25 is exactly 0.
-- With `<` those days can never qualify -- no measurement is below zero -- and
-- the family collapses to 86 spells at 5 stations, which is an artefact of the
-- threshold rather than a fact about the country. `<=` makes "zero rain" count
-- as dry for those stations, which is what a reader means by a dry spell, and
-- the quartile still does the relative work at the genuinely wet stations where
-- p25 is above zero.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW dry_spell_runs AS
WITH dry AS (
    SELECT
        x.station_id, x.name, x.region_norm, x.altitude, x.date, x.rain,
        CAST(x.date AS DATE)
          - CAST(ROW_NUMBER() OVER (PARTITION BY x.station_id ORDER BY x.date) AS INTEGER) AS grp
    FROM wx x
    JOIN station_thresholds t USING (station_id)
    WHERE x.rain <= t.rain_p25
)
SELECT
    station_id, name, region_norm, altitude,
    MIN(date)            AS started,
    MAX(date)            AS ended,
    COUNT(*)             AS days,
    ROUND(SUM(rain), 1)  AS rain_total
FROM dry
GROUP BY station_id, name, region_norm, altitude, grp;

-- ---------------------------------------------------------------------------
-- event_catalogue : every event from every family in one table, so a reader can
-- ask "show me the notable things that happened" without knowing which view
-- holds which family.
--
-- `magnitude` is a bare number whose meaning depends on `family`: degC for
-- heatwave and cold_spell, and mm for heavy_rain and dry_spell. For dry_spell it
-- is the total rain that fell during the spell, not a season total. It is NOT a
-- cross-family score, and the rows must not be sorted by it as though it were
-- one. `headline` is the pre-formatted version to show a human.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW event_catalogue AS
SELECT
    'heatwave' AS family,
    kind       AS basis,
    station_id, name, region_norm, altitude,
    started, ended, days,
    'peak ' || CAST(ROUND(peak_tmax, 1) AS VARCHAR) || ' degC' AS headline,
    peak_tmax AS magnitude,
    peak_date
FROM heatwave_runs
UNION ALL
SELECT
    'cold_spell', 'relative', station_id, name, region_norm, altitude,
    started, ended, days,
    'low of ' || CAST(ROUND(coldest, 1) AS VARCHAR) || ' degC',
    coldest, coldest_date
FROM cold_spell_runs
UNION ALL
SELECT
    'heavy_rain', 'relative', station_id, name, region_norm, altitude,
    started, ended, days,
    'peak ' || CAST(ROUND(peak_rain, 1) AS VARCHAR) || ' mm',
    peak_rain, peak_date
FROM heavy_rain_runs
UNION ALL
SELECT
    'dry_spell', 'relative', station_id, name, region_norm, altitude,
    started, ended, days,
    CAST(days AS VARCHAR) || ' dry days, ' || CAST(ROUND(rain_total, 1) AS VARCHAR) || ' mm',
    rain_total, NULL
FROM dry_spell_runs;

-- ---------------------------------------------------------------------------
-- national_heatwave_days : the calendar view. For each date, how much of the
-- network was in a heatwave.
--
-- This is the table that gives the summer a narrative. The peak days here are
-- the spells a reader would recognise as "the heatwave".
--
-- Two things to be careful with:
--
--   over_35c and over_own_p90 are counted with COUNT(DISTINCT station_id),
--   because a station qualifying on both counts contributes two rows to
--   heatwave_days and would otherwise inflate numerator and denominator alike.
--
--   `stations` is every station that reported a temperature that day, NOT every
--   station in heatwave_days. heatwave_days only contains the stations that
--   cleared a threshold, so using it as the denominator would make the share
--   "of the stations having a heatwave" and inflate it several-fold -- the
--   relative definition qualifies ~10% of stations every day by construction,
--   so its denominator is never the network. The base day grid is built from wx
--   instead, and the event counts left-joined onto it, which also keeps the
--   quiet days that had no heatwave at all. Getting this wrong is not subtle:
--   it put the worst national heatwave day at over 70% of the network when the
--   true figure is 31.7%.
--
--   `significant` marks days with at least 100 reporting stations, and
--   national_events below requires it. On this dataset it is TRUE for all 113
--   days, because the workbook reports the same 372 stations from 1 May to
--   21 August. It is kept because it costs nothing and because a partial or
--   differently-shaped dataset would need it: a share computed over a handful
--   of stations is not comparable with one computed over the full network.
--   Do not read significance into it for this summer.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW national_heatwave_days AS
WITH base AS (
    -- One row per day, with the full reporting network as the denominator.
    SELECT date, COUNT(DISTINCT station_id) AS stations
    FROM wx
    WHERE tmax IS NOT NULL
    GROUP BY date
),
flags AS (
    -- A station can qualify on both definitions, so it contributes two rows
    -- here and must be counted once per definition below.
    SELECT
        date,
        COUNT(DISTINCT CASE WHEN kind = 'absolute' THEN station_id END) AS over_35c,
        COUNT(DISTINCT CASE WHEN kind = 'relative' THEN station_id END) AS over_own_p90,
        MAX(excess)                                                    AS worst_excess
    FROM heatwave_days
    GROUP BY date
)
SELECT
    b.date,
    b.stations,
    COALESCE(f.over_35c, 0)                        AS over_35c,
    ROUND(100.0 * COALESCE(f.over_35c, 0) / b.stations, 1)      AS pct_absolute,
    COALESCE(f.over_own_p90, 0)                    AS over_own_p90,
    ROUND(100.0 * COALESCE(f.over_own_p90, 0) / b.stations, 1)  AS pct_relative,
    ROUND(f.worst_excess, 1)                       AS worst_excess,
    b.stations >= 100                              AS significant
FROM base b
LEFT JOIN flags f USING (date);

-- ---------------------------------------------------------------------------
-- national_events : consecutive days on which the country as a whole was in a
-- heatwave, collapsed the same way as the station-level runs.
--
-- The threshold is deliberately a count, not a percentage: at least 100 of the
-- reporting stations at or above 35 degC on the same day. A share would be
-- hostage to how many stations happen to report, whereas a floor of 100
-- stations cannot be reached until the network is properly populated. These are
-- the episodes worth naming, and they are the reason `significant` exists on the
-- daily view above.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW national_events AS
WITH flagged AS (
    SELECT
        date,
        over_35c,
        over_own_p90,
        stations,
        pct_absolute,
        significant,
        CAST(date AS DATE)
          - CAST(ROW_NUMBER() OVER (ORDER BY date) AS INTEGER) AS grp
    FROM national_heatwave_days
    WHERE significant AND over_35c >= 100
)
SELECT
    'national heatwave'  AS family,
    'absolute, 100+ stations at or above 35 degC' AS basis,
    MIN(date)            AS started,
    MAX(date)            AS ended,
    COUNT(*)             AS days,
    ROUND(MAX(over_35c), 0)      AS peak_stations,
    CAST(MAX(over_35c) AS VARCHAR) || ' stations at peak' AS headline,
    arg_max(date, over_35c)      AS peak_date,
    ROUND(AVG(pct_absolute), 1)  AS mean_pct_absolute,
    ROUND(AVG(over_own_p90), 1)  AS mean_stations_relative
FROM flagged
GROUP BY grp;
