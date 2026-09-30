-- Derived views for the SQL console.
--
-- Two raw tables are registered before this runs:
--   weather    42,827 rows, the workbook exactly as published (tmax/tmin NULL for Taiwan)
--   stations   379 rows, one per physical station, keyed by station_id
--
-- Everything below is a VIEW: nothing is precomputed on disk, so the numbers
-- always come from the same bytes the atlas draws.

-- ---------------------------------------------------------------------------
-- wx : the table most people actually want.
-- One row per station per day, with the workbook's four ambiguous names already
-- resolved to station_id and the "T'ai-pei" spelling already folded into
-- region_norm. Raw `weather` stays untouched alongside it.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW wx AS
SELECT
    w.date,
    strftime(w.date, '%Y-%m')                     AS month,
    CAST(strftime(w.date, '%m') AS INTEGER)       AS month_num,
    s.station_id,
    w.name,
    w.region                                      AS region_raw,
    s.region_norm,
    w.country,
    w.latitude,
    w.longitude,
    w.altitude,
    w.tmax,
    w.tmin,
    w.rain,
    w.tmax - w.tmin                               AS diurnal_range
FROM weather w
JOIN stations s
  ON  w.name      = s.name
  AND w.latitude  = s.latitude
  AND w.longitude = s.longitude;

-- ---------------------------------------------------------------------------
-- province_month : how each province behaved month by month.
-- rain_total is the mean per-station seasonal total for that month.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW province_month AS
SELECT
    region_norm,
    month,
    month_num,
    COUNT(DISTINCT station_id)                    AS stations,
    COUNT(DISTINCT date)                          AS days,
    ROUND(AVG(tmax), 1)                           AS mean_high,
    ROUND(AVG(tmin), 1)                           AS mean_low,
    ROUND(AVG(rain), 1)                           AS mean_rain_per_day,
    ROUND(SUM(rain) / COUNT(DISTINCT station_id), 1) AS rain_total
FROM wx
GROUP BY region_norm, month, month_num;

-- ---------------------------------------------------------------------------
-- network_day : the whole country averaged, one row per day.
-- The warmest and wettest days quoted on the front page come from here.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW network_day AS
SELECT
    date,
    month,
    ROUND(AVG(tmax), 1)      AS mean_high,
    ROUND(AVG(tmin), 1)      AS mean_low,
    ROUND(AVG(rain), 1)      AS mean_rain,
    ROUND(MAX(tmax), 1)      AS hottest_reading,
    ROUND(MIN(tmin), 1)      AS coldest_reading,
    ROUND(MAX(rain), 1)      AS heaviest_rain
FROM wx
GROUP BY date, month;

-- ---------------------------------------------------------------------------
-- altitude_bands : the four groups the atlas uses.
-- The same cutoffs as the published page (<200, 200-800, 800-2000, >2000 m),
-- so these figures should reproduce the page's 30.1 / 29.3 / 27.6 / 20.0 degC.
-- Stations with no temperature (the 7 in Taiwan) fall outside these groups,
-- which is why the counts add up to 372 rather than 379.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW altitude_bands AS
WITH tagged AS (
    SELECT
        CASE
            WHEN altitude <  200 THEN '1. under 200 m'
            WHEN altitude <  800 THEN '2. 200 to 800 m'
            WHEN altitude < 2000 THEN '3. 800 to 2,000 m'
            ELSE                      '4. over 2,000 m'
        END AS band,
        tmax, tmin, rain, station_id, date
    FROM wx
    WHERE tmax IS NOT NULL
)
SELECT
    band,
    COUNT(DISTINCT station_id)                             AS stations,
    ROUND(AVG(tmax), 1)                                    AS mean_high,
    ROUND(AVG(tmin), 1)                                    AS mean_low,
    ROUND(AVG(tmax - tmin), 1)                             AS mean_diurnal_range,
    ROUND(SUM(rain) / COUNT(DISTINCT station_id), 0)       AS rain_per_station,
    ROUND(AVG(rain), 2)                                    AS rain_per_day
FROM tagged
GROUP BY band
ORDER BY band;

-- ---------------------------------------------------------------------------
-- station_extremes : each station's own headline numbers in one row.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW station_extremes AS
SELECT
    station_id,
    ANY_VALUE(name)        AS name,
    ANY_VALUE(region_norm) AS region_norm,
    ANY_VALUE(altitude)    AS altitude,
    ROUND(AVG(tmax), 1)    AS mean_high,
    ROUND(AVG(tmin), 1)    AS mean_low,
    ROUND(MAX(tmax), 1)    AS hottest,
    ROUND(MIN(tmin), 1)    AS coldest,
    ROUND(SUM(rain), 1)    AS rain_total,
    COUNT(*)               AS days
FROM wx
GROUP BY station_id;
