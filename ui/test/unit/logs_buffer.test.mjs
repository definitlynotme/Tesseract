// T-P4-05c: the logs page's buffer (ui/src/pages/logs/buffer.js) and export flow
// (ui/src/pages/logs/export.js). No DOM, no browser.
//
// Buffer: seq append and de-duplication, a seq jump, a batch with dropped > 0 / gap:true, a
// kind:'gap' frame, the shell's rt:log-gap, the dropped counts, the 4,000-row cap (trimmed
// oldest first, counted), the seed (logs.tail pages spliced on the parked live batches by seq,
// snapshot_required refetching the whole group, three in a row failing), a generation change
// (cleared, one restart row) against a same-epoch reconnect (nothing cleared, no separator, no
// duplicate: through the real client on a FakeTransport), is_key_event and its fallback by key
// (criticism B10), runs by run_id and by runtime.start_requested, ALERT as Tesseract + alert, the
// live gate (nothing rendered while the window is hidden, one render when it shows; criticism D8).
// Export: host.saveFile → job → outcome for every branch of 映射_logs §4 (cancelled, dialog_open,
// dialog failed, done with missing_seqs, failed, interrupted), filtered exports carry seqs only
// (never a filter, R9), host.reveal gone / failed.
//
// The two modules are loaded from data: URLs: ui/src/pages/logs/ has no package.json saying
// "type": "module" (the bundle does not need one), and this keeps node from warning about it.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { E1, E2, evt, flush, hello, live, makeT } from './rt_helpers.mjs';

const load = rel => import('data:text/javascript;base64,' + Buffer.from(readFileSync(new URL(rel, import.meta.url))).toString('base64'));
const LB = await load('../../src/pages/logs/buffer.js');
const LX = await load('../../src/pages/logs/export.js');
const { createLogBuffer, createLiveGate, attachBuffer, fetchSeed, isKeyEvent, normalize, parseBoundary, CAP, SLACK } = LB;

const T0 = 1790650000000;
const rec = (seq, o = {}) => ({ seq, ts_ms: T0 + seq * 1000, rx_ms: T0 + seq * 1000 + 7, source: 'TESSERACT', level: 'info', category: 'runtime', key: 'log.raw', title: '', detail: '', raw: `line ${seq}`, ...o });
const range = (a, b, o) => Array.from({ length: b - a + 1 }, (_, i) => rec(a + i, o));
const edge = (recs, o = {}) => ({ kind: 'edge', data: { from_seq: recs.length ? recs[0].seq : 1, records: recs, dropped: 0, dropped_total: 0, gap: false, ...o } });
const logs = buf => buf.rows.filter(r => r.kind === 'log').map(r => r.seq);
const kinds = buf => buf.rows.map(r => (r.kind === 'log' ? r.seq : r.kind === 'gap' ? `gap:${r.n}` : r.kind));

/* ------------------------------------------------------------------ live batches */
test('batches append by seq; a replayed or duplicate record is dropped', () => {
  const b = createLogBuffer();
  assert.deepEqual(b.ingest(edge(range(1, 3))).added.map(r => r.seq), [1, 2, 3]);
  // a batch that repeats 2..3 and continues: only 4 is new
  assert.deepEqual(b.ingest(edge(range(2, 4), { from_seq: 2 })).added.map(r => r.seq), [4]);
  assert.deepEqual(b.ingest(edge(range(1, 4))).added, []);
  assert.deepEqual(kinds(b), [1, 2, 3, 4]);
  assert.equal(b.lastSeq, 4);
  // ords only grow: the page's watermarks compare them
  const ords = b.rows.map(r => r.ord);
  assert.deepEqual(ords, [...ords].sort((x, y) => x - y));
});

test('a from_seq jump inserts one gap row with the missing count (L-5); a jump inside a batch too', () => {
  const b = createLogBuffer();
  b.ingest(edge(range(1, 4)));
  const r = b.ingest(edge(range(8, 9)));
  assert.equal(r.gaps, 1);
  assert.deepEqual(kinds(b), [1, 2, 3, 4, 'gap:3', 8, 9]);
  const g = b.rows.find(x => x.kind === 'gap');
  assert.deepEqual([g.from, g.to, g.n], [5, 7, 3]);
  // records must be consecutive inside a batch; a hole there is shown as well
  b.ingest(edge([rec(10), rec(13)], { from_seq: 10 }));
  assert.deepEqual(kinds(b).slice(-4), [9, 10, 'gap:2', 13]);
  assert.equal(b.counts.lost, 5);
});

