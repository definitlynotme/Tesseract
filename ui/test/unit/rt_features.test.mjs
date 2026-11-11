// T-P4-02a: two-key release, the feature registry and the features gate.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { createFeatures } from '../../src/rt/features.js';
import { FEATURE_PAGES, FEATURE_REGISTRY } from '../../src/rt/feature_registry.js';
import { checkFeatures, findFeatureCalls, loadGates, parseFeatureRegistry, runRule } from '../../tools/scan.mjs';
import { UI, live } from './rt_helpers.mjs';

test('feature(): both keys needed; the four combinations', () => {
  for (const [enabled, declared, want] of [[false, false, false], [true, false, false], [false, true, false], [true, true, true]]) {
    const f = createFeatures({ enabled: enabled ? ['B-31'] : [], caps: () => ({ features: declared ? ['B-31'] : [] }) });
    assert.equal(f.feature('B-31'), want, `build=${enabled} sidecar=${declared}`);
  }
  // an unregistered name is never on, even with both keys
  const diags = [];
  const f = createFeatures({ enabled: ['B-99'], caps: () => ({ features: ['B-99'] }), diag: d => diags.push(d) });
  assert.equal(f.feature('B-99'), false);
  f.feature('B-99');
  assert.equal(diags.length, 1, 'reported once');
  assert.equal(createFeatures({ enabled: ['B-31'] }).feature('B-31'), false, 'no hello yet: caps unknown');
  assert.ok(Object.isFrozen(createFeatures({ enabled: ['B-31'] }).enabled));
});

test('T.rt.feature reads hello.caps.features at run time', async () => {
  const { rt } = await live({ enabledFeatures: ['B-05'], hello: { features: ['B-05', 'B-06'] } });
  assert.equal(rt.feature('B-05'), true);
  assert.equal(rt.feature('B-06'), false, 'sidecar has it, the build did not release it');
});

test('registry: every B entry has name, page, controls and a fallback; unique per (name, page)', () => {
  const seen = new Set();
  const FALLBACKS = new Set(['hide', 'readonly', 'disable', 'copy', 'legacy', 'limit']);
  for (const e of FEATURE_REGISTRY) {
    assert.match(e.name, /^B-\d{2}[a-z]?$/, e.name);
    assert.ok(e.b_item.startsWith(e.name.replace(/([a-z])$/, '')), `${e.name} b_item ${e.b_item}`);
    assert.ok(FEATURE_PAGES.includes(e.page), `${e.name} page ${e.page}`);
    assert.ok(Array.isArray(e.controls) && e.controls.length > 0, `${e.name} controls`);
    assert.ok(FALLBACKS.has(e.fallback), `${e.name} fallback ${e.fallback}`);
    assert.ok(typeof e.note === 'string' && e.note.length > 0);
    const k = e.name + '@' + e.page;
    assert.ok(!seen.has(k), `duplicate ${k}`);
    seen.add(k);
    for (const s of [e.note, ...e.controls]) assert.ok(!/[\u3000-\u9fff\uff00-\uffef]/.test(s), `no CJK in ${e.name}`);
  }
  const perPage = Object.fromEntries(FEATURE_PAGES.map(p => [p, FEATURE_REGISTRY.filter(e => e.page === p).length]));
  assert.deepEqual(perPage, { overview: 2, tiers: 18, logs: 6, settings: 14 });
  // items with no page control of their own are not registered
  for (const n of ['B-02a', 'B-19', 'B-26', 'B-29', 'B-38', 'B-39']) assert.ok(!FEATURE_REGISTRY.some(e => e.name === n), n);
});

test('the gate parses the registry exactly as the runtime sees it', () => {
  const parsed = parseFeatureRegistry(readFileSync(join(UI, 'src', 'rt', 'feature_registry.js'), 'utf8'));
  assert.deepEqual(parsed.map(e => e.name + '@' + e.page), FEATURE_REGISTRY.map(e => e.name + '@' + e.page));
});

test('features.json is consistent with the registry; the features rule passes and is enforced', () => {
  const enabled = JSON.parse(readFileSync(join(UI, 'src', 'features.json'), 'utf8'));
  const names = new Set(FEATURE_REGISTRY.map(e => e.name));
  for (const n of enabled) assert.ok(names.has(n), `features.json lists ${n}, which is not registered`);
  assert.equal(loadGates().features, 'enforce');
  const r = runRule('features');
  assert.deepEqual(r.findings, []);
});

test('features rule checks 1-3 on synthetic input', () => {
  const registry = [{ name: 'B-01', page: 'overview' }, { name: 'B-03', page: 'tiers' }];
  const calls = findFeatureCalls("if (T.rt.feature('B-01')) {}\nif (T.rt.feature('B-77')) {}\nT.rt.feature(name);\n", 'src/pages/x.js');
  const out = checkFeatures({ registry, enabled: ['B-01', 'B-55'], calls, pages: ['tiers'] });
  assert.deepEqual(out.map(f => [f.check, f.text]), [
    ['enabled-unregistered', 'B-55'], ['call-unregistered', 'B-77'], ['dynamic-name', 'T.rt.feature(<dynamic>)'], ['unreferenced', 'B-03 (tiers)'],
  ]);
  assert.deepEqual(checkFeatures({ registry, enabled: [], calls: [], pages: [] }), [], 'check 3 only for pages listed in features_pages');
});
