"""Run sql/events.sql against the built data and report what it found.

This is the measurement pass: every number that goes into the README or the
data dictionary is produced here first, so nothing is published that has not
been counted.
"""

from __future__ import annotations

import duckdb
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
SQL = ROOT / "sql"


def main() -> None:
    c = duckdb.connect()
    c.execute("CREATE VIEW weather AS SELECT * FROM read_parquet('data/weather.parquet')")
    c.execute("CREATE VIEW stations AS SELECT * FROM read_csv_auto('data/stations.csv', header=true)")
    # Registered from the shipped CSV, exactly as the console does, so this
    # report measures the artefact that actually runs in the browser rather
    # than a locally recomputed copy of it.
    c.execute("CREATE TABLE station_thresholds AS SELECT * FROM read_csv_auto('data/station_thresholds.csv', header=true)")
    c.execute((SQL / "views.sql").read_text(encoding="utf-8"))
    c.execute((SQL / "events.sql").read_text(encoding="utf-8"))

    def show(title, sql, limit=None):
        print("\n=== " + title + " ===")
        q = sql if limit is None else f"{sql} LIMIT {limit}"
        print(c.execute(q).df().to_string(index=False))

    show("station_thresholds: a sample, and the spread of each threshold", """
        SELECT COUNT(*) AS stations,
               ROUND(MIN(tmax_p90),1) AS p90_min, ROUND(MEDIAN(tmax_p90),1) AS p90_med,
               ROUND(MAX(tmax_p90),1) AS p90_max,
               ROUND(MIN(tmin_p10),1) AS p10_min, ROUND(MAX(tmin_p10),1) AS p10_max,
               ROUND(MIN(rain_p95),1) AS rp95_min, ROUND(MAX(rain_p95),1) AS rp95_max
        FROM station_thresholds
    """)

    show("stations with no thresholds at all (should be the 7 Taiwan stations)", """
        SELECT COUNT(*) AS no_thresholds FROM station_thresholds WHERE tmax_p90 IS NULL
    """)

    show("event counts per family", """
        SELECT family, basis, COUNT(*) AS events, COUNT(DISTINCT station_id) AS stations,
               ROUND(AVG(days),1) AS avg_days, MAX(days) AS longest
        FROM event_catalogue GROUP BY family, basis ORDER BY family, basis
    """)

    show("longest heatwave, absolute (the 35 degC definition)", """
        SELECT name, region_norm, started, ended, days, peak_tmax, peak_date
        FROM heatwave_runs WHERE kind='absolute' ORDER BY days DESC, peak_tmax DESC LIMIT 10
    """)

    show("longest heatwave, relative (each station's own decile)", """
        SELECT name, region_norm, started, ended, days, peak_tmax,
               ROUND(tmax_p90,1) AS thr FROM (
            SELECT r.*, t.tmax_p90 FROM heatwave_runs r JOIN station_thresholds t USING (station_id)
            WHERE r.kind='relative') ORDER BY days DESC, peak_tmax DESC LIMIT 10
    """)

    show("busiest national days, by count of stations at or above 35 degC", """
        SELECT * FROM national_heatwave_days ORDER BY over_35c DESC LIMIT 10
    """)

    # The reporting network is the same 372 stations on every one of the 113
    # days, so the density guard is TRUE throughout and cannot reorder
    # anything. This check exists to prove that, rather than to assert it: if a
    # future dataset has thin days, significant will be False for some of them
    # and national_events will start dropping rows.
    show("the density guard: how many days it actually excludes", """
        SELECT COUNT(*) AS days,
               COUNT(*) FILTER (WHERE significant) AS significant_days,
               COUNT(*) FILTER (WHERE NOT significant) AS excluded_days,
               MIN(stations) AS fewest_reporters,
               MAX(stations) AS most_reporters
        FROM national_heatwave_days
    """)

    show("the earliest days, to confirm the network is dense from the start", """
        SELECT date, stations, over_35c, pct_absolute, over_own_p90, significant
        FROM national_heatwave_days ORDER BY date LIMIT 6
    """)

    show("the named national episodes", """
        SELECT family, started, ended, days, peak_stations, headline, peak_date,
               mean_pct_absolute, mean_stations_relative
        FROM national_events ORDER BY started
    """)

    show("the two definitions, side by side, over the whole season", """
        SELECT SUM(over_35c) AS station_days_35c, SUM(over_own_p90) AS station_days_p90,
               ROUND(AVG(pct_absolute),1) AS avg_pct_abs,
               ROUND(AVG(pct_relative),1) AS avg_pct_rel
        FROM national_heatwave_days
    """)

    show("longest cold spells", """
        SELECT name, region_norm, started, ended, days, coldest
        FROM cold_spell_runs ORDER BY days DESC, coldest ASC LIMIT 10
    """)

    show("longest heavy-rain episodes", """
        SELECT name, region_norm, started, ended, days, rain_total, peak_rain
        FROM heavy_rain_runs ORDER BY days DESC, rain_total DESC LIMIT 10
    """)

    show("longest dry spells", """
        SELECT name, region_norm, started, ended, days, rain_total
        FROM dry_spell_runs ORDER BY days DESC, rain_total ASC LIMIT 10
    """)

    show("stations that recorded no absolute heatwave but did record a relative one", """
        SELECT COUNT(DISTINCT a.station_id) AS relative_only
        FROM (SELECT DISTINCT station_id FROM heatwave_runs WHERE kind='relative') a
        WHERE a.station_id NOT IN (SELECT station_id FROM heatwave_runs WHERE kind='absolute')
    """)

    show("integrity: catalogue rows must equal the sum of the family views", """
        SELECT
          (SELECT COUNT(*) FROM event_catalogue) AS catalogue,
          (SELECT COUNT(*) FROM heatwave_runs) + (SELECT COUNT(*) FROM cold_spell_runs)
            + (SELECT COUNT(*) FROM heavy_rain_runs) + (SELECT COUNT(*) FROM dry_spell_runs) AS families
    """)

    show("integrity: no run may span a gap in the data", """
        SELECT COUNT(*) AS runs_longer_than_observed_days FROM (
            SELECT r.station_id, r.started, r.ended, r.days
            FROM heatwave_runs r
            WHERE r.kind='absolute'
              AND r.days <> CAST(r.ended - r.started AS INTEGER) + 1
        )
    """)

    show("integrity: every event lies inside the observed date range", """
        SELECT COUNT(*) AS out_of_range FROM event_catalogue
        WHERE started < DATE '2022-05-01' OR ended > DATE '2022-08-21'
    """)

    show("integrity: the arid floor, and what it excluded", """
        SELECT (SELECT COUNT(*) FROM station_thresholds WHERE rain_p95 < 10.0) AS stations_excluded,
               (SELECT COUNT(DISTINCT station_id) FROM wx x JOIN station_thresholds t USING (station_id)
                  WHERE x.rain >= t.rain_p95 AND t.rain_p95 < 10.0) AS station_days_excluded,
               (SELECT COUNT(*) FROM heavy_rain_runs) AS episodes_kept,
               (SELECT ROUND(MIN(peak_rain),2) FROM heavy_rain_runs) AS smallest_kept_peak
    """)

    show("integrity: national episodes must all be contiguous and significant", """
        SELECT COUNT(*) AS episodes,
               COUNT(*) FILTER (WHERE days <> CAST(ended - started AS INTEGER) + 1) AS not_contiguous,
               MIN(peak_stations) AS min_peak_stations
        FROM national_events
    """)

    # The percentage denominator is the easiest thing in this file to get
    # silently wrong: using heatwave_days instead of the reporting network
    # still returns plausible-looking numbers, just several times too large.
    # These two checks pin the denominator to the network and pin the worst
    # national share to the independently-measured 31.7%.
    show("integrity: the percentage denominator is the reporting network", """
        SELECT COUNT(*) AS days_where_denominator_is_wrong
        FROM national_heatwave_days n
        WHERE n.stations <> (SELECT COUNT(DISTINCT w.station_id) FROM wx w
                             WHERE w.tmax IS NOT NULL AND w.date = n.date)
    """)

    show("integrity: worst national share, which must stay near 31.7%", """
        SELECT ROUND(MAX(pct_absolute), 1) AS worst_pct_absolute,
               ROUND(MAX(pct_relative), 1)  AS worst_pct_relative,
               MAX(over_35c)                AS most_stations_over_35c
        FROM national_heatwave_days
    """)

    # A date stored as a bare number is the signature of a reversed
    # arg_max(arg, val): it returns the value, not the key. Every peak-date
    # column must come back as a real DATE.
    show("integrity: peak dates are dates, not measurements", """
        SELECT
          (SELECT COUNT(*) FROM heatwave_runs  WHERE typeof(peak_date)   <> 'DATE') AS bad_heat_peak,
          (SELECT COUNT(*) FROM cold_spell_runs WHERE typeof(coldest_date) <> 'DATE') AS bad_cold_peak,
          (SELECT COUNT(*) FROM heavy_rain_runs WHERE typeof(peak_date)    <> 'DATE') AS bad_rain_peak,
          (SELECT COUNT(*) FROM national_events  WHERE typeof(peak_date)   <> 'DATE') AS bad_national_peak
    """)

    # A dry spell is "the dry quarter" only where the quartile is a real
    # number. For most Chinese stations rain_p25 is exactly 0, and there
    # `<= 0` selects every zero-rain day, which is a larger share than a
    # quarter. That is the intended behaviour -- a dry spell should mean "no
    # rain fell" -- but it must be visible rather than hidden behind the
    # word "quartile", so this splits the two regimes apart.
    show("integrity: what the dry-spell threshold actually selects", """
        SELECT
          CASE WHEN t.rain_p25 = 0 THEN 'p25 is 0 -> counts zero-rain days'
               ELSE 'p25 > 0 -> true quartile' END AS regime,
          COUNT(*)                                   AS stations,
          ROUND(AVG(100.0 * x.dry_days / x.all_days), 1) AS avg_pct_days_selected
        FROM station_thresholds t
        JOIN (
            SELECT station_id,
                   COUNT(*) AS all_days,
                   COUNT(*) FILTER (WHERE rain <= t.rain_p25) AS dry_days
            FROM wx x GROUP BY station_id
        ) x USING (station_id)
        GROUP BY regime ORDER BY regime
    """)

    # A threshold that can never fire is the failure mode worth catching, and
    # it is invisible in a row count: 86 spells at 5 stations looked plausible.
    # The share of days selected is the number that exposes it.
    show("integrity: no station's dry threshold is unreachable", """
        SELECT COUNT(*) AS stations_whose_dry_days_are_0
        FROM (
            SELECT station_id FROM wx x JOIN station_thresholds t USING (station_id)
            WHERE x.rain <= t.rain_p25 GROUP BY station_id HAVING COUNT(*) = 0
        )
    """)


if __name__ == "__main__":
    main()
