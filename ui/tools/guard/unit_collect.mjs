// UI guard item 5 (T-P6-05, Claude ruling 1 of 10-26): which test files and how many test cases
// `node --test ui/test/unit/` collects, without running a single test.
//
//   node ui/tools/guard/unit_collect.mjs <ui/test/unit dir>
//
// Loads the directory exactly as that command does (Node 24 resolves a directory argument as a
// module: ui/test/unit/index.js, which imports every *.test.mjs next to it), with `node:test`
// replaced inside ui/test/unit by a counting stand-in (unit_collect_fake_test.mjs). Prints one JSON
// line: {"files": {"<abs path>": cases}, "total": n, "error": null | "..."}. A test file that fails
// to import makes index.js set a non-zero exit code: reported as "error".
import { createRequire, register } from 'node:module';
import { fileURLToPath, pathToFileURL } from 'node:url';
import path from 'node:path';

const dir = path.resolve(process.argv[2] || 'ui/test/unit');
const here = path.dirname(fileURLToPath(import.meta.url));
register(pathToFileURL(path.join(here, 'unit_collect_hooks.mjs')).href, import.meta.url, {
  data: { fakeUrl: pathToFileURL(path.join(here, 'unit_collect_fake_test.mjs')).href },
});

const errors = [];
const origError = console.error;
console.error = (...args) => { errors.push(args.map(a => (a && a.stack) || String(a)).join(' ')); origError(...args); };

let reported = false;
function report() {
  if (reported) return;
  reported = true;
  const cases = globalThis.__guardUnitCases || new Map();
  const files = {};
  let total = 0;
  for (const [url, n] of cases) {
    const key = url === '?' ? '?' : fileURLToPath(url);
    files[key] = n;
    total += n;
  }
  const error = process.exitCode && process.exitCode !== 0 ? (errors.join('\n').slice(0, 2000) || `exit code ${process.exitCode}`) : null;
  process.stdout.write(JSON.stringify({ dir, files, total, error }) + '\n');
}
process.on('beforeExit', report);

try {
  createRequire(import.meta.url)(dir);
} catch (err) {
  errors.push((err && err.stack) || String(err));
  process.exitCode = 1;
  report();
}
