/* ==========================================================================
   pages/settings.js — 配置
   A long page that scrolls in the host, with a sticky section index (scroll
   spy) on the left and seven sections on the right: the core's own settings, then
   Tesseract's, then the app's (11-04, .build/配置可视化整理方案_20261104.md):
     连接 · 分流 · DNS · 节点 · 安全 · 提醒与通知 · 应用
   Tesseract's fixed mechanisms (its own routing rules, the core's runtime keys) are not
   shown, and nothing sends the user to a file.
   ONE save model: every edit is a draft (a neutral dot in the gutter, an
   effect tag on the row, a dot on the section index). The floating save bar
   states the heaviest pending effect in the user's terms and morphs into the
   result. The one exception is 接管方式, two switches over the one saved mode (系统代理 ·
   虚拟网卡; the same live control as on 总览): it switches at once (mode.set), after an
   in-page confirm while the service runs.
   Wording (T-P4-04e): every on-screen word is a message — settings.* (this page) and
   alerts.* (the 提醒 section). The nodes that render data (rules, addresses, sources, node
   names, versions) carry translate="no". relocalize:'rerender': a language switch re-renders
   the page with snapshotState() / restoreState() keeping the drafts, the typed add fields and
   the scroll.
   Real data (T-P4-05d; mapping_settings §0–§2, PROTOCOL §10–§16):
     every enter reads config.snapshot (the base of every row; B-27's fallback) and merges it
     into the rows without a draft; read_errors show a bar with 打开配置目录 (no path, G48); a
     field the snapshot does not carry leaves its row read-only (never saved over a value the
     page could not read); a list the projection withheld whole says so and asks before it is
     replaced. The draft keys become ConfigChange[] in pages/settings/changes.js (one place for
     the rule literals and the source / DNS / port conversions); settings.apply's job ends in one
     of the save bar's results (已生效 / 已保存待应用 / 已保存但没有生效 / 下次启动生效 / 还不能
     保存 at the field / 保存失败 / 配置已在别处修改) and every saved row carries its own label
     (已生效 / 待生效 / 未生效). SaveResult.rev is the new base_rev; config.changed{rev} not
     newer than it is ignored, a newer one (another write) re-reads the snapshot; a PendingApply
     follows config.changed.pending. The language row saves ui.language and waits for the
     shell's host:language to re-render every page (3 s; then it says to reopen the window).
     Runtime facts: network.mode, killswitch.state, bastion.pool, nodes.list (latency preview),
     bastion.sources, update.state, core.info, geo.status, autostart.state, app.info.
     The 提醒 section (10-30 redesign, .build/提醒模块重设计_20261030.md): notices are local only;
     five switches under alerts.notify are ordinary draft keys (changes.js), plus 预览提醒
     (alerts.test) and 查看提醒记录 (the logs page, alert category).
   ========================================================================== */
import * as C from './settings/changes.js';
import { MODE_SWITCHES as SW_MODES, modeSwitchOn as swOn, modeAfterSwitch as nextMode } from '../data/static.js';

