"""Assemble the publishable index.html from the original atlas plus the console.

The original china_summer_2022.html is a single self-contained file with its
data in an inline JSON blob and no external requests. This script leaves that
file's markup, CSS and drawing code untouched, and appends:

  1. a stylesheet block for the console
  2. a <section> for the console, inserted before the page's <footer>
  3. the Parquet and stations.csv payloads, base64 encoded
  4. the views and saved queries, as script-defined constants
  5. the console script itself

The result still opens from file:// with no server. The DuckDB engine is only
fetched from a CDN if the reader presses the button, so the page above keeps
working offline and the original "no server, nothing leaves the computer" claim
stays true.
"""

from __future__ import annotations

import base64
import json
import re
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DATA = ROOT / "data"
SQL = ROOT / "sql"

SOURCE_HTML = Path.home() / "Downloads" / "china_summer_2022.html"
FALLBACK_HTML = ROOT / "source" / "china_summer_2022.html"

STYLE_MARK = "</style>"
SECTION_ANCHOR = "  <footer>"
BODY_END = "</body>"

MAX_RESULT_ROWS = 500


def find_source_html() -> Path:
    for candidate in (SOURCE_HTML, FALLBACK_HTML):
        if candidate.is_file():
            return candidate
    raise SystemExit(
        "china_summer_2022.html not found.\n"
        f"Looked in: {SOURCE_HTML}\n           and: {FALLBACK_HTML}"
    )


# ---------------------------------------------------------------------------
# Parse the saved queries into the shape the console wants.
# ---------------------------------------------------------------------------
def parse_saved_queries(text: str) -> list[dict]:
    blocks = re.split(r"(?m)^-- @name ", text)[1:]
    out = []
    for block in blocks:
        lines = block.splitlines()
        name = lines[0].strip()
        blurb_lines: list[str] = []
        body: list[str] = []
        for line in lines[1:]:
            stripped = line.strip()
            if stripped.startswith("-- @blurb"):
                blurb_lines.append(stripped[len("-- @blurb"):].strip())
            elif stripped.startswith("--"):
                continue
            else:
                body.append(line)
        out.append({
            "name": name,
            "blurb": " ".join(x for x in blurb_lines if x),
            "sql": "\n".join(body).strip(),
        })
    return out


def js_string(s: str) -> str:
    """Emit a JS string literal. json.dumps is valid JS, and keeps </script> safe."""
    return json.dumps(s, ensure_ascii=False).replace("</", "<\\/")


def indent(text: str, prefix: str) -> str:
    return "\n".join(prefix + line if line.strip() else line for line in text.splitlines())


# ---------------------------------------------------------------------------
# The markup
# ---------------------------------------------------------------------------
def build_section(meta: dict, n_queries: int) -> str:
    bands = "138 / 88 / 93 / 53"
    return f"""  <section id="console" aria-labelledby="console-title">
    <div class="c-head">
      <h2 id="console-title">Then ask the data your own questions</h2>
    </div>
    <p class="c-sub">Everything above is drawn from a fixed set of views. This part lets you write the
    query instead. It is a real <b>DuckDB</b> engine running inside this tab over the same
    {meta['rows']:,} rows, so any number you get here is the same number the map is drawing.
    Full DuckDB SQL, no server, and the data never leaves your browser.</p>

    <div class="c-boot" data-boot>
      <button type="button" class="c-btn" data-boot-btn>Start the query engine</button>
      <p>The engine is roughly 30&nbsp;MB of WebAssembly, fetched once from a public CDN the first
      time you press it. The weather data is already inside this page as
      <code>{Path('data/weather.parquet').as_posix()}</code> ({meta['parquet_kb']} KB) and is never uploaded.
      Prefer to work offline? The <code>data/</code> folder and the <code>sql/</code> queries in the
      repository run against the same file with the DuckDB command line.</p>
      <span class="c-status" data-status role="status" aria-live="polite"></span>
    </div>

    <div data-work hidden>
      <details class="c-schema">
        <summary>What you can query &mdash; 2 tables and 5 views</summary>
        <div class="c-schema-body"><div class="c-tables" data-schema-body></div></div>
      </details>

      <details class="c-saved" open>
        <summary>{n_queries} saved queries worth starting from</summary>
        <div class="c-saved-body"><div class="c-chips" data-chips></div></div>
      </details>

      <div class="c-editor">
        <textarea class="c-sql" data-sql spellcheck="false" aria-label="SQL query">SELECT date, name, region_norm, tmax, tmin, rain
FROM wx
WHERE region_norm = 'Xinjiang' AND tmax &gt; 38
ORDER BY tmax DESC;</textarea>
        <div class="c-side">
          <button type="button" class="c-btn" data-run>Run query</button>
          <button type="button" class="c-btn ghost" data-download>Download CSV</button>
          <span class="c-hint"><kbd>Ctrl</kbd> + <kbd>Enter</kbd> runs it.</span>
        </div>
      </div>

      <div class="c-out">
        <p class="c-note" data-note>Press <b>Run query</b>, or pick one of the saved queries above.</p>
        <p class="c-out-meta" data-meta></p>
        <div class="c-scroll" data-scroll></div>
      </div>
    </div>
  </section>

"""


