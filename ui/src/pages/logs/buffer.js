/* ==========================================================================
   pages/logs/buffer.js — the logs page's in-memory buffer (T-P4-05c)
   No DOM: the page (pages/logs.js) renders what this module keeps, and the unit test
   (ui/test/unit/logs_buffer.test.mjs) drives it directly.

   What it keeps (PROTOCOL §5.5 logs.batch, §11 logs.tail / LogPage, §16 Log; 映射_logs §2,
   §5.1 L-1…L-7, L-12):
     rows      oldest first: the v1 Log records of the current generation, normalised (below),
               plus three kinds of rows of our own:
                 gap       「此处有 N 条日志未送达」: a seq jump, a batch with dropped > 0 or
                           gap:true, a kind:'gap' frame, the shell's rt:log-gap (L-5)
                 restart   「应用后台已重新启动」: a generation change (rt:reset with
                           generation_changed:true; L-7, R5)
               A source boundary (key source_boundary, L-6) is a record (it has a seq and is
               exported) that renders as a boundary line.
     capacity  CAP = 4,000 rows (方案 §6.12; the prototype's 1,500 is gone), trimmed in chunks
               of SLACK past it; trimmed records are counted (the history row, L-12, O-3).
     ord       every row gets a page-wide ordinal that only grows (never reset, not even by a
               generation change): the page's watermarks (clear, pause, undo) compare ords, so
               a new epoch's seq restarting at 1 cannot confuse them. `seq` is the wire seq,
               used to de-duplicate, to splice the seed and the live tail, and for exports.
   Generation change vs same-epoch reconnect (criticism B9, P4 architecture §2.3): only reset()
   clears; the client's rt:reset{generation_changed:false} reaches nobody here and a replayed
   batch is dropped by seq (seq <= lastSeq), so a reconnect neither clears, nor inserts a
   separator, nor duplicates a row.
   Seeding (L-1…L-4): beginSeed() parks the live batches; applySeed(pages) rebuilds the rows
   from the logs.tail pages (ascending), then replays the parked batches on top (seq <=
   watermark dropped, a jump past it is a gap). abortSeed() replays them on the old rows. Either
   way the replayed records are handed back (the page counts them for the badge), and a seed
   that rebuilt rows the page had already marked (a retry after a failure) hands back mapOrd to
   carry the page's ord marks over by seq.
   A kind:'gap' frame's row is settled by the next batch: its record jump, else the growth of
   dropped_total, else the row was a false alarm and goes.
   Runs (R2): a record's run is its run_id (PROTOCOL §5.5, the same id as logs.feed.run_id);
   when the field is absent (a frozen sidecar) a runtime.start_requested record starts a new
   run and the records after it inherit it. Records before the first run have no run.
   Key events (R1, criticism B10): is_key_event when the field is there; when it is absent,
   every record whose key is not "log.raw" / "" / source_boundary is an event (PROTOCOL §5.5:
   absence is never read as false).
   ========================================================================== */

export const CAP = 4000;
export const SLACK = 100;
// the contract names the page reads (PROTOCOL §5.5, §11)
export const TAIL_QUERY = 'logs.tail';
export const FEED_TOPIC = 'logs.feed';
export const SEED_TRIES = 3;          // snapshot_required in a row before 无法载入日志 (L-3 → L-2)
export const SEED_PAGES = 64;         // a 4,000-row group in 256 KiB pages needs far fewer
export const RAW_KEYS = new Set(['', 'log.raw']);
export const BOUNDARY_KEY = 'source_boundary';
export const RUN_START_KEY = 'runtime.start_requested';
export const RUN_STOP_KEY = 'runtime.desktop_stopped';
export const RUN_FAIL_KEY = 'runtime.desktop_failed';
export const RUN_CANCEL_KEY = 'runtime.start_cancelled';      // B-20: only once the bridge sends it
// the three connection keys that fold into one row in 原始日志 (B-21: no other noise kinds)
export const NOISE_KEYS = Object.freeze({ 'mihomo.connection_timeout': 'timeout', 'mihomo.connection_failed': 'failed', 'mihomo.dns_resolve_failed': 'dns' });
export const LEVELS = Object.freeze(['debug', 'info', 'warn', 'error', 'critical']);
export const SOURCES = Object.freeze(['TESSERACT', 'MIHOMO', 'ALERT', 'UNKNOWN']);
export const CATEGORIES = Object.freeze(['runtime', 'routing', 'health', 'circuit_breaker', 'update', 'configuration', 'connection', 'alert', 'other']);

