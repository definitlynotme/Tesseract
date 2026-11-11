// T-P4-02a: gen_rt_spec.py, validate_ui_fixtures.py, the spec table, the app bundle without
// the simulator, and src/rt free of CJK literals.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import * as esbuild from 'esbuild';
import { defineFor } from '../../build.mjs';
import { runRule } from '../../tools/scan.mjs';
import { SCENARIOS, UI, boot, flush, python } from './rt_helpers.mjs';

const SPEC = JSON.parse(readFileSync(join(UI, 'src', 'rt', 'spec.gen.json'), 'utf8'));

test('gen_rt_spec.py --check: spec.gen.json is in sync with bridge_protocol', () => {
  const r = python(['ui/tools/gen_rt_spec.py', '--check']);
  assert.equal(r.status, 0, r.stdout + r.stderr);
  const out = python(['ui/tools/gen_rt_spec.py', '--stdout']);
  assert.equal(out.stdout.replace(/\r\n/g, '\n'), readFileSync(join(UI, 'src', 'rt', 'spec.gen.json'), 'utf8'));
});

test('spec.gen.json carries names and dispatch metadata only (no args, fields or enums)', () => {
  assert.equal(SPEC.proto, 1);
  for (const [name, c] of Object.entries(SPEC.commands)) {
    assert.deepEqual(Object.keys(c).sort(), ['cancel', 'domain', 'reply', 'source'], name);
    assert.ok(['job', 'res', 'private'].includes(c.reply), name);
  }
  for (const q of Object.values(SPEC.queries)) assert.deepEqual(q, { domain: 'normal', source: 'page', reply: 'res', cancel: 'owner' });
  for (const [name, t] of Object.entries(SPEC.topics)) assert.deepEqual(Object.keys(t).sort(), ['keyed', 'kinds', 'patchable', 'shell_only'], name);
  assert.deepEqual(Object.keys(SPEC.topics).filter(t => SPEC.topics[t].patchable).sort(), ['bastion.sources', 'config.items', 'nodes.list', 'subscriptions.list']);
  assert.deepEqual(Object.keys(SPEC.topics).filter(t => SPEC.topics[t].keyed).sort(), ['nodes.list', 'subscriptions.list']);
  assert.deepEqual(Object.keys(SPEC.topics).filter(t => SPEC.topics[t].shell_only).sort(), ['alerts.desktop', 'shutdown', 'update.prepareExit']);
  // 10-30 alert redesign: alerts.onboarding.* (4), alerts.catalog / alerts.deliveries and the
  // alerts.delivery / alerts.onboarding topics left the protocol
  assert.equal(Object.keys(SPEC.commands).length, 34);
  assert.equal(Object.keys(SPEC.queries).length, 8);
  assert.equal(Object.keys(SPEC.topics).length, 29);
  assert.deepEqual(Object.keys(SPEC.commands).filter(n => SPEC.commands[n].reply === 'private'), ['nodes.reveal']);
  assert.ok(!JSON.stringify(SPEC).includes('args'));
});

test('validate_ui_fixtures.py: every rt_* scenario passes the Python contract check', () => {
  const r = python(['ui/tools/validate_ui_fixtures.py']);
  assert.equal(r.status, 0, r.stdout + r.stderr);
  assert.match(r.stdout, /OK: \d+ file/);
  const mine = readdirSync(SCENARIOS).filter(n => n.startsWith('rt_'));
  assert.ok(mine.length >= 5);
  for (const n of mine) {
    const one = python(['ui/tools/validate_ui_fixtures.py', join(SCENARIOS, n)]);
    assert.equal(one.status, 0, n + one.stderr);
  }
});

test('every rt_* scenario replays through the client with no unmatched call', async () => {
  for (const n of readdirSync(SCENARIOS).filter(x => x.startsWith('rt_'))) {
    const { rt, fake } = boot({ scenario: JSON.parse(readFileSync(join(SCENARIOS, n), 'utf8')) });
    await flush();
    assert.deepEqual(fake.failures, [], n);
    assert.equal(rt.state, 'live', n);
    assert.ok(!rt.diag().some(d => ['frame_threw', 'bad_frame', 'unknown_frame', 'bad_evt'].includes(d.kind)), n);
  }
});

test('the app bundle does not contain design/sim.js; the design bundle still does', async () => {
  const inputs = async target => {
    const r = await esbuild.build({ absWorkingDir: UI, entryPoints: [join(UI, 'src', 'entry', `${target}.js`)], bundle: true, write: false,
      format: 'iife', target: 'chrome120', metafile: true, logLevel: 'silent', define: defineFor(target, []),
      loader: { '.woff2': 'empty', '.woff': 'empty' }, external: ['*.woff2'] });
    return Object.keys(r.metafile.inputs).map(p => p.replace(/\\/g, '/'));
  };
  const app = await inputs('app');
  assert.ok(!app.some(p => p.endsWith('src/design/sim.js')), 'app bundles no simulator');
  // (T-P4-07: the transitional thin simulator and the event alias are deleted; the app bundle
  // takes nothing from src/design/)
  for (const f of ['src/rt/client.js', 'src/rt/transport-tauri.js']) assert.ok(app.some(p => p.endsWith(f)), f);
  assert.deepEqual(app.filter(p => /src\/rt\/(legacy_sim|alias)\.js$|src\/design\//.test(p)), [], 'no legacy_sim / alias / design input');
  const design = await inputs('design');
  assert.ok(design.some(p => p.endsWith('src/design/sim.js')));
  assert.ok(design.some(p => p.endsWith('src/rt/transport-fake.js')));
});

test('src/rt carries no CJK literal and no @i18n-pending marker', () => {
  const r = runRule('cjk');
  const mine = [...r.findings, ...r.pending].filter(f => f.file.startsWith('src/rt/') || /^src\/boot\/rt-/.test(f.file));
  assert.deepEqual(mine, []);
});
