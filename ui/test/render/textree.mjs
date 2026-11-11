// Visible text tree snapshots and their comparison (T-P4-10; P4 architecture §1.4, critique E1).
//
// Format: the P4-01 baseline format (ui/test/baseline/README.md; ruling 第三波 T-P4-01 #5).
// One row per visible element in DOM order: [depth, tag, own text, aria-label, title, placeholder].
// "Own text" is the element's direct child text nodes joined and whitespace-collapsed (a child's
// text is in the child's row; the whole tree concatenated is textContent). Rows whose four text
// fields are all empty are left out. Files: one row per line (serialize()).
//
// Comparison (04 series regression checks): diffTrees(a, b, {allow}) gives a line diff. A
// removed row paired with an added row in the same hunk is "allowed" when depth and tag are
// the same and each differing field is covered by the allow table:
//   {"entries": [
//     {"from": "已验证 · 3 分钟前", "to": "已确认 · 3 分钟前", "why": "…", "page"?: "overview"},
//     {"from_re": "^(\\d+) 分钟前$", "to_re": "^(\\d+) 分钟前$", "why": "…"},          both match
//     {"normalize_re": "(?<=\\d),(?=\\d{3})", "normalize_to": "", "why": "grouping"} applied to both sides
//   ]}
// fmtAllowances() turns T-P4-03's T.fmt table (test/fixtures/i18n/fmt_allowed_diffs.json, zh_CN)
// into entries: each example before → after, plus the digit-grouping normalisation.
//
// CLI:  node ui/test/render/textree.mjs diff <a.json> <b.json> [--allow <allow.json>]... [--fmt] [--page <id>]
//       exit 0 when the trees are equal up to allowed differences, 1 otherwise.
import { readFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const UI = resolve(HERE, '..', '..');

// Runs in the page (page.evaluate(textTreeInPage)). Kept self-contained: no closure variables.
export function textTreeInPage() {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const out = [];
  const visit = (el, depth) => {
    for (const c of el.children) {
      if (!c.checkVisibility({ opacityProperty: true, visibilityProperty: true })) {
        // visibility:hidden parents may still have visible children; display:none / opacity:0 do not.
        if (getComputedStyle(c).display !== 'none') visit(c, depth + 1);
        continue;
      }
      let own = '';
      for (const n of c.childNodes) if (n.nodeType === 3) own += n.nodeValue;
      const row = [depth, c.tagName.toLowerCase(), norm(own), norm(c.getAttribute('aria-label')), norm(c.getAttribute('title')), norm(c.getAttribute('placeholder'))];
      if (row[2] || row[3] || row[4] || row[5]) out.push(row);
      visit(c, depth + 1);
    }
  };
  visit(document.body, 0);
  return out;
}

/** Same line layout as the baseline files (one JSON row per line). */
export function serialize(tree) {
  return JSON.stringify(tree, null, 0).replace(/\],\[/g, '],\n[') + '\n';
}

export function readTree(path) {
  const v = JSON.parse(readFileSync(path, 'utf8').replace(/^﻿/, ''));
  if (!Array.isArray(v) || v.some(r => !Array.isArray(r) || r.length !== 6)) throw new Error(`${path}: not a text tree (rows of 6)`);
  return v;
}

export function readAllow(path) {
  const v = JSON.parse(readFileSync(path, 'utf8').replace(/^﻿/, ''));
  const entries = Array.isArray(v) ? v : v.entries;
  if (!Array.isArray(entries)) throw new Error(`${path}: expected {entries: [...]}`);
  for (const e of entries) if (!e || typeof e.why !== 'string' || !e.why.trim()) throw new Error(`${path}: every entry needs a "why"`);
  return entries;
}

/** T-P4-03's T.fmt allowed differences (zh_CN examples) as allow entries. */
export function fmtAllowances(path = join(UI, 'test', 'fixtures', 'i18n', 'fmt_allowed_diffs.json')) {
  const doc = JSON.parse(readFileSync(path, 'utf8'));
  const out = [{ normalize_re: '(?<=\\d),(?=\\d{3}(?!\\d))', normalize_to: '', why: 'T.fmt grouping (fmt_allowed_diffs.json zh_CN grouping)' }];
  const flat = v => (Array.isArray(v) ? v.join(' ') : String(v));
  for (const [fn, list] of Object.entries(doc.zh_CN || {})) {
    if (!Array.isArray(list)) continue;
    for (const d of list) {
      const ex = d && d.example;
      if (!ex || ex.before === undefined || ex.after === undefined) continue;
      if (flat(ex.before) === flat(ex.after)) continue;
      out.push({ from: flat(ex.before), to: flat(ex.after), why: `T.fmt ${fn} ${d.category}: ${d.why}` });
    }
  }
  return out;
}

function compile(entries, page) {
  return (entries || []).filter(e => !e.page || !page || e.page === page).map(e => ({
    ...e,
    fromRe: e.from_re ? new RegExp(e.from_re, 'u') : null,
    toRe: e.to_re ? new RegExp(e.to_re, 'u') : null,
    normRe: e.normalize_re ? new RegExp(e.normalize_re, 'gu') : null,
  }));
}

function fieldAllowed(a, b, rules) {
  if (a === b) return null;
  let na = a, nb = b;
  const norms = [];
  for (const r of rules) {
    if (!r.normRe) continue;
    const xa = na.replace(r.normRe, r.normalize_to || ''), xb = nb.replace(r.normRe, r.normalize_to || '');
    if (xa !== na || xb !== nb) norms.push(r);
    na = xa; nb = xb;
  }
  if (na === nb) return norms[0];
  for (const r of rules) {
    if (r.from !== undefined && r.to !== undefined && r.from === na && r.to === nb) return r;
    if (r.from !== undefined && r.to !== undefined && na.includes(r.from) && na.split(r.from).join(r.to) === nb) return r;
    if (r.fromRe && r.toRe && r.fromRe.test(na) && r.toRe.test(nb)) return r;
  }
  return false;
}

/** Row-pair verdict: null when equal, the list of rules used when allowed, false otherwise. */
export function rowAllowed(ra, rb, rules) {
  if (ra[0] !== rb[0] || ra[1] !== rb[1]) return false;
  const used = [];
  for (let i = 2; i < 6; i++) {
    const v = fieldAllowed(ra[i], rb[i], rules);
    if (v === false) return false;
    if (v) used.push(v);
  }
  return used.length ? used : null;
}

// Longest common subsequence over row keys (rows are short; trees have < ~5,000 rows).
function lcsOps(a, b) {
  const n = a.length, m = b.length;
  const ka = a.map(r => JSON.stringify(r)), kb = b.map(r => JSON.stringify(r));
  // prefix / suffix trimming keeps the table small for near-identical trees
  let p = 0; while (p < n && p < m && ka[p] === kb[p]) p++;
  let s = 0; while (s < n - p && s < m - p && ka[n - 1 - s] === kb[m - 1 - s]) s++;
  const A = ka.slice(p, n - s), B = kb.slice(p, m - s);
  const N = A.length, M = B.length;
  const t = Array.from({ length: N + 1 }, () => new Uint32Array(M + 1));
  for (let i = N - 1; i >= 0; i--) for (let j = M - 1; j >= 0; j--) t[i][j] = A[i] === B[j] ? t[i + 1][j + 1] + 1 : Math.max(t[i + 1][j], t[i][j + 1]);
  const ops = [];
  for (let i = 0; i < p; i++) ops.push({ op: '=', a: i, b: i });
  let i = 0, j = 0;
  while (i < N || j < M) {
    if (i < N && j < M && A[i] === B[j]) { ops.push({ op: '=', a: p + i, b: p + j }); i++; j++; }
    else if (j < M && (i >= N || t[i][j + 1] >= t[i + 1][j])) { ops.push({ op: '+', b: p + j }); j++; }
    else { ops.push({ op: '-', a: p + i }); i++; }
  }
  for (let k = 0; k < s; k++) ops.push({ op: '=', a: n - s + k, b: m - s + k });
  return ops;
}

/**
 * Compare two text trees. Returns {equal, allowed:[{a, b, why[]}], diff:[lines], changes}.
 * `equal` is true when every difference is an allowed row pair.
 */
export function diffTrees(a, b, { allow = [], page = null } = {}) {
  const rules = compile(allow, page);
  const ops = lcsOps(a, b);
  const allowed = [], lines = [];
  let changes = 0;
  for (let k = 0; k < ops.length;) {
    if (ops[k].op === '=') { k++; continue; }
    const del = [], add = [];
    while (k < ops.length && ops[k].op !== '=') { (ops[k].op === '-' ? del : add).push(ops[k]); k++; }
    // pair removed and added rows in order; leftovers are plain changes
    const n = Math.min(del.length, add.length);
    for (let x = 0; x < Math.max(del.length, add.length); x++) {
      const d = del[x], ad = add[x];
      if (x < n) {
        const v = rowAllowed(a[d.a], b[ad.b], rules);
        if (v) { allowed.push({ a: a[d.a], b: b[ad.b], line_a: d.a + 1, line_b: ad.b + 1, why: v.map(r => r.why) }); continue; }
      }
      if (d) { lines.push(`- ${d.a + 1}: ${JSON.stringify(a[d.a])}`); changes++; }
      if (ad) { lines.push(`+ ${ad.b + 1}: ${JSON.stringify(b[ad.b])}`); changes++; }
    }
  }
  return { equal: changes === 0, changes, allowed, diff: lines };
}

async function main(argv) {
  if (argv[0] !== 'diff' || argv.length < 3) {
    console.error('usage: node ui/test/render/textree.mjs diff <a.json> <b.json> [--allow <allow.json>]... [--fmt] [--page <id>]');
    return 2;
  }
  const allow = [];
  let page = null;
  for (let i = 3; i < argv.length; i++) {
    if (argv[i] === '--allow') allow.push(...readAllow(resolve(argv[++i])));
    else if (argv[i] === '--fmt') allow.push(...fmtAllowances());
    else if (argv[i] === '--page') page = argv[++i];
    else throw new Error(`unknown argument: ${argv[i]}`);
  }
  const r = diffTrees(readTree(resolve(argv[1])), readTree(resolve(argv[2])), { allow, page });
  for (const x of r.allowed) console.log(`~ ${x.line_a}→${x.line_b}: ${JSON.stringify(x.a)} → ${JSON.stringify(x.b)}  [${x.why.join('; ')}]`);
  for (const l of r.diff) console.log(l);
  console.log(r.equal ? `textree: equal (${r.allowed.length} allowed difference(s))` : `textree: ${r.changes} differing row(s)`);
  return r.equal ? 0 : 1;
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  main(process.argv.slice(2)).then(code => { process.exitCode = code; }, e => { console.error(e); process.exitCode = 2; });
}
