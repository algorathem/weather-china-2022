"""Build the publishable data artifacts from the source workbook.

Reads ../../Downloads/weather_data_May22.xlsx and writes, into data/:
  weather.parquet        tidy long table, byte-identical in meaning to the workbook
  weather.csv            same, uncompressed, for anything that wants a plain file
  stations.csv           one row per physical station (name+coords is the identity)
  station_thresholds.csv each station's own summer percentiles, for the event views
  weather_meta.json      row counts, date span and the documented data quirks

Nothing is silently corrected. The workbook's quirks (four duplicated station
names, "T'ai-pei" spelled as a region, 791 blank temperatures) are preserved in
weather.parquet and surfaced as explicit columns in stations.csv so that the
SQL layer can either honour or correct them.

station_thresholds.csv is precomputed rather than left to the browser. It is a
pure function of the immutable Parquet, and computing a window quantile over
42,827 rows inside duckdb-wasm added roughly 35 seconds to the console's startup
on every page load. build/verify.py recomputes it from the raw data and fails
on any disagreement, so precomputing it cannot let it drift.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DATA = ROOT / "data"

SOURCE = Path.home() / "Downloads" / "weather_data_May22.xlsx"
FALLBACK = ROOT / "source" / "weather_data_May22.xlsx"

# The workbook spells one Taiwan station's province with a Wade-Giles romanisation.
# It is the only such case, and the only region value that is not a real province.
REGION_ALIASES = {"T'ai-pei": "Taiwan"}

RAW_COLUMNS = [
    "date", "name", "region", "country",
    "latitude", "longitude", "altitude", "tmax", "tmin", "rain",
]


def find_source() -> Path:
    for candidate in (SOURCE, FALLBACK):
        if candidate.is_file():
            return candidate
    raise SystemExit(
        "weather_data_May22.xlsx not found.\n"
        f"Looked in: {SOURCE}\n           and: {FALLBACK}"
    )


def station_id(row: pd.Series) -> str:
    """Stable identity for a physical station.

    Four names in the workbook each cover two towns in different provinces, so
    the name alone is not a key. Coordinates are, and they are what the atlas
    already splits on.
    """
    return f"{row['name']}@{row['latitude']:.3f},{row['longitude']:.3f}"


def main() -> None:
    src = find_source()
    DATA.mkdir(parents=True, exist_ok=True)
    print(f"reading  {src}")

    raw = pd.read_excel(src)
    missing = [c for c in RAW_COLUMNS if c not in raw.columns]
    if missing:
        raise SystemExit(f"workbook is missing expected columns: {missing}")

    df = raw[RAW_COLUMNS].copy()
    df["date"] = pd.to_datetime(df["date"])
    df["altitude"] = df["altitude"].astype("int32")
    for col in ("latitude", "longitude", "tmax", "tmin", "rain"):
        df[col] = df[col].astype("float64")

    # Sort so every artefact is byte-stable across rebuilds.
    df = df.sort_values(["latitude", "longitude", "date"], kind="stable").reset_index(drop=True)

    # ---- weather.parquet / weather.csv : the workbook, unmodified -------------
    # Parquet keeps typed columns (a real DATE, not text) so SQL can group and
    # compare dates directly. The CSV is the same data with a plain YYYY-MM-DD.
    df.assign(date=df["date"].dt.date).to_parquet(
        DATA / "weather.parquet", index=False, compression="zstd"
    )

    flat = df.copy()
    flat["date"] = flat["date"].dt.strftime("%Y-%m-%d")
    flat.to_csv(DATA / "weather.csv", index=False, lineterminator="\n")

    # ---- stations.csv : one row per physical station -------------------------
    per_station = (
        df.groupby(["name", "region", "latitude", "longitude", "altitude"], as_index=False)
          .agg(days=("date", "nunique"),
               tmax_mean=("tmax", "mean"),
               tmax_max=("tmax", "max"),
               tmin_mean=("tmin", "mean"),
               tmin_min=("tmin", "min"),
               rain_total=("rain", "sum"),
               rain_wettest_day=("rain", "max"),
               temp_days=("tmax", "count"))
    )
    per_station["station_id"] = station_id(per_station.iloc[0])  # placeholder, set below
    per_station["station_id"] = per_station.apply(station_id, axis=1)
    per_station["region_norm"] = per_station["region"].replace(REGION_ALIASES)
    per_station["region_was_alias"] = per_station["region"] != per_station["region_norm"]

    # A name shared by two towns is exactly what makes it ambiguous.
    name_counts = per_station["name"].value_counts()
    per_station["name_is_ambiguous"] = per_station["name"].map(name_counts) > 1

    per_station = per_station.sort_values("station_id", kind="stable")
    per_station.to_csv(DATA / "stations.csv", index=False, lineterminator="\n")

    # ---- station_thresholds.csv : each station's own summer percentiles ------
    # sql/events.sql reads this instead of computing the quantiles in the
    # browser. The definitions, in the order the event views need them:
    #   tmax_p90  the ~11 hottest days of that station's summer
    #   tmax_p95  the ~6 hottest
    #   tmin_p10  the ~11 coldest nights
    #   rain_p95  the ~6 wettest days
    #   rain_p25  the dry quarter of days
    # Each is computed on its own column with that column's nulls excluded, so
    # a station missing temperatures still gets a usable rain threshold.
    # The long frame carries name/lat/long but not station_id, so derive it
    # here rather than joining: that keeps the row order of `df` intact.
    joined = df.copy()
    joined["station_id"] = [
        f"{n}@{la:.3f},{lo:.3f}" for n, la, lo in
        zip(joined["name"], joined["latitude"], joined["longitude"])
    ]
    region_lookup = per_station.set_index("station_id")["region_norm"]
    joined["region_norm"] = joined["station_id"].map(region_lookup)
    thresholds = joined.groupby("station_id", as_index=False).agg(
        name=("name", "first"),
        region_norm=("region_norm", "first"),
        tmax_p90=("tmax", lambda s: s.quantile(0.90) if s.notna().any() else None),
        tmax_p95=("tmax", lambda s: s.quantile(0.95) if s.notna().any() else None),
        tmin_p10=("tmin", lambda s: s.quantile(0.10) if s.notna().any() else None),
        rain_p95=("rain", lambda s: s.quantile(0.95) if s.notna().any() else None),
        rain_p25=("rain", lambda s: s.quantile(0.25) if s.notna().any() else None),
    )
    for col in ("tmax_p90", "tmax_p95", "tmin_p10", "rain_p95", "rain_p25"):
        thresholds[col] = thresholds[col].round(4)
    thresholds = thresholds.sort_values("station_id", kind="stable")
    thresholds.to_csv(DATA / "station_thresholds.csv", index=False, lineterminator="\n")

    # ---- meta ----------------------------------------------------------------
    dates = df["date"]
    alias_regions = sorted(r for r in df["region"].unique() if r in REGION_ALIASES)
    dup_names = (
        per_station[per_station["name_is_ambiguous"]]
        .groupby("name")["region_norm"].apply(lambda s: sorted(set(s))).to_dict()
    )
    blank = df[df["tmax"].isna()].groupby(["name", "region"]).size()

    meta = {
        "built_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_file": src.name,
        "rows": int(len(df)),
        "stations_by_coords": int(per_station["station_id"].nunique()),
        "station_names": int(per_station["name"].nunique()),
        "provinces_reported": int(df["region"].nunique()),
        "date_start": dates.min().strftime("%Y-%m-%d"),
        "date_end": dates.max().strftime("%Y-%m-%d"),
        "days": int(dates.nunique()),
        "rows_per_day": int(len(df) / dates.nunique()),
        "blank_temperatures": int(df["tmax"].isna().sum()),
        "blank_temperature_stations": int(len(blank)),
        "quirks": {
            "duplicate_station_names": {k: v for k, v in sorted(dup_names.items())},
            "region_aliases_applied": REGION_ALIASES,
            "region_aliases_present": alias_regions,
            "blank_temperatures": "entirely the 7 Taiwan stations, all 113 days each",
        },
        "units": {"tmax": "degC", "tmin": "degC", "rain": "mm", "altitude": "m"},
    }
    (DATA / "weather_meta.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    # ---- report --------------------------------------------------------------
    for name in ("weather.parquet", "weather.csv", "stations.csv",
                 "station_thresholds.csv", "weather_meta.json"):
        kb = (DATA / name).stat().st_size / 1024
        print(f"  {name:24s} {kb:9.1f} KB")
    print(f"  stations {meta['stations_by_coords']} / names {meta['station_names']}"
          f" / days {meta['days']} / rows {meta['rows']}")

    # sanity: the parquet must round-trip to the same numbers
    back = pd.read_parquet(DATA / "weather.parquet")
    assert len(back) == len(df), "parquet row count changed"
    assert back["rain"].sum() == df["rain"].sum(), "parquet rain total changed"
    assert back["tmax"].max() == df["tmax"].max(), "parquet max tmax changed"
    print("  round-trip ok")

    # The precomputed thresholds must cover every station, and the 7 with no
    # temperature must be the only ones with a missing tmax_p90.
    assert len(thresholds) == meta["stations_by_coords"], "threshold row count wrong"
    blank_p90 = int(thresholds["tmax_p90"].isna().sum())
    assert blank_p90 == meta["blank_temperature_stations"], (
        f"{blank_p90} stations lack a tmax_p90, expected "
        f"{meta['blank_temperature_stations']}"
    )
    print(f"  thresholds ok, {blank_p90} stations without a temperature threshold")


if __name__ == "__main__":
    main()