test('dropped > 0 and gap:true insert a gap row; dropped_total is kept (never smaller)', () => {
  const b = createLogBuffer();
  b.ingest(edge(range(1, 2)));
  b.ingest(edge(range(3, 4), { dropped: 6, dropped_total: 6, gap: true }));
  assert.deepEqual(kinds(b), [1, 2, 'gap:6', 3, 4]);
  assert.equal(b.counts.droppedTotal, 6);
  b.ingest(edge(range(5, 5), { dropped_total: 4 }));         // an older total does not lower it
  assert.equal(b.counts.droppedTotal, 6);
  // gap:true without a count: a gap row of unknown size
  b.ingest(edge(range(6, 6), { gap: true }));
  assert.deepEqual(kinds(b).slice(-2), ['gap:null', 6]);
  assert.equal(b.counts.lost, 6);
});

test("a kind:'gap' frame shows the hole at once; the next batch's jump fills in its count (one row)", () => {
  const b = createLogBuffer();
  b.ingest(edge(range(1, 3)));
  const r = b.ingest({ kind: 'gap', data: { from_seq: 4, to_seq: 6, dropped: 3 } });
  assert.equal(r.gaps, 1);
  assert.deepEqual(kinds(b), [1, 2, 3, 'gap:null']);
  b.ingest(edge(range(11, 12)));
  assert.deepEqual(kinds(b), [1, 2, 3, 'gap:7', 11, 12], 'the same row, now with the 7 missing records');
  assert.equal(b.rows.filter(x => x.kind === 'gap').length, 1);
});

test("a kind:'gap' frame and a batch with no hole after it: dropped_total's growth is the count, no growth removes the row (review S1)", () => {
  // the lost frames held producer drops: dropped_total grew by 2 since the frame
  let b = createLogBuffer();
  b.ingest(edge(range(1, 3)));
  b.ingest({ kind: 'gap', data: { from_seq: 4, to_seq: 4, dropped: 1 } });
  const v = b.version;
  const r = b.ingest(edge(range(4, 5), { dropped_total: 2 }));
  assert.deepEqual(kinds(b), [1, 2, 3, 'gap:2', 4, 5]);
  assert.equal(b.counts.lost, 2);
  assert.ok(r.gaps > 0 && b.version > v, 'the page rebuilds (the row above the tail changed)');
  // nothing lost at all: the row was a false alarm and goes
  b = createLogBuffer();
  b.ingest(edge(range(1, 3)));
  b.ingest({ kind: 'gap', data: { from_seq: 4, to_seq: 5, dropped: 2 } });
  b.ingest({ kind: 'gap', data: { from_seq: 6, to_seq: 6, dropped: 1 } });
  assert.deepEqual(kinds(b), [1, 2, 3, 'gap:null'], 'a second frame while the row is open adds no row');
  const v2 = b.version;
  b.ingest(edge(range(4, 5)));
  assert.deepEqual(kinds(b), [1, 2, 3, 4, 5]);
  assert.equal(b.counts.lost, 0);
  assert.ok(b.version > v2);
  // the next frame after a settled one opens a new row again
  b.ingest({ kind: 'gap', data: { from_seq: 7, to_seq: 7, dropped: 1 } });
  b.ingest(edge(range(9, 9)));
  assert.deepEqual(kinds(b), [1, 2, 3, 4, 5, 'gap:3', 9]);
  // a gap frame parked during the seed, covered by the seed's watermark: no row
  b = createLogBuffer();
  b.beginSeed();
  b.ingest({ kind: 'gap', data: { from_seq: 2, to_seq: 3, dropped: 2 } });
  b.ingest(edge(range(11, 12)));
  b.applySeed([{ records: range(1, 10), watermark: 10, next_cursor: null, gap: false, dropped: 0 }]);
  assert.deepEqual(kinds(b), [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]);
  assert.equal(b.counts.lost, 0);
});

