// 10-30 notice cards (ui/src/notice): the card model and the stack rules without a DOM, the time
// line, the spring curve, and the icons rebuilt from icons.js without markup sinks.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { createStack, MAX_VISIBLE, normalize, springCurve, timeLabel } from '../../src/notice/model.js';
import { iconNode, markNode, ringNode, SVG_NS } from '../../src/notice/svg.js';

const frame = (id, incident, extra = {}) => ({ delivery_id: id, incident, state: 'open', kind: 'outage', group: 'connection', severity: 'error',
  title_id: 'notice.outage.title', detail_id: 'notice.outage.detail', params: { minutes: 3 }, action: 'view', sticky: true, ttl_ms: 0,
  target: 'overview', title: 'Connection lost', detail: 'fallback', shell: false, ...extra });
const card = (id, incident, extra) => normalize(frame(id, incident, extra));
const ops = list => list.map(o => (o.op === 'more' ? `more:${o.count}` : `${o.op}:${o.card.incident}`));

test('normalize: tone, icon and level word by severity; recovered is ok; ttl defaults and bounds', () => {
  const c = card('d1', 'outage-1');
  assert.deepEqual([c.tone, c.icon, c.level, c.sticky, c.ttl, c.action, c.shell], ['err', 'error', 'notice.level.error', true, 0, true, false]);
  assert.deepEqual([c.titleId, c.detailId, c.reasonId, c.fixId], ['notice.outage.title', 'notice.outage.detail', null, null]);
  assert.deepEqual(c.params, { minutes: 3 });
  const warn = card('d2', 'fb', { severity: 'warn', sticky: false });
  assert.deepEqual([warn.tone, warn.icon, warn.level, warn.ttl], ['warn', 'warning', 'notice.level.warn', 10000]);
  const info = card('d3', 'up', { severity: 'info', sticky: false });
  assert.deepEqual([info.tone, info.icon, info.level, info.ttl], ['info', 'info', null, 8000]);
  const ok = card('d4', 'outage-1', { state: 'resolved', severity: 'info', sticky: false });
  assert.deepEqual([ok.tone, ok.icon, ok.level, ok.ttl], ['ok', 'success', null, 6000]);
  assert.equal(card('d5', 'x', { severity: 'critical' }).tone, 'err');
  assert.equal(card('d6', 'x', { sticky: false, ttl_ms: 10 }).ttl, 1500);
  assert.equal(card('d7', 'x', { sticky: false, ttl_ms: 1e9 }).ttl, 120000);
  assert.equal(card('d8', 'x', { sticky: true, ttl_ms: 5000 }).ttl, 0, 'sticky: no timer');
});

test('normalize: a legacy card is its own text; malformed ids and params are dropped; garbage is null', () => {
  const legacy = normalize({ delivery_id: 'l1', incident: 'delivery:l1', state: 'open', kind: 'legacy', severity: 'warn', title: 'Tier down', detail: 'Vanguard offline', action: 'view' });
  assert.deepEqual([legacy.titleId, legacy.title, legacy.detail, legacy.action], [null, 'Tier down', 'Vanguard offline', true]);
  const bad = card('b1', 'x', { title_id: '<img onerror=x>', detail_id: 'Notice.Detail', reason_id: 'a', params: { a: true, b: 'ok', c: 2, d: NaN, e: { x: 1 } } });
  assert.deepEqual([bad.titleId, bad.detailId, bad.reasonId], [null, null, null]);
  assert.deepEqual(bad.params, { b: 'ok', c: 2 });
  for (const garbage of [null, 'x', [], {}, { delivery_id: 'x' }, frame('', 'a'), frame('a', ''), frame('a', 'b', { state: 'gone' })]) {
    assert.equal(normalize(garbage), null, JSON.stringify(garbage));
  }
  assert.equal(card('s1', 'service_stopped', { shell: true }).shell, true);
});

test('stack: newest at the bottom, at most three on screen, the oldest fold and come back on top', () => {
  assert.equal(MAX_VISIBLE, 3);
  const s = createStack();
  assert.deepEqual(ops(s.apply(card('a', 'A'))), ['add:A', 'more:0']);
  s.apply(card('b', 'B')); s.apply(card('c', 'C'));
  assert.deepEqual(ops(s.apply(card('d', 'D'))), ['add:D', 'fold:A', 'more:1']);
  assert.deepEqual(ops(s.apply(card('e', 'E'))), ['add:E', 'fold:B', 'more:2']);
  assert.deepEqual(s.shown.map(c => c.incident), ['C', 'D', 'E']);
  assert.deepEqual(s.folded.map(c => c.incident), ['A', 'B']);
  // A card leaves: the most recently folded comes back on top.
  assert.deepEqual(ops(s.leave('D')), ['unfold:B', 'more:1']);
  assert.deepEqual(s.shown.map(c => c.incident), ['B', 'C', 'E']);
  assert.deepEqual(s.leave('nope'), [], 'a card that is not on screen');
});

test('stack: one card per incident; update in place, resolved morphs, a folded incident comes back newest', () => {
  const s = createStack();
  s.apply(card('a1', 'A')); s.apply(card('b1', 'B')); s.apply(card('c1', 'C')); s.apply(card('d1', 'D'));
  const up = s.apply(card('b2', 'B', { state: 'update' }));
  assert.deepEqual(ops(up), ['update:B', 'more:1']);
  assert.equal(up[0].prev.id, 'b1');
  const morph = s.apply(card('c2', 'C', { state: 'resolved', severity: 'info', sticky: false }));
  assert.deepEqual(ops(morph), ['morph:C', 'more:1']);
  assert.equal(s.find('C').tone, 'ok');
  // A is folded: its new frame brings it back as the newest (and folds the oldest on screen).
  assert.deepEqual(ops(s.apply(card('a2', 'A', { state: 'update' }))), ['add:A', 'fold:B', 'more:1']);
  assert.deepEqual(s.shown.map(c => c.incident), ['C', 'D', 'A']);
  // Resolved with no card for the incident: a short recovered card is added.
  const fresh = createStack();
  assert.deepEqual(ops(fresh.apply(card('r1', 'R', { state: 'resolved', sticky: false }))), ['add:R', 'more:0']);
});

