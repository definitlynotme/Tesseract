// T-P4-02a: topic cache: patch (rev == base_rev), paging, keyed rules, gap, refetch, and the
// merge parity with bridge_protocol.apply_patch.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { applyPatch, createPageBuffer } from '../../src/rt/paging.js';
import { RECONNECT_MIN_GAP_MS, RESYNC_FALLBACK_MS, RESYNC_INFLIGHT_MS } from '../../src/rt/client.js';
import { E1, evt, flush, job, live, loadScenario, boot, python, svc } from './rt_helpers.mjs';

const V1_TOPICS = ['service.state', 'tiers.summary', 'nodes.list', 'subscriptions.list', 'bastion.sources', 'config.items', 'network.mode', 'jobs', 'logs.batch'];
const node = (id, extra = {}) => ({ id, name: id, tier: 'vanguard', kind: 'manual', enabled: true, status: 'healthy', reason_code: null, protocol: 'vless', latency_ms: 80, ...extra });
const nodes = (rev, items, extra = {}) => evt('nodes.list', rev, 'snapshot', { items }, { key: 'vanguard', ...extra });
const npatch = (rev, base_rev, changed, removed = []) => evt('nodes.list', rev, 'patch', { changed, removed }, { key: 'vanguard', base_rev });
const v1 = { hello: { topics: V1_TOPICS } };

test('applyPatch matches bridge_protocol.apply_patch on the exported vectors', () => {
  const r = python(['ui/tools/validate_ui_fixtures.py', '--export-patch-vectors']);
  assert.equal(r.status, 0, r.stderr);
  const vectors = JSON.parse(r.stdout);
  assert.ok(vectors.length >= 12);
  assert.ok(vectors.some(v => v.name.startsWith('v1_')), 'vectors from bridge_schema/fixtures are included');
  for (const v of vectors) {
    const got = applyPatch(v.baseline, v.patch);
    assert.equal(got.needsSnapshot, v.needs_snapshot, v.name);
    assert.deepEqual(got.merged, v.merged, v.name);
  }
});

test('applyPatch never mutates its inputs', () => {
  const base = { rev: 1, items: [{ id: 'a', v: 1 }] };
  const patch = { base_rev: 1, rev: 2, changed: [{ id: 'a', v: 2 }], removed: [] };
  const b0 = JSON.stringify(base), p0 = JSON.stringify(patch);
  const r = applyPatch(base, patch);
  r.merged.items[0].v = 99;
  assert.equal(JSON.stringify(base), b0);
  assert.equal(JSON.stringify(patch), p0);
});

test('patch with rev == base_rev merges by id; a mismatch keeps the old value and goes stale, refetch, snapshot clears', async () => {
  const b = await live(v1, [nodes(1, [node('a'), node('b'), node('c')])]);
  const { rt, fake, T } = b;
  const t = rt.topic('nodes.list', 'vanguard');
  const seen = [];
  t.sub(v => seen.push(v.items.map(i => i.id + (i.status === 'failed' ? '!' : '')).join(',')));
  fake.deliver(npatch(2, 1, [node('b', { status: 'failed' }), node('d')], ['a']));
  assert.deepEqual(seen, ['b!,c,d']);
  assert.equal(T.store.get('stale')['nodes.list/vanguard'], false);
  // mismatch: base_rev 5 against cached rev 2
  const conn = fake.connects;
  fake.deliver(npatch(6, 5, [node('x')]));
  assert.equal(t.get().items.map(i => i.id).join(','), 'b,c,d', 'never last-wins');
  assert.equal(T.store.get('stale')['nodes.list/vanguard'], true);
  assert.ok(rt.diag().some(d => d.kind === 'topic_base_rev'));
  // no rt_resync in the shell: the fallback reconnects for a full replay
  await flush();
  assert.equal(fake.connects, conn + 1);
  // further patches stay paused until a snapshot
  fake.deliver(npatch(7, 6, [node('y')]));
  assert.equal(t.get().items.length, 3);
  // a replayed snapshot older than the newest rev seen at the mismatch (7) is not a
  // replacement: the entry stays stale over the last value
  fake.deliver(nodes(4, [node('old')]));
  assert.equal(t.get().items.map(i => i.id).join(','), 'b,c,d');
  assert.equal(T.store.get('stale')['nodes.list/vanguard'], true);
  assert.ok(rt.diag().some(d => d.kind === 'replacement_too_old' && d.rev === 4 && d.need === 7));
  // a replacement at least as new clears the stale mark
  fake.deliver(nodes(8, [node('z')]));
  assert.equal(t.get().items.map(i => i.id).join(','), 'z');
  assert.equal(T.store.get('stale')['nodes.list/vanguard'], false);
});

