// T-P4-08b: the shell's CSP in the debug shell's WebView2 (P4 architecture §9; ticket requirement 4).
//
//   node ui/test/e2e/csp.mjs [--build] [--evidence <dir>] [--timeout <s>] [--keep]
//
// Why a second check besides the render matrix's --csp strict run: Tauri rewrites the CSP it serves
// (it adds hashes / nonces for its own scripts), so "the page looks fine" proves nothing; this run
// counts the violations the real shell reports while the page goes through its main states.
//
// What it does (the T-P4-11 environment: debug shell, stand-in sidecar, custom scope):
//   - --build: scripts/shell_copy_ui.ps1, then `cargo build --offline` of the debug shell with
//     the job count / priority of evidence/CPU_约定.txt (mode=timing forces -j 2 BelowNormal), the
//     UTC start / end appended to evidence/T-P4-08b/cpu-windows.txt. Without --build the debug exe
//     must be newer than shell/app/tauri.conf.json and shell/app/ui/build-manifest.json, and that
//     manifest's source_digest must be the working tree's (ui/build.mjs sourceDigest: ui/src,
//     ui/css, the build files), so a UI change not yet copied into the shell asks for --build.
//   - one sandbox under %TEMP% (removed at the end unless --keep) with its own custom scope: TESSERACT_INSTANCE_SCOPE,
//     TESSERACT_CONFIG_DIR, TESSERACT_DATA_DIR, TEMP / TMP, APPDATA / LOCALAPPDATA all inside it
//     (refused otherwise, or when one overlaps the live %APPDATA%\Tesseract /
//     %LOCALAPPDATA%\Tesseract); no proxy variables; the isolated example config as
//     scripts/p4_ui_e2e.ps1 writes it. The default scope's mutex / event names are never used.
//   - the debug shell under shell/app/process_watch.py with TESSERACT_SHELL_CDP_PORT (a free
//     loopback port) and --dev-sidecar-script shell/app/src/p4_ui_fixture.py (T-P4-11's stand-in);
//     playwright-core connects over CDP (ui/test/e2e/driver.mjs, T-P4-11).
//   - three violation channels, registered before a reload so the page is watched from its first
//     frame: a `securitypolicyviolation` listener (Page.addScriptToEvaluateOnNewDocument), CDP
//     Audits.issueAdded (ContentSecurityPolicyIssue) and CDP Log.entryAdded (CSP messages). The
//     main document's served Content-Security-Policy header is read from CDP Network.
//   - the walk (each step labelled; a screenshot each): 总览 running, the command palette, an
//     in-app toast and a warning banner, service stopped and running again, 层级 with a tier panel open and
//     closed, the list-stale banner, a foreign stop-job failure banner, 运行日志 with error records
//     and the reader open, 配置 with a draft (save bar), the 提醒 section with a channel drawer, the
//     language switched to en_US and back.
//   - a positive control at the end: setAttribute('style', …) on a span must be blocked (not
//     applied) and reported exactly once on each of the three channels (proves the collector, the
//     CDP channels and the policy work in WebView2); a channel that could not be enabled fails the
//     run. The walk and the control split at one instant (controlStart): everything before it is
//     the walk's, everything after it the control's, so a late walk report either fails the walk
//     or makes the control's count wrong; nothing falls between the two.
//   - tray Quit (native.py tray_quit), the shell's exit, process_watch's inventory (no owned process
//     left), the stand-in's pids gone. A failure is cleaned up the same way (tray Quit, then only this
//     scope's shell is stopped); every step goes into result.json.
// Verdict: 0 violations on every channel during the walk, every step done, the served policy without
// 'unsafe-inline' anywhere, its style-src exactly 'self' and no hash / nonce in any style-src*
// directive (Tauri adds hashes to what it serves, P4 architecture §9), the control blocked and
// reported once per channel, no residue.
// Evidence: .build/<work orders>/evidence/T-P4-08b/e2e-csp-<stamp>/ (result.json, screenshots,
// shell.log, the process inventory); never raw frames. Exit 0 when it passed.
import { spawn, spawnSync } from 'node:child_process';
import { copyFileSync, existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync, appendFileSync } from 'node:fs';
import { createServer } from 'node:net';
import { tmpdir } from 'node:os';
import { join, relative, resolve, isAbsolute } from 'node:path';
import { randomBytes } from 'node:crypto';
import { pathToFileURL } from 'node:url';
import { REPO, openSession, waitFor, sleep, goPage, settingsReady, makeDraft, undoDrafts, writeJson, Failure, check } from './driver.mjs';

const ORDERS = readdirSync(join(REPO, '.build'), { withFileTypes: true }).filter(d => d.isDirectory())
  .map(d => join(REPO, '.build', d.name)).find(d => existsSync(join(d, 'T-P4-08b.md')));