test("the shell's rt:log-gap: a gap over the record range; nothing for a range already here; before any record it is the history's start", () => {
  const b = createLogBuffer();
  // nothing here yet: the lost records are the history before the first one (the origin row)
  assert.equal(b.logGap({ from_seq: 1, to_seq: 9, dropped: 9, record_from_seq: 1, record_to_seq: 300, record_dropped: 300 }), null);
  assert.equal(b.counts.head, 300);
  assert.equal(b.counts.headGap, true);
  assert.deepEqual(b.ingest(edge(range(299, 302), { from_seq: 299 })).added.map(r => r.seq), [301, 302], 'records up to 300 are accounted for');
  // a later loss in the middle of the stream
  const g = b.logGap({ from_seq: 20, to_seq: 22, dropped: 3, record_from_seq: 303, record_to_seq: 340, record_dropped: 38 });
  assert.equal(g.kind, 'gap');
  assert.deepEqual([g.from, g.to, g.n], [303, 340, 38]);
  assert.equal(b.lastSeq, 340);
  // replayed (the client forwards each range once; the buffer ignores one it already covers)
  assert.equal(b.logGap({ from_seq: 20, to_seq: 22, dropped: 3, record_from_seq: 303, record_to_seq: 340, record_dropped: 38 }), null);
  b.ingest(edge(range(341, 341)));
  assert.deepEqual(kinds(b), [301, 302, 'gap:38', 341]);
});

test('the 4,000-row cap: trimmed in chunks of SLACK past it, oldest first, the trimmed records counted', () => {
  assert.equal(CAP, 4000);
  const b = createLogBuffer();
  b.ingest(edge(range(1, CAP)));
  assert.deepEqual(b.trim(), [], 'at the cap: nothing yet');
  b.ingest(edge(range(CAP + 1, CAP + SLACK)));
  assert.deepEqual(b.trim(), [], 'the slack absorbs a burst');
  b.ingest(edge(range(CAP + SLACK + 1, CAP + SLACK + 1)));
  const gone = b.trim();
  assert.equal(gone.length, SLACK + 1);
  assert.deepEqual([gone[0].seq, gone[gone.length - 1].seq], [1, SLACK + 1]);
  assert.equal(b.rows.length, CAP);
  assert.equal(b.rows[0].seq, SLACK + 2);
  assert.equal(b.counts.trimmed, SLACK + 1);
  // 4,000 rows and a burst of 1,000 (one batch at most, PROTOCOL §5.5): still 4,000 after a trim
  b.ingest(edge(range(CAP + SLACK + 2, CAP + SLACK + 1001)));
  b.trim();
  assert.equal(b.rows.length, CAP);
  assert.equal(b.rows[CAP - 1].seq, CAP + SLACK + 1001);
});

/* ------------------------------------------------------------------ the seed */
test('the seed: logs.tail pages spliced with the batches parked meanwhile, by seq; a jump past the watermark is a gap', () => {
  const b = createLogBuffer();
  b.ingest(edge(range(1, 5)));                          // seen before the first visit
  b.beginSeed();
  assert.equal(b.seeding, true);
  // live batches during the fetch wait: one overlaps the history, one continues it, one jumps
  assert.equal(b.ingest(edge(range(9, 12), { from_seq: 9 })).parked, true);
  b.ingest(edge(range(13, 14)));
  b.ingest(edge(range(17, 17)));
  assert.deepEqual(logs(b), [1, 2, 3, 4, 5], 'parked batches are not in the rows yet');
  // two pages, oldest first, the second repeating the first's last record
  b.applySeed([
    { records: range(3, 7), watermark: 10, next_cursor: 'c1', gap: true, dropped: 2 },
    { records: range(7, 10), watermark: 10, next_cursor: null, gap: true, dropped: 2 },
  ]);
  assert.equal(b.seeding, false);
  assert.deepEqual(kinds(b), [3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 'gap:2', 17]);
  assert.equal(b.counts.head, 2, 'LogPage.dropped: evicted before the seed (L-4)');
  assert.equal(b.counts.headGap, true);
});

test('the seed failed: the parked batches go on top of the rows already here', () => {
  const b = createLogBuffer();
  b.ingest(edge(range(1, 2)));
  b.beginSeed();
  b.ingest(edge(range(3, 4)));
  assert.deepEqual(b.abortSeed().map(r => r.seq), [3, 4]);
  assert.deepEqual(logs(b), [1, 2, 3, 4]);
  assert.deepEqual(b.abortSeed(), [], 'not seeding: nothing');
});

