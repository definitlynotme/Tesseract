// `npm run check` (T-P4-01; P4 architecture §1.1.1, §3.9, §6, §7). The only implementation
// of the gate sequence. Fixed order; a tool file that does not exist yet prints "未实现"
// and is skipped. Later tickets turn rules hard by editing their own line in
// tools/scan_gates.json or tools/i18n_gates.json — never this file.
//
//   1. build.mjs --check          in-memory build of both targets + bundle gates (demo)
//   2. scan.mjs                   source gates: cjk, inject, style, features
//   3. gen_rt_spec.py --check     (T-P4-02a) spec.gen.json in sync with bridge_protocol
//   4. validate_ui_fixtures.py    (T-P4-02a) UI fixtures pass bridge_protocol
//   5. i18n_check.mjs --level p4  (T-P4-03)
//
// Exit status is non-zero when any step fails. Rules in "report" mode never fail a step.
import { existsSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const UI = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const REPO = resolve(UI, '..');

function python() {
  if (process.env.TESSERACT_UI_PYTHON) return process.env.TESSERACT_UI_PYTHON;
  const venv = join(REPO, '.venv', 'Scripts', 'python.exe');
  return existsSync(venv) ? venv : join(REPO, '.venv', 'bin', 'python');
}

const steps = [
  { name: 'build.mjs --check', file: join(UI, 'build.mjs'), cmd: () => [process.execPath, [join(UI, 'build.mjs'), '--check']] },
  { name: 'scan.mjs', file: join(UI, 'tools', 'scan.mjs'), cmd: () => [process.execPath, [join(UI, 'tools', 'scan.mjs'), '--rule', 'cjk,inject,style,features']] },
  { name: 'gen_rt_spec.py --check', file: join(UI, 'tools', 'gen_rt_spec.py'), cmd: () => [python(), ['-B', join(UI, 'tools', 'gen_rt_spec.py'), '--check']] },
  { name: 'validate_ui_fixtures.py', file: join(UI, 'tools', 'validate_ui_fixtures.py'), cmd: () => [python(), ['-B', join(UI, 'tools', 'validate_ui_fixtures.py')]] },
  { name: 'i18n_check.mjs --level p4', file: join(UI, 'tools', 'i18n_check.mjs'), cmd: () => [process.execPath, [join(UI, 'tools', 'i18n_check.mjs'), '--level', 'p4']] },
];

const failed = [];
for (const step of steps) {
  if (!existsSync(step.file)) { console.log(`== ${step.name}: 未实现，跳过`); continue; }
  console.log(`== ${step.name}`);
  const [exe, args] = step.cmd();
  const r = spawnSync(exe, args, { stdio: 'inherit', cwd: REPO, env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1' } });
  if (r.status !== 0) { failed.push(step.name); console.log(`== ${step.name}: FAILED (exit ${r.status ?? r.error?.code})`); }
}
console.log(failed.length ? `check: FAILED (${failed.join(', ')})` : 'check: OK');
process.exit(failed.length ? 1 : 0);