test('gap: an older replayed snapshot does not clear stale; the next newer value does', async () => {
  const { fake, T } = await live(v1, [evt('service.state', 3, 'snapshot', svc({ uptime: 3 }))]);
  fake.deliver(evt('service.state', 5, 'gap', { from_seq: 1, to_seq: 3, dropped: 3 }, { seq: 4 }));
  assert.equal(T.store.get('stale')['service.state'], true);
  fake.deliver(evt('service.state', 3, 'snapshot', svc({ uptime: 3 })));
  assert.equal(T.store.get('stale')['service.state'], true, 'the replayed rev 3 snapshot is known to be old');
  fake.deliver(evt('service.state', 6, 'snapshot', svc({ uptime: 6 })));
  assert.equal(T.store.get('stale')['service.state'], false);
  assert.equal(T.store.get('uptime'), 6);
});

test('refetch: rt_resync when the shell has it; the reconnect fallback is limited to once per topic per 30 s', async () => {
  const scenario = { steps: [1, 2, 3].map(() => ({ on_invoke: { host: 'rt_resync', args: { topic: 'nodes.list', key: 'vanguard' } }, result: null })) };
  const a = await live({ ...v1, scenario }, [nodes(1, [node('a')])]);
  a.fake.deliver({ t: 'host:caps', epoch: E1, invokes: ['rt_resync'], events: [] });
  const before = a.fake.connects;
  a.fake.deliver(npatch(3, 2, [node('b')]));
  await flush();
  assert.equal(a.fake.connects, before, 'no reconnect when rt_resync exists');
  assert.deepEqual(a.fake.invocations.filter(i => i.kind === 'host').map(i => [i.name, i.args]), [['rt_resync', { topic: 'nodes.list', key: 'vanguard' }]]);
  assert.deepEqual(a.fake.failures, []);
  // while it is in flight, more patches and gaps on the same topic ask nothing more
  a.fake.deliver(npatch(4, 3, [node('c')]));
  a.fake.deliver(npatch(5, 4, [node('d')]));
  await flush();
  assert.equal(a.fake.invocations.filter(i => i.kind === 'host').length, 1, 'one rt_resync per topic in flight');
  assert.ok(a.rt.diag().some(d => d.kind === 'resync_inflight'));
  // the replacement snapshot ends the in-flight mark: the next mismatch asks again
  a.fake.deliver(nodes(6, [node('e')]));
  a.fake.deliver(npatch(8, 7, [node('f')]));
  await flush();
  assert.equal(a.fake.invocations.filter(i => i.kind === 'host').length, 2);
  // without a replacement the mark expires after RESYNC_INFLIGHT_MS
  a.fake.deliver(npatch(9, 8, [node('g')]));
  await flush();
  assert.equal(a.fake.invocations.filter(i => i.kind === 'host').length, 2);
  a.clock.advance(RESYNC_INFLIGHT_MS + 1);
  a.fake.deliver(npatch(10, 9, [node('h')]));
  await flush();
  assert.equal(a.fake.invocations.filter(i => i.kind === 'host').length, 3);
  assert.deepEqual(a.fake.failures, []);

  const b = await live(v1, [nodes(1, [node('a')]), evt('bastion.sources', 1, 'snapshot', { items: [] })]);
  const c0 = b.fake.connects;
  b.fake.deliver(npatch(3, 2, [node('b')]));
  await flush();
  assert.equal(b.fake.connects, c0 + 1);
  // a second mismatch on the same topic within 30 s: throttled
  b.fake.deliver({ t: 'rt:reset', epoch: E1, reason: 'reconnect' });
  b.fake.deliver(npatch(9, 8, [node('c')]));
  await flush();
  assert.equal(b.fake.connects, c0 + 1);
  assert.ok(b.rt.diag().some(d => d.kind === 'resync_throttled'));
  // another topic is not throttled by the first, but reconnects are at least 2 s apart:
  // requests inside the window share one reconnect
  b.fake.deliver(evt('bastion.sources', 3, 'patch', { changed: [], removed: [] }, { base_rev: 2 }));
  await flush();
  assert.equal(b.fake.connects, c0 + 1, 'merged into a reconnect 2 s after the last one');
  b.clock.advance(RECONNECT_MIN_GAP_MS);
  await flush();
  assert.equal(b.fake.connects, c0 + 2);
  // after 30 s the first topic may reconnect again
  b.clock.advance(RESYNC_FALLBACK_MS + 1);
  b.fake.deliver({ t: 'rt:reset', epoch: E1, reason: 'reconnect' });
  b.fake.deliver(npatch(12, 11, [node('d')]));
  await flush();
  assert.equal(b.fake.connects, c0 + 3);
});