test('a seed that rebuilds rows the page had marked hands back mapOrd: marks follow their records by seq; the parked records come back as added (review S2, S3)', () => {
  const b = createLogBuffer();
  // the first seed failed: the live batches parked meanwhile went on the rows
  b.ingest(edge(range(1, 2)));
  b.beginSeed();
  b.ingest(edge(range(3, 5)));
  assert.deepEqual(b.abortSeed().map(r => r.seq), [3, 4, 5], 'abortSeed hands back what it replayed');
  const ordOf = s => b.rows.find(r => r.kind === 'log' && r.seq === s).ord;
  const wm = ordOf(3), pause = ordOf(5), top = b.ord;
  // retry: the history comes back whole, two live batches parked during the fetch
  b.beginSeed();
  b.ingest(edge(range(6, 7)));
  const sd = b.applySeed([{ records: range(1, 5), watermark: 5, next_cursor: null, gap: false, dropped: 0 }]);
  assert.deepEqual(logs(b), [1, 2, 3, 4, 5, 6, 7]);
  assert.ok(ordOf(1) > top, 'every row has a new ord');
  assert.equal(sd.rows, b.rows);
  assert.deepEqual(sd.added.map(r => r.seq), [6, 7], 'the parked records, for the badge');
  assert.equal(sd.mapOrd(0), 0, 'no mark stays no mark');
  assert.equal(sd.mapOrd(wm), ordOf(3), 'a clear up to seq 3 still ends at seq 3');
  assert.equal(sd.mapOrd(pause), ordOf(5), 'a pause after seq 5 holds 6 and 7, not the history');
  assert.equal(sd.mapOrd(top), ordOf(5), 'a mark at the old top: the newest old record');
  const shown = o => b.rows.filter(r => r.kind === 'log' && r.ord > o).map(r => r.seq);
  assert.deepEqual(shown(sd.mapOrd(wm)), [4, 5, 6, 7], 'the cleared rows stay cleared');
  // the history before the mark was evicted meanwhile: the seed starts after it, all of it is newer
  const c = createLogBuffer();
  c.ingest(edge(range(1, 3)));
  const m = c.rows[2].ord;
  c.beginSeed();
  const sd2 = c.applySeed([{ records: range(8, 9), watermark: 9, next_cursor: null, gap: true, dropped: 7 }]);
  assert.equal(sd2.mapOrd(m), 0);
  // in a new generation the restart row is kept with its ord: a mark on it stays on it
  const d = createLogBuffer();
  d.ingest(edge(range(1, 2)));
  const z = d.reset().ord;
  d.beginSeed();
  const sd3 = d.applySeed([{ records: range(1, 2), watermark: 2, next_cursor: null, gap: false, dropped: 0 }]);
  assert.equal(sd3.mapOrd(z), z);
  assert.deepEqual(kinds(d), ['restart', 1, 2]);
});

test('fetchSeed follows next_cursor; snapshot_required refetches the whole group; three in a row reject; other errors reject at once', async () => {
  const snap = () => Object.assign(new Error('snapshot_required'), { code: 'snapshot_required' });
  const calls = [];
  const script = answers => args => { calls.push(args); const a = answers.shift(); return a instanceof Error ? Promise.reject(a) : Promise.resolve(a); };
  // pages: c1 → c2 → end
  let pages = await fetchSeed(script([{ records: [rec(1)], next_cursor: 'c1' }, { records: [rec(2)], next_cursor: 'c2' }, { records: [rec(3)], next_cursor: null }]));
  assert.equal(pages.length, 3);
  assert.deepEqual(calls, [{ n: 4000 }, { n: 4000, cursor: 'c1' }, { n: 4000, cursor: 'c2' }]);
  // the cursor expired on page 2 (L-3): the group starts over, silently
  calls.length = 0;
  pages = await fetchSeed(script([{ records: [rec(1)], next_cursor: 'c1' }, snap(), { records: [rec(1)], next_cursor: 'c3' }, { records: [rec(2)], next_cursor: null }]));
  assert.deepEqual(pages.map(p => p.records[0].seq), [1, 2], 'only the second group');
  assert.deepEqual(calls, [{ n: 4000 }, { n: 4000, cursor: 'c1' }, { n: 4000 }, { n: 4000, cursor: 'c3' }]);
  // three snapshot_required in a row: 无法载入日志 (L-2)
  calls.length = 0;
  await assert.rejects(fetchSeed(script([snap(), snap(), snap(), { records: [] }])), e => e.code === 'snapshot_required');
  assert.equal(calls.length, 3);
  // any other error: at once
  calls.length = 0;
  await assert.rejects(fetchSeed(script([Object.assign(new Error('x'), { code: 'runtime_unavailable' })])), e => e.code === 'runtime_unavailable');
  assert.equal(calls.length, 1);
  // a newer seed took over: null, and no more pages
  calls.length = 0;
  let alive = true;
  const q = args => { calls.push(args); alive = false; return Promise.resolve({ records: [], next_cursor: 'c9' }); };
  assert.equal(await fetchSeed(q, { alive: () => alive }), null);
  assert.equal(calls.length, 1);
});

