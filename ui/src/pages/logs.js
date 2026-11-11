/* ==========================================================================
   pages/logs.js — 运行日志
   Answers "what happened, and why?".
     command bar   关键事件 / 原始日志 · search (可见/总数) · 全部 / 问题 / 错误 · 筛选
                   (只看本次运行 + 来源 + 类别 in one popover) · 实时跟随 / 已暂停 · ⋯
                   (导出诊断包 and the exports live in ⋯)
     log panel     one framed object: the current-run summary is its head, one
                   scroller (关键事件 list or 原始日志 table) its body, the feed
                   status line its foot
     reader        a modeless side drawer docked inside the panel's body (between the
                   run summary and the status line); ↑ ↓ browse
   Records (T-P4-05c): the v1 Log records of logs.batch, taken from the moment this module loads
   (so the sidebar badge counts from boot, 映射_logs R13), and of the logs.tail seed on the first
   visit (paged by next_cursor; a snapshot_required refetches the whole group, three in a row
   show 无法载入日志 with a retry; L-1…L-4). pages/logs/buffer.js keeps the newest 4,000 rows
   with the page's own rows: gaps (此处有 N 条日志未送达, L-5) and the restart separator of a
   generation change (L-7; a same-epoch reconnect only de-duplicates by seq). Exports go
   through pages/logs/export.js: host.saveFile → logs.exportBundle / logs.exportFiltered{seqs}
   (the seqs of the current view, never a filter: R9) → host.reveal.
   Rows are patched by data-key: unchanged rows keep their element (focus,
   selection and a running glow survive live updates). A live record rebuilds
   only the tail (the last group onward); filters rebuild the list and move
   only what is on screen (viewMotion). 原始日志 rows sit in chunks of ~64 lines
   with content-visibility, so off-screen lines cost no layout or paint; the
   view that is not shown keeps its rendering (content-visibility: hidden) and
   is brought up to date while idle, so switching views only reveals it.
   Untrusted text (映射_logs §7): titles, details, raw lines and event keys only ever become
   text nodes. Rows are built as DOM (a cloned skeleton per row kind, textContent for the data, a
   query marked with <mark> elements); a changed row takes the children of a freshly built one
   (node-level, its element stays); nothing is parsed from a string.
   While the window is hidden (store presence.visible = false, or the document is hidden) a batch
   only goes into the buffer: neither the list, the sidebar badge, the run strip nor the footer
   is written until the window is visible again, then once (criticism D8, review S5).
   Wording (T-P4-04d): every on-screen word is a logs.* message (src/i18n/locales/zh_CN/logs.json);
   records keep the v1 enums (level, source TESSERACT / MIHOMO / ALERT / UNKNOWN, category
   runtime / routing / …, PROTOCOL §5.5, §16) and are named on screen through the catalog. Log
   titles, details and raw lines are data: they render under translate="no". The suggestion cards
   (B-22) are built only inside if (T.rt.feature('B-22')); what they say comes from the advice
   source at the extension point T.ext 'logs/advice' (T-P4-07: the design target registers the
   prototype's cards there, design/demo.js; the app target none, so no card until the bridge sends
   an advice field). Dynamic text follows i18n:change (relocalize()); after a language change the
   history is fetched again so the backend's titles follow it (O-9).
   ========================================================================== */
import { cached as intlCached } from '../i18n/intl.js';
import { createLogBuffer, createLiveGate, attachBuffer, fetchSeed, dtoOf, CAP, FEED_TOPIC, TAIL_QUERY } from './logs/buffer.js';
import { exportLogs, revealExport } from './logs/export.js';

