// Drive the real page in headless Edge and confirm the SQL console works end to end.
// This is the only check that exercises the actual DuckDB-WASM browser path.
const { spawn } = require('child_process');
const http = require('http');
const path = require('path');
const fs = require('fs');

const EDGE = 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';
// Serve the built site from the project root, wherever this file is run from.
const ROOT = process.env.SITE_ROOT
  || (fs.existsSync(path.join(__dirname, '..', 'index.html')) ? path.join(__dirname, '..') : __dirname);
const PORT = Number(process.env.PORT || 8731);
const PROFILE = path.join(__dirname, 'edge-profile');

const MIME = { '.html': 'text/html', '.parquet': 'application/octet-stream', '.csv': 'text/csv', '.json': 'application/json' };

function serve() {
  return new Promise((resolve) => {
    const s = http.createServer((req, res) => {
      let p = decodeURIComponent(req.url.split('?')[0]);
      if (p === '/') p = '/index.html';
      const f = path.join(ROOT, p);
      if (!f.startsWith(ROOT) || !fs.existsSync(f) || !fs.statSync(f).isFile()) {
        res.writeHead(404); return res.end('nope');
      }
      res.writeHead(200, { 'Content-Type': MIME[path.extname(f)] || 'application/octet-stream' });
      fs.createReadStream(f).pipe(res);
    });
    s.listen(PORT, '127.0.0.1', () => resolve(s));
  });
}

async function cdp() {
  const dir = path.join(PROFILE, String(process.pid));
  const args = [
    '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    '--remote-debugging-port=9333', `--user-data-dir=${dir}`,
    '--allow-file-access-from-files', '--no-sandbox', 'about:blank',
  ];
  const proc = spawn(EDGE, args, { stdio: 'ignore' });
  for (let i = 0; i < 60; i++) {
    try {
      const r = await new Promise((res, rej) => {
        http.get('http://127.0.0.1:9333/json/version', (x) => {
          let b = ''; x.on('data', (d) => (b += d)); x.on('end', () => res(JSON.parse(b)));
        }).on('error', rej);
      });
      return { proc, ws: r.webSocketDebuggerUrl };
    } catch { await new Promise((r) => setTimeout(r, 500)); }
  }
  proc.kill();
  throw new Error('edge did not expose a debugging port');
}