test('relabel: titles and details replaced by seq in the current generation (O-9)', () => {
  const b = createLogBuffer();
  b.ingest(edge([rec(1, { key: 'runtime.start_requested', title: '正在启动服务', detail: 'a', is_key_event: true }), rec(2)]));
  const v = b.version;
  assert.equal(b.relabel([rec(1, { title: 'Starting service', detail: 'b' }), rec(9, { title: 'x' })]), 1);
  assert.equal(b.rows[0].title, 'Starting service');
  assert.equal(b.rows[0].dto.title, 'Starting service');
  assert.ok(b.version > v);
  assert.equal(b.relabel([rec(1, { title: 'Starting service', detail: 'b' })]), 0, 'unchanged: nothing');
});

/* ------------------------------------------------------------------ generations */
test('a generation change clears the rows, runs and counts and starts with one restart row; ords keep growing', () => {
  const b = createLogBuffer();
  b.ingest(edge(range(1, 3, { run_id: 'run-4' })));
  b.ingest(edge(range(7, 7), { dropped_total: 2 }));
  const ord = b.ord, gen = b.gen;
  const row = b.reset();
  assert.equal(row.kind, 'restart');
  assert.deepEqual(kinds(b), ['restart']);
  assert.equal(b.gen, gen + 1);
  assert.equal(b.lastSeq, null);
  assert.deepEqual([b.counts.head, b.counts.trimmed, b.counts.lost, b.counts.droppedTotal], [0, 0, 0, 0]);
  assert.equal(b.runs.size, 0);
  assert.ok(row.ord > ord);
  // the new epoch counts from 1 again: nothing is taken for a duplicate
  assert.deepEqual(b.ingest(edge(range(1, 2))).added.map(r => r.seq), [1, 2]);
  assert.ok(b.rows[1].ord > ord && b.rows[1].gen === b.gen);
  // a seed in the new generation keeps its restart row first
  b.beginSeed();
  b.applySeed([{ records: range(1, 3), watermark: 3, next_cursor: null, gap: false, dropped: 0 }]);
  assert.deepEqual(kinds(b), ['restart', 1, 2, 3]);
});

/* Through the real client (FakeTransport): only rt:reset{generation_changed:true} reaches
   reset(); a same-epoch reconnect replays the cached batches, which add nothing. */
async function wired() {
  const b = await live({ hello: { topics: ['service.state', 'logs.batch', 'logs.feed', 'jobs'] } });
  const buf = createLogBuffer();
  const seen = { added: [], changed: 0, generations: 0 };
  attachBuffer(b.T, buf, { added: rows => seen.added.push(...rows.map(r => r.seq)), changed: () => { seen.changed++; }, generation: () => { seen.generations++; } });
  return { ...b, buf, seen };
}
const batchFrame = (rev, recs, epoch = E1, o = {}) => ({ ...evt('logs.batch', rev, 'edge', { from_seq: recs[0].seq, records: recs, dropped: 0, dropped_total: 0, gap: false, ...o }, { seq: rev }), epoch });

test('same-epoch reconnect: the replayed batches add nothing, nothing is cleared, no separator (criticism B9)', async () => {
  const { fake, buf, seen } = await wired();
  fake.deliver(batchFrame(1, range(1, 3)));
  fake.deliver(batchFrame(2, range(4, 5)));
  assert.deepEqual(kinds(buf), [1, 2, 3, 4, 5]);
  // the shell's reconnect: rt:reset{reconnect} for the same epoch, the hello and the cached tail again
  fake.deliver({ t: 'rt:reset', epoch: E1, reason: 'reconnect' });
  fake.deliver(hello({ epoch: E1, topics: ['service.state', 'logs.batch', 'logs.feed', 'jobs'] }));
  fake.deliver(batchFrame(1, range(1, 3)));
  fake.deliver(batchFrame(2, range(4, 5)));
  fake.deliver(batchFrame(3, range(6, 6)));
  await flush();
  assert.deepEqual(kinds(buf), [1, 2, 3, 4, 5, 6], 'no duplicate, no restart row, nothing cleared');
  assert.equal(seen.generations, 0);
  assert.deepEqual(seen.added, [1, 2, 3, 4, 5, 6]);
  // even a batch that slips past the client's rev check (a new rev, old records) adds nothing
  fake.deliver(batchFrame(4, range(5, 6)));
  assert.deepEqual(kinds(buf), [1, 2, 3, 4, 5, 6]);
});

