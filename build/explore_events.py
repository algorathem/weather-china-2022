"""Exploration: what do the event thresholds actually look like?

Nothing here is published. This is the measurement that decides which
definitions go into sql/events.sql, so the thresholds are read off the data
rather than picked to make a nice headline.
"""

import duckdb

c = duckdb.connect()
c.execute("""
CREATE VIEW wx AS
SELECT w.date, s.station_id, w.name, s.region_norm,
       w.latitude, w.longitude, w.altitude, w.tmax, w.tmin, w.rain
FROM read_parquet('data/weather.parquet') w
JOIN read_csv_auto('data/stations.csv') s
  ON w.name = s.name AND w.latitude = s.latitude AND w.longitude = s.longitude
""")

def show(title, sql):
    print("\n=== " + title + " ===")
    print(c.execute(sql).df().to_string(index=False))


show("per-station percentiles of tmax over its own 113-day summer", """
SELECT MIN(p90) AS p90_min, MEDIAN(p90) AS p90_med, MAX(p90) AS p90_max,
       MIN(p99) AS p99_min, MEDIAN(p99) AS p99_med, MAX(p99) AS p99_max
FROM (SELECT station_id,
             quantile_cont(tmax, 0.90) AS p90,
             quantile_cont(tmax, 0.99) AS p99
      FROM wx WHERE tmax IS NOT NULL GROUP BY station_id)
""")

show("how many station-days above each absolute tmax threshold", """
SELECT threshold,
       COUNT(*) FILTER (WHERE tmax >= threshold) AS station_days,
       COUNT(DISTINCT CASE WHEN tmax >= threshold THEN station_id END) AS stations
FROM (SELECT tmax, station_id, 30 AS threshold FROM wx WHERE tmax IS NOT NULL
      UNION ALL SELECT tmax, station_id, 33 FROM wx WHERE tmax IS NOT NULL
      UNION ALL SELECT tmax, station_id, 35 FROM wx WHERE tmax IS NOT NULL
      UNION ALL SELECT tmax, station_id, 37 FROM wx WHERE tmax IS NOT NULL
      UNION ALL SELECT tmax, station_id, 40 FROM wx WHERE tmax IS NOT NULL)
GROUP BY threshold ORDER BY threshold
""")

show("how many station-days above each station-relative percentile", """
WITH p AS (SELECT station_id, tmax,
                  quantile_cont(tmax, 0.90) OVER (PARTITION BY station_id) AS x FROM wx WHERE tmax IS NOT NULL)
SELECT 90 AS pct, COUNT(*) FILTER (WHERE tmax >= x) AS station_days,
           COUNT(DISTINCT CASE WHEN tmax >= x THEN station_id END) AS stations FROM p
UNION ALL
SELECT 95, COUNT(*) FILTER (WHERE tmax >= x), COUNT(DISTINCT CASE WHEN tmax >= x THEN station_id END) FROM p
UNION ALL
SELECT 99, COUNT(*) FILTER (WHERE tmax >= x), COUNT(DISTINCT CASE WHEN tmax >= x THEN station_id END) FROM p
""")

show("run lengths at tmax >= 35C, per station", """
WITH hot AS (
  SELECT station_id, name, region_norm, date,
         CAST(date AS DATE) - CAST(ROW_NUMBER() OVER (PARTITION BY station_id ORDER BY date) AS INTEGER) AS grp
  FROM wx WHERE tmax >= 35
), runs AS (
  SELECT station_id, name, region_norm, grp, COUNT(*) AS days, MIN(date) AS started, MAX(date) AS ended
  FROM hot GROUP BY station_id, name, region_norm, grp
)
SELECT days, COUNT(*) AS runs FROM runs GROUP BY days ORDER BY days DESC
""")

