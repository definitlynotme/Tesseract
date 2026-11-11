// T-P4-09 performance framework, the shared part (P4 architecture §10; ticket requirements 1, 2).
//
// - PROBE: the in-page recorder, installed before any page script (Playwright addInitScript /
//   CDP Page.addScriptToEvaluateOnNewDocument): PerformanceObserver({type:'longtask', buffered:true})
//   and, for the diagnosis only, long-animation-frame entries with their script attribution.
// - cpuIdle(): the measuring condition (criticism E8): the average of 30 one-second samples of
//   \Processor(_Total)\% Processor Time (PowerShell Get-Counter) must be below 10 % before a round;
//   the samples, the CPU model and the logical core count go into the evidence. A busy machine is
//   waited for (up to --cpu-wait minutes), never measured.
// - timingConvention(): evidence/CPU_约定.txt; mode=timing (another session is running timing
//   experiments) refuses the run.
// - rounds(): every item runs N times (default 5) per round and the round's value is the worst run;
//   3 rounds always; a round over the limit is followed by a 60 s pause before the next one; the item
//   passes when at least 2 of the 3 rounds pass. Chrome and WebView2 are judged separately (one run
//   of run.mjs is one target).
import { spawnSync } from 'node:child_process';
import { existsSync, mkdirSync, readdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { cpus } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
export const UI = resolve(HERE, '..', '..');
export const REPO = resolve(UI, '..');
export const ORDERS = readdirSync(join(REPO, '.build'), { withFileTypes: true }).filter(d => d.isDirectory())
  .map(d => join(REPO, '.build', d.name)).find(d => existsSync(join(d, 'T-P4-09.md')));
if (!ORDERS) throw new Error('work-order directory (T-P4-09.md) not found under .build/');
export const EVIDENCE = join(ORDERS, 'evidence', 'T-P4-09');
export const LONG_MS = 50;          // a long task is > 50 ms (the target: none)
export const LANG_FRAME_MS = 200;   // language switch: switch -> first frame done < 200 ms
export const sleep = ms => new Promise(r => setTimeout(r, ms));
export const stamp = () => new Date().toISOString().replace(/[-:]/g, '').replace(/\..*$/, '').replace('T', '-');

export function writeJson(file, value) {
  mkdirSync(dirname(file), { recursive: true });
  writeFileSync(file, JSON.stringify(value, null, 1) + '\n');
}

// ------------------------------------------------------------------------------------ in the page
export const PROBE = `(() => {
  if (window.__perf) return;
  const P = window.__perf = { lt: [], loaf: [], errors: [] };
  try {
    new PerformanceObserver(l => { for (const e of l.getEntries()) P.lt.push({ start: Math.round(e.startTime * 10) / 10, dur: Math.round(e.duration * 10) / 10 }); })
      .observe({ type: 'longtask', buffered: true });
  } catch (e) { P.errors.push('longtask: ' + e); }
  try {
    new PerformanceObserver(l => {
      for (const e of l.getEntries()) {
        if (e.duration < 50) continue;
        P.loaf.push({ start: Math.round(e.startTime), dur: Math.round(e.duration), block: Math.round(e.blockingDuration || 0),
          render: e.renderStart ? Math.round(e.startTime + e.duration - e.renderStart) : 0,
          style: e.styleAndLayoutStart ? Math.round(e.startTime + e.duration - e.styleAndLayoutStart) : 0,
          scripts: Array.from(e.scripts || []).filter(s => s.duration >= 8).slice(0, 6).map(s => ({ d: Math.round(s.duration),
            inv: String(s.invoker || '').slice(0, 60), fn: String(s.sourceFunctionName || ''), src: String(s.sourceURL || '').replace(/^.*\\//, ''),
            at: s.sourceCharPosition, forced: Math.round(s.forcedStyleAndLayoutDuration || 0) })) });
        if (P.loaf.length > 400) P.loaf.splice(0, 100);
      }
    }).observe({ type: 'long-animation-frame', buffered: true });
  } catch (e) { P.errors.push('loaf: ' + e); }
})();`;

// in-page: the long tasks (and long frames) that started at or after t0
export function takeSince(t0) {
  const P = window.__perf || { lt: [], loaf: [] };
  return { lt: P.lt.filter(e => e.start >= t0), loaf: P.loaf.filter(e => e.start >= t0), errors: P.errors || [] };
}

// in-page: wait ms of real time, then for an idle period (a requestIdleCallback that is not a timeout)
export function quiet(ms) {
  return new Promise(res => setTimeout(() => {
    let n = 0;
    const idle = d => { if (!d.didTimeout || ++n > 4) res(); else requestIdleCallback(idle, { timeout: 1500 }); };
    if (typeof requestIdleCallback === 'function') requestIdleCallback(idle, { timeout: 1500 }); else res();
  }, ms));
}

// in-page (hidden30*): the page's writes in the list of each view, the footer and the sidebar item.
// Hidden: every write counts (attributes too). Shown again: each content batch (a MutationObserver
// callback with a row or text change; the glow and glide classes that follow are attributes) is
// timed per region, and the first one in a region records what it brought: in the view shown (cur)
// its row count and the newest line number on the page, in the badge its text. "Caught up in one
// write" (requirement 6, review A S1 / B S5): the first list batch already holds what the end holds.
export function installWrites() {
  const ev = document.getElementById('logs-sc-events');
  const cur = ev && !ev.hidden ? 'events' : 'raw';
  const regionOf = n => {
    const e = n && (n.nodeType === 1 ? n : n.parentElement); if (!e) return 'other';
    if (e.closest('#logs-events')) return 'events';
    if (e.closest('#logs-rows')) return 'raw';
    if (e.closest('#logs-foot')) return 'foot';
    if (e.closest('.nav-item[data-nav="logs"]')) return 'badge';
    return 'other';
  };
  const rowsSel = { events: '#logs-events .logs-row', raw: '#logs-rows .logs-rrow' };
  const newest = () => Math.max(0, ...[...document.querySelectorAll('#logs-events .logs-row .logs-t, #logs-rows .logs-rrow .logs-msg')].map(e => Number((/(\d+)\s*$/.exec(e.textContent) || [])[1]) || 0));
  const badgeText = () => { const b = document.querySelector('.nav-item[data-nav="logs"] .logs-navbadge'); return b && !b.hidden ? b.textContent : null; };
  const tally = window.__writes = { hidden: 0, shown: 0, shownBatches: 0, shownAt: [], phase: 'hidden', cur, regions: {}, first: {} };
  const o = new MutationObserver(ms => {
    tally[tally.phase] += ms.length;
    if (tally.phase !== 'shown') return;
    const content = ms.filter(m => m.type !== 'attributes');
    if (!content.length) return;
    const at = Math.round(performance.now() - tally.t0);
    tally.shownBatches++;
    if (tally.shownAt.length < 20) tally.shownAt.push(at);
    for (const r of new Set(content.map(m => regionOf(m.target)))) {
      const list = tally.regions[r] || (tally.regions[r] = []);
      if (list.length < 20) list.push(at);
      if (tally.first[r]) continue;
      tally.first[r] = r === cur ? { at, rows: document.querySelectorAll(rowsSel[cur]).length, newest: newest() } : r === 'badge' ? { at, badge: badgeText() } : { at };
    }
  });
  for (const sel of ['#logs-rows', '#logs-events', '#logs-foot', '.nav-item[data-nav="logs"]']) { const el = document.querySelector(sel); if (el) o.observe(el, { childList: true, subtree: true, characterData: true, attributes: true }); }
  window.__writesObs = o;
  return cur;
}
// in-page: the end state the first batch is compared with
export function writesEnd() {
  window.__writesObs.takeRecords(); window.__writesObs.disconnect();
  const w = window.__writes;
  const sel = w.cur === 'events' ? '#logs-events .logs-row' : '#logs-rows .logs-rrow';
  const b = document.querySelector('.nav-item[data-nav="logs"] .logs-navbadge');
  return { writes: w, rows: document.querySelectorAll(sel).length, newest: Math.max(0, ...[...document.querySelectorAll('#logs-events .logs-row .logs-t, #logs-rows .logs-rrow .logs-msg')].map(e => Number((/(\d+)\s*$/.exec(e.textContent) || [])[1]) || 0)), badge: b && !b.hidden ? b.textContent : null };
}

// ------------------------------------------------------------------------------------ the machine
export function timingConvention() {
  const dir = join(ORDERS, 'evidence');
  const f = readdirSync(dir).find(n => /^CPU_.*\.txt$/.test(n));
  const conv = { file: f || null, mode: 'normal' };
  if (f) for (const line of readFileSync(join(dir, f), 'utf8').split(/\r?\n/)) { const m = /^\s*(mode|cargo_jobs|cargo_priority)\s*=\s*(\S+)/.exec(line); if (m) conv[m[1]] = m[2]; }
  return conv;
}

let cpuModel = null;
export function machine() {
  if (!cpuModel) {
    const r = spawnSync('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', '(Get-CimInstance Win32_Processor | Select-Object -First 1 -ExpandProperty Name).Trim()'], { encoding: 'utf8', timeout: 30000 });
    cpuModel = (r.stdout || '').trim() || (cpus()[0] && cpus()[0].model) || 'unknown';
  }
  return { cpu: cpuModel, logical_cores: cpus().length, platform: process.platform, node: process.version };
}