if (!ORDERS) throw new Error('work-order directory not found');
const EVIDENCE_ROOT = join(ORDERS, 'evidence', 'T-P4-08b');
const SHELL_EXE = join(REPO, 'shell', 'target', 'debug', 'tesseract-shell.exe');
const FIXTURE = join(REPO, 'shell', 'app', 'src', 'p4_ui_fixture.py');
const WATCH = join(REPO, 'shell', 'app', 'process_watch.py');
const NATIVE = join(REPO, 'ui', 'test', 'e2e', 'native.py');
const PYTHON = join(REPO, '.venv', 'Scripts', 'python.exe');
const CONF = join(REPO, 'shell', 'app', 'tauri.conf.json');
const MANIFEST = join(REPO, 'shell', 'app', 'ui', 'build-manifest.json');
const UI = join(REPO, 'ui');
const SCOPE_VARS = ['TESSERACT_INSTANCE_SCOPE', 'TESSERACT_CONFIG_DIR', 'TESSERACT_DATA_DIR', 'TEMP', 'TMP', 'APPDATA', 'LOCALAPPDATA',
  'TESSERACT_SHELL_CDP_PORT', 'TESSERACT_ISOLATED_AUDIT_MANIFEST', 'TESSERACT_REDUCED_MOTION', 'TESSERACT_SHELL_DEV_READY_TIMEOUT_MS',
  'TESSERACT_SHELL_DEV_WEBVIEW2_MISSING', 'TESSERACT_SHELL_DEV_RECREATE_FAIL', 'TESSERACT_SHELL_DEV_EXIT_WAIT_IGNORE_EXITED', 'TESSERACT_SHELL_DEV_MARK_QUITTING',
  'PYTHONDONTWRITEBYTECODE', 'PYTHONPYCACHEPREFIX', 'PYTHONPATH', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY', 'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy'];

// In the page, from its first frame: every securitypolicyviolation, labelled with the walk step.
const CSP_INIT = `(() => {
  if (window.__csp) return;
  const rec = window.__csp = { step: 'load', list: [] };
  const where = t => !t ? null : t.nodeType === 9 ? 'document' : t.nodeType !== 1 ? null
    : t.tagName.toLowerCase() + (t.id ? '#' + t.id : '') + (typeof t.className === 'string' && t.className.trim() ? '.' + t.className.trim().split(/\\s+/).slice(0, 3).join('.') : '');
  document.addEventListener('securitypolicyviolation', e => {
    rec.list.push({ step: rec.step, directive: e.violatedDirective, effective: e.effectiveDirective, blocked: String(e.blockedURI || ''),
      sample: String(e.sample || '').slice(0, 80), source: String(e.sourceFile || '').slice(-80), line: e.lineNumber, target: where(e.target) });
  }, true);
})();`;

function parseArgs(argv) {
  const o = { build: false, evidence: null, timeout: 600, keep: false };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--build') o.build = true;
    else if (a === '--evidence') o.evidence = argv[++i];
    else if (a === '--timeout') o.timeout = Number(argv[++i]) || 600;
    else if (a === '--keep') o.keep = true;
    else throw new Error(`unknown argument: ${a}`);
  }
  return o;
}

const stamp = () => new Date().toISOString().replace(/[-:]/g, '').replace(/\..*$/, '').replace('T', '-');
const under = (p, root) => { const r = relative(resolve(root), resolve(p)); return r === '' || (!r.startsWith('..') && !isAbsolute(r)); };
const freePort = () => new Promise((ok, bad) => { const s = createServer(); s.once('error', bad); s.listen(0, '127.0.0.1', () => { const { port } = s.address(); s.close(() => ok(port)); }); });
const alive = pid => { try { process.kill(pid, 0); return true; } catch (e) { return e.code === 'EPERM'; } };