test('generation change through the client: cleared once, one restart row, the new epoch from seq 1', async () => {
  const { fake, buf, seen } = await wired();
  fake.deliver(batchFrame(1, range(1, 3)));
  fake.deliver({ t: 'rt:reset', epoch: E1, reason: 'runtime_gone' });
  fake.deliver({ t: 'rt:reset', epoch: E2, reason: 'runtime_gone' });   // the shell's second notice: ends nothing new
  fake.deliver(hello({ epoch: E2, topics: ['service.state', 'logs.batch', 'logs.feed', 'jobs'] }));
  fake.deliver(batchFrame(1, range(1, 2), E2));
  await flush();
  assert.equal(seen.generations, 1);
  assert.deepEqual(kinds(buf), ['restart', 1, 2]);
  assert.equal(buf.rows[1].gen, buf.gen);
});

test("the shell's rt:log-gap through the client: forwarded once per range, a gap row", async () => {
  const { fake, buf } = await wired();
  fake.deliver(batchFrame(1, range(1, 2)));
  const gapFrame = { t: 'rt:log-gap', epoch: E1, data: { from_seq: 2, to_seq: 4, dropped: 3, record_from_seq: 3, record_to_seq: 9, record_dropped: 7 } };
  fake.deliver(gapFrame);
  fake.deliver(gapFrame);
  fake.deliver(batchFrame(5, range(10, 10)));
  assert.deepEqual(kinds(buf), [1, 2, 'gap:7', 10]);
});

/* ------------------------------------------------------------------ the record model */
test('is_key_event wins when present; absent, the key decides (criticism B10); log.raw and "" are raw lines', () => {
  assert.equal(isKeyEvent(rec(1, { key: 'runtime.start_requested', is_key_event: true })), true);
  assert.equal(isKeyEvent(rec(1, { key: 'mihomo.connection_timeout', is_key_event: false })), false, 'a classified line that is not a key event');
  assert.equal(isKeyEvent(rec(1, { key: 'log.raw', title: 'line', is_key_event: false })), false);
  // a frozen sidecar: no field
  assert.equal(isKeyEvent(rec(1, { key: 'runtime.start_requested' })), true);
  assert.equal(isKeyEvent(rec(1, { key: 'log.raw', title: 'a raw line has a title too' })), false, 'never "has a title"');
  assert.equal(isKeyEvent(rec(1, { key: '' })), false);
  assert.equal(isKeyEvent(rec(1, { key: 'source_boundary' })), false);
  const n = normalize(rec(5, { key: '' }), 1, 0);
  assert.equal(n.key, 'log.raw', '"" reads as log.raw');
  assert.equal(n.ev, false);
});

test('ALERT reads as Tesseract with category alert (G45); unknown enums fall back; source_boundary is parsed', () => {
  const a = normalize(rec(1, { source: 'ALERT', category: 'runtime', key: 'alert.delivered', is_key_event: true }), 1, 0);
  assert.deepEqual([a.src, a.wire, a.cat], ['TESSERACT', 'ALERT', 'alert']);
  const u = normalize(rec(2, { source: 'NEW', category: 'verification', level: 'ok' }), 2, 0);
  assert.deepEqual([u.src, u.cat, u.level], ['UNKNOWN', 'other', 'info']);
  const detail = JSON.stringify({ from: 'mihomo_api', to: 'stdout', at_ms: T0, reason: 'stream_error', continuity: 'unknown' });
  const bnd = normalize(rec(3, { source: 'MIHOMO', key: 'source_boundary', title: 'Core log source boundary', detail, raw: detail }), 3, 0);
  assert.deepEqual(bnd.boundary, { from: 'mihomo_api', to: 'stdout', at_ms: T0, reason: 'stream_error', continuity: 'unknown' });
  assert.equal(bnd.ev, false);
  // not JSON: a plain raw line
  assert.equal(parseBoundary(rec(4, { key: 'source_boundary', detail: 'garbage', raw: '{nope' })), null);
  assert.equal(normalize(rec(4, { key: 'source_boundary', detail: 'garbage', raw: 'x' }), 4, 0).boundary, null);
});

