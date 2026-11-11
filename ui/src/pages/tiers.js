/* ==========================================================================
   pages/tiers.js — tiers (page id 'profiles', alias 'tiers')
   --------------------------------------------------------------------------
   · The tier switcher: Vanguard › Sentinel › Bastion on one track (the fallback order), Gateway
     on its own. A white thumb glides to the tier on screen; the carrying marker travels to the
     tier that carries traffic (tiers.summary carrier / fallback / degraded, jobs restart).
   · One list per tier from the runtime (T-P4-05b): nodes.list and subscriptions.list (keyed by
     tier, read with T.rt.topic(name, tier)), the drafts from config.items, Gateway's current exit
     from gateway.current, the carrying node from route.published, Bastion's pool from bastion.pool
     and its switch from tiers.summary.tiers.bastion.enabled. Derivations (row state, current
     marker, drafts, subscription rows, outcomes, the import form rules) are in rt/project_tiers.js.
     Counts are always the runtime's own; the page never recounts and never makes data up.
   · Commands: nodes.diagnose (request_id from T.rt; cancelled with nodes.cancelDiagnose, also when
     the side panel's owner is revoked), nodes.detail after it (the newer of detail and list row),
     nodes.update (secrets as SecretEdit), nodes.delete, nodes.reveal (private: the answer stays in
     the panel's memory, never in the store or an attribute), subscriptions.refresh, import.preview
     → import.apply (an expired plan is previewed again once), gateway.select (node_id; '' turns
     Gateway off), settings.apply section bastion (K-7, when config.snapshot has it).
   · Fields the v1 contract added later (K items: subscription_id, slow, last_checked_at_ms,
     recheck, last_refresh_at_ms, endpoint, protocols, groups, config values bastion) are shown when
     they are present, never through caps.features (P4 architecture section 4 item 2). Prototype-only
     controls (checklist B items) stay behind T.rt.feature('B-xx') (section 5).
   · Untrusted text (node, subscription, source names, protocol keys, labels of import groups) is
     put in as text under translate="no"; markup is built with T.html / DOM nodes, never by
     assigning HTML strings. The page writes no log records (B-19).
   Contract kept: params.tier focuses a tier; the switcher cell carrying [data-flip-target=
   "tier:<id>"] receives the hand-off from the overview deck.
   Wording: every word is a tiers.* message (src/i18n/locales/zh_CN/tiers.json); static text is
   registered for in-place relabel (T.ti), dynamic text is rebuilt on i18n:change (relocalize()).
   ========================================================================== */
import * as P from '../rt/project_tiers.js';