/** the record is a key event: is_key_event when present, otherwise by key (PROTOCOL §5.5) */
export function isKeyEvent(dto) {
  if (typeof dto.is_key_event === 'boolean') return dto.is_key_event;
  const k = typeof dto.key === 'string' ? dto.key : '';
  return !RAW_KEYS.has(k) && k !== BOUNDARY_KEY;
}

/** a source boundary's JSON (detail, else raw): {from, to, at_ms, reason, continuity}; null when
    it does not parse (the record is then shown as a plain raw line) */
export function parseBoundary(dto) {
  for (const s of [dto.detail, dto.raw]) {
    if (typeof s !== 'string' || !s.trim().startsWith('{')) continue;
    try {
      const o = JSON.parse(s);
      if (o && typeof o === 'object' && !Array.isArray(o)) {
        const str = v => (typeof v === 'string' ? v : null);
        return { from: str(o.from), to: str(o.to), at_ms: Number.isFinite(o.at_ms) ? o.at_ms : null, reason: str(o.reason), continuity: str(o.continuity) };
      }
    } catch (e) { /* not JSON: a raw line */ }
  }
  return null;
}

const text = v => (typeof v === 'string' ? v : v == null ? '' : String(v));

/**
 * A v1 Log DTO → the page's record. ALERT is the alert module's own line: it reads as
 * Tesseract with category alert (G45, W-22; 映射_logs §2); the wire source stays in `wire`.
 */
export function normalize(dto, ord, gen) {
  const source = SOURCES.includes(dto.source) ? dto.source : 'UNKNOWN';
  const key = text(dto.key);
  const boundary = key === BOUNDARY_KEY ? parseBoundary(dto) : null;
  const level = LEVELS.includes(dto.level) ? dto.level : 'info';
  const cat = source === 'ALERT' ? 'alert' : CATEGORIES.includes(dto.category) ? dto.category : 'other';
  return {
    kind: 'log', ord, gen, id: 'x' + ord, seq: dto.seq, ts: Number(dto.ts_ms) || 0, rx: Number(dto.rx_ms) || Number(dto.ts_ms) || 0,
    level, src: source === 'ALERT' ? 'TESSERACT' : source, wire: source, cat, key: RAW_KEYS.has(key) ? 'log.raw' : key,
    title: text(dto.title), detail: text(dto.detail), raw: text(dto.raw),
    ev: key === BOUNDARY_KEY ? false : isKeyEvent(dto), runId: typeof dto.run_id === 'string' && dto.run_id ? dto.run_id : null,
    boundary: key === BOUNDARY_KEY && boundary ? boundary : null, noise: NOISE_KEYS[key] || null,
    dto,
  };
}

/** the Log DTO fields of a record, as they came (copy as JSON, 映射_logs #70) */
export function dtoOf(r) {
  const d = r.dto || {};
  const out = { seq: d.seq, ts_ms: d.ts_ms, rx_ms: d.rx_ms, key: d.key, source: d.source, level: d.level, category: d.category, title: d.title, detail: d.detail, raw: d.raw };
  if (typeof d.is_key_event === 'boolean') out.is_key_event = d.is_key_event;
  if (typeof d.run_id === 'string') out.run_id = d.run_id;
  return out;
}

/**
 * createLogBuffer({cap, slack}) → buffer
 *   rows                    the rows (read only)
 *   ingest(evt)             an rt:logs event {kind:'edge'|'gap', data}; → {added: [row…], gaps: n}
 *   logGap(data)            the shell's rt:log-gap {from_seq, to_seq, dropped, record_*}
 *   reset()                 a generation change; → the restart row
 *   beginSeed() · applySeed(pages) → {rows, added, mapOrd} · abortSeed() → added · seeding
 *   relabel(records)        a re-fetched page in the current language: titles / details by seq
 *   trim()                  → the rows removed (oldest first); call after ingest
 *   runs · run(key) · lastRun()
 *   counts                  {head, trimmed, lost, droppedTotal}: before the seed (LogPage.gap /
 *                           dropped), trimmed here, lost in live gaps, the batch's dropped_total
 *   version                 bumped on any change that may alter rows before the tail
 */