// 30 one-second samples of the total processor time -> {avg, samples, at}
export function sampleCpu(seconds = 30) {
  const ps = `$ErrorActionPreference='Stop'; $s = Get-Counter '\\Processor(_Total)\\% Processor Time' -SampleInterval 1 -MaxSamples ${seconds}; ($s.CounterSamples | ForEach-Object { [math]::Round($_.CookedValue, 2) }) -join ','`;
  const at = new Date().toISOString();
  const r = spawnSync('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', ps], { encoding: 'utf8', timeout: (seconds + 60) * 1000 });
  let samples = (r.stdout || '').trim().split(',').map(Number).filter(Number.isFinite);
  let via = 'Get-Counter';
  if (samples.length < seconds / 2) {
    // (a localized counter name: typeperf takes the same English path through PDH)
    const t = spawnSync('typeperf', ['\\Processor(_Total)\\% Processor Time', '-si', '1', '-sc', String(seconds)], { encoding: 'utf8', timeout: (seconds + 60) * 1000 });
    samples = (t.stdout || '').split(/\r?\n/).map(l => /^"[^"]+","([\d.]+)"/.exec(l)).filter(Boolean).map(m => Number(m[1]));
    via = 'typeperf';
  }
  if (!samples.length) throw new Error(`CPU sampling failed: ${(r.stderr || '').slice(-300)}`);
  const avg = Math.round((samples.reduce((a, b) => a + b, 0) / samples.length) * 100) / 100;
  return { at, via, seconds: samples.length, avg, max: Math.max(...samples), samples };
}