class Sess {
  constructor(ws) { this.ws = ws; this.id = 0; this.pending = new Map(); this.events = []; }
  static async open(wsUrl) {
    const WebSocket = require('ws');
    const ws = new WebSocket(wsUrl, { maxPayload: 512 * 1024 * 1024 });
    await new Promise((res, rej) => { ws.once('open', res); ws.once('error', rej); });
    const s = new Sess(ws);
    ws.on('message', (raw) => {
      const m = JSON.parse(raw);
      if (m.id && s.pending.has(m.id)) {
        const { resolve, reject } = s.pending.get(m.id);
        s.pending.delete(m.id);
        m.error ? reject(new Error(JSON.stringify(m.error))) : resolve(m.result);
      } else if (m.method) s.events.push(m);
    });
    return s;
  }
  send(method, params = {}, sessionId) {
    const id = ++this.id;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }));
      setTimeout(() => { if (this.pending.delete(id)) reject(new Error('timeout ' + method)); }, 300000);
    });
  }
  async eval(expression, sessionId) {
    const r = await this.send('Runtime.evaluate', {
      expression, awaitPromise: true, returnByValue: true,
    }, sessionId);
    if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || JSON.stringify(r.exceptionDetails));
    return r.result.value;
  }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const server = await serve();
  const { proc, ws } = await cdp();
  const s = await Sess.open(ws);

  const { targetId } = await s.send('Target.createTarget', { url: 'about:blank' });
  const { sessionId } = await s.send('Target.attachToTarget', { targetId, flatten: true });

  const logs = [];
  await s.send('Runtime.enable', {}, sessionId);
  await s.send('Log.enable', {}, sessionId);
  await s.send('Page.enable', {}, sessionId);
  s.ws.on('message', (raw) => {
    const m = JSON.parse(raw);
    if (m.method === 'Runtime.consoleAPICalled' && m.params.type === 'error') {
      logs.push('console.error: ' + m.params.args.map((a) => a.description || JSON.stringify(a.value)).join(' '));
    }
    if (m.method === 'Runtime.exceptionThrown') {
      logs.push('exception: ' + (m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text));
    }
    if (m.method === 'Log.entryAdded' && m.params.entry.level === 'error') {
      logs.push('log: ' + m.params.entry.text);
    }
  });

  const url = `http://127.0.0.1:${PORT}/index.html`;
  await s.send('Page.navigate', { url }, sessionId);
  await sleep(3500);

  // The page's own drawing code must still have run.
  const atlas = await s.eval(`(() => ({
    circles: document.querySelectorAll('#map circle').length,
    listRows: document.querySelectorAll('#list > *').length,
    monthCards: document.querySelectorAll('.months .band, .months > *').length,
    consolePresent: !!document.getElementById('console'),
    bootVisible: !document.querySelector('[data-boot]').hidden,
    workHidden: document.querySelector('[data-work]').hidden,
    dataKB: Math.round(document.getElementById('console-parquet').textContent.length / 1024)
  }))()`, sessionId);
  console.log('atlas after load:', JSON.stringify(atlas, null, 1));

  if (!atlas.circles) throw new Error('atlas map did not render');
  if (!atlas.consolePresent) throw new Error('console section missing');

  // Press the boot button and wait for DuckDB to come up.
  console.log('\nbooting DuckDB-WASM from the CDN (this downloads ~30MB)...');
  await s.eval(`document.querySelector('[data-boot-btn]').click(); true`, sessionId);

  let ready = false;
  for (let i = 0; i < 300; i++) {
    const st = await s.eval(`(() => ({
      hidden: document.querySelector('[data-work]').hidden,
      status: document.querySelector('[data-status]').textContent
    }))()`, sessionId);
    if (!st.hidden) { ready = true; console.log('  engine ready after ~' + (i * 0.5).toFixed(1) + 's:', st.status); break; }
    if (st.status && /could not|error/i.test(st.status)) throw new Error('boot failed: ' + st.status);
    await sleep(500);
  }
  if (!ready) throw new Error('engine never became ready');

  // The saved-query chips should be populated from the embedded list.
  const chips = await s.eval(`Array.from(document.querySelectorAll('.c-chip')).map(b => b.textContent)`, sessionId);
  console.log(`  saved query chips: ${chips.length}`);
  if (chips.length < 10) throw new Error('expected many saved query chips, got ' + chips.length);

  const schema = await s.eval(`document.querySelectorAll('.c-table').length`, sessionId);
  console.log(`  schema cards: ${schema}`);
  if (schema !== 7) throw new Error('expected 7 schema cards, got ' + schema);

  // Run every saved query through the real browser engine.
  console.log('\nrunning all saved queries in the browser:');
  // Each click must be judged on a FRESH render, so wipe the table and the note
  // first. Without this the previous result is still on screen and every query
  // looks like it passed.
  const results = await s.eval(`(async () => {
    const out = [];
    const note = document.querySelector('[data-note]');
    const scroll = document.querySelector('[data-scroll]');
    for (const b of Array.from(document.querySelectorAll('.c-chip'))) {
      const label = b.textContent;
      scroll.innerHTML = '';
      note.className = 'c-note';
      note.textContent = 'running';
      document.querySelector('[data-meta]').textContent = '';
      b.click();
      // The note is written before the rows are painted, so waiting on it
      // alone races the chunked renderer. Wait for actual tbody rows, an
      // explicit "No rows", or an error.
      for (let i = 0; i < 200; i++) {
        const painted = document.querySelectorAll('.c-table-scroll tbody tr').length;
        const empty = /No rows\./.test(scroll.textContent);
        if (note.className.includes('err') || painted || empty) break;
        await new Promise(r => setTimeout(r, 100));
      }
      out.push({
        name: label,
        error: note.className.includes('err') ? note.textContent.slice(0, 200) : null,
        rows: document.querySelectorAll('.c-table-scroll tbody tr').length,
        head: Array.from(document.querySelectorAll('.c-table-scroll thead th')).map(t => t.textContent)
      });
    }
    return out;
  })()`, sessionId);

  let bad = 0;
  for (const r of results) {
    // A chip that renders 0 rows is a silent failure, not a pass.
    if (r.error) { bad++; console.log(`  [FAIL] ${r.name} :: ${r.error}`); }
    else if (!r.rows) { bad++; console.log(`  [FAIL] ${r.name} :: returned no rows`); }
    else console.log(`  [ok]   ${r.name.padEnd(46)} ${String(r.rows).padStart(4)} rows  [${r.head.slice(0, 4).join(', ')}]`);
  }
  if (bad) throw new Error(`${bad} saved queries failed in the browser`);

  // The published figures, re-derived by the browser engine.
  console.log('\npublished figures, re-derived in the browser:');
  const figuresRaw = await s.eval(`(async () => {
    const h = globalThis.__weatherConsole.testHandle();
    const conn = h.getConn();
    const run = async (sql) => {
      const t = await conn.query(sql);
      return t.toArray().map(r => r.toJSON());
    };
    // CDP cannot serialise BigInt or Arrow values, so flatten everything to
    // strings before stringifying. One leaked BigInt kills the whole payload.
    const S = (v) => (v === null || v === undefined) ? null : String(v);
    const row = (r) => { const o = {}; for (const k in r) o[k] = S(r[k]); return o; };
    const one = async (sql) => row((await run(sql))[0]);
    return JSON.stringify({
      hottest: await one("SELECT name, tmax, CAST(date AS VARCHAR) AS d FROM wx WHERE tmax IS NOT NULL ORDER BY tmax DESC LIMIT 1"),
      coldest: await one('SELECT name, tmin FROM wx WHERE tmin IS NOT NULL ORDER BY tmin LIMIT 1'),
      wettest: await one('SELECT name, rain FROM wx ORDER BY rain DESC LIMIT 1'),
      turpan: await one("SELECT ROUND(AVG(tmax),1) AS hi, ROUND(SUM(rain),1) AS rain FROM wx WHERE name='Turpan'"),
      rows: (await one('SELECT COUNT(*) AS n FROM weather')).n,
      stations: (await one('SELECT COUNT(DISTINCT station_id) AS n FROM stations')).n,
      blanks: (await one('SELECT COUNT(*) AS n FROM weather WHERE tmax IS NULL')).n,
      corr: (await one('SELECT ROUND(CORR(altitude, mean_high),2) AS c FROM station_extremes')).c,
      bands: (await run('SELECT band, stations, mean_high FROM altitude_bands')).map(row),
      warmDay: await one('SELECT CAST(date AS VARCHAR) AS d, mean_high FROM network_day ORDER BY mean_high DESC LIMIT 1'),
      wetDay: await one('SELECT CAST(date AS VARCHAR) AS d, mean_rain FROM network_day ORDER BY mean_rain DESC LIMIT 1'),
      // The gaps-and-islands run query: this is the one that depends on
      // DATE - INTEGER behaving, so it is asserted rather than just rendered.
      longestRun: await one("WITH hot AS (SELECT name, date, CAST(date AS DATE) - CAST(ROW_NUMBER() OVER (PARTITION BY name ORDER BY date) AS INTEGER) AS grp FROM wx WHERE tmax >= 35), runs AS (SELECT name, grp, COUNT(*) AS days FROM hot GROUP BY name, grp) SELECT name, MAX(days) AS days FROM runs GROUP BY name ORDER BY days DESC LIMIT 1")
    });
  })()`, sessionId);
  const figures = JSON.parse(figuresRaw);
  let mism = 0;

  // The event catalogue has to be built by the browser engine too, and its
  // integrity properties re-checked there rather than trusted from Python.
  const eventsRaw = await s.eval(`(async () => {
    const conn = globalThis.__weatherConsole.testHandle().getConn();
    const S = (v) => (v === null || v === undefined) ? null : String(v);
    const rows = async (q) => (await conn.query(q)).toArray().map(r => { const j = r.toJSON(); const o = {}; for (const k in j) o[k] = S(j[k]); return o; });
    const one = async (q) => (await rows(q))[0];
    const split = await one("SELECT (SELECT COUNT(*) FROM event_catalogue) AS c, (SELECT COUNT(*) FROM heatwave_runs) + (SELECT COUNT(*) FROM cold_spell_runs) + (SELECT COUNT(*) FROM heavy_rain_runs) + (SELECT COUNT(*) FROM dry_spell_runs) AS f");
    return JSON.stringify({
      catalogue: split.c,
      catalogueVsFamilies: split.c + '/' + split.f,
      // A reversed arg_max(arg, val) returns the measurement instead of the
      // date, so a peak column that stops being a DATE is the tell. Checked
      // with typeof() in the engine rather than by eyeballing a rendered cell.
      peakDateTypes: (await one("SELECT typeof(peak_date) AS t FROM heatwave_runs LIMIT 1")).t,
      worstPct: (await one("SELECT ROUND(MAX(pct_absolute),1) AS p FROM national_heatwave_days")).p,
      thresholds: await one('SELECT COUNT(*) AS n, COUNT(*) FILTER (WHERE tmax_p90 IS NULL) AS blank FROM station_thresholds'),
      longestAbsolute: await one("SELECT name, days, peak_tmax, CAST(peak_date AS VARCHAR) AS d FROM heatwave_runs WHERE kind='absolute' ORDER BY days DESC LIMIT 1"),
      nationalEpisodes: await rows('SELECT CAST(started AS VARCHAR) AS s, CAST(ended AS VARCHAR) AS e, days, peak_stations FROM national_events ORDER BY days DESC, peak_stations DESC'),
      relativeOnly: S((await one("SELECT COUNT(DISTINCT station_id) AS n FROM heatwave_runs WHERE kind='relative' AND station_id NOT IN (SELECT station_id FROM heatwave_runs WHERE kind='absolute')")).n),
      aridFloor: await one('SELECT (SELECT COUNT(*) FROM station_thresholds WHERE rain_p95 < 10.0) AS excluded, (SELECT ROUND(MIN(peak_rain),2) FROM heavy_rain_runs) AS smallest_kept')
    });
  })()`, sessionId);
  const ev = JSON.parse(eventsRaw);
  console.log('\nevent catalogue, built in the browser:');
  console.log('  ' + JSON.stringify(ev, null, 1).replace(/\n/g, '\n  '));

  const eventExpect = [
    ['catalogue vs families', ev.catalogueVsFamilies, '13911/13911'],
    ['catalogue size', +ev.catalogue, 13911],
    ['peak_date is a DATE', ev.peakDateTypes, 'DATE'],
    ['worst national share', +ev.worstPct, 31.7],
    ['stations with thresholds', ev.thresholds.n, '379'],
    ['stations with no tmax', ev.thresholds.blank, '7'],
    ['longest absolute run', ev.longestAbsolute.name, 'Turpan'],
    ['longest absolute days', ev.longestAbsolute.days, '42'],
    ['longest absolute peak', +ev.longestAbsolute.peak_tmax, 46.6],
    ['national episodes', ev.nationalEpisodes.length, 7],
    ['longest national episode', +ev.nationalEpisodes[0].days, 6],
    ['relative-only stations', ev.relativeOnly, '146'],
    ['arid stations excluded', ev.aridFloor.excluded, '80'],
    ['smallest kept rain peak', +ev.aridFloor.smallest_kept, 10.3],
  ];
  for (const [label, got, want] of eventExpect) {
    const ok = typeof want === 'number' ? Math.abs(got - want) < 0.051 : got === want;
    if (!ok) mism++;
    console.log(`  [${ok ? 'ok' : 'FAIL'}] ${label.padEnd(26)} got ${got} want ${want}`);
  }
  if (mism) throw new Error(`${mism} event checks failed in the browser`);

  console.log(JSON.stringify(figures, null, 1));

  const expect = [
    ['hottest tmax', +figures.hottest.tmax, 46.6],
    ['hottest name', figures.hottest.name, 'Turpan'],
    ['hottest date', figures.hottest.d, '2022-06-23'],
    ['coldest tmin', +figures.coldest.tmin, -15.7],
    ['coldest name', figures.coldest.name, 'Wudaoliang'],
    ['wettest rain', +figures.wettest.rain, 241],
    ['wettest name', figures.wettest.name, 'Jingdezhen'],
    ['Turpan mean high', +figures.turpan.hi, 39.7],
    ['Turpan rain', +figures.turpan.rain, 4.9],
    ['rows', +figures.rows, 42827],
    ['stations', +figures.stations, 379],
    ['blanks', +figures.blanks, 791],
    ['corr', +figures.corr, -0.76],
    ['warmest day', figures.warmDay.d, '2022-07-25'],
    ['warmest day high', +figures.warmDay.mean_high, 31.8],
    ['wettest day', figures.wetDay.d, '2022-07-05'],
    ['wettest day rain', +figures.wetDay.mean_rain, 10.0],
    ['longest hot run', +figures.longestRun.days, 42],
    ['longest hot run at', figures.longestRun.name, 'Turpan'],
  ];
  for (const [label, got, want] of expect) {
    const ok = typeof want === 'number' ? Math.abs(got - want) < 0.051 : got === want;
    if (!ok) mism++;
    console.log(`  [${ok ? 'ok' : 'FAIL'}] ${label.padEnd(22)} got ${got} want ${want}`);
  }
  const bandExpect = [[138, 30.1], [88, 29.3], [93, 27.6], [53, 20.0]];
  figures.bands.forEach((b, i) => {
    const ok = +b.stations === bandExpect[i][0] && Math.abs(+b.mean_high - bandExpect[i][1]) < 0.051;
    if (!ok) mism++;
    console.log(`  [${ok ? 'ok' : 'FAIL'}] band ${b.band.padEnd(20)} ${b.stations} stations, ${b.mean_high} degC`);
  });
  if (mism) throw new Error(`${mism} published figures do not match in the browser`);

  // The table must render numbers right-aligned, nulls as em dashes, and be capped.
  console.log('\nrendering:');
  await s.eval(`(() => { const b = Array.from(document.querySelectorAll('.c-chip')).find(x => /wettest days in the far south/i.test(x.textContent)); b.click(); return true; })()`, sessionId);
  await sleep(1500);
  const render = await s.eval(`(() => {
    const cell = document.querySelector('.c-table-scroll tbody td');
    return {
      numAligned: !!document.querySelector('.c-table-scroll td.num'),
      txtCells: document.querySelectorAll('.c-table-scroll td.txt').length,
      hasNullDash: !!document.querySelector('.c-table-scroll td.null'),
      meta: document.querySelector('[data-meta]').textContent
    };
  })()`, sessionId);
  console.log('  ' + JSON.stringify(render));

  // Big result: the 42k-row table must be capped and must not hang the tab.
  console.log('\nlarge result (all 42,827 rows):');
  await s.eval(`(() => { document.querySelector('[data-sql]').value = 'SELECT * FROM weather'; return true; })()`, sessionId);
  const t0 = Date.now();
  await s.eval(`document.querySelector('[data-run]').click(); true`, sessionId);
  let capped = false;
  for (let i = 0; i < 200; i++) {
    const st = await s.eval(`(() => ({
      rows: document.querySelectorAll('.c-table-scroll tbody tr').length,
      more: (document.querySelector('.c-more') || {}).textContent || '',
      meta: document.querySelector('[data-meta]').textContent
    }))()`, sessionId);
    if (/rows/.test(st.meta) && st.more) { capped = true; console.log(`  ${st.meta} | ${st.more}`); break; }
    await sleep(500);
  }
  if (!capped) throw new Error('large result never finished painting');
  const painted = await s.eval(`document.querySelectorAll('.c-table-scroll tbody tr').length`, sessionId);
  console.log(`  painted ${painted} rows in ${((Date.now() - t0) / 1000).toFixed(1)}s (capped at 500)`);
  if (painted > 500) throw new Error('result table was not capped');

  // A bad query must fail gracefully, not white-screen.
  await s.eval(`(() => { document.querySelector('[data-sql]').value = 'SELECT * FROM nope'; return true; })()`, sessionId);
  await s.eval(`document.querySelector('[data-run]').click(); true`, sessionId);
  await sleep(2000);
  const errState = await s.eval(`(() => ({
    isErr: document.querySelector('[data-note]').className.includes('err'),
    text: document.querySelector('[data-note]').textContent.slice(0, 120)
  }))()`, sessionId);
  console.log(`\nbad query handled: ${errState.isErr} :: ${errState.text.replace(/\n/g, ' ')}`);
  if (!errState.isErr) throw new Error('a bad query should have surfaced an error');

  const stillAlive = await s.eval(`document.querySelectorAll('#map circle').length`, sessionId);
  if (!stillAlive) throw new Error('the atlas broke after the console errored');
  console.log(`atlas still drawing after the error (${stillAlive} stations)`);

  if (logs.length) {
    console.log('\npage errors:');
    logs.forEach((l) => console.log('  ' + l));
  } else {
    console.log('\nno page errors');
  }

  server.close();
  proc.kill();
  console.log('\nALL BROWSER CHECKS PASSED');
  process.exit(0);
})().catch(async (e) => {
  console.error('\nFAILED:', e.message);
  process.exit(1);
});