export function createLogBuffer({ cap = CAP, slack = SLACK } = {}) {
  let rows = [];
  let ord = 0, gen = 0, lastSeq = null, lastRunKey = null, synth = 0;
  let seeding = false, pending = [], pendingGap = null;
  const runs = new Map();            // run key -> {key, id, start, last, end, failed, cancelled, gen}
  const counts = { head: 0, headGap: false, trimmed: 0, lost: 0, droppedTotal: 0 };
  let version = 0;

  function runOf(rec) {
    let key;
    if (rec.runId) key = 'id:' + rec.runId;
    else if (rec.key === RUN_START_KEY) key = 's:' + (++synth);
    else key = lastRunKey;
    if (!key) return null;
    let r = runs.get(key);
    if (!r) { r = { key, id: rec.runId, start: rec.ts, last: rec.ts, end: null, failed: false, cancelled: false, gen }; runs.set(key, r); }
    r.last = Math.max(r.last, rec.ts);
    if (rec.key === RUN_STOP_KEY) r.end = rec.ts;
    else if (rec.key === RUN_FAIL_KEY) { r.failed = true; r.end = rec.ts; }
    else if (rec.key === RUN_CANCEL_KEY) { r.cancelled = true; r.end = rec.ts; }
    lastRunKey = key;
    return key;
  }

  function push(dto) {
    const rec = normalize(dto, ++ord, gen);
    rec.run = runOf(rec);
    rows.push(rec);
    return rec;
  }

  /** a gap row: n records missing (null: unknown), seqs from..to when known */
  function gapRow(n, from, to) {
    const prev = rows[rows.length - 1];
    const row = { kind: 'gap', ord: ++ord, gen, id: 'g' + ord, n: Number.isInteger(n) && n > 0 ? n : null, from: from == null ? null : from, to: to == null ? null : to,
      ts: prev ? prev.ts : Date.now(), run: lastRunKey };
    rows.push(row);
    if (row.n) counts.lost += row.n;
    return row;
  }

  /** missing seqs from..to (inclusive), n of them: a gap row, or the pending one filled in */
  function missing(from, to, n) {
    const last = rows[rows.length - 1];
    if (pendingGap && last === pendingGap) {
      // a kind:'gap' frame announced the hole; the next batch's jump says how big it is
      pendingGap.n = n > 0 ? n : pendingGap.n; pendingGap.from = from; pendingGap.to = to;
      if (pendingGap.n) counts.lost += pendingGap.n;
      const g = pendingGap; pendingGap = null; return g;
    }
    pendingGap = null;
    return gapRow(n, from, to);
  }

  /** a kind:'gap' frame's row is still the last one and the batch after it shows no hole (no
      seq jump, no dropped, no gap flag): the lost frames held no record we lack. What they may
      have held is a producer drop, which dropped_total says: its growth since the frame is the
      row's count; no growth, and the row was a false alarm and goes (review S1) */
  function settleGap(data) {
    const g = pendingGap; pendingGap = null;
    const grew = Number.isInteger(data.dropped_total) && Number.isInteger(g.dt0) ? data.dropped_total - g.dt0 : 0;
    if (grew > 0) { g.n = grew; counts.lost += grew; return; }
    const i = rows.lastIndexOf(g);
    if (i >= 0) rows.splice(i, 1);
  }

  function edge(data, added) {
    const recs = Array.isArray(data.records) ? data.records : [];
    let gaps = 0;
    const from = Number.isInteger(data.from_seq) ? data.from_seq : (recs[0] && recs[0].seq);
    const dropped = Number.isInteger(data.dropped) && data.dropped > 0 ? data.dropped : 0;
    // the hole before this batch: a seq jump, or the batch's own dropped count / gap flag
    let jump = 0, hole = false;
    if (lastSeq != null && Number.isInteger(from) && from > lastSeq + 1) jump = from - lastSeq - 1;
    if (jump || dropped || data.gap === true) {
      if (lastSeq != null || jump || dropped) {
        const n = Math.max(jump, dropped) || null;
        missing(jump ? lastSeq + 1 : null, jump ? from - 1 : null, n || 0);
        gaps++; hole = true;
      }
    }
    if (!hole && pendingGap) {
      if (rows[rows.length - 1] === pendingGap) { settleGap(data); gaps++; } else pendingGap = null;
    }
    if (Number.isInteger(data.dropped_total) && data.dropped_total > counts.droppedTotal) counts.droppedTotal = data.dropped_total;
    for (const dto of recs) {
      if (!dto || !Number.isInteger(dto.seq)) continue;
      if (lastSeq != null && dto.seq <= lastSeq) continue;           // replay / duplicate
      if (lastSeq != null && dto.seq > lastSeq + 1 && added.length) {
        // a jump inside one batch (records must be consecutive): the hole is shown too
        missing(lastSeq + 1, dto.seq - 1, dto.seq - lastSeq - 1); gaps++;
      }
      added.push(push(dto));
      lastSeq = dto.seq;
    }
    if (Number.isInteger(from) && recs.length === 0 && (lastSeq == null || from - 1 > lastSeq)) lastSeq = from - 1;
    return gaps;
  }

  function ingest(evt) {
    const added = [];
    if (!evt || !evt.data || typeof evt.data !== 'object') return { added, gaps: 0 };
    if (seeding) { pending.push(evt); return { added, gaps: 0, parked: true }; }
    let gaps = 0;
    if (evt.kind === 'gap') {
      // lost batch frames (from_seq..to_seq are edge seqs, not record seqs): the hole is
      // shown now; the next batch says how many records it held (its record jump, else the
      // growth of dropped_total since now; neither, and the row goes: settleGap). A second
      // frame while the first row is still open adds no row.
      if (!(pendingGap && rows[rows.length - 1] === pendingGap)) {
        pendingGap = gapRow(null, null, null);
        pendingGap.dt0 = counts.droppedTotal;
      }
      gaps = 1;
    } else gaps = edge(evt.data, added);
    if (gaps) version++;
    return { added, gaps };
  }

  /** the shell's rt:log-gap: its cache lost the oldest batches (records record_from_seq..to) */
  function logGap(d) {
    if (!d || typeof d !== 'object') return null;
    const rf = Number.isInteger(d.record_from_seq) ? d.record_from_seq : null, rt = Number.isInteger(d.record_to_seq) ? d.record_to_seq : null;
    const n = Number.isInteger(d.record_dropped) && d.record_dropped > 0 ? d.record_dropped : rf != null && rt != null && rt >= rf ? rt - rf + 1 : 0;
    if (seeding) { pending.push({ kind: 'log-gap', data: d }); return null; }
    if (lastSeq == null) {
      // nothing here yet: the hole is the history before the first record (the origin row)
      counts.head += n; counts.headGap = true;
      if (rt != null) lastSeq = rt;
      version++;
      return null;
    }
    if (rt != null && rt <= lastSeq) return null;                    // already here
    const from = rf != null ? Math.max(rf, lastSeq + 1) : lastSeq + 1;
    const row = missing(from, rt, rt != null ? rt - from + 1 : n);
    if (rt != null) lastSeq = rt;
    version++;
    return row;
  }

  function reset() {
    gen++;
    rows = [];
    runs.clear();
    lastSeq = null; lastRunKey = null; pendingGap = null;
    counts.head = 0; counts.headGap = false; counts.trimmed = 0; counts.lost = 0; counts.droppedTotal = 0;
    seeding = false; pending = [];
    const row = { kind: 'restart', ord: ++ord, gen, id: 'z' + ord, ts: Date.now(), run: null };
    rows.push(row);
    version++;
    return row;
  }

  function beginSeed() { seeding = true; pending = []; }

  function replayPending(list) {
    const added = [];
    for (const e of list) {
      if (e.kind === 'log-gap') { const g = logGap(e.data); if (g) added.push(g); continue; }
      added.push(...ingest(e).added);
    }
    return added;
  }

  /** an ord of the rows before a seed → the ord of the same place in the rows after it (review
      S2): the newest record of this generation it covered, found again by seq; covering no
      record, the kept restart row (or 0). The rows are in ord order before and after. */
  function ordMapper(old) {
    return o => {
      if (!o) return 0;
      let s = null, base = 0;
      for (const r of old) {
        if (r.ord > o) break;
        if (r.gen !== gen) continue;
        if (r.kind === 'log') s = r.seq;
        else if (r.kind === 'restart') base = r.ord;
      }
      if (s == null) return base;
      let to = base;
      for (const r of rows) {
        if (r.kind !== 'log') continue;
        if (r.seq > s) break;
        to = r.ord;
      }
      return to;
    };
  }

  /**
   * pages: the LogPage objects of one logs.tail group, in order (ascending records).
   * → {rows, added, mapOrd}
   *   added    the parked live records replayed on top (not in the seed): the page counts them
   *            like any live batch (the badge, review S3)
   *   mapOrd   every row was rebuilt with a new ord: the page's ord marks (clear watermarks,
   *            the pause point, the undo) taken before the seed are mapped through it by seq
   */
  function applySeed(pages) {
    const list = pending; pending = []; seeding = false;
    const old = rows;
    const keep = rows.filter(r => r.kind === 'restart' && r.gen === gen);
    rows = keep;
    runs.clear(); lastSeq = null; lastRunKey = null; pendingGap = null;
    counts.trimmed = 0; counts.lost = 0;
    const first = pages[0] || {};
    counts.head = Number.isInteger(first.dropped) ? first.dropped : 0;
    counts.headGap = first.gap === true || counts.head > 0;
    let watermark = null;
    for (const p of pages) {
      for (const dto of Array.isArray(p.records) ? p.records : []) {
        if (!dto || !Number.isInteger(dto.seq) || (lastSeq != null && dto.seq <= lastSeq)) continue;
        push(dto); lastSeq = dto.seq;
      }
      if (Number.isInteger(p.watermark)) watermark = Math.max(watermark == null ? 0 : watermark, p.watermark);
    }
    if (watermark != null && (lastSeq == null || watermark > lastSeq)) lastSeq = watermark;
    version++;
    const added = replayPending(list);
    return { rows, added, mapOrd: ordMapper(old) };
  }

  /** the seed failed or was dropped: the parked batches go on top of the rows already here */
  function abortSeed() {
    if (!seeding) return [];
    const list = pending; pending = []; seeding = false;
    return replayPending(list);
  }

  function relabel(records) {
    const bySeq = new Map();
    for (const dto of records || []) if (dto && Number.isInteger(dto.seq)) bySeq.set(dto.seq, dto);
    let n = 0;
    for (const r of rows) {
      if (r.kind !== 'log' || r.gen !== gen) continue;
      const d = bySeq.get(r.seq);
      if (!d) continue;
      const t = text(d.title), dt = text(d.detail);
      if (t !== r.title || dt !== r.detail) {
        r.title = t; r.detail = dt;
        r.dto = { ...r.dto, title: d.title, detail: d.detail };
        n++;
      }
    }
    if (n) version++;
    return n;
  }

  function trim() {
    if (rows.length <= cap + slack) return [];
    const n = rows.length - cap;
    const gone = rows.splice(0, n);
    counts.trimmed += gone.filter(r => r.kind === 'log').length;
    const live = new Set(rows.map(r => r.run).filter(Boolean));
    for (const k of [...runs.keys()]) if (!live.has(k) && k !== lastRunKey) runs.delete(k);
    version++;
    return gone;
  }

  return {
    get rows() { return rows; },
    get seeding() { return seeding; },
    get gen() { return gen; },
    get lastSeq() { return lastSeq; },
    get version() { return version; },
    get ord() { return ord; },
    counts,
    runs,
    run: key => (key ? runs.get(key) || null : null),
    lastRun() { return lastRunKey ? runs.get(lastRunKey) || null : null; },
    runById(id) { return id ? runs.get('id:' + id) || null : null; },
    ingest, logGap, reset, beginSeed, applySeed, abortSeed, relabel, trim,
    bump() { version++; },
  };
}