// waits for an idle machine (avg < 10 %), up to waitMin minutes; -> the accepted sample (and the
// refused ones); throws when the machine stayed busy
export async function cpuIdle({ waitMin = 10, log = console.log, label = '' } = {}) {
  const tries = [];
  const end = Date.now() + waitMin * 60000;
  for (;;) {
    const s = sampleCpu(30);
    tries.push(s);
    log(`[cpu] ${label} 30 s average ${s.avg} % (max ${s.max} %)`);
    if (s.avg < 10) return { ok: true, accepted: s, refused: tries.slice(0, -1) };
    if (Date.now() > end) return { ok: false, accepted: null, refused: tries };
    await sleep(30000);
  }
}

// ------------------------------------------------------------------------------------ one at a time
// (review B S1) the CPU gate is sampled before a round only: a second measuring run started meanwhile
// (another session's perf or a11y --shell) would go unseen. Every perf run and every a11y run takes
// evidence/T-P4-09/.measure.lock (created exclusively; {pid, tag, started_at}) and removes it when it
// ends; a lock whose holder is alive refuses the run, a stale one (holder gone) is taken over.
export const LOCK = join(EVIDENCE, '.measure.lock');
const pidAlive = pid => { try { process.kill(pid, 0); return true; } catch (e) { return e.code === 'EPERM'; } };
export function acquireLock(tag) {
  mkdirSync(EVIDENCE, { recursive: true });
  const me = { pid: process.pid, tag, started_at: new Date().toISOString() };
  for (let i = 0; i < 2; i++) {
    try {
      writeFileSync(LOCK, JSON.stringify(me) + '\n', { flag: 'wx' });
      let released = false;
      const release = () => {
        if (released) return; released = true;
        try { const cur = JSON.parse(readFileSync(LOCK, 'utf8')); if (cur.pid === process.pid) rmSync(LOCK, { force: true }); } catch (e) { /* gone */ }
      };
      process.once('exit', release);
      for (const sig of ['SIGINT', 'SIGTERM', 'SIGBREAK']) process.once(sig, () => { release(); process.exit(130); });
      return { lock: me, release };
    } catch (e) {
      if (e.code !== 'EEXIST') throw e;
      let held = null; try { held = JSON.parse(readFileSync(LOCK, 'utf8')); } catch (x) { /* half written or unreadable */ }
      if (held && Number.isInteger(held.pid) && held.pid !== process.pid && pidAlive(held.pid)) {
        throw new Error(`another measuring run holds ${LOCK} (pid ${held.pid}, ${held.tag}, since ${held.started_at}): not measuring now`);
      }
      // the holder is gone (or the file unreadable for long): a stale lock, taken over
      try { rmSync(LOCK, { force: true }); } catch (x) { /* raced */ }
    }
  }
  throw new Error(`could not take ${LOCK}`);
}

// ------------------------------------------------------------------------------------ judging
// value of one run: {longest (ms, 0 = no long task), ...}; a run passes when longest <= LONG_MS
// and (when the item has it) its own extra check passes.
export function judgeRound(runs) {
  const longest = Math.max(0, ...runs.map(r => r.longest || 0));
  const ok = runs.every(r => r.ok);
  return { longest, ok, runs: runs.length };
}
export function verdict(rounds) {
  const passed = rounds.filter(r => r.ok).length;
  return { rounds: rounds.length, passed, ok: rounds.length >= 3 ? passed >= 2 : false };
}
