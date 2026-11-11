// State render matrix (T-P4-10; P4 architecture §1.4, §2.7, §3.8, §3.9, §12).
//
//   node ui/test/render/run.mjs [--case <file|name>[,…]] [--page <id>[,…]]
//        [--langs zh_CN,pseudo | all | <lang>,…] [--sizes 1060x700,1280x800]
//        [--run <name>] [--out <dir>] [--build-flags "<build.mjs flags>"] [--dist <dir>]
//        [--csp strict] [--repeat <n>] [--leaks auto|hard] [--jobs <n>] [--self-test]
//
// Every cell = case × language × size: a fresh browser context loads
// file://…/ui/dist/design/index.html?lang=<lang>#<page> (with --csp strict:
// http://127.0.0.1:<port>/index.html?lang=<lang>#<page>, see below) with the deterministic fixture
// (fixture_clock.js) and the case's scenario as window.__TESSERACT_FIXTURE__ (fixture mode of
// boot/rt-design.js; ?lang reaches the page through FakeTransport's hello.lang rewrite, never a
// direct language switch). The scenario's steps run at connect (advance_ms drives the injected
// fake clock T.clock; nothing waits in real time). After the settle the case's actions run,
// then the cell writes, under .build/新界面迁移_工单/evidence/render/<run>/<case>/<lang>/<WxH>/:
//   screenshot.png · textree.json (textree.mjs) · ids.json (--i18n-trace builds) · checks.json
// and the run writes summary.json (every cell; failing cells with their screenshot path) and,
// with --langs all, truncation.json for tools/i18n_check.mjs. Exit code 1 when a cell fails.
//
// Langs: default zh_CN and pseudo; `all` = the 9 product languages (add `,pseudo` for both).
// --build-flags builds a separate design target (node build.mjs --target design --out <fresh
// temp dir> <flags> --pseudo --i18n-trace; the last two are added when missing) and renders that
// one; without it ui/dist/design is used as built (npm --prefix ui run build -- --pseudo
// --i18n-trace).
// Truncations: zh_CN and pseudo cells fail on them; the other 8 product languages only report
// them (counts, checks.json, truncation.json): their level and allow list belong to
// tools/i18n_check.mjs (i18n_gates.json `truncation`: p4 report, p7 hard). A case's
// overflow_allow moves matching truncations out of the verdict in every language.
// Known failures: a case with known_fail {owner, why} renders and is checked as usual, but its
// failing cells do not count for the exit code; they are listed in summary.known_failures (a
// known-fail cell that passes is listed in summary.known_fail_passed: drop the flag).
// --csp strict (T-P4-08b): the build snapshot is served by a loopback http server (serve.mjs:
// node:http, 127.0.0.1, a random port, that directory only; file: pages match 'self' differently
// from the shell's http://tauri.localhost/ — the origin the T-P4-08b e2e saw the shell serve —
// criticism E6) and every .html response carries the
// shell's own CSP (shell/app/tauri.conf.json app.security.csp, plus 'report-sample' so a report
// names the offending text; nothing is loosened) as a <meta> first in <head>. The policy must have
// default-src / script-src / style-src, no 'unsafe-*', nonce, hash or wildcard in any *-src*
// directive (style-src-attr / -elem and script-src-attr / -elem included), and data: only in
// img-src / font-src. Every securitypolicyviolation event in a cell is a failure, and a run with
// any violation exits 1 even when it is in a known_fail cell (known_fail never covers CSP).
// --leaks auto (default): a pseudo-language leak in a region whose source file still starts with
// @i18n-pending (shell: index.html / core.js / app.js; page: pages/<page>.js) is reported as
// pending and does not fail the cell; --leaks hard fails on every leak.
// --jobs <n> (T-P5-13): render n cells at a time, each worker with its own browser (default 1).
// Only the scheduling changes: every cell is still a fresh context with the same fixture, checks
// and verdicts, and summary.json / truncation.json list the cells in the same order as a serial
// run (the console lines come in completion order).
// --self-test: the framework's own checks (see selftest.mjs).
import { chromium } from 'playwright-core';
import { spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { cpSync, existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { basename, dirname, join, relative, resolve, sep } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { textTreeInPage, serialize } from './textree.mjs';
import { serveStatic } from './serve.mjs';
import { cellChecksInPage, classifyLeaks, fontChecks, LEAK_WHITELIST, pendingFiles, visibleIdsInPage } from './checks.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
export const UI = resolve(HERE, '..', '..');
export const REPO = resolve(UI, '..');
export const CHROME = process.env.TESSERACT_UI_CHROME || 'C:/Program Files/Google/Chrome/Application/chrome.exe';
export const EVIDENCE = join(REPO, '.build', '新界面迁移_工单', 'evidence', 'render');
export const ALL_LANGS = ['zh_CN', 'en_US', 'de_DE', 'es_ES', 'fr_FR', 'ja_JP', 'ko_KR', 'pt_BR', 'ru_RU'];
export const DEFAULT_LANGS = ['zh_CN', 'pseudo'];
export const SIZES = ['1060x700', '1280x800'];
export const TRUNCATION_FAILS = ['zh_CN', 'pseudo']; // the rest report to i18n_check
export const SETTLE_MS = 3000; // virtual page time after load (same as the P4-01 baseline)
export const SHELL_CONF = join(REPO, 'shell', 'app', 'tauri.conf.json');
const posix = p => p.split(sep).join('/');

// ---------------------------------------------------------------------------------------------
// The CSP of the --csp strict run: the shell's own policy (T-P4-08b, P4 architecture §9), with
// 'report-sample' added to script-src / style-src (a violation report then carries the first 40
// characters of the blocked text; it allows nothing). Refused when the policy could not prove
// anything: default-src / script-src / style-src missing, any *-src* directive (style-src-attr /
// -elem, script-src-attr / -elem included) carrying an 'unsafe-…' keyword, a nonce, a hash or a
// wildcard, or data: / blob: / filesystem: outside img-src / font-src (fix round 1, S1).
export function shellCsp(file = SHELL_CONF) {
  const csp = JSON.parse(readFileSync(file, 'utf8')).app?.security?.csp;
  if (typeof csp !== 'string' || !csp.trim()) throw new Error(`${posix(relative(REPO, file))}: app.security.csp missing`);
  return csp;
}
export function strictCsp(policy = shellCsp()) {
  const dirs = policy.split(';').map(d => d.trim()).filter(Boolean).map(d => d.split(/\s+/));
  for (const name of ['default-src', 'style-src', 'script-src']) if (!dirs.some(x => x[0].toLowerCase() === name)) throw new Error(`CSP has no ${name}: ${policy}`);
  for (const d of dirs) {
    const name = d[0].toLowerCase();
    if (!name.includes('-src')) continue;
    const bad = d.slice(1).filter(v => /^'(?:[a-z-]*unsafe[a-z-]*|nonce-.*|sha(256|384|512)-.*)'$/i.test(v) || v.includes('*')
      || (/^(data|blob|filesystem):$/i.test(v) && name !== 'img-src' && name !== 'font-src'));
    if (bad.length) throw new Error(`CSP ${name} must not allow inline content or ${bad.join(' ')}: ${policy}`);
    if (/^(style|script)-src/.test(name) && !d.includes("'report-sample'")) d.push("'report-sample'");
  }
  return dirs.map(d => d.join(' ')).join('; ');
}

// ---------------------------------------------------------------------------------------------
export function parseArgs(argv) {
  const o = { cases: null, pages: null, langs: DEFAULT_LANGS, langsAll: false, sizes: SIZES, run: null, out: null, buildFlags: null, dist: null, csp: null, repeat: 1, leaks: 'auto', jobs: 1, selfTest: false };
  const list = v => String(v || '').split(',').map(s => s.trim()).filter(Boolean);
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i], v = () => { if (i + 1 >= argv.length) throw new Error(`${a} needs a value`); return argv[++i]; };
    if (a === '--case') o.cases = list(v());
    else if (a === '--page') o.pages = list(v());
    else if (a === '--langs') {
      const l = list(v());
      o.langsAll = l.includes('all');
      o.langs = [...new Set(l.flatMap(x => (x === 'all' ? ALL_LANGS : [x])))];
    } else if (a === '--sizes') o.sizes = list(v());
    else if (a === '--run') o.run = v();
    else if (a === '--out') o.out = v();
    else if (a === '--build-flags') o.buildFlags = v();
    else if (a === '--dist') o.dist = v();
    else if (a === '--csp') o.csp = v();
    else if (a === '--repeat') o.repeat = Math.max(1, Number(v()) || 1);
    else if (a === '--leaks') o.leaks = v();
    else if (a === '--jobs') o.jobs = Math.max(1, Math.min(16, Math.floor(Number(v())) || 1));
    else if (a === '--self-test') o.selfTest = true;
    else throw new Error(`unknown argument: ${a}`);
  }
  for (const l of o.langs) if (l !== 'pseudo' && !ALL_LANGS.includes(l)) throw new Error(`unknown language: ${l}`);
  for (const s of o.sizes) if (!/^\d+x\d+$/.test(s)) throw new Error(`bad size: ${s} (expected WxH)`);
  if (o.csp !== null && o.csp !== 'strict') throw new Error('--csp only accepts "strict"');
  if (!['auto', 'hard'].includes(o.leaks)) throw new Error('--leaks auto|hard');
  return o;
}

