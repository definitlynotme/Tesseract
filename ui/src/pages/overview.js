/* ==========================================================================
   pages/overview.js — 总览
   hero + run control · 当前出口 · tier deck · 流量 · status strip · 协议分布
   (layout and the reasoning behind it: css/pages/overview.css, DESIGN.md §4.4)
   Data (T-P4-05a): the store keys rt/project.js writes from the v1 topics, the raw DTOs
   of the topic cache for the facts the projection does not carry (T.rt.topic), two queries
   (app.info listeners, config.snapshot ui.overview_auto_exit_lookup) and the page commands
   (T.rt.call: service.start / stop / restart, exit.verify, mode.set). The derivations
   (hero, run pill, exit card, deck, strip, protocol mix, chart) are rt/project_overview.js.
   A topic with no value in this epoch is unknown (a dash, never a placeholder); a stale one
   keeps its last value and says 数据未更新. Nothing here owns runtime state except view-only
   details (pill beat, chart range, pending confirmation, the last command's note).
   Words: the overview.* catalog (T-P4-04b, T-P4-05a). Static text is registered with T.ti
   (or txt() when it sits beside an icon); text that follows state is recomputed
   by the sync functions, which run again on i18n:change. Sentences with markup
   are T.tx messages with tag placeholders, rendered as DOM: parameters (node
   names, addresses) are text nodes, never parsed. Data runs carry translate="no".
   Motion of a start (F-06, DESIGN.md §7 rule 10): nothing jumps when a block grows (glide), the
   hero's name changes character by character (charRoll), and when a real start ends the page
   lights up outward from the run pill (T.motion.wave), the chart's colour runs across its history
   and the white running pill opens out of the full one.
   ========================================================================== */
import {
  currentNode, deckOrder, deckPill, deckStat, dotsOf, downReason, drawable, breaksOf, exitCard, firstTier, foldTick,
  gatewayRow, heroOf, ksKind, mixedListener, modeHint, modeOutcome, pillMode, poolCell, protoMix, readinessKind, routeLine,
} from '../rt/project_overview.js';
import { modeSwitchOn, modeAfterSwitch } from '../data/static.js';