test('runs: run_id groups records; without it runtime.start_requested starts a run; stop / failure end it', () => {
  const b = createLogBuffer();
  b.ingest(edge([rec(1), rec(2, { key: 'runtime.start_requested', is_key_event: true, run_id: 'run-7' }), rec(3, { run_id: 'run-7' }),
    rec(4, { key: 'runtime.desktop_stopped', is_key_event: true, run_id: 'run-7' })]));
  assert.equal(b.rows[0].run, null, 'before the first run: no run');
  const r7 = b.runById('run-7');
  assert.equal(r7.end, T0 + 4000);
  // a frozen sidecar: no run_id, the start record splits
  b.ingest(edge([rec(5, { key: 'runtime.start_requested' }), rec(6), rec(7, { key: 'runtime.desktop_failed' }), rec(8, { key: 'runtime.start_requested' }), rec(9)]));
  const [r5, r8] = [b.rows[4].run, b.rows[7].run];
  assert.notEqual(r5, r8);
  assert.equal(b.rows[5].run, r5);
  assert.equal(b.run(r5).failed, true);
  assert.equal(b.lastRun().key, r8);
  assert.equal(b.rows[8].run, r8);
});

/* ------------------------------------------------------------------ hidden window */
test('while the window is hidden nothing is scheduled or rendered; the first visible moment renders once (criticism D8)', () => {
  let hidden = true, renders = 0;
  const queue = [];
  const gate = createLiveGate({ isHidden: () => hidden, schedule: fn => queue.push(fn), render: () => { renders++; } });
  const b = createLogBuffer();
  const T = makeT();
  attachBuffer(T, b, { changed: () => gate.note() });
  const emit = (e, p) => T.emit(e, p);
  // 30 s at 50 lines a second, in 200 ms batches of 10, while hidden
  for (let i = 0; i < 150; i++) emit('rt:logs', edge(range(i * 10 + 1, i * 10 + 10)));
  assert.equal(b.rows.length, 1500, 'the buffer keeps everything');
  assert.equal(queue.length, 0, 'nothing scheduled');
  assert.equal(renders, 0, 'nothing rendered');
  hidden = false;
  gate.shown();
  assert.equal(queue.length, 1);
  queue.shift()();
  assert.equal(renders, 1, 'one render for the whole period');
  gate.shown();
  assert.equal(queue.length, 0, 'nothing more to show');
  // visible: batches coalesce into one scheduled render
  emit('rt:logs', edge(range(1501, 1510)));
  emit('rt:logs', edge(range(1511, 1520)));
  assert.equal(queue.length, 1);
  queue.shift()();
  assert.equal(renders, 2);
  // hidden between scheduling and the frame: the render waits for the window
  emit('rt:logs', edge(range(1521, 1530)));
  hidden = true;
  queue.shift()();
  assert.equal(renders, 2);
  assert.equal(gate.due, true);
  hidden = false; gate.shown(); queue.shift()();
  assert.equal(renders, 3);
});

/* ------------------------------------------------------------------ export */
function fakeRt(answers) {
  const calls = [];
  return {
    calls,
    call(cmd, args) {
      calls.push([cmd, args]);
      const a = answers[cmd];
      const v = typeof a === 'function' ? a(args) : a;
      return v instanceof Error ? Promise.reject(v) : Promise.resolve(v);
    },
  };
}
const rtErr = (code, o = {}) => Object.assign(new Error(code), { code, ...o });

test('export: Save As cancelled starts nothing; dialog_open says another dialog is open (B8); any other refusal is a dialog failure', async () => {
  let rt = fakeRt({ 'host.saveFile': { cancelled: true, ref: null } });
  assert.deepEqual(await LX.exportLogs(rt, { purpose: 'bundle' }), { status: 'cancelled' });
  assert.deepEqual(rt.calls.map(c => c[0]), ['host.saveFile']);
  rt = fakeRt({ 'host.saveFile': rtErr('conflict', { reason_code: 'dialog_open' }) });
  assert.equal((await LX.exportLogs(rt, { purpose: 'filtered', format: 'json', seqs: [1] })).status, 'dialog_open');
  rt = fakeRt({ 'host.saveFile': rtErr('io') });
  const r = await LX.exportLogs(rt, { purpose: 'filtered', format: 'text', seqs: [1] });
  assert.equal(r.status, 'dialog_failed');
  assert.equal(r.error.code, 'io');
  assert.equal(rt.calls.length, 1, 'no job without a grant');
});