(function () {
  'use strict';
  const T = window.T, D = T.data, S = T.store, M = T.motion, F = T.fmt;
  const I = (n, s, c) => T.icon(n, s || 16, c);
  const icon = (n, s, c) => T.iconEl(n, s || 16, c);
  /** a message as plain text */
  const t = (id, p) => T.t(id, p);
  /* tier tags: the code name of the static metadata (F-03: no role word) */
  const tierEl = (id, o) => {
    const n = T.html`${T.ui.tierTag(id, Object.assign({ icon: false }, o))}`;
    return (n && n.nodeType === 1 ? n : n && n.firstElementChild) || document.createTextNode(D.tier(id).name);
  };
  /* <x>…</x> in a message wraps a data value (a name, an address): it is not translated */
  const dataNode = frag => { const s = document.createElement('span'); s.setAttribute('translate', 'no'); s.append(frag); return s; };
  /** a rich message (whitelisted tags; slots: <x> data value, <tier>{id}</tier> a tier tag whose
      parameter is the tier id, <link>{id}</link> the same tag as a link to the tier's page) as a DOM
      fragment: parameters are text nodes, nothing is parsed as HTML */
  const SLOTS = {
    x: dataNode,
    tier: frag => tierEl(frag.textContent.trim()),
    link: frag => tierEl(frag.textContent.trim(), { link: true }),
  };
  const tx = (id, p, slots) => T.tx(id, p, slots ? Object.assign({}, SLOTS, slots) : SLOTS);
  /** an element: T.h with children (text, nodes) */
  const el = (tag, attrs, ...kids) => T.h(tag, attrs, ...kids);
  /** a data value under translate="no" */
  const datum = (text, cls) => el('span', { class: cls || null, translate: 'no' }, String(text == null ? '' : text));
  const modeLabel = v => t(`mode.${v}.label`);
  /** a tier in running text: its code name (F-03: no role word) */
  const tierLbl = id => D.tier(id).name;
  const clone = C.clone, sig = C.sig, same = C.same;
  const wait = ms => new Promise(r => setTimeout(r, ms));
  const isShown = e => !!(e && e.isConnected && e.getClientRects().length);
  const run = () => S.get('run');
  const isLive = () => run() === 'running';
  let seq = 100;
  const uid = p => `${p}${++seq}`;
  function dayTime(ts) {
    const d = new Date(ts), n = new Date(), y = new Date(n.getTime() - 86400000);
    if (d.toDateString() === n.toDateString()) return t('settings.time.today', { time: F.hm(ts) });
    if (d.toDateString() === y.toDateString()) return t('settings.time.yesterday', { time: F.hm(ts) });
    return t('settings.time.days_ago', { n: Math.max(2, Math.round((n - d) / 86400000)) });
  }
  /* settings.time.* are written in their in-sentence form (「checked today 10:00」); where a
     dayTime stands on its own (a delivery record's time, a Geo status) the whole text shown gets
     its first letter capitalised in the current language (<html lang> is its BCP 47 tag, t.js).
     A first character without case (CJK, a digit) is left as it is (T-P5-04). */
  const capFirst = s => String(s).replace(/^\p{L}/u, c => c.toLocaleUpperCase(document.documentElement.lang || undefined));
  /** an rt error in words (message_key → errors.<code> → errors.unknown) */
  const errText = e => (T.rt && e ? T.rt.errorText(e) : (e && e.message) || '');
  const can = cmd => !!(T.rt && T.rt.can(cmd));
  const quiet = p => { p.catch(() => {}); return p; };

  /** swapNode(el, node, o): M.swapText's rolling swap for content with markup. The swap is keyed
      by o.key (default the node's text); the new layer gets the node instead of text, so nothing
      is parsed as HTML. */
  function swapNode(target, node, o) {
    o = o || {};
    if (!target) return;
    const key = o.key != null ? String(o.key) : `${node.textContent}\u0000${o.kind || ''}`;
    if (target._swap === key) return;
    M.swapText(target, key, { instant: o.instant || !visible, dir: o.dir });
    const box = target._swapBox || target;
    const layer = Array.from(box.children).filter(c => c.classList.contains('sw') && !c.hasAttribute('aria-hidden')).pop();
    if (layer) layer.replaceChildren(node);
  }
  /** a status dot + words (T.ui.status markup) as a node */
  const status = (k, label, o) => T.html`${T.ui.status(k, Object.assign({ label }, o))}`;

  let R = {};                 // element refs
  let visible = false;        // the page is shown (hidden pages never animate)

  /* ================================================================== data */
  /* section ids → settings.sec.<key>.* (the 提醒 section's words are alerts.notify.*). The core's
     own settings first (连接 · 分流 · DNS), then Tesseract's (节点 · 安全), then 提醒 and 应用
     (plan .build/配置可视化整理方案_20261104.md §3.4) */
  const SECS = [
    { id: 'set-conn', key: 'conn', icon: 'link' },
    { id: 'set-route', key: 'route', icon: 'route' },
    { id: 'set-dns', key: 'dns', icon: 'globe' },
    { id: 'set-nodes', key: 'nodes', icon: 'pool' },
    { id: 'set-safety', key: 'safety', icon: 'shield' },
    { id: 'set-alerts', key: 'alerts', icon: 'bell' },
    { id: 'set-app', key: 'app', icon: 'stack' },
  ];
  const secTitle = key => (key === 'alerts' ? t('alerts.notify.sec.title') : t(`settings.sec.${key}.title`));
  /* the TUN adapter (tun, and mixed = TUN + system proxy) needs administrator rights (D.modes[v].admin, static) */
  const needsAdmin = v => !!(D.modes[v] && D.modes[v].admin);
  /* 接管方式: two switches over the one saved mode (SW_MODES, swOn, nextMode: data/static.js) */
  /* the three actions share one neutral chip; glyph and word tell them apart.
     Words: settings.route.act.<a>.label (and direct.hint: a direct target sees the real IP). Rules are kept per action (the backend's
     three lists, compiled direct → proxy → reject, each top to bottom: G14) */
  const ACT = { direct: { icon: 'direct' }, proxy: { icon: 'route' }, block: { icon: 'ban' } };
  const ACTS = Object.keys(ACT);
  const actLabel = a => t(`settings.route.act.${a}.label`);
  /* rule prefixes: the kind word is settings.route.kind.<p> (ip-cidr → ip_cidr); the example is syntax */
  const PFX = {
    domain: { ex: 'domain:api.example.com' },
    suffix: { ex: 'suffix:example.com' },
    keyword: { ex: 'keyword:ads' },
    'ip-cidr': { ex: 'ip-cidr:203.0.113.0/24' },
    geosite: { ex: 'geosite:cn' },
    geoip: { ex: 'geoip:CN' },
    process: { ex: 'process:example.exe' },
    // 配置整理 P2: the kinds the 当 … 时 field picks (an example each; a written prefix still works)
    regex: { ex: 'regex:^ads?[0-9]*\\.example\\.com$' },
    'ip-asn': { ex: 'ip-asn:AS13335' },
    'src-ip-cidr': { ex: 'src-ip-cidr:192.0.2.0/24' },
    'dst-port': { ex: 'dst-port:443' },
    'src-port': { ex: 'src-port:5000' },
    'process-path': { ex: 'process-path:C:\\Program Files\\Example\\example.exe' },
    network: { ex: 'network:udp' },
    'rule-set': { ex: 'rule-set:ads' },
  };
  const kindWord = p => t(`settings.route.kind.${p.replace(/-/g, '_')}`);
  /* the well-known Geo categories get a word (settings.route.geo.<key>) */
  const GEO_SUB = { 'geosite:cn': 'geosite_cn', 'geosite:category-ads-all': 'geosite_ads', 'geosite:private': 'private', 'geoip:cn': 'geoip_cn', 'geoip:private': 'private' };
  /* resolver kinds: settings.net.res.<t>.{label,type,hint,role}; the placeholder is an example address */
  const RES_T = {
    main: { ph: 'https://dns.example.net/dns-query' },
    node: { ph: 'tls://dot.example.net' },
    boot: { ph: '192.0.2.53' },
  };
  /* public source kinds: settings.bastion.src.<t>.{label,hint} (local files: shown, never added here) */
  const SRC_T = {
    sub: { ph: 'https://nodes.example.net/share.txt' },
    gh: { ph: 'example-user/free-nodes' },
    tg: { ph: '@example_share' },
  };
  const srcLabel = k => t(`settings.bastion.src.${k}.label`);
  /* (T-P5-13) a kind chip of the resolver and source lists is as wide as the widest kind word of the
     language: the other kinds' words sit under the shown one, invisible (.set-act-z, as the rule
     action chips of T-P5-11), so the kind column lines up from row to row and no word is cut (de
     Allgemein, ru Репозиторий GitHub) */
  const kindChip = (cls, shown, all) => el('span', { class: `chip sm ${cls}` },
    el('span', { class: 'set-act-g' }, el('span', {}, shown), ...all.filter(x => x !== shown).map(x => el('span', { class: 'set-act-z', 'aria-hidden': 'true' }, x))));
  const srcChip = k => kindChip('set-src-t', srcLabel(k), ['sub', 'gh', 'tg', 'file'].map(srcLabel));
  /* DNS modes: settings.net.dns.mode.<value>.{label,desc} */
  const DNS_MODES = ['auto', 'enc', 'system', 'custom'];
  /* the language list: each language by its own name (settings.app.lang.name.<code in lower case>) */
  const LANGS = ['zh_CN', 'en_US', 'ja_JP', 'ko_KR', 'fr_FR', 'de_DE', 'es_ES', 'pt_BR', 'ru_RU'];
  /* the latency ceiling's default (latency_policy_view: the policy's default ceiling) */
  const LAT_DEF = 800;
  /* the releases page (app_info.APP_RELEASES_URL; host.openUrl allows github.com only) */
  const RELEASES_URL = 'https://github.com/definitlynotme/Tessract/releases';
  /* Geo data kinds, in the updater's order (Geo.items[].kind): settings.app.geo.kind.<kind> */
  const GEO_KINDS = ['geosite', 'geoip', 'mmdb', 'asn'];
  const GEO_NAME = { geosite: 'GeoSite', geoip: 'GeoIP', mmdb: 'MMDB', asn: 'ASN' };
  const GEO_STALE_MS = 36 * 3600000;


  /* ================================================================ model
     base = saved values (config.snapshot) · draft = what the controls show. K[key] describes a
     setting: section, effect, its row, paint(v) (push a value into the controls), commit(v) (side
     effects once saved) and lock() (the element made read-only while the value is unknown).
     known[key]: the snapshot carried the field; hidden[key]: field ids the projection withheld
     whole (secret_fields_set), replaced only after the user confirms (replaced[key]). */
  const DEFAULTS = {
    ks: false, ksLan: false, ksDns: false, ksDnsList: [], ksExtra: [], autoExit: false,
    rules: [], ruleSets: [], bypass: [], lan: false, ipv6: false, portMixed: '', portSub: '',
    dnsMode: 'auto', dnsRules: false, dnsList: [], egress: false, egressRetry: false, latency: LAT_DEF,
    bastionOn: false, bastionMin: '', sources: [], lang: 'zh_CN', autostart: false, tray: false,
    nOn: true, nConn: true, nTier: true, nUpd: true, nSound: true,
  };
  const base = clone(DEFAULTS);
  const draft = clone(base);
  const known = {};            // contract keys: false until the snapshot carries them
  const hidden = {};           // key → [withheld field ids]
  const replaced = {};         // key → true: the user agreed to replace a list with hidden entries
  const K = {};
  const effOf = k => (K[k] && K[k].eff ? (typeof K[k].eff === 'function' ? K[k].eff() : K[k].eff) : C.effectOf(k, draft));
  const isContract = k => Object.prototype.hasOwnProperty.call(C.SECTION, k);
  const editable = k => !isContract(k) || known[k] === true;
  const changedKeys = () => Object.keys(K).filter(k => editable(k) && !same(draft[k], base[k]));
  function setD(k, v) { draft[k] = clone(v); if (rowRes.has(k) && rowRes.get(k).state !== 'pending') { rowRes.delete(k); } track(); remoteCheck(k); }

  /* ---- the snapshot (config.snapshot): its state, rev and read errors ---- */
  /* rev: the config revision of the base; epoch: the runtime epoch it was read in (the sidecar
     keeps the revision in memory, a new process counts from 0 again) */
  const snap = { state: 'loading', rev: null, epoch: null, readErrors: [], pending: null, error: null, ever: false };
  let reqSnap = 0;             // the newest snapshot request (an older answer is dropped)
  let saving = null;           // the running settings.apply, so a config.changed it causes is not foreign
  let lastSave = null;         // { keys, retry }: the rows of the last third result, for 重试应用
  let seenRev = null;          // the highest config.changed rev seen while a save ran (another write during it)
  let leftover = null;         // key → sig(draft): drafts a partial save left (the third result keeps the bar)
  /* each saved row's own result: key → { state: applied | pending | not_applied | next_start | failed | external, reason } */
  const rowRes = new Map();
  /* the rows a save left pending: { keys, ids: Set(intent_id), rev, idsOnly? } (C.pendingFollows: the
     ledger rebases an intent into a successor with a new id when the config rev moves on). idsOnly:
     set on entering the page, so the visit's first snapshot follows by intent id only (05d R4-3) */
  let pendTrack = null;
  /* the rows a save left pending before a generation change: { keys, read } (the new generation's
     snapshot read?). They settle on their own once that snapshot and the service state are known;
     a save after the change starts a new pendTrack instead of joining them (05d 续2 SHOULD 1) */
  let genTrack = null;
  let app = { version: null, listeners: [] };
  let auto = null;             // autostart.state
  let geo = null;              // Geo (geo.status)
  let geoDisabled = null;      // the error of a geo.update refused as disabled (conflict / disabled)
  let langWait = null;         // { lang, timer } after a language save, until host:language
  let langNote = null;         // a note on the language row: { kind: reopen | failed, reason }
  let modeNote = null;         // the last mode.set failure, shown on the mode status line

  /* text typed into an add field (or an open inline edit) is part of the draft: valid
     text marks its row and brings up the bar; saving adds it first, and text that
     can't be added holds the save with the reason at the field. o: { sec, eff, row(), at(),
     input(), text() → typed?, value() → normalized value when valid else '', commit() → added?,
     reset(), msg() → the hold's reason } */
  const PENDING = [];
  function pendingField(k, o) {
    base[k] = ''; draft[k] = '';
    K[k] = { sec: o.sec, eff: o.eff, row: o.row, paint: v => { if (!v) o.reset(); } };
    PENDING.push(Object.assign({ k }, o));
    return () => { const v = o.value() || ''; if (v !== draft[k]) setD(k, v); };
  }
  function flushPending() {
    for (const p of PENDING) {
      if (!p.text()) continue;
      // a list withheld whole is only replaced once the user agreed (onSave asks first, fix M1):
      // typed text never adds to it behind that question
      if (p.list && needsGuard(p.list)) { const f = p.input(); return { row: p.row(), at: p.at ? p.at() : f, focus: f, msg: t('settings.hidden.hold') }; }
      if (p.commit()) continue;
      const f = p.input();
      return { row: p.row(), at: p.at ? p.at() : f, focus: f, msg: p.msg() };
    }
    return null;
  }

  /* ================================================================ helpers (DOM) */
  const callout = (kind, title, msg, id, hide) => {
    const n = el('div', { class: `callout callout-${kind} sm`, id: id || null, role: 'note', hidden: !!hide },
      icon(kind === 'error' ? 'error' : kind === 'success' ? 'success' : kind, 16),
      el('div', { class: 'callout-body' }, title ? el('b', { class: 'callout-t' }, title) : null, el('span', null, msg)));
    return n;
  };
  const swEl = (k, label, sm) => el('button', { class: `switch${sm ? ' sm' : ''}`, type: 'button', role: 'switch', 'data-k': k, 'aria-checked': 'false', 'aria-label': label });
  /* a settings row: title line (title, live state), description, control, sub-block. desc null: no
     description line; '': a line the page fills only when there is something to say (a message, a
     state's reason), folded away until then (note) */
  const frow = (id, title, desc, ctl, o) => {
    o = o || {};
    const head = el('div', { class: 'frow-t' }, el('span', { class: 'set-t' }, title), o.live || null);
    const left = el('div', { class: 'frow-l' }, head, desc != null ? el('div', { class: `frow-d${desc === '' ? ' is-blank' : ''}`, id: o.did || null }, desc) : null);
    return el('div', { class: `frow${o.block ? ' is-block' : ''}${o.cls ? ' ' + o.cls : ''}`, id }, left, ctl ? el('div', { class: 'frow-c' }, ctl) : null, o.sub || null);
  };
  const numBox = (id, cls, label, mono) => el('div', { class: `input num sm set-num ${cls}${mono ? ' mono' : ''}` }, el('input', { id, 'aria-label': label, autocomplete: 'off' }));
  const live = id => el('span', { class: 'set-live', id });

  /* a message line (hint ↔ error) that rolls in place; errors carry the octagon. A line with
     nothing to say (most fields have no hint: F-01) is folded away (.is-blank), so no empty strip
     is left under a field or a row title */
  function note(msgEl, text, err, instant) {
    if (!msgEl) return;
    const node = err ? T.html`${I('error', 14)}${T.ui.tech(text)}` : text instanceof Node ? text : document.createTextNode(String(text == null ? '' : text));
    msgEl.classList.toggle('is-blank', !err && !node.textContent);
    const key = `${err ? 'e' : 'h'}:${node.textContent}`;
    swapNode(msgEl, node, { key, dir: err ? 1 : -1, instant: instant || !visible });
  }
  function fieldNote(field, text, err, instant) {
    field.classList.toggle('is-invalid', !!err);
    note(field.querySelector('.field-msg'), text, err, instant);
  }

  /* blocks that appear or leave: the rest of the page glides (transform FLIP),
     a leaving block fades out where it was (a ghost), an entering one rises in */
  function followers(e) {
    const out = [];
    for (let n = e; n && n !== R.main && n !== R.root; n = n.parentElement) {
      for (let s = n.nextElementSibling; s; s = s.nextElementSibling) if (!s.hidden && !s.classList.contains('set-ghost') && s.getClientRects().length) out.push(s);
    }
    return out;
  }
  function ghostOf(e) {
    const par = e.offsetParent || e.parentElement;
    const r = e.getBoundingClientRect(), pr = par.getBoundingClientRect();
    const g = e.cloneNode(true);
    g.removeAttribute('id'); T.$$('[id]', g).forEach(x => x.removeAttribute('id'));
    g.setAttribute('aria-hidden', 'true'); g.classList.add('set-ghost');
    Object.assign(g.style, { position: 'absolute', left: `${r.left - pr.left - par.clientLeft + par.scrollLeft}px`, top: `${r.top - pr.top - par.clientTop + par.scrollTop}px`, width: `${r.width}px`, margin: '0', pointerEvents: 'none' });
    par.append(g);
    return g;
  }
  /* the rest of the page glides from where it visibly was to its new place; one glide per element */
  const LAG = 70;               // a leaving copy is mostly gone before anything slides over its place
  let tocApi = null;
  function glide(fol, y0, lag) {
    fol.forEach(f => { if (f._glide) { f._glide.cancel(); f._glide = null; } });
    let moved = false;
    fol.forEach((f, i) => {
      if (!f.isConnected || f.hidden || !f.getClientRects().length) return;
      const dy = y0[i] - f.getBoundingClientRect().top;
      if (Math.abs(dy) > 0.5) { moved = true; f._glide = f.animate([{ transform: `translateY(${dy}px)` }, { transform: 'none' }], { duration: M.SP.soft.duration, delay: lag, easing: M.SP.soft.easing, fill: 'backwards' }); }
    });
    return moved;
  }
  /* shift(anchor, mutate): anything that changes the anchor's height glides what follows it */
  let shifting = 0, shiftLag = 0;
  function shift(anchor, mutate) {
    if (!visible || M.reduced || shifting || !isShown(anchor)) return mutate();
    const fol = followers(anchor), y0 = fol.map(f => f.getBoundingClientRect().top);
    shifting++; shiftLag = 0;
    let lag;
    try { lag = mutate(); } finally { shifting--; }
    glide(fol, y0, Math.max(+lag || 0, shiftLag));
    return lag;
  }
  /* a list that gains or loses rows: T.motion.list moves the rows inside it and shift() the page below */
  const liveKeys = box => Array.from(box.children).filter(x => !x._ghost && x.dataset && x.dataset.key).map(x => x.dataset.key);
  const FRAMED = '.set-rl, .set-chlist, .set-recent';
  function frameFade(box, dh, lag) {
    if (box._ff) box._ff.forEach(a => a.cancel());
    box.style.setProperty('--set-dh', `${dh.toFixed(1)}px`);
    const o = { duration: 240, delay: lag, easing: M.ease.inout, fill: 'backwards' };
    const out = box.animate([{ opacity: 1 }, { opacity: 0 }], Object.assign({ pseudoElement: '::after' }, o));
    const inn = box.animate([{ opacity: 0 }, { opacity: 1 }], Object.assign({ pseudoElement: '::before' }, o));
    box._ff = [out, inn];
    M.onDone(out, () => { if (box._ff && box._ff[0] === out) { box._ff = null; box.style.removeProperty('--set-dh'); } }, 700);
  }
  function listFx(box, mutate, o) {
    o = o || {};
    if (!visible || !isShown(box)) { if (o.before) o.before(); mutate(); if (o.after) o.after(); return; }
    shift(box, () => {
      if (o.before) o.before();
      const k0 = liveKeys(box), h0 = box.offsetHeight;
      M.list(box, mutate, o);
      const k1 = new Set(liveKeys(box)), removed = k0.some(k => !k1.has(k));
      const lag = removed ? LAG : 0, h1 = box.offsetHeight;
      if (!M.reduced && Math.abs(h1 - h0) > 0.5 && box.matches(FRAMED)) frameFade(box, h0 - h1, lag);
      if (o.after) o.after();
      return lag;
    });
  }
  function reveal(changes, animate) {
    changes = changes.filter(([e, show]) => e && e.hidden === !!show);
    if (!changes.length) return;
    changes.sort((a, b) => (a[0].compareDocumentPosition(b[0]) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1));
    const els = changes.map(c => c[0]);
    const go = animate !== false && visible && !M.reduced && isShown(els[0].parentElement);
    if (!go) { changes.forEach(([e, show]) => { e.hidden = !show; }); return; }
    const fol = shifting ? [] : followers(els[0]).filter(f => !els.some(e => e === f || e.contains(f)));
    const y0 = fol.map(f => f.getBoundingClientRect().top);
    const ghosts = changes.filter(([e, show]) => !show && isShown(e)).map(([e]) => ghostOf(e));
    changes.forEach(([e, show]) => { e.hidden = !show; });
    const lag = ghosts.length ? LAG : 0;
    if (shifting) shiftLag = Math.max(shiftLag, lag); else glide(fol, y0, lag);
    changes.filter(c => c[1]).forEach(([e], i) => M.anim(e, [{ opacity: 0, transform: 'translateY(-4px)' }, { opacity: 1, transform: 'none' }], { duration: 220, delay: 60 + i * 30, easing: M.ease.out, fill: 'backwards' }));
    ghosts.forEach(g => M.onDone(g.animate([{ opacity: 1, transform: 'none' }, { opacity: 0, transform: 'scale(.985)' }], { duration: 120, easing: M.ease.in, fill: 'forwards' }), () => g.remove(), 400));
  }
  /* focus moves on when the control that had it leaves (a deleted row, an undone tag) */
  const focusNow = e => { if (e && e.isConnected) e.focus({ preventScroll: true }); };
  const ROW_CTL = '.frow-c .switch, .frow-c input, .frow-c .select, .set-tok-in, .set-add-in input, [role="slider"], .set-rule .grip, .set-chrow, .set-ch, .set-mode .opt input:checked';

  /* ---- the first entry without a long task (T-P4-09; P4 architecture §10, mapping_settings S-24) ----
     render() still builds and wires every section (the model, the save bar and the palette need them
     all), but only the sections that fill the first window take part in the first frame's style and
     layout: the others are hidden and shown again one per idle period afterwards
     (requestIdleCallback; showing one is an attribute write, its style and layout is one short
     frame). A hidden section costs nothing when the snapshot's paint reads layout in it (no box), and
     its components place themselves when it is shown (their ResizeObservers, as on a page shown
     later). Whatever scrolls to, focuses or measures a section shows it and every section before it
     first (showUpTo): the index (a capture listener ahead of T.ui.toc's own), glideTo (focusTo,
     showProblem), focusTo and restoreState (the scroll position and the typed text). A section shown
     late is below the window: nothing on screen moves.
     (content-visibility: auto was tried first: the snapshot's layout reads in the skipped sections
     force their layout at once, which brought the long task back, 73 ms against 56 ms) */
  const SEC_MIN = 750;          // a window this high is filled by the first section (连接: 729 px + its gap)
  const TOPBAR = 52;            // the shell's top bar above the page host
  let later = [], laterH = 0;
  const ric = fn => (window.requestIdleCallback ? requestIdleCallback(fn, { timeout: 500 }) : setTimeout(fn, 16));
  const cric = h => (window.cancelIdleCallback ? cancelIdleCallback(h) : clearTimeout(h));
  function deferSections() {
    if (laterH) cric(laterH);
    laterH = 0;
    // one section per SEC_MIN px of window: the window's height less the top bar (innerHeight needs
    // no layout; a language re-render runs in a page that is shown, where a layout read would lay
    // out the whole relabelled document at once)
    const all = T.$$('.fsec', R.main);
    const lead = Math.max(1, Math.ceil((window.innerHeight - TOPBAR) / SEC_MIN));
    all.forEach((s, i) => { s.hidden = i >= lead; });
    later = all.slice(lead);
  }
  function showSoon() { if (!laterH && later.length) laterH = ric(showNext); }
  function show(s) { s.hidden = false; const fns = shownFns.get(s); if (fns) { shownFns.delete(s); fns.forEach(fn => fn()); } }
  function showNext() { laterH = 0; const s = later.shift(); if (s) show(s); showSoon(); }
  function showUpTo(e) {
    const sec = e && e.closest ? e.closest('.fsec') : null;
    if (!sec || !later.includes(sec)) return;
    while (later.length) { const s = later.shift(); show(s); if (s === sec) break; }
    if (!later.length && laterH) { cric(laterH); laterH = 0; }
  }
  /* fn() once el's section is shown: work that measures a control at wiring time while the page is
     shown (a language re-render) waits for its section, as it would have found it laid out. While
     the page itself is hidden (a first render) it runs at once, as it always did */
  const shownFns = new WeakMap();
  function whenShown(el, fn) {
    const sec = el && el.closest ? el.closest('.fsec') : null;
    // (the router's re-render calls leave() first: the page's own element says whether it is shown)
    if (!R.root || R.root.hidden || !sec || !later.includes(sec)) { fn(); return; }
    const fns = shownFns.get(sec) || []; fns.push(fn); shownFns.set(sec, fns);
  }

  /* glide the host to an element; a short wash says "here" */
  const host = () => T.$('#host');
  function glideTo(e, off) {
    showUpTo(e);
    const h = host(); if (!h || !e) return 0;
    const y = Math.max(0, h.scrollTop + e.getBoundingClientRect().top - h.getBoundingClientRect().top - (off == null ? 24 : off));
    const dist = Math.abs(y - h.scrollTop);
    if (dist > 2) h.scrollTo({ top: y, behavior: M.reduced ? 'auto' : 'smooth' });
    return dist;
  }
  function afterScroll(dist, fn) {
    if (dist <= 2 || M.reduced) { setTimeout(fn, 40); return; }
    const h = host(); let done = false;
    const go = () => { if (done) return; done = true; h.removeEventListener('scrollend', go); fn(); };
    h.addEventListener('scrollend', go); setTimeout(go, 900);
  }
  function wash(e) {
    if (!e) return;
    e.classList.add('set-hit');
    const a = e.animate([{ opacity: 0 }, { opacity: 1, offset: 0.12 }, { opacity: 1, offset: 0.55 }, { opacity: 0 }], { duration: 1700, easing: 'linear', pseudoElement: '::after' });
    M.onDone(a, () => e.classList.remove('set-hit'), 2100);
  }

  /* ================================================================ validators
     Quick local hints (settings.*.err.*); the backend decides (settings.validate on change,
     settings.apply's hold at the field: G49). The ones that also have a short form for the save
     bar return { msg, short }. */
  const dupErr = v => t('settings.err.duplicate', { v });
  const V4 = /^(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(\.(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3}$/;
  const isV4 = s => V4.test(s);
  function isV6(s) {
    s = String(s).replace(/^\[|\]$/g, '');
    if (!/^[0-9a-f:]+$/i.test(s) || s.indexOf(':') < 0) return false;
    const dbl = s.split('::'); if (dbl.length > 2) return false;
    const segs = s.split(':').filter(x => x !== '');
    if (segs.some(x => x.length > 4)) return false;
    return dbl.length === 2 ? segs.length <= 7 : segs.length === 8;
  }
  const isIP = s => isV4(s) || isV6(s);
  function cidrOk(s) { const m = String(s).match(/^([^/]+)\/(\d{1,3})$/); if (!m) return false; const b = +m[2]; return isV4(m[1]) ? b <= 32 : isV6(m[1]) ? b <= 128 : false; }
  const isDomain = s => /^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$/i.test(s);
  function privV4(parts) { const [a, b] = parts; return a === 10 || a === 127 || (a === 172 && b >= 16 && b <= 31) || (a === 192 && b === 168) || (a === 169 && b === 254); }
  function privIP(s) {
    if (isV4(s)) return privV4(s.split('.').map(Number));
    const x = String(s).replace(/^\[|\]$/g, '').toLowerCase();
    return isV6(x) && (x === '::1' || /^fe[89ab]/.test(x) || /^f[cd]/.test(x));
  }
  /* the kill switch's keywords (killswitch.firewall_remote_token): typed in English or in the
     current language's words (settings.conn.ks.kw.*), shown in the current language */
  const KW_IDS = { LocalSubnet: 'local_subnet', DefaultGateway: 'default_gateway' };
  const kwShow = v => (KW_IDS[v] ? t(`settings.conn.ks.kw.${KW_IDS[v]}`) : v);
  /* T-P5-14: both sides NFKC, then lower case: half-width katakana (IME F8), full-width Latin
     (IME full-width mode) and NFD-composed accents (fr é as e + U+0301) all match the word */
  const kwFold = s => String(s).normalize('NFKC').toLowerCase();
  const ksKw = s => {
    const x = kwFold(s);
    const words = { localsubnet: 'LocalSubnet', dns: 'DNS', dhcp: 'DHCP', wins: 'WINS', defaultgateway: 'DefaultGateway' };
    Object.keys(KW_IDS).forEach(k => { words[kwFold(kwShow(k))] = k; });
    return words[x] || null;
  };
  const isKw = v => v === 'DNS' || v === 'DHCP' || v === 'WINS' || !!KW_IDS[v];
  const KS_EX = { ip: '203.0.113.5', cidr: '198.51.100.0/24', range: '203.0.113.1-203.0.113.9' };
  function ksExtraErr(v, list) {
    if (list.includes(v)) return dupErr(kwShow(v));
    if (isKw(v) || isIP(v) || cidrOk(v)) return null;
    const r = v.split('-');
    if (r.length === 2 && ((isV4(r[0]) && isV4(r[1])) || (isV6(r[0]) && isV6(r[1])))) return null;
    return t('settings.conn.ks.extra.err.invalid', Object.assign({ v }, KS_EX));
  }
  function dnsIpErr(v, list) {
    if (list.includes(v)) return dupErr(v);
    return isIP(v) || cidrOk(v) ? null : t('settings.conn.ks.dns.err.not_ip', { v, ex: '192.0.2.53' });
  }
  function bypassErr(v, list) {
    const x = v.toLowerCase();
    if (list.some(y => y.toLowerCase() === x)) return dupErr(v);
    if (x === 'localhost' || x === '<local>') return null;
    if (/^[a-z0-9-]+$/.test(x) && !/^\d+$/.test(x)) return null;
    if (/^(\*\.)?([a-z0-9-]+\.)*(local|lan|home\.arpa)$/.test(x) && !/^(local|lan|home\.arpa)$/.test(x)) return null;
    if (privIP(x) || (cidrOk(x) && privIP(x.split('/')[0]))) return null;
    const w = x.match(/^(\d{1,3}(?:\.\d{1,3}){0,2})\.\*$/);
    if (w) {
      const p = w[1].split('.').map(Number);
      if (privV4(p) && (p[0] === 10 || p[0] === 127 || p.length >= 2)) return null;
      return t('settings.route.bypass.err.too_wide', { v, a: '10.*', b: '192.168.*' });
    }
    if (isIP(x) || cidrOk(x) || isDomain(x)) return t('settings.route.bypass.err.public', { v });
    return t('settings.route.bypass.err.unknown', { v, a: 'localhost', b: '<local>', c: 'printer', d: '*.local', e: '192.168.*' });
  }
  function ipRule(s) {
    const [ip, bits] = s.split('/');
    const v6 = isV6(ip);
    if (!isV4(ip) && !v6) return { err: t('settings.route.rule.err.ip', { v: s }) };
    if (bits != null && (!/^\d{1,3}$/.test(bits) || +bits > (v6 ? 128 : 32))) return { err: t('settings.route.rule.err.cidr_bits', { v: s, ver: v6 ? 6 : 4, max: v6 ? 128 : 32 }) };
    return { v: s, kind: 'ip-cidr', label: bits != null ? kindWord('ip-cidr') : t('settings.route.kind.ip') };
  }
  /* ports: → null or { msg, short } (short: the reason without the advice, for the save bar) */
  function portErr(v, other, otherPort) {
    const s = String(v).trim();
    const E = (id, p) => ({ msg: t(`settings.net.port.err.${id}`, p), short: t(`settings.net.port.err.${id}.short`, p) });
    if (!s) return E('empty', {});
    if (!/^\d+$/.test(s)) return E('not_int', { v: s });
    if (+s < 1 || +s > 65535) return E('range', { v: s });
    if (String(other).trim() && +s === +other) return E('conflict', { other_port: otherPort, other: String(other) });
    return null;
  }
  function resErr(rt, v, list) {
    if (list.some(r => r.t === rt && r.v.toLowerCase() === v.toLowerCase())) return dupErr(v);
    if (rt === 'boot') return isIP(v) ? null : t('settings.net.res.err.boot_ip', { ex: RES_T.boot.ph });
    if (isIP(v)) return null;
    const m = v.match(/^(https|tls|quic):\/\/([^/:\s]+)(:\d+)?(\/\S*)?$/i);
    if (!m) return t('settings.net.res.err.unknown', { v });
    if (!isDomain(m[2]) && !isIP(m[2])) return t('settings.net.res.err.host', { v: m[2] });
    return null;
  }
  /* public sources (B-29: as wide as the backend: http or https subscriptions, GitHub owner/repo or
     its URL, a Telegram channel as @name, name or t.me link); the values are secrets, so a
     duplicate can only be told among the values typed on this page */
  function srcNorm(st, v) {
    v = String(v).trim();
    if (st === 'tg' && /^[A-Za-z0-9_]+$/.test(v)) v = '@' + v;
    if (st === 'gh' && !/^https?:/i.test(v)) v = v.replace(/^\/+|\/+$/g, '');
    return v;
  }
  function srcErr(st, v, list, selfId) {
    if (!v || v === '@') return t('settings.bastion.src.err.empty', { kind: st });
    if (list.some(s => s.id !== selfId && s.t === st && s.v && s.v.toLowerCase() === v.toLowerCase())) return t('settings.bastion.src.err.duplicate', { v });
    // the backend's rules (bridge_validation._source_value, config_validation.validate_url): a
    // subscription is any http(s) URL with a host (localhost:25500 too); GitHub is owner/repo, a
    // github.com/owner/repo URL, or another http(s) URL (a mirror); a Telegram channel any name
    if (st === 'sub') return webUrl(v) ? null : t('settings.bastion.src.err.sub', { v, ex: SRC_T.sub.ph });
    if (st === 'gh') return /^[\w.-]+\/[\w.-]+$/.test(v) || ghUrl(v) ? null : t('settings.bastion.src.err.gh', { ex: SRC_T.gh.ph });
    return v.replace(/^@/, '').trim() ? null : t('settings.bastion.src.err.tg', { ex: SRC_T.tg.ph });
  }
  const webUrl = v => { try { const u = new URL(v); return (u.protocol === 'http:' || u.protocol === 'https:') && !!u.host && /^https?:\/\/[^/?#\s]/i.test(v); } catch (e) { return false; } };
  function ghUrl(v) {
    if (!webUrl(v)) return false;
    const u = new URL(v);
    if (u.hostname.toLowerCase() !== 'github.com') return true;
    const parts = u.pathname.replace(/^\/+|\/+$/g, '').split('/');
    return !u.username && !u.password && !u.search && !u.hash && parts.length === 2 && parts.every(x => /^[\w.-]+$/.test(x));
  }

  /* ================================================================ markup (DOM) */
  function build(root) {
    const toc = el('nav', { class: 'toc set-toc', id: 'set-toc' }, ...SECS.map(s => el('a', { class: 'toc-a', href: `#${s.id}`, 'data-target': s.id },
      icon(s.icon, 15), el('span', { class: 'set-toc-l' }, secTitle(s.key)), el('span', { class: 'dot', 'aria-hidden': 'true' }))));
    const main = el('div', { class: 'set-main', id: 'set-main' },
      el('div', { class: 'set-status', id: 'set-status', 'aria-live': 'polite' }),
      secConn(), secRoute(), secDns(), secNodes(), secSafety(), secAlerts(), secApp());
    root.replaceChildren(el('div', { class: 'set' }, el('aside', { class: 'set-side', 'data-enter': '', 'aria-label': t('settings.toc.aria') }, toc), main));
  }
  /* a section's head: its title (F-01: no subtitle that only restates it), a live summary on the right */
  const head = (title, extra) => el('div', { class: 'fsec-head' },
    el('div', { class: 'set-hd-l' }, el('h2', { class: 'fsec-t', tabindex: '-1' }, title)), extra ? el('div', { class: 'set-hd-a' }, extra) : null);
  const tokBox = (id, ph) => el('div', { class: 'field set-tok', id }, el('div', { class: 'input set-tok-box' },
    el('input', { class: 'set-tok-in', placeholder: ph, autocomplete: 'off', spellcheck: 'false', 'aria-label': ph })), el('div', { class: 'field-msg' }));
  const hiddenNote = id => el('div', { class: 'set-hidden-note', id, hidden: true }, icon('lock', 13), el('span', null, t('settings.hidden.note')));

  const subrow = (id, title, ctl, extra) => el('div', { class: 'set-subrow', id }, el('div', { class: 'frow-l' }, el('div', { class: 'frow-t' }, el('span', { class: 'set-t' }, title))), ctl ? el('div', { class: 'frow-c' }, ctl) : null, extra || null);
  /* 连接: 接管方式 (two live switches, mode.set; the system proxy's bypass list under its switch,
     shown while it is on), then the proxy port, sharing it with other devices, IPv6 */
  function secConn() {
    const modeSw = k => el('button', { class: 'switch sm', type: 'button', role: 'switch', 'data-mode': k, 'aria-checked': 'false', 'aria-label': modeLabel(k) });
    const bypass = el('div', { class: 'set-subrow-x set-bypass', id: 'set-bypass-row' },
      el('div', { class: 'set-bypass-t' }, t('settings.route.bypass.title')),
      hiddenNote('set-bypass-hidden'),
      tokBox('set-tok-bypass', t('settings.route.bypass.placeholder', { a: 'nas.local', b: '192.168.*' })));
    // (11-05: no line saying what each mode takes over; the letter for users says it, letter/letter.js)
    const modeRow = (k, extra) => el('div', { class: 'set-subrow set-mode-sw', id: `set-mode-${k}-row` },
      el('div', { class: 'frow-l' }, el('div', { class: 'frow-t' }, el('span', { class: 'set-t' }, modeLabel(k)))),
      el('div', { class: 'frow-c' }, modeSw(k)), extra);
    return el('section', { class: 'fsec', id: 'set-conn', 'data-enter': '' },
      head(t('settings.sec.conn.title')),
      el('div', { class: 'frow is-block set-mode', id: 'set-mode-row' },
        el('div', { class: 'frow-l' }, el('div', { class: 'frow-t' }, el('span', { class: 'set-t' }, t('settings.conn.mode.title')))),
        el('div', { class: 'set-sub set-mode-sws', id: 'set-mode-sws', role: 'group', 'aria-label': t('settings.conn.mode.title') },
          modeRow('system', bypass),
          modeRow('tun', el('div', { class: 'set-subrow-x set-mode-gate', id: 'set-gate-tun', hidden: true }, icon('lock', 13), el('span', null, t('settings.conn.mode.gate'))))),
        el('div', { class: 'set-mode-st', id: 'set-mode-st', 'aria-live': 'polite' })),
      frow('set-portMixed-row', t('settings.net.port_mixed.title'), t('settings.net.port_mixed.hint', { def: '7890' }), numBox('set-port-mixed', 'w-port', t('settings.net.port_mixed.title'), true), { cls: 'set-vrow' }),
      frow('set-lan-row', t('settings.net.lan.title'), null, swEl('lan', t('settings.net.lan.title'))),
      frow('set-ipv6-row', t('settings.net.ipv6.title'), null, swEl('ipv6', t('settings.net.ipv6.title'))));
  }

  /* 安全: the kill switch and what it still lets through */
  function secSafety() {
    const ksSub = el('div', { class: 'frow-sub set-ks-sub-wrap' },
      callout('warning', t('settings.conn.ks.admin.title'), t('settings.conn.ks.admin.body'), 'set-ks-admin'),
      // (11-05: no standing "off" warning while the switch is off; turning it off asks once (the
      // danger confirmation of onChange below), and the letter for users says why it matters)
      el('div', { class: 'set-sub', id: 'set-ks-sub' },
        el('div', { class: 'set-sub-h' }, t('settings.conn.ks.allow.title')),
        subrow('set-ksLan-row', t('settings.conn.ks.lan.title'), swEl('ksLan', t('settings.conn.ks.lan.aria'), true)),
        subrow('set-ksDns-row', t('settings.conn.ks.dns.title'), swEl('ksDns', t('settings.conn.ks.dns.aria'), true),
          el('div', { class: 'set-subrow-x', id: 'set-ksDnsList-wrap' }, hiddenNote('set-ksDnsList-hidden'), tokBox('set-tok-ksdns', t('settings.conn.ks.dns.placeholder')))),
        el('div', { class: 'set-subrow is-block', id: 'set-ksExtra-row' }, el('div', { class: 'frow-l' }, el('div', { class: 'frow-t' }, el('span', { class: 'set-t' }, t('settings.conn.ks.extra.title'))),
          el('div', { class: 'frow-d' }, t('settings.conn.ks.extra.desc', { local: kwShow('LocalSubnet'), gateway: kwShow('DefaultGateway'), dhcp: 'DHCP' }))),
        hiddenNote('set-ksExtra-hidden'), tokBox('set-tok-ksextra', t('settings.conn.ks.extra.placeholder')))));
    return el('section', { class: 'fsec', id: 'set-safety', 'data-enter': '' },
      head(t('settings.sec.safety.title')),
      frow('set-ks-row', t('settings.conn.ks.title'), null, swEl('ks', t('settings.conn.ks.title')), { live: live('set-ks-live'), sub: ksSub }));
  }

  function secRoute() {
    const cond = condMarkup('set-rule-in', 'set-rule');
    return el('section', { class: 'fsec', id: 'set-route', 'data-enter': '' },
      head(t('settings.sec.route.title')),
      el('div', { class: 'frow is-block set-rules', id: 'set-rules-row' },
        el('div', { class: 'frow-l' }, el('div', { class: 'frow-t' }, el('span', { class: 'set-t' }, t('settings.route.rules.title')))),
        // 当 [种类] [内容] 时 → [直连 | 代理 | 屏蔽]: one rule, filled in (配置整理 P2). The list below
        // is the match order; Tesseract's own rules around it (local and LAN addresses before, the
        // rest after) are fixed and not shown (plan §0 3)
        el('div', { class: 'field set-add-f', id: 'set-rule-f' },
          el('div', { class: 'set-add set-when' },
            el('span', { class: 'set-when-w' }, t('settings.route.when.if')),
            cond.kindBtn, cond.box, cond.pickBtn,
            el('span', { class: 'set-when-w' }, t('settings.route.when.then')),
            el('span', { class: 'seg', id: 'set-rule-act', 'aria-label': t('settings.route.act.aria') }, ...ACTS.map(a => el('button', { class: 'seg-btn', 'data-v': a }, icon(ACT[a].icon, 14), actLabel(a)))),
            el('button', { class: 'btn btn-sm', id: 'set-rule-add', type: 'button' }, el('span', { class: 'bi' }, icon('plus', 15)), el('span', { class: 'lab' }, t('settings.action.add')))),
          el('div', { class: 'field-msg' })),
        el('div', { class: 'set-rule-more' },
          el('button', { class: 'link-btn', id: 'set-combo-open', type: 'button', 'aria-expanded': 'false', 'aria-controls': 'set-combo' }, icon('plus', 14), el('span', null, t('settings.route.combo.open')))),
        el('div', { class: 'set-combo', id: 'set-combo', hidden: true }),
        el('div', { class: 'set-rl-h' }, el('span', { class: 'set-rl-t' }, t('settings.route.custom.title')), el('span', { class: 'set-rl-m', id: 'set-rl-cm' }), el('span', { class: 'spacer' }),
          el('button', { class: 'link-btn', id: 'set-geo-link', type: 'button' }, el('span', null, t('settings.app.geo.title')), el('span', { id: 'set-geo-link-t' }), icon('chevron', 14))),
        hiddenNote('set-rules-hidden'),
        el('div', { class: 'list set-rl', id: 'set-rl-custom', 'aria-label': t('settings.route.custom.title') }),
        el('div', { class: 'set-rl-empty', id: 'set-rl-empty', hidden: true }, el('span', null, t('settings.route.custom.empty')))),
      // 规则集: a rule set by its address, downloaded through the proxy; a rule names it
      el('div', { class: 'frow is-block set-sets', id: 'set-sets-row' },
        el('div', { class: 'frow-l' }, el('div', { class: 'frow-t' }, el('span', { class: 'set-t' }, t('settings.route.sets.title')), el('span', { class: 'set-rl-m', id: 'set-sets-m' }))),
        el('div', { class: 'list set-rl dense', id: 'set-sets-list', hidden: true, 'aria-label': t('settings.route.sets.title') }),
        el('div', { class: 'field set-add-f', id: 'set-set-f' },
          el('div', { class: 'set-add' },
            el('div', { class: 'input sm set-set-name' }, el('input', { id: 'set-set-name', translate: 'no', maxlength: '40', autocomplete: 'off', spellcheck: 'false', placeholder: t('settings.route.sets.name_ph'), 'aria-label': t('settings.route.sets.name_ph') })),
            el('div', { class: 'input mono sm set-add-in' }, el('input', { id: 'set-set-url', translate: 'no', autocomplete: 'off', spellcheck: 'false', placeholder: 'https://rules.example/ads.mrs', 'aria-label': t('settings.route.sets.url_aria') })),
            el('button', { class: 'select sm w-behav', id: 'set-set-behav', 'aria-label': t('settings.route.sets.behavior_aria') }, ...selV('set-set-behav-v')),
            el('button', { class: 'btn btn-sm', id: 'set-set-add', type: 'button' }, el('span', { class: 'bi' }, icon('plus', 15)), el('span', { class: 'lab' }, t('settings.action.add')))),
          el('div', { class: 'field-msg' }))));
  }

  function secDns() {
    const sel = (id, label, cls) => el('button', { class: `select sm ${cls}`, id, 'aria-label': label });
    return el('section', { class: 'fsec', id: 'set-dns', 'data-enter': '' },
      head(t('settings.sec.dns.title')),
      frow('set-dns-row', t('settings.net.dns.title'), null, sel('set-dns-mode', t('settings.net.dns.mode_aria'), 'w-dns'), { sub: el('div', { class: 'frow-sub', id: 'set-dns-custom', hidden: true },
        el('div', { class: 'set-res' },
          el('div', { class: 'set-res-req', id: 'set-res-req' }),
          hiddenNote('set-dnsList-hidden'),
          el('div', { class: 'list set-rl dense', id: 'set-res-list', 'aria-label': t('settings.net.res.list_aria') }),
          el('div', { class: 'field set-add-f', id: 'set-res-f' },
            el('div', { class: 'set-add' }, sel('set-res-type', t('settings.net.res.type_aria'), 'w-res'),
              el('div', { class: 'input mono sm set-add-in' }, el('input', { id: 'set-res-in', translate: 'no', autocomplete: 'off', spellcheck: 'false', 'aria-label': t('settings.net.res.input_aria') })),
              el('button', { class: 'btn btn-sm', id: 'set-res-add', type: 'button' }, el('span', { class: 'bi' }, icon('plus', 15)), el('span', { class: 'lab' }, t('settings.action.add')))),
            el('div', { class: 'field-msg' })))) }),
      frow('set-dnsRules-row', t('settings.net.dns_rules.title'), null, swEl('dnsRules', t('settings.net.dns_rules.title'))));
  }

  /** a latency in the preview's own words (no digit grouping: 5000 ms) */
  const msText = v => t('settings.quality.ms', { v: String(v) });
  /* 节点: how Tesseract judges and keeps its nodes (the latency ceiling, egress recovery, the exit IP
     lookup), Bastion's automatic pool, and the subscription service that hands the nodes out */
  function secNodes() {
    const mk = id => el('span', { class: 'set-lat-mk', id }, el('span'));
    return el('section', { class: 'fsec', id: 'set-nodes', 'data-enter': '' },
      head(t('settings.sec.nodes.title')),
      el('div', { class: 'frow is-block set-lat-row', id: 'set-lat-row' },
        el('div', { class: 'frow-l' }, el('div', { class: 'frow-t' }, el('span', { class: 'set-t' }, t('settings.quality.latency.title')))),
        el('div', { class: 'set-lat-top' },
          el('div', { class: 'set-lat-read' }, el('span', { class: 'set-lat-v tnum', id: 'set-lat-v', 'aria-hidden': 'true' }), el('span', { class: 'set-lat-def', id: 'set-lat-def' })),
          el('div', { class: 'set-lat-sl', id: 'set-lat-sl' })),
        el('div', { class: 'set-lat-prev', id: 'set-lat-prev', 'aria-label': t('settings.quality.preview_aria') },
          el('div', { class: 'set-lat-dots', id: 'set-lat-dots' }),
          el('div', { class: 'set-lat-bar' }, el('i', { class: 'b-ok', id: 'set-lb-ok' }), el('i', { class: 'b-slow', id: 'set-lb-slow' }), el('i', { class: 'b-down', id: 'set-lb-down' }), el('span', { class: 'set-lat-cut', id: 'set-lc-half' }), el('span', { class: 'set-lat-cut', id: 'set-lc-ceil' })),
          el('div', { class: 'set-lat-axis' }, el('span', { class: 'set-lat-end l' }, msText(20)), mk('set-lm-half'), mk('set-lm-ceil'), el('span', { class: 'set-lat-end r', id: 'set-lat-endr' }, msText(5000)))),
        el('div', { class: 'set-lat-sum', id: 'set-lat-sum', 'aria-live': 'polite' })),
      frow('set-egress-row', t('settings.net.egress.title'), t('settings.net.egress.desc'), swEl('egress', t('settings.net.egress.title')), { sub: el('div', { class: 'frow-sub', id: 'set-egress-sub' }, el('div', { class: 'set-sub' },
        el('div', { class: 'set-subrow', id: 'set-egressRetry-row' }, el('div', { class: 'frow-l' }, el('div', { class: 'frow-t' }, el('span', { class: 'set-t' }, t('settings.net.egress_retry.title', { bastion: tierLbl('bastion') })))),
          el('div', { class: 'frow-c' }, swEl('egressRetry', t('settings.net.egress_retry.title', { bastion: tierLbl('bastion') }), true))))) }),
      frow('set-autoExit-row', t('settings.conn.auto_exit.title'), null, swEl('autoExit', t('settings.conn.auto_exit.title'))),
      frow('set-bastionOn-row', t('settings.bastion.on.title'), null, swEl('bastionOn', t('settings.bastion.on.title')), { live: live('set-bastion-live') }),
      frow('set-bastionMin-row', t('settings.bastion.min.title'), '', numBox('set-bastion-min', 'w-min', t('settings.bastion.min.title')), { cls: 'set-vrow' }),
      el('div', { class: 'frow is-block', id: 'set-sources-row' },
        el('div', { class: 'frow-l' }, el('div', { class: 'frow-t' }, el('span', { class: 'set-t' }, t('settings.bastion.sources.title')), live('set-src-live'))),
        el('div', { class: 'list set-rl', id: 'set-src-list', 'aria-label': t('settings.bastion.sources.title') }),
        el('div', { class: 'field set-add-f', id: 'set-src-f' },
          el('div', { class: 'set-add' }, el('button', { class: 'select sm w-src', id: 'set-src-type', 'aria-label': t('settings.bastion.src.type_aria') }),
            el('div', { class: 'input mono sm set-add-in' }, el('input', { id: 'set-src-in', translate: 'no', autocomplete: 'off', spellcheck: 'false', 'aria-label': t('settings.bastion.src.input_aria') })),
            el('button', { class: 'btn btn-sm', id: 'set-src-add', type: 'button' }, el('span', { class: 'bi' }, icon('plus', 15)), el('span', { class: 'lab' }, t('settings.action.add')))),
          el('div', { class: 'field-msg' }))),
      frow('set-portSub-row', t('settings.net.port_sub.title'), t('settings.net.port_sub.hint', { def: '25500' }), numBox('set-port-sub', 'w-port', t('settings.net.port_sub.title'), true), { cls: 'set-vrow' }));
  }

  function secApp() {
    const btn = (id, ic, label, cls) => el('button', { class: `btn btn-sm${cls ? ' ' + cls : ''}`, id, type: 'button' }, el('span', { class: 'bi' }, icon(ic, 15)), el('span', { class: 'lab' }, label));
    // (no 数据目录 row: the page never sends the user to files; plan 配置可视化整理方案 §3.4. The
    // configuration folder is offered only when a file cannot be read, in the status line)
    const notes = el('button', { class: 'btn btn-sm btn-ghost', id: 'set-upd-notes', type: 'button', hidden: true }, t('settings.app.update.notes'));
    const install = el('button', { class: 'btn btn-sm btn-primary', id: 'set-upd-install', type: 'button', hidden: true }, el('span', { class: 'bi' }, icon('download', 15)), el('span', { class: 'lab' }, t('settings.app.update.install')));
    const page = el('button', { class: 'btn btn-sm btn-ghost', id: 'set-upd-page', type: 'button', hidden: true }, el('span', { class: 'bi' }, icon('external', 15)), el('span', { class: 'lab' }, t('settings.app.update.release_page')));
    return el('section', { class: 'fsec', id: 'set-app', 'data-enter': '' },
      head(t('settings.sec.app.title')),
      // (the three rows' description lines carry only a state's reason: a language not switched, autostart
      // unreadable / unsupported / pointing elsewhere, the tray row resting while autostart is off)
      frow('set-lang-row', t('settings.app.lang.title'), '', el('button', { class: 'select sm w-lang', id: 'set-lang', 'aria-label': t('settings.app.lang.aria') }), { did: 'set-lang-d' }),
      frow('set-autostart-row', t('settings.app.autostart.title'), '', swEl('autostart', t('settings.app.autostart.title')), { live: live('set-auto-live'), did: 'set-auto-d' }),
      frow('set-tray-row', t('settings.app.tray_on_autostart.label'), '', swEl('tray', t('settings.app.tray_on_autostart.label')), { did: 'set-tray-d' }),
      el('div', { class: 'frow', id: 'set-upd-row' },
        el('div', { class: 'frow-l' }, el('div', { class: 'frow-t' }, el('span', { class: 'set-t' }, t('settings.app.update.title'))),
          el('div', { class: 'frow-d set-ver' }, 'Tesseract ', el('b', { class: 'tnum', id: 'set-ver-app', translate: 'no' }), el('span', { class: 'set-sep' }, '·'), t('settings.app.version.core'), ' ', el('b', { class: 'tnum', id: 'set-ver-core' })),
          el('div', { class: 'set-upd-st', id: 'set-upd-st', 'aria-live': 'polite' }),
          el('div', { class: 'set-upd-meta', id: 'set-upd-meta', hidden: true }),
          el('div', { class: 'set-upd-prog', id: 'set-upd-prog', hidden: true }, el('span', { class: 'prog sm' }, el('i')))),
        el('div', { class: 'frow-c set-upd-btns' }, notes, page, install, btn('set-upd-check', 'refresh', t('settings.app.update.check')))),
      el('div', { class: 'frow is-block', id: 'set-geo-row' },
        el('div', { class: 'set-rowhead' }, el('div', { class: 'frow-l' }, el('div', { class: 'frow-t' }, el('span', { class: 'set-t' }, t('settings.app.geo.title')))),
          btn('set-geo-btn', 'download', t('settings.app.geo.update'))),
        el('div', { class: 'set-geo', id: 'set-geo' }),
        el('div', { class: 'set-geo-err', id: 'set-geo-err', hidden: true })));
  }

  /* ================================================================ the page's status line
     loading · the snapshot could not be read (重试) · read_errors (打开配置目录, no path: G48) ·
     a saved change waiting for the service (config.snapshot.pending) · B-27's live notice */
  let extNotice = false;
  function paintStatus(animate) {
    if (!R.status) return;
    const parts = [];
    if (snap.state === 'loading' && !snap.ever) parts.push(el('div', { class: 'set-st-line' }, el('span', { class: 'spin' }), el('span', null, t('settings.status.loading'))));
    if (snap.state === 'failed') {
      const c = callout('error', t('settings.status.failed.title'), snap.error ? errText(snap.error) : t('settings.status.failed.body'));
      const act = el('div', { class: 'callout-act' }, el('button', { class: 'btn btn-xs', type: 'button', 'data-act': 'retry' }, el('span', { class: 'lab' }, t('common.action.retry'))));
      c.append(act); c.setAttribute('role', 'alert');
      parts.push(c);
    }
    if (snap.readErrors.length) {
      const c = callout('error', t('settings.status.read_errors.title', { n: snap.readErrors.length }), t('settings.status.read_errors.body'));
      const list = el('ul', { class: 'set-st-errs' }, ...snap.readErrors.slice(0, 4).map(e => el('li', null, errText(e))));
      c.querySelector('.callout-body').append(list);
      c.append(el('div', { class: 'callout-act' }, el('button', { class: 'btn btn-xs', type: 'button', 'data-act': 'open-config' }, el('span', { class: 'bi' }, icon('folder', 14)), el('span', { class: 'lab' }, t('settings.app.dirs.open_config')))));
      parts.push(c);
    }
    const pend = snap.pending && (snap.pending.state === 'pending' || snap.pending.state === 'applying');
    if (pend) parts.push(el('div', { class: 'set-st-line' }, icon('clock', 14), el('span', null, t('settings.save.state.pending_ready'))));
    // B-27: a live notice for configuration changed outside this page (the fallback re-reads on every visit)
    if (T.rt.feature('B-27') && extNotice) parts.push(el('div', { class: 'set-st-line' }, icon('info', 14), el('span', null, t('settings.status.external'))));
    const key = parts.map(p => p.textContent).join('|');
    if (R.status._k === key) return;
    R.status._k = key;
    shift(R.status, () => { R.status.replaceChildren(...parts); });
    void animate;
  }

  /* ================================================================ save model */
  /* a save made while the service is starting is deferred (or held) by the backend: the effect
     tags treat 'starting' like 'running' (PROTOCOL §15) */
  const applies = () => { const r = run(); return r === 'running' || r === 'starting'; };
  const countOf = k => C.countOf(k, draft, base);
  const countAll = keys => C.countChanges(keys, draft, base);
  /* an added or edited item carries the changed dot: rows in the page gutter, chips inside the chip */
  const TOKS = [];                    // [key, token box]
  function paintItems() {
    const ids = k => { if (same(draft[k], base[k])) return new Set(); const d = C.listDiff(draft[k], base[k]); return new Set(d.add.concat(d.edit).map(x => String(C.itemKey(x)))); };
    [[R.rlCustom, 'rules'], [R.resList, 'dnsList'], [R.srcList, 'sources']].forEach(([box, k]) => {
      if (!box) return;
      const on = ids(k);
      T.$$('.set-rule', box).forEach(r => { if (!r._ghost) r.classList.toggle('is-item-changed', on.has(r.dataset.key)); });
    });
    TOKS.forEach(([k, tok]) => {
      const on = ids(k);
      T.$$('.set-tok-i', tok.field).forEach(c => c.classList.toggle('is-item-changed', on.has(c.dataset.key)));
    });
  }
  function track() {
    if (!R.root) return;
    const keys = changedKeys(), lv = applies();
    const rows = new Map();
    Object.keys(K).forEach(k => {
      const row = K[k].row(); if (!row) return;
      if (!rows.has(row)) rows.set(row, []);
      if (keys.includes(k)) rows.get(row).push(k);
    });
    rows.forEach((ks, row) => paintMark(row, ks, lv));
    paintItems();
    T.$$('.toc-a', R.toc).forEach(a => a.classList.toggle('is-changed', keys.some(k => K[k].sec === a.dataset.target)));
    syncBar(keys);
  }
  function effTag(ks, lv) {
    const rank = Math.max(...ks.map(k => C.RANK[effOf(k)] || 0));
    // worded by what the user will experience: applied on save (the core reloads in place, no
    // interruption), or a restart of the proxy service (a 1–2 s interruption)
    if (rank === 0) return [t('settings.eff.on_save'), 'bolt', false];
    if (!lv) return [t('settings.eff.next_start'), 'clock', false];
    return rank < 3 ? [t('settings.eff.on_save'), 'bolt', false] : [t('settings.eff.restart'), 'restart', true];
  }
  /* a row's result after a save (no draft on it): 已生效 (fades) · 待生效 · 未生效 · 下次启动生效 ·
     配置已在别处修改; the reason in its tooltip */
  function resultOf(ks) {
    let best = null;
    const order = ['external', 'failed', 'not_applied', 'pending', 'next_start', 'applied'];
    ks.forEach(k => { const r = rowRes.get(k); if (r && (!best || order.indexOf(r.state) < order.indexOf(best.state))) best = r; });
    return best;
  }
  function paintMark(row, ks, lv) {
    const on = ks.length > 0;
    row.classList.toggle('is-changed', on);
    const ttl = row.querySelector('.frow-t'); if (!ttl) return;
    let tag = ttl.querySelector(':scope > .set-eff-w:not(.is-leaving)');
    if (on) {
      const [txt, ic, warn] = effTag(ks, lv);
      if (!tag) {
        tag = el('span', { class: 'set-eff-w' }, el('span', { class: 'set-eff' }),
          el('button', { class: 'icon-btn sm set-undo', type: 'button', 'aria-label': t('settings.eff.undo'), 'data-tip': t('settings.eff.undo') }, icon('undo', 14)));
        ttl.append(tag);
        if (visible) M.anim(tag, [{ opacity: 0, transform: 'translateX(-4px)' }, { opacity: 1, transform: 'none' }], { duration: 200, easing: M.ease.out });
      }
      const eff = tag.querySelector('.set-eff');
      eff.classList.toggle('is-warn', warn);
      swapNode(eff, T.html`${I(ic, 13)}${txt}`, { key: `${ic}:${txt}` });
      tag._keys = ks.slice();
    } else if (tag) {
      tag.classList.add('is-leaving');
      if (!visible || M.reduced) tag.remove();
      else M.onDone(M.anim(tag, [{ opacity: 1 }, { opacity: 0 }], { duration: 120, easing: M.ease.in, fill: 'forwards' }), () => tag.remove(), 300);
    }
    // the row's own result: without a draft; a section that was not saved (未保存) or was changed
    // elsewhere keeps its draft and says so beside the draft's tag (a new edit clears it: setD)
    const keysHere = Object.keys(K).filter(k => K[k].row && K[k].row() === row);
    const found = resultOf(keysHere);
    const res = found && (!on || found.state === 'failed' || found.state === 'external') ? found : null;
    let rt = ttl.querySelector(':scope > .set-res-tag');
    if (!res) { if (rt) rt.remove(); return; }
    if (!rt) { rt = el('span', { class: 'set-res-tag' }); ttl.append(rt); }
    const label = t(`settings.save.row.${res.state}`);
    rt.className = `set-res-tag is-${res.state}`;
    if (rt.textContent !== label) rt.textContent = label;
    if (res.reason) rt.setAttribute('data-tip', res.reason); else rt.removeAttribute('data-tip');
  }
  /* the bar's effect, by the service's state: stopped → 下次启动服务时生效; starting → 等服务就绪时生效 */
  function barEffect(keys) {
    const rank = Math.max(...keys.map(k => C.RANK[effOf(k)] || 0));
    if (rank === 0) return 'instant';
    const r = run();
    if (r === 'starting') return { icon: 'clock', text: t('core.savebar.effect.deferred'), busy: t('core.savebar.busy.apply'), done: t('settings.save.done.pending') };
    if (r !== 'running' && r !== 'stopping') return { icon: 'clock', text: t('core.savebar.effect.next_start'), busy: t('settings.save.busy.saving'), done: t('settings.save.done.next_start_short') };
    if (rank === 3) return 'restart';
    if (rank === 2) return 'reload';
    return { icon: 'refresh', text: t('core.savebar.effect.recheck'), busy: t('core.savebar.busy.apply'), done: t('settings.save.done.recheck') };
  }
  /* (while a channel drawer, a modal, is open the library keeps the bar out of sight: #app.has-modal) */
  function syncBar(keys) {
    const cur = T.ui.saveBarCurrent;
    if (!keys.length) { if (cur && cur.state === 'dirty') cur.update({ count: 0 }); return; }
    if (T.router.current !== 'settings') return;
    // the third result stays while only the drafts the partial save left remain (its sentence
    // counts them); a new edit takes the bar, 关闭 brings it back as 有未保存的更改
    if (cur && cur.alive && cur.state === 'not_applied' && leftover && keys.every(k => leftover[k] === C.sig(draft[k]))) return;
    T.ui.saveBar({ count: countAll(keys), effect: barEffect(keys), onSave, onUndo, onRetry, onClose });
  }
  function repaint(keys, animate) {
    keys.forEach(k => { if (K[k]) { K[k].paint(draft[k], animate); if (K[k].clear) K[k].clear(); } });
  }
  function revert(keys) {
    keys.forEach(k => { draft[k] = clone(base[k]); });
    repaint(keys, true);
    track();
  }
  /* 关闭 on the third result: the drafts it left come back with the dirty bar */
  function onClose() {
    leftover = null; lastSave = null;
    setTimeout(() => { if (T.router.current === 'settings') track(); }, 0);
  }
  function onUndo() {
    const keys = changedKeys();
    keys.forEach(k => { draft[k] = clone(base[k]); });
    repaint(keys, visible);
    track();
  }
  /* a draft key outside changes.js's table → its ConfigChange (K[key].change(draft, base) → { section,
     values, secrets? } | null) */
  const adaptKey = k => (K[k] && typeof K[k].change === 'function' ? K[k].change(clone(draft[k]), clone(base[k])) : null);
  const CHECKS = [];
  function firstProblem() { for (const c of CHECKS) { const p = c(); if (p) return p; } return null; }
  function showProblem(p) {
    // (T-P4-09, review S2) a row in a section not shown yet (deferSections) is shown first: its
    // rects, and the control to focus (p.ctl: the row's first shown control), exist only then
    showUpTo(p.at || p.row);
    if (!p.focus && p.ctl && p.row) p.focus = T.$$(ROW_CTL, p.row).find(isShown) || null;
    // the whole row when it fits above the save bar (its title says what the field is), else the field itself
    const h = host(), at = p.at || p.row;
    const fits = h && p.row && at !== p.row && (at.getBoundingClientRect().bottom - p.row.getBoundingClientRect().top) < h.clientHeight - 150;
    const dist = fits ? glideTo(p.row, 32) : glideTo(at, 96);
    if (p.show) p.show();
    afterScroll(dist, () => { if (p.focus) p.focus.focus({ preventScroll: true }); wash(p.row); });
  }
  const holdErr = (msg, extra) => Object.assign(new Error(msg), { hold: true }, extra || {});

  /* ---- settings.validate: the backend's word on a changed value (G49; the local checks are only
     quick hints). The latest answer per key; a refusal holds the save at its row. ---- */
  const remoteErr = {};
  const remoteSeq = {};
  const REMOTE = {
    portMixed: v => ['network', 'mihomo_mixed_port', v], portSub: v => ['network', 'subscription_port', v],
    bastionMin: v => ['bastion', 'min_alive_nodes', v],
    ksDnsList: v => ['killswitch', 'dns_whitelist', v], ksExtra: v => ['killswitch', 'additional_whitelist', v],
    bypass: v => ['network', 'system_proxy', { bypass: v }],
  };
  let remoteT = {};
  function remoteCheck(k) {
    if (!REMOTE[k] || !can('settings.validate') || !editable(k)) return;
    clearTimeout(remoteT[k]);
    remoteT[k] = setTimeout(() => {
      const v = draft[k];
      if (same(v, base[k])) { setRemote(k, null); return; }
      if ((k === 'portMixed' || k === 'portSub' || k === 'bastionMin') && !/^\d+$/.test(String(v).trim())) return;   // the local hint already says why
      const n = remoteSeq[k] = (remoteSeq[k] || 0) + 1;
      const [section, field, value] = REMOTE[k](clone(v));
      T.rt.call('settings.validate', { section, field, value }).then(r => {
        if (remoteSeq[k] !== n || !same(draft[k], v)) return;
        setRemote(k, r && r.valid === false && Array.isArray(r.errors) && r.errors.length ? errText(r.errors[0]) : null);
      }, () => { if (remoteSeq[k] === n) setRemote(k, null); });   // no answer: the local check stands, the save decides
    }, 350);
  }
  function setRemote(k, msg) {
    if ((remoteErr[k] || null) === (msg || null)) return;
    remoteErr[k] = msg || null;
    if (K[k] && K[k].remote) K[k].remote(remoteErr[k]);
  }

  /* ---- 保存 ---- */
  /* (fix M1) the lists withheld whole (secret_fields_set) that this save would replace: a draft on
     one, or text typed into its add field that saving adds first. Each asks 会替换整张列表 before
     anything is sent (C1-#5) — 保存, Ctrl S and 保存并离开 all come here; declining holds the save at
     the list's row and nothing is sent. */
  function guardsFor() {
    const ks = new Set(changedKeys().filter(needsGuard));
    PENDING.forEach(p => { if (p.list && needsGuard(p.list) && p.text()) ks.add(p.list); });
    return [...ks];
  }
  function onSave() {
    const ask = guardsFor();
    if (!ask.length) return saveNow();
    return ask.reduce((p, k) => p.then(ok => (ok ? guard(k) : false)), Promise.resolve(true)).then(ok => {
      if (ok) return saveNow();
      const k = ask.find(needsGuard) || ask[0], row = K[k] && K[k].row();
      const pf = PENDING.find(p => p.list === k && p.text());
      if (row) showProblem({ row, focus: (pf && pf.input()) || null, ctl: true });
      return Promise.reject(holdErr(t('settings.hidden.hold')));
    });
  }
  function saveNow() {
    // typed-but-not-added text first (it may add a rule or a resolver), then the checks
    const bad = flushPending() || firstProblem();
    if (bad) { showProblem(bad); return Promise.reject(holdErr(bad.msg)); }
    const keys = changedKeys();
    if (!keys.length) return Promise.resolve({ outcome: 'applied', text: t('settings.save.no_changes') });
    if (snap.state !== 'ready' && !snap.ever) return Promise.reject(holdErr(t('settings.save.hold_unread')));
    // the last guard: a list withheld whole is never sent unless the user agreed to replace it
    const unasked = keys.find(needsGuard);
    if (unasked) { const row = K[unasked].row(); if (row) showProblem({ row, ctl: true }); return Promise.reject(holdErr(t('settings.hidden.hold'))); }
    const { changes, sent } = C.toChanges({ draft, base, keys, adapt: adaptKey });
    if (!changes.length) return Promise.reject(holdErr(t('settings.save.no_changes')));
    if (!can('settings.apply')) return Promise.reject(new T.rt.RtError('not_allowed'));
    // what is saved is what the controls showed when 保存 was pressed: an edit made while the save
    // runs stays a draft, and the bar comes back for it once the result has shown
    const sentKeys = [].concat(...Object.values(sent));
    const snapv = {}; sentKeys.forEach(k => { snapv[k] = clone(draft[k]); });
    seenRev = null; leftover = null; lastSave = null;
    const p = T.rt.call('settings.apply', { base_rev: snap.rev, changes }).then(
      result => finishSave({ result }, sentKeys, sent, snapv),
      error => finishSave({ error }, sentKeys, sent, snapv));
    saving = p;
    const done = () => { if (saving === p) saving = null; };
    p.then(done, done);
    return p;
  }
  /* another write seen while the save ran (config.changed with a newer rev): read the snapshot
     once the save's own rev is the base, so its values reach the rows without drafts */
  function afterExternal() {
    const r = seenRev; seenRev = null;
    if (r != null && (snap.rev == null || r > snap.rev)) { extNotice = true; loadSnapshot(); return true; }
    return false;
  }
  /* a job's end → base, rows, the bar's result (resolve) or its hold / failure (reject) */
  function finishSave({ result, error }, sentKeys, sent, snapv) {
    const o = C.outcome({ result: result || null, error: error || null }, { run: run(), sent });
    if (o.kind === 'conflict') {
      // re-read the snapshot: rows without a draft follow it, drafts the other write changed say so
      seenRev = null;
      loadSnapshot({ conflict: true });
      return Promise.reject({ conflict: true, title: t('settings.save.conflict'), message: t('settings.save.conflict_body') });
    }
    if (o.kind === 'held') {
      const k = C.fieldKey(o.field);
      const msg = o.error && o.error.code === 'lifecycle_busy' ? t('settings.save.hold_starting') : errText(o.error);
      if (k && K[k]) {
        const row = K[k].row();
        if (K[k].remote) K[k].remote(msg);
        if (row) showProblem({ row, ctl: true });
      }
      afterExternal();
      return Promise.reject(holdErr(msg));
    }
    if (o.kind === 'failed') {
      if (sentKeys.includes('lang')) langFailed(o.error);
      afterExternal();
      return Promise.reject(o.error && o.error instanceof Error ? o.error : Object.assign(new Error(errText(o.error)), o.error || {}));
    }
    // saved (all or some sections): the saved sections' values are the new base
    const res = result || (error && error.result) || null;
    if (res && Number.isInteger(res.rev)) { snap.rev = res.rev; snap.epoch = T.rt.epoch; }
    const savedSecs = new Set(Object.entries(o.rows).filter(([, r]) => r.state !== 'failed').map(([s]) => s));
    const secOf = C.sectionsOf(sent);
    const stateOf = k => (o.rows[secOf[k]] ? o.rows[secOf[k]].state : null);
    sentKeys.forEach(k => {
      const sec = secOf[k], row = o.rows[sec];
      if (savedSecs.has(sec)) {
        // the sources: the value typed here is a secret and is not shown once saved (the row says
        // 来源 N · 已设置); the sidecar gives each saved row its own id and a masked hint, which the
        // snapshot read below brings (fix M3: a later save sends the real ids)
        base[k] = k === 'sources' ? snapv[k].map(x => Object.assign({}, x, { v: '' })) : clone(snapv[k]);
        if (same(draft[k], snapv[k])) { draft[k] = clone(base[k]); if (k === 'sources' && K.sources) K.sources.paint(draft.sources, false); }
        if (K[k] && K[k].commit) K[k].commit(clone(base[k]));
      }
      if (row) rowRes.set(k, { state: row.state, reason: row.error ? rowReason(row, k) : row.state === 'pending' ? t('settings.save.state.pending') : null });
    });
    // a section the sidecar held while others were saved: its field's row says why (the bar says one sentence)
    const heldRow = Object.values(o.rows).find(r => r.state === 'failed' && r.error && r.error.field && K[C.fieldKey(r.error.field)]);
    if (heldRow) {
      const k = C.fieldKey(heldRow.error.field), row = K[k].row();
      if (K[k].remote) K[k].remote(errText(heldRow.error));
      if (row) showProblem({ row, ctl: true });
    }
    // the rows left pending: followed through config.changed.pending, whatever id the ledger gives
    // the intent after a rebase (fix M4: a later save or gateway.select moves it to a successor)
    const pendKeys = sentKeys.filter(k => stateOf(k) === 'pending');
    if (pendTrack) pendTrack.keys = pendTrack.keys.filter(k => !sentKeys.includes(k) || pendKeys.includes(k));
    // the batch of the lost generation: a key saved again belongs to this save's result now
    if (genTrack) { genTrack.keys = genTrack.keys.filter(k => !sentKeys.includes(k)); if (!genTrack.keys.length) genTrack = null; }
    if (pendKeys.length) {
      const tr = pendTrack || { keys: [], ids: new Set(), rev: snap.rev };
      pendKeys.forEach(k => { if (!tr.keys.includes(k)) tr.keys.push(k); });
      if (o.pending && typeof o.pending.intent_id === 'string') tr.ids.add(o.pending.intent_id);
      if (!Number.isInteger(tr.rev)) tr.rev = snap.rev;
      pendTrack = tr;
    } else if (pendTrack && !pendTrack.keys.length) pendTrack = null;
    // the third result: 重试应用 only where a restart of the proxy service can apply what was saved
    // (a section whose Core reload / restart failed: coreRetry). A section that needs no Core (the
    // system proxy's bypass, the language, autostart) gives no retry: submitting the same values
    // again changes nothing and would read 已生效 without doing anything (fix M5). The sources'
    // failed reload (effect 'reload', source_reload_failed) does get one: a restart loads the
    // sources from the saved file, and only service.restart's own applied says 已生效
    const naKeys = sentKeys.filter(k => stateOf(k) === 'not_applied');
    // reopen: only the language was saved but not switched (language_sync_failed): the bar says what
    // the row says (reopen the window), not the generic sentence
    // (the ui section also holds autoExit and the tray; its preferences write may fail too:
    // preferences_apply_failed, which reopening the window does not fix)
    lastSave = naKeys.length ? { keys: naKeys, retry: naKeys.filter(k => coreRetry(o.rows[secOf[k]], k)), text: '',
      reopen: sentKeys.includes('lang') && naKeys.every(k => secOf[k] === 'ui') && langUnsynced(o.rows.ui) } : null;
    // the language: the shell re-renders every page on host:language; a failure rolls the choice back
    if (sentKeys.includes('lang')) {
      const ui = o.rows.ui;
      if (ui && ui.state === 'not_applied') { langNote = { kind: 'reopen' }; paintLangNote(true); }   // saved, not switched
      else if (ui && ui.state !== 'failed') expectLanguage(snapv.lang);
      else langFailed(ui && ui.error);
    }
    if (sentKeys.includes('autostart') || sentKeys.includes('tray')) loadAutostart();
    if (sentKeys.includes('latency') && o.rows.latency && o.rows.latency.state === 'applied' && o.rows.latency.effect === 'recheck') rechecking(true);
    // the sources' new ids and hints (fix M3), or another write seen meanwhile: read the snapshot
    if (!afterExternal() && savedSecs.has('sources') && sentKeys.includes('sources')) loadSnapshot();
    fadeApplied();
    // the drafts a partial save left (an unsaved section) keep the third result on the bar; the
    // bar counts them in its sentence and comes back as 有未保存的更改 once it is closed
    if (o.kind === 'not_applied') { leftover = {}; changedKeys().forEach(k => { leftover[k] = C.sig(draft[k]); }); }
    track();
    if (changedKeys().length) setTimeout(() => { if (T.router.current === 'settings') track(); }, 1300);
    return outcomeForBar(o);
  }
  /* can restarting the proxy service apply a section saved but not in effect? its Core reload or
     restart failed (SectionResult.effect), or (no effect reported) the estimate needs the Core */
  function coreRetry(r, k) {
    if (!r) return false;
    if (r.effect === 'reload' || r.effect === 'restart') return true;
    return (!r.effect || r.effect === 'none') && (C.RANK[effOf(k)] || 0) >= 2;
  }
  /* why a section that needs no Core did not take effect. The sidecar's apply_failed sentence says a
     restart of the service applies it, which is true only where the Core applies the section: here
     the section's own reason by reason_code (bridge_settings _run_actions), else a neutral one;
     an error with a message of its own keeps it (05d 新发现「not_applied_saved 文案」) */
  function noCoreReason(e) {
    if (!e) return t('settings.save.not_applied_reason.other');
    // the bar puts the reason before its own full stop: one the error's sentence ends with is dropped
    if (e.message_key !== 'errors.settings.apply_failed') return errText(e).replace(/[\s.!\u3002\uFF0E\uFF01]+$/u, '');
    if (e.reason_code === 'system_proxy_override_failed') return t('settings.save.not_applied_reason.system_proxy');
    return t('settings.save.not_applied_reason.other');
  }
  /* the language was saved but not switched (bridge_settings _run_actions: language_sync_failed) */
  function langUnsynced(r) {
    return !!(r && r.state === 'not_applied' && r.error && r.error.reason_code === 'language_sync_failed');
  }
  /* a saved row's reason (its label's tip): a section that needs no Core does not promise that a
     restart of the service applies it (PROTOCOL §15, the bar's sentence above); the language that
     did not switch says what the language row says */
  function rowReason(row, k) {
    if (row.state !== 'not_applied' || coreRetry(row, k)) return errText(row.error);
    if (k === 'lang' && langUnsynced(row)) return t('settings.app.lang.reopen');
    return noCoreReason(row.error);
  }
  /* the bar's words for a saved outcome (C.barFace: the mapping table, unit-tested) */
  function outcomeForBar(o) {
    const f = C.barFace(o);
    const r = { outcome: f.outcome, text: f.error ? errText(f.error) : t(f.text) };
    if (f.outcome === 'not_applied') {
      if (lastSave && lastSave.retry.length) r.retryLabel = t(f.retry);
      // no retry (the section needs no Core): the reason and that the setting is saved, no promise of
      // how it takes effect (PROTOCOL §15: a restart does not switch the language); the language's
      // own sentence when only the language did not switch
      else { r.noRetry = true; r.text = lastSave && lastSave.reopen ? t('settings.app.lang.reopen') : t('settings.save.not_applied_saved', { reason: noCoreReason(f.error) }); }
      const left = leftover ? countAll(Object.keys(leftover)) : 0;
      if (left) r.text = t('settings.save.left_unsaved', { reason: r.text, count: left });
      if (lastSave) lastSave.text = r.text;
    } else if (f.retry) r.retryLabel = t(f.retry);
    return r;
  }
  /* 已生效 is a passing word: it fades a few seconds after the save */
  let fadeT = 0;
  function fadeApplied() {
    clearTimeout(fadeT);
    fadeT = setTimeout(() => {
      let any = false;
      rowRes.forEach((r, k) => { if (r.state === 'applied' || r.state === 'next_start') { rowRes.delete(k); any = true; } });
      if (any) track();
    }, 6000);
  }
  /* 重试应用 (the third result, only offered for a section a Core reload / restart did not apply):
     restarting the proxy service loads the saved files — it interrupts the connections for a moment,
     so it asks first. A stopped service applies everything on its next start. */
  function onRetry() {
    const ls = lastSave;
    if (!ls || !ls.retry.length) return Promise.resolve({ outcome: 'applied', text: t('core.savebar.done.instant') });
    const again = () => ({ outcome: 'not_applied', text: ls.text || t('core.savebar.failed_reason'), retryLabel: t('settings.save.retry_apply') });
    if (!applies()) {
      ls.keys.forEach(k => rowRes.set(k, { state: 'next_start', reason: null }));
      lastSave = null; leftover = null;
      track();
      return Promise.resolve({ outcome: 'applied', text: t('settings.save.done.next_start_short') });
    }
    if (!can('service.restart')) return Promise.resolve(Object.assign(again(), { retryLabel: undefined, noRetry: true }));
    return T.confirm({ title: t('settings.save.retry_confirm.title'), message: t('settings.save.retry_confirm.body'), confirmLabel: t('settings.save.retry_confirm.ok') }).then(ok => {
      if (!ok) return again();
      return T.rt.call('service.restart', {}).then(r => {
        if (r && r.applied) {
          ls.retry.forEach(k => rowRes.set(k, { state: 'applied', reason: null }));
          const rest = ls.keys.filter(k => !ls.retry.includes(k));
          lastSave = rest.length ? { keys: rest, retry: [], text: ls.text } : null;
          fadeApplied(); track();
          return rest.length ? { outcome: 'not_applied', text: ls.text, noRetry: true } : { outcome: 'applied', text: t('core.savebar.done.restart') };
        }
        return again();
      }, e => { ls.retry.forEach(k => rowRes.set(k, { state: 'not_applied', reason: errText(e) })); track(); return Object.assign(again(), { text: errText(e) }); });
    });
  }

  /* ---- the language: saved as ui.language; the shell's host:language re-renders every page
     (architecture §3.7). No host:language within 3 s (a shell that does not forward ui.language):
     the row says to reopen the window, and the console keeps a note (B3). ---- */
  /* the shell's word may come first: the sidecar records ui.language inside the save, so
     host:language usually arrives before the job's end, and T.i18n switches asynchronously; the
     store's lang (written on host:language) counts as switched too (fix M2) */
  const switchedTo = lang => lang === S.get('lang') || lang === (T.i18n && T.i18n.lang);
  function expectLanguage(lang) {
    if (langWait) clearTimeout(langWait.timer);
    if (switchedTo(lang)) { langWait = null; langNote = null; paintLangNote(true); return; }
    langWait = { lang, timer: setTimeout(() => {
      if (!langWait || langWait.lang !== lang) return;
      if (switchedTo(lang)) { langWait = null; return; }
      langWait = null;
      langNote = { kind: 'reopen' };
      console.info('[settings] ui.language saved; no host:language within 3 s (the shell does not forward ui.language)');
      paintLangNote(true);
    }, 3000) };
  }
  function langFailed(err) {
    draft.lang = clone(base.lang);
    if (K.lang) K.lang.paint(draft.lang, true);
    langNote = { kind: 'failed', reason: err ? errText(err) : '' };
    paintLangNote(true);
  }
  function paintLangNote(animate) {
    if (!R.langD) return;
    if (!langNote) { note(R.langD, '', false, !animate); return; }
    if (langNote.kind === 'reopen') note(R.langD, t('settings.app.lang.reopen'), false, !animate);
    else note(R.langD, t('settings.app.lang.failed', { reason: langNote.reason || t('common.reason_unknown') }), true, !animate);
  }

  /* ---- the snapshot: read on every visit (B-27's fallback) and after a conflict / another write ---- */
  function applySnapshot(d, o) {
    o = o || {};
    if (Number.isInteger(d.rev)) { snap.rev = d.rev; snap.epoch = T.rt.epoch; }
    snap.readErrors = Array.isArray(d.read_errors) ? d.read_errors : [];
    snap.pending = d.pending || null;
    const hasPending = Object.prototype.hasOwnProperty.call(d, 'pending');
    snap.state = 'ready'; snap.error = null; snap.ever = true;
    const m = C.fromSnapshot(d.values, d.secret_fields_set);
    const touched = [];
    C.KEYS.forEach(k => {
      const was = known[k] === true;
      known[k] = !!m.known[k];
      hidden[k] = m.hidden[k] || null;
      if (!known[k]) return;
      const v = m.values[k];
      const hadDraft = was && !same(draft[k], base[k]);
      // a fresh read: a list withheld whole asks again before its next edit (the agreement covered
      // the list as it was then); a list being edited keeps it
      if (!hadDraft) replaced[k] = false;
      const moved = was && !same(base[k], v);
      base[k] = clone(v);
      if (!hadDraft) { draft[k] = clone(v); touched.push(k); }
      else if (o.conflict && moved) rowRes.set(k, { state: 'external', reason: t('settings.save.conflict') });
    });
    // a pending apply (config.snapshot.pending) carrying rows this page saved: the rows follow it
    // (the visit's first snapshot by intent id only, 05d R4-3). After a generation change the new
    // process loaded the saved files: that batch is settled as pending:null
    const idsOnly = !!(pendTrack && pendTrack.idsOnly);
    if (pendTrack) pendTrack.idsOnly = false;
    if (genTrack) { genTrack.read = true; settleGeneration(); }
    if (hasPending) followPending(snap.pending, idsOnly);
    if (R.root) {
      repaint(touched, false);
      paintKnown();
      paintStatus(true);
      paintLatAll();
      // the live states read the saved values too (the kill switch's, the sources' count)
      paintAdmin(visible); paintBastionLive(visible); paintSrcLive(visible); paintAuto(visible);
      track();
    }
  }
  function loadSnapshot(o) {
    o = o || {};
    if (!can('config.snapshot')) {
      snap.state = 'failed'; snap.error = new T.rt.RtError('not_allowed');
      paintStatus(true); paintKnown();
      return Promise.resolve(false);
    }
    const n = ++reqSnap;
    if (!snap.ever) snap.state = 'loading';
    paintStatus(true);
    return T.rt.query('config.snapshot', {}).then(d => {
      if (n !== reqSnap) return false;
      // an answer older than what a save since then wrote (the visit's read, slower than the save):
      // the save's rev and base stand. Only within one runtime epoch (a new sidecar counts from 0
      // again, 续复核1 MUST 1), and never for the read after a conflict: the sidecar just said the
      // page's rev is not its own, so whatever it reads now is the base
      // (the visit's idsOnly goes with it: a save since then started its own batch; a later read,
      // e.g. after a conflict, is not the visit's first)
      if (!o.conflict && d && Number.isInteger(d.rev) && Number.isInteger(snap.rev) && d.rev < snap.rev && snap.ever && snap.epoch === T.rt.epoch) { if (pendTrack) pendTrack.idsOnly = false; paintStatus(true); return false; }
      applySnapshot(d || {}, o);
      return true;
    }, e => {
      if (n !== reqSnap) return false;
      snap.state = 'failed'; snap.error = e;
      paintStatus(true); paintKnown();
      return false;
    });
  }
  function loadAutostart() {
    if (!can('autostart.state')) { auto = null; paintAuto(true); return; }
    quiet(T.rt.query('autostart.state', {}).then(d => { auto = d || null; paintAuto(true); }, e => { auto = { error: e, failed: true }; paintAuto(true); }));
  }
  function loadAll() {
    loadSnapshot();
    loadAutostart();
    if (can('app.info')) quiet(T.rt.query('app.info', {}).then(d => { app = Object.assign({ version: null, listeners: [] }, d || {}); paintVersion(true); paintModeStatus(true); }, () => {}));
    if (can('geo.status')) quiet(T.rt.query('geo.status', {}).then(d => { geo = d || null; paintGeo(true); }, () => {}));
  }
  /* config.changed: our own save (rev ≤ base_rev) is ignored; another write re-reads the snapshot;
     a PendingApply of ours relabels its rows */
  function onConfigChanged(e) {
    if (e && Object.prototype.hasOwnProperty.call(e, 'pending')) followPending(e.pending || null);
    if (!e || !Number.isInteger(e.rev) || e.instant) return;
    // while a save runs, its own write and any other are told apart by its SaveResult.rev at the end
    if (saving) { if (seenRev == null || e.rev > seenRev) seenRev = e.rev; return; }
    if (snap.rev != null && e.rev <= snap.rev) return;
    // the configuration changed elsewhere: Geo may be turned on again (05d §9-3); the next
    // geo.update says it if not
    resetGeoDisabled();
    if (!ownWrites) extNotice = true;          // this page's own mode.set is not 别处修改
    if (visible) loadSnapshot(); else snap.stale = true;
  }
  /* rt:reset with generation_changed (runtime_gone, a new epoch; PROTOCOL §13): the config revision
     lives in the sidecar's memory and starts again in the new process, so the page's rev (and
     the epoch it was read in) belongs to the lost generation. Forgotten: a save waits for the new
     read (hold_unread) instead of sending the old base_rev, the next config.changed is not
     filtered by the old number, a read still in flight is dropped. The new hello's rt:caps reads
     the snapshot again (or the next visit does). 续复核1 MUST 1. */
  function onGeneration(e) {
    if (!e || !e.generation_changed) return;
    reqSnap++;
    snap.rev = null; snap.epoch = null; snap.ever = false; snap.pending = null;
    if (snap.state === 'ready') snap.state = 'loading';
    seenRev = null;
    // the pending rows of the lost generation wait for the new one's snapshot (genTrack); a save
    // from now on is followed on its own
    if (pendTrack) {
      if (genTrack) pendTrack.keys.forEach(k => { if (!genTrack.keys.includes(k)) genTrack.keys.push(k); });
      else genTrack = { keys: pendTrack.keys.slice() };
      pendTrack = null;
    }
    if (genTrack) genTrack.read = false;
    resetGeoDisabled();       // a new process reads its configuration again (05d §9-3)
    paintStatus(visible);
  }
  /* Geo 已禁用 is learnt from a refused geo.update; a new generation or a configuration change
     elsewhere may have turned Geo on again: the button is offered again */
  function resetGeoDisabled() {
    if (!geoDisabled) return;
    geoDisabled = null;
    paintGeo(visible);
  }
  /* the rows a save left pending before a generation change: once the new generation's snapshot
     is read and its service state is known, they read as pending:null does (a newer start loaded
     the saved files): 已生效 while it runs, 下次启动生效 when stopped */
  function settleGeneration() {
    if (!genTrack || !genTrack.read) return;
    const r = run();
    if (r == null || r === 'unknown') return;      // rt:state brings it
    const st = applies() ? 'applied' : 'next_start';
    genTrack.keys.forEach(k => rowRes.set(k, { state: st, reason: null }));
    genTrack = null;
    fadeApplied();
    track();
  }
  /* a PendingApply (config.changed.pending, config.snapshot.pending) → the rows a save left pending.
     pending:null = this generation holds no intent: a newer start loaded the saved files. */
  function followPending(p, idsOnly) {
    if (!pendTrack) return;
    let st;
    if (p == null) st = applies() ? 'applied' : 'next_start';
    else {
      // idsOnly: an intent this page has not seen is not taken for its own by its config_rev
      // (made elsewhere while the page was not shown, e.g. a gateway.select; 05d R4-3)
      if (idsOnly ? !(typeof p.intent_id === 'string' && pendTrack.ids.has(p.intent_id)) : !C.pendingFollows(p, pendTrack)) return;
      if (typeof p.intent_id === 'string') pendTrack.ids.add(p.intent_id);
      st = C.pendingState(p);
      if (!st) return;
    }
    pendTrack.keys.forEach(k => rowRes.set(k, { state: st, reason: st === 'not_applied' && p && p.error ? errText(p.error) : st === 'pending' ? t('settings.save.state.pending') : null }));
    if (st !== 'pending') { pendTrack = null; fadeApplied(); }
    track();
  }
  /* a row whose value the snapshot does not carry (or before it is read) is read-only */
  function paintKnown() {
    if (!R.root) return;
    Object.keys(K).forEach(k => {
      if (!isContract(k) || !K[k].lock) return;
      const lockEl = K[k].lock();
      if (!lockEl) return;
      const off = known[k] !== true;
      lockEl.inert = off;
      lockEl.classList.toggle('is-unknown', off);
      const row = K[k].row();
      if (row) {
        const ttl = row.querySelector(':scope .frow-t');
        // (a soft key, routing.rule_order, only hides its own control when an older sidecar does not
        // carry it: the row's other keys stay editable and the row is not marked unread)
        const unk = Object.keys(K).some(x => isContract(x) && !K[x].soft && K[x].row() === row && known[x] !== true) && snap.ever;
        let n = ttl && ttl.querySelector(':scope > .set-unk');
        if (unk && ttl && !n) { n = el('span', { class: 'set-unk', 'data-tip': t('settings.row.unknown_tip') }, t('settings.row.unknown')); ttl.append(n); }
        else if (!unk && n) n.remove();
      }
      if (K[k].hiddenNote) { const hn = K[k].hiddenNote(); if (hn) hn.hidden = !(hidden[k] && hidden[k].length && !replaced[k]); }
    });
    // the application rows' own locks (autostart unsupported, D8 off with autostart) stay on
    lockApp();
  }
  /* a list withheld whole that the user has not agreed to replace yet */
  function needsGuard(k) { return !!(hidden[k] && hidden[k].length && !replaced[k]); }
  /* a list the projection withheld whole: the first edit asks before it replaces the whole list */
  const guardAsk = {};          // key → the open question (a save and the field's own blur ask once)
  function guard(k) {
    if (!needsGuard(k)) return Promise.resolve(true);
    if (guardAsk[k]) return guardAsk[k];
    // registered before the dialog opens: opening it moves the focus, and the list field's blur asks too
    let answer = null;
    const asked = guardAsk[k] = new Promise(r => { answer = r; });
    T.confirm({ title: t('settings.hidden.confirm.title'), message: t('settings.hidden.confirm.body'), confirmLabel: t('settings.hidden.confirm.ok'), danger: true })
      .then(ok => { guardAsk[k] = null; if (ok) { replaced[k] = true; paintKnown(); } answer(!!ok); }, () => { guardAsk[k] = null; answer(false); });
    return asked;
  }

  /* the pending effect as one sentence for the leave question */
  function effectSentence(keys) {
    const e = barEffect(keys);
    if (typeof e === 'object') return e.text;
    return e === 'restart' ? t('settings.leave.effect_restart') : e === 'reload' ? t('core.savebar.effect.reload') : t('core.savebar.effect.instant');
  }
  /* leaving with drafts: an in-page question with three ways out. 保存并离开 runs the bar's own
     save and only then lets the router go on; a failed save keeps the user here with the reason. */
  function askLeave(bar) {
    const keys = changedKeys(), n = countAll(keys);
    let saveFirst = false;
    const asked = T.confirm({ title: t('settings.leave.title', { n }), message: t('settings.leave.body', { effect: effectSentence(keys) }), confirmLabel: t('settings.leave.discard'), cancelLabel: t('core.savebar.leave.confirm.stay'), danger: true });
    const dlg = T.$$('.ovl .dlg').pop();
    if (dlg) {
      dlg.classList.remove('dlg-danger'); dlg.classList.add('set-leave');
      const acts = dlg.querySelector('.dlg-actions'), yes = acts.querySelector('[data-a="yes"]'), no = acts.querySelector('[data-a="no"]');
      yes.className = 'btn btn-danger-soft set-leave-drop';
      const sv = el('button', { class: 'btn btn-primary', type: 'button' }, el('span', { class: 'lab' }, t('settings.leave.save')));
      sv.addEventListener('click', () => { saveFirst = true; no.click(); });
      acts.prepend(yes); acts.append(sv);
    }
    return asked.then(discard => {
      if (discard) { onUndo(); if (bar) bar.hide(true); return true; }
      if (!saveFirst || !bar || !bar.alive) return false;
      return bar.save().then(ok => (ok ? wait(M.reduced ? 300 : 650).then(() => true) : false));
    });
  }

  /* ================================================================ 连接 */
  let modeSws = null, pendingMode = null, modeFlashT = 0, ownWrites = 0;
  const admin = () => S.get('admin') !== false;
  /* the proxy port the service listens on (app.info listeners, kind mixed); else the saved one */
  const mixedPort = () => { const l = (app.listeners || []).find(x => x && x.kind === 'mixed'); return l ? l.port : base.portMixed || ''; };
  function paintModeStatus(animate, flash) {
    if (!R.modeSt) return;
    const r = run(), mode = S.get('mode'), busy = S.get('modeBusy'), sp = S.get('systemProxy') || {};
    // (the TUN adapter takes over every app in tun and mixed alike)
    const p = { mode: modeLabel(mode), tun: mode === 'tun' || mode === 'mixed' ? 'yes' : 'no', port: String(mixedPort()) };
    const gatedNow = !busy && !flash && needsAdmin(mode) && !admin();
    // the system proxy rewritten by another program: the browser's traffic is not Tesseract's (G08)
    const drift = !busy && !flash && r === 'running' && mode !== 'tun' && (sp.drift || sp.proxy_state === 'other');
    let n, kind;
    if (busy) { kind = 'busy'; n = T.html`<span class="spin"></span>${tx('settings.conn.mode.st.switching', { mode: modeLabel(pendingMode || mode) })}`; }
    else if (modeNote) { kind = 'err'; n = T.html`${I('error', 14)}${t('settings.conn.mode.st.failed', { reason: modeNote })}`; }
    else if (flash) { kind = 'done'; n = T.html`${I('check', 14)}${tx('settings.conn.mode.st.switched', p)}`; }
    else if (gatedNow) { kind = 'gated'; n = T.html`${I('lock', 14)}${tx('settings.conn.mode.st.gated', p)}`; }
    else if (drift) { kind = 'drift'; n = T.html`${I('warning', 14)}${t('settings.conn.mode.st.drift')}`; }
    else if (r === 'running') { kind = 'run'; n = T.html`<span class="sdot ok"></span>${tx('settings.conn.mode.st.running', p)}`; }
    // not running: the mode only (F-01: the service's state and what happens on start are not restated
    // here; the dot says starting / stopping)
    else if (r === 'starting' || r === 'stopping') { kind = r === 'starting' ? 'start' : 'stopping'; n = T.html`<span class="sdot busy"></span>${tx('settings.conn.mode.st.stopped', p)}`; }
    else { kind = 'stopped'; n = T.html`<span class="sdot off"></span>${tx('settings.conn.mode.st.stopped', p)}`; }
    R.modeSt.classList.toggle('is-done', kind === 'done');
    R.modeSt.classList.toggle('is-gated', kind === 'gated' || kind === 'drift');
    R.modeSt.classList.toggle('is-err', kind === 'err');
    swapNode(R.modeSt, n, { kind, instant: !animate });
  }
  /* the two switches show the saved mode, or the one being switched to; a switch holding its own
     request (aria-busy, T.ui.switch) keeps its knob until the request settles */
  const shownMode = () => (S.get('modeBusy') && pendingMode ? pendingMode : S.get('mode'));
  function syncModeSwitches() {
    if (!R.root || !modeSws) return;
    const mode = shownMode(), known = S.get('modeKnown') !== false && mode != null;
    // (they show the stored mode, as the status line under them does; read-only while it is unconfirmed)
    SW_MODES.forEach(k => { const sw = modeSws[k]; if (sw.el.getAttribute('aria-busy') !== 'true') sw.set(mode != null && swOn(mode, k)); });
    // live controls: read-only until the runtime says which mode is set
    if (R.modeSws) R.modeSws.inert = !known || !can('mode.set');
    paintAdmin(false, true);
  }
  /* the bypass list belongs to the system proxy: shown while it is on (and while the mode is unknown) */
  function paintBypass(animate) {
    if (!R.bypassRow) return;
    const mode = S.get('mode');
    reveal([[R.bypassRow, mode == null || swOn(mode, 'system')]], animate);
  }
  function paintAdmin(animate, switchesOnly) {
    if (!R.root) return;
    // without administrator rights no switch may bring the TUN adapter up: 系统代理 can only lead to
    // tun or mixed (locked), 虚拟网卡 can only go off (back to the system proxy); the line under it says why
    const gated = !admin(), mode = S.get('mode');
    if (modeSws) {
      modeSws.system.el.setAttribute('aria-disabled', String(gated));
      modeSws.tun.el.setAttribute('aria-disabled', String(gated && !(mode != null && swOn(mode, 'tun'))));
    }
    reveal([[R.gateTun, gated]], animate);
    if (switchesOnly) return;
    reveal([[R.ksAdmin, !admin() && !!draft.ks]], animate);
    paintModeStatus(animate);
    paintKsLive(animate);
  }
  /* a locked switch pressed: the line under 虚拟网卡 says why (flashes) */
  function flashGate() {
    if (R.gateTun && !R.gateTun.hidden) M.anim(R.gateTun, [{ opacity: 0.3 }, { opacity: 1 }], { duration: 480, easing: M.ease.out });
  }
  /* a switch pressed: the mode it leads to (nextMode), asked like any mode switch (askMode); the
     switch holds its knob until the answer (true: it moves; false or a refusal: it stays) */
  function askSwitch(k) {
    const cur = S.get('mode');
    if (S.get('modeBusy') || S.get('modeKnown') === false || cur == null) return false;
    const v = nextMode(cur, k);
    if (needsAdmin(v) && !admin()) { flashGate(); return false; }
    const r = askMode(v);
    return r === false ? false : r.then(ok => { syncModeSwitches(); paintBypass(true); return ok; });
  }
  /* mode.set: switches at once (not a draft); admin_required (also while stopped), a failed apply
     (saved, not applied) and cleanup_pending say why on the status line */
  function askMode(v) {
    if (S.get('modeBusy')) return false;
    if (needsAdmin(v) && !admin()) return false;
    const go = () => {
      pendingMode = v; modeNote = null;
      // the config.changed this write causes is the page's own (not B-27's 配置已被其他操作修改)
      ownWrites++;
      const own = () => { setTimeout(() => { ownWrites = Math.max(0, ownWrites - 1); }, 0); };
      const call = T.rt.call('mode.set', { mode: v });
      call.then(own, own);
      return call.then(r => {
        if (r && r.applied === false && r.apply_error) { modeNote = errText(r.apply_error); paintModeStatus(true); clearNote(); }
        return true;
      }, e => {
        pendingMode = null;
        const ae = e && e.result && e.result.apply_error;
        modeNote = errText(ae || e);
        syncModeSwitches();
        paintModeStatus(true); clearNote();
        return false;
      });
    };
    if (applies()) {
      return T.confirm({ title: t('mode.switch.confirm.title', { label: modeLabel(v) }), message: t('mode.switch.confirm.body', { mode: v }), confirmLabel: t('mode.switch.confirm.ok') })
        .then(ok => (ok ? go() : false));
    }
    return go();
  }
  function clearNote() { clearTimeout(modeFlashT); modeFlashT = setTimeout(() => { modeNote = null; paintModeStatus(visible); }, 8000); }
  /* the kill switch's live state (killswitch.state through rt/project: disabled · error · triggered
     · inactive · blocked · armed) against the saved setting */
  function paintKsLive(animate) {
    if (!R.ksLive) return;
    const ks = S.get('killswitch'), r = run();
    let d, w;
    if (!base.ks || ks === 'disabled') { d = 'off'; w = 'off'; }
    else if (ks === 'error') { d = 'err'; w = 'error'; }
    else if (ks === 'triggered') { d = 'err'; w = 'triggered'; }
    else if (ks === 'blocked' || !admin()) { d = 'warn'; w = 'no_admin'; }
    else if (ks === 'armed') { d = 'ok'; w = 'armed'; }
    else { d = 'off'; w = null; }
    R.ksLive.classList.toggle('is-warn', d === 'warn');
    // on but not armed while the service is not running: no state word (F-01: 服务已停止 · 启动后布防
    // only restated the service's state); the switch says it is on
    R.ksLive.hidden = !w;
    if (!w) return;
    swapNode(R.ksLive, T.html`<span class="sdot ${d}"></span>${t(`settings.conn.ks.live.${w}`)}`, { kind: d, instant: !animate });
  }
  function ksDeps(on, animate) {
    reveal([[R.ksSub, on], [R.ksAdmin, on && !admin()]], animate);
  }

  /* ---- token list: chips + an input; paste many at once; each entry validated ---- */
  function tokens(fieldEl, o) {
    const box = fieldEl.querySelector('.set-tok-box'), inp = box.querySelector('input');
    let list = [], armed = null;
    const show = v => (o.show ? o.show(v) : v);
    // an address is data (not translated); a keyword chip (本地子网) is a word of the catalog
    const chip = v => {
      const word = o.isWord && o.isWord(v);
      return el('span', { class: `chip set-tok-i${word ? '' : ' mono'}`, 'data-key': v },
        el('span', { class: 'set-tok-v', translate: word ? null : 'no' }, show(v)),
        el('button', { class: 'chip-x', type: 'button', tabindex: '-1', 'aria-label': t('settings.chip.remove_aria', { v: show(v) }) }, icon('close', 12)));
    };
    function render(animate) {
      const put = () => { T.$$('.set-tok-i', box).forEach(x => x.remove()); inp.before(...list.map(chip)); paintItems(); };
      if (animate && visible) listFx(box, put, { stagger: 18 }); else put();
    }
    // separators: white space, , ; and the full-width \uff0c\uff1b\u3001 (written as escapes: no CJK literal in the source)
    const split = s => s.split(/[\s,;\uff0c\uff1b\u3001]+/).map(x => x.trim()).filter(Boolean);
    const check = text => { const acc = list.slice(); for (const p of split(text)) { const v = o.norm ? o.norm(p) : p; const e = o.validate(v, acc); if (e) return e; acc.push(v); } return null; };
    const typed = () => { if (o.onText) o.onText(); };
    // a list with withheld entries asks once before its first edit (o.guard)
    const guarded = fn => { const g = o.guard ? o.guard() : true; if (g === true) return fn(); g.then(ok => { if (ok) fn(); }); return true; };
    function add(text) {
      const good = [], bad = [];
      for (const p of split(text)) { const v = o.norm ? o.norm(p) : p; const e = o.validate(v, list.concat(good)); if (e) bad.push([p, e]); else good.push(v); }
      // the draft first, so the new chips carry their changed dot while the page measures
      if (good.length) { list = list.concat(good); o.onChange(list.slice()); render(true); }
      if (bad.length) { inp.value = bad.map(b => b[0]).join(' '); fieldNote(fieldEl, bad[0][1], true); typed(); return false; }
      inp.value = ''; fieldNote(fieldEl, o.hint, false); typed(); return true;
    }
    const addG = text => guarded(() => add(text));
    // Enter / leaving the field: the text as it is once the question (if any) is answered — a save
    // that asked the same question may have added it meanwhile (fix M1)
    const addLive = () => guarded(() => { if (inp.value.trim()) add(inp.value); });
    function remove(v) {
      list = list.filter(x => x !== v); disarm();
      o.onChange(list.slice()); render(true);
    }
    const removeG = v => guarded(() => remove(v));
    function arm(e) { disarm(); armed = e; e.classList.add('is-armed'); }
    function disarm() { if (armed) armed.classList.remove('is-armed'); armed = null; }
    inp.addEventListener('keydown', e => {
      if (e.key === 'Enter') { e.preventDefault(); if (inp.value.trim()) addLive(); }
      else if (e.key === 'Backspace' && !inp.value && list.length) {
        e.preventDefault();
        const lastChip = T.$$('.set-tok-i', box).pop();
        if (armed === lastChip) removeG(lastChip.dataset.key); else arm(lastChip);
      } else if (e.key === 'Escape' && armed) { e.stopPropagation(); disarm(); }
    });
    inp.addEventListener('input', () => {
      disarm(); typed();
      if (!fieldEl.classList.contains('is-invalid')) return;
      const e = inp.value.trim() ? check(inp.value) : null;
      fieldNote(fieldEl, e || o.hint, !!e);
    });
    inp.addEventListener('blur', () => { disarm(); if (inp.value.trim()) addLive(); });
    inp.addEventListener('paste', e => {
      const s = (e.clipboardData || window.clipboardData).getData('text');
      if (/[\s,;\uff0c\uff1b\u3001]/.test(s.trim())) { e.preventDefault(); addG(`${inp.value} ${s}`); }
    });
    box.addEventListener('click', e => {
      const x = e.target.closest('.chip-x');
      if (x) { removeG(x.closest('.set-tok-i').dataset.key); inp.focus(); return; }
      if (!e.target.closest('.set-tok-i')) inp.focus();
    });
    fieldNote(fieldEl, o.hint, false, true);
    return {
      set(l, animate) { list = (l || []).slice(); render(animate); inp.value = ''; disarm(); fieldNote(fieldEl, o.hint, false, !animate); typed(); },
      get: () => list.slice(),
      clear() { inp.value = ''; fieldNote(fieldEl, o.hint, false); typed(); },
      remote(msg) { if (msg) fieldNote(fieldEl, msg, true); else if (!inp.value.trim()) fieldNote(fieldEl, o.hint, false); },
      input: inp, field: fieldEl,
      // (T-P4-09: an empty field needs no layout read; a snapshot's paint asks every field)
      text: () => (inp.value && isShown(inp) ? inp.value.trim() : ''),
      value: () => { const s = inp.value && isShown(inp) ? inp.value.trim() : ''; return s && !check(s) ? split(s).map(x => (o.norm ? o.norm(x) : x)).join(' ') : ''; },
      commit: () => add(inp.value),
    };
  }
  /* a token box's typed text as a pending draft (see pendingField) */
  function pendTokens(k, tok, o) {
    return pendingField(k, Object.assign({ eff: 'instant', input: () => tok.input, at: () => tok.field, text: tok.text, value: tok.value, commit: tok.commit, reset: tok.clear }, o));
  }
  /* (fix M1) tok.commit stays the unguarded add: flushPending is synchronous and runs only after
     onSave asked guard() for every list it would touch (the o.list of each pending field) */

  /* ---- a numeric row (ports, Bastion minimum): the row's description line is its message ---- */
  function rowNumber(row, input, o) {
    const box = input.closest('.input');
    const num = T.ui.number(box, { min: o.min, max: o.max });
    const d = row.querySelector('.frow-d');
    // an error is { msg, short } (validate) or a sentence (error(text)); err keeps the last one
    let err = null, touched = false;
    function setErr(e) {
      if (typeof e === 'string') e = { msg: e, short: e };
      if ((e && e.msg) === (err && err.msg)) return; err = e;
      row.classList.toggle('is-invalid', !!e);
      box.classList.toggle('is-invalid', !!e);
      input.setAttribute('aria-invalid', String(!!e));
      note(d, e ? e.msg : o.hint(), !!e);
    }
    const check = () => { const e = o.validate(input.value); setErr(e); return e; };
    input.addEventListener('input', () => { if (touched || err) check(); o.onInput(input.value); });
    input.addEventListener('blur', () => { touched = true; check(); });
    note(d, o.hint(), false, true);
    return {
      run: check, error: setErr, get err() { return err; }, num,
      set(v) { input.value = v == null ? '' : v; touched = false; setErr(null); note(d, o.hint(), false); },
      hint() { if (!err) note(d, o.hint(), false, true); },
    };
  }

  function wireConn() {
    modeSws = {};
    SW_MODES.forEach(k => {
      const node = R.root.querySelector(`.switch[data-mode="${k}"]`);
      modeSws[k] = T.ui.switch(node, { checked: S.get('mode') != null && swOn(S.get('mode'), k), onChange: () => askSwitch(k) });
      node.addEventListener('click', () => { if (node.getAttribute('aria-disabled') === 'true') flashGate(); });
    });
    const bindSw = (k, sec, rowId, dep, lockSel) => {
      const sw = R.root.querySelector(`.switch[data-k="${k}"]`);
      K[k] = { sec, row: () => R.root.querySelector('#' + rowId), lock: () => (lockSel ? R.root.querySelector(lockSel) : sw),
        paint(v, animate) { sw.setAttribute('aria-checked', String(!!v)); if (dep) dep(!!v, animate); } };
      sw.addEventListener('change', e => { setD(k, e.detail.checked); if (dep) dep(e.detail.checked, true); });
    };
    bindSw('ks', 'set-safety', 'set-ks-row', ksDeps);
    K.ks.commit = () => paintKsLive(true);
    // switching the protection off is the one draft that asks first (it is what keeps the real IP hidden)
    T.ui.switch(R.root.querySelector('.switch[data-k="ks"]'), {
      onChange: v => v || !base.ks || T.confirm({ title: t('settings.conn.ks.confirm_off.title'), message: t('settings.conn.ks.confirm_off.body'), confirmLabel: t('settings.conn.ks.confirm_off.ok'), danger: true }),
    });
    bindSw('ksLan', 'set-safety', 'set-ksLan-row');
    bindSw('ksDns', 'set-safety', 'set-ksDns-row', (on, a) => reveal([[R.ksDnsWrap, on]], a));
    bindSw('autoExit', 'set-nodes', 'set-autoExit-row');
    // (the token boxes' lines carry only their errors: what goes in is said by the placeholder, the
    // other addresses' row by its description)
    const ksDnsTok = tokens(R.root.querySelector('#set-tok-ksdns'), { hint: '', validate: dnsIpErr, onChange: l => setD('ksDnsList', l), onText: () => pendKsDns(), guard: () => guard('ksDnsList') });
    TOKS.push(['ksDnsList', ksDnsTok]);
    K.ksDnsList = { sec: 'set-safety', row: () => R.root.querySelector('#set-ksDns-row'), lock: () => R.ksDnsWrap, hiddenNote: () => R.root.querySelector('#set-ksDnsList-hidden'), paint: (v, a) => ksDnsTok.set(v, a), remote: m => ksDnsTok.remote(m) };
    const ksExtraTok = tokens(R.root.querySelector('#set-tok-ksextra'), {
      hint: '',
      norm: v => ksKw(v) || v, show: kwShow, isWord: v => !!KW_IDS[v],
      validate: ksExtraErr, onChange: l => setD('ksExtra', l), onText: () => pendKsExtra(), guard: () => guard('ksExtra'),
    });
    TOKS.push(['ksExtra', ksExtraTok]);
    K.ksExtra = { sec: 'set-safety', row: () => R.root.querySelector('#set-ksExtra-row'), lock: () => R.root.querySelector('#set-tok-ksextra'), hiddenNote: () => R.root.querySelector('#set-ksExtra-hidden'), paint: (v, a) => ksExtraTok.set(v, a), remote: m => ksExtraTok.remote(m) };
    const pendKsDns = pendTokens('pend:ksDns', ksDnsTok, { sec: 'set-safety', list: 'ksDnsList', row: () => R.root.querySelector('#set-ksDns-row'), msg: () => t('settings.conn.ks.dns.pending') });
    const pendKsExtra = pendTokens('pend:ksExtra', ksExtraTok, { sec: 'set-safety', list: 'ksExtra', row: () => R.root.querySelector('#set-ksExtra-row'), msg: () => t('settings.conn.ks.extra.pending') });
    ['ks', 'ksLan', 'ksDns', 'autoExit', 'ksDnsList', 'ksExtra'].forEach(k => K[k].paint(draft[k], false));
    R.ksDnsWrap.hidden = !draft.ksDns;
    ksDeps(!!draft.ks, false);
  }

  /* ================================================================ 分流 */
  /* 配置整理 P2 (11-04, .build/配置可视化整理方案_20261104.md §3.2): the rules are ONE list in match
     order (routing.rule_list), each a sentence filled in: 当 [种类] [内容] 时 → [直连 | 代理 | 屏蔽].
     The kind is picked, or recognised from what is typed (a domain, an IP or network, AS13335,
     app.exe, a program path; a written prefix such as suffix: still works), so nothing is written in
     a syntax. A combination (同时满足 / 满足任一, each condition maybe 不满足) has its own small form.
     The list is model-first: every edit computes the next list from the draft (by id) and the DOM
     only animates it; a new rule goes to the top, dragging changes the order. A rule an earlier rule
     of another action already covers is marked 不会生效 (C.shadowed: only what the literals prove); a
     rule kept from the old layout that did not take effect is 未启用 (off) until an action is picked
     for it. Tesseract's own rules before and after the list are not shown (§0 3). Rule sets
     (routing.providers) are rows of name, content and address; a rule names one. */
  let ruleAct = 'proxy', ruleSeg = null, ruleCond = null, pendRule = () => {};
  let combo = null;                    // the combination form while it is open
  const liveRows = box => T.$$('.set-rule', box).filter(x => !x._ghost);
  /** a rule as messages name it: its action and its literal (直连 · suffix:example.cn) */
  const ruleName = r => `${actLabel(r.act)} · ${r.v}`;
  const ruleKey = r => (r.op ? `${r.op}|${r.of.map(c => `${c.not ? '!' : ''}${c.v.toLowerCase()}`).join('|')}` : r.raw ? `raw|${r.v}` : `${r.not ? '!' : ''}${r.v.toLowerCase()}`);
  /* the conflict guard as the page can tell it at once (routing_rules: the backend checks again, and
     also Tesseract's own hosts, which the page does not know) */
  const LOCAL_SUFFIX = /(^|\.)(localhost|local|lan|home\.arpa)$/i;
  const OWN_PROGRAM = /(^|[\\/])(tesseract|tesseract-runtime|mihomo)\.exe$/i;
  const portsOk = s => /^\d{1,5}(-\d{1,5})?(\/\d{1,5}(-\d{1,5})?)*$/.test(s)
    && s.split('/').every(p => { const [a, b] = p.split('-').map(Number); return a >= 1 && (b == null ? a : b) <= 65535 && a <= (b == null ? a : b); });
  /* a condition typed (or picked) → { v, kind, label, sub } | { err, label } | { empty }. kind 'auto'
     recognises it; act: the action it will get (a local-only condition is refused unless direct) */
  function parseCond(kind, raw, o) {
    o = o || {};
    const s = String(raw == null ? '' : raw).trim();
    if (!s) return { empty: true };
    const label = kind === 'auto' ? undefined : kindWord(kind);
    const E = (id, p) => ({ err: t(`settings.route.rule.err.${id}`, p), id, label });
    const ok = (k, v, extra) => Object.assign({ v: `${k}:${v}`, kind: k, label: kindWord(k) }, extra || {});
    const local = () => !o.not && o.act !== 'direct';
    if (kind === 'auto') {
      const m = s.match(/^([a-z][a-z0-9-]*):(?!\/\/)([\s\S]*)$/i);
      if (m && (C.KINDS.includes(m[1].toLowerCase()) || m[1].toLowerCase() === 'ip-cidr6') && !m[2].trim()) return E('empty', { p: m[1].toLowerCase(), ex: (PFX[m[1].toLowerCase()] || PFX['ip-cidr']).ex });
      if (m && (C.KINDS.includes(m[1].toLowerCase()) || m[1].toLowerCase() === 'ip-cidr6')) return parseCond(m[1].toLowerCase() === 'ip-cidr6' ? 'ip-cidr' : m[1].toLowerCase(), m[2], o);
      if (/^[a-z]:[\\/]/i.test(s) || (/[\\/]/.test(s) && /\.exe$/i.test(s))) return parseCond('process-path', s, o);
      if (m && !/^[0-9a-f]*$/i.test(m[1])) return E('prefix', { p: m[1] });
      if (/\s/.test(s)) return E('space', { v: s });
      if (/^as\d+$/i.test(s)) return parseCond('ip-asn', s, o);
      if ((s.includes(':') && /^[0-9a-f:]+(\/\d{1,3})?$/i.test(s) && s.split(':').length > 2) || /^[\d./]+$/.test(s)) return parseCond('ip-cidr', s, o);
      if (/^[^\\/:*?"<>|()]+\.exe$/i.test(s)) return parseCond('process', s, o);
      if (isDomain(s.replace(/^[+*]\./, ''))) return parseCond('suffix', s, o);
      return E('unknown', { v: s });
    }
    if (s.includes(',')) return E('comma');
    switch (kind) {
      case 'domain': case 'suffix': {
        const d = kind === 'suffix' ? s.replace(/^[+*]\./, '') : s;
        if (/\s/.test(d) || !isDomain(d)) return E('domain', { v: d, ex: PFX[kind].ex });
        if (local() && LOCAL_SUFFIX.test(d)) return E('local_only');
        return ok(kind, d.toLowerCase(), kind === 'suffix' ? { sub: t('settings.route.kind.sub_domains') } : null);
      }
      case 'keyword':
        return /^[a-z0-9._-]+$/i.test(s) ? ok(kind, s.toLowerCase()) : E('keyword');
      case 'regex': {
        let good = !/\(\?[=!<]|\\[1-9]/.test(s);
        try { new RegExp(s); } catch (e) { good = false; }
        return good ? ok(kind, s) : E('regex', { v: s });
      }
      case 'geosite': case 'geoip': {
        if (!/^[a-z0-9!@_-]+$/i.test(s)) return E('geo', { v: s, ex: PFX[kind].ex });
        if (local() && (kind === 'geoip' ? /^(private|lan)$/i : /^private$/i).test(s)) return E('local_only');
        const geo = GEO_SUB[`${kind}:${s}`.toLowerCase()];
        return ok(kind, s, geo ? { sub: t(`settings.route.geo.${geo}`) } : null);
      }
      case 'ip-cidr': case 'src-ip-cidr': {
        const r = ipRule(s);
        if (r.err) return Object.assign(r, { label });
        if (kind === 'ip-cidr' && local() && C.localNet(s)) return E('local_only');
        return ok(kind, s, kind === 'ip-cidr' ? { label: r.label } : null);
      }
      case 'ip-asn': {
        const m = s.match(/^(?:as)?(\d{1,10})$/i);
        return m && +m[1] >= 1 && +m[1] <= 4294967295 ? ok(kind, String(+m[1])) : E('asn', { v: s, ex: PFX[kind].ex });
      }
      case 'dst-port': case 'src-port':
        return portsOk(s) ? ok(kind, s) : E('port', { v: s, ex: PFX[kind].ex });
      case 'process':
        if (/[\\/:*?"<>|()]/.test(s)) return E('process', { v: s, ex: PFX.process.ex });
        return !o.not && OWN_PROGRAM.test(s) ? E('own_program') : ok(kind, s);
      case 'process-path':
        if (!/[\\/]/.test(s) || /[()]/.test(s) || s.length > 260) return E('path', { v: s, ex: PFX[kind].ex });
        return !o.not && OWN_PROGRAM.test(s) ? E('own_program') : ok(kind, s);
      case 'network':
        return /^(tcp|udp)$/i.test(s) ? ok(kind, s.toLowerCase()) : E('unknown', { v: s });
      case 'rule-set':
        return draft.ruleSets.some(x => x.name === s) ? ok(kind, s) : E('no_set');
      default:
        return E('unknown', { v: s });
    }
  }
  /* what the guard says of a row given this action: the first condition it refuses (a LAN address
     not direct, Tesseract's own programs, a rule set that is gone), or null. Only the guard: a
     stored value the field would write differently is not judged again. */
  const GUARD = new Set(['local_only', 'own_program', 'no_set']);
  function guardErr(r, act) {
    if (r.raw) return null;
    for (const c of r.op ? r.of : [r]) {
      const s = C.splitLit(c.v), p = s ? parseCond(s.kind, s.value, { not: c.not, act }) : {};
      if (p.err && GUARD.has(p.id)) return p.err;
    }
    return null;
  }
  /* the kind picker: 自动识别, then the kinds by what they look at */
  const KIND_GROUPS = [['site', ['suffix', 'domain', 'keyword', 'regex', 'geosite']], ['ip', ['ip-cidr', 'geoip', 'ip-asn', 'src-ip-cidr']],
    ['port', ['dst-port', 'src-port']], ['app', ['process', 'process-path']], ['other', ['network', 'rule-set']]];
  const kindOptions = () => [{ value: 'auto', label: t('settings.route.rule.kind_auto') },
    ...KIND_GROUPS.flatMap(([g, ks]) => [{ sep: true }, { group: t(`settings.route.kind.g.${g}`) }, ...ks.map(k => ({ value: k, label: kindWord(k) }))])];
  /* one condition's fields: the kind, then what is typed, or picked from a list (TCP / UDP, a rule
     set). The markup is built with the section; condWire gives it its controls. */
  // (a select's shown value made here keeps an id of the page's own: T.ui.select names it sv<n>)
  const selV = id => [el('span', { class: 'select-v', id: id || null }), icon('chevron-down', 15)];
  const condMarkup = (inputId, idp) => ({
    kindBtn: el('button', { class: 'select sm w-kind', id: idp ? `${idp}-kind` : null, 'aria-label': t('settings.route.when.kind_aria') }, ...selV(idp && `${idp}-kind-v`)),
    box: el('div', { class: 'input mono sm set-add-in' }, el('input', { id: inputId || null, autocomplete: 'off', spellcheck: 'false', 'aria-label': t('settings.route.rule.aria') })),
    pickBtn: el('button', { class: 'select sm w-val', id: idp ? `${idp}-pick` : null, hidden: true, 'aria-label': t('settings.route.rule.aria') }, ...selV(idp && `${idp}-pick-v`)),
  });
  const PICK_KINDS = ['network', 'rule-set'];
  function condWire(m, o) {
    o = o || {};
    let kind = o.kind || 'auto';
    const input = m.box.querySelector('input');
    const changed = () => { if (o.onChange) o.onChange(); };
    const pickOpts = () => (kind === 'network' ? ['tcp', 'udp'].map(v => ({ value: v, label: v.toUpperCase(), translate: 'no' }))
      : draft.ruleSets.map(x => ({ value: x.name, label: x.name, translate: 'no' })));
    const pSel = T.ui.select(m.pickBtn, { options: [], placeholder: t('settings.route.when.no_sets'), onChange: () => { setTimeout(changed, 0); } });
    const kSel = T.ui.select(m.kindBtn, { value: kind, options: kindOptions(), onChange: v => { kind = v; paint(); setTimeout(changed, 0); } });
    function paint() {
      const pick = PICK_KINDS.includes(kind);
      m.box.hidden = pick; m.pickBtn.hidden = !pick;
      if (pick) {
        const opts = pickOpts();
        pSel.setOptions(opts);
        if (!opts.some(x => x.value === pSel.get())) pSel.set(opts.length ? opts[0].value : null, false);
      }
      input.placeholder = kind === 'auto' ? t('settings.route.rule.placeholder', { a: 'example.com', b: '203.0.113.0/24' }) : PFX[kind].ex.replace(/^[a-z-]+:/, '');
    }
    input.addEventListener('input', changed);
    paint();
    return {
      get kind() { return kind; },
      setKind(v) { kind = v; kSel.set(v, false); paint(); },
      parse: x => parseCond(kind, PICK_KINDS.includes(kind) ? (pSel.get() || '') : input.value, x),
      typed: () => (PICK_KINDS.includes(kind) ? '' : input.value.trim()),
      reset() { input.value = ''; paint(); },
      refresh: paint,
      focus() { focusNow(PICK_KINDS.includes(kind) ? m.pickBtn : input); },
      input,
    };
  }
  /* a condition as the list shows it: 不满足, the kind's word, the content (data) */
  function condEl(c) {
    const s = C.splitLit(c.v);
    return el('span', { class: 'set-cond' }, c.not ? el('span', { class: 'set-cond-not' }, t('settings.route.combo.not')) : null,
      el('span', { class: 'set-cond-k' }, s ? kindWord(s.kind) : t('settings.route.kind.raw')), el('span', { class: 'mono', translate: 'no' }, s ? s.value : c.v));
  }
  const rawText = r => [r.v, ...(r.params || [])].join(',');
  /* the kind column of a row: what it is (and 不满足); a combination says how its conditions combine */
  function kindText(r) {
    if (r.op) return t(`settings.route.combo.${r.op === 'and' ? 'all' : 'any'}`);
    if (r.raw) return t('settings.route.kind.raw');
    const s = C.splitLit(r.v), p = s ? parseCond(s.kind, s.value, { not: r.not, act: 'direct' }) : {};
    return `${r.not ? `${t('settings.route.combo.not')} · ` : ''}${p.label || (s ? kindWord(s.kind) : '')}${p.sub ? ` · ${p.sub}` : ''}`;
  }
  function ruleRow(r) {
    const a = ACT[r.act], al = actLabel(r.act), stuck = r.off && r.note === 'unreadable';
    const val = r.op ? el('span', { class: 'set-rule-v set-rule-combo' }, ...r.of.flatMap((c, i) => [i ? el('span', { class: 'set-rule-sep', 'aria-hidden': 'true' }, '·') : null, condEl(c)]).filter(Boolean))
      : r.raw ? el('span', { class: 'c-mono c-ell set-rule-v', title: rawText(r), translate: 'no' }, rawText(r))
        : el('span', { class: 'c-mono c-ell set-rule-v', title: r.v, translate: 'no' }, (C.splitLit(r.v) || { value: r.v }).value);
    const marks = [];
    if (r.off) {
      const why = t(`settings.route.off.${r.note || 'kept'}`);
      marks.push(el('span', { class: 'set-rule-sh set-rule-off', 'data-tip': why }, icon('pause', 12), el('span', { 'aria-hidden': 'true' }, t('settings.route.off.label')), el('span', { class: 'sr-only' }, why)));
    }
    if (r.resolve) marks.push(el('span', { class: 'set-rule-res', 'data-tip': t('settings.route.row.resolve_tip') }, t('settings.route.row.resolve')));
    return el('div', { class: `li set-rule${r.off ? ' is-off' : ''}`, 'data-key': r.id },
      el('button', { class: 'grip', type: 'button', 'aria-label': t('settings.route.rule.grip_aria'), 'data-tip': t('settings.route.rule.grip_tip'), 'data-tip-kbd': 'Alt ↑ ↓' }, icon('grip', 14)),
      /* (T-P5-11) the chip is as wide as the widest of the three action words in this language: the
         other two sit under the shown one, invisible (.set-act-z), so every row's chip has the same
         width and the value column stays lined up (the words are never cut: de Blockieren, ru
         Напрямую) */
      el('button', { class: 'chip sm chip-btn set-act', type: 'button', 'data-act': '', 'aria-haspopup': 'menu', disabled: stuck || null, 'aria-label': t('settings.route.act.chip_aria', { action: al }) }, icon(a.icon, 13),
        el('span', { class: 'set-act-g' }, el('span', { class: 'set-act-l' }, al), ...ACTS.filter(x => x !== r.act).map(x => el('span', { class: 'set-act-z', 'aria-hidden': 'true' }, actLabel(x))))),
      val,
      el('span', { class: 'set-rule-k' }, el('span', { class: 'set-rule-kt' }, kindText(r)), ...marks),
      el('span', { class: 'li-act' }, el('button', { class: 'icon-btn sm', type: 'button', 'data-del': '', 'aria-label': t('settings.route.rule.delete_aria', { v: r.op ? kindText(r) : r.raw ? rawText(r) : (C.splitLit(r.v) || { value: r.v }).value, kind: kindText(r) }), 'data-tip': t('settings.action.delete') }, icon('trash', 14))));
  }
  /* 不会生效 on a row an earlier rule of another action covers; the covering rule in its tooltip
     (and, for a screen reader, in the mark's own words) */
  function paintShadows(list) {
    if (!R.rlCustom) return;
    const sh = C.shadowed(list || draft.rules);
    liveRows(R.rlCustom).forEach(row => {
      const by = sh.get(row.dataset.key), k = row.querySelector('.set-rule-k');
      let m = k && k.querySelector('.set-rule-sh:not(.set-rule-off)');
      row.classList.toggle('is-shadowed', !!by);
      if (!by) { if (m) m.remove(); return; }
      const why = t('settings.route.shadow.tip', { rule: ruleName(by) });
      if (!m) { m = el('span', { class: 'set-rule-sh' }, icon('warning', 12), el('span', { class: 'set-rule-shl', 'aria-hidden': 'true' }), el('span', { class: 'sr-only' })); k.append(m); }
      m.setAttribute('data-tip', why);
      m.querySelector('.set-rule-shl').textContent = t('settings.route.shadow.label');
      m.querySelector('.sr-only').textContent = why;
    });
  }
  /* the count and the empty state follow the list they are given (the model) */
  function paintRuleMeta(list, animate) {
    M.swapText(R.rlCm, t('settings.route.custom.count', { n: list.length }), { instant: !animate || !visible });
    reveal([[R.rlEmpty, !list.length]], animate);
  }
  function renderRules(list, animate) {
    const put = () => { T.$$('.set-rule', R.rlCustom).forEach(x => x.remove()); R.rlCustom.append(...list.map(ruleRow)); paintItems(); paintShadows(list); };
    if (animate && visible) listFx(R.rlCustom, put, { after: () => paintRuleMeta(list, true) });
    else { put(); paintRuleMeta(list, animate); }
  }
  function setRules(list) { setD('rules', list); paintRuleMeta(draft.rules, true); paintShadows(); }
  function findRule(r) {
    const k = ruleKey(r), i = draft.rules.findIndex(x => ruleKey(x) === k);
    return i < 0 ? null : { r: draft.rules[i], n: i + 1 };
  }
  /* the add field's line when nothing is wrong: empty (F-04: no note on what a direct rule means) */
  function ruleHint(instant) { fieldNote(R.ruleF, '', false, instant); }
  /** a new row at the top of the list (shown with the add field's flourish); its cover said under the field */
  function putRule(r, from, at) {
    const next = [r].concat(draft.rules);
    listFx(R.rlCustom, () => R.rlCustom.prepend(ruleRow(r)), { highlight: true, from: visible ? from : null, before: () => reveal([[R.rlEmpty, false]], true) });
    setRules(next);
    const by = C.shadowed(next).get(r.id);
    if (by) fieldNote(at, t('settings.route.shadow.tip', { rule: ruleName(by) }), false);
    return true;
  }
  function dupNote(at, dup) {
    fieldNote(at, t('settings.route.rule.duplicate', { v: dup.r.op ? kindText(dup.r) : dup.r.v, n: dup.n, action: actLabel(dup.r.act) }), true);
    const row = R.root.querySelector(`.set-rule[data-key="${dup.r.id}"]`);
    if (row && visible) { row.classList.remove('is-new'); void row.offsetWidth; row.classList.add('is-new'); setTimeout(() => row.classList.remove('is-new'), 1600); }
  }
  /* → true when added (or nothing was typed), false when the field now says why not */
  function addRule() {
    const p = ruleCond.parse({ act: ruleAct });
    if (p.empty) return true;
    if (p.err) { fieldNote(R.ruleF, p.err, true); return false; }
    const r = { id: uid('r'), act: ruleAct, v: p.v };
    const dup = findRule(r);
    if (dup) { dupNote(R.ruleF, dup); return false; }
    ruleCond.reset(); ruleHint();
    putRule(r, R.ruleIn.closest('.set-when'), R.ruleF);
    pendRule();
    return true;
  }
  const addRuleNow = () => { if (!ruleCond.typed() && !PICK_KINDS.includes(ruleCond.kind)) { ruleCond.focus(); return; } guard('rules').then(ok => { if (ok) addRule(); }); };

  /* ---- 组合条件: [同时满足 | 满足任一], two to eight conditions, each maybe 不满足 ---- */
  function comboItem(item) {
    const m = condMarkup(null, null), notBtn = el('button', { class: 'chip sm chip-btn set-combo-not', type: 'button', 'aria-pressed': 'false' }, t('settings.route.combo.not'));
    const del = el('button', { class: 'icon-btn sm', type: 'button', 'data-tip': t('settings.action.delete') }, icon('close', 14));
    const node = el('div', { class: 'set-combo-i' }, notBtn, m.kindBtn, m.box, m.pickBtn, del);
    const cond = condWire(m, { onChange: () => fieldNote(combo.f, '', false) });
    notBtn.addEventListener('click', () => { item.not = !item.not; notBtn.setAttribute('aria-pressed', String(item.not)); fieldNote(combo.f, '', false); });
    del.addEventListener('click', () => { if (combo.items.length <= 2) return; combo.items = combo.items.filter(x => x !== item); listFx(combo.list, () => node.remove()); paintCombo(); });
    Object.assign(item, { node, cond, del });
    return node;
  }
  function paintCombo() {
    if (!combo) return;
    combo.items.forEach((x, i) => {
      x.del.hidden = combo.items.length <= 2;
      x.del.setAttribute('aria-label', t('settings.route.combo.remove_aria', { i: i + 1 }));
      x.node.setAttribute('aria-label', t('settings.route.combo.item_aria', { i: i + 1 }));
    });
    combo.more.hidden = combo.items.length >= 8;
  }
  function comboOpen() {
    if (combo) { focusNow(combo.items[0].cond.input); return; }
    const list = el('div', { class: 'set-combo-l', role: 'group' });
    const opSeg = el('span', { class: 'seg', 'aria-label': t('settings.route.combo.op_aria') }, el('button', { class: 'seg-btn', 'data-v': 'and' }, t('settings.route.combo.all')), el('button', { class: 'seg-btn', 'data-v': 'or' }, t('settings.route.combo.any')));
    const actSeg = el('span', { class: 'seg', 'aria-label': t('settings.route.act.aria') }, ...ACTS.map(a => el('button', { class: 'seg-btn', 'data-v': a }, icon(ACT[a].icon, 14), actLabel(a))));
    const more = el('button', { class: 'link-btn', type: 'button' }, icon('plus', 14), t('settings.route.combo.add_item'));
    const cancel = el('button', { class: 'btn btn-sm btn-ghost', type: 'button' }, t('settings.action.cancel'));
    const add = el('button', { class: 'btn btn-sm', type: 'button' }, el('span', { class: 'bi' }, icon('plus', 15)), el('span', { class: 'lab' }, t('settings.route.combo.add')));
    const f = el('div', { class: 'field set-combo-f' }, el('div', { class: 'field-msg' }));
    R.combo.replaceChildren(el('div', { class: 'set-combo-h' }, opSeg), list, el('div', { class: 'set-combo-more' }, more),
      el('div', { class: 'set-combo-a' }, el('span', { class: 'set-when-w' }, t('settings.route.when.then')), actSeg, el('span', { class: 'spacer' }), cancel, add), f);
    combo = { op: 'and', act: ruleAct, items: [{ not: false }, { not: false }], list, more, f };
    T.ui.seg(opSeg, { value: 'and', onChange: v => { combo.op = v; } });
    T.ui.seg(actSeg, { value: combo.act, onChange: v => { combo.act = v; fieldNote(f, '', false); } });
    list.append(...combo.items.map(comboItem));
    more.addEventListener('click', () => { const it = { not: false }; combo.items.push(it); listFx(list, () => list.append(comboItem(it))); paintCombo(); it.cond.focus(); });
    cancel.addEventListener('click', comboClose);
    add.addEventListener('click', () => guard('rules').then(ok => { if (ok) comboAdd(); }));
    paintCombo();
    R.comboBtn.setAttribute('aria-expanded', 'true');
    reveal([[R.combo, true]], true);
    focusNow(combo.items[0].cond.input);
  }
  function comboClose() {
    if (!combo) return;
    combo = null;
    R.comboBtn.setAttribute('aria-expanded', 'false');
    reveal([[R.combo, false]], true);
    focusNow(R.comboBtn);
  }
  function comboAdd() {
    if (!combo) return;
    const of = [];
    for (const [i, x] of combo.items.entries()) {
      const p = x.cond.parse({ not: x.not, act: combo.act });
      if (p.empty) continue;
      if (p.err) { fieldNote(combo.f, t('settings.route.combo.item_err', { i: i + 1, reason: p.err }), true); x.cond.focus(); return; }
      of.push(Object.assign({ v: p.v }, x.not ? { not: true } : {}));
    }
    if (of.length < 2) { fieldNote(combo.f, t('settings.route.combo.need_two'), true); return; }
    // (a combination of local-only conditions with another action never matches: the backend says so too)
    const r = { id: uid('r'), act: combo.act, op: combo.op, of };
    const dup = findRule(r);
    if (dup) { dupNote(combo.f, dup); return; }
    putRule(r, R.combo, R.ruleF);
    comboClose();
  }

  /* ---- 规则集: name, content (网站 / IP / 混合规则), address; downloaded through the proxy ---- */
  let setBehav = 'domain', behavSel = null, pendSet = () => {};
  const behavLabel = b => t(`settings.route.sets.b.${b}`);
  const setKinds = () => [...C.SET_BEHAVIORS.map(behavLabel), t('settings.route.sets.local')];
  function setRow(s) {
    const local = s.type && s.type !== 'http';
    return el('div', { class: 'li set-rule set-set-r', 'data-key': s.id },
      el('span', { class: 'set-set-n c-ell', translate: 'no', title: s.name }, s.name),
      kindChip('set-set-t', local ? t('settings.route.sets.local') : behavLabel(s.behavior || 'classical'), setKinds()),
      s.url && !s.hidden ? el('span', { class: 'c-mono c-ell set-set-u', title: s.url, translate: 'no' }, s.url)
        : el('span', { class: 'set-set-u set-set-hid' }, local ? '' : t('settings.route.sets.url_hidden')),
      el('span', { class: 'li-act' }, el('button', { class: 'icon-btn sm', type: 'button', 'data-del': '', 'aria-label': t('settings.route.sets.delete_aria', { v: s.name }), 'data-tip': t('settings.action.delete') }, icon('trash', 14))));
  }
  function renderSets(list, animate) {
    const put = () => { T.$$('.set-set-r', R.setList).forEach(x => x.remove()); R.setList.append(...list.map(setRow)); paintItems(); };
    if (animate && visible) listFx(R.setList, put); else put();
    M.swapText(R.setM, list.length ? t('settings.route.sets.count', { n: list.length }) : '', { instant: !animate || !visible });
    reveal([[R.setList, list.length > 0]], animate);
    refreshPicks();
  }
  /* the conditions that pick a rule set follow the list */
  function refreshPicks() {
    if (ruleCond) ruleCond.refresh();
    if (combo) combo.items.forEach(x => x.cond && x.cond.refresh());
  }
  const setNameErr = (v, list) => {
    if (!v) return t('settings.route.sets.err.name_empty');
    if (/[,()\r\n\t]/.test(v) || v.length > 40) return t('settings.route.sets.err.name');
    return list.some(x => x.name.toLowerCase() === v.toLowerCase()) ? dupErr(v) : null;
  };
  function setUrlErr(v) {
    if (!webUrl(v)) return t('settings.route.sets.err.url');
    const u = new URL(v);
    return u.username || u.password || u.search || u.hash ? t('settings.route.sets.err.url') : null;
  }
  const setFormat = url => (/\.mrs$/i.test(new URL(url).pathname) ? 'mrs' : /\.(txt|list|conf)$/i.test(new URL(url).pathname) ? 'text' : 'yaml');
  /* → true when added (or nothing was typed), false when the field now says why not */
  function addSet() {
    const name = R.setName.value.trim(), url = R.setUrl.value.trim();
    if (!name && !url) return true;
    const e = setNameErr(name, draft.ruleSets) || setUrlErr(url) || (setFormat(url) === 'mrs' && setBehav === 'classical' ? t('settings.route.sets.err.mrs') : null);
    if (e) { fieldNote(R.setF, e, true); return false; }
    const s = { id: uid('s'), name, url, behavior: setBehav, format: setFormat(url), type: 'http' };
    const list = draft.ruleSets.concat([s]);
    setD('ruleSets', list); renderSets(list, true);
    const row = R.setList.querySelector(`[data-key="${s.id}"]`);
    if (row && visible) { row.classList.add('is-new'); setTimeout(() => row.classList.remove('is-new'), 1600); }
    R.setName.value = ''; R.setUrl.value = ''; fieldNote(R.setF, '', false);
    pendSet();
    return true;
  }
  const addSetNow = () => { if (!R.setName.value.trim() && !R.setUrl.value.trim()) { R.setName.focus(); return; } addSet(); };
  function delSet(row) {
    const s = draft.ruleSets.find(x => x.id === row.dataset.key);
    if (!s) return;
    // a rule set a rule still uses stays (the backend refuses it too: errors.settings.rule_set_in_use)
    if (C.setsInUse(draft.rules).has(s.name)) { fieldNote(R.setF, t('settings.route.sets.err.in_use', { v: s.name }), true); return; }
    const list = draft.ruleSets.filter(x => x.id !== s.id);
    listFx(R.setList, () => row.remove());
    setD('ruleSets', list);
    renderSets(list, true);
  }

  function wireRoute() {
    // (the action decides what the guard allows, a LAN address only direct: a note shown is checked again)
    ruleSeg = T.ui.seg(R.root.querySelector('#set-rule-act'), { value: ruleAct, onChange: v => {
      ruleAct = v;
      const p = R.ruleF.classList.contains('is-invalid') ? ruleCond.parse({ act: ruleAct }) : {};
      if (p.err) fieldNote(R.ruleF, p.err, true); else ruleHint();
    } });
    ruleCond = condWire({ kindBtn: R.root.querySelector('#set-rule-kind'), box: R.ruleIn.closest('.input'), pickBtn: R.root.querySelector('#set-rule-pick') }, {
      onChange: () => {
        pendRule();
        if (R.ruleF.classList.contains('is-invalid')) { const p = ruleCond.parse({ act: ruleAct }); if (p.err) fieldNote(R.ruleF, p.err, true); else ruleHint(); }
      },
    });
    R.ruleIn.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); addRuleNow(); } });
    R.root.querySelector('#set-rule-add').addEventListener('click', addRuleNow);
    R.comboBtn.addEventListener('click', comboOpen);
    R.root.querySelector('#set-geo-link').addEventListener('click', () => { const row = R.root.querySelector('#set-geo-row'); afterScroll(glideTo(row, 120), () => wash(row)); });
    // after a drag: the new order of the live rows, mapped back onto the draft by id
    T.ui.sortable(R.rlCustom, { items: '.set-rule', onEnd: () => {
      guard('rules').then(ok => {
        if (!ok) { renderRules(draft.rules, false); return; }
        const by = new Map(draft.rules.map(r => [r.id, r]));
        setRules(liveRows(R.rlCustom).map(x => by.get(x.dataset.key)).filter(Boolean));
      });
    } });
    R.rlCustom.addEventListener('click', e => {
      const del = e.target.closest('[data-del]');
      if (del) { guard('rules').then(ok => { if (ok) delRule(del.closest('.set-rule')); }); return; }
      const a = e.target.closest('[data-act]');
      if (a && !a.disabled) pickAct(a);
    });
    R.rlCustom.addEventListener('keydown', e => {
      if ((e.key === 'Delete' || e.key === 'Backspace') && e.target.closest('.grip')) {
        e.preventDefault();
        const row = e.target.closest('.set-rule');
        guard('rules').then(ok => { if (ok) delRule(row); });
      }
    });
    K.rules = { sec: 'set-route', row: () => R.root.querySelector('#set-rules-row'), lock: () => R.root.querySelector('#set-rules-row'), hiddenNote: () => R.root.querySelector('#set-rules-hidden'), paint: (v, a) => renderRules(v, a), remote: m => { if (m) fieldNote(R.ruleF, m, true); } };
    pendRule = pendingField('pend:rule', { sec: 'set-route', eff: 'reload', list: 'rules',
      row: () => R.root.querySelector('#set-rules-row'), at: () => R.ruleF, input: () => R.ruleIn,
      text: () => ruleCond.typed(),
      value: () => { const p = ruleCond.parse({ act: ruleAct }); return p.v && !findRule({ v: p.v }) ? p.v : ''; },
      commit: addRule, reset: () => { ruleCond.reset(); ruleHint(); },
      msg: () => t('settings.route.rule.pending') });
    // 规则集
    behavSel = T.ui.select(R.root.querySelector('#set-set-behav'), { value: setBehav, options: C.SET_BEHAVIORS.map(b => ({ value: b, label: behavLabel(b) })), onChange: v => { setBehav = v; } });
    [R.setName, R.setUrl].forEach(i => {
      i.addEventListener('input', () => { pendSet(); if (R.setF.classList.contains('is-invalid')) fieldNote(R.setF, '', false); });
      i.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); addSetNow(); } });
    });
    R.root.querySelector('#set-set-add').addEventListener('click', addSetNow);
    R.setList.addEventListener('click', e => { const del = e.target.closest('[data-del]'); if (del) delSet(del.closest('.set-set-r')); });
    K.ruleSets = { sec: 'set-route', row: () => R.root.querySelector('#set-sets-row'), lock: () => R.root.querySelector('#set-sets-row'), paint: (v, a) => renderSets(v, a), remote: m => { if (m) fieldNote(R.setF, m, true); } };
    pendSet = pendingField('pend:set', { sec: 'set-route', eff: 'reload',
      row: () => R.root.querySelector('#set-sets-row'), at: () => R.setF, input: () => (R.setName.value.trim() ? R.setUrl : R.setName),
      text: () => `${R.setName.value.trim()}${R.setUrl.value.trim()}`,
      value: () => `${R.setName.value.trim()}\u0000${R.setUrl.value.trim()}`.replace(/^\u0000$/, ''),
      commit: addSet, reset: () => { R.setName.value = ''; R.setUrl.value = ''; fieldNote(R.setF, '', false); },
      msg: () => t('settings.route.sets.pending') });
    const bypass = tokens(R.root.querySelector('#set-tok-bypass'), { hint: '', validate: bypassErr, onChange: l => setD('bypass', l), onText: () => pendBypass(), guard: () => guard('bypass') });
    TOKS.push(['bypass', bypass]);
    K.bypass = { sec: 'set-conn', row: () => R.root.querySelector('#set-bypass-row'), lock: () => R.root.querySelector('#set-tok-bypass'), hiddenNote: () => R.root.querySelector('#set-bypass-hidden'), paint: (v, a) => bypass.set(v, a), remote: m => bypass.remote(m) };
    const pendBypass = pendTokens('pend:bypass', bypass, { sec: 'set-conn', list: 'bypass', row: () => R.root.querySelector('#set-bypass-row'), msg: () => t('settings.route.bypass.pending') });
    bypass.set(draft.bypass);
    renderSets(draft.ruleSets, false);
    renderRules(draft.rules, false);
    ruleHint(true);
  }
  function delRule(row) {
    // (a list painted again while the question was open: the same rule's new row)
    if (row && !row.isConnected) row = liveRows(R.rlCustom).find(x => x.dataset.key === row.dataset.key) || null;
    if (!row) return;
    const id = row.dataset.key, rows = liveRows(R.rlCustom), i = rows.indexOf(row);
    const had = row.contains(document.activeElement);
    const nb = rows[i + 1] || rows[i - 1];
    const list = draft.rules.filter(r => r.id !== id);        // the model decides; the DOM only animates
    listFx(R.rlCustom, () => row.remove(), { after: () => paintRuleMeta(list, true) });
    setRules(list);
    // focus stays in the list: the next row's handle, or the add field once the list is empty
    if (had) focusNow(nb ? nb.querySelector('.grip') : R.ruleIn);
  }
  /* the action chip: another action for this rule (it keeps its place); a rule kept off takes effect
     once an action is picked for it */
  function pickAct(btn) {
    const id = btn.closest('.set-rule').dataset.key, r = draft.rules.find(x => x.id === id);
    if (!r) return;
    T.ui.menu(btn, ACTS.map(a => ({ label: actLabel(a), value: a, icon: ACT[a].icon, checked: !r.off && r.act === a })), { placement: 'bottom-start', minWidth: 168 })
      .then(it => {
        const cur = draft.rules.find(x => x.id === id);
        if (!it || !cur || (it.value === cur.act && !cur.off) || !btn.isConnected) return;
        guard('rules').then(ok => {
          if (!ok) return;
          // (the list may have changed while the question was open)
          const now = draft.rules.find(x => x.id === id);
          if (!now || (it.value === now.act && !now.off)) return;
          const why = guardErr(now, it.value);
          if (why) { fieldNote(R.ruleF, why, true); return; }
          const changed = Object.assign({}, now, { act: it.value });
          delete changed.off; delete changed.note;
          const next = draft.rules.map(x => (x.id === id ? changed : x));
          setD('rules', next); renderRules(next, true);
        });
      });
  }

  /* ================================================================ 网络 */
  let dnsSel = null, resSel = null, resType = 'main';
  function resRow(r) {
    return el('div', { class: 'li set-rule set-res-r', 'data-key': r.id }, kindChip('set-res-t', t(`settings.net.res.${r.t}.label`), Object.keys(RES_T).map(k => t(`settings.net.res.${k}.label`))),
      el('span', { class: 'c-mono c-ell', title: r.v, translate: 'no' }, r.v), el('span', { class: 'set-rule-k' }, t(`settings.net.res.${r.t}.role`)),
      el('span', { class: 'li-act' }, el('button', { class: 'icon-btn sm', type: 'button', 'data-del': '', 'aria-label': t('settings.net.res.delete_aria', { v: r.v }), 'data-tip': t('settings.action.delete') }, icon('trash', 14))));
  }
  const RES_ORDER = { main: 0, node: 1, boot: 2 };
  function renderRes(list, animate) {
    const sorted = list.slice().sort((a, b) => RES_ORDER[a.t] - RES_ORDER[b.t]);
    const put = () => { T.$$('.set-res-r', R.resList).forEach(x => x.remove()); R.resList.append(...sorted.map(resRow)); paintItems(); };
    if (animate && visible) listFx(R.resList, put, { after: () => paintResReq(animate) });
    else { put(); paintResReq(animate); }
  }
  /* the required resolver kinds that are missing: [] · ['main'] · ['node'] · ['main', 'node'] */
  function resMissing(list) { const m = []; if (!list.some(r => r.t === 'main')) m.push('main'); if (!list.some(r => r.t === 'node')) m.push('node'); return m; }
  const missWhich = miss => (miss.length > 1 ? 'both' : miss[0]);
  function paintResReq(animate) {
    const miss = resMissing(draft.dnsList);
    const mk = rt => { const lab = t(`settings.net.res.${rt}.option`); return draft.dnsList.some(r => r.t === rt) ? el('span', { class: 'st st-ok' }, lab) : el('span', { class: 'st st-down' }, t('settings.net.res.missing', { kind: lab })); };
    const n = [el('span', { class: 'set-res-req-l' }, t('settings.net.res.required')), mk('main'), mk('node')];
    R.resReq.classList.toggle('is-bad', miss.length > 0);
    const key = n.map(x => x.textContent).join('|');
    if (R.resReq._h === key) return; R.resReq._h = key;
    if (animate && visible) M.anim(R.resReq, [{ opacity: 0.4 }, { opacity: 1 }], { duration: 200, easing: M.ease.out });
    R.resReq.replaceChildren(...n);
  }
  let pendRes = () => {};
  /* → true when added (or nothing was typed), false when the field now says why not */
  function addRes() {
    const v = R.resIn.value.trim();
    if (!v) return true;
    const e = resErr(resType, v, draft.dnsList);
    if (e) { fieldNote(R.resF, e, true); return false; }
    const r = { id: uid('d'), t: resType, v };
    const list = draft.dnsList.concat([r]);
    setD('dnsList', list); renderRes(list, true);
    const row = R.resList.querySelector(`[data-key="${r.id}"]`);
    if (row && visible) { row.classList.add('is-new'); setTimeout(() => row.classList.remove('is-new'), 1600); }
    R.resIn.value = ''; fieldNote(R.resF, '', false);
    pendRes();
    return true;
  }
  const addResNow = () => { if (!R.resIn.value.trim()) { R.resIn.focus(); return; } guard('dnsList').then(ok => { if (ok) addRes(); }); };
  function wireNet() {
    const bindSw = (k, sec, dep) => {
      const sw = R.root.querySelector(`.switch[data-k="${k}"]`);
      K[k] = { sec, row: () => R.root.querySelector(`#set-${k}-row`), lock: () => sw, paint(v, a) { sw.setAttribute('aria-checked', String(!!v)); if (dep) dep(!!v, a); } };
      sw.addEventListener('change', e => { setD(k, e.detail.checked); if (dep) dep(e.detail.checked, true); });
    };
    bindSw('lan', 'set-conn');
    bindSw('ipv6', 'set-conn');
    bindSw('dnsRules', 'set-dns');
    bindSw('egress', 'set-nodes', (on, a) => reveal([[R.egressSub, on]], a));
    const er = R.root.querySelector('.switch[data-k="egressRetry"]');
    K.egressRetry = { sec: 'set-nodes', row: () => R.root.querySelector('#set-egressRetry-row'), lock: () => er, paint(v) { er.setAttribute('aria-checked', String(!!v)); } };
    er.addEventListener('change', e => setD('egressRetry', e.detail.checked));
    // ports: 1–65535, and never the same port twice
    const num = v => (/^\d+$/.test(String(v).trim()) ? +v : String(v));
    const pm = rowNumber(R.root.querySelector('#set-portMixed-row'), R.root.querySelector('#set-port-mixed'), {
      min: 1, max: 65535, hint: () => t('settings.net.port_mixed.hint', { def: '7890' }),
      validate: v => portErr(v, draft.portSub, 'sub'),
      onInput: v => { setD('portMixed', num(v)); if (ps.err) ps.run(); },
    });
    const ps = rowNumber(R.root.querySelector('#set-portSub-row'), R.root.querySelector('#set-port-sub'), {
      min: 1, max: 65535, hint: () => t('settings.net.port_sub.hint', { def: '25500' }),
      validate: v => portErr(v, draft.portMixed, 'mixed'),
      onInput: v => { setD('portSub', num(v)); if (pm.err) pm.run(); },
    });
    R.portMixedCtl = pm;
    // a port conflict reported by the save lands on its row (Error.field, B-26(3))
    K.portMixed = { sec: 'set-conn', row: () => R.root.querySelector('#set-portMixed-row'), lock: () => R.root.querySelector('#set-port-mixed').closest('.input'), paint: v => pm.set(v), remote: m => { if (m) pm.error(m); else pm.hint(); } };
    K.portSub = { sec: 'set-nodes', row: () => R.root.querySelector('#set-portSub-row'), lock: () => R.root.querySelector('#set-port-sub').closest('.input'), paint: v => ps.set(v), remote: m => { if (m) ps.error(m); else ps.hint(); } };
    CHECKS.push(() => (editable('portMixed') && pm.run() ? { row: R.root.querySelector('#set-portMixed-row'), focus: R.root.querySelector('#set-port-mixed'), msg: t('settings.net.port_mixed.problem', { reason: pm.err.short }) } : null));
    CHECKS.push(() => (editable('portSub') && ps.run() ? { row: R.root.querySelector('#set-portSub-row'), focus: R.root.querySelector('#set-port-sub'), msg: t('settings.net.port_sub.problem', { reason: ps.err.short }) } : null));
    // DNS
    dnsSel = T.ui.select(R.root.querySelector('#set-dns-mode'), {
      value: draft.dnsMode, options: DNS_MODES.map(m => ({ value: m, label: t(`settings.net.dns.mode.${m}.label`) })),
      onChange: v => { setD('dnsMode', v); reveal([[R.dnsCustom, v === 'custom']], true); pendRes(); },
    });
    K.dnsMode = { sec: 'set-dns', row: () => R.root.querySelector('#set-dns-row'), lock: () => R.root.querySelector('#set-dns-mode'),
      paint: (v, a) => { dnsSel.set(v, a); reveal([[R.dnsCustom, v === 'custom']], a); pendRes(); } };
    resSel = T.ui.select(R.root.querySelector('#set-res-type'), {
      value: resType, options: Object.keys(RES_T).map(rt => ({ value: rt, label: t(`settings.net.res.${rt}.option`) })),
      onChange: v => { resType = v; R.resIn.placeholder = RES_T[v].ph; fieldNote(R.resF, '', false); pendRes(); },
    });
    R.resIn.placeholder = RES_T[resType].ph;
    fieldNote(R.resF, '', false, true);
    R.resIn.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); addResNow(); } });
    R.resIn.addEventListener('input', () => { pendRes(); if (R.resF.classList.contains('is-invalid')) { const e = R.resIn.value.trim() ? resErr(resType, R.resIn.value.trim(), draft.dnsList) : null; fieldNote(R.resF, e || '', !!e); } });
    R.root.querySelector('#set-res-add').addEventListener('click', addResNow);
    R.resList.addEventListener('click', e => {
      const d = e.target.closest('[data-del]'); if (!d) return;
      guard('dnsList').then(ok => {
        if (!ok) return;
        const row = d.closest('.set-res-r'), id = row.dataset.key, had = row.contains(document.activeElement);
        const rows = T.$$('.set-res-r', R.resList).filter(x => !x._ghost), i = rows.indexOf(row), nb = rows[i + 1] || rows[i - 1];
        const list = draft.dnsList.filter(r => r.id !== id);
        setD('dnsList', list); renderRes(list, true);
        if (had) focusNow(nb ? R.resList.querySelector(`.set-res-r[data-key="${nb.dataset.key}"]:not([aria-hidden]) [data-del]`) : R.resIn);
      });
    });
    K.dnsList = { sec: 'set-dns', eff: () => (draft.dnsMode === 'custom' ? 'reload' : 'instant'), row: () => R.root.querySelector('#set-dns-row'), lock: () => R.root.querySelector('#set-dns-custom .set-res'), hiddenNote: () => R.root.querySelector('#set-dnsList-hidden'), paint: (v, a) => renderRes(v, a), remote: m => { if (m) fieldNote(R.resF, m, true); } };
    // the resolver editor only exists in 自定义 mode: text left in it while hidden is not pending
    const resTyped = () => (R.dnsCustom.hidden ? '' : R.resIn.value.trim());
    pendRes = pendingField('pend:res', { sec: 'set-dns', list: 'dnsList', eff: () => effOf('dnsList'),
      row: () => R.root.querySelector('#set-dns-row'), at: () => R.resF, input: () => R.resIn, text: resTyped,
      value: () => { const v = resTyped(); return v && !resErr(resType, v, draft.dnsList) ? `${resType} ${v}` : ''; },
      commit: addRes, reset: () => { R.resIn.value = ''; fieldNote(R.resF, '', false); },
      msg: () => t('settings.net.res.pending') });
    CHECKS.push(() => {
      if (draft.dnsMode !== 'custom' || same(draft.dnsList, base.dnsList) && same(draft.dnsMode, base.dnsMode)) return null;
      const miss = resMissing(draft.dnsList); if (!miss.length) return null;
      const which = missWhich(miss);
      return { row: R.root.querySelector('#set-dns-row'), focus: R.resIn, at: R.resF, msg: t('settings.net.res.missing_save', { which }),
        show: () => { fieldNote(R.resF, t('settings.net.res.missing_field', { which }), true); if (miss[0] === 'node' && resType !== 'node') { resType = 'node'; resSel.set('node'); R.resIn.placeholder = RES_T.node.ph; } } };
    });
    ['lan', 'ipv6', 'dnsRules', 'egress', 'egressRetry', 'portMixed', 'portSub', 'dnsMode', 'dnsList'].forEach(k => K[k].paint(draft[k], false));
    R.egressSub.hidden = !draft.egress;
    R.dnsCustom.hidden = draft.dnsMode !== 'custom';
  }

  /* ================================================================ 连接质量 */
  /* one log scale for the slider and the preview: the slider's track covers 100–5000 ms and sits
     over exactly that stretch of the preview (20–5000 ms), so the thumb rides on the ceiling line */
  const LO = 20, HI = 5000, LN = Math.log(HI / LO), SMIN = 100, SLN = Math.log(HI / SMIN);
  const lpos = ms => T.util.clamp(Math.log(Math.max(LO, ms) / LO) / LN, 0, 1);
  const msOf = i => { const raw = SMIN * Math.exp(i / 1000 * SLN), st = raw < 300 ? 10 : raw < 1000 ? 50 : 100; return T.util.clamp(Math.round(raw / st) * st, SMIN, HI); };
  const iOf = ms => Math.round(Math.log(ms / SMIN) / SLN * 1000);
  let latSlider = null;
  /* the preview's nodes: nodes.list (Vanguard, Sentinel, Gateway) and their last latency; a node
     without a latency counts as unreachable. Only an estimate by the draft ceiling: the runtime's
     own verdict is in nodes.list (W-24). Stopped: only the cached lists are there. */
  const latNodes = () => ['vanguard', 'sentinel', 'gateway'].flatMap(tid => {
    const l = T.rt ? T.rt.topic('nodes.list', tid).get() : null;
    return (l && Array.isArray(l.items) ? l.items : []).filter(n => n && n.enabled !== false).map(n => ({ n: { id: n.id, name: n.name, lat: Number.isFinite(n.latency_ms) ? n.latency_ms : null }, tid }));
  });
  const band = (lat, v) => (lat == null ? 'down' : lat <= v / 2 ? 'ok' : lat <= v ? 'slow' : 'down');
  function latCounts(v) { const c = { ok: 0, slow: 0, down: 0 }; latNodes().forEach(({ n }) => { c[band(n.lat, v)]++; }); return c; }
  let recheck = null;          // { state: running | incomplete } after a latency save with effect recheck
  function rechecking(on) {
    clearTimeout(rechecking.t);
    recheck = on ? { state: 'running' } : null;
    if (on) rechecking.t = setTimeout(() => { recheck = null; paintLatAll(); }, 12000);   // the nodes.list updates show the result
    paintLatAll();
  }
  const paintLatAll = () => { if (latSlider) paintLat(draft.latency, visible); };
  function paintLat(v, animate, noDots) {
    if (!R.lbOk) return;
    const a = lpos(v / 2), b = lpos(v);
    R.lbOk.style.transform = `scaleX(${a.toFixed(4)})`;
    R.lbSlow.style.transform = `translateX(${(a * 100).toFixed(3)}%) scaleX(${(b - a).toFixed(4)})`;
    R.lbDown.style.transform = `translateX(${(b * 100).toFixed(3)}%) scaleX(${(1 - b).toFixed(4)})`;
    R.lcHalf.style.transform = `translateX(${(a * 100).toFixed(3)}%)`;
    R.lcCeil.style.transform = `translateX(${(b * 100).toFixed(3)}%)`;
    R.lmHalf.style.transform = `translateX(${(a * 100).toFixed(3)}%)`;
    R.lmCeil.style.transform = `translateX(${(b * 100).toFixed(3)}%)`;
    R.lmHalf.firstElementChild.textContent = msText(Math.round(v / 2));
    R.lmCeil.firstElementChild.textContent = msText(v);
    R.lmCeil.classList.toggle('is-end', b > 0.94);
    R.latEndR.classList.toggle('is-hid', b > 0.86);
    R.latV.textContent = msText(v);
    if (!noDots) paintLatDots(v);
    const c = latCounts(v), n = latNodes().length;
    // while the service is not running the counts come from the last check, and say so
    // no node latency known yet: no summary at all (F-01: the empty preview says it)
    const kids = [];
    if (n) {
      const lead = el('span', { class: 'set-lat-lead' }, tx('settings.quality.summary', { live: isLive() ? 'yes' : 'no', v: msText(v), n }));
      lead.querySelectorAll('b').forEach(x => x.classList.add('tnum'));
      kids.push(lead, status('ok', t('settings.quality.count.ok', { n: c.ok })), status('slow', t('settings.quality.count.slow', { n: c.slow })), status('down', t('settings.quality.count.down', { n: c.down })));
    }
    if (recheck) kids.push(el('span', { class: 'set-lat-rc' }, recheck.state === 'running' ? el('span', { class: 'spin' }) : null, recheck.state === 'running' ? t('settings.quality.rechecking') : t('settings.quality.recheck_incomplete')));
    const key = kids.map(x => x.textContent).join('|');
    if (R.latSum._h !== key) { R.latSum._h = key; R.latSum.replaceChildren(...kids); }
    const dk = v === LAT_DEF ? 'def' : `is:${v}`;
    if (R.latDef._h !== dk) {
      R.latDef._h = dk;
      R.latDef.replaceChildren(...(v === LAT_DEF ? [t('settings.quality.default')] : [t('settings.quality.default_is', { v: msText(LAT_DEF) }), ' · ', el('button', { class: 'link-btn set-lat-reset', type: 'button' }, t('settings.action.restore_default'))]));
    }
    void animate;
  }
  let latW = 0;                  // the dot strip's width, from the ResizeObserver (never a forced layout read)
  function paintLatDots(v) {
    const W = latW; if (!W || !R.latDots) return;
    const items = latNodes().map(({ n, tid }) => ({ n, tid, x: T.util.clamp(n.lat == null ? W : lpos(n.lat) * W, 5, W - 5) })).sort((p, q) => p.x - q.x);
    const lanes = [];
    items.forEach(it => { let L = 0; while (lanes[L] != null && it.x - lanes[L] < 11) L++; lanes[L] = it.x; it.lane = Math.min(L, 2); });
    const ids = new Set(items.map(it => String(it.n.id)));
    T.$$('.set-ld', R.latDots).forEach(d => { if (!ids.has(d.dataset.id)) d.remove(); });
    items.forEach(it => {
      let d = R.latDots.querySelector(`[data-id="${CSS.escape(String(it.n.id))}"]`);
      if (!d) { d = el('span', { class: 'set-ld', 'data-id': String(it.n.id) }, el('i')); R.latDots.append(d); }
      const b = band(it.n.lat, v);
      d.className = `set-ld b-${b}`;
      d.style.transform = `translate(${it.x.toFixed(1)}px, ${-it.lane * 10}px)`;
      const tip = t('settings.quality.dot_tip', { tier: tierLbl(it.tid), node: it.n.name, lat: it.n.lat == null ? t('settings.quality.no_response') : msText(it.n.lat), band: t(`settings.quality.band.${b}`) });
      if (d.getAttribute('data-tip') !== tip) d.setAttribute('data-tip', tip);
    });
  }
  function wireQuality() {
    R.root.querySelector('.set-lat-top').style.setProperty('--lat-a', `${(lpos(SMIN) * 100).toFixed(3)}%`);
    const onI = i => { const v = msOf(i); if (v !== draft.latency) { paintLat(v); setD('latency', v); } };
    latSlider = T.ui.slider(R.root.querySelector('#set-lat-sl'), {
      min: 0, max: 1000, step: 10, bigStep: 100, value: iOf(draft.latency), label: t('settings.quality.latency.title'), format: i => msText(msOf(i)),
      onInput: onI, onChange: onI,
    });
    R.latDef.addEventListener('click', e => {
      const b = e.target.closest('.set-lat-reset'); if (!b) return;
      const had = b.contains(document.activeElement);
      latSlider.set(iOf(LAT_DEF), true); paintLat(LAT_DEF, true); setD('latency', LAT_DEF);
      if (had) focusNow(R.root.querySelector('#set-lat-sl'));       // the link is gone at the default
    });
    K.latency = { sec: 'set-nodes', row: () => R.root.querySelector('#set-lat-row'), lock: () => R.root.querySelector('.set-lat-top'),
      paint: (v, a) => { latSlider.set(iOf(v), a); paintLat(v, a); } };
    paintLat(draft.latency);
    if (window.ResizeObserver) new ResizeObserver(es => { latW = es[es.length - 1].contentRect.width; paintLatDots(draft.latency); }).observe(R.latDots);
    else latW = R.latDots.clientWidth;
  }

  /* ================================================================ Bastion */
  let srcSel = null, srcType = 'sub', pendSrc = () => {}, pendEdit = () => {};
  const srcOf = id => draft.sources.find(x => x.id === id);
  const editingRow = () => T.$$('.set-src-r.is-editing', R.srcList).find(x => !x._ghost) || null;
  /* a source row: its kind and what can be shown of its value (R-05, U4): the value is a secret,
     so the row shows the sidecar's masked hint (a•••/r•••), a value typed on this page and not saved
     yet (the user's own draft), or 「来源 N · 已设置」. Once saved the typed value is gone from the
     page: the row shows the hint the snapshot brings. Editing replaces the value (never pre-filled). */
  function srcShown(s, i) {
    if (s.v) return datum(s.v, 'c-mono c-ell set-src-v');
    if (s.hint) return datum(s.hint, 'c-mono c-ell set-src-v');
    return el('span', { class: 'c-ell set-src-v set-src-set' }, t('settings.bastion.src.unnamed', { index: i + 1 }));
  }
  function srcRow(s, i) {
    // the buttons' names: the kind and the row's number (never the value: aria-label is read out and
    // kept by tools; no state word either: a draft row is not 已设置, 05d R4-2)
    const name = `${srcLabel(s.t)} · ${t('settings.bastion.src.row_aria', { index: i + 1 })}`;
    const kids = [srcChip(s.t), srcShown(s, i)];
    // B-07: the source's last fetch result (no contract field: only with the item)
    if (T.rt.feature('B-07')) kids.push(el('span', { class: 'set-src-last' }));
    const editable_ = s.t !== 'file';
    kids.push(el('span', { class: 'li-act' },
      editable_ ? el('button', { class: 'icon-btn sm', type: 'button', 'data-edit': '', 'aria-label': t('settings.bastion.src.edit_aria', { v: name }), 'data-tip': t('settings.bastion.src.replace_tip') }, icon('edit', 14)) : null,
      el('button', { class: 'icon-btn sm', type: 'button', 'data-del': '', 'aria-label': t('settings.bastion.src.delete_aria', { v: name }), 'data-tip': t('settings.action.delete') }, icon('trash', 14))));
    return el('div', { class: 'li set-rule set-src-r', 'data-key': s.id }, ...kids);
  }
  function renderSrc(list, animate) {
    const put = () => { T.$$('.set-src-r', R.srcList).forEach(x => x.remove()); R.srcList.append(...list.map(srcRow)); paintItems(); };
    if (animate && visible) listFx(R.srcList, put); else put();
    paintSrcLive(animate);
    pendEdit();
  }
  function paintSrcLive(animate) {
    const n = draft.sources.length;
    M.swapText(R.srcLive, t('settings.bastion.sources.count', { n }), { instant: !animate || !visible });
  }
  /* the pool's numbers are the runtime's (D9: bastion.pool capacity / target / min); stopped: no
     baseline (PROTOCOL §11 bastion.pool): the row has no line then (F-01: not 「要等服务运行后才知道」) */
  const pool = () => S.get('bastion') || null;
  const poolCap = () => { const b = pool(); return b && Number.isInteger(b.capacity) && b.capacity > 0 ? b.capacity : null; };
  function bastionHint() {
    const cap = poolCap();
    return cap ? t('settings.bastion.min.hint_pool', { capacity: cap }) : '';
  }
  /* B-sem (U2): Bastion only says whether it is collecting, plus the runtime's numbers */
  function paintBastionLive(animate) {
    if (!R.bastionLive) return;
    const b = pool(), r = run();
    let n;
    if (!b || !(r === 'running' || r === 'starting')) n = T.html`<span class="sdot off"></span>${t('settings.bastion.live.unknown')}`;
    else n = T.html`<span class="sdot ${b.collecting ? 'busy' : 'ok'}"></span>${t('settings.bastion.live.pool', { collecting: b.collecting ? 'yes' : 'no', ok: Number(b.ok) || 0 })}`;
    swapNode(R.bastionLive, n, { instant: !animate });
  }
  /* inline edit of one source = replacing its value (never pre-filled: the value is a secret). An
     open edit whose text differs is a pending draft (row._edit, read by the pending check). */
  function editSrc(row) {
    if (row.classList.contains('is-editing')) return;
    const s = srcOf(row.dataset.key); if (!s) return;
    row.classList.add('is-editing');
    const old = Array.from(row.childNodes);
    const inp = el('input', { 'aria-label': t('settings.bastion.src.input_edit_aria', { kind: s.t }), spellcheck: 'false', translate: 'no', placeholder: t('settings.bastion.src.replace_ph') });
    const err = el('span', { class: 'set-inline-e' });
    row.replaceChildren(srcChip(s.t), el('div', { class: 'input mono sm set-inline' }, inp),
      el('span', { class: 'set-inline-a' },
        el('button', { class: 'icon-btn sm', type: 'button', 'data-ok': '', 'aria-label': t('settings.bastion.src.done_aria'), 'data-tip': t('settings.action.done'), 'data-tip-kbd': 'Enter' }, icon('check', 14)),
        el('button', { class: 'icon-btn sm', type: 'button', 'data-no': '', 'aria-label': t('settings.bastion.src.cancel_aria'), 'data-tip': t('settings.action.cancel'), 'data-tip-kbd': 'Esc' }, icon('close', 14))),
      err);
    M.anim(row.querySelector('.set-inline'), [{ opacity: 0.4 }, { opacity: 1 }], { duration: 160, easing: M.ease.out });
    inp.focus();
    // the error line opens inside the row: the rows after it and the page below glide
    const errNode = e => T.html`${I('error', 14)}<span>${T.ui.tech(e)}</span>`;
    const setErr = e => {
      if (!!e === row.classList.contains('is-invalid')) { if (e) err.replaceChildren(errNode(e)); return; }
      listFx(R.srcList, () => { err.replaceChildren(...(e ? [errNode(e)] : [])); row.classList.toggle('is-invalid', !!e); }, { enter: false });
    };
    const close = () => { const had = row.contains(document.activeElement); row._edit = null; setErr(null); row.classList.remove('is-editing'); row.replaceChildren(...old); pendEdit(); if (had) focusNow(row.querySelector('[data-edit]')); };
    const ok = () => {
      const raw = inp.value.trim();
      if (!raw) { close(); return true; }
      const v = srcNorm(s.t, raw), e = srcErr(s.t, v, draft.sources, s.id);
      if (e) { setErr(e); return false; }
      const ns = Object.assign({}, s, { v }), had = row.contains(document.activeElement);
      row._edit = null;
      setErr(null);
      const list = draft.sources.map(x => (x.id === s.id ? ns : x));
      const nr = srcRow(ns, list.indexOf(ns));
      row.replaceWith(nr);
      if (visible) { nr.classList.add('is-new'); setTimeout(() => nr.classList.remove('is-new'), 1600); }
      setD('sources', list);
      pendEdit();
      if (had) focusNow(nr.querySelector('[data-edit]'));     // the edited row's own edit button
      return true;
    };
    row._edit = {
      input: inp, ok, cancel: close,
      changed: () => !!inp.value.trim(),
      value: () => { const v = srcNorm(s.t, inp.value); return inp.value.trim() && !srcErr(s.t, v, draft.sources, s.id) ? `${s.id} ${v}` : ''; },
    };
    inp.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); if (!ok()) inp.focus(); } else if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); close(); const b = row.querySelector('[data-edit]'); if (b) b.focus(); } });
    inp.addEventListener('input', () => {
      pendEdit();
      if (row.classList.contains('is-invalid')) setErr(inp.value.trim() ? srcErr(s.t, srcNorm(s.t, inp.value), draft.sources, s.id) : null);
    });
    row.querySelector('[data-ok]').addEventListener('click', () => { if (!ok()) inp.focus(); });
    row.querySelector('[data-no]').addEventListener('click', () => { close(); const b = row.querySelector('[data-edit]'); if (b) b.focus(); });
  }
  /* → true when added (or nothing was typed), false when the field now says why not */
  function addSrc() {
    if (!R.srcIn.value.trim()) return true;
    const v = srcNorm(srcType, R.srcIn.value);
    const e = srcErr(srcType, v, draft.sources);
    if (e) { fieldNote(R.srcF, e, true); return false; }
    const s = { id: uid('new-src-'), t: srcType, hint: null, enabled: true, v };
    const list = draft.sources.concat([s]);
    listFx(R.srcList, () => R.srcList.append(srcRow(s, list.length - 1)), { highlight: true });
    setD('sources', list);
    R.srcIn.value = ''; fieldNote(R.srcF, '', false); paintSrcLive(true);
    pendSrc();
    return true;
  }
  const addSrcNow = () => { if (!R.srcIn.value.trim()) { R.srcIn.focus(); return; } addSrc(); };
  function wireBastion() {
    const sw = R.root.querySelector('.switch[data-k="bastionOn"]');
    K.bastionOn = { sec: 'set-nodes', row: () => R.root.querySelector('#set-bastionOn-row'), lock: () => sw,
      paint(v) { sw.setAttribute('aria-checked', String(!!v)); } };
    sw.addEventListener('change', e => { setD('bastionOn', e.detail.checked); });
    // the pool's capacity is the runtime's (D9: bastion.pool.capacity), never a literal
    const minErr = (id, p) => ({ msg: t(`settings.bastion.min.err.${id}`, p), short: t(`settings.bastion.min.err.${id}.short`, p) });
    const bm = rowNumber(R.root.querySelector('#set-bastionMin-row'), R.root.querySelector('#set-bastion-min'), {
      min: 1, max: 100000, hint: bastionHint,
      validate: v => {
        const s = String(v).trim(), cap = poolCap();
        if (!/^\d+$/.test(s)) return s ? minErr('not_int', { v: s, capacity: cap || 0 }) : minErr('empty', { capacity: cap || 0 });
        return +s < 1 || (cap && +s > cap) ? minErr('range', { v: s, capacity: cap || 0 }) : null;
      },
      onInput: v => setD('bastionMin', /^\d+$/.test(String(v).trim()) ? +v : String(v)),
    });
    R.bastionMinCtl = bm;
    K.bastionMin = { sec: 'set-nodes', row: () => R.root.querySelector('#set-bastionMin-row'), lock: () => R.root.querySelector('#set-bastion-min').closest('.input'), paint: v => bm.set(v), remote: m => { if (m) bm.error(m); else bm.hint(); } };
    CHECKS.push(() => (editable('bastionMin') && !same(draft.bastionMin, base.bastionMin) && bm.run() ? { row: R.root.querySelector('#set-bastionMin-row'), focus: R.root.querySelector('#set-bastion-min'), msg: t('settings.bastion.min.problem', { reason: bm.err.short }) } : null));
    srcSel = T.ui.select(R.root.querySelector('#set-src-type'), {
      value: srcType, options: Object.keys(SRC_T).map(st => ({ value: st, label: srcLabel(st) })),
      onChange: v => { srcType = v; R.srcIn.placeholder = SRC_T[v].ph; fieldNote(R.srcF, '', false); pendSrc(); },
    });
    R.srcIn.placeholder = SRC_T[srcType].ph;
    fieldNote(R.srcF, '', false, true);
    R.srcIn.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); addSrcNow(); } });
    R.srcIn.addEventListener('input', () => { pendSrc(); if (R.srcF.classList.contains('is-invalid')) { const v = srcNorm(srcType, R.srcIn.value), e = R.srcIn.value.trim() ? srcErr(srcType, v, draft.sources) : null; fieldNote(R.srcF, e || '', !!e); } });
    R.root.querySelector('#set-src-add').addEventListener('click', addSrcNow);
    R.srcList.addEventListener('click', e => {
      const row = e.target.closest('.set-src-r'); if (!row || row._ghost || row.classList.contains('is-editing')) return;
      if (e.target.closest('[data-edit]')) { editSrc(row); return; }
      if (e.target.closest('[data-del]')) {
        const id = row.dataset.key, list = draft.sources.filter(s => s.id !== id), had = row.contains(document.activeElement);
        const rows = T.$$('.set-src-r', R.srcList).filter(x => !x._ghost), i = rows.indexOf(row), nb = rows[i + 1] || rows[i - 1];
        listFx(R.srcList, () => row.remove());
        setD('sources', list); paintSrcLive(true);
        if (had) focusNow(nb ? nb.querySelector('[data-edit], [data-del]') : R.srcIn);     // the next source, or the add field
      }
    });
    K.sources = { sec: 'set-nodes', row: () => R.root.querySelector('#set-sources-row'), lock: () => R.root.querySelector('#set-sources-row'), paint: (v, a) => renderSrc(v, a) };
    pendSrc = pendingField('pend:src', { sec: 'set-nodes', eff: 'instant',
      row: () => R.root.querySelector('#set-sources-row'), at: () => R.srcF, input: () => R.srcIn,
      text: () => R.srcIn.value.trim(),
      value: () => { if (!R.srcIn.value.trim()) return ''; const v = srcNorm(srcType, R.srcIn.value); return srcErr(srcType, v, draft.sources) ? '' : `${srcType} ${v}`; },
      commit: addSrc, reset: () => { R.srcIn.value = ''; fieldNote(R.srcF, '', false); },
      msg: () => t('settings.bastion.src.pending') });
    pendEdit = pendingField('pend:srcEdit', { sec: 'set-nodes', eff: 'instant',
      row: () => R.root.querySelector('#set-sources-row'),
      at: () => editingRow() || R.srcList, input: () => { const r = editingRow(); return r && r._edit ? r._edit.input : null; },
      text: () => { const r = editingRow(); return !!(r && r._edit && r._edit.changed()); },
      value: () => { const r = editingRow(); return r && r._edit ? r._edit.value() : ''; },
      commit: () => { const r = editingRow(); return r && r._edit ? r._edit.ok() : true; },
      reset: () => { const r = editingRow(); if (r && r._edit) r._edit.cancel(); },
      msg: () => t('settings.bastion.src.edit_pending') });
    ['bastionOn', 'bastionMin', 'sources'].forEach(k => K[k].paint(draft[k], false));
    paintBastionLive(false);
  }
  /* bastion.sources: a row list the sidecar publishes; without a draft on the sources it is their base */
  function onSourcesTopic() {
    const l = T.rt.topic('bastion.sources').get();
    if (!l || !Array.isArray(l.items) || !known.sources) return;
    const m = C.fromSnapshot({ sources: l.items }, []).values.sources;
    if (!m) return;
    const hadDraft = !same(draft.sources, base.sources);
    base.sources = m;
    if (!hadDraft) { draft.sources = clone(m); if (K.sources) K.sources.paint(draft.sources, visible); }
    track();
  }

  /* ================================================================ 提醒 (10-30 redesign)
     Notices are local only (.build/提醒模块重设计_20261030.md §5, §10): five switches under
     config.snapshot values.alerts.notify (draft keys nOn · nConn · nTier · nUpd · nSound, one
     ordinary section in pages/settings/changes.js); 预览提醒 is alerts.test (a forced card that
     waits for the shell's receipt) and 查看提醒记录 opens the logs page on the alert category.
     The event rows and the sound row are shown only while 桌面提醒 is on (DESIGN §9: what does
     not apply is hidden, not shown dead). */
  /* only 连接问题 keeps a line under it (which notices it covers); F-04 dropped the tier row's
     line (it repeated its title) and the sound row's (when it plays) */
  const NOTICE_ROWS = [['nConn', 'connection', true], ['nTier', 'tier', false], ['nUpd', 'update', false]];
  function secAlerts() {
    const row = (k, id, desc) => frow(`set-${k}-row`, t(`alerts.notify.${id}.title`), desc ? t(`alerts.notify.${id}.desc`) : null,
      swEl(k, t(`alerts.notify.${id}.title`)), { cls: id === 'on' ? null : 'set-n-row' });
    return el('section', { class: 'fsec', id: 'set-alerts', 'data-enter': '' },
      head(t('alerts.notify.sec.title')),
      row('nOn', 'on', false),
      el('div', { class: 'set-n-dep', id: 'set-n-dep' },
        el('div', { class: 'set-n-grp' }, t('alerts.notify.events.title')),
        ...NOTICE_ROWS.map(([k, id, desc]) => row(k, id, desc)),
        row('nSound', 'sound', false)),
      el('div', { class: 'set-n-foot' },
        el('button', { class: 'btn btn-sm', id: 'set-n-preview', type: 'button' }, icon('bell', 15), el('span', { class: 'lab' }, t('alerts.notify.preview'))),
        el('button', { class: 'link-btn', id: 'set-n-history', type: 'button' }, t('alerts.notify.history'), icon('chevron', 14)),
        el('span', { class: 'set-n-res', id: 'set-n-res', role: 'status', 'aria-live': 'polite' })));
  }
  function wireAlerts() {
    const bind = (k, dep) => {
      const sw = R.root.querySelector(`.switch[data-k="${k}"]`);
      K[k] = { sec: 'set-alerts', row: () => R.root.querySelector(`#set-${k}-row`), lock: () => sw,
        paint(v, a) { sw.setAttribute('aria-checked', String(!!v)); if (dep) dep(!!v, a); } };
      sw.addEventListener('change', e => { setD(k, e.detail.checked); if (dep) dep(e.detail.checked, true); });
    };
    bind('nOn', (on, a) => reveal([[R.nDep, on]], a));
    NOTICE_ROWS.forEach(([k]) => bind(k));
    bind('nSound');
    ['nOn', 'nConn', 'nTier', 'nUpd', 'nSound'].forEach(k => K[k].paint(draft[k], false));
    // 查看提醒记录: the logs page filtered to the alert category, every level, every run
    R.nHistory.addEventListener('click', () => T.go('logs', { category: 'alert', level: 'all', runOnly: false }));
    R.nPreview.addEventListener('click', () => runPreview(R.nPreview));
  }
  /* 预览提醒 (alerts.test, args {}): one forced card; the job ends with the shell's receipt. A card
     nothing could show (dropped), a timeout or a busy slot says why beside the button. */
  function runPreview(btn) {
    const say = (ok, text) => { R.nRes.classList.toggle('is-err', !ok); R.nRes.textContent = text; };
    say(true, '');
    T.ui.busy(btn, () => T.rt.call('alerts.test', {}).then(
      () => { say(true, t('alerts.notify.preview.shown')); return t('alerts.notify.preview.shown'); },
      e => {
        const code = e && e.reason_code;
        say(false, code ? T.rt.reasonText('alerts', `test_reason.${code}`) : errText(e));
        throw Object.assign(new Error(t('alerts.notify.preview.failed')), { shown: true });
      }),
      { busy: t('alerts.notify.preview.waiting'), done: r => r, fail: () => t('alerts.notify.preview.failed'), icon: 'bell', busyIcon: 'bell', hold: 1800, failHold: 2600 });
  }

  /* ================================================================ 应用 */
  /* autostart.state: supported · enabled · target (shell / legacy_qt / other / none) · mismatch ·
     error (G50; custom and audit scopes are read-only: supported false) */
  function paintAuto(animate) {
    if (!R.autoLive) return;
    const a = auto;
    let n, kind, desc = '', derr = false;
    if (!a) { kind = 'unknown'; n = status('unknown', t('settings.app.autostart.unknown')); }
    else if (a.failed || a.error) { kind = 'error'; n = status('down', t('settings.app.autostart.read_failed')); desc = errText(a.error); derr = true; }
    else if (a.supported === false) { kind = 'unsupported'; n = status('off', t('settings.app.autostart.unsupported')); desc = t('settings.app.autostart.unsupported_desc'); }
    else if (a.mismatch && (a.target === 'legacy_qt' || a.target === 'other')) { kind = 'mismatch'; n = status('slow', t('settings.app.autostart.mismatch', { target: a.target })); desc = t('settings.app.autostart.mismatch_desc'); }
    else { kind = a.enabled ? 'on' : 'off'; n = status(a.enabled ? 'ok' : 'off', t(a.enabled ? 'settings.app.autostart.on' : 'settings.app.autostart.off')); }
    swapNode(R.autoLive, n, { kind, instant: !animate || !visible });
    note(R.autoD, desc, derr, !animate);
    paintTray(animate);
  }
  /* the application rows' locks: autostart unsupported → its switch is read-only whatever the
     snapshot says; D8 only with autostart on (paintKnown calls this too, so a re-read keeps them) */
  const trayOff = () => !draft.autostart || !!(auto && auto.supported === false);
  function lockApp() {
    if (!R.root) return;
    const sa = R.root.querySelector('.switch[data-k="autostart"]'), st = R.root.querySelector('.switch[data-k="tray"]');
    if (sa) sa.inert = known.autostart !== true || !!(auto && auto.supported === false);
    if (st) { const off = trayOff(); st.inert = off || known.tray !== true; st.setAttribute('aria-disabled', String(off)); }
  }
  /* D8: 开机自启时进入托盘 — off by default; only with autostart on (else read-only; F-04: without
     a line saying why, the row sits under 开机自启) */
  function paintTray() {
    if (!R.trayD) return;
    const off = trayOff();
    const row = R.root.querySelector('#set-tray-row');
    row.classList.toggle('is-off', off);
    lockApp();
  }
  function paintVersion(animate) {
    if (!R.verApp) return;
    const v = app.version || (T.rt.hello && T.rt.hello.app_version) || '';
    if (R.verApp.textContent !== v) R.verApp.textContent = v;
    // the proxy core's version (core.info); not read yet / no Core: 未检测 (G50)
    const core = S.get('core'), cv = core && core.version;
    const n = cv ? datum(cv) : document.createTextNode(t('settings.app.version.core_unknown'));
    R.verCore.replaceChildren(n);
    void animate;
  }
  /* update.state (Update): idle · checking · available · downloading · verified · handoff · failed;
     installable false → the releases page (G51); notes (plain text, untrusted) when present;
     enabled false → this build has no in-app update. B-30(a)(c)(d): the check time, the download
     percentage, the signed / size / date line are prototype-only. */
  const upd = () => (T.rt ? T.rt.topic('update.state').get() : null) || null;
  function paintUpd(animate) {
    if (!R.updSt) return;
    const u = upd();
    const st = !u ? 'none' : u.enabled === false ? 'disabled' : u.state === 'idle' && u.update_available ? 'available' : u.state;
    const ver = u && u.version ? u.version : '';
    let n, kind = st;
    if (st === 'none') n = document.createTextNode(t('settings.app.update.state.unknown'));
    else if (st === 'disabled') n = document.createTextNode(t('settings.app.update.state.disabled'));
    else if (st === 'checking') n = T.html`<span class="spin"></span>${t('settings.app.update.state.checking')}`;
    else if (st === 'downloading') n = T.html`<span class="spin"></span>${t('settings.app.update.state.downloading_plain', { version: ver })}`;
    else if (st === 'verified') n = T.html`${I('check', 14)}${t('settings.app.update.state.verifying_done')}`;
    else if (st === 'handoff') n = T.html`${I('check', 14)}${t('settings.app.update.state.ready')}`;
    else if (st === 'failed') n = T.html`${I('error', 14)}${t('settings.app.update.state.failed_plain', { reason: errText(u.error) || t('common.reason_unknown') })}`;
    else if (st === 'available') n = T.html`${tx('settings.app.update.state.found_plain', { version: ver }, { x: frag => el('span', { class: 'set-upd-new', translate: 'no' }, frag) })}${!u.installable ? ` · ${t('settings.app.update.not_installable')}` : ''}`;
    else if (u.previous_version) n = T.html`${I('check', 14)}${t('settings.app.update.state.updated', { version: u.current_version || app.version || '' })}`;
    // the last check's time is a contract field (Update.last_check_at_ms, T-P4-C2 #10): shown when
    // present; B-30(a)'s prototype wording (an automatic check on a schedule) only with the item
    else if (Number.isFinite(u.last_check_at_ms)) n = T.html`${I('check', 14)}${t('settings.app.update.state.latest', { manual: T.rt.feature('B-30a') ? 'no' : 'yes', when: dayTime(u.last_check_at_ms) })}`;
    else n = T.html`${I('check', 14)}${t('settings.app.update.state.latest_plain')}`;
    if (u && st === 'available' && Number.isFinite(u.last_check_at_ms)) n = T.html`${n} · ${t('settings.app.update.checked_at', { when: dayTime(u.last_check_at_ms) })}`;
    R.updSt.classList.toggle('is-ok', st === 'idle' || st === 'verified' || st === 'handoff');
    R.updSt.classList.toggle('is-err', st === 'failed');
    swapNode(R.updSt, n, { kind, instant: !animate || !visible });
    // B-30(d): signed · size · date (no contract field) only with the item
    R.updMeta.hidden = !(T.rt.feature('B-30d') && st === 'available');
    const busy = st === 'downloading' || st === 'verified' || st === 'handoff';
    // B-30(c): a download percentage only with the item; otherwise an indeterminate bar
    reveal([[R.updProg, st === 'downloading']], animate);
    if (st === 'downloading') { const p = T.ui.progress(R.updProg.querySelector('.prog')); p.pending(!T.rt.feature('B-30c')); }
    const check = R.root.querySelector('#set-upd-check');
    check.hidden = busy;
    check.disabled = st === 'disabled' || !can('app.update.check');
    reveal([[R.updNotes, !!(u && typeof u.notes === 'string' && u.notes.trim() && st === 'available')], [R.updInstall, st === 'available' && !!u.installable && can('app.update.install')], [R.updPage, st === 'available' && !u.installable]], animate);
  }
  /* 更新说明: the publisher's text (Update.notes), shown as plain text (never parsed: R-09) */
  function releaseNotes(from) {
    const u = upd(); if (!u || !u.notes) return;
    const body = el('div', { class: 'set-notes-text', translate: 'no' }, u.notes);
    const actions = [{ label: t('settings.action.close'), ghost: true }];
    if (u.installable && can('app.update.install')) actions.push({ label: t('settings.app.update.install'), primary: true, run: () => setTimeout(() => install(R.updInstall), 60) });
    T.sheet({ from, title: t('settings.app.update.notes_title', { version: u.version || '' }), width: 520, content: body, actions });
  }
  function install(btn) {
    const u = upd(); if (!u || !u.installable) return;
    T.confirm({ title: t('settings.app.update.confirm.title', { version: u.version || '' }), message: t('settings.app.update.confirm.body_plain'), confirmLabel: t('settings.app.update.confirm.ok') }).then(ok => {
      if (!ok) return;
      // the sidecar verifies, the shell closes Tesseract and runs the installer (update.state follows)
      T.ui.busy(btn, T.rt.call('app.update.install', {}).then(() => t('settings.app.update.installing')), { busy: t('settings.app.update.busy'), done: r => r, fail: e => errText(e), icon: 'download', busyIcon: 'download', stay: true, failHold: 4000 });
    });
  }
  /* host.* answers: a conflict + dialog_open (another native dialog open) and the other host reasons
     are worded by the shell's reason catalog (shell.host_reason.*, T.shellc.errorText; B8) */
  const hostErr = e => (T.shellc && typeof T.shellc.errorText === 'function' ? T.shellc.errorText(e) : errText(e));
  const openUrl = (btn, url) => T.ui.busy(btn, T.rt.call('host.openUrl', { url }).then(r => (r && r.opened === false ? Promise.reject(new T.rt.RtError('io')) : t('settings.app.dirs.opened'))), { done: r => r, fail: hostErr, hold: 1200, failHold: 3200 });
  const openFolder = (btn, kind) => T.ui.busy(btn, T.rt.call('host.openFolder', { kind }).then(r => (r && r.opened === false ? Promise.reject(new T.rt.RtError('io')) : t('settings.app.dirs.opened'))), { done: r => r, fail: hostErr, hold: 1200, failHold: 3200 });
  /* Geo: one line, or one card per kind when Geo.items is sent (C2 #11); failures by message_key;
     older than 36 h: 未按时更新 (W-25) */
  function paintGeo(animate) {
    if (!R.geo) return;
    const g = geo;
    const at = g && Number.isFinite(g.updated_at_ms) ? g.updated_at_ms : null;
    const stale = ms => ms != null && Date.now() - ms > GEO_STALE_MS;
    const card = (kind, ms, extra) => {
      const s = g && g.running ? T.html`<span class="spin"></span>${t('settings.app.geo.updating')}`
        : ms == null ? status('unknown', t('settings.app.geo.never'))
          : stale(ms) ? status('slow', capFirst(t('settings.app.geo.stale', { when: dayTime(ms) }))) : status('ok', capFirst(dayTime(ms)));
      return el('div', { class: `set-geo-i${extra ? ' ' + extra : ''}`, 'data-k': kind || 'all' },
        el('span', { class: 'set-geo-n' }, kind ? t(`settings.app.geo.kind.${kind}`) : t('settings.app.geo.all'), kind ? el('small', { translate: 'no' }, GEO_NAME[kind]) : null),
        el('span', { class: 'set-geo-s' }, s));
    };
    let cards;
    if (g && Array.isArray(g.items) && g.items.length) cards = g.items.filter(i => i && GEO_KINDS.includes(i.kind)).map(i => card(i.kind, Number.isFinite(i.updated_at_ms) ? i.updated_at_ms : null, i.updated_at_ms == null ? 'is-fail' : ''));
    else cards = [card(null, at, 'is-wide')];
    const key = `${cards.map(c => c.textContent).join('|')}|${g && g.running}`;
    if (R.geo._k !== key) { R.geo._k = key; R.geo.replaceChildren(...cards); R.geo.classList.toggle('is-one', cards.length === 1); }
    const errs = g && Array.isArray(g.errors) ? g.errors : [];
    const lines = errs.slice(0, 4).map(e => el('div', { class: 'set-geo-e' }, icon('error', 13), el('span', null, errText(e))));
    if (geoFail && !errs.length) lines.push(el('div', { class: 'set-geo-e' }, icon('error', 13), el('span', null, errText(geoFail))));
    if (geoDisabled) lines.push(el('div', { class: 'set-geo-e' }, icon('info', 13), el('span', null, errText(geoDisabled))));
    R.geoErr.replaceChildren(...lines);
    reveal([[R.geoErr, lines.length > 0]], animate);
    R.geoLinkT.textContent = `· ${errs.length ? t('settings.app.geo.link_failed', { n: errs.length }) : at ? t('settings.app.geo.link_updated', { when: dayTime(at) }) : t('settings.app.geo.never')}`;
    const btn = R.root.querySelector('#set-geo-btn');
    // Geo updates turned off in the configuration (geo.update conflict / disabled): the button rests (31b-18)
    btn.disabled = !!geoDisabled || !can('geo.update') || !!(g && g.running);
  }
  /* geo.update: done needs triggered && completed; a failure says why under the cards (needs_route:
     需要先有可用出口; disabled in the configuration: the button rests), the button only says it failed */
  let geoFail = null;
  function updateGeo(btn) {
    geoFail = null;
    return T.ui.busy(btn, () => T.rt.call('geo.update', { force: true }).then(r => {
      if (r && r.status) geo = r.status;
      if (r && !(r.triggered && r.completed)) { geoFail = { code: 'io', message_key: 'errors.geo.update_failed', params: {} }; paintGeo(true); throw new T.rt.RtError('io'); }
      paintGeo(true);
      return t('settings.app.geo.all_updated');
    }, e => {
      if (e && e.code === 'conflict' && e.reason_code === 'disabled') geoDisabled = e;
      else geoFail = e;
      if (e && e.result && e.result.status) geo = e.result.status;
      paintGeo(true);
      throw e;
    }), { busy: t('settings.app.geo.busy'), done: r => r, fail: () => t('settings.app.geo.failed_short'), icon: 'download', busyIcon: 'download', hold: 1600, failHold: 3600 });
  }
  function wireApp() {
    /* each option is a language's own name (settings.app.lang.name.*, never translated, kept as is
       by the pseudo language too): translate="no" and that language's lang (BCP 47), on the menu's
       options and on the shown choice (T.ui.select option lang / translate; T-P5-04, T-P5-07) */
    const lang = T.ui.select(R.root.querySelector('#set-lang'), { value: draft.lang, options: LANGS.map(v => ({ value: v, label: t(`settings.app.lang.name.${v.toLowerCase()}`), lang: v.replace('_', '-'), translate: 'no' })), onChange: v => { langNote = null; paintLangNote(true); setD('lang', v); } });
    K.lang = { sec: 'set-app', row: () => R.root.querySelector('#set-lang-row'), lock: () => R.root.querySelector('#set-lang'), paint: (v, a) => lang.set(v, a) };
    const au = R.root.querySelector('.switch[data-k="autostart"]');
    K.autostart = { sec: 'set-app', row: () => R.root.querySelector('#set-autostart-row'), lock: () => au,
      paint(v) { au.setAttribute('aria-checked', String(!!v)); paintTray(false); } };
    au.addEventListener('change', e => { setD('autostart', e.detail.checked); paintTray(true); });
    const tr = R.root.querySelector('.switch[data-k="tray"]');
    K.tray = { sec: 'set-app', row: () => R.root.querySelector('#set-tray-row'), lock: () => tr, paint(v) { tr.setAttribute('aria-checked', String(!!v)); } };
    tr.addEventListener('change', e => setD('tray', e.detail.checked));
    ['lang', 'autostart', 'tray'].forEach(k => K[k].paint(draft[k], false));
    paintAuto(false); paintLangNote(false);
    const check = R.root.querySelector('#set-upd-check');
    // (T-P4-09: the width of every face is measured once the section is laid out)
    whenShown(check, () => T.ui.reserve(check, [t('settings.app.update.checking'), t('settings.app.update.latest_short'), t('settings.app.update.check_failed')]));
    check.addEventListener('click', () => {
      T.ui.busy(check, T.rt.call('app.update.check', {}).then(u => (u && (u.update_available || u.state === 'available') ? t('settings.app.update.found_short', { version: u.version || '' }) : t('settings.app.update.latest_short'))),
        { busy: t('settings.app.update.checking'), done: r => r, fail: () => t('settings.app.update.check_failed'), icon: 'refresh', busyIcon: 'refresh', hold: 1600, failHold: 2600 });
    });
    R.updNotes.addEventListener('click', () => releaseNotes(R.updNotes));
    R.updInstall.addEventListener('click', () => install(R.updInstall));
    R.updPage.addEventListener('click', () => openUrl(R.updPage, RELEASES_URL));
    paintUpd(false); paintVersion(false);
    const geoBtn = R.root.querySelector('#set-geo-btn');
    whenShown(geoBtn, () => T.ui.reserve(geoBtn, [t('settings.app.geo.busy'), t('settings.app.geo.all_updated'), t('settings.app.geo.update'), t('settings.app.geo.failed_short')]));
    geoBtn.addEventListener('click', () => updateGeo(geoBtn));
    paintGeo(false);
    R.status.addEventListener('click', e => {
      const b = e.target.closest('[data-act]'); if (!b) return;
      if (b.dataset.act === 'retry') loadAll();
      else if (b.dataset.act === 'open-config') openFolder(b, 'config');
    });
  }

  /* ================================================================ runtime sync */
  function syncRuntime(animate) {
    paintModeStatus(animate); syncModeSwitches(); paintBypass(animate); paintKsLive(animate); paintBastionLive(animate);
    if (latSlider) paintLat(draft.latency, animate, true);     // its summary says whether it is live or last-known
    if (R.bastionMinCtl) R.bastionMinCtl.hint();
  }
  /* the B-item controls (architecture §5): shown only when both keys hold. hello.caps.features (key 1)
     arrives after the first render, so this runs on render and on every rt:caps */
  function paintFeatures() {
    if (!R.root) return;
    const show = (sel, on) => { const e = R.root.querySelector(sel); if (e) e.hidden = !on; };
    if (K.sources) K.sources.paint(draft.sources, false);           // B-07 (each source's last fetch)
    paintStatus(false); paintUpd(false);
  }
  /* {focus} landings (host:navigate, the update banner, the palette): the alerts section lands on its
     heading (focused: a notification click lands there), the update row on its controls */
  const FOCUS = {
    killswitch: 'set-ks-row', ks: 'set-ks-row', mode: 'set-mode-row', rules: 'set-rules-row', routing: 'set-rules-row', bypass: 'set-bypass-row',
    dns: 'set-dns-row', ports: 'set-portMixed-row', latency: 'set-lat-row', quality: 'set-lat-row', bastion: 'set-bastionOn-row', alerts: 'set-alerts',
    update: 'set-upd-row', geo: 'set-geo-row', app: 'set-app', conn: 'set-conn', route: 'set-route', net: 'set-conn', nodes: 'set-nodes', safety: 'set-safety',
    language: 'set-lang-row', autostart: 'set-autostart-row',
  };
  function focusTo(key) {
    const id = FOCUS[key] || key, target = R.root.querySelector('#' + id);
    if (!target) return;
    showUpTo(target);
    requestAnimationFrame(() => {
      if (!visible) return;
      const isSec = target.classList.contains('fsec');
      afterScroll(glideTo(target, isSec ? 24 : 96), () => {
        if (!visible) return;
        wash(target);
        if (key === 'alerts') { const h = target.querySelector('.fsec-t'); if (h) h.focus({ preventScroll: true }); return; }
        if (key === 'update') { const b = T.$$('#set-upd-install, #set-upd-page, #set-upd-check', target).find(isShown); if (b) T.router.land(b); return; }
        // arriving from the keyboard (a link, a banner action, the palette): focus lands on the row's control
        T.router.land(T.$$(ROW_CTL, target).find(isShown) || T.$$('button, input, [tabindex="0"]', target).find(isShown));
      });
    });
  }

  function refs(root) {
    const $ = s => root.querySelector(s);
    R = {
      root, main: $('#set-main'), toc: $('#set-toc'), status: $('#set-status'),
      modeSt: $('#set-mode-st'),
      ksLive: $('#set-ks-live'), ksSub: $('#set-ks-sub'), ksAdmin: $('#set-ks-admin'), ksDnsWrap: $('#set-ksDnsList-wrap'),
      ruleF: $('#set-rule-f'), ruleIn: $('#set-rule-in'), combo: $('#set-combo'), comboBtn: $('#set-combo-open'),
      setList: $('#set-sets-list'), setM: $('#set-sets-m'), setF: $('#set-set-f'), setName: $('#set-set-name'), setUrl: $('#set-set-url'),
      rlCustom: $('#set-rl-custom'), rlCm: $('#set-rl-cm'), rlEmpty: $('#set-rl-empty'),
      modeSws: $('#set-mode-sws'), gateTun: $('#set-gate-tun'), bypassRow: $('#set-bypass-row'), geoLinkT: $('#set-geo-link-t'),
      dnsCustom: $('#set-dns-custom'), resReq: $('#set-res-req'), resList: $('#set-res-list'), resF: $('#set-res-f'), resIn: $('#set-res-in'),
      egressSub: $('#set-egress-sub'),
      latV: $('#set-lat-v'), latDef: $('#set-lat-def'), latDots: $('#set-lat-dots'), latSum: $('#set-lat-sum'), latEndR: $('#set-lat-endr'),
      lbOk: $('#set-lb-ok'), lbSlow: $('#set-lb-slow'), lbDown: $('#set-lb-down'), lcHalf: $('#set-lc-half'), lcCeil: $('#set-lc-ceil'), lmHalf: $('#set-lm-half'), lmCeil: $('#set-lm-ceil'),
      bastionLive: $('#set-bastion-live'), srcLive: $('#set-src-live'), srcList: $('#set-src-list'), srcF: $('#set-src-f'), srcIn: $('#set-src-in'),
      nDep: $('#set-n-dep'), nPreview: $('#set-n-preview'), nHistory: $('#set-n-history'), nRes: $('#set-n-res'),
      langD: $('#set-lang-d'), autoLive: $('#set-auto-live'), autoD: $('#set-auto-d'), trayD: $('#set-tray-d'),
      updSt: $('#set-upd-st'), updMeta: $('#set-upd-meta'), verApp: $('#set-ver-app'), verCore: $('#set-ver-core'), updProg: $('#set-upd-prog'), updNotes: $('#set-upd-notes'), updInstall: $('#set-upd-install'), updPage: $('#set-upd-page'),
      geo: $('#set-geo'), geoErr: $('#set-geo-err'),
    };
  }

  function registerCommands() {
    const onPage = () => T.router.current === 'settings';
    const go = f => () => T.go('settings', { focus: f });
    /* palette words: the label (settings.palette.<cmd>), the fixed English keywords and the current
       language's own (settings.palette.keywords.<cmd>; pinyin only in zh_CN) */
    const cmd = (id, en, o) => Object.assign({ id: `set-${id.replace(/_/g, '-')}`, group: 'settings', label: () => t(`settings.palette.${id}`),
      get keywords() { return `${en} ${t(`settings.palette.keywords.${id}`)}`; } }, o);
    T.cmd.register([
      cmd('go_ks', 'killswitch whitelist', { icon: 'shield', run: go('killswitch') }),
      cmd('go_rules', 'rules routing', { icon: 'route', run: go('rules') }),
      cmd('go_dns', 'dns resolver', { icon: 'globe', run: go('dns') }),
      cmd('go_lat', 'latency', { icon: 'pulse', run: go('latency') }),
      cmd('go_bastion', 'bastion tier3', { icon: 'bastion', run: go('bastion') }),
      cmd('go_alerts', 'alerts notifications', { icon: 'bell', run: go('alerts') }),
      cmd('go_update', 'update', { icon: 'refresh', run: go('update') }),
      cmd('save', 'save', { label: () => { const b = T.ui.saveBarCurrent; return t('settings.palette.save', { n: b ? b.count : 0 }); }, icon: 'check', hint: 'Ctrl S', mutates: true,
        when: () => onPage() && !!(T.ui.saveBarCurrent && T.ui.saveBarCurrent.dirty), run: () => T.ui.saveBarCurrent && T.ui.saveBarCurrent.save() }),
      cmd('undo', 'undo discard', { icon: 'undo', mutates: true,
        when: () => onPage() && changedKeys().length > 0, run: () => { onUndo(); const b = T.ui.saveBarCurrent; if (b) b.hide(); } }),
    ]);
  }

  /* ================================================================ page */
  /* ---- a language switch re-renders the page (relocalize 'rerender', architecture §3.7 item 4) ----
     The model (base, draft, the snapshot, the rows' results) lives
     outside the DOM, so render() can run again on a fresh root: the registries the wiring fills
     (K, CHECKS, PENDING, TOKS) start over, and the long-lived listeners of the previous render are
     dropped. The snapshot holds what lives only in the DOM: the drafts (again, to restore them over
     the base the new render paints), the text typed into add fields and an open inline edit, the
     open channel drawer (its tab and advanced block), and the scroll position. */
  let lifeOffs = [];
  function resetRegistries() {
    Object.keys(K).forEach(k => { delete K[k]; });
    CHECKS.length = 0; PENDING.length = 0; TOKS.length = 0;
    lifeOffs.splice(0).forEach(off => { try { off(); } catch (e) { /* already gone */ } });
    if (tocApi && tocApi.destroy) tocApi.destroy();
  }
  const TYPED = () => [['rule', R.ruleIn], ['setName', R.setName], ['setUrl', R.setUrl], ['res', R.resIn], ['src', R.srcIn],
    ...TOKS.map(([k, tok]) => [`tok:${k}`, tok.input])];
  /* The page scroll and the drawer belong to the page only while it is shown */
  function snapshotState() {
    const typed = {};
    TYPED().forEach(([k, e]) => { if (e && e.value) typed[k] = e.value; });
    const edit = editingRow();
    return {
      draft: clone(draft),
      typed, ruleAct, ruleKind: ruleCond ? ruleCond.kind : null, setBehav, resType, srcType,
      edit: edit && edit._edit ? { id: edit.dataset.key, value: edit._edit.input.value } : null,
      // (T-P4-09) read without layout: the language switch has just relabelled the document, and a
      // layout read here would lay out the whole old page only to throw it away. The position is the
      // one the last scroll event saw; the last section reaching into the window is at most the one
      // after the index's current (its spy line is the window's midline): the new render shows it
      // and every one before it at once, so the position can be put back
      scroll: visible && host() ? scrollAt : null,
      lastSec: visible && scrollAt > 0 ? secAfterCurrent() : null,
    };
  }
  let scrollAt = 0;
  function secAfterCurrent() {
    const a = R.toc && R.toc.querySelector('.toc-a[aria-current="true"]');
    const i = a ? SECS.findIndex(x => x.id === a.dataset.target) : -1;
    return i < 0 ? SECS[SECS.length - 1].id : SECS[Math.min(SECS.length - 1, i + 1)].id;
  }
  function restoreState(s) {
    if (!s) return;
    // the drafts over the base the new render painted (rows, lists, the bar)
    const keys = Object.keys(K).filter(k => !k.startsWith('pend:') && editable(k) && s.draft && k in s.draft && !same(s.draft[k], base[k]));
    keys.forEach(k => { draft[k] = clone(s.draft[k]); });
    repaint(keys, false);
    // the add fields' choices and typed text (their pending drafts follow from the input events)
    if (s.ruleAct && ruleSeg) { ruleAct = s.ruleAct; ruleSeg.set(ruleAct); ruleHint(true); }
    if (s.ruleKind && ruleCond) ruleCond.setKind(s.ruleKind);
    if (s.setBehav && behavSel) { setBehav = s.setBehav; behavSel.set(setBehav); }
    if (s.resType && resSel) { resType = s.resType; resSel.set(resType); R.resIn.placeholder = RES_T[resType].ph; fieldNote(R.resF, '', false, true); }
    if (s.srcType && srcSel) { srcType = s.srcType; srcSel.set(srcType); R.srcIn.placeholder = SRC_T[srcType].ph; fieldNote(R.srcF, '', false, true); }
    // (T-P4-09: a field with typed text is shown at once, its section too: a pending draft counts
    // only text in a shown field)
    TYPED().forEach(([k, e]) => { if (e && s.typed && s.typed[k] != null) { showUpTo(e); e.value = s.typed[k]; e.dispatchEvent(new Event('input', { bubbles: true })); } });
    if (s.edit) {
      showUpTo(R.srcList);
      const row = R.srcList.querySelector(`.set-src-r[data-key="${s.edit.id}"]`);
      if (row) { editSrc(row); const inp = row.querySelector('input'); if (inp) { inp.value = s.edit.value; inp.dispatchEvent(new Event('input', { bubbles: true })); } }
    }
    track();
    const h = host();
    // (T-P4-09) a scroll write lays out the whole relabelled document at once: at the top nothing is
    // written, else it is written from the next frame's callback, whose layout is the one that
    // frame makes anyway (the frame is painted at the restored position)
    if (h && s.scroll) {
      if (s.lastSec) showUpTo(R.root.querySelector('#' + s.lastSec));
      // (in that frame: sections are shown one by one until the page is long enough for the
      // position, so it is never clamped short)
      requestAnimationFrame(() => {
        if (!visible) return;
        while (later.length && h.scrollHeight - h.clientHeight < s.scroll) show(later.shift());
        h.scrollTop = s.scroll;
      });
    }
  }

  T.page('settings', {
    title: T.t('shell.page.settings.title'),
    titleId: 'shell.page.settings.title',
    relocalize: 'rerender',
    snapshotState,
    restoreState,
    render(root, ctx) {
      resetRegistries();
      build(root); refs(root);
      // (T-P4-09) the sections below the first window wait for idle periods, from before the wiring:
      // a component that reads layout while it is set up (a segmented control placing its thumb)
      // then lays out only the first window, also when the page is shown (a language re-render)
      deferSections();
      wireConn(); wireRoute(); wireNet(); wireQuality(); wireBastion(); wireAlerts(); wireApp();
      paintAdmin(false);
      paintKnown(); paintStatus(false);
      // the spy's line is the window's midline (read on every pass, so a resize keeps it true)
      tocApi = T.ui.toc(R.toc, { scroller: host(), offset: 24, spyOffset: () => { const hh = host() ? host().clientHeight : 0; return hh > 200 ? Math.round(hh * 0.5) : 112; } });
      R.main.addEventListener('click', e => {
        const u = e.target.closest('.set-undo'); if (!u) return;
        const w = u.closest('.set-eff-w'); if (!w || !w._keys) return;
        const row = u.closest('.set-subrow, .frow'), had = u.contains(document.activeElement) || u === document.activeElement;
        revert(w._keys);
        // the tag (and its undo) leaves with the draft: focus goes to the row's own control
        if (had && row) focusNow(T.$$(ROW_CTL, row).find(isShown));
      });
      syncRuntime(false);
      paintFeatures();
      track();
      // (T-P4-09) a jump from the index lays out every section first (capture phase: before T.ui.toc
      // measures its target): the index keeps the section it was sent to, as when all were laid
      // out (a section shown later would make its spy look again)
      R.toc.addEventListener('click', e => { if (e.target.closest('[data-target]') && later.length) showUpTo(later[later.length - 1]); }, true);
      if (host()) ctx.life.listen(host(), 'scroll', () => { if (visible) scrollAt = host().scrollTop; }, { passive: true });
      if (!ctx.relocalized) registerCommands();
      // long-lived listeners: kept in lifeOffs so a re-render (language switch) replaces them
      const life = { on: (e, fn) => { const off = ctx.life.on(e, fn); lifeOffs.push(off); return off; }, sub: (k, fn, o) => { const off = ctx.life.sub(k, fn, o); lifeOffs.push(off); return off; } };
      const topic = (name, key, fn) => { if (!T.rt) return; const off = T.rt.topic(name, key).sub(fn); lifeOffs.push(off); };
      life.on('rt:state', () => { settleGeneration(); syncRuntime(visible); if (visible) track(); paintUpd(visible); });
      life.sub('admin', () => paintAdmin(visible));
      life.sub('systemProxy', () => paintModeStatus(visible));
      life.sub('bastion', () => { paintBastionLive(visible); if (R.bastionMinCtl) R.bastionMinCtl.hint(); });
      life.sub('core', () => paintVersion(visible));
      life.sub('update', () => paintUpd(visible));
      life.on('rt:mode', e => {
        if (e.busy) { pendingMode = e.mode; modeNote = null; }
        syncModeSwitches();
        clearTimeout(modeFlashT);
        paintModeStatus(visible, !e.busy && visible && !modeNote);
        if (!e.busy) { pendingMode = null; syncModeSwitches(); modeFlashT = setTimeout(() => paintModeStatus(visible), modeNote ? 8000 : 2400); }
        paintBypass(visible);
      });
      life.sub('modeKnown', () => { syncModeSwitches(); paintBypass(visible); });
      life.sub('killswitch', () => paintKsLive(visible));
      life.on('rt:config', e => onConfigChanged(e));
      life.on('rt:reset', e => onGeneration(e));
      // a new runtime generation (hello after rt:reset): read what the page could not read before
      life.on('rt:caps', () => { if (visible && (snap.state === 'failed' || !snap.ever)) loadAll(); syncModeSwitches(); paintFeatures(); });
      life.on('host:language', e => {
        if (langWait && e && e.language === langWait.lang) { clearTimeout(langWait.timer); langWait = null; langNote = null; }
        else if (langNote && langNote.kind === 'reopen' && e && e.language === base.lang) { langNote = null; paintLangNote(visible); }   // a late push: it did switch
      });
      topic('bastion.sources', null, () => onSourcesTopic());
      topic('update.state', null, () => paintUpd(visible));
      topic('geo.status', null, () => { const g = T.rt.topic('geo.status').get(); if (g) { geo = g; paintGeo(visible); } });
      // the latency preview follows nodes.list calmly (at most once a second)
      let latT = 0;
      const latSoon = () => { if (latT) return; latT = setTimeout(() => { latT = 0; if (visible) paintLat(draft.latency, true); }, 1000); };
      ['vanguard', 'sentinel', 'gateway'].forEach(k => topic('nodes.list', k, latSoon));
      lifeOffs.push(() => clearTimeout(latT));
    },
    enter(ctx) {
      visible = true;
      if (!ctx.relocalized && !ctx.again) scrollAt = 0;      // (the router put the host back to the top)
      showSoon();
      // B-27's fallback: every visit reads the saved configuration again (not a language re-render)
      // (a new visit: the pending rows of an earlier one follow the first snapshot by intent id only)
      if (!ctx.relocalized) { extNotice = false; if (pendTrack && !ctx.again) pendTrack.idsOnly = true; loadAll(); }
      syncRuntime(false);
      paintLat(draft.latency, false, true);        // no layout read here: the dots follow from the ResizeObserver / next frame
      requestAnimationFrame(() => { if (visible) paintLatDots(draft.latency); });
      paintUpd(false); paintGeo(false); paintVersion(false); paintAuto(false);
      track();                                    // drafts that survived a forced navigation bring their bar back
      if (ctx.params && ctx.params.focus) focusTo(ctx.params.focus);
    },
    leave() { visible = false; },
    /* asks from the model, not from the bar's state: an edit made while a save ran is a draft even
       while the bar still shows the result. A running save is waited for, then the question is asked
       only if drafts remain. */
    beforeLeave() {
      const ask = () => {
        if (!changedKeys().length) return true;
        let b = T.ui.saveBarCurrent;
        if (!b || b.page !== 'settings') { track(); b = T.ui.saveBarCurrent; }
        return askLeave(b);
      };
      return saving ? saving.then(ask, ask) : ask();
    },
  });
  /* the shell's quit question (architecture §2.9) adds T.ui.saveBar.dirtyCount() and the registered
     drafts. A channel drawer and an inline source edit write this page's drafts (the bar counts
     them, typed text included as pend:* keys), so while the bar is live and dirty this adds 0;
     drafts the bar does not hold (another page shown after a forced navigation, the bar showing a
     result while an edit made during the save waits) are counted here instead, never twice. */
  if (T.shellc && T.shellc.registerDraft) {
    T.shellc.registerDraft(() => {
      if (!R.root) return 0;
      const b = T.ui.saveBarCurrent;
      if (b && b.page === 'settings' && b.dirty) return 0;
      return countAll(changedKeys());
    });
  }
})();
