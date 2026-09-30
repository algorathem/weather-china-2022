"""Verify the SQL layer against the published figures.

Runs sql/views.sql and every saved query against a real DuckDB instance, then
re-derives the numbers the front page states. If this passes, the in-browser
DuckDB-WASM console is showing the same thing the page claims.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import duckdb

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DATA = ROOT / "data"
SQL = ROOT / "sql"

failures: list[str] = []
checks = 0


def check(label: str, got, want, tol: float = 0.051) -> None:
    global checks
    checks += 1
    if isinstance(want, (int, float)) and isinstance(got, (int, float)):
        ok = abs(float(got) - float(want)) <= tol
    else:
        ok = str(got) == str(want)
    print(f"  [{'ok' if ok else 'FAIL'}] {label:52s} got {got!r} want {want!r}")
    if not ok:
        failures.append(label)


def one(conn, sql: str):
    return conn.execute(sql).fetchone()[0]


def day(conn, sql: str) -> str:
    """Fetch a date as YYYY-MM-DD regardless of driver date type."""
    return str(one(conn, f"SELECT CAST(({sql}) AS DATE)"))


def main() -> int:
    global checks
    con = duckdb.connect()
    con.execute(f"CREATE VIEW weather AS SELECT * FROM read_parquet('{DATA / 'weather.parquet'}')")
    con.execute(f"CREATE VIEW stations AS SELECT * FROM read_csv_auto('{DATA / 'stations.csv'}')")
    # Registered as a TABLE, the same way the console registers it, so that the
    # verifier exercises the shipped file rather than recomputing the quantiles.
    con.execute(
        f"CREATE TABLE station_thresholds AS SELECT * FROM read_csv_auto('{DATA / 'station_thresholds.csv'}')"
    )

    # events.sql builds on views.sql, so it runs second. The console is given
    # these two files concatenated in the same order; keeping the verifier on
    # the same path is what stops a saved query from passing here and failing
    # in the browser.
    con.execute((SQL / "views.sql").read_text(encoding="utf-8"))
    con.execute((SQL / "events.sql").read_text(encoding="utf-8"))
    print("views.sql + events.sql applied\n")

    # ---- station_thresholds.csv must match the raw data ----------------------
    # The thresholds are precomputed so the browser does not pay for them on
    # every page load. That is only safe if they are re-derived and compared
    # here, so this block is what makes the precomputation legitimate.
    print("station_thresholds.csv re-derived from the raw readings")
    derived = con.execute("""
        SELECT station_id, ANY_VALUE(name) AS name, ANY_VALUE(region_norm) AS region_norm,
               quantile_cont(tmax, 0.90) AS tmax_p90, quantile_cont(tmax, 0.95) AS tmax_p95,
               quantile_cont(tmin, 0.10) AS tmin_p10, quantile_cont(rain, 0.95) AS rain_p95,
               quantile_cont(rain, 0.25) AS rain_p25
        FROM wx GROUP BY station_id
    """).fetchall()
    shipped = con.execute("""
        SELECT station_id, name, region_norm, tmax_p90, tmax_p95, tmin_p10, rain_p95, rain_p25
        FROM station_thresholds
    """).fetchall()
    check("  threshold row count", len(shipped), len(derived))
    by_id = {r[0]: r for r in shipped}
    worst = 0.0
    missing = 0
    for row in derived:
        got = by_id.get(row[0])
        if got is None:
            failures.append(f"station_thresholds is missing {row[0]}")
            continue
        for i, col in enumerate(("tmax_p90", "tmax_p95", "tmin_p10", "rain_p95", "rain_p25"), start=3):
            if row[i] is None and got[i] is None:
                continue
            if row[i] is None or got[i] is None:
                missing += 1
                failures.append(f"station_thresholds.{col} null mismatch for {row[0]}")
                continue
            worst = max(worst, abs(row[i] - got[i]))
    check("  max threshold drift", round(worst, 4), 0.0)
    check("  null mismatches", missing, 0)

    # ---- front-page figures --------------------------------------------------
    print("published figures")
    check("rows", one(con, "SELECT COUNT(*) FROM weather"), 42827)
    check("days", one(con, "SELECT COUNT(DISTINCT date) FROM weather"), 113)
    check("stations by coords", one(con, "SELECT COUNT(DISTINCT station_id) FROM stations"), 379)
    check("station names", one(con, "SELECT COUNT(DISTINCT name) FROM stations"), 375)
    check("blank temperatures", one(con, "SELECT COUNT(*) FROM weather WHERE tmax IS NULL"), 791)
    check("blank-temp stations", one(con, "SELECT COUNT(DISTINCT station_id) FROM wx WHERE tmax IS NULL"), 7)

    check("hottest reading", one(con, "SELECT MAX(tmax) FROM weather"), 46.6)
    check("  its station/date", con.execute("SELECT name || ' ' || CAST(date AS VARCHAR) FROM weather ORDER BY tmax DESC LIMIT 1").fetchone()[0], "Turpan 2022-06-23")
    check("coldest night", one(con, "SELECT MIN(tmin) FROM weather"), -15.7)
    check("  its station/date", con.execute("SELECT name || ' ' || CAST(date AS VARCHAR) FROM weather ORDER BY tmin LIMIT 1").fetchone()[0], "Wudaoliang 2022-05-01")
    check("heaviest single rain", one(con, "SELECT MAX(rain) FROM weather"), 241.0)
    check("  its station/date", con.execute("SELECT name || ' ' || CAST(date AS VARCHAR) FROM weather ORDER BY rain DESC LIMIT 1").fetchone()[0], "Jingdezhen 2022-06-19")

    check("Turpan mean high", one(con, "SELECT ROUND(AVG(tmax),1) FROM weather WHERE name='Turpan'"), 39.7)
    check("Turpan total rain", one(con, "SELECT ROUND(SUM(rain),1) FROM weather WHERE name='Turpan'"), 4.9)
    check("Wudaoliang altitude", one(con, "SELECT altitude FROM weather WHERE name='Wudaoliang' LIMIT 1"), 4613)

    check("warmest network day", day(con, "SELECT date FROM network_day ORDER BY mean_high DESC LIMIT 1"), "2022-07-25")
    check("  its mean high", one(con, "SELECT MAX(mean_high) FROM network_day"), 31.8)
    check("wettest network day", day(con, "SELECT date FROM network_day ORDER BY mean_rain DESC LIMIT 1"), "2022-07-05")
    check("  its mean rain", one(con, "SELECT MAX(mean_rain) FROM network_day"), 10.0)

    check("altitude correlation", one(con, "SELECT ROUND(CORR(altitude, mean_high),2) FROM station_extremes"), -0.76)

    # ---- event catalogue -----------------------------------------------------
    # Structural integrity, not a list of findings. Each of these catches a
    # specific way the views can produce confident nonsense.
    print("\nevent catalogue integrity")
    check("catalogue equals the four families",
          one(con, "SELECT COUNT(*) FROM event_catalogue"),
          one(con, "SELECT (SELECT COUNT(*) FROM heatwave_runs) + (SELECT COUNT(*) FROM cold_spell_runs)"
                   " + (SELECT COUNT(*) FROM heavy_rain_runs) + (SELECT COUNT(*) FROM dry_spell_runs)"))
    check("no run spans a gap in the data",
          one(con, "SELECT COUNT(*) FROM (SELECT 1 FROM heatwave_runs WHERE kind='absolute'"
                   " AND days <> CAST(ended - started AS INTEGER) + 1)"), 0)
    check("every event is inside the observed range",
          one(con, "SELECT COUNT(*) FROM event_catalogue"
                   " WHERE started < DATE '2022-05-01' OR ended > DATE '2022-08-21'"), 0)

    # A reversed arg_max(arg, val) returns the measurement rather than the key,
    # which types the column as DOUBLE. That is how the bug presents: the query
    # runs, the numbers look plausible, and only the type is wrong.
    for view, col in [("heatwave_runs", "peak_date"), ("cold_spell_runs", "coldest_date"),
                      ("heavy_rain_runs", "peak_date"), ("national_events", "peak_date")]:
        check(f"{view}.{col} is a DATE",
              one(con, f"SELECT DISTINCT typeof({col}) FROM {view}"), "DATE")

    # The share denominator must be every station that reported, not every
    # station that happened to have a heatwave. Using heatwave_days here still
    # returns plausible numbers, just several times too large, so it is pinned
    # to the independently measured worst day rather than merely being non-null.
    check("share denominator is the reporting network",
          one(con, "SELECT COUNT(*) FROM national_heatwave_days n WHERE n.stations <>"
                   " (SELECT COUNT(DISTINCT w.station_id) FROM wx w"
                   "  WHERE w.tmax IS NOT NULL AND w.date = n.date)"), 0)
    check("worst national share over 35 degC",
          one(con, "SELECT ROUND(MAX(pct_absolute),1) FROM national_heatwave_days"), 31.7)

    # A dry threshold that can never fire is invisible in a row count: 86 spells
    # at 5 stations looked plausible. What exposes it is how many days each
    # station's rule actually selects.
    check("no station has an unreachable dry threshold",
          one(con, "SELECT COUNT(*) FROM (SELECT station_id FROM wx x"
                   " JOIN station_thresholds t USING (station_id) WHERE x.rain <= t.rain_p25"
                   " GROUP BY station_id HAVING COUNT(*) = 0)"), 0)
    check("dry spells cover the whole network",
          one(con, "SELECT COUNT(DISTINCT station_id) FROM dry_spell_runs"), 379)

    check("longest absolute heatwave",
          con.execute("SELECT name || ' ' || CAST(days AS VARCHAR) FROM heatwave_runs"
                      " WHERE kind='absolute' ORDER BY days DESC LIMIT 1").fetchone()[0],
          "Turpan 42")
    check("national episodes",
          one(con, "SELECT COUNT(*) FROM national_events"), 7)
    check("every national episode is contiguous",
          one(con, "SELECT COUNT(*) FROM national_events"
                   " WHERE days <> CAST(ended - started AS INTEGER) + 1"), 0)
    check("national episodes all clear 100 stations",
          one(con, "SELECT MIN(peak_stations) FROM national_events"), 101)

    # ---- altitude bands, against the page's four cards -----------------------
    print("\naltitude bands")
    page = {  # band -> (stations, mean_high, mean_low, rain)
        "1. under 200 m":   (138, 30.1, 21.8, 614),
        "2. 200 to 800 m":  (88,  29.3, 18.6, 449),
        "3. 800 to 2,000 m": (93, 27.6, 16.0, 299),
        "4. over 2,000 m":  (53,  20.0,  7.8, 269),
    }
    rows = con.execute("SELECT band, stations, mean_high, mean_low, rain_per_station FROM altitude_bands").fetchall()
    check("band count", len(rows), 4)
    for band, n, hi, lo, rain in rows:
        want = page[band]
        check(f"{band} stations", n, want[0])
        check(f"{band} mean high", hi, want[1])
        check(f"{band} mean low", lo, want[2])
        check(f"{band} rain", rain, want[3], tol=1.0)

    # ---- month cards ---------------------------------------------------------
    print("\nmonth cards")
    for month, high, rain in [(5, 23.5, 3.4), (6, 27.9, 5.1), (7, 30.2, 3.9), (8, 30.8, 3.5)]:
        check(f"month {month} mean high",
              one(con, f"SELECT ROUND(AVG(tmax),1) FROM wx WHERE month_num={month}"), high)
        check(f"month {month} rain/station/day",
              one(con, f"SELECT ROUND(AVG(rain),2) FROM wx WHERE month_num={month}"), rain)

    # ---- saved queries -------------------------------------------------------
    print("\nsaved queries")
    text = (SQL / "saved_queries.sql").read_text(encoding="utf-8")
    blocks = re.split(r"(?m)^-- @name ", text)[1:]
    if not blocks:
        failures.append("no saved queries parsed")
    for block in blocks:
        lines = block.splitlines()
        name = lines[0].strip()
        body = "\n".join(l for l in lines[1:] if not l.strip().startswith("-- @blurb"))
        try:
            got = con.execute(body)
            cols = [d[0] for d in got.description]
            n = len(got.fetchall())
            checks += 1
            # An empty result is a silent failure, not a pass. Some of the
            # event queries are supposed to return nothing on a different
            # dataset, but not on this one.
            if n == 0:
                failures.append(f"saved query returned no rows: {name}")
                print(f"  [FAIL] {name:52s} returned no rows")
            else:
                print(f"  [ok] {name:52s} {n:>4} rows, {len(cols)} cols")
        except Exception as exc:  # noqa: BLE001
            checks += 1
            failures.append(f"saved query: {name}")
            print(f"  [FAIL] {name:52s} {exc}")

    # ---- meta agrees with reality -------------------------------------------
    print("\nweather_meta.json")
    meta = json.loads((DATA / "weather_meta.json").read_text(encoding="utf-8"))
    for key, sql in [
        ("rows", "SELECT COUNT(*) FROM weather"),
        ("days", "SELECT COUNT(DISTINCT date) FROM weather"),
        ("stations_by_coords", "SELECT COUNT(DISTINCT station_id) FROM stations"),
        ("station_names", "SELECT COUNT(DISTINCT name) FROM stations"),
        ("blank_temperatures", "SELECT COUNT(*) FROM weather WHERE tmax IS NULL"),
    ]:
        check(f"meta.{key}", meta[key], one(con, sql))

    print(f"\n{checks - len(failures)}/{checks} checks passed")
    if failures:
        print("\nFAILED:")
        for f in failures:
            print("  -", f)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