(function () {
  'use strict';
  const T = window.T, D = T.data, S = T.store, M = T.motion, F = T.fmt;
  const I = (n, s, c) => T.icon(n, s || 16, c);
  let R = {};
  let visible = false;
  let deckExt = null; // T.ext 'overview/deck' (design target only; null in the app target)

  /* the three start phases are a front-end static definition (mapping_overview §5);
     their words are overview.phase.<key>.label (the pill says the phase; F-01: no second line
     repeats it in the hero or on the deck). The fill jumps to a phase's level when
     service.state reaches it and creeps on slowly inside the phase (B3, R9: a real phase can
     last tens of seconds) */
  const PHASES = ['collect', 'verify', 'ready'];
  const FILL_AT = [0.12, 0.45, 0.88], FILL_TOP = [0.4, 0.82, 0.97], CREEP_MS = 40000;
  const phaseKey = ph => PHASES[Math.max(0, Math.min(PHASES.length - 1, ph))];
  const phaseLabel = ph => T.t(`overview.phase.${phaseKey(ph)}.label`);
  const phaseNow = () => Math.max(0, Number(S.get('phase')) || 0);

  /* ---- runtime data (T-P4-05a) ------------------------------------------------------------
     raw(topic, key): the topic cache's DTO (undefined = nothing in this epoch: unknown);
     stale(topic): the projection's stale flag of a topic that has a value (stale.js);
     can(cmd): the sidecar registered it (hello.caps). B items are T.rt.feature('B-xx') with a
     literal name at each site (both keys, P4 arch §5; scan.mjs --rule features reads the call) */
  const raw = (name, key) => (T.rt && T.rt.topic ? T.rt.topic(name, key).get() : undefined);
  const known = name => raw(name) !== undefined;
  const stale = name => !!(S.get('stale') || {})[name] && known(name);
  // a keyed topic (nodes.list/<tier>): stale.js keys it "<topic>/<key>"
  const staleKeyed = (name, key) => !!(S.get('stale') || {})[`${name}/${key}`] && raw(name, key) !== undefined;
  const can = cmd => !!(T.rt && typeof T.rt.can === 'function' && T.rt.can(cmd));
  const runNow = () => S.get('run') || 'unknown';
  // the page is on screen: shown, window visible (store presence from the shell contract, T-P4-06)
  const onScreen = () => visible && !document.hidden && (S.get('presence') || {}).visible !== false;
  const ignorable = err => !!err && ['cancelled', 'superseded', 'runtime_gone', 'owner_revoked'].includes(err.code);
  const errText = err => (T.rt && T.rt.errorText ? T.rt.errorText(err) : String(err && err.code || ''));
  // the short word of an exit reason (badge, one line) and its full sentence (tip, sub line)
  const exitShort = code => T.rt.reasonText('overview.exit_reason', code);
  const exitLong = code => T.rt.errorText({ code: 'internal', message_key: T.i18n.hasAny(`errors.exit.${code}`) ? `errors.exit.${code}` : 'errors.exit.unavailable' });

  /* page-local facts: what the page itself did or asked (never runtime state) */
  let listen = null, autoExit = null, infoEpoch = null;   // app.info mixed listener · ui.overview_auto_exit_lookup
  let stopFail = null;            // {error}: the last service.stop failed (G05: 停止失败 · 重试)
  let runNote = null, runNoteT = 0;   // {text}: a refused start / stop / restart, for a few seconds (S-R5, S-R6)
  let modeNote = null, modeNoteT = 0; // a mode.set outcome (S-M3..S-M7), for a few seconds
  let lostGen = false;            // a runtime generation ended: 正在重新连接运行时 (S-R7)
  const ownExit = new Set();      // request ids of this window's exit.verify (R3, when trigger is absent)

  /* the queries: once per epoch (a same-epoch reconnect replays hello: nothing is asked again);
     config.snapshot again when the configuration changed */
  // each query's latest request number: an older reply that arrives after a newer request is
  // dropped (two rt:config in a row must not let the first reply win; review 05a S3)
  const asked = { info: 0, config: 0 };
  function fetchInfo(force) {
    if (!T.rt || T.rt.state !== 'live') return;
    const ep = T.rt.epoch;
    if (!force && infoEpoch === ep) return;
    infoEpoch = ep;
    if (!force && can('app.info')) {
      const n = ++asked.info;
      T.rt.query('app.info', {}).then(d => { if (T.rt.epoch !== ep || n !== asked.info) return; listen = mixedListener(d && d.listeners); syncStrip(onScreen()); }, () => {});
    }
    if (can('config.snapshot')) {
      const n = ++asked.config;
      T.rt.query('config.snapshot', {}).then(d => {
        if (T.rt.epoch !== ep || n !== asked.config) return;
        const ui = d && d.values && d.values.ui;
        autoExit = ui && typeof ui.overview_auto_exit_lookup === 'boolean' ? ui.overview_auto_exit_lookup : null;
        syncExit(onScreen());
      }, () => {});
    }
  }
  function note(kind, text, ms) {
    if (kind === 'run') { clearTimeout(runNoteT); runNote = text ? { text } : null; if (text) runNoteT = setTimeout(() => { runNote = null; syncHero(onScreen()); }, ms || 6000); syncHero(onScreen()); }
    else { clearTimeout(modeNoteT); modeNote = text || null; if (text) modeNoteT = setTimeout(() => { modeNote = null; syncStrip(onScreen()); }, ms || 6000); syncStrip(onScreen()); }
  }

  /* the projected keys of a topic count only once this epoch delivered the topic (S-R7; review
     05a S4): the projection's reset() leaves carrier / tiers / route / gateway as they were, so
     a new epoch's service.state arriving first must not bring back the last generation's carrier,
     counts or node names */
  const carrierNow = () => (known('tiers.summary') ? S.get('carrier') || null : null);
  const tiersNow = () => (known('tiers.summary') ? S.get('tiers') || {} : {});
  const routeNow = () => (known('route.published') ? S.get('route') || raw('route.published') || null : null);
  const gatewayNow = () => (known('gateway.current') ? S.get('gateway') || raw('gateway.current') || null : null);
  /* the carrier's current node (nodes.list of that tier, located by the published route) */
  const carrierItems = () => { const c = carrierNow(); const v = c ? raw('nodes.list', c) : null; return v && Array.isArray(v.items) ? v.items : []; };
  const carrierNode = () => { const c = carrierNow(); return c ? currentNode(routeNow(), carrierItems(), c) : null; };
  // B-01 (latency rising) needs a runtime fact v1 does not have; inside its feature branch the
  // carrier node's Node.slow (T-P4-C2 item 4) stands for it
  const carrierSlow = () => { if (!T.rt.feature('B-01')) return false; const n = carrierNode(); return !!(n && n.slow === true); };
  // the kill switch word: unknown until killswitch.state arrived; the settings' ksEnabled / ksBlocked come first
  function ksWord() {
    if (!known('killswitch.state')) return 'unknown';
    if (S.get('ksEnabled') === false) return 'off';
    const k = ksKind(S.get('killswitch'));
    return k === 'armed' && S.get('ksBlocked') ? 'blocked' : k;
  }
  const ksProjected = () => ({ off: 'disabled', blocked: 'blocked', error: 'error', triggered: 'triggered', armed: 'armed', inactive: 'inactive' })[ksWord()] || null;
  function heroIn() {
    const sum = raw('tiers.summary');
    return {
      run: runNow(), cancelling: !!S.get('cancelling'), phase: S.get('phase'), restarting: !!S.get('restarting'),
      carrier: carrierNow(), fallback: known('tiers.summary') && !!S.get('fallback'), tiers: tiersNow(), killswitch: ksProjected(),
      readiness: S.get('readiness'), failure: S.get('failure'), offline: sum ? sum.offline_reason : null, stale: stale('service.state'),
      stopFailed: !!stopFail, stopError: stopFail && stopFail.error, note: runNote, slow: carrierSlow(), again: lostGen,
    };
  }
  const heroView = () => heroOf(heroIn());

  /* ---- i18n helpers ------------------------------------------------------
     label(root): the static words written into the markup as data-i18n="<id>" /
     data-i18n-attr="<attr>:<id>;…" are registered with T.ti (relabelled in place on
     a language switch). txt(id): a text node beside an icon or a lock glyph (the
     element keeps its own text, so the markup keeps its shape); rewritten by
     relocalize(). data(s) / dataSlot: a data run (node name, protocol) in a span
     that machine translation skips. swapRich(el, face): M.swapText's roll with a
     DOM face (T.tx fragments; parameters stay text nodes). The mode names are the
     shared words of T-P4-04a (mode.<id>.short), read by id at use. */
  const texts = [];
  function txt(id, params) { const n = document.createTextNode(T.t(id, params)); texts.push([n, id, params]); return n; }
  function label(root) {
    const els = Array.from(root.querySelectorAll('[data-i18n],[data-i18n-attr]'));
    if (root.matches && root.matches('[data-i18n],[data-i18n-attr]')) els.unshift(root);
    els.forEach(el => {
      const id = el.getAttribute('data-i18n');
      if (id) T.ti(el, id);
      String(el.getAttribute('data-i18n-attr') || '').split(';').map(p => p.split(':')).filter(p => p.length === 2 && p[0] && p[1])
        .forEach(([attr, aid]) => T.ti(el, aid.trim(), null, { attr: attr.trim() }));
    });
    return root;
  }
  function frag(...kids) { const f = document.createDocumentFragment(); kids.forEach(k => { if (k != null && k !== '') f.append(k); }); return f; }
  function span(cls, ...kids) { const s = document.createElement('span'); if (cls) s.className = cls; s.append(...kids); return s; }
  const warnSlot = kids => span('warn', kids);
  const wideSlot = kids => span('wide-only', kids);
  const dataSlot = kids => { const s = span('dt', kids); s.setAttribute('translate', 'no'); return s; };
  const data = text => dataSlot(String(text == null ? '' : text));
  // a face kept from the last frame of a stale topic: "<face> · 数据未更新" (requirement 3)
  const staleAfter = face => frag(face, ' · ', span('ov-stale', T.t('overview.common.stale')));
  const noTranslate = (root, sel) => { root.querySelectorAll(sel).forEach(e => e.setAttribute('translate', 'no')); return root; };
  // a key for a DOM face: tags, classes and text (only compared, never parsed)
  const sig = n => (n.nodeType === 3 ? n.data
    : `${n.nodeType === 1 ? `<${n.tagName} ${n.getAttribute('class') || ''}>` : ''}${Array.from(n.childNodes, sig).join('')}${n.nodeType === 1 ? '</>' : ''}`);
  function swapRich(el, face, o) {
    const key = '\u0001' + sig(face);
    if (el._rich === key) return;
    el._rich = key;
    M.swapText(el, key, o);
    const faces = el.querySelectorAll('.sw:not([aria-hidden])');
    const nu = faces[faces.length - 1];
    if (!nu) return;
    // an --i18n-trace build wraps each T.tx result in span[data-i18n-id]: unwrap it so the face keeps
    // its shape (components.css styles .sw > .hop / .arr / .ic as direct children); the id moves
    // to the face itself
    Array.from(face.childNodes).forEach(k => {
      if (k.nodeType !== 1 || k.localName !== 'span' || k.className || !k.hasAttribute('data-i18n-id')) return;
      if (!nu.hasAttribute('data-i18n-id')) nu.setAttribute('data-i18n-id', k.getAttribute('data-i18n-id'));
      k.replaceWith(...k.childNodes);
    });
    nu.replaceChildren(face);
  }
  /* the label of a T.ui.busy button: its face rolls (busy → done → idle), so it is written
     here rather than bound with T.ti, and rewritten on a language switch only while idle */
  const labs = [];
  function setLab(btn, id) {
    const lab = btn.querySelector('.lab');
    if (!labs.some(x => x[0] === btn)) labs.push([btn, id]);
    if (!lab || /\bis-(busy|done|fail)\b/.test(btn.className)) return;
    setText(lab, T.t(id));
  }
  /* T.ui.busy writes back the face it saved at the click once the job is over (the done face
     holds `hold` ms, a fail face core's 2400 ms): a language switch in between would leave the
     old words, so the label is written again after that, only if the language changed */
  function relabelAfter(btn, id, hold, job) {
    const lang = T.i18n && T.i18n.lang;
    const again = ms => setTimeout(() => { if (T.i18n && T.i18n.lang !== lang) setLab(btn, id); }, ms + 50);
    Promise.resolve(job).then(() => again(hold), () => again(2400));
  }
  /* T.ui.copy keeps the aria-label / data-tip strings it found when it was wired and puts them
     back 1.6 s after a copy: for a T.ti-bound button those are the start language's words, so
     the bindings are rewritten as soon as the copied face is gone */
  function relabelAfterCopy(btn) {
    if (!btn || !T.i18n || typeof MutationObserver !== 'function') return;
    new MutationObserver(rs => {
      if (rs.some(r => /\bis-done\b/.test(r.oldValue || '')) && !btn.classList.contains('is-done')) T.i18n.relabel(btn);
    }).observe(btn, { attributes: true, attributeFilter: ['class'], attributeOldValue: true });
  }
  // rewrite a label in place: through its roll when M.swapText owns it, else as plain text
  function setText(el, s) { if (!el) return; if (el._swap !== undefined) M.swapText(el, s, { instant: true }); else el.textContent = s; }

  /* the hero's name changes character by character (F-06): the old word leaves upward, the new
     one rises out of its line (.cr-v clips at the line's foot); a Latin word never breaks inside.
     Each face (.sw.cr-f) carries its own look (idle grey, clay), so a leaving word keeps it. While
     it rolls a screen reader reads the word (.sr-only) and the characters are aria-hidden; at rest
     the face is one plain text node again, the shape swapText leaves (kerning returns, the text
     tree is the same). Instant without motion, while hidden and on the first paint */
  function charFace(text, cls) {
    const f = span(`sw cr-f${cls ? ' ' + cls : ''}`);
    const v = span('cr-v'); v.setAttribute('aria-hidden', 'true');
    const chars = [];
    String(text).split(/(\s+)/).forEach(tok => {
      if (!tok) return;
      if (/^\s+$/.test(tok)) { v.append(' '); return; }
      const w = span('cr-w');
      Array.from(tok).forEach(c => { const s = span('cr-c', c); chars.push(s); w.append(s); });
      v.append(w);
    });
    f.append(span('sr-only', String(text)), v);
    return { f, v, chars };
  }
  function charRoll(el, text, cls, o) {
    o = o || {};
    const key = `${text}\u0003${cls || ''}`;
    if (el._cr === key) return;
    const first = el._cr === undefined; el._cr = key;
    el.classList.add('cr');
    const { f, chars } = charFace(text, cls);
    const plain = () => f.replaceChildren(String(text));
    if (first || o.instant || M.reduced || !visible) { el.replaceChildren(f); plain(); return; }
    const step = o.step || 26, wd = o.delay != null ? o.delay : M.wave.delay(el);
    Array.from(el.children).forEach(old => {
      if (old._out) return; old._out = true; old.setAttribute('aria-hidden', 'true');
      // a face at rest is plain text: split it again to send it off character by character
      if (!old.querySelector('.cr-c')) old.replaceChildren(charFace(old.textContent).v);
      const cs = Array.from(old.querySelectorAll('.cr-c'));
      let last = null;
      // one line up and out through the slot's top (never over the eyebrow above it)
      cs.forEach((c, i) => { last = c.animate([{ transform: 'none', opacity: 1 }, { transform: 'translateY(-100%)', opacity: 0 }], { duration: 240, delay: wd + i * 16, easing: M.ease.in, fill: 'forwards' }); });
      M.onDone(last, () => old.remove(), wd + cs.length * 16 + 700);
    });
    el.append(f);
    let lastIn = null;
    chars.forEach((c, i) => { lastIn = c.animate([{ transform: 'translateY(102%)' }, { transform: 'none' }], { duration: M.SP.settle.duration, delay: wd + 80 + i * step, easing: M.SP.settle.easing, fill: 'backwards' }); });
    M.onDone(lastIn, () => { if (el._cr === key && f.isConnected) plain(); }, wd + 80 + chars.length * step + M.SP.settle.duration + 300);
  }

  /* glide (F-06): a block that grows or shrinks never makes what follows jump. The first change of
     a frame takes the blocks' places (vis() is read before any sync writes); at the frame's end (rAF,
     before paint) every block that moved plays from where it was (transform only, SP.soft), a block
     inside another by its own part only. A deck card that grew unrolls from its old height (its band
     carries the card's ground and rides the edge down); a block that was not shown comes in */
  let gl = null;
  const glideEls = () => [R.gate, R.strip, R.modeCell, R.ks, R.pool, R.protoSec, R.root.querySelector('.exit-full'), R.root.querySelector('.idle-rows'),
    R.root.querySelector('.exit-route'), R.root.querySelector('.ov-traffic'), ...DECK.map(k => cards[k] && cards[k].querySelector('.dcard-id'))].filter(Boolean);
  // a block's place in the layout, transforms aside (a running glide does not count as a move)
  const layTop = e => { let y = 0; for (let n = e; n; n = n.offsetParent) y += n.offsetTop; return y; };
  function glidePre() {
    if (gl || M.reduced || !R.root || !visible) return;
    const els = glideEls();
    gl = { els, shown: els.map(e => e.getClientRects().length > 0), vis: els.map(e => e.getBoundingClientRect().top), lay: els.map(layTop),
      hs: DECK.map(k => (cards[k] ? cards[k].offsetHeight : 0)) };
    requestAnimationFrame(glidePlay);
  }
  function glidePlay() {
    const g = gl; gl = null;
    if (!g || !visible || M.reduced) return;
    const shownNow = g.els.map(e => e.isConnected && e.getClientRects().length > 0);
    const moved = g.els.map((e, i) => g.shown[i] && shownNow[i] && layTop(e) !== g.lay[i]);
    const grew = DECK.map((k, i) => (cards[k] ? cards[k].offsetHeight - g.hs[i] : 0));
    const dys = g.els.map((e, i) => {
      if (!moved[i]) return 0;
      if (e._gl) { e._gl.cancel(); e._gl = null; }
      return g.vis[i] - e.getBoundingClientRect().top;
    });
    g.els.forEach((e, i) => {
      if (!g.shown[i] && shownNow[i]) {
        e.animate([{ opacity: 0, transform: 'translateY(6px)' }, { opacity: 1, transform: 'none' }], { duration: 320, delay: M.wave.delay(e) + 60, easing: M.ease.out, fill: 'backwards' });
        return;
      }
      if (!moved[i]) return;
      let dy = dys[i];
      for (let j = 0; j < g.els.length; j++) if (j !== i && moved[j] && g.els[j].contains(e)) { dy -= dys[j]; break; }
      if (Math.abs(dy) < 0.5) return;
      e._gl = e.animate([{ transform: `translateY(${dy.toFixed(1)}px)` }, { transform: 'none' }], { duration: M.SP.soft.duration, easing: M.SP.soft.easing, composite: 'add' });
    });
    // the unroll's clip reaches past the card's ring and shadow, so nothing pops in at its end
    DECK.forEach((k, i) => {
      const c = cards[k], d = grew[i];
      if (!c || d <= 0) return;
      c.animate([{ clipPath: `inset(-14px -14px ${d}px -14px round 24px 24px 10px 10px)` }, { clipPath: 'inset(-14px -14px -20px -14px round 24px)' }], { duration: M.SP.soft.duration, easing: M.SP.soft.easing });
    });
  }

  /* one health word for every "running" dot on screen (hero, run pill): the hero's (A5, G06).
     v1 degraded (some nodes of the carrier unusable, R2) is not a warning by itself; a kill
     switch that is off, not in effect or failing is (the privacy promise is off); a stale
     service.state greys the dot (S-R8) */
  const health = () => (runNow() === 'running' ? heroView().dot : runNow() === 'stopped' ? 'off' : 'busy');

  /* ================================================================ markup */
  function build(root) {
    // static words: data-i18n / data-i18n-attr (registered by label() below), or txt() beside
    // an icon. Every data slot (addresses, location, network, node names) is empty here and
    // filled with textContent by the sync functions (mapping_overview §6: no data in markup).
    root.replaceChildren(T.html`
<div class="ov">
  <div class="ov-hero" data-enter>
    <div class="ov-id">
      <div class="eyebrow" data-i18n="overview.hero.eyebrow"></div>
      <div class="ov-tier"><h2 class="ov-tier-name" id="ov-name"></h2></div>
      <div class="ov-hint"><span class="sdot lg" id="ov-dot"></span><span id="ov-hint"></span></div>
    </div>
  </div>
  <div class="ov-ctl" data-enter>
    <button class="btn btn-ghost ov-restart" id="ov-restart" type="button"><span class="bi">${I('restart')}</span><span class="lab"></span></button>
    <div class="run" id="run" role="group" data-i18n-attr="aria-label:overview.run.aria">
      <span class="run-layer run-base" aria-live="polite"></span>
      <span class="run-layer run-over" aria-hidden="true"></span>
      <span class="run-layer run-solid" aria-hidden="true"><span class="run-row center">${I('start', 17)}<span data-i18n="overview.run.start"></span></span></span>
      <button class="run-main" type="button" data-i18n-attr="aria-label:overview.run.start"></button>
      <button class="run-act" type="button"></button>
    </div>
  </div>

  <section class="ov-exit" id="ov-exit" data-state="ok" data-enter data-i18n-attr="aria-label:overview.exit.title">
    <header class="ov-head">
      <h3 class="ov-title" data-i18n="overview.exit.title"></h3>
      <span class="prov" id="ov-prov"></span>
      <div class="ov-act"><button class="btn btn-sm exit-refresh" id="ov-refresh" type="button"><span class="bi">${I('refresh', 15)}</span><span class="lab"></span></button></div>
    </header>
    <div class="exit-stage">
      <div class="exit-full">
        <div class="ip-block">
          <div class="ip-row"><span class="fam">IPv4</span><span class="ip ip4" id="ov-ip4" translate="no"></span><span class="chip sm ip-src" id="ov-src4" data-i18n="overview.exit.single_source" hidden></span><button class="copy" id="ov-copy4"></button><span class="last-tag" data-i18n="overview.exit.last"></span></div>
          <div class="ip-meta">
            <span class="ip-loc" id="ov-loc" translate="no"></span>
            <span class="ip-net"><span id="ov-net-t" translate="no"></span><span class="chip sm" id="ov-net-type"></span></span>
          </div>
        </div>
        <div class="ip-block">
          <div class="ip-row"><span class="fam">IPv6</span><span class="ip ip6" id="ov-ip6" translate="no"></span><span class="ip-same" id="ov-same"></span><span class="chip sm ip-src" id="ov-src6" data-i18n="overview.exit.single_source" hidden></span><button class="copy" id="ov-copy6"></button><span class="last-tag" data-i18n="overview.exit.last"></span></div>
        </div>
      </div>
      <div class="exit-idle">
        <div class="idle-rows" aria-hidden="true">
          <div class="ip-row"><span class="fam">IPv4</span><span class="ip-ghost">—</span></div>
          <div class="ip-row"><span class="fam">IPv6</span><span class="ip-ghost">—</span></div>
        </div>
        <div class="idle-msg" hidden><span class="idle-t"><span id="ov-idle-t"></span></span><span class="idle-s" id="ov-idle-s"></span></div>
      </div>
    </div>
    <div class="exit-route"><span id="ov-route"></span></div>
  </section>

  <section class="ov-deckcol" data-enter data-i18n-attr="aria-label:overview.deck.title">
    <header class="ov-head">
      <h3 class="ov-title" data-i18n="overview.deck.title"></h3><span class="ov-meta deck-meta" id="ov-deck-meta"></span>
    </header>
    <div class="deck" id="ov-deck"></div>
    <button class="dgate" id="ov-gate" type="button">
      ${I('gateway')}<span class="dc-name" translate="no">${D.tier('gateway').name}</span><span class="dc-stat" id="ov-gate-stat"></span><span class="dots" id="ov-gate-dots"></span>
    </button>
  </section>

  <section class="ov-traffic" data-enter data-i18n-attr="aria-label:overview.traffic.title">
    <header class="ov-head">
      <h3 class="ov-title" data-i18n="overview.traffic.title"></h3><span class="ov-meta" id="ov-scope"></span>
      <div class="ov-act"><span class="seg" id="ov-range" data-i18n-attr="aria-label:overview.traffic.range.aria"><button class="seg-btn" data-v="24h" data-i18n="overview.traffic.range.h24"></button><button class="seg-btn" data-v="5m" data-i18n="overview.traffic.range.m5"></button></span></div>
    </header>
    <div class="tot">
      <div><div class="tot-lab">${I('download', 14, 'down')}${txt('overview.traffic.down')}<span class="tot-rate" id="ov-live-d"></span></div><div class="tot-val"><span class="tot-num" id="ov-tot-d"></span><span class="tot-unit" id="ov-tot-du"></span></div></div>
      <div><div class="tot-lab">${I('upload', 14, 'up')}${txt('overview.traffic.up')}<span class="tot-rate" id="ov-live-u"></span></div><div class="tot-val"><span class="tot-num" id="ov-tot-u"></span><span class="tot-unit" id="ov-tot-uu"></span></div></div>
    </div>
    <div class="chart" id="ov-chart" role="img"></div>
  </section>

  <div class="ov-side" data-enter>
    <div class="ov-strip" id="ov-strip" role="group" data-i18n-attr="aria-label:overview.strip.aria">
      <div class="cell" id="ov-modecell">
        <div class="cell-row">
          <span class="cell-lab">${I('mode-system')}${txt('overview.mode.label')}</span>
          <span class="seg seg-multi" id="ov-mode" role="group" data-i18n-attr="aria-label:overview.mode.aria">
            <button class="seg-btn" type="button" data-v="system" aria-pressed="false">${txt('mode.system.short')}</button><button class="seg-btn" type="button" data-v="tun" aria-pressed="false">${txt('mode.tun.short')}</button>
          </span>
        </div>
        <div class="cell-hint"><span id="ov-mode-hint"></span></div>
      </div>
      <button class="cell" id="ov-ks" type="button" data-i18n-attr="aria-label:overview.ks.aria">
        <span class="cell-in">
          <span class="cell-row"><span class="cell-lab">${I('shield')}${txt('overview.ks.label')}</span><span class="cell-val" id="ov-ks-val"><span class="sdot" id="ov-ks-dot"></span><span id="ov-ks-t"></span></span></span>
          <span class="cell-hint"><span id="ov-ks-hint"></span></span>
        </span>
        ${I('chevron', 14, 'go')}
      </button>
      <button class="cell" id="ov-pool" type="button" data-i18n-attr="aria-label:overview.pool.aria">
        <span class="cell-in">
          <span class="cell-row"><span class="cell-lab">${I('pool')}${txt('overview.pool.label')}</span><span class="cell-val" id="ov-pool-val"><span id="ov-pool-n"></span><span class="cell-unit" id="ov-pool-u" data-i18n="overview.pool.unit"></span></span></span>
          <span class="cell-hint"><span id="ov-pool-hint"></span><span class="dots-group" id="ov-pool-dots" aria-hidden="true"></span></span>
        </span>
        ${I('chevron', 14, 'go')}
      </button>
    </div>
    <section class="ov-proto" id="ov-proto-sec" data-i18n-attr="aria-label:overview.proto.title">
      <header class="ov-head"><h3 class="ov-title" data-i18n="overview.proto.title"></h3><span class="ov-meta" id="ov-proto-total"></span></header>
      <div class="proto" id="ov-proto">
        <div class="pbar" id="ov-pbar"></div>
        <ul class="pleg" id="ov-pleg"></ul>
      </div>
    </section>
  </div>
</div>`);
    label(root);
    ['4', '6'].forEach(n => {
      const b = root.querySelector('#ov-copy' + n), family = `IPv${n}`;
      T.ti(b, 'overview.exit.copy.aria', { family }, { attr: 'aria-label' });
      T.ti(b, 'overview.exit.copy.tip', { family }, { attr: 'data-tip' });
    });
    // an extension point after the deck header (T.ext 'overview/deck'): the design target's
    // 层级回退 control (design/demo.js, mapping_overview §5); nothing in the app target (T-P4-07)
    deckExt = T.ext.get('overview/deck');
    if (deckExt && typeof deckExt.mount === 'function') deckExt.mount(root.querySelector('#ov-deck-meta'));
    const $ = s => root.querySelector(s);
    R = {
      root, name: $('#ov-name'), dot: $('#ov-dot'), hint: $('#ov-hint'), restart: $('#ov-restart'),
      run: $('#run'), base: $('.run-base'), over: $('.run-over'), solid: $('.run-solid'), main: $('.run-main'), act: $('.run-act'),
      strip: $('#ov-strip'), modeCell: $('#ov-modecell'), modeSeg: $('#ov-mode'), modeHint: $('#ov-mode-hint'),
      ks: $('#ov-ks'), ksVal: $('#ov-ks-val'), ksDot: $('#ov-ks-dot'), ksT: $('#ov-ks-t'), ksHint: $('#ov-ks-hint'),
      pool: $('#ov-pool'), poolVal: $('#ov-pool-val'), poolN: $('#ov-pool-n'), poolU: $('#ov-pool-u'), poolHint: $('#ov-pool-hint'), poolDots: $('#ov-pool-dots'),
      exit: $('#ov-exit'), prov: $('#ov-prov'), refresh: $('#ov-refresh'), ip4: $('#ov-ip4'), ip6: $('#ov-ip6'),
      copy4: $('#ov-copy4'), copy6: $('#ov-copy6'), src4: $('#ov-src4'), src6: $('#ov-src6'), same: $('#ov-same'),
      meta: $('.ip-meta'), loc: $('#ov-loc'), net: $('.ip-net'), netT: $('#ov-net-t'), netType: $('#ov-net-type'),
      idleMsg: $('.idle-msg'), idleT: $('#ov-idle-t'), idleS: $('#ov-idle-s'), route: $('#ov-route'),
      deck: $('#ov-deck'), deckMeta: $('#ov-deck-meta'), gate: $('#ov-gate'), gateStat: $('#ov-gate-stat'), gateDots: $('#ov-gate-dots'),
      scope: $('#ov-scope'), totD: $('#ov-tot-d'), totDu: $('#ov-tot-du'), totU: $('#ov-tot-u'), totUu: $('#ov-tot-uu'),
      liveD: $('#ov-live-d'), liveU: $('#ov-live-u'), rateD: null, rateU: null,
      chartBox: $('#ov-chart'), range: $('#ov-range'),
      protoSec: $('#ov-proto-sec'), proto: $('#ov-proto'), pbar: $('#ov-pbar'), pleg: $('#ov-pleg'), protoTotal: $('#ov-proto-total'),
    };
    setLab(R.restart, 'overview.restart.label');
    setLab(R.refresh, 'overview.exit.refresh.label');
    // (T-P5-13) the tips follow the buttons' forms (syncRestartName / syncRefreshName, again in
    // fitRun / fitExit and relocalize)
    syncRestartName(); syncRefreshName();
    liveRates();
  }
  /* 实时 <rate>: one sentence per language; the rate run (#ov-rate-d / -u) is rewritten at 1 Hz */
  function liveRates() {
    [['liveD', 'rateD', 'ov-rate-d'], ['liveU', 'rateU', 'ov-rate-u']].forEach(([box, key, id]) => {
      const was = R[key] ? R[key].textContent : '';
      R[box].replaceChildren(T.tx('overview.traffic.live', { rate: was }, { x: kids => { const s = span(null, kids); s.id = id; R[key] = s; return s; } }));
    });
  }

  /* ============================================================ hero */
  /* the kill switch's promise is off (switched off, or running without the rights it needs):
     the hero says so next to the health (overview.hero.hint.* ks=off|blocked), and its dot turns
     amber (已触发 has its own brick state) */
  const errSlot = kids => span('err', kids);
  /* the hint line of a heroOf() descriptor (G06: readiness, blocked, failure; S-R2, S-R7, S-R8).
     The words of a failure are the backend's message (T.rt.errorText), whole: the line ellipsizes
     and its data-tip carries the full sentence.
     F-01 (user feedback 10-27): the line says only what the name and the pill do not. Connecting,
     starting (the pill shows the phase), stopping / cancelling, stopped, a start failure without a
     message (the pill says 启动失败 · 重试) and an unconfirmed carrier (the name says 路由未确认)
     leave it empty, and syncHero then hides its dot */
  function heroHint(h) {
    const slots = { x: warnSlot }, ks = h.ks || 'none';
    switch (h.kind) {
      case 'connecting': case 'phase': case 'stopping': case 'stopped': case 'unconfirmed': return frag();
      case 'failed': return frag(h.failure ? errText(h.failure) : '');
      case 'stop_failed': return T.tx('overview.hero.hint.stop_failed', null, { x: errSlot });
      case 'restarting': return T.tx('overview.hero.hint.restarting');
      case 'note': return frag(warnSlot(h.note.text));
      case 'blocked': return T.tx('overview.hero.hint.blocked', null, { x: errSlot });
      case 'data_plane_failed': return T.tx('overview.hero.hint.data_plane_failed', null, { x: errSlot });
      // (literal ids: the catalog gates find every id the page names)
      case 'hold': return frag(T.t('overview.hero.hint.hold'));
      case 'validating': return frag(T.t('overview.hero.hint.validating'));
      case 'stale': return frag(T.t('overview.hero.hint.stale'));
      case 'fallback': return T.tx('overview.hero.hint.fallback', { tier: D.tier(h.tier).name, ks }, slots);
      // B-01: the latency-rising words exist only inside its feature branch (heroIn's slow)
      case 'slow': return T.tx('overview.hero.hint.degraded', { tier: D.tier(h.tier).name, ks }, slots);
      // no count for the carrier: no sentence about it (F-01), only a kill switch whose promise is
      // off; with nothing to say the line stays empty (not an empty message). Gateway carrying
      // (M1) is the same case: the tier under it is unconfirmed, and F-04 dropped the sentence
      // that said so
      case 'gateway':
      case 'nodes_unknown': return ['off', 'blocked', 'error'].includes(ks) ? T.tx('overview.hero.hint.nodes_unknown', { ks }, slots) : frag();
      default: return T.tx('overview.hero.hint.nodes', { count: h.count, ok: h.ok, total: h.total, ks }, slots);
    }
  }
  function heroTip(h) {
    if (h.kind === 'failed' && h.failure) return errText(h.failure);
    if (h.kind === 'stop_failed' && h.error) return errText(h.error);
    if (h.kind === 'note') return h.note.text;
    return '';
  }
  function syncHero(animate) {
    const run = runNow(), v = heroView();
    let name, idle = true;
    if (v.name.kind === 'tier') { name = D.tier(v.name.tier).name; idle = false; }
    else name = T.t('overview.hero.state', { state: v.name.state });
    const dot = v.dot, hint = heroHint(v.hint), tip = heroTip(v.hint);
    if (tip) R.hint.setAttribute('data-tip', tip); else R.hint.removeAttribute('data-tip');
    // the name and the pill are the event: the name changes with the pill, never held by a wave;
    // the look rides on the face (a leaving word keeps its own), the classes stay on the heading
    const err = v.name.state === 'blocked' || v.name.state === 'failed';
    charRoll(R.name, name, idle && !err ? 'is-idle' : err ? 'is-err' : '', { instant: !animate, delay: 0, step: idle ? 22 : 30 });
    // running, the name is the carrier's code name (never translated); otherwise a state word
    if (idle) R.name.removeAttribute('translate'); else R.name.setAttribute('translate', 'no');
    R.name.classList.toggle('is-idle', idle && !err);
    R.name.classList.toggle('is-err', err);
    R.dot.className = `sdot lg ${dot}`;
    // an empty hint line keeps its height (nothing below moves between states) without a lone dot
    R.dot.parentElement.classList.toggle('is-empty', !hint.textContent.trim());
    swapRich(R.hint, hint, { instant: !animate });
    R.restart.classList.toggle('is-away', run !== 'running');
    R.restart.disabled = run !== 'running';
    R.restart.setAttribute('aria-hidden', String(run !== 'running'));
    syncPillDot();
    fitRun();
  }

  /* ============================================================ run control */
  // pill.at: when the pill last changed what it shows. A pointer click on the 取消 / 停止
  // segment within 400 ms of that (e.g. the second click of a double-click on 启动服务)
  // is ignored: the target changed meaning under the cursor. Keyboard activation is immediate.
  // pill.beat: the ✓ 已就绪 beat and the running pill's opening are under way (they run to their end)
  let pill = { mode: null, readyTimer: 0, fill: null, fade: null, p: 0, at: 0, beat: false };
  const clipAt = p => `inset(0 ${((1 - p) * 100).toFixed(2)}% 0 0 round 999px)`;
  /* the uptime: service.state's freshness.uptime_s, walking on by itself between snapshots and
     corrected by every snapshot (mapping_overview §1 uptime) */
  let up = { s: 0, at: 0 };
  const upNow = () => up.s + (runNow() === 'running' && up.at ? Math.max(0, Math.floor((performance.now() - up.at) / 1000)) : 0);
  function takeUptime() { up = { s: Number(S.get('uptime')) || 0, at: performance.now() }; }
  /* the pill's row for a mode, built by T.html (words escaped); rows are compared by sig()
     (modes: pillMode() in rt/project_overview.js, plus 'ready' for the beat) */
  function rowEl(mode) {
    const ph = phaseNow();
    switch (mode) {
      case 'start': return T.html`<span class="run-row center">${I('start', 17)}<span>${T.t('overview.run.start')}</span></span>`;
      case 'starting': return T.html`<span class="run-row"><span class="run-read"><span class="spin"></span><span data-ph>${phaseLabel(ph)}</span><span class="run-step" data-st>${ph + 1}/3</span></span><span class="run-div"></span><span class="run-seg">${T.t('overview.run.cancel')}</span></span>`;
      case 'ready': return T.html`<span class="run-row center">${I('check', 17)}<span>${T.t('overview.run.ready')}</span></span>`;
      case 'running': return T.html`<span class="run-row"><span class="run-read"><span class="sdot ${health()}"></span><span class="run-word">${T.t('overview.run.running')}</span><span class="run-up" data-up aria-live="off">${F.clock(upNow())}</span></span><span class="run-div"></span><span class="run-seg">${I('stop', 14)}<span>${T.t('overview.run.stop')}</span></span></span>`;
      case 'failed': return T.html`<span class="run-row"><span class="run-read"><span class="sdot err"></span><span class="run-err">${T.t('overview.run.failed')}</span></span><span class="run-div"></span><span class="run-seg">${I('restart', 14)}<span>${T.t('overview.run.retry')}</span></span></span>`;
      // the Core still runs (or still cleans up): 重试 is the read area (run-main), 停止 stays (G05)
      case 'failed_stop': return T.html`<span class="run-row"><span class="run-read"><span class="sdot err"></span><span class="run-err">${T.t('overview.run.failed')}</span><span class="run-link">${T.t('overview.run.retry_link')}</span></span><span class="run-div"></span><span class="run-seg">${I('stop', 14)}<span>${T.t('overview.run.stop')}</span></span></span>`;
      case 'stop_failed': return T.html`<span class="run-row"><span class="run-read"><span class="sdot err"></span><span class="run-err">${T.t('overview.run.stop_failed')}</span></span><span class="run-div"></span><span class="run-seg">${I('stop', 14)}<span>${T.t('overview.run.retry')}</span></span></span>`;
      case 'connecting': return T.html`<span class="run-row center"><span class="spin"></span><span>${T.t('overview.run.connecting')}</span></span>`;
      case 'cancelling': return T.html`<span class="run-row center"><span class="spin"></span><span>${T.t('overview.run.stopping', { cancelling: 'yes' })}</span></span>`;
      default: return T.html`<span class="run-row center"><span class="spin"></span><span>${T.t('overview.run.stopping', { cancelling: 'no' })}</span></span>`;
    }
  }
  const rowKey = mode => sig(rowEl(mode));
  /* a clipped vertical roll with full-height travel: the old label leaves upward as the new one
     arrives from below, so the two never share the line (no double exposure, no blank frame) */
  function swapRow(layer, nu, animate) {
    const old = layer.querySelector('.run-row:not(.is-out)');
    const key = sig(nu);
    if (old && old._key === key) return;
    nu._key = key;
    if (!old) { layer.append(nu); return; }
    if (!animate || M.reduced || !visible) { old.replaceWith(nu); return; }
    old.classList.add('is-out');
    layer.append(nu);
    const ease = 'cubic-bezier(.35,0,.15,1)', dur = 260;
    const a = old.animate([{ transform: 'none' }, { transform: 'translateY(-100%)' }], { duration: dur, easing: ease, fill: 'forwards' });
    M.onDone(a, () => old.remove(), dur + 200);
    nu.animate([{ transform: 'translateY(100%)' }, { transform: 'none' }], { duration: dur, easing: ease });
  }
  function setRows(mode, animate) {
    if (pill.mode !== mode) pill.at = performance.now();
    pill.mode = mode;
    swapRow(R.base, rowEl(mode), animate); swapRow(R.over, rowEl(mode), animate);
    fitRun();
  }
  /* the width of e's content box in fractional pixels (border box less borders and padding): the
     fit checks below compare a word's range with it (T-P5-13 fix round 1) */
  function contentW(e) { const cs = getComputedStyle(e); return e.getBoundingClientRect().width - (parseFloat(cs.paddingLeft) || 0) - (parseFloat(cs.paddingRight) || 0) - (parseFloat(cs.borderLeftWidth) || 0) - (parseFloat(cs.borderRightWidth) || 0); }
  /* T-P5-11 (ruling 2: interface words are read whole): while 重启服务 is away (the service is not
     running) its room in the control row goes to the pill, but only when the pill's row would cut
     a word at its own width (de Knoten sammeln 1/3); where the words fit (zh_CN) the pill keeps its
     width. Measured on the shown row in fractional pixels, again after a resize (layoutDeck) and a
     phase change (renderPhase). */
  function fitRun() {
    const ctl = R.restart && R.restart.parentElement;
    if (!ctl || !R.run || !R.base || !R.run.offsetWidth) return;
    ctl.classList.remove('is-roomy');
    R.run.classList.remove('is-word-out');
    R.run.removeAttribute('data-tip');
    const row = R.base.querySelector('.run-row:not(.is-out)'); if (!row) return;
    /* the action segment (取消 / 停止 / 重试) is 78 px, or as wide as its words need (de
       Wiederholen): --seg-w sizes the segment and its hit area (.run-act) together */
    const seg = row.querySelector('.run-seg');
    let segW = 0;
    if (seg) { const rs = document.createRange(); rs.selectNodeContents(seg); segW = Math.ceil(rs.getBoundingClientRect().width + 20); }
    if (segW > 78) R.run.style.setProperty('--seg-w', segW + 'px'); else R.run.style.removeProperty('--seg-w');
    // a box whose own words are wider than its content box (fractional pixels, the 0.02 px tolerance
    // of the tiers pill's short(): Chrome draws the ellipsis at any fraction past the content box, fix
    // round 1: es Error al iniciar was cut by 0.13 px with a 0.5 px threshold)
    const cutIn = box => Array.from(box.querySelectorAll('span')).some(e => {
      if (!Array.from(e.childNodes).some(n => n.nodeType === 3 && n.nodeValue.trim())) return false;
      const rg = document.createRange(); rg.selectNodeContents(e);
      return rg.getBoundingClientRect().width - contentW(e) > 0.02;
    });
    if (R.restart.classList.contains('is-away')) { if (cutIn(row)) ctl.classList.add('is-roomy'); }
    else {
      /* T-P5-13 (决定_P5裁定 P5-e #5, form b): 重启服务 gives way to the pill. Its label steps out
         (the icon stays; the words are its accessible name and lead its tip) when the label would be
         cut, or when the pill's words would be cut beside it (es En ejecución 00:02:01) */
      R.restart.classList.remove('is-icon');
      const lab = R.restart.querySelector('.lab');
      if ((lab && cutIn(R.restart)) || cutIn(row)) R.restart.classList.add('is-icon');
      syncRestartName();
    }
    /* then the pill's run word (运行中) steps out while its row is still cut: the state dot and the
       uptime stay, the word is kept for a screen reader (visually hidden) and is the pill's tip */
    const word = row.querySelector('.run-word');
    if (word && cutIn(row)) {
      R.run.classList.add('is-word-out');
      R.run.setAttribute('data-tip', word.textContent);
    }
  }
  /* T-P5-13 (决定_P5裁定 P5-e #5): the check chip's words (de Automatisch bestätigt · vor 2 Minuten)
     are read whole. 刷新 beside it gives way first as an icon button (form b: its label is its
     accessible name and leads its tip); a chip still too long then wraps to a second line (form a,
     overview.css). Measured on one line (.is-one) in fractional pixels */
  function fitExit() {
    const p = R.prov, b = R.refresh;
    if (!p || !b || !p.offsetWidth) return;
    b.classList.remove('is-icon'); p.classList.add('is-one');
    const face = Array.from(p.querySelectorAll('.sw')).filter(x => !x.hasAttribute('aria-hidden')).pop();
    if (face) { const rg = document.createRange(); rg.selectNodeContents(face); if (rg.getBoundingClientRect().width - contentW(face) > 0.02) b.classList.add('is-icon'); }
    p.classList.remove('is-one');
    syncRefreshName();
  }
  // the icon form of 刷新 is named by its label, which is also its tip; the labelled button has
  // no tip (F-01: no sentence on how the check works)
  function syncRefreshName() {
    if (!R.refresh) return;
    if (R.refresh.classList.contains('is-icon')) {
      R.refresh.setAttribute('aria-label', T.t('overview.exit.refresh.label'));
      R.refresh.setAttribute('data-tip', T.t('overview.exit.refresh.label'));
    } else {
      R.refresh.removeAttribute('aria-label');
      R.refresh.removeAttribute('data-tip');
    }
  }
  // the icon form of 重启服务 is named by its label; its tip leads with the label (T-P5-13)
  function syncRestartName() {
    const icon = R.restart.classList.contains('is-icon');
    if (icon) {
      R.restart.setAttribute('aria-label', T.t('overview.restart.label'));
      R.restart.setAttribute('data-tip', `${T.t('overview.restart.label')} · ${T.t('overview.restart.tip')}`);
    } else {
      R.restart.removeAttribute('aria-label');
      R.restart.setAttribute('data-tip', T.t('overview.restart.tip'));
    }
  }
  function syncPillDot() {
    if (pill.mode !== 'running') return;
    const h = health();
    T.$$('.run-row:not(.is-out) .run-read .sdot', R.run).forEach(d => { d.className = `sdot ${h}`; });
    const key = rowKey('running');
    T.$$('.run-row:not(.is-out)', R.run).forEach(r => { r._key = key; });
  }
  // the accent fill of the pill (clip from the left) while the service starts
  function fillPill(p, dur, easing, animate) {
    const from = pill.p; pill.p = p;
    if (pill.fill) { try { pill.fill.commitStyles(); } catch (e) { /* not rendered */ } pill.fill.cancel(); pill.fill = null; }
    if (!animate || M.reduced || !dur || !visible) { R.over.style.clipPath = clipAt(p); return; }
    const a = R.over.animate([{ clipPath: R.over.style.clipPath || clipAt(from) }, { clipPath: clipAt(p) }], { duration: dur, easing: easing || 'cubic-bezier(.3,.6,.35,1)', fill: 'forwards' });
    pill.fill = a;
    M.onDone(a, () => { if (pill.fill === a) { R.over.style.clipPath = clipAt(p); a.cancel(); pill.fill = null; } }, dur + 400);
  }
  // (T-P4-09: the fade is kept, not looked up: getAnimations() and getComputedStyle() recompute
  // the style, which on every service.state while the window is hidden was a style pass a second;
  // the computed opacity is read only when a fade starts)
  function overOpacity(o, animate, dur) {
    if (pill.fade) { pill.fade.cancel(); pill.fade = null; }
    const go = animate && !M.reduced && visible;
    const from = go ? getComputedStyle(R.over).opacity : null;
    R.over.style.opacity = String(o);
    if (go && +from !== o) pill.fade = R.over.animate([{ opacity: from }, { opacity: o }], { duration: dur || 260, easing: M.ease.out });
  }
  /* the phase fill: jump to the phase's level, then creep on toward its top for as long as the
     phase lasts (B3, R9; no simulated durations) */
  let creepT = 0;
  function fillPhase(ph, animate) {
    clearTimeout(creepT);
    fillPill(FILL_AT[ph], 380, M.ease.out, animate);
    if (!animate || M.reduced || !visible) return;
    creepT = setTimeout(() => { if (runNow() === 'starting' && phaseNow() === ph && pill.mode === 'starting') fillPill(FILL_TOP[ph], CREEP_MS, 'cubic-bezier(.1,.55,.25,1)', true); }, 420);
  }
  const pillIn = () => ({ run: runNow(), cancelling: !!S.get('cancelling'), core: S.get('core') || {}, failure: S.get('failure'), stopFailed: !!stopFail });
  function renderRun(ev, animate) {
    const run = runNow(), prev = ev && ev.prev, mode = pillMode(pillIn());
    // the ready beat and the opening run to their end while the service stays running (a readiness
    // or core update in between redraws nothing on the pill); leaving running ends them at once
    if (pill.beat && mode === 'running' && prev !== 'starting') return;
    if (pill.beat) { pill.beat = false; R.base.classList.remove('is-bloom'); R.base.getAnimations().forEach(a => a.cancel()); }
    clearTimeout(pill.readyTimer);
    if (mode === 'starting' && R.run.dataset.state !== 'starting') {
      // the solid start pill leaves in the same frame the empty track appears: the label roll
      // carries the change (a fading solid read as a drain and a lavender "disabled" flash)
      R.run.classList.add('is-snap');
      requestAnimationFrame(() => requestAnimationFrame(() => R.run.classList.remove('is-snap')));
    }
    // the pill's skin: stopped (the solid start pill), starting (the accent track), stopping
    // (also cancelling and connecting), running (also the failure faces, which are white pills)
    R.run.dataset.state = mode === 'start' ? 'stopped' : mode === 'starting' ? 'starting' : mode === 'running' ? 'running'
      : mode === 'stopping' || mode === 'cancelling' || mode === 'connecting' ? 'stopping' : 'failed';
    R.main.disabled = !(mode === 'start' || mode === 'failed_stop');
    R.act.disabled = !(mode === 'running' || mode === 'starting' || mode === 'failed' || mode === 'failed_stop' || mode === 'stop_failed');
    T.ti(R.act, 'overview.run.act_aria', { state: mode === 'starting' ? 'starting' : mode === 'failed' ? 'retry' : mode === 'stop_failed' ? 'stop_retry' : 'other' }, { attr: 'aria-label' });
    T.ti(R.main, mode === 'failed_stop' ? 'overview.run.main_aria_retry' : 'overview.run.start', null, { attr: 'aria-label' });
    if (mode === 'starting') {
      if (pill.mode !== 'starting') {
        R.over.style.clipPath = clipAt(0); pill.p = 0; overOpacity(1, false);
        setRows('starting', animate);
      }
      fillPhase(phaseNow(), animate);
      return;
    }
    clearTimeout(creepT);
    if (mode === 'running') {
      // ✓ 已就绪: a short beat on the full accent pill, only when the start ends ready (B5);
      // running but not ready falls straight to the running pill (the hero says why)
      const rk = readinessKind(S.get('readiness'));
      if (animate && prev === 'starting' && rk === 'ready') {
        fillPill(1, 180, M.ease.out, true); overOpacity(1, false);
        setRows('ready', true);
        pill.beat = true;
        pill.readyTimer = setTimeout(() => { if (runNow() !== 'running') { pill.beat = false; return; } openPill(); }, 760);
      } else {
        fillPill(0, 0); overOpacity(0, false); setRows('running', animate);
      }
      return;
    }
    if (mode === 'stopping' || mode === 'cancelling') {
      if (pill.fill) { try { pill.fill.commitStyles(); } catch (e) { /* ignore */ } pill.fill.cancel(); pill.fill = null; }
      overOpacity(0, animate, 220);
      setRows(mode, animate);
      return;
    }
    // a stop: the start pill blooms out of the centre over the stopping one (the mirror of the
    // running pill opening at ready); the row under it changes at once, the bloom covers it
    const was = pill.mode;
    const bloom = mode === 'start' && animate && visible && !M.reduced && (was === 'stopping' || was === 'cancelling');
    fillPill(0, 0); overOpacity(0, false);
    setRows(mode, animate && !bloom);
    if (bloom) R.solid.animate([{ clipPath: 'inset(50% 50% 50% 50% round 999px)' }, { clipPath: 'inset(0% 0% 0% 0% round 999px)' }], { duration: 520, easing: 'cubic-bezier(.2,.7,.2,1)' });
  }
  /* after ✓ 已就绪 the white running pill opens out of the centre over the accent (豁然: a narrow
     opening that widens to the whole pill; never a fill draining back to the left). The running
     row is put under the accent at once; the accent layer leaves when the opening is done */
  function openPill() {
    swapRow(R.base, rowEl('running'), false);
    pill.mode = 'running'; pill.at = performance.now(); fitRun();
    const settle = () => {
      if (!pill.beat) return;                 // the beat was ended (a stop): renderRun owns the pill
      pill.beat = false; R.base.classList.remove('is-bloom');
      if (pill.fill) { pill.fill.cancel(); pill.fill = null; }
      overOpacity(0, false); R.over.style.clipPath = clipAt(0); pill.p = 0;
      swapRow(R.over, rowEl('running'), false);
    };
    if (!visible || M.reduced) { settle(); return; }
    R.base.classList.add('is-bloom');
    const a = R.base.animate([{ clipPath: 'inset(50% 50% 50% 50% round 999px)' }, { clipPath: 'inset(0% 0% 0% 0% round 999px)' }], { duration: 520, easing: 'cubic-bezier(.2,.7,.2,1)' });
    M.onDone(a, settle, 1000);
  }
  function renderPhase(animate) {
    if (runNow() !== 'starting' || pill.mode !== 'starting') return;
    const ph = phaseNow();
    [R.base, R.over].forEach(l => {
      const row = l.querySelector('.run-row:not(.is-out)'); if (!row) return;
      M.swapText(row.querySelector('[data-ph]'), phaseLabel(ph), { instant: !animate });
      M.swapText(row.querySelector('[data-st]'), `${ph + 1}/3`, { instant: !animate });
      row._key = rowKey('starting');
    });
    fitRun();
    fillPhase(ph, animate);
  }
  function tickUptime() {
    if (!visible || pill.mode !== 'running') return;       // hidden: resynced on enter()
    const t = F.clock(upNow());
    T.$$('[data-up]', R.run).forEach(x => { x.textContent = t; });
    const key = rowKey('running');
    T.$$('.run-row:not(.is-out)', R.run).forEach(r => { r._key = key; });
  }

  /* ============================================================ strip */
  let confirmEl = null, pendingMode = null, gateNote = null, gateT = 0;
  /* 接管方式: two toggles over the saved mode (系统代理, 虚拟网卡; data/static.js modeAfterSwitch: at
     least one stays on). A mode with the TUN adapter (tun, mixed) needs administrator rights: without
     them the 虚拟网卡 toggle is shown locked, and a press that would bring the adapter up says why in
     the hint line (never a silent no-op) */
  // (only once network.mode said whether the app runs elevated: an unknown state locks nothing)
  const gated = v => !!(D.modes[v] && D.modes[v].admin && modeKnownNow() && !S.get('admin'));
  const GATED_MODES = Object.keys(D.modes).filter(v => D.modes[v].admin);
  const modeKnownNow = () => S.get('modeKnown') === true || known('network.mode');
  function syncGates() {
    T.$$('.seg-btn', R.modeSeg).forEach(b => {
      // (only 虚拟网卡 is ever locked; it is not disabled: while the saved mode has it on, a press
      // still turns it off, back to the system proxy)
      const g = gated(b.dataset.v), has = !!b.querySelector('.gate-l');
      if (g === has && b.classList.contains('is-gated') === g) return;
      b.classList.toggle('is-gated', g);
      if (g) { T.ti(b, 'overview.mode.gated_tip', null, { attr: 'data-tip' }); if (!has) b.prepend(T.iconEl('lock', 12, 'gate-l')); }
      else { b.removeAttribute('data-tip'); b.removeAttribute('data-i18n-attr'); const l = b.querySelector('.gate-l'); if (l) l.remove(); }
    });
  }
  /* the toggles show a mode: each on while the mode has it (none while the mode is unknown) */
  function paintToggles(mode) {
    T.$$('.seg-btn', R.modeSeg).forEach(b => b.setAttribute('aria-pressed', String(!!mode && modeSwitchOn(mode, b.dataset.v))));
  }
  function explainGate(v) {
    clearTimeout(gateT); gateNote = v;
    syncStrip(visible);
    gateT = setTimeout(() => { gateNote = null; syncStrip(visible); }, 3200);
  }
  /* the mode hint line: a transient face first (switching, a gate note, the last mode.set's
     outcome), then modeHint() (I6-I13, G08). The listener address is data (app.info.listeners,
     I7): <mono>, not translated; unknown, the sentence goes without it */
  function modeFace(run, mode, shown, busy) {
    const slots = { x: warnSlot }, lock = () => T.iconEl('lock', 13, 'gate-ic');
    if (busy) return { face: frag(span('spin'), T.tx('overview.mode.hint.switching', { mode: shown })) };
    if (gateNote && (gated(gateNote) || modeNote && modeNote.kind === 'admin_required')) return { face: frag(lock(), T.tx('overview.mode.hint.gate_note', { mode: gateNote }, slots)) };
    if (modeNote) {
      const k = modeNote.kind;
      if (k === 'next_start') return { face: frag(T.t('overview.mode.note.next_start')) };
      const tip = modeNote.error ? errText(modeNote.error) : '';
      const id = { cleanup_pending: 'overview.mode.note.cleanup_pending', not_applied: 'overview.mode.note.not_applied', conflict: 'overview.mode.note.conflict' }[k] || 'overview.mode.note.failed';
      return { face: T.tx(id, null, slots), tip };
    }
    if (stale('network.mode')) return { face: frag(T.t('overview.common.stale')) };
    const h = modeHint({ run, mode, modeKnown: modeKnownNow(), admin: !!S.get('admin'), gatedModes: GATED_MODES, systemProxy: S.get('systemProxy') });
    switch (h.kind) {
      // F-01: an unknown mode says nothing (the control shows no selection)
      case 'unknown': return { face: frag() };
      case 'gated': return { face: frag(lock(), T.tx('overview.mode.hint.gated', null, slots)) };
      // the sentence ellipsizes on a narrow strip; the remedy (重启服务, its own message) stays
      // whole after it
      case 'drift': return { face: frag(span('ov-drift-t', T.tx('overview.mode.hint.drift', null, slots)), restartLink(T.t('overview.restart.label'))), tip: T.t('overview.mode.hint.drift') };
      case 'proxy_unknown': return { face: T.tx('overview.mode.hint.proxy_unknown', null, slots) };
      default: {
        /* tun / mixed / system. F-01: only a mode in force says so (已接管, with the listener the
           system proxy points at, I7); not running, or not yet taken over, the line says nothing
           (no 「启动后接管」 forecast, no description of what a mode does) */
        if (h.state !== 'running') return { face: frag() };
        const id = h.kind !== 'tun' && listen ? 'overview.mode.hint.system' : 'overview.mode.hint.system_noport';
        return { face: noTranslate(T.tx(id, { port: listen || '' }), '.mono') };
      }
    }
  }
  // G08's remedy: 重启服务 inside the hint (W-11: no separate re-take command)
  const restartLink = kids => {
    const b = document.createElement('button');
    b.type = 'button'; b.className = 'link-btn ov-relink'; b.append(kids);
    b.addEventListener('click', e => { e.stopPropagation(); doRestart(); });
    return b;
  };
  function syncStrip(animate) {
    const run = runNow(), mode = S.get('mode'), busy = !!S.get('modeBusy');
    // while a switch is in flight the control stays on the asked mode (its thumb breathes)
    const shown = busy && pendingMode ? pendingMode : mode;
    if (!confirmEl) paintToggles(modeKnownNow() ? shown : null);
    if (busy) R.modeSeg.setAttribute('aria-busy', 'true'); else R.modeSeg.removeAttribute('aria-busy');
    R.modeCell.classList.toggle('is-unknown', !modeKnownNow());
    syncGates();
    const mf = modeFace(run, mode, shown, busy);
    if (mf.tip) R.modeHint.setAttribute('data-tip', mf.tip); else R.modeHint.removeAttribute('data-tip');
    // a cell with nothing to add is one row (F-01): its hint line goes, the frame stays
    R.modeHint.parentElement.hidden = !mf.face.textContent.trim();
    swapRich(R.modeHint, mf.face, { instant: !animate });

    // 熔断保护 (J): the projection's word; the settings' ksEnabled / ksBlocked first; unknown until
    // killswitch.state arrived; an error says its message in the tip (S-K1)
    const kw = ksWord();
    const kstate = kw === 'inactive' ? 'inactive' : kw;
    const kd = { off: 'off', blocked: 'warn', error: 'err', triggered: 'err', armed: 'ok', inactive: 'off', unknown: 'off' }[kstate];
    const kc = { off: 'is-idle', blocked: 'is-warn', error: 'is-err', triggered: 'is-err', armed: '', inactive: 'is-idle', unknown: 'is-idle' }[kstate];
    R.ksVal.className = `cell-val ${kc}`; R.ksDot.className = `sdot ${kd}`;
    M.swapText(R.ksT, T.t('overview.ks.state', { state: kstate }), { instant: !animate });
    const ksRaw = raw('killswitch.state');
    const ksTip = kstate === 'error' && ksRaw && ksRaw.error ? errText(ksRaw.error) : '';
    if (ksTip) R.ksHint.setAttribute('data-tip', ksTip); else R.ksHint.removeAttribute('data-tip');
    // F-01: the hint speaks only when the user has something to do (needs rights, failing); the
    // state word says the rest (已触发: the hero says what happened), without a sentence on how
    // the kill switch works
    const ksHint = stale('killswitch.state') ? T.t('overview.common.stale')
      : kstate === 'blocked' || kstate === 'error' ? T.t('overview.ks.hint', { state: kstate }) : '';
    R.ksHint.parentElement.hidden = !ksHint;
    M.swapText(R.ksHint, ksHint, { instant: !animate });

    // 节点池 is your own + subscription nodes (Bastion's automatic pool is counted apart); unknown
    // or stale counts are a dash (S-P1); 待复检 only when the runtime reports it (S-P2, C2 item 7)
    const tiers = S.get('tiers') || {}, col = S.get('collected') || {};
    const pc = poolCell({ run, pool: S.get('pool'), summary: raw('tiers.summary'), stale: stale('tiers.summary') });
    R.poolVal.classList.toggle('is-idle', !pc.known);
    M.swapText(R.poolN, pc.known ? `${F.num(pc.ok)}/${F.num(pc.total)}` : T.t('overview.pool.value', { state: pc.state }), { instant: !animate });
    R.poolU.hidden = !pc.known;
    // (F-01: no counts yet, no sentence: the value says — or 检测中)
    M.swapText(R.poolHint, !pc.known ? ''
      : pc.rate == null ? T.t('overview.pool.empty') : T.t('overview.pool.online', { rate: pc.rate, pending: pc.pending }), { instant: !animate });
    const bp = S.get('bastion') || {}, bOn = !!(tiers.bastion && tiers.bastion.enabled), running = run === 'running';
    const groups = ['vanguard', 'sentinel', 'bastion', 'gateway'].map(k => {
      const c = tiers[k] || {}, got = running || (run === 'starting' && col[k]);
      if (k === 'bastion') {
        if (!bOn) return { tip: T.t('overview.pool.bastion_off'), none: true };
        // the pool's places don't fit as dots: a compact meter, the same one the deck card shows (D9: capacity)
        const n = running ? Number(bp.ok) || 0 : 0, cap = Number(bp.capacity) || 0;
        return { meter: 'ov-bmeter meter', tip: T.t('overview.pool.bastion_tip', { n, capacity: cap }), v: (cap ? Math.min(1, n / cap) : 0).toFixed(3) };
      }
      if (!c.total) return { none: true };
      const d = dotsOf(c.total, got ? c.ok : 0, got ? '' : 'off');
      return d.meter ? { meter: `ov-bmeter meter${got ? ' ink' : ' is-off'}`, v: d.v.toFixed(3) } : { n: d.n, ok: d.ok, style: d.style };
    });
    const key = JSON.stringify(groups);
    if (R.poolDots._h !== key) {
      R.poolDots._h = key;
      R.poolDots.replaceChildren(...groups.map(g => {
        const el = g.meter ? meterEl(g.meter, g.v) : span('dots', ...(g.none ? [span('none')] : dotKids(g.n, g.ok, g.style)));
        if (g.tip) el.setAttribute('data-tip', g.tip);
        return el;
      }));
    }
  }
  /* point rows and pool meters, built as DOM (no markup strings; --v through the CSSOM) */
  function dotKids(n, ok, style) {
    return Array.from({ length: n || 0 }, (_, i) => { const e = document.createElement('i'); e.className = style || (i < ok ? '' : 'fail'); return e; });
  }
  function meterEl(cls, v) { const m = span(cls, span('meter-track', span('meter-fill'))); m.style.setProperty('--v', v); return m; }
  const belowMode = () => [R.ks, R.pool, R.protoSec];
  /* v: the mode the press leads to; k: the toggle pressed (it carries the pending mark) */
  function openConfirm(v, k) {
    closeConfirm(false);
    T.$$('.seg-btn', R.modeSeg).forEach(b => b.classList.toggle('is-pending', b.dataset.v === k));
    // the shared wording of a mode switch (mode.switch.confirm.*, as in 配置 and the palette), the
    // title as one sentence per mode; primary last as in the dialogs
    const msg = document.createTextNode(T.t('mode.switch.confirm.body', { mode: v }));
    const el = T.html`<div class="ov-confirm" role="alertdialog" data-i18n-attr="aria-label:overview.mode.confirm.aria">
      ${I('restart', 18)}<div class="txt"><b></b>${msg}</div>
      <div class="acts"><button class="btn btn-sm btn-ghost" type="button" data-a="no" data-i18n="common.action.cancel"></button><button class="btn btn-sm btn-primary" type="button" data-a="ok" data-i18n="mode.switch.confirm.ok"></button></div></div>`;
    label(el);
    T.ti(el.querySelector('.txt > b'), 'overview.mode.confirm.title', { mode: v });
    confirmEl = el; el._v = v; el._k = k; el._msg = msg;
    // the confirmation unrolls downward (clip) on the same spring the cells below are pushed
    // with, so its lower edge and the next cell's top edge travel together: nothing overlaps
    M.flip(belowMode(), () => R.modeCell.append(el));
    M.anim(el, [{ clipPath: 'inset(0 0 100% 0)' }, { clipPath: 'inset(0 0 0 0)' }], { duration: M.SP.soft.duration, easing: M.SP.soft.easing });
    Array.from(el.children).forEach((k, i) => M.anim(k, [{ opacity: 0 }, { opacity: 1 }], { duration: 200, delay: 60 + i * 20, easing: M.ease.out, fill: 'backwards' }));
    el.querySelector('[data-a="ok"]').addEventListener('click', () => {
      const mv = el._v;
      closeConfirm(true); paintToggles(mv); setMode(mv);
    });
    el.querySelector('[data-a="no"]').addEventListener('click', () => { closeConfirm(true); focusMode(k); });
    setTimeout(() => { if (confirmEl === el) el.querySelector('[data-a="ok"]').focus({ preventScroll: true }); }, 40);
  }
  /* focus back on the toggle that was pressed (else the first one on) */
  function focusMode(k) {
    const b = (k && R.modeSeg.querySelector(`.seg-btn[data-v="${k}"]`)) || R.modeSeg.querySelector('[aria-pressed="true"]');
    if (b) b.focus({ preventScroll: true });
  }
  function closeConfirm(animate) {
    const el = confirmEl; if (!el) return;
    confirmEl = null;
    T.$$('.seg-btn', R.modeSeg).forEach(b => b.classList.remove('is-pending'));
    if (!animate || M.reduced || !visible) { el.remove(); return; }
    // the reverse of opening: it rolls up while the cells below rise with its edge
    const top = el.offsetTop, D = 200;
    M.flip(belowMode(), () => { el.classList.add('is-closing'); Object.assign(el.style, { position: 'absolute', left: '0', right: '0', top: top + 'px', margin: '0' }); }, { duration: D, easing: M.ease.inout });
    Array.from(el.children).forEach(k => k.animate([{ opacity: 1 }, { opacity: 0 }], { duration: 90, easing: M.ease.in, fill: 'forwards' }));
    const a = el.animate([{ clipPath: 'inset(0 0 0 0)' }, { clipPath: 'inset(0 0 100% 0)' }], { duration: D, easing: M.ease.inout, fill: 'forwards' });
    M.onDone(a, () => el.remove(), D + 200);
  }
  /* mode.set (I2, I11-I13; S-M3..S-M7): the control stays on the asked mode while the job runs;
     the outcome is the hint for a few seconds (下次启动生效 · 已保存未生效 with apply_error · 正在清理 ·
     需要管理员权限, also when stopped · 另一项设置正在应用). Resolves true when the job was done. */
  function setMode(v) {
    if (!can('mode.set')) return Promise.resolve(false);
    pendingMode = v;
    note('mode', null);
    return T.rt.call('mode.set', { mode: v }).then(r => {
      pendingMode = null;
      const o = modeOutcome({ ok: true, result: r, run: runNow() });
      if (o) note('mode', o); else syncStrip(onScreen());
      return true;
    }, err => {
      pendingMode = null;
      const o = modeOutcome({ ok: false, error: err, run: runNow(), cleanupPending: !!(S.get('core') || {}).cleanup_pending });
      if (o && o.kind === 'admin_required') explainGate(v);
      if (o) note('mode', o); else syncStrip(onScreen());
      return false;
    });
  }

  /* ============================================================ exit */
  /* the exit card: exitCard() (G07, G09; S-E1..S-E17). Addresses, location, network and node
     names are data: textContent under translate="no". A reason is two texts: its short word
     (overview.exit_reason.*, the badge or the line) and the backend's full sentence
     (errors.exit.*, the tip or the second line) */
  let shownAddr = { v4: null, v6: null };   // what the copy buttons copy: the addresses on screen
  const exitIn = () => ({
    run: runNow(), cancelling: !!S.get('cancelling'), phase: S.get('phase'), identity: raw('exit.identity') || null, curHow: (S.get('exit') || {}).how,
    ownIds: ownExit, auto: autoExit, stale: stale('exit.identity'), canVerify: can('exit.verify'),
  });
  function badgeFace(b) {
    const ic = (n, c) => T.iconEl(n, 13, c);
    switch (b.kind) {
      case 'running': return { face: frag(span('spin'), T.t('overview.exit.prov.pending', { phase: b.phase || 'querying', how: b.how })), cls: '' };
      // both results are confirmed exits (one tone): 已确认 by the automatic lookup, 已验证 by 刷新
      // (F-01: the words say which; no tip explaining how either works)
      case 'ok': return { face: frag(ic('check'), T.t('overview.exit.prov.ok', { how: b.how, ago: F.ago(b.at || Date.now()) })), cls: 'ok' };
      case 'info': return { face: frag(ic('check'), exitShort(b.reason)), cls: 'ok', tip: exitLong(b.reason) };
      case 'warn': return { face: frag(ic('warning'), exitShort(b.reason)), cls: 'warn', tip: exitLong(b.reason) };
      case 'expired': return { face: frag(T.t('overview.exit.prov.expired')), cls: 'warn', tip: exitLong(b.reason || 'identity_stale') };
      case 'failed': return { face: frag(ic('error'), exitShort(b.reason)), cls: 'err', tip: exitLong(b.reason) };
      case 'stale': return { face: frag(T.t('overview.exit.prov.stale')), cls: 'warn' };
      default: return { face: frag(T.t('overview.exit.prov.idle', { state: b.state === 'starting' ? 'starting' : b.state === 'wait' ? 'wait' : 'other' })), cls: '' };
    }
  }
  // one family row: the address, or (no address) the reason it has none (D15: never a fake address)
  function legRow(ip, copy, src, leg, fam) {
    const addr = leg && leg.address;
    if (addr) { ip.setAttribute('translate', 'no'); ip.classList.remove('is-why'); setText(ip, addr); }
    else {
      ip.removeAttribute('translate'); ip.classList.add('is-why');
      setText(ip, leg && leg.reason ? exitShort(leg.reason) : T.t('overview.exit.leg.none', { status: leg ? leg.status : 'unknown' }));
    }
    copy.hidden = !addr;
    // 单一来源 (C2 item 1): the chip and its tip exist only while the runtime says so
    const single = !!(addr && leg.single);
    if (single && src.hidden) T.ti(src, 'overview.exit.single_source_tip', null, { attr: 'data-tip' });
    else if (!single && !src.hidden) { src.removeAttribute('data-tip'); src.removeAttribute('data-i18n-attr'); }
    src.hidden = !single;
    shownAddr[fam] = addr || null;
  }
  function syncExit(animate) {
    const run = runNow(), on = run === 'running', v = exitCard(exitIn());
    R.exit.dataset.state = v.show === 'addr' ? 'ok' : 'none';
    R.exit.classList.toggle('is-pending', v.dim);
    R.exit.classList.toggle('is-last', v.last);
    // automatic lookup off (G09): nothing waits for a lookup, so the badge says 未查询, not 等待查询
    const b = badgeFace(v.show !== 'addr' && v.msg.kind === 'auto_off' && v.badge.kind === 'idle' ? { kind: 'idle', state: 'other' } : v.badge);
    ['ok', 'warn', 'err'].forEach(c => R.prov.classList.toggle(c, c === b.cls));
    if (b.tip) R.prov.setAttribute('data-tip', b.tip); else R.prov.removeAttribute('data-tip');
    swapRich(R.prov, b.face, { instant: !animate });
    R.refresh.classList.toggle('is-away', !v.refresh.shown);
    R.refresh.disabled = !v.refresh.enabled;
    fitExit();
    if (v.show === 'addr') {
      const { v4, v6 } = v.legs;
      legRow(R.ip4, R.copy4, R.src4, v4, 'v4'); legRow(R.ip6, R.copy6, R.src6, v6, 'v6');
      // the location and network lines: the first family with an address; city, time zone, ASN,
      // operator, type only when the runtime reported them (C2 item 1)
      const main = v4.address ? v4 : v6;
      const loc = [main.country ? F.region(main.country) : null, main.city, main.timezone].filter(Boolean).join(' · ');
      setText(R.loc, loc); R.loc.hidden = !loc;
      const net = [main.asn ? `AS${main.asn}` : null, main.org].filter(Boolean).join(' · ');
      R.netT.textContent = net;
      if (main.type) T.ti(R.netType, 'overview.exit.net_type', { type: main.type }); else R.netType.textContent = '';
      R.netType.hidden = !main.type;
      R.net.hidden = !net && !main.type;
      R.meta.hidden = R.loc.hidden && R.net.hidden;
      // IPv6's own location when it differs (G07); 位置同上 when it is IPv4's
      const sameShown = !!(v4.address && v6.address);
      R.same.hidden = !sameShown;
      if (sameShown) {
        if (v.same) { R.same.removeAttribute('translate'); setText(R.same, T.t('overview.exit.same_location')); }
        else { R.same.setAttribute('translate', 'no'); setText(R.same, F.region(v6.country)); }
      }
    } else {
      shownAddr = { v4: null, v6: null };
      /* no address: the two dashes and the badge say it. F-01: no sentence on why there is none
         (stopped, starting, waiting, querying — the hero, the pill and the badge already show it)
         or on how the lookup works; only a reason (the check failed, the exit is in doubt) has
         its words: the short one, then the backend's full sentence (errors.exit.*) */
      const m = v.msg, why = m.kind === 'hidden' || m.kind === 'failed';
      R.idleMsg.hidden = !why;
      R.exit.classList.toggle('is-why', why);
      if (why) {
        const title = exitShort(m.reason), sub = exitLong(m.reason);
        // a reason's full sentence may ellipsize: the tip carries it whole
        R.idleS.setAttribute('data-tip', sub);
        M.swapText(R.idleT, title, { instant: !animate });
        M.swapText(R.idleS, sub, { instant: !animate });
      }
    }
    if (v.show === 'addr') R.exit.classList.remove('is-why');
    // the route (D17): running, the carrier's node (and the Gateway exit) by name. Node names are
    // data (<tier> runs: icon + name, not translated). Not running there is no route: the band
    // stays empty (F-01: no 「启动后经由 …」 forecast)
    const rl = routeLine({ run, carrier: carrierNow(), route: routeNow(), items: carrierItems(), gateway: gatewayNow(), tiers: tiersNow() });
    let route;
    const arrow = () => T.iconEl('arrow', 13, 'arr');
    const hopOf = (via, viaIcon) => {
      let n = 0;
      return kids => {
        // which hop by its order in the sentence (a translation may put the exit first: by text)
        const name = kids.textContent, isVia = name === via ? true : n++ === 0 && via == null;
        const s = span('hop', T.iconEl(isVia ? viaIcon : 'gateway', 14), kids);
        s.setAttribute('translate', 'no');
        return s;
      };
    };
    if (rl.kind === 'via') route = T.tx('overview.exit.route.running', { via: rl.via, exit: rl.exit }, { tier: hopOf(rl.via, D.tier(rl.viaTier).icon), x: arrow });
    else if (rl.kind === 'direct') route = T.tx('overview.exit.route.direct', { via: rl.via }, { tier: hopOf(rl.via, D.tier(rl.viaTier).icon) });
    // Gateway carries (M1): 经由未确认的承载层 → <the Gateway node>; the only hop is the exit
    else if (rl.kind === 'gateway') route = T.tx('overview.exit.route.gateway', { exit: rl.exit }, { tier: kids => { const s = span('hop', T.iconEl('gateway', 14), kids); s.setAttribute('translate', 'no'); return s; }, x: arrow });
    else if (rl.kind === 'unconfirmed') route = frag(T.t('overview.exit.route.unconfirmed'));
    else route = frag();
    R.exit.classList.toggle('is-off', !on);
    // F-01 round 1: no route, no band (its hairline and room go; the family rows move up under the head)
    R.exit.classList.toggle('is-noroute', !route.textContent.trim());
    // a stale route keeps its last words and says so (requirement 3, review 05a S6)
    const routeStale = on && rl.kind !== 'unconfirmed' && (stale('route.published') || (rl.kind === 'gateway' && stale('gateway.current')));
    if (routeStale) route = staleAfter(route);
    // the longer stale line may ellipsize: the tip says it whole
    if (routeStale) R.route.setAttribute('data-tip', route.textContent); else R.route.removeAttribute('data-tip');
    swapRich(R.route, route, { instant: !animate });
  }
  function flashExit() {
    if (!visible || M.reduced) return;
    [R.ip4, R.ip6].forEach((ip, i) => ip.animate([{ opacity: 1 }, { opacity: 0 }], { duration: 900, delay: i * 70, easing: 'ease-out', pseudoElement: '::after' }));
  }

  /* ============================================================ deck */
  const DECK = ['vanguard', 'sentinel', 'bastion'];
  const cards = {};
  function buildDeck() {
    DECK.forEach(k => {
      const t = D.tier(k);
      const b = T.h('button', { class: 'dcard', type: 'button', 'data-tier': k });
      b.append(T.html`<span class="dcard-top"><span class="dc-row1"><span class="pill" data-pill><span class="sdot"></span><span data-pt></span></span><span class="dc-more">${txt('overview.deck.more')}${I('chevron', 13)}</span></span><span class="dc-node" data-node></span></span>
        <span class="dcard-id">${I(t.icon)}<span class="dc-name" translate="no">${t.name}</span><span class="dc-stat" data-stat></span><span class="dots" data-dots></span></span>`);
      label(b);
      // a card behind the front one hands off only its visible band
      b.addEventListener('click', () => T.go('profiles', { tier: k, flipFrom: b.classList.contains('is-front') ? b : b.querySelector('.dcard-id'), flipKey: 'tier:' + k }));
      R.deck.append(b); cards[k] = b;
    });
    deckAria();
    R.gate.addEventListener('click', () => T.go('profiles', { tier: 'gateway', flipFrom: R.gate, flipKey: 'tier:gateway' }));
  }
  // the card's name for a screen reader: the code name and where it leads (read now, so it follows
  // the language); the Gateway row below the deck says the same sentence
  function deckAria() {
    const say = (el, k) => el.setAttribute('aria-label', T.t('overview.deck.card_aria', { name: D.tier(k).name }));
    DECK.forEach(k => say(cards[k], k));
    say(R.gate, 'gateway');
  }
  function deckMetrics() { const cs = getComputedStyle(R.root); return { B: parseFloat(cs.getPropertyValue('--dk-b')) || 30 }; }
  const tf = (r, B) => `translateY(${r * B}px) scale(${(1 - r * 0.035).toFixed(3)})`;
  // the runtime order has four ids, the carrier first (PROTOCOL 5.3): the deck takes the three
  // fallback tiers in that order, never gateway (ruling 4th wave 02b-8)
  function layoutDeck() {
    const { B } = deckMetrics(), order = deckOrder(S.get('order'));
    order.forEach((k, r) => {
      const el = cards[k]; if (!el) return;
      el.dataset.r = r; el.style.zIndex = String(10 - r); el.style.transform = tf(r, B);
      el.classList.toggle('is-front', r === 0);
      el.tabIndex = 0;
    });
    fitBands(); fitNode(); fitRun(); fitExit();
  }
  function reorderDeck(prevRaw, animate) {
    const order = deckOrder(S.get('order')), prevOrder = Array.isArray(prevRaw) ? deckOrder(prevRaw) : null;
    layoutDeck();
    if (!animate || M.reduced || !prevOrder) return;
    const { B } = deckMetrics(), D0 = 620;
    order.forEach((k, r1) => {
      const r0 = prevOrder.indexOf(k); if (r0 < 0 || r0 === r1) return;
      const rising = r1 < r0, el = cards[k];
      el._movedAt = performance.now();
      const mid = (r0 + r1) / 2;
      const midT = `translate(${rising ? 18 : -14}px, ${mid * B + (rising ? 8 : -4)}px) scale(${(1 - mid * 0.035 + (rising ? 0.012 : -0.01)).toFixed(3)})`;
      // the cards pass each other: z-order swaps discretely at the midpoint (not an animated value)
      el.animate([
        { transform: tf(r0, B), zIndex: 10 - r0 },
        { transform: midT, zIndex: 10 - r0, offset: 0.49 },
        { transform: midT, zIndex: 10 - r1, offset: 0.5 },
        { transform: tf(r1, B), zIndex: 10 - r1 },
      ], { duration: D0, easing: 'cubic-bezier(.45,0,.2,1)' });
      // the cards carry their content through the pass: the rising card's front area is in
      // before it crosses over, the sinking card's stays until it is covered (never a blank card)
      const top = el.querySelector('.dcard-top');
      top.animate(rising
        ? [{ opacity: 0 }, { opacity: 0, offset: 0.22 }, { opacity: 1, offset: 0.46 }, { opacity: 1 }]
        : [{ opacity: 1 }, { opacity: 1, offset: 0.52 }, { opacity: 0, offset: 0.72 }, { opacity: 0 }],
      { duration: D0, easing: 'linear' });
    });
  }
  // dotsOf() / deckStat() specs -> paintDots specs (a meter carries its class list)
  const METER = { off: ' ink is-off', ink: ' ink', warn: ' warn', fail: ' is-off', '': ' ink' };
  const dotsSpec = d => (d.meter ? { meter: `meter dc-meter${METER[d.style || ''] || ' ink'}`, v: (Number(d.v) || 0).toFixed(3) } : { n: d.n, ok: d.ok, style: d.style || '' });
  function paintDots(el, spec) {
    const key = JSON.stringify(spec), mt = el.firstElementChild;
    if (spec.meter && mt && mt.classList.contains('dc-meter')) {
      // the pool meter stays in place and glides to its new fill
      mt.className = spec.meter; mt.style.setProperty('--v', spec.v); el._h = key;
    } else if (el._h !== key) { el._h = key; el.replaceChildren(...(spec.meter ? [meterEl(spec.meter, spec.v)] : dotKids(spec.n, spec.ok, spec.style))); }
  }
  /* a node line (E11): name and protocol are data (not translated), the latency a message — the
     last checked value (B-01: no 1 Hz latency), 未测 without one; amber when the runtime marks the
     node slow (Node.slow, T-P4-C2 item 4) */
  function nodeLine(n) {
    let lat;
    if (n.latency_ms != null) {
      lat = T.tx('overview.deck.node.lat', { lat: n.latency_ms });
      const b = lat.querySelector('b'); if (b && n.slow === true) b.classList.add('warn');
    } else lat = T.t('overview.deck.node.untested');
    return frag(data(n.name), ...(n.protocol ? [' · ', data(n.protocol)] : []), ' · ', lat);
  }
  // deckStat() kinds -> their message (literal ids for the catalog gates). F-01: Bastion's pool
  // with no numbers yet (bastion_unknown, bastion_after_ready while starting) is a dash, no sentence
  const DECK_STAT = {
    unknown: 'overview.deck.stat.unknown', bastion_off: 'overview.deck.stat.bastion_off', bastion_unknown: 'overview.deck.stat.unknown',
    collecting: 'overview.deck.stat.collecting', bastion_after_ready: 'overview.deck.stat.unknown',
    bastion_filling: 'overview.deck.stat.bastion_filling', bastion_low: 'overview.deck.stat.bastion_low', bastion_standby: 'overview.deck.stat.bastion_standby',
    unconfigured: 'overview.deck.stat.unconfigured', off: 'overview.deck.stat.off', nodes: 'overview.deck.stat.nodes', down: 'overview.deck.stat.down',
    usable: 'overview.deck.stat.usable', standby: 'overview.deck.stat.standby', unconfirmed: 'overview.deck.stat.unconfirmed',
  };
  const listOf = k => { const v = raw('nodes.list', k); return v && Array.isArray(v.items) ? v.items : []; };
  const PILL = {
    slow: ['pill-warn', 'warn'], fallback: ['pill-warn', 'warn'], down: ['pill-line', 'fail'], carrying: ['pill-ok', 'ok'], unconfirmed: ['pill-line', 'ok'], standby: ['', 'ok'],
    starting: ['pill-line', 'busy'], cancelling: ['pill-line', 'busy'], stopping: ['pill-line', 'busy'], stopped: ['pill-line', 'off'], unknown: ['pill-line', 'off'],
  };
  function syncDeck(animate) {
    const run = runNow(), order = deckOrder(S.get('order')), front = order[0], tiers = tiersNow(), col = S.get('collected') || {};
    const sumKnown = known('tiers.summary');
    const carrier = carrierNow(), cancelling = !!S.get('cancelling'), fb = sumKnown && !!S.get('fallback');
    const ghost = run === 'stopped' || run === 'stopping' || run === 'failed' || run === 'unknown';
    R.deck.classList.toggle('is-ghost', ghost);
    const bp = S.get('bastion') || {};
    const slow = carrierSlow();
    DECK.forEach(k => {
      const el = cards[k];
      const stat = el.querySelector('[data-stat]'), dotsEl = el.querySelector('[data-dots]');
      // a tier that is off stays quiet: its state line is ink-3
      const ds = deckStat({ tier: k, run, carrier, front, tiers, collected: col, bastion: bp, bastionKnown: known('bastion.pool'), summaryKnown: sumKnown });
      const sk = ds.stat.kind;
      let s = T.t(DECK_STAT[sk] || 'overview.deck.stat.unknown', ds.stat);
      // Bastion's numbers come from bastion.pool: kept and marked while that topic is stale
      // (requirement 3, review 05a S6). F-04 follow-up: the last count alone, without a state word
      // (what the pool does now is unknown), so the band stays whole in every language
      const staleNums = k === 'bastion' && run === 'running' && /^bastion_(filling|low|standby)$/.test(sk) && stale('bastion.pool');
      if (staleNums) s = T.t('overview.common.stale_after', { text: T.t('overview.deck.stat.bastion_count', ds.stat) });
      ['warn', 'err', 'mute'].forEach(c => stat.classList.toggle(c, c === ds.tone));   // toggled, never overwritten (.swap must stay)
      M.swapText(stat, s, { instant: !animate });
      paintDots(dotsEl, dotsSpec(ds.dots));
      // front-only area (present on every card so a card never passes empty)
      const pillEl = el.querySelector('[data-pill]'), pt = el.querySelector('[data-pt]'), node = el.querySelector('[data-node]');
      const pstate = deckPill({ tier: k, run, cancelling, carrier, tiers, slow });
      const [pcls, dcls] = PILL[pstate] || PILL.unknown;
      let face = frag();
      if (pstate === 'slow') {
        // B-01 (feature branch only): the prototype's latency-rising line
        const n = carrierNode();
        face = n ? frag(data(n.name), ' · ', span('warn', F.ms(n.latency_ms)), ' · ', T.t('overview.deck.node.over_limit')) : frag();
      } else if (pstate === 'fallback' || pstate === 'carrying') {
        const n = k === carrier ? carrierNode() : null;
        // the node line comes from route.published + nodes.list: kept and marked while stale
        if (n) face = stale('route.published') || staleKeyed('nodes.list', k) ? staleAfter(nodeLine(n)) : nodeLine(n);
      } else if (pstate === 'unconfirmed') {
        // Gateway carries (M1): the node under the Gateway exit is not reported
        face = frag(T.t('overview.deck.node.unconfirmed'));
      } else if (pstate === 'down') {
        const r = downReason(listOf(k));
        face = frag(r.code && !r.mixed ? T.t('overview.deck.node.down_reason', { reason: T.rt.reasonText('tiers.reason', r.code) }) : T.t('overview.deck.node.down'));
      } else if (pstate === 'standby') {
        // B-01: a standby tier's current node needs a runtime fact v1 does not have; without it
        // the standby card shows its counts only (E12)
        if (T.rt.feature('B-01')) {
          const t = ((raw('tiers.summary') || {}).tiers || {})[k];
          if (t && t.current && t.current.name) face = frag(data(t.current.name));
        }
      }
      // starting, cancelling, stopping, stopped, unknown: the pill says the state; the node line
      // stays empty (F-01: no phase repeated, no 「启动后依次回退：…」 explaining the order the
      // deck itself shows)
      pillEl.className = `pill${pcls ? ' ' + pcls : ''}`; pillEl.firstElementChild.className = `sdot ${dcls}`;
      const still = !animate || k !== front || (el._movedAt && performance.now() - el._movedAt < 700);
      M.swapText(pt, pstate === 'unconfirmed' ? T.t('overview.deck.pill_unconfirmed') : T.t('overview.deck.pill', { state: pstate }), { instant: still });
      swapRich(node, face, { instant: still });
    });
    // gateway (detached, outside the fallback order; F1-F7)
    const gr = gatewayRow({ run, tiers, collected: col, phase: S.get('phase'), gateway: gatewayNow() });
    let gs;
    switch (gr.kind) {
      case 'off': gs = frag(T.t('overview.gate.stat.off')); break;
      case 'stopped': gs = frag(data(gr.name)); break;
      case 'waiting': gs = frag(T.t('overview.gate.stat.waiting')); break;
      case 'verifying': gs = frag(T.t('overview.gate.stat.verifying')); break;
      case 'missing': gs = T.tx('overview.gate.stat.missing', { name: gr.name || '' }, { x: warnSlot, tier: dataSlot }); break;
      case 'pending': gs = T.tx('overview.gate.stat.pending', { name: gr.name || '' }, { x: wideSlot, tier: dataSlot }); break;
      case 'current': gs = T.tx('overview.gate.stat.current', { name: gr.name, ok: gr.ok, total: gr.total }, { x: wideSlot, tier: dataSlot }); break;
      // chosen but not in force: never "current" (T-P4-H2 item 3)
      case 'selected': gs = T.tx('overview.gate.stat.selected', { name: gr.name, ok: gr.ok, total: gr.total }, { x: wideSlot, tier: dataSlot }); break;
      default: gs = run === 'unknown' || !sumKnown ? frag(T.t('overview.deck.stat.unknown')) : frag(T.t('overview.gate.stat.counts', { ok: gr.ok, total: gr.total }));
    }
    // a stale gateway.current keeps the last state and says so (requirement 3, review 05a S6)
    if (run === 'running' && gr.name && ['current', 'selected', 'missing', 'pending'].includes(gr.kind) && stale('gateway.current')) gs = staleAfter(gs);
    R.gateStat.classList.toggle('mute', gr.tone === 'mute');
    R.gateStat.classList.toggle('warn', gr.tone === 'warn');
    swapRich(R.gateStat, gs, { instant: !animate });
    paintDots(R.gateDots, dotsSpec(gr.dots));
    // header, then the extension point: the head states only a state the deck does not show by
    // itself (stale, fallen back); F-01: not 服务未启动, not how it switches back
    const meta = stale('tiers.summary') ? frag(T.t('overview.common.stale'))
      : run === 'running' && fb ? frag(T.t('overview.deck.meta.fallback')) : frag();
    swapRich(R.deckMeta, meta, { instant: !animate });
    if (deckExt && typeof deckExt.sync === 'function') deckExt.sync({ run, fallback: !!fb });
    fitBands(); fitNode();
  }
  /* T-P5-13 (决定_P5裁定 P5-e #5, form a): the front card's node line is read whole (a stopped order,
     a down reason: de … sind alle nicht verfügbar, wird ausgehender Verkehr blockiert). It wraps,
     and the deck grows by its extra lines (--dk-h on the deck: every card and the stack follow; the
     stacking step --dk-b does not change); one line keeps the designed height. Measured on the
     newest face; again after a resize (layoutDeck) */
  function fitNode() {
    const deck = R.deck;
    if (!deck || !deck.offsetWidth) return;
    deck.style.removeProperty('--dk-h');
    deck.classList.remove('is-bare');
    // not running, every card is its tier band only (css .deck.is-ghost)
    if (deck.classList.contains('is-ghost')) return;
    const front = deck.querySelector('.dcard.is-front'), node = front && front.querySelector('[data-node]'); if (!node) return;
    const face = Array.from(node.querySelectorAll('.sw')).filter(x => !x.hasAttribute('aria-hidden')).pop();
    /* F-01 round 2: a front card with nothing on its second line (starting, cancelling, a carrier
       whose node is not known …) keeps its state pill and folds the empty line away: the deck's
       --dk-h becomes the pill row and the band's padding, whatever the state */
    if (!face || !face.textContent.trim()) {
      deck.classList.add('is-bare');
      const top = front.querySelector('.dcard-top'), row = top && top.querySelector('.dc-row1');
      if (!row) return;
      const cs = getComputedStyle(top), B = parseFloat(getComputedStyle(deck).getPropertyValue('--dk-b')) || 30;
      deck.style.setProperty('--dk-h', `${Math.ceil((parseFloat(cs.paddingTop) || 0) + row.getBoundingClientRect().height + (parseFloat(cs.paddingBottom) || 0)) + B}px`);
      return;
    }
    const lh = parseFloat(getComputedStyle(node).lineHeight) || 18;
    const lines = Math.round(face.getBoundingClientRect().height / lh);
    if (lines > 1) deck.style.setProperty('--dk-h', `${(parseFloat(getComputedStyle(deck).getPropertyValue('--dk-h')) || 92) + (lines - 1) * lh}px`);
  }
  /* T-P5-11 (ruling 2: interface words are read whole, an ellipsis is for data): a deck band (icon,
     name, state words, dots) or the Gateway line that cannot hold its state words lets its dots or
     pool meter step out (.is-tight; the tiers page shows them per node), so the state words
     (0/3 · nicht verfügbar …) show whole. (F-03: the role word that used to step out first is gone.) The state words are never swapped for a shorter form (fix round 1, ruling 2): the band
     is one line by the deck's geometry (its height is the cards' stacking step, --dk-b), so a
     sentence that does not fit even then is a translation to shorten (the render matrix asserts
     the off Bastion's sentence and the stale pool numbers whole in nine languages at 1060 px).
     Measured on the newest face in fractional pixels; again after a resize (layoutDeck) and a
     language switch (syncDeck runs again). A hidden page measures nothing and keeps what it has. */
  const statShort = st => {
    const face = Array.from(st.querySelectorAll('.sw')).filter(x => !x._out).pop() || st;
    const rg = document.createRange(); rg.selectNodeContents(face);
    return rg.getBoundingClientRect().width - contentW(st) > 0.02;
  };
  function fitBands() {
    for (const b of [...DECK.map(k => cards[k] && cards[k].querySelector('.dcard-id')), R.gate]) {
      if (!b || !b.offsetWidth) continue;
      const st = b.querySelector('.dc-stat'); if (!st) continue;
      b.classList.remove('is-tight');
      if (statShort(st)) b.classList.add('is-tight');
    }
  }

  /* ============================================================ traffic + chart */
  /* 流量 (G3-G6): the session counters and rates of traffic.tick; unknown before its first sample
     (a dash, no zero); the scope label says what the counters mean today (B-02b: 累计 since the
     last config apply, never 本次会话 — the prototype's label stays in the feature branch) */
  function syncTraffic(animate) {
    const run = runNow(), on = run === 'running', has = known('traffic.tick') && run !== 'unknown';
    const t = S.get('traffic') || { down: 0, up: 0, rateDown: 0, rateUp: 0 };
    const [dn, du] = has ? F.bytesParts(t.down) : ['—', ''], [un, uu] = has ? F.bytesParts(t.up) : ['—', ''];
    // (unknown: the dash on screen, words for a screen reader)
    const none = T.t('overview.traffic.unknown_aria');
    M.odometer(R.totD, dn, { label: has ? F.bytes(t.down) : none, instant: !animate });
    M.odometer(R.totU, un, { label: has ? F.bytes(t.up) : none, instant: !animate });
    M.swapText(R.totDu, du, { instant: !animate }); M.swapText(R.totUu, uu, { instant: !animate });
    const rd = on && has ? F.rate(t.rateDown) : '—', ru = on && has ? F.rate(t.rateUp) : '—';
    R.rateD.textContent = rd; R.rateU.textContent = ru;   // live rates: calm 1 Hz text, no roll
    const tick = raw('traffic.tick');
    let scope;
    if (stale('traffic.tick')) scope = T.t('overview.common.stale');
    else if (tick && tick.error_code) scope = T.t('overview.traffic.error');
    else if (T.rt.feature('B-02b')) scope = T.t('overview.traffic.scope', { scope: run === 'stopped' || run === 'stopping' ? 'last' : 'current' });
    else scope = T.t('overview.traffic.cumulative');
    R.scope.classList.toggle('warn', !!(stale('traffic.tick') || (tick && tick.error_code)));
    M.swapText(R.scope, scope, { instant: !animate });
    // not running: the plot greys out and says nothing (F-01: the user knows why there is no curve)
    if (chart) chart.setPaused(!on, animate);
  }

  const NICE = [0.25, 0.5, 1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10, 12, 16, 20, 30, 40].map(v => v * F.MB);
  const niceMax = v => NICE.find(s => v <= s) || NICE[NICE.length - 1];
  // axis ticks: up to two decimals in MB/s, whole KB/s below (the fmt catalog's units and joiner)
  const axisTxt = v => (v <= 0 ? '0' : T.t('fmt.qty', {
    num: F.num(v >= F.MB ? +(v / F.MB).toFixed(2) : Math.round(v / F.KB)),
    unit: T.t('fmt.unit.per_second', { unit: T.t(v >= F.MB ? 'fmt.unit.mb' : 'fmt.unit.kb') }),
  }));
  function mono(xs, ys) {
    const n = xs.length; if (n < 2) return '';
    const dx = [], m = [], t = new Array(n);
    for (let i = 0; i < n - 1; i++) { dx[i] = xs[i + 1] - xs[i]; m[i] = (ys[i + 1] - ys[i]) / (dx[i] || 1e-6); }
    t[0] = m[0]; t[n - 1] = m[n - 2];
    for (let i = 1; i < n - 1; i++) {
      if (m[i - 1] * m[i] <= 0) t[i] = 0;
      else { const w1 = 2 * dx[i] + dx[i - 1], w2 = dx[i] + 2 * dx[i - 1]; t[i] = (w1 + w2) / (w1 / m[i - 1] + w2 / m[i]); }
    }
    let d = `M${xs[0].toFixed(1)},${ys[0].toFixed(1)}`;
    for (let i = 0; i < n - 1; i++) {
      const hh = dx[i] / 3;
      d += `C${(xs[i] + hh).toFixed(1)},${(ys[i] + t[i] * hh).toFixed(1)} ${(xs[i + 1] - hh).toFixed(1)},${(ys[i + 1] - t[i + 1] * hh).toFixed(1)} ${xs[i + 1].toFixed(1)},${ys[i + 1].toFixed(1)}`;
    }
    return d;
  }
  /* the chart's series (H2-H4): page copies of traffic.history's 24 h buckets (the last bucket
     follows the ticks, H3) and the projection's live ring (one point per running tick). A
     snapshot, also a gap replay of a rebuilt monitor (PROTOCOL 5.4), replaces both. */
  const LIVE_SLOTS = 300, H24_SLOTS = 288, BUCKET_MS = 300000;
  const HIST = { h24: [], live: [] };
  function loadHistory() {
    const h = S.get('history'), ok = !!(h && !h.placeholder);
    HIST.h24 = ok ? (h.h24 || []).filter(p => p && Number.isFinite(p.t)).map(p => ({ t: p.t, d: Number(p.d) || 0, u: Number(p.u) || 0, n: p.n })) : [];
    HIST.live = ok && Array.isArray(h.live) ? h.live : [];
  }
  let chart = null;
  function makeChart(box) {
    // (the gradient stops get their colour through the CSSOM: no style attribute in markup)
    box.replaceChildren(T.html`<svg aria-hidden="true"><defs><linearGradient id="ch-gd" x1="0" y1="0" x2="0" y2="1"><stop offset="0"/><stop offset="1"/></linearGradient><clipPath id="ch-clip"><rect class="ch-cr" x="0" y="-8" width="0" height="4000"/></clipPath></defs>
      <g class="ch-grid"><line/><line/><line class="base"/></g>
      <g class="ch-ghost"><use href="#ch-slide"/></g>
      <g class="ch-plot" clip-path="url(#ch-clip)"><g class="ch-slide" id="ch-slide"><path class="ch-au"/><path class="ch-ad"/><path class="ch-lu"/><path class="ch-ld"/></g></g>
      <circle class="ch-end ch-eu" r="2.8" cx="0" cy="0"/><circle class="ch-end ch-ed" r="3.6" cx="0" cy="0"/>
      <g class="ch-hov"><line/><circle class="u" r="3.4"/><circle class="d" r="3.8"/></g></svg>
      <div class="ch-y"></div><div class="ch-x"></div><div class="ch-tip"></div>`);
    const q = s => box.querySelector(s);
    box.querySelectorAll('#ch-gd stop').forEach((st, i) => { st.style.stopColor = 'var(--c-accent)'; st.style.stopOpacity = i ? '0' : '.16'; });
    T.ti(box, 'overview.chart.aria', { range: 'h24' }, { attr: 'aria-label' });
    const P = { svg: q('svg'), cr: q('.ch-cr'), slide: q('.ch-slide'), ld: q('.ch-ld'), lu: q('.ch-lu'), ad: q('.ch-ad'), au: q('.ch-au'), ed: q('.ch-ed'), eu: q('.ch-eu'),
      grid: Array.from(box.querySelectorAll('.ch-grid line')), yWrap: q('.ch-y'), xWrap: q('.ch-x'), tip: q('.ch-tip'),
      hl: q('.ch-hov line'), hd: q('.ch-hov .d'), hu: q('.ch-hov .u') };
    P.yWrap.innerHTML = '<span></span><span></span><span></span>';
    const ylab = Array.from(P.yWrap.children);
    const pad = { l: 54, r: 6, t: 8, b: 22 };
    // the series are the page's (HIST: traffic.history + ticks); x by time: 24 h = 288 five-minute
    // slots ending at the newest bucket, 5 min = 300 one-second slots ending at 现在 (fewer points
    // fill the right end only, never stretched across the plot); a hole longer than 1.5 slots
    // breaks the line (H4: no interpolation across a gap)
    let W = 0, H = 0, mode = '24h', yMax = 0, yFrom = 0, yTo = 0, yT0 = 0, morph = null, reveal = null, hover = null, raf = 0, lastTick = performance.now(), paused = false, empty = false, active = false, slideAnims = [];
    const pw = () => W - pad.l - pad.r, ph = () => H - pad.t - pad.b;
    const yOf = v => pad.t + (1 - T.util.clamp(v / (yMax || 1), 0, 1.06)) * ph();
    const pts = md => ((md || mode) === '5m' ? HIST.live : HIST.h24);
    const slides = () => mode === '5m' && !paused && !M.reduced;
    function geom(md) {
      const a = pts(md), n = a.length, R0 = W - pad.r;
      if ((md || mode) === '5m') { const step = pw() / (LIVE_SLOTS - 2); return { xs: a.map((_, i) => R0 + step - (n - 1 - i) * step), ds: a.map(p => p.d), us: a.map(p => p.u), step, a, br: breaksOf(a, 1000) }; }
      const end = n ? a[n - 1].t : 0, span = (H24_SLOTS - 1) * BUCKET_MS;
      return { xs: a.map(p => pad.l + (1 - (end - p.t) / span) * pw()), ds: a.map(p => p.d), us: a.map(p => p.u), step: 0, a, br: breaksOf(a, BUCKET_MS) };
    }
    function wantMax(md) { const a = pts(md); let mx = 0; const from = (md || mode) === '5m' ? 1 : 0; for (let i = from; i < a.length; i++) if (a[i].d > mx) mx = a[i].d; return niceMax(mx * 1.1); }
    // one subpath per unbroken run (a run of one point draws nothing)
    function segPaths(xs, ys, br, base) {
      const cuts = [0, ...(br || []), xs.length];
      let line = '', area = '';
      for (let k = 0; k < cuts.length - 1; k++) {
        const a = cuts[k], b = cuts[k + 1];
        if (b - a < 2) continue;
        const l = mono(xs.slice(a, b), ys.slice(a, b));
        line += l; area += `${l}L${xs[b - 1].toFixed(1)},${base}L${xs[a].toFixed(1)},${base}Z`;
      }
      return { line, area };
    }
    function drawPaths(xs, ds, us, br) {
      const yD = ds.map(yOf), yU = us.map(yOf), b = (pad.t + ph()).toFixed(1);
      const D0 = segPaths(xs, yD, br, b), U0 = segPaths(xs, yU, br, b);
      P.ld.setAttribute('d', D0.line); P.lu.setAttribute('d', U0.line);
      P.ad.setAttribute('d', D0.area); P.au.setAttribute('d', U0.area);
      return { yD, yU };
    }
    function setEnd(c, x, y) { c.getAnimations().forEach(a => a.cancel()); c.style.transform = `translate(${x}px, ${y}px)`; }
    // 0 or 1 point: the empty state (ruling 5th wave 10-5: no NaN), nothing is drawn. Paused (not
    // running) the plot greys out. F-01: neither state lays a note over the grid; the chart is quiet
    function syncNote() {
      empty = !drawable(pts());
      box.classList.toggle('is-paused', paused);
      box.classList.toggle('is-empty', !paused && empty);
    }
    function render() {
      if (!W || morph) return;
      slideAnims.forEach(a => a.cancel()); slideAnims = [];
      syncNote();
      if (empty) { ['ld', 'lu', 'ad', 'au'].forEach(k => P[k].setAttribute('d', '')); return; }
      const g = geom(), { yD, yU } = drawPaths(g.xs, g.ds, g.us, g.br), R0 = W - pad.r, n = g.xs.length;
      if (mode === '5m') {
        const p = slides() ? T.util.clamp((performance.now() - lastTick) / 1000, 0, 1) : 1;
        if (p < 1) {
          const dur = (1 - p) * 1000;
          slideAnims.push(P.slide.animate([{ transform: `translateX(${-g.step * p}px)` }, { transform: `translateX(${-g.step}px)` }], { duration: dur, easing: 'linear', fill: 'forwards' }));
          [[P.ed, yD], [P.eu, yU]].forEach(([c, ys]) => {
            const y0 = T.util.lerp(ys[n - 2], ys[n - 1], p);
            c.style.transform = `translate(${R0}px, ${ys[n - 1]}px)`;
            slideAnims.push(c.animate([{ transform: `translate(${R0}px, ${y0}px)` }, { transform: `translate(${R0}px, ${ys[n - 1]}px)` }], { duration: dur, easing: 'linear' }));
          });
        } else { P.slide.style.transform = `translateX(${-g.step}px)`; setEnd(P.ed, R0, yD[n - 1]); setEnd(P.eu, R0, yU[n - 1]); }
      } else { P.slide.style.transform = 'none'; setEnd(P.ed, R0, yD[n - 1]); setEnd(P.eu, R0, yU[n - 1]); }
    }
    function yLabels(instant) {
      const tgt = morph ? morph.yTo : yTo;
      ylab.forEach((s, i) => { s.style.top = `${pad.t + (1 - i / 2) * ph()}px`; M.swapText(s, axisTxt(tgt * i / 2), { instant }); });
    }
    function xLabels(instant) {
      const now = T.t('overview.chart.x.now');
      const items = mode === '5m'
        ? [5, 4, 3, 2, 1].map((n, i) => [T.t('overview.chart.x.minutes_ago', { n }), i * 0.2]).concat([[now, 1]])
        : [0, 0.25, 0.5, 0.75, 1].map(f => [f === 1 ? now : F.hm(Date.now() - (1 - f) * 86400000), f]);
      const build = () => {
        P.xWrap.replaceChildren(...items.map(([t, f], i) => {
          const s = span(null, t);
          s.style.left = `${(pad.l + f * pw()).toFixed(1)}px`;
          s.style.transform = `translateX(${i === 0 ? '0' : i === items.length - 1 ? '-100%' : '-50%'})`;
          return s;
        }));
      };
      if (instant || M.reduced) { build(); return; }
      const a = P.xWrap.animate([{ opacity: 1 }, { opacity: 0 }], { duration: 130, fill: 'forwards' });
      M.onDone(a, () => { build(); a.cancel(); P.xWrap.animate([{ opacity: 0, transform: 'translateY(3px)' }, { opacity: 1, transform: 'none' }], { duration: 240, easing: M.ease.out }); }, 300);
    }
    function layout() {
      const r = box.getBoundingClientRect(); if (!r.width || !r.height) return;
      W = r.width; H = r.height;
      P.svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
      P.cr.setAttribute('x', pad.l); P.cr.setAttribute('width', pw() + 1);
      [1, 0.5, 0].forEach((f, i) => { const y = Math.round(pad.t + f * ph()) + 0.5; const l = P.grid[i]; l.setAttribute('x1', pad.l); l.setAttribute('x2', W - pad.r); l.setAttribute('y1', y); l.setAttribute('y2', y); });
      if (!yMax) yMax = yTo = wantMax();
      yLabels(true); xLabels(true); render();
    }
    function kick() { if (!raf && active) raf = requestAnimationFrame(frame); }
    // the frame loop exists only while a data morph (range switch, y-rescale) or a hover readout needs it
    function frame(now) {
      raf = 0; if (!W || !active) return;
      let more = false;
      if (yT0) { const k = Math.min(1, (now - yT0) / 480); yMax = T.util.lerp(yFrom, yTo, 1 - Math.pow(1 - k, 3)); if (k >= 1) yT0 = 0; else more = true; render(); }
      if (morph) {
        const k = Math.min(1, (now - morph.t0) / morph.dur), e = 1 - Math.pow(1 - k, 4), Mn = morph.b.d.length, step = pw() / (Mn - 1);
        const xs = Array.from({ length: Mn }, (_, i) => pad.l + i * step);
        const ds = morph.a.d.map((v, i) => T.util.lerp(v, morph.b.d[i], e)), us = morph.a.u.map((v, i) => T.util.lerp(v, morph.b.u[i], e));
        yMax = T.util.lerp(morph.yA, morph.yTo, e);
        const { yD, yU } = drawPaths(xs, ds, us, null);
        setEnd(P.ed, W - pad.r, yD[Mn - 1]); setEnd(P.eu, W - pad.r, yU[Mn - 1]);
        morph.cur = { ds, us };
        if (k >= 1) { yTo = yMax = morph.yTo; morph = null; render(); } else more = true;
      }
      if (hover != null) updateHover(now);
      if (more) kick();
    }
    function resample(arr, m) { const out = [], n = arr.length; if (n === m) return arr.slice(); for (let i = 0; i < m; i++) { const p = i / (m - 1) * (n - 1), a = Math.floor(p), b = Math.min(n - 1, a + 1); out.push(T.util.lerp(arr[a], arr[b], p - a)); } return out; }
    // what is on screen for a range: 24 h = 288 buckets across the plot; 5 min = the 299 samples
    // left of 现在 after the slide (the first sample sits under the left clip)
    function visibleSeries(md) { const a = pts(md); const s = md === '5m' ? a.slice(1) : a; return { d: s.map(p => p.d), u: s.map(p => p.u) }; }
    function setRange(r) {
      if (r === mode) return;
      T.ti(box, 'overview.chart.aria', { range: r === '5m' ? 'm5' : 'h24' }, { attr: 'aria-label' });
      const toMax = wantMax(r);
      // the morph needs both ranges full and unbroken (a partial or broken series switches at once)
      const full = HIST.h24.length >= H24_SLOTS && HIST.live.length >= LIVE_SLOTS && !breaksOf(HIST.h24, BUCKET_MS).length && !breaksOf(HIST.live, 1000).length;
      if (M.reduced || !W || !visible || !full) { morph = null; mode = r; yMax = yTo = toMax; yLabels(true); xLabels(true); render(); return; }
      // morph onto the destination's own point count and x-grid, so the last frame IS the
      // destination (no peaks popping in at the end); an interrupted morph continues from
      // the shape currently on screen, not from the previous target
      const b = visibleSeries(r), Mn = b.d.length;
      const src = morph && morph.cur ? { d: morph.cur.ds, u: morph.cur.us } : visibleSeries(mode);
      morph = { a: { d: resample(src.d, Mn), u: resample(src.u, Mn) }, b, t0: performance.now(), dur: 600, yA: yMax, yTo: toMax, cur: null };
      slideAnims.forEach(x => x.cancel()); slideAnims = []; P.slide.style.transform = 'none';
      mode = r; yTo = toMax; yLabels(false); xLabels(false); kick();
    }
    function onTick() {
      lastTick = performance.now();
      if (!active || !W) return;
      const want = wantMax();
      if (!morph && want !== yTo) { yFrom = yMax; yTo = want; yT0 = performance.now(); yLabels(false); kick(); }
      render();
      if (hover != null) kick();
    }
    function updateHover(now) {
      const R0 = W - pad.r;
      if (hover < pad.l - 4 || hover > R0 + 4 || (!morph && !drawable(pts()))) { box.classList.remove('is-hover'); return; }
      let xs, ds, us, a = null;
      if (morph && morph.cur) { ds = morph.cur.ds; us = morph.cur.us; const st = pw() / (ds.length - 1); xs = ds.map((_, i) => pad.l + i * st); }
      else { const g = geom(); const off = mode === '5m' && slides() ? -g.step * T.util.clamp((now - lastTick) / 1000, 0, 1) : mode === '5m' ? -g.step : 0; xs = g.xs.map(x => x + off); ds = g.ds; us = g.us; a = g.a; }
      let best = 0, bd = Infinity;
      for (let i = 0; i < xs.length; i++) { if (xs[i] < pad.l - 0.5 || xs[i] > R0 + 0.5) continue; const dd = Math.abs(xs[i] - hover); if (dd < bd) { bd = dd; best = i; } }
      const x = xs[best], b = pad.t + ph();
      box.classList.add('is-hover');
      P.hl.setAttribute('x1', x); P.hl.setAttribute('x2', x); P.hl.setAttribute('y1', pad.t); P.hl.setAttribute('y2', b);
      P.hd.setAttribute('cx', x); P.hd.setAttribute('cy', yOf(ds[best])); P.hu.setAttribute('cx', x); P.hu.setAttribute('cy', yOf(us[best]));
      const tt = !a ? '' : mode === '5m' ? F.time(a[best].t) : T.t('overview.chart.tip.average', { time: F.hm(a[best].t) });
      const rows = [['down', ds[best]], ['up', us[best]]].map(([k, v]) => [k, T.t(`overview.traffic.${k}`), F.rate(v)]);
      const key = JSON.stringify([tt, rows]);
      if (P.tip._h !== key) {
        P.tip._h = key;
        const tdiv = document.createElement('div'); tdiv.className = 'tt'; tdiv.textContent = tt || ' ';
        P.tip.replaceChildren(tdiv, ...rows.map(([k, word, rate]) => {
          const row = document.createElement('div'), sw = span('sw'), b = document.createElement('b');
          row.className = 'tr'; sw.style.background = `var(--c-series-${k})`; b.textContent = rate;
          row.append(sw, word, b);
          return row;
        }));
      }
      const tw = P.tip.offsetWidth || 150;
      P.tip.style.transform = `translate(${Math.round(x + 14 + tw > W ? x - 14 - tw : x + 14)}px, ${pad.t}px)`;
    }
    box.addEventListener('pointermove', e => { const r = box.getBoundingClientRect(); hover = e.clientX - r.left; kick(); });
    box.addEventListener('pointerleave', () => { hover = null; box.classList.remove('is-hover'); });
    if (window.ResizeObserver) new ResizeObserver(() => { if (active) layout(); }).observe(box);
    return {
      layout, setRange, onTick,
      // a language switch: the ticks and the hover readout are words
      relabel() { P.tip._h = null; if (W) { yLabels(true); xLabels(true); } },
      // draw-in: the plot's clip rect scales open (a transform animation, no per-frame script)
      show() {
        active = true; layout();
        if (reveal) reveal.cancel();
        reveal = M.reduced ? null : P.cr.animate([{ transform: 'scaleX(0)' }, { transform: 'scaleX(1)' }], { duration: 620, delay: 80, easing: 'cubic-bezier(.33,1,.68,1)', fill: 'backwards' });
      },
      hide() { active = false; hover = null; box.classList.remove('is-hover'); slideAnims.forEach(a => a.cancel()); slideAnims = []; if (raf) cancelAnimationFrame(raf); raf = 0; if (reveal) { reveal.cancel(); reveal = null; } },
      setPaused(on, animate) {
        const was = paused; paused = !!on;
        // going live (F-06): the colour runs across the grey history from the left (the plot's clip
        // rect opens, the draw-in's own transform); the grey copy under it stays until it has passed.
        // A sweep of its own, so it leaves early instead of waiting for the wave to reach the chart
        if (was && !paused && animate && active && W && visible && !M.reduced) {
          box.classList.add('is-sweep');
          if (reveal) reveal.cancel();
          const a = reveal = P.cr.animate([{ transform: 'scaleX(0)' }, { transform: 'scaleX(1)' }], { duration: 720, delay: Math.min(220, M.wave.delay(box)), easing: 'cubic-bezier(.45,0,.2,1)', fill: 'backwards' });
          M.onDone(a, () => { if (reveal === a) reveal = null; box.classList.remove('is-sweep'); }, 1300);
        }
        syncNote();
        if (was !== paused && active) render();
      },
      // new series (a traffic.history snapshot, a gap replay): rescale and redraw
      reload() { if (!W) { syncNote(); return; } morph = null; yMax = yTo = wantMax(); yLabels(true); render(); },
    };
  }

  /* ============================================================ protocols */
  /* the lightest end of the single-hue ramp: the least-needed panel carries no heavy fill */
  const RAMP = ['var(--c-ramp-2)', 'var(--c-ramp-3)', 'var(--c-ramp-4)', 'var(--c-ramp-5)', 'var(--c-ramp-5)', 'var(--c-ramp-5)', 'var(--c-ramp-5)'];
  let mixKey = '';
  /* protocols.mix (L1-L7, D6, G33): the runtime's counts, sorted and with the tail merged into
     其他 (protoMix); a protocol name is a back-end key (data: text node, translate="no"); the
     total is entries only (R5) with the source (loaded / configured); none: 还没有节点 */
  function paintProto() {
    // (the key includes the language: the segment tips are sentences)
    const mix = protoMix(S.get('protocols')), key = JSON.stringify(mix.list) + '|' + T.i18n.lang;
    if (key === mixKey) return mix;
    mixKey = key;
    const total = mix.entries, ramp = i => RAMP[i] || RAMP[RAMP.length - 1];
    // sizes and colours via the CSSOM (no style attribute)
    R.pbar.replaceChildren(...mix.list.map((p, i) => {
      const seg = document.createElement('i');
      seg.dataset.i = String(i);
      seg.setAttribute('data-tip', p.other ? T.t('overview.proto.other_tip', { n: p.names.length, count: p.count, share: p.count / total })
        : T.t('overview.proto.tip', { name: p.name, count: p.count, share: p.count / total }));
      seg.style.flex = `${p.count} 1 0`; seg.style.background = ramp(i);
      return seg;
    }));
    R.pleg.replaceChildren(...mix.list.map((p, i) => {
      const li = document.createElement('li'), sw = span('sw'), name = span(null, p.other ? T.t('overview.proto.other') : p.name);
      li.className = 'prow'; li.dataset.i = String(i); sw.style.background = ramp(i);
      if (!p.other) name.setAttribute('translate', 'no');
      li.append(sw, name, span('pc', F.num(p.count)));
      return li;
    }));
    if (!mix.list.length && known('protocols.mix')) {
      // L6: no node yet -> a way to 层级
      const li = document.createElement('li'), go = document.createElement('button');
      li.className = 'prow is-empty'; go.type = 'button'; go.className = 'link-btn';
      go.textContent = T.t('overview.proto.empty');
      go.addEventListener('click', () => T.go('profiles', {}));
      li.append(go); R.pleg.append(li);
    }
    return mix;
  }
  function buildProto() {
    paintProto();
    const hl = i => { R.pbar.classList.toggle('hl', i != null); T.$$('i', R.pbar).forEach(s => s.classList.toggle('on', s.dataset.i === String(i))); T.$$('.prow', R.pleg).forEach(r => r.classList.toggle('on', r.dataset.i === String(i))); };
    [R.pbar, R.pleg].forEach(h => { h.addEventListener('pointerover', e => { const x = e.target.closest('[data-i]'); if (x) hl(x.dataset.i); }); h.addEventListener('pointerleave', () => hl(null)); });
  }
  let protoOn = null;
  function syncProto(animate) {
    const run = runNow(), on = run === 'running';
    const has = known('protocols.mix') && run !== 'unknown';
    const mix = has ? paintProto() : (R.pbar.replaceChildren(), R.pleg.replaceChildren(), mixKey = '', { entries: 0, list: [], source: null });
    R.proto.classList.toggle('is-idle', !on);
    let total;
    // starting: the Core has not loaded the configuration yet (L7) until the mix says loaded: a
    // dash until then (F-01: no 正在加载配置…). F-04: no total once it has (协议入口, entries
    // counted once per protocol, was an internal count; the legend gives each protocol's nodes)
    if (!has || (run === 'starting' && mix.source !== 'loaded')) total = T.t('overview.deck.stat.unknown');
    else if (stale('protocols.mix')) total = T.t('overview.common.stale');
    else total = '';
    M.swapText(R.protoTotal, total, { instant: !animate });
    if (protoOn === on) return;
    protoOn = on;
    // going live the segments brighten one after another as the wave passes (F-06: the idle bar is
    // already there, so nothing regrows from empty)
    if (on && animate && visible && !M.reduced) {
      const wd = M.wave.delay(R.pbar);
      T.$$('i', R.pbar).forEach((s, i) => s.animate([{ opacity: 0.25 }, { opacity: 1 }], { duration: 420, delay: wd + 40 + i * 90, easing: M.ease.out, fill: 'backwards' }));
    }
  }

  /* ============================================================ wiring */
  function syncAll(animate) {
    syncHero(animate); syncStrip(animate); syncExit(animate); syncDeck(animate); syncTraffic(animate); syncProto(animate);
  }
  /* the page commands (T.rt.call; mapping_overview B, C, D): every outcome comes back through
     the topics; a refusal (ack accepted=false, lifecycle_busy, …) is a short note in the hero,
     except the start's: the shell's command-error banner says it (doStart, P5 #27) */
  const ignoredNote = err => !err || ignorable(err);
  // error codes of a command the sidecar refused before running it (PROTOCOL 7)
  const REFUSALS = ['lifecycle_busy', 'conflict', 'not_allowed', 'validation', 'runtime_unavailable', 'not_running'];
  const refusal = err => (err && err.skipped_reason ? T.rt.reasonText('common.skipped', err.skipped_reason) : errText(err));
  function doStart() {
    if (!can('service.start')) return;
    stopFail = null; note('run', null);
    // through the shell's command entry (T.shellc.run, P5 #27): a start failure brings the startup
    // banner back even when the user closed one with the same identity (the user tried again),
    // any other error is its one command-error banner, so the hero adds no note of its own
    if (T.shellc && typeof T.shellc.run === 'function') { T.shellc.run('service.start', {}).catch(() => {}); return; }
    T.rt.call('service.start', {}).catch(err => {
      // a start that ran and failed is service.state failed (the hero shows its failure)
      if (ignoredNote(err) || runNow() === 'failed') return;
      note('run', refusal(err));
    });
  }
  function doStop() {
    if (!can('service.stop')) return;
    const ss = raw('service.state'), g = ss && ss.intent ? ss.intent.runtime_generation : null;
    stopFail = null; note('run', null);
    renderRun(null, onScreen()); syncHero(onScreen());
    T.rt.call('service.stop', Number.isInteger(g) ? { expected_generation: g } : {}).catch(err => {
      if (ignoredNote(err)) return;
      // refused before it ran (ack skipped_reason; busy, conflict, not allowed): a note; a stop
      // that ran and failed (timeout, still stopping, cleanup pending, non-zero exit):
      // 停止失败 · 重试 (G05, S-R4, S-R5)
      const refused = !!err.skipped_reason || REFUSALS.includes(err.code);
      const ran = !refused;
      if (!ran) note('run', refusal(err));
      else { stopFail = { error: err }; renderRun(null, onScreen()); syncAll(onScreen()); }
    });
  }
  function doRestart() {
    if (runNow() !== 'running' || !can('service.restart')) return;
    note('run', null);
    relabelAfter(R.restart, 'overview.restart.label', 1300,
      T.ui.busy(R.restart, {
        run: () => T.rt.call('service.restart', {}).catch(err => { if (!ignoredNote(err)) note('run', errText(err)); throw err; }),
        busy: T.t('overview.restart.busy'), done: T.t('overview.restart.done'), fail: T.t('overview.restart.failed'),
        icon: 'restart', busyIcon: 'restart', doneIcon: 'check', hold: 1300,
      }));
  }
  function doVerify() {
    const run = runNow();
    if ((run !== 'running' && run !== 'starting') || !can('exit.verify')) return;
    // the card says how it ended (D5: a failure is shown in the card, not on the button)
    relabelAfter(R.refresh, 'overview.exit.refresh.label', 1000,
      T.ui.busy(R.refresh, {
        run: () => T.rt.call('exit.verify', { trigger: 'manual' }).then(r => { if (r && r.request_id) ownExit.add(r.request_id); return r; },
          err => { if (err && err.result && err.result.request_id) ownExit.add(err.result.request_id); throw err; }),
        busy: T.t('overview.exit.refresh.busy'), done: T.t('overview.exit.refresh.done'), fail: false,
        icon: 'refresh', busyIcon: 'refresh', doneIcon: 'check', hold: 1000,
      }));
  }
  function wire(life) {
    R.main.addEventListener('click', () => doStart());
    R.act.addEventListener('click', e => {
      // see pill.at; e.timeStamp is when the click happened, even if the main thread was busy then
      if (e.detail > 0 && (pill.mode === 'ready' || e.timeStamp - pill.at < 400)) return;
      if (pill.mode === 'failed') doStart(); else doStop();
    });
    R.restart.addEventListener('click', () => doRestart());
    R.refresh.addEventListener('click', () => doVerify());
    ['#ov-copy4', '#ov-copy6'].forEach(sel => relabelAfterCopy(R.root.querySelector(sel)));
    T.ui.copy(R.copy4, () => shownAddr.v4 || '');
    T.ui.copy(R.copy6, () => shownAddr.v6 || '');
    // a toggle pressed: the mode it leads to (modeAfterSwitch), confirmed inline while running
    R.modeSeg.addEventListener('click', e => {
      const b = e.target.closest('.seg-btn'); if (!b || !R.modeSeg.contains(b)) return;
      if (S.get('modeBusy') || !can('mode.set') || !modeKnownNow() || !S.get('mode')) return;
      const k = b.dataset.v, v = modeAfterSwitch(S.get('mode'), k);
      if (gated(v)) { closeConfirm(true); explainGate(v); return; }
      if (runNow() === 'running') { openConfirm(v, k); return; }
      closeConfirm(true); paintToggles(v); setMode(v);
    });
    rangeSeg = T.ui.seg(R.range, { value: '24h', onChange: v => { chart.setRange(v); } });
    R.ks.addEventListener('click', () => T.go('settings', { focus: 'killswitch' }));
    R.pool.addEventListener('click', () => T.go('profiles', {}));
    buildDeck(); buildProto();
    loadHistory();
    chart = makeChart(R.chartBox);
    // Esc closes the inline confirmation wherever focus is (overlays and menus handle Esc first)
    life.listen(document, 'keydown', e => {
      if (e.key !== 'Escape' || !confirmEl || !visible || e.defaultPrevented || T.overlayOpen() || T.cmd.isOpen) return;
      const inside = confirmEl.contains(document.activeElement), k = confirmEl._k;
      closeConfirm(true); if (inside) focusMode(k);
    });

    // (reading vis() before a sync writes anything takes the blocks' places for this frame's glide)
    const vis = () => { const v = onScreen(); if (v) glidePre(); return v; };
    // where the start under way began: a real start (stopped, failed …) ends with the wave; a
    // running → starting → running blip (the runtime checking again after a node change) does not
    let startFrom = null;
    life.on('rt:state', ev => {
      const animate = !ev.instant && vis();
      if (ev.instant) closeConfirm(false);
      if (ev.run === 'starting' && ev.prev !== 'starting') startFrom = ev.prev || null;
      // the start is over: the page lights up outward from the pill that was pressed (F-06)
      if (animate && ev.run === 'running' && ev.prev === 'starting' && startFrom !== 'running') {
        const r = R.run.getBoundingClientRect();
        M.wave.start({ x: r.left + r.width / 2, y: r.top + r.height / 2 });
      }
      if (ev.run !== 'running') { closeConfirm(animate); pendingMode = null; }
      if (ev.run === 'stopped' || ev.run === 'starting') stopFail = null;
      // 下次启动生效 is over once the service starts (the start applies the saved mode)
      if (modeNote && modeNote.kind === 'next_start' && ev.run !== 'stopped') { clearTimeout(modeNoteT); modeNote = null; }
      if (ev.run === 'running') takeUptime();
      renderRun(ev, animate);
      syncAll(animate);
      if (ev.run === 'stopped' || ev.instant) layoutDeck();
    });
    life.on('rt:phase', () => { renderPhase(vis()); syncHero(vis()); syncDeck(vis()); syncStrip(vis()); syncProto(vis()); syncTraffic(vis()); });
    life.on('rt:collect', () => { syncDeck(vis()); syncStrip(vis()); });
    life.sub('order', (v, prev) => reorderDeck(prev, vis()));
    // cancelling, a failure, the Core's cleanup: the pill's face changes without a run edge
    ['cancelling', 'core', 'failure', 'readiness'].forEach(k => life.sub(k, () => { renderRun(null, vis()); syncHero(vis()); syncDeck(vis()); syncExit(vis()); }));
    life.on('rt:tiers', () => { syncHero(vis()); syncDeck(vis()); syncStrip(vis()); syncExit(vis()); });
    life.on('rt:tier', () => { syncHero(vis()); syncDeck(vis()); syncStrip(vis()); syncExit(vis()); });
    life.on('rt:route', () => { syncHero(vis()); syncDeck(vis()); syncExit(vis()); });
    life.on('rt:gateway', () => { syncDeck(vis()); syncExit(vis()); });
    let lastExit = null;
    life.on('rt:exit', e => {
      const was = lastExit;
      const cur = e.exit && e.exit.current;
      lastExit = cur ? cur.status : null;
      syncHero(vis()); syncExit(vis());
      if (!e.instant && cur && cur.status === 'ok' && was !== 'ok') flashExit();
    });
    life.on('rt:restart', () => syncHero(vis()));
    life.on('rt:mode', e => { pendingMode = e.busy ? e.mode : null; syncStrip(vis()); });
    life.on('rt:killswitch', () => { syncStrip(vis()); syncHero(vis()); syncDeck(vis()); });
    life.on('rt:bastion', () => { syncDeck(vis()); syncStrip(vis()); });
    life.on('rt:protocols', () => syncProto(vis()));
    life.on('rt:stale', () => syncAll(vis()));
    life.on('rt:history', () => { loadHistory(); if (chart) chart.reload(); });
    life.on('rt:config', () => fetchInfo(true));
    life.on('rt:caps', () => { fetchInfo(false); syncAll(false); });
    // rt:reset: a lost generation drops what the page knew of it (the projection already shows
    // run unknown: 正在重新连接运行时); a same-epoch reconnect changes nothing on screen (B9)
    life.on('rt:reset', e => {
      if (!e.generation_changed) return;
      lostGen = true; stopFail = null; listen = null; autoExit = null; infoEpoch = null; ownExit.clear();
      clearTimeout(runNoteT); runNote = null; clearTimeout(modeNoteT); modeNote = null; pendingMode = null;
      closeConfirm(false); loadHistory(); if (chart) chart.reload();
      renderRun(null, false); syncAll(false);
    });
    life.sub('killswitch', () => syncStrip(vis()));
    life.sub('ksBlocked', () => { syncStrip(vis()); syncHero(vis()); });
    life.sub('ksEnabled', () => { syncStrip(vis()); syncHero(vis()); syncDeck(vis()); });
    life.sub('admin', () => { if (confirmEl && gated(confirmEl._v)) closeConfirm(vis()); syncStrip(vis()); });
    // Bastion's pool (filling, drifting, switched on or off from 层级 or 配置) and the counts it feeds
    life.sub('bastion', () => { if (vis()) { syncDeck(true); syncStrip(true); } });
    life.sub('uptime', () => { takeUptime(); tickUptime(); });
    // a language switch: T.ti-bound words are already relabelled; the rest is recomputed here
    life.on('i18n:change', relocalize);
    life.sub('tiers', () => { if (vis()) syncHero(true); });            // the hero's 可用 count
    life.sub('protocols', () => { if (vis()) syncProto(true); });
    life.sub('presence', () => { if (onScreen()) { syncAll(false); tickUptime(); } });
    // the nodes of each tier (the carrier's current node line, a down tier's reason)
    ['vanguard', 'sentinel', 'bastion', 'gateway'].forEach(k => { if (T.rt && T.rt.topic) life.add(T.rt.topic('nodes.list', k).sub(() => { if (vis()) { syncDeck(true); syncExit(false); syncHero(false); } })); });
    life.on('rt:tick', e => {
      // the 24 h buckets follow the ticks (H3), on screen or not
      if (e && e.running && e.sample) foldTick(HIST.h24, e.sample, BUCKET_MS, H24_SLOTS);
      if (!vis()) return;                      // hidden: nothing is written; enter() resyncs
      syncTraffic(true);
      if (chart) chart.onTick();
    });
    renderRun(null, false);
    layoutDeck();
    fetchInfo(false);
  }

  /* everything on the page that is not bound with T.ti (architecture §3.7 item 3): the words
     beside icons, the busy buttons' labels, the card names, the open confirmation's sentence,
     the pill rows, the chart, then every sync function once (instant) */
  let rangeSeg = null;
  function relocalize() {
    texts.forEach(([n, id, params]) => { n.data = T.t(id, params); });
    labs.forEach(([btn, id]) => setLab(btn, id));
    syncRestartName(); syncRefreshName();
    deckAria();
    liveRates();
    if (confirmEl && confirmEl._msg) confirmEl._msg.data = T.t('mode.switch.confirm.body', { mode: confirmEl._v });
    if (pill.mode === 'starting') {
      // in place: the phase label may carry a roll (renderPhase), which a fresh row would drop
      const ph = phaseNow();
      [R.base, R.over].forEach(l => {
        const row = l.querySelector('.run-row:not(.is-out)'); if (!row) return;
        setText(row.querySelector('[data-ph]'), phaseLabel(ph));
        setText(row.querySelector('.run-seg'), T.t('overview.run.cancel'));
        row._key = rowKey('starting');
      });
    } else if (pill.mode) { swapRow(R.base, rowEl(pill.mode), false); swapRow(R.over, rowEl(pill.mode), false); }
    if (chart) chart.relabel();
    syncAll(false);
    // the labels changed width: the thumb follows
    if (rangeSeg) rangeSeg.place(false);
  }

  // (the page's name is the shell's: shell.page.overview.title)
  T.page('overview', {
    render(root, ctx) { build(root); wire(ctx.life); syncAll(false); },
    enter(ctx) {
      visible = true;
      layoutDeck();
      syncAll(false); tickUptime();
      // a repeat visit (ctx.again: 总览 clicked while shown, Ctrl 1, a palette command; the
      // sidebar has no status card, G60) keeps what is on screen: no chart replay, no second listener
      if (!ctx.again) requestAnimationFrame(() => { if (visible) { chart.show(); layoutDeck(); } });
      ctx.scope.listen(window, 'resize', T.util.raf(layoutDeck));
      // (T-P4-09: T.util.fontsReady, not document.fonts.ready, which lays out the document when read)
      if (!ctx.again) T.util.fontsReady().then(() => { if (visible) { chart.layout(); layoutDeck(); } });
      // the 1 Hz walk of the page (the uptime between service.state snapshots, the exit badge's
      // 「N 分钟前」): a scope timer, so it ends when the page is left. T-P4-06's scope pause
      // (T.scopes.pause while the window is hidden) already stops it; onScreen() stays as a second,
      // free guard (store presence, document.hidden; 第八波 05a-§12)
      ctx.scope.every(1000, () => {
        if (!onScreen()) return;
        tickUptime();
        const v = raw('exit.identity');
        if (v && v.current && (v.current.status === 'ok' || v.current.status === 'partial')) syncExit(false);
      });
    },
    leave() { visible = false; chart.hide(); },
  });
})();
