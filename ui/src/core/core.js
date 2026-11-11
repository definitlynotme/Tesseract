/* ==========================================================================
   core.js — window.T : the shared runtime every page builds on
   --------------------------------------------------------------------------
   Classic script (no modules; works from file://). Load order:
     core.js → icons.js → data.js → sim.js → pages/*.js → app.js

   DOM
     T.$(sel, root) / T.$$(sel, root)        query helpers ($$ returns an array)
     T.esc(str)                               HTML-escape
     T.raw(str)                               mark a string as trusted HTML: a literal
                                              or T.icon() only (scan.mjs --rule inject,
                                              P4 architecture §7)
     T.html`<div>${x}</div>`                  tagged template → Element (or
                                              DocumentFragment if several roots);
                                              interpolations are escaped unless
                                              T.raw()/T.icon(); a Node goes in as
                                              itself; arrays are joined. A tag only:
                                              any other call form throws (T-P4-07)
     T.h(tag, attrs, ...children)             create an element. attrs: class,
                                              style (string → cssText | object →
                                              setProperty, never the attribute:
                                              CSP, T-P4-08b), text,
                                              dataset {}, on:{click(){}} or
                                              onClick, any other attribute;
                                              true → '' , false/null → omitted.
                                              No html attribute (T-P4-07: it throws;
                                              markup is a Node child); an on* value
                                              that is not a function and srcdoc throw
   Events / state
     T.on(evt, fn) → off()   T.once   T.off   T.emit(evt, payload)
     T.store.get(k) / set(k, v) / patch({…}) / update(k, fn) / sub(k, fn, {now}) → off()
       Keys written by the foundation (see sim.js): run, phase, cancelling,
       uptime, carrier, order, fallback, degraded, collected, tiers, pool,
       mode, modeBusy, killswitch, exit, traffic, page; cross-page settings
       from boot: admin, ksEnabled, ksBlocked, bastion, latCeiling, update
   Pages / router
     T.page(id, { title, titleId, render(root, ctx), enter(ctx), leave(ctx),
                  relocalize: 'rerender', snapshotState() → state, restoreState(state) })
        title: the crumb / window title is the message titleId, else shell.page.<id>.title,
        else title (T-P4-04a); it follows the language (i18n:change).
        relocalize 'rerender' (architecture §3.7 item 4): on a language switch the page is
        rendered again, one order on both paths (snapshotState → [leave when shown] → fresh
        life scope, empty root → render → enter → restoreState; every hook guarded). The page
        shown gets ctx.relocalized and params {} (no one-time landing such as focus is replayed;
        the last navigation's params are the frozen ctx.navParams); a page that is not shown is
        redone on its next visit, with that navigation's params, restored after it has entered.
        Without it only data-i18n relabels (core/README.md).
        render: once, the first time the page is shown.
        enter/leave: every visit. ctx = { id, root, params, from, first,
        scope, life }. scope auto-disposes on leave:
          scope.on(evt, fn) · scope.sub(key, fn) · scope.every(ms, fn)
          scope.after(ms, fn) · scope.listen(el, type, fn) · scope.add(fn)
        life: same API, never disposed (use it in render()).
        If T.go targets the page already shown, enter(ctx) runs again with
        ctx.again = true and the new params.
     T.go(id, opts)   ids: overview · profiles (alias tiers) · logs · settings
        opts: any params for the page (e.g. { tier:'sentinel', focus:'ks' }); they reach
        render and enter as ctx.params (focus: the section to land on, read by the page in
        enter(ctx) from ctx.params.focus, a one-time intent of this navigation only; see
        core/README.md, cross-page interfaces),
        plus flipFrom: Element + flipKey: 'tier:sentinel' → shared-element
        hand-off onto the element in the new page that carries
        [data-flip-target="tier:sentinel"] (falls back to the page top).
     T.router.current  T.router.titles
   Notices (severity by shape + word + colour)
     T.notify({ kind, title, message, actions:[{label, primary, run, keep}],
                sticky, timeout, id }) → { dismiss() }
        success/info → toast pill in the top bar centre, countdown ring
                       (default 4 s / 5 s; sticky:true to keep)
        warning/error → banner: a page of the floating card under the top bar's
                       right end that covers the page, never moves it (F-05);
                       one banner at a time, dots page between several; it
                       peeks open, then tucks into the capsule in the top bar;
                       persistent unless timeout given
        info + banner:true → the same banner, neutral, without a level word (F-01)
     T.dismiss(id)    T.dismissToast()
   Overlays (in-page, never window.confirm)
     T.confirm({ title, message, confirmLabel, cancelLabel, danger }) → Promise<boolean>
     T.drawer.open(o) / T.drawer(o) · T.sheet.open(o) / T.sheet(o) → api
        o: { title, subtitle, width, height, content, actions, head, from (container
             morph from an element), modal:false (drawer: side panel, no scrim), dock (modeless
             drawer docked inside an element's box), focus, onClose }
        content: Element | html string | fn(box, api). actions: like notify (+ danger, icon, left)
        api = { el, head, body, foot, close(), setTitle(t), setSubtitle(t),
                setContent(c, {dir}), setActions(a), action(i), place(), closed }
     T.overlayOpen() (modal only) · T.overlays.closeModeless() / closeFloats()
     T.overlays.open(panel, { inT, outT, onClose, beforeClose }) → { close(val), cancel() }:
        a page-built modal panel on the same stack (the letter, letter/letter.js)
     Page def may add beforeLeave(ctx, toId) → false | Promise<boolean> to hold navigation.
   Command palette
     T.cmd.register({ id, group, label, icon, hint, keywords, run, disabled, when, mutates, groupLast, data })
        label/hint/keywords/disabled/when may be functions (evaluated on open); mutates:true
        marks a state-changing command, which is never pre-selected on open. data:true: the label
        and the group name are not catalog words (translate="no"; T-P4-07, the design demo block).
        group is an id: service · go · mode · overview · profiles · logs · settings
        (named by shell.palette.group.<id>; ordered in that order). Any other value is
        shown as given, after the known groups (groupLast:true: after all of them);
        blocks with the same shown name are one block.
     T.cmd.open() / close() / toggle()
   UI library (T.ui) — full contract in DESIGN.md §5.3 / §6
     busy(btn, job, {busy, done, fail, …}) · reserve(btn, labels) · seg · switch · options
     field · number · search · select · menu(anchor, items) · popover · closeFloats
     slider · tabs · listNav · fold · sortable · toc · spark · saveBar · meter · progress
     saveBar.dirtyCount() → number (unsaved items of the live save bar; T-P4-04a)
     status · lat · dots · tierTag · kbd · tech · callout · empty · skeleton · copy · copyText · hideTip
     [data-tip="…"] (+ data-tip-kbd="Ctrl K", data-tip-pos="bottom") gives a tooltip.
     Markup-only: .switch / .fchip toggle and fire 'change'; [data-go="page" data-go-x="…"] navigates.
     Keys: Ctrl F → the visible .search[data-hotkey]; Ctrl S → the save bar.
   Motion (T.motion)
     reduced                      the shell's __TESSERACT_MOTION__.reduced OR live
                                  prefers-reduced-motion (T-P4-06); html.reduced-motion
     ease / SP                    CSS easings incl. spring presets {easing, duration}
     anim(el, keyframes, opts)    WAAPI wrapper; reduced motion → 1 ms
     spring(el, {x,y,scale,sx,sy,opacity}, {k,c,instant,from,onRest})
                                  JS spring that carries velocity through interruptions
     swapText(el, text | Node, {dir})  overlapped vertical roll (no blank frames); text is
                                  text, markup is a Node (T-P4-07: no {html})
     odometer(el, text, {mode:'forward'|'auto', label})  digit strips, masked,
                                  aria-label = real value, strips aria-hidden
     tweenNumber(el, to, fmt, {dur})  one-off numeric tween (not for 1 Hz streams)
     flip(els, mutate, {duration, easing})  transform-only FLIP
     stagger(els, {base, step, dur, y, max})  entrance (<= 400 ms total)
     handoff(srcEl, targetEl, opts)   shared-element transform FLIP
     list(container, mutate, {highlight, from, stagger, enterDelay, followers})  enter / leave / reorder
     morph(src, target, {reverse})    container transform (drawer/sheet `from`)
   Formatting (T.fmt): bytes, bytesParts, rate, rateParts, ms, ago, time, hm,
     clock, uptime, pct, num, latBand, latWord
   Utilities (T.util): clamp, lerp, rng(seed), raf(fn) throttle
   ========================================================================== */
