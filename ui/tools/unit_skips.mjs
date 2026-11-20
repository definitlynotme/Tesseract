// The unit suite's skip register (ui/README.md §2: only a registered test may skip).
//
// node --test counts a skipped test as passing, and a test can skip on its own: the browser tests
// without Chrome (TESSERACT_UI_REQUIRE_CHROME=1 turns that into a failure), the fixture contract
// test of inject_api.test.mjs without the repository venv. CI therefore reads the run's TAP and
// fails on a skip that is not registered here, on a registered one whose reason changed, and on a
// registered one that no longer skips (fixed or renamed: take it off this list and the README).
//
//   node --test --test-reporter=spec --test-reporter-destination=stdout \
//        --test-reporter=tap --test-reporter-destination=unit.tap test/unit/     (from ui/)
//   node ui/tools/unit_skips.mjs unit.tap
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

export const REGISTERED = [
  {
    file: 'settings_i18n.test.mjs',
    name: 'regression: sim mode, deterministic fixture: the settings text tree equals the T-P4-01 baseline (allowed: data values split into translate="no" nodes)',
    reason: /^superseded by T-P4-05d /,
  },
  {
    file: 'settings_i18n.test.mjs',
    name: 'a drawer reopened by a language switch gives focus back to its opener (tile or in-use row) when it closes',
    reason: /^quarantined: /,
  },
];

/** The skipped tests of a node:test TAP report: [{name, reason}]. */
export function skipsOf(tap) {
  const out = [];
  for (const line of tap.split(/\r?\n/)) {
    const m = /^\s*ok \d+ - (.*?) # SKIP(?: (.*))?$/.exec(line);
    if (m) out.push({ name: m[1].replace(/\\#/g, '#'), reason: m[2] || '' });
  }
  return out;
}

/** {problems, skips}: problems is empty when the skips are exactly the registered ones. */
export function checkSkips(tap, registered = REGISTERED) {
  const problems = [];
  const total = /^# tests (\d+)$/m.exec(tap);
  if (!total || !Number(total[1])) problems.push('the TAP report has no test count: did the run happen?');
  const skips = skipsOf(tap);
  for (const s of skips) {
    const r = registered.find(x => x.name === s.name);
    if (!r) problems.push(`skipped but not registered: ${s.name} (${s.reason || 'no reason given'})`);
    else if (!r.reason.test(s.reason)) problems.push(`registered skip with another reason: ${s.name} (${s.reason || 'no reason given'})`);
  }
  for (const r of registered) {
    if (!skips.some(s => s.name === r.name)) problems.push(`registered but not skipped (fixed or renamed?): ${r.name} (${r.file}); take it off ui/tools/unit_skips.mjs and ui/README.md §2`);
  }
  return { problems, skips };
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  const file = process.argv[2];
  if (!file) { console.error('usage: node ui/tools/unit_skips.mjs <node --test TAP report>'); process.exit(2); }
  const { problems, skips } = checkSkips(readFileSync(file, 'utf8'));
  for (const s of skips) console.log(`skip: ${s.name} — ${s.reason}`);
  for (const p of problems) console.log(`FAIL ${p}`);
  console.log(problems.length ? `unit skips: FAILED (${problems.length})` : `unit skips: OK (${skips.length} registered)`);
  process.exit(problems.length ? 1 : 0);
}