show("run lengths at station-relative p90, per station", """
WITH p AS (
  SELECT station_id, name, region_norm, date, tmax,
         quantile_cont(tmax, 0.90) OVER (PARTITION BY station_id) AS thr
  FROM wx WHERE tmax IS NOT NULL
), hot AS (
  SELECT station_id, name, region_norm, date,
         CAST(date AS DATE) - CAST(ROW_NUMBER() OVER (PARTITION BY station_id ORDER BY date) AS INTEGER) AS grp
  FROM p WHERE tmax >= thr
), runs AS (
  SELECT station_id, name, region_norm, grp, COUNT(*) AS days, MIN(date) AS started, MAX(date) AS ended
  FROM hot GROUP BY station_id, name, region_norm, grp
)
SELECT days, COUNT(*) AS runs FROM runs GROUP BY days ORDER BY days DESC
""")

show("network-wide heatwave days: share of stations over 35C per day", """
SELECT date, ROUND(100.0 * COUNT(*) FILTER (WHERE tmax >= 35) / COUNT(*), 1) AS pct_stations
FROM wx WHERE tmax IS NOT NULL
GROUP BY date HAVING COUNT(*) FILTER (WHERE tmax >= 35) > 0 ORDER BY pct_stations DESC LIMIT 15
""")

show("cold nights: run lengths at tmin <= station-relative p10", """
WITH p AS (
  SELECT station_id, name, region_norm, date, tmin,
         quantile_cont(tmin, 0.10) OVER (PARTITION BY station_id) AS thr
  FROM wx WHERE tmin IS NOT NULL
), cold AS (
  SELECT station_id, name, region_norm, date,
         CAST(date AS DATE) - CAST(ROW_NUMBER() OVER (PARTITION BY station_id ORDER BY date) AS INTEGER) AS grp
  FROM p WHERE tmin <= thr
), runs AS (
  SELECT station_id, name, region_norm, grp, COUNT(*) AS days, MIN(date) AS started, MAX(date) AS ended
  FROM cold GROUP BY station_id, name, region_norm, grp
)
SELECT days, COUNT(*) AS runs FROM runs GROUP BY days ORDER BY days DESC
""")

show("heavy rain: days over station-relative p95 of rain", """
WITH p AS (
  SELECT station_id, date, rain,
         quantile_cont(rain, 0.95) OVER (PARTITION BY station_id) AS thr
  FROM wx WHERE rain IS NOT NULL
)
SELECT COUNT(*) AS station_days, COUNT(DISTINCT station_id) AS stations,
       MAX(rain) AS heaviest, MEDIAN(thr) AS median_threshold
FROM p WHERE rain >= thr
""")

show("rain run lengths at station-relative p95", """
WITH p AS (
  SELECT station_id, name, region_norm, date, rain,
         quantile_cont(rain, 0.95) OVER (PARTITION BY station_id) AS thr
  FROM wx WHERE rain IS NOT NULL
), wet AS (
  SELECT station_id, name, region_norm, date,
         CAST(date AS DATE) - CAST(ROW_NUMBER() OVER (PARTITION BY station_id ORDER BY date) AS INTEGER) AS grp
  FROM p WHERE rain >= thr
), runs AS (
  SELECT station_id, name, region_norm, grp, COUNT(*) AS days, MIN(date) AS started, MAX(date) AS ended
  FROM wet GROUP BY station_id, name, region_norm, grp
)
SELECT days, COUNT(*) AS runs FROM runs GROUP BY days ORDER BY days DESC
""")

show("drought: longest run of days with rain below station-relative p25", """
WITH p AS (
  SELECT station_id, name, region_norm, date, rain,
         quantile_cont(rain, 0.25) OVER (PARTITION BY station_id) AS thr
  FROM wx WHERE rain IS NOT NULL
), dry AS (
  SELECT station_id, name, region_norm, date,
         CAST(date AS DATE) - CAST(ROW_NUMBER() OVER (PARTITION BY station_id ORDER BY date) AS INTEGER) AS grp
  FROM p WHERE rain < thr
), runs AS (
  SELECT station_id, name, region_norm, grp, COUNT(*) AS days, MIN(date) AS started, MAX(date) AS ended
  FROM dry GROUP BY station_id, name, region_norm, grp
)
SELECT days, COUNT(*) AS runs FROM runs GROUP BY days ORDER BY days DESC
""")
