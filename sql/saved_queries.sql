-- Saved queries for the SQL console.
-- Each block starts with @name and @blurb; blank-line separated.
-- Runnable as a whole file:  duckdb -c ".read sql/saved_queries.sql"

-- @name The ten hottest station-days
-- @blurb The whole 42,827-row network compressed to ten rows.
SELECT date, name, region_norm, altitude, tmax, tmin, rain
FROM wx
WHERE tmax IS NOT NULL
ORDER BY tmax DESC
LIMIT 10;

-- @name The ten coldest nights
-- @blurb Note the altitudes. This is a high-altitude list, not a northern one.
SELECT date, name, region_norm, altitude, tmin, tmax
FROM wx
WHERE tmin IS NOT NULL
ORDER BY tmin
LIMIT 10;

-- @name The ten wettest single days
-- @blurb One station, one day. 241 mm at Jingdezhen on 19 June is three times
-- @blurb the next reading and about 60x the national average for that day.
SELECT date, name, region_norm, altitude, rain, tmax
FROM wx
ORDER BY rain DESC
LIMIT 10;

-- @name Driest stations of the whole summer
-- @blurb Turpan averages 39.7 degC on 4.9 mm of rain across 113 days.
SELECT station_id, name, region_norm, altitude, mean_high, rain_total, days
FROM station_extremes
ORDER BY rain_total
LIMIT 15;

-- @name Wettest stations of the whole summer
-- @blurb The far south and the mountain edge of the monsoon.
SELECT station_id, name, region_norm, altitude, mean_high, rain_total
FROM station_extremes
ORDER BY rain_total DESC
LIMIT 15;

-- @name Warmest provinces, by summer average
-- @blurb Station-weighted so a province with many lowland stations is not
-- @blurb outvoted by one with many mountain stations.
SELECT region_norm, COUNT(DISTINCT station_id) AS stations,
       ROUND(AVG(tmax), 1) AS mean_high, ROUND(AVG(tmin), 1) AS mean_low
FROM wx
WHERE tmax IS NOT NULL
GROUP BY region_norm
ORDER BY mean_high DESC
LIMIT 12;

-- @name How the country warmed through the summer
-- @blurb The national daily average, all 113 days. This is the series the
-- @blurb front page's month cards are built from; the peak is 25 July.
SELECT date, mean_high, mean_low, mean_rain,
       hottest_reading, heaviest_rain
FROM network_day
ORDER BY date;

-- @name National averages, month by month
-- @blurb Same shape as the front page's month cards.
SELECT month_num AS month, COUNT(*) AS days,
       ROUND(AVG(tmax), 1) AS mean_high, ROUND(AVG(rain), 2) AS mean_rain
FROM wx
GROUP BY month_num
ORDER BY month_num;

-- @name The hottest day each province ever saw
-- @blurb One row per province instead of one row per reading.
SELECT region_norm, name, date, tmax, tmin, rain
FROM (
  SELECT *, ROW_NUMBER() OVER (PARTITION BY region_norm ORDER BY tmax DESC) AS rk
  FROM wx WHERE tmax IS NOT NULL
)
WHERE rk = 1
ORDER BY tmax DESC;

-- @name How temperature falls with altitude
-- @blurb The page quotes -0.76 between altitude and mean summer high.
-- @blurb Here it is bucketed, which is the shape the correlation hides.
SELECT band, stations, mean_high, mean_low, mean_diurnal_range, rain_per_station
FROM altitude_bands;

-- @name The altitude correlation, recomputed
-- @blurb Confirms the published figure straight from the raw rows.
SELECT ROUND(CORR(altitude, mean_high), 2) AS corr_altitude_mean_high,
       ROUND(CORR(altitude, rain_total), 2) AS corr_altitude_rain
FROM station_extremes
WHERE rain_total IS NOT NULL;

-- @name Desert heat: biggest day-to-night swing
-- @blurb Diurnal range separates continental desert from maritime coast.
-- @blurb Turpan swings hardest; the southeast coast barely moves.
SELECT station_id, name, region_norm, altitude,
       ROUND(AVG(tmax - tmin), 1) AS mean_swing,
       ROUND(MAX(tmax - tmin), 1) AS biggest_swing
FROM wx
WHERE tmax IS NOT NULL AND tmin IS NOT NULL
GROUP BY station_id, name, region_norm, altitude
ORDER BY mean_swing DESC
LIMIT 15;

-- @name Longest run of hot days at a single station
-- @blurb Counts consecutive days at or above 35 degC, per station.
-- @blurb Xinjiang and the Turpan basin dominate; the coast has no run at all.
WITH hot AS (
  SELECT station_id, name, region_norm, date,
         CAST(date AS DATE) - CAST(ROW_NUMBER() OVER (PARTITION BY station_id ORDER BY date) AS INTEGER) AS grp
  FROM wx WHERE tmax >= 35
),
runs AS (
  SELECT station_id, name, region_norm, grp,
         COUNT(*) AS days, MIN(date) AS started, MAX(date) AS ended
  FROM hot GROUP BY station_id, name, region_norm, grp
)
SELECT name, region_norm, started, ended, days
FROM runs
ORDER BY days DESC, name
LIMIT 12;