(function () {
  'use strict';
  const T = window.T, D = T.data, S = T.store, M = T.motion, F = T.fmt;
  const I = (n, s, c) => T.icon(n, s || 16, c);
  const TIERS = P.TIERS;
  let R = {}, visible = false;

  /* ================================================================ wording and DOM helpers */
  const L = (id, p) => T.t(id, p);
  const el = (tag, cls) => { const e = document.createElement(tag); if (cls) e.className = cls; return e; };
  /** an element whose text is one message, registered for in-place relabel */
  const tiEl = (tag, cls, id, p) => T.ti(el(tag, cls), id, p || null);
  /** translatable attributes registered for relabel: tiAttr(el, ['aria-label', id], ['data-tip', id, params]) */
  const tiAttr = (e, ...pairs) => { pairs.forEach(([a, id, p]) => T.ti(e, id, p || null, { attr: a })); return e; };
  /** data text (names, protocols, addresses): a text node under translate="no" */
  const dataEl = (s, cls, tag) => { const e = el(tag || 'span', cls); e.setAttribute('translate', 'no'); e.textContent = s == null ? '' : String(s); return e; };
  /** rich message whose <x> wraps data: the slot keeps the data inside a translate="no" span */
  const xData = (cls, tag) => kids => { const e = el(tag || 'span', cls); e.setAttribute('translate', 'no'); e.append(kids); return e; };
  const tx = (id, p) => T.tx(id, p, { x: xData() });
  /** a message whose <x> wraps data, kept as { id, p } until shown: msgN builds nodes, msgS plain text
      (for the core's string-only surfaces: T.confirm, T.notify, T.ui.busy faces) */
  const msgOf = (id, p) => ({ id, p });
  const msgN = m => (m == null ? '' : typeof m === 'string' ? m : tx(m.id, m.p));
  const msgS = m => (m == null ? '' : typeof m === 'string' ? m : tx(m.id, m.p).textContent);
  const hN = (tag, cls, ...kids) => { const e = el(tag, cls); kids.forEach(k => { if (k != null && k !== '') e.append(k); }); return e; };
  const fragN = (...kids) => { const f = document.createDocumentFragment(); kids.forEach(k => { if (k != null && k !== '') f.append(k); }); return f; };
  const iconN = (n, s, c) => T.iconEl(n, s || 16, c);
  /** a button with a registered label (and optional icon): btnN('btn btn-sm', <message id>, {icon, data}) */
  function btnN(cls, id, o) {
    o = o || {};
    const b = el('button', cls); b.type = 'button';
    if (o.icon) b.append(hN('span', 'bi', iconN(o.icon, o.size || 15)));
    if (id) b.append(tiEl('span', 'lab', id, o.p));
    if (o.data) Object.assign(b.dataset, o.data);
    return b;
  }
  /** an icon button named by a message (aria-label + tip, registered) */
  function iconBtn(cls, icon, id, o) {
    o = o || {};
    const b = el('button', cls); b.type = 'button';
    b.append(iconN(icon, o.size || 14));
    tiAttr(b, ['aria-label', id, o.p], ['data-tip', id, o.p]);
    if (o.data) Object.assign(b.dataset, o.data);
    if (o.tab != null) b.tabIndex = o.tab;
    return b;
  }
  /** parts joined by the page's separator between facts */
  const dotJoin = parts => fragN(...parts.filter(p => p != null && p !== '').flatMap((p, i) => (i ? [' · ', p] : [p])));
  /** set a cell to a built node when what it shows changed (the key is read, never parsed back) */
  const keyOf = node => { const b = el('div'); b.append(node.cloneNode(true)); return b.innerHTML; };
  const setD = (e, node) => { if (!e) return; const k = keyOf(node); if (e._h !== k) { e._h = k; e.replaceChildren(node); } };
  const setT = (e, s) => { if (e && e._t !== s) { e._t = s; e.textContent = s; } };
  /** the library's status / dot builders as nodes (T-P4-07: core.js builds them as nodes; a
      Raw still goes through T.html). The key of a node is its markup, read (keyOf) */
  const rawN = r => T.html`${r}`;
  const keyRaw = r => (r instanceof Node ? keyOf(r) : String(r));
  const setRaw = (e, r) => { if (!e) return; const s = keyRaw(r); if (e._h !== s) { e._h = s; e.replaceChildren(rawN(r)); } };
  /* swapText with a DOM face: the library rolls it in, the face is built as nodes (data stays text) */
  function swapNode(e, node, o) {
    const key = '\u0001' + node.textContent;
    if (e._swap === key) return;
    M.swapText(e, key, o);
    const host = e._swapBox || e, face = host.lastElementChild;
    if (face) face.replaceChildren(node);
  }
  /* a tier is named by its code name alone, never translated (F-03: no role word beside it) */
  const tierName = x => D.tier(x).name;
  const tierLabel = tierName;
  /* 最近检测 with seconds (the checks are 30-120 s apart) */
  const agoSec = ts => { const s = Math.max(0, Math.round((Date.now() - ts) / 1000)); return s < 60 ? L('tiers.ago_sec', { s: s < 2 ? 0 : s }) : F.ago(ts); };
  /* an RtError (or a protocol Error) in words: its message_key, errors.<code>, errors.unknown */
  const errText = e => T.rt.errorText(e || { code: 'internal' });

  /* ================================================================ vocabulary */
  /* reason_code → tiers.reason.<code> (word) and tiers.reason.<code>.fix (advice); the code list is
     rt/project_tiers.js REASON_CODES (backend_keys.py reasons tiers.reason + configuration_invalid) */
  const wordOf = r => T.rt.reasonText('tiers.reason', P.reasonKey(r));
  const fixOf = (r, n) => L(`tiers.reason.${P.reasonKey(r)}.fix`, { max_ms: ceil(), source: n && n.kind === 'subscription' ? 'subscription' : 'own' });

  /* ================================================================ view state */
  const V = {
    tier: 'vanguard', drill: null, q: '', sort: 'lat', gen: 0, panel: null, pkey: null, edit: null, pr: null, mute: false, imp: null, mng: null,
    cfg: null, cfgBusy: false, rev: {}, results: new Map(), fresh: null, expect: null, lastFlush: 0, flushT: 0,
  };
  /* the latency ceiling (config.snapshot.values.latency.primary_gateway_max_latency_ms): it only
     colours latencies (T.ui.lat's bands), it never re-judges a node */
  const ceil = () => (V.cfg && V.cfg.ceil) || 800;

  /* ================================================================ runtime data (read on demand) */
  const topic = (name, key) => T.rt.topic(name, key).get();
  const listOf = (name, key) => { const v = topic(name, key); return v && Array.isArray(v.items) ? v.items : null; };
  const run = () => S.get('run');
  const running = () => P.isRunning(run());
  const DS = {
    /** nodes.list rows of a tier (null: no snapshot yet in this epoch) */
    nodes: t => listOf('nodes.list', t),
    /** config.items rows (drafts, the degraded subscription rows, ConfigItem ids) */
    items: () => listOf('config.items') || [],
    subsPublished: () => T.rt.has('subscriptions.list'),
    /** subscription rows of a tier (subscriptions.list, or config.items when the topic is absent) */
    subs: t => P.subscriptionRows(t, { list: listOf('subscriptions.list', t), published: DS.subsPublished(), items: DS.items() }),
    gateway: () => topic('gateway.current') || null,
    route: () => topic('route.published') || null,
    pool: () => topic('bastion.pool') || null,
    /** bastion.pool has no fresh value (gap, no snapshot yet: rt/stale.js); its last figures stay */
    poolStale: () => !!(S.get('stale') || {})['bastion.pool'],
    summary: () => topic('tiers.summary') || null,
    ctx: () => ({ route: DS.route(), gateway: DS.gateway(), run: run() }),
    stale: t => { const s = S.get('stale') || {}; return !!s[`nodes.list/${t}`]; },
  };
  const bastionOn = () => { const t = (S.get('tiers') || {}).bastion; return !!(t && t.enabled); };
  /** the runtime's rows of a tier, by kind (drafts from config.items are separate) */
  const liveRows = t => {
    const rows = (DS.nodes(t) || []).filter(n => P.nodeState(n) !== 'draft');
    /* during the wait after an import (V.expect), until nodes.list catches up, the configured nodes it
       lacks are listed from config.items. A stopped service re-projects nodes.list from its configuration
       (bridge_stopped_node_inventory), so its rows and ids are real in every state. */
    return V.expect && V.expect.t === t ? rows.concat(P.unloadedRows(t, DS.items(), rows)) : rows;
  };
  const draftsOf = t => { const n = DS.nodes(t) || []; return n.filter(x => P.nodeState(x) === 'draft').concat(P.draftRows(t, DS.items(), n)); };
  const subName = (t, id) => { if (!id) return null; const s = (DS.subs(t) || []).find(x => x.id === id); return s ? s.name : null; };
  const membership = t => P.hasMembership(DS.nodes(t));
  /** every row the page can show for a key (nodes of every tier, drafts, the pool) */
  function findRow(id) {
    for (const t of TIERS) {
      const hit = (DS.nodes(t) || []).find(n => n.id === id) || liveRows(t).find(n => n.id === id) || P.draftRows(t, DS.items(), DS.nodes(t) || []).find(n => n.id === id);
      if (hit) return hit;
    }
    return null;
  }
  function findSub(id) {
    for (const t of TIERS) { const hit = (DS.subs(t) || []).find(s => s.id === id); if (hit) return hit; }
    return null;
  }
  const isCur = (n, t) => P.isCurrent(n, t || n.tier, DS.ctx());
  const stateOf = n => P.nodeState(n);
  const lastKnown = s => P.lastKnown(s, run());
  const stTag = (s, chip) => T.ui.status(s, { chip, last: lastKnown(s) });
  /** read-only rows: subscription and automatic (pool) nodes are maintained elsewhere */
  const roOf = n => n.kind === 'subscription' || n.kind === 'automatic';

  /* config.snapshot: the latency ceiling and the Bastion values (K-7, T-P4-C1 item 1) */
  function loadConfig() {
    if (V.cfgBusy || !T.rt.can('config.snapshot')) return;
    V.cfgBusy = true;
    T.rt.query('config.snapshot', {}).then(d => {
      V.cfgBusy = false;
      const values = (d && d.values) || {};
      const lat = values.latency || {};
      const c = Number(lat.primary_gateway_max_latency_ms);
      const b = values.bastion && typeof values.bastion === 'object' ? values.bastion : null;
      V.cfg = { rev: d && Number.isInteger(d.rev) ? d.rev : 0, ceil: c > 0 ? c : null, bastion: b };
      changed('config');
    }, () => { V.cfgBusy = false; });
  }
  /** K-7: Bastion's switch and minimum can be written (config.snapshot carries the bastion section) */
  const bastionWritable = () => !!(V.cfg && V.cfg.bastion && T.rt.can('settings.apply'));

  /* ================================================================ markup */
  function build(root) {
    root.classList.add('is-fill');
    const cell = t => {
      const d = D.tier(t);
      const c = T.html`<button class="tiers-cell" type="button" role="tab" id="tiers-tab-${t}" data-tier="${t}" data-flip-target="tier:${t}" aria-controls="tiers-stage" aria-selected="false" tabindex="-1">
        <span class="tiers-cell-in"><span class="tiers-l1">${I(d.icon, 16)}<span class="tiers-nm" translate="no">${d.name}</span></span>
        <span class="tiers-l2"><span class="pill tiers-pill-ph" aria-hidden="true" hidden><span class="sdot"></span><span class="tiers-pill-ph-t"></span></span><span class="tiers-slot"></span><span class="tiers-r"><span class="tiers-cnt"><span class="tiers-cnt-n"></span><span class="tiers-cnt-u wide-only" data-i18n="tiers.cell.usable_unit">${L('tiers.cell.usable_unit')}</span></span><span class="tiers-dots"></span></span></span></span></button>`;
      return c;
    };
    const page = T.html`<div class="tiers">
  <div class="tiers-sw" role="tablist" aria-label="${L('tiers.switcher.aria')}" data-i18n-attr="aria-label:tiers.switcher.aria" data-enter>
    <span class="tiers-land" aria-hidden="true"></span><span class="tiers-well tiers-well-a" aria-hidden="true"></span><span class="tiers-well tiers-well-b" aria-hidden="true"></span>
    <span class="tiers-thumb" aria-hidden="true"></span>
    ${cell('vanguard')}<span class="tiers-chev c1" aria-hidden="true">${I('chevron', 14)}</span>${cell('sentinel')}<span class="tiers-chev c2" aria-hidden="true">${I('chevron', 14)}</span>${cell('bastion')}${cell('gateway')}
    <span class="pill tiers-pill" aria-hidden="true"><span class="sdot"></span><span class="tiers-pill-t"></span></span>
  </div>
  <span class="tiers-pill-desc" id="tiers-pill-desc" hidden></span>
  <div class="toolbar tiers-tb" data-enter>
    <label class="search sm tiers-search"><input type="search" aria-label="${L('tiers.toolbar.search_aria')}" data-i18n-attr="aria-label:tiers.toolbar.search_aria"></label><span class="tiers-search-desc" id="tiers-search-desc" hidden></span>
    <span class="seg tiers-sort" aria-label="${L('tiers.toolbar.sort_aria')}" data-i18n-attr="aria-label:tiers.toolbar.sort_aria"><button class="seg-btn" data-v="lat" data-i18n="tiers.toolbar.sort.lat">${L('tiers.toolbar.sort.lat')}</button><button class="seg-btn" data-v="name" data-i18n="tiers.toolbar.sort.name">${L('tiers.toolbar.sort.name')}</button></span>
    <span class="spacer"></span>
    <button class="btn btn-sm btn-primary tiers-imp" type="button"><span class="bi">${I('import', 15)}</span><span class="lab" data-i18n="tiers.toolbar.import">${L('tiers.toolbar.import')}</span></button>
  </div>
  <div class="tiers-stage" id="tiers-stage" role="tabpanel" data-enter></div>
</div>`;
    root.replaceChildren(page);
    const q = s => root.querySelector(s);
    R = { root: q('.tiers'), page: root, sw: q('.tiers-sw'), thumb: q('.tiers-thumb'), land: q('.tiers-land'), pill: q('.tiers-pill'), pillT: q('.tiers-pill-t'), pillDesc: q('.tiers-pill-desc'),
      tb: q('.tiers-tb'), searchEl: q('.tiers-search'), searchDesc: q('.tiers-search-desc'), sortEl: q('.tiers-sort'),
      test: null, imp: q('.tiers-imp'), stage: q('.tiers-stage'), cells: {} };
    syncTestBtn();
    TIERS.forEach(t => {
      const c = q(`[data-tier="${t}"]`);
      R.cells[t] = { cell: c, ph: c.querySelector('.tiers-pill-ph'), phT: c.querySelector('.tiers-pill-ph-t'), slot: c.querySelector('.tiers-slot'), cnt: c.querySelector('.tiers-cnt'), cntN: c.querySelector('.tiers-cnt-n'), dots: c.querySelector('.tiers-dots') };
    });
  }

  /* B-05 (test every node of the list): the control is kept behind its feature; its command is
     wired when the H item lands (P4 architecture section 5), so it calls nothing here. The feature
     is known once hello arrives (its caps are key 1): the button follows rt:caps */
  function syncTestBtn() {
    const on = T.rt.feature('B-05');
    if (on && !R.test) {
      R.test = btnN('btn btn-sm tiers-test', 'tiers.toolbar.test', { icon: 'pulse' });
      R.imp.before(R.test);
    } else if (!on && R.test) { R.test.remove(); R.test = null; }
  }
  /* the B items that change the page's structure (columns, toolbar, sections): a view built before
     hello (no caps yet) is rebuilt when they turn out to be on */
  /* literal names only: the features gate (scan rule features, check dynamic-name) reads each one */
  const structFeatures = () => [
    T.rt.feature('B-03'), T.rt.feature('B-04'), T.rt.feature('B-05'), T.rt.feature('B-06'), T.rt.feature('B-08'),
    T.rt.feature('B-09'), T.rt.feature('B-10'), T.rt.feature('B-11'), T.rt.feature('B-12'), T.rt.feature('B-13'),
    T.rt.feature('B-15'), T.rt.feature('B-16'), T.rt.feature('B-17'), T.rt.feature('B-18'), T.rt.feature('B-37'),
  ];
  const featKey = () => structFeatures().map(on => (on ? 1 : 0)).join('');

  /* ================================================================ switcher */
  /* one dot per node in the status vocabulary's shapes; not running, the same shapes dimmed */
  const dotsRaw = (list, stale) => T.ui.dots(0, 0, { states: list.map(stateOf), last: stale });
  function cellInfo(t) {
    const r = run(), on = r === 'running', car = on ? S.get('carrier') : null;
    const sum = (S.get('tiers') || {})[t] || { ok: 0, total: 0, enabled: false, state: 'off', unknown: true };
    const nodes = DS.nodes(t), list = nodes ? nodes.filter(n => stateOf(n) !== 'draft') : [];
    let slot = '', cls = 'mute', cnt = null, dots = null, meter = null, word = '';
    const pair = (ok, total) => { cnt = { ok, total, text: `${ok}/${total}` }; };
    if (t === 'bastion') {
      const pv = P.poolView(DS.pool(), { enabled: bastionOn(), run: r, carrier: S.get('carrier') });
      if (pv.phase === 'off') { slot = word = L('tiers.cell.word.bastion_off'); }
      else if (!pv.known) { slot = !P.isRunning(r) ? L('tiers.cell.slot.service_off') : L('tiers.cell.slot.unknown'); word = slot; }
      else {
        /* a stale pool keeps its last figures, dimmed, and says so (as the overview deck does); a
           carrying cell leaves its second line to the travelling pill */
        const old = P.isRunning(r) && DS.poolStale();
        slot = !P.isRunning(r) ? L('tiers.cell.slot.service_off') : car === 'bastion' ? '' : old ? L('tiers.list.stale')
          : pv.phase === 'filling' ? L('tiers.cell.slot.filling') : L('tiers.cell.slot.standby');
        word = car === 'bastion' ? L('tiers.cell.word.carrying') : slot;
        /* §8u: the tier's available/total like every tier (own nodes plus the auto pool, one
           bastion.pool sample); never over the target. An older sidecar without totals: the count alone */
        if (P.isRunning(r)) {
          const all = pv.total != null && pv.userTotal != null;
          cnt = all ? { ok: pv.ok + pv.user, total: pv.total + pv.userTotal, text: `${pv.ok + pv.user}/${pv.total + pv.userTotal}`, old }
            : { ok: pv.ok, total: null, text: String(pv.ok), old, aria: L('tiers.pool.count_aria', { n: pv.ok }) };
          meter = { v: pv.ok, max: pv.capacity || pv.target || 1, hot: !old && (pv.phase === 'low' || car === 'bastion') };
        }
      }
    } else if (t === 'gateway') {
      const g = P.gatewayView(DS.gateway(), r);
      if (!g.known) { slot = L('tiers.cell.slot.unknown'); word = slot; }
      else if (g.missing) { slot = L('tiers.cell.slot.missing'); cls = 'err'; word = slot; }
      // (the name is data of any length: its own box ends in an ellipsis inside the slot, P5 #29)
      else if (g.name) { slot = el('span', 'st st-current'); slot.setAttribute('translate', 'no'); slot.append(dataEl(g.name, 'st-n')); cls = ''; word = L('tiers.cell.word.gateway_current', { name: g.name }); }
      else { slot = L('tiers.cell.slot.unused'); word = L('tiers.cell.word.gateway_unused'); }
      if (on) pair(sum.ok, sum.total);
      dots = dotsRaw(list, !on);
    } else if (r === 'stopped' || r === 'stopping' || r === 'failed' || r === 'unknown') {
      slot = nodes ? L('tiers.cell.slot.nodes', { n: list.length }) : '';
      word = r === 'unknown' ? L('tiers.cell.slot.unknown') : L('tiers.cell.word.stopped'); dots = dotsRaw(list, true);
    } else {
      /* a tier collected during the start is live (as the overview deck shows it); the tier that
         will carry the traffic says starting, the others standby */
      const down = sum.enabled && sum.state === 'down' && sum.total > 0, first = r === 'starting' && t === (S.get('order') || TIERS)[0];
      slot = car === t ? '' : down ? L('tiers.cell.slot.unavailable') : first ? L('tiers.cell.slot.starting') : L('tiers.cell.slot.standby'); cls = down && car !== t ? 'err' : 'mute';
      word = car === t ? (S.get('fallback') ? L('tiers.cell.word.fallback_carrying') : L('tiers.cell.word.carrying')) : slot;
      pair(sum.ok, sum.total); dots = dotsRaw(list, false);
    }
    const aria = L('tiers.cell.aria', { name: D.tier(t).name, word: word || '', has_word: word ? 'yes' : 'no', ok: cnt ? cnt.ok : 0, total: cnt && cnt.total != null ? cnt.total : 0,
      has_count: !cnt ? 'no' : cnt.total == null ? 'ok_only' : 'yes' });
    return { slot, cls, cnt, dots, meter, aria };
  }
  function syncCells(animate, noPlace) {
    animate = !!animate && visible;
    TIERS.forEach(t => {
      const c = R.cells[t], info = cellInfo(t), sel = t === V.tier;
      c.cell.classList.toggle('is-sel', sel); c.cell.setAttribute('aria-selected', String(sel)); c.cell.tabIndex = sel ? 0 : -1;
      const sk = typeof info.slot === 'string' ? info.slot : '\u0001' + info.slot.textContent;
      if (c.slot._h !== sk) { c.slot._h = sk; if (typeof info.slot === 'string') M.swapText(c.slot, info.slot, { instant: !animate }); else swapNode(c.slot, info.slot, { instant: !animate }); }
      ['mute', 'err'].forEach(k => c.slot.classList.toggle(k, info.cls === k));
      c.cnt.hidden = !info.cnt;
      c.cnt.classList.toggle('is-old', !!(info.cnt && info.cnt.old));
      if (info.cnt) M.odometer(c.cntN, info.cnt.text, { mode: 'auto', label: info.cnt.aria || L('tiers.cell.count_aria', { ok: info.cnt.ok, total: info.cnt.total }), instant: !animate });
      else c.cntN.removeAttribute('aria-label');
      if (info.meter) {
        let m = c.dots.querySelector('.tiers-mm');
        if (!m) { c.dots._h = null; c.dots.replaceChildren(T.html`<span class="meter tiers-mm"><span class="meter-track"><span class="meter-fill"></span></span></span>`); m = c.dots.firstElementChild; }
        /* Bastion's pool is neutral on standby; amber when low or carrying traffic (D9: of the capacity) */
        m.style.setProperty('--v', Math.min(1, info.meter.v / info.meter.max).toFixed(3)); m.classList.toggle('warn', info.meter.hot); m.classList.toggle('ink', !info.meter.hot);
      } else if (info.dots) setRaw(c.dots, info.dots);
      else if (c.dots._h !== '') { c.dots._h = ''; c.dots.replaceChildren(); }
      c.cell.setAttribute('aria-label', info.aria);
    });
    if (!noPlace) placePill(animate);
    R.stage.setAttribute('aria-labelledby', `tiers-tab-${V.tier}`);
  }
  function pillInfo() {
    if (run() !== 'running') return null;
    const car = S.get('carrier'); if (!car || !R.cells[car]) return null;
    if (S.get('restarting')) return { tier: car, text: L('tiers.pill.restarting'), cls: 'pill-line', dot: 'busy' };
    if (S.get('fallback')) return { tier: car, text: L('tiers.pill.fallback'), cls: 'pill-warn', dot: 'warn' };
    if (S.get('degraded') && car === 'vanguard') return { tier: car, text: L('tiers.pill.degraded'), cls: 'pill-warn', dot: 'warn' };
    return { tier: car, text: L('tiers.pill.carrying'), cls: 'pill-ok', dot: 'ok' };
  }
  /* the carrying cell's room steps (placePill), in the order things step out */
  const TIGHT = ['tight-1', 'tight-2', 'tight-3', 'tight-4'];
  /* the width of e's content box in fractional pixels (border box less borders and padding) */
  function contentW(e) { const cs = getComputedStyle(e); return e.getBoundingClientRect().width - (parseFloat(cs.paddingLeft) || 0) - (parseFloat(cs.paddingRight) || 0) - (parseFloat(cs.borderLeftWidth) || 0) - (parseFloat(cs.borderRightWidth) || 0); }
  /* layout offset of el inside anc, summed along the offsetParent chain */
  function offIn(e, anc) { let x = 0, y = 0; for (let n = e; n && n !== anc; n = n.offsetParent) { x += n.offsetLeft; y += n.offsetTop; } return { x, y }; }
  /* the carrying marker: one element that travels between cells on a JS spring. It lands on an
     invisible twin in the carrying cell's second line (.tiers-pill-ph: same classes, same words), so
     the cell's own text (the Gateway's name, the count) is laid out after it and never under it;
     the other cells' twins are hidden (T-P5-03, 10th-wave ruling item 3) */
  function placePill(animate) { placePillOnly(animate); fitWords(); }
  /* T-P5-13 (决定_P5裁定 P5-e #5, form b): a switcher cell's slot word (待命, de In Bereitschaft …)
     shows whole or steps out (F-03: the role word beside the name is gone). A word wider than its room is taken
     out of the line (it stays in the tab's name, cellInfo's aria-label) and its line carries it as
     the hover tip; in the second line the unit word beside the count gives way first (.unit-out),
     then the slot word (.slot-out, the unit word back). The Gateway's name in the slot is data: it
     ends in an ellipsis as before. Measured in fractional pixels after placePillOnly (the carrying
     cell's own steps), again on every placement (a resize, a language switch, a sync). */
  /* (fix round 1) against the content box, with the 0.02 px tolerance of placePill's short(): Chrome
     draws the ellipsis as soon as the words pass the content box by any fraction (es En espera,
     0.41 px), so a word cut by less than half a pixel used to stay with a half word */
  const wordCut = e => { if (!e || !e.isConnected || !e.offsetWidth) return false; const rg = document.createRange(); rg.selectNodeContents(e); return rg.getBoundingClientRect().width - contentW(e) > 0.02; };
  function fitWords() {
    TIERS.forEach(t => {
      const c = R.cells[t], cell = c.cell;
      const l2 = cell.querySelector('.tiers-l2');
      cell.classList.remove('unit-out', 'slot-out');
      l2.removeAttribute('data-tip');
      if (!cell.offsetWidth) return;
      const face = c.slot.querySelector(':scope > .sw:not([aria-hidden]), :scope > .swap-box > .sw:not([aria-hidden])');
      if (!face || !face.textContent.trim() || face.querySelector('[translate="no"]') || !wordCut(face)) return;
      cell.classList.add('unit-out');
      if (!wordCut(face)) return;
      cell.classList.remove('unit-out'); cell.classList.add('slot-out');
      l2.setAttribute('data-tip', face.textContent);
    });
  }
  function placePillOnly(animate) {
    const p = pillInfo(), e = R.pill;
    /* the pill is decoration for assistive technology (aria-hidden, never an image inside the
       tablist): its whole words are the carrying tab's description, a hidden node the tab names with
       aria-describedby (T-P5-11 ruling 4, form (b)); hidden, no tab has it and the node is empty, so
       a language switch never leaves stale words */
    if (R.pillDesc.textContent !== (p ? p.text : '')) R.pillDesc.textContent = p ? p.text : '';
    TIERS.forEach(t => {
      const c = R.cells[t], on = !!p && p.tier === t;
      if (on) { c.ph.className = `pill tiers-pill-ph ${p.cls}`; if (c.phT.textContent !== p.text) c.phT.textContent = p.text; }
      c.ph.hidden = !on;
      if (on) c.cell.setAttribute('aria-describedby', 'tiers-pill-desc'); else c.cell.removeAttribute('aria-describedby');
      c.cell.classList.remove(...TIGHT, 'unit-out', 'slot-out');
    });
    if (!p) { M.spring(e, { opacity: 0, scale: 0.86 }, { k: 520, c: 44, instant: !animate }); R.pillAt = null; e.classList.remove('is-dot'); e.removeAttribute('data-tip'); return; }
    const pc = R.cells[p.tier], ph = pc.ph;
    /* the pill's words show whole or not at all (T-P5-06 ruling 1). While the twin's words do not
       fit, one more thing steps out: the unit word, the dots, the count (.tight-1 … .tight-3), and
       last the pill folds to its dot (.tight-4) */
    /* measured in fractional pixels (the words' range over their box): scrollWidth / clientWidth are
       whole pixels and let a cut of less than a pixel pass as fitting */
    const short = () => { const rg = document.createRange(); rg.selectNodeContents(pc.phT); return rg.getBoundingClientRect().width - pc.phT.getBoundingClientRect().width > 0.02; };
    for (const k of TIGHT) { if (!short()) break; pc.cell.classList.add(k); }
    const dot = pc.cell.classList.contains('tight-4');
    /* the dot form gives the words' room back: what stepped out for them comes back, last out first
       in (the count, the dots, the unit word), while it fits the line: the row of count and dots
       stays inside the line and the slot (the Gateway's name) keeps its floor. The name gives way
       first here too, as on the way out (T-P5-06 fix round 1: not "the name may not narrow at all",
       which kept a long name whole and the count out) */
    if (dot) {
      const l2 = pc.cell.querySelector('.tiers-l2'), rr = pc.cell.querySelector('.tiers-r'), sl = pc.slot;
      const floor = (parseFloat(getComputedStyle(sl).minWidth) || 0) - 0.5;
      const fits = () => rr.getBoundingClientRect().right - l2.getBoundingClientRect().right <= 0.5 && sl.getBoundingClientRect().width >= floor;
      for (const k of ['tight-3', 'tight-2', 'tight-1']) {
        pc.cell.classList.remove(k);
        if (!fits()) { pc.cell.classList.add(k); break; }
      }
    }
    const at = offIn(ph, R.sw);
    const x = at.x, y = at.y + (ph.offsetHeight - 22) / 2;
    e.className = `pill tiers-pill ${p.cls}${dot ? ' is-dot' : ''}`;
    /* the dot form carries the words as its hover tip (its accessible form is the tab's description) */
    if (dot) e.setAttribute('data-tip', p.text); else e.removeAttribute('data-tip');
    /* rounded up: offsetWidth rounds the twin's fractional width down, and a fraction of a pixel short
       would clip the pill's words where the twin shows them whole */
    e.style.width = `${Math.ceil(ph.getBoundingClientRect().width)}px`;
    e.firstElementChild.className = `sdot ${p.dot}`;
    if (R.pillT._swap !== p.text) M.swapText(R.pillT, p.text, { instant: !animate || R.pillAt == null });
    if (R.pillAt == null) { M.spring(e, { x, y }, { instant: true }); M.spring(e, { opacity: 1, scale: 1 }, { k: 520, c: 44, instant: !animate }); }
    else M.spring(e, { x, y, opacity: 1, scale: 1 }, { k: 300, c: 34, instant: !animate });
    R.pillAt = p.tier;
  }
  /* The overview hand-off (the router flies a ghost of what was clicked for 560 ms and fades it from
     55 %; it lands on the first [data-flip-target="tier:<id>"] in the page). The front deck card lands
     as this tier's plate; a one-line source lands on the cell's identity line (.tiers-land sized to it). */
  function landFrom(src) {
    const D0 = 560, out = 'cubic-bezier(.2, .7, .2, 1)', cell = R.cells[V.tier].cell;
    const l1 = cell.querySelector('.tiers-l1'), l2 = cell.querySelector('.tiers-l2');
    const w0 = src.offsetWidth, h0 = src.offsetHeight, anim = (e, k, d) => e.animate(k, { duration: d || D0, easing: 'linear' });
    R.sw.getAnimations().forEach(a => a.cancel());
    M.anim(R.sw, [{ opacity: 0 }, { opacity: 1 }], { duration: 220, delay: 20, easing: M.ease.out, fill: 'backwards' });
    if (w0 && h0 && w0 / h0 > 5 && l1.offsetWidth) {
      const w = l1.offsetWidth, h = w * h0 / w0, s = (w / w0).toFixed(3), o = '0 50%', at = offIn(l1, R.sw);
      Object.assign(R.land.style, { left: at.x + 'px', top: (at.y + (l1.offsetHeight - h) / 2).toFixed(1) + 'px', width: w + 'px', height: h.toFixed(1) + 'px' });
      R.land.setAttribute('data-flip-target', 'tier:' + V.tier);
      setTimeout(() => R.land.removeAttribute('data-flip-target'), 0);
      anim(R.thumb, [{ opacity: 0, transform: 'scale(.94)' }, { opacity: 0, transform: 'scale(.94)', offset: 0.3, easing: out }, { opacity: 1, transform: 'none' }], D0 * 0.8);
      anim(l1, [{ opacity: 0, transform: `scale(${s})`, transformOrigin: o }, { opacity: 0, transform: `scale(${s})`, transformOrigin: o, offset: 0.5, easing: out }, { opacity: 1, transform: 'none', transformOrigin: o }]);
    } else anim(l1, [{ opacity: 0 }, { opacity: 0, offset: 0.56, easing: out }, { opacity: 1 }]);
    anim(l2, [{ opacity: 0, transform: 'translateY(3px)' }, { opacity: 0, transform: 'translateY(3px)', offset: 0.64, easing: out }, { opacity: 1, transform: 'none' }]);
    if (R.pillAt === V.tier) anim(R.pill, [{ opacity: 0 }, { opacity: 0, offset: 0.64, easing: out }, { opacity: 1 }]);
  }
  /* the selection plate is also where the overview deck card lands */
  function placeThumb(animate) {
    R.thumb.setAttribute('data-flip-target', 'tier:' + V.tier);
    const c = R.cells[V.tier].cell; if (!c.offsetWidth) return;
    R.thumb.style.width = c.offsetWidth + 'px'; R.thumb.style.height = c.offsetHeight + 'px';
    M.spring(R.thumb, { x: c.offsetLeft, y: c.offsetTop }, { k: 520, c: 44, instant: !animate || !visible || !R.thumbOn });
    R.thumbOn = true;
  }

  /* ================================================================ stale state + toolbar */
  /* 11-05: the context line under the switcher (Bastion's 「公共节点，请勿登录」) is gone: the
     page states no standing warnings; enabling Bastion still asks first (the on_confirm dialog) */
  function syncCtx() {
    /* not running: the list shows the last results, so live figures step back to the secondary ink */
    R.root.classList.toggle('is-stale', run() !== 'running');
  }
  function placeholder() {
    if (V.drill) return V.drill.kind === 'pool' ? L('tiers.search.placeholder.pool') : L('tiers.search.placeholder.sub');
    return L(`tiers.search.placeholder.${V.tier}`);
  }
  /* every placeholder of the page: the four tiers', a subscription's (drilled in), the pool's */
  const allPlaceholders = () => [...TIERS.map(x => L(`tiers.search.placeholder.${x}`)), L('tiers.search.placeholder.sub'), L('tiers.search.placeholder.pool')];
  /* T-P5-15 (决定_P5裁定 P5-e #5: catalog words show whole or step out): the search box's placeholder.
     The box takes the width the language's longest placeholder needs (all six of them, so it keeps one
     width while you move between tiers and drill in; never narrower than the CSS width, at most its
     max-width, and the toolbar may shrink it). A placeholder still wider than the input steps down to
     the box's short name (tiers.toolbar.search_aria, the words of its aria-label), then to none (the
     magnifier stays); the whole words are then the input's hover tip (on the input, so a keyboard focus
     shows it too: core's focusin asks the [data-tip] element for :focus-visible) and its description
     (aria-describedby → .tiers-search-desc). Measured in fractional pixels against the input's content
     box (the 0.02 px of wordCut) after every view change and layout (a resize, a language switch,
     fonts ready). The rest of the box (magnifier, gaps, keycap, padding) is measured without a query
     and kept: with one, the count may share the keycap's cell wider than the keycap (fix round 1). */
  function textWs(input, list) {
    const cs = getComputedStyle(input), m = document.createElement('span');
    m.setAttribute('aria-hidden', 'true');
    Object.assign(m.style, { position: 'fixed', left: '0', top: '0', visibility: 'hidden', whiteSpace: 'pre', pointerEvents: 'none',
      fontFamily: cs.fontFamily, fontSize: cs.fontSize, fontWeight: cs.fontWeight, fontStyle: cs.fontStyle, fontStretch: cs.fontStretch,
      fontVariant: cs.fontVariant, fontFeatureSettings: cs.fontFeatureSettings, fontKerning: cs.fontKerning, letterSpacing: cs.letterSpacing,
      wordSpacing: cs.wordSpacing, textTransform: cs.textTransform });
    document.body.append(m);
    const w = list.map(s => { m.textContent = s; return m.getBoundingClientRect().width; });
    m.remove();
    return w;
  }
  function fitSearch() {
    if (!R.search) return;
    const box = R.searchEl, input = R.search.input, full = placeholder();
    let ph = full;
    if (box.isConnected && box.offsetWidth) {
      const short = L('tiers.toolbar.search_aria');
      const [wShort, wFull, ...ws] = textWs(input, [short, full, ...allPlaceholders()]);
      let chrome = box.getBoundingClientRect().width - contentW(input);
      if (!box.classList.contains('has-q') || R.searchChrome == null) R.searchChrome = chrome; else chrome = R.searchChrome;
      box.style.setProperty('--tiers-search-need', `${Math.ceil(Math.max(...ws) + chrome + 1)}px`);
      const room = contentW(input);
      if (wFull - room > 0.02) ph = wShort - room > 0.02 ? '' : short;
    }
    if (input.placeholder !== ph) input.placeholder = ph;
    const out = ph !== full;
    R.searchDesc.textContent = out ? full : '';
    if (out) { input.setAttribute('data-tip', full); input.setAttribute('aria-describedby', R.searchDesc.id); }
    else { input.removeAttribute('data-tip'); input.removeAttribute('aria-describedby'); }
  }
  /* inside a subscription or the pool there is nothing to import into: 导入 leaves the toolbar.
     Bastion switched off keeps search and import: your own sources and nodes are still there (G39) */
  function syncToolbar(animate) {
    const gone = !!V.drill;
    if (R.imp.hidden !== gone) {
      const anim = !!animate && visible && !M.reduced;
      if (anim && R.test) M.flip([R.test], () => { R.imp.hidden = gone; }, M.SP.glide); else R.imp.hidden = gone;
      if (!gone && anim) M.anim(R.imp, [{ opacity: 0, transform: 'scale(.96)' }, { opacity: 1, transform: 'none' }], { duration: 200, delay: 90, easing: M.ease.out, fill: 'backwards' });
    }
    fitSearch();
  }
  function clearSearch() { V.q = ''; if (R.search.value) { V.mute = true; R.search.set(''); V.mute = false; } }

  /* ================================================================ entries (what the list shows) */
  /* names sort the current language's way (Intl.Collator for T.i18n.lang, cached per language by T.fmt) */
  const byName = (a, b) => F.compare(a.name, b.name);
  const byLat = (a, b) => P.latKey(a) - P.latKey(b) || byName(a, b);
  const sorter = () => (V.sort === 'name' ? byName : byLat);
  /** the search (G40: a subscription's name finds its nodes when Node.subscription_id is there) */
  function matcher() {
    const q = V.q.trim(); if (!q) return null;
    return n => P.matches(n, q, n.subscription_id ? subName(n.tier, n.subscription_id) : null);
  }
  /* every node the search looks through in the current view (the visible / total count) */
  function searchDomain() {
    const t = V.tier;
    if (V.drill) return V.drill.kind === 'pool' ? poolRows() : P.membersOf(V.drill.id, DS.nodes(t));
    const all = liveRows(t).concat(draftsOf(t));
    return t === 'bastion' ? all.filter(n => n.kind !== 'automatic' || bastionOn()) : all;
  }
  const poolRows = () => (DS.nodes('bastion') || []).filter(n => n.kind === 'automatic' && stateOf(n) !== 'draft');
  function entriesFor() {
    const t = V.tier, m = matcher(), srt = sorter(), out = [];
    const keep = n => !m || m(n);
    const node = (n, x) => out.push(Object.assign({ key: n.id, kind: 'node', n, cur: isCur(n, t) }, x));
    const lbl = (key, text, o) => out.push(Object.assign({ key: 'l:' + key, kind: 'label', text }, o));
    const group = s => out.push({ key: 'g:' + s.id, kind: 'group', g: s });
    const srcOf = n => (n.subscription_id ? subName(t, n.subscription_id) : null);
    const nodes = DS.nodes(t);
    if (V.drill && V.drill.kind === 'sub') {
      const ks = P.membersOf(V.drill.id, nodes).filter(keep).sort(srt);
      if (ks.length) { lbl('members', L('tiers.list.label.nodes'), { count: ks.length }); ks.forEach(n => node(n)); }
      return out;
    }
    if (V.drill && V.drill.kind === 'pool') { poolRows().filter(keep).sort(srt).forEach(n => node(n)); return out; }
    if (nodes == null) { out.push({ key: 'loading', kind: 'loading' }); return out; }
    const drafts = draftsOf(t).filter(keep).sort(byName);
    if (m) {
      searchDomain().filter(keep).sort(srt).forEach(n => node(n, { src: srcOf(n) }));
      return out;
    }
    const subs = DS.subs(t) || [];
    if (t === 'vanguard') {
      const live = liveRows(t).sort(srt);
      live.forEach(n => node(n));
      if (drafts.length) { lbl('draft', L('tiers.list.label.draft'), { count: drafts.length }); drafts.forEach(n => node(n)); }
      if (!live.length && !drafts.length) out.push({ key: 'empty-tier', kind: 'empty-tier' });
    } else if (t === 'sentinel') {
      /* B-10 (in use / standby split) is not in v1: one node list with the current marker (F-01: the
         section's hint, B-10's one included, explained the fallback and is gone) */
      const fromSubs = liveRows(t).filter(n => n.kind === 'subscription').sort(srt), loose = liveRows(t).filter(n => n.kind !== 'subscription').sort(srt);
      if (fromSubs.length) { lbl('nodes', L('tiers.list.label.nodes'), { count: fromSubs.length }); fromSubs.forEach(n => node(n, { src: srcOf(n) })); }
      if (subs.length) {
        /* B-06 (update every subscription) stays behind its feature */
        lbl('subs', L('tiers.list.label.subs'), { count: subs.length, act: subs.length > 1 && T.rt.feature('B-06') ? 'updall' : null, caps: true });
        subs.forEach(group);
      }
      const rest = loose.concat(drafts);
      if (rest.length) { lbl('loose', L('tiers.list.label.loose'), { count: rest.length }); rest.forEach(n => node(n)); }
      if (!fromSubs.length && !subs.length && !rest.length) out.push({ key: 'empty-tier', kind: 'empty-tier' });
    } else if (t === 'gateway') {
      const g = P.gatewayView(DS.gateway(), run()), live = liveRows(t);
      if (!live.length && !drafts.length && !subs.length && !(g.known && (g.missing || g.selected))) { out.push({ key: 'empty-tier', kind: 'empty-tier' }); return out; }
      /* the row of the current exit only by gateway.current.selected_node_id (T-P2-H8); without it
         the current exit is named as text and no row is guessed from its name (7th-wave ruling) */
      const cur = g.nodeId ? live.find(n => n.id === g.nodeId) || null : null;
      /* 停用 Gateway sits at the section's end; the missing row carries its own (G37) */
      lbl('cur', L('tiers.list.label.current'), { act: g.known && !g.missing && (g.name || g.waiting) ? 'off' : null });
      if (!g.known) out.push({ key: 'gw-unknown', kind: 'off', variant: 'unknown' });
      else if (g.missing) out.push({ key: 'gw-missing', kind: 'off', variant: 'missing', name: g.selected });
      else if (cur) node(cur, { cur: true });
      else if (g.name) out.push({ key: 'gw-named', kind: 'off', variant: 'named', name: g.name });
      else if (g.waiting) out.push({ key: 'gw-waiting', kind: 'off', variant: 'waiting', name: g.selected });
      else out.push({ key: 'off', kind: 'off', variant: 'gateway' });
      const rest = live.filter(n => n !== cur).sort(srt);
      lbl('cand', L('tiers.list.label.candidates'), { count: rest.length });
      rest.forEach(n => node(n));
      if (subs.length) { lbl('gsubs', L('tiers.list.label.subs'), { count: subs.length, caps: true }); subs.forEach(group); }
      if (drafts.length) { lbl('draft', L('tiers.list.label.draft'), { count: drafts.length }); drafts.forEach(n => node(n)); }
    } else if (t === 'bastion') {
      /* the automatic pool is the panel above the list (its one home); the list holds what you added,
         also while Bastion is switched off (G39). A node of one of your sources names it on its second
         line (data), as Sentinel's subscription nodes do (F-01 round 1) */
      const own = liveRows(t).filter(n => n.kind !== 'automatic').sort(srt).concat(drafts);
      if (own.length) { lbl('mine', L('tiers.list.label.mine'), { count: own.length }); own.forEach(n => node(n, { src: srcOf(n) })); }
      if (subs.length) { lbl('msrc', L('tiers.list.label.my_sources'), { count: subs.length, caps: true }); subs.forEach(group); }
      if (!own.length && !subs.length) { lbl('mine', L('tiers.list.label.my_sources')); out.push({ key: 'off', kind: 'off', variant: 'bastion' }); }
    }
    return out;
  }

  /* ================================================================ views */
  /* columns: name · latency · state · protocol [· region B-03] [· trend B-04] · actions; beside the side
     panel name · latency · state · actions (the optional columns carry .c-opt; tiers.css hides them) */
  /* the latency column is 88 px (not the prototype's 72): its two-character head 延迟 still fits in
     the longest languages and the pseudo language (7th-wave ruling, T-P4-H item 1); 地区 (B-03) the same */
  /* the state column is 100 px (T-P5-11: was 84): with the state word wrapping in it (the page's stylesheet), the
     longest single word of a real language (es comprobación, ru Перепроверка) still fits beside its dot.
     The actions (the last track) are 104 / 60 px or as wide as their buttons are (T-P5-11: a busy
     button's reserved width, de Als aktuell festlegen, ran out of the cell); being last, a wider
     cell moves no other column's start */
  function colsFor(gw, compact) {
    const reg = T.rt.feature('B-03'), trend = T.rt.feature('B-04'), act = gw ? 'minmax(104px, max-content)' : 'minmax(60px, max-content)';
    // compact (the panel is open): the protocol column is hidden (.c-opt), so the row's actions are
    // the fourth track; it keeps room for its buttons (T-P5-11: it was a 0-wide 1fr track at 1060,
    // the buttons ran past the row into the panel), and the name (data) gives way first
    if (compact) return 'minmax(0, 280px) 88px 100px minmax(max-content, 1fr)';
    return ['minmax(168px, 280px)', '88px', '100px', trend ? 'minmax(92px, 150px)' : 'minmax(92px, 1fr)', reg ? '88px' : null, trend ? 'minmax(0, 1fr)' : null, act].filter(Boolean).join(' ');
  }
  function listLabel() {
    if (V.drill) { const s = V.drill.kind === 'pool' ? null : findSub(V.drill.id); return V.drill.kind === 'pool' ? L('tiers.list.aria.pool') : L('tiers.list.aria.sub', { name: s ? s.name : '' }); }
    return tierLabel(V.tier);
  }
  function headN() {
    const head = hN('div', 'tiers-head');
    head.setAttribute('aria-hidden', 'true');
    head.append(tiEl('span', '', 'tiers.col.name'), tiEl('span', 'c-num', 'tiers.col.lat'), tiEl('span', '', 'tiers.col.state'), tiEl('span', 'c-opt', 'tiers.col.proto'));
    if (T.rt.feature('B-03')) head.append(tiEl('span', 'c-opt', 'tiers.col.region'));
    if (T.rt.feature('B-04')) head.append(tiEl('span', 'c-opt tiers-trend-h', 'tiers.col.trend'));
    head.append(el('span'));
    return head;
  }
  function buildView() {
    const v = hN('div', 'tiers-view');
    R.feat = featKey();
    R.bxOn = bastionOn();   // the Bastion view this build shows (flush rebuilds it when the switch flips)
    R.list = R.scroll = R.head = R.bar = R.barErr = R.poolEl = R.nav = R.staleEl = null;
    if (R.bxRO) { R.bxRO.disconnect(); R.bxRO = null; }
    if (V.drill) v.append(...drillBar());
    else if (V.tier === 'bastion' && bastionOn()) v.append(poolPanel());
    const gw = V.tier === 'gateway';
    const sc = hN('div', 'tiers-scroll');
    sc.style.setProperty('--cols-w', colsFor(gw, false)); sc.style.setProperty('--cols-c', colsFor(gw, true));
    /* Bastion switched off: the explanation card leads the scroller, your own list follows it (G39) */
    if (V.tier === 'bastion' && !bastionOn() && !V.drill) { v.classList.add('is-bx'); sc.append(bastionOff()); }
    const stale = hN('div', 'tiers-stale');
    stale.hidden = true;
    const head = headN();
    const list = hN('div', 'list tiers-list');
    list.setAttribute('aria-label', listLabel());
    sc.append(stale, head, list); v.append(sc);
    Object.assign(R, { view: v, scroll: sc, head, list, staleEl: stale });
    wireList(list, sc);
    return v;
  }
  /* views swap by sliding a short way in the direction of travel (no blank frame) */
  function swapView(dir, o) {
    o = o || {};
    const old = R.view, animate = !!o.animate && visible && !M.reduced;
    if (!o.relabel) V.gen++;
    if (!o.relabel || visible) T.ui.closeFloats();
    const nu = buildView();
    R.stage.append(nu);
    reserveBar();
    renderList(false);
    if (old) {
      if (!animate) old.remove();
      else {
        old.classList.add('is-leaving'); old.setAttribute('aria-hidden', 'true');
        const a = old.animate([{ opacity: 1, transform: 'none' }, { opacity: 0, transform: `translateX(${-dir * 10}px)` }], { duration: 120, easing: M.ease.in, fill: 'forwards' });
        M.onDone(a, () => old.remove(), 320);
      }
    }
    if (animate) {
      nu.animate([{ opacity: 0, transform: `translateX(${dir * 10}px)` }, { opacity: 1, transform: 'none' }], { duration: 240, delay: 30, easing: M.ease.out, fill: 'backwards' });
      if (R.list && o.rows !== false) M.stagger(T.$$('.tiers-list > :not(.list-hl)', nu).slice(0, 9), { base: 70, step: 16, dur: 220, y: 6, max: 9 });
    }
    return nu;
  }
  function setTier(t, o) {
    o = o || {};
    if (t === V.tier && !V.drill) return;
    const dir = Math.sign(TIERS.indexOf(t) - TIERS.indexOf(V.tier)) || -1;
    closePanel();
    V.tier = t; V.drill = null; V.edit = null;
    clearSearch();
    if (!o.quiet) { placeThumb(o.animate); syncCells(o.animate); syncCtx(o.animate); syncToolbar(o.animate); }
    swapView(dir, { animate: o.animate });
  }
  function confirmDiscard() {
    const n = V.edit && findRow(V.edit.id);
    return T.confirm({ title: L('tiers.discard.title'), message: L('tiers.discard.body', { has_name: n ? 'yes' : 'no', name: n ? n.name : '' }), confirmLabel: L('tiers.discard.ok'), cancelLabel: L('tiers.discard.cancel'), danger: true })
      .then(ok => { if (ok) endEdit(); return ok; });
  }
  function requestTier(t) {
    if (t === V.tier && !V.drill) return;
    if (V.edit && V.edit.dirty) { confirmDiscard().then(ok => { if (ok) setTier(t, { animate: true }); else placeThumb(false); }); return; }
    setTier(t, { animate: true });
  }

  /* ================================================================ list rendering (keyed: rows persist) */
  function makeEl(e) {
    let r;
    if (e.kind === 'label') { r = hN('div', 'list-g tiers-lbl'); r.dataset.key = e.key; }
    else if (e.kind === 'node') {
      /* the name and protocol are data (translate="no"); the sub line marks its own data parts */
      r = T.html`<div class="li two tiers-row" data-kind="node"><span class="li-main"><span class="li-t tiers-t"><span class="tiers-name" translate="no"></span><span class="st st-current tiers-cur" data-i18n="tiers.row.current">${L('tiers.row.current')}</span></span><span class="li-sub tiers-sub"></span></span><span class="c-num tiers-lat"></span><span class="tiers-st"></span><span class="c-dim c-ell c-opt tiers-proto" translate="no"></span></div>`;
      if (T.rt.feature('B-03')) r.append(hN('span', 'c-dim c-ell c-opt tiers-reg'));
      if (T.rt.feature('B-04')) r.append(hN('span', 'c-opt tiers-sp'));
      r.append(hN('span', 'li-act'));
      r.dataset.key = e.key;
    } else if (e.kind === 'group') {
      /* row actions stay out of the tab order until their row is selected (listNav's actions option) */
      r = T.html`<div class="li two tiers-grp" data-kind="group"><span class="tiers-gic">${I('stack', 15)}</span>
        <span class="li-main"><span class="li-t tiers-gt"><b class="tiers-gn" translate="no"></b><span class="chip sm tiers-gc"></span></span><span class="li-sub tiers-gsub"></span></span>
        <span class="tiers-gh"><span class="tiers-ght"></span><span class="meter tiers-gm"><span class="meter-track"><span class="meter-fill"></span></span></span></span>
        <span class="c-dim c-ell tiers-gup"></span>
        <span class="li-act"></span>
        <span class="tiers-gchev">${I('chevron', 16)}</span><span class="prog sm tiers-gprog"><i></i></span></div>`;
      r.dataset.key = e.key;
      r.querySelector('.li-act').append(iconBtn('icon-btn sm', 'refresh', 'tiers.sub.update', { data: { a: 'gupd' }, tab: -1 }), iconBtn('icon-btn sm', 'more', 'tiers.row.more', { data: { a: 'more' }, tab: -1 }));
    } else if (e.kind === 'off') {
      r = hN('div', 'tiers-offrow'); r.dataset.key = e.key;
    } else if (e.kind === 'loading') {
      r = hN('div', 'tiers-offrow tiers-loading', hN('span', 'spin'), tiEl('span', '', 'tiers.list.loading')); r.dataset.key = 'loading';
    } else if (e.kind === 'empty-tier') {
      r = emptyTier(V.tier); r.dataset.key = 'empty-tier';
    } else {
      r = T.ui.empty({ icon: 'search', title: L('tiers.search.empty.title'), actions: [{ label: L('tiers.search.empty.clear'), run: () => R.search.clear() }] });
      r.dataset.key = 'empty';
    }
    r._kind = e.kind;
    return r;
  }
  /* G34: an empty tier says what is missing, with the action that adds it (F-01: no sentence on what
     the tier is for) */
  function emptyTier(t) {
    const k = t === 'sentinel' ? 'sentinel' : t === 'gateway' ? 'gateway' : 'vanguard';
    return T.ui.empty({ icon: k === 'sentinel' ? 'stack' : 'import', title: L(`tiers.empty.${k}.title`),
      actions: [{ label: L(k === 'sentinel' ? 'tiers.empty.sentinel.action' : 'tiers.empty.import'), primary: true, run: () => openImport(R.imp, { tab: k === 'sentinel' ? 'sub' : 'paste' }) }] });
  }
  /* the off / missing / unknown rows of Gateway and the empty source line of Bastion */
  function paintOff(r, e) {
    const v = e.variant;
    let ic = 'ban', title = null, body = null, act = null;
    /* F-01: the empty source line, the unknown and the unused Gateway are their titles alone (the
       bodies explained them) */
    if (v === 'bastion') { ic = 'info'; title = L('tiers.list.off.bastion.title'); }
    else if (v === 'unknown') { ic = 'info'; title = L('tiers.gateway.unknown.title'); }
    else if (v === 'missing') {
      /* G37: the configured exit is not among the candidates: say which, and offer the two ways out */
      ic = 'warning'; title = L('tiers.gateway.missing.title'); body = tx('tiers.gateway.missing.body', { name: e.name || '' });
      act = 'missing';
    } else if (v === 'waiting') { ic = 'info'; title = L('tiers.gateway.waiting.title'); body = tx('tiers.gateway.waiting.body', { name: e.name || '' }); }
    /* (F-01: the current exit's row is its name; the section label above it already says 当前出口) */
    else if (v === 'named') { ic = 'target'; body = dataEl(e.name || '', 'tiers-offrow-n'); }
    else title = L('tiers.list.off.gateway.title');
    const key = v + '\u0001' + (title || '') + '\u0001' + (body == null ? '' : typeof body === 'string' ? body : body.textContent);
    if (r._h === key) return;
    r._h = key;
    r.classList.toggle('is-warn', v === 'missing');
    const text = hN('span', 'tiers-offrow-t', title ? hN('b', '', title) : null, body);
    r.replaceChildren(iconN(ic, 16), text);
    if (act === 'missing') {
      const pick = btnN('btn btn-xs', 'tiers.gateway.missing.pick', { data: { a: 'gwpick' } });
      const off = btnN('btn btn-xs btn-ghost', 'tiers.gateway.off_link', { data: { a: 'gwoff' } });
      r.append(hN('span', 'tiers-offrow-a', pick, off));
    }
  }
  /* the row's second line: source (a subscription name: data) · reason (F-01: a draft's 不参与回退 and a
     recheck's wait were said on every row; the state column says 草稿 / 待复检) */
  function subNode(n, e) {
    const parts = [];
    if (e.src) parts.push(dataEl(e.src));
    const r = P.nodeReason(n);
    if (r) parts.push(hN('span', 'tiers-why', wordOf(r)));
    return dotJoin(parts);
  }
  function actKind(n, e) {
    if (stateOf(n) === 'draft') return 'draft';
    if (n.tier === 'gateway' && !e.cur) return 'use';
    /* B-12: pool nodes are checked by the runtime itself (nodes.diagnose answers tier3_auto_pool_self_check) */
    if (n.kind === 'automatic' && !T.rt.feature('B-12')) return 'more';
    return 'std';
  }
  function actNodes(k) {
    const more = () => iconBtn('icon-btn sm', 'more', 'tiers.row.more', { data: { a: 'more' }, tab: -1 });
    const diag = () => iconBtn('icon-btn sm', 'pulse', 'tiers.row.diag', { data: { a: 'diag' }, tab: -1 });
    if (k === 'use') {
      const b = btnN('btn btn-xs tiers-use', 'tiers.row.use', { data: { a: 'use' } }); b.tabIndex = -1;
      return [b, iconBtn('icon-btn sm tiers-use-i', 'target', 'tiers.row.use_aria', { data: { a: 'use' }, tab: -1 }), more()];
    }
    if (k === 'draft' || k === 'more') return [more()];
    return [diag(), more()];
  }
  function latNode(n) {
    const st = stateOf(n);
    if (n._testing) return T.html`<span class="spin tiers-spin" role="img" aria-label="${L('tiers.row.checking')}"></span>`;
    if (st === 'draft' || st === 'pending' || st === 'recheck' || st === 'down' || n.latency_ms == null) return T.html`<span class="tiers-dash">—</span>`;
    return uiLat(n.latency_ms, { ceiling: ceil() });
  }
  /* the library's latency (band colour) with its value in the language's number-and-unit form
     (F.ms): the core writes a literal 「N ms」, which is replaced as text, never as markup */
  function uiLat(ms, o) {
    const node = rawN(T.ui.lat(ms, o));
    const w = document.createTreeWalker(node, NodeFilter.SHOW_TEXT);
    for (let t = w.nextNode(); t; t = w.nextNode()) { const m = /^(\d+) ms$/.exec(t.nodeValue); if (m) t.nodeValue = F.ms(Number(m[1])); }
    return node;
  }
  function paintLat(cell, n) {
    if (!cell) return;
    const node = latNode(n), k = keyOf(node);
    if (cell._h === k) return;
    cell._h = k; cell.replaceChildren(node);
  }
  function paintSt(cell, n, instant) {
    const s = stateOf(n), h = keyRaw(stTag(s));
    if (cell._k === h) return;
    cell._k = h;
    swapNode(cell, rawN(stTag(s)), { instant: instant || !visible });
  }
  function updateNode(r, e) {
    const n = e.n, q = s => r.querySelector(s), fresh = !r._init;
    const st = stateOf(n);
    r.classList.toggle('is-off', st === 'draft');
    const was = r.classList.contains('is-cur'); r.classList.toggle('is-cur', !!e.cur);
    if (e.cur && !was && !fresh && visible) M.anim(q('.tiers-cur'), [{ opacity: 0, transform: 'scale(.6)' }, { opacity: 1, transform: 'none' }], { duration: M.SP.press.duration, easing: M.SP.press.easing });
    setT(q('.tiers-name'), n.name);
    setD(q('.tiers-sub'), subNode(n, e));
    setT(q('.tiers-proto'), n.protocol || '—');
    paintLat(q('.tiers-lat'), n);
    paintSt(q('.tiers-st'), n, fresh);
    const k = actKind(n, e); if (r._act !== k) { r._act = k; q('.li-act').replaceChildren(...actNodes(k)); r._tabs = null; }
    r.setAttribute('aria-label', L('tiers.row.aria', { name: n.name, last: lastKnown(st) ? 'yes' : 'no', state: st || '', cur: e.cur ? 'yes' : 'no' }));
    r._e = e; r._init = true;
  }
  /* a subscription row: name, node count, health (G35 visibility, G17 disabled), the latest manual
     update's result (B-07 fallback), the tier's refresh time (K-6, tier granularity). groupSub is
     the subscription's state in words (the side panel's 健康 row; null when the head's health chip
     says it all); groupLine is the row's second line, what is happening to it (F-01: the row no longer
     repeats its node count, beside its name, or its health words, in its health column) */
  function groupSub(g) {
    const res = V.results.get(g.id), busy = V.busySubs && V.busySubs.has(g.id);
    if (busy || g.refreshing) return fragN(hN('span', 'spin'), L('tiers.sub.job.updating'));
    if (g.enabled === false) return hN('span', 'tiers-why-mute', L('tiers.sub.disabled'));
    /* the latest manual update's result shows once, also on a degraded row (section 4.3) */
    if (res) return resultLine(res);
    if (g.degraded) return fragN(L('tiers.sub.degraded'));
    if (g.recheck && Number.isInteger(g.recheck.total)) return fragN(L('tiers.drill.rechecking', { done: g.recheck.done, total: g.recheck.total }));
    const h = P.subscriptionHealth(g);
    if (h === 'pending') return fragN(L('tiers.sub.pending'));
    if (h === 'unknown') return fragN(L('tiers.sub.unknown'));
    if (h === 'previous') return fragN(L('tiers.sub.previous'));
    return null;
  }
  function groupLine(g) {
    const res = V.results.get(g.id), busy = V.busySubs && V.busySubs.has(g.id);
    if (busy || g.refreshing) return fragN(hN('span', 'spin'), L('tiers.sub.job.updating'));
    if (res) return resultLine(res);
    if (g.degraded) return fragN(L('tiers.sub.degraded'));
    if (g.recheck && Number.isInteger(g.recheck.total)) return fragN(L('tiers.drill.rechecking', { done: g.recheck.done, total: g.recheck.total }));
    return fragN();
  }
  /* a refresh result in one line (G35's four results and the refusals) */
  function resultLine(res) {
    const bad = res.kind === 'failed' || res.kind === 'kept_previous' || res.kind === 'stored_unapplied';
    return hN('span', bad ? 'tiers-why' : 'tiers-ok-t', L('tiers.refresh.result', { kind: res.kind }));
  }
  function updateGroup(r, e) {
    const q = s => r.querySelector(s), gm = q('.tiers-gm'), g = e.g;
    setT(q('.tiers-gn'), g.name);
    const gc = q('.tiers-gc'); gc.hidden = g.total == null; setT(gc, g.total == null ? '' : L('tiers.sub.count', { n: g.total }));
    setD(q('.tiers-gsub'), groupLine(g));
    const h = P.subscriptionHealth(g);
    if (h === 'disabled') setRaw(q('.tiers-ght'), T.ui.status('off', { label: L('tiers.sub.disabled_short') }));
    else if (h === 'unknown') setRaw(q('.tiers-ght'), T.ui.status('pending'));
    else if (h === 'pending') setRaw(q('.tiers-ght'), T.ui.status('recheck'));
    // (T-P5-11 fix round 1: a space between the count and its note is the line's break
    // opportunity; without it "disponibles" + "Précédent" was one unbreakable word that pushed the
    // cell into the name column in es / fr / pt)
    else setD(q('.tiers-ght'), fragN(T.tx('tiers.sub.health', { good: g.healthy || 0, total: g.total || 0 }), h === 'previous' ? ' ' : null, h === 'previous' ? hN('span', 'tiers-note', L('tiers.sub.previous_short')) : null));
    const busy = !!(g.refreshing || (V.busySubs && V.busySubs.has(g.id)));
    gm.hidden = h === 'disabled' || h === 'unknown';
    gm.style.setProperty('--v', (g.total ? (g.healthy || 0) / g.total : 0).toFixed(3)); gm.classList.toggle('warn', !busy && g.total > 0 && (g.healthy || 0) / g.total < 0.5);
    gm.classList.toggle('ink', !running() || h === 'previous');
    /* K-6: one refresh time per tier (Sentinel and Gateway only); absent: nothing */
    setT(q('.tiers-gup'), Number.isInteger(g.last_refresh_at_ms) ? L('tiers.sub.updated', { ago: F.ago(g.last_refresh_at_ms) }) : '');
    r.classList.toggle('is-busy', busy);
    r.classList.toggle('is-off', g.enabled === false);
    /* B-06's proportional progress stays behind its feature; v1: an indeterminate bar while updating */
    q('.tiers-gprog').classList.toggle('is-pending', busy);
    r.setAttribute('aria-label', L('tiers.sub.aria', { name: g.name, total: g.total || 0, good: g.healthy || 0, drill: membership(g.tier) ? 'yes' : 'no' }));
    r._e = e;
  }
  function updateLabel(r, e) {
    /* a section label is its name and count (F-01: the hints beside them explained the section) */
    const key = JSON.stringify([e.text, e.count, e.caps, e.act]);
    if (r._h === key) { r._e = e; return; }
    r._h = key;
    const kids = [hN('span', 'tiers-lbl-t', e.text)];
    if (e.count != null) kids.push(hN('span', 'tiers-lbl-n', String(e.count)));
    /* a section of subscriptions gets its own captions over the group rows' columns */
    if (e.caps) { const cap = hN('span', 'tiers-lbl-cap', tiEl('span', '', 'tiers.list.cap.health'), tiEl('span', 'tiers-lbl-cap-u', 'tiers.list.cap.updated')); cap.setAttribute('aria-hidden', 'true'); kids.push(cap); }
    /* 停用 Gateway is rare and asks first: a quiet link at the section's end */
    if (e.act === 'off') kids.push(btnN('link-btn tiers-lbl-a', 'tiers.gateway.off_link', { data: { a: 'gwoff' } }));
    if (e.act === 'updall') kids.push(btnN('btn btn-xs tiers-lbl-a', 'tiers.sub.update_all', { icon: 'refresh', size: 13, data: { a: 'updall' } }));
    r.replaceChildren(...kids);
    r._e = e;
  }
  function updateEl(r, e) {
    if (e.kind === 'node') updateNode(r, e);
    else if (e.kind === 'group') updateGroup(r, e);
    else if (e.kind === 'label') updateLabel(r, e);
    else if (e.kind === 'off') { paintOff(r, e); r._e = e; }
    else r._e = e;
  }
  /* the list keeps what it showed while a fresh snapshot is on its way (paging, a base_rev mismatch,
     a gap): a thin line says the data is not up to date (12.3) */
  function paintStale() {
    const s = R.staleEl; if (!s) return;
    const t = V.drill && V.drill.kind === 'pool' ? 'bastion' : V.tier;
    const on = DS.stale(t) && DS.nodes(t) != null;
    if (s.hidden === !on) return;
    s.hidden = !on;
    if (on && !s.firstChild) s.append(hN('span', 'prog sm is-pending tiers-stale-p', el('i')), tiEl('span', 'tiers-note', 'tiers.list.stale'));
  }
  function renderList(animate, o) {
    o = o || {};
    if (R.poolEl) paintPool();
    paintStale();
    const list = R.list; if (!list) return;
    let ents = entriesFor();
    if (!ents.some(e => e.kind !== 'label') && V.q) ents = [{ key: 'empty', kind: 'empty' }];
    else if (V.q && !ents.some(e => e.kind === 'node' || e.kind === 'group')) ents = [{ key: 'empty', kind: 'empty' }];
    /* value-only updates keep the order on screen (nothing re-sorts under the cursor): when the
       same rows are shown, they stay where they are */
    if (o.keepOrder && list._rows) {
      const now = [...list.children].filter(x => x._kind && !x._ghost).map(x => x.dataset.key);
      const want = ents.map(e => e.key);
      if (now.length === want.length && want.every(k => now.includes(k))) { const at = new Map(ents.map(e => [e.key, e])); ents = now.map(k => at.get(k)); }
    }
    const head = ents.some(e => e.kind === 'node');
    R.head.hidden = !head; R.scroll.classList.toggle('has-head', head);
    const focused = document.activeElement && list.contains(document.activeElement) ? document.activeElement : null;
    const fresh = V.fresh;
    const mutate = () => {
      const cache = list._rows || (list._rows = new Map()), keep = new Set();
      const els = ents.map(e => {
        let r = cache.get(e.key);
        if (!r || r._kind !== e.kind) { if (r) r.remove(); r = makeEl(e); cache.set(e.key, r); }
        updateEl(r, e); keep.add(e.key); return r;
      });
      cache.forEach((r, k) => { if (!keep.has(k)) { r.remove(); cache.delete(k); } });
      let ref = list.firstElementChild;
      while (ref && (ref.classList.contains('list-hl') || ref._ghost)) ref = ref.nextElementSibling;
      els.forEach(r => { if (r === ref) { ref = ref.nextElementSibling; while (ref && ref._ghost) ref = ref.nextElementSibling; } else list.insertBefore(r, ref); });
    };
    if (animate && visible && !M.reduced) M.list(list, mutate, { highlight: o.highlight, from: o.from, stagger: 18, max: 10 });
    else mutate();
    /* write-after-read: the rows an import added (ids by difference, never the batch tokens) glow once */
    if (fresh && fresh.size) {
      V.fresh = null;
      fresh.forEach(k => { const r = list._rows.get(k); if (r) { r.classList.remove('is-new'); void r.offsetWidth; r.classList.add('is-new'); setTimeout(() => r.classList.remove('is-new'), 1700); } });
      const first = [...fresh].map(k => list._rows.get(k)).find(Boolean);
      if (first) first.scrollIntoView({ block: 'nearest' });
    }
    if (focused && focused.isConnected && document.activeElement !== focused) focused.focus({ preventScroll: true });
    navRefresh();
    reserveUse();
    if (V.q) R.search.count(ents.filter(e => e.kind === 'node').length, searchDomain().length);
    if (R.bar) paintBar();
  }
  /* the keyboard model (tab stop, highlight) re-reads row geometry: once per frame is enough */
  let navRaf = 0;
  function navRefresh() {
    if (!R.nav || navRaf) return;
    navRaf = requestAnimationFrame(() => {
      navRaf = 0; if (!R.nav) return;
      const c = R.nav.get();
      if (c && !c.isConnected && !(R.list && R.list._rows && R.list._rows.has(c.dataset.key))) R.nav.clear();
      else R.nav.refresh();
    });
  }
  /* 设为当前 → 应用中 → 已切换: the row button keeps one width for all its faces */
  function reserveUse() {
    if (!R.list || !visible) return;
    T.$$('.tiers-use', R.list).forEach(b => { if (!b._rsv && b.offsetWidth) { b._rsv = true; T.ui.reserve(b, [L('tiers.row.use'), L('tiers.row.use_busy'), L('tiers.row.use_done')]); } });
  }
  function rowOf(key) { return R.list && R.list._rows ? R.list._rows.get(key) : null; }
  function patchRows() {
    const list = R.list; if (!list || !list._rows) return;
    list._rows.forEach(r => { if (r._e) updateEl(r, r._e); });
  }

  /* ================================================================ list wiring */
  function wireList(list, sc) {
    /* Tab order: the list is one stop (its selected row) plus that row's own actions */
    R.nav = T.ui.listNav(list, {
      scroller: sc, actions: '.li-act [data-a], .tiers-offrow-a [data-a]',
      onSelect: (row, how) => { if (how === 'key' && V.panel && !V.panel.closed && row.dataset.kind === 'node') followPanel(row); },
      onOpen: row => openRow(row, true),
      onEscape: () => { if (V.drill) drillOut(true, true); },
    });
    list.addEventListener('click', e => {
      const a = e.target.closest('[data-a]');
      if (a && list.contains(a)) { rowAction(a.dataset.a, e.target.closest('.li, .tiers-offrow'), a); return; }
      const row = e.target.closest('.li');
      if (!row || e.target.closest('button, a, input')) return;
      openRow(row, false);
    });
    /* the row menu from the keyboard: the Menu key or Shift + F10 on a focused row */
    list.addEventListener('keydown', e => {
      if (!(e.key === 'ContextMenu' || (e.shiftKey && e.key === 'F10'))) return;
      const row = e.target.closest('.li'); if (!row || e.target !== row) return;
      e.preventDefault(); R.nav.select(row);
      rowMenu(row.querySelector('[data-a="more"]') || row, row);
    });
    list.addEventListener('contextmenu', e => {
      const row = e.target.closest('.li'); if (!row || !row.dataset.kind) return;
      e.preventDefault(); R.nav.select(row);
      rowMenu({ left: e.clientX, top: e.clientY, right: e.clientX, bottom: e.clientY, width: 0, height: 0 }, row);
    });
  }
  function openRow(row, viaKey) {
    const k = row.dataset.kind, key = row.dataset.key;
    if (k === 'group') { const g = findSub(key.slice(2)); if (g && membership(g.tier) && !g.degraded) drillIn('sub', g.id, row, viaKey); else openPanel(key, { viaKey }); }
    else if (k === 'node') openPanel(key, { viaKey });
  }
  function followPanel(row) {
    const rows = T.$$('.li', R.list), prev = rowOf(V.pkey);
    const dir = prev ? Math.sign(rows.indexOf(row) - rows.indexOf(prev)) : 0;
    openPanel(row.dataset.key, { dir });
  }
  function rowAction(a, row, btn) {
    const e = row && row._e;
    if (a === 'gwoff') return askGatewayOff(btn);
    if (a === 'gwpick') { const r = R.list && R.list.querySelector('.tiers-row'); if (r && R.nav) R.nav.select(r, { focus: true }); return null; }
    if (a === 'updall') return null;   // B-06: kept behind its feature; its command comes with the H item
    if (!e) return null;
    if (a === 'diag' && e.n) return diagFromRow(e.n);
    if (a === 'use' && e.n) return useGateway(e.n, btn);
    if (a === 'gupd' && e.g) return refreshSub(e.g, btn);
    if (a === 'more') { R.nav.select(row); return rowMenu(btn, row); }
    return null;
  }
  function rowMenu(anchor, row) {
    const k = row.dataset.kind, key = row.dataset.key;
    if (k === 'group') {
      const g = findSub(key.slice(2)); if (!g) return null;
      const items = [];
      if (membership(g.tier) && !g.degraded) items.push({ label: L('tiers.menu.view_nodes'), icon: 'list', run: () => drillIn('sub', g.id, row) });
      items.push({ label: L('tiers.sub.update'), icon: 'refresh', run: () => refreshSub(g, row.querySelector('[data-a="gupd"]')) });
      items.push({ label: L('tiers.sub.manage'), icon: 'edit', run: () => manageSub(g, row) });
      items.push({ sep: true }, { label: L('tiers.sub.delete'), icon: 'trash', danger: true, run: () => askDeleteSub(g, null) });
      return T.ui.menu(anchor, items, { placement: 'bottom-end' });
    }
    const n = findRow(key); if (!n) return null;
    const ro = roOf(n), gw = n.tier === 'gateway', draft = stateOf(n) === 'draft';
    const items = [];
    if (!draft && (n.kind !== 'automatic' || T.rt.feature('B-12'))) items.push({ label: L('tiers.menu.diagnose'), icon: 'pulse', run: () => diagFromRow(n) });
    if (gw && !draft && !isCur(n, 'gateway')) items.push({ label: L('tiers.row.use_aria'), icon: 'target', run: () => useGateway(n, T.$$('[data-a="use"]', row).find(b => b.offsetParent) || null) });
    if (!ro) {
      items.push({ sep: true });
      /* each acts once the panel shows this node: a half-finished edit elsewhere asks first, once */
      if (n.kind === 'manual' && !n.fromConfig) items.push({ label: L('tiers.menu.edit'), icon: 'edit', run: () => openPanel(n.id, { then: () => startEdit(n) }) });
      items.push({ label: L('tiers.menu.toggle', { to: draft ? (gw ? 'candidate' : 'fallback') : 'draft' }), icon: draft ? 'play' : 'pause', run: () => openPanel(n.id, { then: () => toggleFromPanel(n, draft) }) });
      items.push({ sep: true }, { label: L('tiers.menu.delete'), icon: 'trash', danger: true, run: () => openPanel(n.id, { then: () => askDelete(n, null) }) });
    }
    if (!items.length) items.push({ label: L('tiers.menu.details'), icon: 'info', run: () => openPanel(n.id) });
    return T.ui.menu(anchor, items.filter((x, i, a) => !(x.sep && (i === 0 || a[i - 1].sep))), { placement: 'bottom-end' });
  }

  /* ================================================================ drill-in (subscriptions with K-4 members, the pool) */
  function flyText(src, dst) {
    if (!src || !dst || M.reduced || !visible) return;
    const a = src.getBoundingClientRect(), b = dst.getBoundingClientRect(); if (!a.width || !b.width) return;
    const cs = getComputedStyle(src);
    const g = src.cloneNode(true); g.removeAttribute('id'); g.setAttribute('aria-hidden', 'true'); g.className = 'tiers-fly';
    Object.assign(g.style, { left: a.left + 'px', top: a.top + 'px', font: cs.font, color: cs.color, letterSpacing: cs.letterSpacing });
    (T.$('#layer') || document.body).append(g);
    const s = b.height / (a.height || 1), D0 = 400;
    const mv = g.animate([{ transform: 'none' }, { transform: `translate(${b.left - a.left}px, ${b.top - a.top}px) scale(${s.toFixed(3)})` }], { duration: D0, easing: M.SP.settle.easing, fill: 'forwards' });
    g.animate([{ opacity: 1 }, { opacity: 1, offset: 0.62 }, { opacity: 0 }], { duration: D0, easing: 'linear', fill: 'forwards' });
    dst.animate([{ opacity: 0 }, { opacity: 0, offset: 0.55 }, { opacity: 1 }], { duration: D0, easing: 'linear' });
    M.onDone(mv, () => g.remove(), D0 + 300);
  }
  function drillIn(kind, id, row, viaKey) {
    if (kind === 'sub' && !findSub(id)) return;
    closePanel();
    const src = row && row.querySelector('.tiers-gn, .tiers-pool-t');
    V.drill = { kind, id }; clearSearch();
    syncToolbar(true); syncCtx(true);
    swapView(1, { animate: true, rows: true });
    flyText(src, R.view.querySelector('.tiers-bar-t b'));
    if (viaKey && R.nav) requestAnimationFrame(() => { const r = R.list && R.list.querySelector('.li'); if (r) R.nav.select(r, { focus: true }); });
  }
  function drillOut(animate, viaKey) {
    const d = V.drill; if (!d) return;
    const src = R.view && R.view.querySelector('.tiers-bar-t b');
    closePanel();
    V.drill = null; clearSearch();
    syncToolbar(animate); syncCtx(animate);
    swapView(-1, { animate, rows: false });
    if (d.kind === 'pool') {
      if (R.poolEl) { if (animate) flyText(src, R.poolEl.querySelector('.tiers-pool-t')); if (viaKey) { const go = R.poolEl.querySelector('[data-pp="go"]'); if (go) go.focus({ preventScroll: true }); } }
      return;
    }
    const row = rowOf('g:' + d.id);
    if (row) { if (animate) flyText(src, row.querySelector('.tiers-gn')); R.nav.select(row, { focus: !!viaKey }); }
  }
  function drillBar() {
    const d = V.drill, back = D.tier(V.tier).name;
    const bar = hN('div', 'tiers-bar'), err = hN('div', 'tiers-bar-err');
    const backBtn = T.html`<button class="btn btn-sm btn-ghost tiers-back" type="button" data-b="back" aria-label="${L('tiers.drill.back', { tier: back })}" data-tip="${L('tiers.drill.back', { tier: back })}" data-tip-kbd="Esc"><span class="bi">${I('chevron-left', 15)}</span><span class="lab" translate="no">${back}</span></button>`;
    const sep = hN('span', 'toolbar-sep'); sep.setAttribute('aria-hidden', 'true');
    const prog = hN('span', 'prog sm tiers-bar-prog', el('i')); prog.dataset.b = 'prog';
    const meta = hN('span', 'tiers-bar-m'); meta.dataset.b = 'meta';
    if (d.kind === 'pool') {
      const title = hN('span', 'tiers-bar-t', iconN('pool', 15), tiEl('b', '', 'tiers.pool.title'));
      bar.append(backBtn, sep, title, meta, hN('span', 'spacer'));
      /* B-13 (refill now): kept behind its feature; its command is wired with the H item and goes
         through T.rt.call, whose jobs always reach a terminal (02b-8 A-S5d) */
      if (T.rt.feature('B-13')) bar.append(btnN('btn btn-sm', 'tiers.pool.fill', { icon: 'refresh', data: { b: 'fill' } }));
      bar.append(prog);
    } else {
      const g = findSub(d.id);
      const title = hN('span', 'tiers-bar-t', iconN('stack', 15), dataEl(g ? g.name : '', '', 'b'));
      bar.append(backBtn, sep, title, meta, hN('span', 'spacer'),
        btnN('btn btn-sm', 'tiers.sub.update', { icon: 'refresh', data: { b: 'upd' } }), btnN('btn btn-sm btn-ghost', 'tiers.sub.manage', { data: { b: 'mng' } }), prog);
    }
    bar.addEventListener('click', e => {
      const b = e.target.closest('[data-b]'); if (!b) return;
      const g = findSub(d.id);
      if (b.dataset.b === 'back') drillOut(true, e.detail === 0);
      else if (b.dataset.b === 'upd' && g) refreshSub(g, b);
      else if (b.dataset.b === 'mng' && g) manageSub(g, b);
      else if (b.dataset.b === 'fill') refillPool(b);
    });
    R.bar = bar; R.barErr = err;
    return [bar, err];
  }
  function reserveBar() {
    if (!R.bar) return;
    const u = R.bar.querySelector('[data-b="upd"]');
    if (u) T.ui.reserve(u, [L('tiers.sub.busy.updating'), L('tiers.sub.busy.updated'), L('tiers.sub.busy.failed')]);
  }
  function paintBar() {
    if (!R.bar || !V.drill) return;
    const meta = R.bar.querySelector('[data-b="meta"]'), prog = R.bar.querySelector('[data-b="prog"]');
    if (V.drill.kind === 'pool') {
      const pv = P.poolView(DS.pool(), { enabled: bastionOn(), run: run(), carrier: S.get('carrier') });
      setT(meta, poolBarLine(pv));
      R.bar.classList.toggle('is-busy', pv.phase === 'filling');
      prog.classList.toggle('is-pending', false);
      prog.style.setProperty('--p', pv.known && pv.capacity ? Math.min(1, pv.ok / pv.capacity).toFixed(3) : '0');
      return;
    }
    const g = findSub(V.drill.id); if (!g) return;
    const busy = !!(g.refreshing || (V.busySubs && V.busySubs.has(g.id)));
    const total = P.membersOf(g.id, DS.nodes(g.tier)).length;
    /* one sentence: N nodes [· updated X ago] (K-6 tier time) or the recheck progress (K-6) */
    setT(meta, busy ? L('tiers.sub.job.updating') : g.recheck && Number.isInteger(g.recheck.total) ? L('tiers.drill.rechecking', { done: g.recheck.done, total: g.recheck.total })
      : L('tiers.drill.meta', { total, has_ago: Number.isInteger(g.last_refresh_at_ms) ? 'yes' : 'no', ago: Number.isInteger(g.last_refresh_at_ms) ? F.ago(g.last_refresh_at_ms) : '' }));
    R.bar.classList.toggle('is-busy', busy || !!g.recheck);
    prog.classList.toggle('is-pending', busy);
    prog.style.setProperty('--p', g.recheck && g.recheck.total ? (g.recheck.done / g.recheck.total).toFixed(3) : '0');
    paintResultCard(R.barErr, V.results.get(g.id), true);
  }
  /* the latest manual update's failure (B-07's fallback: no history), above the list or in a drawer */
  function paintResultCard(box, res, flip) {
    if (!box) return;
    const want = res && (res.kind === 'failed' || res.kind === 'kept_previous' || res.kind === 'stored_unapplied') ? res.kind + '\u0001' + (res.error ? errText(res.error) : '') : '';
    if (box._s === want) return;
    box._s = want;
    const put = () => {
      box.replaceChildren();
      if (want) box.append(T.ui.callout({ kind: res.kind === 'stored_unapplied' ? 'warning' : 'error', small: true, title: L('tiers.refresh.result', { kind: res.kind }), message: res.error ? errText(res.error) : '' }));
    };
    if (flip && visible && !M.reduced && R.scroll) M.flip([R.scroll], put); else put();
  }

  /* ================================================================ Bastion */
  /* the drill bar's meta line (the pool panel is not on screen there) */
  function poolBarLine(pv) {
    if (pv.phase === 'off') return L('tiers.pool.bar.off');
    if (!pv.known) return L('tiers.pool.bar.unknown');
    if (pv.phase === 'idle') return L('tiers.pool.bar.stopped', { min: pv.min });
    return L('tiers.pool.bar.meta', { phase: DS.poolStale() ? 'stale' : pv.phase === 'filling' ? 'filling' : 'ready', ok: pv.ok, total: pv.total != null ? pv.total : pv.ok, min: pv.min });
  }
  /* the line under the pool's figure: the fastest automatic node (its name: data; no region, no time:
     B-03 / K items). F-01: while the pool is unknown, idle, filling or below its minimum the line is
     empty (it explained the state the pill beside the title already says) */
  function poolDetail(pv) {
    if (!pv.known || pv.phase === 'idle' || pv.phase === 'filling' || pv.phase === 'low') return fragN();
    const f = poolRows().filter(n => (stateOf(n) === 'ok' || stateOf(n) === 'slow') && n.latency_ms != null).sort(byLat)[0];
    return hN('span', 'tnum', tx('tiers.pool.detail.ready', { fastest: f ? 'yes' : 'no', name: f ? f.name : '', ms: f ? f.latency_ms : 0 }));
  }
  /* the pool's one home on this page: figure, meter and state, the way in; K-7 (when config.snapshot
     carries the bastion section): the minimum and 停用. Without K-7 the minimum is read-only and the
     switch is not offered (the settings page cannot write it either: no hint to go there) */
  function poolPanel() {
    const pv = P.poolView(DS.pool(), { enabled: true, run: run(), carrier: S.get('carrier') });
    const s = T.html`<section class="tiers-pool" aria-label="${L('tiers.pool.aria')}" data-i18n-attr="aria-label:tiers.pool.aria">
      <div class="tiers-pool-l">
        <div class="tiers-pool-hd"><span class="tiers-pool-t" data-i18n="tiers.pool.title">${L('tiers.pool.title')}</span><span class="pill" data-pp="pill"><span class="sdot"></span><span data-pp="pt"></span></span></div>
        <div class="tiers-pool-fig"><span class="tiers-pool-n" data-pp="n"></span><span class="tiers-pool-of" data-pp="of"></span>
          <button class="link-btn tiers-pool-go" type="button" data-pp="go"><span data-pp="gol"></span>${I('chevron', 14)}</button></div>
        <span class="meter lg tiers-pool-m" data-pp="m"></span>
        <div class="tiers-pool-line" data-pp="line"></div>
      </div>
      <div class="tiers-pool-r" data-pp="r"></div></section>`;
    R.poolEl = s;
    R.poolM = T.ui.meter(s.querySelector('[data-pp="m"]'), { value: 0, max: pv.capacity || 1, min: pv.min || 0 });
    s.querySelector('[data-pp="go"]').addEventListener('click', e => drillIn('pool', 'pool', s, e.detail === 0));
    paintPoolControls();
    return s;
  }
  /* the right column: the minimum (input with K-7, else a read-only figure) and 停用 (K-7) */
  function paintPoolControls() {
    const box = R.poolEl && R.poolEl.querySelector('[data-pp="r"]'); if (!box) return;
    const w = bastionWritable(), pool = DS.pool(), min = V.cfg && V.cfg.bastion ? V.cfg.bastion.min_alive_nodes : pool ? pool.min_alive_nodes : null;
    const key = (w ? 'w' : 'r') + '\u0001' + min + '\u0001' + T.i18n.lang;
    if (box._h === key && !(w && box.contains(document.activeElement))) return;
    if (box.contains(document.activeElement)) return;   // being typed in: the value is the user's
    box._h = key;
    if (w) {
      box.replaceChildren(minField('tiers-pool-min', min, true), btnN('link-btn tiers-pool-off', 'tiers.pool.disable', { data: { pp: 'off' } }));
      box.querySelector('[data-pp="off"]').addEventListener('click', e => disableBastion(e.currentTarget));
    } else box.replaceChildren(minReadOnly(min));
  }
  const minReadOnly = min => hN('div', 'tiers-pool-ro', tiEl('span', 'field-lab', 'tiers.pool.min.label'),
    hN('b', 'tnum', min == null ? '—' : L('tiers.pool.min.value', { n: min })));
  /* 最少可用节点: settings.apply section bastion (K-7), one write per change (debounced); 1 – capacity (D9) */
  function minField(cls, min, small) {
    const cap = () => { const p = DS.pool(); return p && p.capacity > 0 ? p.capacity : null; };
    const f = T.html`<div class="field ${cls}"><label class="field-lab" data-i18n="tiers.pool.min.label">${L('tiers.pool.min.label')}</label><div class="input num${small ? ' sm' : ''}"><input inputmode="numeric" aria-label="${L('tiers.pool.min.label')}" data-i18n-attr="aria-label:tiers.pool.min.label"><span class="input-unit" data-i18n="tiers.pool.min.unit">${L('tiers.pool.min.unit')}</span></div><div class="field-msg"></div></div>`;
    const input = f.querySelector('input');
    input.value = min == null ? '' : String(min);
    const fld = T.ui.field(f, { validate: v => { const c = cap(); const s = String(v).trim(); return !/^\d+$/.test(s) || +s < 1 || (c != null && +s > c) ? L('tiers.pool.min.invalid', { max: c || 1, has_max: c != null ? 'yes' : 'no' }) : null; } });
    T.ui.number(f.querySelector('.input.num'), { min: 1, max: cap() || 9999, onChange: () => later() });
    let t = 0;
    const later = () => { clearTimeout(t); t = setTimeout(() => { if (fld.validate()) writeBastion({ min_alive_nodes: Math.round(+input.value) }, null, fld); }, 600); };
    input.addEventListener('input', later);
    return f;
  }
  /* settings.apply{base_rev, changes:[{section:'bastion', values}]} (K-7). A hold while the service
     starts (D-sem 3), a stale rev (conflict: the snapshot is read again), a validation on the field. */
  function writeBastion(values, btn, fld) {
    if (!bastionWritable()) return Promise.reject(new T.rt.RtError('not_allowed'));
    const cur = V.cfg.bastion || {};
    if (Object.keys(values).every(k => cur[k] === values[k])) return Promise.resolve(null);
    const job = T.rt.call('settings.apply', { base_rev: V.cfg.rev, changes: [{ section: 'bastion', values }] }).then(r => {
      V.cfg = { ...V.cfg, rev: r && Number.isInteger(r.rev) ? r.rev : V.cfg.rev, bastion: { ...cur, ...values } };
      loadConfig();
      return r;
    }, err => {
      if (err && err.code === 'conflict') loadConfig();
      const sec = err && err.result && Array.isArray(err.result.sections) ? err.result.sections.find(x => x.section === 'bastion') : null;
      const e2 = (sec && sec.error) || err;
      if (fld && e2 && e2.field === 'bastion.min_alive_nodes') fld.error(errText(e2));
      throw e2;
    });
    if (btn) return T.ui.busy(btn, job, { busy: L(values.enabled === false ? 'tiers.busy.disabling' : 'tiers.busy.enabling'), done: L(values.enabled === false ? 'tiers.busy.disabled' : 'tiers.busy.enabled'), fail: e => errText(e), hold: 600 });
    job.catch(e => { if (!(fld && e && e.field === 'bastion.min_alive_nodes')) T.notify({ kind: 'warning', id: 'tiers-bastion', message: errText(e), timeout: 6000 }); });
    return job;
  }
  function paintPool() {
    const s = R.poolEl; if (!s || !s.isConnected) return;
    const pv = P.poolView(DS.pool(), { enabled: bastionOn(), run: run(), carrier: S.get('carrier') });
    const q = k => s.querySelector(`[data-pp="${k}"]`);
    const on = pv.known && pv.phase !== 'idle' && pv.phase !== 'off';
    /* a stale pool (no fresh bastion.pool while running) keeps its figures, dimmed, and its pill
       says 数据未更新 (as the overview deck marks the same numbers) */
    const old = on && DS.poolStale();
    s.classList.toggle('is-old', old);
    /* on standby the pool is neutral; amber only when it carries traffic or is below its minimum */
    const hot = !old && (pv.phase === 'low' || pv.phase === 'carrying');
    q('pill').className = 'pill ' + (hot ? 'pill-warn' : 'pill-line');
    q('pill').firstElementChild.className = 'sdot ' + (old ? 'off' : pv.phase === 'filling' ? 'busy' : hot ? 'warn' : pv.phase === 'ready' ? '' : 'off');
    M.swapText(q('pt'), old ? L('tiers.list.stale') : L('tiers.pool.state', { state: pv.known ? pv.phase : 'unknown' }), { instant: !visible });
    /* §8u: the auto pool's available/total (the target stays internal); without totals (older
       sidecar) the count alone */
    const frac = on && pv.total != null;
    M.odometer(q('n'), !on ? '—' : frac ? `${pv.ok}/${pv.total}` : String(pv.ok), { mode: 'auto',
      label: frac ? L('tiers.cell.count_aria', { ok: pv.ok, total: pv.total }) : L('tiers.pool.count_aria', { n: on ? pv.ok : 0 }), instant: !visible });
    setT(q('of'), pv.known ? L('tiers.pool.of') : '');
    if (R.poolM) R.poolM.set(on ? pv.ok : 0, pv.capacity || 1, pv.min || 0);
    q('m').classList.toggle('warn', hot);
    q('m').classList.toggle('ink', !hot);
    setD(q('line'), poolDetail(pv));
    /* the way into the pool's nodes counts the automatic rows actually listed (not the target) */
    setT(q('gol'), L('tiers.pool.view_all', { n: poolRows().length }));
    q('go').hidden = !poolRows().length;
    paintPoolControls();
  }
  function bastionOff() {
    /* 11-05: the card is its title, the minimum and 启用 only; the safety point is said once, in the
       confirmation before enabling (the on_confirm dialog) */
    const s = T.html`<section class="tiers-bx" aria-label="${L('tiers.bx.title')}" data-i18n-attr="aria-label:tiers.bx.title"><div class="tiers-bx-hd"><span class="tiers-bx-ic">${I('bastion', 22)}</span><div><h3 class="tiers-bx-t" data-i18n="tiers.bx.title">${L('tiers.bx.title')}</h3></div></div><div class="tiers-bx-ctl"></div></section>`;
    const ctl = s.querySelector('.tiers-bx-ctl');
    const pool = DS.pool(), min = V.cfg && V.cfg.bastion ? V.cfg.bastion.min_alive_nodes : pool ? pool.min_alive_nodes : null;
    if (bastionWritable()) {
      /* K-7 delivered: the minimum and 启用 (settings.apply section bastion) */
      const act = hN('div', 'tiers-bx-act', btnN('btn btn-primary', 'tiers.bx.enable', { icon: 'shield', size: 16, data: { bx: 'on' } }));
      ctl.append(minField('tiers-bx-min', min, false), act);
      act.querySelector('[data-bx="on"]').addEventListener('click', e => enableBastion(e.currentTarget));
    } else {
      /* K-7 absent: read-only (the switch is not offered here or on the settings page) */
      ctl.append(hN('div', 'tiers-bx-min', minReadOnly(min)), hN('p', 'tiers-bx-ro', iconN('lock', 13), tiEl('span', '', 'tiers.bx.readonly')));
    }
    return s;
  }
  function enableBastion(btn) {
    T.confirm({ title: L('tiers.bx.on_confirm.title'), message: L('tiers.bx.on_confirm.body'), confirmLabel: L('tiers.bx.on_confirm.ok') }).then(ok => {
      if (!ok) return;
      writeBastion({ enabled: true }, btn).then(() => setTimeout(() => {
        /* the view follows tiers.summary (bastion.enabled) when it says so */
        changed('bastion');
      }, 420), () => {});
    });
  }
  function disableBastion(btn) {
    T.confirm({ title: L('tiers.bx.off_confirm.title'), message: L('tiers.bx.off_confirm.body'), confirmLabel: L('tiers.action.disable'), danger: true }).then(ok => {
      if (!ok) return;
      writeBastion({ enabled: false }, btn).then(() => setTimeout(() => changed('bastion'), 380), () => {});
    });
  }
  /* B-13: kept behind its feature (no v1 command) */
  function refillPool(btn) {
    const cmd = 'bastion.refill';
    if (!T.rt.can(cmd)) return null;
    return T.ui.busy(btn, T.rt.call(cmd, {}), { busy: L('tiers.pool.busy.filling'), done: L('tiers.pool.busy.filled'), fail: e => errText(e) });
  }

  /* ================================================================ Gateway */
  /* gateway.select (K-10; T-P2-H5 delivered node_id, 5th-wave ruling C2 item 1): by node_id.
     spec.gen.json carries no argument shapes, so the choice cannot be read from it: node_id is
     sent, and only a sidecar that does not know node_id falls back to proxy_name, the name the
     sidecar's own list gives (the Gateway row's Node.name, copied as is). Such a sidecar answers
     missing_field (PROTOCOL section 17, T-P2-H5: the old schema's proxy_name is required); the
     design simulator answers validation on proxy_name. The new sidecar's own refusals
     (unknown_node, not_gateway_node, node_unusable: conflict) are never retried by name. */
  const byNameRetry = err => !!err && !(err.result && err.result.saved)
    && (err.code === 'missing_field' || (err.code === 'validation' && err.field === 'proxy_name' && !err.reason_code));
  function selectGateway(n) {
    return T.rt.call('gateway.select', { node_id: n.id }).catch(err => {
      if (byNameRetry(err)) return T.rt.call('gateway.select', { proxy_name: n.name });
      throw err;
    });
  }
  /* the result of a write in words: applied / next start / pending / unapplied (P4 contract memo) */
  const outcomeWord = (k, what) => L('tiers.outcome', { kind: k, what: what || 'saved' });
  function useGateway(n, btn) {
    if (btn && btn._busy) return btn._busy;
    const job = selectGateway(n).then(r => {
      const k = P.appliedOutcome(r, run());
      if (k !== 'applied') T.notify({ kind: k === 'unapplied' ? 'warning' : 'info', id: 'tiers-gw', message: outcomeWord(k, 'gateway'), timeout: 5000 });
      return k;
    });
    const p = btn ? T.ui.busy(btn, job, { busy: L('tiers.row.use_busy'), done: k => (k === 'applied' ? L('tiers.row.use_done') : outcomeWord(k, 'short')), fail: e => errText(P.detailError(e)), hold: 1000 })
      : job.catch(e => { T.notify({ kind: 'warning', id: 'tiers-gw', message: errText(P.detailError(e)), timeout: 6000 }); });
    p.then(() => changed('gateway'), () => {});
    return p;
  }
  function askGatewayOff(btn) {
    T.confirm({ title: L('tiers.gateway.off_confirm.title'), message: L('tiers.gateway.off_confirm.body'), confirmLabel: L('tiers.action.disable'), danger: true }).then(ok => {
      if (!ok) return;
      /* proxy_name '' turns Gateway off (T-P4-C2 #9, PROTOCOL section 15) */
      const job = T.rt.call('gateway.select', { proxy_name: '' });
      (btn ? T.ui.busy(btn, job, { busy: L('tiers.busy.disabling'), done: L('tiers.busy.disabled'), fail: e => errText(P.detailError(e)), hold: 700 }) : job)
        .then(() => changed('gateway'), e => { if (!btn) T.notify({ kind: 'warning', id: 'tiers-gw', message: errText(P.detailError(e)), timeout: 6000 }); });
    });
  }

  /* ================================================================ the side panel (docked, modeless) */
  // a narrow stage (the web build's fallback on a phone or a side panel): the panel takes all of it
  const PW = () => { const w = R && R.stage ? R.stage.offsetWidth : 0; return w && w < 800 ? w : innerWidth <= 1180 ? 364 : 396; };
  function placePanel() {
    if (!V.panel || V.panel.closed) return;
    V.panel.place();
    const w = PW();
    if (R.root.classList.contains('has-panel')) R.stage.style.paddingRight = w >= R.stage.offsetWidth ? '' : (w + 16) + 'px';
  }
  /* the list makes room for the panel: the optional columns fade out while it slides in, then the list
     narrows; latency / state glide if they have to move. Closing reverses it. */
  function inView(els) {
    if (!R.scroll) return [];
    const sr = R.scroll.getBoundingClientRect();
    return els.filter(e => { const r = e.getBoundingClientRect(); return r.height && r.bottom > sr.top && r.top < sr.bottom; });
  }
  function compact(on) {
    clearTimeout(R.cmpT);
    const root = R.root, anim = visible && !M.reduced;
    if (on === root.classList.contains('has-panel')) { root.classList.remove('is-compacting'); if (on) placePanel(); return; }
    const apply = () => {
      const mut = () => { root.classList.remove('is-compacting'); root.classList.toggle('has-panel', on); if (on) placePanel(); else { R.stage.style.paddingRight = ''; reserveUse(); } };
      if (!anim || !R.scroll) { mut(); return; }
      M.flip(inView(T.$$(`.tiers-head > :nth-child(2), .tiers-head > :nth-child(3), .tiers-row .tiers-lat, .tiers-row .tiers-st${on ? '' : ', .tiers-grp .tiers-gh'}`, R.scroll)), mut);
      if (on) inView(T.$$('.tiers-grp .tiers-gh', R.scroll)).forEach(e => M.anim(e, [{ opacity: 0 }, { opacity: 1 }], { duration: 200, delay: 60, easing: M.ease.out, fill: 'backwards' }));
      else inView(T.$$('.tiers-head > .c-opt, .tiers-row > .c-opt, .tiers-grp .tiers-gup, .tiers-grp .li-act', R.scroll))
        .forEach(e => M.anim(e, [{ opacity: 0 }, { opacity: 1 }], { duration: 200, delay: 80, easing: M.ease.out, fill: 'backwards' }));
    };
    if (on && anim) { root.classList.add('is-compacting'); R.cmpT = setTimeout(apply, 110); } else apply();
  }
  /* The panel's state for the item it shows (V.pr): one page owner per shown item (P4 architecture
     2.4, criticism B5). A diagnosis in flight registers its nodes.cancelDiagnose with the owner, so
     revoking it (the panel closes, shows another item, or the runtime generation changes) cancels the
     sidecar's diagnosis and drops the revealed values and late answers. */
  function newPr(it) {
    const pr = { key: it.kind === 'group' ? 'g:' + it.g.id : it.n.id, kind: it.kind, row: it.n || null, sub: it.g || null, detail: null, diag: null, secret: null, override: null, dropped: false, noCancel: false };
    pr.owner = T.rt.owner('tiers-panel', {
      onRevoke: () => (!pr.noCancel && pr.diag && pr.diag.running && !pr.diag.cancelling && T.rt.can('nodes.cancelDiagnose') ? [{ cmd: 'nodes.cancelDiagnose', args: { request_id: pr.diag.req } }] : []),
    });
    return pr;
  }
  function dropPr(pr, o) {
    if (!pr || pr.dropped) return;
    if (o && o.generation) pr.noCancel = true;   // the old sidecar is gone with its diagnosis
    pr.dropped = true;
    pr.secret = null;                           // revealed values live only here: gone with the panel
    if (pr.paramsEl) pr.paramsEl.replaceChildren();
    pr.owner.revoke();
  }
  function closePanel(o) {
    const pr = V.pr;
    /* the drawer's own onClose drops the owner first: a generation change is marked before it */
    if (pr && o && o.generation) pr.noCancel = true;
    if (V.panel && !V.panel.closed) { V.quiet = true; V.panel.close(); V.quiet = false; }
    dropPr(pr, o);
    endEdit();
    V.panel = null; V.pkey = null; V.pr = null;
  }
  function panelBtn(id) { return V.panel && !V.panel.closed ? T.$$('.ovl-foot .btn', V.panel.el).find(b => b.textContent.trim() === L(id)) : null; }
  function resolve(key) {
    if (key.startsWith('g:')) { const g = findSub(key.slice(2)); return g ? { kind: 'group', g } : null; }
    const n = findRow(key); return n ? { kind: 'node', n } : null;
  }
  /* o.then(pr): runs once the panel shows `key` (after a discard question was answered yes; never
     when it was answered no), so an action from a row menu acts on the item it was asked for */
  function openPanel(key, o) {
    o = o || {};
    const it = resolve(key); if (!it) return;
    const shown = () => { if (typeof o.then === 'function' && V.pr && V.pr.key === key && V.panel && !V.panel.closed) o.then(V.pr); };
    if (V.panel && !V.panel.closed) {
      /* Enter on the row whose panel is open moves the keyboard into the panel (Esc brings it back) */
      if (V.pkey === key && !o.force) { if (o.viaKey) focusPanel(); shown(); return; }
      if (V.edit && V.edit.dirty) {
        confirmDiscard().then(ok => { if (ok) openPanel(key, o); else { const r = rowOf(V.pkey); if (r && R.nav) R.nav.select(r); } });
        return;
      }
      endEdit(); V.pkey = key; fillPanel(it, o.dir || 0); shown(); return;
    }
    endEdit(); V.pkey = key;
    const pr = V.pr = newPr(it);
    const Ps = spec(it, pr);
    V.panel = T.drawer.open({
      modal: false, dock: () => R.stage, width: PW, class: 'tiers-panel', title: Ps.title, subtitle: Ps.sub, head: Ps.head, content: Ps.content, actions: Ps.actions,
      /* Esc and × ask before a half-finished edit is thrown away, like every other way out */
      beforeClose: () => (V.edit && V.edit.dirty ? confirmDiscard() : true),
      onClose: () => {
        const k = V.pkey, back = !V.quiet && document.activeElement && R.list && R.list.contains(document.activeElement);
        dropPr(V.pr); endEdit();
        V.panel = null; V.pkey = null; V.pr = null; compact(false);
        const r = back && k ? rowOf(k) : null;
        if (r && r.isConnected && R.nav) R.nav.select(r, { focus: true });
      },
    });
    /* the title is a node's or a subscription's name: data */
    const pt = V.panel.el.querySelector('.ovl-title'); if (pt) pt.setAttribute('translate', 'no');
    V.panel.el.setAttribute('aria-label', L('tiers.panel.aria', { name: Ps.title }));
    setSub(V.panel, Ps, true);
    placePanel(); compact(true);
    if (it.kind === 'node') loadDetail(pr);
    shown();
  }
  function focusPanel() {
    const p = V.panel; if (!p || p.closed) return;
    const f = T.$$('.ovl-page:not(.is-out) button:not([disabled]), .ovl-page:not(.is-out) input, .ovl-foot .btn', p.el).find(x => x.offsetParent && x.tabIndex >= 0);
    (f || p.el).focus({ preventScroll: true });
  }
  function fillPanel(it, dir) {
    const p = V.panel; if (!p || p.closed) return;
    const keep = V.pr && V.pr.key === (it.kind === 'group' ? 'g:' + it.g.id : it.n.id) ? V.pr : null;
    let pr = keep;
    if (!keep) { dropPr(V.pr); pr = V.pr = newPr(it); }
    else { pr.row = it.n || pr.row; pr.sub = it.g || pr.sub; }
    const Ps = spec(it, pr);
    p.setTitle(Ps.title); p.el.setAttribute('aria-label', L('tiers.panel.aria', { name: Ps.title })); setSub(p, Ps);
    p.head.replaceChildren(Ps.head); p.head._h = null;
    p.setContent(Ps.content, { dir }); p.setActions(Ps.actions);
    if (it.kind === 'node' && !keep) loadDetail(pr);
  }
  /* the panel's subtitle as nodes (its data parts stay text under translate="no") */
  function setSub(p, Ps, instant) {
    const e = p && p.el && p.el.querySelector('.ovl-sub'); if (!e) return;
    if (!Ps.subN) { p.setSubtitle(Ps.sub); return; }
    swapNode(e, Ps.subN, { instant: !!instant || !visible });
  }
  /* the panel follows the runtime: its item's row changed, went away (another generation, deleted) */
  function refreshPanel() {
    const pr = V.pr; if (!pr || !V.panel || V.panel.closed || V.edit) return;
    const it = resolve(pr.key);
    if (!it) { closePanel(); T.notify({ kind: 'info', id: 'tiers-gone', message: L('tiers.panel.gone') }); return; }
    if (it.kind === 'node') {
      const had = P.nodeReason(panelNode(pr)), was = stateOf(panelNode(pr));
      pr.row = it.n;
      if (P.nodeReason(panelNode(pr)) !== had || (stateOf(panelNode(pr)) === 'draft') !== (was === 'draft')) { fillPanel(it, 0); return; }
      paintPanelLive(pr);
    } else { pr.sub = it.g; fillPanel(it, 0); }
  }
  /* the node the panel shows: the list row, or the diagnosis' nodes.detail while it is the newer one */
  function panelNode(pr) {
    const live = findRow(pr.key) || pr.row;
    if (pr.override && (V.rev[pr.row.tier] || 0) <= pr.override.rev) return pr.override.node;
    return live;
  }
  /* Known limit (第五波 31b-19): nodes.detail answers from the sidecar's last nodes_latest, which only
     a live nodes.list subscription keeps fresh. The shell subscribes every page topic after hello,
     so in the app it is fresh; without that subscription the answer may be old, which is why the
     list row wins whenever it is newer (pickNewer, panelNode). Recorded for P5 in P5_交接. */
  function loadDetail(pr) {
    if (!pr.row || pr.row.fromConfig || !T.rt.can('nodes.detail')) return;
    T.rt.query('nodes.detail', { node_id: pr.row.id }, { owner: pr.owner }).then(d => {
      if (pr.dropped) return;
      pr.detail = d && typeof d === 'object' ? d : null;
      paintDetail(pr);
    }, err => {
      if (pr.dropped || !err || err.code === 'owner_revoked' || err.code === 'runtime_gone') return;
      /* the node id belongs to a runtime generation: an answer for an expired id closes the panel */
      if (err.code === 'conflict' || err.code === 'validation' || err.code === 'not_found') { closePanel(); T.notify({ kind: 'info', id: 'tiers-gone', message: L('tiers.panel.gone') }); }
    });
  }
  function spec(it, pr) {
    if (it.kind === 'group') return groupSpec(it.g, pr);
    const n = it.n, d = D.tier(n.tier), sub = n.subscription_id ? subName(n.tier, n.subscription_id) : null;
    /* the subtitle: the subscription's name is data (subN: the same as nodes) */
    const subN = sub ? dotJoin([tierLabel(n.tier), dataEl(sub)]) : n.kind === 'automatic' ? fragN(L('tiers.panel.sub_pool')) : fragN(d.name);
    return { title: n.name, sub: subN.textContent, subN, head: headNode(n), content: box => nodeBody(box, n, pr), actions: nodeActions(n, pr) };
  }
  const headNode = n => fragN(isCur(n) ? rawN(T.ui.status('current', { chip: true })) : null, rawN(stTag(stateOf(n), true)));
  function nodeActions(n, pr) {
    if (n.kind === 'automatic') return [];
    const draft = stateOf(n) === 'draft', gw = n.tier === 'gateway';
    if (n.kind === 'subscription') {
      const g = n.subscription_id ? findSub(n.subscription_id) : null;
      return (g ? [{ label: L('tiers.sub.manage'), keep: true, run: b => manageSub(g, b) }] : [])
        .concat(gw && !draft && !isCur(n) ? [{ label: L('tiers.row.use'), primary: true, keep: true, run: b => useGateway(n, b) }] : []);
    }
    const acts = [{ label: L('tiers.panel.delete'), danger: true, left: true, keep: true, run: b => askDelete(n, b) }];
    if (n.kind === 'manual' && !n.fromConfig) acts.push({ label: L('tiers.panel.edit'), keep: true, run: () => startEdit(n) });
    if (gw && !draft) {
      /* 停用 asks first (in-page), so it is a plain button: the footer keeps one destructive red (删除) */
      if (isCur(n)) acts.push({ label: L('tiers.action.disable'), keep: true, run: b => askGatewayOff(b) });
      else acts.push({ label: L('tiers.row.use'), primary: true, keep: true, run: b => useGateway(n, b) });
    }
    return acts;
  }
  /* kv rows: <dt> is a message (registered) or data; <dd> is built as nodes */
  // (T-P5-11 fix round 1: the value goes into a real <dd>; it used to be appended bare, so a row
  // with two value parts put the second one under the key column, and the dd rules never applied)
  const kvRow = (labelId, dd, cls) => { const w = hN('div', cls || ''); w.append(tiEl('dt', '', labelId), hN('dd', '', dd)); return w; };
  function nodeBody(box, n, pr) {
    const gw = n.tier === 'gateway', ro = roOf(n), draft = stateOf(n) === 'draft', r = P.nodeReason(n);
    const pn = hN('div', 'tiers-pn');
    /* F-01: no note on top any more: what a draft is (the head's status says 草稿), and a Bastion
       node's safety note, which the line under the switcher already says on the Bastion tab where
       this panel opens */
    const kv = hN('dl', 'kv lined tiers-kv');
    const q = {};
    const span = (k, cls, dnt) => { const s = dnt ? dataEl('', cls) : hN('span', cls || ''); q[k] = s; return s; };
    kv.append(kvRow('tiers.panel.kv.lat', fragN(span('lat'), span('latnote', 'tiers-note'))));
    kv.append(kvRow('tiers.panel.kv.proto', span('protos', '', true)));
    /* K-1: the masked host and the port, when nodes.detail carries endpoint (no reveal: v1 has no host field) */
    const addr = kvRow('tiers.panel.kv.address', span('host', 'mono tiers-host', true)); addr.hidden = true; q.addrRow = addr; kv.append(addr);
    /* K-4: the subscription it comes from */
    const sub = n.subscription_id ? subName(n.tier, n.subscription_id) : null;
    if (sub) kv.append(kvRow('tiers.panel.kv.source', dataEl(sub)));
    if (gw && !isCur(n)) kv.append(kvRow('tiers.panel.kv.role', tiEl('span', '', draft ? 'tiers.panel.role.draft' : 'tiers.panel.role.candidate')));
    kv.append(kvRow('tiers.panel.kv.checked', span('chk')));
    if (r) kv.append(kvRow('tiers.panel.kv.reason', fragN(hN('span', 't-err', wordOf(r)), hN('span', 'tiers-note', fixOf(r, n))), 'tiers-kv-why'));
    pn.append(kv);
    /* (F-01: the read-only note of a subscription or pool node is gone: such a node simply offers no
       edit, switch or delete) */
    /* B-04: the 30-minute trend block stays behind its feature (no v1 history: the panel shows the last latency) */
    if (T.rt.feature('B-04')) pn.append(hN('section', 'tiers-sec', hN('div', 'sh', tiEl('span', 'sh-t', 'tiers.panel.trend.title')), tiEl('p', 'tiers-spk-none', 'tiers.panel.trend.none')));
    if (!draft && (n.kind !== 'automatic' || T.rt.feature('B-12'))) pn.append(diagSection(n, pr));
    const chain = hN('section', 'tiers-sec'); chain.hidden = true; q.chainSec = chain; pn.append(chain);
    if (!ro) pn.append(toggleSection(n, pr));
    if (n.kind === 'manual') pn.append(paramsSection(n, pr));
    box.replaceChildren(pn);
    pr.box = box; pr.q = q;
    paintPanelLive(pr, true);
    paintDetail(pr);
    paintRes(pr, true);
    /* rebuilt while a diagnosis runs: its 停止 shows again */
    paintDiagCtl(pr);
  }
  /* the live parts of the panel: latency, its note, the last check, the head, the toggle hint */
  function paintPanelLive(pr, force) {
    if (!pr || !pr.q || pr.kind !== 'node' || (!force && (!V.panel || V.panel.closed || !pr.box.isConnected))) return;
    const n = panelNode(pr), q = pr.q, st = stateOf(n), live = st !== 'draft' && run() === 'running';
    setD(q.lat, latNode(n));
    const note = st === 'draft' || st === 'down' || n.latency_ms == null ? '' : n.latency_ms > ceil() ? (live ? 'over' : 'last') : live ? 'live' : 'last';
    setT(q.latnote, note ? L('tiers.panel.latnote', { kind: note, max_ms: ceil() }) : '');
    /* K-2: the time of the last check when Node carries last_checked_at_ms (F-01: no longer followed
       by how often the core checks; a draft, which is not checked, shows —) */
    const at = Number.isInteger(n.last_checked_at_ms) ? n.last_checked_at_ms : null;
    let chk;
    if (st === 'draft') chk = '—';
    else if (at != null) chk = agoSec(at);
    else if (n.last_checked_at_ms === null) chk = L('tiers.panel.checked.never');
    else chk = '—';
    setT(q.chk, chk);
    if (pr.tgh) setT(pr.tgh, toggleHint(n, pr));
    const h = headNode(n), hk = keyOf(hN('div', '', h.cloneNode(true)));
    if (V.panel && V.panel.head && V.panel.head._h !== hk) { V.panel.head._h = hk; V.panel.head.replaceChildren(h); }
  }
  /* what nodes.detail adds: protocols and their order (K-3), the masked address (K-1), the
     protocol choice of the diagnosis (K-9 via NodeProtocol.node_id), the last diagnosis */
  function paintDetail(pr) {
    if (!pr || !pr.q || pr.kind !== 'node') return;
    const n = panelNode(pr), det = pr.detail || {}, q = pr.q;
    const protos = Array.isArray(det.protocols) && det.protocols.length ? det.protocols : null;
    q.protos.textContent = protos ? protos.map(x => x.name).join(' → ') : (n.protocol || '—');
    const ep = det.endpoint && typeof det.endpoint === 'object' ? det.endpoint : null;
    q.addrRow.hidden = !ep;
    if (ep) q.host.textContent = `${ep.host_masked}:${ep.port}`;
    if (q.chainSec) {
      q.chainSec.hidden = !(protos && protos.length > 1);
      if (protos && protos.length > 1) {
        const ol = hN('ol', 'tiers-chain');
        protos.forEach((x, i) => ol.append(hN('li', '', hN('span', 'tiers-rank', String(i + 1)), dataEl(x.name, 'tiers-chain-p'), dataEl(`:${x.port}`, 'mono tiers-chain-port'))));
        q.chainSec.replaceChildren(hN('div', 'sh', tiEl('span', 'sh-t', 'tiers.panel.chain.title')), ol);
      }
    }
    if (pr.protoBox) {
      /* K-9: a multi-protocol node's protocol is chosen by its variant's node_id */
      const want = protos && protos.length > 1 && protos.some(x => x.node_id);
      pr.protoBox.hidden = !want;
      if (want && !pr.protoSel) {
        const sel = pr.protoBox.querySelector('.select');
        const keep = pr.protoKeep && protos.some(x => x.name === pr.protoKeep && x.node_id) ? pr.protoKeep : protos[0].name;
        pr.protoSel = T.ui.select(sel, { value: keep, options: protos.map((x, i) => ({ value: x.name, label: x.name, hint: L('tiers.panel.diag.rank', { n: i + 1 }), disabled: !x.node_id })) });
        sel.setAttribute('translate', 'no');
      }
    }
    if (!pr.diag && det.last_diagnostic && !pr.lastShown) { pr.lastShown = true; paintRes(pr); }
    paintPanelLive(pr);
  }

  /* ---- 连通诊断 ---------------------------------------------------------------- */
  let dselN = 0;   // ids of the protocol labels (a rebuilt panel gets a new one)
  function diagSection(n, pr) {
    const gw = n.tier === 'gateway';
    const s = hN('section', 'tiers-sec');
    const run1 = btnN('btn btn-sm', 'tiers.panel.diag.run', { icon: 'pulse', data: { p: 'diag' } });
    const stop = btnN('btn btn-sm btn-ghost', 'tiers.panel.diag.cancel', { data: { p: 'dstop' } }); stop.hidden = true;
    s.append(hN('div', 'sh', tiEl('span', 'sh-t', 'tiers.panel.diag.title'), hN('div', 'sh-a', stop, run1)));
    /* K-9: the protocol of a multi-protocol node (shown once nodes.detail lists its variants) */
    /* T-P5-14: the select is named by this label (aria-labelledby; T.ui.select appends its value) */
    const plab = tiEl('span', 'tiers-dopt-l', 'tiers.panel.diag.proto'); plab.id = `tiers-dopt-l${++dselN}`;
    const pb = hN('div', 'tiers-dopt', plab, T.html`<button class="select sm tiers-dsel" type="button" aria-labelledby="${plab.id}"></button>`);
    /* a rebuilt panel (language switch, refreshPanel) gets a new select: paintDetail sets it up again,
       keeping the protocol that was chosen */
    if (pr.protoSel) pr.protoKeep = pr.protoSel.get();
    pr.protoSel = null;
    pb.hidden = true; pr.protoBox = pb; s.append(pb);
    if (gw) {
      /* G40: a Gateway candidate is diagnosed through the carrying tier by default (chain) */
      const seg = T.html`<span class="seg" aria-label="${L('tiers.panel.diag.mode_aria')}" data-i18n-attr="aria-label:tiers.panel.diag.mode_aria"><button class="seg-btn" data-v="chain" data-i18n="tiers.panel.diag.chain">${L('tiers.panel.diag.chain')}</button><button class="seg-btn" data-v="direct" data-i18n="tiers.panel.diag.direct">${L('tiers.panel.diag.direct')}</button></span>`;
      const hint = hN('span', 'tiers-note');
      s.append(hN('div', 'tiers-dopt', seg, hint));
      /* the chain's way (data: the carrying tier and its node) when the route names it; F-01: the
         generic sentences (what 直连 means, a chain whose way is not known) are gone */
      const hintFor = v => {
        if (v !== 'chain') return fragN();
        const route = DS.route(), car = S.get('carrier');
        const via = route && route.valid && route.leaf && route.managed_tier && route.managed_tier !== 'gateway' ? route.selected_chain && route.selected_chain[0] : null;
        return car && via ? tx('tiers.panel.diag.via', { tier: tierName(car), name: via }) : fragN();
      };
      pr.modeSeg = T.ui.seg(seg, { value: 'chain', onChange: v => swapNode(hint, hintFor(v)) });
      swapNode(hint, hintFor('chain'), { instant: true });
    }
    const res = hN('div', 'tiers-res'); res.setAttribute('aria-live', 'polite');
    s.append(res);
    pr.res = res; pr.diagBtn = run1; pr.stopBtn = stop;
    requestAnimationFrame(() => T.ui.reserve(run1, DIAG_FACES()));
    run1.addEventListener('click', () => runDiag(pr, run1));
    stop.addEventListener('click', () => cancelDiag(pr, stop));
    return s;
  }
  /* the faces the diagnose button can take (its width is reserved for the widest, in the current language) */
  const DIAG_FACES = () => [L('tiers.row.checking'), F.ms(1234), wordOf('tls_failed'), wordOf('latency_limit_exceeded'), wordOf('dns_resolution_failed'), L('tiers.panel.diag.done')];
  /* the reason of a failed diagnosis: the error's reason_code (or the partial result's), else the code */
  function diagReason(err) {
    if (!err) return null;
    const r = err.reason_code || (err.result && err.result.reason_code) || null;
    if (r) return r;
    if (err.code === 'timeout') return 'timeout';
    if (err.code === 'cancelled') return 'cancelled';
    if (err.code === 'superseded') return 'node_changed';
    return null;
  }
  function runDiag(pr, btn) {
    if (pr.diag && pr.diag.running) return btn._busy || null;
    if (!T.rt.can('nodes.diagnose')) return T.ui.busy(btn, Promise.reject(new T.rt.RtError('not_allowed')), { fail: e => errText(e) });
    const n = panelNode(pr);
    const proto = pr.protoSel && !pr.protoBox.hidden ? pr.protoSel.get() : null;
    const variant = proto && pr.detail && Array.isArray(pr.detail.protocols) ? pr.detail.protocols.find(x => x.name === proto) : null;
    const nodeId = variant && variant.node_id ? variant.node_id : n.id;
    const mode = pr.modeSeg ? pr.modeSeg.get() : 'direct';
    /* request_id: the T.rt generator (bridge_diagnostics _REQUEST_ID format, P4 contract memo) */
    const d = pr.diag = { req: T.rt.reqId(), running: true, mode, proto: variant ? variant.name : null, nodeId, result: null };
    paintDiagCtl(pr);
    const job = T.rt.call('nodes.diagnose', { node_id: nodeId, probe_mode: mode, request_id: d.req }, { owner: pr.owner })
      .then(res => afterDiag(pr, d, res), err => {
        d.running = false;
        if (pr.dropped || !err || err.code === 'owner_revoked' || err.code === 'runtime_gone') throw err;
        d.result = { ok: false, err, reason: diagReason(err), cleanup: !!(err.result && err.result.cleanup_pending) };
        paintDiagCtl(pr); paintRes(pr);
        throw err;
      });
    return T.ui.busy(btn, job, { busy: L('tiers.row.checking'), done: r => (r && r.ms != null ? F.ms(r.ms) : L('tiers.panel.diag.done')),
      fail: err => { const r = diagReason(err); return r ? wordOf(r) : errText(err); }, hold: 1800, failHold: 2400 });
  }
  /* nodes.diagnose answers without a latency: nodes.detail's last_diagnostic has it; the node fields
     shown are the newer of the detail and the list row (mapping_tiers risk 10) */
  function afterDiag(pr, d, res) {
    d.running = false;
    const done = ms => {
      d.result = { ok: true, ms, applied: !(res && res.status_applied === false), cleanup: !!(res && res.cleanup_pending), mode: d.mode, proto: d.proto };
      paintDiagCtl(pr); paintRes(pr);
      return d.result;
    };
    if (pr.dropped || !T.rt.can('nodes.detail')) return done(null);
    const t = pr.row.tier, revAt = V.rev[t] || 0;
    return T.rt.query('nodes.detail', { node_id: d.nodeId }, { owner: pr.owner }).then(det => {
      const ld = det && det.last_diagnostic;
      const ms = ld && (!res || ld.request_id === res.request_id) && ld.latency_ms != null ? Math.round(ld.latency_ms) : null;
      if (det && d.nodeId === pr.row.id) {
        pr.detail = det;
        const newer = P.pickNewer({ listRow: findRow(pr.row.id), revAtRequest: revAt, revNow: V.rev[t] || 0, detailNode: det.node });
        pr.override = newer === det.node && det.node ? { node: det.node, rev: V.rev[t] || 0 } : null;
        paintDetail(pr);
      }
      return done(ms);
    }, () => done(null));
  }
  function cancelDiag(pr, btn) {
    const d = pr.diag; if (!d || !d.running || d.cancelling) return;
    d.cancelling = true;
    T.ui.busy(btn, T.rt.call('nodes.cancelDiagnose', { request_id: d.req }, { owner: pr.owner }),
      { busy: L('tiers.panel.diag.cancelling'), done: r => L(r && r.cleanup_pending ? 'tiers.panel.diag.cleanup' : 'tiers.panel.diag.cancel_sent'), fail: e => errText(e), hold: 1600 })
      /* the cancel failed (conflict, rate_limited, a lost delivery): the user may try again, and
         closing the panel sends the cancel again (onRevoke). Not after owner_revoked / runtime_gone:
         the panel or the generation is gone (05b review cont. 1 S1; T-P4-H2 item 4) */
      .catch(e => { if (!(e && ['owner_revoked', 'runtime_gone'].includes(e.code)) && pr.diag === d) d.cancelling = false; });
  }
  function paintDiagCtl(pr) {
    if (!pr.stopBtn) return;
    const on = !!(pr.diag && pr.diag.running);
    pr.stopBtn.hidden = !on || !T.rt.can('nodes.cancelDiagnose');
    if (pr.protoSel) pr.protoBox.classList.toggle('is-disabled', on);
  }
  /* the result area: the diagnosis of this panel (ok with its latency, a failure with the reason and
     what to do, cancelled), else the last diagnosis from nodes.detail, else a hint */
  function paintRes(pr, force) {
    const box = pr && pr.res; if (!box || (!force && !box.isConnected)) return;
    const d = pr.diag, n = panelNode(pr);
    let node;
    if (d && d.result && d.result.ok) {
      const ok = hN('span', 'tnum', T.tx('tiers.panel.diag.ok', { has_ms: d.result.ms != null ? 'yes' : 'no', ms: d.result.ms || 0, has_proto: d.result.proto ? 'yes' : 'no', proto: d.result.proto || '', chain: d.result.mode === 'chain' ? 'yes' : 'no' }, { x: xData() }));
      /* (F-04: no footnote on whether the result was written back or the test core is still being
         cleaned up: internals, nothing for the user to do) */
      node = hN('div', 'tiers-res-ok', iconN('success', 15), ok);
    } else if (d && d.result && !d.result.ok) {
      const r = d.result.reason, e = d.result.err;
      if (r === 'cancelled') node = hN('p', 'tiers-res-hint', L('tiers.panel.diag.cancelled'));
      else {
        const off = (r === 'component_missing' || r === 'startup_failed') && !running() && T.rt.can('service.start');
        /* F-04: when the panel's 原因 row above already gives this reason with its advice, the
           callout keeps only the title */
        const told = r && n && P.nodeReason(n) === r;
        node = T.ui.callout({ kind: 'error', small: true, title: r ? L('tiers.panel.diag.fail_title', { word: wordOf(r), has_proto: d.proto ? 'yes' : 'no', proto: d.proto || '' }) : errText(e),
          message: r && !told ? fixOf(r, n) : '',
          actions: off ? [{ label: L('tiers.panel.diag.start'), run: () => T.rt.call('service.start', {}).catch(() => {}) }] : null });
      }
    } else if (pr.detail && pr.detail.last_diagnostic && pr.detail.last_diagnostic.state !== 'running') {
      const ld = pr.detail.last_diagnostic;
      if (ld.state === 'ok') node = hN('div', 'tiers-res-ok is-last', iconN('success', 15), hN('span', 'tnum', T.tx('tiers.panel.diag.last_ok', { has_ms: ld.latency_ms != null ? 'yes' : 'no', ms: ld.latency_ms != null ? Math.round(ld.latency_ms) : 0, chain: ld.mode === 'chain' ? 'yes' : 'no' })));
      else node = hN('p', 'tiers-res-hint', L('tiers.panel.diag.last_failed', { state: ld.state, has_word: ld.reason_code ? 'yes' : 'no', word: ld.reason_code ? wordOf(ld.reason_code) : '' }));
    } else node = fragN();   // (F-01: no sentence on what a diagnosis does before there is a result)
    const k = keyOf(hN('div', '', node.cloneNode(true)));
    if (box._h === k) return;
    box._h = k;
    box.replaceChildren(node);
    if (!force && box.firstElementChild) M.anim(box.firstElementChild, [{ opacity: 0, transform: 'translateY(-3px)' }, { opacity: 1, transform: 'none' }], { duration: 200, easing: M.ease.out });
  }
  function diagFromRow(n) {
    /* only once the panel shows this node (a discard question answered no leaves the other one) */
    openPanel(n.id, { force: V.pkey !== n.id, then: pr => {
      if (pr.key !== n.id || !pr.diagBtn) return;
      pr.diagBtn.scrollIntoView({ block: 'nearest' }); runDiag(pr, pr.diagBtn);
    } });
  }

  /* ---- 参与回退 ↔ 草稿 (nodes.update enabled) ------------------------------------- */
  /* the switch's line under its label: only when a draft cannot be switched on again (K-11, what to do
     about it). F-01: the lines that explained what the switch does (and B-11's variant) are gone */
  function toggleHint(n, pr) {
    return stateOf(n) === 'draft' && !P.configItemIdOf(n, DS.items(), pr && pr.detail) ? L('tiers.panel.toggle.k11') : '';
  }
  function toggleSection(n, pr) {
    const gw = n.tier === 'gateway', draft = stateOf(n) === 'draft';
    const s = T.html`<section class="tiers-sec"><label class="toggle tiers-tg"><span class="toggle-t"><span data-i18n="${gw ? 'tiers.panel.toggle.candidate' : 'tiers.panel.toggle.fallback'}">${L(gw ? 'tiers.panel.toggle.candidate' : 'tiers.panel.toggle.fallback')}</span><small></small></span><button class="switch" type="button" role="switch" aria-checked="${draft ? 'false' : 'true'}" data-p="tg"></button></label></section>`;
    const tg = s.querySelector('[data-p="tg"]');
    tiAttr(tg, ['aria-label', gw ? 'tiers.panel.toggle.candidate' : 'tiers.panel.toggle.fallback']);
    pr.tgh = s.querySelector('small');
    setT(pr.tgh, toggleHint(n, pr));
    /* K-11 (T-P4-C1 item 8): a disabled node is switched on again by its ConfigItem id; without one the
       switch only goes off */
    if (draft && !P.configItemIdOf(n, DS.items(), pr.detail)) tg.disabled = true;
    T.ui.switch(tg, { onChange: v => setEnabled(panelNode(pr), v, pr) });
    return s;
  }
  function toggleFromPanel(n, enable) {
    const pr = V.pr; if (!pr || pr.key !== n.id) return;
    const tg = pr.box && pr.box.querySelector('[data-p="tg"]');
    if (tg && !tg.disabled) tg.click(); else setEnabled(n, enable, pr);
  }
  /* B-18 (kept behind its feature): the prototype names the node that takes over, here the fastest
     other usable row of the tier; without the feature the wording is generic (the runtime decides) */
  function successorOf(n) {
    if (!T.rt.feature('B-18')) return null;
    const x = liveRows(n.tier).filter(r => r.id !== n.id && r.kind !== 'automatic' && (stateOf(r) === 'ok' || stateOf(r) === 'slow')).sort(byLat)[0];
    return x ? x.name : null;
  }
  /* G18: switching off the carrying node or the tier's last one asks first (a warning), then goes */
  function setEnabled(n, on, pr) {
    const role = n.tier === 'gateway' ? 'exit' : 'fallback';
    let ask = Promise.resolve(true), itemId;
    if (!on) {
      const cur = isCur(n), others = liveRows(n.tier).filter(x => x.id !== n.id && x.kind !== 'automatic').length;
      itemId = n.fromConfig ? n.configItemId : n.id;
      const next = cur ? successorOf(n) : null;
      if (cur || !others) ask = T.confirm({ title: L('tiers.draft_confirm.title', { name: n.name }), message: L('tiers.draft_confirm.body', { role, cur: cur ? 'yes' : 'no', last: others ? 'no' : 'yes', tier: tierName(n.tier), has_next: next ? 'yes' : 'no', next: next || '' }), confirmLabel: L('tiers.draft_confirm.ok') });
    } else {
      itemId = P.configItemIdOf(n, DS.items(), pr && pr.detail);
      if (!itemId) { panelNote(pr, L('tiers.panel.toggle.k11')); return Promise.resolve(false); }
    }
    return ask.then(ok => {
      if (!ok) return false;
      return write('nodes.update', { item_id: itemId, updates: { enabled: !!on } }).then(r => {
        const k = P.appliedOutcome(r, run());
        if (k !== 'applied') panelNote(pr, outcomeWord(k, 'saved'), 'info');
        changed('nodes');
        return true;
      }, err => { panelNote(pr, writeError(err)); return false; });
    });
  }
  /* nodes.update / nodes.delete: a job that saved but could not apply ends failed with its Applied
     result (saved=true, applied=false: PROTOCOL section 15, a done job is applied). The write did
     happen, so it reads as the outcome unapplied, not as an error (P4 contract memo) */
  function write(cmd, args) {
    return T.rt.call(cmd, args).catch(err => {
      if (P.writeFailure(err) === 'unapplied') return { ...err.result, saved: true, applied: false };
      throw err;
    });
  }
  /* a failed write in words: rolled back, saved but not applied, busy, or the error itself (12.3) */
  function writeError(err) {
    const k = P.writeFailure(err);
    if (k === 'rolled_back') return L('tiers.write.rolled_back');
    if (k === 'unapplied') return L('tiers.write.unapplied', { reason: errText(P.detailError(err)) });
    if (err && err.code === 'conflict' && !err.result) return L('tiers.write.stale');
    return errText(P.detailError(err));
  }
  function panelNote(pr, text, kind) {
    if (!pr || !pr.box || !pr.box.isConnected) { if (text) T.notify({ kind: kind === 'info' ? 'info' : 'warning', id: 'tiers-note', message: text, timeout: 6000 }); return; }
    const pn = pr.box.querySelector('.tiers-pn'); if (!pn) return;
    let e = pn.querySelector('.tiers-pn-note');
    if (!e) { e = hN('div', 'tiers-pn-note'); pn.prepend(e); }
    e.replaceChildren(T.ui.callout({ kind: kind || 'warning', small: true, message: text }));
    M.anim(e, [{ opacity: 0, transform: 'translateY(-4px)' }, { opacity: 1, transform: 'none' }], { duration: 220, easing: M.ease.out });
    if (V.panel && V.panel.body) V.panel.body.scrollTo({ top: 0, behavior: M.reduced ? 'auto' : 'smooth' });
  }

  /* ---- delete (nodes.delete) -------------------------------------------------------- */
  function askDelete(n, btn) {
    const pr = V.pr && V.pr.key === n.id ? V.pr : null;
    const cur = isCur(n), others = liveRows(n.tier).filter(x => x.id !== n.id && x.kind !== 'automatic').length, draft = stateOf(n) === 'draft';
    const itemId = draft ? P.configItemIdOf(n, DS.items(), pr && pr.detail) || n.id : n.id;
    /* G18: a warning first (carrying, the last node of the tier), then it is allowed */
    const next = cur ? successorOf(n) : null;
    T.confirm({ title: L('tiers.delete_confirm.title', { name: n.name }), message: L('tiers.delete_confirm.body', { cur: cur ? 'yes' : 'no', role: n.tier === 'gateway' ? 'exit' : 'fallback', last: !draft && !others ? 'yes' : 'no', tier: tierName(n.tier), has_next: next ? 'yes' : 'no', next: next || '' }), confirmLabel: L('tiers.delete_confirm.ok'), danger: true })
      .then(ok => {
        if (!ok) return;
        const job = write('nodes.delete', { item_id: itemId }).then(r => {
          const k = P.appliedOutcome(r, run());
          if (k !== 'applied') T.notify({ kind: k === 'unapplied' ? 'warning' : 'info', id: 'tiers-del', message: outcomeWord(k, 'deleted'), timeout: 5000 });
          return k;
        });
        const p = btn ? T.ui.busy(btn, job, { busy: L('tiers.busy.deleting'), done: false, fail: e => writeError(e) }) : job;
        p.then(() => { if (V.pkey === n.id) closePanel(); changed('nodes'); }, e => { if (!btn) panelNote(pr, writeError(e)); });
      });
  }

  /* ---- 完整技术参数: nodes.reveal (private; mapping_tiers 8.5) ------------------------------------
     v1 has no structured parameters: the share link, masked until its eye is clicked. The value is
     fetched on that click (never ahead of it), kept in the panel's memory (pr.secret) and shown as
     text; hiding it puts the mask back, closing the panel (or a revoked owner, another generation,
     another node) drops it. It is never written to an attribute, the store or a log. */
  const MASK = '••••••••';
  function paramsSection(n, pr) {
    const s = hN('section', 'tiers-sec');
    const fold = T.html`<button class="tiers-fold" type="button" aria-expanded="false" data-press>${I('chevron', 14, 'fold-chev')}<span data-i18n="tiers.panel.params.title">${L('tiers.panel.params.title')}</span></button>`;
    const box = hN('dl', 'kv tiers-params');
    box.hidden = true; box.setAttribute('translate', 'no');
    s.append(fold, box);
    pr.paramsEl = box;
    fold.addEventListener('click', () => {
      const open = fold.getAttribute('aria-expanded') !== 'true';
      fold.setAttribute('aria-expanded', String(open));
      box.hidden = !open;
      if (open && !box.firstChild) paintParams(n, pr);
      if (open) M.anim(box, [{ opacity: 0, transform: 'translateY(-4px)' }, { opacity: 1, transform: 'none' }], { duration: 200, easing: M.ease.out });
    });
    return s;
  }
  function paintParams(n, pr) {
    const box = pr.paramsEl; if (!box) return;
    const v = hN('span', 'mono tiers-secv', MASK);
    const eye = iconBtn('icon-btn sm', 'eye', 'tiers.panel.params.show_value');
    eye.addEventListener('click', () => revealUri(n, pr, v, eye));
    const row = hN('div', '');
    row.append(tiEl('dt', '', 'tiers.panel.params.field.uri'), hN('dd', '', v, eye));
    box.replaceChildren(row);
  }
  function setEye(eye, on) {
    eye.replaceChildren(iconN(on ? 'eye-off' : 'eye', 14));
    tiAttr(eye, ['aria-label', on ? 'tiers.panel.params.hide_value' : 'tiers.panel.params.show_value'], ['data-tip', on ? 'tiers.panel.params.hide_value' : 'tiers.panel.params.show_value']);
  }
  function revealUri(n, pr, v, eye) {
    if (v._on) { v._on = false; v.textContent = MASK; setEye(eye, false); return; }
    const show = () => {
      const val = pr.secret && typeof pr.secret.uri === 'string' && pr.secret.uri ? pr.secret.uri : null;
      if (!val) { v.textContent = L('tiers.panel.params.unset'); return; }
      v._on = true; v.textContent = val; setEye(eye, true);
    };
    if (pr.secret) { show(); return; }
    if (eye.getAttribute('aria-busy') === 'true' || !T.rt.can('nodes.reveal')) return;
    eye.setAttribute('aria-busy', 'true');
    T.rt.call('nodes.reveal', { item_id: n.fromConfig ? n.configItemId : n.id, fields: ['uri'] }, { owner: pr.owner }).then(d => {
      eye.removeAttribute('aria-busy');
      if (pr.dropped) return;
      pr.secret = d && d.fields && typeof d.fields === 'object' ? { ...d.fields } : {};
      show();
    }, e => {
      eye.removeAttribute('aria-busy');
      /* not_allowed (the panel closed / the owner was revoked): folded away quietly, not retried */
      if (pr.dropped || !e || e.code === 'owner_revoked' || e.code === 'not_allowed' || e.code === 'runtime_gone') return;
      v.textContent = errText(e);
    });
  }

  /* ---- edit (nodes.update; nodes you added) -------------------------------------------------
     Vanguard: name, address and port (updates), the protocol order (fallback_order, K-3) and the
     share links (SecretEdit set). Other tiers: the name, and the share link replaced as a whole
     (uri, W-19 / G16). An empty secret field keeps the stored value; nothing is prefilled from a mask. */
  const vHost = v => (!String(v).trim() ? L('tiers.form.host.empty') : !/^(?:[a-z0-9-]+\.)+[a-z]{2,}$|^\d{1,3}(?:\.\d{1,3}){3}$|^[0-9a-f:]+$/i.test(String(v).trim()) ? L('tiers.form.host.invalid') : null);
  const vPort = v => (!/^\d+$/.test(String(v).trim()) || +v < 1 || +v > 65535 ? L('tiers.form.port.invalid') : null);
  const LINK_OF = name => (/vless/i.test(name) ? 'vless_link' : /hysteria2/i.test(name) ? 'hysteria2_link' : /naive/i.test(name) ? 'naive_link' : null);
  const LINKS_IN = label => String(label || '').split(/[^A-Za-z0-9_]+/).map(LINK_OF).filter(Boolean);
  /** a labelled input (static label registered; value and placeholder are set as properties, never as markup) */
  function textField(key, labelId, o) {
    o = o || {};
    const f = T.html`<div class="field" data-f="${key}"><label class="field-lab" data-i18n="${labelId}">${L(labelId)}</label><div class="input${o.mono ? ' mono' : ''}${o.num ? ' num' : ''}${o.sm ? ' sm' : ''}"><input autocomplete="off" spellcheck="false"></div><div class="field-msg"></div></div>`;
    const input = f.querySelector('input');
    if (o.opt) f.querySelector('.field-lab').append(' ', tiEl('span', 'opt-l', 'tiers.form.optional'));
    if (o.secret) { input.type = 'password'; addEye(f, input); }
    if (o.ph) input.placeholder = o.ph;
    if (o.value != null) input.value = String(o.value);
    if (o.hint) f.querySelector('.field-msg').textContent = o.hint;
    return { el: f, input };
  }
  function addEye(f, input) {
    const b = iconBtn('icon-btn sm tiers-eye', 'eye', 'tiers.form.show');
    b.addEventListener('click', () => { const on = input.type === 'password'; input.type = on ? 'text' : 'password'; b.replaceChildren(iconN(on ? 'eye-off' : 'eye', 14)); tiAttr(b, ['aria-label', on ? 'tiers.form.hide' : 'tiers.form.show'], ['data-tip', on ? 'tiers.form.hide' : 'tiers.form.show']); });
    f.querySelector('.input').append(b);
  }
  function startEdit(n) {
    if (!V.panel || V.panel.closed || V.pkey !== n.id) openPanel(n.id, { force: true });
    const pr = V.pr; if (!V.panel || !pr || pr.key !== n.id) return;
    endEdit();
    const ed = V.edit = { id: n.id, dirty: false, f: {}, secrets: [], order: null };
    /* the shell's quit question counts an edit with unsaved changes (criticism C9) */
    ed.unreg = T.shellc.registerDraft(() => (V.edit === ed && ed.dirty ? 1 : 0));
    V.panel.setContent(box => editBody(box, n, pr, ed), { dir: 0 });
    V.panel.setActions(editActions(n, ed));
    V.panel.head.replaceChildren(tiEl('span', 'chip sm', 'tiers.edit.chip')); V.panel.head._h = null;
  }
  function endEdit() { const ed = V.edit; V.edit = null; if (ed && ed.unreg) { ed.unreg(); ed.unreg = null; } }
  function backFromEdit() { endEdit(); const pr = V.pr, it = pr ? resolve(pr.key) : null; if (it) fillPanel(it, 0); else closePanel(); }
  const editActions = (n, ed) => [{ label: L('tiers.form.cancel'), ghost: true, keep: true, run: () => backFromEdit() }, { label: L('tiers.form.save'), primary: true, keep: true, run: b => saveEdit(n, b, ed) }];
  /* a language switch while an edit is open: the fields keep what was typed; the subtitle and the footer follow */
  function relabelEdit() {
    const p = V.panel, it = V.pkey ? resolve(V.pkey) : null; if (!p || p.closed || !it || !it.n || !V.edit) return;
    const Ps = spec(it, V.pr);
    p.el.setAttribute('aria-label', L('tiers.panel.aria', { name: Ps.title })); setSub(p, Ps);
    const sb = p.action(1); if (!(sb && sb._busy)) p.setActions(editActions(it.n, V.edit));
  }
  function editBody(box, n, pr, ed) {
    const tier1 = n.tier === 'vanguard', det = pr.detail || {}, ep = det.endpoint || null;
    const protos = Array.isArray(det.protocols) ? det.protocols : [];
    const w = hN('div', 'tiers-edit');
    /* (F-01: the note on when a save applies and the name's hint are gone; the save's outcome is said
       after it) */
    const nf = textField('name', 'tiers.form.name.label', { value: n.name });
    ed.f.name = T.ui.field(nf.el, { validate: v => (!v.trim() ? L('tiers.form.name.empty') : v.trim().length > 64 ? L('tiers.form.name.too_long', { max: 64 }) : null) });
    w.append(nf.el);
    /* address / port: Vanguard (tier1 updates); B-17 (the other tiers' field edit) stays behind its feature */
    if (tier1 || T.rt.feature('B-17')) {
      const hf = textField('host', 'tiers.form.host.label', { mono: true, ph: ep ? ep.host_masked : '', hint: L('tiers.edit.host_keep') });
      const pf = textField('port', 'tiers.form.port.label', { num: true, value: ep ? ep.port : '', hint: L('tiers.form.port.hint') });
      ed.f.host = T.ui.field(hf.el, { validate: v => (String(v).trim() ? vHost(v) : null) });
      ed.f.port = T.ui.field(pf.el, { validate: v => (String(v).trim() ? vPort(v) : null) });
      T.ui.number(pf.el.querySelector('.input'), { min: 1, max: 65535 });
      w.append(hN('div', 'tiers-hp', hf.el, pf.el));
    }
    /* K-3: the protocol order of a multi-protocol node (reordering only: per-protocol ports are B-17) */
    if (tier1 && protos.length > 1) {
      const lst = hN('div', 'tiers-ce-list');
      const rowEl = (x, i) => {
        const r = hN('div', 'tiers-ce is-order', iconBtn('grip', 'grip', 'tiers.chain.grip_aria_plain'), hN('span', 'tiers-rank', String(i + 1)), dataEl(x.name, 'tiers-ce-p'), dataEl(`:${x.port}`, 'mono tiers-chain-port'));
        r.dataset.p = x.name;
        return r;
      };
      lst.append(...protos.map(rowEl));
      const field = hN('div', 'field', hN('label', 'field-lab', tiEl('span', '', 'tiers.form.chain.label')), lst);
      const renum = () => T.$$('.tiers-ce', lst).forEach((r, i) => { r.querySelector('.tiers-rank').textContent = String(i + 1); });
      T.ui.sortable(lst, { items: '.tiers-ce', handle: '.grip', onEnd: () => { renum(); ed.dirty = true; } });
      ed.order = () => T.$$('.tiers-ce', lst).map(r => r.dataset.p);
      ed.orderWas = protos.map(x => x.name);
      w.append(field);
    }
    /* the share links: replaced whole (SecretEdit set); empty keeps the stored one */
    /* without nodes.detail.protocols, Node.protocol may be a label naming several protocols: every
       link it names is offered, not only the first one (review A S8) */
    const links = tier1 ? [...new Set(protos.length ? protos.map(x => LINK_OF(x.name)).filter(Boolean) : LINKS_IN(n.protocol))] : ['uri'];
    for (const fld of links) {
      const sf = textField('sec-' + fld, `tiers.edit.link.${fld}`, { mono: true, secret: true, hint: L('tiers.edit.sec_hint') });
      ed.secrets.push([fld, sf.input, T.ui.field(sf.el, {})]);
      w.append(sf.el);
    }
    box.replaceChildren(w);
    const mark = () => { if (V.edit === ed) ed.dirty = true; };
    box.addEventListener('input', mark);
    box.addEventListener('change', mark);
    setTimeout(() => { const i = box.querySelector('[data-f="name"] input'); if (i) i.focus(); }, 60);
  }
  function saveEdit(n, b, ed) {
    const f = ed.f; if (V.edit !== ed) return;
    const ok = [f.name.validate(), f.host ? f.host.validate() : true, f.port ? f.port.validate() : true];
    if (!ok.every(Boolean)) { const bad = V.panel.body.querySelector('.field.is-invalid input'); if (bad) bad.focus(); return; }
    const pr = V.pr, det = (pr && pr.detail) || {}, ep = det.endpoint || null;
    const updates = {}, secrets = [];
    const name = f.name.value.trim(); if (name !== n.name) updates.name = name;
    if (f.host && f.host.value.trim()) updates.address = f.host.value.trim();
    if (f.port && f.port.value.trim() && (!ep || Math.round(+f.port.value) !== ep.port)) updates.port = Math.round(+f.port.value);
    if (ed.order) { const o = ed.order(); if (o.join('\u0000') !== ed.orderWas.join('\u0000')) updates.fallback_order = o; }
    for (const [fld, input] of ed.secrets) if (input.value.trim()) secrets.push({ field: fld, action: 'set', value: input.value.trim(), target_id: n.id });
    if (!Object.keys(updates).length && !secrets.length) { backFromEdit(); return; }
    const args = { item_id: n.id, updates };
    if (secrets.length) args.secrets = secrets;
    T.ui.busy(b, write('nodes.update', args), { busy: L('tiers.busy.saving'), done: r => outcomeWord(P.appliedOutcome(r, run()), 'short'), fail: e => writeError(e), hold: 900, failHold: 2600 }).then(r => {
      /* the typed secrets leave the form's memory as soon as they are saved */
      ed.secrets.forEach(([, input]) => { input.value = ''; });
      ed.dirty = false;
      const k = P.appliedOutcome(r, run());
      /* back to the details; a result other than applied is said there (it outlives the edit view) */
      setTimeout(() => {
        if (V.edit === ed) backFromEdit();
        if (k !== 'applied' && V.pr === pr) panelNote(pr, outcomeWord(k, 'saved'), k === 'unapplied' ? 'warning' : 'info');
      }, 720);
      changed('nodes');
    }, err => {
      const d = P.detailError(err), fld = d && d.field;
      const target = fld === 'name' ? f.name : fld === 'address' ? f.host : fld === 'port' ? f.port : (ed.secrets.find(([k]) => k === fld) || [])[2];
      if (target) target.error(writeError(err)); else panelNote(pr, writeError(err));
    });
  }

  /* ---- subscription panel (groupSpec) ----------------------------------------------------- */
  function groupHead(g) {
    const h = P.subscriptionHealth(g);
    if (h === 'disabled') return rawN(T.ui.status('off', { chip: true, label: L('tiers.sub.disabled_short') }));
    if (h === 'unknown') return rawN(T.ui.status('pending', { chip: true }));
    if (h === 'pending') return rawN(T.ui.status('recheck', { chip: true }));
    return rawN(T.ui.status(g.healthy ? 'ok' : 'down', { chip: true, label: L('tiers.panel.sub_health', { ok: g.healthy || 0, total: g.total || 0 }) }));
  }
  function groupSpec(g, pr) {
    return {
      title: g.name, sub: L('tiers.panel.sub_subscription', { tier: tierName(g.tier) }), head: groupHead(g),
      content: box => groupBody(box, g, pr),
      actions: [{ label: L('tiers.sub.manage'), keep: true, run: b => manageSub(g, b) }, { label: L('tiers.sub.update'), primary: true, keep: true, run: b => refreshSub(g, b) }],
    };
  }
  function groupBody(box, g, pr) {
    const pn = hN('div', 'tiers-pn'), res = hN('div', 'tiers-pn-res');
    pn.append(res);
    /* (F-01: the notes that a disabled subscription's nodes do not run and that a degraded row shows
       only its name are gone: the head and the health line say 已停用 / 订阅状态暂不可用) */
    const kv = hN('dl', 'kv lined tiers-kv');
    /* the address as the sidecar masks it (Subscription.display: the token is hidden) */
    if (g.display) kv.append(kvRow('tiers.panel.kv.address', dataEl(g.display, 'mono tiers-host')));
    kv.append(kvRow('tiers.panel.kv.nodes', hN('span', 'tnum', g.total == null ? '—' : L('tiers.sub.count', { n: g.total }))));
    const state = groupSub(g);
    if (state) kv.append(kvRow('tiers.panel.kv.health', state));
    /* B-08: the per-subscription interval stays behind its feature (F-01: without it no row; it said
       that the subscription updates on its own) */
    if (T.rt.feature('B-08') && g.auto_update) kv.append(kvRow('tiers.panel.kv.auto', hN('span', '', L(`tiers.sub.auto.${g.auto_update}`))));
    /* K-6: one refresh time for the tier (Sentinel, Gateway) */
    if (Number.isInteger(g.last_refresh_at_ms)) kv.append(kvRow('tiers.panel.kv.updated', hN('span', '', F.ago(g.last_refresh_at_ms))));
    pn.append(kv);
    if (membership(g.tier) && !g.degraded) {
      const go = btnN('btn btn-sm', 'tiers.menu.view_nodes');
      go.append(hN('span', 'bi', iconN('chevron', 15)));
      go.addEventListener('click', () => drillIn('sub', g.id, rowOf('g:' + g.id), false));
      pn.append(hN('div', 'btns tiers-pn-go', go));
    }
    box.replaceChildren(pn);
    pr.box = box; pr.res = null;
    paintResultCard(res, V.results.get(g.id), false);
  }

  /* ---- subscriptions.refresh (G35: four results and the refusals) -------------------------- */
  function refreshErr(err) {
    const r = err && err.result;
    if (r && r.error) return r.error;
    if (r && r.apply_error) return r.apply_error;
    return err;
  }
  function refreshSub(g, btn) {
    if (!V.busySubs) V.busySubs = new Set();
    if (V.busySubs.has(g.id)) return btn ? btn._busy : null;
    if (!T.rt.can('subscriptions.refresh')) return null;
    V.busySubs.add(g.id); changed('subs');
    const job = T.rt.call('subscriptions.refresh', { item_id: g.id }).then(r => {
      const kind = P.refreshOutcome({ result: r, run: run() });
      V.results.set(g.id, { kind, error: r && r.error ? r.error : r && r.apply_error ? r.apply_error : null });
      return kind;
    }, err => {
      const kind = P.refreshOutcome({ error: err, run: run() });
      if (err && (err.code === 'owner_revoked' || err.code === 'runtime_gone')) throw err;
      V.results.set(g.id, { kind, error: refreshErr(err) });
      throw Object.assign(err || {}, { kind });
    });
    const end = () => { V.busySubs.delete(g.id); changed('subs'); };
    job.then(end, end);
    const opts = { busy: L('tiers.sub.busy.updating'), done: k => L('tiers.refresh.short', { kind: k }), fail: e => L('tiers.refresh.short', { kind: (e && e.kind) || 'failed' }), hold: 1500, failHold: 2600 };
    if (btn && btn.isConnected) return T.ui.busy(btn, job, opts);
    job.then(k => { if (k !== 'applied') T.notify({ kind: 'info', id: 'tiers-sub', message: L('tiers.refresh.result', { kind: k }), timeout: 5000 }); },
      e => { if (e && e.kind) T.notify({ kind: 'warning', id: 'tiers-sub', message: L('tiers.refresh.result', { kind: e.kind }), timeout: 6000 }); });
    return job;
  }

  /* ---- manage subscription (modal drawer) ---------------------------------------------------
     The name is read-only (B-09); the address: shown masked, revealed on demand (nodes.reveal
     subscription_url, the drawer's owner, memory only), replaced whole (SecretEdit set; empty keeps
     it); 启用 (G17, nodes.update enabled); the interval is B-08. Saving a new address updates the
     subscription once (two jobs, each reports its own result). */
  function manageSub(g, from) {
    /* st.owner is replaced when the runtime generation changes (onReset): the revealed address of
       the lost generation is dropped and a later reveal asks the new sidecar (PROTOCOL section 12) */
    const st = { dirty: false, secret: null, f: {}, owner: T.rt.owner('tiers-manage'), forget: null };
    const unreg = T.shellc.registerDraft(() => (st.dirty ? 1 : 0));
    const d = T.drawer.open({
      from: from && from.isConnected ? from : null, title: L('tiers.sub.manage'), subtitle: g.name, width: 440,
      content: box => manageBody(box, g, st),
      actions: [
        { label: L('tiers.sub.delete'), danger: true, left: true, keep: true, run: b => askDeleteSub(g, b, d) },
        { label: L('tiers.form.cancel'), ghost: true },
        { label: L('tiers.form.save'), primary: true, keep: true, run: b => saveSub(g, st, b, d) },
      ],
      beforeClose: () => (st.dirty ? T.confirm({ title: L('tiers.discard.title'), message: L('tiers.discard.body', { has_name: 'yes', name: g.name }), confirmLabel: L('tiers.discard.ok'), cancelLabel: L('tiers.discard.cancel'), danger: true }) : true),
      onClose: () => { st.secret = null; st.forget = null; st.owner.revoke(); unreg(); if (V.mng === st) V.mng = null; },
    });
    /* the subtitle is the subscription's name: data */
    const sub = d.el.querySelector('.ovl-sub'); if (sub) sub.setAttribute('translate', 'no');
    st.drawer = d;
    V.mng = st;
  }
  function manageBody(box, g, st) {
    const w = hN('div', 'tiers-edit'), res = hN('div', 'tiers-pn-res');
    w.append(res);
    paintResultCard(res, V.results.get(g.id), false);
    /* B-09: naming / renaming stays behind its feature; v1 shows the name */
    if (T.rt.feature('B-09')) { const nf = textField('sname', 'tiers.form.name.label', { value: g.name }); st.f.name = T.ui.field(nf.el, {}); w.append(nf.el); }
    const kv = hN('dl', 'kv lined tiers-kv');
    kv.append(kvRow('tiers.form.name.label', dataEl(g.name)));
    /* the current address: masked; the eye asks the sidecar (private) */
    const val = dataEl(g.display || MASK, 'mono tiers-host');
    const eye = iconBtn('icon-btn sm', 'eye', 'tiers.panel.params.show_value');
    eye.hidden = !T.rt.can('nodes.reveal') || !!g.degraded;
    /* back to the mask, the revealed value gone (the drawer's owner was revoked) */
    st.forget = () => { st.secret = null; val._on = false; val.textContent = g.display || MASK; eye.removeAttribute('aria-busy'); setEye(eye, false); };
    eye.addEventListener('click', () => {
      if (val._on) { val._on = false; val.textContent = g.display || MASK; setEye(eye, false); return; }
      const show = () => { const u = st.secret && st.secret.subscription_url; if (u) { val._on = true; val.textContent = u; setEye(eye, true); } else val.textContent = L('tiers.panel.params.unset'); };
      if (st.secret) { show(); return; }
      if (eye.getAttribute('aria-busy') === 'true') return;
      eye.setAttribute('aria-busy', 'true');
      const owner = st.owner;
      T.rt.call('nodes.reveal', { item_id: g.id, fields: ['subscription_url'] }, { owner }).then(r => {
        eye.removeAttribute('aria-busy');
        if (!owner.active) return;
        st.secret = r && r.fields ? { ...r.fields } : {};
        show();
      }, e => { if (!owner.active) return; eye.removeAttribute('aria-busy'); if (e && !['owner_revoked', 'not_allowed', 'runtime_gone'].includes(e.code)) val.textContent = errText(e); });
    });
    const dd = hN('dd', 'tiers-url', val, eye);
    const urlRow = hN('div', ''); urlRow.append(tiEl('dt', '', 'tiers.form.url.label'), dd); kv.append(urlRow);
    w.append(kv);
    const uf = textField('url', 'tiers.manage.url_new', { mono: true, secret: true });
    st.f.url = T.ui.field(uf.el, { validate: v => (!String(v).trim() ? null : !/^https?:\/\/[^\s/]+\.[^\s/]+/i.test(String(v).trim()) ? L('tiers.form.url.scheme') : null) });
    w.append(uf.el);
    /* G17 / W-16: switch a subscription off or on */
    const tg = T.html`<label class="toggle tiers-tg"><span class="toggle-t"><span data-i18n="tiers.manage.enabled">${L('tiers.manage.enabled')}</span></span><button class="switch" type="button" role="switch" aria-checked="${g.enabled === false ? 'false' : 'true'}"></button></label>`;
    const sw = tg.querySelector('.switch');
    tiAttr(sw, ['aria-label', 'tiers.manage.enabled']);
    st.f.enabled = T.ui.switch(sw, {});
    sw.addEventListener('change', () => { st.dirty = true; });
    w.append(tg);
    /* B-08: the interval select stays behind its feature (F-01: without it, no note that the
       subscription updates on its own) */
    if (T.rt.feature('B-08')) {
      const af = T.html`<div class="field" data-f="auto"><label class="field-lab" data-i18n="tiers.form.auto.label">${L('tiers.form.auto.label')}</label><button class="select tiers-w220" type="button"></button><div class="field-msg"></div></div>`;
      st.f.auto = T.ui.select(af.querySelector('.select'), { value: g.auto_update || '6h', options: ['30m', '6h', '12h', '1d', 'manual'].map(k => ({ value: k, label: L(`tiers.sub.auto.${k}`) })) });
      w.append(af);
    }
    const kv2 = hN('dl', 'kv lined tiers-kv');
    kv2.append(kvRow('tiers.panel.kv.nodes', hN('span', 'tnum', g.total == null ? '—' : L('tiers.sub.count', { n: g.total }))));
    if (Number.isInteger(g.last_refresh_at_ms)) kv2.append(kvRow('tiers.panel.kv.updated', hN('span', '', F.ago(g.last_refresh_at_ms))));
    w.append(kv2);
    box.replaceChildren(w);
    box.addEventListener('input', () => { st.dirty = true; });
  }
  function saveSub(g, st, b, d) {
    if (!st.f.url.validate()) { st.f.url.input.focus(); return; }
    const url = st.f.url.value.trim(), en = st.f.enabled.get(), updates = {};
    if (en !== (g.enabled !== false)) updates.enabled = en;
    const args = { item_id: g.id, updates };
    if (url) args.secrets = [{ field: 'subscription_url', action: 'set', value: url, target_id: g.id }];
    if (!Object.keys(updates).length && !url) { st.dirty = false; d.close(); return; }
    const job = write('nodes.update', args);
    T.ui.busy(b, job, { busy: L('tiers.busy.saving'), done: r => (url ? L('tiers.busy.saved_updating') : outcomeWord(P.appliedOutcome(r, run()), 'short')), fail: e => writeError(e), hold: 600, failHold: 2600 }).then(r => {
      st.f.url.input.value = '';
      st.dirty = false;
      setTimeout(() => {
        d.close();
        const k = P.appliedOutcome(r, run());
        if (k !== 'applied' && !url) T.notify({ kind: 'info', id: 'tiers-sub', message: outcomeWord(k, 'saved'), timeout: 5000 });
        /* a new address: update once (its own job and result) */
        if (url && en) { const ub = R.bar && V.drill && V.drill.id === g.id ? R.bar.querySelector('[data-b="upd"]') : null; refreshSub(g, ub); }
        changed('subs');
      }, 380);
    }, err => { const df = P.detailError(err); if (df && df.field && /url/.test(df.field)) st.f.url.error(writeError(err)); });
  }
  /* G18: deleting a subscription warns (its nodes go with it; in use, connections move), then goes */
  function askDeleteSub(g, btn, drawer) {
    T.confirm({ title: L('tiers.delete_confirm.title', { name: g.name }), message: L('tiers.sub_delete.body', { has_total: g.total == null ? 'no' : 'yes', n: g.total || 0 }), confirmLabel: L('tiers.delete_confirm.ok'), danger: true }).then(ok => {
      if (!ok) return;
      const job = write('nodes.delete', { item_id: g.id }).then(r => {
        const k = P.appliedOutcome(r, run());
        if (k !== 'applied') T.notify({ kind: k === 'unapplied' ? 'warning' : 'info', id: 'tiers-del', message: outcomeWord(k, 'deleted'), timeout: 5000 });
        return k;
      });
      (btn ? T.ui.busy(btn, job, { busy: L('tiers.busy.deleting'), done: false, fail: e => writeError(e) }) : job).then(() => {
        if (drawer && !drawer.closed) drawer.close();
        if (V.pkey === 'g:' + g.id) closePanel();
        V.results.delete(g.id);
        if (V.drill && V.drill.id === g.id) drillOut(visible); else changed('subs');
      }, e => { if (!btn) T.notify({ kind: 'warning', id: 'tiers-del', message: writeError(e), timeout: 6000 }); });
    });
  }

  /* ================================================================ import sheet (paste · manual · subscription) */
  /* import.specs is static for an app version (PROTOCOL section 11): one query per version */
  let SPECS = null;
  function loadSpecs() {
    const ver = S.get('appVersion') || '';
    if (SPECS && SPECS.ver === ver) return SPECS.p;
    if (!T.rt.can('import.specs')) return Promise.reject(new T.rt.RtError('not_allowed'));
    const p = T.rt.query('import.specs', {}).catch(e => { if (SPECS && SPECS.p === p) SPECS = null; throw e; });
    SPECS = { ver, p };
    return p;
  }
  const importFail = e => errText(P.detailError(e));
  /** the ids of a tier's rows before a write: the new ones are found by difference afterwards */
  const beforeIds = t => new Set([...(DS.nodes(t) || []).map(n => n.id), ...(DS.subs(t) || []).map(s => 'g:' + s.id)]);
  function openImport(from, o) {
    o = o || {};
    if (V.imp && V.imp.sh && !V.imp.sh.closed) return;
    const t = V.tier;
    const st = { t, tab: o.tab === 'sub' && t !== 'vanguard' ? 'sub' : o.tab === 'manual' ? 'manual' : 'paste', owner: T.rt.owner('tiers-import'), seq: 0, text: '', res: null, err: null, previewing: false, retried: false, notice: null, manual: null, sub: null };
    /* the shell's quit question counts an import with something typed in (criticism C9) */
    st.unreg = T.shellc.registerDraft(() => impDirty(st));
    let sh = null;
    sh = T.sheet.open({
      from, title: impTitle(t), width: 640, height: Math.min(560, innerHeight - 96), class: 'tiers-imp',
      content: box => buildImport(box, st, () => sh),
      actions: impActions(st, () => sh),
      /* the preview plan, the typed secrets and late answers go with the sheet (its owner) */
      onClose: () => { st.owner.revoke(); if (st.unreg) st.unreg(); st.unreg = null; st.text = ''; st.res = null; if (st.manual) st.manual.clear(); st.manual = null; if (V.imp && V.imp.st === st) V.imp = null; },
    });
    V.imp = { sh, st, t };
  }
  const impDirty = st => (st.text.trim() || (st.manual && st.manual.dirty()) || (st.sub && st.sub.url.value.trim()) ? 1 : 0);
  const impTitle = t => L('tiers.import.title', { tier: tierLabel(t) });
  /* a language switch while the import sheet is open: title, subtitle, footer and the paste lines follow */
  function relabelImport() {
    const im = V.imp; if (!im || !im.sh || im.sh.closed) { V.imp = null; return; }
    im.sh.setTitle(impTitle(im.t));
    const b = im.sh.action(1); if (!(b && b._busy)) im.sh.setActions(impActions(im.st, () => im.sh));
    if (im.st.paint) im.st.paint();
    requestAnimationFrame(() => refreshImpBtn(im.st, im.sh));
  }
  function impActions(st, getSh) {
    const cancel = { label: L('tiers.form.cancel'), ghost: true };
    if (st.tab === 'paste') return [cancel, { label: L('tiers.import.go_n', { n: 0 }), primary: true, keep: true, run: b => commitPaste(st, getSh(), b) }];
    if (st.tab === 'manual') return [cancel, { label: L('tiers.import.go'), primary: true, keep: true, run: b => commitManual(st, getSh(), b) }];
    return [cancel, { label: L('tiers.import.add_sub'), primary: true, keep: true, run: b => commitSub(st, getSh(), b) }];
  }
  /* B-37: a bad line rejects the whole batch, so 导入 needs accepted > 0 and nothing rejected */
  function refreshImpBtn(st, sh) {
    if (!sh || sh.closed) return;
    const b = sh.action(1); if (!b || b._busy) return;
    if (st.tab === 'paste') {
      const r = st.res, n = r && !st.previewing ? r.accepted || 0 : 0;
      const lab = b.querySelector('.lab') || b;
      M.swapText(lab, L('tiers.import.go_n', { n }));
      b.disabled = !(n > 0 && !(r.rejected && r.rejected.length));
    } else b.disabled = false;
  }
  function setTab(st, v, getSh) {
    st.tab = v;
    const sh = getSh(); sh.setActions(impActions(st, getSh));
    requestAnimationFrame(() => refreshImpBtn(st, sh));
    if (v === 'manual' && st.buildManual) st.buildManual();
  }
  function buildImport(box, st, getSh) {
    const t = st.t;
    const tabs = T.html`<div class="tabs sm tiers-imp-tabs"><button class="tab" data-v="paste" aria-controls="tiers-imp-paste" data-i18n="tiers.import.tab.paste">${L('tiers.import.tab.paste')}</button><button class="tab" data-v="manual" aria-controls="tiers-imp-manual" data-i18n="tiers.import.tab.manual">${L('tiers.import.tab.manual')}</button></div>`;
    if (t !== 'vanguard') tabs.append(T.html`<button class="tab" data-v="sub" aria-controls="tiers-imp-sub" data-i18n="tiers.import.tab.sub">${L('tiers.import.tab.sub')}</button>`);
    const pPaste = hN('div', 'tiers-imp-p'), pManual = hN('div', 'tiers-imp-p'), pSub = hN('div', 'tiers-imp-p');
    pPaste.id = 'tiers-imp-paste'; pManual.id = 'tiers-imp-manual'; pSub.id = 'tiers-imp-sub';
    /* the panes go in before T.ui.tabs runs: the sheet is not in the document yet, and the strip
       finds its panes in this detached tree (F-01: every pane used to show at once until a click) */
    box.replaceChildren(tabs, pPaste, pManual, ...(t !== 'vanguard' ? [pSub] : []));
    st.tabs = T.ui.tabs(tabs, { value: st.tab, onChange: v => {
      setTab(st, v, getSh);
      const pn = box.querySelector(`#tiers-imp-${v}`); if (pn) M.anim(pn, [{ opacity: 0, transform: 'translateY(4px)' }, { opacity: 1, transform: 'none' }], { duration: 200, easing: M.ease.out });
      setTimeout(() => { const f = pn && pn.querySelector('textarea, input:not([type=password])'); if (f && v !== 'manual') f.focus(); }, 30);
    } });
    buildPaste(pPaste, st, getSh);
    /* the manual form needs import.specs: fetched the first time the tab is shown */
    let built = false;
    st.buildManual = () => { if (built) return; built = true; buildManual(pManual, st, getSh); };
    if (t !== 'vanguard') buildSub(pSub, st);
    if (st.tab === 'manual') st.buildManual();
    requestAnimationFrame(() => refreshImpBtn(st, getSh()));
  }

  /* ---- paste: import.preview (res, the sheet's owner; a new text voids the older plan) ---- */
  function preview(st, my) {
    if (!T.rt.can('import.preview')) { st.previewing = false; st.err = new T.rt.RtError('not_allowed'); return Promise.resolve(); }
    return T.rt.call('import.preview', { tier: st.t, text: st.text }, { owner: st.owner }).then(r => {
      if (my !== st.seq) return;
      st.previewing = false; st.res = r && typeof r === 'object' ? r : null; st.err = null;
    }, e => {
      if (my !== st.seq || !e || e.code === 'owner_revoked') return;
      st.previewing = false; st.res = null; st.err = e;
    });
  }
  /* F-01: the paste tab is the box and what the preview found (the line on the supported links and
     the hint on how the batch is judged were teaching; a rejected batch still says why) */
  function buildPaste(el, st, getSh) {
    const area = T.html`<div class="field tiers-imp-ta"><div class="input area mono"><textarea rows="5" spellcheck="false" placeholder="${L('tiers.import.paste.ph')}" aria-label="${L('tiers.import.paste.aria')}" data-i18n-attr="placeholder:tiers.import.paste.ph;aria-label:tiers.import.paste.aria"></textarea></div></div>`;
    const sum = hN('div', 'tiers-imp-sum'); sum.setAttribute('aria-live', 'polite');
    const det = hN('div', 'tiers-imp-det');
    el.replaceChildren(area, sum, det);
    const ta = area.querySelector('textarea');
    let timer = 0;
    const paint = st.paint = () => {
      const sh = getSh();
      if (st.previewing) { sum.replaceChildren(hN('span', 'tiers-imp-busy', hN('span', 'spin'), L('tiers.import.recognizing'))); det.replaceChildren(); refreshImpBtn(st, sh); return; }
      if (st.err) { sum.replaceChildren(hN('span', 'st st-down', errText(st.err))); det.replaceChildren(); refreshImpBtn(st, sh); return; }
      const r = st.res;
      if (!r) { sum.replaceChildren(); det.replaceChildren(); refreshImpBtn(st, sh); return; }
      const rej = P.rejectedSummary(r.rejected), bad = rej.reduce((a, x) => a + x.n, 0);
      const chips = [];
      if (r.accepted) chips.push(hN('span', 'st st-ok', L('tiers.import.sum.ok', { n: r.accepted })));
      if (bad) chips.push(hN('span', 'st st-down', L('tiers.import.sum.bad', { n: bad })));
      if (!r.accepted && !bad) chips.push(hN('span', 'st st-pending', L('tiers.import.sum.none')));
      chips.push(hN('span', 'tiers-note', st.notice || (bad ? L('tiers.import.sum.note_bad') : '')));
      sum.replaceChildren(...chips);
      const parts = [];
      /* G38: the recognised nodes by physical node (import.preview groups, when the sidecar sends them) */
      const groups = P.previewGroups(r);
      if (groups) parts.push(groupsN(groups));
      if (rej.length) parts.push(reasonsN(rej, st, getSh));
      /* B-37: the per-line list (names, protocols, addresses) stays behind its feature */
      if (T.rt.feature('B-37') && Array.isArray(r.rows)) parts.push(previewRowsN(r.rows));
      det.replaceChildren(...parts);
      refreshImpBtn(st, sh);
    };
    const recog = () => {
      clearTimeout(timer);
      st.text = ta.value; st.retried = false; st.notice = null;
      const my = ++st.seq;
      st.res = null; st.err = null;
      if (!ta.value.trim()) { st.previewing = false; paint(); return; }
      st.previewing = true; paint();
      timer = setTimeout(() => { preview(st, my).then(paint); }, 320);
    };
    ta.addEventListener('input', recog);
    st.repreview = () => { if (!ta.value.trim()) return Promise.resolve(); const my = ++st.seq; st.previewing = true; paint(); return preview(st, my).then(paint); };
  }
  /* (F-01: no heading sentence over the groups: 「N 个节点，按物理节点分为 M 组」 repeated the count and
     the list) */
  function groupsN(groups) {
    const list = hN('div', 'list dense tiers-imp-groups');
    groups.forEach((g, i) => {
      const name = g.label ? dataEl(g.label, 'li-t') : hN('span', 'li-t', L('tiers.import.groups.unnamed', { i: i + 1 }));
      list.append(hN('div', 'li tiers-imp-row', hN('span', 'li-main', name), hN('span', 'c-dim', L('tiers.import.groups.count', { n: g.row_indexes.length }))));
    });
    return hN('div', 'tiers-imp-g', list);
  }
  function reasonsN(rej, st, getSh) {
    const box = hN('div', 'tiers-imp-why');
    box.append(tiEl('div', 'tiers-imp-gh', 'tiers.import.why_title'));
    for (const x of rej) {
      const row = hN('div', 'tiers-imp-wr', hN('span', 'tiers-why', errText(x.error)), x.n > 1 ? hN('span', 'tiers-note', L('tiers.import.why_count', { n: x.n })) : null);
      /* a subscription address pasted as a node: offer the subscription tab, prefilled from the page's
         own input (the sidecar never sends the address back; W-18) */
      if (x.error.reason_code === 'subscription_url_not_node' && st.t !== 'vanguard') {
        const url = P.firstUrlLine(st.text);
        if (url) {
          const lb = btnN('link-btn tiers-imp-tosub', 'tiers.import.row.to_sub');
          lb.addEventListener('click', () => { st.tabs.set('sub'); setTab(st, 'sub', getSh); if (st.sub) { st.sub.url.set(url); setTimeout(() => st.sub.url.input.focus(), 30); } });
          row.append(' · ', lb);
        }
      }
      box.append(row);
    }
    return box;
  }
  /* B-37 (behind its feature): one row per recognised line */
  function previewRowsN(rows) {
    const list = hN('div', 'list dense tiers-imp-prev');
    rows.forEach(p => list.append(hN('div', 'li tiers-imp-row', hN('span', 'li-main', dataEl(p.name, 'li-t')), dataEl(p.protocol || '—', 'c-dim c-ell'), rawN(T.ui.status(p.ok ? 'ok' : 'down')))));
    return list;
  }
  function commitPaste(st, sh, b) {
    const r = st.res; if (!r || !r.accepted || (r.rejected && r.rejected.length) || st.previewing) return;
    const before = beforeIds(st.t);
    const job = T.rt.call('import.apply', { tier: st.t, mode: 'paste', plan_id: r.plan_id }, { owner: st.owner });
    T.ui.busy(b, job, { busy: L('tiers.busy.importing'), done: L('tiers.busy.imported'), fail: e => (P.planRetry(e) && !st.retried ? L('tiers.import.replanning') : importFailWord(e)), hold: 600, failHold: 2600 }).then(res => {
      setTimeout(() => { const rect = sh.el.getBoundingClientRect(); sh.close(); afterImport(st.t, before, res, rect); }, 260);
    }, err => {
      /* the plan expired or the config moved on: preview again once, then ask again */
      if (P.planRetry(err) && !st.retried) { st.retried = true; st.res = null; st.repreview().then(() => { st.notice = L('tiers.import.replanned'); st.paint(); }); }
      else if (!importSavedFail(st, sh, before, err)) { st.notice = importFail(err); st.paint(); }
    });
  }
  /* import.apply failed after it saved (PROTOCOL section 15: the reload or the Core apply failed;
     the job ends failed with saved=true, applied=false and its items): the nodes are in the config,
     so this is not a plain failure. The form closes, "saved, not applied" is said (with why), and
     the lists are read again: the new rows glow by difference (afterImport). save_outcome_unknown
     (mapping_tiers 12.3): the form keeps what was typed (it says to check the list), and the lists
     are read again too, so rows that did land glow. Returns true when the form was closed. */
  const importFailWord = e => (e && e.result && e.result.saved === true ? outcomeWord('unapplied', 'short') : importFail(e));
  function importSavedFail(st, sh, before, err) {
    const r = err && err.result;
    if (r && r.saved === true) {
      setTimeout(() => {
        if (sh.closed) return;
        const rect = sh.el.getBoundingClientRect(); sh.close();
        const why = r.apply_error && typeof r.apply_error === 'object' ? errText(r.apply_error) : null;
        afterImport(st.t, before, { ...r, applied: false, pending: false }, rect,
          { kind: 'warning', id: 'tiers-imp', titleId: 'tiers.outcome', titleParams: { kind: 'unapplied', what: 'saved' }, message: why || undefined, timeout: 8000 });
      }, 260);
      return true;
    }
    const d = P.detailError(err);
    if (d && d.reason_code === 'save_outcome_unknown') { rereadLists(st.t, before); renderList(true); }
    return false;
  }
  /* write-after-read (P4 architecture section 4 item 3): import.apply's items are batch tokens, not
     node ids. The new rows are the difference between the tier's rows before and after the import;
     the page waits for nodes.list / subscriptions.list to move on (and asks the shell to replay them
     at once when it can: rt_resync), then they glow once. */
  function afterImport(t, before, res, rect, note) {
    const n = res && Array.isArray(res.items) ? res.items.length : 0;
    const k = P.appliedOutcome(res, run());
    T.notify(note || { kind: k === 'unapplied' ? 'warning' : 'success', id: 'tiers-imp', message: L('tiers.import.done_toast', { n, first: T.rt.feature('B-14') ? 'yes' : 'no', outcome: k }) });
    rereadLists(t, before);
    clearSearch();
    if (V.drill) { V.drill = null; syncToolbar(); syncCtx(false); swapView(-1, { animate: false }); }
    renderList(true);
    syncCells(true);
  }
  /* wait for the tier's nodes.list / subscriptions.list to move on (asking the shell to replay them
     at once when it can), then the rows not in `before` glow once */
  function rereadLists(t, before) {
    V.expect = { t, before, until: performance.now() + 15000 };
    /* T.rt.requestResync: rt_resync when the shell has it, deduplicated with the rt layer's own
       refetches (T-P4-H2 item 4); never rejects */
    for (const topicName of ['nodes.list', 'subscriptions.list']) T.rt.requestResync(topicName, t);
    checkExpect();
  }
  function checkExpect() {
    const x = V.expect; if (!x) return;
    if (performance.now() > x.until) { V.expect = null; return; }
    const rows = [...(DS.nodes(x.t) || []).map(n => ({ id: n.id })), ...(DS.subs(x.t) || []).map(s => ({ id: 'g:' + s.id }))];
    const nu = P.diffNew(x.before, rows);
    if (nu.length) { V.fresh = new Set(nu); V.expect = null; }
  }

  /* ---- manual: the form import.specs describes (G19) --------------------------------------- */
  function buildManual(el, st, getSh) {
    el.replaceChildren(hN('div', 'tiers-imp-busy', hN('span', 'spin'), L('tiers.import.specs_loading')));
    loadSpecs().then(specs => {
      if (!el.isConnected) return;
      const t = st.t, form = hN('div', 'tiers-mf-box');
      const parts = [];
      if (t === 'vanguard') {
        const seg = T.html`<span class="seg" aria-label="${L('tiers.import.kind_aria')}" data-i18n-attr="aria-label:tiers.import.kind_aria"><button class="seg-btn" data-v="single" data-i18n="tiers.import.kind.single">${L('tiers.import.kind.single')}</button><button class="seg-btn" data-v="multi" data-i18n="tiers.import.kind.multi">${L('tiers.import.kind.multi')}</button></span>`;
        /* (F-01: the line beside the choice that explained single and multi protocol is gone) */
        T.ui.seg(seg, { value: 'single', onChange: v => {
          show(v); M.anim(form, [{ opacity: 0, transform: 'translateY(4px)' }, { opacity: 1, transform: 'none' }], { duration: 200, easing: M.ease.out });
        } });
        parts.push(hN('div', 'tiers-imp-kind', seg));
      }
      el.replaceChildren(...parts, form);
      const show = kind => { if (st.manual) st.manual.clear(); st.manual = kind === 'multi' ? multiForm(form, st, specs) : singleForm(form, st, specs); };
      show('single');
    }, e => { if (el.isConnected) el.replaceChildren(T.ui.callout({ kind: 'error', small: true, title: L('tiers.import.specs_failed'), message: errText(e) })); });
  }
  /* a choice's words: <label_id>.choice.<value> (T-P4-04e catalog.*); a value without one is data */
  const choiceN = (f, v) => { const id = `${f.label_id}.choice.${v}`; return T.i18n.hasAny(id) ? L(id) : String(v); };
  /** one spec field; api.values holds what is typed (memory only, dropped with the sheet) */
  function specField(f, fields, api, prefix) {
    const id = (prefix || '') + f.id, req = P.fieldRequired(f, fields, api.values);
    const lab = hN('label', 'field-lab', tiEl('span', '', f.label_id));
    if (!req) lab.append(' ', tiEl('span', 'opt-l', 'tiers.form.optional'));
    const msg = hN('div', 'field-msg');
    const w = hN('div', 'field'); w.dataset.f = id;
    const c = f.conditions || {};
    let ctl;
    if (f.type === 'choice' || f.type === 'multichoice') {
      const btn = T.html`<button class="select" type="button"></button>`;
      w.append(lab, btn, msg);
      /* never prefilled: an implicit default only drives the conditions (P4 contract memo, Shadowsocks) */
      const sel = T.ui.select(btn, { value: api.values[f.id] == null ? null : api.values[f.id], placeholder: c.implicit_default != null ? L('tiers.form.choice_default', { value: choiceN(f, c.implicit_default) }) : L('tiers.form.choose'),
        options: (f.choices || []).map(v => ({ value: v, label: choiceN(f, v) })), onChange: v => { api.values[f.id] = v; w.classList.remove('is-invalid'); api.touch(); api.rerender(); } });
      ctl = { el: w, error: m => { w.classList.toggle('is-invalid', !!m); msg.replaceChildren(m ? fragN(iconN('error', 14), m) : ''); }, focus: () => btn.focus(), sel };
    } else if (f.type === 'boolean') {
      const tg = T.html`<label class="toggle tiers-tg sm"><span class="toggle-t"></span><button class="switch" type="button" role="switch" aria-checked="${api.values[f.id] ? 'true' : 'false'}"></button></label>`;
      tg.querySelector('.toggle-t').append(tiEl('span', '', f.label_id));
      const sw = tg.querySelector('.switch'); T.ti(sw, f.label_id, null, { attr: 'aria-label' });
      T.ui.switch(sw, {});
      sw.addEventListener('change', e => { api.values[f.id] = !!(e.detail && e.detail.checked); api.touch(); });
      w.append(tg);
      ctl = { el: w, error: () => {}, focus: () => sw.focus() };
    } else {
      const area = f.type === 'textarea';
      const box = area ? T.html`<div class="input area mono"><textarea rows="3" spellcheck="false"></textarea></div>` : T.html`<div class="input${f.type === 'integer' ? ' num' : ' mono'}"><input autocomplete="off" spellcheck="false"></div>`;
      const input = box.querySelector('input, textarea');
      if (api.values[f.id] != null) input.value = String(api.values[f.id]);
      w.append(lab, box, msg);
      if (f.type === 'secret') { input.type = 'password'; addEye(w, input); }
      const fld = T.ui.field(w, {});
      if (f.type === 'integer') T.ui.number(box, { min: c.min != null ? c.min : -Infinity, max: c.max != null ? c.max : Infinity });
      input.addEventListener('input', () => { api.values[f.id] = input.value; api.touch(); });
      ctl = { el: w, error: m => fld.error(m), focus: () => input.focus() };
    }
    api.fields[f.id] = ctl;
    return w;
  }
  /* the fields of one protocol: required first, then the rest; advanced ones in a fold (G19). The
     visible set follows the conditions (visible_when / required_when) as choices change; notices
     (catalog.notice.<code>) show under the fields while their condition holds. */
  function paintSpec(box, spec, api, opts) {
    opts = opts || {};
    api.fields = {};
    if (!spec) { box.replaceChildren(); return; }
    const { main, advanced, all } = P.formFields(spec, api.values, opts);
    const skip = new Set(opts.skip || []);
    const grid = hN('div', 'fields tiers-f2');
    main.filter(f => !skip.has(f.id)).forEach(f => grid.append(specField(f, all, api, opts.prefix)));
    const kids = [grid];
    const adv = advanced.filter(f => !skip.has(f.id));
    if (adv.length) {
      const fold = T.html`<button class="tiers-fold" type="button" aria-expanded="${api.advOpen ? 'true' : 'false'}" data-press>${I('chevron', 14, 'fold-chev')}<span data-i18n="tiers.form.advanced">${L('tiers.form.advanced')}</span></button>`;
      const ag = hN('div', 'fields tiers-f2');
      ag.hidden = !api.advOpen;
      adv.forEach(f => ag.append(specField(f, all, api, opts.prefix)));
      fold.addEventListener('click', () => { api.advOpen = !api.advOpen; fold.setAttribute('aria-expanded', String(api.advOpen)); ag.hidden = !api.advOpen; });
      kids.push(fold, ag);
    }
    for (const f of main.concat(advanced)) for (const code of P.fieldNotices(f, all, api.values)) {
      const nid = `catalog.notice.${code}`;
      if (T.i18n.hasAny(nid)) kids.push(T.ui.callout({ kind: 'warning', small: true, message: L(nid) }));
    }
    box.replaceChildren(...kids);
  }
  /* local checks (required, integer range); the rest is the sidecar's (error.field lands on the field) */
  function checkSpec(api, spec, opts) {
    const probs = P.localProblems(spec, api.values, opts);
    let first = null;
    for (const [id, ctl] of Object.entries(api.fields)) {
      const p = probs[id];
      if (!p) { ctl.error(null); continue; }
      const f = spec.fields.find(x => x.id === id), c = (f && f.conditions) || {};
      ctl.error(p === 'required' ? L('tiers.form.required', { name: L(f.label_id) }) : L('tiers.form.range', { min: c.min != null ? c.min : 0, max: c.max != null ? c.max : 0, has_max: c.max != null ? 'yes' : 'no' }));
      if (!first) first = ctl;
    }
    if (first) first.focus();
    return !first;
  }
  function singleForm(form, st, specs) {
    const protos = P.protocolsForTier(specs, st.t);
    const api = { proto: null, values: {}, fields: {}, advOpen: false, touched: false };
    api.touch = () => { api.touched = true; };
    api.dirty = () => api.touched;
    api.clear = () => { api.values = {}; api.fields = {}; };
    const top = T.html`<div class="field" data-f="protocol"><label class="field-lab" data-i18n="tiers.form.proto.label">${L('tiers.form.proto.label')}</label><button class="select" type="button"></button><div class="field-msg"></div></div>`;
    const body = hN('div', 'tiers-pf');
    form.replaceChildren(hN('div', 'fields tiers-mf', top), body);
    /* the protocol's message line: empty, or 先选择协议 / a sidecar error (F-01: the hint that the
       fields follow the protocol is gone) */
    const msg = top.querySelector('.field-msg');
    const specOf = () => protos.find(p => p.id === api.proto) || null;
    api.rerender = () => paintSpec(body, specOf(), api);
    T.ui.select(top.querySelector('.select'), { placeholder: L('tiers.form.proto.ph'), options: protos.map(p => ({ value: p.id, label: L(p.label_id) })),
      onChange: v => { api.proto = v; api.touch(); top.classList.remove('is-invalid'); setT(msg, ''); api.rerender(); } });
    api.validate = () => {
      if (!api.proto) { top.classList.add('is-invalid'); setT(msg, L('tiers.form.proto.missing')); top.querySelector('.select').focus(); return false; }
      return checkSpec(api, specOf(), {});
    };
    api.args = () => ({ tier: st.t, mode: 'manual', manual: { protocol: api.proto, fields: P.applyFields(specOf(), api.values) } });
    api.fieldError = (field, text) => {
      const id = String(field || '').replace(/^manual\.(fields\.)?/, '');
      if (id === 'protocol') { top.classList.add('is-invalid'); setT(msg, text); return true; }
      const ctl = api.fields[id]; if (ctl) { ctl.error(text); ctl.focus(); return true; }
      return false;
    };
    return api;
  }
  /* a multi-protocol node (Vanguard): the shared name and server, then the chain (2-16 protocols that
     allow it, no duplicate); each row carries its port and, folded, its own parameters */
  function multiForm(form, st, specs) {
    const protos = P.protocolsForTier(specs, 'vanguard', { chain: true });
    const api = { fields: {}, rows: [], touched: false };
    api.touch = () => { api.touched = true; };
    api.dirty = () => api.touched;
    api.clear = () => { api.rows.forEach(r => { r.values = {}; }); api.rows = []; };
    const nf = textField('name', 'tiers.form.name.label', { ph: L('tiers.import.name_ph_multi') });
    const hf = textField('host', 'tiers.form.host.label', { mono: true, ph: L('tiers.form.host.ph_multi') });
    api.name = T.ui.field(nf.el, { validate: v => (!String(v).trim() ? L('tiers.form.name.empty') : null) });
    api.host = T.ui.field(hf.el, { validate: v => (!String(v).trim() ? L('tiers.form.host.empty') : null) });
    const list = hN('div', 'tiers-ce-list'), add = hN('div', 'tiers-ce-add'), msg = hN('div', 'field-msg');
    const chain = hN('div', 'field tiers-mf-chain', hN('label', 'field-lab', tiEl('span', '', 'tiers.form.chain.label')), list, add, msg);
    form.replaceChildren(hN('div', 'fields tiers-mf tiers-f2', nf.el, hf.el), chain);
    form.addEventListener('input', api.touch);
    const specOf = id => protos.find(p => p.id === id) || null;
    const pname = id => { const s = specOf(id); return s ? L(s.label_id) : id; };
    /* the chain's message line: a problem, or how many protocols are still needed (F-01: no line on
       the order once there are two) */
    const paintMsg = err => { chain.classList.toggle('tiers-ce-err', !!err); msg.replaceChildren(err ? fragN(iconN('error', 14), err) : api.rows.length < 2 ? L('tiers.chain.need_more', { n: 2 - api.rows.length }) : ''); };
    const renum = () => { T.$$('.tiers-ce', list).forEach((e, i) => { e.querySelector('.tiers-rank').textContent = String(i + 1); }); api.rows.sort((a, b) => [...list.children].indexOf(a.el) - [...list.children].indexOf(b.el)); };
    const rowOfP = id => {
      const r = { p: id, values: {}, fields: {}, advOpen: false, touch: api.touch };
      const port = T.html`<div class="input sm num tiers-ce-port"><input inputmode="numeric"></div>`;
      const pin = port.querySelector('input');
      const tg = T.html`<button class="tiers-ce-tg" type="button" aria-expanded="false" data-press><span class="lab" data-i18n="tiers.chain.params">${L('tiers.chain.params')}</span>${I('chevron-down', 13, 'tiers-ce-chev')}</button>`;
      const rm = iconBtn('icon-btn sm', 'close', 'tiers.chain.remove');
      const grip = iconBtn('grip', 'grip', 'tiers.chain.grip_aria_plain');
      const pf = hN('div', 'tiers-ce-pf'); pf.hidden = true;
      const e = hN('div', 'tiers-ce has-pf', grip, hN('span', 'tiers-rank'), hN('span', 'tiers-ce-nm', hN('span', 'tiers-ce-p', pname(id)), tg), port, rm, pf);
      e.dataset.p = id;
      tiAttr(pin, ['aria-label', 'tiers.chain.port_aria_plain']);
      pin.addEventListener('input', () => { r.values.port = pin.value; api.touch(); });
      r.rerender = () => paintSpec(pf, specOf(id), r, { shared: true, skip: ['port'], prefix: id + '.' });
      r.rerender();
      tg.addEventListener('click', () => { const on = pf.hidden; pf.hidden = !on; tg.setAttribute('aria-expanded', String(on)); e.classList.toggle('is-open', on); });
      rm.addEventListener('click', () => { M.list(list, () => e.remove(), { enter: false }); api.rows = api.rows.filter(x => x !== r); renum(); paintMsg(null); api.touch(); });
      r.el = e; r.port = pin; r.open = () => { pf.hidden = false; tg.setAttribute('aria-expanded', 'true'); e.classList.add('is-open'); };
      return r;
    };
    const addRow = id => { const r = rowOfP(id); api.rows.push(r); list.append(r.el); renum(); paintMsg(null); return r; };
    const initial = ['hysteria2', 'vless_reality'].filter(id => specOf(id));
    (initial.length === 2 ? initial : protos.slice(0, 2).map(p => p.id)).forEach(addRow);
    const addBtn = btnN('btn btn-sm', 'tiers.chain.add', { icon: 'plus' });
    add.append(addBtn);
    addBtn.addEventListener('click', e => {
      const have = api.rows.map(r => r.p);
      T.ui.menu(e.currentTarget, protos.filter(p => !have.includes(p.id)).map(p => ({ label: L(p.label_id), value: p.id })), { placement: 'bottom-start' })
        .then(it => { if (!it) return; const r = addRow(it.value); r.open(); api.touch(); });
    });
    T.ui.sortable(list, { items: '.tiers-ce', handle: '.grip', onEnd: () => { renum(); api.touch(); } });
    api.validate = () => {
      const ok = [api.name.validate(), api.host.validate()];
      let err = null;
      if (api.rows.length < 2) err = L('tiers.chain.min_two', { n: 2 });
      else if (api.rows.length > 16) err = L('tiers.chain.max', { n: 16 });
      for (const r of api.rows) {
        const pv = vPort(r.port.value);
        r.port.closest('.input').classList.toggle('is-invalid', !!pv);
        if (pv && !err) err = msgS(msgOf('tiers.chain.port_invalid', { proto: pname(r.p) }));
        if (!checkSpec(r, specOf(r.p), { shared: true })) { r.open(); if (!err) err = msgS(msgOf('tiers.chain.fill_params', { proto: pname(r.p) })); }
      }
      paintMsg(err);
      return ok.every(Boolean) && !err;
    };
    api.args = () => ({ tier: 'vanguard', mode: 'chain', chain: api.rows.map((r, i) => ({ protocol: r.p,
      fields: Object.assign(i === 0 ? { name: api.name.value.trim(), server: api.host.value.trim() } : {}, P.applyFields(specOf(r.p), r.values, { shared: true })) })) });
    api.fieldError = (field, text) => {
      const m = /^chain\.(\d+)(?:\.(?:fields\.)?(.+))?$/.exec(String(field || ''));
      if (field === 'name') { api.name.error(text); return true; }
      if (field === 'server') { api.host.error(text); return true; }
      if (!m) { paintMsg(text); return true; }
      const r = api.rows[+m[1]]; if (!r) { paintMsg(text); return true; }
      const ctl = m[2] && r.fields[m[2]];
      if (ctl) { r.open(); ctl.error(text); ctl.focus(); } else paintMsg(text);
      return true;
    };
    return api;
  }
  function commitManual(st, sh, b) {
    const m = st.manual; if (!m || !m.validate()) return;
    const before = beforeIds(st.t);
    const job = T.rt.call('import.apply', m.args(), { owner: st.owner });
    T.ui.busy(b, job, { busy: L('tiers.busy.importing'), done: L('tiers.busy.imported'), fail: e => importFailWord(e), hold: 600, failHold: 2600 }).then(res => {
      setTimeout(() => { const rect = sh.el.getBoundingClientRect(); if (st.manual) st.manual.clear(); sh.close(); afterImport(st.t, before, res, rect); }, 260);
    }, err => {
      if (importSavedFail(st, sh, before, err)) return;
      const d = P.detailError(err); if (d && d.field) m.fieldError(d.field, errText(d));
    });
  }

  /* ---- subscription: import.apply mode subscriptions -------------------------------------- */
  /* F-01: Bastion's safety note is the sheet's subtitle (no second warning in this tab); the address
     field's hint and the note that subscriptions update on their own are gone */
  function buildSub(el, st) {
    const parts = [];
    const uf = textField('url', 'tiers.form.url.label', { mono: true, ph: L('tiers.import.sub.url_ph') });
    st.sub = { url: T.ui.field(uf.el, { validate: v => (!String(v).trim() ? L('tiers.form.url.empty') : !/^https?:\/\/[^\s/]+\.[^\s/]+/i.test(String(v).trim()) ? L('tiers.form.url.scheme') : null) }) };
    parts.push(uf.el);
    /* B-09 (a name of your own) and B-08 (the interval) stay behind their features */
    if (T.rt.feature('B-09')) { const nf = textField('sname', 'tiers.form.name.label', { opt: true, ph: L('tiers.import.sub.name_ph'), hint: L('tiers.import.sub.name_hint') }); st.sub.name = T.ui.field(nf.el, {}); parts.push(nf.el); }
    if (T.rt.feature('B-08')) {
      const af = T.html`<div class="field" data-f="auto"><label class="field-lab" data-i18n="tiers.form.auto.label">${L('tiers.form.auto.label')}</label><button class="select" type="button"></button><div class="field-msg"></div></div>`;
      st.sub.auto = T.ui.select(af.querySelector('.select'), { value: '6h', options: ['30m', '6h', '12h', '1d', 'manual'].map(k => ({ value: k, label: L(`tiers.sub.auto.${k}`) })) });
      parts.push(af);
    }
    el.replaceChildren(...parts);
  }
  function commitSub(st, sh, b) {
    const s = st.sub; if (!s || !s.url.validate()) { if (s) s.url.input.focus(); return; }
    const url = s.url.value.trim(), before = beforeIds(st.t);
    const job = T.rt.call('import.apply', { tier: st.t, mode: 'subscriptions', urls: [url] }, { owner: st.owner });
    T.ui.busy(b, job, { busy: L('tiers.busy.fetching_sub'), done: r => { const x = r && r.subscriptions && r.subscriptions[0]; return L('tiers.import.sub.added', { n: x ? x.total : 0 }); }, fail: e => importFail(e), hold: 700, failHold: 2600 }).then(res => {
      setTimeout(() => { const rect = sh.el.getBoundingClientRect(); s.url.input.value = ''; sh.close(); afterImport(st.t, before, res, rect); }, 300);
    }, err => {
      /* saved, but its first fetch failed (the job failed with saved=true): the subscription is there */
      const r = err && err.result;
      if (r && r.saved) {
        const x = Array.isArray(r.subscriptions) ? r.subscriptions[0] : null;
        setTimeout(() => {
          s.url.input.value = ''; sh.close();
          T.notify({ kind: 'warning', id: 'tiers-imp', message: L('tiers.import.sub.added_failed', { reason: errText((x && x.error) || P.detailError(err)) }), timeout: 8000 });
          rereadLists(st.t, before); renderList(true);
        }, 300);
        return;
      }
      const d = P.detailError(err); if (d && d.field === 'urls') s.url.error(errText(d));
    });
  }

  /* ================================================================ runtime reactions */
  /* Runtime data changed (a topic, a store key): one repaint per turn. Value-only updates of the list
     (nodes.list patches) keep the order on screen and come at most once a second (mapping_tiers 4.1:
     merged to 1 s, no re-sort); anything else repaints at once. A page that is not shown repaints when
     it is shown again. */
  const SOFT = new Set(['nodes-patch', 'tick']);
  function changed(kind) {
    const kinds = V.dirty || (V.dirty = new Set());
    kinds.add(kind);
    const soft = [...kinds].every(k => SOFT.has(k));
    const wait = soft ? Math.max(0, 1000 - (performance.now() - V.lastFlush)) : 0;
    if (V.flushT) { if (wait >= V.flushWait) return; clearTimeout(V.flushT); }
    V.flushWait = wait;
    V.flushT = setTimeout(flush, wait);
  }
  function flush() {
    V.flushT = 0;
    const kinds = V.dirty || new Set(); V.dirty = new Set();
    V.lastFlush = performance.now();
    if (!R.root) return;
    checkExpect();
    if (!visible) { V.stalePaint = true; return; }
    const soft = [...kinds].every(k => SOFT.has(k));
    syncCells(!soft);
    syncCtx(!soft);
    /* the Bastion view follows its switch (tiers.summary), wherever it was flipped */
    if (V.tier === 'bastion' && !V.drill && R.bxOn !== bastionOn()) { closePanel(); syncToolbar(true); swapView(R.bxOn ? -1 : 1, { animate: true }); }
    else renderList(!soft && !V.fresh ? false : !!V.fresh, { keepOrder: soft || !kinds.has('nodes') });
    if (V.pr) refreshPanel();
    if (V.drill && V.drill.kind === 'sub' && !findSub(V.drill.id)) drillOut(true);
  }
  function tick() {
    if (!visible) return;
    if (V.pr && V.pr.kind === 'node') paintPanelLive(V.pr);
    if (R.list && R.list._rows) R.list._rows.forEach(r => { if (r._kind === 'group' && r._e && Number.isInteger(r._e.g.last_refresh_at_ms)) setT(r.querySelector('.tiers-gup'), L('tiers.sub.updated', { ago: F.ago(r._e.g.last_refresh_at_ms) })); });
  }
  /* rt:reset. A generation change (rt:reset runtime_gone / a new epoch): node ids, the subscription
     results and the import plan belong to the lost generation. The side panel closes (its owner is
     revoked: revealed values and late answers are dropped; the lost sidecar's diagnosis went with it,
     so no cancel is sent); an open import sheet keeps what was typed and previews it again; an
     open 管理订阅 drawer closes too (T-P4-H2). A
     same-epoch reconnect changes nothing (the panel stays; criticism B9). */
  function onReset(e) {
    if (e && e.generation_changed) {
      const was = !!(V.panel && !V.panel.closed);
      if (V.pr || was) { closePanel({ generation: true }); if (was) T.notify({ kind: 'info', id: 'tiers-gone', message: L('tiers.panel.reset') }); }
      const im = V.imp;
      if (im && im.sh && !im.sh.closed) { im.st.owner.revoke(); im.st.owner = T.rt.owner('tiers-import'); im.st.res = null; if (im.st.repreview) im.st.repreview(); }
      /* an open 管理订阅 drawer closes, as the side panel does (its subscription id, its results
         and its revealed address belong to the lost generation; PROTOCOL section 12): its onClose
         revokes the owner and drops the address. 05b review cont. 1 S2, 8th-wave ruling T-P4-H2
         item 4 */
      const mg = V.mng;
      if (mg) {
        const open = !!(mg.drawer && !mg.drawer.closed);
        mg.secret = null; if (mg.forget) mg.forget();
        if (open) mg.drawer.close(); else { mg.owner.revoke(); V.mng = null; }
        if (open && !was) T.notify({ kind: 'info', id: 'tiers-gone', message: L('tiers.panel.reset') });
      }
      V.results.clear(); V.rev = {}; V.expect = null; V.fresh = null; V.cfg = null;
      if (V.busySubs) V.busySubs.clear();
    }
    changed('reset');
  }
  function layout() { placeThumb(false); placePill(false); placePanel(); fitSearch(); }
  function onDocKey(e) {
    if (e.key !== 'Escape' || e.defaultPrevented || !V.drill || !visible || T.overlayOpen() || T.cmd.isOpen) return;
    if (e.target && e.target.closest && e.target.closest('input, textarea, [contenteditable="true"], .menu, .pop')) return;
    drillOut(true, true);
  }
  function wire(life) {
    /* the dot-form pill takes the pointer (its hover tip): a click on it is a click on its cell */
    R.sw.addEventListener('click', e => { const c = e.target.closest('.tiers-cell') || (e.target.closest('.tiers-pill') && R.pillAt ? R.cells[R.pillAt].cell : null); if (c) requestTier(c.dataset.tier); });
    R.sw.addEventListener('keydown', e => {
      const i = TIERS.indexOf(document.activeElement && document.activeElement.dataset ? document.activeElement.dataset.tier : ''); if (i < 0) return;
      let j = null;
      if (e.key === 'ArrowRight') j = (i + 1) % 4; else if (e.key === 'ArrowLeft') j = (i + 3) % 4; else if (e.key === 'Home') j = 0; else if (e.key === 'End') j = 3;
      else if (e.key === 'ArrowDown' && R.list) { e.preventDefault(); const r = R.list.querySelector('.li'); if (r) R.nav.select(r, { focus: true }); return; }
      if (j == null) return;
      e.preventDefault(); R.cells[TIERS[j]].cell.focus(); requestTier(TIERS[j]);
    });
    R.search = T.ui.search(R.searchEl, { onInput: q => { if (V.mute) return; V.q = q; renderList(true); } });
    R.sort = T.ui.seg(R.sortEl, { value: V.sort, onChange: v => { V.sort = v; renderList(true); } });
    R.imp.addEventListener('click', () => openImport(R.imp));
    /* the runtime's data: keyed lists per tier, the config items, Gateway, the pool, the route */
    TIERS.forEach(t => {
      T.rt.topic('nodes.list', t).sub((v, m) => { if (m && Number.isInteger(m.rev)) V.rev[t] = m.rev; changed(m && m.kind === 'patch' ? 'nodes-patch' : 'nodes'); });
      T.rt.topic('subscriptions.list', t).sub(() => changed('subs'));
    });
    T.rt.topic('config.items').sub(() => changed('items'));
    T.rt.topic('gateway.current').sub(() => changed('gateway'));
    T.rt.topic('bastion.pool').sub(() => changed('pool'));
    T.rt.topic('route.published').sub(() => changed('route'));
    ['rt:state', 'rt:phase', 'rt:collect', 'rt:tiers', 'rt:tier', 'rt:restart'].forEach(ev => life.on(ev, () => changed(ev)));
    life.on('rt:stale', e => { if (e && /^(nodes\.list|subscriptions\.list|bastion\.pool|gateway\.current|config\.items)/.test(String(e.topic))) changed('stale'); });
    life.on('rt:config', e => { if (!V.cfg || (e && e.rev > V.cfg.rev)) loadConfig(); });
    life.on('rt:caps', () => {
      loadConfig();
      if (R.feat !== featKey()) { syncTestBtn(); relocalize(); }
      changed('caps');
    });
    life.on('rt:reset', onReset);
    /* a language switch: the words that are not registered for relabel are redone (relocalize) */
    life.on('i18n:change', relocalize);
  }

  /* a language switch (i18n:change): static words relabel in place (data-i18n); the rest is redone
     here: the cells, the context line, the toolbar, the view and the open side panel. An open edit
     keeps its fields; an open import sheet redoes its title, subtitle, footer and paste lines. The
     view is rebuilt without ending the jobs in flight (swapView relabel). */
  function relocalize() {
    if (!R.root) return;
    if (visible) T.ui.closeFloats();
    syncCells(false); syncCtx(false); syncToolbar(false);
    const top = R.scroll ? R.scroll.scrollTop : 0;
    swapView(0, { animate: false, relabel: true });
    if (R.scroll) R.scroll.scrollTop = top;
    const sel = V.panel && !V.panel.closed ? rowOf(V.pkey) : null;
    if (sel && R.nav) R.nav.select(sel);
    if (V.panel && !V.panel.closed && V.pr) { if (V.edit) relabelEdit(); else { const it = resolve(V.pr.key); if (it) fillPanel(it, 0); } }
    relabelImport();
    if (visible) layout();
  }

  /* palette: jump to a tier from anywhere; import while the tiers page is shown. B-05 / B-06 commands
     stay behind their features. Labels and keywords are evaluated when the palette opens. */
  setTimeout(() => T.cmd.register([
    ...TIERS.map(t => ({ id: 'tiers-go-' + t, group: 'profiles', tier: t, label: () => tierLabel(t), icon: D.tier(t).icon, keywords: () => `${t} ${tierName(t)} ${L('tiers.cmd.keywords.go')}`, run: () => T.go('profiles', { tier: t }) })),
    { id: 'tiers-test', group: 'profiles', label: () => L('tiers.cmd.test'), icon: 'pulse', keywords: () => `test latency ${L('tiers.cmd.keywords.test')}`, when: () => T.rt.feature('B-05') && T.router.current === 'profiles' && !!R.test, run: () => {} },
    { id: 'tiers-updall', group: 'profiles', label: () => L('tiers.cmd.update_all'), icon: 'refresh', keywords: () => `update subscription ${L('tiers.cmd.keywords.update_all')}`, when: () => T.rt.feature('B-06'), run: () => {} },
    { id: 'tiers-import', group: 'profiles', label: () => L('tiers.cmd.import', { tier: tierName(V.tier) }), icon: 'import', keywords: () => `import ${L('tiers.cmd.keywords.import')}`, when: () => T.router.current === 'profiles' && !R.imp.hidden, run: () => openImport(R.imp) },
  ]), 0);

  T.page('profiles', {
    titleId: 'shell.page.profiles.title',
    render(root, ctx) {
      build(root); wire(ctx.life);
      loadConfig();
      syncCells(false); syncCtx(false); syncToolbar();
      swapView(0, { animate: false });
    },
    enter(ctx) {
      visible = true;
      const p = ctx.params || {};
      /* the data moved on while the page was away: one repaint, the Bastion view rebuilt if its switch flipped */
      if (V.stalePaint) {
        V.stalePaint = false;
        if (V.tier === 'bastion' && !V.drill && R.bxOn !== bastionOn()) { closePanel(); swapView(0, { animate: false }); }
        if (V.pr) refreshPanel();
      }
      const want = TIERS.includes(p.tier) ? p.tier : null;
      if (ctx.again) {
        if (want && (want !== V.tier || V.drill)) requestTier(want);
      } else {
        /* writes first, then one geometry pass (the router reads the landing rect right after) */
        if (want && (want !== V.tier || V.drill)) setTier(want, { animate: false, quiet: true });
        else renderList(false);
        R.thumb.setAttribute('data-flip-target', 'tier:' + V.tier);
        syncCells(false, true); syncCtx(false); syncToolbar();
        layout();
        if (p.flipFrom && !M.reduced) landFrom(p.flipFrom);
      }
      /* arriving from the keyboard (a tier link, a banner action, the palette): focus lands on the tier's tab */
      ctx.landing = R.cells[want || V.tier].cell;
      ctx.scope.listen(window, 'resize', T.util.raf(layout));
      ctx.scope.listen(document, 'keydown', onDocKey);
      ctx.scope.every(1000, tick);
      // (T-P4-09: T.util.fontsReady, not document.fonts.ready, which lays out the document when read)
      if (!ctx.again) T.util.fontsReady().then(() => { if (visible) layout(); });
    },
    leave() { visible = false; V.gen++; clearTimeout(R.settleT); },
    beforeLeave() { return V.edit && V.edit.dirty ? confirmDiscard() : true; },
  });
})();