/**
 * The buffer on the runtime's events (pages/logs.js from the moment it loads; the unit test on a
 * FakeTransport):
 *   rt:logs       a logs.batch frame {kind, data, instant}: ingest, then trim
 *   rt:log-gap    the shell's log tail lost batches: a gap (or the history's start), then trim
 *   rt:reset      only generation_changed:true clears (reset() inserts the restart row); a
 *                 same-epoch reconnect (generation_changed:false) does nothing here: its replayed
 *                 batches are dropped by rev in the client and by seq in the buffer (criticism B9)
 *   attachBuffer(T, buf, {added(rows, evt), trimmed(rows), changed(), generation()}) → off()
 *     added      the records a live batch added (not called for a batch parked by the seed)
 *     trimmed    the rows the 4,000 cap pushed out (possibly none)
 *     changed    the rows changed: the page schedules a render (createLiveGate)
 *     generation after reset(): the page drops what belonged to the old generation
 */
export function attachBuffer(T, buf, { added = () => {}, trimmed = () => {}, changed = () => {}, generation = () => {} } = {}) {
  const offs = [
    T.on('rt:logs', e => {
      if (!e || !e.data) return;
      const r = buf.ingest(e);
      if (r.parked) return;
      added(r.added, e);
      trimmed(buf.trim());
      if (r.added.length || r.gaps) changed();
    }),
    T.on('rt:log-gap', d => { buf.logGap(d); trimmed(buf.trim()); changed(); }),
    T.on('rt:reset', e => { if (e && e.generation_changed) { buf.reset(); generation(); changed(); } }),
  ];
  return () => offs.forEach(off => { if (typeof off === 'function') off(); });
}

