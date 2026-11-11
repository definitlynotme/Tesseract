// T-P4-02a: the fixture boot of the design target (src/boot/rt-design.js), the only fixture
// entry (criticism C2) that T-P4-10 builds on. The real file is bundled with esbuild (the
// simulator import is stubbed: it belongs to T-P4-02b) and run with a fake window/location.
import { mock, test } from 'node:test';
import assert from 'node:assert/strict';
import { join } from 'node:path';
import * as esbuild from 'esbuild';
import { defineFor } from '../../build.mjs';
import { parseFeatureDefine } from '../../src/rt/features.js';
import { UI, flush, loadScenario, makeT } from './rt_helpers.mjs';

let bundled = null;
async function bundle(features) {
  const key = JSON.stringify(features);
  if (bundled && bundled.key === key) return bundled.code;
  const stubSim = {
    name: 'stub-design-sim',
    setup(b) {
      b.onResolve({ filter: /design[\\/]sim\.js$/ }, () => ({ path: 'design-sim-stub', namespace: 'stub' }));
      b.onLoad({ filter: /.*/, namespace: 'stub' }, () => ({ contents: 'window.T.simLoaded = (window.T.simLoaded || 0) + 1;\n'
        + 'export function designBackend(T) { T.simBackendAsked = (T.simBackendAsked || 0) + 1; return { attach() {}, replay: () => [], invoke() {} }; }', loader: 'js' }));
    },
  };
  const r = await esbuild.build({ absWorkingDir: UI, entryPoints: [join(UI, 'src', 'boot', 'rt-design.js')], bundle: true, write: false,
    format: 'iife', target: 'chrome120', logLevel: 'silent', define: defineFor('design', features), plugins: [stubSim] });
  bundled = { key, code: r.outputFiles[0].text };
  return bundled.code;
}

// Run the bundled boot with `fixture` on window and `search` as location.search.
async function run({ fixture, search = '', features = [] } = {}) {
  const code = await bundle(features);
  const T = makeT();
  const win = { T };
  if (fixture !== undefined) win.__TESSERACT_FIXTURE__ = fixture;
  new Function('window', 'location', code)(win, { search });
  await flush(6);
  return T;
}

test('fixture boot: ?lang from the whitelist rewrites hello.lang; anything else leaves it alone', async () => {
  const sc = loadScenario('rt_connect_replay');
  const zh = await run({ fixture: sc });
  assert.equal(zh.simLoaded, 1, 'design/sim.js is still imported');
  assert.equal(zh.rt.state, 'live');
  assert.equal(zh.rt.transport, 'fake');
  assert.equal(zh.store.get('lang'), 'zh_CN', 'no ?lang: the scenario hello.lang');
  for (const lang of ['en_US', 'ja_JP', 'pseudo']) assert.equal((await run({ fixture: sc, search: `?lang=${lang}` })).store.get('lang'), lang, lang);
  for (const bad of ['xx_YY', 'en', 'zh_CN;alert(1)', '']) {
    assert.equal((await run({ fixture: sc, search: `?lang=${encodeURIComponent(bad)}` })).store.get('lang'), 'zh_CN', `?lang=${bad} is not whitelisted`);
  }
});

test('fixture boot: the scenario may be JSON text; without a fixture the simulator branch stays untouched', async () => {
  const sc = loadScenario('rt_connect_replay');
  const T = await run({ fixture: JSON.stringify(sc), search: '?lang=ko_KR' });
  assert.equal(T.rt.state, 'live');
  assert.equal(T.store.get('lang'), 'ko_KR');
  // (T-P4-07: the thin facade and the rt:* -> sim:* mirror are deleted with rt/legacy_sim.js and
  // rt/alias.js; the stubbed simulator installs nothing on T)
  assert.equal(T.sim, undefined, 'no thin facade');
  assert.ok(T.named('rt:caps').length >= 1, 'rt:caps is emitted');
  assert.deepEqual(T.events.map(e => e[0]).filter(n => n.startsWith('sim:')), [], 'no event is mirrored as sim:*');
  // sim mode starts T.rt's stale timer on the real clock: run it on mocked timers so it ends with the test
  mock.timers.enable({ apis: ['setInterval', 'setTimeout'] });
  let plain;
  try { plain = await run({}); } finally { mock.timers.reset(); }
  assert.equal(plain.rt.transport, 'fake', 'no fixture: sim mode (T-P4-02b): T.rt on FakeTransport over the simulator backend');
  assert.equal(plain.simBackendAsked, 1);
  assert.equal(plain.sim, undefined, 'no fixture: T.sim is the (stubbed) simulator, not the thin one');
  assert.equal(plain.simLoaded, 1);
});

test('__H_FEATURES__ is a JSON string define; the boot reads it into the second key', async () => {
  assert.equal(defineFor('design', ['B-31']).__H_FEATURES__, JSON.stringify('["B-31"]'));
  assert.deepEqual(parseFeatureDefine('["B-31",1]'), ['B-31']);
  assert.deepEqual(parseFeatureDefine(['B-31']), ['B-31']);
  assert.deepEqual(parseFeatureDefine('not json'), []);
  assert.deepEqual(parseFeatureDefine(undefined), []);
  const sc = loadScenario('rt_connect_replay');
  const hello = sc.steps.find(s => s.deliver && s.deliver.t === 'hello').deliver;
  hello.caps.features = ['B-31'];
  const on = await run({ fixture: sc, features: ['B-31'] });
  assert.equal(on.rt.feature('B-31'), true, 'both keys');
  const off = await run({ fixture: sc, features: [] });
  assert.equal(off.rt.feature('B-31'), false, 'the build key is missing');
});

