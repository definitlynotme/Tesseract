// T-P4-09 performance measurement (P4 architecture §10; ticket requirements 1–7).
//
//   node ui/test/perf/run.mjs --target design|shell [--items a,b] [--runs 5] [--rounds 3]
//        [--cpu-wait <min>] [--no-cpu-gate] [--label <name>] [--build] [--real-sidecar]
//        [--dist <design build>] [--stale-ui] [--keep]
//
//   --target design   playwright-core + the local Chrome, the design target in fixture mode
//                     (design.mjs)
//   --target shell    the debug shell's WebView2 over CDP (TESSERACT_SHELL_CDP_PORT), custom scope,
//                     the T-P4-11 stand-in sidecar (shell.mjs); --build rebuilds it first
//                     (scripts/shell_copy_ui.ps1 + cargo build, CPU convention); --real-sidecar adds
//                     the first-screen measurement on the source sidecar (T-P2-33 hardened sandbox);
//                     --stale-ui measures the shell as built (its embedded UI may be another source
//                     state: the "before" runs); --dist measures a given design build (design)
//   PERF_TRACE=1      (design) also the longest task of each run from a Chrome trace, under 50 ms
//                     too: the margin (informational)
//
// Conditions (criticism E8): evidence/CPU_约定.txt must not say mode=timing; before every round the
// 30 s CPU average must be below 10 % (waited for up to --cpu-wait minutes, default 10, else the run
// stops: never measured on a busy machine). Every item runs --runs times (default 5) per round, the
// round's value is the worst run; 3 rounds always, a round over the limit is followed by 60 s of
// pause; the item passes when 2 of the 3 rounds pass. hidden30 runs once per round (30 s each).
//
// One measuring run at a time (review B S1): evidence/T-P4-09/.measure.lock is taken exclusively at
// the start (refused while its holder is alive) and removed at the end; the CPU is sampled again
// for 15 s after every round (cpu[].after_round, informational).
//
// Evidence: .build/<work orders>/evidence/T-P4-09/<target>-<label|stamp>/result.json (every run of
// every round, the CPU samples, the machine) and a one-line verdict per item on stdout.
// Exit 0 when every item passed.
import { join, relative } from 'node:path';
import { EVIDENCE, REPO, acquireLock, cpuIdle, judgeRound, machine, sampleCpu, sleep, stamp, timingConvention, verdict, writeJson } from './common.mjs';

// PERF_TRACE=1 (design): the longest task of each run from a Chrome trace, also under 50 ms
function traceMax(runs) {
  const vals = [];
  const take = o => { if (o && o.trace_longest && Number.isFinite(o.trace_longest.max_ms)) vals.push(o.trace_longest.max_ms); };
  for (const x of runs) { take(x); for (const g of [x.steps, x.switches]) if (g) Object.values(g).forEach(take); }
  return vals.length ? Math.max(...vals) : null;
}

function parseArgs(argv) {
  const o = { target: null, items: null, runs: 5, rounds: 3, cpuWait: 10, cpuGate: true, label: null, build: false, real: false, keep: false, dist: null, staleUi: false };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i], v = () => argv[++i];
    if (a === '--target') o.target = v();
    else if (a === '--items') o.items = v().split(',').map(s => s.trim()).filter(Boolean);
    else if (a === '--runs') o.runs = Math.max(1, Number(v()) || 5);
    else if (a === '--rounds') o.rounds = Math.max(1, Number(v()) || 3);
    else if (a === '--cpu-wait') o.cpuWait = Math.max(0, Number(v()) || 0);
    else if (a === '--no-cpu-gate') o.cpuGate = false;
    else if (a === '--label') o.label = v();
    else if (a === '--build') o.build = true;
    else if (a === '--real-sidecar') o.real = true;
    else if (a === '--keep') o.keep = true;
    else if (a === '--dist') o.dist = v();
    else if (a === '--stale-ui') o.staleUi = true;
    else throw new Error(`unknown argument: ${a}`);
  }
  if (!['design', 'shell'].includes(o.target)) throw new Error('--target design|shell is required');
  return o;
}