/**
 * The seed's fetch (L-1…L-4; PROTOCOL §11 logs.tail, bridge_log_view.py): one group of
 * logs.tail{n} pages, oldest first, following next_cursor until it is empty. The cursor expires
 * after 60 s idle and is refused after a clear, an eviction or a language change: a
 * snapshot_required restarts the whole group (pages already taken are dropped), up to `tries`
 * groups in a row; that, or any other error, rejects (the page shows 无法载入日志 with a retry).
 *   fetchSeed(query, {n, tries, maxPages, alive}) → Promise<[LogPage…] | null>
 *     query(args)  → Promise<LogPage> (the page passes args => T.rt.query('logs.tail', args))
 *     alive()      false once a newer seed or a generation change took over: resolves null
 */
export async function fetchSeed(query, { n = CAP, tries = SEED_TRIES, maxPages = SEED_PAGES, alive = () => true } = {}) {
  for (let attempt = 1; ; attempt++) {
    try {
      const pages = [];
      let cursor = null;
      for (let i = 0; i < maxPages; i++) {
        const p = await query(cursor ? { n, cursor } : { n });
        if (!alive()) return null;
        pages.push(p && typeof p === 'object' ? p : {});
        cursor = p && typeof p.next_cursor === 'string' && p.next_cursor ? p.next_cursor : null;
        if (!cursor) break;
      }
      return pages;
    } catch (e) {
      if (!alive()) return null;
      if (!(e && e.code === 'snapshot_required') || attempt >= tries) throw e;
    }
  }
}

/**
 * The live path between the buffer and the screen (criticism D8, P4 architecture §10): while
 * the window is hidden a batch only goes into the buffer; nothing is scheduled and nothing is
 * painted. The first moment it is visible again, one render brings the page up to date.
 *   createLiveGate({isHidden, schedule, render}) → {note(), shown()}
 *     note()   records arrived: schedules one render (coalesced) unless hidden
 *     shown()  the window became visible: one render if anything arrived meanwhile
 */
export function createLiveGate({ isHidden = () => false, schedule = fn => fn(), render = () => {} } = {}) {
  let due = false, queued = false;
  const run = () => { queued = false; if (isHidden()) { due = true; return; } due = false; render(); };
  return {
    note() {
      if (isHidden()) { due = true; return; }
      if (queued) return;
      queued = true; schedule(run);
    },
    shown() {
      if (!due || isHidden() || queued) return;
      queued = true; schedule(run);
    },
    get due() { return due; },
  };
}