// T-P4-H (决定_第五波裁定 10-4): rt-design.js hands its FakeTransport over as T.rt.__fake (both
// branches); boot/design.js reads it for __TESSERACT_DESIGN__ instead of wrapping
// FakeTransport.prototype.
let bundledHooks = null;
async function bundleWithHooks() {
  if (bundledHooks) return bundledHooks;
  const stubSim = {
    name: 'stub-design-sim',
    setup(b) {
      b.onResolve({ filter: /design[\/]sim\.js$/ }, () => ({ path: 'design-sim-stub', namespace: 'stub' }));
      b.onLoad({ filter: /.*/, namespace: 'stub' }, () => ({ contents: 'export function designBackend(T) { return { attach() {}, replay: () => [], invoke() {} }; }', loader: 'js' }));
    },
  };
  const r = await esbuild.build({ absWorkingDir: UI, stdin: { contents: "import './rt-design.js';\nimport './design.js';\n", resolveDir: join(UI, 'src', 'boot'), loader: 'js' },
    bundle: true, write: false, format: 'iife', target: 'chrome120', logLevel: 'silent', define: defineFor('design', []), plugins: [stubSim] });
  bundledHooks = r.outputFiles[0].text;
  return bundledHooks;
}

test('fixture hand-over: T.rt.__fake is the transport in use; __TESSERACT_DESIGN__ reads it, FakeTransport.prototype is not wrapped', async () => {
  const code = await bundleWithHooks();
  const sc = loadScenario('rt_connect_replay');
  const T = makeT();
  const win = { T, __TESSERACT_FIXTURE__: sc };
  new Function('window', 'location', code)(win, { search: '' });
  await flush(6);
  const fake = T.rt.__fake;
  assert.ok(fake, 'T.rt.__fake is set');
  assert.equal(fake.mode, 'fixture');
  assert.equal(T.rt.transport, 'fake');
  assert.equal(fake.connects, 1, 'it is the instance T.rt connected');
  const proto = Object.getPrototypeOf(fake);
  for (const name of ['connect', 'reconnect', 'push', 'invoke']) {
    assert.equal(Object.prototype.hasOwnProperty.call(fake, name), false, `${name}: no own override`);
    assert.doesNotMatch(proto[name].toString(), /seen\.includes|active = this/, `${name}: the prototype is the transport's own`);
  }
  const D = win.__TESSERACT_DESIGN__;
  assert.equal(D.fixture, true);
  assert.deepEqual(D.transport(), { failures: [], invocations: [], instances: 1 });
  // an unmatched call is a failure of that instance
  await T.rt.call('service.stop').catch(() => {});
  await flush();
  const t = D.transport();
  assert.equal(t.instances, 1);
  assert.equal(t.failures[0].kind, 'unmatched_invoke');
  assert.equal(t.invocations.at(-1).cmd, 'service.stop');
  // deliver() goes to the handed-over instance (one Channel delivery in order)
  const before = fake.order;
  D.deliver({ t: 'host:language', language: 'ja_JP', epoch: 'x' });
  assert.equal(fake.order, before + 1);
  assert.equal(T.store.get('lang'), 'ja_JP');
  // a rebuilt T.rt with its own transport is picked up; the first one's failures still count
  const prev = T.rt;
  T.rt = { __fake: { failures: [{ kind: 'later' }], invocations: [], deliver() {} } };
  assert.equal(D.transport().instances, 2);
  assert.deepEqual(D.transport().failures.map(f => f.kind), ['unmatched_invoke', 'later']);
  T.rt = prev;
});

test('fixture hand-over (fix round 1, S7-1): an instance registered at hand-over counts even when T.rt is rebuilt before the first read', async () => {
  const code = await bundleWithHooks();
  const T = makeT();
  const win = { T, __TESSERACT_FIXTURE__: loadScenario('rt_connect_replay') };
  new Function('window', 'location', code)(win, { search: '' });
  await flush(6);
  const first = T.rt.__fake;
  assert.deepEqual(win.__TESSERACT_FAKES__, [first], 'rt-design.js registers its instance at once');
  // a rebuild that hands over its own instance, replaced again before __TESSERACT_DESIGN__ is read
  const middle = { failures: [{ kind: 'middle' }], invocations: [], deliver() {} };
  win.__TESSERACT_FAKES__.push(middle);
  const prev = T.rt;
  T.rt = { __fake: { failures: [{ kind: 'last' }], invocations: [], deliver() {} } };
  const t = win.__TESSERACT_DESIGN__.transport();
  assert.equal(t.instances, 3);
  assert.deepEqual(t.failures.map(f => f.kind), ['middle', 'last']);
  T.rt = prev;
});

test('sim hand-over: T.rt.__fake is the sim-mode transport; __TESSERACT_DESIGN__ keeps fixture-only bookkeeping', async () => {
  const code = await bundleWithHooks();
  const T = makeT();
  const win = { T };
  mock.timers.enable({ apis: ['setInterval', 'setTimeout'] });
  try {
    new Function('window', 'location', code)(win, { search: '' });
    await flush(6);
  } finally { mock.timers.reset(); }
  assert.equal(T.rt.__fake.mode, 'sim');
  assert.equal(win.__TESSERACT_DESIGN__.fixture, false);
  assert.deepEqual(win.__TESSERACT_DESIGN__.transport(), { failures: [], invocations: [], instances: 0 });
  assert.throws(() => win.__TESSERACT_DESIGN__.deliver({ t: 'host:language', language: 'ja_JP' }), /no fixture transport/);
});
