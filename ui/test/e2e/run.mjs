// T-P4-11 end-to-end entry (`npm --prefix ui run test:e2e`, via tools/run_if_exists.mjs).
//
//   node ui/test/e2e/run.mjs [args...]
//       Runs scripts/p4_ui_e2e.ps1 with the same arguments (it builds the shell, makes one custom
//       scope per flow and drives this file per launch). -SkipBuild, -RealSidecar, -Flows a,b.
//   node ui/test/e2e/run.mjs --list [--real]
//       The flows and the shell launches each needs (args, extra environment, fixture options), as
//       JSON for the script.
//   node ui/test/e2e/run.mjs --flow <name> --launch <n> --port <cdp> --pid <shell> --data <dir>
//                            --evidence <dir> [--real]
//       One launch of one flow against a running debug shell (the script started it under
//       process_watch.py in a custom scope). Prints one line "E2E_RESULT <json>" and writes
//       <evidence>/launch-<n>.json; exit 0 when the launch passed.
import { spawnSync } from 'node:child_process';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { Failure, REPO, openSession, parseArgs, writeJson } from './driver.mjs';

export const FLOWS = ['connect', 'reconnect', 'reset', 'commands', 'notify', 'quit', 'language', 'navigate', 'visibility', 'autostart', 'motion', 'keys', 'recover', 'list_stale', 'reader', 'recreate'];

async function load(name) {
  if (!FLOWS.includes(name)) throw new Error(`unknown flow ${name}`);
  return import(pathToFileURL(join(REPO, 'ui', 'test', 'e2e', 'flows', `${name}.mjs`)).href);
}

async function list(real) {
  const out = [];
  for (const name of FLOWS) {
    const f = await load(name);
    const launches = typeof f.launches === 'function' ? f.launches(real) : f.launches;
    out.push({ name, real: !!f.real, skip_real: f.skipReal || null, launches });
  }
  return out;
}

async function runOne(a) {
  const real = a.real === true || a.real === 'true';
  const launch = Number(a.launch || 0);
  const result = { flow: a.flow, launch, real, passed: false, error: null, checks: {}, started_at: new Date().toISOString() };
  let s = null;
  try {
    const f = await load(a.flow);
    s = await openSession({ port: a.port, pid: a.pid, data: a.data, evidence: a.evidence, real, python: a.python });
    await f.run(s, launch, real);
    result.passed = true;
  } catch (e) {
    result.error = e instanceof Failure ? e.message : (e && e.stack) || String(e);
  } finally {
    if (s) { result.checks = s.checks; result.known_fail = s.knownFails; result.known_fixed = s.knownFixed; await s.detach(); }
    result.ended_at = new Date().toISOString();
  }
  writeJson(join(a.evidence, `launch-${launch}.json`), result);
  console.log('E2E_RESULT ' + JSON.stringify(result));
  return result.passed ? 0 : 1;
}

const a = parseArgs(process.argv.slice(2));
if (a.list) {
  console.log(JSON.stringify(await list(a.real === true || a.real === 'true')));
} else if (a.flow) {
  process.exitCode = await runOne(a);
} else {
  const script = join(REPO, 'scripts', 'p4_ui_e2e.ps1');
  const r = spawnSync('powershell.exe', ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', script, ...process.argv.slice(2)], { stdio: 'inherit', cwd: REPO });
  process.exitCode = r.status ?? 1;
}
