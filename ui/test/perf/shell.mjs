// T-P4-09 performance, WebView2 target (P4 architecture §10; 决定_第八波裁定: the shell target covers
// the 4,000-line log page and the 30 s hidden window): the debug shell's own WebView2 over CDP.
//
// Environment (as ui/test/e2e/csp.mjs, T-P4-08b, whose launcher this follows; driver.mjs is
// T-P4-11's and is only imported): one sandbox per round (a fresh shell each round: the stand-in
// ends itself after 30 minutes) under %TEMP% with its own custom scope
// (TESSERACT_INSTANCE_SCOPE, TESSERACT_CONFIG_DIR, TESSERACT_DATA_DIR, TEMP / TMP, APPDATA /
// LOCALAPPDATA inside it; refused when one is missing, the default scope, outside the sandbox or
// overlapping the live %APPDATA%\Tesseract / %LOCALAPPDATA%\Tesseract), no proxy variables, the
// debug shell under shell/app/process_watch.py with TESSERACT_SHELL_CDP_PORT and the T-P4-11
// stand-in sidecar (--dev-sidecar-script shell/app/src/p4_ui_fixture.py), playwright-core
// connectOverCDP. --build first runs scripts/shell_copy_ui.ps1 and `cargo build --offline` with the
// job count and priority of evidence/CPU_约定.txt; without it the debug exe must be newer than
// tauri.conf.json and the embedded UI's source_digest must be the working tree's.
// The end: tray Quit, the shell's exit, process_watch's inventory (no owned process left), the
// stand-in gone, the sandbox removed (--keep keeps it).
//
// Items (each run reloads the page at its start page: every page is entered for the first time):
//   entry.overview / entry.tiers / entry.logs /      a click on the page's sidebar item (总览 from 层级,
//   entry.settings                                   the others from 总览); entry.logs on the ~200 lines
//                                                    of a normal history (every round's fresh shell: the
//                                                    entry items come before the 4,000, review M2)
//   logs4000.load / .filter / .search               the stand-in holds 4,000 lines (pushed once per
//                                                    launch; every one a key event: the worst case for
//                                                    关键事件) and streams 50 lines/s (op log_rate)
//   logs4000.leave                                   then 总览 from 运行日志 holding the 4,000 lines
//   lang.overview / lang.settings (with a draft) /  ui.language en_US then zh_CN through the stand-in
//   lang.logs4000 (运行日志 holding the 4,000)       (op language: the shell's host:language)
//   hidden30 / hidden30.badge                        the window closed to the tray for 30 s with 50
//                                                    lines/s (+ an error line every 3 s): CDP Performance
//                                                    LayoutCount / RecalcStyleCount deltas 0, no write in
//                                                    the list, footer and sidebar badge; shown again: they
//                                                    catch up in one write (the first list batch holds
//                                                    the stand-in's newest line; the badge's first write
//                                                    its final count: common.mjs installWrites)
// first_screen (once, at the launch; with --real-sidecar also on the source sidecar in the T-P2-33
// hardened sandbox, as scripts/p4_ui_e2e.ps1 -RealSidecar, with the HKCU proxy values and the
// firewall rule names read before and after): shell.log's sidecar_ready / rt_seed_done /
// host_ready / window_revealed and the page's own paint timing.
import { spawn, spawnSync } from 'node:child_process';
import { appendFileSync, copyFileSync, existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { createServer } from 'node:net';
import { tmpdir } from 'node:os';
import { isAbsolute, join, relative, resolve } from 'node:path';
import { createHash, randomBytes } from 'node:crypto';
import { pathToFileURL } from 'node:url';
import { REPO, openSession, waitFor, settingsReady, makeDraft, undoDrafts, logsTotal, Failure } from '../e2e/driver.mjs';
import { EVIDENCE, LANG_FRAME_MS, LONG_MS, PROBE, UI, installWrites, quiet, sleep, takeSince, writeJson, writesEnd } from './common.mjs';

const SHELL_EXE = join(REPO, 'shell', 'target', 'debug', 'tesseract-shell.exe');
const FIXTURE = join(REPO, 'shell', 'app', 'src', 'p4_ui_fixture.py');
const WATCH = join(REPO, 'shell', 'app', 'process_watch.py');
const NATIVE = join(REPO, 'ui', 'test', 'e2e', 'native.py');
const PYTHON = join(REPO, '.venv', 'Scripts', 'python.exe');
const CONF = join(REPO, 'shell', 'app', 'tauri.conf.json');
const MANIFEST = join(REPO, 'shell', 'app', 'ui', 'build-manifest.json');
const P2_33 = join(REPO, 'tests', 'test_bridge_p2_33.py');
const SCOPE_VARS = ['TESSERACT_INSTANCE_SCOPE', 'TESSERACT_CONFIG_DIR', 'TESSERACT_DATA_DIR', 'TEMP', 'TMP', 'APPDATA', 'LOCALAPPDATA',
  'TESSERACT_SHELL_CDP_PORT', 'TESSERACT_ISOLATED_AUDIT_MANIFEST', 'TESSERACT_REDUCED_MOTION', 'TESSERACT_SHELL_DEV_READY_TIMEOUT_MS',
  'TESSERACT_SHELL_DEV_WEBVIEW2_MISSING', 'TESSERACT_SHELL_DEV_RECREATE_FAIL', 'TESSERACT_SHELL_DEV_EXIT_WAIT_IGNORE_EXITED', 'TESSERACT_SHELL_DEV_MARK_QUITTING',
  'PYTHONDONTWRITEBYTECODE', 'PYTHONPYCACHEPREFIX', 'PYTHONPATH', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY', 'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy'];
const HASH = { overview: 'overview', profiles: 'tiers', logs: 'logs', settings: 'settings' };
const LOGS_TOTAL = 4000;

export const ITEMS = ['entry.overview', 'entry.tiers', 'entry.logs', 'entry.settings', 'logs4000.load', 'logs4000.filter', 'logs4000.search', 'logs4000.leave', 'lang.overview', 'lang.settings', 'lang.logs4000', 'hidden30', 'hidden30.badge'];
export const ONCE_PER_ROUND = new Set(['hidden30', 'hidden30.badge']);

const under = (p, root) => { const r = relative(resolve(root), resolve(p)); return r === '' || (!r.startsWith('..') && !isAbsolute(r)); };
const freePort = () => new Promise((ok, bad) => { const s = createServer(); s.once('error', bad); s.listen(0, '127.0.0.1', () => { const { port } = s.address(); s.close(() => ok(port)); }); });
const alive = pid => { try { process.kill(pid, 0); return true; } catch (e) { return e.code === 'EPERM'; } };
const liveDirs = () => [process.env.APPDATA, process.env.LOCALAPPDATA].filter(Boolean).map(b => resolve(b, 'Tesseract'));

// ------------------------------------------------------------------------------------ build
function cpuConvention() {
  const conv = { mode: 'normal', cargo_jobs: '1', cargo_priority: 'BelowNormal' };
  const dir = join(EVIDENCE, '..');
  const f = readdirSync(dir).find(n => /^CPU_.*\.txt$/.test(n));
  if (f) for (const line of readFileSync(join(dir, f), 'utf8').split(/\r?\n/)) { const m = /^\s*(mode|cargo_jobs|cargo_priority)\s*=\s*(\S+)/.exec(line); if (m) conv[m[1]] = m[2]; }
  if (conv.mode === 'timing') { conv.cargo_jobs = '2'; conv.cargo_priority = 'BelowNormal'; }
  if (!/^\d+$/.test(conv.cargo_jobs) || !/^(Idle|BelowNormal|Normal|AboveNormal)$/.test(conv.cargo_priority)) throw new Error(`bad CPU convention: ${JSON.stringify(conv)}`);
  return conv;
}
function build(facts) {
  const copy = spawnSync('powershell.exe', ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', join(REPO, 'scripts', 'shell_copy_ui.ps1')], { cwd: REPO, encoding: 'utf8' });
  if (copy.status !== 0) throw new Error(`shell_copy_ui.ps1 failed: ${(copy.stderr || copy.stdout || '').slice(-800)}`);
  const conv = cpuConvention();
  const log = join(EVIDENCE, 'cpu-windows.txt');
  mkdirSync(EVIDENCE, { recursive: true });
  appendFileSync(log, `T-P4-09 perf cargo build --offline -j ${conv.cargo_jobs} priority=${conv.cargo_priority} mode=${conv.mode} start ${new Date().toISOString()}\n`);
  const ps = `$p = Start-Process -FilePath cargo -ArgumentList @('build','--offline','-j','${conv.cargo_jobs}') -WorkingDirectory '${join(REPO, 'shell').replace(/'/g, "''")}' -NoNewWindow -PassThru; $null = $p.Handle; try { $p.PriorityClass = [Diagnostics.ProcessPriorityClass]'${conv.cargo_priority}' } catch { }; $p.WaitForExit(); exit $p.ExitCode`;
  const cargo = spawnSync('powershell.exe', ['-NoProfile', '-Command', ps], { cwd: REPO, encoding: 'utf8', maxBuffer: 64 * 1024 * 1024 });
  appendFileSync(log, `T-P4-09 perf cargo build end ${new Date().toISOString()} exit=${cargo.status}\n`);
  facts.build = { copy_ui: copy.status, cargo: cargo.status, jobs: conv.cargo_jobs, priority: conv.cargo_priority, mode: conv.mode };
  if (cargo.status !== 0) throw new Error(`cargo build failed: ${(cargo.stderr || cargo.stdout || '').slice(-1500)}`);
}
async function fresh(facts, staleUi = false) {
  for (const f of [SHELL_EXE, FIXTURE, WATCH, NATIVE, PYTHON]) if (!existsSync(f)) throw new Error(`missing ${f}${f === SHELL_EXE ? ' (run with --build)' : ''}`);
  const exeAt = statSync(SHELL_EXE).mtimeMs;
  const stale = [CONF, MANIFEST].filter(f => existsSync(f) && statSync(f).mtimeMs > exeAt);
  if (stale.length) throw new Error(`the debug shell is older than ${stale.map(f => relative(REPO, f)).join(', ')}: run with --build`);
  const digest = (await import(pathToFileURL(join(UI, 'build.mjs')).href)).sourceDigest(UI);
  const embedded = existsSync(MANIFEST) ? JSON.parse(readFileSync(MANIFEST, 'utf8')).source_digest : null;
  facts.source_digest = { working_tree: digest, embedded };
  // (--stale-ui: measure the shell as built, e.g. the state before this ticket's changes; both
  // digests go into the evidence)
  if (embedded !== digest && !staleUi) throw new Error(`shell/app/ui was built from another UI source state (${String(embedded).slice(0, 12)} vs ${digest.slice(0, 12)}): run with --build`);
  facts.stale_ui = embedded !== digest;
}

// ------------------------------------------------------------------------------------ sandbox
function standInSandbox() {
  const box = mkdtempSync(join(tmpdir(), 'tesseract-p4perf-'));
  const s = { box, config: join(box, 'config'), data: join(box, 'data'), temp: join(box, 'temp'), appdata: join(box, 'appdata'), localappdata: join(box, 'localappdata'),
    scope: 'p4perf-' + randomBytes(5).toString('hex'), launch: join(box, 'launch-0'), real: false };
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
// the source sidecar's hardened sandbox (tests/test_bridge_p2_33.py prepare), plus a bytecode cache
// outside the source tree (as scripts/p4_ui_e2e.ps1 -RealSidecar)
function realSandbox() {
  const box = mkdtempSync(join(tmpdir(), 'tesseract-p4perf-real-'));
  const r = spawnSync(PYTHON, ['-B', P2_33, 'prepare', '--sandbox', box], { cwd: REPO, encoding: 'utf8' });
  if (r.status !== 0) { rmSync(box, { recursive: true, force: true }); throw new Error(`hardened sandbox preparation failed: ${(r.stderr || '').slice(-800)}`); }
  const s = { box, config: join(box, 'config'), data: join(box, 'data'), temp: join(box, 'temp'), appdata: join(box, 'appdata'), localappdata: join(box, 'localappdata'),
    scope: 'p4perf-real-' + randomBytes(5).toString('hex'), launch: join(box, 'launch-0'), real: true, pycache: join(box, 'pycache') };
  for (const d of [s.launch, s.pycache]) mkdirSync(d, { recursive: true });
  // (the warm-up runs in the sandbox's scope too: its imports never see the live profile)
  const env = scopedEnv(s, 0); delete env.PYTHONDONTWRITEBYTECODE; delete env.TESSERACT_SHELL_CDP_PORT;
  const warm = spawnSync(PYTHON, ['-c', 'import bridge_main, desktop_runtime'], { cwd: REPO, encoding: 'utf8', env });
  s.warm = warm.status;
  return s;
}
function scopedEnv(s, port) {
  const env = { ...process.env };
  for (const k of Object.keys(env)) if (SCOPE_VARS.includes(k) || SCOPE_VARS.includes(k.toUpperCase())) delete env[k];
  Object.assign(env, { TESSERACT_INSTANCE_SCOPE: s.scope, TESSERACT_CONFIG_DIR: s.config, TESSERACT_DATA_DIR: s.data, TEMP: s.temp, TMP: s.temp,
    APPDATA: s.appdata, LOCALAPPDATA: s.localappdata, TESSERACT_SHELL_CDP_PORT: String(port), PYTHONDONTWRITEBYTECODE: '1' });
  if (s.pycache) env.PYTHONPYCACHEPREFIX = s.pycache;
  for (const k of ['TESSERACT_INSTANCE_SCOPE', 'TESSERACT_CONFIG_DIR', 'TESSERACT_DATA_DIR', 'TEMP', 'TMP', 'APPDATA', 'LOCALAPPDATA']) if (!env[k]) throw new Error(`refusing to launch: ${k} is not set`);
  if (/^(default)?$/i.test(env.TESSERACT_INSTANCE_SCOPE) || env.TESSERACT_INSTANCE_SCOPE !== s.scope) throw new Error('refusing to launch: not this run\'s custom scope');
  for (const k of ['TESSERACT_CONFIG_DIR', 'TESSERACT_DATA_DIR', 'TEMP', 'TMP', 'APPDATA', 'LOCALAPPDATA']) {
    if (!under(env[k], s.box)) throw new Error(`refusing to launch: ${k}=${env[k]} is outside the sandbox`);
    for (const live of liveDirs()) if (under(env[k], live) || under(live, env[k])) throw new Error(`refusing to launch: ${k} overlaps the live profile ${live}`);
  }
  return env;
}
// HKCU Internet Settings proxy values and the Tesseract / kill-switch firewall rule names (read only).
// Compared in memory; the evidence keeps a short hash of each (review B S9: an AutoConfigURL may carry
// a token on another machine), never the values
const HOST_KEYS = ['ProxyEnable', 'ProxyServer', 'ProxyOverride', 'AutoConfigURL', 'firewall', 'firewall_error'];
const hashFacts = f => Object.fromEntries(HOST_KEYS.map(k => [k, f[k] == null ? null : 'sha256:' + createHash('sha256').update(String(f[k])).digest('hex').slice(0, 16)]));
function hostFacts() {
  const ps = `$f=[ordered]@{}; $v=Get-ItemProperty -LiteralPath 'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings'; foreach($n in @('ProxyEnable','ProxyServer','ProxyOverride','AutoConfigURL')){ $p=$v.PSObject.Properties[$n]; $f[$n]= if($null -eq $p){$null}else{[string]$p.Value} }; try { $r=@(Get-NetFirewallRule -ErrorAction Stop | Where-Object { $_.Name -like '*Tesseract*' -or $_.DisplayName -like '*Tesseract*' -or $_.Name -like 'ProxyManager-KillSwitch*' -or $_.DisplayName -like 'ProxyManager-KillSwitch*' }); $f.firewall=(@($r | ForEach-Object { [string]$_.Name }) | Sort-Object) -join ';'; $f.firewall_error=$null } catch { $f.firewall=$null; $f.firewall_error=$_.Exception.Message }; $f | ConvertTo-Json -Compress`;
  const r = spawnSync('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', ps], { encoding: 'utf8', timeout: 120000 });
  return JSON.parse((r.stdout || '{}').trim() || '{}');
}

// ------------------------------------------------------------------------------------ one launch
async function launch(s, log, { standIn = true, extraEnv = {} } = {}) {
  const port = await freePort();
  const env = scopedEnv(s, port);
  // (extra variables are shell options only, e.g. TESSERACT_REDUCED_MOTION; never the scope's own)
  for (const [k, v] of Object.entries(extraEnv)) { if (['TESSERACT_INSTANCE_SCOPE', 'TESSERACT_CONFIG_DIR', 'TESSERACT_DATA_DIR', 'TEMP', 'TMP', 'APPDATA', 'LOCALAPPDATA', 'TESSERACT_SHELL_CDP_PORT'].includes(k)) throw new Error(`refusing to override ${k}`); env[k] = String(v); }
  const out = n => join(s.launch, n);
  const args = ['-B', WATCH, '--output', out('process-inventory.json'), '--ready', out('root.json'), '--', SHELL_EXE];
  if (standIn) args.push('--dev-sidecar-script', FIXTURE);
  const t0 = Date.now();
  const watcher = spawn(PYTHON, args, { cwd: REPO, env, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] });
  const wlog = [];
  watcher.stdout.on('data', d => wlog.push(String(d))); watcher.stderr.on('data', d => wlog.push(String(d)));
  let exit = null; watcher.on('exit', c => { exit = c; });
  await waitFor(() => existsSync(out('root.json')) || exit !== null, 'process_watch started the shell', 30000);
  if (!existsSync(out('root.json'))) throw new Error(`process_watch failed: ${wlog.join('').slice(-800)}`);
  const pid = Number(JSON.parse(readFileSync(out('root.json'), 'utf8')).pid);
  const logFile = join(s.data, 'logs', 'shell.log');
  const L = { s, port, pid, watcher, t0, logFile, closed: false, wlog };
  await waitFor(() => {
    const text = existsSync(logFile) ? readFileSync(logFile, 'utf8') : '';
    if (/ (sidecar_start_failed|hello_timeout|sidecar_exit_before_hello)/.test(text)) throw new Failure('sidecar failed before hello');
    return / shell_window_ready/.test(text) && / sidecar_ready /.test(text); // 11-05: the window opens before hello
  }, 'shell window ready', 60000, 200);
  if (standIn) await waitFor(() => readdirSync(join(s.data, 'p4-ui-fixture')).some(n => n.startsWith('ready-')), 'stand-in sidecar ready', 30000, 200);
  else await waitFor(() => / rt_seed_done /.test(existsSync(logFile) ? readFileSync(logFile, 'utf8') : ''), 'rt_seed_done (source sidecar)', 60000, 200);
  log(`[shell] launched pid ${pid} scope ${s.scope} cdp ${port}`);
  return L;
}
async function quit(L, evidence, tag) {
  const cleanup = [];
  if (L.closed) return cleanup;
  L.closed = true;
  if (alive(L.pid)) {
    const q = spawnSync(PYTHON, ['-B', NATIVE, 'tray_quit', '--pid', String(L.pid)], { encoding: 'utf8', timeout: 20000 });
    cleanup.push(`tray_quit=${q.status}`);
    const gone = await waitFor(() => !alive(L.pid), 'shell exit', 30000, 200).then(() => true, () => false);
    cleanup.push(`shell_exited=${gone}`);
    if (!gone) {
      spawnSync(PYTHON, ['-B', NATIVE, 'click', '--pid', String(L.pid), '--button', '7'], { encoding: 'utf8', timeout: 20000 });
      if (alive(L.pid)) { spawnSync('taskkill', ['/PID', String(L.pid), '/T', '/F']); cleanup.push('stop_scoped_shell'); }
    }
  }
  await waitFor(() => L.watcher.exitCode !== null, 'process_watch exit', 90000, 250).catch(() => cleanup.push('watcher_timeout'));
  const inv = join(L.s.launch, 'process-inventory.json');
  let inventory = 'missing';
  if (existsSync(inv)) { const j = JSON.parse(readFileSync(inv, 'utf8')); inventory = { passed: j.passed, active_processes: j.active_processes, total_processes: j.total_processes, root_exit_code: j.root_exit_code ?? null }; copyFileSync(inv, join(evidence, `${tag}-process-inventory.json`)); }
  cleanup.push({ inventory, watcher_exit: L.watcher.exitCode, watcher_tail: inventory === 'missing' ? L.wlog.join('').slice(-600) : undefined });
  const state = join(L.s.data, 'p4-ui-fixture');
  const pids = existsSync(state) ? readdirSync(state).filter(n => /^launch-\d+$/.test(n)).map(n => Number(readFileSync(join(state, n), 'utf8'))) : [];
  cleanup.push({ stand_in_alive: pids.filter(alive).length });
  // process_watch.py audits the first 90 s of a launch only (a longer one ends with "owned process
  // audit exceeded deadline", inventory missing): every process whose command line names this
  // launch's sandbox (the shell, its WebView2 profile, the stand-in) must be gone as well (15 s)
  const residue = () => {
    const ps = `@(Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -and $_.CommandLine.IndexOf('${L.s.box.replace(/'/g, "''")}', [StringComparison]::OrdinalIgnoreCase) -ge 0 } | ForEach-Object { "$($_.ProcessId) $($_.Name)" }) -join ';'`;
    const r = spawnSync('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', ps], { encoding: 'utf8', timeout: 60000 });
    return (r.stdout || '').trim();
  };
  const left = await waitFor(() => (residue() === '' ? true : null), 'no process of this sandbox', 15000, 1000).then(() => '', () => residue());
  cleanup.push({ sandbox_processes_left: left || 0 });
  if (existsSync(L.logFile)) copyFileSync(L.logFile, join(evidence, `${tag}-shell.log`));
  return cleanup;
}
function removeBox(s, keep, pid) {
  if (keep) return 'sandbox_kept: --keep';
  if (pid && alive(pid)) return 'sandbox_kept: the shell is still alive';
  try { rmSync(s.box, { recursive: true, force: true, maxRetries: 5, retryDelay: 400 }); return 'sandbox_removed'; } catch (e) { return `sandbox_remove_failed: ${e && e.code || e}`; }
}

// shell.log -> {code: [ms, fields]} (first of each code) and the first line's time
function shellTimeline(file) {
  const lines = existsSync(file) ? readFileSync(file, 'utf8').split(/\r?\n/).filter(Boolean) : [];
  const out = { start: null, codes: {} };
  for (const l of lines) {
    const m = /^(\d+) (\S+)(.*)$/.exec(l); if (!m) continue;
    const ms = Number(m[1]); if (out.start === null) out.start = ms;
    if (!out.codes[m[2]]) out.codes[m[2]] = { ms, rest: m[3].trim() };
  }
  return out;
}
async function firstScreen(sess, L) {
  const tl = shellTimeline(L.logFile);
  const page = await sess.eval(() => {
    const nav = performance.getEntriesByType('navigation')[0];
    const paint = Object.fromEntries(performance.getEntriesByType('paint').map(p => [p.name, Math.round(p.startTime)]));
    return { origin: Math.round(performance.timeOrigin), paint, dom_content_loaded: nav ? Math.round(nav.domContentLoadedEventEnd) : null, load: nav ? Math.round(nav.loadEventEnd) : null, run: document.getElementById('app').dataset.run };
  });
  const at = c => (tl.codes[c] ? tl.codes[c].ms : null);
  const rel = v => (v == null || tl.start == null ? null : v - tl.start);
  const seed = tl.codes.rt_seed_done ? tl.codes.rt_seed_done.rest : null;
  const fcp = page.paint['first-contentful-paint'] != null ? page.origin + page.paint['first-contentful-paint'] : null;
  // the first screen is complete when the seed is done (every slot: rt_seed_done) and the page has
  // painted after it; the page paints each slot as it comes, so the later of the two. A missing
  // time is never read as 0 (review B S3): complete is then null and the missing ones are named
  const parts = { rt_seed_done: at('rt_seed_done'), host_ready: at('host_ready'), first_contentful_paint: fcp };
  const missing = Object.keys(parts).filter(k => parts[k] == null);
  const complete = missing.length ? null : Math.max(...Object.values(parts));
  return { shell_start_ms: tl.start, rel_ms: { sidecar_ready: rel(at('sidecar_ready')), rt_seed_done: rel(at('rt_seed_done')), webview_browser_started: rel(at('webview_browser_started')),
    page_origin: rel(page.origin), first_contentful_paint: rel(fcp), host_ready: rel(at('host_ready')), window_revealed: rel(at('window_revealed')), complete: rel(complete) },
  complete_missing: missing, seed, window_revealed: tl.codes.window_revealed ? tl.codes.window_revealed.rest : null, page, from_sidecar_ready_ms: complete && at('sidecar_ready') ? complete - at('sidecar_ready') : null };
}

// ------------------------------------------------------------------------------------ in the page
async function landAt(sess, id) {
  // a fresh page at <id>: every page is entered for the first time from here
  await sess.eval(h => { history.replaceState(null, '', '#' + h); }, HASH[id] || id);
  await sess.reloadObserved();
  await sess.until(i => window.T.router.current === i, `landed on ${id}`, 15000, id);
  await sess.page.evaluate(() => document.fonts.ready);
  await sess.page.evaluate(quiet, 1500);
  return sess.eval(() => document.visibilityState);
}
async function window_(sess, fn, settle = 1500) {
  const t0 = await sess.eval(() => performance.now());
  const extra = await fn();
  await sess.page.evaluate(quiet, settle);
  const l = await sess.page.evaluate(takeSince, t0);
  return { longest: Math.round(Math.max(0, ...l.lt.map(e => e.dur))), lt: l.lt.map(e => e.dur), loaf: l.loaf, extra };
}
const clickNav = (sess, id) => sess.page.click(`.nav-item[data-nav="${id}"]`);
// in-page: the next switch to lang, timed from the language store write to the first frame after
// i18n:change (a message posted from that frame's rAF callback runs after its paint)
function armLanguage(lang) {
  window.__langT = new Promise((res, rej) => {
    const T = window.T; let t0 = null;
    const offS = T.store.sub('lang', v => { if (v === lang && t0 === null) t0 = performance.now(); });
    const offC = T.on('i18n:change', p => {
      if (!p || p.lang !== lang) return;
      const tc = performance.now(); offC && offC(); offS && offS();
      requestAnimationFrame(() => { const ch = new MessageChannel(); ch.port1.onmessage = () => res({ t_change: Math.round(tc - t0), t_frame: Math.round(performance.now() - t0), html_lang: document.documentElement.lang }); ch.port2.postMessage(0); });
    });
    setTimeout(() => rej(new Error(`no i18n:change to ${lang} in 8 s`)), 8000);
  });
}

// ------------------------------------------------------------------------------------ setup
export async function setup({ build: doBuild, real, evidence, keep, log, staleUi }) {
  const facts = {};
  mkdirSync(evidence, { recursive: true });
  if (doBuild) build(facts);
  await fresh(facts, !!staleUi);
  // One shell per round: the stand-in sidecar ends itself 30 minutes after its start
  // (p4_ui_fixture.py), and three rounds take longer. run.mjs's pause (before a round's CPU sample)
  // quits the shell and removes its sandbox; resume launches a fresh one in a fresh sandbox. The
  // first launch (here) gives the first screen; every launch's is recorded (facts.launches).
  let s = null, L = null, sess = null, pushed = 0, launches = 0;
  facts.launches = [];
  async function start() {
    s = standInSandbox();
    L = await launch(s, log);
    launches++;
    sess = await openSession({ port: L.port, pid: L.pid, data: s.data, evidence: join(evidence, `shots-${launches}`), python: PYTHON });
    const fs = await firstScreen(sess, L);
    facts.launches.push({ n: launches, sandbox: s.box, scope: s.scope, first_screen_rel_ms: fs.rel_ms, first_screen_missing: fs.complete_missing, seed: fs.seed });
    if (launches === 1) {
      facts.sandbox = s.box; facts.scope = s.scope;
      facts.webview = (shellTimeline(L.logFile).codes.webview_runtime || {}).rest || null;
      facts.first_screen_stand_in = fs;
    }
    await sess.page.addInitScript(PROBE);
    await sess.cdp.send('Performance.enable');
    // ~200 lines now (a normal history); the 4,000 come before the first log item
    await sess.control({ op: 'logs', n: 200 });
    pushed = 203;
  }
  async function stop() {
    if (!L) return [];
    const cleanup = [];
    try { await sess.control({ op: 'log_rate', per_s: 0 }); } catch (e) { /* gone */ }
    try { await sess.detach(); } catch (e) { /* gone */ }
    cleanup.push(...await quit(L, evidence, `stand-in-${launches}`));
    cleanup.push(removeBox(s, keep, L.pid));
    L = null; sess = null;
    return cleanup;
  }
  await start();
  const ensure4000 = async () => { if (pushed >= LOGS_TOTAL) return; for (let n = LOGS_TOTAL - pushed; n > 0; n -= 1000) await sess.control({ op: 'logs', n: Math.min(1000, n) }, 30000); pushed = LOGS_TOTAL; };
  const visible = async () => { const v = await sess.eval(() => document.visibilityState); if (v !== 'visible') { sess.native('restore'); await waitFor(() => sess.win().visible, 'window restored', 10000); } return v; };

  async function runItem(item) {
    await visible();
    if (item.startsWith('entry.')) {
      const go = { 'entry.overview': 'overview', 'entry.tiers': 'profiles', 'entry.logs': 'logs', 'entry.settings': 'settings' }[item];
      // (总览 from 层级: from 运行日志 holding 4,000 lines it would measure leaving that page too,
      // which is logs4000.leave)
      const start = go === 'overview' ? 'profiles' : 'overview';
      await sess.control({ op: 'log_rate', per_s: 0 });
      const vis = await landAt(sess, start);
      const w = await window_(sess, () => clickNav(sess, go), 1800);
      const shown = await sess.eval(id => { const p = document.querySelector(`.page[data-page="${id}"]`); return !!(p && !p.hidden && p.childElementCount); }, go);
      // (entry.logs) the normal history it entered on: the rows it shows (well under the 4,000)
      const rows = go === 'logs' ? await sess.eval(() => ({ events: document.querySelectorAll('#logs-events .logs-row').length, raw: document.querySelectorAll('#logs-rows .logs-rrow').length, pushed_before: null })) : undefined;
      if (rows) rows.pushed_before = pushed;
      return { ...w, ok: w.longest <= LONG_MS && shown && (!rows || (rows.events > 0 && pushed < LOGS_TOTAL)), shown, rows, visibility: vis };
    }
    if (item.startsWith('logs4000.')) {
      await ensure4000();
      await sess.control({ op: 'log_rate', per_s: 50 });
      const vis = await landAt(sess, 'overview');
      const load = await window_(sess, () => clickNav(sess, 'logs'), 2500);
      const rows = await sess.eval(() => ({ events: document.querySelectorAll('#logs-events .logs-row').length, raw: document.querySelectorAll('#logs-rows .logs-rrow').length }));
      if (item === 'logs4000.load') return { ...load, ok: load.longest <= LONG_MS && rows.events > 0, rows, visibility: vis };
      if (item === 'logs4000.leave') {
        await sess.page.evaluate(quiet, 1500);
        const w = await window_(sess, () => clickNav(sess, 'overview'), 1800);
        const shown = await sess.eval(() => window.T.router.current === 'overview');
        return { ...w, ok: w.longest <= LONG_MS && shown, rows, shown, visibility: vis };
      }
      const steps = {};
      const act = async (name, fn, settle = 900) => { const w = await window_(sess, fn, settle); steps[name] = { longest: w.longest, lt: w.lt, loaf: w.loaf }; };
      const p = sess.page;
      if (item === 'logs4000.filter') {
        await act('events_issue', () => p.click('#logs-level [data-v="issue"]'));
        await act('events_all', () => p.click('#logs-level [data-v="all"]'));
        await act('view_raw', () => p.click('#logs-view [data-v="raw"]'));
        await act('raw_error', () => p.click('#logs-level [data-v="error"]'));
        await act('raw_all', () => p.click('#logs-level [data-v="all"]'));
        await act('view_events', () => p.click('#logs-view [data-v="events"]'));
      } else {
        for (const view of ['events', 'raw']) {
          if (view === 'raw') await p.click('#logs-view [data-v="raw"]');
          await p.evaluate(quiet, 600);
          await act(`${view}_type`, async () => { await p.click('#logs-search input'); await p.keyboard.type('fixture', { delay: 110 }); });
          await act(`${view}_erase`, async () => { for (let i = 0; i < 7; i++) { await p.keyboard.press('Backspace'); await sleep(110); } });
        }
      }
      const longest = Math.max(0, ...Object.values(steps).map(x => x.longest));
      return { longest, ok: longest <= LONG_MS, steps, rows, visibility: vis };
    }
    if (item === 'lang.logs4000') {
      // (review M1) 运行日志 holding the 4,000 lines (no live feed: the switch alone); the window takes
      // in the shown view's sliced rebuild and the other view's idle one (settle 2500 ms)
      await ensure4000();
      await sess.control({ op: 'log_rate', per_s: 0 });
      const vis = await landAt(sess, 'overview');
      await clickNav(sess, 'logs');
      await sess.until(() => document.querySelectorAll('#logs-events .logs-row').length > 1000, '4,000 lines on 运行日志', 20000);
      await sess.page.evaluate(quiet, 2500);
      const count = () => sess.eval(() => ({ events: document.querySelectorAll('#logs-events .logs-row').length, raw: document.querySelectorAll('#logs-rows .logs-rrow').length, lang: document.documentElement.lang, banners: document.querySelectorAll('#banners .banner').length }));
      const rows0 = await count();
      const switches = {};
      for (const lang of ['en_US', 'zh_CN']) {
        let timing = null;
        const w = await window_(sess, async () => { await sess.eval(armLanguage, lang); await sess.control({ op: 'language', language: lang }); timing = await sess.eval(() => window.__langT); return timing; }, 2500);
        switches[`${Object.keys(switches).length}:${lang}`] = { longest: w.longest, lt: w.lt, loaf: w.loaf, ...timing, rows: await count() };
      }
      const rows1 = await count();
      const longest = Math.max(0, ...Object.values(switches).map(x => x.longest));
      const frame = Math.max(0, ...Object.values(switches).map(x => x.t_frame));
      const kept = rows1.events === rows0.events && rows0.events > 1000;
      return { longest, frame_ms: frame, ok: longest <= LONG_MS && frame < LANG_FRAME_MS && kept, switches, rows_before: rows0, rows_after: rows1, visibility: vis };
    }
    if (item.startsWith('lang.')) {
      await sess.control({ op: 'log_rate', per_s: 0 });
      const onSettings = item === 'lang.settings';
      const vis = await landAt(sess, onSettings ? 'settings' : 'overview');
      let draft = null;
      if (onSettings) {
        await settingsReady(sess);
        const key = await makeDraft(sess);
        await sess.page.evaluate(quiet, 800);
        draft = { key, before: await sess.eval(() => window.T.ui.saveBar.dirtyCount()) };
      }
      const switches = {};
      for (const lang of ['en_US', 'zh_CN']) {
        let timing = null;
        const w = await window_(sess, async () => { await sess.eval(armLanguage, lang); await sess.control({ op: 'language', language: lang }); timing = await sess.eval(() => window.__langT); return timing; }, 1200);
        switches[`${Object.keys(switches).length}:${lang}`] = { longest: w.longest, lt: w.lt, loaf: w.loaf, ...timing };
      }
      if (draft) { draft.after = await sess.eval(() => window.T.ui.saveBar.dirtyCount()); await undoDrafts(sess).catch(() => {}); }
      const longest = Math.max(0, ...Object.values(switches).map(x => x.longest));
      const frame = Math.max(0, ...Object.values(switches).map(x => x.t_frame));
      return { longest, frame_ms: frame, ok: longest <= LONG_MS && frame < LANG_FRAME_MS && (!draft || (draft.after === draft.before && draft.after > 0)), switches, draft, visibility: vis };
    }
    if (item.startsWith('hidden30')) return hidden(item === 'hidden30.badge');
    throw new Error(`unknown item ${item}`);
  }

  // the window closed to the tray (native.py close: the shell hides it; WebView2 IsVisible=false)
  async function hidden(badge, seconds = 30) {
    await ensure4000();
    await sess.control({ op: 'log_rate', per_s: 0 });
    const page = badge ? 'overview' : 'logs';
    if (badge) await landAt(sess, 'overview'); else { await landAt(sess, 'overview'); await clickNav(sess, 'logs'); await sess.page.evaluate(quiet, 2500); }
    const watermark = () => sess.eval(() => window.T.rt.query('logs.tail', { n: 1 }).then(d => d.watermark));
    const before = { total: badge ? null : await logsTotal(sess), badge: await sess.eval(() => { const b = document.querySelector('.nav-item[data-nav="logs"] .logs-navbadge'); return b && !b.hidden ? b.textContent : null; }) };
    sess.native('close');
    await waitFor(() => !sess.win().visible, 'window closed to the tray', 10000);
    await sess.until(() => { const p = window.T.store.get('presence'); return p && p.visible === false && document.hidden; }, 'page hidden', 10000);
    await sleep(1500);
    // the writes while hidden, and the catch-up batches once shown, by region (common.mjs)
    await sess.eval(installWrites);
    await sess.cdp.send('Performance.enable');           // (the reload made a new CDP session)
    const m0 = await sess.metrics();
    await sess.control({ op: 'log_rate', per_s: 50 });
    const errs = [];
    const until = Date.now() + seconds * 1000;
    while (Date.now() < until - 3000) { await sleep(3000); await sess.control({ op: 'logs', n: 1, level: 'error' }); errs.push(1); }
    await sleep(Math.max(0, until - Date.now()));
    await sess.control({ op: 'log_rate', per_s: 0 });
    const m1 = await sess.metrics();
    const hiddenWrites = await sess.eval(() => { window.__writesObs.takeRecords(); return window.__writes.hidden; });
    const sent = await watermark();
    await sess.eval(() => { window.__writes.phase = 'shown'; window.__writes.t0 = performance.now(); });
    sess.native('restore');
    await waitFor(() => sess.win().visible, 'window restored', 10000);
    await sess.until(() => { const p = window.T.store.get('presence'); return p && p.visible === true && !document.hidden; }, 'page visible', 10000);
    // caught up: the newest line on the page is the stand-in's newest (its title carries its seq)
    const newest = () => sess.eval(() => Math.max(0, ...[...document.querySelectorAll('#logs-events .logs-row .logs-t, #logs-rows .logs-rrow .logs-msg')].map(e => Number((/(\d+)\s*$/.exec(e.textContent) || [])[1]) || 0)));
    let caught = null;
    if (!badge) caught = await waitFor(async () => ((await newest()) >= sent ? true : null), `the newest line ${sent} on the page`, 15000).then(() => true, () => false);
    await sess.page.evaluate(quiet, 1500);
    const after = await sess.eval(writesEnd);
    const d = k => Math.round((m1[k] || 0) - (m0[k] || 0));
    const delta = { LayoutCount: d('LayoutCount'), RecalcStyleCount: d('RecalcStyleCount'), ScriptDuration_ms: Math.round(((m1.ScriptDuration || 0) - (m0.ScriptDuration || 0)) * 1000), TaskDuration_ms: Math.round(((m1.TaskDuration || 0) - (m0.TaskDuration || 0)) * 1000) };
    const totalAfter = badge ? null : { total: await logsTotal(sess), newest: await newest() };
    // caught up in one write (review A S1 / B S5): the first content batch of the list shown
    // already holds the stand-in's newest line; the badge's first write is already its final count
    const w = after.writes, first = badge ? w.first.badge : w.first[w.cur];
    const oneWrite = badge ? !!(first && first.badge && first.badge === after.badge) : !!(first && first.newest >= sent);
    const ok = delta.LayoutCount === 0 && delta.RecalcStyleCount === 0 && hiddenWrites === 0 && (badge ? !!after.badge : caught === true) && oneWrite;
    return { longest: 0, ok, page, seconds, delta, writes_hidden: hiddenWrites, after, before, sent, total_after: totalAfter, caught_up: caught, one_write: oneWrite, errors_sent: errs.length };
  }

  return {
    facts,
    runItem,
    // (the CPU is sampled with no shell of this run alive: the round's shell starts after it)
    pause: async () => { const n = launches, c = await stop(); if (n) facts.launches[n - 1].cleanup = c; },
    resume: async () => { if (!L) await start(); },
    extra: async () => {
      const out = {};
      // (the stand-in's shell is quit first: one shell of this run at a time)
      if (real) { const n = launches, c = await stop(); if (n) facts.launches[n - 1].cleanup = c; out.first_screen_real_sidecar = await realFirstScreen(evidence, keep, log); }
      return out;
    },
    // -> every launch's cleanup (tray Quit, process inventory, stand-in gone, sandbox removed)
    close: async () => { const n = launches, c = await stop(); if (n && c.length) facts.launches[n - 1].cleanup = c; return facts.launches.map(x => ({ launch: x.n, cleanup: x.cleanup || 'missing' })); },
  };
}

// the first screen on the source sidecar (hardened sandbox, host facts before / after, read-back)
async function realFirstScreen(evidence, keep, log) {
  const before = hostFacts();
  const out = { host_before: hashFacts(before) };
  if (before.firewall_error) return { ...out, error: `no firewall baseline: ${before.firewall_error}` };
  let s = null, L = null, sess = null;
  try {
    s = realSandbox();
    out.scope = s.scope; out.bytecode_warm = s.warm;
    L = await launch(s, log, { standIn: false });
    sess = await openSession({ port: L.port, pid: L.pid, data: s.data, evidence: join(evidence, 'shots-real'), real: true, python: PYTHON });
    await sleep(1500);
    out.first_screen = await firstScreen(sess, L);
  } catch (e) {
    out.error = e instanceof Failure ? e.message : String(e && e.stack || e);
  } finally {
    try { if (sess) await sess.detach(); } catch (e) { /* gone */ }
    if (L) out.cleanup = await quit(L, evidence, 'real');
    if (s) {
      const rb = spawnSync(PYTHON, ['-B', P2_33, 'readback', '--sandbox', s.box], { cwd: REPO, encoding: 'utf8' });
      try { out.readback = JSON.parse((rb.stdout || '').trim().split('\n').pop()); } catch (e) { out.readback = { error: (rb.stderr || '').slice(-400) }; }
      const after = hostFacts();
      out.host_after = hashFacts(after);
      out.host_changed = HOST_KEYS.filter(k => String(after[k]) !== String(before[k]));
      if (!out.error && (out.host_changed.length || (out.readback && out.readback.problems && out.readback.problems.length))) out.error = `host facts / read-back: ${JSON.stringify({ changed: out.host_changed, problems: out.readback && out.readback.problems })}`;
      out.sandbox = removeBox(s, keep, L && L.pid);
    }
  }
  return out;
}

// ------------------------------------------------------------------------------------ for ui/test/a11y
export const buildShell = build;
/** one stand-in launch with extra shell options (a11y --shell: TESSERACT_REDUCED_MOTION=1) */
export async function launchScoped({ env = {}, evidence, log }) {
  const facts = {};
  await fresh(facts);
  mkdirSync(evidence, { recursive: true });
  const s = standInSandbox();
  const L = await launch(s, log, { extraEnv: env });
  const sess = await openSession({ port: L.port, pid: L.pid, data: s.data, evidence: join(evidence, 'shots-shell'), python: PYTHON });
  return {
    sess, facts, scope: s.scope,
    close: async keep => {
      try { await sess.detach(); } catch (e) { /* gone */ }
      const cleanup = await quit(L, evidence, 'a11y-shell');
      cleanup.push(removeBox(s, keep, L.pid));
      const inv = cleanup.find(x => x && x.inventory) || {};
      const standIn = cleanup.find(x => x && typeof x === 'object' && 'stand_in_alive' in x) || {};
      const left = cleanup.find(x => x && typeof x === 'object' && 'sandbox_processes_left' in x) || {};
      const ok = !alive(L.pid) && inv.inventory && inv.inventory.passed === true && inv.inventory.active_processes === 0 && standIn.stand_in_alive === 0 && left.sandbox_processes_left === 0;
      return { ok, cleanup };
    },
  };
}