test('paging: pages in order install at once; out of order or a missing page never installs', async () => {
  const page = (rev, index, complete, items, id = 's1') => nodes(rev, items, { page: { snapshot_id: id, index, complete, next_cursor: complete ? null : `c${index + 1}` } });
  const { rt, fake, T } = await live(v1, [nodes(1, [node('old')])]);
  const t = rt.topic('nodes.list', 'vanguard');
  fake.deliver(page(5, 0, false, [node('a')]));
  assert.equal(t.get().items[0].id, 'old', 'nothing installed until the group completes');
  assert.equal(T.store.get('stale')['nodes.list/vanguard'], true, 'pages not complete');
  // a patch arriving meanwhile is held, then applied on top of the installed group
  fake.deliver(npatch(6, 5, [node('c')]));
  fake.deliver(page(5, 1, true, [node('b')]));
  assert.deepEqual(t.get().items.map(i => i.id), ['a', 'b', 'c']);
  assert.equal(T.store.get('stale')['nodes.list/vanguard'], false);

  // out of order: index 1 first
  const b = await live(v1, [nodes(1, [node('old')])]);
  const tb = b.rt.topic('nodes.list', 'vanguard');
  b.fake.deliver(page(5, 1, true, [node('b')]));
  assert.equal(tb.get().items[0].id, 'old');
  assert.ok(b.rt.diag().some(d => d.kind === 'page_broken' && d.reason === 'page_missing_start'));
  // a missing page: 0 then 2
  const c = await live(v1, [nodes(1, [node('old')])]);
  const tc = c.rt.topic('nodes.list', 'vanguard');
  c.fake.deliver(page(5, 0, false, [node('a')]));
  c.fake.deliver(page(5, 2, true, [node('c')]));
  assert.equal(tc.get().items[0].id, 'old');
  assert.equal(c.T.store.get('stale')['nodes.list/vanguard'], true);
  // a page of another group: discarded
  const pb = createPageBuffer();
  assert.equal(pb.add(page(5, 0, false, [node('a')]).valueOf()).status, 'buffering');
  assert.equal(pb.add(page(5, 1, true, [node('b')], 's2')).reason, 'page_mixed_group');
});

test('the p2 scenario: a two-page nodes.list group installs as one list', async () => {
  const { rt, fake } = boot({ scenario: loadScenario('rt_p2_projection') });
  await flush();
  assert.deepEqual(fake.failures, []);
  const list = rt.topic('nodes.list', 'vanguard').get();
  assert.ok(list && list.items.length >= 2);
});