test('export: host.saveFile ended by the runtime (a new generation, a revoked owner, a reconnected channel) is an interrupted export, not a dialog failure (review S4)', async () => {
  for (const code of ['runtime_gone', 'owner_revoked', 'runtime_unavailable']) {
    const rt = fakeRt({ 'host.saveFile': rtErr(code, code === 'runtime_unavailable' ? { reason_code: 'reconnected' } : {}) });
    const r = await LX.exportLogs(rt, { purpose: 'filtered', format: 'json', seqs: [1, 2] });
    assert.equal(r.status, 'interrupted', code);
    assert.equal(r.error.code, code);
    assert.equal(rt.calls.length, 1, 'no job');
  }
  // the job itself ended by a reconnected channel: interrupted too
  const rt = fakeRt({ 'host.saveFile': { cancelled: false, ref: 'save-1' }, 'logs.exportBundle': rtErr('runtime_unavailable', { reason_code: 'reconnected' }) });
  assert.equal((await LX.exportLogs(rt, { purpose: 'bundle' })).status, 'interrupted');
});

test('export: a filtered export names the seqs of the view (never a filter, R9), at most 4,000; JSON / text only (U5)', async () => {
  const rt = fakeRt({ 'host.saveFile': { cancelled: false, ref: 'save-1' }, 'logs.exportFiltered': { ref: 'exp-1', count: 2, missing_seqs: [3] } });
  const seqs = Array.from({ length: 4100 }, (_, i) => i + 1);
  const r = await LX.exportLogs(rt, { purpose: 'filtered', format: 'json', seqs });
  assert.equal(r.status, 'done');
  assert.deepEqual(r.result.missing_seqs, [3], 'G41: what the sidecar no longer had');
  assert.deepEqual(rt.calls[0], ['host.saveFile', { purpose: 'filtered', format: 'json' }]);
  const [cmd, args] = rt.calls[1];
  assert.equal(cmd, 'logs.exportFiltered');
  assert.deepEqual(Object.keys(args).sort(), ['format', 'save_ref', 'seqs']);
  assert.equal(args.save_ref, 'save-1');
  assert.equal(args.seqs.length, 4000);
  assert.equal(args.seqs[3999], 4100, 'the newest 4,000');
  assert.deepEqual(LX.FORMATS.filtered, ['json', 'text']);
  await assert.rejects(LX.exportLogs(rt, { purpose: 'filtered', format: 'csv', seqs: [1] }));
  assert.deepEqual(await LX.exportLogs(rt, { purpose: 'filtered', format: 'json', seqs: [] }), { status: 'empty' }, 'O-2: nothing shown, nothing exported');
});

test('export: the bundle job (zip); failed, interrupted and refused jobs', async () => {
  let rt = fakeRt({ 'host.saveFile': { cancelled: false, ref: 'save-2' }, 'logs.exportBundle': { ref: 'b-1', size: 2048, files: ['monitor.log'] } });
  let r = await LX.exportLogs(rt, { purpose: 'bundle' });
  assert.equal(r.status, 'done');
  assert.deepEqual(rt.calls.map(c => c[1]), [{ purpose: 'bundle', format: 'zip' }, { save_ref: 'save-2' }]);
  for (const [code, want] of [['io', 'failed'], ['not_allowed', 'failed'], ['snapshot_required', 'failed'], ['job_capacity', 'failed'],
    ['cancelled', 'interrupted'], ['superseded', 'interrupted'], ['runtime_gone', 'interrupted'], ['owner_revoked', 'interrupted']]) {
    rt = fakeRt({ 'host.saveFile': { cancelled: false, ref: 's' }, 'logs.exportBundle': rtErr(code) });
    r = await LX.exportLogs(rt, { purpose: 'bundle' });
    assert.equal(r.status, want, code);
    assert.equal(r.error.code, code);
  }
  // the job's progress is handed over once the dialog is closed
  let job = null;
  rt = fakeRt({ 'host.saveFile': { cancelled: false, ref: 's' }, 'logs.exportBundle': { ref: 'b', size: 1, files: [] } });
  await LX.exportLogs(rt, { purpose: 'bundle', onJob: p => { job = p; } });
  assert.ok(job && typeof job.then === 'function');
});

test('reveal: revealed, gone (revealed:false, or a ref of an earlier sidecar: not_allowed), failed', async () => {
  assert.deepEqual(await LX.revealExport(fakeRt({ 'host.reveal': { revealed: true } }), 'r'), { status: 'revealed' });
  assert.deepEqual(await LX.revealExport(fakeRt({ 'host.reveal': { revealed: false } }), 'r'), { status: 'gone' });
  assert.equal((await LX.revealExport(fakeRt({ 'host.reveal': rtErr('not_allowed') }), 'r')).status, 'gone');
  assert.equal((await LX.revealExport(fakeRt({ 'host.reveal': rtErr('io') }), 'r')).status, 'failed');
  assert.deepEqual(await LX.revealExport(fakeRt({}), null), { status: 'gone' });
});