// ---------------------------------------------------------------------------------------------
// Build selection. --build-flags builds a private design target in a fresh temp directory (so
// two runs never share or --clean each other's output); --pseudo and --i18n-trace are added when
// missing (the matrix needs pseudo for its default languages and the self-test, ids.json needs
// the trace), so `--build-flags "--h-features all"` works as written in the ticket.
export function effectiveBuildFlags(buildFlags) {
  const flags = String(buildFlags || '').split(/\s+/).filter(Boolean);
  if (flags.some(f => ['--target', '--out', '--clean', '--check'].includes(f))) throw new Error('--build-flags must not contain --target / --out / --clean / --check');
  for (const f of ['--pseudo', '--i18n-trace']) if (!flags.includes(f)) flags.push(f);
  return flags;
}

/** {dir, temp, flags}: temp is true when dir is a private build the caller removes. */
export function prepareDist({ buildFlags = null, dist = null } = {}) {
  if (dist) return { dir: resolve(dist), temp: false, flags: null };
  // F-01: an empty --build-flags "" still means a private build (with --pseudo --i18n-trace); only
  // leaving the option out renders ui/dist/design as built
  if (buildFlags === null || buildFlags === undefined) return { dir: join(UI, 'dist', 'design'), temp: false, flags: null };
  const flags = effectiveBuildFlags(buildFlags);
  const out = mkdtempSync(join(tmpdir(), 'tesseract-render-build-'));
  const r = spawnSync(process.execPath, [join(UI, 'build.mjs'), '--target', 'design', '--out', out, ...flags], { cwd: UI, encoding: 'utf8' });
  if (r.status !== 0) { rmSync(out, { recursive: true, force: true }); throw new Error(`design build with ${flags.join(' ')} failed:\n${r.stdout}\n${r.stderr}`); }
  return { dir: out, temp: true, flags };
}