# ---------------------------------------------------------------------------
# Assemble
# ---------------------------------------------------------------------------
def main() -> None:
    src = find_source_html()
    print(f"reading  {src}")

    html = src.read_text(encoding="utf-8")
    original_len = len(html)

    # The atlas embeds its own copy of the data. Confirm the two agree, so the
    # map and the SQL console can never quietly disagree.
    check_atlas_agrees(html)

    for marker, label in ((STYLE_MARK, "</style>"), (SECTION_ANCHOR, "<footer>"), (BODY_END, "</body>")):
        if marker not in html:
            raise SystemExit(f"could not find {label} in the source HTML; the injector needs updating")

    css = (HERE / "console.css").read_text(encoding="utf-8").rstrip()
    js = (HERE / "console.js").read_text(encoding="utf-8").rstrip()
    views_sql = (SQL / "views.sql").read_text(encoding="utf-8").strip()
    queries = parse_saved_queries((SQL / "saved_queries.sql").read_text(encoding="utf-8"))

    parquet = (DATA / "weather.parquet").read_bytes()
    stations_csv = (DATA / "stations.csv").read_text(encoding="utf-8")

    meta = json.loads((DATA / "weather_meta.json").read_text(encoding="utf-8"))
    meta["parquet_kb"] = round(len(parquet) / 1024)

    # 1. stylesheet, appended just inside the existing <style> block
    html = html.replace(
        STYLE_MARK,
        "\n/* ---- SQL console ---- */\n" + css + "\n" + STYLE_MARK,
        1,
    )

    # 2. the section, before the page's own footer
    html = html.replace(SECTION_ANCHOR, build_section(meta, len(queries)) + SECTION_ANCHOR, 1)

    # 3-5. payload, SQL, and the console script, at the end of the body
    b64 = base64.b64encode(parquet).decode("ascii")
    b64_csv = base64.b64encode(stations_csv.encode("utf-8")).decode("ascii")
    payload = f"""
<script id="console-parquet" type="application/octet-stream">{b64}</script>
<script id="console-stations" type="application/octet-stream">{b64_csv}</script>
<script>
const SCHEMA_SQL = {js_string(views_sql)};
const SAVED_QUERIES = {json.dumps(queries, ensure_ascii=False, indent=1).replace("</", "<\\/")};
</script>
<script>
{indent(js, "  ")}
</script>
"""
    html = html.replace(BODY_END, payload + BODY_END, 1)

    out = ROOT / "index.html"
    out.write_text(html, encoding="utf-8")
    kb = out.stat().st_size / 1024
    print(f"wrote    {out}  ({kb:,.0f} KB, was {original_len/1024:,.0f} KB)")

    # keep a pristine copy of the atlas for rebuilding
    source_dir = ROOT / "source"
    source_dir.mkdir(exist_ok=True)
    keep = source_dir / src.name
    if not keep.exists():
        shutil.copy2(src, keep)
        print(f"kept     {keep}")


def check_atlas_agrees(html: str) -> None:
    """Compare the atlas's own embedded numbers against the rebuilt Parquet.

    The page and the console ship two encodings of one dataset. If they ever
    diverge the page starts lying, so the build refuses to continue.
    """
    import duckdb

    m = re.search(r'<script id="wx" type="application/json">(.*?)</script>', html, re.S)
    if not m:
        raise SystemExit("could not find the atlas's inline data blob")
    atlas = json.loads(m.group(1))

    con = duckdb.connect()
    con.execute(f"CREATE VIEW weather AS SELECT * FROM read_parquet('{(DATA / 'weather.parquet').as_posix()}')")

    # The atlas relabels the four duplicated station names, so it carries 379
    # distinct labels where the workbook has 375. Names therefore cannot be
    # compared across the two. Coordinates can: every station is a unique
    # lat/long pair in both, and that is the identity the page itself splits on.
    latlon = {(round(s["la"], 4), round(s["lo"], 4)) for s in atlas["stations"]}
    par_latlon = {
        (round(r[0], 4), round(r[1], 4))
        for r in con.execute("SELECT DISTINCT latitude, longitude FROM weather").fetchall()
    }
    checks = [
        ("stations", len(atlas["stations"]), len(latlon)),
        ("days", len(atlas["dates"]), con.execute("SELECT COUNT(DISTINCT date) FROM weather").fetchone()[0]),
    ]
    only_atlas = latlon - par_latlon
    only_parquet = par_latlon - latlon
    if only_atlas or only_parquet:
        raise SystemExit(
            "station coordinates differ between the atlas and the parquet:\n"
            f"  only in atlas:   {sorted(only_atlas)[:8]}\n"
            f"  only in parquet: {sorted(only_parquet)[:8]}"
        )
    print(f"  cross-check coordinates    all {len(latlon)} stations match the parquet")
    # natX is the atlas's national daily mean high; natP its national daily rain.
    got = con.execute(
        "SELECT ROUND(AVG(tmax), 2) FROM weather GROUP BY date ORDER BY date"
    ).fetchall()
    got_rain = con.execute(
        "SELECT ROUND(AVG(rain), 2) FROM weather GROUP BY date ORDER BY date"
    ).fetchall()
    checks.append(("natX series", len(atlas["natX"]), len(got)))
    checks.append(("natP series", len(atlas["natP"]), len(got_rain)))

    bad = [c for c in checks if c[1] != c[2]]
    for label, a, b in checks:
        print(f"  cross-check {label:14s} atlas {a} vs parquet {b} {'ok' if a == b else 'MISMATCH'}")
    if bad:
        raise SystemExit("the atlas and the rebuilt parquet disagree; not writing index.html")

    drift = max(abs(a - b) for a, b in zip(atlas["natX"], [r[0] for r in got]))
    drift_rain = max(abs(a - b) for a, b in zip(atlas["natP"], [r[0] for r in got_rain]))
    print(f"  cross-check max drift    high {drift:.3f} degC, rain {drift_rain:.3f} mm")
    if drift > 0.06 or drift_rain > 0.06:
        raise SystemExit(f"national series drift too large: {drift:.3f} / {drift_rain:.3f}")


if __name__ == "__main__":
    sys.exit(main())
