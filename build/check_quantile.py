"""Is quantile_cont() OVER (PARTITION BY ...) behaving as a window function?

The first exploration returned the same station-day count for p90, p95 and p99,
which cannot be right. This isolates the cause before anything is published.
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


show("ground truth: per-station quantile via GROUP BY, no window", """
WITH q AS (
  SELECT station_id,
         quantile_cont(tmax, 0.90) AS p90,
         quantile_cont(tmax, 0.95) AS p95,
         quantile_cont(tmax, 0.99) AS p99
  FROM wx WHERE tmax IS NOT NULL GROUP BY station_id
)
SELECT ROUND(AVG(p90),3) AS p90, ROUND(AVG(p95),3) AS p95, ROUND(AVG(p99),3) AS p99,
       ROUND(MIN(p90),3) AS p90_min, ROUND(MAX(p90),3) AS p90_max,
       ROUND(MIN(p99),3) AS p99_min, ROUND(MAX(p99),3) AS p99_max
FROM q
""")

show("the window-function form, for comparison", """
SELECT ROUND(AVG(x90),3) AS p90, ROUND(AVG(x95),3) AS p95, ROUND(AVG(x99),3) AS p99
FROM (
  SELECT DISTINCT station_id,
         quantile_cont(tmax, 0.90) OVER (PARTITION BY station_id) AS x90,
         quantile_cont(tmax, 0.95) OVER (PARTITION BY station_id) AS x95,
         quantile_cont(tmax, 0.99) OVER (PARTITION BY station_id) AS x99
  FROM wx WHERE tmax IS NOT NULL
)
""")

show("count of station-days above each threshold, GROUP BY quantile (the correct way)", """
WITH q AS (
  SELECT station_id,
         quantile_cont(tmax, 0.90) AS p90,
         quantile_cont(tmax, 0.95) AS p95,
         quantile_cont(tmax, 0.99) AS p99
  FROM wx WHERE tmax IS NOT NULL GROUP BY station_id
)
SELECT 'p90' AS which, COUNT(*) AS station_days, COUNT(DISTINCT w.station_id) AS stations
FROM wx w JOIN q USING (station_id) WHERE w.tmax >= q.p90
UNION ALL
SELECT 'p95', COUNT(*), COUNT(DISTINCT w.station_id)
FROM wx w JOIN q USING (station_id) WHERE w.tmax >= q.p95
UNION ALL
SELECT 'p99', COUNT(*), COUNT(DISTINCT w.station_id)
FROM wx w JOIN q USING (station_id) WHERE w.tmax >= q.p99
""")

show("the window form's counts, reproduced from the exploration", """
WITH p AS (
  SELECT station_id, tmax,
         quantile_cont(tmax, 0.90) OVER (PARTITION BY station_id) AS x,
         quantile_cont(tmax, 0.95) OVER (PARTITION BY station_id) AS y,
         quantile_cont(tmax, 0.99) OVER (PARTITION BY station_id) AS z
  FROM wx WHERE tmax IS NOT NULL
)
SELECT COUNT(*) FILTER (WHERE tmax >= x) AS above_x90,
       COUNT(*) FILTER (WHERE tmax >= y) AS above_y95,
       COUNT(*) FILTER (WHERE tmax >= z) AS above_z99,
       COUNT(*) AS total
FROM p
""")

show("are the three window quantiles actually distinct per station?", """
SELECT COUNT(*) AS stations,
       COUNT(*) FILTER (WHERE x = y) AS x_equals_y,
       COUNT(*) FILTER (WHERE y = z) AS y_equals_z
FROM (SELECT DISTINCT station_id,
             quantile_cont(tmax, 0.90) OVER (PARTITION BY station_id) AS x,
             quantile_cont(tmax, 0.95) OVER (PARTITION BY station_id) AS y,
             quantile_cont(tmax, 0.99) OVER (PARTITION BY station_id) AS z
      FROM wx WHERE tmax IS NOT NULL)
""")

show("sanity: 113 days, so p90 should be exceeded ~11 times, p99 ~1 time", """
SELECT COUNT(*) AS stations,
       ROUND(AVG(n90),2) AS avg_days_above_p90,
       ROUND(AVG(n95),2) AS avg_days_above_p95,
       ROUND(AVG(n99),2) AS avg_days_above_p99
FROM (
  SELECT w.station_id,
         COUNT(*) FILTER (WHERE w.tmax >= q.p90) AS n90,
         COUNT(*) FILTER (WHERE w.tmax >= q.p95) AS n95,
         COUNT(*) FILTER (WHERE w.tmax >= q.p99) AS n99
  FROM wx w JOIN (
    SELECT station_id, quantile_cont(tmax,0.90) AS p90, quantile_cont(tmax,0.95) AS p95,
           quantile_cont(tmax,0.99) AS p99
    FROM wx WHERE tmax IS NOT NULL GROUP BY station_id
  ) q USING (station_id)
  GROUP BY w.station_id
)
""")
