// Unit test for the console's statement splitter, run under node.
//
// The bug this guards against is specific: a semicolon inside a SQL comment
// used to split a statement in half, and the leftover prose reached the
// parser as 'syntax error at or near "to"'. These cases pin the four contexts
// where a semicolon is not a separator.
const splitStatements = (sql) => {
  const out = []; let buf = '', i = 0;
  const n = sql.length;
  let inLine = false, inBlock = false, inStr = false, inIdent = false;
  while (i < n) {
    const ch = sql[i], next = sql[i + 1];
    if (inLine) { if (ch === '\n') { inLine = false; buf += ch; } else buf += ch; i++; continue; }
    if (inBlock) { if (ch === '*' && next === '/') { inBlock = false; buf += ch + next; i += 2; continue; } buf += ch; i++; continue; }
    if (inStr) {
      buf += ch;
      if (ch === "'" && next === "'") { buf += next; i += 2; continue; }
      if (ch === "'") inStr = false; i++; continue;
    }
    if (inIdent) { buf += ch; if (ch === '"') inIdent = false; i++; continue; }
    if (ch === '-' && next === '-') { inLine = true; buf += ch + next; i += 2; continue; }
    if (ch === '/' && next === '*') { inBlock = true; buf += ch + next; i += 2; continue; }
    if (ch === "'") { inStr = true; buf += ch; i++; continue; }
    if (ch === '"') { inIdent = true; buf += ch; i++; continue; }
    if (ch === ';') { out.push(buf); buf = ''; i++; continue; }
    buf += ch; i++;
  }
  out.push(buf);
  return out.map((s) => s.trim()).filter((s) => {
    if (s.length === 0) return false;
    return s.replace(/--[^\n]*/g, '').replace(/\/\*[\s\S]*?\*\//g, '').trim().length > 0;
  });
};

const cases = [
  ['plain two statements', 'SELECT 1; SELECT 2;', 2],
  ['trailing semicolon', 'SELECT 1;', 1],
  ['semicolon in a line comment', '-- a; b to see which\nSELECT 1;', 1],
  ['semicolon in a block comment', '/* a; b */ SELECT 1;', 1],
  ['semicolon in a string literal', "SELECT 'a;b' AS s;", 1],
  ['escaped quote then semicolon', "SELECT 'it''s; fine' AS s; SELECT 2;", 2],
  ['semicolon in a quoted identifier', 'SELECT "we;ird" FROM t;', 1],
  ['comment with no trailing newline', 'SELECT 1; -- trailing; comment', 1],
  ['empty statements collapse', 'SELECT 1;;;SELECT 2;', 2],
];

let bad = 0;
for (const [label, sql, want] of cases) {
  const got = splitStatements(sql);
  const ok = got.length === want;
  if (!ok) bad++;
  console.log(`  [${ok ? 'ok' : 'FAIL'}] ${label.padEnd(30)} ${got.length} stmt(s), want ${want}`);
  if (!ok) console.log('        got:', JSON.stringify(got));
}

// The real regression: every statement of the shipped SQL must come out whole.
const fs = require('fs');
const path = require('path');
const root = process.env.SITE_ROOT || path.join(__dirname, '..', '..');
const sql = fs.readFileSync(path.join(root, 'sql', 'views.sql'), 'utf8') + '\n' +
  fs.readFileSync(path.join(root, 'sql', 'events.sql'), 'utf8');
const stmts = splitStatements(sql);
const creates = stmts.filter((s) => /\bCREATE OR REPLACE\b/i.test(s)).length;
const expectCreates = 13; // 5 views + 8 event views
const okAll = creates === expectCreates && stmts.every((s) => !/^--[\s\S]*\b(to|and|the)\b/.test(s.replace(/^\s*--.*$/gm, '')));
if (!okAll) bad++;
console.log(`  [${okAll ? 'ok' : 'FAIL'}] shipped SQL splits into whole statements   ${creates} CREATE of ${expectCreates}`);
if (!okAll) stmts.forEach((s, i) => console.log(`        [${i}] ${s.split('\n').filter((l) => l.trim())[0] || '(comment only)'}`));

console.log(bad ? `\n${bad} splitter checks FAILED` : '\nall splitter checks passed');
process.exit(bad ? 1 : 0);