(function () {
  'use strict';
  const T = window.T, D = T.data, S = T.store, M = T.motion, F = T.fmt;
  const I = (n, s, c) => T.icon(n, s || 16, c);
  /* an icon as a node: parsed once per (name, size, class), then cloned */
  const ICONS = new Map();
  function icon(n, s, c) {
    const k = `${n}|${s || 16}|${c || ''}`;
    let p = ICONS.get(k);
    if (!p) { p = T.html`${I(n, s, c)}`; ICONS.set(k, p); }
    return p.cloneNode(true);
  }
  const pad = (n, w) => String(n).padStart(w || 2, '0');
  /** a catalog message (plain text) */
  const t = (id, p) => T.t(id, p);
  /* DOM builders: the element, its class, then text (always a text node) or child nodes */
  function mk(tag, cls, attrs) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (attrs) for (const k in attrs) if (attrs[k] != null && attrs[k] !== false) e.setAttribute(k, attrs[k] === true ? '' : attrs[k]);
    return e;
  }
  const txt = (tag, cls, s, attrs) => { const e = mk(tag, cls, attrs); e.textContent = s == null ? '' : String(s); return e; };

  /* ================================================================ vocabulary */
  const RANK = { debug: 0, info: 1, ok: 1, warn: 2, error: 3, critical: 3 };
  // level → style class; the word is logs.level.<level>
  const SEV = { debug: 'sev-debug', info: '', ok: 'sev-ok', warn: 'sev-warn', error: 'sev-error', critical: 'sev-error' };
  const sevWord = lv => t('logs.level.' + lv);
  /* the level a key-event row shows. The v1 Log has no 正常 (positive outcome) level: it reads as
     信息 (映射_logs §2, B-23). With B-23 open, a record the bridge marks tone 'ok' shows 正常 (the
     field B-23 asks the bridge for; nothing sends it in v1). */
  let b23 = false;
  const shownLv = r => (b23 && r.dto && r.dto.tone === 'ok' ? 'ok' : r.level);
  function sevEl(lv, extra, q) {
    const s = mk('span', 'sev' + (SEV[lv] ? ' ' + SEV[lv] : '') + (extra ? ' ' + extra : ''));
    markInto(s, sevWord(lv), q);
    return s;
  }
  // v1 Log.category (PROTOCOL §16) in the prototype's chip order. The prototype's 验证 has no
  // runtime category (方案 §4.1 C7): buffer.js reads an unknown category as other, so no record
  // carries it and its chip never shows (a chip is offered only with records behind it; 映射_logs
  // #8). The id stays listed for the day C7 gives it a runtime category (T-P4-04d).
  const CATS = ['runtime', 'routing', 'health', 'circuit_breaker', 'verification', 'update', 'configuration', 'connection', 'alert', 'other'];
  // (ALERT is not a chip: the alert module's lines are Tesseract's own, category alert; buffer.js)
  const SRCS = ['TESSERACT', 'MIHOMO', 'UNKNOWN'];
  /* what a category / source is called on screen: the word the user looks for (the record keeps the id) */
  const catLabel = c => t('logs.category.' + c);
  const srcName = s => t('logs.source.' + String(s || 'UNKNOWN').toLowerCase());
  const LEVELS = { all: 0, issue: 2, error: 3 };
  const noiseWord = k => t('logs.noise.' + k);
  /* a tier codename in running text (never translated). F-03 (user 10-28): it is named alone, with
     no role word beside it; text the backend writes is shown as written. */
  let tierRe = null;
  const TIER_RE = () => (tierRe = tierRe || /\b(Vanguard|Sentinel|Bastion|Gateway)\b(?!\s*\d)/g);
  /** the text a row shows (and search matches) */
  const shown = s => String(s || '');
  /* (11-05: the reader no longer adds a warning to a Bastion event; the warning is said once when
     traffic falls back to Bastion (the notice card) and in the letter for users) */

  /* ================================================================ time */
  /* rows are rebuilt on every live record: the per-timestamp strings are memoised (and dropped
     when the language changes: digits, separators and day words are the language's) */
  const memos = [];
  // (T-P4-09, mapping_logs R10: the cap follows the 4,000-line buffer, at least twice its size)
  const MEMO_CAP = 8000;
  const memo = f => { const m = new Map(); memos.push(m); return ts => { let v = m.get(ts); if (v === undefined) { if (m.size > MEMO_CAP) m.clear(); v = f(ts); m.set(ts, v); } return v; }; };
  const hms = memo(ts => F.time(ts));
  const ymd = ts => { const d = new Date(ts); return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`; };
  const hmsN = ts => { const d = new Date(ts); return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`; };
  // machine timestamps (copy, JSON, 诊断信息): ISO digits whatever the language
  const full = ts => `${ymd(ts)} ${hmsN(ts)}.${pad(new Date(ts).getMilliseconds(), 3)}`;
  const iso = ts => { const o = -new Date(ts).getTimezoneOffset(); return `${full(ts)}${o >= 0 ? '+' : '-'}${pad(Math.floor(Math.abs(o) / 60))}:${pad(Math.abs(o) % 60)}`; };
  const dayKey = memo(ts => ymd(ts).replace(/-/g, ''));
  /* day words by Intl (P4 architecture §3.4; 映射_logs §8): 今天 / 昨天 / 前天 from
     RelativeTimeFormat (numeric: auto), the month and day and the weekday from DateTimeFormat;
     the joiners are messages. Cached per language with T.fmt's Intl objects. */
  const lng = () => (T.i18n && T.i18n.lang) || 'zh_CN';
  const intl = (k, make) => intlCached(lng(), 'logs:' + k, make);
  function dayDiff(ts) { const a = new Date(ts); a.setHours(0, 0, 0, 0); const b = new Date(); b.setHours(0, 0, 0, 0); return Math.round((b - a) / 864e5); }
  const relDay = n => intl('rel', l => new Intl.RelativeTimeFormat(l, { numeric: 'auto' })).format(-n, 'day');
  const monthDay = ts => intl('md', l => new Intl.DateTimeFormat(l, { month: 'long', day: 'numeric' })).format(new Date(ts));
  const weekday = ts => intl('wd', l => new Intl.DateTimeFormat(l, { weekday: 'short' })).format(new Date(ts));
  /** 今天 · 昨天 · 前天, otherwise the month and day */
  function dayWord(ts) { const n = dayDiff(ts); return n >= 0 && n <= 2 ? relDay(n) : monthDay(ts); }
  /** the day label of a list: 「今天 · 9月29日 · 周二」 within two days, else 「9月26日 · 周五」. The
      label stands alone (a heading), so it starts with a capital where the script has one
      (RelativeTimeFormat gives "today", "сегодня" in running-text case) */
  function dayLabel(ts) {
    const n = dayDiff(ts), date = monthDay(ts), wd = weekday(ts);
    const s = n >= 0 && n <= 2 ? t('logs.list.day.recent', { rel: relDay(n), date, weekday: wd }) : t('logs.list.day.date', { date, weekday: wd });
    return s.charAt(0).toUpperCase() + s.slice(1);
  }
  const dur = ms => F.uptime(Math.max(0, ms / 1000));

  /* ================================================================ model */
  const B = createLogBuffer({ cap: CAP });   // the rows, oldest first (pages/logs/buffer.js)
  let bsGen = 0;                   // bumped when rows before the tail may have changed (trim, run state, relabel)
  const NO_RUN = Object.freeze({ key: null, start: null, last: null, end: null, failed: false, cancelled: false });
  const feed = () => (T.rt ? T.rt.topic(FEED_TOPIC).get() : undefined);
  /** the run the strip and 只看… speak of: the one logs.feed names (the current run), else the
      newest run in the buffer (映射_logs R2) */
  function curRun() {
    const f = feed();
    return (f && f.run_id && B.runById(f.run_id)) || B.lastRun() || NO_RUN;
  }
  const runOf = key => B.run(key) || NO_RUN;
  /** a record (not one of our rows, not a source boundary): what the counts and the views count */
  const isRec = r => r.kind === 'log' && !r.boundary;
  /** fields joined by a separator nobody types: a query matches inside one visible field, never across */
  const FS = '\u0001';
  /** what the 原始日志 message cell shows: the raw line, or the headline when the bridge kept none
      (a line classified as a failure keeps raw empty, PROTOCOL §5.5) */
  const rawOf = r => r.raw || r.title || '';
  /** the strings a query is matched against, in the current language's words (redone on i18n:change).
      Search matches only what the row shows, field by field: 关键事件 rows show the time, the level
      word, the headline and the detail (a tier name reads "Sentinel 订阅" on screen); 原始日志 rows
      show the time, the level, the source and the message, line by line. */
  function searchText(r) {
    if (r.kind !== 'log') { r.qe = r.qr = ''; return; }
    r.qe = r.ev ? [hms(r.ts), sevWord(shownLv(r)), shown(r.title), shown(r.detail)].join(FS).toLowerCase() : '';
    r.qr = [hms(r.ts), sevWord(r.level), srcName(r.src)].concat(String(rawOf(r)).split('\n')).join(FS).toLowerCase();
  }
  const qOf = (r, view) => { if (r.qr === undefined) searchText(r); return view === 'events' ? r.qe : r.qr; };
  const resetSearch = () => { for (const r of B.rows) r.qe = r.qr = undefined; };

  /* ================================================================ view state */
  const V = {
    view: 'events', q: '', level: 'all', cats: new Set(), srcs: new Set(), runOnly: false,
    wm: { events: 0, raw: 0 }, paused: false, pauseOrd: 0, open: new Set(),
    wrap: true, diag: false, dirty: { events: true, raw: true }, scroll: { events: null, raw: null },
  };
  const filtersActive = () => !!(V.q || V.level !== 'all' || V.cats.size || V.srcs.size || V.runOnly);
  /** what a build depends on besides the records: a live tail rebuild is valid only while it is unchanged */
  const buildSig = view => `${bsGen}|${B.version}|${V.q}|${V.level}|${[...V.cats].sort()}|${[...V.srcs].sort()}|${V.runOnly}|${V.wm[view]}|${V.paused ? V.pauseOrd : '-'}`;
  /* the clear watermark and the pause point compare ords (buffer.js): a page-wide order that a
     new epoch's seq restarting at 1 cannot confuse */
  function pass(r, view, o) {
    o = o || {};
    if (r.ord <= V.wm[view]) return false;
    if (V.paused && !o.held && r.ord > V.pauseOrd) return false;
    // our rows (a gap, the restart separator) and source boundaries are in both views and no
    // filter hides them (PROTOCOL §16 LogPage: a boundary is never filtered out; 映射_logs #41, #42)
    if (r.kind !== 'log' || r.boundary) return true;
    if (view === 'events' && !r.ev) return false;
    if (RANK[r.level] < LEVELS[V.level]) return false;
    if (V.cats.size && !V.cats.has(r.cat)) return false;
    if (V.srcs.size && !V.srcs.has(r.src)) return false;
    if (V.runOnly && r.run !== curRun().key) return false;
    if (V.q && !qOf(r, view).includes(V.q)) return false;
    return true;
  }
  const inView = (r, view) => isRec(r) && (view !== 'events' || r.ev);
  const held = r => V.paused && r.ord > V.pauseOrd;         // arrived while paused: kept, shown on 继续

  /* ================================================================ text */
  /** appends s to parent as text, the query (lower case) marked with <mark> elements; returns parent */
  function markInto(parent, s, q) {
    s = String(s == null ? '' : s);
    const low = q ? s.toLowerCase() : '';
    // (a lower-cased string of another length would put the marks in the wrong place: plain text then)
    if (!q || low.length !== s.length) { if (s) parent.append(s); return parent; }
    let i = 0, j;
    while ((j = low.indexOf(q, i)) !== -1) {
      if (j > i) parent.append(s.slice(i, j));
      parent.append(txt('mark', 'logs-hit', s.slice(j, j + q.length)));
      i = j + q.length;
    }
    if (i < s.length) parent.append(s.slice(i));
    return parent;
  }
  /** a tier name inside running text: the codename, a link to 层级 (the row form has no icon:
      several tags with glyphs made one line of text read like a toolbar). In a row it takes the
      weight and colour of the text around it (logs.css) and is a tab stop only in the selected
      row (tabStops). */
  function tierLinkEl(k, q, row) {
    const x = D.tier(k); if (!x) return document.createTextNode(k);
    const b = mk('button', 'ttag ttag-btn logs-tt', { type: 'button', 'data-go': 'profiles', 'data-go-tier': x.id, 'aria-label': t('logs.tier_link.aria', { name: x.name }) });
    if (row) b.tabIndex = -1;
    b.append(markInto(mk('b'), x.name, q));
    return b;
  }
  /* technical values (URLs, IPs with :port, CIDR, host names) set in mono: the same pattern as
     T.ui.tech (core.js), applied to text nodes */
  const TECH_RE = /([a-z][a-z0-9+.-]*:\/\/[^\s<>"'\uFF0C\u3002\uFF1B\u3001]+|\b(?:\d{1,3}\.){3}\d{1,3}(?:\/\d{1,2})?(?::\d{1,5})?\b|\b[0-9a-f]{1,4}(?::[0-9a-f]{1,4}){7}\b|(?:\b[0-9a-f]{1,4})?(?::[0-9a-f]{1,4})*::(?:[0-9a-f]{1,4}(?::[0-9a-f]{1,4})*)?(?:\/\d{1,3})?|\b(?:[a-z0-9-]+\.)+[a-z]{2,}(?::\d{1,5})?\b)/gi;
  /** plain-language text into parent: technical values in mono, tier names as links to 层级, the
      query marked; every piece of the text is a text node */
  function richInto(parent, s, q, row) {
    s = String(s || '');
    if (!s) return parent;
    const tiers = part => {
      let i = 0;
      for (const m of part.matchAll(TIER_RE())) {
        if (m.index > i) markInto(parent, part.slice(i, m.index), q);
        parent.append(tierLinkEl(m[1].toLowerCase(), q, row));
        i = m.index + m[0].length;
      }
      if (i < part.length) markInto(parent, part.slice(i), q);
    };
    let i = 0;
    for (const m of s.matchAll(TECH_RE)) {
      if (m[0].length <= 2) continue;
      if (m.index > i) tiers(s.slice(i, m.index));
      parent.append(markInto(mk('span', 'tech'), m[0], q));
      i = m.index + m[0].length;
    }
    if (i < s.length) tiers(s.slice(i));
    return parent;
  }
  const tiersIn = text => { const out = []; String(text || '').replace(TIER_RE(), (m, n) => { const k = n.toLowerCase(); if (!out.includes(k)) out.push(k); }); return out; };

  /* ================================================================ rows */
  const EVI = new Map(), RWI = new Map();       // row key → item { key, kind, recs, noise }
  const itemsFor = view => (view === 'events' ? EVI : RWI);
  const ROWSEL = { events: '.li[data-key]', raw: '.logs-rrow[data-key]' };   // ghosts carry no data-key
  const BS = { events: null, raw: null };       // the last build per view: its groups, for the live tail
  const LAB = ['logs-day', 'list-sep', 'logs-origin', 'logs-gap', 'logs-restart', 'logs-bound'];
  const isLab = el => LAB.some(c => el.classList.contains(c));
  /* A row of a build: {key, sig, make() → Element, ck?, apply?}. sig is the row's content in
     short (what it shows, in which language, under which query): an element whose sig is
     unchanged is kept as it is; make() builds the element from scratch, as DOM. */
  const lsig = () => lng();

  function dayRow(ts, n, raw) {
    const k = dayKey(ts), key = 'd' + k;
    return { key, sig: `D|${k}|${n}|${raw}|${lsig()}`, make: () => {
      const el = mk('div', raw ? 'logs-day logs-tday' : 'list-g logs-day', { role: 'presentation', 'data-key': key });
      el.append(txt('span', '', dayLabel(ts)), txt('span', 'logs-day-n', t('logs.list.day.count', { n })));
      return el;
    } };
  }
  /** the run's state as the separator says it: 本次运行 · 启动失败 · 已取消 (B-20) · 运行了 X */
  function runMeta(run) {
    const going = run.key === curRun().key && S.get('run') !== 'stopped' && !run.end;
    if (going) return t('logs.list.run.meta.current');
    if (run.failed) return t('logs.list.run.meta.failed');
    // a cancelled start reads as stopped until the bridge sends runtime.start_cancelled (B-20)
    if (run.cancelled && T.rt.feature('B-20')) return t('logs.list.run.meta.cancelled');
    // a run that ended: its stop record, or (no stop record kept) its last record
    const end = run.end != null ? run.end : run.key !== curRun().key ? run.last : null;
    return end != null && run.start != null ? t('logs.list.run.meta.ran', { duration: dur(end - run.start) }) : '';
  }
  function sepRow(run, dk, raw) {
    const cont = dayKey(run.start) !== dk;
    const lab = t(cont ? 'logs.list.run.continued' : 'logs.list.run.started', { time: hms(run.start) });
    const meta = runMeta(run);
    const key = `s${run.key}-${dk}`;
    return { key, sig: `S|${lab}|${meta}|${raw}`, make: () => {
      const el = mk('div', 'list-sep logs-sep' + (raw ? ' logs-tsep' : ''), { role: 'presentation', 'data-key': key });
      const s = txt('span', '', lab);
      if (meta) s.append(txt('span', 'logs-sep-m', ' · ' + meta));
      el.append(s);
      return el;
    } };
  }
  /* the history row: where the in-memory record starts. What came before was evicted from
     memory (B-25 not built: the bridge does not say whether those lines are in a file, so the
     page only says they left memory; with B-25 the prototype's sentence comes back) */
  function originRow(raw) {
    const first = B.rows.find(isRec), c = B.counts, n = c.head + c.trimmed;
    const start = t('logs.list.origin.start', { day: dayWord(first.ts), time: hms(first.ts) });
    const gone = T.rt.feature('B-25') ? t('logs.list.origin.dropped', { n }) : n ? t('logs.list.origin.evicted', { n }) : t('logs.list.origin.evicted_some');
    return { key: 'origin', sig: `O|${first.id}|${start}|${gone}|${raw}`, make: () => {
      const el = mk('div', 'logs-origin' + (raw ? ' logs-torigin' : ''), { role: 'presentation', 'data-key': 'origin' });
      const tt = mk('span', 'logs-origin-t'); tt.append(icon('history', 14), start);
      const b = mk('button', 'link-btn', { type: 'button', 'data-act': 'folder', tabindex: '-1' }); b.append(t('logs.list.origin.open_folder'), icon('chevron', 14));
      el.append(tt, txt('span', 'logs-origin-s', gone), b);
      return el;
    } };
  }
  const showOrigin = view => {
    const c = B.counts, first = B.rows.find(isRec);
    return !!first && (c.head > 0 || c.headGap || c.trimmed > 0) && !V.runOnly && V.wm[view] === 0 && !V.q && V.level === 'all' && !V.cats.size && !V.srcs.size && pass(first, 'raw');
  };
  const countDays = vis => { const m = {}; vis.forEach(r => { if (!isRec(r)) return; const k = dayKey(r.ts); m[k] = (m[k] || 0) + 1; }); return m; };

  /* ---- our own rows (both views, never filtered): a gap, the restart separator, a source
     boundary. Labels in the list (not selectable rows). ---- */
  function gapRow(r, raw) {
    const key = r.id, msg = r.n ? t('logs.list.gap', { n: r.n }) : t('logs.list.gap_unknown');
    return { key, sig: `G|${msg}|${raw}`, make: () => {
      const el = mk('div', 'logs-gap' + (raw ? ' logs-tgap' : ''), { role: 'presentation', 'data-key': key });
      const s = mk('span', 'logs-gap-t'); s.append(icon('warning', 14), msg);
      el.append(s);
      return el;
    } };
  }
  function restartRow(r, raw) {
    const key = r.id, msg = t('logs.list.restart', { time: hms(r.ts) });
    return { key, sig: `Z|${msg}|${raw}`, make: () => {
      const el = mk('div', 'list-sep logs-restart' + (raw ? ' logs-tsep' : ''), { role: 'presentation', 'data-key': key });
      el.append(txt('span', '', msg));
      return el;
    } };
  }
  /* a Core log source boundary (key source_boundary, PROTOCOL §16 LogPage): its JSON says where
     the lines come from now; the record's own title is the bridge's fixed English and is never
     shown (映射_logs #42) */
  function boundaryRow(r, raw) {
    const b = r.boundary, key = 'b' + r.id;
    const msg = t('logs.list.boundary', { to: b.to === 'stdout' || b.to === 'mihomo_api' ? b.to : 'none' });
    return { key, sig: `B|${msg}|${hms(r.ts)}|${raw}`, make: () => {
      const el = mk('div', 'logs-bound' + (raw ? ' logs-tbound' : ''), { role: 'presentation', 'data-key': key });
      el.append(txt('span', 'logs-bound-t', msg), txt('span', 'logs-bound-time', hms(r.ts)));
      return el;
    } };
  }
  const ownRow = (r, raw) => (r.kind === 'gap' ? gapRow(r, raw) : r.kind === 'restart' ? restartRow(r, raw) : boundaryRow(r, raw));

  /* ---- 关键事件: consecutive identical events fold into one ×N row ---- */
  function evGroup(vis, items) {
    for (const r of vis) {
      if (!isRec(r)) { items.push({ kind: 'own', run: r.kind === 'restart' ? null : r.run, dk: dayKey(r.ts), recs: [r] }); continue; }
      const last = items[items.length - 1], lv = shownLv(r), fid = `${lv}|${r.src}|${r.title}|${r.detail}`, dk = dayKey(r.ts);
      if (last && last.fid === fid && last.run === r.run && last.dk === dk) last.recs.push(r);
      else items.push({ fid, run: r.run, dk, recs: [r], kind: 'event' });
    }
    return items;
  }
  /* skeletons of the row kinds, cloned per row (one clone is far cheaper than one element per part) */
  const SK = {};
  function skel(name, build) { let p = SK[name]; if (!p) { p = SK[name] = build(); } return p.cloneNode(true); }
  /** rows for event items; st carries the day / run context and the per-day counts. Each item
      remembers the key of the first row it emitted (k0: its day label, run separator or itself). */
  function evRows(items, st, rows) {
    const q = V.q, L = lsig();
    for (const it of items) {
      const f = it.recs[0], last = it.recs[it.recs.length - 1], n = it.recs.length, dk = it.dk, at = rows.length;
      if (dk !== st.day) { st.day = dk; st.run = null; rows.push(dayRow(f.ts, st.perDay[dk] || 0, false)); }
      if (it.kind === 'own') {
        if (f.kind === 'restart') st.run = null;
        rows.push(ownRow(f, false));
        it.key = rows[rows.length - 1].key; it.k0 = rows[at].key;
        continue;
      }
      if (it.run !== st.run) { st.run = it.run; if (it.run) rows.push(sepRow(runOf(it.run), dk, false)); }
      const key = 'e' + f.id; it.key = key; EVI.set(key, it);
      const lv = shownLv(last), dim = RANK[lv] <= 1;
      // ×N sits with the headline it counts (a right-edge toggle read as a second "open" chevron);
      // the headline and the detail are the record's own text (data: translate="no")
      rows.push({ key, sig: `E|${f.id}|${last.id}|${n}|${lv}|${q}|${L}|${last.title}|${last.detail}`, make: () => {
        const el = skel('ev', () => {
          const e = mk('div', 'li logs-row', { role: 'option', 'aria-selected': 'false', tabindex: '-1' });
          const main = mk('span', 'logs-main');
          main.append(mk('span', 'logs-t', { translate: 'no' }), mk('span', 'logs-d', { translate: 'no' }));
          const act = mk('span', 'li-act', { 'aria-hidden': 'true' }); act.append(icon('chevron', 14));
          e.append(mk('span', 'logs-time'), mk('span', 'sev'), main, act);
          // (T-P4-09) a row below or above the window is skipped by layout and paint until it comes
          // near (CSSOM, no style attribute: CSP); one line high (--row-h-dense), the size it keeps
          // once it has been laid out. 原始日志 does the same per chunk (logs.css .logs-chunk)
          e.style.setProperty('content-visibility', 'auto');
          e.style.setProperty('contain-intrinsic-block-size', 'auto 30px');
          return e;
        });
        el.className = `li logs-row lv-${lv}${dim ? ' is-dim' : ''}`;
        el.setAttribute('data-key', key);
        const [tm, sv, main] = el.children;
        if (n === 1) markInto(tm, hms(last.ts), q);
        sv.replaceWith(sevEl(lv, '', q));
        richInto(main.children[0], last.title, q, true);
        richInto(main.children[1], last.detail, q, true);
        if (n > 1) {
          const b = mk('button', 'logs-fold', { type: 'button', tabindex: '-1', 'data-fold': key, 'aria-expanded': 'false', 'aria-label': t('logs.list.fold.aria', { n }), 'data-tip': t('logs.list.fold.tip', { n, time: hms(f.ts) }), 'data-press': true });
          const badge = mk('span', 'badge'); badge.append(mk('span', 'logs-n'));
          b.append(icon('chevron', 12, 'fold-chev'), badge);
          main.children[0].after(b);
        }
        return el;
      }, apply: n > 1 ? (el, anim) => applyGroup(el, it, anim) : null });
      if (n > 1) it.recs.forEach((m, i) => {
        const k = 'k' + m.id, end = i === n - 1; EVI.set(k, { key: k, kind: 'event', recs: [m], parent: key });
        rows.push({ key: k, sig: `K|${m.id}|${i}|${end}|${key}|${q}|${L}`, make: () => {
          const el = mk('div', 'li logs-row logs-kid fold-kid is-dim' + (end ? ' is-end' : ''), { role: 'option', 'aria-selected': 'false', tabindex: '-1', 'data-key': k, 'data-fold-of': key });
          const d = mk('span', 'logs-d');
          d.append(txt('span', 'logs-kid-n', t('logs.list.fold.nth', { n: i + 1 })), markInto(mk('span', 'logs-kid-raw', { translate: 'no' }), rawOf(m), q));
          const main = mk('span', 'logs-main'); main.append(d);
          const act = mk('span', 'li-act', { 'aria-hidden': 'true' }); act.append(icon('chevron', 14));
          el.append(markInto(mk('span', 'logs-time'), hms(m.ts), q), mk('span'), main, act);
          return el;
        }, apply: el => { el.hidden = !V.open.has(key); } });
      });
      it.k0 = rows[at].key;
    }
  }
  function buildEvents() {
    EVI.clear();
    const vis = B.rows.filter(r => pass(r, 'events'));
    const items = evGroup(vis, []), perDay = countDays(vis), rows = [];
    if (showOrigin('events')) rows.push(originRow(false));
    evRows(items, { day: null, run: null, perDay }, rows);
    const total = B.rows.filter(r => inView(r, 'events') && r.ord > V.wm.events && !held(r)).length;
    const shownN = vis.filter(isRec).length;
    BS.events = { sig: buildSig('events'), ord: B.ord, groups: items, perDay, visible: shownN, total };
    return { rows, visible: shownN, total };
  }
  function applyGroup(el, it, animate) {
    const n = it.recs.length, last = it.recs[it.recs.length - 1];
    const b = el.querySelector('.logs-fold'); if (b) { b.setAttribute('aria-expanded', String(V.open.has(it.key))); b.setAttribute('aria-label', t('logs.list.fold.aria', { n })); }
    const nEl = el.querySelector('.logs-n'); if (nEl) M.odometer(nEl, '×' + n, { label: t('logs.list.fold.count', { n }), instant: !animate });
    const tm = el.querySelector('.logs-time'), th = `${hms(last.ts)}|${V.q}`;
    if (tm && tm._h !== th) {
      tm._h = th;
      // the time rolls when no query marks it; a marked time is written as it is
      if (V.q) tm.replaceChildren(markInto(document.createDocumentFragment(), hms(last.ts), V.q));
      else M.swapText(tm, hms(last.ts), { instant: !animate });
    }
    if (animate && el._n != null && n > el._n) glow(el);
    el._n = n;
  }

  /* ---- 原始日志: verbatim lines; runs of connection noise fold into one row (the three
     connection keys only, B-21) ---- */
  function rawGroup(vis, groups, q) {
    for (const r of vis) {
      const last = groups[groups.length - 1], dk = dayKey(r.ts);
      if (!isRec(r)) { groups.push({ kind: 'own', run: r.kind === 'restart' ? null : r.run, dk, recs: [r] }); continue; }
      if (!q && r.noise && last && last.noise === r.noise && last.level === r.level && last.run === r.run && last.dk === dk) last.recs.push(r);
      else groups.push({ noise: !q && r.noise ? r.noise : null, level: r.level, run: r.run, dk, recs: [r] });
    }
    return groups;
  }
  /** the message cell: the first line, or the line the query hits when that is a later one */
  function rawMsgInto(cell, raw, q) {
    const ls = String(raw).split('\n');
    let i = 0;
    if (q && ls.length > 1 && !ls[0].toLowerCase().includes(q)) { const j = ls.findIndex(l => l.toLowerCase().includes(q)); if (j > 0) i = j; }
    if (i) cell.append(txt('span', 'logs-at', t('logs.raw.line_at', { n: i + 1 })));
    cell.append(markInto(mk('span', 'logs-rawt', { translate: 'no' }), ls[i], q));
    if (ls.length > 1) cell.append(txt('span', 'logs-more', i ? t('logs.raw.lines_total', { n: ls.length }) : t('logs.raw.lines_more', { n: ls.length - 1 })));
    return cell;
  }
  function rawLine(r, q, kid, ck) {
    const key = 'r' + r.id;
    return { key, ck, sig: `R|${r.id}|${q}|${kid || ''}|${lsig()}`, make: () => {
      const el = skel('raw', () => {
        const e = mk('div', 'logs-rrow', { role: 'row', 'aria-selected': 'false', tabindex: '-1' });
        e.append(mk('span', 'logs-c logs-ctime', { role: 'gridcell' }), mk('span', 'logs-c', { role: 'gridcell' }), mk('span', 'logs-c logs-src', { role: 'gridcell' }), mk('span', 'logs-c logs-msg', { role: 'gridcell' }));
        return e;
      });
      el.className = 'logs-rrow' + (RANK[r.level] <= 1 ? ' is-dim' : '') + (kid ? ' fold-kid' : '');
      el.setAttribute('data-key', key);
      if (kid) el.setAttribute('data-fold-of', kid);
      const [tm, lv, src, msg] = el.children;
      markInto(tm, hms(r.ts), q);
      lv.append(sevEl(r.level, '', q));
      markInto(src, srcName(r.src), q);
      rawMsgInto(msg, rawOf(r), q);
      return el;
    } };
  }
  /** the connection target of a noise line: B-21 asks the bridge for it (no field in v1) */
  const targetOf = r => (r.dto && typeof r.dto.target === 'string' && r.dto.target ? r.dto.target : null);
  /* 原始日志 rows live in chunks of ~64 lines (by order, never across a day label or run
     separator): content-visibility on ~65 chunks costs far less than on 4,000 rows (each one is
     an intersection target and a paint chunk), and a live line relays out one chunk. A row keeps
     its chunk whatever the filter, so filtering never moves rows between containers. */
  const CH = 64;
  const chunkOf = (r, g) => `${Math.floor(r.ord / CH)}-${g.dk}-${g.run}`;
  function rawRows(groups, st, rows) {
    const q = V.q, L = lsig(), b21 = T.rt.feature('B-21');
    for (const g of groups) {
      const f = g.recs[0], dk = g.dk, at = rows.length;
      if (dk !== st.day) { st.day = dk; st.run = null; rows.push(dayRow(f.ts, st.perDay[dk] || 0, true)); }
      if (g.kind === 'own') {
        if (f.kind === 'restart') st.run = null;
        rows.push(ownRow(f, true));
        g.key = rows[rows.length - 1].key; g.k0 = rows[at].key;
        continue;
      }
      if (g.run !== st.run) { st.run = g.run; if (g.run) rows.push(sepRow(runOf(g.run), dk, true)); }
      if (!g.noise || g.recs.length < 3) {
        g.recs.forEach(r => { const k = 'r' + r.id; RWI.set(k, { key: k, kind: 'raw', recs: [r] }); rows.push(rawLine(r, q, null, chunkOf(r, g))); });
      } else {
        const ck = chunkOf(f, g);
        const key = 'n' + f.id, n = g.recs.length;
        // B-21: the targets of the folded lines (host:port counts), once the bridge names them
        const hosts = {};
        if (b21) g.recs.forEach(r => { const h = targetOf(r); if (h) hosts[h] = (hosts[h] || 0) + 1; });
        const sum = Object.entries(hosts).sort((a, b) => b[1] - a[1]).map(([h, c]) => `${h} ×${c}`).join(', ');
        Object.assign(g, { key, kind: 'noise', sum, hosts: Object.keys(hosts).length });
        RWI.set(key, g);
        const lv = g.level, dim = RANK[lv] <= 1;
        rows.push({ key, ck, sig: `N|${f.id}|${n}|${lv}|${sum}|${L}`, make: () => {
          const el = mk('div', 'logs-rrow fold' + (dim ? ' is-dim' : ''), { role: 'row', 'aria-selected': 'false', tabindex: '-1', 'data-key': key });
          const msg = mk('span', 'logs-c logs-msg', { role: 'gridcell' });
          const b = mk('button', 'logs-fold logs-tfold', { type: 'button', tabindex: '-1', 'data-fold': key, 'aria-expanded': 'false', 'aria-label': t('logs.raw.noise.aria', { kind: noiseWord(g.noise), n }), 'data-press': true });
          b.append(icon('chevron', 12, 'fold-chev'), txt('span', '', noiseWord(g.noise)), txt('span', 'badge', '×' + n));
          msg.append(b);
          if (g.hosts) {
            // the folded targets are data (translate="no"); the count is ours
            const s = mk('span', 'logs-fsum');
            s.append(txt('span', 'logs-fsum-n', t('logs.raw.noise.targets', { n: g.hosts })), ' · ', txt('span', '', sum, { translate: 'no' }));
            msg.append(s);
          }
          const lvc = mk('span', 'logs-c', { role: 'gridcell' }); lvc.append(sevEl(lv));
          el.append(txt('span', 'logs-c logs-ctime', hms(f.ts), { role: 'gridcell' }), lvc, txt('span', 'logs-c logs-src', srcName(f.src), { role: 'gridcell' }), msg);
          return el;
        }, apply: el => { const b = el.querySelector('.logs-fold'); if (b) b.setAttribute('aria-expanded', String(V.open.has(key))); } });
        g.recs.forEach(r => { const k = 'r' + r.id; RWI.set(k, { key: k, kind: 'raw', recs: [r], parent: key }); const row = rawLine(r, q, key, ck); row.apply = el => { el.hidden = !V.open.has(key); }; rows.push(row); });
      }
      g.k0 = rows[at].key;
    }
  }
  function buildRaw() {
    RWI.clear();
    const vis = B.rows.filter(r => pass(r, 'raw'));
    const groups = rawGroup(vis, [], V.q), perDay = countDays(vis), rows = [];
    if (showOrigin('raw')) rows.push(originRow(true));
    rawRows(groups, { day: null, run: null, perDay }, rows);
    const total = B.rows.filter(r => inView(r, 'raw') && r.ord > V.wm.raw && !held(r)).length;
    const shownN = vis.filter(isRec).length;
    BS.raw = { sig: buildSig('raw'), ord: B.ord, groups, perDay, visible: shownN, total };
    return { rows, visible: shownN, total };
  }

  /* ---- the live tail: only the last group can change (it may grow ×N or start a new group,
     day or run), so everything before it keeps its rows. Valid while nothing else changed:
     the same filters, no trim, no run state change (buildSig). ---- */
  function tailBuild(view, box) {
    const bs = BS[view];
    if (!bs || bs.sig !== buildSig(view) || !bs.groups.length) return null;
    const groups = bs.groups, lastG = groups[groups.length - 1];
    if (!lastG.k0 || !box.querySelector(`[data-key="${lastG.k0}"]`)) return null;
    const recs = B.rows;
    let i = recs.length; while (i > 0 && recs[i - 1].ord > bs.ord) i--;
    const add = recs.slice(i);
    bs.ord = B.ord;
    bs.total += add.filter(r => inView(r, view) && r.ord > V.wm[view] && !held(r)).length;
    const vis = add.filter(r => pass(r, view));
    if (!vis.length) return { rows: null, visible: bs.visible, total: bs.total };
    bs.visible += vis.filter(isRec).length;
    const days = new Map();
    vis.forEach(r => { const k = dayKey(r.ts); if (isRec(r)) bs.perDay[k] = (bs.perDay[k] || 0) + 1; days.set(k, r.ts); });
    groups.pop();
    const prev = groups[groups.length - 1], st = { day: prev ? prev.dk : null, run: prev ? prev.run : null, perDay: bs.perDay };
    const tail = view === 'events' ? evGroup(lastG.recs.concat(vis), []) : rawGroup(lastG.recs.concat(vis), [], V.q);
    groups.push(...tail);
    const rows = [];
    (view === 'events' ? evRows : rawRows)(tail, st, rows);
    // a day label above the tail keeps its element; only its count changes
    const inTail = new Set(rows.map(r => r.key));
    const days2 = [...days].filter(([k]) => !inTail.has('d' + k)).map(([k, ts]) => dayRow(ts, bs.perDay[k], view === 'raw'));
    return { rows, from: lastG.k0, days: days2, visible: bs.visible, total: bs.total };
  }

  /* ---- keyed patch: unchanged rows keep their element; a changed row keeps its element too
     (focus, selection, a running glow) and takes the children and attributes of a freshly built
     one: node-level, nothing parsed from a string ---- */
  const KEEP = new Set(['class', 'aria-selected', 'tabindex', 'style']);   // state owned by listNav / pushDays
  const HOT = ['is-new', 'logs-in', 'logs-mv', 'is-picked'];
  function morphEl(el, row) {
    const n = takePre(row) || row.make();
    const hot = HOT.filter(c => el.classList.contains(c));
    el.className = n.className; hot.forEach(c => el.classList.add(c));
    Array.from(el.attributes).forEach(a => { if (!KEEP.has(a.name) && !n.hasAttribute(a.name)) el.removeAttribute(a.name); });
    Array.from(n.attributes).forEach(a => { if (!KEEP.has(a.name) && el.getAttribute(a.name) !== a.value) el.setAttribute(a.name, a.value); });
    el.replaceChildren(...n.childNodes); el._sig = row.sig;
  }
  const RAW_H = 26;                                   // a 原始日志 row (one line): a chunk's height is known without layout
  function sizeChunk(ch) {
    let n = 0; for (let x = ch.firstElementChild; x; x = x.nextElementSibling) if (!x.hidden) n++;
    const v = n * RAW_H + 'px'; if (ch.style.containIntrinsicBlockSize !== v) ch.style.containIntrinsicBlockSize = v;
  }
  /** patches box to rows; returns the new elements. Rows with a chunk key (ck) live in chunk
      containers, the rest (labels, 关键事件 rows) are children of box. With o.from (a row or label
      key) only the tail from that row on is managed: everything before it keeps its elements. */
  function patch(box, rows, o) {
    o = o || {};
    const fresh = [];
    const mutate = () => {
      const start = o.from ? box.querySelector(`[data-key="${o.from}"]`) : null;
      const sChunk = start && start.parentElement !== box ? start.parentElement : null;
      const bStart = sChunk || start;
      const bPrev = bStart ? bStart.previousElementSibling : null, rPrev = sChunk ? start.previousElementSibling : null;
      const oldBox = new Map(), oldRows = new Map();
      for (let el = bStart || box.firstElementChild; el; el = el.nextElementSibling) {
        if (el._ghost) continue;
        if (el.dataset.chunk) { oldBox.set('c:' + el.dataset.chunk, el); for (let r = el === sChunk ? start : el.firstElementChild; r; r = r.nextElementSibling) if (r.dataset.key) oldRows.set(r.dataset.key, r); }
        else if (el.dataset.key) oldBox.set(el.dataset.key, el);
      }
      // the plan: box-level entries (labels, unchunked rows, chunks holding their rows)
      const plan = [], wantR = new Set();
      rows.forEach(r => {
        if (!r.ck) { plan.push(r); return; }
        wantR.add(r.key);
        const e = plan[plan.length - 1];
        if (e && e.chunk === r.ck) e.rows.push(r); else plan.push({ key: 'c:' + r.ck, chunk: r.ck, rows: [r] });
      });
      const wantB = new Set(plan.map(e => e.key));
      oldRows.forEach((el, k) => { if (!wantR.has(k)) { el.remove(); oldRows.delete(k); stashRow(el); } });
      oldBox.forEach((el, k) => { if (!wantB.has(k) && el !== sChunk) { el.remove(); oldBox.delete(k); stashRow(el); } });
      // every new row is built as DOM (a cloned skeleton, text set as text) before anything is
      // placed; runs of new elements go into the list as one DocumentFragment
      const need = rows.filter(r => !(r.ck ? oldRows : oldBox).has(r.key));
      for (const r of need) { const el = takePre(r) || r.make(); el._sig = r.sig; el._fresh = true; (r.ck ? oldRows : oldBox).set(r.key, el); fresh.push(el); }
      const skip = c => { while (c && (c._ghost || !(c.dataset.key || c.dataset.chunk))) c = c.nextElementSibling; return c; };
      let bc = skip(bPrev ? bPrev.nextElementSibling : box.firstElementChild);
      const frag = document.createDocumentFragment();
      const flush = () => { if (frag.firstChild) box.insertBefore(frag, bc); };
      plan.forEach(e => {
        let el = oldBox.get(e.key);
        if (e.chunk && !el) { el = document.createElement('div'); el.className = 'logs-chunk'; el.dataset.chunk = e.chunk; el.setAttribute('role', 'presentation'); el._fresh = true; }
        else if (!e.chunk && el._sig !== e.sig) morphEl(el, e);
        if (el._fresh) { el._fresh = false; frag.append(el); }
        else if (el === bc) { flush(); bc = skip(bc.nextElementSibling); }
        else { flush(); box.insertBefore(el, bc); }
        if (!e.chunk) { if (e.apply) e.apply(el, !!o.animate); return; }
        let rc = el === sChunk ? (rPrev ? rPrev.nextElementSibling : el.firstElementChild) : el.firstElementChild;
        e.rows.forEach(r => {
          const re = oldRows.get(r.key);
          if (re._fresh) re._fresh = false;
          else if (re._sig !== r.sig) morphEl(re, r);
          if (re === rc) rc = rc.nextElementSibling; else el.insertBefore(re, rc);
          if (r.apply) r.apply(re, !!o.animate);
        });
        sizeChunk(el);
      });
      flush();
      if (o.after) o.after();
    };
    if (o.animate && visible && !M.reduced && !o.live) viewMotion(box, mutate);
    else mutate();
    return fresh;
  }
  /* ---- rows built ahead, in slices (T-P4-09; P4 architecture §10, mapping_logs R10) ----
     A rebuild of thousands of rows (the 4,000-line seed, a first visit with a full buffer, the view
     that is not shown) would build every row in one task (90–200 ms). ahead(view, then) builds the
     rows the next render of that view needs and the list does not hold (or holds with another
     sig), SLICE_MS of work per task, keeps them by key (PRE: elements never in the list yet), and
     calls then() once they are all built: that render's patch() and morphEl() take them (same key,
     same sig) instead of building them, so it only places them. The view shows what it showed
     until then (the loading skeleton on a first visit). A live batch meanwhile only marks the view
     dirty (the render that ends the job takes it in); a render of the view itself, a generation
     change, a language change and leaving the page end the job (PRE is dropped on the last
     three). Small rebuilds (< PRE_MIN rows to build) stay synchronous.
     A job of the shown view carries fin: what its caller does after the render besides placing
     the rows (the other view's idle rebuild, the open reader). A render of the view that ends the job
     before then() (a filter, a search meanwhile) runs fin after itself; a newer job of the same
     view takes it over (T-P4-09 fix 1, review S4). Every job has its own token: a slice message
     posted for an ended job drives nothing (review S7). */
  const PRE = new Map(), PRE_MIN = 250, PRE_CAP = 9000, SLICE_MS = 8;
  let pre = null, preTok = 0;                          // { view, list, i, then, fin, idle, h, tok }
  const takePre = r => { const el = PRE.get(r.key); if (!el) return null; PRE.delete(r.key); return el._sig === r.sig ? el : null; };
  /** PRE above n entries: the oldest go first (a stashed row long out of the list, not the rows a
      running job just built) */
  function trimPre(n) { for (const k of PRE.keys()) { if (PRE.size <= n) break; PRE.delete(k); } }
  /* the records still in the buffer (by B.version, which every trim bumps): a row whose record
     the buffer trimmed never comes back and is not stashed (review S3 / S7) */
  let liveV = -1, liveIds = null;
  const isLiveRec = id => { if (liveV !== B.version || !liveIds) { liveV = B.version; liveIds = new Set(B.rows.map(r => r.id)); } return liveIds.has(id); };
  /* a row a filter or a search took out of the list goes to PRE too: the filter undone, the search
     cleared, it comes back as it was (same key and sig: the same record in the same wording), with
     the state the list gave it reset (selection, tab stop, motion and pick marks). Rows keyed by a
     record (e/k/r/n + its id) whose record left the buffer are dropped instead. */
  function stashRow(el) {
    const key = el.dataset.key;
    if (!el._sig || !key || el.dataset.chunk || isLab(el)) return;
    if (/^[ekrn]x\d+$/.test(key) && !isLiveRec(key.slice(1))) return;
    el.classList.remove('is-new', 'logs-in', 'logs-mv', 'is-picked');
    if (el.getAttribute('aria-selected') === 'true' || el.tabIndex === 0) {
      el.setAttribute('aria-selected', 'false');
      el.tabIndex = -1;
      el.querySelectorAll('button').forEach(b => { b.tabIndex = -1; });
    }
    if (PRE.size >= PRE_CAP) trimPre(PRE_CAP - 1);
    PRE.delete(key);                                   // (re-inserted: the newest)
    PRE.set(key, el);
  }
  const preChan = new MessageChannel();
  preChan.port1.onmessage = e => step(e.data);
  function sigsIn(box) {
    const m = new Map();
    for (let el = box.firstElementChild; el; el = el.nextElementSibling) {
      if (el._ghost) continue;
      if (el.dataset.chunk) { for (let x = el.firstElementChild; x; x = x.nextElementSibling) if (x.dataset.key) m.set(x.dataset.key, x._sig); }
      else if (el.dataset.key) m.set(el.dataset.key, el._sig);
    }
    return m;
  }
  function endPre(drop) {
    if (pre && pre.idle && pre.h) cic(pre.h);
    pre = null;
    if (drop) PRE.clear();
  }
  /** → true when the rebuild goes in slices (then() follows, then fin), false when the caller
      renders now (and runs its own fin). fin: a Set of functions (see above). */
  function ahead(view, then, idle, fin) {
    // a newer rebuild: built rows stay in PRE; a replaced job of this view hands its fin over
    const carried = pre && pre.view === view && pre.fin ? pre.fin : null;
    if (pre) endPre(false);
    // a planning build: BS[view] keeps describing what the list holds, so the render that follows
    // (then(), or the caller's at once) rebuilds instead of finding no tail to patch (fix 2, N1)
    const bs0 = BS[view];
    const res = view === 'events' ? buildEvents() : buildRaw();
    BS[view] = bs0;
    const dom = sigsIn(boxOf(view));
    const list = res.rows.filter(r => dom.get(r.key) !== r.sig && !(PRE.has(r.key) && PRE.get(r.key)._sig === r.sig));
    if (list.length < PRE_MIN) {
      // the caller renders now; what the replaced job still owed runs right after that render
      if (carried) queueMicrotask(() => carried.forEach(f => { if (!fin || !fin.has(f)) f(); }));
      return false;
    }
    if (PRE.size + list.length > PRE_CAP) trimPre(Math.max(0, PRE_CAP - list.length));
    const all = new Set([...(carried || []), ...(fin || [])]);
    pre = { view, list, i: 0, then, fin: all.size ? all : null, idle: !!idle, h: 0, tok: ++preTok };
    next();
    return true;
  }
  function next() {
    if (!pre) return;
    const tok = pre.tok;
    if (pre.idle) pre.h = ric(() => step(tok)); else preChan.port2.postMessage(tok);
  }
  function step(tok) {
    const job = pre; if (!job || job.tok !== tok) return;
    job.h = 0;
    const t0 = performance.now();
    while (job.i < job.list.length && performance.now() - t0 < SLICE_MS) {
      const r = job.list[job.i++];
      const had = PRE.get(r.key);
      if (had && had._sig === r.sig) continue;
      const el = r.make(); el._sig = r.sig; PRE.set(r.key, el);
    }
    if (job.i < job.list.length) { next(); return; }
    pre = null;
    job.then();
    if (job.fin) job.fin.forEach(f => f());
  }
  /** the last row of box (descending into chunks) */
  function lastRow(box) {
    for (let el = box.lastElementChild; el; el = el.previousElementSibling) {
      if (el._ghost || el.hidden) continue;
      if (el.dataset.chunk) { for (let x = el.lastElementChild; x; x = x.previousElementSibling) if (!x.hidden && x.dataset.key) return x; continue; }
      if (el.dataset.key && !isLab(el)) return el;
    }
    return null;
  }

  /** the rows and labels inside the scroller's viewport: chunks by rect, box-level rows by offset
      (binary search), so a filter never reads a rect per row of the log */
  function screenKids(box, sc, s) {
    const flow = [], out = [], near = r => r.bottom > s.top + 1 && r.top < s.bottom - 1;
    for (let el = box.firstElementChild; el; el = el.nextElementSibling) {
      if (el._ghost || el.hidden) continue;
      if (el.dataset.chunk) { if (near(el.getBoundingClientRect())) for (let x = el.firstElementChild; x; x = x.nextElementSibling) { if (!x.hidden && x.dataset.key) out.push(x); } }
      else if (!el.dataset.key) continue;
      else if (isLab(el)) { if (near(el.getBoundingClientRect())) out.push(el); }
      else flow.push(el);
    }
    if (flow.length) {
      const base = box.offsetTop, y0 = sc.scrollTop, y1 = y0 + sc.clientHeight;
      let lo = 0, hi = flow.length;
      while (lo < hi) { const m = (lo + hi) >> 1, e = flow[m]; if (base + e.offsetTop + e.offsetHeight <= y0) lo = m + 1; else hi = m; }
      for (let i = lo; i < flow.length && base + flow[i].offsetTop < y1; i++) out.push(flow[i]);
    }
    return out;
  }
  /** rows that leave are cloned into this layer, the first thing in the list: it paints under the
      rows and chunks that follow it, so arriving rows cover the ghosts they replace */
  function ghostLayer(box) {
    let gl = box.querySelector(':scope > .logs-ghosts');
    if (!gl) { gl = T.h('div', { class: 'logs-ghosts', 'aria-hidden': 'true' }); const hl = box.querySelector(':scope > .list-hl'); if (hl) hl.after(gl); else box.prepend(gl); }
    return gl;
  }
  /* a filter in a long scroller. Only what is on screen moves, and nothing goes blank:
     - rows that arrive on screen fade in at their place from the first frame (≤ 4 ms apart, 160 ms),
       on an opaque ground, so the old row underneath is covered as the new one comes in;
     - rows that leave fade where they were (160 ms, ease-in; labels 100 ms), under everything;
     - rows that stay on screen glide to their new place above the rest; a selected one takes its
       highlight along. A row that would cross more than half the list re-enters in place.
     - labels (day, run, history) never slide across rows: they move at most one row, otherwise
       they appear at their final place (day labels are placed from the final layout first). */
  function viewMotion(box, mutate) {
    const sc = box.closest('.logs-scroll'), s = sc.getBoundingClientRect();
    const top = s.top + (box === R.rows ? R.gh.offsetHeight : 0);         // under the sticky header
    const on = r => r.height > 0 && r.bottom > top + 1 && r.top < s.bottom - 1;
    const before = new Map();
    // a copy of what is on screen is taken now: a label that stays in the DOM changes its text in the patch
    screenKids(box, sc, s).forEach(el => { const r = el.getBoundingClientRect(); if (on(r)) before.set(el.dataset.key, { el, r, copy: el.cloneNode(true) }); });
    mutate();
    const sp = M.SP.soft, far = s.height / 2, rowH = box === R.rows ? RAW_H : 30;
    const stay = new Set(), moves = [], enters = [];
    screenKids(box, sc, s).forEach(el => {
      const r = el.getBoundingClientRect(); if (!on(r)) return;
      const k = el.dataset.key, b = before.get(k), lab = isLab(el), dy = b ? b.r.top - r.top : 0;
      // a row in a chunk is clipped to it (paint containment): it glides only within its chunk
      const ch = el.parentElement !== box && el.parentElement.getBoundingClientRect(), inside = !ch || (b && b.r.top >= ch.top - 0.5 && b.r.bottom <= ch.bottom + 0.5);
      if (b && inside && Math.abs(dy) <= (lab ? rowH + 2 : far)) { stay.add(k); if (Math.abs(dy) > 0.5) moves.push([el, dy, lab]); }
      else enters.push([el, r.top]);
    });
    const gl = before.size > stay.size ? ghostLayer(box) : null, g0 = gl && gl.getBoundingClientRect();
    before.forEach((b, k) => {
      if (stay.has(k)) return;
      const g = b.copy, lab = isLab(b.el); g._ghost = true; g.hidden = false;
      g.removeAttribute('data-key'); g.removeAttribute('tabindex'); g.setAttribute('aria-hidden', 'true');
      g.classList.remove('is-new', 'logs-in', 'logs-mv');
      Object.assign(g.style, { position: 'absolute', margin: '0', zIndex: lab ? '1' : 'auto', left: (b.r.left - g0.left) + 'px', top: (b.r.top - g0.top) + 'px', width: b.r.width + 'px', height: b.r.height + 'px', translate: 'none' });
      gl.append(g);
      // a label that leaves goes quicker than a row: it must never sit over the rows that arrive
      M.onDone(g.animate([{ opacity: 1 }, { opacity: 0 }], { duration: lab ? 100 : 160, easing: M.ease.in, fill: 'forwards' }), () => g.remove(), 400);
    });
    enters.sort((a, b) => a[1] - b[1]).forEach(([el], i) => {
      el.classList.add('logs-in');
      M.onDone(el.animate([{ opacity: 0, transform: 'translateY(4px)' }, { opacity: 1, transform: 'none' }], { duration: 160, delay: Math.min(i, 6) * 4, easing: M.ease.out, fill: 'backwards' }), () => el.classList.remove('logs-in'), 700);
    });
    const hl = box.querySelector(':scope > .list-hl.on');
    moves.forEach(([el, dy, lab]) => {
      if (!lab) el.classList.add('logs-mv');
      M.onDone(el.animate([{ transform: `translateY(${dy}px)` }, { transform: 'none' }], { duration: sp.duration, easing: sp.easing }), () => el.classList.remove('logs-mv'), sp.duration + 400);
      if (hl && el.getAttribute('aria-selected') === 'true') hl.animate([{ transform: `translateY(${dy}px)` }, { transform: 'none' }], { duration: sp.duration, easing: sp.easing, composite: 'add' });
    });
  }
  /* live tail: the rows in view glide up by what arrived (dy) on one spring, the new rows ride
     in from below the edge with them. Only rows in view are touched: a live record costs a
     handful of animations, not one per row in the log. */
  function glideTail(box, sc, dy, fresh) {
    const s = sc.getBoundingClientRect(), top = s.top - 2, isNew = new Set(fresh);
    const sp = M.SP.soft, move = [{ transform: `translateY(${dy}px)` }, { transform: 'none' }];
    const els = [];                                   // read every rect first, then write (no layout thrash)
    const take = el => { const r = el.getBoundingClientRect(); if (!r.height) return true; if (r.bottom < top) return false; els.push(el); return true; };
    // a chunk moves as one piece (its rows are clipped to it); the rows that arrived in it fade in
    for (let el = box.lastElementChild; el; el = el.previousElementSibling) {
      if (el.hidden || el._ghost || el.classList.contains('logs-day') || !(el.dataset.key || el.dataset.chunk)) continue;
      if (!take(el)) break;
    }
    // the selection highlight is placed by offset (unchanged): it travels with its row
    const hl = box.querySelector(':scope > .list-hl.on'), hr = hl && hl.getBoundingClientRect();
    els.forEach(el => {
      if (isNew.has(el)) el.animate([{ opacity: 0, transform: `translateY(${dy}px)` }, { opacity: 1, offset: 0.55 }, { opacity: 1, transform: 'none' }], { duration: sp.duration, easing: sp.easing, fill: 'backwards' });
      else el.animate(move, { duration: sp.duration, easing: sp.easing });
      if (el.dataset.chunk) fresh.forEach(x => { if (x.parentElement === el && !x.hidden) x.animate([{ opacity: 0 }, { opacity: 1, offset: 0.55 }, { opacity: 1 }], { duration: sp.duration, easing: sp.easing, fill: 'backwards' }); });
    });
    if (hl && hr.bottom >= top && hr.top <= s.bottom) hl.animate(move, { duration: sp.duration, easing: sp.easing, composite: 'add' });
  }
  /* a one-shot glow on a row that just arrived or was located (the library's .is-new: at rest the
     glow is transparent, so a finished glow leaves no animation behind even on a row that scrolled
     into a skipped chunk before its class came off) */
  function glow(el, fresh) {
    if (M.reduced || !el) return;
    if (!fresh) { el.classList.remove('is-new'); void el.offsetWidth; }    // restart it on a row already there
    el.classList.add('is-new');
    clearTimeout(el._glowT); el._glowT = setTimeout(() => el.classList.remove('is-new'), 1700);
  }

  /* ================================================================ markup */
  let R = {}, visible = false, rendered = false, navE = null, navR = null, foldE = null, foldR = null, searchApi = null, viewSeg = null, levelSeg = null;
  /* static wording: data-t="<id>" (the element's text) and data-ta="<attr>:<id>;…" (attributes) are
     registered with T.ti right after the markup is parsed, so a language switch relabels them in
     place (P4 architecture §3.7 item 2) */
  function labelStatic(root) {
    root.querySelectorAll('[data-t]').forEach(el => { const id = el.getAttribute('data-t'); el.removeAttribute('data-t'); T.ti(el, id); });
    root.querySelectorAll('[data-ta]').forEach(el => {
      const spec = el.getAttribute('data-ta'); el.removeAttribute('data-ta');
      spec.split(';').forEach(p => { const at = p.indexOf(':'); if (at > 0) T.ti(el, p.slice(at + 1), null, { attr: p.slice(0, at) }); });
    });
  }
  function build(root) {
    root.replaceChildren(T.html`
<div class="logs">
  <div class="toolbar logs-bar" data-enter>
    <span class="seg" id="logs-view" data-ta="aria-label:logs.bar.view.aria"><button class="seg-btn" data-v="events" data-t="logs.view.events"></button><button class="seg-btn" data-v="raw" data-t="logs.view.raw"></button></span>
    <label class="search sm logs-search" id="logs-search"><input data-ta="placeholder:logs.bar.search;aria-label:logs.bar.search"></label>
    <span class="seg" id="logs-level" data-ta="aria-label:logs.bar.level.aria"><button class="seg-btn" data-v="all" data-t="logs.bar.level.all"></button><button class="seg-btn" data-v="issue" data-tip-pos="bottom" data-t="logs.bar.level.issue" data-ta="data-tip:logs.bar.level.issue_tip"></button><button class="seg-btn" data-v="error" data-t="logs.bar.level.error"></button></span>
    <button class="btn btn-sm logs-filter" id="logs-filter" type="button" aria-haspopup="dialog" aria-expanded="false"><span class="bi">${I('filter', 15)}</span><span class="lab" data-t="logs.bar.filter"></span><span class="badge logs-fbadge" id="logs-fbadge" hidden></span></button>
    <span class="spacer"></span>
    <button class="btn btn-sm btn-ghost logs-follow" id="logs-follow" type="button" data-tip-pos="bottom"><span class="bi logs-fl"><span class="logs-fl-a"></span><span class="logs-fl-b"></span></span><span class="lab"></span></button>
    <button class="btn btn-sm btn-icon" id="logs-more" type="button" data-ta="aria-label:logs.bar.more.aria;data-tip:logs.bar.more.tip" data-tip-pos="bottom" aria-haspopup="menu">${I('more', 16)}</button>
  </div>

  <section class="logs-panel" data-enter data-ta="aria-label:logs.panel.aria">
    <div class="logs-run" id="logs-run">
      <span class="logs-run-t" id="logs-run-t"></span>
      <span class="logs-run-m" id="logs-run-m"></span>
      <span class="chip sm logs-stale" id="logs-stale" hidden data-t="logs.run.stale"></span>
      <span class="toolbar-sep" id="logs-run-sep"></span>
      <span class="logs-cnts" id="logs-cnts">
        <button class="logs-cnt" id="logs-cnt-e" type="button" data-lv="error" data-press><span class="sev sev-error"><b class="logs-cn"></b> </span></button>
        <button class="logs-cnt" id="logs-cnt-w" type="button" data-lv="issue" data-press><span class="sev sev-warn"><b class="logs-cn"></b> </span></button>
        <span class="sev sev-ok logs-cnt-ok" id="logs-cnt-ok" data-t="logs.run.count.none"></span>
      </span>
    </div>
    <div class="logs-body" id="logs-body">
      <div class="logs-lvprobe" id="logs-lvprobe" aria-hidden="true"><div><span data-t="logs.level.debug"></span><span data-t="logs.level.info"></span><span data-t="logs.level.ok"></span><span data-t="logs.level.warn"></span><span data-t="logs.level.error"></span><span data-t="logs.level.critical"></span></div><div><span data-t="logs.raw.col.level"></span></div><div><span data-t="logs.source.tesseract"></span><span data-t="logs.source.mihomo"></span><span data-t="logs.source.alert"></span><span data-t="logs.source.unknown"></span><span data-t="logs.raw.col.source"></span></div></div>
      <div class="logs-scroll" id="logs-sc-events"><div class="list dense logs-events" id="logs-events" data-ta="aria-label:logs.view.events"></div></div>
      <div class="logs-scroll" id="logs-sc-raw" hidden>
        <div class="logs-grid" role="grid" data-ta="aria-label:logs.view.raw" aria-readonly="true">
          <div class="logs-gh" role="row"><span role="columnheader" data-t="logs.raw.col.time"></span><span role="columnheader" data-t="logs.raw.col.level"></span><span role="columnheader" data-t="logs.raw.col.source"></span><span role="columnheader" data-t="logs.raw.col.message"></span></div>
          <div class="logs-gb" id="logs-rows" role="rowgroup"></div>
        </div>
      </div>
      <div class="logs-skel" id="logs-skel" hidden aria-hidden="true"></div>
      <div class="logs-empty" id="logs-empty" hidden></div>
      <button class="logs-pill is-away" id="logs-pill" type="button" tabindex="-1" aria-hidden="true" data-press><b class="logs-pill-n"></b><span class="logs-pill-w"></span><span class="logs-pill-cut"></span><span class="logs-pill-go" data-t="logs.pill.resume"></span>${I('arrow-down', 14)}</button>
    </div>
    <footer class="logs-foot" id="logs-foot">
      <span class="logs-feed" id="logs-feed-t"></span>
      <span class="logs-feed" id="logs-feed-m"></span>
      <span class="logs-foot-sep"></span>
      <span class="logs-upd" id="logs-upd-l"></span>
      <span class="logs-foot-sep"></span>
      <span class="logs-total tnum" id="logs-total"></span>
      <span class="chip sm logs-debug" id="logs-debug" hidden data-t="logs.footer.debug"></span>
      <span class="chip sm logs-drop" id="logs-drop" hidden></span>
      <span class="spacer"></span>
    </footer>
  </section>
</div>`);
    labelStatic(root);
    const $ = s => root.querySelector(s);
    R = {
      root, bar: $('.logs-bar'), view: $('#logs-view'), search: $('#logs-search'), level: $('#logs-level'), filter: $('#logs-filter'), fbadge: $('#logs-fbadge'),
      follow: $('#logs-follow'), flA: $('.logs-fl-a'), flB: $('.logs-fl-b'), flLab: $('#logs-follow .lab'), more: $('#logs-more'),
      panel: $('.logs-panel'), runT: $('#logs-run-t'), runM: $('#logs-run-m'), stale: $('#logs-stale'), runSep: $('#logs-run-sep'), cntE: $('#logs-cnt-e'), cntW: $('#logs-cnt-w'), cntOk: $('#logs-cnt-ok'),
      body: $('#logs-body'), scE: $('#logs-sc-events'), scR: $('#logs-sc-raw'), events: $('#logs-events'), grid: $('.logs-grid'), gh: $('.logs-gh'), rows: $('#logs-rows'), skel: $('#logs-skel'), empty: $('#logs-empty'), pill: $('#logs-pill'), pillN: $('.logs-pill-n'), pillW: $('.logs-pill-w'), pillCut: $('.logs-pill-cut'),
      feedT: $('#logs-feed-t'), feedM: $('#logs-feed-m'), updL: $('#logs-upd-l'), total: $('#logs-total'), debug: $('#logs-debug'), drop: $('#logs-drop'),
      lvProbe: $('#logs-lvprobe'),
    };
    watchLevelCol();
    // the loading skeleton (L-1): plain rows; their widths are logs.css's (no inline style)
    for (let i = 0; i < 9; i++) { const r = mk('div', 'skel-row'); r.append(mk('span', 'skel skel-dot'), mk('span', 'skel skel-t'), mk('span', 'skel skel-t logs-skel-end')); R.skel.append(r); }
  }
  /* the level column (T-P5-04): as wide as the current language's widest level word (logs.level.*,
     all six) and the column header, so the words are the languages' real words, never cut down to
     fit. The words sit in a hidden probe (.logs-lvprobe, static data-t: a language switch relabels
     it in place); a ResizeObserver reads the probe's widths after layout, at first render, after
     every language switch and when a web font lands. Nothing is read per row or per frame and no
     layout is forced. logs.css turns --logs-lvw (widest word) and --logs-lvh (header) into the
     原始日志 column (62 px, zh_CN as designed, to 108 px) and the 关键事件 column. A hidden page
     measures 0 × 0 and keeps the last widths. */
  function watchLevelCol() {
    const probe = R.lvProbe;
    if (!probe || typeof ResizeObserver !== 'function') return;
    const [words, head, src] = probe.children;
    // (T-P5-13) the 原始日志 source column the same way: its widest source word or header (--logs-srcw)
    const VAR = new Map([[words, '--logs-lvw'], [head, '--logs-lvh'], [src, '--logs-srcw']]), last = new Map();
    const ro = new ResizeObserver(entries => {
      for (const e of entries) {
        // rounded up: a column a fraction of a pixel narrower than the word lets the badge spill
        const w = Math.ceil(e.contentRect.width), k = VAR.get(e.target);
        if (!k || w <= 0 || last.get(e.target) === w) continue;
        last.set(e.target, w);
        R.body.style.setProperty(k, w + 'px');
      }
    });
    VAR.forEach((k, el) => ro.observe(el));
  }
  const scOf = view => (view === 'events' ? R.scE : R.scR);
  const boxOf = view => (view === 'events' ? R.events : R.rows);
  const navOf = view => (view === 'events' ? navE : navR);
  const atBottom = sc => sc.scrollHeight - sc.scrollTop - sc.clientHeight < 6;
  /* keyboard entry at the tail: with nothing selected, Tab lands on the newest row (the library's
     roving stop is the first row, and focusing it would scroll a log back to its oldest line).
     Buttons inside rows (tier links, ×N) are tab stops only in the selected row, so one Tab
     leaves the list instead of walking every link in the log. Rows are emitted with
     tabindex="-1", so this touches two or three elements, never every row. */
  const tabAt = { events: null, raw: null }, btnAt = { events: null, raw: null };
  function tabStops(view) {
    const nav = navOf(view); if (!nav) return;
    const box = boxOf(view), cur = nav.get(), sel = cur && cur.isConnected && !cur.hidden ? cur : null;
    let t = sel;
    if (!t) t = lastRow(box);
    if (tabAt[view] && tabAt[view] !== t) tabAt[view].tabIndex = -1;
    if (t && t.tabIndex !== 0) t.tabIndex = 0;
    tabAt[view] = t;
    if (btnAt[view] && btnAt[view] !== sel) btnAt[view].querySelectorAll('button').forEach(b => { b.tabIndex = -1; });
    if (sel) sel.querySelectorAll('button').forEach(b => { b.tabIndex = 0; });
    btnAt[view] = sel;
  }
  /* the library's refresh() walks every row (aria-selected, tab stops, a rect each): call it only
     when the selection is gone or may have moved (a filter), never for a live tail */
  function syncNav(view, mode) {
    const nav = navOf(view); if (!nav) return;
    const c = nav.get();
    if (c && (!c.isConnected || c.hidden || (mode !== 'live' && view === V.view))) {
      nav.refresh();
      if (!nav.get()) boxOf(view).querySelectorAll('.li[tabindex="0"], .logs-rrow[tabindex="0"]').forEach(el => { if (el !== tabAt[view]) el.tabIndex = -1; });
    }
    tabStops(view);
  }

  /* ================================================================ render */
  const last = { events: { visible: 0, total: 0 }, raw: { visible: 0, total: 0 } };
  let pending = 0;
  const stick = { events: true, raw: true };     // the view is parked at the tail (kept there when the scroller shrinks)
  const lastTop = { events: 0, raw: 0 };
  /** mode: 'instant' (no motion) · 'filter' (rows glide / fade) · 'live' (tail follows, new rows rise).
      A build first tries the tail (only what arrived since the last build); full rebuilds it all. */
  function render(view, mode, full) {
    // (T-P4-09) this render is the rebuild itself; what its caller owed runs after it (review S4)
    let fin = null;
    if (pre && pre.view === view) { fin = pre.fin; endPre(false); }
    const box = boxOf(view), sc = scOf(view), shown = view === V.view && visible && !sc.hidden;
    const follow = shown && !V.paused && (stick[view] || atBottom(sc));
    const animate = shown && mode !== 'instant';
    const top0 = shown ? sc.scrollTop : 0;
    const after = () => {
      if (!shown) return;
      if (follow && mode !== 'instant') { sc.scrollTop = sc.scrollHeight; stick[view] = true; }
      pushDays(view);                                // day labels from the final layout, before anything is revealed
    };
    b23 = T.rt.feature('B-23');
    let res = full ? null : tailBuild(view, box), fresh = [];
    if (res) {
      if (res.rows) {
        fresh = patch(box, res.rows, { from: res.from, animate, live: true, after });
        res.days.forEach(d => { const el = box.querySelector(`:scope > [data-key="${d.key}"]`); if (el && el._sig !== d.sig) morphEl(el, d); });
      }
    } else {
      res = view === 'events' ? buildEvents() : buildRaw();
      fresh = patch(box, res.rows, { animate, live: mode === 'live', after });
    }
    const live = mode === 'live' && animate && !M.reduced, dy = live ? Math.max(0, sc.scrollTop - top0) : 0;
    if (mode === 'live' && animate && !follow) {
      const added = res.visible - last[view].visible;
      if (added > 0) { pending += added; syncPill(true); }
    }
    last[view] = res;
    V.dirty[view] = false;
    syncNav(view, mode);
    if (view === V.view) { syncCount(); syncEmpty(mode !== 'instant'); }
    if (live && fresh.length) {
      /* following the tail: everything in view moves up as one block and the new rows come in
         with it (glideTail). Not following: they land out of view and the pill counts them. */
      if (dy) glideTail(box, sc, dy, fresh);
      else fresh.forEach((el, i) => {
        if (el.hidden || el.classList.contains('logs-day')) return;
        M.anim(el, [{ opacity: 0, transform: 'translateY(10px)' }, { opacity: 1, transform: 'none' }], { duration: 260, delay: Math.min(i, 4) * 40, easing: M.ease.out, fill: 'backwards' });
      });
      fresh.forEach(el => { if (!el.hidden && !isLab(el)) glow(el, true); });
    }
    syncReaderAfter(view);
    if (fin) fin.forEach(f => f());
    return res;
  }
  /* the view that is not shown is brought up to date when the page is idle, so a switch only
     unhides it (its tail since the last build, or a full build if the filters changed) */
  let idleH = 0;
  const ric = window.requestIdleCallback ? f => requestIdleCallback(f, { timeout: 1200 }) : f => setTimeout(f, 250);
  const cic = h => (window.cancelIdleCallback ? cancelIdleCallback(h) : clearTimeout(h));
  function idleRender() {
    if (idleH) return;
    idleH = ric(() => {
      idleH = 0;
      const o = V.view === 'events' ? 'raw' : 'events';
      const go = () => { if (visible && rendered && V.dirty[o] && V.view !== o) render(o, 'instant'); };
      // (T-P4-09) a large rebuild of the hidden view is built in idle slices first
      if (visible && rendered && V.dirty[o] && !(pre && pre.view === V.view) && !ahead(o, go, true)) render(o, 'instant');
    });
  }
  function cancelIdle() { if (idleH) { cic(idleH); idleH = 0; } }
  /* (T-P4-09) what a sliced rebuild of the shown view owes after its render (ahead's fin): the
     other view's idle rebuild (held back while the job runs), the open reader's wording */
  const FIN_IDLE = () => idleRender();
  const FIN_READER = () => { if (rd && !rd.closed && !repaintReader(rd._view)) rd.close(); };
  const FIN_ENTER = new Set([FIN_IDLE]), FIN_SEED = new Set([FIN_IDLE, FIN_READER]), FIN_LANG = new Set([FIN_IDLE]);
  /* sticky day labels: the next day's label pushes the stuck one up and out instead of
     sliding over it (two labels half-stacked read as a glitch). Scroll-linked, transform only.
     Layout positions, not rects (right after a filter the rows are still gliding); every read
     happens before any write. */
  function pushDays(view) {
    const sc = scOf(view); if (!sc || sc.hidden) return;
    const box = boxOf(view), heads = [];
    for (let el = box.firstElementChild; el; el = el.nextElementSibling) if (el.classList.contains('logs-day') && !el._ghost) heads.push(el);
    if (!heads.length) return;
    const base = box.offsetTop, line = sc.scrollTop + (view === 'raw' ? base : 0);
    const at = heads.map(h => [base + h.offsetTop, h.offsetHeight]);
    heads.forEach((h, i) => {
      let y = 0;
      if (i + 1 < heads.length) { const d = at[i + 1][0] - line, hh = at[i][1]; if (d < hh) y = Math.max(-hh, Math.min(0, d - hh)); }
      const v = y ? `0 ${Math.round(y)}px` : '';
      if (h.style.translate !== v) h.style.translate = v;
    });
  }
  /* a list pinned to its tail may open with the previous day's label half pushed out at the top (a
     clipped line would be the first thing seen): nudge up just enough to show that label whole. The
     tail stays followed (the nudge is ours, not the user leaving the tail). */
  function settleTop(view) {
    const sc = scOf(view); if (!sc || sc.hidden || !stick[view]) return;
    const box = boxOf(view), heads = [];
    for (let el = box.firstElementChild; el; el = el.nextElementSibling) if (el.classList.contains('logs-day') && !el._ghost && !el.hidden) heads.push(el);
    const base = box.offsetTop, line = sc.scrollTop + (view === 'raw' ? base : 0);
    for (let i = 0; i + 1 < heads.length; i++) {
      const d = base + heads[i + 1].offsetTop - line, hh = heads[i].offsetHeight;
      if (d > 0 && d < hh) { sc.scrollTop -= Math.ceil(hh - d); lastTop[view] = sc.scrollTop; break; }
    }
    pushDays(view);
  }
  function syncCount() {
    const r = last[V.view];
    if (searchApi) searchApi.count(r.visible, r.total);
    const f = filtersActive();
    // one sentence per state; the shown count is bold through the message's <b> tag
    const sig = `${lng()}|${f}|${r.visible}|${r.total}`;
    if (R.total._sig === sig) return;
    R.total._sig = sig;
    R.total.replaceChildren(f ? T.tx('logs.footer.shown', { n: r.visible, total: r.total }) : T.tx('logs.footer.total', { total: r.total }));
  }

  /* ---- empty states: a title and the way out; no sentence on why it is empty (F-01: the user
     knows; a failed seed says the backend's reason when it has one) ---- */
  let emptyKind = null, undo = null;
  function syncEmpty(animate) {
    const r = last[V.view];
    let kind = null;
    // the seed (L-1 loading: a skeleton; L-2 failed: 无法载入日志 + 重试) stands in for an empty list
    const loading = seedState === 'loading' && !r.visible;
    if (R.skel.hidden === loading) R.skel.hidden = !loading;
    if (!r.visible && !loading) kind = seedState === 'failed' ? 'failed' : V.wm[V.view] && !filtersActive() ? 'cleared' : filtersActive() ? 'filtered' : V.paused ? 'paused' : 'none';
    // only a search narrows the view: the way out is 清除搜索 (and the other filters stay)
    const onlyQ = !!V.q && V.level === 'all' && !V.cats.size && !V.srcs.size && !V.runOnly;
    const sig = kind && `${kind}|${V.view}|${kind === 'cleared' && undo && undo.view === V.view ? 'u' : ''}|${V.q}|${onlyQ}|${lng()}`;
    if (sig === emptyKind) return;
    emptyKind = sig;
    if (!kind) {
      if (!R.empty.hidden) {
        if (animate && !M.reduced) { const a = R.empty.animate([{ opacity: 1 }, { opacity: 0 }], { duration: 120, fill: 'forwards' }); M.onDone(a, () => { if (!emptyKind) R.empty.hidden = true; a.cancel(); }, 300); }
        else R.empty.hidden = true;
      }
      return;
    }
    let o;
    if (kind === 'failed') o = { icon: 'warning', title: t('logs.load.failed_title'), text: seedErr ? T.rt.errorText(seedErr) : '', actions: [{ label: t('logs.load.retry'), primary: true, run: () => seed() }] };
    else if (kind === 'filtered') {
      // the other view may hold what you are looking for (raw lines are not key events)
      const other = V.view === 'events' ? 'raw' : 'events', n = B.rows.filter(r => isRec(r) && pass(r, other)).length;
      const acts = [onlyQ ? { label: t('logs.empty.filtered.clear_search'), run: () => searchApi.clear() } : { label: t('logs.empty.filtered.clear_filters'), run: () => resetFilters(true) }];
      if (n) acts.unshift({ label: t('logs.empty.filtered.other_view', { view: other, n }), primary: true, run: () => setView(other) });
      o = { icon: 'search', title: t('logs.empty.filtered.title'), actions: acts };
    }
    // the undo lives in the notice (G43): the empty state only says what happened
    else if (kind === 'cleared') o = { icon: 'eye-off', title: t('logs.empty.cleared.title') };
    else if (kind === 'paused') o = { icon: 'pause', title: t('logs.empty.paused.title'), actions: [{ label: t('common.action.continue'), run: () => setPaused(false) }] };
    else o = V.view === 'events' ? { icon: 'logs', title: t('logs.empty.none.events_title') } : { icon: 'code', title: t('logs.empty.none.raw_title') };
    R.empty.replaceChildren(T.ui.empty(o));
    const was = R.empty.hidden; R.empty.hidden = false;
    R.empty.getAnimations().forEach(a => a.cancel());
    if (animate && was) M.anim(R.empty, [{ opacity: 0, transform: 'translateY(6px)' }, { opacity: 1, transform: 'none' }], { duration: 240, delay: 80, easing: M.ease.out, fill: 'backwards' });
  }

  /* ---- 「N 条新日志 ↓」: shown when new rows land below while you read further up ---- */
  /* while paused it carries the held count and resumes on click: 「12 条新日志 · 继续 ↓」 */
  function syncPill(animate) {
    const n = V.paused ? heldCount() : pending, on = n > 0, away = R.pill.classList.contains('is-away');
    if (on) {
      M.odometer(R.pillN, String(n), { mode: V.paused ? 'forward' : 'auto', label: t('logs.pill.count', { n }), instant: !animate || away });
      // the word after the counter agrees with it (plural); the counter itself is the odometer
      const w = t('logs.pill.word', { n }); if (R.pillW.textContent !== w) R.pillW.textContent = w;
      // L-12: what the 4,000-row buffer pushed out while paused (it will not come back on 继续)
      const cut = V.paused ? heldCut : 0, cw = cut ? t('logs.pill.cut', { n: cut }) : '';
      if (R.pillCut.textContent !== cw) R.pillCut.textContent = cw;
      R.pill.classList.toggle('is-held', V.paused);
      R.pill.setAttribute('aria-label', t(V.paused ? 'logs.pill.aria_held' : 'logs.pill.aria_new', { n }) + (cw ? ' ' + cw : ''));
    }
    R.pill.classList.toggle('is-away', !on);
    R.pill.setAttribute('aria-hidden', String(!on));
    R.pill.tabIndex = on ? 0 : -1;
  }
  function toBottom(smooth) {
    const sc = scOf(V.view);
    pending = 0; syncPill(true);
    sc.scrollTo({ top: sc.scrollHeight, behavior: smooth && !M.reduced ? 'smooth' : 'auto' });
  }

  /* ================================================================ view switching */
  function setView(v, o) {
    o = o || {};
    if (v === V.view && !o.force) return;
    cancelIdle();
    clearPicks();
    const from = V.view, outEl = scOf(from), inEl = scOf(v);
    const rdRecId = rd && rd._it ? rd._it.recs[rd._it.recs.length - 1].id : null;
    V.scroll[from] = atBottom(outEl) ? null : outEl.scrollTop;
    V.view = v;
    if (viewSeg && viewSeg.get() !== v) viewSeg.set(v, true);
    pending = 0; syncPill(false);
    if (V.paused) syncFollow(true);            // the held count is per view
    // the old list goes at once and the new one comes up from half strength (two dense lists
    // crossing at half opacity read as one jumble; a fade-through left a near-blank frame)
    if (from !== v) { outEl.getAnimations().forEach(a => a.cancel()); outEl.hidden = true; }
    inEl.hidden = false;
    if (V.dirty[v]) render(v, 'instant');      // usually built already, while idle (idleRender)
    if (o.select == null) { if (V.scroll[v] == null) inEl.scrollTop = inEl.scrollHeight; else inEl.scrollTop = V.scroll[v]; stick[v] = V.scroll[v] == null; }
    else stick[v] = false;
    syncCount(); syncEmpty(false); pushDays(v);
    if (visible && !M.reduced && from !== v) {
      inEl.getAnimations().forEach(a => a.cancel());
      inEl.animate([{ opacity: 0.5, transform: 'translateY(6px)' }, { opacity: 1, transform: 'none' }], { duration: 180, easing: M.ease.out });
    }
    // the reader stays on the same record when the other view has it
    const target = o.select != null ? o.select : rdRecId;
    if (target != null) {
      const row = rowForRec(v, target, o.select != null);
      if (row) { navOf(v).select(row); if (o.select != null) { row.scrollIntoView({ block: 'center' }); glow(row); } if (rd && !rd.closed) showReader(row, 'api'); }
      else if (rd && !rd.closed) rd.close();
    }
  }
  function rowForRec(view, id, expand) {
    const items = itemsFor(view);
    let key = null;
    items.forEach((it, k) => { if (!key && !it.parent && it.recs.some(r => r.id === id)) key = k; });
    if (!key) return null;
    const it = items.get(key);
    if (view === 'raw' && it.kind === 'noise' && expand) { V.open.add(key); render('raw', 'instant', true); key = 'r' + id; }
    return boxOf(view).querySelector(`[data-key="${key}"]`);
  }

  /* ================================================================ filters */
  function setLevel(lv, animate) { V.level = lv; if (levelSeg && levelSeg.get() !== lv) levelSeg.set(lv, animate !== false); }
  /* 只看本次运行 is one of the filters (its check lives in 筛选's popover; the run strip's counts set it) */
  function setRunOnly(on) {
    V.runOnly = on;
    const c = fltPop && fltPop.el ? fltPop.el.querySelector('[data-runonly]') : null; if (c) c.checked = on;
    syncFilterBadge();
  }
  /** which run the strip and 只看… speak of: the current one while it runs, the last one once stopped */
  const runScope = () => (S.get('run') !== 'stopped' ? 'current' : 'last');
  /* a control answers first (the seg thumb glides, the switch knob moves, both on the compositor);
     the list follows once that frame is out, so the filter work never holds the press back */
  let refA = 0, refB = 0;
  function refilterSoon() {
    if (refA || refB) return;
    refA = requestAnimationFrame(() => { refA = 0; refB = setTimeout(() => { refB = 0; refilter(); }, 0); });
  }
  function cancelRefilter() { if (refA) cancelAnimationFrame(refA); if (refB) clearTimeout(refB); const was = !!(refA || refB); refA = refB = 0; return was; }
  function refilter() {
    cancelRefilter();
    render(V.view, 'filter'); V.dirty[V.view === 'events' ? 'raw' : 'events'] = true; syncFilterBadge();
    if (V.paused) { syncFollow(true); syncPill(true); }
    idleRender();
  }
  function resetFilters(animate) {
    V.q = ''; if (searchApi && searchApi.value) { quiet = true; searchApi.clear(); quiet = false; }
    setLevel('all', animate); V.cats.clear(); V.srcs.clear(); setRunOnly(false);
    refilter();
  }
  let quiet = false;
  function syncFilterBadge() {
    const n = V.cats.size + V.srcs.size + (V.runOnly ? 1 : 0);
    R.fbadge.hidden = !n; if (n) R.fbadge.textContent = String(n);
    R.filter.classList.toggle('is-on', !!n);
    R.filter.setAttribute('aria-label', n ? t('logs.bar.filter_aria', { n }) : t('logs.bar.filter'));
  }
  // open menus and popovers, closed on a language switch (they are rebuilt on the next open)
  let fltPop = null, moreMenu = null, rowMn = null, bundlePop = null;
  function openFilter() {
    if (fltPop) { fltPop.close(); return; }
    const counts = (list, prop) => { const m = {}; B.rows.forEach(r => { if (inView(r, V.view) && r.ord > V.wm[V.view]) m[r[prop]] = (m[r[prop]] || 0) + 1; }); return list.map(x => [x, m[x] || 0]); };
    // a chip with nothing behind it is not offered (unless it is the one already chosen); the
    // chip's value is a v1 id, its text the catalog's word for it
    const chip = (x, n, set, kind) => { const b = txt('button', 'fchip', kind === 'cat' ? catLabel(x) : srcName(x), { type: 'button', 'data-k': kind, 'data-v': x, 'aria-pressed': String(set.has(x)) }); b.append(txt('span', 'n', n)); return b; };
    const chips = (list, prop, set, kind) => { const box = mk('div', 'chips'); counts(list, prop).filter(([x, n]) => n || set.has(x)).forEach(([x, n]) => box.append(chip(x, n, set, kind))); return box; };
    const sec = (...kids) => { const s = mk('div', 'logs-flt-sec'); s.append(...kids); return s; };
    fltPop = T.ui.popover(R.filter, (el, api) => {
      const ro = mk('input', '', { type: 'checkbox', 'data-runonly': true }); ro.checked = V.runOnly;
      const lab = mk('label', 'check'); lab.append(ro, txt('span', 'check-t', t('logs.filter.run_only', { scope: runScope() })));
      const clr = txt('button', 'link-btn', t('logs.filter.clear'), { type: 'button', 'data-clear': true });
      const foot = mk('div', 'logs-flt-foot'); foot.append(clr);
      const box = mk('div', 'logs-flt');
      box.append(sec(lab), sec(txt('div', 'logs-flt-h', t('logs.filter.source')), chips(SRCS, 'src', V.srcs, 'src')), sec(txt('div', 'logs-flt-h', t('logs.filter.category')), chips(CATS, 'cat', V.cats, 'cat')), foot);
      el.append(box);
      const paint = () => { clr.disabled = !(V.cats.size || V.srcs.size || V.runOnly); };
      el.addEventListener('change', e => {
        if (e.target === ro) { V.runOnly = ro.checked; syncFilterBadge(); paint(); refilterSoon(); return; }
        const c = e.target.closest('.fchip'); if (!c) return;
        const set = c.dataset.k === 'src' ? V.srcs : V.cats;
        if (c.getAttribute('aria-pressed') === 'true') set.add(c.dataset.v); else set.delete(c.dataset.v);
        paint(); refilterSoon();
      });
      clr.addEventListener('click', () => { V.cats.clear(); V.srcs.clear(); V.runOnly = false; ro.checked = false; T.$$('.fchip', el).forEach(c => c.setAttribute('aria-pressed', 'false')); paint(); refilterSoon(); });
      paint();
    }, { width: 336, placement: 'bottom-start', onClose: () => { fltPop = null; } });
    fltPop.el.setAttribute('aria-label', t('logs.filter.aria'));
  }

  /* ================================================================ follow / pause */
  function heldCount() { const recs = B.rows; let n = 0; for (let i = recs.length - 1; i >= 0 && recs[i].ord > V.pauseOrd; i--) if (isRec(recs[i]) && pass(recs[i], V.view, { held: true })) n++; return n; }
  let heldCut = 0;                    // records that arrived while paused and were trimmed before 继续 (L-12)
  /* the text says the state (实时跟随 / 已暂停); the held count lives on the pill alone
     (「12 条新日志 · 继续」), so one control carries it. The glyph is neutral (following the tail,
     not a health dot) and turns into what a click does on hover. */
  function syncFollow(animate) {
    const p = V.paused, n = p ? heldCount() : 0;
    R.follow.classList.toggle('is-paused', p);
    const ga = p ? 'pause' : 'arrow-down', gb = p ? 'play' : 'pause';
    if (R.flA._h !== ga) { R.flA._h = ga; R.flA.replaceChildren(icon(ga, 15)); R.flB.replaceChildren(icon(gb, 15)); }
    R.follow.setAttribute('data-tip', t(p ? 'logs.follow.tip_paused' : 'logs.follow.tip_live'));
    R.follow.setAttribute('aria-pressed', String(p));
    R.follow.setAttribute('aria-label', p ? t('logs.follow.aria_paused', { n }) : t('logs.follow.aria_live'));
    const lab = t(p ? 'logs.follow.paused' : 'logs.follow.live');
    if (R.flLab._t !== lab) { R.flLab._t = lab; M.swapText(R.flLab, lab, { instant: !animate }); }
  }
  function setPaused(p) {
    if (p === V.paused) return;
    V.paused = p;
    if (p) V.pauseOrd = B.ord;
    heldCut = 0;
    syncFollow(true);
    if (!p) {
      pending = 0; syncPill(false);
      const sc = scOf(V.view);
      render(V.view, 'filter'); V.dirty[V.view === 'events' ? 'raw' : 'events'] = true; idleRender();
      stick[V.view] = true;
      sc.scrollTo({ top: sc.scrollHeight, behavior: M.reduced ? 'auto' : 'smooth' });
    } else { pending = 0; syncPill(false); syncCount(); syncEmpty(true); }
  }

  /* ================================================================ run summary + status line */
  function runCounts() {
    const key = curRun().key; let e = 0, w = 0;
    if (key) B.rows.forEach(r => { if (r.run === key && isRec(r) && r.ev) { if (RANK[r.level] >= 3) e++; else if (r.level === 'warn') w++; } });
    return { e, w };
  }
  const wideOnly = kids => { const s = document.createElement('span'); s.className = 'wide-only'; s.append(kids); return s; };
  const PHASES = ['collect', 'verify', 'ready'];     // startup phases by index (store `phase`)
  function syncRun(animate) {
    const run = S.get('run'), cr = curRun(), up = S.get('uptime') || 0;
    const scope = runScope();
    const title = t('logs.run.title', { scope });
    if (R.runT._t !== title) { R.runT._t = title; M.swapText(R.runT, title, { instant: !animate }); }
    // the run's start: its first record, else now − uptime (映射_logs #21)
    const start = cr.start != null ? cr.start : Date.now() - up * 1000;
    let m, rich = null;
    if (run === 'running') { rich = ['logs.run.meta.running', { time: hms(start), uptime: F.uptime(up) }]; m = t(...rich); }
    else if (run === 'starting') { const ph = Math.max(0, S.get('phase') || 0); m = t('logs.run.meta.starting', { phase: PHASES[ph] || 'ready', step: ph + 1, total: PHASES.length }); }
    else if (run === 'stopping') m = t('logs.run.meta.stopping', { cancelling: S.get('cancelling') ? 'yes' : 'no' });
    // a failed start says why (service.state.runtime.failure, the same reason as the overview's)
    else if (run === 'failed') { const f = S.get('failure'); m = f ? t('logs.run.meta.failed_reason', { reason: T.rt.errorText(f) }) : t('logs.run.meta.failed'); }
    else if (run === 'unknown') m = t('logs.run.meta.unknown');
    else if (cr.start == null) m = '—';
    else { const end = cr.end != null ? cr.end : cr.last; m = end != null && end > cr.start ? t('logs.run.meta.ended', { start: hms(cr.start), end: hms(end), duration: dur(end - cr.start) }) : t('logs.run.meta.stopped', { start: hms(cr.start) }); }
    if (R.runM._h !== m) {
      R.runM._h = m;
      // the running line has a tag slot (the start time hides in a narrow window): written as
      // nodes; the others roll as text
      if (rich) R.runM.replaceChildren(T.tx(rich[0], rich[1], { x: wideOnly }));
      else M.swapText(R.runM, m, { instant: !animate });
    }
    // L-11: service.state no longer fresh (freshness.stale or the watchdog): the strip says so
    const stale = !!(S.get('stale') || {})['service.state'];
    if (R.stale.hidden === stale) R.stale.hidden = !stale;
    const c = runCounts();
    // no run to speak of: no 没有错误或警告 either (and no separator before an empty slot)
    R.cntE.hidden = !c.e; R.cntW.hidden = !c.w; R.cntOk.hidden = !!(c.e || c.w) || !cr.key;
    if (R.runSep.hidden === !!cr.key) R.runSep.hidden = !cr.key;
    // the scope follows the strip's own word: 本次运行 while it runs, 上次运行 once stopped
    const which = `${scope}|${lng()}`;
    if (R.cntE._w !== which) {
      R.cntE._w = which;
      // 警告 applies the 问题 threshold (warnings and errors): the label says so
      const te = t('logs.run.count.errors_tip', { scope }), tw = t('logs.run.count.warnings_tip', { scope });
      R.cntE.setAttribute('data-tip', te); R.cntW.setAttribute('data-tip', tw);
      R.cntE.setAttribute('aria-label', te); R.cntW.setAttribute('aria-label', tw);
    }
    // the word after each counter agrees with it (plural); it is the text node after the digits
    const word = (b, id, n) => { const w = ' ' + t(id, { n }), tn = b.querySelector('.sev').lastChild; if (tn && tn.nodeType === 3 && tn.nodeValue !== w) tn.nodeValue = w; };
    if (c.e) { word(R.cntE, 'logs.run.count.errors_word', c.e); M.odometer(R.cntE.querySelector('.logs-cn'), String(c.e), { mode: 'auto', label: t('logs.run.count.errors_aria', { n: c.e }), instant: !animate }); }
    if (c.w) { word(R.cntW, 'logs.run.count.warnings_word', c.w); M.odometer(R.cntW.querySelector('.logs-cn'), String(c.w), { mode: 'auto', label: t('logs.run.count.warnings_aria', { n: c.w }), instant: !animate }); }
    // (the tier carrying traffic is not repeated here: the overview and the tiers page say it)
  }
  /* the status line: one feed per source (state dot, the source's name, the state word, and why
     Mihomo's stream is not live when logs.feed says so) */
  function feedInto(el, dot, name, state, off, reason) {
    const sig = `${dot}|${name}|${state}|${off}|${reason || ''}|${lng()}`;
    if (el._sig === sig) return;
    el._sig = sig;
    const kids = [mk('span', 'sdot ' + dot), txt('span', '', name), txt('span', 'logs-feed-s' + (off ? ' is-off' : ''), t('logs.footer.feed.state', { state }))];
    if (reason) kids.push(txt('span', 'logs-feed-r', t('logs.feed_reason.' + reason)));
    el.replaceChildren(...kids);
  }
  const tnum = kids => { const s = document.createElement('span'); s.className = 'tnum'; s.append(kids); return s; };
  const REASONS = new Set(['controller_disabled', 'core_not_running', 'stream_error']);
  let controller = null;               // app.info listeners[kind=controller], "host:port"
  /* the controller's address for the status line's tip (app.info listeners, 映射_logs #52): asked
     once per generation, when the page has been shown and hello says app.info is there */
  let controllerAsked = false;
  function loadController() {
    if (controllerAsked || !rendered || !T.rt || !T.rt.hello || !T.rt.can('app.info')) return;
    controllerAsked = true;
    T.rt.query('app.info', {}).then(i => {
      const c = i && Array.isArray(i.listeners) ? i.listeners.find(x => x && x.kind === 'controller') : null;
      if (c && typeof c.host === 'string' && c.host && Number.isInteger(c.port)) { controller = `${c.host}:${c.port}`; if (visible) syncFoot(); }
    }, () => { controllerAsked = false; });
  }
  function syncFoot() {
    const run = S.get('run'), fd = feed();
    // Tesseract: the runtime connection itself (logs.feed has no Tesseract source, 映射_logs #50)
    const cs = T.rt ? T.rt.state : 'idle';
    if (cs === 'live') feedInto(R.feedT, 'ok', 'Tesseract', 'live', false);
    else if (cs === 'idle') feedInto(R.feedT, 'off', 'Tesseract', 'disconnected', true);
    else feedInto(R.feedT, 'busy', 'Tesseract', 'reconnecting', false);
    // Mihomo: logs.feed.state (L-9: reconnecting / stdout in amber), else the service state (L-8)
    let md, st;
    const fs = fd && fd.state;
    if (fs === 'connected') { md = 'ok'; st = 'live'; }
    else if (fs === 'reconnecting') { md = 'warn'; st = 'reconnecting'; }
    else if (fs === 'stdout') { md = 'warn'; st = 'stdout'; }
    else if (S.get('restarting')) { md = 'busy'; st = 'reconnecting'; }
    else if (run === 'starting') { md = 'busy'; st = S.get('cancelling') ? 'cancelling' : 'connecting'; }
    else if (run === 'stopping') { md = 'busy'; st = S.get('cancelling') ? 'cancelling' : 'stopping'; }
    else if (run === 'running' && !fd) { md = 'ok'; st = 'live'; }
    else { md = 'off'; st = 'off'; }
    // L-10 (T-P4-C2 item 12): the reason, whenever logs.feed carries one and the stream is not live
    const reason = fd && fs !== 'connected' && REASONS.has(fd.reason) ? fd.reason : null;
    feedInto(R.feedM, md, 'Mihomo', st, md === 'off', reason);
    // the tip is a value (the controller's address while the stream is live), not an explanation
    // of the state word (F-01)
    tipOn(R.feedM, st === 'live' && controller ? t('logs.footer.feed.mihomo_tip', { controller }) : '');
    // L-1: while the seed loads, the line says so; afterwards the time of the newest record
    let lr = null; for (let i = B.rows.length - 1; i >= 0 && !lr; i--) if (B.rows[i].kind === 'log') lr = B.rows[i];
    const time = lr ? hms(lr.ts) : '—', us = seedState === 'loading' ? `L|${lng()}` : `${time}|${lng()}`;
    if (R.updL._t !== us) { R.updL._t = us; R.updL.replaceChildren(seedState === 'loading' ? document.createTextNode(t('logs.load.loading')) : T.tx('logs.footer.updated', { time }, { x: tnum })); }
    // DEBUG capture is what logs.feed says (not a local switch: O-4)
    const dbg = !!(fd && fd.level === 'debug');
    if (R.debug.hidden === dbg) R.debug.hidden = !dbg;
    // dropped lines (G46): the producer's counts (logs.feed.dropped) or the batches' running total
    const dc = fd && fd.dropped, total = Math.max(dc && Number.isInteger(dc.total) ? dc.total : 0, B.counts.droppedTotal);
    if (R.drop.hidden === !!total) R.drop.hidden = !total;
    if (total) {
      const w = t('logs.footer.dropped', { n: total });
      if (R.drop.textContent !== w) R.drop.textContent = w;
      // the tip is the per-level breakdown, when logs.feed has one
      tipOn(R.drop, dc ? t('logs.footer.dropped_tip', { debug: dc.debug || 0, info: dc.info || 0, warn: dc.warn || 0, error: dc.error || 0, critical: dc.critical || 0 }) : '');
    }
  }
  /** an element's hover tip: set, or removed when there is nothing to say */
  function tipOn(el, tip) {
    if ((el.getAttribute('data-tip') || '') === tip) return;
    if (tip) el.setAttribute('data-tip', tip); else el.removeAttribute('data-tip');
  }

  /* ================================================================ reader (modeless side drawer) */
  let rd = null;
  /* The reader's size (对等台账 #181: the old viewer's splitter could be dragged and was remembered):
     the docked reader is a side panel, so its splitter is its left edge and the size is its
     width. Dragged with the grip, kept in the WebView's localStorage (per viewer; every read and
     write may throw: the default width is used then). A narrow panel (the web build's fallback
     on a phone or a side panel) gives the reader its whole body. */
  const RW_KEY = 'tesseract.logs.reader.width', RW_MIN = 300;
  let readerPref = null;
  try { const v = Number(localStorage.getItem(RW_KEY)); if (Number.isFinite(v) && v >= RW_MIN) readerPref = Math.round(v); } catch (e) { readerPref = null; }
  const readerMax = () => Math.max(RW_MIN, (R.body ? R.body.clientWidth : innerWidth) - 280);
  const readerW = () => {
    const w = R.body ? R.body.clientWidth : 0;
    if (w && w < 760) return w;
    const def = innerWidth <= 1180 ? 344 : 384;
    return readerPref ? Math.min(readerPref, readerMax()) : def;
  };
  /** the grip on the docked reader's left edge: drag to resize, double click for the default */
  function readerGrip(panel) {
    if (panel.querySelector(':scope > .logs-rd-grip')) return;
    const g = mk('div', 'logs-rd-grip', { role: 'separator', 'aria-orientation': 'vertical', tabindex: '0', 'aria-label': t('logs.reader.resize') });
    T.ti(g, 'logs.reader.resize', null, { attr: 'aria-label' });
    let x0 = 0, w0 = 0;
    const set = w => { readerPref = Math.max(RW_MIN, Math.min(readerMax(), Math.round(w))); if (rd && !rd.closed) { rd.place(); setReaderPad(true); } };
    const save = () => { try { if (readerPref) localStorage.setItem(RW_KEY, String(readerPref)); else localStorage.removeItem(RW_KEY); } catch (e) { /* not kept: the default next time */ } };
    const move = e => set(w0 + (x0 - e.clientX));
    const up = e => { g.releasePointerCapture(e.pointerId); g.removeEventListener('pointermove', move); g.removeEventListener('pointerup', up); g.classList.remove('is-drag'); save(); };
    g.addEventListener('pointerdown', e => { if (e.button !== 0) return; e.preventDefault(); x0 = e.clientX; w0 = panel.getBoundingClientRect().width; g.setPointerCapture(e.pointerId); g.classList.add('is-drag'); g.addEventListener('pointermove', move); g.addEventListener('pointerup', up); });
    g.addEventListener('dblclick', () => { readerPref = null; save(); if (rd && !rd.closed) { rd.place(); setReaderPad(true); } });
    g.addEventListener('keydown', e => { if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return; e.preventDefault(); set(panel.getBoundingClientRect().width + (e.key === 'ArrowLeft' ? 24 : -24)); save(); });
    panel.append(g);
  }
  /* B-22 (the bridge has no advice field in v1): the suggestion card, built only when the reader
     asks inside if (T.rt.feature('B-22')) (P4 rule 14: a B item behind its feature, never behind
     the design build flag; 决定_第八波裁定). What it says comes from the advice source at T.ext 'logs/advice'
     (record → { title, text, label?, go?, params? } | null): the design target registers the
     prototype's cards by event key (design/demo.js, `--h-features all`); the app target registers
     none, so no card shows until the bridge sends advice (the source then reads the record's
     field). Its words are data to the page (translate="no"), every value goes in as text. */
  function adviceFor(r) {
    const src = T.ext.get('logs/advice');
    const fx = typeof src === 'function' ? src(r) : null;
    if (!fx || typeof fx !== 'object') return null;
    const body = mk('div', 'callout-body');
    body.append(txt('b', 'callout-t', String(fx.title == null ? '' : fx.title)), txt('span', '', String(fx.text == null ? '' : fx.text)));
    if (typeof fx.go === 'string' && fx.go) {
      const b = txt('button', 'link-btn logs-fix-go', String(fx.label == null ? '' : fx.label), { type: 'button', 'data-go': fx.go });
      for (const [pk, v] of Object.entries(fx.params || {})) if (/^[a-z][a-z0-9-]*$/.test(pk)) b.setAttribute('data-go-' + pk, String(v));
      b.append(icon('chevron', 14));
      body.append(b);
    }
    const el = mk('div', 'callout callout-info sm logs-fix', { role: 'note', translate: 'no' });
    el.append(icon('info', 16), body);
    return el;
  }
  const kvRow = (id, ...dd) => { const d = mk('div'), v = mk('dd'); v.append(...dd); d.append(txt('dt', '', t(id)), v); return d; };
  /* the reader's title is the record's own headline (data) or one of ours (a folded noise group,
     a raw line); `data` marks which, so the title element gets translate="no" only for data */
  function readerParts(it, view) {
    const n = it.recs.length, f = it.recs[0], l = it.recs[n - 1];
    const noise = it.kind === 'noise';
    const data = !noise && l.ev && !!l.title;
    const title = noise ? t('logs.reader.title.noise', { kind: noiseWord(it.noise), n }) : data ? l.title : t('logs.reader.title.raw', { source: srcName(l.src) });
    // the context line: where from · what kind (the time is the first key below, once)
    const sub = t('logs.reader.sub', { source: srcName(l.src), category: catLabel(l.cat) });
    const rawText = n > 1 ? it.recs.map(r => `${full(r.ts)}  ${rawOf(r)}`).join('\n') : rawOf(l);
    // 复制 JSON: the Log DTO as it came (映射_logs #70), every member of a group
    const json = JSON.stringify(n > 1 ? it.recs.map(dtoOf) : dtoOf(l), null, 2);
    const tiers = tiersIn(`${l.title || ''} ${l.detail || ''}`);
    const fx = !noise && T.rt.feature('B-22') ? adviceFor(l) : null;
    const rows = [kvRow('logs.reader.kv.time', txt('span', 'tnum', full(l.ts), { translate: 'no' }))];
    if (n > 1 && !noise) rows.push(kvRow('logs.reader.kv.repeat', t('logs.reader.kv.repeat_value', { n, same: dayWord(f.ts) === dayWord(l.ts) ? 'yes' : 'no', day: dayWord(f.ts), time: hms(f.ts) })));
    if (noise) {
      rows.push(kvRow('logs.reader.kv.range', txt('span', 'tnum', t('logs.reader.kv.range_value', { n, from: hms(f.ts), to: hms(l.ts) }))));
      // B-21: the targets of the folded lines, once the bridge names them
      if (it.hosts) {
        const hosts = {}; it.recs.forEach(r => { const h = targetOf(r); if (h) hosts[h] = (hosts[h] || 0) + 1; });
        const box = mk('span', 'logs-rd-hosts', { translate: 'no' });
        for (const [h, c] of Object.entries(hosts).sort((a, b) => b[1] - a[1])) { const s = mk('span'); s.append(txt('span', 'tech', h), txt('span', 'logs-rd-hn', '×' + c)); box.append(s); }
        rows.push(kvRow('logs.reader.kv.targets', box));
      }
    }
    if (tiers.length) {
      const tags = [];
      tiers.forEach((k, i) => { if (i) tags.push(mk('span', 'logs-kv-sp')); tags.push(T.html`${T.ui.tierTag(k, { link: true })}`); });
      rows.push(kvRow('logs.reader.kv.tier', ...tags));
    }
    // 诊断信息: when it was received, its sequence number and its event key. The prototype's
    // metadata tree (logger, capture kind, event parameters) is not ported: the v1 Log carries
    // none of it and the old viewer's showed local paths (决定_第五波裁定 T-P2-33 #6, privacy)
    const diag = [kvRow('logs.reader.diag.received', txt('span', 'mono', iso(l.rx), { translate: 'no' })), kvRow('logs.reader.diag.seq', txt('span', 'tnum', l.seq)),
      kvRow('logs.reader.diag.key', l.key !== 'log.raw' ? txt('span', 'mono', l.key, { translate: 'no' }) : txt('span', 'logs-mute', '—'))];
    const content = box => {
      const rd = mk('div', 'logs-rd');
      // the event's detail (data) heads the reader; a raw line, a folded group or an event without
      // a detail starts with its facts (F-01: no sentence saying there is no detail)
      const detail = data && l.detail ? String(l.detail).trim() : '';
      if (detail) rd.append(richInto(mk('p', 'logs-rd-d', { translate: 'no' }), detail, ''));
      if (fx) rd.append(fx);
      const dl = mk('dl', 'kv lined logs-rd-kv'); dl.append(...rows);
      const copyB = mk('button', 'copy logs-copy-l', { type: 'button', 'data-copy': true, 'aria-label': t('logs.reader.raw.copy'), 'data-tip': t('logs.reader.raw.copy') });
      const wb = mk('button', 'icon-btn sm', { type: 'button', 'data-wrap': true, 'aria-pressed': String(V.wrap), 'aria-label': t('logs.reader.raw.wrap'), 'data-tip': t('logs.reader.raw.wrap') }); wb.append(icon('wrap', 14));
      const sa = mk('div', 'sh-a'); sa.append(copyB, wb);
      // (the count of a group is in 重复 / 范围 above: the heading is the word alone)
      const sh = mk('div', 'sh logs-rd-sh'); sh.append(txt('span', 'sh-t', t('logs.reader.raw.title')), sa);
      const pre = txt('pre', 'logs-raw' + (V.wrap ? ' is-wrap' : ''), rawText, { translate: 'no' });
      const db = mk('button', 'link-btn logs-rd-more', { type: 'button', 'data-diag': true, 'aria-expanded': String(V.diag) }); db.append(t('logs.reader.diag.toggle'), icon('chevron-down', 14));
      const dg = mk('div', 'logs-rd-diag'); dg.hidden = !V.diag;
      const dl2 = mk('dl', 'kv logs-rd-kv'); dl2.append(...diag); dg.append(dl2);
      rd.append(dl, sh, pre, db, dg);
      box.replaceChildren(rd);
      // the key column's width (the kv lists' --kv-w), set through the CSSOM: no inline style attribute
      box.querySelectorAll('.logs-rd-kv').forEach(x => x.style.setProperty('--kv-w', '56px'));
      T.ui.copy(copyB, () => rawText);
      wb.addEventListener('click', () => { V.wrap = !V.wrap; wb.setAttribute('aria-pressed', String(V.wrap)); pre.classList.toggle('is-wrap', V.wrap); });
      db.addEventListener('click', () => {
        V.diag = !V.diag; db.setAttribute('aria-expanded', String(V.diag)); dg.hidden = !V.diag;
        if (V.diag) { M.stagger(dg.querySelectorAll('.kv > div'), { base: 0, step: 24, y: 4, dur: 200 }); setTimeout(() => dg.scrollIntoView({ block: 'nearest', behavior: M.reduced ? 'auto' : 'smooth' }), 60); }
      });
    };
    const actions = [{ label: t('logs.reader.action.copy_json'), icon: 'code', ghost: true, left: true, keep: true, run: b => copyMorph(b, json) }];
    if (view === 'events') actions.push({ label: t('logs.action.locate_raw'), icon: 'target', keep: true, run: () => locate(it, 'raw') });
    // 在关键事件中查看 only for a key event (O-6: a raw line has no row there)
    else if (l.ev && !noise) actions.push({ label: t('logs.action.locate_events'), icon: 'list', keep: true, run: () => locate(it, 'events') });
    return { title, data, sub, head: sevEl(view === 'raw' ? l.level : shownLv(l), 'logs-rd-sev'), content, actions };
  }
  /* the reader's title: a record's headline is data (translate="no"); the panel's accessible name
     is one of our sentences around it, so it never stands alone as untranslated text */
  function readerTitle(p) {
    if (!rd) return;
    const el = rd.el.querySelector('.ovl-title');
    if (el) { if (p.data) el.setAttribute('translate', 'no'); else el.removeAttribute('translate'); }
    rd.el.setAttribute('aria-label', t('logs.reader.aria', { title: p.title }));
  }
  function showReader(row, how) {
    const view = V.view, it = itemsFor(view).get(row.dataset.key); if (!it) return;
    if (rd && !rd.closed && rd._key === row.dataset.key && rd._view === view && rd._n === it.recs.length) return;
    const p = readerParts(it, view);
    // which way the content slides when browsing: the direction of travel in the list
    const idx = T.$$(ROWSEL[view], boxOf(view)).filter(r => !r.hidden).indexOf(row);
    if (!rd || rd.closed) {
      // docked in the panel's body: under the run summary, above the status line and inside the frame,
      // so it covers neither; it slides in and out from under the body's right edge (T.drawer dock)
      rd = T.drawer.open({ modal: false, dock: R.body, width: readerW, class: 'logs-reader', title: p.title, subtitle: p.sub, head: p.head, content: p.content, actions: p.actions,
        onClose: () => { const was = rd; rd = null; setReaderPad(false, was); } });
      readerGrip(rd.el);
      setReaderPad(true);
    } else {
      const dir = how === 'key' && rd._idx != null && idx >= 0 ? Math.sign(idx - rd._idx) : 0;
      rd.setTitle(p.title); rd.setSubtitle(p.sub); rd.head.replaceChildren(p.head);
      rd.setContent(p.content, { dir }); rd.setActions(p.actions);
    }
    readerTitle(p);
    Object.assign(rd, { _key: row.dataset.key, _view: view, _it: it, _idx: idx, _n: it.recs.length });
  }
  /** the open reader again, in place (a language switch, a group that grew) */
  function repaintReader(view) {
    if (!rd || rd.closed || rd._view !== view) return false;
    const it = itemsFor(view).get(rd._key);
    if (!it) return false;
    const p = readerParts(it, view);
    rd.setTitle(p.title); rd.setSubtitle(p.sub); rd.head.replaceChildren(p.head); rd.setContent(p.content, { instant: true }); rd.setActions(p.actions);
    readerTitle(p);
    Object.assign(rd, { _it: it, _n: it.recs.length });
    return true;
  }
  /* the room the docked reader takes, set on the few elements that need it (an inherited custom
     property on the page would restyle every row of the log) */
  function readerRoom(w) {
    const v = w ? w + 'px' : '';
    [R.scE, R.scR].forEach(sc => { if (sc.style.paddingRight !== v) sc.style.paddingRight = v; });
    R.empty.style.right = v;
    R.pill.style.left = w ? `calc((100% - ${w}px) / 2)` : '';
  }
  let padT = 0;
  function setReaderPad(on) {
    clearTimeout(padT);
    const apply = () => {
      const v = V.view, sc = scOf(v), tail = stick[v] && atBottom(sc);
      if (on && rd && !rd.closed) {
        readerRoom(readerW());
        R.panel.classList.add('has-reader');
      } else if (!rd) { readerRoom(0); R.panel.classList.remove('has-reader'); }
      // the narrower rows may re-wrap (the history note): a view parked at the tail stays there,
      // and the sticky day labels are re-placed for the new geometry
      if (tail) sc.scrollTop = sc.scrollHeight;
      pushDays(v);
    };
    // opening: the rows re-truncate under the arriving panel; closing: after the panel is gone
    if (on) apply(); else padT = setTimeout(apply, 150);
  }
  function syncReaderAfter(view) {
    if (!rd || rd.closed || rd._view !== view) return;
    const it = itemsFor(view).get(rd._key);
    if (!it) { if (!V.paused) rd.close(); return; }
    if (it.recs.length !== rd._n) repaintReader(view);
  }
  function locate(it, to) {
    const target = it.recs[it.recs.length - 1];
    // O-6: the record left the 4,000-row buffer (or its generation) since the reader opened
    if (!B.rows.includes(target)) { T.notify({ kind: 'info', message: t('logs.locate.gone') }); return; }
    if (to === 'events' && !target.ev) return;
    if (!pass(target, to)) {
      if (target.ord <= V.wm[to]) V.wm[to] = 0;
      if (V.paused && target.ord > V.pauseOrd) setPaused(false);
      if (RANK[target.level] < LEVELS[V.level]) setLevel('all');
      if (V.cats.size && !V.cats.has(target.cat)) V.cats.clear();
      if (V.srcs.size && !V.srcs.has(target.src)) V.srcs.clear();
      if (V.runOnly && target.run !== curRun().key) setRunOnly(false);
      if (V.q && !qOf(target, to).includes(V.q)) { V.q = ''; quiet = true; searchApi.clear(); quiet = false; }
      syncFilterBadge(); V.dirty.events = V.dirty.raw = true;
      T.notify({ kind: 'info', message: t('logs.locate.relaxed') });
    }
    if (V.dirty[to]) render(to, 'instant');
    setView(to, { select: target.id, force: to === V.view });
  }

  /* the clipboard, with an honest answer (O-7): the async API, else the legacy copy command,
     whose result says whether it worked */
  function copyStrict(text) {
    return new Promise(res => {
      const legacy = () => {
        let ok = false;
        try {
          const ta = mk('textarea', '', { readonly: true });
          Object.assign(ta.style, { position: 'fixed', left: '-9999px', opacity: '0' });
          ta.value = text; document.body.append(ta); ta.select();
          ok = document.execCommand('copy') === true; ta.remove();
        } catch (e) { ok = false; }
        res(ok);
      };
      try { if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(text).then(() => res(true), legacy); else legacy(); } catch (e) { legacy(); }
    });
  }
  const copyFailed = () => T.notify({ kind: 'warning', message: t('logs.copy.failed') });
  function copyMorph(b, text) {
    copyStrict(text).then(ok => {
      if (!ok) { copyFailed(); return; }
      const lab = b.querySelector('.lab'), bi = b.querySelector('.bi');
      const done = t('common.action.copied');
      if (!b._idle) { b._idle = { lab: lab.textContent, ic: bi ? Array.from(bi.childNodes, n => n.cloneNode(true)) : [] }; T.ui.reserve(b, [done]); }
      b.classList.add('is-done'); M.swapText(lab, done);
      if (bi) { bi.replaceChildren(icon('check', 15)); M.anim(bi, [{ transform: 'scale(.55)' }, { transform: 'none' }], { duration: M.SP.press.duration, easing: M.SP.press.easing }); }
      clearTimeout(b._cmT);
      // a language switch inside the 1.5 s closes the popover or repaints the reader: the button is
      // gone by then, and a detached one is left alone (its saved label is in the old language)
      b._cmT = setTimeout(() => { if (!b.isConnected) return; b.classList.remove('is-done'); M.swapText(lab, b._idle.lab); if (bi) bi.replaceChildren(...b._idle.ic.map(n => n.cloneNode(true))); }, 1500);
    });
  }
  /** a row copy keeps time, level and source (pasteable into a chat); feedback lands on the row */
  function rowText(it, view) {
    view = view || V.view;
    return it.recs.map(r => {
      const base = `${full(r.ts)} [${sevWord(view === 'events' ? shownLv(r) : r.level)}] [${srcName(r.src)}]`;
      return r.ev && view === 'events' ? `${base} ${r.title}${r.detail ? ' — ' + r.detail : ''}` : `${base} ${rawOf(r)}`;
    }).join('\n') + (it.recs.length > 1 && view === 'events' ? '\n' + t('logs.copy.repeat_note', { n: it.recs.length, first: full(it.recs[0].ts) }) : '');
  }
  function copyRow(row) {
    if (picks.keys.size > 1 && picks.view === V.view) { copyPicks(); return; }
    const it = itemsFor(V.view).get(row.dataset.key); if (!it) return;
    copyStrict(rowText(it)).then(ok => {
      if (!ok) { copyFailed(); return; }
      const old = row.querySelector('.logs-copied'); if (old) old.remove();
      const tag = mk('span', 'logs-copied', { role: 'status' });
      tag.append(icon('check', 13), T.tx('logs.copy.done', { n: it.recs.length }));
      row.append(tag);
      M.anim(tag, [{ opacity: 0, transform: 'translateX(6px)' }, { opacity: 1, transform: 'none' }], { duration: 180, easing: M.ease.out });
      setTimeout(() => { const a = M.anim(tag, [{ opacity: 1 }, { opacity: 0 }], { duration: 160, easing: M.ease.in, fill: 'forwards' }); M.onDone(a, () => tag.remove(), 300); }, 1400);
    });
  }

  /* ---- several rows (G44, O-8): Ctrl / ⌘ click adds or removes a row, Shift click takes the
     range from the last one, Ctrl A takes every row shown; Ctrl C copies them in list order
     (「已复制 N 条」); Esc, a filter, a view switch or a clear lets them go ---- */
  const picks = { view: null, keys: new Set(), anchor: null };
  const pickRows = view => T.$$(ROWSEL[view], boxOf(view)).filter(r => !r.hidden);
  function paintPicks(view) {
    const box = boxOf(view);
    box.querySelectorAll('.is-picked').forEach(el => { if (!(picks.view === view && picks.keys.has(el.dataset.key))) el.classList.remove('is-picked'); });
    if (picks.view !== view) return;
    for (const k of picks.keys) { const el = box.querySelector(`[data-key="${k}"]`); if (el) el.classList.add('is-picked'); }
  }
  function clearPicks() {
    if (!picks.keys.size) return;
    const v = picks.view;
    picks.keys.clear(); picks.view = null; picks.anchor = null;
    if (v) paintPicks(v);
  }
  function pick(row, how) {
    const view = V.view, key = row.dataset.key;
    if (picks.view !== view) { picks.keys.clear(); picks.view = view; }
    const cur = navOf(view) && navOf(view).get();
    if (!picks.keys.size && cur && cur.isConnected && cur.dataset.key !== key) picks.keys.add(cur.dataset.key);
    if (how === 'range') {
      const rs = pickRows(view), a = rs.findIndex(r => r.dataset.key === (picks.anchor || (cur && cur.dataset.key))), b = rs.indexOf(row);
      if (a >= 0 && b >= 0) for (let i = Math.min(a, b); i <= Math.max(a, b); i++) picks.keys.add(rs[i].dataset.key);
      else picks.keys.add(key);
    } else if (how === 'all') {
      pickRows(view).forEach(r => picks.keys.add(r.dataset.key));
    } else {
      if (picks.keys.has(key)) picks.keys.delete(key); else picks.keys.add(key);
      picks.anchor = key;
    }
    paintPicks(view);
  }
  function copyPicks() {
    const view = picks.view, items = itemsFor(view);
    const list = pickRows(view).filter(r => picks.keys.has(r.dataset.key)).map(r => items.get(r.dataset.key)).filter(Boolean);
    if (!list.length) return;
    const n = list.reduce((s, it) => s + it.recs.length, 0);
    copyStrict(list.map(it => rowText(it, view)).join('\n')).then(ok => {
      if (!ok) { copyFailed(); return; }
      T.notify({ kind: 'success', message: t('logs.copy.done', { n }) });
    });
  }

  function rowMenu(row, at) {
    const it = itemsFor(V.view).get(row.dataset.key); if (!it) return;
    const l = it.recs[it.recs.length - 1];
    const many = picks.view === V.view && picks.keys.size > 1 && picks.keys.has(row.dataset.key);
    if (rowMn) rowMn.close();
    rowMn = T.ui.menu(at || row, [
      { label: t('logs.menu.details'), icon: 'eye', kbd: 'Enter', run: () => showReader(row, 'api') },
      { label: t('logs.menu.copy', { n: many ? picks.keys.size : it.recs.length }), icon: 'copy', kbd: 'Ctrl C', run: () => (many ? copyPicks() : copyRow(row)) },
      V.view === 'events' ? { label: t('logs.action.locate_raw'), icon: 'target', run: () => locate(it, 'raw') } : l.ev && it.kind !== 'noise' ? { label: t('logs.action.locate_events'), icon: 'list', run: () => locate(it, 'events') } : null,
      { sep: true },
      // O-2: nothing shown, nothing to export
      { label: t('logs.menu.export_filtered'), icon: 'export', disabled: !canExport('logs.exportFiltered'), run: () => exportView('json') },
    ].filter(Boolean), { placement: 'bottom-start', onClose: () => { rowMn = null; } });
  }

  /* ================================================================ actions: export, clear, debug, folder */
  /* Exports (映射_logs §4; pages/logs/export.js): the shell's Save As first (cancel → nothing
     happens), then the job; the ⋯ button shows the job's progress and its outcome, and a saved
     bundle pops from it. The page never sees a path or a file name. */
  let exporting = false;
  const canExport = cmd => T.rt.can(cmd) && !exporting && !R.more._busy && (cmd === 'logs.exportBundle' || last[V.view].visible > 0);
  /** the seqs of every record the current view shows (folded members included; our own rows
      excluded): level, source, category, run, search, view and the clear watermark all apply (R9) */
  function viewSeqs() {
    const view = V.view, out = [];
    for (const r of B.rows) if (r.kind === 'log' && r.gen === B.gen && pass(r, view)) out.push(r.seq);
    return out;
  }
  function exportBundle() {
    if (moreMenu) moreMenu.close();
    if (!canExport('logs.exportBundle')) return;
    runExport({ purpose: 'bundle' });
  }
  function exportView(format) {
    if (moreMenu) moreMenu.close();
    if (!canExport('logs.exportFiltered')) return;
    const seqs = viewSeqs();
    if (!seqs.length) return;
    runExport({ purpose: 'filtered', format, seqs });
  }
  function runExport(o) {
    exporting = true;
    const bundle = o.purpose === 'bundle';
    exportLogs(T.rt, { ...o, onJob: job => {
      // the progress shows only while the job runs (not while the Save As dialog is open)
      T.ui.busy(R.more, job, { busy: t(bundle ? 'logs.bundle.busy' : 'logs.export.busy'), done: t(bundle ? 'logs.bundle.done' : 'logs.export.saved'), fail: t('logs.export.fail_short'), hold: 2600 }).catch(() => {});
    } }).then(out => { exporting = false; exportOutcome(out, o); }, err => { exporting = false; T.notify({ kind: 'error', message: T.rt.errorText(err) }); });
  }
  const exportError = e => (e && e.code === 'not_allowed' ? t('logs.export.expired') : T.rt.errorText(e));
  function exportOutcome(out, o) {
    const bundle = o.purpose === 'bundle';
    switch (out.status) {
      case 'cancelled': case 'empty': return;
      // criticism B8: another native dialog (Save As, a link confirmation) is still open
      case 'dialog_open': T.notify({ kind: 'warning', message: t('logs.export.dialog_open') }); return;
      case 'dialog_failed': T.notify({ kind: 'error', title: t('logs.export.dialog_failed'), message: T.rt.errorText(out.error) }); return;
      case 'interrupted': T.notify({ kind: 'warning', message: t('logs.export.interrupted') }); return;
      case 'failed': T.notify({ kind: 'error', title: t(bundle ? 'logs.bundle.failed' : 'logs.export.failed'), message: exportError(out.error) }); return;
      default: break;
    }
    const r = out.result || {};
    if (bundle) { if (visible && R.more.isConnected) setTimeout(() => showBundle(r), 180); else T.notify({ kind: 'success', message: t('logs.bundle.done'), actions: [{ label: t('logs.export.reveal'), run: () => reveal(r.ref) }] }); return; }
    // G41: records the sidecar no longer had when it wrote the file
    const missing = Array.isArray(r.missing_seqs) ? r.missing_seqs.length : 0;
    T.notify({ kind: 'success', title: t('logs.export.done', { n: Number.isInteger(r.count) ? r.count : 0 }), message: missing ? t('logs.export.missing', { n: missing }) : '', actions: [{ label: t('logs.export.reveal'), run: () => reveal(r.ref) }] });
  }
  function reveal(ref) {
    revealExport(T.rt, ref).then(r => {
      if (r.status === 'gone') T.notify({ kind: 'warning', message: t('logs.export.gone') });
      else if (r.status === 'failed') T.notify({ kind: 'error', message: T.rt.errorText(r.error) });
    });
  }
  function showBundle(r) {
    if (!visible) return;
    const files = Array.isArray(r.files) ? r.files.filter(f => typeof f === 'string') : [];
    // no file name (the page never learns it): what was saved, how big, what is inside
    const pop = bundlePop = T.ui.popover(R.more, (el, api) => {
      const ic = mk('span', 'logs-exp-ic'); ic.append(icon('folder', 18));
      const fn = mk('div', 'logs-exp-fn'); fn.append(txt('div', 'logs-exp-title', t('logs.bundle.done')), txt('div', 'logs-exp-meta', t('logs.bundle.meta', { size: F.bytes(Number(r.size) || 0), n: files.length })));
      const head = mk('div', 'logs-exp-file'); head.append(ic, fn);
      const list = mk('ul', 'logs-exp-list logs-exp-files', { translate: 'no' });
      files.slice(0, 6).forEach(f => { const li = mk('li'); li.append(icon('check', 13), txt('span', 'logs-exp-fname', f)); list.append(li); });
      const kids = [head];
      if (files.length) kids.push(list);
      if (files.length > 6) kids.push(txt('div', 'logs-exp-more', t('logs.bundle.more_files', { n: files.length - 6 })));
      kids.push(T.ui.callout({ kind: 'warning', small: true, message: t('logs.bundle.privacy') }));
      const open = mk('button', 'btn btn-sm', { type: 'button', 'data-open': true });
      const bi = mk('span', 'bi'); bi.append(icon('folder', 15));
      open.append(bi, txt('span', 'lab', t('logs.bundle.open_folder')));
      const act = mk('div', 'logs-exp-act'); act.append(open);
      kids.push(act);
      const box = mk('div', 'logs-exp'); box.append(...kids);
      el.append(box);
      open.addEventListener('click', () => { api.close(); reveal(r.ref); });
    }, { width: 372, placement: 'bottom-end', onClose: () => { if (bundlePop === pop) bundlePop = null; } });
    if (pop && pop.el) pop.el.setAttribute('aria-label', t('logs.bundle.aria'));
  }

  /* 清空当前视图 (G43, O-3): a watermark per view, nothing deleted. The undo lives in a notice with
     a 5 s countdown ring: new lines do not push it away; leaving the page or clearing again ends
     it; lines that left memory meanwhile are counted when it is used. */
  let undoNote = null;
  function endUndo() { if (undoNote) { const n = undoNote; undoNote = null; n.dismiss(); } undo = null; }
  function clearView() {
    const v = V.view;
    if (!last[v].visible) return;
    endUndo();
    const n = B.rows.filter(r => inView(r, v) && r.ord > V.wm[v] && !held(r)).length;
    undo = { view: v, wm: V.wm[v], to: B.ord, n };
    V.wm[v] = B.ord;
    if (rd && !rd.closed && rd._view === v) rd.close();
    clearPicks();
    render(v, 'filter');
    syncEmpty(true);
    const u = undo;
    undoNote = T.notify({ kind: 'info', message: t('logs.clear.done', { view: v }), timeout: 5000, actions: [{ label: t('logs.empty.cleared.undo'), run: () => { if (undo === u) undoClear(); } }] });
  }
  function undoClear() {
    if (!undo) return;
    const u = undo; undo = null; undoNote = null;
    if (V.wm[u.view] !== u.to) return;               // cleared again since
    V.wm[u.view] = u.wm;
    render(u.view, 'filter');
    if (V.view === u.view) { const sc = scOf(u.view); sc.scrollTop = sc.scrollHeight; }
    const back = B.rows.filter(r => inView(r, u.view) && r.ord > u.wm && r.ord <= u.to).length, gone = Math.max(0, u.n - back);
    T.notify({ kind: 'success', message: gone ? t('logs.clear.restored_partial', { n: back, gone }) : t('logs.clear.restored', { n: back }) });
  }

  /* Mihomo DEBUG capture (O-4): logs.setMihomoLevel; what is on is what logs.feed.level says */
  const debugOn = () => { const f = feed(); return !!(f && f.level === 'debug'); };
  function toggleDebug() {
    const want = debugOn() ? 'info' : 'debug';
    T.rt.call('logs.setMihomoLevel', { level: want }).then(r => {
      if (r && r.applied === false) T.notify({ kind: 'warning', message: t('logs.debug.not_applied') });
      else {
        T.notify({ kind: 'info', message: t(want === 'debug' ? 'logs.debug.started' : 'logs.debug.stopped') });
        if (want === 'debug' && visible) M.anim(R.debug, [{ opacity: 0, transform: 'scale(.9)' }, { opacity: 1, transform: 'none' }], { duration: 200, easing: M.ease.out });
      }
      syncFoot();
    }, err => {
      T.notify({ kind: 'error', title: t('logs.debug.failed'), message: err && err.code === 'not_running' ? t('logs.debug.not_running') : T.rt.errorText(err) });
      syncFoot();
    });
  }
  /* 打开日志文件夹: the scope's data folder (shell host.openFolder{kind:'data'}), where the old
     window opened it too (monitor.log's folder: monitor.log, events.jsonl, and the logs/
     subfolder with shell.log and sidecar-stderr.log); the notice says so (D17, G47: 已打开数据
     目录; 8th-wave ruling T-P4-H2 item 5). B-24 (a folder holding the logs only) is the
     prototype's claim: data/logs (kind 'logs') until a dedicated folder exists. */
  function openFolder() {
    const b24 = T.rt.feature('B-24');
    T.rt.call('host.openFolder', { kind: b24 ? 'logs' : 'data' }).then(r => {
      if (r && r.opened) T.notify({ kind: 'info', message: t(b24 ? 'logs.folder.opened_logs_only' : 'logs.folder.opened_data') });
      else T.notify({ kind: 'warning', message: t('logs.folder.failed') });
    }, err => T.notify({ kind: 'error', title: t('logs.folder.failed'), message: T.rt.errorText(err) }));
  }
  function openMore(e) {
    if (moreMenu) { moreMenu.close(); return; }
    if (R.more._busy) return;                      // an export is running: the button shows its progress
    const empty = !last[V.view].visible, on = debugOn();
    const canDebug = S.get('run') === 'running' && T.rt.can('logs.setMihomoLevel');
    // export formats: JSON and text (U5: no CSV)
    moreMenu = T.ui.menu(R.more, [
      { group: t('logs.more.group.export') },
      { label: t('logs.more.bundle'), icon: 'export', disabled: !canExport('logs.exportBundle'), run: exportBundle },
      { label: t('logs.more.export_json'), icon: 'export', hint: t('logs.more.export_count', { n: last[V.view].visible }), disabled: !canExport('logs.exportFiltered'), run: () => exportView('json') },
      { label: t('logs.more.export_text'), icon: 'export', disabled: !canExport('logs.exportFiltered'), run: () => exportView('text') },
      { group: t('logs.more.group.capture') },
      { label: t('logs.more.debug', { on: on ? 'yes' : 'no' }), icon: 'code', disabled: !canDebug, run: toggleDebug },
      { group: t('logs.more.group.files') },
      { label: t('logs.more.folder'), icon: 'folder', run: openFolder },
      { sep: true },
      { label: t('logs.more.clear'), icon: 'eye-off', disabled: empty, run: clearView },
    ], { placement: 'bottom-end', minWidth: 248, keyboard: e && e.detail === 0, onClose: () => { moreMenu = null; } });
  }

  /* ================================================================ live records */
  const unseen = { n: 0, err: false };
  let badgeDue = false;
  /* the window is hidden: the shell's presence (store presence.visible, T-P4-06) or the document */
  const winHidden = () => { const p = S.get('presence'); return !!(p && p.visible === false) || document.visibilityState === 'hidden'; };
  /* one render per frame for whatever arrived; while the window is hidden nothing is scheduled
     and nothing is written (the list, the badge); the first visible moment writes it all once */
  const gate = createLiveGate({ isHidden: winHidden, schedule: fn => requestAnimationFrame(fn), render: liveRender });
  function liveRender() {
    if (badgeDue) { badgeDue = false; paintBadge(true); }
    if (!rendered) return;
    if (!visible) { V.dirty.events = V.dirty.raw = true; return; }
    syncRun(true); syncFoot();
    V.dirty[V.view === 'events' ? 'raw' : 'events'] = true;
    if (V.paused) { syncFollow(true); syncPill(true); return; }
    if (pre && pre.view === V.view) { V.dirty[V.view] = true; return; }   // (T-P4-09) the render ending the job takes it
    render(V.view, 'live');
    idleRender();
  }
  /** rows that left the buffer (trim): what pointed at them goes too */
  function afterTrim(gone) {
    if (!gone.length) return;
    bsGen++;
    const ids = new Set(gone.map(r => r.id));
    V.open.forEach(k => { if (ids.has(k.slice(1))) V.open.delete(k); });
    for (const k of [...picks.keys]) if (ids.has(k.slice(1))) picks.keys.delete(k);
    if (V.paused) heldCut += gone.filter(r => isRec(r) && r.ord > V.pauseOrd).length;
  }
  /** the buffer changed: the list follows (now, or when it is next shown) */
  function changed() {
    if (rendered && !visible) V.dirty.events = V.dirty.raw = true;
    gate.note();
  }
  /* logs.batch (rt:logs), the shell's rt:log-gap and rt:reset from the moment this module loads:
     the badge counts from boot (R13). It counts the records this page sees for the first time
     (the buffer hands over only those, by seq), whether the frame is instant or not (review S3):
     a new epoch's seed window (PROTOCOL rt:replayed, up to 15 s) is all instant and carries real
     edges, and a same-epoch reconnect's replayed batches are dropped by seq before they get here.
     Only a generation change clears the buffer (L-7, R5); a same-epoch reconnect replays batches
     that the buffer drops by seq (criticism B9; buffer.js attachBuffer). */
  attachBuffer(T, B, {
    added: recs => { recs.forEach(badge); },
    trimmed: afterTrim,
    changed,
    generation: onGeneration,
  });
  // the new generation's hello: its history is fetched once the page has been shown
  T.on('rt:caps', () => { if (needSeed && rendered) seed(); loadController(); });
  /* the footer while the window is hidden (review S5; P4 architecture §10): nothing is written,
     the live gate's render after the window shows brings it up to date (liveRender) */
  T.on('rt:conn', () => { if (rendered && visible) { if (winHidden()) gate.note(); else syncFoot(); } });
  S.sub('presence', () => gate.shown());
  document.addEventListener('visibilitychange', () => gate.shown());
  /* the sidebar item says when something needs a look while you are elsewhere
     (errors and route changes only; routine warnings would make it cry wolf) */
  function badge(r) {
    if (T.router && T.router.current === 'logs') return;
    if (!isRec(r) || !r.ev || !(RANK[r.level] >= 3 || (r.level === 'warn' && r.cat === 'routing'))) return;
    unseen.n++; unseen.err = unseen.err || RANK[r.level] >= 3;
    badgeDue = true;
  }

  /* ================================================================ the seed and the generation */
  /* The seed (L-1…L-4): logs.tail{n:4000}, page by page while next_cursor is set. A
     snapshot_required (the cursor expired after 60 s idle, the history was cleared or evicted, the
     language changed) refetches the whole group; three of them in a row, or any other failure,
     show 无法载入日志 with a retry. Batches that arrive meanwhile wait in the buffer and are
     spliced on by seq. Without logs.tail in hello.caps the page shows the live lines only. */
  let seedState = 'idle', seedErr = null, seedTok = 0, seedLang = null, needSeed = true;
  /* the language the backend renders titles in: the store's lang (hello.lang, host:language), which
     the page's catalog follows a moment later. A seed taken while the catalog is still switching to
     it is already in that language: no second fetch when the switch lands (relabelTail). */
  const backendLang = () => S.get('lang') || lng();
  /** one logs.tail group (pages/logs/buffer.js fetchSeed); null once a newer seed or a
      generation change took over */
  const fetchTail = tok => fetchSeed(args => T.rt.query(TAIL_QUERY, args), { n: CAP, alive: () => tok === seedTok });
  function seed() {
    if (!T.rt) return;
    if (!T.rt.hello) { needSeed = true; seedState = 'loading'; return; }       // rt:caps seeds
    needSeed = false;
    if (!T.rt.can(TAIL_QUERY)) { seedState = 'ready'; syncAfterSeed(); return; }
    const tok = ++seedTok;
    seedState = 'loading'; seedErr = null; seedLang = backendLang();
    B.beginSeed();
    syncAfterSeed();
    fetchTail(tok).then(pages => {
      if (tok !== seedTok || !pages) return;
      const sd = B.applySeed(pages);
      // review S2: every row came back with a new ord. A retry after a failed seed rebuilds rows
      // the page had marked (a clear, a pause, an undo): the marks follow them by seq; picks,
      // unfolded rows and the list's focus named the old rows and go.
      V.wm.events = sd.mapOrd(V.wm.events); V.wm.raw = sd.mapOrd(V.wm.raw);
      if (V.paused) V.pauseOrd = sd.mapOrd(V.pauseOrd);
      if (undo) { undo.wm = sd.mapOrd(undo.wm); undo.to = sd.mapOrd(undo.to); }
      clearPicks();
      V.open.clear();
      [navE, navR].forEach(n => { if (n) n.clear(); });
      // the live records parked during the fetch are news like any batch (review S3)
      sd.added.forEach(badge);
      if (badgeDue) gate.note();
      afterTrim(B.trim());
      bsGen++;
      seedState = 'ready';
      T.dismiss('logs-seed');
      syncAfterSeed();
    }, err => {
      if (tok !== seedTok) return;
      B.abortSeed().forEach(badge);
      if (badgeDue) gate.note();
      afterTrim(B.trim());
      bsGen++;
      seedState = 'failed'; seedErr = err;
      syncAfterSeed();
      // L-2 with live lines on screen (they arrived meanwhile): the empty state cannot say it, a
      // notice does, with the same retry
      if (visible && last[V.view].visible) T.notify({ kind: 'warning', id: 'logs-seed', message: t('logs.load.failed_partial'), actions: [{ label: t('logs.load.retry'), run: () => seed() }] });
    });
  }
  /** the rows changed wholesale (a seed, a generation change, a relabel): both views rebuild */
  function syncAfterSeed() {
    V.dirty.events = V.dirty.raw = true;
    if (!rendered) return;
    syncFoot(); syncRun(false);
    if (visible) {
      // (T-P4-09) thousands of new rows: built in slices first, then placed at once (landSeed);
      // a render of the view meanwhile (a filter, a search) still runs the rest (FIN_SEED)
      if (ahead(V.view, landSeed, false, FIN_SEED)) return;
      landSeed();
      FIN_SEED.forEach(f => f());
    } else syncEmpty(false);
  }
  function landSeed() {
    if (!visible) return;
    const sc = scOf(V.view), tail = stick[V.view] || atBottom(sc), top = sc.scrollTop;
    render(V.view, 'instant', true);
    sc.scrollTop = tail ? sc.scrollHeight : top;
    if (tail) settleTop(V.view);
  }
  /* O-9: after a language change the backend's titles follow it: the history is fetched again and
     the rows' titles and details are replaced by seq (selection, filters, scroll stay). A failure
     keeps the old wording and offers a retry. */
  function relabelTail() {
    if (seedState !== 'ready' || !T.rt || !T.rt.can(TAIL_QUERY) || lng() === seedLang) return;
    const tok = seedTok, want = lng();
    fetchTail(tok).then(pages => {
      if (tok !== seedTok || !pages || lng() !== want) return;
      seedLang = want;
      T.dismiss('logs-relabel');
      if (B.relabel(pages.flatMap(p => (Array.isArray(p.records) ? p.records : [])))) { resetSearch(); bsGen++; syncAfterSeed(); }
    }, () => {
      if (tok !== seedTok || lng() !== want) return;
      T.notify({ kind: 'warning', id: 'logs-relabel', message: t('logs.load.relabel_failed'), actions: [{ label: t('logs.load.retry'), run: () => relabelTail() }] });
    });
  }
  /* a generation change (rt:reset generation_changed, L-7): the buffer, the watermarks, the
     undo, the selection and the reader belong to the old generation and go; a separator
     「应用后台已重新启动」 starts the new one; the new hello seeds it again */
  function onGeneration() {
    endPre(true);                                   // (T-P4-09) rows of the old generation
    seedTok++;                                      // (B.reset() already ran: attachBuffer)
    V.wm.events = V.wm.raw = 0;
    endUndo();
    clearPicks();
    V.open.clear();
    heldCut = 0;
    if (V.paused) V.pauseOrd = B.ord;
    pending = 0;
    bsGen++;
    if (rd && !rd.closed) rd.close();
    [navE, navR].forEach(n => { if (n) n.clear(); });
    seedState = 'idle'; needSeed = true;
    controller = null; controllerAsked = false;
    if (rendered && T.rt.hello && T.rt.state === 'live') seed();
    syncAfterSeed();
  }
  function paintBadge(animate) {
    const it = T.$('.nav-item[data-nav="logs"]'); if (!it) return;
    let b = it.querySelector('.logs-navbadge');
    if (!unseen.n) {
      if (b && !b._gone) { b._gone = true; const a = M.anim(b, [{ opacity: 1, transform: 'none' }, { opacity: 0, transform: 'scale(.6)' }], { duration: 140, easing: M.ease.in, fill: 'forwards' }); M.onDone(a, () => b.remove(), 300); }
      return;
    }
    const fresh = !b || b._gone;
    if (fresh) { if (b) b.remove(); b = T.h('span', { class: 'logs-navbadge', role: 'img' }); const lab = it.querySelector(':scope > span:not(.nav-key)'); (lab || it).after(b); }
    b.className = `logs-navbadge ${unseen.err ? 'is-err' : 'is-warn'}`;
    b.textContent = String(unseen.n);
    b.setAttribute('aria-label', t('logs.nav_badge.aria', { n: unseen.n, kind: unseen.err ? 'error' : 'route' }));
    if (animate) M.anim(b, fresh ? [{ opacity: 0, transform: 'scale(.5)' }, { opacity: 1, transform: 'none' }] : [{ transform: 'scale(1.18)' }, { transform: 'none' }], { duration: M.SP.press.duration, easing: M.SP.press.easing });
  }
  // the language changed (T.i18n.set → i18n:change; ruling wave 4, T-P4-03 item 2): the words this
  // page wrote itself are written again. Static wording (T.ti) is relabelled by the runtime.
  T.on('i18n:change', relocalize);
  const onLogs = () => T.router.current === 'logs';
  // a cross-page command exists from boot (the likeliest place to use it is 总览, before any visit)
  // one 前往 group: this entry sits after the four pages (order 1), so typing a page name finds the page.
  // Groups are ids (go, logs: shell.palette.group.<id>). Keywords (architecture §3.7 item 8): the
  // fixed English words below for every language, plus logs.palette.keywords.<cmd> in the current
  // one (pinyin only in the zh_CN catalog); read when the palette scores a query.
  const cmdOf = (id, en, o) => Object.assign({ id, get keywords() { return `${en} ${t('logs.palette.keywords.' + id.replace(/^logs-/, '').replace(/-/g, '_'))}`; } }, o);
  T.cmd.register(cmdOf('logs-go-issues', 'errors issues', { group: 'go', order: 1, label: () => t('logs.palette.go_issues'), hint: () => t('shell.page.logs.title'), icon: 'warning', when: () => !onLogs(), run: () => T.go('logs', { level: 'issue', runOnly: true }) }));

  /* 原始日志 selection and keys. The library's listNav reads a rect per row on every selection and
     key press (to skip hidden rows); in chunks that content-visibility skips, each read forces the
     chunk to render (≈ 450 ms per key press at 1,500 lines). This keeps its behaviour and API
     subset (get · select · refresh · clear; click, ↑ ↓ Home End PageUp PageDown, Enter, double
     click) and tells hidden rows by their flag alone (a raw row is never hidden any other way). */
  function rowNav(list, o) {
    const sel = o.rows;
    let cur = null;
    const rows = () => T.$$(sel, list).filter(r => !r.hidden);
    const flag = (r, on) => { r.setAttribute('aria-selected', String(on)); r.tabIndex = on ? 0 : -1; };
    function select(row, how, focus) {
      if (!row) return;
      const prev = cur; cur = row;
      if (prev && prev !== row) flag(prev, false);
      flag(row, true);
      if (focus || how === 'key') row.focus({ preventScroll: true });
      if (how === 'key' || how === 'api') row.scrollIntoView({ block: 'nearest' });
      if (prev !== row && o.onSelect) o.onSelect(row, how);
    }
    list.addEventListener('click', e => {
      const r = e.target.closest(sel); if (!r || !list.contains(r)) return;
      const c = e.target.closest('button, a, input, select, textarea, label, [data-no-select]'); if (c && c !== r && r.contains(c)) return;
      select(r, 'click');
    });
    list.addEventListener('dblclick', e => { const r = e.target.closest(sel); if (r && list.contains(r) && o.onOpen) o.onOpen(r, 'dblclick'); });
    list.addEventListener('keydown', e => {
      if (e.target.closest('input, textarea, select') || !/^(ArrowDown|ArrowUp|Home|End|PageDown|PageUp|Enter)$/.test(e.key)) return;
      if (e.key === 'Enter') { if (cur && o.onOpen) { e.preventDefault(); o.onOpen(cur, 'key'); } return; }
      const rs = rows(); if (!rs.length) return;
      const i = rs.indexOf(cur), last = rs.length - 1, page = Math.max(1, Math.floor(o.scroller.clientHeight / RAW_H) - 2);
      const j = e.key === 'ArrowDown' ? (i < 0 ? 0 : Math.min(last, i + 1)) : e.key === 'ArrowUp' ? (i < 0 ? 0 : Math.max(0, i - 1))
        : e.key === 'Home' ? 0 : e.key === 'End' ? last : e.key === 'PageDown' ? Math.min(last, Math.max(0, i) + page) : Math.max(0, i - page);
      e.preventDefault(); select(rs[j], 'key');
    });
    return {
      el: list,
      get: () => cur,
      select: (r, so) => select(typeof r === 'number' ? rows()[r] : r, 'api', so && so.focus),
      clear() { if (cur && cur.isConnected) flag(cur, false); cur = null; },
      refresh() {
        if (cur && !cur.isConnected) { const k = cur.dataset.key; cur = k ? list.querySelector(`[data-key="${k}"]`) : null; if (cur) flag(cur, true); }
        if (cur && cur.hidden) { flag(cur, false); cur = null; }
      },
    };
  }

  /* ================================================================ language switch */
  /* Every string the page derived from the catalog or Intl is rebuilt: the per-timestamp memos
     (times, day keys), the tier-role pattern, each record's search text (level and source words),
     then the rows of both views (a full build: every row's words changed; the row signatures carry
     the language), the run strip, the status line, the pill, the empty state and the open reader.
     Open menus and the filter popover close (they are rebuilt on the next open). The selection, the
     filters, the scroll position and the cleared views stay. The backend's own titles are fetched
     again in the new language (relabelTail, O-9). */
  function relocalize() {
    endPre(true);                                   // (T-P4-09) rows in the old language
    memos.forEach(m => m.clear()); tierRe = null;
    resetSearch();
    if (!winHidden()) paintBadge(false); else if (unseen.n) badgeDue = true;
    if (!rendered) return;
    bsGen++;
    [fltPop, moreMenu, rowMn, bundlePop].forEach(f => { if (f) f.close(); });
    emptyKind = undefined;                          // (not null: null is "no empty state shown")
    [R.total,R.runM, R.runT, R.cntE, R.feedT, R.feedM, R.updL, R.flLab].forEach(el => { if (el) { el._sig = el._h = el._t = el._w = undefined; } });
    relabelTail();
    syncFollow(false); syncRun(false); syncFoot(); syncFilterBadge();
    V.dirty.events = V.dirty.raw = true;
    if (visible) {
      syncPill(false);
      // (T-P4-09 fix 1, review M1) every row's words changed: a large view is built in slices
      // first (the old wording stays on screen meanwhile), then placed at once (landLang); the
      // other view follows while idle (FIN_LANG)
      if (!ahead(V.view, landLang, false, FIN_LANG)) { landLang(); FIN_LANG.forEach(f => f()); }
    }
    if (rd && !rd.closed) repaintReader(rd._view);
  }
  function landLang() {
    if (!visible) return;
    const sc = scOf(V.view), top = sc.scrollTop, tail = stick[V.view] && atBottom(sc);
    render(V.view, 'instant', true);
    sc.scrollTop = tail ? sc.scrollHeight : top;
  }

  /* ================================================================ wiring */
  function wire(life) {
    viewSeg = T.ui.seg(R.view, { value: 'events', onChange: v => { setView(v); } });
    levelSeg = T.ui.seg(R.level, { value: 'all', onChange: v => { V.level = v; refilterSoon(); } });
    searchApi = T.ui.search(R.search, { debounce: 150, onInput: q => { if (quiet) return; V.q = q.toLowerCase(); refilter(); } });
    R.filter.addEventListener('click', openFilter);
    R.follow.addEventListener('click', () => setPaused(!V.paused));
    R.more.addEventListener('click', openMore);
    R.pill.addEventListener('click', () => (V.paused ? setPaused(false) : toBottom(true)));
    [R.cntE, R.cntW].forEach(b => b.addEventListener('click', () => { setLevel(b.dataset.lv); setRunOnly(true); if (V.view !== 'events') setView('events'); refilterSoon(); }));
    R.root.addEventListener('click', e => { const a = e.target.closest('[data-act="folder"]'); if (a) openFolder(); });

    // lists: single click opens the reader; ↑ ↓ browse with the reader following; Enter opens
    const listOpts = view => ({
      onSelect: (row, how) => { tabStops(view); if (how === 'click' || ((how === 'key' || how === 'api') && rd && !rd.closed)) showReader(row, how); },
      onOpen: row => showReader(row, 'key'),
    });
    navE = T.ui.listNav(R.events, Object.assign({ rows: ROWSEL.events, scroller: R.scE }, listOpts('events')));
    navR = rowNav(R.rows, Object.assign({ rows: ROWSEL.raw, scroller: R.scR }, listOpts('raw')));
    /* the library's fold motion moves the rows of the fold's own container; in 原始日志 that is one
       chunk, so what sits below the chunk is moved here by the same amount on the same spring */
    const onFold = (t, open) => {
      if (open) V.open.add(t.dataset.fold); else V.open.delete(t.dataset.fold);
      const ch = t.closest('.logs-chunk');
      if (ch) {
        sizeChunk(ch);
        const n = ch.querySelectorAll(`[data-fold-of="${t.dataset.fold}"]`).length, dy = (open ? -1 : 1) * n * RAW_H;
        const sc = scOf('raw'), s = sc.getBoundingClientRect();
        if (dy && !M.reduced && visible) for (let el = ch.nextElementSibling; el; el = el.nextElementSibling) {
          if (el.hidden || el._ghost) continue;
          const r = el.getBoundingClientRect(); if (r.top > s.bottom + Math.abs(dy)) break;
          el.animate([{ transform: `translateY(${dy}px)` }, { transform: 'none' }], { duration: M.SP.soft.duration, easing: M.SP.soft.easing });
        }
      }
      requestAnimationFrame(() => pushDays(V.view));
    };
    foldE = T.ui.fold(R.events, { onToggle: onFold });
    foldR = T.ui.fold(R.rows, { onToggle: onFold });
    [[R.events, ROWSEL.events, navE, foldE], [R.rows, ROWSEL.raw, navR, foldR]].forEach(([box, sel, nav, fold]) => {
      // several rows (G44): Ctrl / ⌘ or Shift with a click picks rows instead of opening the reader;
      // a capture listener, so the list's own click handling does not see that click
      box.addEventListener('click', e => {
        if (!(e.ctrlKey || e.metaKey || e.shiftKey) || e.target.closest('button, a')) return;
        const row = e.target.closest(sel); if (!row || row.hidden) return;
        e.preventDefault(); e.stopImmediatePropagation();
        pick(row, e.shiftKey ? 'range' : 'toggle');
      }, true);
      box.addEventListener('keydown', e => {
        if (!e.target.matches(sel)) return;
        if ((e.ctrlKey || e.metaKey) && !e.shiftKey && !e.altKey && e.key.toLowerCase() === 'a') { e.preventDefault(); e.stopPropagation(); pick(e.target, 'all'); }
        else if (e.key === 'Escape' && picks.keys.size) { e.preventDefault(); e.stopPropagation(); clearPicks(); }
      }, true);
      // with nothing selected yet, arrows move from the focused row (the tail stop), not from the top
      box.addEventListener('keydown', e => {
        if (nav.get() || !/^(ArrowUp|ArrowDown|Enter)$/.test(e.key) || !e.target.matches(sel)) return;
        const rs = T.$$(sel, box).filter(r => !r.hidden), i = rs.indexOf(e.target); if (i < 0) return;
        e.preventDefault(); e.stopPropagation();
        const row = e.key === 'Enter' ? e.target : rs[Math.max(0, Math.min(rs.length - 1, i + (e.key === 'ArrowUp' ? -1 : 1)))];
        nav.select(row, { focus: true });
        if (e.key === 'Enter') showReader(row, 'key');
      }, true);
      box.addEventListener('click', e => {
        const row = e.target.closest(sel); if (!row || e.target.closest('button, a')) return;
        if (!rd || rd.closed) showReader(row, 'click');
      });
      box.addEventListener('keydown', e => {
        const row = e.target.closest(sel); if (!row) return;
        if ((e.ctrlKey || e.metaKey) && !e.shiftKey && !e.altKey && e.key.toLowerCase() === 'c' && !String(getSelection())) { e.preventDefault(); copyRow(row); }
        else if (e.key === 'ContextMenu' || (e.shiftKey && e.key === 'F10')) { e.preventDefault(); rowMenu(row); }
        else if ((e.key === 'ArrowRight' || e.key === 'ArrowLeft') && !e.target.closest('[data-fold]')) {
          const t = row.querySelector('[data-fold]');
          if (t) { e.preventDefault(); fold.toggle(t, e.key === 'ArrowRight'); }
          else if (e.key === 'ArrowLeft' && row.dataset.foldOf) {
            // ← on a folded-out line: back to its ×N row, and the group closes (tree convention)
            const pt = box.querySelector(`[data-fold="${row.dataset.foldOf}"]`), pr = pt && pt.closest(sel);
            if (pr) { e.preventDefault(); nav.select(pr, { focus: true }); fold.toggle(pt, false); }
          }
        }
      });
      box.addEventListener('contextmenu', e => {
        const row = e.target.closest(sel); if (!row) return;
        e.preventDefault(); nav.select(row);
        rowMenu(row, { left: e.clientX, right: e.clientX, top: e.clientY, bottom: e.clientY, width: 0, height: 0 });
      });
    });
    /* the tail stays followed unless you scroll up: a scroller that just shrank under a banner
       (its scroll event lands after the layout change) must not read as "the user left the tail" */
    [R.scE, R.scR].forEach(sc => sc.addEventListener('scroll', T.util.raf(() => {
      if (sc !== scOf(V.view) || sc.hidden) return;
      const v = V.view, top = sc.scrollTop;
      stick[v] = atBottom(sc) || (stick[v] && top >= lastTop[v] - 1);
      lastTop[v] = top;
      pushDays(v);
      if (pending && stick[v] && !V.paused) { pending = 0; syncPill(true); }
    }), { passive: true }));

    // a run state change rewrites run separators above the tail (本次运行 → 运行了 …): full rebuild
    life.on('rt:state', () => {
      bsGen++;
      if (visible) V.dirty[V.view] = true;
      // hidden window: written when it shows (the gate), or on the next visit (enter)
      if (winHidden()) { if (visible) gate.note(); return; }
      syncRun(visible); syncFoot();
      if (visible) gate.note();
    });
    // the strip and the footer follow only while the window shows (review S5): hidden, the live
    // gate's render after it shows writes them once (liveRender: syncRun + syncFoot)
    const shownOr = fn => { if (!visible) return; if (winHidden()) gate.note(); else fn(); };
    life.on('rt:phase', () => shownOr(() => syncRun(true)));
    life.on('rt:restart', () => shownOr(syncFoot));
    life.on('rt:stale', () => shownOr(() => syncRun(false)));
    life.sub('failure', () => shownOr(() => syncRun(true)));
    life.sub('uptime', v => { if (Math.floor(v / 60) !== Math.floor((v - 1) / 60)) shownOr(() => syncRun(true)); });
    // logs.feed: Mihomo's stream, DEBUG capture, dropped counts, the current run
    if (T.rt) life.add(T.rt.topic(FEED_TOPIC).sub(() => { bsGen++; shownOr(() => { syncFoot(); syncRun(true); }); }));
    // the reader is docked in the body: it follows the window size
    // the scroller shrinking (a smaller window): a view parked at the tail stays at the tail
    if (window.ResizeObserver) {
      new ResizeObserver(() => {
        const sc = scOf(V.view);
        if (visible && !sc.hidden && stick[V.view]) { sc.scrollTop = sc.scrollHeight; settleTop(V.view); }
        if (rd && !rd.closed) setReaderPad(true);             // (the reader re-docks itself)
      }).observe(R.body);
      // content that changes height without a scroll (a fold, a re-wrapped row) re-places the day labels
      const ro = new ResizeObserver(T.util.raf(() => { if (visible) pushDays(V.view); }));
      ro.observe(R.events); ro.observe(R.grid);
    }

    T.cmd.register([
      cmdOf('logs-view', 'raw events', { group: 'logs', label: () => t('logs.palette.view', { to: V.view === 'events' ? 'raw' : 'events' }), icon: () => (V.view === 'events' ? 'code' : 'list'), when: onLogs, run: () => setView(V.view === 'events' ? 'raw' : 'events') }),
      cmdOf('logs-follow', 'pause follow', { group: 'logs', label: () => t('logs.palette.follow', { paused: V.paused ? 'yes' : 'no' }), icon: () => (V.paused ? 'play' : 'pause'), when: onLogs, run: () => setPaused(!V.paused) }),
      cmdOf('logs-bundle', 'diagnostic bundle zip', { group: 'logs', label: () => t('logs.palette.bundle'), icon: 'export', when: onLogs, run: exportBundle }),
      cmdOf('logs-issues', 'errors warnings', { group: 'logs', label: () => t('logs.palette.issues'), icon: 'warning', when: onLogs, run: () => { setLevel('issue'); setRunOnly(true); refilter(); } }),
      cmdOf('logs-reset', 'clear filter', { group: 'logs', label: () => t('logs.palette.reset'), icon: 'filter', when: onLogs, disabled: () => !filtersActive(), run: () => resetFilters(true) }),
    ]);
  }

  T.page('logs', {
    titleId: 'shell.page.logs.title',
    render(root, ctx) {
      root.classList.add('is-fill');
      build(root); wire(ctx.life);
      rendered = true;
      // the history (L-1): logs.tail once hello is here; the controller's address for the
      // status line's tip (app.info listeners, 映射_logs #52)
      if (needSeed) seed();
      loadController();
      // (T-P4-09) built by enter() (the shown view, in slices when large) and while idle (the other)
      V.dirty.events = V.dirty.raw = true;
      syncFollow(false); syncRun(false); syncFoot(); syncFilterBadge();
    },
    enter(ctx) {
      visible = true;
      unseen.n = 0; unseen.err = false; badgeDue = false; paintBadge(!ctx.again);
      if (needSeed && seedState !== 'loading') seed();
      const p = ctx.params || {};
      let refil = false;
      if (p.level && LEVELS[p.level] != null && p.level !== V.level) { setLevel(p.level, false); refil = true; }
      if (p.runOnly != null && !!p.runOnly !== V.runOnly) { setRunOnly(!!p.runOnly); refil = true; }
      // category (a v1 Log.category; 待用户认可表第 11 行, the alert area's 查看全部): the category
      // filter becomes that one category and the source filter is cleared, so the page opens on
      // that category's lines whichever source wrote them (the search text, typed by the user, stays)
      if (typeof p.category === 'string' && CATS.includes(p.category) && !(V.cats.size === 1 && V.cats.has(p.category) && !V.srcs.size)) {
        V.cats.clear(); V.cats.add(p.category); V.srcs.clear(); refil = true;
      }
      if (refil) { V.dirty.events = V.dirty.raw = true; syncFilterBadge(); }
      const sc = scOf(V.view);
      // (T-P4-09) a large view is built in slices first; its render then lands where this visit would
      const built = () => { if (!visible || !V.dirty[V.view]) return; render(V.view, 'instant'); if (stick[V.view]) { scOf(V.view).scrollTop = scOf(V.view).scrollHeight; settleTop(V.view); } idleRender(); };
      if (V.dirty[V.view] && !ahead(V.view, built, false, FIN_ENTER)) render(V.view, 'instant');
      syncRun(false); syncFoot(); syncFollow(false); syncPill(false);
      if (!ctx.again) { stick[V.view] = V.scroll[V.view] == null; if (V.scroll[V.view] == null) { sc.scrollTop = sc.scrollHeight; settleTop(V.view); } else sc.scrollTop = V.scroll[V.view]; }
      // (T-P4-09: T.util.fontsReady, not document.fonts.ready, which lays out the document when read)
      if (!ctx.again) T.util.fontsReady().then(() => { if (visible && V.scroll[V.view] == null) { sc.scrollTop = sc.scrollHeight; settleTop(V.view); } if (viewSeg) viewSeg.place(false); if (levelSeg) levelSeg.place(false); });
      ctx.scope.listen(window, 'resize', T.util.raf(() => { pushDays(V.view); if (rd && !rd.closed) setReaderPad(true); }));
      pushDays(V.view);
      idleRender();
      // Esc: the reader closes first (the overlay stack), a toast is dismissed next (app.js, which
      // marks that Esc as used), and only an Esc with nothing else to close clears the filters
      ctx.scope.listen(document, 'keydown', e => {
        if (e.key !== 'Escape' || e.defaultPrevented || T.overlayOpen() || T.cmd.isOpen || T.$('.menu, .pop')) return;
        if (e.target.closest && e.target.closest('input, textarea')) return;
        if (picks.keys.size) { e.preventDefault(); clearPicks(); return; }
        if (filtersActive()) { e.preventDefault(); resetFilters(true); }
      });
    },
    leave() {
      visible = false;
      cancelIdle();
      endPre(false);                                // (T-P4-09) the next visit builds what is still dirty
      if (cancelRefilter()) refilter();             // a filter changed in the last frame: apply it, without motion
      V.scroll[V.view] = atBottom(scOf(V.view)) ? null : scOf(V.view).scrollTop;
      // the undo of a clear ends with the visit (O-3)
      if (undo || undoNote) { endUndo(); emptyKind = undefined; }
      clearPicks();
      pending = 0; syncPill(false);
    },
  });
})();