test('keyed rules: keyed topics need a tier key, other topics must not carry one', async () => {
  const { rt, fake } = await live(v1, []);
  fake.deliver(evt('nodes.list', 1, 'snapshot', { items: [] }));
  fake.deliver(evt('nodes.list', 1, 'snapshot', { items: [] }, { key: 'moon' }));
  fake.deliver(evt('service.state', 1, 'snapshot', svc(), { key: 'vanguard' }));
  assert.equal(rt.topic('nodes.list').get(), undefined);
  assert.equal(rt.topic('service.state').get(), undefined);
  assert.equal(rt.diag().filter(d => d.kind === 'bad_key').length, 3);
  fake.deliver(evt('nodes.list', 1, 'snapshot', { items: [node('a')] }, { key: 'vanguard' }));
  fake.deliver(evt('subscriptions.list', 1, 'snapshot', { items: [] }, { key: 'sentinel' }));
  assert.equal(rt.topic('nodes.list', 'vanguard').get().items.length, 1);
  assert.deepEqual(rt.topic('subscriptions.list', 'sentinel').get(), { items: [] });
  assert.equal(rt.topic('nodes.list', 'sentinel').get(), undefined, 'keys are independent');
  // patch is refused on a non-patchable topic, so is an unknown kind
  fake.deliver(evt('service.state', 2, 'patch', { changed: [], removed: [] }, { base_rev: 1 }));
  assert.ok(rt.diag().some(d => d.kind === 'bad_kind'));
});

test('shell-only topics never reach the page cache', async () => {
  const { rt, fake } = await live({ hello: { topics: [...V1_TOPICS, 'ui.language'] } }, []);
  fake.deliver(evt('ui.language', 1, 'snapshot', { language: 'en_US', config_rev: 1 }));
  fake.deliver(evt('alerts.desktop', 1, 'edge', { delivery_id: 'x' }, { seq: 1 }));
  assert.equal(rt.topic('ui.language').get(), undefined);
  assert.equal(rt.diag().filter(d => d.kind === 'shell_topic').length, 2);
});

test('gap: a state topic goes stale and is refetched; the next full value clears it', async () => {
  const { rt, fake, T } = await live(v1, [evt('service.state', 1, 'snapshot', svc())]);
  const c0 = fake.connects;
  fake.deliver(evt('service.state', 2, 'gap', { from_seq: 1, to_seq: 3, dropped: 3 }, { seq: 4 }));
  assert.equal(T.store.get('stale')['service.state'], true);
  await flush();
  assert.equal(fake.connects, c0 + 1);
  fake.deliver(evt('service.state', 3, 'edge', svc({ uptime: 9 }), { seq: 5 }));
  assert.equal(T.store.get('stale')['service.state'], false);
  assert.equal(T.store.get('uptime'), 9);
  assert.ok(!rt.diag().some(d => d.kind === 'edge_seq_jump'), 'the edge after the gap continues its seq');
});

test('jobs: a break in seq is only recorded (the shell rebuilds the session); duplicates are dropped', async () => {
  const { rt, fake, T } = await live(v1, []);
  fake.deliver(evt('jobs', 1, 'edge', job('a', 'service.restart', 'running'), { seq: 1 }));
  fake.deliver(evt('jobs', 3, 'edge', job('a', 'service.restart', 'done', { result: { applied: true, core_running: true, core_cleanup_pending: false, core_generation: 1 } }), { seq: 3 }));
  fake.deliver(evt('jobs', 3, 'edge', job('a', 'service.restart', 'done'), { seq: 3 }));
  assert.ok(rt.diag().some(d => d.kind === 'jobs_sequence_gap'));
  assert.equal(T.named('rt:job').length, 2);
  assert.equal(T.store.get('stale').jobs, undefined, 'jobs are never stale');
});

test('logs.batch: gaps go to the log buffer as rt:logs{kind:gap}; never stale', async () => {
  const { fake, T } = await live(v1, []);
  fake.deliver(evt('logs.batch', 1, 'edge', { from_seq: 1, records: [], dropped: 0, dropped_total: 0, gap: false }, { seq: 1 }));
  fake.deliver(evt('logs.batch', 2, 'gap', { from_seq: 2, to_seq: 5, dropped: 4 }, { seq: 6 }));
  assert.deepEqual(T.named('rt:logs').map(l => l.kind), ['edge', 'gap']);
  assert.equal(T.store.get('stale')['logs.batch'], undefined);
});
