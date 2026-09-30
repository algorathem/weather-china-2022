"""Build the publishable data artifacts from the source workbook.

Reads ../../Downloads/weather_data_May22.xlsx and writes, into data/:
  weather.parquet   tidy long table, byte-identical in meaning to the workbook
  weather.csv       same, uncompressed, for anything that wants a plain file
  stations.csv      one row per physical station (name+coords is the identity)
  weather_meta.json row counts, date span and the documented data quirks

Nothing is silently corrected. The workbook's quirks (four duplicated station
names, "T'ai-pei" spelled as a region, 791 blank temperatures) are preserved in
weather.parquet and surfaced as explicit columns in stations.csv so that the
SQL layer can either honour or correct them.
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
    for name in ("weather.parquet", "weather.csv", "stations.csv", "weather_meta.json"):
        kb = (DATA / name).stat().st_size / 1024
        print(f"  {name:22s} {kb:9.1f} KB")
    print(f"  stations {meta['stations_by_coords']} / names {meta['station_names']}"
          f" / days {meta['days']} / rows {meta['rows']}")

    # sanity: the parquet must round-trip to the same numbers
    back = pd.read_parquet(DATA / "weather.parquet")
    assert len(back) == len(df), "parquet row count changed"
    assert back["rain"].sum() == df["rain"].sum(), "parquet rain total changed"
    assert back["tmax"].max() == df["tmax"].max(), "parquet max tmax changed"
    print("  round-trip ok")


if __name__ == "__main__":
    main()