test('stack: closed takes a card away silently (and a folded one), clear keeps the shell cards', () => {
  const s = createStack();
  s.apply(card('a', 'A')); s.apply(card('b', 'B')); s.apply(card('c', 'C')); s.apply(card('d', 'D'));
  assert.deepEqual(ops(s.apply(card('x', 'C', { state: 'closed' }))), ['remove:C', 'unfold:A', 'more:0']);
  assert.deepEqual(ops(s.close('nope')), ['more:0']);
  s.apply(card('e', 'E'));
  assert.deepEqual(s.folded.map(c => c.incident), ['A']);
  assert.deepEqual(ops(s.close('A')), ['more:0'], 'a folded card goes without a remove op');
  const t = createStack();
  t.apply(card('s', 'service_stopped', { shell: true })); t.apply(card('o', 'O')); t.apply(card('p', 'P')); t.apply(card('q', 'Q'));
  assert.deepEqual(t.folded.map(c => c.incident), ['service_stopped']);
  assert.deepEqual(ops(t.clear()), ['remove:O', 'remove:P', 'remove:Q', 'unfold:service_stopped', 'more:0'], 'the folded shell card comes back');
  assert.deepEqual(t.shown.map(c => c.incident), ['service_stopped']);
  const u = createStack();
  u.apply(card('o', 'O')); u.apply(card('p', 'P')); u.apply(card('q', 'Q')); u.apply(card('r', 'R'));
  assert.deepEqual(ops(u.clear()), ['remove:P', 'remove:Q', 'remove:R', 'more:0']);
  assert.deepEqual([u.shown, u.folded], [[], []], 'the folded sidecar card went too');
});

test('time line, spring curve', () => {
  assert.deepEqual(timeLabel(1000, 1000 + 59_999), { id: 'notice.time.now', params: null });
  assert.deepEqual(timeLabel(1000, 1000 + 60_000), { id: 'notice.time.minutes', params: { n: 1 } });
  assert.deepEqual(timeLabel(5000, 1000), { id: 'notice.time.now', params: null }, 'a clock step back reads now');
  const sp = springCurve(380, 38);
  assert.match(sp.easing, /^linear\(0, [\d., ]+, 1\)$/);
  assert.ok(sp.duration > 200 && sp.duration < 600, `${sp.duration} ms`);
  const values = sp.easing.slice(7, -1).split(', ').map(Number);
  assert.ok(values.every((v, i) => i === 0 || v >= values[i - 1] - 1e-9) && Math.max(...values) <= 1, 'no overshoot, no wobble');
});

// a minimal DOM for the SVG builders: elements record their namespace, attributes and children
function fakeDoc() {
  const make = (ns, tag) => ({ ns, tag, attrs: {}, children: [], setAttribute(k, v) { this.attrs[k] = String(v); }, appendChild(c) { this.children.push(c); return c; } });
  return { createElementNS: make };
}

// icons.js is a classic script on window.T: run it in a sandbox of its own (no global window
// here), without core.js, so T.icon returns its markup string as in the notice window
function iconsJs() {
  const sandbox = {}; sandbox.window = sandbox;
  vm.runInNewContext(readFileSync(new URL('../../src/core/icons.js', import.meta.url), 'utf8'), sandbox);
  return sandbox.T;
}

test('icons are rebuilt from icons.js with createElementNS: shapes and drawing attributes only', () => {
  const T = iconsJs();
  const doc = fakeDoc();
  for (const name of ['error', 'warning', 'info', 'success', 'close']) {
    const svg = iconNode(doc, String(T.icon(name, 16)));
    assert.equal(svg.ns, SVG_NS);
    assert.equal(svg.attrs.viewBox, '0 0 24 24', name);
    assert.ok(svg.attrs.class.startsWith('ic ic-'), name);
    assert.equal(svg.attrs['aria-hidden'], 'true');
    assert.ok(svg.children.length >= 2 && svg.children.every(c => ['path', 'circle', 'rect'].includes(c.tag)), name);
  }
  const error = iconNode(doc, String(T.icon('error', 16)));
  assert.deepEqual(error.children.map(c => c.tag), ['path', 'path', 'circle']);
  assert.equal(error.children[2].attrs.class, 'd');
  // Anything else in the markup is dropped: no script, no handler, no foreign element.
  const evil = iconNode(doc, '<svg viewBox="0 0 24 24" onload="x()"><script/><path d="M1 1" onclick="y()"/><foreignObject/><image href="x"/></svg>');
  assert.deepEqual(Object.keys(evil.attrs).sort(), ['aria-hidden', 'focusable', 'viewBox']);
  assert.deepEqual(evil.children.map(c => [c.tag, Object.keys(c.attrs)]), [['path', ['d']]]);
  assert.equal(iconNode(doc, 'not svg').children.length, 0);
  assert.deepEqual(markNode(doc).children.map(c => c.tag), ['path', 'circle']);
  assert.equal(ringNode(doc).children[0].attrs.r, '16');
});