-- @name The named national heatwave episodes
-- @blurb Days on which at least 100 stations simultaneously reached 35 degC,
-- @blurb collapsed into spells. The long one in early August is the one to look at.
SELECT started, ended, days, peak_stations, peak_date, mean_pct_absolute
FROM national_events
ORDER BY days DESC, peak_stations DESC;

-- @name The whole event catalogue, longest first
-- @blurb Heatwaves, cold spells, heavy rain and dry spells in one table.
-- @blurb `magnitude` means different things per family, so read the headline.
SELECT family, basis, name, region_norm, started, ended, days, headline
FROM event_catalogue
ORDER BY days DESC, family
LIMIT 40;

-- @name Where the two heatwave definitions disagree
-- @blurb These 146 stations never reach 35 degC at all, so an absolute rule
-- @blurb would say they had no heatwave, while their own decile says they did.
SELECT region_norm, COUNT(DISTINCT station_id) AS stations, ROUND(MAX(peak_tmax),1) AS hottest
FROM heatwave_runs
WHERE kind = 'relative'
  AND station_id NOT IN (SELECT station_id FROM heatwave_runs WHERE kind = 'absolute')
GROUP BY region_norm
ORDER BY stations DESC;

-- @name The longest relative heatwave at each station
-- @blurb Each station against itself, so a 39 degC spell in Guangzhou counts
-- @blurb alongside one in Turpan.
SELECT name, region_norm, started, ended, days, peak_tmax,
       ROUND(peak_tmax - peak_excess, 1) AS their_own_threshold
FROM heatwave_runs
WHERE kind = 'relative'
ORDER BY days DESC, peak_tmax DESC
LIMIT 25;

-- @name Every heavy-rain episode worth naming
-- @blurb Two days or more above a station's own 95th percentile of rain.
-- @blurb Desert stations are excluded: their p95 is a fraction of a millimetre.
SELECT name, region_norm, started, ended, days, rain_total, peak_rain, peak_date
FROM heavy_rain_runs
WHERE days >= 2
ORDER BY rain_total DESC;

-- @name The longest cold spells of the early summer
-- @blurb Nights at or below a station's own 10th percentile.
SELECT name, region_norm, started, ended, days, coldest, coldest_date
FROM cold_spell_runs
WHERE days >= 5
ORDER BY days DESC, coldest ASC;

-- @name Where the network was hottest, day by day
-- @blurb Only counts days with at least 100 stations reporting, otherwise a
-- @blurb May day with one hot station scores 100 percent.
SELECT date, stations, over_35c, pct_absolute, over_own_p90, pct_relative
FROM national_heatwave_days
WHERE significant
ORDER BY over_35c DESC
LIMIT 25;

-- @name Rainfall by month and province
-- @blurb June is the wet month nationwide. Swap the two grouped columns to
-- @blurb pivot the result yourself.
SELECT region_norm, month_num AS month, rain_total, stations
FROM province_month
WHERE stations >= 3
ORDER BY region_norm, month_num;

-- @name Provinces by total rain over the whole season
SELECT region_norm, stations, rain_total, mean_high
FROM province_month
WHERE month_num = (SELECT MAX(month_num) FROM province_month)
ORDER BY rain_total DESC
LIMIT 15;

-- @name Hottest readings north of 40 degrees north
-- @blurb Restricting the map cuts most of the extremes off, which is the
-- @blurb clearest sign of how continental the north is.
SELECT date, name, region_norm, latitude, tmax, tmin
FROM wx
WHERE tmax IS NOT NULL AND latitude > 40
ORDER BY tmax DESC
LIMIT 12;

-- @name Wettest days in the far south
-- @blurb Hainan, Guangdong and Guangxi, where the monsoon lands first.
SELECT date, name, region_norm, altitude, rain, tmax, tmin
FROM wx
WHERE latitude < 24 AND rain > 0
ORDER BY rain DESC
LIMIT 15;

-- @name Stations with incomplete records
-- @blurb The data-quality check. All 791 blank temperatures belong to the
-- @blurb seven Taiwan stations, so nothing else should appear here.
SELECT station_id, name, region_norm,
       COUNT(DISTINCT date) AS days,
       COUNT(DISTINCT date) - COUNT(tmax) AS days_missing_tmax
FROM wx
GROUP BY station_id, name, region_norm
HAVING COUNT(tmax) < COUNT(DISTINCT date)
ORDER BY days_missing_tmax DESC;

-- @name The four names that cover two towns each
-- @blurb Bugt, Jinghe, Ji'an and Yichun are each two stations in two provinces.
-- @blurb This is why station_id exists and why name alone is not a key.
SELECT name, COUNT(DISTINCT station_id) AS stations,
         LIST(DISTINCT region_norm) AS provinces
FROM wx
GROUP BY name
HAVING COUNT(DISTINCT station_id) > 1
ORDER BY name;
