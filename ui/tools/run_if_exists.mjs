// npm script shim (T-P4-01): run a test driver if it exists, otherwise print
// "未实现" and exit 0, so package.json scripts can be written once (P4 architecture §1.1.1).
import { existsSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { resolve } from 'node:path';

const [script, ...args] = process.argv.slice(2);
if (!script) {
  console.error('usage: node tools/run_if_exists.mjs <script> [args...]');
  process.exit(2);
}
const path = resolve(script);
if (!existsSync(path)) {
  console.log(`未实现：${script}`);
  process.exit(0);
}
const r = spawnSync(process.execPath, [path, ...args], { stdio: 'inherit' });
process.exit(r.status ?? 1);
