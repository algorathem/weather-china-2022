/* ==========================================================================
   SQL console for the weather atlas.

   Design constraints that shaped this:
   - The page is a single file that must keep working from file:// with no
     network, so the engine is only fetched when the reader asks for it.
   - The data is embedded as base64 Parquet, not fetched, so there is no
     server dependency and the numbers can never drift from the workbook.
   - DuckDB-WASM runs in a Web Worker, so the UI thread stays responsive.
   ========================================================================== */

(function () {
  "use strict";

  var CDN = "https://cdn.jsdelivr.net/npm/@duckdb/duckdb-wasm@1.32.0/+esm";
  var MAX_ROWS = 500;          // rows painted into the table
  var CHUNK = 200;             // rows added per animation frame

  var root = document.getElementById("console");
  if (!root) return;

  var el = {
    boot: root.querySelector("[data-boot]"),
    bootBtn: root.querySelector("[data-boot-btn]"),
    status: root.querySelector("[data-status]"),
    work: root.querySelector("[data-work]"),
    sql: root.querySelector("[data-sql]"),
    run: root.querySelector("[data-run]"),
    download: root.querySelector("[data-download]"),
    note: root.querySelector("[data-note]"),
    meta: root.querySelector("[data-meta]"),
    scroll: root.querySelector("[data-scroll]"),
    schema: root.querySelector("[data-schema]"),
    schemaBody: root.querySelector("[data-schema-body]"),
    chips: root.querySelector("[data-chips]")
  };

  var db = null;
  var conn = null;
  var lastResult = null;   // { columns, rows } for CSV export
  var painting = false;

  // ---- base64 -> bytes ---------------------------------------------------
  // The Parquet payload is embedded so the page needs no fetch. atob gives a
  // binary string; building the view directly avoids a second copy.
  function b64ToBytes(b64) {
    var clean = b64.replace(/\s+/g, "");
    var bin = atob(clean);
    var n = bin.length;
    var bytes = new Uint8Array(n);
    for (var i = 0; i < n; i++) bytes[i] = bin.charCodeAt(i);
    return bytes;
  }

  function setStatus(text, isError) {
    el.status.textContent = text;
    el.status.classList.toggle("err", !!isError);
  }

  function showError(err) {
    var msg = (err && (err.message || err.toString())) || "Something went wrong.";
    el.note.className = "c-note err";
    el.note.textContent = msg;
    el.meta.textContent = "";
    el.scroll.innerHTML = "";
  }

  // Exposed so build/browser_test.js can assert against the very same engine
  // the UI is using, instead of standing up a second one.
  function testHandle() { return { getDb: function () { return db; }, getConn: function () { return conn; }, boot: boot }; }

  // ---- engine boot --------------------------------------------------------
  function boot() {
    if (db) return;
    el.bootBtn.disabled = true;
    setStatus("Downloading the query engine, about 30 MB. This happens once.");

    // The payload script tags live outside the section, so look them up on the
    // document rather than within root.
    var payload = document.getElementById("console-parquet");
    var stations = document.getElementById("console-stations");
    var thresholds = document.getElementById("console-thresholds");
    if (!payload || !stations || !thresholds) {
      el.bootBtn.disabled = false;
      setStatus("The embedded data payload is missing from this page.", true);
      return;
    }
    var bytes = b64ToBytes(payload.textContent);
    var csv = new TextDecoder().decode(b64ToBytes(stations.textContent));
    var thrCsv = new TextDecoder().decode(b64ToBytes(thresholds.textContent));

    setStatus("Loading the DuckDB module.");
    import(CDN)
      .then(function (duckdb) {
        setStatus("Starting DuckDB in a worker.");
        // selectBundle is async in the published builds; awaiting it is what
        // yields the real worker URL rather than undefined.
        return duckdb.selectBundle(duckdb.getJsDelivrBundles()).then(function (bundle) {
          if (!bundle || !bundle.mainWorker) throw new Error("could not resolve the DuckDB worker bundle");
          // A blob URL avoids the cross-origin worker script restriction.
          var workerUrl = URL.createObjectURL(
            new Blob(["importScripts('" + bundle.mainWorker + "');"], { type: "text/javascript" })
          );
          var worker = new Worker(workerUrl);
          db = new duckdb.AsyncDuckDB(new duckdb.VoidLogger(), worker);
          return db
            .instantiate(bundle.mainModule, bundle.pthreadWorker)
            // NOTE: open() must be given a config object. In duckdb-wasm 1.32.0,
            // calling open() with no argument leaves the internal config
            // undefined and the first query dies with
            // "Cannot read properties of undefined (reading 'path')".
            .then(function () { return db.open({}); });
        });
      })
      .then(function () {
        setStatus("Registering " + Math.round(bytes.length / 1024) + " KB of station data.");
        return db.registerFileBuffer("weather.parquet", bytes).then(function () {
          // The CSVs are embedded as text, so encode them the same way.
          var enc = new TextEncoder().encode(csv);
          return db.registerFileBuffer("stations.csv", enc).then(function () {
            return db.registerFileBuffer("station_thresholds.csv", new TextEncoder().encode(thrCsv));
          });
        });
      })
      .then(function () {
        return db.connect();
      })
      .then(function (c) {
        conn = c;
        return conn.query(
          "CREATE OR REPLACE VIEW weather AS SELECT * FROM read_parquet('weather.parquet');"
        ).then(function () {
          return conn.query("CREATE OR REPLACE VIEW stations AS SELECT * FROM read_csv_auto('stations.csv');");
        }).then(function () {
          // A TABLE, not a VIEW: these percentiles are precomputed by
          // build/build_data.py. Computing a window quantile over 42,827 rows
          // inside wasm added ~35s to every page load, and the values are a
          // pure function of the immutable Parquet anyway. build/verify.py
          // re-derives all seven columns and fails on any disagreement.
          return conn.query("CREATE OR REPLACE TABLE station_thresholds AS SELECT * FROM read_csv_auto('station_thresholds.csv');");
        });
      })
      .then(function () {
        setStatus("Building the derived views.");
        return runAll(SCHEMA_SQL);
      })
      .then(function () {
        return loadSchema();
      })
      .then(function () {
        buildChips();
        el.boot.hidden = true;
        el.work.hidden = false;
        setStatus("Ready. DuckDB is running entirely in this browser tab.");
        el.sql.focus();
      })
      .catch(function (err) {
        el.bootBtn.disabled = false;
        setStatus("Could not start the engine: " + (err && err.message ? err.message : err), true);
        if (err && err.stack) console.error(err);
      });
  }

  // Multi-statement SQL has to be split, since query() takes one statement.
  // Split a multi-statement script on semicolons that actually end a statement.
  //
  // A plain sql.split(";") is wrong in a way that fails confusingly: a
  // semicolon inside a "--" comment or inside a string literal cuts a
  // statement in half, and the leftover prose is then handed to the parser,
  // which reports something like 'syntax error at or near "to"' with no hint
  // that a comment caused it. That is not hypothetical -- it is how the event
  // views failed to boot once. So this tracks the four contexts where a
  // semicolon is not a separator.
  function splitStatements(sql) {
    var out = [], buf = "", i = 0, n = sql.length;
    var inLine = false, inBlock = false, inStr = false, inIdent = false;
    while (i < n) {
      var ch = sql[i], next = sql[i + 1];
      if (inLine) {
        if (ch === "\n") { inLine = false; buf += ch; }
        else { buf += ch; }
        i++;
        continue;
      }
      if (inBlock) {
        if (ch === "*" && next === "/") { inBlock = false; buf += ch + next; i += 2; continue; }
        buf += ch; i++; continue;
      }
      if (inStr) {
        buf += ch;
        // '' inside a string is an escaped quote, not a terminator.
        if (ch === "'" && next === "'") { buf += next; i += 2; continue; }
        if (ch === "'") inStr = false;
        i++; continue;
      }
      if (inIdent) {
        buf += ch;
        if (ch === '"') inIdent = false;
        i++; continue;
      }
      if (ch === "-" && next === "-") { inLine = true; buf += ch + next; i += 2; continue; }
      if (ch === "/" && next === "*") { inBlock = true; buf += ch + next; i += 2; continue; }
      if (ch === "'") { inStr = true; buf += ch; i++; continue; }
      if (ch === '"') { inIdent = true; buf += ch; i++; continue; }
      if (ch === ";") { out.push(buf); buf = ""; i++; continue; }
      buf += ch; i++;
    }
    out.push(buf);
    return out
      .map(function (s) { return s.trim(); })
      .filter(function (s) {
        if (s.length === 0) return false;
        // Drop comment-only chunks, e.g. a trailing comment with no newline
        // after the last semicolon. DuckDB tolerates them, but there is no
        // reason to send one.
        return s.replace(/--[^\n]*/g, "").replace(/\/\*[\s\S]*?\*\//g, "").trim().length > 0;
      });
  }

  function runAll(sql) {
    return splitStatements(sql)
      .reduce(function (chain, stmt) {
        return chain.then(function () { return conn.query(stmt); });
      }, Promise.resolve());
  }

  // ---- schema browser -----------------------------------------------------
  function loadSchema() {
    var doc = [
      { name: "weather", kind: "table", note: "42,827 rows. The workbook exactly as published.",
        cols: [["date", "DATE"], ["name", "VARCHAR"], ["region", "VARCHAR"], ["country", "VARCHAR"],
               ["latitude", "DOUBLE"], ["longitude", "DOUBLE"], ["altitude", "INTEGER"],
               ["tmax", "DOUBLE"], ["tmin", "DOUBLE"], ["rain", "DOUBLE"]] },
      { name: "stations", kind: "table", note: "379 rows. One per physical station.",
        cols: [["station_id", "VARCHAR"], ["name", "VARCHAR"], ["region_norm", "VARCHAR"],
               ["altitude", "INTEGER"], ["tmax_mean", "DOUBLE"], ["tmin_mean", "DOUBLE"],
               ["rain_total", "DOUBLE"], ["days", "INTEGER"], ["temp_days", "INTEGER"]] },
      { name: "wx", kind: "view", note: "The main table: station x day, with the ambiguous names already split.",
        cols: [["date", "DATE"], ["month", "VARCHAR"], ["station_id", "VARCHAR"], ["name", "VARCHAR"],
               ["region_norm", "VARCHAR"], ["altitude", "INTEGER"], ["tmax", "DOUBLE"],
               ["tmin", "DOUBLE"], ["rain", "DOUBLE"], ["diurnal_range", "DOUBLE"]] },
      { name: "network_day", kind: "view", note: "113 rows. The country averaged, one row per day.",
        cols: [["date", "DATE"], ["mean_high", "DOUBLE"], ["mean_low", "DOUBLE"],
               ["mean_rain", "DOUBLE"], ["hottest_reading", "DOUBLE"], ["heaviest_rain", "DOUBLE"]] },
      { name: "province_month", kind: "view", note: "Province by month averages.",
        cols: [["region_norm", "VARCHAR"], ["month", "VARCHAR"], ["stations", "INTEGER"],
               ["days", "INTEGER"], ["mean_high", "DOUBLE"], ["mean_low", "DOUBLE"],
               ["rain_total", "DOUBLE"]] },
      { name: "altitude_bands", kind: "view", note: "The four altitude groups used on the page above.",
        cols: [["band", "VARCHAR"], ["stations", "INTEGER"], ["mean_high", "DOUBLE"],
               ["mean_low", "DOUBLE"], ["mean_diurnal_range", "DOUBLE"], ["rain_per_station", "DOUBLE"]] },
      { name: "station_extremes", kind: "view", note: "One row per station, its own headline numbers.",
        cols: [["station_id", "VARCHAR"], ["name", "VARCHAR"], ["region_norm", "VARCHAR"],
               ["altitude", "INTEGER"], ["mean_high", "DOUBLE"], ["hottest", "DOUBLE"],
               ["coldest", "DOUBLE"], ["rain_total", "DOUBLE"], ["days", "INTEGER"]] }
    ];

    el.schemaBody.innerHTML = "";
    doc.forEach(function (t) {
      var box = document.createElement("div");
      box.className = "c-table";

      var h = document.createElement("h4");
      h.textContent = t.name;
      var kind = document.createElement("div");
      kind.className = "c-kind";
      kind.textContent = t.kind;
      var note = document.createElement("p");
      note.className = "c-hint";
      note.style.margin = "6px 0 0";
      note.textContent = t.note;

      var ul = document.createElement("ul");
      t.cols.forEach(function (pair) {
        var li = document.createElement("li");
        var b = document.createElement("b");
        b.textContent = pair[0];
        var s = document.createElement("span");
        s.textContent = "  " + pair[1];
        li.appendChild(b);
        li.appendChild(s);
        ul.appendChild(li);
      });

      box.appendChild(h);
      box.appendChild(kind);
      box.appendChild(note);
      box.appendChild(ul);
      el.schemaBody.appendChild(box);
    });
  }

  // ---- saved query chips --------------------------------------------------
  function buildChips() {
    el.chips.innerHTML = "";
    (typeof SAVED_QUERIES !== "undefined" ? SAVED_QUERIES : []).forEach(function (q) {
      var b = document.createElement("button");
      b.type = "button";
      b.className = "c-chip";
      b.textContent = q.name;
      b.title = q.blurb || "";
      b.addEventListener("click", function () {
        el.sql.value = q.sql;
        el.note.className = "c-note";
        el.note.textContent = q.blurb || "";
        run();
      });
      el.chips.appendChild(b);
    });
  }

  // ---- running a query ----------------------------------------------------
  function run() {
    if (!conn) return;
    var sql = el.sql.value.trim();
    if (!sql) return;
    el.run.disabled = true;
    setStatus("Running.");
    var t0 = performance.now();

    conn.query(sql)
      .then(function (table) {
        var ms = Math.round(performance.now() - t0);
        show(table, ms, sql);
        setStatus("Ready.");
      })
      .catch(function (err) {
        showError(err);
        setStatus("That query did not run.", true);
      })
      .finally(function () {
        el.run.disabled = false;
      });
  }

  // Arrow hands back a schema plus rows, but the JS types are not what the
  // DuckDB types suggest. Measured on 1.32.0:
  //   DATE            -> a Number of milliseconds since the epoch, not a Date
  //   BIGINT          -> a BigInt
  //   DECIMAL         -> a Number, already scaled
  //   LIST/STRUCT     -> a JS Array / plain object
  // So the declared column type is what tells us how to read each value.
  var DAY_MS = 86400000;

  function normalise(v, type) {
    if (v === null || v === undefined) return null;

    // Date32<DAY> arrives as days-since-epoch scaled to ms in some builds and
    // as a plain epoch-ms number in others. Treat a bare number as ms.
    if (type && /^Date(32)?</.test(type) && typeof v === "number") {
      var ms = v > 1e11 ? v : v * DAY_MS;
      return new Date(ms).toISOString().slice(0, 10);
    }
    if (type && /^Timestamp/.test(type) && typeof v === "number") {
      return new Date(v).toISOString().replace("T", " ").slice(0, 19);
    }
    if (typeof v === "bigint") return Number(v);
    if (v instanceof Date) return v.toISOString().slice(0, 10);
    if (Array.isArray(v)) return v.map(function (x) { return x === null ? null : String(x); }).join(", ");
    if (typeof v === "object") {
      try { return JSON.stringify(v); } catch (e) { return String(v); }
    }
    return v;
  }

  function isNumericColumn(values) {
    var seen = 0;
    for (var i = 0; i < values.length; i++) {
      var v = values[i];
      if (v === null) continue;
      if (typeof v !== "number") return false;
      seen++;
    }
    return seen > 0;
  }

  function show(table, ms, sql) {
    var fields = table.schema.fields;
    var columns = fields.map(function (f) {
      return { name: f.name, type: String(f.type) };
    });
    var all = table.toArray().map(function (row) {
      var j = row.toJSON();
      return columns.map(function (c) { return normalise(j[c.name], c.type); });
    });

    lastResult = { columns: columns, rows: all, sql: sql };

    el.note.className = "c-note";
    el.note.textContent = all.length + " row" + (all.length === 1 ? "" : "s") + " in " + ms + " ms.";

    if (!all.length) {
      el.meta.textContent = "";
      el.scroll.innerHTML = '<p class="c-more">No rows.</p>';
      return;
    }

    var numeric = columns.map(function (_, i) {
      return isNumericColumn(all.map(function (r) { return r[i]; }));
    });

    el.meta.innerHTML = "";
    var b = document.createElement("b");
    b.textContent = all.length.toLocaleString();
    el.meta.appendChild(b);
    el.meta.appendChild(
      document.createTextNode(" rows · " + columns.length + " columns · " + ms + " ms")
    );

    el.scroll.innerHTML = "";
    var table2 = document.createElement("table");
    table2.className = "c-table-scroll";

    var thead = document.createElement("thead");
    var htr = document.createElement("tr");
    columns.forEach(function (c, i) {
      var th = document.createElement("th");
      th.scope = "col";
      th.textContent = c.name;
      if (numeric[i]) th.style.textAlign = "right";
      th.title = c.name + " " + c.type;
      htr.appendChild(th);
    });
    thead.appendChild(htr);
    table2.appendChild(thead);

    var tbody = document.createElement("tbody");
    table2.appendChild(tbody);
    el.scroll.appendChild(table2);

    var more = document.createElement("div");
    more.className = "c-more";
    el.scroll.appendChild(more);

    // Paint in chunks so a 40,000-row result cannot freeze the tab.
    painting = true;
    var i = 0;
    function step() {
      var end = Math.min(i + CHUNK, Math.min(MAX_ROWS, all.length));
      var frag = document.createDocumentFragment();
      for (; i < end; i++) {
        var tr = document.createElement("tr");
        for (var j = 0; j < columns.length; j++) {
          var v = all[i][j];
          var td = document.createElement("td");
          if (v === null) {
            td.className = "null";
            td.textContent = "—";
          } else if (numeric[j]) {
            td.className = "num";
            td.textContent = typeof v === "number" ? formatNum(v) : String(v);
          } else {
            td.className = "txt";
            td.textContent = String(v);
          }
          tr.appendChild(td);
        }
        frag.appendChild(tr);
      }
      tbody.appendChild(frag);
      if (i < end) {
        more.textContent = "showing " + i.toLocaleString() + " of " + all.length.toLocaleString() + "…";
        requestAnimationFrame(step);
      } else {
        more.textContent = all.length > MAX_ROWS
          ? "showing the first " + MAX_ROWS.toLocaleString() + " of " + all.length.toLocaleString() +
            " rows. Download the CSV for the full result."
          : "";
        painting = false;
      }
    }
    requestAnimationFrame(step);
  }

  function formatNum(v) {
    if (!isFinite(v)) return String(v);
    if (Number.isInteger(v)) return v.toLocaleString();
    // Keep floats readable without pretending to more precision than we have.
    return Math.abs(v) >= 1000 ? v.toLocaleString(undefined, { maximumFractionDigits: 1 }) : String(v);
  }

  // ---- CSV export ---------------------------------------------------------
  function downloadCsv() {
    if (!lastResult) return;
    var cols = lastResult.columns;
    var esc = function (s) {
      s = s === null ? "" : String(s);
      return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
    };
    var lines = [cols.map(function (c) { return esc(c.name); }).join(",")];
    lastResult.rows.forEach(function (r) {
      lines.push(r.map(esc).join(","));
    });
    var blob = new Blob([lines.join("\n")], { type: "text/csv;charset=utf-8" });
    var a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "query-result.csv";
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(function () { URL.revokeObjectURL(a.href); }, 1000);
  }

  // ---- wiring -------------------------------------------------------------
  window.__weatherConsole = { testHandle: testHandle };

  el.bootBtn.addEventListener("click", boot);
  el.run.addEventListener("click", run);
  el.download.addEventListener("click", downloadCsv);
  el.sql.addEventListener("keydown", function (e) {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
      e.preventDefault();
      run();
    }
  });
})();