async function main() {
  const o = parseArgs(process.argv.slice(2));
  const conv = timingConvention();
  if (conv.mode === 'timing') throw new Error(`evidence/${conv.file}: mode=timing (another session is running timing experiments): not measuring now`);
  // (review B S1) one measuring run at a time: evidence/T-P4-09/.measure.lock
  const held = acquireLock(`perf ${o.target} ${o.label || ''}`.trim());
  const mod = o.target === 'design' ? await import('./design.mjs') : await import('./shell.mjs');
  const items = o.items || mod.ITEMS;
  for (const it of items) if (!mod.ITEMS.includes(it)) throw new Error(`unknown item ${it} (${mod.ITEMS.join(', ')})`);
  const dir = join(EVIDENCE, `${o.target}-${o.label || stamp()}`);
  const result = { ticket: 'T-P4-09', target: o.target, started_at: new Date().toISOString(), machine: machine(), convention: conv,
    rule: { runs_per_round: o.runs, rounds: o.rounds, pass: '>= 2 of 3 rounds; a round = the worst of its runs; long task > 50 ms fails', cpu_gate: o.cpuGate ? '30 s average < 10 %' : 'off' },
    lock: held.lock, cpu: [], items: {}, verdict: {} };
  const log = (...a) => console.log(...a);
  let env = null;
  try {
    env = await mod.setup({ build: o.build, real: o.real, evidence: dir, keep: o.keep, log, dist: o.dist, staleUi: o.staleUi });
    result.env = env.facts;
    const failedLast = new Set();
    for (let r = 1; r <= o.rounds; r++) {
      if (r > 1 && failedLast.size) { log(`[round ${r}] a round was over the limit (${[...failedLast].join(', ')}): 60 s pause`); await sleep(60000); }
      if (o.cpuGate) {
        await env.pause?.();
        const c = await cpuIdle({ waitMin: o.cpuWait, log, label: `round ${r}` });
        result.cpu.push({ round: r, ...c });
        if (!c.ok) throw new Error(`CPU not idle before round ${r} (30 s averages ${c.refused.map(x => x.avg).join(', ')} %): not measured`);
        await env.resume?.();
      }
      failedLast.clear();
      for (const it of items) {
        const n = mod.ONCE_PER_ROUND.has(it) ? 1 : o.runs;
        const runs = [];
        for (let i = 0; i < n; i++) {
          let run;
          try { run = await env.runItem(it); } catch (e) { run = { longest: null, ok: false, error: String(e && e.stack || e).slice(0, 1500) }; }
          run.at = new Date().toISOString();
          runs.push(run);
        }
        const round = judgeRound(runs);
        if (runs.some(x => x.error)) round.ok = false;
        const entry = result.items[it] || (result.items[it] = { rounds: [] });
        entry.rounds.push({ round: r, ...round, data: runs });
        if (!round.ok) failedLast.add(it);
        const extra = runs.map(x => (x.frame_ms != null ? `${x.frame_ms}ms-frame` : x.delta ? `layout+${x.delta.LayoutCount}/style+${x.delta.RecalcStyleCount}/writes ${x.writes_hidden != null ? x.writes_hidden : x.after && x.after.writes ? x.after.writes.hidden : '?'}` : '')).filter(Boolean).join(' ');
        const tmax = traceMax(runs);
        if (tmax != null) round.trace_longest_ms = tmax;
        log(`[round ${r}] ${it}: ${round.ok ? 'ok  ' : 'OVER'} worst long task ${round.longest} ms (runs ${runs.map(x => (x.error ? 'ERR' : x.longest)).join(', ')})${tmax != null ? ` [trace: longest task ${tmax} ms]` : ''}${extra ? ' ' + extra : ''}${runs.find(x => x.error) ? ' ' + runs.find(x => x.error).error.split('\n')[0] : ''}`);
        writeJson(join(dir, 'result.json'), result);
      }
      // (review B S1) the CPU right after the round as well (informational: the gate is the 30 s
      // before it; a busy machine during the round shows here)
      if (o.cpuGate) {
        const after = sampleCpu(15);
        const c = result.cpu.find(x => x.round === r);
        if (c) c.after_round = { at: after.at, seconds: after.seconds, avg: after.avg, max: after.max, samples: after.samples };
        log(`[cpu] after round ${r}: 15 s average ${after.avg} % (max ${after.max} %)`);
        writeJson(join(dir, 'result.json'), result);
      }
    }
    for (const [it, e] of Object.entries(result.items)) result.verdict[it] = verdict(e.rounds);
    if (env.extra) result.extra = await env.extra();
  } catch (e) {
    result.error = String(e && e.stack || e);
  } finally {
    if (env) { try { result.cleanup = await env.close(); } catch (e) { result.cleanup = `close failed: ${e && e.message || e}`; result.error = result.error || result.cleanup; } }
    result.ended_at = new Date().toISOString();
    result.passed = !result.error && Object.keys(result.verdict).length === items.length && Object.values(result.verdict).every(v => v.ok);
    writeJson(join(dir, 'result.json'), result);
    held.release();
  }
  for (const [it, v] of Object.entries(result.verdict)) console.log(`${v.ok ? 'PASS' : 'FAIL'} ${it}: ${v.passed}/${v.rounds} rounds (worst per round ${result.items[it].rounds.map(r => r.longest).join(' / ')} ms)`);
  console.log(`perf ${o.target}: ${result.passed ? 'OK' : 'FAILED'} -> ${relative(REPO, join(dir, 'result.json')).split('\\').join('/')}${result.error ? '\n' + result.error : ''}`);
  return result.passed ? 0 : 1;
}

main().then(c => { process.exitCode = c; }, e => { console.error(e && e.stack || e); process.exitCode = 1; });