// ---------------------------------------------------------------------------------- build
function cpuConvention() {
  const conv = { mode: 'normal', cargo_jobs: '1', cargo_priority: 'BelowNormal', file: null };
  const dir = join(ORDERS, 'evidence');
  const f = readdirSync(dir).find(n => /^CPU_.*\.txt$/.test(n));
  if (f) {
    conv.file = f;
    for (const line of readFileSync(join(dir, f), 'utf8').split(/\r?\n/)) {
      const m = /^\s*(mode|cargo_jobs|cargo_priority)\s*=\s*(\S+)/.exec(line);
      if (m) conv[m[1]] = m[2];
    }
  }
  if (conv.mode === 'timing') { conv.cargo_jobs = '2'; conv.cargo_priority = 'BelowNormal'; }
  if (!/^\d+$/.test(conv.cargo_jobs) || !/^(Idle|BelowNormal|Normal|AboveNormal)$/.test(conv.cargo_priority)) throw new Error(`bad CPU convention: ${JSON.stringify(conv)}`);
  return conv;
}
function build(result) {
  const copy = spawnSync('powershell.exe', ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', join(REPO, 'scripts', 'shell_copy_ui.ps1')], { cwd: REPO, encoding: 'utf8' });
  result.build = { copy_ui: copy.status };
  if (copy.status !== 0) throw new Error(`shell_copy_ui.ps1 failed: ${(copy.stderr || copy.stdout || '').slice(-800)}`);
  const conv = cpuConvention();
  const log = join(EVIDENCE_ROOT, 'cpu-windows.txt');
  mkdirSync(EVIDENCE_ROOT, { recursive: true });
  appendFileSync(log, `T-P4-08b csp.mjs cargo build --offline -j ${conv.cargo_jobs} priority=${conv.cargo_priority} mode=${conv.mode} start ${new Date().toISOString()}\n`);
  // (PowerShell sets the priority class, as scripts/p4_ui_e2e.ps1 does)
  const ps = `$p = Start-Process -FilePath cargo -ArgumentList @('build','--offline','-j','${conv.cargo_jobs}') -WorkingDirectory '${join(REPO, 'shell').replace(/'/g, "''")}' -NoNewWindow -PassThru; $null = $p.Handle; try { $p.PriorityClass = [Diagnostics.ProcessPriorityClass]'${conv.cargo_priority}' } catch { }; $p.WaitForExit(); exit $p.ExitCode`;
  const cargo = spawnSync('powershell.exe', ['-NoProfile', '-Command', ps], { cwd: REPO, encoding: 'utf8', maxBuffer: 64 * 1024 * 1024 });
  appendFileSync(log, `T-P4-08b csp.mjs cargo build end ${new Date().toISOString()} exit=${cargo.status}\n`);
  result.build.cargo = { exit: cargo.status, jobs: conv.cargo_jobs, priority: conv.cargo_priority, mode: conv.mode };
  if (cargo.status !== 0) throw new Error(`cargo build failed: ${(cargo.stderr || cargo.stdout || '').slice(-1500)}`);
}

// ---------------------------------------------------------------------------------- sandbox
function sandbox() {
  const box = mkdtempSync(join(tmpdir(), 'tesseract-p4csp-'));
  const s = { box, config: join(box, 'config'), data: join(box, 'data'), temp: join(box, 'temp'), appdata: join(box, 'appdata'), localappdata: join(box, 'localappdata'),
    scope: 'p4csp-' + randomBytes(5).toString('hex'), launch: join(box, 'launch-0') };
  for (const d of [s.config, s.data, s.temp, s.appdata, s.localappdata, s.launch, join(s.data, 'p4-ui-fixture')]) mkdirSync(d, { recursive: true });
  // the isolated example config, as scripts/p4_ui_e2e.ps1 writes it (the stand-in reads none)
  const py = spawnSync(PYTHON, ['-B', '-c', `
import sys, pathlib, yaml
repo, config = map(pathlib.Path, sys.argv[1:])
c = yaml.safe_load((repo/'config.example.yaml').read_text('utf-8'))
c['tiers']['tier3']['enabled'] = False
for t in ('tier1','tier2'):
    c['tiers'][t]['nodes'] = []
    c['tiers'][t]['subscription_urls'] = []
c['client']['system_proxy']['enabled'] = False
c['alerts']['enabled'] = False
if isinstance(c.get('killswitch'), dict): c['killswitch']['enabled'] = False
(config/'config.yaml').write_bytes(yaml.safe_dump(c,allow_unicode=True).encode('utf-8'))
(config/'sources.yaml').write_bytes(b'migrations:\\n  tier3_source_portfolio_defaults_v1: true\\n  tier3_source_portfolio_defaults_v2: true\\n  tier3_source_default_delivery_v1: true\\ngithub_repos: []\\ntelegram_channels: []\\nsubscription_urls: []\\nlocal_files: []\\n')
`, REPO, s.config], { cwd: REPO, encoding: 'utf8' });
  if (py.status !== 0) throw new Error(`isolated config setup failed: ${py.stderr}`);
  writeFileSync(join(s.data, 'p4-ui-fixture', 'options.json'), JSON.stringify({ phase: 'running' }));
  return s;
}
function scopedEnv(s, port, liveDirs) {
  const env = { ...process.env };
  for (const k of Object.keys(env)) if (SCOPE_VARS.includes(k) || SCOPE_VARS.includes(k.toUpperCase())) delete env[k];
  Object.assign(env, { TESSERACT_INSTANCE_SCOPE: s.scope, TESSERACT_CONFIG_DIR: s.config, TESSERACT_DATA_DIR: s.data, TEMP: s.temp, TMP: s.temp,
    APPDATA: s.appdata, LOCALAPPDATA: s.localappdata, TESSERACT_SHELL_CDP_PORT: String(port), PYTHONDONTWRITEBYTECODE: '1' });
  // refuses a launch that is not fully inside this sandbox's custom scope
  for (const k of ['TESSERACT_INSTANCE_SCOPE', 'TESSERACT_CONFIG_DIR', 'TESSERACT_DATA_DIR', 'TEMP', 'TMP', 'APPDATA', 'LOCALAPPDATA']) if (!env[k]) throw new Error(`refusing to launch: ${k} is not set`);
  if (/^(default)?$/i.test(env.TESSERACT_INSTANCE_SCOPE) || env.TESSERACT_INSTANCE_SCOPE !== s.scope) throw new Error('refusing to launch: not this run\'s custom scope');
  for (const k of ['TESSERACT_CONFIG_DIR', 'TESSERACT_DATA_DIR', 'TEMP', 'TMP', 'APPDATA', 'LOCALAPPDATA']) {
    if (!under(env[k], s.box)) throw new Error(`refusing to launch: ${k}=${env[k]} is outside the sandbox`);
    for (const live of liveDirs) if (under(env[k], live) || under(live, env[k])) throw new Error(`refusing to launch: ${k} overlaps the live profile ${live}`);
  }
  return env;
}

// ---------------------------------------------------------------------------------- the walk
async function step(run, name, fn) {
  const t0 = Date.now();
  const entry = { name, ok: false, error: null, shot: null, violations: 0 };
  run.steps.push(entry);
  const before = await run.s.eval(n => { window.__csp.step = n; return window.__csp.list.length; }, name);
  try {
    await fn();
    await sleep(600);
    entry.ok = true;
  } catch (e) {
    entry.error = e instanceof Failure ? e.message : String(e && e.message || e);
  }
  entry.shot = await run.s.shot(name);
  entry.violations = await run.s.eval(n => window.__csp.list.length - n, before);
  entry.ms = Date.now() - t0;
  console.log(`${entry.ok ? 'ok  ' : 'FAIL'} ${name} (${entry.ms} ms, ${entry.violations} violation(s))${entry.error ? ' ' + entry.error : ''}`);
}

async function walk(run) {
  const s = run.s;
  const visible = sel => s.until(q => { const e = document.querySelector(q); return !!(e && e.checkVisibility({ visibilityProperty: true, opacityProperty: true })); }, `visible ${sel}`, 10000, sel);
  await step(run, 'overview-running', async () => {
    await goPage(s, 'overview');
    await s.until(() => document.getElementById('app').dataset.run === 'running', 'run=running', 15000);
  });
  await step(run, 'palette', async () => {
    await s.page.keyboard.press('Control+K');
    await visible('.cmdk .cmdk-panel');
    await s.page.keyboard.type('log');
    await sleep(300);
    await s.shot('palette-open');
    await s.page.keyboard.press('Escape');
    await s.until(() => { const e = document.querySelector('.cmdk'); return !e || e.hidden || !e.checkVisibility(); }, 'palette closed', 5000);
  });
  // (the page's own notice builders, T.notify -> toast / banner, which host:notify uses too: a
  // host:notify from the stand-in would go to a real Windows balloon while the test window is not
  // in front, a desktop side effect this check does not need)
  await step(run, 'toast-and-banner', async () => {
    await s.eval(() => {
      window.T.notify({ kind: 'info', title: 'CSP check', message: 'an in-app notice', data: true, actions: [{ label: 'OK', run: () => {} }] });
      window.T.notify({ kind: 'warning', id: 'csp-check', title: 'CSP check', message: 'a warning banner', data: true });
    });
    await s.until(() => !!document.querySelector('#toast-slot .toast') && window.T.notices.banners.has('csp-check'), 'a toast and a banner', 10000);
    await sleep(800);
    await s.eval(() => window.T.dismiss('csp-check'));
  });
  await step(run, 'service-stopped', async () => {
    await s.control({ op: 'service', phase: 'stopped' });
    await s.until(() => document.getElementById('app').dataset.run === 'stopped', 'run=stopped', 10000);
  });
  await step(run, 'service-running', async () => {
    await s.control({ op: 'service', phase: 'running' });
    await s.until(() => document.getElementById('app').dataset.run === 'running', 'run=running again', 10000);
  });
  await step(run, 'tiers', async () => {
    await goPage(s, 'profiles');
    await sleep(500);
  });
  await step(run, 'tiers-panel', async () => {
    // (a node row opens its panel: pages/tiers.js openPanel)
    await s.until(() => Array.from(document.querySelectorAll('.tiers-row[data-key]')).some(r => r.checkVisibility({ visibilityProperty: true, opacityProperty: true })), 'a visible tier row', 10000);
    await s.eval(() => { const r = Array.from(document.querySelectorAll('.tiers-row[data-key]')).find(x => x.checkVisibility({ visibilityProperty: true, opacityProperty: true })); r.setAttribute('data-csp-row', 'tier'); });
    await s.page.click('[data-csp-row="tier"]');
    await visible('.tiers-panel');
    await sleep(500);
    await s.shot('tiers-panel-open');
    await s.page.keyboard.press('Escape');
    await s.until(() => { const p = document.querySelector('.tiers-panel'); return !p || !p.checkVisibility(); }, 'tier panel closed', 5000);
  });
  await step(run, 'list-stale-banner', async () => {
    await s.control({ op: 'stale', topic: 'nodes.list', key: 'sentinel', fails: 3 });
    await s.until(() => window.T.notices.banners.has('list-stale'), 'the list-stale banner', 15000);
  });
  await step(run, 'stop-failed-banner', async () => {
    await s.control({ op: 'job', name: 'service.stop', state: 'failed', owner: 'tray', req_id: 'csp-stop-1',
      error: { code: 'lifecycle_busy', message_key: 'errors.bridge.lifecycle_busy', params: {} } });
    await s.until(() => window.T.notices.banners.has('stop'), 'the stop-failure banner', 10000);
  });
  await step(run, 'logs', async () => {
    await goPage(s, 'logs');
    await s.control({ op: 'logs', n: 20 });
    await s.control({ op: 'logs', n: 3, level: 'error' });
    await s.until(() => Array.from(document.querySelectorAll('#logs-events .li[data-key]')).some(r => r.checkVisibility({ visibilityProperty: true, opacityProperty: true })), 'visible log rows', 15000);
  });
  await step(run, 'logs-reader', async () => {
    await s.eval(() => { const rows = Array.from(document.querySelectorAll('#logs-events .li[data-key]')).filter(r => !r.hidden && r.checkVisibility({ visibilityProperty: true, opacityProperty: true }) && r.getBoundingClientRect().height > 0); const r = rows[rows.length - 1]; if (r) r.setAttribute('data-csp-row', '1'); });
    await s.page.click('[data-csp-row="1"]');
    await s.until(() => { const p = document.querySelector('.logs-reader'); return !!(p && p.getBoundingClientRect().width > 0); }, 'the log reader', 10000);
    await sleep(600);
  });
  await step(run, 'settings-draft', async () => {
    await settingsReady(s);
    await makeDraft(s);
    await visible('.savebar');
  });
  await step(run, 'settings-alerts-drawer', async () => {
    await s.eval(() => window.T.go('settings', { focus: 'alerts' }));
    await s.until(() => !!document.querySelector('#set-ch-gal .set-ch'), 'the alert channel gallery', 10000);
    await s.eval(() => document.querySelector('#set-ch-gal .set-ch').click());
    await visible('.ovl .drawer');
    await sleep(400);
    await s.shot('alerts-drawer-open');
    await s.page.keyboard.press('Escape');
    await s.until(() => { const d = document.querySelector('.ovl .drawer'); return !d || !d.checkVisibility(); }, 'drawer closed', 5000);
    await undoDrafts(s);
  });
  await step(run, 'language-en', async () => {
    await s.control({ op: 'language', language: 'en_US' });
    await s.until(() => document.documentElement.lang === 'en-US' || (window.T.i18n && window.T.i18n.lang === 'en_US'), 'en_US', 10000);
    await goPage(s, 'overview');
  });
  await step(run, 'language-zh', async () => {
    await s.control({ op: 'language', language: 'zh_CN' });
    await s.until(() => window.T.i18n && window.T.i18n.lang === 'zh_CN', 'zh_CN', 10000);
  });
}

// ---------------------------------------------------------------------------------- one run
async function main() {
  const o = parseArgs(process.argv.slice(2));
  const result = { ticket: 'T-P4-08b', passed: false, error: null, started_at: new Date().toISOString(), steps: [], channels: {}, served_csp: null, control: null, cleanup: [] };
  const evidence = o.evidence ? resolve(o.evidence) : join(EVIDENCE_ROOT, `e2e-csp-${stamp()}`);
  mkdirSync(evidence, { recursive: true });
  const liveDirs = [process.env.APPDATA, process.env.LOCALAPPDATA].filter(Boolean).map(b => resolve(b, 'Tesseract'));
  let watcher = null, shellPid = null, s = null, box = null;
  const cdpSeen = { audits: [], log: [] };
  // the overall limit: this run's own shell is asked to quit (tray Quit), then stopped, then exit 3
  const deadline = setTimeout(() => {
    console.error(`csp.mjs: over ${o.timeout} s`);
    if (shellPid && alive(shellPid)) {
      spawnSync(PYTHON, ['-B', NATIVE, 'tray_quit', '--pid', String(shellPid)], { encoding: 'utf8', timeout: 20000 });
      const end = Date.now() + 15000; while (alive(shellPid) && Date.now() < end) Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 250);
      if (alive(shellPid)) spawnSync('taskkill', ['/PID', String(shellPid), '/T', '/F']);
    }
    if (box && !o.keep && !(shellPid && alive(shellPid))) { try { rmSync(box.box, { recursive: true, force: true, maxRetries: 3, retryDelay: 400 }); } catch (e) { /* left for the user */ } }
    process.exit(3);
  }, o.timeout * 1000);
  try {
    if (o.build) build(result);
    for (const f of [SHELL_EXE, FIXTURE, WATCH, NATIVE, PYTHON]) if (!existsSync(f)) throw new Error(`missing ${f}${f === SHELL_EXE ? ' (build it: node ui/test/e2e/csp.mjs --build)' : ''}`);
    const exeAt = statSync(SHELL_EXE).mtimeMs;
    const stale = [CONF, MANIFEST].filter(f => existsSync(f) && statSync(f).mtimeMs > exeAt);
    if (stale.length) throw new Error(`the debug shell is older than ${stale.map(f => relative(REPO, f)).join(', ')}: run with --build`);
    // (fix round 1: a UI source change not yet copied into shell/app/ui is stale too)
    const digest = (await import(pathToFileURL(join(UI, 'build.mjs')).href)).sourceDigest(UI);
    const embedded = existsSync(MANIFEST) ? JSON.parse(readFileSync(MANIFEST, 'utf8')).source_digest : null;
    result.source_digest = { working_tree: digest, embedded };
    if (embedded !== digest) throw new Error(`shell/app/ui was built from another UI source state (${String(embedded).slice(0, 12)} vs the working tree ${digest.slice(0, 12)}): run with --build`);
    result.policy_configured = JSON.parse(readFileSync(CONF, 'utf8')).app.security.csp;

    box = sandbox();
    result.sandbox = box.box; result.scope = box.scope;
    const port = await freePort();
    const env = scopedEnv(box, port, liveDirs);
    const out = n => join(box.launch, n);
    watcher = spawn(PYTHON, ['-B', WATCH, '--output', out('process-inventory.json'), '--ready', out('root.json'), '--', SHELL_EXE, '--dev-sidecar-script', FIXTURE],
      { cwd: REPO, env, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] });
    const wlog = [];
    watcher.stdout.on('data', d => wlog.push(String(d))); watcher.stderr.on('data', d => wlog.push(String(d)));
    let watcherExit = null; watcher.on('exit', c => { watcherExit = c; });
    await waitFor(() => existsSync(out('root.json')) || watcherExit !== null, 'process_watch started the shell', 30000);
    if (!existsSync(out('root.json'))) throw new Error(`process_watch failed: ${wlog.join('').slice(-800)}`);
    shellPid = Number(JSON.parse(readFileSync(out('root.json'), 'utf8')).pid);
    result.shell_pid = shellPid; result.cdp_port = port;
    const logFile = join(box.data, 'logs', 'shell.log');
    await waitFor(() => {
      const text = existsSync(logFile) ? readFileSync(logFile, 'utf8') : '';
      if (/ (sidecar_start_failed|hello_timeout|sidecar_exit_before_hello)/.test(text)) throw new Failure('sidecar failed before hello');
      return / shell_window_ready/.test(text) && / sidecar_ready /.test(text); // 11-05: the window opens before hello
    }, 'shell window ready', 60000, 200);
    await waitFor(() => readdirSync(join(box.data, 'p4-ui-fixture')).some(n => n.startsWith('ready-')), 'stand-in sidecar ready', 30000, 200);

    s = await openSession({ port, pid: shellPid, data: box.data, evidence, python: PYTHON });
    // the three channels, then a reload: watched from the first frame
    const cdp = await s.page.context().newCDPSession(s.page);
    const docs = [];
    cdp.on('Audits.issueAdded', ({ issue }) => { if (issue && issue.code === 'ContentSecurityPolicyIssue') cdpSeen.audits.push({ at: Date.now(), type: issue.details?.contentSecurityPolicyIssueDetails?.contentSecurityPolicyViolationType || null, directive: issue.details?.contentSecurityPolicyIssueDetails?.violatedDirective || null }); });
    cdp.on('Log.entryAdded', ({ entry }) => { if (entry && /Content Security Policy/i.test(entry.text || '')) cdpSeen.log.push({ at: Date.now(), source: entry.source, level: entry.level, text: String(entry.text).slice(0, 300) }); });
    cdp.on('Network.responseReceived', ({ type, response }) => { if (type === 'Document') docs.push({ url: response.url, status: response.status, headers: response.headers || {} }); });
    await cdp.send('Audits.enable').catch(e => { result.channels.audits_error = String(e.message); });
    await cdp.send('Log.enable').catch(e => { result.channels.log_error = String(e.message); });
    await cdp.send('Network.enable').catch(e => { result.channels.network_error = String(e.message); });
    await s.page.addInitScript(CSP_INIT);
    await s.reloadObserved();
    check(await s.eval(() => !!window.__csp), 'the CSP listener is not in the page after the reload');
    await s.until(() => window.__e2e.events.some(e => e.e === 'rt:replayed'), 'rt:replayed after the reload', 20000);
    const doc = docs.filter(d => /^https?:\/\/tauri\.localhost\//.test(d.url)).pop() || null;
    const header = doc ? Object.entries(doc.headers).find(([k]) => k.toLowerCase() === 'content-security-policy') : null;
    const meta = await s.eval(() => Array.from(document.querySelectorAll('meta[http-equiv="Content-Security-Policy" i]')).map(m => m.content));
    result.served_csp = { url: doc ? doc.url : null, header: header ? header[1] : null, meta };
    const served = [header ? header[1] : null, ...meta].filter(Boolean);
    check(served.length > 0, 'no served CSP seen (neither a header nor a <meta>)');
    for (const p of served) {
      const dirs = p.split(';').map(d => d.trim().split(/\s+/)).filter(d => d[0]);
      const style = dirs.find(d => d[0].toLowerCase() === 'style-src');
      // style-src exactly 'self'; no style-src* directive with a hash or a nonce (fix round 1: Tauri
      // hashes inline blocks of the HTML it serves, P4 architecture §9, which would hide one)
      check(!!style && style.length === 2 && style[1] === "'self'", `served style-src is not exactly 'self': ${p}`);
      check(!dirs.some(d => /^style-src/i.test(d[0]) && d.slice(1).some(v => /^'(sha(256|384|512)|nonce)-/i.test(v))), `served style-src* carries a hash or a nonce: ${p}`);
      check(!/'unsafe-inline'/i.test(p), `served CSP: ${p}`);
    }

    await walk({ s, steps: result.steps });
    result.walk_end = new Date().toISOString();
    // (a short quiet period lets a report of the last step arrive before the split)
    await sleep(500);

    // positive control: an inline style attribute in the live shell is blocked and reported.
    // controlStart splits every channel: the page tags its entries 'control' from this instant on
    // (the step label is set in the same task that plants the style), the CDP entries by arrival.
    const controlStart = Date.now();
    result.control = await s.eval(() => {
      window.__csp.step = 'control';
      const e = document.createElement('span'); e.id = '__csp_control'; e.textContent = 'x';
      e.setAttribute('style', 'color: rgb(255, 0, 0)');
      document.body.append(e);
      const applied = getComputedStyle(e).color === 'rgb(255, 0, 0)';
      return new Promise(r => setTimeout(() => { e.remove(); r({ applied }); }, 300));
    });
    await sleep(500);
    const pageAll = await s.eval(() => window.__csp.list.slice());
    const pageList = pageAll.filter(v => v.step !== 'control');
    const pageControl = pageAll.filter(v => v.step === 'control');
    result.violations = pageList;
    result.channels.event = pageList.length;
    result.channels.audits = cdpSeen.audits.filter(x => x.at < controlStart).length;
    result.channels.log = cdpSeen.log.filter(x => x.at < controlStart).length;
    result.channels.audits_list = cdpSeen.audits.slice(0, 20); result.channels.log_list = cdpSeen.log.slice(0, 20);
    Object.assign(result.control, { events: pageControl.length, directives: pageControl.map(x => x.directive),
      audits: cdpSeen.audits.filter(x => x.at >= controlStart).length, log: cdpSeen.log.filter(x => x.at >= controlStart).length });
    await cdp.detach().catch(() => {});

    const failedSteps = result.steps.filter(x => !x.ok).map(x => `${x.name}: ${x.error}`);
    check(!failedSteps.length, `steps failed: ${failedSteps.join(' | ')}`);
    check(result.channels.event === 0 && result.channels.audits === 0 && result.channels.log === 0,
      `CSP violations during the walk: event ${result.channels.event}, audits ${result.channels.audits}, log ${result.channels.log} (${JSON.stringify(pageList.slice(0, 5))})`);
    // (fix round 1: the CDP channels must have been enabled and must each report the control once,
    // or their walk zeros prove nothing)
    const chanErrors = ['audits_error', 'log_error', 'network_error'].filter(k => result.channels[k]);
    check(!chanErrors.length, `CDP channel not enabled: ${chanErrors.map(k => `${k}=${result.channels[k]}`).join(' | ')}`);
    check(!result.control.applied && result.control.events === 1 && /^style-src/.test(result.control.directives[0] || '')
      && result.control.audits === 1 && result.control.log === 1, `positive control: ${JSON.stringify(result.control)}`);
    result.walk_passed = true;
  } catch (e) {
    result.error = e instanceof Failure ? e.message : String(e && e.stack || e);
  } finally {
    // the end of the launch: tray Quit, the shell's exit, process_watch's inventory, the stand-in gone
    try {
      if (s) { await undoDrafts(s).catch(() => {}); await s.detach(); }
      if (shellPid && alive(shellPid)) {
        const q = spawnSync(PYTHON, ['-B', NATIVE, 'tray_quit', '--pid', String(shellPid)], { encoding: 'utf8', timeout: 20000 });
        result.cleanup.push(`tray_quit=${q.status}`);
        const gone = await waitFor(() => !alive(shellPid), 'shell exit', 30000, 200).then(() => true, () => false);
        result.cleanup.push(`shell_exited=${gone}`);
        if (!gone) {
          // only this run's own custom-scope shell (the pid process_watch started)
          spawnSync(PYTHON, ['-B', NATIVE, 'click', '--pid', String(shellPid), '--button', '7'], { encoding: 'utf8', timeout: 20000 });
          if (alive(shellPid)) { spawnSync('taskkill', ['/PID', String(shellPid), '/T', '/F']); result.cleanup.push('stop_scoped_shell'); result.error = result.error || 'the shell did not exit after the tray Quit'; }
        }
      }
      if (watcher) {
        await waitFor(() => watcher.exitCode !== null, 'process_watch exit', 90000, 250).catch(() => { result.cleanup.push('watcher_timeout'); });
        const inv = box && join(box.launch, 'process-inventory.json');
        if (inv && existsSync(inv)) {
          const j = JSON.parse(readFileSync(inv, 'utf8'));
          result.inventory = { passed: j.passed, active_processes: j.active_processes, total_processes: j.total_processes, root_exit_code: j.root_exit_code ?? null };
          copyFileSync(inv, join(evidence, 'process-inventory.json'));
          if (!j.passed || j.active_processes !== 0) result.error = result.error || `owned process residue: ${JSON.stringify(result.inventory)}`;
        } else { result.inventory = 'missing'; result.error = result.error || 'process inventory missing'; }
      }
      if (box) {
        const state = join(box.data, 'p4-ui-fixture');
        const pids = existsSync(state) ? readdirSync(state).filter(n => /^launch-\d+$/.test(n)).map(n => Number(readFileSync(join(state, n), 'utf8'))) : [];
        result.fixture_pids_alive = pids.filter(alive).length;
        if (result.fixture_pids_alive) result.error = result.error || `stand-in sidecar still alive: ${pids.filter(alive).join(',')}`;
        const log = join(box.data, 'logs', 'shell.log');
        if (existsSync(log)) copyFileSync(log, join(evidence, 'shell.log'));
      }
    } catch (e) { result.cleanup.push(`cleanup error: ${e && e.message || e}`); result.error = result.error || String(e && e.message || e); }
    // (fix round 1, S4: the sandbox, ~12 MB, goes once shell.log and the inventory are copied; only
    // when this run's shell is gone)
    if (box && !o.keep) {
      if (shellPid && alive(shellPid)) result.cleanup.push('sandbox_kept: the shell is still alive');
      else {
        try { rmSync(box.box, { recursive: true, force: true, maxRetries: 5, retryDelay: 400 }); result.cleanup.push('sandbox_removed'); }
        catch (e) { result.cleanup.push(`sandbox_remove_failed: ${e && e.code || e}`); }
      }
    } else if (box) result.cleanup.push('sandbox_kept: --keep');
    clearTimeout(deadline);
  }
  result.passed = !result.error && !!result.walk_passed;
  result.ended_at = new Date().toISOString();
  writeJson(join(evidence, 'result.json'), result);
  console.log(`${result.passed ? 'csp e2e: OK' : 'csp e2e: FAILED'} — ${result.steps.length} step(s), violations event ${result.channels.event ?? '?'} / audits ${result.channels.audits ?? '?'} / log ${result.channels.log ?? '?'}; control ${result.control ? `blocked=${!result.control.applied} reported=${result.control.events}` : '?'} → ${relative(REPO, join(evidence, 'result.json')).split('\\').join('/')}${result.error ? '\n' + result.error : ''}`);
  return result.passed ? 0 : 1;
}

main().then(code => { process.exitCode = code; }, e => { console.error(e && e.stack || e); process.exitCode = 1; });