export function readManifest(distDir) {
  const p = join(distDir, 'build-manifest.json');
  if (!existsSync(join(distDir, 'index.html')) || !existsSync(p)) throw new Error(`${posix(relative(REPO, distDir))}: no design build (run: npm --prefix ui run build -- --pseudo --i18n-trace)`);
  const m = JSON.parse(readFileSync(p, 'utf8'));
  if (m.target !== 'design') throw new Error(`${posix(relative(REPO, distDir))}: build-manifest target is ${m.target}, not design`);
  return m;
}

// The run renders a private copy of the build: ui/dist/design is shared and may be rebuilt by
// someone else while a matrix runs. The copy is checked against its build-manifest.json
// (sha256 of every file), retried when it caught a half-written build.
export function snapshotDist(distDir) {
  for (let attempt = 1; ; attempt++) {
    const dir = mkdtempSync(join(tmpdir(), 'tesseract-render-dist-'));
    try {
      // the copy itself is retried too: a concurrent rebuild may remove files mid-copy (ENOENT)
      cpSync(distDir, dir, { recursive: true });
      const manifest = readManifest(dir);
      const bad = (manifest.files || []).filter(f => !existsSync(join(dir, f.path)) || createHash('sha256').update(readFileSync(join(dir, f.path))).digest('hex') !== f.sha256);
      if (bad.length) throw new Error(`build changed while copying (${bad.length} file(s) differ from build-manifest.json)`);
      return { dir, manifest };
    } catch (e) {
      rmSync(dir, { recursive: true, force: true });
      if (attempt >= 3) throw new Error(`${posix(relative(REPO, distDir))}: ${e.message}`);
      Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 500); // let a rebuild finish
    }
  }
}

// The --csp strict server: the build directory over loopback http with the shell's CSP <meta>
// (serve.mjs). -> {origin, close, log, policy}
export async function cspServer(distDir, policy = strictCsp()) {
  const srv = await serveStatic(distDir, { csp: policy });
  return { ...srv, policy };
}

// ---------------------------------------------------------------------------------------------
// Cases
export function loadScenario(ref) {
  if (ref && typeof ref === 'object') return ref;
  const p = join(UI, 'test', 'fixtures', 'scenarios', `${ref}.json`);
  if (!existsSync(p)) throw new Error(`scenario not found: ${ref} (ui/test/fixtures/scenarios/${ref}.json)`);
  return JSON.parse(readFileSync(p, 'utf8'));
}

export async function loadCases({ cases = null, pages = null, dir = join(HERE, 'cases') } = {}) {
  const out = [];
  const files = readdirSync(dir).filter(n => n.endsWith('.mjs')).sort();
  for (const f of files) {
    const fileId = basename(f, '.mjs');
    const mod = await import(pathToFileURL(join(dir, f)).href);
    const list = mod.default;
    if (!Array.isArray(list)) throw new Error(`cases/${f}: default export must be an array`);
    for (const c of list) {
      if (!c || !c.scenario || !c.page) throw new Error(`cases/${f}: every case needs scenario and page`);
      const name = c.name || (typeof c.scenario === 'string' ? c.scenario : c.scenario.name);
      if (!name || !/^[A-Za-z0-9_.-]+$/.test(name)) throw new Error(`cases/${f}: case name "${name}" must be [A-Za-z0-9_.-]+`);
      if (cases && !cases.includes(fileId) && !cases.includes(name)) continue;
      if (pages && !pages.includes(c.page)) continue;
      out.push({ ...c, name, file: fileId });
    }
  }
  const seen = new Set();
  for (const c of out) { const k = `${c.file}/${c.name}`; if (seen.has(k)) throw new Error(`duplicate case ${k}`); seen.add(k); }
  return out;
}