import { fmt as intlFmt } from '../i18n/intl.js'; // T.fmt (T-P4-03)
import { registerDraft as shellcRegisterDraft } from '../shellc/quit.js'; // T.shellc.registerDraft (T-P4-06)
import { createReducedMotion } from '../shellc/presence.js'; // T.motion.reduced (T-P4-06)
(function () {
  'use strict';
  const T = window.T = window.T || {};
  const doc = document;
  const rootEl = doc.documentElement;

  /* ------------------------------------------------------------------ DOM */
  T.$ = (s, r) => (r || doc).querySelector(s);
  T.$$ = (s, r) => Array.from((r || doc).querySelectorAll(s));
  const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  T.esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ESC[c]);
  function Raw(s) { this.s = String(s); }
  Raw.prototype.toString = function () { return this.s; };
  T.raw = s => (s instanceof Raw ? s : new Raw(s));
  const part = v => v == null || v === false ? '' : v instanceof Raw ? v.s : Array.isArray(v) ? v.map(part).join('') : T.esc(v);
  // T-P4-03 (architecture §3.3): an interpolated Node (e.g. a T.tx fragment) goes in as a
  // placeholder comment and is swapped in after parsing, so it is never serialised or parsed
  // as HTML. A Node in an attribute value contributes its textContent (via setAttribute).
  // The placeholder carries a random per-call nonce, so escaped user text that decodes to
  // "<!--t-node-…-->" inside an attribute cannot forge one; an index with no node is left as
  // it is. A Node that ends up where it cannot be swapped in (an unquoted attribute, or raw
  // text such as <textarea>/<title>) is dropped; development builds warn about it.
  const htmlNonce = () => {
    const c = window.crypto;
    if (c && c.getRandomValues) { const a = new Uint32Array(2); c.getRandomValues(a); return a[0].toString(36) + a[1].toString(36); }
    return Math.random().toString(36).slice(2) + Math.random().toString(36).slice(2);
  };
  const htmlDev = typeof __I18N_DEV__ !== 'undefined' && __I18N_DEV__;
  // T-P4-07 fix round 1: a tagged template only. Called as a plain function (T.html([s]),
  // T.html.call(null, [s]), Reflect.apply) `strings` would be caller data parsed as markup, so
  // anything but the engine's frozen strings array (with its frozen .raw) is refused.
  T.html = function (strings, ...vals) {
    if (!Array.isArray(strings) || !Array.isArray(strings.raw) || !Object.isFrozen(strings)) throw new TypeError('T.html: a tagged template only');
    const tag = `t-node-${htmlNonce()}-`;
    const nodes = [];
    const put = v => {
      if (v instanceof Node) { nodes.push(v); return `<!--${tag}${nodes.length - 1}-->`; }
      if (Array.isArray(v)) return v.map(put).join('');
      return part(v);
    };
    let out = '';
    strings.forEach((s, i) => { out += s; if (i < vals.length) out += put(vals[i]); });
    const t = doc.createElement('template');
    t.innerHTML = out.trim();
    const c = t.content;
    if (nodes.length) {
      const used = new Set();
      const marks = [];
      const re = new RegExp(`<!--${tag}(\\d+)-->`, 'g');
      const w = doc.createTreeWalker(c, NodeFilter.SHOW_COMMENT | NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT);
      for (let n = w.nextNode(); n; n = w.nextNode()) {
        if (n.nodeType === 8 && n.data.startsWith(tag)) marks.push(n);
        else if (n.nodeType === 3) { if (n.data.includes(`<!--${tag}`)) n.data = n.data.replace(re, ''); } // raw text (<textarea>, <title>)
        else if (n.nodeType === 1) {
          for (const a of Array.from(n.attributes)) {
            if (a.value.includes(`<!--${tag}`)) n.setAttribute(a.name, a.value.replace(re, (m, k) => { if (!nodes[+k]) return m; used.add(+k); return nodes[+k].textContent; }));
          }
        }
      }
      for (const m of marks) {
        const k = +m.data.slice(tag.length);
        if (nodes[k] && !used.has(k)) { used.add(k); m.replaceWith(nodes[k]); } else m.remove();
      }
      if (htmlDev && used.size < nodes.length) console.warn(`[T.html] ${nodes.length - used.size} interpolated Node(s) dropped: a Node can go in element content or a quoted attribute value, not in an unquoted attribute or raw text (<textarea>, <title>)`);
    }
    return c.childNodes.length === 1 && c.firstElementChild ? c.firstElementChild : c;
  };
  // T-P4-08b (CSP style-src 'self', P4 architecture §9): a style goes through the CSSOM only, never
  // the style attribute: a string is el.style.cssText, an object one setProperty per entry
  // (custom properties `--x` as they are, camelCase names as their CSS names; null / false
  // removes). Any spelling of the key (STYLE, Style) takes this path, never setAttribute.
  const cssName = k => (k.startsWith('--') ? k : k === 'cssFloat' ? 'float'
    : k.replace(/^(webkit|moz|ms)(?=[A-Z])/, '-$1').replace(/[A-Z]/g, m => '-' + m.toLowerCase()));
  const setStyle = (el, v) => {
    if (typeof v === 'string') { el.style.cssText = v; return; }
    for (const k in v) {
      const x = v[k];
      if (x == null || x === false) el.style.removeProperty(cssName(k)); else el.style.setProperty(cssName(k), String(x));
    }
  };
  T.h = function (tag, attrs, ...kids) {
    const el = doc.createElement(tag);
    if (attrs) for (const k in attrs) {
      const v = attrs[k];
      if (v == null || v === false) continue;
      if (k === 'class') el.className = v;
      else if (k.toLowerCase() === 'style') setStyle(el, v);
      else if (k === 'text') el.textContent = v;
      // T-P4-07 (P4 architecture §7): no markup strings; build the markup as a Node child
      else if (k === 'html') throw new TypeError('T.h: the html attribute is not accepted; pass a Node (T.html / T.tx / T.h) as a child');
      else if (k === 'dataset') Object.assign(el.dataset, v);
      else if (k === 'on') { for (const e in v) el.addEventListener(e, v[e]); }
      // T-P4-07 fix round 1: an on* attribute is a listener (a function) or an error, never a
      // string handler; srcdoc (markup in an attribute) is refused like html
      else if (/^on/i.test(k)) {
        if (typeof v !== 'function') throw new TypeError(`T.h: ${k} takes a function`);
        el.addEventListener(k.slice(2).toLowerCase(), v);
      } else if (/^srcdoc$/i.test(k)) throw new TypeError('T.h: the srcdoc attribute is not accepted');
      else el.setAttribute(k, v === true ? '' : v);
    }
    for (const c of kids.flat()) {
      if (c == null || c === false) continue;
      el.append(c instanceof Node ? c : c instanceof Raw ? T.html`${c}` : String(c));
    }
    return el;
  };
  const isShown = el => !!(el && el.isConnected && el.getClientRects().length);

  /* ------------------------------------------------------------ event bus */
  const bus = new Map();
  T.on = (e, fn) => { let s = bus.get(e); if (!s) bus.set(e, s = new Set()); s.add(fn); return () => T.off(e, fn); };
  T.off = (e, fn) => { const s = bus.get(e); if (s) s.delete(fn); };
  T.once = (e, fn) => { const off = T.on(e, p => { off(); fn(p); }); return off; };
  T.emit = (e, p) => {
    const s = bus.get(e); if (!s) return;
    for (const fn of Array.from(s)) { try { fn(p); } catch (err) { setTimeout(() => { throw err; }); } }
  };

  /* ---------------------------------------------------------------- store */
  const state = Object.create(null), subs = new Map();
  T.store = {
    get: k => state[k],
    set(k, v) {
      const prev = state[k];
      if (Object.is(prev, v)) return;
      state[k] = v;
      const s = subs.get(k);
      if (s) for (const fn of Array.from(s)) { try { fn(v, prev); } catch (err) { setTimeout(() => { throw err; }); } }
    },
    patch(o) { for (const k in o) T.store.set(k, o[k]); },
    update(k, fn) { T.store.set(k, fn(state[k])); },
    sub(k, fn, opts) {
      let s = subs.get(k); if (!s) subs.set(k, s = new Set()); s.add(fn);
      if (opts && opts.now) fn(state[k], undefined);
      return () => s.delete(fn);
    },
    snapshot: () => Object.assign({}, state),
  };

  /* -------------------------------------------------------- extension points
     T.ext.set(name, value) · T.ext.get(name) → value | null (T-P4-07). A page names a place where
     another build may add to it and documents the value it takes ('overview/deck': { mount(after),
     sync({run, fallback}) }; 'logs/advice': record → { title, text, label?, go?, params? } | null;
     a slash, not a dot, so the names never read as message ids).
     The design target's design/demo.js registers its demo controls there; the app target
     registers nothing, so in the product every extension point is empty (P4 architecture §6:
     no demo code reaches the app target). */
  const EXT = new Map();
  T.ext = Object.freeze({
    set(name, value) { EXT.set(String(name), value); },
    get: name => (EXT.has(String(name)) ? EXT.get(String(name)) : null),
  });

  /* ----------------------------------------------------------- utilities */
  T.util = {
    clamp: (v, a, b) => Math.max(a, Math.min(b, v)),
    lerp: (a, b, t) => a + (b - a) * t,
    rng(seed) {
      let a = seed >>> 0;
      return function () { a |= 0; a = a + 0x6D2B79F5 | 0; let t = Math.imul(a ^ a >>> 15, 1 | a); t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t; return ((t ^ t >>> 14) >>> 0) / 4294967296; };
    },
    raf(fn) { let id = 0, args; return function (...a) { args = a; if (!id) id = requestAnimationFrame(() => { id = 0; fn(...args); }); }; },
    fontsReady: () => FONTS.p,
  };
  /* T.util.fontsReady() → a Promise that settles once no web font is loading (T-P4-09). Reading
     document.fonts.ready lays out the whole document at once in Chrome / WebView2 (measured: 39 ms
     on 配置 right after a language change, 94 ms for ja-JP), so it is read once, here, while the
     document is still small; later loads are followed through the FontFaceSet events. */
  const FONTS = { p: Promise.resolve(), res: null };
  if (doc.fonts && doc.fonts.addEventListener) {
    try { FONTS.p = doc.fonts.ready; } catch (e) { /* no font loading API: resolved */ }
    doc.fonts.addEventListener('loading', () => { if (!FONTS.res) FONTS.p = new Promise(r => { FONTS.res = r; }); });
    const settle = () => { const r = FONTS.res; FONTS.res = null; if (r) r(); };
    doc.fonts.addEventListener('loadingdone', settle);
    doc.fonts.addEventListener('loadingerror', settle);
  }

  /* ------------------------------------------------------------ formatting */
  // T-P4-03: delegated to i18n/intl.js (Intl, per-language caches, `fmt` catalog). Same
  // names and return shapes: KB, MB, GB, bytesParts → [num, unit], bytes, rateParts →
  // [num, unit], rate, ms, ago, time, hm, clock, uptime, pct, num, latBand, latWord;
  // new: region, list, compare, collator. Output changes: T-P4-03 report, 允许差异表.
  T.fmt = intlFmt;

  /* ================================================================ motion */
  // T-P4-06 (G52, criticism D4): reduced = the shell's injected window.__TESSERACT_MOTION__.reduced
  // (T-P4-C3) OR prefers-reduced-motion (followed live); html.reduced-motion mirrors it for CSS,
  // since the injected flag does not reach the media query (css/shell.css)
  const RM = createReducedMotion(window);
  let reduced = RM.reduced;
  const markReduced = () => rootEl.classList.toggle('reduced-motion', reduced);
  markReduced();
  RM.onChange(v => { reduced = v; markReduced(); });

  function springCurve(k, c) {
    const dt = 1 / 1000; let x = 0, v = 0, t = 0; const xs = [0];
    while (t < 2.5) {
      const a = -k * (x - 1) - c * v; v += a * dt; x += v * dt; t += dt; xs.push(x);
      if (t > 0.05 && Math.abs(1 - x) < 0.001 && Math.abs(v) < 0.02) break;
    }
    const n = 40, pts = [];
    for (let i = 0; i <= n; i++) pts.push(i === 0 ? 0 : i === n ? 1 : +xs[Math.round((xs.length - 1) * i / n)].toFixed(4));
    return { easing: `linear(${pts.join(', ')})`, duration: Math.round(t * 1000), k, c };
  }
  const hasLinear = !!(window.CSS && CSS.supports && CSS.supports('transition-timing-function', 'linear(0, 1)'));
  const mk = (k, c, fb) => hasLinear ? springCurve(k, c) : { easing: 'cubic-bezier(.2,.9,.25,1)', duration: fb, k, c };
  const SP = {
    press: mk(560, 46, 300),    // controls: no overshoot
    soft: mk(380, 38, 380),     // panels, notices
    glide: mk(520, 44, 320),    // indicators
    settle: mk(300, 34, 460),   // deck cards, shared elements
  };
  const EASE = { out: 'cubic-bezier(.2,.8,.2,1)', in: 'cubic-bezier(.4,0,1,1)', inout: 'cubic-bezier(.45,0,.2,1)', quart: 'cubic-bezier(.25,1,.5,1)' };
  rootEl.style.setProperty('--spring', SP.press.easing);
  rootEl.style.setProperty('--spring-dur', SP.press.duration + 'ms');
  rootEl.style.setProperty('--spring-soft', SP.soft.easing);
  rootEl.style.setProperty('--spring-soft-dur', SP.soft.duration + 'ms');

  function anim(el, kf, opts) {
    if (!el || !el.animate) return null;
    if (reduced) opts = Object.assign({}, opts, { duration: 1, delay: 0, endDelay: 0 });
    return el.animate(kf, opts);
  }
  /** run fn once an animation ends (with a timeout fallback if the clock stalls) */
  function onDone(a, fn, ms) {
    let done = false; const f = () => { if (!done) { done = true; fn(); } };
    if (a) { a.addEventListener('finish', f); a.addEventListener('cancel', f); setTimeout(f, ms || 600); } else f();
  }

  /* -- JS spring with velocity carry-over (interruptible) ------------------ */
  const loop = { set: new Set(), on: false, last: 0 };
  function loopTick(now) {
    const dt = Math.min(0.05, Math.max(0, (now - loop.last) / 1000)); loop.last = now;
    for (const s of Array.from(loop.set)) { if (!stepSpring(s, dt)) loop.set.delete(s); applySpring(s); if (!loop.set.has(s) && s.onRest) { const f = s.onRest; s.onRest = null; f(); } }
    if (loop.set.size) requestAnimationFrame(loopTick); else loop.on = false;
  }
  const EPS = { x: 0.05, y: 0.05, rotate: 0.05, scale: 0.0004, sx: 0.0004, sy: 0.0004, opacity: 0.002 };
  function stepSpring(s, dt) {
    let alive = false;
    for (let rem = dt; rem > 1e-6; rem -= 1 / 240) {
      const h = Math.min(rem, 1 / 240);
      for (const p in s.vals) {
        const q = s.vals[p]; if (q.rest) continue;
        const a = -s.k * (q.x - q.t) - s.c * q.v; q.v += a * h; q.x += q.v * h;
      }
    }
    for (const p in s.vals) {
      const q = s.vals[p], e = EPS[p] || 0.01;
      if (Math.abs(q.x - q.t) < e && Math.abs(q.v) < e * 12) { q.x = q.t; q.v = 0; q.rest = true; } else { q.rest = false; alive = true; }
    }
    return alive;
  }
  function applySpring(s) {
    const V = p => (s.vals[p] ? s.vals[p].x : null);
    const x = V('x'), y = V('y');
    if (x != null || y != null) s.el.style.translate = `${(x || 0).toFixed(2)}px ${(y || 0).toFixed(2)}px`;
    const sc = V('scale'), sx = V('sx'), sy = V('sy');
    if (sc != null) s.el.style.scale = sc.toFixed(4);
    else if (sx != null || sy != null) s.el.style.scale = `${(sx == null ? 1 : sx).toFixed(4)} ${(sy == null ? 1 : sy).toFixed(4)}`;
    const r = V('rotate'); if (r != null) s.el.style.rotate = r.toFixed(2) + 'deg';
    const o = V('opacity'); if (o != null) s.el.style.opacity = T.util.clamp(o, 0, 1).toFixed(3);
  }
  function spring(el, props, o) {
    o = o || {};
    const s = el._spring || (el._spring = { el, vals: {}, k: 520, c: 44 });
    if (o.k) s.k = o.k; if (o.c) s.c = o.c;
    if (o.onRest) s.onRest = o.onRest;
    for (const p in props) {
      const q = s.vals[p];
      if (!q) s.vals[p] = { x: o.from && o.from[p] != null ? o.from[p] : props[p], v: 0, t: props[p], rest: false };
      else { q.t = props[p]; q.rest = false; }
    }
    if (reduced || o.instant) {
      for (const p in s.vals) { const q = s.vals[p]; q.x = q.t; q.v = 0; q.rest = true; }
      loop.set.delete(s); applySpring(s);
      if (s.onRest) { const f = s.onRest; s.onRest = null; f(); }
      return s;
    }
    loop.set.add(s);
    if (!loop.on) { loop.on = true; loop.last = performance.now(); requestAnimationFrame(loopTick); }
    return s;
  }

  /* -- text swap: overlapped vertical roll; old label stays until new is in --
     The labels stack in one grid cell. A host whose own display wins over .swap
     (a flex row such as .field-msg, .prov, .dc-node) would lay the two labels
     side by side, so such a host gets an inner .swap-box (decided once). */
  function swapBox(el) {
    el.classList.add('swap');                                      // again every call: a className overwrite drops it
    if (el._swapBox !== undefined) return el._swapBox || el;
    const d = getComputedStyle(el).display;
    if (d === 'none') return el;                                   // not laid out yet (e.g. [hidden]): decide later
    if (d === 'inline-grid' || d === 'grid') { el._swapBox = null; return el; }
    const box = doc.createElement('span'); box.className = 'swap-box';
    el.replaceChildren(box); el._swapBox = box;
    return box;
  }
  // the swap key of a Node face: its markup, read (never parsed back); the \u0002 prefix keeps it
  // apart from a text face (pages key their own node faces with \u0001 + text)
  function nodeKey(node) {
    const b = doc.createElement('div');
    b.append(node.cloneNode(true));
    return '\u0002' + b.innerHTML;
  }
  // T-P4-07 (P4 architecture §7): the face is text, or a Node built by the caller (T.html / T.tx /
  // T.h); {html} strings are gone (it throws, so a leftover caller fails loudly instead of
  // showing markup as text)
  function swapText(el, text, o) {
    o = o || {};
    if (o.html) throw new TypeError('swapText: {html} is not accepted; pass a Node');
    if (!el) return;
    const node = text instanceof Node ? text : null;
    const val = node ? nodeKey(node) : String(text == null ? '' : text);
    if (el._swap === val) return;
    const first = el._swap === undefined; el._swap = val;
    const host = swapBox(el);
    const nu = doc.createElement('span'); nu.className = 'sw';
    if (node) nu.append(node); else nu.textContent = val;
    if (first || o.instant || reduced || !isShown(el)) { host.replaceChildren(nu); return; }
    const dir = o.dir || 1;
    // inside an open wave the roll waits for the element's distance from the wave's origin
    const wd = o.delay != null ? o.delay : waveDelay(el);
    Array.from(host.children).forEach(old => {
      if (old._out) return; old._out = true;
      old.setAttribute('aria-hidden', 'true');
      const a = old.animate([{ opacity: 1, transform: 'none' }, { opacity: 0, transform: `translateY(${-0.5 * dir}em)` }], { duration: 170, delay: wd, easing: EASE.in, fill: 'forwards' });
      onDone(a, () => old.remove(), 420 + wd);
    });
    host.append(nu);
    nu.animate([{ opacity: 0, transform: `translateY(${0.5 * dir}em)` }, { opacity: 1, transform: 'none' }], { duration: 240, delay: 30 + wd, easing: EASE.out, fill: 'backwards' });
  }
  /** forget swapText state (the caller is about to set the element's content itself) */
  function swapReset(el) { if (!el) return; el._swap = undefined; el._swapBox = undefined; el.classList.remove('swap'); }

  /* -- wave: one state change travelling outward from a point (F-06, the start) ---------------
     wave.start({ x, y }) opens a short window. Inside it swapText and odometer (and any caller that
     asks wave.delay(el)) hold an element's change for its distance from the point, so a page that
     changes all at once lights up outward from the control that was pressed. One shot: the window
     closes by itself, nothing runs after it; reduced motion never opens it. */
  const WAVE = { t0: 0, x: 0, y: 0, k: 0.5, max: 460, until: 0 };
  function waveStart(pt, o) {
    o = o || {};
    if (reduced || !pt) { WAVE.until = 0; return; }
    WAVE.t0 = performance.now(); WAVE.x = pt.x; WAVE.y = pt.y;
    WAVE.k = o.speed || 0.5; WAVE.max = o.max != null ? o.max : 460; WAVE.until = WAVE.t0 + (o.span || 1600);
  }
  /** ms an element's change still waits inside the open wave (0 when none is open) */
  function waveDelay(el) {
    if (!el || !WAVE.until || reduced) return 0;
    const now = performance.now();
    if (now > WAVE.until) { WAVE.until = 0; return 0; }
    const r = el.getBoundingClientRect();
    if (!r.width && !r.height) return 0;
    const d = Math.hypot(r.left + r.width / 2 - WAVE.x, r.top + r.height / 2 - WAVE.y);
    return Math.max(0, Math.round(WAVE.t0 + Math.min(WAVE.max, d * WAVE.k) - now));
  }

  /* -- odometer ------------------------------------------------------------ */
  const isD = c => c >= '0' && c <= '9';
  const LH = 1.2; // em per digit row (matches .odo-d)
  function odoSnap(s, d) { s.style.transition = 'none'; s.style.transform = `translateY(${-d * LH}em)`; s._d = s._pos = d; void s.offsetWidth; s.style.transition = ''; }
  function odoBuild(el, st, text) {
    const v = doc.createElement('span'); v.className = 'odo-v'; v.setAttribute('aria-hidden', 'true');
    st.cells = [];
    for (const c of text) {
      if (isD(c)) {
        const d = doc.createElement('span'); d.className = 'odo-d';
        const s = doc.createElement('span'); s.className = 'odo-s';
        for (let k = 0; k < 20; k++) { const g = doc.createElement('span'); g.textContent = String(k % 10); s.append(g); }
        s.style.transform = `translateY(${-c * LH}em)`; s._d = s._pos = +c;
        s.addEventListener('transitionend', () => { if (s._d !== s._pos) odoSnap(s, s._d); });
        d.append(s); v.append(d); st.cells.push(s);
      } else { const x = doc.createElement('span'); x.className = 'odo-c'; x.textContent = c; v.append(x); st.cells.push(null); }
    }
    st.sel = doc.createElement('span'); st.sel.className = 'sr-only'; st.sel.textContent = text;
    el.replaceChildren(st.sel, v);
    return v;
  }
  function odometer(el, text, o) {
    o = o || {};
    if (!el) return;
    const val = String(text);
    const st = el._odo || (el._odo = { str: null, cells: [] });
    el.classList.add('odo'); el.setAttribute('role', 'img'); el.setAttribute('aria-label', o.label || val);
    if (st.str === val) return;
    const prev = st.str; st.str = val;
    const animate = !o.instant && !reduced && prev != null && isShown(el);
    const same = prev != null && prev.length === val.length && Array.from(val).every((c, i) => isD(c) === isD(prev[i]) && (isD(c) || c === prev[i]));
    const wd = animate ? waveDelay(el) : 0;
    if (!same) {
      const v = odoBuild(el, st, val);
      if (animate) v.animate([{ opacity: 0.25, transform: 'translateY(0.18em)' }, { opacity: 1, transform: 'none' }], { duration: 240, delay: wd, easing: EASE.out, fill: 'backwards' });
      return;
    }
    st.sel.textContent = val;
    const fwd = (o.mode || 'forward') === 'forward';
    Array.from(val).forEach((c, i) => {
      if (!isD(c) || c === prev[i]) return;
      const s = st.cells[i], d = +c;
      if (!animate) { odoSnap(s, d); return; }
      if (s._pos !== s._d) odoSnap(s, s._d);
      const pos = fwd ? (d >= s._d ? d : d + 10) : d;
      s._d = d; s._pos = pos;
      s.style.transitionDelay = `${wd + (val.length - 1 - i) * 18}ms`;
      s.style.transform = `translateY(${-pos * LH}em)`;
    });
  }

  /* -- one-off number tween (not for 1 Hz streams) ------------------------------ */
  function tweenNumber(el, to, fmt, o) {
    o = o || {};
    fmt = fmt || (v => String(Math.round(v)));
    const from = el._tw != null ? el._tw : (o.from != null ? o.from : to);
    cancelAnimationFrame(el._twRaf);
    if (reduced || o.instant || !isShown(el)) { el._tw = to; el.textContent = fmt(to); return; }
    const t0 = performance.now(), dur = o.dur || 600;
    const step = now => {
      const k = Math.min(1, (now - t0) / dur), e = 1 - Math.pow(1 - k, 3);
      el._tw = from + (to - from) * e; el.textContent = fmt(el._tw);
      if (k < 1) el._twRaf = requestAnimationFrame(step);
    };
    el._twRaf = requestAnimationFrame(step);
  }

  /* -- FLIP (transform only) -------------------------------------------------------- */
  function flip(els, mutate, o) {
    o = o || {};
    els = Array.from(els || []).filter(Boolean);
    const first = els.map(e => e.getBoundingClientRect());
    if (mutate) mutate();
    if (reduced) return Promise.resolve();
    const list = [];
    els.forEach((e, i) => {
      if (!e.isConnected) return;
      const r = e.getBoundingClientRect(), f = first[i];
      const dx = f.left - r.left, dy = f.top - r.top;
      if (Math.abs(dx) < 0.5 && Math.abs(dy) < 0.5) return;
      list.push(e.animate([{ transform: `translate(${dx}px, ${dy}px)` }, { transform: 'none' }],
        { duration: o.duration || SP.soft.duration, easing: o.easing || SP.soft.easing, composite: o.composite || 'replace' }));
    });
    return Promise.all(list.map(a => a.finished.catch(() => {})));
  }

  /* -- entrance stagger (<= 400 ms total) --------------------------------------------- */
  function stagger(els, o) {
    o = o || {};
    const base = o.base != null ? o.base : 30, step = o.step != null ? o.step : 18, dur = o.dur || 240, y = o.y != null ? o.y : 8, max = o.max || 8;
    return Array.from(els || []).map((el, i) => anim(el,
      [{ opacity: 0, transform: `translateY(${y}px)` }, { opacity: 1, transform: 'none' }],
      { duration: dur, delay: base + Math.min(i, max - 1) * step, easing: EASE.out, fill: 'backwards' }));
  }

  /* -- shared-element hand-off (transform FLIP between two elements) ----------------- */
  function handoff(src, target, o) {
    o = o || {};
    if (!src || !src.isConnected) return;
    const r0 = o.rect || src.getBoundingClientRect();
    const w0 = src.offsetWidth || r0.width, h0 = src.offsetHeight || r0.height;
    const cs = getComputedStyle(src);
    let bg = cs.backgroundColor, radius = cs.borderRadius;
    for (let n = src.parentElement; n && (/rgba\(0, 0, 0, 0\)|transparent/.test(bg)); n = n.parentElement) { const c = getComputedStyle(n); bg = c.backgroundColor; if (radius === '0px') radius = c.borderRadius; }
    const ghost = src.cloneNode(true);
    ghost.removeAttribute('id'); T.$$('[id]', ghost).forEach(n => n.removeAttribute('id'));
    ghost.setAttribute('aria-hidden', 'true');
    ghost.classList.add('flip-ghost');
    Object.assign(ghost.style, {
      width: w0 + 'px', height: h0 + 'px', background: bg, borderRadius: radius === '0px' ? '10px' : radius,
      boxShadow: 'var(--sh-card-lift)', color: cs.color, transform: 'none', transition: 'none', visibility: 'visible', opacity: '1',
    });
    (T.$('#app') || doc.body).append(ghost);
    const s0x = r0.width / w0, s0y = r0.height / h0;
    const start = `translate(${r0.left}px, ${r0.top}px) scale(${s0x}, ${s0y})`;
    const tr = target && target.isConnected ? target.getBoundingClientRect() : null;
    const r1 = tr && tr.width ? tr : { left: r0.left + 12, top: Math.max(80, r0.top - 40), width: r0.width, height: r0.height };
    // one uniform scale (the face is never squashed), centred on the target
    const s = Math.min(r1.width / w0, r1.height / h0);
    const ex = r1.left + (r1.width - w0 * s) / 2, ey = r1.top + (r1.height - h0 * s) / 2;
    const end = `translate(${ex}px, ${ey}px) scale(${s})`;
    if (reduced) { ghost.remove(); return; }
    const D = o.duration || 560;
    // travel spans the whole duration on the settle spring (~90 % in by ~220 ms); the
    // ghost fades on its own linear track over the second half, so it never parks
    const g = ghost.animate([{ transform: start }, { transform: end }], { duration: D, easing: SP.settle.easing, fill: 'forwards' });
    ghost.animate([{ opacity: 1 }, { opacity: 1, offset: 0.55 }, { opacity: 0 }], { duration: D, easing: 'linear', fill: 'forwards' });
    if (tr && tr.width) {
      const dx = r0.left - r1.left, dy = r0.top - r1.top, k = r0.width / r1.width;
      target.animate([{ transform: `translate(${dx}px, ${dy}px) scale(${k})`, transformOrigin: '0 0', opacity: 0 },
        { opacity: 0, offset: 0.35 }, { transform: 'none', transformOrigin: '0 0', opacity: 1 }],
      { duration: D, easing: SP.settle.easing });
    }
    onDone(g, () => ghost.remove(), D + 300);
  }

  /* -- list enter / leave / reorder (transform-only FLIP) ---------------------------
     T.motion.list(container, mutate, { key, stagger, max, enter, leave, from, highlight, duration,
                                        enterDelay, followers })
     Snapshots the children (by data-key, data-id or identity), runs mutate() —
     which may add, remove, hide or reorder children — then: moved rows glide
     (translate), new rows fade/rise in with a short stagger (or fly in from
     `from`: an Element or DOMRect), removed rows leave as a fading ghost at
     their old place (not for table rows; 120 ms). When rows left, the rows and
     followers that slide over their place start 70 ms later, so text never slides
     over a fading copy. enterDelay: ms before new rows appear (a fold opening lets
     the rows below clear most of their travel first). followers: true | root Element
     → what comes after the container (its later siblings, and its ancestors' later
     siblings up to root, default the page) glides from where it was when the
     container changes height. highlight:true glows new rows once. */
  const LEAVE_LAG = 70, LEAVE_MS = 120;
  function followersOf(box, root) {
    root = root && root.nodeType === 1 ? root : box.closest('.page') || doc.body;
    const out = [];
    for (let n = box; n && n !== root && root.contains(n); n = n.parentElement) {
      for (let s = n.nextElementSibling; s; s = s.nextElementSibling) if (!s.hidden && !s._ghost && s.getClientRects().length) out.push(s);
    }
    return out;
  }
  function listMotion(box, mutate, o) {
    o = o || {};
    const keyOf = o.key || (el => (el.dataset && (el.dataset.key || el.dataset.id)) || el);
    const kids = () => Array.from(box.children).filter(el => !el._ghost && !el.classList.contains('list-hl') && el.tagName !== 'TEMPLATE');
    const vis = el => !el.hidden && el.getClientRects().length > 0;
    const before = new Map();
    const live = !reduced && isShown(box);
    const fol = live && o.followers ? followersOf(box, o.followers) : [], fy = fol.map(f => f.getBoundingClientRect().top);
    if (live) kids().forEach(el => { if (vis(el)) before.set(keyOf(el), { el, r: el.getBoundingClientRect() }); });
    if (mutate) mutate();
    if (reduced || !isShown(box)) {
      if (o.highlight && !reduced) kids().forEach(el => { if (!before.has(keyOf(el))) el.classList.add('is-new'); });
      return Promise.resolve();
    }
    const b1 = box.getBoundingClientRect(), out = [], seen = new Set();
    const from = o.from ? (o.from.getBoundingClientRect ? o.from.getBoundingClientRect() : o.from) : null;
    const now = kids().filter(vis), nowKeys = new Set(now.map(keyOf));
    let removed = false; before.forEach((b, k) => { if (!nowKeys.has(k)) removed = true; });
    const ghosts = o.leave !== false && !/^(TBODY|THEAD|TABLE|TR)$/.test(box.tagName);
    const lag = removed && ghosts ? LEAVE_LAG : 0;
    const moveOpts = { duration: o.duration || SP.soft.duration, easing: SP.soft.easing, delay: lag, fill: 'backwards' };
    let n = 0;
    now.forEach(el => {
      const k = keyOf(el); seen.add(k);
      const r = el.getBoundingClientRect(), b = before.get(k);
      if (b) {
        const dx = b.r.left - r.left, dy = b.r.top - r.top;
        if (Math.abs(dx) > 0.5 || Math.abs(dy) > 0.5) out.push(el.animate([{ transform: `translate(${dx}px, ${dy}px)` }, { transform: 'none' }], moveOpts));
        return;
      }
      if (o.enter === false) return;
      const delay = (o.enterDelay || 0) + Math.min(n++, o.max || 10) * (o.stagger != null ? o.stagger : 22);
      if (from) {
        const dx = from.left + from.width / 2 - (r.left + r.width / 2), dy = from.top + from.height / 2 - (r.top + r.height / 2);
        out.push(el.animate([{ transform: `translate(${dx}px, ${dy}px) scale(.94)`, opacity: 0 }, { opacity: 1, offset: 0.3 }, { transform: 'none', opacity: 1 }],
          { duration: 560, delay, easing: SP.settle.easing, fill: 'backwards' }));
      } else out.push(el.animate([{ opacity: 0, transform: 'translateY(-6px)' }, { opacity: 1, transform: 'none' }], { duration: 240, delay, easing: EASE.out, fill: 'backwards' }));
      if (o.highlight) { el.classList.remove('is-new'); void el.offsetWidth; el.classList.add('is-new'); setTimeout(() => el.classList.remove('is-new'), 1700 + delay); }
    });
    if (ghosts) {
      before.forEach((b, k) => {
        if (seen.has(k)) return;
        const g = b.el.cloneNode(true); g._ghost = true; g.hidden = false;
        g.removeAttribute('id'); g.setAttribute('aria-hidden', 'true'); g.classList.remove('is-new');
        if (getComputedStyle(box).position === 'static') box.style.position = 'relative';
        Object.assign(g.style, { position: 'absolute', margin: '0', pointerEvents: 'none', zIndex: '0', boxSizing: 'border-box',
          left: (b.r.left - b1.left - box.clientLeft + box.scrollLeft) + 'px', top: (b.r.top - b1.top - box.clientTop + box.scrollTop) + 'px',
          width: b.r.width + 'px', height: b.r.height + 'px' });
        box.append(g);
        const a = g.animate([{ opacity: 1, transform: 'none' }, { opacity: 0, transform: 'scale(.98)' }], { duration: LEAVE_MS, easing: EASE.in, fill: 'forwards' });
        onDone(a, () => g.remove(), 400);
      });
    }
    // what follows the container glides from where it visibly was (one glide per element: a newer
    // change replaces a running one instead of stacking on it)
    fol.forEach((f, i) => {
      if (f._follow) { f._follow.cancel(); f._follow = null; }
      if (!f.isConnected || f.hidden) return;
      const dy = fy[i] - f.getBoundingClientRect().top;
      if (Math.abs(dy) > 0.5) out.push(f._follow = f.animate([{ transform: `translateY(${dy}px)` }, { transform: 'none' }], moveOpts));
    });
    return Promise.all(out.map(a => a.finished.catch(() => {})));
  }

  /* -- container morph (shared element into a panel) ------------------------------
     T.motion.morph(src, target, { duration, reverse, rect, land }) — the target
     (laid out at its final place) grows from the source's rect with a transform
     (translate + scale); its content fades in once it is near its shape. A copy
     of the source's face (unscaled, never distorted) rides with the growing
     container onto `land` (default: the target's title) and hands over to it,
     so no frame is an empty box. reverse:true shrinks it back onto the source.
     Returns the target's Animation (null when reduced / not measurable). */
  function morph(src, target, o) {
    o = o || {};
    if (reduced || !src || !target || !target.isConnected || (!o.rect && !src.isConnected)) return null;
    const r0 = o.rect || src.getBoundingClientRect(), r1 = target.getBoundingClientRect();
    if (!r0.width || !r0.height || !r1.width || !r1.height) return null;
    const D = o.duration || (o.reverse ? 240 : 440), rev = !!o.reverse;
    const ease = rev ? EASE.in : SP.settle.easing;
    // where the source's face lands: measured before the container starts moving
    const land = o.land !== undefined ? o.land : target.querySelector('.ovl-title, .dlg-title, h1, h2, h3');
    const lr = land && land.getBoundingClientRect();
    const kf = [{ transform: `translate(${r0.left - r1.left}px, ${r0.top - r1.top}px) scale(${r0.width / r1.width}, ${r0.height / r1.height})`, transformOrigin: '0 0' },
      { transform: 'none', transformOrigin: '0 0' }];
    const a = target.animate(rev ? [kf[1], kf[0]] : kf, { duration: D, easing: ease, fill: rev ? 'forwards' : 'none' });
    // closing: the content leaves by 35 % while the source's face is already coming in
    // (20–40 %), so the shrinking container is never an empty box
    Array.from(target.children).forEach(k => k.animate(rev
      ? [{ opacity: 1 }, { opacity: 0, offset: 0.35 }, { opacity: 0 }]
      : [{ opacity: 0 }, { opacity: 0, offset: 0.4 }, { opacity: 1 }], { duration: D, easing: 'linear', fill: rev ? 'forwards' : 'none' }));
    if (src.isConnected) {
      const cs = getComputedStyle(src);
      const g = src.cloneNode(true);
      g.removeAttribute('id'); T.$$('[id]', g).forEach(x => x.removeAttribute('id'));
      g.setAttribute('aria-hidden', 'true'); g.classList.add('morph-ghost'); g.classList.remove('is-new');
      Object.assign(g.style, {
        width: r0.width + 'px', height: r0.height + 'px', transition: 'none', animation: 'none', boxShadow: 'none',
        gridTemplateColumns: cs.gridTemplateColumns, color: cs.color, font: cs.font, background: /^(button|a)$/i.test(src.tagName) ? cs.backgroundColor : 'transparent',
        borderRadius: cs.borderRadius,
      });
      layer().append(g);
      const pl = parseFloat(cs.paddingLeft) || 0;
      const L = lr && lr.width ? [lr.left - pl, lr.top + lr.height / 2 - r0.height / 2] : [r1.left, r1.top];
      const P0 = `translate(${r0.left}px, ${r0.top}px)`, P1 = `translate(${L[0]}px, ${L[1]}px)`;
      g.style.transform = rev ? P0 : P1;
      g.animate([{ transform: rev ? P1 : P0 }, { transform: rev ? P0 : P1 }], { duration: D, easing: ease, fill: 'forwards' });
      const ga = g.animate(rev ? [{ opacity: 0 }, { opacity: 0, offset: 0.2 }, { opacity: 1, offset: 0.4 }, { opacity: 1 }]
        : [{ opacity: 1 }, { opacity: 1, offset: 0.45 }, { opacity: 0, offset: 0.72 }, { opacity: 0 }], { duration: D, easing: 'linear', fill: 'forwards' });
      onDone(ga, () => g.remove(), D + 200);
    }
    return a;
  }

  T.motion = {
    get reduced() { return reduced; },
    SP, ease: EASE, springCurve, anim, onDone, spring, swapText, odometer, tweenNumber, flip, stagger, handoff,
    list: listMotion, morph,
    wave: { start: waveStart, delay: waveDelay },
  };

  /* ================================================================== UI */
  /* -- tooltip -- */
  // [data-tip] text · [data-tip-kbd="Ctrl F"] keycaps · [data-tip-pos="bottom"] placement.
  // Delay 460 ms; once one tip has shown, neighbours show at once for 500 ms (warm).
  // tipsOff: while a sortable row is pressed or dragged no tooltip shows (the rows move under the pointer)
  let tipEl = null, tipTimer = 0, tipFor = null, tipMute = null, tipWarmUntil = 0, tipsOff = false;
  function tipNode() { if (!tipEl) { tipEl = T.h('div', { class: 'tip', role: 'tooltip' }); doc.body.append(tipEl); } return tipEl; }
  function hideTip() { clearTimeout(tipTimer); tipFor = null; if (tipEl && tipEl.classList.contains('on')) { tipEl.classList.remove('on'); tipWarmUntil = performance.now() + 500; } }
  function showTip(t) {
    if (tipsOff) return;
    const text = t.getAttribute('data-tip'); if (!text || !t.isConnected) return;
    const el = tipNode(); el.textContent = text;
    const k = t.getAttribute('data-tip-kbd');
    if (k) el.append(T.html`<span class="tip-k">${k.split(/\s+/).map(x => T.html`<kbd>${x}</kbd>`)}</span>`);
    el.classList.add('on');
    const r = t.getBoundingClientRect(), w = el.offsetWidth, hgt = el.offsetHeight;
    let x = r.left + r.width / 2 - w / 2; x = Math.max(8, Math.min(innerWidth - w - 8, x));
    const below = t.getAttribute('data-tip-pos') === 'bottom';
    let y = below ? r.bottom + 8 : r.top - hgt - 8;
    if (!below && y < 8) y = r.bottom + 8;
    if (below && y + hgt > innerHeight - 8) y = r.top - hgt - 8;
    el.style.left = x + 'px'; el.style.top = y + 'px';
  }
  doc.addEventListener('pointerover', e => {
    const t = e.target.closest && e.target.closest('[data-tip]');
    if (t === tipFor) return;
    hideTip(); if (!t || !t.getAttribute('data-tip') || t === tipMute) return;
    tipFor = t; tipTimer = setTimeout(() => showTip(t), performance.now() < tipWarmUntil ? 0 : 460);
  });
  doc.addEventListener('pointerout', e => {
    const t = e.target.closest && e.target.closest('[data-tip]');
    if (t && !t.contains(e.relatedTarget)) { hideTip(); if (t === tipMute) tipMute = null; }
  });
  // once pressed, a control stays quiet until the pointer leaves it (labels swap under the cursor)
  doc.addEventListener('pointerdown', e => { hideTip(); tipMute = e.target.closest && e.target.closest('[data-tip]'); }, true);
  doc.addEventListener('focusin', e => { const t = e.target.closest && e.target.closest('[data-tip]'); if (t && t !== tipMute && t.matches(':focus-visible')) { tipFor = t; showTip(t); } });
  doc.addEventListener('focusout', hideTip);

  /* -- gliding bar (seg thumb, tab underline): translateX + scaleX FLIP that
        starts from where the bar visibly is when a glide is interrupted -- */
  function glideBar(el, st, x, w, animate) {
    let fx = st.x, fw = st.w;
    const running = el.getAnimations ? el.getAnimations() : [];
    if (running.length) {
      if (st.w) { try { const m = new DOMMatrixReadOnly(getComputedStyle(el).transform); fx = m.e; fw = m.a * st.w; } catch (e) { /* keep target */ } }
      running.forEach(a => a.cancel());
    }
    el.style.width = w + 'px'; el.style.transform = `translateX(${x}px)`;
    if (animate && st.w && !reduced && (Math.abs(fx - x) > 0.5 || Math.abs(fw - w) > 0.5)) {
      el.animate([{ transform: `translateX(${fx}px) scaleX(${fw / w})` }, { transform: `translateX(${x}px) scaleX(1)` }],
        { duration: SP.glide.duration, easing: SP.glide.easing });
    }
    st.x = x; st.w = w;
  }

  /* -- segmented control with a gliding thumb (FLIP: translateX + scaleX) -- */
  /* (T-P4-09) a control that is not rendered: on a hidden page (a walk up the tree, no style at all:
     a page hidden while the language switches is relabelled in place and must not recalculate the
     style of the whole document in that task, review M1), else by checkVisibility() (style only, no
     layout). Only the page: a hidden 配置 section keeps the checkVisibility path (measured: skipping
     those too moved the style work into lang.settings' switch task, 50-56 ms) */
  const unrendered = el => !!el.closest('.page[hidden]') || (el.checkVisibility ? !el.checkVisibility() : false);
  function seg(el, o) {
    o = o || {};
    let thumb = el.querySelector('.seg-thumb');
    if (!thumb) { thumb = T.h('span', { class: 'seg-thumb', 'aria-hidden': 'true' }); el.prepend(thumb); }
    el.setAttribute('role', el.getAttribute('role') || 'radiogroup');
    const btns = () => T.$$('.seg-btn', el);
    btns().forEach(b => { b.setAttribute('role', 'radio'); b.type = 'button'; });
    let cur = o.value != null ? o.value : (btns().find(b => b.getAttribute('aria-checked') === 'true') || btns()[0]).dataset.v;
    const geo = { x: 0, w: 0 };
    function place(animate) {
      const b = btns().find(x => x.dataset.v === cur); if (!b) return;
      // (T-P4-09) a control that is not rendered (a hidden page or section) has nothing to measure:
      // checkVisibility() needs style only, where offsetLeft would lay out the whole document; its
      // ResizeObserver places it once it is shown
      if (unrendered(b)) return;
      const x = b.offsetLeft, w = b.offsetWidth;
      if (!w) return;
      glideBar(thumb, geo, x, w, animate);
    }
    function set(v, animate) {
      cur = v;
      btns().forEach(b => { const on = b.dataset.v === v; b.setAttribute('aria-checked', String(on)); b.tabIndex = on ? 0 : -1; });
      place(animate !== false);
    }
    el.addEventListener('click', e => {
      const b = e.target.closest('.seg-btn'); if (!b || b.disabled || !el.contains(b)) return;
      const v = b.dataset.v; if (v === cur) return;
      const prev = cur;
      if (o.onChange && o.onChange(v, prev) === false) return;
      set(v, true);
    });
    // ← → move focus AND select (the radiogroup pattern); onChange may still veto
    el.addEventListener('keydown', e => {
      if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
      const bs = btns().filter(b => !b.disabled), i = bs.indexOf(doc.activeElement); if (i < 0) return;
      e.preventDefault();
      const nb = bs[(i + (e.key === 'ArrowRight' ? 1 : bs.length - 1)) % bs.length];
      nb.focus();
      const v = nb.dataset.v; if (v === cur) return;
      const prev = cur;
      if (o.onChange && o.onChange(v, prev) === false) return;
      set(v, true);
    });
    set(cur, false);
    if (window.ResizeObserver) new ResizeObserver(() => place(false)).observe(el);
    // (T-P4-09: placed again from the frame after the fonts are in, never inside the task that
    // built it, where its layout read would lay out the whole document)
    T.util.fontsReady().then(() => requestAnimationFrame(() => place(false)));
    return { el, set, get: () => cur, place };
  }

  /* -- copy button: widens into ✓ 已复制 on itself -- */
  function copyText(text) {
    return new Promise(res => {
      const legacy = () => {
        try { const ta = T.h('textarea', { readonly: true, style: 'position:fixed;left:-9999px;opacity:0' }); ta.value = text; doc.body.append(ta); ta.select(); doc.execCommand('copy'); ta.remove(); } catch (e) { /* the label still confirms intent */ }
        res();
      };
      try { if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(text).then(res, legacy); else legacy(); } catch (e) { legacy(); }
    });
  }
  function copy(btn, getText) {
    if (!btn.querySelector('.copy-face')) {
      btn.classList.add('copy'); btn.type = 'button';
      btn.replaceChildren(T.html`<span class="copy-face">${T.icon('copy', 16, 'ic-a')}${T.icon('check', 16, 'ic-b')}<span class="copy-lab" data-i18n="common.action.copied">${T.t('common.action.copied')}</span></span>`);
    }
    // (an aria-label the caller set is the caller's; without one the default word is read at use)
    const own = btn.getAttribute('aria-label');
    const label = () => own || T.t('common.action.copy');
    const tip = btn.getAttribute('data-tip');
    btn.addEventListener('click', () => {
      const text = typeof getText === 'function' ? getText() : getText;
      hideTip();
      copyText(text).then(() => {
        clearTimeout(btn._t);
        btn.classList.add('is-done'); btn.setAttribute('aria-label', T.t('common.action.copied')); btn.removeAttribute('data-tip');
        // the tooltip comes back with the idle face (it used to stay removed for good)
        btn._t = setTimeout(() => { btn.classList.remove('is-done'); btn.setAttribute('aria-label', label()); if (tip) btn.setAttribute('data-tip', tip); }, 1600);
      });
    });
    return btn;
  }

  /* ======================================================================
     T.ui — behaviour for the shared components (markup + classes live in
     components.css; the full contract is in DESIGN.md)
     ====================================================================== */
  let uid = 0;
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));

  /* -- busy → done / fail morph on the control itself ----------------------
     T.ui.busy(btn, job, opts)       job: Promise | () => Promise      (v2)
     T.ui.busy(btn, { run, ...opts })                                    (v1)
     opts  busy '检测中' · done '42 ms' | fn(result) | false
           fail '连接超时' | fn(error) | false (default: failWord(error): core.busy.failed for
                runtime_gone / cancelled / superseded, else the catalog text of an rt error
                (T.rt.errorText), else error.message, else core.busy.failed)
           icon / busyIcon / doneIcon / failIcon  (buttons with a .bi slot)
           hold 1400 · failHold 2400 · stay (keep the result face) · reserve
     Returns the job's promise. A click while busy joins the running job.
     The label rolls (old face stays until the new one is in: no blank
     frame) and the button reserves the width of every face it knows, so it
     never jumps under the cursor. T-P5-10: the reserved width is capped at the
     room its container gives it; a fail face that would not fit there shows the
     short core.busy.failed word, and the whole words become the hover tip and
     the accessible description (failAs / failNote below). */
  function ensureLab(btn) {
    let lab = btn.querySelector('.lab');
    if (lab) return lab;
    lab = doc.createElement('span'); lab.className = 'lab';
    const texts = Array.from(btn.childNodes).filter(n => n.nodeType === 3 && n.textContent.trim());
    if (texts.length) { texts[0].before(lab); texts.forEach(n => lab.append(n.textContent.trim())); texts.forEach(n => n.remove()); }
    else btn.append(lab);
    const bare = btn.querySelector(':scope > svg.ic');
    if (bare && !btn.querySelector('.bi')) { const bi = doc.createElement('span'); bi.className = 'bi'; bare.replaceWith(bi); bi.append(bare); }
    return lab;
  }
  // T-P4-07 (P4 architecture §7): a button face is nodes, never a markup string. faceOf keeps a
  // copy of the face shown (the label's visible layer); frag() makes a fresh copy each time it
  // is shown again (a node can be in one place only).
  const faceOf = lab => { const s = lab.querySelector(':scope > .sw:not([aria-hidden]), :scope > .swap-box > .sw:not([aria-hidden])'); return Array.from((s || lab).childNodes, n => n.cloneNode(true)); };
  const frag = nodes => { const f = doc.createDocumentFragment(); f.append(...nodes.map(n => n.cloneNode(true))); return f; };
  // the widest of the faces on this button (px), or null when there is no layout to measure
  function faceWidth(btn, faces) {
    // (T-P4-09: a button that is not rendered returns before any layout read)
    if (!btn || !btn.isConnected || unrendered(btn) || !btn.offsetWidth) return null;
    const probe = btn.cloneNode(true);
    probe.removeAttribute('id'); probe.classList.remove('is-away', 'is-busy', 'is-done', 'is-fail');
    probe.querySelectorAll('.busy-why').forEach(n => n.remove());
    probe.style.cssText = 'position:absolute;left:-9999px;top:0;visibility:hidden;min-width:0;width:auto;transition:none;animation:none';
    const pl = probe.querySelector('.lab'); if (!pl) return null;
    (btn.parentNode || doc.body).append(probe);
    let w = 0;
    faces.forEach(f => {
      if (f == null || f === false) return;
      const sw = doc.createElement('span'); sw.className = 'sw'; sw.append(f.cloneNode(true));
      pl.replaceChildren(sw); w = Math.max(w, probe.offsetWidth);
    });
    probe.remove();
    return w;
  }
  /* T-P5-10 (ruling 1): a busy button never leaves its container (the reserved width and the
     fail face; see busy() above).
     spill(btn): how far (px) the button, or a neighbour it pushes, sticks out right now, two
     numbers (left, right) per element and box: the button and its neighbours against its
     parent's content box (a sheet's 取消 stays in the footer, a list row's 设为当前 stays in its
     fixed action cell and never spreads over the row's columns), then the button against the
     content box of each ancestor above the parent, up to the first one that clips (at most 6
     boxes; inline and display: contents wrappers have no box and are skipped).
     roomFor(btn, w): the widest the button can be, up to w, without any of those numbers growing
     (never less than it is): what already sticks out (a negative margin, a button already wider
     than its cell) is not counted against it, and is not made worse. */
  function contentBox(el) {
    const r = el.getBoundingClientRect(), cs = getComputedStyle(el), x = r.left + el.clientLeft;
    return { l: x + (parseFloat(cs.paddingLeft) || 0), r: x + el.clientWidth - (parseFloat(cs.paddingRight) || 0) };
  }
  function spill(btn) {
    const over = [], out = (el, b) => { const r = el.getBoundingClientRect(); over.push(r.width ? Math.max(0, b.l - r.left) : 0, r.width ? Math.max(0, r.right - b.r) : 0); };
    let n = 0, sibs = btn.parentElement ? Array.from(btn.parentElement.children) : [];
    for (let a = btn.parentElement; a && a !== doc.body && a !== doc.documentElement && n < 6; a = a.parentElement) {
      const cs = getComputedStyle(a);
      if (cs.display === 'inline' || cs.display === 'contents') continue;
      const b = contentBox(a), clips = cs.overflowX !== 'visible'; n++;
      if (sibs) { sibs.forEach(c => out(c, b)); sibs = null; }
      else out(btn, b);
      if (clips) break;
    }
    return over;
  }
  // how much more any one element sticks out than it did in `base` (element by element: a
  // neighbour that already sticks out further does not hide what this button adds)
  const spillMore = (now, base) => now.reduce((m, v, i) => Math.max(m, v - (base[i] || 0)), 0);
  function roomFor(btn, w) {
    const cur = btn.offsetWidth;
    if (w <= cur) return w;
    const keep = btn.style.minWidth, base = spill(btn);
    let x = w, fit = cur;
    for (let i = 0; i < 4; i++) {
      btn.style.minWidth = x + 'px';
      const extra = spillMore(spill(btn), base);
      if (extra <= 0.5) { fit = x; break; }
      const nx = Math.max(cur, Math.floor(x - extra));
      if (nx >= x || nx === cur) break;
      x = nx;
    }
    btn.style.minWidth = keep;
    return fit;
  }
  // the button keeps the width of the widest face it knows (no jump between states), capped at
  // the room its container gives it (T-P5-10)
  function reserveFaces(btn, faces) {
    const fw = faceWidth(btn, faces);
    if (fw == null) return;
    const w = Math.ceil(Math.max(btn.offsetWidth, fw)), min = parseFloat(btn.style.minWidth) || 0;
    if (w <= min) return;
    const room = roomFor(btn, w);
    if (room > min) btn.style.minWidth = Math.ceil(room) + 'px';
  }
  /** does this face fit on the button without it (or a neighbour) leaving its container? */
  function fitsFace(btn, f) {
    const fw = faceWidth(btn, [f]);
    if (fw == null) return true;                      // no layout: the face shows as given
    const w = Math.ceil(fw);
    return w <= btn.offsetWidth || roomFor(btn, w) >= w;
  }
  /* the whole failure sentence behind a short fail word (T-P5-10): the hover tip (data-tip) and the
     accessible description (aria-describedby → a hidden .busy-why in the button), so a screen
     reader says the new name (the short word) and the description that changed with it. null takes
     them away and gives back the button's own tip and description. */
  function failNote(btn, text) {
    let d = btn.querySelector(':scope > .busy-why');
    if (text == null) {
      if (!d) return;
      d.remove();
      const was = btn._failWas || {};
      if (was.tip == null) btn.removeAttribute('data-tip'); else btn.setAttribute('data-tip', was.tip);
      if (was.desc == null) btn.removeAttribute('aria-describedby'); else btn.setAttribute('aria-describedby', was.desc);
      btn._failWas = null;
      if (tipFor === btn) hideTip();
      return;
    }
    if (!d) {
      btn._failWas = { tip: btn.getAttribute('data-tip'), desc: btn.getAttribute('aria-describedby') };
      d = doc.createElement('span'); d.className = 'busy-why'; d.hidden = true; d.id = 'bw' + (++uid);
      btn.append(d);
    }
    d.textContent = text;
    btn.setAttribute('data-tip', text);
    const desc = btn._failWas.desc;
    btn.setAttribute('aria-describedby', desc ? `${desc} ${d.id}` : d.id);
    // the pointer that pressed it still rests on it, or the key that ran it left it focused: the
    // whole words show now (the press muted the button's tip, and no pointerover / focusin is
    // coming to show a tip written after it)
    if (btn.matches(':hover, :focus-visible')) {
      if (tipMute === btn) tipMute = null;
      clearTimeout(tipTimer); tipFor = btn; showTip(btn);
    }
  }
  /** T.ui.reserve(btn, ['检测中', '42 ms', '连接超时']): fix the width before the first click */
  function reserve(btn, labels) {
    const lab = ensureLab(btn), bare = !btn.querySelector('.bi');
    if (!lab) return;
    reserveFaces(btn, (labels || []).map(t => (bare ? T.html`${T.icon('check', 15)}${t}` : T.html`${t}`)));
  }
  // the default failure word (mapping_core U1): the three ends that are not errors of the
  // command have their own words; an rt error says what failed through its message_key
  const BUSY_ENDS = ['runtime_gone', 'cancelled', 'superseded'];
  function failWord(err) {
    const code = err && typeof err.code === 'string' ? err.code : '';
    if (BUSY_ENDS.includes(code)) return T.t('core.busy.failed', { code });
    if (err && err.message_key && T.rt && typeof T.rt.errorText === 'function') return T.rt.errorText(err);
    return (err && err.message) || T.t('core.busy.failed', { code: 'other' });
  }
  // T-P4-06 (critique D7): a job that runs longer than this says so on its control; o.slowAfter
  // overrides it (ms), o.slow === false turns it off
  const BUSY_SLOW_MS = 10000;
  function slowFace(btn, p, o, fn) {
    if (o.slow === false) return;
    const ms = Number.isFinite(o.slowAfter) && o.slowAfter >= 0 ? o.slowAfter : BUSY_SLOW_MS;
    clearTimeout(btn._slowT);
    const id = btn._slowT = setTimeout(fn, ms);
    const stop = () => { clearTimeout(id); if (btn._slowT === id) btn._slowT = 0; };
    p.then(stop, stop);
  }
  function busy(btn, a, b) {
    let o, job;
    if (a != null && (typeof a === 'function' || typeof a.then === 'function')) { job = a; o = b || {}; }
    else { o = a || {}; job = o.run; }
    if (btn._busy) return btn._busy;                       // a second click joins the running job
    clearTimeout(btn._holdT);
    const iconOnly = btn.classList.contains('icon-btn') || btn.classList.contains('btn-icon');
    const txt = (v, r, dflt) => (typeof v === 'function' ? v(r) : v == null ? dflt : v);
    const state = s => {
      ['is-busy', 'is-done', 'is-fail'].forEach(c => btn.classList.toggle(c, c === 'is-' + s));
      if (s === 'busy') btn.setAttribute('aria-busy', 'true'); else btn.removeAttribute('aria-busy');
    };
    const pop = el => anim(el, [{ transform: 'scale(.55)' }, { transform: 'none' }], { duration: SP.press.duration, easing: SP.press.easing });
    let p;
    // (T-P4-07) faces and glyphs are nodes: a Raw (T.icon, the literal spinner) goes through
    // T.html, a kept face is a list of nodes copied each time it shows again
    const SPIN = T.raw('<span class="spin"></span>');
    const icNode = v => (v == null ? null : Array.isArray(v) ? frag(v) : T.html`${v}`);
    if (iconOnly) {                                         // icon buttons: the glyph morphs, the name is announced
      if (!btn._idle || !btn._idle.nodes) btn._idle = { nodes: Array.from(btn.childNodes, n => n.cloneNode(true)), label: btn.getAttribute('aria-label') || '' };
      const idle = btn._idle, put = (s, ic, label) => { state(s); btn.replaceChildren(icNode(ic)); if (label) btn.setAttribute('aria-label', label); if (s === 'done' || s === 'fail') pop(btn.firstElementChild); };
      const toIdle = () => { put('idle', idle.nodes, idle.label); };
      put('busy', SPIN, o.busy);
      p = Promise.resolve().then(() => (typeof job === 'function' ? job() : job));
      btn._busy = p;
      // slow feedback (T-P4-06, critique D7): after BUSY_SLOW_MS the name says it is still working
      slowFace(btn, p, o, () => { if (btn._busy === p) btn.setAttribute('aria-label', T.t('common.state.still_working')); });
      p.then(r => { btn._busy = null; if (o.done === false) return toIdle(); put('done', T.icon(o.doneIcon || 'check', 16), txt(o.done, r, T.t('core.busy.done'))); if (!o.stay) btn._holdT = setTimeout(toIdle, o.hold || 1400); },
        err => { btn._busy = null; if (o.fail === false) return toIdle(); put('fail', T.icon(o.failIcon || 'error', 16), txt(o.fail, err, failWord(err))); if (!o.stay) btn._holdT = setTimeout(toIdle, o.failHold || 2400); });
      return p;
    }
    const lab = ensureLab(btn), bi = btn.querySelector('.bi');
    const keep = () => ({ face: faceOf(lab), icon: bi ? Array.from(bi.childNodes, n => n.cloneNode(true)) : null });
    if (!/is-(busy|done|fail)/.test(btn.className)) btn._idle = keep();
    const idle = btn._idle && btn._idle.face ? btn._idle : keep();
    // the label face: the word as text, after its glyph when the button has no .bi slot
    const face = (ic, text) => (bi ? T.html`${text}` : T.html`${ic}${text}`);
    const busyIc = o.busyIcon ? T.icon(o.busyIcon, 15) : SPIN;
    const doneIc = T.icon(o.doneIcon || 'check', 15), failIc = T.icon(o.failIcon || 'error', 15);
    const busyFace = face(SPIN, o.busy || T.t('core.busy.working'));
    const put = (s, ic, f) => {
      state(s);
      if (s !== 'fail') failNote(btn, null);
      const n = bi ? icNode(ic) : null;
      if (n) { const b2 = doc.createElement('div'); b2.append(n.cloneNode(true)); if (bi.innerHTML !== b2.innerHTML) { bi.replaceChildren(n); if (s === 'done' || s === 'fail') pop(bi); } }
      swapText(lab, f);
    };
    const toIdle = () => put('idle', o.icon ? T.icon(o.icon, 15) : idle.icon, frag(idle.face));
    /* T-P5-10 (ruling 1): the fail face as it will show. The whole words when the button can hold
       them inside its container; otherwise the short word of core.busy.failed (by the error's code:
       runtime_gone / cancelled / superseded, else other) and the whole words go to failNote. No
       ellipsis: a cut reason cannot be read. */
    // When even the short word does not fit (a list row's narrow action cell): the short word
    // without its glyph (a .bi button keeps its glyph slot, so it has no such step), then the
    // glyph alone with the short word kept as the button's name (.sr-only) and the whole words
    // in the tip. Each step keeps the button inside its container; nothing is ever clipped.
    const failAs = (said, err) => {
      const full = face(failIc, said), words = full.textContent.trim();
      if (!words || fitsFace(btn, full)) return { f: full, why: null };
      const code = err && typeof err.code === 'string' && BUSY_ENDS.includes(err.code) ? err.code : 'other';
      const short = T.t('core.busy.failed', { code }), why = words === short ? null : words;
      const steps = [words === short ? null : face(failIc, short), bi ? null : T.html`${short}`];
      for (const f of steps) if (f && fitsFace(btn, f)) return { f, why };
      return { f: bi ? T.html`<span class="sr-only">${short}</span>` : T.html`${failIc}<span class="sr-only">${short}</span>`, why: words };
    };
    // the faces known before the job ends; a fail face that comes from the error (o.fail a function,
    // or the default) is not known yet, but when it shows short it is core.busy.failed's other
    // word, so that one is reserved too (capped like the rest) and the fail face does not widen it
    const failKnown = typeof o.fail === 'string' ? failAs(o.fail, null).f
      : o.fail === false ? null : face(failIc, T.t('core.busy.failed', { code: 'other' }));
    if (o.reserve !== false) reserveFaces(btn, [frag(idle.face), busyFace, typeof o.done === 'string' ? face(doneIc, o.done) : null, failKnown]);
    put('busy', busyIc, busyFace);
    p = Promise.resolve().then(() => (typeof job === 'function' ? job() : job));
    btn._busy = p;
    // slow feedback (T-P4-06, critique D7): after BUSY_SLOW_MS the label says 「仍在处理…」
    // (common.state.still_working); the promise is not touched, its end replaces the label as usual
    slowFace(btn, p, o, () => {
      if (btn._busy !== p) return;
      const slow = face(SPIN, T.t('common.state.still_working'));
      if (o.reserve !== false) reserveFaces(btn, [slow]);
      put('busy', busyIc, slow);
    });
    p.then(r => {
      btn._busy = null;
      if (o.done === false) { toIdle(); return; }
      const d = face(doneIc, txt(o.done, r, T.t('core.busy.done')));
      if (o.reserve !== false) reserveFaces(btn, [d]);
      put('done', doneIc, d);
      if (!o.stay) btn._holdT = setTimeout(toIdle, o.hold || 1400);
    }, err => {
      btn._busy = null;
      if (o.fail === false) { toIdle(); return; }
      const { f, why } = failAs(txt(o.fail, err, failWord(err)), err);
      if (o.reserve !== false) reserveFaces(btn, [f]);
      put('fail', failIc, f); failNote(btn, why);
      if (!o.stay) btn._holdT = setTimeout(toIdle, o.failHold || 2400);
    });
    return p;
  }

  /* -- switch: T.ui.switch(el, { checked, onChange(v, prev) → false | Promise, optimistic }) --
     Markup alone already works (<button class="switch" role="switch" aria-checked>):
     a click toggles it and fires a bubbling 'change' ({detail:{checked}}).
     With onChange: false vetoes; a Promise holds the knob (aria-busy) until it
     resolves true (or flips at once and reverts on failure when optimistic). */
  function switchCtl(el, o) {
    o = o || {};
    el._sw = true; el.setAttribute('role', 'switch'); if (el.tagName === 'BUTTON') el.type = 'button';
    if (o.checked != null) el.setAttribute('aria-checked', String(!!o.checked));
    const get = () => el.getAttribute('aria-checked') === 'true';
    const set = v => { el.setAttribute('aria-checked', String(!!v)); };
    const fire = v => el.dispatchEvent(new CustomEvent('change', { bubbles: true, detail: { checked: v } }));
    el.addEventListener('click', () => {
      if (el.disabled || el.getAttribute('aria-busy') === 'true' || el.getAttribute('aria-disabled') === 'true') return;
      const prev = get(), v = !prev;
      const r = o.onChange ? o.onChange(v, prev) : undefined;
      if (r === false) return;
      if (r && typeof r.then === 'function') {
        el.setAttribute('aria-busy', 'true');
        if (o.optimistic) set(v);
        r.then(ok => { el.removeAttribute('aria-busy'); if (ok === false) set(prev); else { set(v); fire(v); } },
          () => { el.removeAttribute('aria-busy'); set(prev); });
        return;
      }
      set(v); fire(v);
    });
    return { el, get, set, toggle: () => el.click() };
  }

  /* -- option cards: T.ui.options(el, { value, onChange(v, prev) → false | Promise }) --
     A Promise keeps the old choice selected and marks the asked card pending
     until it resolves (true = switch). Gated cards (.is-gated, input disabled)
     flash their reason when pressed. */
  function options(el, o) {
    o = o || {};
    const inputs = () => T.$$('input[type="radio"]', el);
    let cur = o.value != null ? o.value : ((inputs().find(i => i.checked) || {}).value);
    const set = v => { cur = v; inputs().forEach(i => { i.checked = i.value === v; }); };
    if (cur != null) set(cur);
    el.setAttribute('role', 'radiogroup');
    el.addEventListener('change', e => {
      const i = e.target; if (!i.matches || !i.matches('input[type="radio"]')) return;
      const v = i.value, prev = cur; if (v === prev) return;
      const r = o.onChange ? o.onChange(v, prev) : undefined;
      if (r === false) { set(prev); return; }
      if (r && typeof r.then === 'function') {
        set(prev); const card = i.closest('.opt'); if (card) card.classList.add('is-pending');
        r.then(ok => { if (card) card.classList.remove('is-pending'); if (ok !== false) set(v); }, () => { if (card) card.classList.remove('is-pending'); });
        return;
      }
      cur = v;
    });
    el.addEventListener('click', e => {
      const card = e.target.closest('.opt.is-gated'); if (!card) return;
      const g = card.querySelector('.opt-gate'); if (g) anim(g, [{ opacity: 0.3 }, { opacity: 1 }], { duration: 480, easing: EASE.out });
    });
    return { el, get: () => cur, set };
  }

  /* -- field: T.ui.field(el, { validate(value, input) → '错误说明' | null, hint, onInput }) --
     Validates on blur, then live while invalid. The message line swaps between
     hint and error in place (roll), so validation never moves the layout. */
  function field(el, o) {
    o = o || {};
    const input = el.querySelector('input, textarea');
    let msg = el.querySelector('.field-msg');
    if (!msg) { msg = T.h('div', { class: 'field-msg' }); el.append(msg); }
    const hint = o.hint != null ? o.hint : msg.textContent.trim();
    msg.id = msg.id || ('fm' + (++uid));
    if (input) { input.setAttribute('aria-describedby', msg.id); if (!input.id) input.id = 'fi' + (++uid); const lab = el.querySelector('.field-lab'); if (lab && lab.tagName === 'LABEL') lab.htmlFor = input.id; }
    let touched = false, err = null;
    // the hint as text (or a copy of a Node hint); the error after its glyph, as text (T-P4-07)
    const hintFace = () => (hint instanceof Node ? hint.cloneNode(true) : String(hint));
    const show = e => {
      err = e || null;
      el.classList.toggle('is-invalid', !!err);
      if (input) input.setAttribute('aria-invalid', String(!!err));
      swapText(msg, err ? T.html`${T.icon('error', 14)}${err}` : hintFace(), { dir: err ? 1 : -1 });
    };
    const run = () => { const e = o.validate ? o.validate(input.value, input) : null; show(e); return !e; };
    swapText(msg, hintFace(), { instant: true });
    if (input) {
      input.addEventListener('blur', () => { touched = true; run(); });
      input.addEventListener('input', () => { if (touched || err) run(); if (o.onInput) o.onInput(input.value); });
    }
    return { el, input, validate: () => { touched = true; return run(); }, error: show, get value() { return input.value; }, set(v) { input.value = v; if (touched) run(); } };
  }

  /* -- number input: T.ui.number(el, { min, max, step, format, onChange }) --
     .input.num with an <input>; adds −/+ steppers (hold to repeat), ↑/↓ keys
     (Shift ×10). Typed values are validated by T.ui.field, not clamped. */
  function number(el, o) {
    o = o || {};
    const input = el.querySelector('input');
    const nOr = (v, d) => (v == null || v === '' || isNaN(+v) ? d : +v);
    const min = nOr(o.min, nOr(input.min, -Infinity)), max = nOr(o.max, nOr(input.max, Infinity)), step = nOr(o.step, nOr(input.step, 1)) || 1;
    input.setAttribute('inputmode', 'numeric'); input.setAttribute('role', 'spinbutton'); input.autocomplete = 'off';
    if (isFinite(min)) input.setAttribute('aria-valuemin', min); if (isFinite(max)) input.setAttribute('aria-valuemax', max);
    if (!el.querySelector('.num-step')) el.append(T.html`<span class="num-step"><button class="num-b" type="button" tabindex="-1" data-d="-1" aria-label="${T.t('core.number.decrease')}" data-i18n-attr="aria-label:core.number.decrease">${T.icon('minus', 14)}</button><button class="num-b" type="button" tabindex="-1" data-d="1" aria-label="${T.t('core.number.increase')}" data-i18n-attr="aria-label:core.number.increase">${T.icon('plus', 14)}</button></span>`);
    const get = () => { const v = parseFloat(String(input.value).replace(/[^\d.-]/g, '')); return isNaN(v) ? null : v; };
    const paint = () => { const v = get(); input.setAttribute('aria-valuenow', v == null ? '' : v); T.$$('.num-b', el).forEach(b => { b.disabled = v != null && (+b.dataset.d < 0 ? v <= min : v >= max); }); };
    const set = (v, fire) => {
      v = clamp(+(Math.round(v / step) * step).toFixed(6), min, max);
      input.value = o.format ? o.format(v) : String(v); paint();
      if (fire) { input.dispatchEvent(new Event('input', { bubbles: true })); if (o.onChange) o.onChange(v); }
    };
    const bump = (d, mult) => { const v = get(); set((v == null ? (isFinite(min) ? min : 0) : v) + d * step * (mult || 1), true); };
    let rep = 0, repT = 0;
    const stop = () => { clearTimeout(repT); clearInterval(rep); rep = repT = 0; };
    el.addEventListener('pointerdown', e => {
      const b = e.target.closest('.num-b'); if (!b || b.disabled || e.button !== 0) return;
      e.preventDefault(); const d = +b.dataset.d; bump(d);
      repT = setTimeout(() => { rep = setInterval(() => { if (b.disabled) return stop(); bump(d); }, 70); }, 420);
      const up = () => { stop(); removeEventListener('pointerup', up); removeEventListener('pointercancel', up); };
      addEventListener('pointerup', up); addEventListener('pointercancel', up);
    });
    el.addEventListener('pointerleave', stop);
    input.addEventListener('keydown', e => { if (e.key === 'ArrowUp' || e.key === 'ArrowDown') { e.preventDefault(); bump(e.key === 'ArrowUp' ? 1 : -1, e.shiftKey ? 10 : 1); } });
    input.addEventListener('input', paint);
    paint();
    return { el, input, get, set: v => set(v, false) };
  }

  /* -- search: T.ui.search(el, { onInput(q), debounce, hotkey:true, life }) → { count(v,t), clear(), set(q), focus(), value, dispose() } --
     <label class="search">…<input …></label>. Adds the 可见/总数 count, a clear
     button and the Ctrl F keycap (all overlaid in one cell: nothing shifts).
     Ctrl F focuses the first visible search on screen (inside the top modal
     if one is open). Esc clears, then blurs. The count follows i18n:change: pass the caller's
     scope as `life` (ctx.life / ctx.scope, or anything with add(fn)) so the listener goes with
     it, or call dispose(); without either, a box that was in the document and has left it drops
     the listener on the next switch (one that is not attached yet keeps it). */
  function search(el, o) {
    o = o || {};
    const input = el.querySelector('input');
    if (!el.querySelector(':scope > .ic')) el.prepend(T.html`${T.icon('search', 15)}`);
    if (!el.querySelector('.search-tail')) {
      el.append(T.html`<span class="search-tail"><span class="search-q"><span class="search-count" aria-live="polite"></span><button class="icon-btn sm search-x" type="button" tabindex="-1" aria-label="${T.t('core.search.clear')}" data-i18n-attr="aria-label:core.search.clear">${T.icon('close', 14)}</button></span>${o.hotkey === false ? '' : T.raw('<span class="search-kbd" aria-hidden="true" translate="no"><kbd>Ctrl</kbd><kbd>F</kbd></span>')}</span>`);
    }
    if (o.hotkey !== false) el.setAttribute('data-hotkey', 'ctrl+f');
    input.setAttribute('role', 'searchbox'); input.autocomplete = 'off'; input.spellcheck = false;
    if (o.placeholder) input.placeholder = o.placeholder;
    const cnt = el.querySelector('.search-count');
    let vis = null, tot = null, timer = 0;
    const paint = () => {
      const q = input.value;
      el.classList.toggle('has-q', !!q);
      // one message with the tag in it (the count reads "<b>3</b> / 12"; the order is the translator's)
      if (q && vis != null) { cnt.replaceChildren(T.tx('core.search.count', { n: vis, total: tot })); cnt.classList.toggle('is-zero', vis === 0); }
      else cnt.textContent = '';
    };
    let seen = el.isConnected;
    const offLang = T.on('i18n:change', () => { if (el.isConnected) { seen = true; paint(); } else if (seen) offLang(); });
    if (o.life && typeof o.life.add === 'function') o.life.add(offLang);
    const emit = () => { if (o.onInput) o.onInput(input.value.trim()); };
    input.addEventListener('input', () => { paint(); clearTimeout(timer); if (o.debounce) timer = setTimeout(emit, o.debounce); else emit(); });
    input.addEventListener('keydown', e => {
      if (e.key !== 'Escape') return;
      if (input.value) { e.preventDefault(); e.stopPropagation(); api.clear(); } else input.blur();
    });
    el.querySelector('.search-x').addEventListener('click', e => { e.preventDefault(); api.clear(); input.focus(); });
    const api = {
      el, input,
      get value() { return input.value.trim(); },
      count(v, t) { vis = v; tot = t; paint(); },
      clear() { if (!input.value) return; input.value = ''; paint(); emit(); },
      set(q) { input.value = q || ''; paint(); emit(); },
      focus() { input.focus(); input.select(); },
      dispose() { offLang(); },
    };
    paint();
    return api;
  }

  /* -- floating layer shared by menus and popovers ----------------------------
     one open at a time · outside press / Esc / Tab close it · follows scroll */
  const FLOAT = { cur: null };
  function closeFloats() { if (FLOAT.cur) FLOAT.cur.close(null, true); }
  function placeFloat(el, anchor, o) {
    const r = anchor.getBoundingClientRect ? anchor.getBoundingClientRect() : anchor;
    const mw = el.offsetWidth, mh = el.offsetHeight, gap = o.gap != null ? o.gap : 6, pl = o.placement || 'bottom-start';
    let up = pl.startsWith('top');
    if (!up && r.bottom + gap + mh > innerHeight - 8 && r.top - gap - mh >= 8) up = true;
    else if (up && r.top - gap - mh < 8 && r.bottom + gap + mh <= innerHeight - 8) up = false;
    const top = clamp(up ? r.top - gap - mh : r.bottom + gap, 8, Math.max(8, innerHeight - mh - 8));
    const al = pl.split('-')[1] || 'start';
    const left = clamp(al === 'end' ? r.right - mw : al === 'center' ? r.left + r.width / 2 - mw / 2 : r.left, 8, Math.max(8, innerWidth - mw - 8));
    el.style.left = Math.round(left) + 'px'; el.style.top = Math.round(top) + 'px';
    el.style.transformOrigin = `${al === 'end' ? '100%' : al === 'center' ? '50%' : '0'} ${up ? '100%' : '0'}`;
    return up;
  }
  function openFloat(el, anchor, o) {
    closeFloats(); hideTip();
    layer().append(el);
    const up = placeFloat(el, anchor, o);
    anim(el, [{ opacity: 0, transform: `translateY(${up ? 4 : -4}px) scale(.98)` }, { opacity: 1, transform: 'none' }], { duration: 170, easing: EASE.out });
    const isEl = anchor && anchor.nodeType === 1;
    if (isEl) anchor.setAttribute('aria-expanded', 'true');
    let closed = false;
    const inside = t => el.contains(t) || (isEl && anchor.contains(t));
    const onDown = e => { if (!inside(e.target)) close(null, false, true); };
    const onScroll = e => { if (!el.contains(e.target)) placeFloat(el, anchor, o); };
    const onResize = () => placeFloat(el, anchor, o);
    doc.addEventListener('pointerdown', onDown, true);
    addEventListener('scroll', onScroll, true); addEventListener('resize', onResize);
    function close(result, instant, outside) {
      if (closed) return; closed = true;
      doc.removeEventListener('pointerdown', onDown, true); removeEventListener('scroll', onScroll, true); removeEventListener('resize', onResize);
      if (isEl) anchor.setAttribute('aria-expanded', 'false');
      if (FLOAT.cur && FLOAT.cur.el === el) FLOAT.cur = null;
      const hadFocus = el.contains(doc.activeElement);
      if (instant || reduced) el.remove();
      else { const a = anim(el, [{ opacity: 1, transform: 'none' }, { opacity: 0, transform: 'scale(.98)' }], { duration: 110, easing: EASE.in, fill: 'forwards' }); onDone(a, () => el.remove(), 300); }
      if (!outside && hadFocus && isEl && anchor.isConnected) anchor.focus({ preventScroll: true });
      if (o.onClose) o.onClose(result);
    }
    FLOAT.cur = { el, close, anchor };
    return { el, close, place: () => placeFloat(el, anchor, o) };
  }

  /* -- menu: T.ui.menu(anchor, items, { placement, minWidth, select, onClose }) → Promise<item|null> (+ .close(), .el) --
     items: { label, value, icon, hint, kbd:'Ctrl C', checked, danger, disabled, lang, translate, run(item) }
            (lang / translate: the label's own language, see textLang; T-P5-07; on the item
             itself as well when it has no hint / kbd)
            | { sep:true } | { group:'分组名' }
     Keyboard: ↑ ↓ Home End · Enter/Space · type to jump · Esc · Tab. The highlight glides. */
  function menu(anchor, items, o) {
    o = o || {};
    const el = T.h('div', { class: 'menu', role: o.select ? 'listbox' : 'menu', tabindex: '-1' });
    const list = [];
    const marks = !!o.select || (items || []).some(x => x && x.checked != null);
    // (T-P4-07) built as nodes: labels, hints and group names are text
    el.append(T.html`<span class="menu-hl" aria-hidden="true"></span>`);
    (items || []).forEach(x => {
      if (!x) return;
      if (x.sep) { el.append(T.html`<div class="menu-sep" role="separator"></div>`); return; }
      if (x.group && x.label == null) { el.append(T.html`<div class="menu-g" role="presentation">${x.group}</div>`); return; }
      const i = list.length; list.push(x);
      const hint = x.kbd ? T.html`<span class="hint">${String(x.kbd).split(/\s+/).map(k => T.html`<kbd>${k}</kbd>`)}</span>` : x.hint ? T.html`<span class="hint">${x.hint}</span>` : '';
      const role = o.select ? 'option' : marks ? 'menuitemradio' : 'menuitem';
      const b = T.html`<button class="menu-i${x.danger ? ' danger' : ''}" type="button" id="mi${++uid}" data-i="${i}" tabindex="-1" role="${role}">${marks ? T.html`<span class="mk">${x.checked ? T.icon('check', 14) : ''}</span>` : ''}${x.icon ? T.icon(x.icon, 16) : ''}<span class="menu-l">${x.label}</span>${hint}</button>`;
      if (x.disabled) b.setAttribute('aria-disabled', 'true');
      if (marks) b.setAttribute(`aria-${o.select ? 'selected' : 'checked'}`, String(!!x.checked));
      textLang(b.querySelector('.menu-l'), x);   // before the menu is placed (a lang can change the font)
      // no hint: the label is the option's only text, so the option itself (the node a screen reader
      // follows through aria-activedescendant) gets the language too; a hint stays in the UI language
      if (!hint) textLang(b, x);
      el.append(b);
    });
    if (o.minWidth) el.style.minWidth = Math.round(o.minWidth) + 'px';
    const hl = el.querySelector('.menu-hl');
    const btn = i => el.querySelector(`.menu-i[data-i="${i}"]`);
    const enabled = () => list.map((x, i) => (x.disabled ? -1 : i)).filter(i => i >= 0);
    let sel = -1, resolve;
    const p = new Promise(r => { resolve = r; });
    function hi(i, animate) {
      sel = i;
      T.$$('.menu-i', el).forEach(b => b.classList.toggle('is-hl', +b.dataset.i === i));
      const b = btn(i);
      if (!b) { hl.classList.remove('on'); el.removeAttribute('aria-activedescendant'); return; }
      el.setAttribute('aria-activedescendant', b.id);
      hl.style.height = b.offsetHeight + 'px'; // (T-P5-13) an item whose words wrap is taller than 32 px
      spring(hl, { y: b.offsetTop }, { k: 700, c: 52, instant: !animate || !hl.classList.contains('on') });
      hl.classList.add('on');
      const top = b.offsetTop, bot = top + b.offsetHeight;
      if (top < el.scrollTop) el.scrollTop = top - 4; else if (bot > el.scrollTop + el.clientHeight) el.scrollTop = bot - el.clientHeight + 4;
    }
    const f = openFloat(el, anchor, Object.assign({}, o, { onClose: r => { resolve(r || null); if (o.onClose) o.onClose(r || null); } }));
    const start = list.findIndex(x => x.checked && !x.disabled);
    const kb = o.keyboard != null ? o.keyboard : lastInput === 'key';     // opened with Enter / Space: highlight the first item
    if (start >= 0) hi(start, false); else if (kb) hi(enabled()[0] != null ? enabled()[0] : -1, false);
    el.focus({ preventScroll: true });
    function choose(i) { const x = list[i]; if (!x || x.disabled) return; f.close(x); if (x.run) setTimeout(() => x.run(x), 0); }
    el.addEventListener('click', e => { const b = e.target.closest('.menu-i'); if (b) choose(+b.dataset.i); });
    el.addEventListener('pointermove', e => { const b = e.target.closest('.menu-i'); if (b && +b.dataset.i !== sel && b.getAttribute('aria-disabled') !== 'true') hi(+b.dataset.i, true); });
    let typed = '', typedT = 0;
    el.addEventListener('keydown', e => {
      const en = enabled(); if (!en.length) return;
      const k = en.indexOf(sel);
      if (e.key === 'ArrowDown') { e.preventDefault(); hi(en[k < 0 ? 0 : (k + 1) % en.length], true); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); hi(en[k <= 0 ? en.length - 1 : k - 1], true); }
      else if (e.key === 'Home') { e.preventDefault(); hi(en[0], true); }
      else if (e.key === 'End') { e.preventDefault(); hi(en[en.length - 1], true); }
      else if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); if (sel >= 0) choose(sel); }
      else if (e.key === 'Tab') { e.preventDefault(); f.close(null); }
      else if (e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) {
        clearTimeout(typedT); typed += e.key.toLowerCase(); typedT = setTimeout(() => { typed = ''; }, 650);
        const j = en.find(i => String(list[i].label).toLowerCase().startsWith(typed)); if (j != null) hi(j, true);
      }
    });
    p.close = () => f.close(null);
    p.el = el;
    return p;
  }

  /* -- popover: T.ui.popover(anchor, content, { title, width, placement, onClose }) → { el, close(), place() } --
     content: Node | fn(el, api); a string is text (T-P4-07: never parsed as HTML). Same
     open/close rules as menus. */
  function popover(anchor, content, o) {
    o = o || {};
    const el = T.h('div', { class: 'pop' + (o.class ? ' ' + o.class : ''), role: 'dialog', tabindex: '-1', 'aria-label': o.title || '' });
    if (o.width) el.style.width = typeof o.width === 'number' ? o.width + 'px' : o.width;
    if (o.title) el.append(T.h('div', { class: 'pop-t', text: o.title }));
    let f = null;
    const api = { el, close: r => { if (f) f.close(r); }, place: () => { if (f) f.place(); } };
    if (typeof content === 'function') content(el, api); else if (content instanceof Node) el.append(content); else if (content != null) el.append(String(content));
    f = openFloat(el, anchor, o);
    el.focus({ preventScroll: true });
    return api;
  }

  /* -- textLang(node, option): an option's text in its own language (T-P5-07) --
     option.lang (BCP 47, e.g. 'zh-CN') → lang="…"; option.translate 'no' / false → translate="no",
     'yes' / true → translate="yes" (e.g. a language list: each language by its own name, which no
     catalog translates and a screen reader should read in that language). An option without them
     leaves neither attribute (a shown value loses them again when the choice changes). */
  function textLang(node, x) {
    if (!node) return;
    const lang = x && x.lang ? String(x.lang) : '';
    const tr = !x || x.translate == null || x.translate === '' ? '' : x.translate === false || x.translate === 'no' ? 'no' : 'yes';
    if (lang) node.setAttribute('lang', lang); else node.removeAttribute('lang');
    if (tr) node.setAttribute('translate', tr); else node.removeAttribute('translate');
  }

  /* -- select: T.ui.select(el, { options:[{value,label,hint,icon,disabled,lang,translate}], value, placeholder, onChange(v, prev) → false }) --
     <button class="select"> opens a menu with the current value checked. An option's lang /
     translate (textLang) go on its menu label and, while it is the choice, on the shown value
     (.select-v). */
  function select(el, o) {
    o = o || {};
    let opts = o.options || [], cur = o.value;
    el.classList.add('select'); if (el.tagName === 'BUTTON') el.type = 'button';
    el.setAttribute('aria-haspopup', 'listbox'); el.setAttribute('aria-expanded', 'false');
    if (!el.querySelector('.select-v')) el.replaceChildren(T.html`<span class="select-v"></span>${T.icon('chevron-down', 15)}`);
    const v = el.querySelector('.select-v');
    /* T-P5-10 (ruling 2): closed, the accessible name is the label and the current value
       (aria-labelledby = the label, then .select-v), not an aria-label alone. The label is the
       caller's own aria-labelledby, else the button's aria-label (the button names itself first:
       its id in its own aria-labelledby reads its aria-label, which keeps following the language),
       else its <label for>, else the .field-lab of the .field it sits in. Without any label the
       name stays the value. The fading old value is aria-hidden, so only the current one is read;
       option lang / translate (textLang) stay on .select-v. */
    if (!v.id) v.id = 'sv' + (++uid);
    const named = (el.getAttribute('aria-labelledby') || '').split(/\s+/).filter(x => x && x !== v.id);
    if (!named.length) {
      const fld = el.parentElement && el.parentElement.classList.contains('field') ? el.parentElement : null;
      const lab = el.hasAttribute('aria-label') ? el : (el.labels && el.labels[0]) || (fld && fld.querySelector(':scope > .field-lab')) || null;
      if (lab) { if (!lab.id) lab.id = 'sl' + (++uid); named.push(lab.id); }
    }
    if (named.length) el.setAttribute('aria-labelledby', [...named, v.id].join(' '));
    const paint = animate => { const it = opts.find(x => x.value === cur); swapText(v, it ? it.label : (o.placeholder || T.t('core.select.placeholder')), { instant: !animate }); v.classList.toggle('is-ph', !it); textLang(v, it); };
    let open = null;
    const show = keyboard => {
      if (open) { open.close(); return; }
      open = menu(el, opts.map(x => Object.assign({}, x, { checked: x.value === cur })), { select: true, keyboard, minWidth: el.offsetWidth, placement: o.placement });
      open.then(it => {
        open = null;
        if (!it || it.value === cur) return;
        const prev = cur;
        if (o.onChange && o.onChange(it.value, prev) === false) return;
        cur = it.value; paint(true);
      });
    };
    el.addEventListener('click', e => show(e.detail === 0));   // Enter / Space on the button → keyboard highlight
    el.addEventListener('keydown', e => { if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); if (!open) show(true); } });
    paint(false);
    return { el, get: () => cur, set(val, animate) { cur = val; paint(animate !== false); }, setOptions(list) { opts = list || []; paint(false); } };
  }

  /* -- slider: T.ui.slider(el, { min, max, step, bigStep, value, format(v), label,
          bands:[{ to, cls:'ok'|'warn'|'err'|'mute' }], marks:[{ v, label }], onInput(v), onChange(v) }) → { get, set(v, animate) } --
     Thumb = translate, fill = scaleX (both springs on a track click, instant
     while dragging). Bubble shows the value on hover, focus and drag. */
  function slider(el, o) {
    o = o || {};
    const min = o.min != null ? o.min : 0, max = o.max != null ? o.max : 100, step = o.step || 1, fmt = o.format || (x => String(x));
    const snap = x => clamp(+(Math.round((x - min) / step) * step + min).toFixed(6), min, max);
    let val = snap(o.value != null ? o.value : min);
    const frac = x => (x - min) / (max - min || 1);
    el.classList.add('slider'); el.setAttribute('role', 'slider'); el.tabIndex = 0;
    el.setAttribute('aria-valuemin', min); el.setAttribute('aria-valuemax', max); if (o.label) el.setAttribute('aria-label', o.label);
    const bands = o.bands && o.bands.length ? o.bands : null;
    el.classList.toggle('has-bands', !!bands); el.classList.toggle('has-marks', !!(o.marks && o.marks.length));
    let from = min;
    // (T-P4-07) built as nodes: a band's class is one of the four tones (mapping_core §6), a
    // mark's label is text; the inline sizes are set through the CSSOM (el.style), not markup
    const TONES = ['ok', 'warn', 'err', 'mute'];
    const bandEls = bands ? bands.map(b => { const w = Math.max(0, Math.min(b.to, max) - from); from = Math.min(b.to, max); return T.h('i', { class: TONES.includes(b.cls) ? b.cls : 'mute', style: { flex: `${w} 1 0` } }); }) : [];
    const markEls = o.marks ? o.marks.map((m, i, a) => T.h('span', { class: i === 0 && frac(m.v) < 0.04 ? 'is-first' : i === a.length - 1 && frac(m.v) > 0.96 ? 'is-last' : '', style: { left: `${(frac(m.v) * 100).toFixed(2)}%` } }, m.label == null ? '' : String(m.label))) : [];
    el.replaceChildren(
      T.h('div', { class: 'sl-track' }, bands ? [T.h('div', { class: 'sl-bands' }, bandEls), T.h('div', { class: 'sl-rest' })] : T.h('div', { class: 'sl-fill' })),
      T.html`<div class="sl-thumb"><span class="sl-bubble"></span></div>`,
      ...(markEls.length ? [T.h('div', { class: 'sl-marks', 'aria-hidden': 'true' }, markEls)] : []));
    const thumb = el.querySelector('.sl-thumb'), fill = el.querySelector('.sl-fill'), rest = el.querySelector('.sl-rest'), bubble = el.querySelector('.sl-bubble');
    function paint(animate) {
      // (T-P4-09: not rendered, nothing to measure without a layout; the ResizeObserver paints it once shown)
      const W = unrendered(el) ? 0 : el.clientWidth, f = frac(val);
      const sp = { k: 420, c: 40, instant: !animate || !W };
      if (W) spring(thumb, { x: f * W }, sp);
      if (fill) spring(fill, { sx: Math.max(0.0001, f) }, sp);
      if (rest) spring(rest, { sx: Math.max(0.0001, 1 - f) }, sp);
      bubble.textContent = fmt(val);
      el.setAttribute('aria-valuenow', val); el.setAttribute('aria-valuetext', fmt(val));
    }
    const setVal = (x, animate) => { x = snap(x); if (x === val) return false; val = x; paint(animate); if (o.onInput) o.onInput(val); return true; };
    const at = cx => { const r = el.getBoundingClientRect(); return min + clamp((cx - r.left) / (r.width || 1), 0, 1) * (max - min); };
    el.addEventListener('pointerdown', e => {
      if (e.button !== 0 || el.getAttribute('aria-disabled') === 'true') return;
      // no preventDefault: the natural mousedown focus keeps :focus-visible (the ring) for keyboard use only
      try { el.setPointerCapture(e.pointerId); } catch (err) { /* synthetic */ }
      el.classList.add('is-drag');
      const start = val;
      setVal(at(e.clientX), !e.target.closest('.sl-thumb'));
      const move = ev => setVal(at(ev.clientX), false);
      const up = () => { el.classList.remove('is-drag'); el.removeEventListener('pointermove', move); el.removeEventListener('pointerup', up); el.removeEventListener('pointercancel', up); if (val !== start && o.onChange) o.onChange(val); };
      el.addEventListener('pointermove', move); el.addEventListener('pointerup', up); el.addEventListener('pointercancel', up);
    });
    el.addEventListener('keydown', e => {
      if (el.getAttribute('aria-disabled') === 'true') return;
      const big = o.bigStep || step * 10;
      const map = { ArrowRight: step, ArrowUp: step, ArrowLeft: -step, ArrowDown: -step, PageUp: big, PageDown: -big };
      let nv = null;
      if (e.key in map) nv = val + map[e.key]; else if (e.key === 'Home') nv = min; else if (e.key === 'End') nv = max;
      if (nv == null) return;
      e.preventDefault(); if (setVal(nv, true) && o.onChange) o.onChange(val);
    });
    if (window.ResizeObserver) new ResizeObserver(() => paint(false)).observe(el);
    paint(false);
    return { el, get: () => val, set(x, animate) { val = snap(x); paint(animate !== false); } };
  }

  /* -- tabs: T.ui.tabs(el, { value, onChange(v, prev) → false }) → { set, get } --
     .tabs > .tab[data-v] (+ aria-controls → panels are shown/hidden). The ink
     underline glides; ← → Home End move and select. */
  function tabs(el, o) {
    o = o || {};
    let ink = el.querySelector('.tabs-ink');
    if (!ink) { ink = T.h('span', { class: 'tabs-ink', 'aria-hidden': 'true' }); el.append(ink); }
    el.setAttribute('role', 'tablist');
    const btns = () => T.$$('.tab', el);
    btns().forEach(b => { b.setAttribute('role', 'tab'); b.type = 'button'; });
    const first = btns().find(b => b.getAttribute('aria-selected') === 'true') || btns()[0];
    let cur = o.value != null ? o.value : first && first.dataset.v;
    const geo = { x: 0, w: 0 };
    const place = animate => { const b = btns().find(x => x.dataset.v === cur); if (b && !b.closest('.page[hidden]') && b.offsetWidth) glideBar(ink, geo, b.offsetLeft, b.offsetWidth, animate); };
    // F-01: a tab strip built before its sheet is in the document (T.sheet.open builds, then
    // attaches) finds its panels in its own detached tree; doc.getElementById alone missed them
    // and every panel showed at once until the first click
    const pane = id => {
      if (!id) return null;
      const root = el.getRootNode();
      return doc.getElementById(id) || (root !== doc && root.querySelector ? root.querySelector('#' + CSS.escape(id)) : null);
    };
    function set(v, animate) {
      cur = v;
      btns().forEach(b => {
        const on = b.dataset.v === v; b.setAttribute('aria-selected', String(on)); b.tabIndex = on ? 0 : -1;
        const pn = pane(b.getAttribute('aria-controls')); if (pn) pn.hidden = !on;
      });
      place(animate !== false);
    }
    const pick = (b, focus) => { if (!b || b.disabled) return; const v = b.dataset.v; if (v === cur) return; if (o.onChange && o.onChange(v, cur) === false) return; set(v, true); if (focus) b.focus(); };
    el.addEventListener('click', e => { const b = e.target.closest('.tab'); if (b && el.contains(b)) pick(b); });
    el.addEventListener('keydown', e => {
      const bs = btns().filter(b => !b.disabled), i = bs.indexOf(doc.activeElement); if (i < 0) return;
      const j = e.key === 'ArrowRight' ? (i + 1) % bs.length : e.key === 'ArrowLeft' ? (i - 1 + bs.length) % bs.length : e.key === 'Home' ? 0 : e.key === 'End' ? bs.length - 1 : null;
      if (j == null) return; e.preventDefault(); pick(bs[j], true);
    });
    set(cur, false);
    if (window.ResizeObserver) new ResizeObserver(() => place(false)).observe(el);
    // (T-P4-09: placed again from the frame after the fonts are in, never inside the task that
    // built it, where its layout read would lay out the whole document)
    T.util.fontsReady().then(() => requestAnimationFrame(() => place(false)));
    return { el, set, get: () => cur, place };
  }

  /* -- list keyboard + selection: T.ui.listNav(list, { rows:'.li', highlight:true, wrap, scroller, actions,
          onSelect(row, how), onOpen(row, how), onEscape() }) → { select(rowOrIndex, {focus}), get(), refresh(), clear() } --
     Click / ↑ ↓ / Home End / PageUp PageDown select; Enter or double-click opens;
     roving tabindex (a select writes only the old and the new stop); the selection highlight
     glides (spring) between rows, placed from layout offsets against the list (right inside
     paint-contained chunks and while rows glide). actions: true | selector keeps row controls
     out of the tab order except on the selected row. Hidden rows are skipped (their flag, a
     hidden ancestor or content-visibility: hidden; no rect per row). Call refresh() after
     re-rendering rows (one pass: roles, flags, stops; selection is kept by row[data-key]).
     Works on tbody rows (highlight off). */
  function listNav(list, o) {
    o = o || {};
    const sel = o.rows || '.li';
    const tabular = /^(TABLE|TBODY)$/.test(list.tagName);
    const all = () => T.$$(sel, list).filter(r => !r._ghost);   // (a leaving row's fading copy is not a row)
    // hidden rows are told by their flag, a [hidden] / display:none ancestor or a content-visibility:hidden
    // one (checkVisibility: style only, no layout; a rect per row forced skipped chunks to render)
    const shown = r => !r.hidden && (r.checkVisibility ? r.checkVisibility() : r.getClientRects().length > 0);
    const rows = () => all().filter(r => r.getAttribute('aria-disabled') !== 'true' && shown(r));
    // o.actions: row controls stay out of the tab order except on the selected row, so Tab leaves the
    // list in one stop (true → '.li-act button, .li-act a'; or a selector)
    const actSel = o.actions === true ? '.li-act button, .li-act a' : o.actions || null;
    let cur = null, hl = null, stop = null, actRow = null;
    if (o.highlight !== false && !tabular) {
      hl = list.querySelector(':scope > .list-hl');
      if (!hl) { hl = T.h('span', { class: 'list-hl', 'aria-hidden': 'true' }); list.prepend(hl); }
      list.classList.add('has-hl');
    }
    if (!tabular && !list.getAttribute('role')) list.setAttribute('role', 'listbox');
    // the row's layout offset measured against the list itself (transform-free, so a row that is
    // still gliding reports where it lands); a row inside a paint-contained chunk is its own offset
    // parent's child, so the chain is walked up to the list. Rects are the fallback.
    const offIn = el => {
      let y = 0, n = el;
      for (; n && n !== list; n = n.offsetParent) { if (!list.contains(n)) break; y += n.offsetTop; }
      if (n === list) return y;
      return el.getBoundingClientRect().top - list.getBoundingClientRect().top - list.clientTop + list.scrollTop;
    };
    const paintHl = animate => {
      if (!hl) return;
      if (hl.parentNode !== list) { list.prepend(hl); animate = false; }   // the page re-rendered the rows with innerHTML
      if (!cur || !cur.isConnected || !shown(cur)) { hl.classList.remove('on'); return; }
      hl.style.height = cur.offsetHeight + 'px';
      spring(hl, { y: offIn(cur) }, { k: 640, c: 50, instant: !animate || !hl.classList.contains('on') });
      hl.classList.add('on');
    };
    const acts = (r, on) => { if (r && actSel) r.querySelectorAll(actSel).forEach(b => { const v = on ? 0 : -1; if (b.tabIndex !== v) b.tabIndex = v; }); };
    // roving tabindex: only the previous and the new stop are written (every row starts at -1)
    // the row whose controls are in the tab order: the selected row, or (nothing selected yet) the tab stop,
    // so a row reached by Tab offers its own buttons next
    const setStop = t => {
      if (stop && stop !== t && stop.isConnected && stop.tabIndex !== -1) stop.tabIndex = -1;
      if (t && t.tabIndex !== 0) t.tabIndex = 0;
      stop = t || null;
      const ar = cur || stop;
      if (actSel && actRow !== ar) { acts(actRow, false); acts(ar, true); actRow = ar; }
    };
    const firstRow = () => { for (const r of all()) if (r.getAttribute('aria-disabled') !== 'true' && shown(r)) return r; return null; };
    function select(row, how, focus) {
      if (typeof row === 'number') row = rows()[row];
      if (!row) return;
      const prev = cur; cur = row;
      if (prev && prev !== row) prev.setAttribute('aria-selected', 'false');
      row.setAttribute('aria-selected', 'true');
      if (!tabular && !row.getAttribute('role')) row.setAttribute('role', 'option');
      setStop(row);
      paintHl(!!prev && prev !== row);
      if (focus || how === 'key') row.focus({ preventScroll: true });
      if (how === 'key' || how === 'api') row.scrollIntoView({ block: 'nearest' });
      if (prev !== row && o.onSelect) o.onSelect(row, how);
    }
    list.addEventListener('click', e => {
      const r = e.target.closest(sel); if (!r || !list.contains(r)) return;
      const ctl = e.target.closest('button, a, input, select, textarea, label, [data-no-select]');
      if (ctl && ctl !== r && r.contains(ctl)) return;
      select(r, 'click');
    });
    list.addEventListener('dblclick', e => { const r = e.target.closest(sel); if (r && list.contains(r) && o.onOpen) o.onOpen(r, 'dblclick'); });
    list.addEventListener('keydown', e => {
      if (e.target.closest('input, textarea, select')) return;
      // a control inside a row (检测延迟, 更多操作…) keeps its own keys: Enter / Space press it and the
      // arrows don't pull the selection away from it; a control between rows (a group header) keeps
      // Enter / Space, and the arrows still move the selection
      const row = e.target.closest(sel);
      const ctl = e.target.closest('button, a[href], [role="button"], [contenteditable="true"]');
      if (ctl && ctl !== row && list.contains(ctl) && (e.key === 'Enter' || e.key === ' ' || (row && row.contains(ctl)))) return;
      const rs = rows(); if (!rs.length) return;
      const i = rs.indexOf(cur), sc = o.scroller || list;
      const page = Math.max(1, Math.floor((sc.clientHeight || 360) / ((cur && cur.offsetHeight) || 36)) - 1);
      let j = null;
      if (e.key === 'ArrowDown') j = i < 0 ? 0 : i === rs.length - 1 ? (o.wrap ? 0 : i) : i + 1;
      else if (e.key === 'ArrowUp') j = i < 0 ? 0 : i === 0 ? (o.wrap ? rs.length - 1 : 0) : i - 1;
      else if (e.key === 'Home') j = 0;
      else if (e.key === 'End') j = rs.length - 1;
      else if (e.key === 'PageDown') j = Math.min(rs.length - 1, Math.max(0, i) + page);
      else if (e.key === 'PageUp') j = Math.max(0, i - page);
      else if (e.key === 'Enter' && o.onOpen) {
        // nothing selected yet: the focused tab-stop row is the one meant
        const r = cur || (row && row === e.target ? row : null); if (!r) return;
        e.preventDefault(); if (r !== cur) select(r, 'key'); o.onOpen(r, 'key'); return;
      }
      else if (e.key === ' ' && row && row === e.target && row !== cur) { e.preventDefault(); select(row, 'key'); return; }
      else if (e.key === 'Escape' && o.onEscape) { o.onEscape(); return; }
      if (j == null) return;
      e.preventDefault(); select(rs[j], 'key');
    });
    if (window.ResizeObserver) new ResizeObserver(() => paintHl(false)).observe(list);
    // one full pass (after a render): roles, selection flags, stale stops and row controls; no layout reads
    function sweep() {
      all().forEach(r => {
        if (!tabular && !r.getAttribute('role')) r.setAttribute('role', 'option');
        const s = String(r === cur); if (r.getAttribute('aria-selected') !== s) r.setAttribute('aria-selected', s);
        if (r !== cur && (r.tabIndex === 0 || !r.hasAttribute('tabindex'))) r.tabIndex = -1;   // focusable by click, not by Tab
        if (actSel && r !== cur) acts(r, false);
      });
      actRow = null; stop = null;
      setStop(cur || firstRow());
    }
    sweep();
    return {
      el: list,
      get: () => cur,
      select: (r, so) => select(r, 'api', so && so.focus),
      clear() { if (cur && cur.isConnected) cur.setAttribute('aria-selected', 'false'); acts(cur, false); cur = null; actRow = null; paintHl(false); setStop(firstRow()); },
      refresh() {
        if (cur && !cur.isConnected) { const k = cur.dataset.key; cur = k != null ? all().find(r => r.dataset.key === k) || null : null; }
        if (cur && cur.hidden) cur = null;         // filtered out (a parked view keeps its selection)
        sweep(); paintHl(false);
      },
    };
  }

  /* -- folding rows ×N: T.ui.fold(root, { onToggle(toggle, open) }) → { toggle(el, open) } --
     toggle: [data-fold="id"][aria-expanded]; children: [data-fold-of="id"] hidden.
     Opening FLIPs the rows below and staggers the children in. → / ← on a toggle. */
  function toggleFold(root, t, o, force) {
    const open = force != null ? force : t.getAttribute('aria-expanded') !== 'true';
    if ((t.getAttribute('aria-expanded') === 'true') === open) return;
    t.setAttribute('aria-expanded', String(open));
    const kids = T.$$(`[data-fold-of="${t.dataset.fold}"]`, root);
    const box = kids[0] ? kids[0].parentElement : t.parentElement;
    // opening: the rows below make room first (the soft spring has covered ~60 % of the way at
    // ~100 ms), then the children fade in, so new text never lands on rows still moving through
    listMotion(box, () => kids.forEach(k => { k.hidden = !open; }), { stagger: 14, max: 12, enterDelay: open ? 100 : 0 });
    if (o && o.onToggle) o.onToggle(t, open);
  }
  function fold(root, o) {
    o = o || {};
    root.addEventListener('click', e => {
      const t = e.target.closest('[data-fold]'); if (!t || !root.contains(t)) return;
      const ctl = e.target.closest('a, input, [data-no-fold]'); if (ctl && ctl !== t) return;
      toggleFold(root, t, o);
    });
    root.addEventListener('keydown', e => {
      if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
      const t = e.target.closest && e.target.closest('[data-fold]'); if (!t) return;
      e.preventDefault(); toggleFold(root, t, o, e.key === 'ArrowRight');
    });
    return { toggle: (t, open) => toggleFold(root, t, o, open) };
  }

  /* -- drag to reorder: T.ui.sortable(list, { items, handle:'.grip', onEnd(items, from, to) }) --
     Pointer drag on the handle (4 px threshold); siblings make room with
     transform; the row settles into its slot with the press spring.
     Keyboard: Alt + ↑ / ↓ on a focused handle moves the row one step. */
  function sortable(list, o) {
    o = o || {};
    const handle = o.handle || '.grip';
    const items = () => Array.from(list.children).filter(x => !x._ghost && !x.classList.contains('list-hl') && (!o.items || x.matches(o.items)));
    const finish = (item, from, to) => { if (to !== from && o.onEnd) o.onEnd(items(), from, to); };
    list.addEventListener('pointerdown', e => {
      const h = e.target.closest(handle); if (!h || !list.contains(h) || e.button !== 0) return;
      const all = items(), item = all.find(x => x.contains(h)); if (!item) return;
      e.preventDefault();
      // pressed or dragged, the handles carry no tooltip (it would hang over the moving rows)
      tipsOff = true; hideTip();
      const tipsBack = () => { tipsOff = false; removeEventListener('pointerup', tipsBack, true); removeEventListener('pointercancel', tipsBack, true); };
      addEventListener('pointerup', tipsBack, true); addEventListener('pointercancel', tipsBack, true);
      const from = all.indexOf(item), rects = all.map(x => x.getBoundingClientRect());
      const gapPx = all.length > 1 ? Math.max(0, rects[1].top - rects[0].bottom) : 0, H = rects[from].height + gapPx;
      const y0 = e.clientY; let started = false, to = from;
      const move = ev => {
        const dy = ev.clientY - y0;
        if (!started) { if (Math.abs(dy) < 4) return; started = true; list.classList.add('is-sorting'); item.classList.add('is-dragging'); hideTip(); }
        const d = clamp(dy, rects[0].top - rects[from].top - 6, rects[all.length - 1].top - rects[from].top + 6);
        item.style.transform = `translateY(${d}px)`;
        const c = rects[from].top + rects[from].height / 2 + d;
        to = from;
        for (let i = from + 1; i < all.length; i++) if (c > rects[i].top + rects[i].height / 2) to = i;
        for (let i = from - 1; i >= 0; i--) if (c < rects[i].top + rects[i].height / 2) to = i;
        all.forEach((x, i) => { if (x === item) return; const s = from < to && i > from && i <= to ? -H : to < from && i >= to && i < from ? H : 0; x.style.transform = s ? `translateY(${s}px)` : ''; });
      };
      const up = () => {
        removeEventListener('pointermove', move); removeEventListener('pointerup', up); removeEventListener('pointercancel', up);
        if (!started) return;
        const vis = item.getBoundingClientRect();
        all.forEach(x => { x.style.transition = 'none'; x.style.transform = ''; });
        list.classList.remove('is-sorting'); item.classList.remove('is-dragging');
        if (to !== from) list.insertBefore(item, to > from ? all[to].nextSibling : all[to]);
        const fin = item.getBoundingClientRect();
        if (!reduced) item.animate([{ transform: `translateY(${vis.top - fin.top}px)`, boxShadow: 'var(--sh-drag)' }, { transform: 'none', boxShadow: '0 0 0 0 rgba(0,0,0,0)' }], { duration: SP.press.duration, easing: SP.press.easing });
        requestAnimationFrame(() => all.forEach(x => { x.style.transition = ''; }));
        finish(item, from, to);
      };
      addEventListener('pointermove', move); addEventListener('pointerup', up); addEventListener('pointercancel', up);
    });
    list.addEventListener('keydown', e => {
      if (!e.altKey || (e.key !== 'ArrowUp' && e.key !== 'ArrowDown')) return;
      const h = e.target.closest(handle); if (!h) return;
      const all = items(), item = all.find(x => x.contains(h)), from = all.indexOf(item), to = from + (e.key === 'ArrowUp' ? -1 : 1);
      if (!item || to < 0 || to >= all.length) return;
      e.preventDefault();
      listMotion(list, () => list.insertBefore(item, to > from ? all[to].nextSibling : all[to]), { enter: false });
      h.focus(); finish(item, from, to);
    });
    return { el: list };
  }

  /* -- section index with scroll spy: T.ui.toc(nav, { scroller, offset, spyOffset, onChange(id) }) → { refresh(), set(id) } --
     nav > .toc-a[data-target="section-id"]. The marker glides; clicking scrolls
     smoothly to `offset` px below the scroller's top (scroll spy holds the clicked item
     until the scroll settles). spyOffset (px, or a function read on every pass, e.g.
     () => host.clientHeight * 0.5 for the window's midline) is the line the spy tests;
     it defaults to offset. The spy also looks again once sections change size without a
     scroll (a block opening, a list growing), after their glide has landed.
     scroller defaults to the page host (#host); pass window for a document. */
  function toc(nav, o) {
    o = o || {};
    const sc = o.scroller || T.$('#host') || window;
    const isWin = sc === window;
    const links = () => T.$$('[data-target]', nav);
    let ind = nav.querySelector('.toc-ind');
    if (!ind) { ind = T.h('span', { class: 'toc-ind', 'aria-hidden': 'true' }); nav.prepend(ind); }
    const off = o.offset != null ? o.offset : 24;
    const spyOff = () => (typeof o.spyOffset === 'function' ? o.spyOffset() : o.spyOffset != null ? o.spyOffset : off);
    let cur = null, lock = 0, lockT = 0, respyT = 0;
    const topOf = () => (isWin ? 0 : sc.getBoundingClientRect().top);
    function mark(a, animate) {
      if (!a) return;
      const moved = a !== cur; cur = a;
      links().forEach(l => l.setAttribute('aria-current', String(l === a)));
      ind.style.height = a.offsetHeight + 'px';
      spring(ind, { y: a.offsetTop }, { k: 520, c: 44, instant: !animate || !ind.classList.contains('on') });
      ind.classList.add('on');
      if (moved && o.onChange) o.onChange(a.dataset.target);
    }
    function spy() {
      if (lock) return;
      const ls = links(); if (!ls.length) return;
      const t = topOf(), line = spyOff(); let best = ls[0];
      ls.forEach(a => { const s = doc.getElementById(a.dataset.target); if (s && s.getBoundingClientRect().top - t <= line + 2) best = a; });
      const st = isWin ? scrollY : sc.scrollTop, sh = isWin ? doc.documentElement.scrollHeight : sc.scrollHeight, ch = isWin ? innerHeight : sc.clientHeight;
      if (st + ch >= sh - 2) best = ls[ls.length - 1];
      mark(best, true);
    }
    const onScroll = T.util.raf(spy);
    (isWin ? window : sc).addEventListener('scroll', () => { onScroll(); if (lock) { clearTimeout(lockT); lockT = setTimeout(() => { lock = 0; }, 140); } }, { passive: true });
    nav.addEventListener('click', e => {
      const a = e.target.closest('[data-target]'); if (!a) return;
      e.preventDefault();
      const s = doc.getElementById(a.dataset.target); if (!s) return;
      mark(a, true); lock = 1; clearTimeout(lockT); lockT = setTimeout(() => { lock = 0; }, 900);
      const y = (isWin ? scrollY : sc.scrollTop) + s.getBoundingClientRect().top - topOf() - off;
      (isWin ? window : sc).scrollTo({ top: Math.max(0, y), behavior: reduced ? 'auto' : 'smooth' });
      if (!s.hasAttribute('tabindex')) s.setAttribute('tabindex', '-1');
      s.focus({ preventScroll: true });
    });
    if (window.ResizeObserver) {
      new ResizeObserver(() => { if (cur) mark(cur, false); }).observe(nav);
      // sections that grow or shrink move the ones below without a scroll: look again once the
      // page's glide (SP.soft plus a short lag) has landed, so the rects read are the final ones
      const respy = () => { clearTimeout(respyT); respyT = setTimeout(() => { if (isShown(nav)) spy(); }, SP.soft.duration + 90); };
      const ro = new ResizeObserver(respy);
      requestAnimationFrame(() => links().forEach(a => { const s = doc.getElementById(a.dataset.target); if (s) ro.observe(s); }));
      if (!isWin) ro.observe(sc);
    }
    requestAnimationFrame(spy);
    return { el: nav, refresh: spy, set: id => mark(links().find(l => l.dataset.target === id), true) };
  }

  /* -- sparkline: T.ui.spark(el, { values, min, max, tone:'ink', area, dot, bands:[{at, cls}], format(v, i), label }) → { update(values) } --
     One path per update (call it once per sample, never per frame). null
     values are gaps (drawn as a dotted brick run on the baseline = down). */
  // one SVG <line> (T-P4-07: the sparkline's gaps and bands are built as nodes, not markup)
  function svgLine(cls, x1, x2, y1, y2) {
    const l = doc.createElementNS('http://www.w3.org/2000/svg', 'line');
    l.setAttribute('class', cls); l.setAttribute('x1', x1); l.setAttribute('x2', x2); l.setAttribute('y1', y1); l.setAttribute('y2', y2);
    return l;
  }
  function spark(el, o) {
    o = Object.assign({ area: true, dot: true }, o);
    el.classList.add('spk'); if (o.tone) el.classList.add(o.tone);
    el.replaceChildren(T.html`<svg aria-hidden="true"><g class="bands"></g><path class="a"/><path class="l"/><g class="gaps"></g><line class="hv"/><circle class="dot" r="2.6"/><circle class="hd" r="3"/></svg>${o.format ? T.raw('<span class="spk-tip"></span>') : ''}`);
    if (o.label) { el.setAttribute('role', 'img'); el.setAttribute('aria-label', o.label); }
    const svg = el.querySelector('svg'), q = s => svg.querySelector(s), tip = el.querySelector('.spk-tip');
    let vals = (o.values || []).slice(), g = null;
    function draw() {
      const W = el.clientWidth, H = el.clientHeight; if (!W || !H) return;
      svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
      const nums = vals.filter(v => v != null), pad = 3, n = vals.length;
      let lo = o.min != null ? o.min : nums.length ? Math.min(...nums) : 0, hi = o.max != null ? o.max : nums.length ? Math.max(...nums) : 1;
      if (o.max == null) (o.bands || []).forEach(b => { if (b.at > hi && b.at < hi * 1.35) hi = b.at; });
      if (hi - lo < 1e-9) { hi = lo + 1; lo -= 1; }
      const x = i => pad + (n <= 1 ? 0 : i / (n - 1) * (W - pad * 2)), y = v => H - pad - (clamp(v, lo, hi) - lo) / (hi - lo) * (H - pad * 2);
      let d = '', a = '', s0 = -1, g0 = -1;
      const gaps = [];
      for (let i = 0; i <= n; i++) {
        const v = i < n ? vals[i] : null;
        if (v == null) {
          if (s0 >= 0) { a += `L${x(i - 1).toFixed(1)},${H}L${x(s0).toFixed(1)},${H}Z`; s0 = -1; }
          if (i < n && g0 < 0) g0 = i;
          if ((i === n || vals[i + 1] != null) && g0 >= 0) { gaps.push(svgLine('gap', x(Math.max(0, g0 - (g0 ? 1 : 0))).toFixed(1), x(Math.min(n - 1, i + (i < n - 1 ? 1 : 0))).toFixed(1), H - pad, H - pad)); g0 = -1; }
          continue;
        }
        const P = `${x(i).toFixed(1)},${y(v).toFixed(1)}`;
        if (s0 < 0) { d += `M${P}`; a += `M${x(i).toFixed(1)},${H}L${P}`; s0 = i; } else { d += `L${P}`; a += `L${P}`; }
      }
      q('.l').setAttribute('d', d); q('.a').setAttribute('d', o.area ? a : '');
      q('.gaps').replaceChildren(...gaps);
      q('.bands').replaceChildren(...(o.bands || []).filter(b => b.at >= lo && b.at <= hi).map(b => svgLine(`band ${b.cls || ''}`, 0, W, Math.round(y(b.at)) + 0.5, Math.round(y(b.at)) + 0.5)));
      let li = -1; for (let i = n - 1; i >= 0; i--) if (vals[i] != null) { li = i; break; }
      const dot = q('.dot');
      if (o.dot && li >= 0) { dot.setAttribute('cx', x(li)); dot.setAttribute('cy', y(vals[li])); dot.style.display = ''; } else dot.style.display = 'none';
      g = { x, y, n, W, H, pad };
    }
    el.addEventListener('pointermove', e => {
      if (!g || !g.n || !o.format) return;
      const r = el.getBoundingClientRect(), i = clamp(Math.round((e.clientX - r.left - g.pad) / (g.W - g.pad * 2) * (g.n - 1)), 0, g.n - 1), v = vals[i];
      const X = g.x(i), Y = v == null ? g.H - g.pad : g.y(v);
      const hv = q('.hv'); hv.setAttribute('x1', X); hv.setAttribute('x2', X); hv.setAttribute('y1', 0); hv.setAttribute('y2', g.H);
      const hd = q('.hd'); hd.setAttribute('cx', X); hd.setAttribute('cy', Y);
      tip.textContent = o.format(v, i); tip.style.left = clamp(X, tip.offsetWidth / 2, g.W - tip.offsetWidth / 2) + 'px';
      el.classList.add('is-hover');
    });
    el.addEventListener('pointerleave', () => el.classList.remove('is-hover'));
    // while the window is hidden (T.scopes paused, T-P4-06 / G57) new values are kept and drawn
    // once on the way back, not on every sample
    let owed = false, offResume = null;
    const redraw = () => {
      if (!(T.scopes && T.scopes.paused)) { draw(); return; }
      if (owed) return;
      owed = true;
      offResume = T.scopes.onResume(() => { owed = false; if (offResume) offResume(); offResume = null; if (el.isConnected) draw(); });
    };
    if (window.ResizeObserver) new ResizeObserver(redraw).observe(el);
    draw();
    return { el, update(values) { vals = (values || []).slice(); redraw(); } };
  }

  /* -- unsaved changes: T.ui.saveBar({ count, effect, onSave() → Promise, onUndo() → false?, doneText }) → bar --
     effect, worded by what the user experiences (not by what the engine does):
             'instant' 保存后立即生效 · 'reload' 保存后立即生效，连接不中断 (the core reloads in place)
             · 'restart' 保存后会重启代理服务，流量中断约 1–2 秒 · or { icon, text, busy, done, warn }.
     One bar at a time: calling again updates it. bar.update({count, effect}) (count 0 hides),
     bar.save(), bar.hide(), bar.dirty, bar.confirmLeave() → Promise<boolean>
     (use it in the page's beforeLeave). Ctrl S saves. The bar leaves with its page.
     onSave rejecting with { hold: true } (validation) holds: 「还不能保存」 + the reason, still 保存.
     Focus inside the bar when it goes returns to where it came from. Under a modal overlay
     (#app.has-modal) the bar is out of sight until the last modal closes.
     T-P4-04a: every word is a catalog message (core.savebar.*, common.action.*); the bar
     repaints its current state on i18n:change. A built-in effect names its messages by id
     (text / busy / done); a caller's own { icon, text, busy, done, warn } carries words and is
     shown as given. core.savebar.effect.recheck / deferred / next_start are reserved for
     T-P4-05d (SectionResult.effect, a save while the service is starting or stopped).
     T.ui.saveBar.dirtyCount() → number: the unsaved items of the live bar (0 when there is
     none, or once saved); the shell's quit question adds the drawers' drafts to it
     (architecture §2.9; T-P4-06).
     T-P4-05d (settings.apply results, PROTOCOL §15): onSave may resolve with a string (the done
     text, as before) or with { outcome, text, retryLabel }:
       'applied' (default)  saved and in effect: 已保存 + text;
       'pending'            saved, applied once the service is ready: 已保存，待应用 + text (it
                            still goes away by itself; the page labels the rows);
       'not_applied'        the third result: saved (all or some sections) but not in effect:
                            已保存，但没有生效 + text; the bar stays, 撤销 reads 关闭 and 保存
                            reads retryLabel (default 重试) and runs o.onRetry() → the same result
                            shapes; without onRetry, or with res.noRetry (nothing a retry could
                            apply), only 关闭 (o.onClose() runs after it). Nothing is dirty in this state.
     A rejection with { conflict: true, title?, message? } (base_rev stale, the page re-read the
     snapshot) shows 配置已在别处修改 + the message and keeps the drafts, still 保存; { hold: true }
     (validation, a save held while the service starts) is as before. Each row's own result is
     the page's (the bar says one sentence). */
  const EFFECT = {
    instant: { icon: 'bolt', text: 'core.savebar.effect.instant', busy: 'core.savebar.busy.apply', done: 'core.savebar.done.instant' },
    reload: { icon: 'refresh', text: 'core.savebar.effect.reload', busy: 'core.savebar.busy.apply', done: 'core.savebar.done.reload' },
    restart: { icon: 'restart', text: 'core.savebar.effect.restart', busy: 'core.savebar.busy.restart', done: 'core.savebar.done.restart', warn: true },
  };
  // the words of an effect: a built-in one reads its messages now (the current language)
  const effectWords = (e, builtIn) => (builtIn
    ? { icon: e.icon, warn: e.warn, text: T.t(e.text), busy: T.t(e.busy), done: T.t(e.done) }
    : { icon: e.icon, warn: e.warn, text: String(e.text == null ? '' : e.text), busy: e.busy, done: e.done });
  // an error's own words: an rt error says what failed through its message_key (catalog)
  const errWords = err => (err && err.message_key && T.rt && typeof T.rt.errorText === 'function' ? T.rt.errorText(err) : err && err.message) || '';
  let SB = null;
  function saveBar(o) {
    if (SB && SB.alive) { SB.update(o || {}); return SB; }
    o = Object.assign({}, o);
    const main = T.$('.main');
    const el = T.h('div', { class: 'savebar' + (main ? '' : ' is-fixed'), role: 'region' });
    el.append(T.html`<div class="sb"><span class="sb-ic"><span class="sb-dot"></span></span><span class="sb-text"><span class="sb-count" aria-live="polite"></span><span class="sb-eff"></span></span><span class="sb-acts"><button class="btn btn-sm btn-ghost sb-undo" type="button"><span class="bi">${T.icon('undo', 15)}</span><span class="lab"></span></button><button class="btn btn-sm btn-ghost sb-close" type="button" hidden><span class="lab"></span></button><button class="btn btn-sm btn-primary sb-save" type="button"><span class="lab"></span><kbd translate="no">Ctrl</kbd><kbd translate="no">S</kbd></button></span><span class="prog sm sb-prog"><i></i></span></div>`);
    T.ti(el, 'core.savebar.aria', null, { attr: 'aria-label' });
    T.ti(el.querySelector('.sb-undo .lab'), 'core.savebar.undo');
    T.ti(el.querySelector('.sb-close .lab'), 'common.action.close');
    (main || doc.body).append(el);
    const app = T.$('#app'); if (app) app.classList.add('has-savebar');
    const q = s => el.querySelector(s);
    const cnt = q('.sb-count'), eff = q('.sb-eff'), ic = q('.sb-ic'), saveBtn = q('.sb-save');
    const E = () => (typeof api.effect === 'object' && api.effect ? effectWords(api.effect, false)
      : EFFECT[api.effect] ? effectWords(EFFECT[api.effect], true) : { icon: 'info', text: String(api.effect || '') });
    const api = { el, alive: true, state: 'dirty', count: 0, effect: 'instant', page: T.router && T.router.current };
    Object.defineProperty(api, 'dirty', { get: () => api.alive && api.state !== 'saved' && api.state !== 'not_applied' && api.count > 0 });
    let prevCount = 0;
    // the words of the state the bar is in (also the repaint after a language switch)
    let face = null; // { state, res, err } of the last result shown
    // the effect line: its glyph, then the words as text (T-P4-07: a Node face, no markup string)
    const effFace = (e, text) => T.html`${T.icon(e.icon || 'info', 13)}${text}`;
    const setIc = v => ic.replaceChildren(T.html`${v}`);
    function paint(animate) {
      const e = E();
      face = null;
      el.classList.remove('is-saving', 'is-saved', 'is-failed', 'is-held', 'is-partial');
      q('.sb-undo').hidden = false; q('.sb-close').hidden = true; saveBtn.hidden = false;
      if (!ic.querySelector('.sb-dot')) setIc(T.raw('<span class="sb-dot"></span>'));
      swapText(cnt, T.t('core.savebar.count', { count: api.count }), { instant: !animate, dir: api.count >= prevCount ? 1 : -1 });
      prevCount = api.count;
      swapText(eff, effFace(e, e.text), { instant: !animate });
      eff.removeAttribute('title');
      eff.classList.toggle('warn', !!e.warn);
      swapText(saveBtn.querySelector('.lab'), T.t('common.action.save'), { instant: !animate });
    }
    // saving / saved / held / failed: one place for the words of each result
    function paintFace(f, animate) {
      face = f;
      const e = E(), o2 = { instant: !animate }, lab = saveBtn.querySelector('.lab');
      if (f.state === 'saving') {
        swapText(cnt, T.t('core.savebar.saving', { count: api.count }), o2);
        swapText(eff, effFace(e, e.busy || T.t('core.savebar.busy.wait')), o2);
      } else if (f.state === 'saved') {
        // (T-P4-05d) a result object carries its own text; 'pending': saved, applied once the service is ready
        const r = f.res && typeof f.res === 'object' ? f.res : null;
        swapText(cnt, T.t(r && r.outcome === 'pending' ? 'core.savebar.saved_pending' : 'core.savebar.saved'), o2);
        swapText(eff, r ? String(r.text == null ? '' : r.text) : (typeof o.doneText === 'function' ? o.doneText(f.res) : o.doneText || e.done || T.t('core.savebar.done.instant')), o2);
      } else if (f.state === 'not_applied') {
        // the third result: saved, not in effect (all or some sections); the reason, then 重试应用 / 关闭
        swapText(cnt, T.t('core.savebar.not_applied'), o2);
        swapText(eff, String(f.res && f.res.text != null ? f.res.text : ''), o2);
        swapText(lab, (f.res && f.res.retryLabel) || T.t('common.action.retry'), o2);
      } else if (f.state === 'conflict') {
        swapText(cnt, (f.err && f.err.title) || T.t('core.savebar.conflict'), o2);
        swapText(eff, (f.err && f.err.message) || T.t('core.savebar.conflict_reason'), o2);
        swapText(lab, T.t('common.action.save'), o2);
      } else if (f.state === 'held') {
        swapText(cnt, T.t('core.savebar.held'), o2);
        swapText(eff, errWords(f.err) || T.t('core.savebar.held_reason'), o2);
        swapText(lab, T.t('common.action.save'), o2);
      } else if (f.state === 'failed') {
        swapText(cnt, T.t('core.savebar.failed'), o2);
        swapText(eff, errWords(f.err) || T.t('core.savebar.failed_reason'), o2);
        swapText(lab, T.t('common.action.retry'), o2);
      }
      // (T-P4-05d) a reason can outrun the bar (it ends in an ellipsis): the bar's title holds it whole
      const why = f.state === 'not_applied' || f.state === 'conflict' || f.state === 'held' || f.state === 'failed';
      const layer = why ? eff.querySelector('.sw:last-child') : null;
      if (layer && layer.textContent) eff.setAttribute('title', layer.textContent); else eff.removeAttribute('title');
    }
    const offLang = T.on('i18n:change', () => { if (!api.alive) return; if (face) paintFace(face, false); else paint(false); });
    api.update = n => {
      n = n || {};
      ['onSave', 'onUndo', 'doneText', 'onRetry', 'onClose'].forEach(k => { if (n[k]) o[k] = n[k]; });
      if (n.count != null) api.count = n.count;
      if (n.effect) api.effect = n.effect;
      if (api.state === 'saving') return api;
      // the third result stays until it is retried, closed, or a new draft takes the bar
      if (api.state === 'not_applied' && !(n.count > 0)) return api;
      if (api.count <= 0) { api.hide(); return api; }
      api.state = 'dirty'; paint(true); return api;
    };
    // run(fn): the save (or the retry of the apply) and its result face; save() / retry() below
    const run = fn => {
      api.state = 'saving'; el.classList.remove('is-failed', 'is-held', 'is-partial'); el.classList.add('is-saving');
      q('.sb-undo').hidden = false; q('.sb-close').hidden = true; saveBtn.hidden = false;
      setIc(T.raw('<span class="spin"></span>'));
      paintFace({ state: 'saving' }, true);
      q('.sb-prog').classList.add('is-pending');
      return Promise.resolve().then(fn).then(res => {
        if (!api.alive) return true;
        q('.sb-prog').classList.remove('is-pending');
        el.classList.remove('is-saving');
        if (res && typeof res === 'object' && res.outcome === 'not_applied') {
          api.state = 'not_applied'; el.classList.add('is-partial');
          setIc(T.icon('warning', 15));
          // res.noRetry (T-P4-05d fix M5): nothing a retry could apply (the bar says why): only 关闭
          api.noRetry = !o.onRetry || res.noRetry === true;
          q('.sb-undo').hidden = true; q('.sb-close').hidden = false; saveBtn.hidden = api.noRetry;
          paintFace({ state: 'not_applied', res }, true);
          eff.classList.remove('warn');
          return false;
        }
        api.state = 'saved';
        el.classList.add('is-saved');
        setIc(T.icon(res && res.outcome === 'pending' ? 'clock' : 'check', 15)); anim(ic, [{ transform: 'scale(.5)' }, { transform: 'none' }], { duration: SP.press.duration, easing: SP.press.easing });
        paintFace({ state: 'saved', res }, true);
        eff.classList.remove('warn');
        clearTimeout(api._t); api._t = setTimeout(() => { if (api.state === 'saved') api.hide(); }, res && res.outcome === 'pending' ? 2600 : 1700);
        return true;
      }, err => {
        if (!api.alive) return false;
        q('.sb-prog').classList.remove('is-pending');
        if (err && err.conflict) {
          // the configuration changed elsewhere (the page has re-read it): the drafts stay, 保存 again
          api.state = 'dirty'; el.classList.remove('is-saving', 'is-failed'); el.classList.add('is-held');
          setIc(T.icon('warning', 15));
          paintFace({ state: 'conflict', err }, true);
          eff.classList.remove('warn');
          return false;
        }
        if (err && err.hold) {
          // validation stopped the save before anything was applied: the drafts stay drafts, the bar
          // says why (the page shows the field), and the button stays 保存 (it is not a retry)
          api.state = 'dirty'; el.classList.remove('is-saving', 'is-failed'); el.classList.add('is-held');
          setIc(T.icon('warning', 15));
          paintFace({ state: 'held', err }, true);
          eff.classList.remove('warn');
          return false;
        }
        api.state = 'failed';
        el.classList.remove('is-saving'); el.classList.add('is-failed');
        setIc(T.icon('error', 15));
        paintFace({ state: 'failed', err }, true);
        eff.classList.remove('warn');
        return false;
      });
    };
    // 保存 runs onSave; in the third result it retries the apply (onRetry); after a failed retry 重试 retries it again
    let last = 'save';
    api.save = () => {
      if (!api.alive || api.state === 'saving') return Promise.resolve(false);
      if (api.state === 'not_applied' && api.noRetry) return Promise.resolve(false);   // (Ctrl S: no retry to run)
      if (api.state === 'not_applied' || (api.state === 'failed' && last === 'retry')) return api.retry();
      if (!o.onSave) return Promise.resolve(false);
      last = 'save';
      return run(o.onSave);
    };
    api.retry = () => {
      if (!api.alive || api.state === 'saving' || !o.onRetry) return Promise.resolve(false);
      last = 'retry';
      return run(o.onRetry);
    };
    // focus that is inside the bar when it goes (撤销, a saved result) returns to where it came from
    let back = null;
    const onFocusIn = ev => { if (!el.contains(ev.target) && ev.target !== doc.body) back = ev.target; };
    doc.addEventListener('focusin', onFocusIn);
    if (doc.activeElement && doc.activeElement !== doc.body) back = doc.activeElement;
    api.hide = instant => {
      if (!api.alive) return; api.alive = false; if (SB === api) SB = null;
      clearTimeout(api._t); offPage(); offLang();
      doc.removeEventListener('focusin', onFocusIn);
      if (el.contains(doc.activeElement)) {
        const t = back && back.isConnected && isShown(back) && !back.disabled ? back : null;
        if (t) t.focus({ preventScroll: true }); else doc.activeElement.blur();
      }
      if (app) app.classList.remove('has-savebar');
      if (instant || reduced || (app && app.classList.contains('has-modal'))) { el.remove(); return; }   // (under a modal it is already out of sight)
      const a = anim(el, [{ opacity: 1, transform: 'none' }, { opacity: 0, transform: 'translateY(14px)' }], { duration: 180, easing: EASE.in, fill: 'forwards' });
      onDone(a, () => el.remove(), 400);
    };
    api.confirmLeave = () => T.confirm({ title: T.t('core.savebar.leave.confirm.title', { count: api.count }), message: T.t('core.savebar.leave.confirm.body'),
      confirmLabel: T.t('common.action.discard'), cancelLabel: T.t('core.savebar.leave.confirm.stay'), danger: true })
      .then(ok => { if (ok) { if (o.onUndo) o.onUndo(); api.hide(true); } return ok; });
    q('.sb-undo').addEventListener('click', () => { const r = o.onUndo ? o.onUndo() : undefined; if (r !== false) api.hide(); });
    q('.sb-close').addEventListener('click', () => { api.hide(); if (o.onClose) o.onClose(); });
    saveBtn.addEventListener('click', () => api.save());
    const offPage = T.on('page:change', ev => { if (api.page && ev.id !== api.page) api.hide(true); });
    SB = api;
    api.count = o.count || 0; api.effect = o.effect || 'instant'; prevCount = api.count;
    paint(false);
    // created under a modal (an edit inside a drawer): no entrance now, the CSS brings it in when the modal closes
    if (!(app && app.classList.contains('has-modal'))) anim(el, [{ opacity: 0, transform: 'translateY(18px) scale(.98)' }, { opacity: 1, transform: 'none' }], { duration: SP.soft.duration, easing: SP.soft.easing });
    return api;
  }
  /* cross-page interface (T-P4-04a; architecture §2.9, critique C9): the number of unsaved
     items the live save bar holds, as the bar shows it (0 without a bar, or once it is saved).
     T-P4-05d reworks the bar and keeps this signature. */
  saveBar.dirtyCount = () => (SB && SB.dirty ? SB.count : 0);

  /* -- small builders (return T.raw HTML strings or Elements) ------------------ */
  /** T.ui.status('slow', { chip, label, last }) → <span class="st st-slow">较慢</span>
      The key is one of ST_KEYS; its word is the message core.status.<key> (T-P4-04a: callers pass
      the key only, never the word). An unknown key is shown as given.
      last: true → the last known result while the service is stopped: the same shape dimmed,
      the word core.status.last.<key> 上次可用 / 上次较慢 / 上次不可用 (only ok / slow / down have a
      last-known form) */
  const ST_KEYS = ['ok', 'slow', 'down', 'pending', 'recheck', 'draft', 'current', 'off', 'unknown', 'error', 'stale'];
  const LAST = { ok: 1, slow: 1, down: 1 };
  const status = (k, o) => {
    o = o || {};
    const last = !!o.last && !!LAST[k];
    const word = o.label || (last ? T.t(`core.status.last.${k}`) : ST_KEYS.includes(k) ? T.t(`core.status.${k}`) : String(k));
    return T.html`<span class="st st-${k}${last ? ' st-last' : ''}${o.chip ? ' chip-st' : ''}">${word}</span>`;
  };
  /** T.ui.lat(ms, { last, ceiling, word }) → band-coloured latency. Unreachable (null): the last-known
      value struck through and faint (the status column says 不可用); without `last`, or with word:true, the word 不可用. */
  const lat = (ms, o) => {
    o = o || {};
    if (ms == null) {
      const last = o.last != null ? T.html`<span class="lat-last" title="${T.t('core.lat.last')}">${Math.round(o.last)} ms</span>` : null;
      return o.word || !last ? T.html`<span class="lat lat-down">${T.t('core.lat.down')}</span>${last}` : last;
    }
    const b = T.fmt.latBand(ms, o.ceiling);
    return T.html`<span class="lat${b === 'ok' ? '' : ' lat-' + b}">${Math.round(ms)} ms</span>`;
  };
  /** T.ui.dots(total, ok, { off, label, states, last }) → dot row (failed = hollow brick ring)
      states: one status key per node (ok · slow · down · pending · recheck · draft · off) instead of
      total/ok: 较慢 half-filled, 不可用 a hollow ring, 待检查 / 待复检 a dashed ring, 草稿 / off a grey ring.
      last: true → the last known result (service stopped): the same shapes, dimmed. */
  const DOT = { ok: '', slow: 'slow', down: 'fail', pending: 'pend', recheck: 'pend', draft: 'off', off: 'off' };
  const dots = (total, ok, o) => {
    o = o || {}; const s = [];
    if (o.states) {
      total = o.states.length;
      o.states.forEach(k => { const c = DOT[k] != null ? DOT[k] : 'off'; s.push(T.h('i', { class: o.last && LAST[k] ? `last ${c || 'ok'}` : c })); });
    } else for (let i = 0; i < total; i++) s.push(T.h('i', { class: o.off ? 'off' : i < ok ? (o.last ? 'last ok' : '') : o.last ? 'last fail' : 'fail' }));
    return T.h('span', o.label ? { class: 'dots', role: 'img', 'aria-label': o.label } : { class: 'dots', 'aria-hidden': 'true' }, total ? s : T.h('span', { class: 'none' }));
  };
  /** T.ui.tierTag('sentinel', { link, icon:false, size }) → icon + codename (link → 层级).
      The one way a tier is named anywhere (hero, palette, sentences). The code name is never
      translated (translate="no"). F-03 (user 10-28): no role word beside it (「自建」 etc. were the
      product's advice on what to put in a tier, which the interface does not carry). */
  const tierTag = (id, o) => {
    o = o || {};
    const t = T.data && T.data.tier(id); if (!t) return doc.createDocumentFragment();
    // (mapping_core U5) the name is text, the id an escaped attribute value
    const inner = [o.icon === false ? '' : T.icon(t.icon, o.size || 14), T.html`<b translate="no">${t.name}</b>`];
    return o.link ? T.html`<button class="ttag ttag-btn" type="button" data-go="profiles" data-go-tier="${t.id}" aria-label="${T.t('core.tier_tag.aria', { name: t.name })}">${inner}</button>` : T.html`<span class="ttag">${inner}</span>`;
  };
  const kbd = keys => T.html`${String(keys).split(/\s+/).map(k => T.html`<kbd>${k}</kbd>`)}`;
  /** T.ui.tech('控制端口 127.0.0.1:25500') → the text escaped, with every IP (v4 / v6, CIDR, :port),
      URL, host or domain wrapped in <span class="tech"> (Geist Mono): technical values are never
      set in the sans face, where 127.0.0.1 reads as 127.0.01 */
  // (full-width ，。；、 written as escapes: no CJK literal in the source, architecture §3.9)
  const TECH_RE = /([a-z][a-z0-9+.-]*:\/\/[^\s<>"'\uFF0C\u3002\uFF1B\u3001]+|\b(?:\d{1,3}\.){3}\d{1,3}(?:\/\d{1,2})?(?::\d{1,5})?\b|\b[0-9a-f]{1,4}(?::[0-9a-f]{1,4}){7}\b|(?:\b[0-9a-f]{1,4})?(?::[0-9a-f]{1,4})*::(?:[0-9a-f]{1,4}(?::[0-9a-f]{1,4})*)?(?:\/\d{1,3})?|\b(?:[a-z0-9-]+\.)+[a-z]{2,}(?::\d{1,5})?\b)/gi;
  // (T-P4-07) built as nodes: the text stays text, each technical value a <span class="tech">
  const tech = text => {
    const s = String(text == null ? '' : text), f = doc.createDocumentFragment();
    let at = 0;
    for (const m of s.matchAll(TECH_RE)) {
      if (m[0].length <= 2) continue;
      if (m.index > at) f.append(s.slice(at, m.index));
      f.append(T.h('span', { class: 'tech' }, m[0]));
      at = m.index + m[0].length;
    }
    if (at < s.length) f.append(s.slice(at));
    return f;
  };
  /** T.ui.callout({ kind:'info'|'success'|'warning'|'error', title, message, actions, small }) → Element */
  function callout(o) {
    o = o || {};
    const kind = o.kind || 'info', icon = { info: 'info', success: 'success', warning: 'warning', error: 'error' }[kind] || 'info';
    const el = T.h('div', { class: `callout callout-${kind}${o.small ? ' sm' : ''}`, role: kind === 'error' ? 'alert' : 'note' });
    el.append(T.html`${T.icon(o.icon || icon, o.small ? 16 : 18)}<div class="callout-body">${o.title ? T.html`<b class="callout-t">${o.title}</b>` : ''}${o.message ? T.html`<span>${o.message}</span>` : ''}</div>`);
    if (o.actions && o.actions.length) el.append(T.h('div', { class: 'callout-act' }, actionNodes(o.actions, true)));
    if (o.actions) wireActions(el, o.actions.map(a => Object.assign({ keep: true }, a)), () => {});
    return el;
  }
  /** T.ui.empty({ icon, title, text, actions, row }) → Element */
  function empty(o) {
    o = o || {};
    const el = T.h('div', { class: 'empty' + (o.row ? '' : ' is-col') });
    el.append(T.html`<span class="empty-ic">${T.icon(o.icon || 'search', 20)}</span><div><div class="empty-t">${o.title || ''}</div>${o.text ? T.html`<div class="empty-s">${o.text}</div>` : ''}</div>`);
    if (o.actions && o.actions.length) el.append(T.h('div', { class: 'empty-a' }, actionNodes(o.actions, false)));
    if (o.actions) wireActions(el, o.actions.map(a => Object.assign({ keep: true }, a)), () => {});
    return el;
  }
  /** T.ui.skeleton(rows) → skeleton rows (only in the DOM while loading) */
  const skeleton = rows => {
    // (T-P4-07) nodes; the widths through the CSSOM (el.style), not markup
    const r = T.util.rng((rows || 5) * 7 + 3), f = doc.createDocumentFragment();
    for (let i = 0; i < (rows || 5); i++) {
      f.append(T.h('div', { class: 'skel-row', 'aria-hidden': 'true' }, T.h('span', { class: 'skel skel-dot' }),
        T.h('span', { class: 'skel skel-t', style: { width: `${Math.round(26 + r() * 32)}%` } }),
        T.h('span', { class: 'skel skel-t', style: { width: `${Math.round(8 + r() * 10)}%`, marginLeft: 'auto' } })));
    }
    return f;
  };
  /** T.ui.meter(el, { value, max, min }) → { set(value, max, min) }  (fill = scaleX) */
  function meter(el, o) {
    o = o || {};
    el.classList.add('meter');
    if (!el.querySelector('.meter-track')) el.replaceChildren(T.html`<span class="meter-track"><span class="meter-fill"></span></span>${o.min != null ? T.raw('<span class="meter-min"></span>') : ''}`);
    const api = { set(v, max, min) { max = max || 1; el.style.setProperty('--v', clamp((v || 0) / max, 0, 1).toFixed(4)); if (min != null) el.style.setProperty('--min', clamp(min / max, 0, 1).toFixed(4)); } };
    api.set(o.value || 0, o.max, o.min);
    return api;
  }
  /** T.ui.progress(el) → { set(0..1), pending(on), done() }; indeterminate runs only under .is-pending */
  function progress(el) {
    el.classList.add('prog'); if (!el.querySelector('i')) el.append(doc.createElement('i'));
    return {
      set(p) { el.classList.remove('is-pending', 'is-done'); el.style.setProperty('--p', clamp(p, 0, 1)); },
      pending(on) { el.classList.toggle('is-pending', on !== false); el.classList.remove('is-done'); },
      done() { el.classList.remove('is-pending'); el.style.setProperty('--p', 1); clearTimeout(el._pt); el._pt = setTimeout(() => el.classList.add('is-done'), 460); },
    };
  }

  T.ui = {
    seg, copy, copyText, busy, reserve, hideTip,
    switch: switchCtl, options, field, number, search, select, menu, popover, closeFloats, slider, tabs,
    listNav, fold, sortable, toc, spark, saveBar, meter, progress,
    status, lat, dots, tierTag, kbd, tech, callout, empty, skeleton,
    get saveBarCurrent() { return SB && SB.alive ? SB : null; },
  };

  /* cross-page interface (T-P4-04a; architecture §2.9, critique C9; core/README.md):
     T.shellc.registerDraft(fn) → unregister. A drawer with an unsaved draft (05b, 05d, 05e)
     registers fn() → number | boolean (its unsaved items; true counts as one) while it is open
     and calls unregister() when it closes. The shell's quit question (host:confirmQuit) counts
     T.ui.saveBar.dirtyCount() plus these. T-P4-06: the registry is shellc/quit.js (the same one
     the quit answer counts); the signature stays. */
  T.shellc = T.shellc || {};
  T.shellc.registerDraft = shellcRegisterDraft;

  /* markup-only behaviour: .switch toggles aria-checked, .fchip toggles aria-pressed
     (both fire a bubbling 'change'); [data-go="page"] + data-go-<param> navigates */
  doc.addEventListener('click', e => {
    const t = e.target.closest && e.target.closest('.switch, .fchip, [data-go]');
    if (!t) return;
    if (t.hasAttribute('data-go')) {
      e.preventDefault();
      const params = {};
      for (const k in t.dataset) if (k.length > 2 && k.startsWith('go')) params[k[2].toLowerCase() + k.slice(3)] = t.dataset[k];
      T.go(t.dataset.go, params); return;
    }
    if (t._sw || t.disabled || t.hasAttribute('data-manual') || t.getAttribute('aria-disabled') === 'true') return;
    const attr = t.classList.contains('switch') ? 'aria-checked' : 'aria-pressed';
    const v = t.getAttribute(attr) !== 'true'; t.setAttribute(attr, String(v));
    t.dispatchEvent(new CustomEvent('change', { bubbles: true, detail: { checked: v, pressed: v } }));
  });
  /* Ctrl F → the visible search · Ctrl S → the save bar */
  doc.addEventListener('keydown', e => {
    if (!(e.ctrlKey || e.metaKey) || e.altKey || e.shiftKey) return;
    const k = e.key.toLowerCase();
    if (k === 'f') {
      const top = stack.filter(x => x.modal).pop();
      const s = T.$$('[data-hotkey~="ctrl+f"]').find(x => isShown(x) && (!top || top.panel.contains(x)));
      if (!s) return;
      e.preventDefault(); const inp = s.querySelector('input') || s; inp.focus(); if (inp.select) inp.select();
    } else if (k === 's' && SB && SB.alive && SB.state !== 'saving' && !T.overlayOpen()) { e.preventDefault(); SB.save(); }
  });

  /* ============================================================== router */
  const ALIAS = { tiers: 'profiles', layers: 'profiles', routes: 'profiles', log: 'logs', config: 'settings' };
  const HASH = { overview: 'overview', profiles: 'tiers', logs: 'logs', settings: 'settings' };
  const R = { pages: new Map(), current: null, started: false, titles: {} };
  /* scope intervals pause while the window is hidden (T-P4-06; mapping_core R6, R10, G57):
     T.scopes.pause() stops every live scope.every() timer; T.scopes.resume() runs each paused
     callback once at once (the data catches up first) and restarts its interval. The shell layer
     (shellc/index.js) drives it from store `presence`. scope.after() one-shots are not paused. */
  const TIMERS = { paused: false, live: new Set(), onResume: new Set() };
  function startTimer(tm) { tm.id = setInterval(tm.fn, tm.ms); }
  T.scopes = {
    get paused() { return TIMERS.paused; },
    pause() {
      if (TIMERS.paused) return;
      TIMERS.paused = true;
      TIMERS.live.forEach(tm => { clearInterval(tm.id); tm.id = 0; });
    },
    resume() {
      if (!TIMERS.paused) return;
      TIMERS.paused = false;
      Array.from(TIMERS.live).forEach(tm => {
        if (!TIMERS.live.has(tm) || tm.id) return;
        try { tm.fn(); } catch (e) { setTimeout(() => { throw e; }); }
        if (TIMERS.live.has(tm) && !TIMERS.paused && !tm.id) startTimer(tm);
      });
      Array.from(TIMERS.onResume).forEach(fn => { try { fn(); } catch (e) { setTimeout(() => { throw e; }); } });
    },
    /* fn() after every resume → off (core's own deferred work, e.g. chart redraws) */
    onResume(fn) { TIMERS.onResume.add(fn); return () => TIMERS.onResume.delete(fn); },
  };
  function makeScope() {
    const fns = [];
    return {
      add(fn) { fns.push(fn); return fn; },
      on(e, fn) { const off = T.on(e, fn); fns.push(off); return off; },
      sub(k, fn, o) { const off = T.store.sub(k, fn, o); fns.push(off); return off; },
      every(ms, fn) {
        const tm = { ms, fn, id: 0 };
        TIMERS.live.add(tm);
        if (!TIMERS.paused) startTimer(tm);
        fns.push(() => { clearInterval(tm.id); tm.id = 0; TIMERS.live.delete(tm); });
        return tm;
      },
      after(ms, fn) { const id = setTimeout(fn, ms); fns.push(() => clearTimeout(id)); return id; },
      listen(el, t, fn, o) { el.addEventListener(t, fn, o); fns.push(() => el.removeEventListener(t, fn, o)); },
      dispose() { fns.splice(0).forEach(f => { try { f(); } catch (e) { /* ignore */ } }); },
    };
  }
  /* a page's title (crumb, window title, the section's name): the message def.titleId, else
     shell.page.<id>.title for the four shell pages, else def.title (T-P4-04a; read now, so it
     follows the language) */
  const SHELL_PAGES = ['overview', 'profiles', 'logs', 'settings'];
  function titleOf(p) {
    if (!p) return '';
    if (p.def.titleId) return T.t(p.def.titleId);
    if (SHELL_PAGES.includes(p.id) && T.t) return T.t(`shell.page.${p.id}.title`);
    return p.def.title || p.id;
  }
  T.page = function (id, def) {
    const p = { id, def: def || {}, el: null, rendered: false, scope: null, life: makeScope(), ctx: null };
    R.pages.set(id, p);
    // (a getter: the title in the current language whenever it is read)
    Object.defineProperty(R.titles, id, { get: () => titleOf(p), enumerable: true, configurable: true });
    if (T.$('#pages')) mount(p);
    return p;
  };
  function mount(p) {
    if (p.el) return;
    p.el = T.h('section', { class: `page page-${p.id}`, 'data-page': p.id, 'aria-label': titleOf(p), hidden: true });
    T.$('#pages').append(p.el);
  }
  /* relocalize 'rerender' (architecture §3.7 item 4): a fresh render of a page that is already
     rendered, its own state kept by snapshotState() / restoreState(state). One order on both
     paths: snapshotState → (leave) → render → enter → restoreState; the page's life scope is
     renewed (render's listeners do not double up). Every page hook is guarded like the router's
     others (an exception is rethrown asynchronously; the switch or the navigation goes on).
     - The page shown now (rerender(p)): render and enter get params {} (a one-time landing such
       as focus is not replayed: the state comes back through restoreState only); enter runs
       with ctx.relocalized = true and ctx.navParams = the last navigation's params (frozen, read
       only). Focus that was inside the page and is lost to <body> goes to ctx.landing or the
       page's root.
     - A page redone on its next visit (rerender(p, opts) from T.go): render gets that new
       navigation's params; the returned finish() is the restore, which T.go runs after it has
       made the page current and run enter. */
  function hook(fn) { try { return fn(); } catch (e) { setTimeout(() => { throw e; }); return undefined; } }
  function rerender(p, navOpts) {
    const def = p.def;
    const shown = navOpts === undefined && R.current === p.id;
    const st = hook(() => (typeof def.snapshotState === 'function' ? def.snapshotState() : undefined));
    const finish = () => { if (typeof def.restoreState === 'function') hook(() => def.restoreState(st)); };
    const ae = doc.activeElement;
    const hadFocus = shown && !!ae && ae !== doc.body && p.el.contains(ae);
    if (shown) {
      hook(() => { if (def.leave) def.leave(p.ctx); });
      if (p.scope) p.scope.dispose();
    }
    p.life.dispose(); p.life = makeScope();
    const params = shown ? {} : Object.assign({}, navOpts);
    p.el.replaceChildren();
    hook(() => { if (def.render) def.render(p.el, { id: p.id, root: p.el, life: p.life, params, relocalized: true }); });
    if (!shown) return finish;
    p.scope = makeScope();
    const last = p.ctx ? (p.ctx.navParams || p.ctx.params) : null;
    const navParams = Object.freeze(Object.assign({}, last));
    p.ctx = Object.assign({}, p.ctx, { root: p.el, params, navParams, scope: p.scope, life: p.life, first: false, again: false, relocalized: true, landing: null });
    hook(() => { if (def.enter) def.enter(p.ctx); });
    finish();
    const now = doc.activeElement;
    if (hadFocus && (!now || now === doc.body)) {
      // (T-P4-09) from the next frame's callback: isShown() and focus() lay out the whole document,
      // which right after a language switch means laying out every relabelled text at once (40 ms on
      // 配置); the frame lays it out anyway. Skipped when something took the focus meanwhile.
      requestAnimationFrame(() => {
        const a = doc.activeElement;
        if ((a && a !== doc.body) || !p.el.isConnected || R.current !== p.id) return;
        const t = p.ctx.landing && p.ctx.landing.isConnected && isShown(p.ctx.landing) ? p.ctx.landing : p.el;
        if (t === p.el && !p.el.hasAttribute('tabindex')) p.el.tabIndex = -1;
        hook(() => t.focus({ preventScroll: true }));
      });
    }
    return null;
  }
  /* i18n:change (T-P4-04a; ruling 4th wave T-P4-03 #2): the crumb, the window title and each
     page's name follow the language; a page with relocalize:'rerender' is rendered again (the
     page shown now, the others on their next visit) */
  T.on('i18n:change', () => {
    R.pages.forEach(p => {
      if (p.el) p.el.setAttribute('aria-label', titleOf(p));
      if (p.rendered && p.def.relocalize === 'rerender') {
        if (R.current === p.id) { p.relocalizeDue = false; rerender(p); } else p.relocalizeDue = true;
      }
    });
    if (R.current) {
      const p = R.pages.get(R.current);
      setCrumb(titleOf(p), false);
      doc.title = T.t('shell.window.title', { page: titleOf(p) });
    }
  });

  /* nav indicator (JS spring, velocity carries through rapid switching) + hover pill */
  function navItem(id) { return T.$(`.nav-item[data-nav="${id}"]`); }
  function placeNav(id, animate) {
    const ind = T.$('.nav-ind'), it = navItem(id); if (!ind || !it) return;
    T.$$('.nav-item').forEach(n => { if (n === it) n.setAttribute('aria-current', 'page'); else n.removeAttribute('aria-current'); });
    spring(ind, { y: it.offsetTop }, { k: 520, c: 44, instant: !animate });
  }
  function wireNav() {
    const nav = T.$('#nav'), hov = T.$('.nav-hov'); if (!nav) return;
    nav.addEventListener('pointerover', e => {
      const it = e.target.closest('.nav-item'); if (!it) return;
      const wasHidden = hov.style.opacity !== '1';
      if (wasHidden) hov.style.transition = 'opacity .15s';
      hov.style.translate = `0 ${it.offsetTop}px`;
      hov.style.opacity = it.getAttribute('aria-current') === 'page' ? '0' : '1';
      if (wasHidden) requestAnimationFrame(() => { hov.style.transition = 'opacity .15s, translate .2s cubic-bezier(.2,.8,.2,1)'; });
    });
    nav.addEventListener('pointerleave', () => { hov.style.opacity = '0'; });
    nav.addEventListener('click', e => { const it = e.target.closest('.nav-item'); if (it) { T.go(it.dataset.nav); hov.style.opacity = '0'; } });
  }
  function setCrumb(text, animate) {
    const box = T.$('#crumb'); if (!box) return;
    const old = box.querySelector('span:not(.leaving)');
    if (old && old.textContent === text) return;
    const nu = T.h('span', { text }); box.append(nu);
    if (!old) return;
    if (!animate || reduced) { old.remove(); return; }
    old.classList.add('leaving');
    const a = old.animate([{ opacity: 1, transform: 'none' }, { opacity: 0, transform: 'translateY(-9px)' }], { duration: 150, easing: EASE.in, fill: 'forwards' });
    onDone(a, () => old.remove(), 400);
    nu.animate([{ opacity: 0, transform: 'translateY(10px)' }, { opacity: 1, transform: 'none' }], { duration: 240, delay: 30, easing: EASE.out, fill: 'backwards' });
  }
  /* continuous crossfade: outgoing fades in ~100 ms while the incoming staggers in (no blank frames) */
  function swapPages(from, to, animate) {
    const host = T.$('#host');
    T.$$('.page.is-leaving').forEach(p => { p.getAnimations().forEach(a => a.cancel()); p.classList.remove('is-leaving'); p.style.top = ''; p.style.height = ''; if (p !== to) p.hidden = true; });
    // a page interrupted while it was still staggering in: bring its blocks to full
    // opacity first, so the 110 ms fade-out starts from what is visible (no blank frame)
    if (from && from._enter) { from._enter.forEach(a => { try { if (a && a.playState !== 'finished') a.finish(); } catch (e) { /* ignore */ } }); from._enter = null; }
    if (from && animate && !reduced && !from.hidden) {
      // the outgoing page keeps its box while it fades: taken out of flow its height would follow
      // its content, and a layout that fills the window (the overview's chart) would squash
      const h = from.offsetHeight;
      from.style.top = -host.scrollTop + 'px';
      from.style.height = h + 'px';
      from.classList.add('is-leaving');
      const out = from.animate([{ opacity: 1, transform: 'none' }, { opacity: 0, transform: 'translateY(-4px)' }], { duration: 110, easing: 'linear', fill: 'forwards' });
      onDone(out, () => { if (from.classList.contains('is-leaving')) { out.cancel(); from.classList.remove('is-leaving'); from.style.top = ''; from.style.height = ''; from.hidden = from !== to; } }, 400);
    } else if (from) from.hidden = true;
    to.hidden = false;
    host.scrollTop = 0;
    if (animate) {
      let blocks = T.$$('[data-enter]', to);
      if (!blocks.length) blocks = Array.from(to.children);
      to._enter = stagger(blocks, { base: 20, step: 18, dur: 240, y: 7, max: 8 });
    }
  }
  function writeHash(id) {
    const tok = '#' + (HASH[id] || id);
    if (location.hash !== tok) { try { history.replaceState(null, '', tok); } catch (e) { location.hash = tok; } }
  }
  /* keyboard landing: a navigation made from the keyboard (a link in a page, a banner action, the
     palette, Ctrl 1–4) must not drop focus on the document body. Focus goes to what the page names as its
     landing (ctx.landing, set in enter(), or later T.router.land(el) once a glide ends), else to the
     page itself, so the next Tab continues inside the page. Focus that is in the sidebar or the top
     bar stays there. */
  function landFocus(p) {
    R.kbNav = lastInput === 'key' ? p.id : null;
    if (!R.kbNav) return;
    const ae = doc.activeElement;
    if (ae && ae !== doc.body && ae.closest && ae.closest('.side, .topbar') && isShown(ae)) return;
    let t = p.ctx && p.ctx.landing && isShown(p.ctx.landing) ? p.ctx.landing : p.el;
    if (t === p.el && !p.el.hasAttribute('tabindex')) p.el.tabIndex = -1;
    t.focus({ preventScroll: true });
  }
  doc.addEventListener('pointerdown', () => { R.kbNav = null; }, true);
  T.go = function (id, opts) {
    opts = opts || {};
    id = ALIAS[id] || id;
    const p = R.pages.get(id); if (!p) return false;
    const prev = R.current ? R.pages.get(R.current) : null;
    hideTip();
    if (!opts.keepOverlays) closeModals();
    if (prev === p) {
      // same page: enter() runs again with ctx.again = true on a fresh scope (the old
      // scope's listeners and timers are disposed first, so nothing doubles up)
      if (p.scope) p.scope.dispose();
      p.scope = makeScope();
      p.ctx = Object.assign({}, p.ctx, { params: opts, again: true, first: false, scope: p.scope, landing: null });
      // a navigation, not a re-render: drop what a language re-render left on the ctx
      delete p.ctx.relocalized; delete p.ctx.navParams;
      try { if (p.def.enter) p.def.enter(p.ctx); } catch (e) { setTimeout(() => { throw e; }); }
      landFocus(p);
      return true;
    }
    // a page may hold navigation (unsaved changes): def.beforeLeave(ctx, toId) → false | Promise<boolean>
    if (prev && !opts.force && prev.def.beforeLeave) {
      let ok = true;
      try { ok = prev.def.beforeLeave(prev.ctx, id); } catch (e) { setTimeout(() => { throw e; }); }
      if (ok === false) { writeHash(prev.id); return false; }
      if (ok && typeof ok.then === 'function') {
        ok.then(yes => { if (yes) T.go(id, Object.assign({}, opts, { force: true })); else writeHash(prev.id); });
        return false;
      }
    }
    closeFloats(); closeModeless();
    const animate = !opts.instant && R.started;
    if (prev) {
      try { if (prev.def.leave) prev.def.leave(prev.ctx); } catch (e) { setTimeout(() => { throw e; }); }
      if (prev.scope) prev.scope.dispose();
    }
    mount(p);
    const flipRect = opts.flipFrom && opts.flipFrom.isConnected ? opts.flipFrom.getBoundingClientRect() : null;
    let relocalized = null;
    if (!p.rendered) {
      p.rendered = true; p.relocalizeDue = false;
      try { if (p.def.render) p.def.render(p.el, { id, root: p.el, life: p.life, params: opts }); } catch (e) { setTimeout(() => { throw e; }); }
    } else if (p.relocalizeDue) {
      // the language changed while the page was away: render it again before it is shown (its
      // state is restored once it is current and has entered, below)
      p.relocalizeDue = false;
      relocalized = rerender(p, opts);
    }
    R.current = id;
    T.store.set('page', id);
    placeNav(id, animate);
    setCrumb(titleOf(p), animate);
    doc.title = T.t('shell.window.title', { page: titleOf(p) });
    writeHash(id);
    p.scope = makeScope();
    p.ctx = { id, root: p.el, params: opts, from: prev ? prev.id : null, first: !p.entered, scope: p.scope, life: p.life, again: false };
    p.entered = true;
    swapPages(prev && prev.el, p.el, animate);
    try { if (p.def.enter) p.def.enter(p.ctx); } catch (e) { setTimeout(() => { throw e; }); }
    if (relocalized) relocalized();
    if (flipRect && animate) {
      const key = opts.flipKey;
      const target = key ? p.el.querySelector(`[data-flip-target="${key}"]`) : null;
      handoff(opts.flipFrom, target, { rect: flipRect });
    }
    landFocus(p);
    T.emit('page:change', { id, from: prev ? prev.id : null, params: opts });
    return true;
  };
  T.router = {
    get current() { return R.current; },
    /* a landing that is ready only later (after a glide): focuses el if the last navigation to
       this page came from the keyboard and the user has not pointed anywhere since */
    land(el) { if (el && R.kbNav && R.kbNav === R.current && isShown(el)) el.focus({ preventScroll: true }); },
    titles: R.titles,
    pages: R.pages,
    parseHash(h) { const tok = String(h || '').replace(/^#\/?/, '').split(/[/?]/)[0]; const id = ALIAS[tok] || tok; return R.pages.has(id) ? id : null; },
    start(initial) {
      R.pages.forEach(mount);
      wireNav();
      const id = T.router.parseHash(location.hash) || initial || 'overview';
      T.go(id, { instant: true });            // first paint: no crossfade …
      R.started = true;
      const p = R.pages.get(id);              // … but keep the quick entrance stagger (<= 400 ms)
      if (p && p.el) { let blocks = T.$$('[data-enter]', p.el); if (!blocks.length) blocks = Array.from(p.el.children); p.el._enter = stagger(blocks, { base: 40, step: 18, dur: 260, y: 8, max: 6 }); }
      addEventListener('hashchange', () => { const t = T.router.parseHash(location.hash); if (t && t !== R.current) T.go(t); });
      T.util.fontsReady().then(() => placeNav(R.current, false));
      addEventListener('resize', T.util.raf(() => placeNav(R.current, false)));
    },
  };

  /* ============================================================= notices */
  const N = { single: null, queue: null, more: null, banners: new Map(), seq: 0 };
  // the words of a notice that are the shell's own (not the caller's message) are registered with
  // T.ti, so a language switch rewrites them in place: the close button's name and tooltip, a
  // banner's level word and its default title
  function tiNotice(el, kind, defaultTitle) {
    if (!T.ti) return;
    const x = el.querySelector(':scope > .icon-btn');
    T.ti(x, 'shell.notice.close', null, { attr: 'aria-label' });
    T.ti(x, 'shell.notice.close', null, { attr: 'data-tip' });
    const sev = el.querySelector('.banner-sev');
    if (sev) T.ti(sev, 'shell.notice.banner.severity', { kind });
    if (defaultTitle) T.ti(el.querySelector('.banner-title > span'), 'shell.notice.banner.title', { kind });
  }
  /* actions: [{ label, primary, ghost, danger (quiet destructive), icon, left (pinned to the start), keep, run(button) }] */
  /* the buttons as nodes (T-P4-06: the notices are built without markup strings; labels
     are text nodes; T-P4-07: the only builder left, the markup-string one is gone). small: true
     btn-xs, 'none' no size class (drawer / sheet foot), else btn-sm. a.labelId (+ a.labelParams)
     instead of a.label: a catalog message, rewritten in place on a language switch (T.ti) */
  function actionNodes(actions, small) {
    const one = (a, i) => {
      const b = T.html`<button class="${['btn', small === 'none' ? '' : small ? 'btn-xs' : 'btn-sm', a.primary ? 'btn-primary' : a.ghost ? 'btn-ghost' : a.danger ? 'btn-danger-soft' : ''].join(' ')}" type="button" data-i="${i}">${a.icon ? T.html`<span class="bi">${T.icon(a.icon, 15)}</span>` : ''}<span class="lab">${a.labelId ? '' : a.label}</span></button>`;
      if (a.labelId && T.ti) T.ti(b.querySelector('.lab'), a.labelId, a.labelParams || null);
      return b;
    };
    const list = (actions || []).map((a, i) => [a, i]);
    const left = list.filter(([a]) => a.left), right = list.filter(([a]) => !a.left);
    return [...left.map(([a, i]) => one(a, i)), ...(left.length ? [T.h('span', { class: 'spacer' })] : []), ...right.map(([a, i]) => one(a, i))];
  }
  // a notice's close button (mapping_core N10; its name and tooltip are registered with T.ti by
  // tiNotice, so a language switch rewrites them in place)
  const closeBtn = size => T.html`<button class="icon-btn" type="button" aria-label="${T.t('shell.notice.close')}" data-tip="${T.t('shell.notice.close')}">${T.icon('close', size)}</button>`;
  function wireActions(el, actions, close) {
    T.$$('[data-i]', el).forEach(b => b.addEventListener('click', () => {
      const a = actions[+b.dataset.i]; if (!a) return;
      if (!a.keep) close();
      if (a.run) a.run(b);
    }));
  }
  /* toast(o, hooks) → handle: one pill in the top bar (success / info). How many show at once is
     the shell layer's queue (shellc/notify.js, T.notices.useQueue: 5, the rest merged into one
     「另有 N 条提醒」 row, T-P4-06 / G57); without a queue a new toast replaces the one shown (the
     prototype). The caller's title and message go in as text nodes (host:notify text is untrusted;
     every caller's text is treated alike); o.data marks them translate="no" (text that is data
     to the page, e.g. host:notify's, already in the shell's language). o.messageId / o.titleId
     (+ o.messageParams / o.titleParams) instead of o.message / o.title: a catalog message,
     rewritten in place on a language switch (T.ti; T-P4-06, 04a report item 6 ①). handle = { el, dismiss(instant), pause(reason),
     resume(reason) }; hooks.onClose() runs once, as soon as the toast starts to leave. The
     countdown holds while the pointer is over it and while any other reason holds it (the window
     hidden). */
  function toast(o, hooks) {
    hooks = hooks || {};
    let closed = false;
    const onClose = () => { if (closed) return; closed = true; if (hooks.onClose) hooks.onClose(); };
    const slot = T.$('#toast-slot');
    if (!slot) { const h = { el: null, dismiss: onClose, pause() {}, resume() {} }; onClose(); return h; }
    const timeout = o.sticky ? 0 : (o.timeout || (o.kind === 'success' ? 4000 : 5000));
    const C = 2 * Math.PI * 14;
    const el = T.h('div', { class: `toast toast-${o.kind}`, role: 'status' });
    el.append(T.html`<span class="n-ic">${T.icon(o.kind === 'success' ? 'check' : 'info', 14)}${timeout ? T.html`<svg class="n-ring" viewBox="0 0 30 30" aria-hidden="true"><circle cx="15" cy="15" r="14" stroke-dasharray="${C.toFixed(2)}"/></svg>` : ''}</span>`,
      T.h('span', { class: 'toast-msg' }), ...actionNodes(o.actions, true), closeBtn(14));
    noticeText(el.querySelector('.toast-msg'), o, 'b');
    tiNotice(el, o.kind, false);
    const handle = { el, dismiss: instant => { onClose(); closeToast(el, false, instant); }, pause() {}, resume() {} };
    if (!N.queue && N.single) closeToast(N.single.el, true);
    slot.append(el);
    if (N.more && N.more.isConnected) slot.append(N.more);           // the merge row stays last
    if (!N.queue) N.single = handle;
    // the pill grows out of the top bar's centre: its surface (fill, ring and shadow on ::before)
    // widens with a transform, edge and shadow on from the first frame; the content follows
    const TD = Math.max(260, SP.soft.duration - 80);
    anim(el, [{ transform: 'translateY(-4px)' }, { transform: 'none' }], { duration: TD, easing: SP.soft.easing });
    anim(el, [{ transform: 'scaleX(.6)', opacity: 0 }, { opacity: 1, offset: 0.25 }, { transform: 'none', opacity: 1 }], { duration: TD, easing: SP.soft.easing, pseudoElement: '::before' });
    Array.from(el.children).forEach(k => anim(k, [{ opacity: 0 }, { opacity: 1 }], { duration: 160, delay: 140, easing: EASE.out, fill: 'backwards' }));
    el.querySelector(':scope > .icon-btn').addEventListener('click', () => handle.dismiss());
    wireActions(el, o.actions, () => handle.dismiss());
    if (timeout) {
      const held = new Set();
      let remain = timeout, t0 = Date.now(), timer = setTimeout(() => handle.dismiss(), remain);
      const ring = el.querySelector('.n-ring circle');
      // under reduced motion the ring steps once a second instead of gliding
      const ra = ring.animate([{ strokeDashoffset: 0 }, { strokeDashoffset: C }],
        { duration: timeout, easing: reduced ? `steps(${Math.max(1, Math.round(timeout / 1000))}, end)` : 'linear', fill: 'forwards' });
      const hold = () => { clearTimeout(timer); timer = 0; remain -= Date.now() - t0; ra.pause(); };
      const run = () => { t0 = Date.now(); timer = setTimeout(() => handle.dismiss(), Math.max(600, remain)); ra.play(); };
      handle.pause = reason => { if (closed || held.has(reason)) return; held.add(reason); if (held.size === 1) hold(); };
      handle.resume = reason => { if (closed || !held.delete(reason)) return; if (!held.size) run(); };
      el.addEventListener('pointerenter', () => handle.pause('pointer'));
      el.addEventListener('pointerleave', () => handle.resume('pointer'));
      el._clear = () => clearTimeout(timer);
    }
    return handle;
  }
  /* a notice's caller text as text nodes: the title (in a <b>) and the message; a Node message
     (e.g. a T.tx fragment) goes in as it is */
  function noticeText(box, o, titleTag) {
    if (!box) return;
    // a catalog message alone is the box's own text (the same nodes as a plain message)
    if (o.messageId && T.ti && !(titleTag && hasText(o.title, o.titleId))) { T.ti(box, o.messageId, o.messageParams || null); return; }
    if (titleTag && hasText(o.title, o.titleId)) box.append(noticePart(titleTag, o.title, o.titleId, o.titleParams, o.data));
    if (o.message instanceof Node && !o.messageId) box.append(o.message);
    else if (hasText(o.message, o.messageId)) box.append(o.messageId || o.data ? noticePart('span', o.message, o.messageId, o.messageParams, o.data) : doc.createTextNode(String(o.message)));
  }
  const hasText = (v, id) => !!id || (v != null && v !== '');
  // one part of a notice's text: a catalog message (T.ti) or the caller's text as a text node,
  // translate="no" when it is data
  function noticePart(tag, text, id, params, data) {
    const el = doc.createElement(tag);
    if (id && T.ti) T.ti(el, id, params || null);
    else if (id) el.textContent = T.t(id, params);
    else el.textContent = String(text);
    if (data && !id) el.setAttribute('translate', 'no');
    return el;
  }
  function closeToast(el, replaced, instant) {
    if (!el || el._gone) return; el._gone = true;
    if (el._clear) el._clear();
    if (N.single && N.single.el === el) N.single = null;
    if (instant) { el.remove(); return; }
    const a = anim(el, [{ opacity: 1, transform: 'none' }, { opacity: 0, transform: replaced ? 'translateY(-6px) scale(.97)' : 'scale(.96)' }], { duration: replaced ? 120 : 160, easing: EASE.in, fill: 'forwards' });
    onDone(a, () => el.remove(), 400);
  }
  /* the queue's merge row: n notices waiting (0 removes it); its close drops them (onClear) */
  function mergeRow(n, onClear) {
    const slot = T.$('#toast-slot'); if (!slot) return;
    let el = N.more;
    if (!n) { if (el) { N.more = null; closeToast(el); } return; }
    if (!el || el._gone) {
      el = N.more = T.h('div', { class: 'toast toast-info toast-more', role: 'status' });
      el.append(T.html`<span class="n-ic">${T.icon('info', 14)}</span>`, T.h('span', { class: 'toast-msg' }), closeBtn(14));
      tiNotice(el, 'info', false);
      el.querySelector(':scope > .icon-btn').addEventListener('click', () => { if (el._onClear) el._onClear(); });
      anim(el, [{ opacity: 0 }, { opacity: 1 }], { duration: 160, easing: EASE.out });
    }
    slot.append(el);
    el._onClear = onClear;
    T.ti(el.querySelector('.toast-msg'), 'shell.notice.more', { count: n });
  }
  /* banner(o): warning / error (or info with o.banner), replaced in place by id. Title and
     message are text nodes (as for toasts). o.onDismiss() runs when the user closes it with its
     close button (not when the code takes it down: T.dismiss / a timeout / an action).
     F-05 (11-04, user: a bar opening above the page and pushing it down is not elegant): a banner
     is a page of the floating card #notices under the top bar's right end, which covers the page
     instead of moving it; the card shows one banner at a time (newest first; dots page between
     them), and the capsule #notice-pill in the top bar stands for the banners while the card is
     tucked away (the notice island, below). */
  function banner(o) {
    const host = T.$('#banners'); if (!host) return { el: null, id: o.id || null, dismiss() {} };
    const id = o.id || ('n' + (++N.seq));
    const old = N.banners.get(id);
    const el = T.h('div', { class: `banner banner-${o.kind}`, role: o.kind === 'error' ? 'alert' : 'status', 'data-id': id });
    // (F-01: an info banner — o.banner, e.g. a new version — has no level word: it is no warning)
    const leveled = o.kind === 'error' || o.kind === 'warning';
    el.append(...[T.html`<span class="n-ic">${T.icon(o.kind === 'error' ? 'error' : leveled ? 'warning' : 'info', 14)}</span>`,
      T.h('div', { class: 'banner-body' }, T.h('div', { class: 'banner-title' }, T.h('span'), leveled ? T.h('span', { class: 'banner-sev' }) : null),
        hasText(o.message, o.messageId) ? T.h('div', { class: 'banner-msg' }) : null),
      o.actions && o.actions.length ? T.h('div', { class: 'banner-actions' }, actionNodes(o.actions)) : null,
      closeBtn()].filter(Boolean));
    const ttl = el.querySelector('.banner-title > span');
    if (o.titleId && T.ti) T.ti(ttl, o.titleId, o.titleParams || null);
    else ttl.textContent = o.title ? String(o.title) : T.t('shell.notice.banner.title', { kind: o.kind });
    if (o.data && o.title && !o.titleId) ttl.setAttribute('translate', 'no');
    if (leveled) el.querySelector('.banner-sev').textContent = T.t('shell.notice.banner.severity', { kind: o.kind });
    noticeText(el.querySelector('.banner-msg'), Object.assign({}, o, { title: null, titleId: null }));
    tiNotice(el, o.kind, !o.title && !o.titleId);
    const handle = { el, id, kind: o.kind, dismiss: () => closeBanner(id) };
    el.querySelector(':scope > .icon-btn').addEventListener('click', () => {
      handle.dismiss();
      if (typeof o.onDismiss === 'function') { try { o.onDismiss(); } catch (e) { setTimeout(() => { throw e; }); } }
    });
    wireActions(el, o.actions, handle.dismiss);
    if (old) {          // same id: replace in place (the island stays as it is, so does the page shown)
      const on = old.el.classList.contains('is-on');
      old.el.replaceWith(el); clearTimeout(old.timer);
      N.banners.set(id, handle);
      if (on) { el.classList.add('is-on'); anim(el, [{ opacity: 0.4 }, { opacity: 1 }], { duration: 200, easing: EASE.out }); }
      syncPill(); syncDots();
    } else {
      N.banners.set(id, handle);
      // the capsule first (the card is placed under it), then the card opens, so the live region
      // is shown when the banner lands in it; the new banner is the first page and the one shown
      syncPill();
      const was = ISL.open;
      islOpen(true);
      host.prepend(el);
      showPage(el, -1, !was);
      const ic = el.querySelector('.n-ic');
      anim(ic, [{ transform: 'scale(.4)', opacity: 0 }, { transform: 'none', opacity: 1 }], { duration: SP.press.duration, delay: 110, easing: SP.press.easing, fill: 'backwards' });
    }
    if (o.timeout) handle.timer = setTimeout(handle.dismiss, o.timeout);
    return handle;
  }
  function closeBanner(id) {
    const hd = N.banners.get(id); if (!hd) return;
    const el = hd.el, list = pages(), i = list.indexOf(el);
    N.banners.delete(id); clearTimeout(hd.timer);
    syncPill();
    if (!N.banners.size) { islClose(false, () => { el.remove(); syncDots(); }); return; }   // the last one: the card goes with it
    if (el.classList.contains('is-on')) {
      // the next (older) banner takes its place, else the one before; focus on the page that goes
      // moves to the same place on the new one (its close button), never to <body>
      const next = list[i + 1] || list[i - 1], focused = el.contains(doc.activeElement);
      showPage(next, list[i + 1] ? 1 : -1, !ISL.open);
      if (focused) { const b = next.querySelector(':scope > .icon-btn'); if (b) b.focus({ preventScroll: true }); }
    }
    if (el.classList.contains('is-leaving')) onDone(el._out, () => el.remove(), 400);   // (it fades where it was first)
    else el.remove();
    syncDots();
    peekTimer();        // the error that went may have been what held a peek open
  }

  /* the notice island (F-05). The card opens by itself when a new banner arrives (a peek: focus
     stays where it is) and on a click of the capsule (from the keyboard, focus goes to the page
     shown). It tucks away on a press anywhere outside the card and the capsule, on Esc inside
     them, on the capsule again, with its last banner, and — a peek without an error in it — after
     PEEK_MS without the pointer or focus on the card (LINGER_MS once they have left; the clock
     holds while the window is hidden). An error stays until the user turns to something else.
     While a modal overlay is open the card steps out of sight (#app.has-modal, shell.css). */
  const PEEK_MS = 6000, LINGER_MS = 3000;
  const ISL = { open: false, peek: false, ms: PEEK_MS, timer: 0, held: new Set(), a: null };
  // under the capsule, the card's right edge on the capsule's, never past the main column's sides;
  // it grows out of the capsule (transform-origin: the capsule's centre)
  function islPlace() {
    const card = T.$('#notices'), pill = T.$('#notice-pill'), main = card && card.offsetParent;
    if (!card || !main) return;
    const m = main.getBoundingClientRect(), w = card.offsetWidth;
    // (the capsule's layout box: it may still be scaling in)
    const pp = pill && !pill.hidden && pill.offsetParent, ppr = pp && pp.getBoundingClientRect();
    const p = pp ? { left: ppr.left + pill.offsetLeft, right: ppr.left + pill.offsetLeft + pill.offsetWidth, top: ppr.top + pill.offsetTop, width: pill.offsetWidth, height: pill.offsetHeight } : null;
    const right = Math.max(16, Math.min(p ? m.right - p.right : 16, m.width - w - 16));
    card.style.right = right + 'px';
    if (p) card.style.transformOrigin = `${Math.round(p.left + p.width / 2 - (m.right - right - w))}px ${Math.round(p.top + p.height / 2 - m.top - card.offsetTop)}px`;
  }
  // the banners' elements in the card, newest first (one still fading out after it went is not)
  const pages = () => { const host = T.$('#banners'); return host ? Array.from(host.children).filter(c => { const h = N.banners.get(c.dataset.id); return !!h && h.el === c; }) : []; };
  /* one page of the card is shown (.is-on); the others wait out of the flow, hidden (shell.css). A
     switch slides the words across inside the card (dir 1: the next, older banner comes in from
     the right; -1: from the left) while the one shown before fades where it was, and the card's
     surface follows the new height (scaleY on ::before, as the toast's scaleX). instant: no motion
     (the card is opening or out of sight) */
  function showPage(to, dir, instant) {
    const card = T.$('#notices'), host = T.$('#banners'); if (!card || !host || !to) return;
    const from = host.querySelector(':scope > .banner.is-on');
    if (to._out) { to._out.cancel(); to._out = null; to.classList.remove('is-leaving'); }
    if (from === to) { syncDots(); return; }
    const h0 = card.offsetHeight;
    if (from) from.classList.remove('is-on');
    to.classList.add('is-on');
    syncDots();
    if (instant || !ISL.open) return;
    const h1 = card.offsetHeight;
    if (h0 && h1 && Math.abs(h0 - h1) > 1) anim(card, [{ transform: `scaleY(${(h0 / h1).toFixed(4)})` }, { transform: 'none' }], { duration: SP.soft.duration, easing: SP.soft.easing, pseudoElement: '::before' });
    if (from) {
      from.classList.add('is-leaving');
      const a = from._out = anim(from, [{ opacity: 1, transform: 'none' }, { opacity: 0, transform: `translateX(${-14 * dir}px)` }], { duration: 140, easing: EASE.in, fill: 'forwards' });
      onDone(a, () => { if (from._out !== a) return; from._out = null; from.classList.remove('is-leaving'); if (a) a.cancel(); }, 400);
    }
    anim(to, [{ opacity: 0, transform: `translateX(${14 * dir}px)` }, { opacity: 1, transform: 'none' }], { duration: 260, delay: 70, easing: EASE.out, fill: 'backwards' });
  }
  /* the dots: one per banner, newest first, from two banners on, in the banner's hue; a dot's name
     and tooltip are its banner's title (the card's own words; data stays translate="no"). A click
     shows that banner; ← → move along them while a dot has the focus */
  function syncDots() {
    const dots = T.$('#notice-dots'); if (!dots) return;
    const list = pages(), focused = dots.contains(doc.activeElement);
    if (tipFor && dots.contains(tipFor)) hideTip();     // (a tooltip of a dot about to be replaced goes with it)
    dots.hidden = list.length < 2;
    // (the tooltip above the dot, over the top bar: below it would cover the card's title)
    dots.replaceChildren(...(list.length < 2 ? [] : list.map(el => {
      const h = N.banners.get(el.dataset.id), ttl = el.querySelector('.banner-title > span'), name = ttl ? ttl.textContent : '';
      return T.h('button', { class: 'ntc-dot', type: 'button', 'data-id': el.dataset.id, 'data-kind': h.kind, 'aria-label': name, 'data-tip': name,
        'aria-current': el.classList.contains('is-on') ? 'true' : null, translate: ttl && ttl.closest('[translate="no"]') ? 'no' : null });
    })));
    if (focused) { const b = dots.querySelector('[aria-current="true"]'); if (b) b.focus({ preventScroll: true }); }
  }
  function stepPage(d) {
    const list = pages(), i = list.findIndex(el => el.classList.contains('is-on')), to = list[i + d];
    if (to) showPage(to, d);
  }
  function peekTimer() {
    clearTimeout(ISL.timer); ISL.timer = 0;
    if (!ISL.open || !ISL.peek || ISL.held.size) return;
    for (const h of N.banners.values()) if (h.kind === 'error') return;
    ISL.timer = setTimeout(() => islClose(), ISL.ms);
  }
  function islOpen(peek) {
    const card = T.$('#notices'), pill = T.$('#notice-pill'); if (!card) return;
    if (!ISL.open) {
      ISL.open = true; ISL.peek = !!peek;
      if (ISL.a) { ISL.a.cancel(); ISL.a = null; }
      card.classList.add('is-open');
      // (the capsule's tooltip only says what the open card shows: it rests while the card is open)
      if (pill) { pill.setAttribute('aria-expanded', 'true'); pill.removeAttribute('data-tip'); if (tipFor === pill) hideTip(); }
      islPlace();
      anim(card, [{ opacity: 0, transform: 'translateY(-8px) scale(.94)' }, { opacity: 1, transform: 'none' }], { duration: SP.soft.duration, easing: SP.soft.easing });
    } else if (!peek) ISL.peek = false;          // the user opened it: it stays
    ISL.ms = PEEK_MS;
    peekTimer();
  }
  // after() runs once the card is out of sight (at once when it already was)
  function islClose(instant, after) {
    const card = T.$('#notices'), pill = T.$('#notice-pill');
    const done = () => { if (after) after(); };
    if (!card || !ISL.open) { done(); return; }
    ISL.open = false; ISL.peek = false; clearTimeout(ISL.timer); ISL.timer = 0;
    if (pill) { pill.setAttribute('aria-expanded', 'false'); if (pill._label) pill.setAttribute('data-tip', pill._label); }
    // focus in the card goes back to the capsule, or to the page when the capsule goes too
    if (card.contains(doc.activeElement)) {
      const to = pill && N.banners.size ? pill : T.$('#pages > .page:not(.is-leaving)');
      if (to && to !== pill && !to.hasAttribute('tabindex')) to.tabIndex = -1;   // (as the router lands on a page)
      if (to) to.focus({ preventScroll: true });
    }
    const end = () => { if (!ISL.open) card.classList.remove('is-open'); if (ISL.a) { ISL.a.cancel(); ISL.a = null; } done(); };
    if (instant) { end(); return; }
    const a = ISL.a = anim(card, [{ opacity: 1, transform: 'none' }, { opacity: 0, transform: 'translateY(-6px) scale(.96)' }], { duration: 160, easing: EASE.in, fill: 'forwards' });
    onDone(a, end, 400);
  }
  const hold = r => { ISL.held.add(r); peekTimer(); };
  const release = r => { if (!ISL.held.delete(r)) return; ISL.ms = r === 'hidden' ? PEEK_MS : LINGER_MS; peekTimer(); };
  /* the capsule: the most serious kind on screen (error, then warning, then info) by its shape and
     hue, and how many from two on; its name and tooltip are the newest banner's title, plus how
     many more (shell.notice.more). instant: it goes without its exit */
  function syncPill(instant) {
    const pill = T.$('#notice-pill'); if (!pill) return;
    const list = Array.from(N.banners.values()), n = list.length;
    if (!n) {
      if (pill.hidden) return;
      if (instant) { pill._going = false; pill.getAnimations().forEach(x => x.cancel()); pill.hidden = true; pill._key = ''; return; }
      if (pill._going) return;
      pill._going = true;
      const a = anim(pill, [{ opacity: 1, transform: 'none' }, { opacity: 0, transform: 'scale(.8)' }], { duration: 140, easing: EASE.in, fill: 'forwards' });
      onDone(a, () => { if (pill._going) { pill._going = false; pill.hidden = true; pill._key = ''; } if (a) a.cancel(); }, 400);
      return;
    }
    const kind = list.some(h => h.kind === 'error') ? 'error' : list.some(h => h.kind === 'warning') ? 'warning' : 'info';
    if (pill._key !== kind + n) {
      pill._key = kind + n;
      pill.dataset.kind = kind;
      pill.replaceChildren(T.html`<span class="n-ic">${T.icon(kind, 16)}</span>`);
      if (n > 1) pill.append(T.h('span', { class: 'np-n' }, T.fmt.num(n)));
    }
    // (the Map keeps the order banners first came in: a banner replaced in place keeps its place)
    const ttl = list[n - 1].el.querySelector('.banner-title > span');
    const label = (ttl ? ttl.textContent : '') + (n > 1 ? ' · ' + T.t('shell.notice.more', { count: n - 1 }) : '');
    pill._label = label;
    pill.setAttribute('aria-label', label);
    // a title that is data (host:notify text, translate="no") stays data in the capsule's name
    if (ttl && ttl.closest('[translate="no"]')) pill.setAttribute('translate', 'no'); else pill.removeAttribute('translate');
    if (ISL.open) pill.removeAttribute('data-tip'); else pill.setAttribute('data-tip', label);
    if (pill.hidden || pill._going) {
      const back = pill._going;
      pill._going = false; pill.getAnimations().forEach(x => x.cancel()); pill.hidden = false;
      if (!back) anim(pill, [{ opacity: 0, transform: 'scale(.6)' }, { opacity: 1, transform: 'none' }], { duration: SP.press.duration, easing: SP.press.easing });
    }
  }
  (function wireIsland() {
    const card = T.$('#notices'), dots = T.$('#notice-dots'), pill = T.$('#notice-pill'); if (!card) return;
    card.addEventListener('pointerenter', () => hold('pointer'));
    card.addEventListener('pointerleave', () => release('pointer'));
    card.addEventListener('focusin', () => hold('focus'));
    card.addEventListener('focusout', e => { if (!card.contains(e.relatedTarget)) release('focus'); });
    doc.addEventListener('visibilitychange', () => (doc.hidden ? hold('hidden') : release('hidden')));
    // a press anywhere else tucks the card away (the press itself goes on to what it was for)
    doc.addEventListener('pointerdown', e => {
      if (ISL.open && !card.contains(e.target) && !(pill && pill.contains(e.target))) islClose();
    }, true);
    // (capture, and registered before the overlays' trapKeys: Esc in the card tucks the card,
    // not a side panel that happens to be open)
    doc.addEventListener('keydown', e => {
      if (e.key !== 'Escape' || !ISL.open || T.overlayOpen()) return;
      const a = doc.activeElement;
      if (!card.contains(a) && a !== pill) return;
      e.preventDefault(); e.stopImmediatePropagation();
      islClose();
      if (pill && !pill.hidden) pill.focus({ preventScroll: true });
    }, true);
    if (dots) {
      dots.addEventListener('click', e => {
        const b = e.target.closest && e.target.closest('.ntc-dot'); if (!b) return;
        const list = pages(), to = list.find(el => el.dataset.id === b.dataset.id), from = list.findIndex(el => el.classList.contains('is-on'));
        if (to) showPage(to, list.indexOf(to) > from ? 1 : -1);
      });
      dots.addEventListener('keydown', e => {
        if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
        e.preventDefault(); stepPage(e.key === 'ArrowRight' ? 1 : -1);
      });
    }
    if (pill) pill.addEventListener('click', () => {
      if (ISL.open) { islClose(); return; }
      islOpen(false);
      if (lastInput === 'key') { const b = card.querySelector('.banner.is-on button'); if (b) b.focus({ preventScroll: true }); }
    });
    addEventListener('resize', T.util.raf(() => { if (ISL.open) islPlace(); }));
    // the capsule's and the dots' names are banners' titles and a catalog message: worded again with them
    T.on('i18n:change', () => requestAnimationFrame(() => { syncPill(); syncDots(); }));
  })();
  /* T.notify(o): success / info → a toast (through the shell's queue when it is installed: the
     result is then the queue's ticket { dismiss(), handle, shown }); warning / error → a banner;
     info with o.banner → a banner without a level word (F-01: a new version is no warning) */
  T.notify = function (o) {
    o = Object.assign({ kind: 'info' }, o);
    if ((o.kind === 'success' || o.kind === 'info') && !(o.kind === 'info' && o.banner)) return N.queue ? N.queue.push(o) : toast(o);
    return banner(o);
  };
  T.dismiss = id => { if (N.banners.has(id)) closeBanner(id); };
  const newestToast = () => (N.queue ? N.queue.newest : N.single);
  T.dismissToast = () => { const h = newestToast(); if (h) h.dismiss(); };
  T.notices = {
    get toast() { return newestToast(); },
    banners: N.banners,
    // the shell layer's view of the toast slot and its queue (shellc/index.js)
    port: { toast, merge: mergeRow },
    useQueue(q) { N.queue = q || null; },
    clearAll(instant) {
      if (N.queue) N.queue.clear(instant);
      else if (N.single) N.single.dismiss(instant);
      mergeRow(0);
      Array.from(N.banners.keys()).forEach(id => { if (instant) { const h = N.banners.get(id); clearTimeout(h.timer); h.el.remove(); N.banners.delete(id); } else closeBanner(id); });
      if (instant) { islClose(true); syncPill(true); syncDots(); }
    },
  };

  /* ============================================================ overlays */
  const stack = [];
  let lastInput = 'pointer';                       // how the user last interacted (for focus decisions)
  doc.addEventListener('keydown', () => { lastInput = 'key'; }, true);
  doc.addEventListener('pointerdown', () => { lastInput = 'pointer'; }, true);
  function layer() { return T.$('#layer') || doc.body; }
  /* #app.has-modal while a modal overlay is open: the save bar steps away under it (components.css) */
  function syncModal() { const app = T.$('#app'); if (app) app.classList.toggle('has-modal', stack.some(x => x.modal)); }
  function trapKeys(e) {
    if (e.key === 'Escape' && FLOAT.cur) { e.preventDefault(); e.stopPropagation(); FLOAT.cur.close(null); return; }   // menus first
    const top = stack[stack.length - 1]; if (!top) return;
    if (e.key === 'Escape') {
      const t = e.target;
      if (t && t.closest && t.closest('.search') && t.value) return;                    // a search with text clears itself first
      if (!top.modal && t && t.matches && t.matches('input, textarea, select, [contenteditable="true"]') && !top.panel.contains(t)) return;
      e.preventDefault(); e.stopPropagation(); top.cancel(); return;
    }
    if (e.key === 'Tab' && top.modal) {
      const f = T.$$('button:not([disabled]), [href], input:not([disabled]), select, textarea, [tabindex]:not([tabindex="-1"])', top.panel).filter(x => x.offsetParent !== null);
      if (!f.length) return;
      const i = f.indexOf(doc.activeElement);
      if (e.shiftKey && (i <= 0)) { e.preventDefault(); f[f.length - 1].focus(); }
      else if (!e.shiftKey && i === f.length - 1) { e.preventDefault(); f[0].focus(); }
    }
  }
  doc.addEventListener('keydown', trapKeys, true);
  /* o: { modal (default true), inT, outT, morphFrom: Element, onClose(val) } */
  function openOverlay(panel, o) {
    const modal = o.modal !== false;
    const root = T.h('div', { class: 'ovl' + (modal ? '' : ' is-modeless') });
    const scrim = modal ? T.h('div', { class: 'ovl-scrim' }) : null;
    if (scrim) root.append(scrim);
    root.append(panel); layer().append(root);
    const ret = doc.activeElement;
    // the user's own ways out (Esc, ×, the scrim) ask o.beforeClose first: false | Promise<boolean>
    // (a half-finished edit asks 放弃未保存的更改？); programmatic closes and navigation don't
    let asking = false;
    const entry = { root, panel, modal, cancel: () => {
      if (closed || asking) return;
      let ok = true;
      try { ok = o.beforeClose ? o.beforeClose() : true; } catch (err) { setTimeout(() => { throw err; }); }
      if (ok === false) return;
      if (ok && typeof ok.then === 'function') { asking = true; ok.then(yes => { asking = false; if (yes) close(false); }); return; }
      close(false);
    } };
    let closed = false;
    function close(val) {
      if (closed) return; closed = true;
      const i = stack.indexOf(entry); if (i >= 0) stack.splice(i, 1);
      syncModal();
      const src = o.morphFrom && o.morphFrom.isConnected && isShown(o.morphFrom) ? o.morphFrom : null;
      let a = src ? morph(src, panel, { reverse: true, duration: 240 }) : null;
      if (!a) a = anim(panel, [{ opacity: 1, transform: 'none' }, { opacity: 0, transform: o.outT || 'translateY(4px) scale(.99)' }], { duration: 130, easing: EASE.in, fill: 'forwards' });
      if (scrim) anim(scrim, [{ opacity: 1 }, { opacity: 0 }], { duration: src ? 220 : 130, fill: 'forwards' });
      onDone(a, () => root.remove(), src ? 480 : 320);
      if ((modal || panel.contains(doc.activeElement)) && ret && ret.focus && ret.isConnected) ret.focus({ preventScroll: true });
      if (o.onClose) o.onClose(val);
    }
    if (scrim) scrim.addEventListener('click', () => entry.cancel());
    stack.push(entry);
    syncModal();
    if (scrim) anim(scrim, [{ opacity: 0 }, { opacity: 1 }], { duration: 160, easing: EASE.out });
    const m = o.morphFrom ? morph(o.morphFrom, panel, {}) : null;
    if (!m) anim(panel, [{ opacity: 0, transform: o.inT || 'translateY(8px) scale(.98)' }, { opacity: 1, transform: 'none' }], { duration: Math.max(240, SP.soft.duration - 60), easing: SP.soft.easing });
    entry.close = close;
    return entry;
  }
  function closeModeless() { stack.filter(x => !x.modal).forEach(x => x.close(false)); }
  /* navigation closes modal overlays too (a link inside a drawer must not leave it open over the new page) */
  function closeModals() { stack.slice().reverse().forEach(x => { if (x.modal) x.close(false); }); }
  T.confirm = function (o) {
    o = o || {};
    return new Promise(resolve => {
      const panel = T.h('div', { class: `dlg${o.danger ? ' dlg-danger' : ''}`, role: 'alertdialog', 'aria-modal': 'true' });
      // (T-P4-07) built as nodes: title, message and labels are text
      panel.append(T.html`<div class="dlg-title">${o.danger ? T.icon('warning', 20) : ''}<span>${o.title || T.t('core.confirm.title')}</span></div>${o.message ? T.html`<p class="dlg-msg">${o.message}</p>` : ''}<div class="dlg-actions"><button class="btn" type="button" data-a="no">${o.cancelLabel || T.t('common.action.cancel')}</button><button class="btn ${o.danger ? 'btn-danger' : 'btn-primary'}" type="button" data-a="yes">${o.confirmLabel || T.t('common.action.ok')}</button></div>`);
      const e = openOverlay(panel, { onClose: v => resolve(!!v) });
      panel.querySelector('[data-a="no"]').addEventListener('click', () => e.close(false));
      panel.querySelector('[data-a="yes"]').addEventListener('click', () => e.close(true));
      panel.addEventListener('keydown', ev => { if (ev.key === 'Enter' && ev.target.tagName !== 'BUTTON') { ev.preventDefault(); e.close(true); } });
      setTimeout(() => panel.querySelector(o.danger ? '[data-a="no"]' : '[data-a="yes"]').focus(), 20);
    });
  };
  /* drawer / sheet
     o: { title, subtitle, width, height (sheet: fixed height, body scrolls), content, actions,
          head (Element/html for the head slot), from (Element: container morph from it),
          modal (drawer only: false = side panel without scrim; the page stays usable),
          focus (modeless: move focus in), class, onClose(val),
          beforeClose() → false | Promise<boolean> (asked by Esc, × and the scrim; not by api.close()) }
     api: { el, head, body, foot, close(), setTitle(t), setSubtitle(t), setContent(c, {dir, instant}),
            setActions(actions), action(i) → button, closed } */
  function panelOverlay(kind, o) {
    o = o || {};
    const modal = kind !== 'drawer' || o.modal !== false;
    const panel = T.h('div', { class: kind + (modal ? '' : ' is-modeless') + (o.class ? ' ' + o.class : ''), role: modal ? 'dialog' : 'complementary', 'aria-label': o.title || '' });
    if (modal) panel.setAttribute('aria-modal', 'true');
    if (o.width) panel.style.width = typeof o.width === 'number' ? o.width + 'px' : o.width;
    if (o.height) { panel.classList.add('is-fixed-h'); panel.style.setProperty('--sheet-h', typeof o.height === 'number' ? o.height + 'px' : o.height); }
    panel.append(T.html`<div class="ovl-head"><div class="ovl-hd col"><div class="ovl-title"></div><div class="ovl-sub"></div></div><div class="ovl-extra"></div><button class="icon-btn ovl-x" type="button" data-tip-kbd="Esc">${T.icon('close')}</button></div><div class="ovl-body"></div><div class="ovl-foot" hidden></div>`);
    const q = s => panel.querySelector(s);
    T.ti(q('.ovl-x'), 'common.action.close', null, { attr: 'aria-label' });
    T.ti(q('.ovl-x'), 'common.action.close', null, { attr: 'data-tip' });
    const title = q('.ovl-title'), sub = q('.ovl-sub'), extra = q('.ovl-extra'), body = q('.ovl-body'), foot = q('.ovl-foot');
    title.textContent = o.title || ''; sub.textContent = o.subtitle || '';
    let e = null;
    const api = {
      el: panel, head: extra, body, foot,
      get closed() { return !!(e && stack.indexOf(e) < 0); },
      close: () => { if (e) e.close(true); },
      setTitle(t) { swapText(title, t || ''); panel.setAttribute('aria-label', t || ''); },
      setSubtitle(t) { if (!t) { swapReset(sub); sub.textContent = ''; } else swapText(sub, t); },
      setContent, setActions,
      action: i => foot.querySelector(`[data-i="${i}"]`),
    };
    // content / head: a Node or fn(box, api); a string is text (T-P4-07: never parsed as HTML)
    const fill = (box, c) => { if (typeof c === 'function') c(box, api); else if (c instanceof Node) box.append(c); else if (c != null) box.append(String(c)); };
    function setContent(c, so) {
      so = so || {};
      const nu = T.h('div', { class: 'ovl-page' });
      T.$$(':scope > .ovl-page.is-out', body).forEach(x => x.remove());
      const olds = Array.from(body.children);
      fill(nu, c);
      if (!olds.length || so.instant || reduced || !isShown(body)) { olds.forEach(x => x.remove()); body.append(nu); body.scrollTop = 0; return nu; }
      const st = body.scrollTop;
      olds.forEach(x => {
        x.classList.add('ovl-page', 'is-out'); x.style.top = (16 - st) + 'px'; x.setAttribute('aria-hidden', 'true');
        const a = x.animate([{ opacity: 1 }, { opacity: 0 }], { duration: 110, easing: 'linear', fill: 'forwards' }); onDone(a, () => x.remove(), 300);
      });
      body.scrollTop = 0; body.append(nu);
      nu.animate([{ opacity: 0, transform: `translateY(${(so.dir || 0) * 8}px)` }, { opacity: 1, transform: 'none' }], { duration: 220, delay: 40, easing: EASE.out, fill: 'backwards' });
      return nu;
    }
    function setActions(actions) {
      foot.hidden = !(actions && actions.length);
      foot.replaceChildren(...(actions && actions.length ? actionNodes(actions, 'none') : []));
      if (actions) wireActions(foot, actions, api.close);
    }
    if (o.head) fill(extra, o.head);
    const first = T.h('div', { class: 'ovl-page' }); body.append(first); fill(first, o.content);
    setActions(o.actions);
    const isDrawer = kind === 'drawer';
    const dock = isDrawer && !modal && o.dock ? dockPanel(panel, o) : null;
    if (typeof o.width === 'function' && !dock) panel.style.width = o.width() + 'px';
    e = openOverlay(panel, { modal, beforeClose: o.beforeClose, onClose: v => { if (dock) dock.off(); if (o.onClose) o.onClose(v); }, morphFrom: o.from, inT: isDrawer ? 'translateX(28px)' : undefined, outT: isDrawer ? 'translateX(20px)' : undefined });
    if (dock) { dock.root = e.root; dock.place(); }
    api.place = () => { if (dock) dock.place(); };
    q('.ovl-x').addEventListener('click', () => e.cancel());
    // focus: a text field is focused at once; otherwise the panel itself after a pointer open (no stray ring),
    // the first control after a keyboard open
    panel.tabIndex = -1;
    if (modal || o.focus) setTimeout(() => {
      if (panel.contains(doc.activeElement)) return;
      const f = body.querySelector('input:not([type=checkbox]):not([type=radio]), textarea') || (lastInput === 'key' && (body.querySelector('button, select, [tabindex="0"]') || q('.ovl-x')));
      (f || panel).focus({ preventScroll: true });
    }, 30);
    return api;
  }
  /* a side panel docked inside an element's box (T.drawer({ modal: false, dock: el })): its top, bottom
     and right edge are the element's, taken from layout offsets (transforms ignored), so it docks where
     the page is going; while the page is still travelling there (its entrance) the panel rides the
     move on the soft spring. The layer is clipped to the box, so the panel slides in and out from
     under the element's right edge. It re-places on window resize and when the element changes
     size (banners float since F-05: they no longer move it). width may be a function (read on
     every placement). */
  function dockPanel(panel, o) {
    const target = () => (typeof o.dock === 'function' ? o.dock() : o.dock);
    const host = T.$('#host');
    panel.classList.add('is-docked');
    function layoutBox(el) {
      let x = 0, y = 0, n = el;
      if (host && host.contains(el)) for (; n && n !== host; n = n.offsetParent) { x += n.offsetLeft; y += n.offsetTop; }
      if (!host || n !== host) { const r = el.getBoundingClientRect(); return { left: r.left, top: r.top, right: r.right, bottom: r.bottom }; }
      // the host's own layout box: its rect is read from its offset in its (untransformed) offset
      // parent, not from getBoundingClientRect (transforms ignored)
      const par = host.offsetParent, pr = par ? par.getBoundingClientRect() : null;
      const hr = pr ? { left: pr.left + par.clientLeft + host.offsetLeft, top: pr.top + par.clientTop + host.offsetTop } : host.getBoundingClientRect();
      const left = hr.left + host.clientLeft - host.scrollLeft + x, top = hr.top + host.clientTop - host.scrollTop + y;
      return { left, top, right: left + el.offsetWidth, bottom: top + el.offsetHeight };
    }
    let settleT = 0;
    const d = {
      root: null,
      place() {
        const el = target(); if (!el || !el.isConnected || !panel.isConnected) return;
        const b = layoutBox(el), vis = el.getBoundingClientRect();
        const right = Math.max(0, innerWidth - b.right), bottom = Math.max(0, innerHeight - b.bottom);
        const w = typeof o.width === 'function' ? o.width() : o.width;
        Object.assign(panel.style, { top: b.top + 'px', right: right + 'px', bottom: bottom + 'px' });
        if (w) panel.style.width = (typeof w === 'number' ? w + 'px' : w);
        const clip = (t, bt) => `inset(${t}px ${right}px ${bt}px ${b.left}px)`;
        if (d.root) d.root.style.clipPath = clip(b.top, bottom);
        [panel._ride, d.root && d.root._ride].forEach(a => { if (a) a.cancel(); });
        const dy = Math.round(vis.top - b.top);
        if (Math.abs(dy) > 1 && !reduced && d.root) {
          panel._ride = panel.animate([{ translate: `0 ${dy}px` }, { translate: '0 0' }], { duration: SP.soft.duration, easing: SP.soft.easing });
          d.root._ride = d.root.animate([{ clipPath: clip(b.top + dy, Math.max(0, bottom - dy)) }, { clipPath: clip(b.top, bottom) }], { duration: SP.soft.duration, easing: SP.soft.easing });
          clearTimeout(settleT); settleT = setTimeout(() => d.place(), SP.soft.duration + 60);   // and checks where it came to rest
        }
      },
      off() { clearTimeout(settleT); removeEventListener('resize', onResize); if (ro) ro.disconnect(); },
    };
    const onResize = T.util.raf(() => d.place());
    addEventListener('resize', onResize);
    const ro = window.ResizeObserver ? new ResizeObserver(onResize) : null;
    if (ro) { const el = target(); if (el) ro.observe(el); }
    return d;
  }
  T.drawer = o => panelOverlay('drawer', o);
  T.drawer.open = T.drawer;
  T.sheet = o => panelOverlay('sheet', o);
  T.sheet.open = T.sheet;
  /** true while a MODAL overlay is open (side panels opened with modal:false do not count) */
  T.overlayOpen = () => stack.some(x => x.modal);
  // open(panel, o): a page-built modal panel on the same stack (scrim, Esc, Tab trap, focus return,
  // closed by navigation); o as openOverlay above. The letter (letter/letter.js) is its one user.
  T.overlays = { get stack() { return stack.slice(); }, closeModeless, closeFloats, open: (panel, o) => openOverlay(panel, o || {}) };

  /* ===================================================== command palette */
  const CMD = { list: [], el: null, open: false, sel: 0, items: [], ret: null };
  const val = (v, ...a) => (typeof v === 'function' ? v(...a) : v);
  T.cmd = {
    register(c) { (Array.isArray(c) ? c : [c]).forEach(x => { CMD.list = CMD.list.filter(y => !x.id || y.id !== x.id); CMD.list.push(x); }); },
    open: openCmd, close: closeCmd,
    toggle() { if (CMD.open) closeCmd(); else openCmd(); },
    get isOpen() { return CMD.open; },
  };
  function buildCmd() {
    const el = T.h('div', { class: 'cmdk', hidden: true });
    el.append(T.html`<div class="cmdk-scrim"></div><div class="cmdk-panel" role="dialog" aria-modal="true">
      <div class="cmdk-in">${T.icon('search', 17)}<input autocomplete="off" spellcheck="false" role="combobox" aria-expanded="true" aria-controls="cmdk-list"><kbd translate="no">Esc</kbd></div>
      <div class="cmdk-list" id="cmdk-list" role="listbox"></div>
      <div class="cmdk-foot"><span data-k="select"></span><span data-k="run"></span><span class="end" data-k="pages"></span></div></div>`);
    layer().append(el);
    CMD.el = el; CMD.q = el.querySelector('input'); CMD.listEl = el.querySelector('.cmdk-list'); CMD.panel = el.querySelector('.cmdk-panel'); CMD.scrim = el.querySelector('.cmdk-scrim');
    // the static words are registered (T.ti) and follow a language switch in place; each footer
    // hint is one message with its keycaps inside (<kbd> tags), so the order is the translator's
    T.ti(CMD.panel, 'shell.palette.aria', null, { attr: 'aria-label' });
    T.ti(CMD.q, 'shell.palette.input.placeholder', null, { attr: 'placeholder' });
    T.ti(CMD.q, 'shell.palette.input.aria', null, { attr: 'aria-label' });
    T.$$('.cmdk-foot [data-k]', el).forEach(s => T.ti(s, `shell.palette.foot.${s.dataset.k}`));
    // the list itself is rebuilt from the catalog while it is open
    T.on('i18n:change', () => { if (CMD.open) renderCmd(); });
    CMD.q.addEventListener('input', () => { CMD.sel = 0; CMD.fresh = false; renderCmd(); });
    CMD.listEl.addEventListener('pointermove', e => { const b = e.target.closest('.cmdk-item'); if (b && +b.dataset.i !== CMD.sel && b.getAttribute('aria-disabled') !== 'true') { CMD.sel = +b.dataset.i; CMD.fresh = false; placeHl(true); } });
    CMD.listEl.addEventListener('click', e => { const b = e.target.closest('.cmdk-item'); if (b) execCmd(+b.dataset.i); });
    CMD.scrim.addEventListener('click', closeCmd);
    el.addEventListener('keydown', e => {
      if (e.key === 'Escape') { e.preventDefault(); closeCmd(); }
      else if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault(); CMD.fresh = false; if (!CMD.items.length) return;
        const dir = e.key === 'ArrowDown' ? 1 : -1; let j = CMD.sel;
        for (let n = 0; n < CMD.items.length; n++) { j = (j + dir + CMD.items.length) % CMD.items.length; if (!CMD.items[j].dis) break; }
        CMD.sel = j; placeHl(true);
      } else if (e.key === 'Enter') { e.preventDefault(); execCmd(CMD.sel); }
      else if (e.key === 'Tab') e.preventDefault();
    });
  }
  /* how well a command matches the query: the label first (exact › prefix › word prefix › substring),
     then its keywords, then the hint and the group name */
  function cmdScore(x, q) {
    const l = String(x.label || '').toLowerCase();
    if (l === q) return 100;
    if (l.startsWith(q)) return 90;
    if (l.split(/[\s·\uFF08(]+/).some(w => w && w.startsWith(q))) return 80; // (a full-width open paren too)
    if (l.includes(q)) return 70;
    const kw = String(x.kw || '').toLowerCase().split(/\s+/);
    if (kw.includes(q)) return 50;
    if (kw.some(w => w.startsWith(q))) return 45;
    if (kw.some(w => w.includes(q))) return 40;
    if (`${x.hint || ''} ${x.g || ''}`.toLowerCase().includes(q)) return 20;
    return 0;
  }
  /* groups are ids (T-P4-04a; mapping_core §0 #5, P2): their names are catalog messages and their
     order is this list. A group that is not an id (a page that still registers its own word) is
     shown as given: in the block of the id whose name it equals, else after the known groups in
     the order first seen; a command with groupLast:true puts its block after all of them (the
     design target's demo block, ui/src/design/demo.js; T-P4-07: no demo group in the shell) */
  const CMD_GROUPS = ['service', 'go', 'mode', 'overview', 'profiles', 'logs', 'settings'];
  const groupName = g => (CMD_GROUPS.includes(g) ? T.t(`shell.palette.group.${g}`) : String(g || ''));
  function renderCmd() {
    const q = CMD.q.value.trim().toLowerCase();
    let items = CMD.list.map((c, i) => ({ c, i, label: val(c.label), hint: val(c.hint), dis: !!val(c.disabled), icon: val(c.icon) })).filter(x => val(x.c.when) !== false);
    // one block per group, whatever order the pages registered in; within a group, `order` then registration
    const named = new Map(CMD_GROUPS.map((g, k) => [groupName(g), k]));
    const gi = new Map();
    items.forEach(x => {
      x.g = groupName(x.c.group); x.kw = val(x.c.keywords);
      if (!gi.has(x.g)) gi.set(x.g, x.c.groupLast ? 1e3 : named.has(x.g) ? named.get(x.g) : 10 + gi.size);
    });
    items.sort((a, b) => gi.get(a.g) - gi.get(b.g) || (a.c.order || 0) - (b.c.order || 0) || a.i - b.i);
    if (q) {
      // ranked: the group holding the best match comes first, and the best match leads its group
      items.forEach(x => { x.s = cmdScore(x, q); });
      items = items.filter(x => x.s > 0);
      const best = new Map(); items.forEach(x => { best.set(x.g, Math.max(best.get(x.g) || 0, x.s)); });
      items.sort((a, b) => (best.get(b.g) - best.get(a.g)) || gi.get(a.g) - gi.get(b.g) || b.s - a.s || (a.c.order || 0) - (b.c.order || 0) || a.i - b.i);
    }
    CMD.items = items;
    CMD.sel = Math.min(CMD.sel, Math.max(0, CMD.items.length - 1));
    // on open: the first page to go to (not the one shown). Never a command that changes state or
    // that leaves and runs something (Ctrl K → Enter must not stop the proxy or start a check)
    if (!q && CMD.fresh) {
      let j = CMD.items.findIndex(x => !x.dis && x.c.nav && x.c.nav !== (T.router && T.router.current));
      if (j < 0) j = CMD.items.findIndex(x => !x.dis && !x.c.mutates && !x.c.runs);
      if (j >= 0) CMD.sel = j;
    }
    if (CMD.items[CMD.sel] && CMD.items[CMD.sel].dis) { const j = CMD.items.findIndex(x => !x.dis); if (j >= 0) CMD.sel = j; }
    // (T-P4-07) built as nodes: labels, hints and group names are text
    const kids = [T.html`<span class="cmdk-hl" aria-hidden="true"></span>`];
    let g = null;
    CMD.items.forEach((x, i) => {
      if (x.g !== g) {
        g = x.g;
        const gh = T.html`<div class="cmdk-g" role="presentation">${g || ''}</div>`;
        if (x.c.data) gh.setAttribute('translate', 'no');
        kids.push(gh);
      }
      // (keycaps and technical values such as an IP are data: translate="no")
      const hint = x.hint ? (/^(Ctrl|Alt|Shift)\b/.test(x.hint) ? T.html`<span class="hint" translate="no">${x.hint.split(' ').map(k => T.html`<kbd>${k}</kbd>`)}</span>` : /[\d.:]{6,}/.test(x.hint) ? T.html`<span class="hint mono" translate="no">${x.hint}</span>` : T.html`<span class="hint ">${x.hint}</span>`) : '';
      // a command that names a tier (cmd.tier) shows it as the tier tag: the name, then its role
      const lab = x.c.tier ? T.html`<span class="cmdk-tt">${tierTag(x.c.tier, { icon: false })}</span>` : T.html`<span>${x.label}</span>`;
      const item = T.html`<button class="cmdk-item" role="option" id="cmdk-${i}" data-i="${i}" aria-selected="${String(i === CMD.sel)}" aria-disabled="${String(x.dis)}" type="button" tabindex="-1" aria-label="${x.label}">${T.icon(x.icon || 'dot', 16)}${lab}${hint}</button>`;
      // data: the label (and its group's name) are not catalog words: translate="no", like data
      if (x.c.data) item.setAttribute('translate', 'no');
      kids.push(item);
    });
    if (!CMD.items.length) kids.push(T.html`<div class="cmdk-empty">${T.t('shell.palette.empty')}</div>`);
    CMD.listEl.replaceChildren(...kids); placeHl(false);
  }
  function placeHl(animate) {
    const hl = CMD.listEl.querySelector('.cmdk-hl'), it = CMD.listEl.querySelector(`[data-i="${CMD.sel}"]`);
    if (!hl) return;
    if (!it) { hl.style.opacity = '0'; return; }
    hl.style.opacity = '1';
    CMD.listEl.querySelectorAll('.cmdk-item').forEach(b => b.setAttribute('aria-selected', String(b.dataset.i === String(CMD.sel))));
    CMD.q.setAttribute('aria-activedescendant', `cmdk-${CMD.sel}`);
    spring(hl, { y: it.offsetTop }, { k: 700, c: 52, instant: !animate });
    const top = it.offsetTop, bot = top + it.offsetHeight, L = CMD.listEl;
    if (top < L.scrollTop) L.scrollTop = top - 6; else if (bot > L.scrollTop + L.clientHeight) L.scrollTop = bot - L.clientHeight + 6;
  }
  function openCmd() {
    if (CMD.open) return;
    if (!CMD.el) buildCmd();
    CMD.open = true; CMD.ret = doc.activeElement; hideTip(); closeFloats();
    CMD.el.hidden = false; CMD.q.value = ''; CMD.sel = 0; CMD.fresh = true; renderCmd(); CMD.q.focus();
    anim(CMD.panel, [{ opacity: 0, transform: 'translateY(-6px) scale(.985)' }, { opacity: 1, transform: 'none' }], { duration: 180, easing: EASE.out });
    anim(CMD.scrim, [{ opacity: 0 }, { opacity: 1 }], { duration: 160 });
  }
  function closeCmd() {
    if (!CMD.open) return; CMD.open = false;
    const a = anim(CMD.panel, [{ opacity: 1, transform: 'none' }, { opacity: 0, transform: 'translateY(-4px) scale(.99)' }], { duration: 110, easing: EASE.in, fill: 'forwards' });
    anim(CMD.scrim, [{ opacity: 1 }, { opacity: 0 }], { duration: 110, fill: 'forwards' });
    onDone(a, () => {
      CMD.panel.getAnimations().forEach(x => x.cancel()); CMD.scrim.getAnimations().forEach(x => x.cancel());
      // focus goes back where it came from, unless a command took the user elsewhere (a page change
      // hid it, or the landing already took focus)
      if (!CMD.open) {
        CMD.el.hidden = true;
        const r = CMD.ret, ae = doc.activeElement;
        if (r && r.focus && r.isConnected && isShown(r) && (!ae || ae === doc.body || CMD.el.contains(ae))) r.focus({ preventScroll: true });
      }
    }, 300);
  }
  function execCmd(i) { const x = CMD.items[i]; if (!x || x.dis) return; CMD.byKey = lastInput === 'key'; closeCmd(); setTimeout(() => x.c.run && x.c.run(), 60); }
})();