// ---------------------------------------------------------------------------------------------
// Browser
// loopback: the browser of a --csp strict run (or the self-test's CSP blocks) must reach the
// serve.mjs origin, so only then 127.0.0.1 is excluded from the resolver rule (T-P4-08b fix round
// 1, S5: a run without --csp keeps 08a ruling #9 as it was).
export async function launch({ loopback = false } = {}) {
  // offline: no DNS at all (08a ruling #9), and every request other than file:, data: and the
  // cell's own loopback origin (--csp strict, serve.mjs) is aborted in openCell; a WebSocket,
  // which context.route does not see, is refused there as well
  const rules = loopback ? 'MAP * ~NOTFOUND, EXCLUDE 127.0.0.1' : 'MAP * ~NOTFOUND';
  return chromium.launch({ executablePath: CHROME, headless: true, args: ['--disable-gpu', '--font-render-hinting=none', `--host-resolver-rules=${rules}`] });
}

function parseSize(s) { const [w, h] = s.split('x').map(Number); return { width: w, height: h }; }

// origin: the loopback server of a --csp strict run (the page loads from it); else file:.
// TESSERACT_RENDER_DSF (F-02, design review only): the device scale of the screenshots, e.g. 1.5 for
// a 150 % Windows display; checks are in CSS px and do not change. Unset, it is 1 as before.
export async function openCell(browser, { distDir, scenario, lang, size, page: pageId, csp = false, origin = null }) {
  const context = await browser.newContext({
    viewport: parseSize(size), deviceScaleFactor: Number(process.env.TESSERACT_RENDER_DSF) || 1, reducedMotion: 'reduce', locale: 'zh-CN', timezoneId: 'Asia/Shanghai', colorScheme: 'light',
  });
  const net = [];
  await context.route('**/*', route => {
    const u = route.request().url();
    if (u.startsWith('file:') || u.startsWith('data:') || (origin && u.startsWith(origin + '/'))) return route.continue();
    net.push(u);
    return route.abort();
  });
  // (no connectToServer: the socket never leaves the browser; it is closed and recorded)
  await context.routeWebSocket(() => true, ws => { net.push(ws.url()); ws.close({ code: 1008, reason: 'render matrix: offline' }); });
  await context.addInitScript({ path: join(HERE, 'fixture_clock.js') });
  await context.addInitScript(json => {
    window.__TESSERACT_FIXTURE__ = json;
    window.__cspViolations = [];
    // (capture phase on the document: an attribute violation is dispatched at its element when
    // that is connected, else at the document; both reach this listener)
    const where = t => {
      if (!t || t.nodeType !== 1) return t && t.nodeType === 9 ? 'document' : null;
      return t.tagName.toLowerCase() + (t.id ? '#' + t.id : '') + (typeof t.className === 'string' && t.className.trim() ? '.' + t.className.trim().split(/\s+/).slice(0, 3).join('.') : '');
    };
    document.addEventListener('securitypolicyviolation', e => {
      window.__cspViolations.push({ directive: e.violatedDirective, blocked: String(e.blockedURI || ''), sample: String(e.sample || '').slice(0, 80), source: String(e.sourceFile || '').replace(/^.*\//, ''), line: e.lineNumber, col: e.columnNumber, target: where(e.target) });
    }, true);
  }, JSON.stringify(scenario));
  const page = await context.newPage();
  const errors = [];
  page.on('console', m => { if (m.type() === 'error') errors.push({ kind: 'console.error', text: m.text().slice(0, 500) }); });
  page.on('pageerror', e => errors.push({ kind: 'pageerror', text: String(e && e.message || e).slice(0, 500) }));
  const url = (origin ? `${origin}/index.html` : pathToFileURL(join(distDir, 'index.html')).href) + `?lang=${encodeURIComponent(lang)}#${pageId}`;
  await page.goto(url, { waitUntil: 'load' });
  await settle(page, SETTLE_MS);
  return { context, page, errors, net, csp };
}

export async function settle(page, virtualMs = 0) {
  await page.evaluate(() => document.fonts.ready);
  await page.evaluate(ms => window.__fixtureClock.advance(ms), virtualMs);
  await page.waitForTimeout(200); // real time, for 1 ms WAAPI animations (reduced motion) to finish
  await page.evaluate(() => window.__fixtureClock.advance(0));
}

// Case actions (cases/README.md).
export async function runAction(page, a) {
  if (Number.isFinite(a.advance_ms)) {
    await page.evaluate(ms => { if (window.T && T.clock && T.clock.advance) T.clock.advance(ms); }, a.advance_ms);
    await page.evaluate(ms => window.__fixtureClock.advance(ms), a.advance_ms);
  } else if (Number.isFinite(a.page_ms)) await page.evaluate(ms => window.__fixtureClock.advance(ms), a.page_ms);
  else if (a.deliver) await page.evaluate(f => window.__TESSERACT_DESIGN__.deliver(f), a.deliver);
  else if (a.click) await page.click(a.click, { timeout: 5000 });
  else if (a.hover) await page.hover(a.hover, { timeout: 5000 });
  else if (a.press) await page.keyboard.press(a.press);
  else if (a.fill) await page.fill(a.fill[0], a.fill[1], { timeout: 5000 });
  else if (a.go) await page.evaluate(([id, params]) => T.go(id, params || {}), [a.go, a.params || null]);
  else if (a.eval) await page.evaluate(a.eval, a.arg);
  else throw new Error(`unknown action: ${JSON.stringify(a)}`);
  await page.evaluate(() => window.__fixtureClock.advance(0));
  await page.waitForTimeout(50);
  await page.evaluate(() => window.__fixtureClock.advance(0));
}

// ---------------------------------------------------------------------------------------------
// One cell: open, act, capture, check.
export async function renderCell(browser, cell, env) {
  const t0 = Date.now();
  const scenario = loadScenario(cell.case.scenario);
  const { context, page, errors, net } = await openCell(browser, { distDir: env.distDir, scenario, lang: cell.lang, size: cell.size, page: cell.case.page, origin: env.origin || null });
  const failures = [];
  const result = { case: cell.case.name, file: cell.case.file, scenario: typeof cell.case.scenario === 'string' ? cell.case.scenario : scenario.name, page: cell.case.page, lang: cell.lang, size: cell.size };
  try {
    if (cell.case.params) await runAction(page, { go: cell.case.page, params: cell.case.params });
    for (const a of cell.case.actions || []) await runAction(page, a);
    if ((cell.case.actions || []).length || cell.case.params) await settle(page, 0);

    const state = await page.evaluate(() => ({
      lang: window.T && T.i18n ? T.i18n.lang : null,
      html_lang: document.documentElement.lang,
      design: window.__TESSERACT_DESIGN__ ? { build: window.__TESSERACT_DESIGN__.build, fixture: window.__TESSERACT_DESIGN__.fixture, transport: window.__TESSERACT_DESIGN__.transport() } : null,
      clock_errors: window.__fixtureClock.errors.slice(),
      csp: (window.__cspViolations || []).slice(),
    }));
    const tree = await page.evaluate(textTreeInPage);
    const png = await page.screenshot({ type: 'png' });
    const ids = state.design && state.design.build.trace ? await page.evaluate(visibleIdsInPage) : null;
    const checks = await page.evaluate(cellChecksInPage, { pseudo: cell.lang === 'pseudo', whitelist: [...LEAK_WHITELIST, ...(cell.case.leak_allow || [])] });
    const before = errors.length;
    const fonts = env.fonts === false ? null : await fontChecks(context, page, cell.lang);
    // CSS.enable on a file: page makes Chrome log "Unsafe attempt to load URL file:…" for the
    // page itself: an artefact of the measurement, not of the page
    for (let i = errors.length - 1; i >= before; i--) if (/^Unsafe attempt to load URL file:/.test(errors[i].text)) errors.splice(i, 1);
    let metrics = null;
    const expectProblems = [];
    if (typeof cell.case.expect === 'function') {
      try {
        const r = await cell.case.expect({ page, context, tree, checks, lang: cell.lang, size: cell.size, scenario });
        if (r && Array.isArray(r.problems)) for (const p of r.problems) expectProblems.push(String(p));
        if (r && r.metrics) metrics = r.metrics;
      } catch (e) { expectProblems.push(String(e && e.message || e)); }
    }

    // ---- verdicts
    if (!state.design || !state.design.fixture) failures.push({ check: 'fixture', msg: 'page did not boot in fixture mode (boot/design.js hook missing?)' });
    if (state.lang !== cell.lang) failures.push({ check: 'lang', msg: `T.i18n.lang is ${state.lang}, expected ${cell.lang} (?lang → hello.lang)` });
    for (const e of errors) failures.push({ check: 'console', ...e });
    for (const e of state.clock_errors) failures.push({ check: 'console', kind: 'timer', text: String(e).slice(0, 500) });
    for (const f of (state.design && state.design.transport.failures) || []) failures.push({ check: 'fixture_invoke', ...f });
    for (const u of net) failures.push({ check: 'network', url: u });
    const allowRules = (cell.case.overflow_allow || []).map(a => ({ re: new RegExp(a.selector, 'u'), why: a.why }));
    const overflowAllowed = [], truncs = [];
    for (const t of checks.truncations) {
      const a = allowRules.find(r => r.re.test(t.selector));
      if (a) overflowAllowed.push({ ...t, why: a.why }); else truncs.push(t);
    }
    // product languages other than zh_CN: reported to i18n_check (truncation.json), not failed here
    const truncFails = TRUNCATION_FAILS.includes(cell.lang);
    if (truncFails) for (const t of truncs) failures.push({ check: 'overflow', ...t });
    for (const k of checks.host_scroll_x) failures.push({ check: 'host_scroll', element: k, ...checks.scroll[k] });
    const leaks = classifyLeaks(checks.leaks, cell.case.page, env.pending, { leakPolicy: env.leaks });
    for (const l of leaks.failing) failures.push({ check: 'leak', ...l });
    for (const i of checks.injection) failures.push({ check: 'inject', ...i });
    // garbage text (NaN / undefined / [object Object]) fails every cell (T-P4-07, 决定_第八波裁定);
    // a case's garbage_allow [{selector, why}] moves matching hits out of the verdict
    const garbageRules = (cell.case.garbage_allow || []).map(a => ({ re: new RegExp(a.selector, 'u'), why: a.why }));
    const garbageAllowed = [];
    for (const g of checks.garbage) {
      const a = garbageRules.find(r => r.re.test(g.selector));
      if (a) garbageAllowed.push({ ...g, why: a.why }); else failures.push({ check: 'garbage', ...g, msg: `${g.attr ? g.attr + ': ' : ''}${g.text}` });
    }
    if (env.csp) for (const v of state.csp) failures.push({ check: 'csp', ...v, msg: `${v.directive} ${v.blocked}${v.target ? ' at ' + v.target : ''}${v.sample ? ' «' + v.sample + '»' : ''}${v.source ? ` (${v.source}:${v.line})` : ''}` });
    if (fonts) for (const f of fonts.failures) failures.push({ check: 'font', ...f });
    for (const p of expectProblems) failures.push({ check: 'expect', msg: p });

    // ---- files
    const dir = join(env.outDir, cell.case.file === cell.case.name ? cell.case.name : `${cell.case.file}.${cell.case.name}`, cell.lang, cell.size);
    mkdirSync(dir, { recursive: true });
    writeFileSync(join(dir, 'screenshot.png'), png);
    writeFileSync(join(dir, 'textree.json'), serialize(tree));
    if (ids) writeFileSync(join(dir, 'ids.json'), JSON.stringify({ lang: cell.lang, page: cell.case.page, case: cell.case.name, size: cell.size, screenshot: 'screenshot.png', ids }, null, 1) + '\n');
    const detail = { ...result, state: { lang: state.lang, html_lang: state.html_lang, build: state.design && state.design.build, invocations: state.design ? state.design.transport.invocations.length : null },
      checks: { ...checks, truncations: truncs, overflow_allowed: overflowAllowed, garbage_allowed: garbageAllowed, truncation_verdict: truncFails ? 'fail' : 'report', leaks: undefined, leaks_failing: leaks.failing, leaks_pending: leaks.pending }, fonts, csp: env.csp ? state.csp : undefined, metrics, failures };
    writeFileSync(join(dir, 'checks.json'), JSON.stringify(detail, null, 1) + '\n');
    Object.assign(result, {
      ok: failures.length === 0,
      ms: Date.now() - t0,
      counts: { rows: tree.length, truncations: truncs.length, truncations_report_only: truncFails ? 0 : truncs.length, overflow_allowed: overflowAllowed.length, ellipsized: checks.ellipsized.length, scrollable_x: checks.scrollable_x.length, leaks_failing: leaks.failing.length, leaks_pending: leaks.pending.length, injection: checks.injection.length, garbage: checks.garbage.length - garbageAllowed.length, garbage_allowed: garbageAllowed.length, ids: ids ? ids.length : null, font_checked: fonts ? fonts.checked : null, font_scope: fonts ? fonts.scope : null, csp: env.csp ? state.csp.length : null },
      failures: failures.map(f => ({ check: f.check, selector: f.selector, msg: f.msg || f.text || f.rule || f.url || f.element || f.kind })),
      screenshot: posix(relative(REPO, join(dir, 'screenshot.png'))),
      dir: posix(relative(REPO, dir)),
      metrics,
      truncations: truncs.map(t => ({ selector: t.selector, id: t.id, text: t.text, by: t.by })),
      _tree: tree, _png: png,
    });
  } finally {
    await context.close();
  }
  return result;
}

// ---------------------------------------------------------------------------------------------
// summary.font_scope: cells by font rule scope (renderCell counts.font_scope), with the cells that
// fell back to the kana rule
export function fontScope(cells) {
  const out = { i18n_trace: 0, kana_fallback: 0, cyrillic: 0, none: 0, fallback_cells: [] };
  for (const c of cells) {
    const k = (c.counts && c.counts.font_scope) || 'none';
    out[k] = (out[k] || 0) + 1;
    if (k === 'kana_fallback' && out.fallback_cells.length < 20) out.fallback_cells.push(`${c.case} ${c.lang} ${c.size}`);
  }
  return out;
}

export function runName(o) {
  if (o.run) { if (!/^[A-Za-z0-9_.-]+$/.test(o.run)) throw new Error('--run must be [A-Za-z0-9_.-]+'); return o.run; }
  const d = new Date();
  const p = n => String(n).padStart(2, '0');
  // the process id keeps two runs started in the same second apart
  return `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}-${process.pid}-${(o.cases || ['all']).join('_').replace(/[^A-Za-z0-9_]/g, '')}`;
}

// truncation.json for tools/i18n_check.mjs (`truncation` check; allow list tools/i18n_allow.json
// matches on id / lang / selector / page): {langs, items: [{lang, page, case, size, selector, id,
// text}], coverage}. Only the 9 product languages; pseudo truncations fail their own cells
// instead. coverage says what the run covered: complete is true only for an unfiltered run
// (every case, all 9 languages, both sizes); i18n_check / T-P4-12 should only trust a complete one.
export function truncationDoc(cells, langsRun, coverage = null) {
  const langs = ALL_LANGS.filter(l => langsRun.includes(l));
  const items = [];
  for (const c of cells) if (langs.includes(c.lang)) for (const t of c.truncations || []) items.push({ lang: c.lang, page: c.page, case: c.case, size: c.size, selector: t.selector, id: t.id, text: t.text });
  return { langs, items, coverage };
}

/** Snapshot the selected build; a private --build-flags build is removed once copied. */
export function selectBuild(o) {
  const built = prepareDist(o);
  try { return { ...snapshotDist(built.dir), distDir: built.dir, temp: built.temp, flags: built.flags }; }
  finally { if (built.temp) rmSync(built.dir, { recursive: true, force: true }); }
}

export function missingLangsError(missing, available, o) {
  const how = o.dist
    ? `rebuild ${o.dist} with --pseudo / --i18n-trace, or leave ${missing.join(', ')} out with --langs`
    : 'build with: npm --prefix ui run build -- --pseudo --i18n-trace, or leave it out with --langs';
  return new Error(`language ${missing.join(', ')} is not in the build (${[...available].join(', ')}); ${how}`);
}

// The @i18n-pending scan reads the working tree while the cells render a build snapshot; the
// summary records which source state the scan saw and whether it is the one that was built.
async function pendingState(manifest) {
  const pending = pendingFiles(UI);
  let digest = null;
  try { digest = (await import(pathToFileURL(join(UI, 'build.mjs')).href)).sourceDigest(UI); } catch (e) { /* recorded as null */ }
  return { pending, digest, matches_build: digest !== null && digest === manifest.source_digest };
}

export async function runMatrix(o) {
  const snap = selectBuild(o);
  const manifest = snap.manifest;
  const available = new Set(Object.keys(manifest.locales || {}));
  const cases = await loadCases(o).catch(e => { rmSync(snap.dir, { recursive: true, force: true }); throw e; });
  const missing = o.langs.filter(l => !available.has(l));
  if (missing.length || !cases.length) {
    rmSync(snap.dir, { recursive: true, force: true });
    if (missing.length) throw missingLangsError(missing, available, o);
    throw new Error(`no case matches --case ${o.cases || '*'} --page ${o.pages || '*'}`);
  }
  const name = runName(o);
  const outDir = o.out ? resolve(o.out) : join(EVIDENCE, name);
  mkdirSync(outDir, { recursive: true });
  let server = null;
  try { if (o.csp) server = await cspServer(snap.dir); }
  catch (e) { rmSync(snap.dir, { recursive: true, force: true }); throw e; }
  const pend = await pendingState(manifest);
  const env = { distDir: snap.dir, outDir, pending: pend.pending, leaks: o.leaks, csp: !!o.csp, origin: server ? server.origin : null };
  // the cells in matrix order; --jobs n renders them n at a time (one browser per worker)
  const todo = [];
  for (const c of cases) {
    const langs = c.langs ? o.langs.filter(l => c.langs.includes(l)) : o.langs;
    const sizes = c.sizes ? o.sizes.filter(s => c.sizes.includes(s)) : o.sizes;
    for (const lang of langs) for (const size of sizes) todo.push({ c, lang, size });
  }
  const slots = new Array(todo.length);
  let next = 0;
  const t0 = Date.now();
  const worker = async () => {
    const browser = await launch({ loopback: !!server });
    try {
      while (next < todo.length) {
        const i = next++;
        const { c, lang, size } = todo[i];
        const first = await renderCell(browser, { case: c, lang, size }, env);
        for (let r = 1; r < o.repeat; r++) {
          const again = await renderCell(browser, { case: c, lang, size }, { ...env, outDir: join(outDir, `_repeat${r}`) });
          const sameTree = JSON.stringify(again._tree) === JSON.stringify(first._tree);
          const samePng = again._png.equals(first._png);
          if (!sameTree || !samePng) { first.ok = false; first.failures.push({ check: 'stability', msg: `repeat ${r}: textree ${sameTree ? 'same' : 'differs'}, png ${samePng ? 'same' : 'differs'}` }); }
        }
        delete first._tree; delete first._png;
        if (c.known_fail) first.known_fail = { owner: c.known_fail.owner || null, why: c.known_fail.why || '' };
        slots[i] = first;
        const tag = first.ok ? (c.known_fail ? 'ok? ' : 'ok  ') : (c.known_fail ? 'KNOWN' : 'FAIL');
        console.log(`${tag} ${first.case} ${lang} ${size} (${first.ms} ms)${first.ok ? '' : ' ' + first.failures.slice(0, 3).map(f => `${f.check}:${f.msg || f.selector}`).join(' | ')}`);
      }
    } catch (e) { next = todo.length; throw e; } // a broken cell stops the run, as in a serial run
    finally { await browser.close(); }
  };
  try {
    await Promise.all(Array.from({ length: Math.min(o.jobs || 1, Math.max(1, todo.length)) }, worker));
  } finally {
    if (server) await server.close();
    rmSync(snap.dir, { recursive: true, force: true });
  }
  const cells = slots.filter(Boolean);
  const total = Date.now() - t0;
  const counted = cells.filter(c => !c.known_fail);
  const cspViolations = cells.reduce((n, c) => n + (c.counts ? c.counts.csp || 0 : 0), 0);
  const unfiltered = !o.cases && !o.pages && ALL_LANGS.every(l => o.langs.includes(l)) && SIZES.every(z => o.sizes.includes(z));
  const coverage = {
    cases: cases.map(c => `${c.file}/${c.name}`), pages: [...new Set(cases.map(c => c.page))].sort(), langs: o.langs, sizes: o.sizes,
    source_digest: manifest.source_digest, h_features: manifest.h_features || null, build_flags: snap.flags ? snap.flags.join(' ') : null, complete: unfiltered,
  };
  const summary = {
    run: name,
    build: { dist: snap.temp ? `(private build: ${snap.flags.join(' ')})` : posix(relative(REPO, snap.distDir)), source_digest: manifest.source_digest, build_flags: o.buildFlags, build_flags_effective: snap.flags, locales: [...available].sort(), h_features: manifest.h_features || null, csp: o.csp },
    // --csp strict: the policy, the loopback server's non-2xx answers (a missing asset is a cell
    // failure as well: console / network) and the violations of all cells
    csp: server ? { policy: server.policy, origin: server.origin, requests: server.log.length, non_2xx: server.log.filter(r => r.status >= 300).slice(0, 50), violations: cspViolations,
      known_fail_cells_with_violations: cells.filter(c => c.known_fail && c.counts && c.counts.csp).map(c => `${c.case} ${c.lang} ${c.size}`) } : null,
    matrix: { cases: cases.map(c => `${c.file}/${c.name}`), langs: o.langs, sizes: o.sizes, repeat: o.repeat, leaks: o.leaks, jobs: o.jobs || 1, truncation_fails: TRUNCATION_FAILS, complete: unfiltered },
    // (T-P5-07, T-P5-03 S5) which font rule each cell ran: i18n_trace (ja_JP, the --i18n-trace scope),
    // kana_fallback (ja_JP without a trace build: the old kana rule), cyrillic (ru_RU), none (no font
    // rule for the language, or a cell rendered with fonts: false); the cells that fell back are
    // listed (first 20) and named on the console
    font_scope: fontScope(cells),
    pending_files: [...env.pending].sort(),
    pending_scan: { source_digest: pend.digest, matches_build: pend.matches_build },
    // (fix round 1: a CSP violation fails the run even in a known_fail cell)
    ok: counted.every(c => c.ok) && cspViolations === 0,
    cells: cells.length,
    failed: counted.filter(c => !c.ok).map(c => ({ cell: `${c.case} ${c.lang} ${c.size}`, screenshot: c.screenshot, failures: c.failures.slice(0, 20) })),
    known_failures: cells.filter(c => c.known_fail && !c.ok).map(c => ({ cell: `${c.case} ${c.lang} ${c.size}`, ...c.known_fail, screenshot: c.screenshot, failures: c.failures.slice(0, 20) })),
    known_fail_passed: cells.filter(c => c.known_fail && c.ok).map(c => `${c.case} ${c.lang} ${c.size}`),
    total_ms: total,
    avg_ms_per_cell: cells.length ? Math.round(total / cells.length) : 0,
    results: cells,
  };
  if (summary.font_scope.kana_fallback) console.log(`note: ${summary.font_scope.kana_fallback} ja_JP cell(s) ran the font rule's kana fallback (no --i18n-trace build): ${summary.font_scope.fallback_cells.slice(0, 3).join(' | ')}`);
  if (!pend.matches_build) console.log(`note: the @i18n-pending scan read a working tree (${pend.digest ? pend.digest.slice(0, 12) : '?'}) that is not the rendered build (${String(manifest.source_digest).slice(0, 12)})`);
  writeFileSync(join(outDir, 'summary.json'), JSON.stringify(summary, null, 1) + '\n');
  if (o.langsAll) writeFileSync(join(outDir, 'truncation.json'), JSON.stringify(truncationDoc(cells, o.langs, coverage), null, 1) + '\n');
  const known = (summary.known_failures.length ? `, ${summary.known_failures.length} known failure(s) not counted` : '')
    + (summary.csp && summary.csp.known_fail_cells_with_violations.length ? `, CSP violations in known_fail cell(s) still fail the run: ${summary.csp.known_fail_cells_with_violations.slice(0, 3).join(' | ')}` : '');
  console.log(`${summary.ok ? 'render: OK' : 'render: FAILED'} — ${cells.length} cell(s), ${summary.failed.length} failed${known}, avg ${summary.avg_ms_per_cell} ms/cell → ${posix(relative(REPO, outDir))}/summary.json`);
  return summary;
}

async function main(argv) {
  const o = parseArgs(argv);
  if (o.selfTest) {
    const { selfTest } = await import('./selftest.mjs');
    return (await selfTest(o)) ? 0 : 1;
  }
  const s = await runMatrix(o);
  return s.ok ? 0 : 1;
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  main(process.argv.slice(2)).then(code => { process.exitCode = code; }, e => { console.error(e && e.stack || e); process.exitCode = 1; });
}
