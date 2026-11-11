// Machine checks for one render-matrix cell (T-P4-10; P4 architecture §1.4, §3.8, §3.9, §12;
// DESIGN.md §4.3). The `*InPage` functions run inside the page (page.evaluate) and are
// self-contained; the rest runs in Node.
//
// Rules and thresholds (the report repeats them):
//   overflow    an element with its own visible text whose text runs past its padding box
//               (scrollWidth > clientWidth + 1 and a text rect beyond the box by > 1 px), or past
//               the nearest ancestor that clips horizontally (overflow-x hidden / clip) by > 1 px,
//               and the box has no effective `text-overflow: ellipsis` (ellipsis + overflow-x ≠
//               visible) → truncation. An effective text-overflow ellipsis box is measured
//               without that gate, the way Chrome draws the ellipsis: some text rect beyond its
//               fractional content box (border box less borders and padding) by > 0.02 px
//               (T-P5-13 fix round 1; a cut of a fraction of a pixel, or one inside the padding,
//               shows an ellipsis too). Effective ellipsis is listed as `ellipsized` (info only)
//               when what it cuts is data; when it cuts catalog text (T-P5-13, 决定_P5裁定 P5-e
//               #5: a cut text node not under translate="no" with a data-i18n-id or a traced T.t
//               string, also one ' · ' part of it) it is a truncation (by: 'ellipsis'; a line
//               clamp: clamp: true), so it fails zh_CN / pseudo cells and goes to i18n_check
//               for the other 8 languages like every truncation; a case allows one with
//               overflow_allow and a reason. Known limits of the catalog test: a text node is
//               recognised only as a whole traced string, one ' · ' part of one, or under a
//               data-i18n-id; catalog words built by concatenation, or split over nodes by
//               richInto, are not recognised and stay `ellipsized` (info); an atomic inline box
//               (inline-block / inline-flex, e.g. a .ttag tier tag with its role word) cut by its
//               row's ellipsis is not a text node of that row and is seen by the box / ancestor
//               rules only. An ellipsis box is still checked against its clipping ancestors with its border
//               box (the box itself cut by an ancestor → truncation, by: ancestor_of_ellipsis).
//               Overflow-x auto / scroll (on the box or the nearest non-visible ancestor) only
//               makes the text scrollable: listed as `scrollable_x` (info). Vertical: a box with
//               some text lines inside the nearest vertically clipping ancestor (itself included;
//               overflow-y hidden / clip) and some beyond it by > 1 px → truncation (text wholly
//               outside is hidden by design: odometer strips, roll swaps); -webkit-line-clamp
//               counts as an ellipsis. Inline elements are measured on their nearest non-inline
//               ancestor. Boxes of 1 px or less (visually hidden .sr-only text) are not measured.
//               A case's overflow_allow [{selector: regex source, why}] moves matching
//               truncations to `allowed` (run.mjs).
//   box         (T-P5-11) the border box of an element (widened by its own text drawn past it)
//               leaves the padding box of a row, cell, card, sheet … it sits in, horizontally,
//               by > 1 px, whether or not an ancestor clips: a truncation with by: 'box' (same
//               verdict as the rest of overflow). Measured: visible non-inline elements that draw
//               text (own text or inline children's, the text rule's blockOf), controls, and the
//               root of a floating layer (absolute / fixed) that shows text. An element that is
//               itself the cell (overflow-x visible) whose text runs past its own padding box by
//               > 1 px is a finding against itself (own_cell: a grid key into its value, a
//               segment into the next one; the text rule calls that scrollable_x inside a
//               scrolling page), unless the text also runs past its nearest horizontal scroll
//               container (then it is that scroller's content). Not measured: content of a horizontal scroll container, a
//               floating layer against anything but its containing block (absolute) or the
//               viewport (fixed), [data-overflow-ok] subtrees and the BOX_OK components (each
//               with a reason; an entry with a px ceiling may stand out by that much only), 1 px
//               boxes, a moved face (under a transform / translate, or aria-hidden / inert) wholly
//               outside its clipping ancestor. A declared bleed is not an overflow: a negative
//               horizontal margin is taken off that side, up to 8 px (a text button's hover wash).
//               Known limits: vertical overflow is not part of this rule (fixed-height rows,
//               odometer strips and roll faces stand out of their boxes vertically by design); an
//               absolutely positioned element whose containing block is <body> is not measured
//               (the shell's layers are fixed; nothing in the pages is positioned against body).
//   host_scroll #host, body and the document must not scroll horizontally
//               (scrollWidth > clientWidth + 1 is a failure). Vertical numbers are recorded.
//   leak        pseudo language only: visible text nodes (SVG <text> / <title> included), CSS
//               ::before / ::after quoted content and visible aria-label / title / placeholder /
//               alt / data-tip / input[type=button|submit|reset] value must not contain letters
//               outside ⟦ ⟧. Brackets pair over the text nodes of one pairing scope in DOM
//               order (hidden ones too, so a rich message split over inline nodes stays
//               balanced) and never across scopes. The scope of a text node is its nearest
//               ancestor that is not inline-level: the walk up passes every display inline* box
//               (inline, inline-block, inline-flex <tier> slot .ttag, inline-grid <kbd> …),
//               display contents, a display:none phrasing element (span, b, kbd …), a phrasing
//               element whose parent is display (inline-)flex / (inline-)grid (a blockified flex
//               / grid item: .cmdk-foot > span > kbd) and the direct (blockified) children of an
//               inline-* box; non-phrasing flex / grid items (div, button …) stay their own
//               scopes. A scope left open or a ⟧ with
//               nothing open is an `unbalanced` leak and the next scope starts at depth 0. Only
//               the overflow check measures on the nearest non-inline box (display inline /
//               contents). Subtrees under translate="no" are not read
//               at all. Tokens on the whitelist (LEAK_WHITELIST + the case's leak_allow) are not
//               letters for this check: digits, tier code names, protocol names, IPs…
//               Not read: <input> values other than buttons, canvas, images.
//   inject      no script:not([src]), [onerror], [onload], img[src="x"]; window.__pwned unset.
//   garbage     visible text or the text attributes aria-label / title / placeholder / data-tip
//               with NaN / undefined / [object <Type>] (as words) is a failure in every cell
//               (T-P4-07, 决定_第八波裁定: was info only; data-tip added). translate="no" data is
//               read too, and SVG content (<text>, <title>, aria-label) as well (fix round 1). A case's garbage_allow [{selector: regex source, why}] moves matching
//               hits to `garbage_allowed` (run.mjs), e.g. a fixture whose data is the word itself.
//   console     every console.error / page error / fixture-clock error / unmatched fixture
//               invoke is a failure (collected by run.mjs).
//   fonts       (CDP CSS.getPlatformFontsForNode; Chrome is started offline). Every element is
//               measured on its OWN text nodes only, one CDP call per text node (T-P5-03 #6a):
//               the call on an element also counts the text of its child elements (a .mono
//               port, a CIDR), so mixed runs read as two fonts. Elements are visible, not under
//               translate="no", with own text that has a letter. While measuring, text-overflow
//               ellipses are off (the "…" is drawn in the cutting box's font and counted for the
//               text node it cuts).
//               ja_JP: none of them may be drawn with Microsoft YaHei / SimSun / NSimSun (a
//               Chinese glyph shape). Scope with an --i18n-trace build (T-P5-03 #1, P5 §4): all
//               catalog text, i.e. an element that carries data-i18n-id or has an ancestor that
//               does (T.tx / T.ti), or whose own text is a traced T.t string (T.i18n.traced()),
//               or whose own text has kana (so the scope is a superset of the old rule: a T.t
//               string joined with other words is still checked); kanji-only messages (設定,
//               状態) are checked too. Exempt (counted in exempt): text under a lang attribute
//               other than ja. (The language list's own names, settings.app.lang.name.*, are
//               under translate="no" with their own lang, on the shown choice and on every menu
//               option, since T-P5-07: they need no exemption by id.)
//               Known limit: T.tx puts a string parameter in the same text node as the message,
//               where the page cannot mark it translate="no", so Chinese data in a message
//               parameter is measured with the message and can fail the cell (no fixture has
//               such data today). Without a trace build: the old
//               rule (own text with kana), named in the result (scope: kana_fallback, note) and
//               warned once on stderr. ru_RU: every element whose own text is Cyrillic (not mixed
//               with CJK data) must be drawn with exactly one font.
//               Probes (FONT_PROBES, appended for the measurement and removed again) always
//               count: a probe for which no font is measured is a failure (font_unmeasured). A
//               page element that is not painted (in a content-visibility: auto subtree off
//               screen, e.g. a virtualised log list) or whose own text measures no font at all is
//               skipped and counted (unmeasured_skipped, unmeasured_by, samples in unmeasured).
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

// ---------------------------------------------------------------------------------------------
// leak whitelist: whole tokens (after stripping surrounding punctuation) that may stay unbracketed
export const LEAK_WHITELIST = [
  '^(?:Vanguard|Sentinel|Bastion|Gateway)$', // tier code names are never translated (architecture §3.1)
  '^(?:Tesseract|Mihomo)$', // product / core names
  '^(?:VLESS|VMess|vmess|vless|Trojan|trojan|SS|ss|SSR|Shadowsocks|shadowsocks|Hysteria2?|hysteria2?|hy2|TUIC|tuic|WireGuard|wireguard|SOCKS5?|socks5?|HTTPS?|https?|TCP|UDP|DNS|DoH|DoT|QUIC|TLS|REALITY|Reality|gRPC|grpc|WS|ws|H2|h2|AnyTLS|anytls|Snell|snell|mixed)$', // protocol names
  '^IPv[46]$',
  '^(?:\\d{1,3}\\.){3}\\d{1,3}(?::\\d{1,5})?(?:/\\d{1,2})?$', // IPv4 [:port] [/cidr]
  '^\\[?[0-9A-Fa-f]{0,4}(?::[0-9A-Fa-f]{0,4}){2,7}\\]?(?::\\d{1,5})?(?:/\\d{1,3})?$', // IPv6
  '^AS\\d+$', // autonomous system numbers
  '^v?\\d+(?:\\.\\d+)+(?:[-+][0-9A-Za-z.]+)?$', // version numbers
  '^[0-9a-f]{8,}$', // hashes / ids (lower-case hex, at least 8)
];

const PENDING_RE = /@i18n-pending/;

// ---------------------------------------------------------------------------------------------
// In page: overflow, host scroll, leaks, injection. opts = {pseudo, whitelist: [regex source]}.
export function cellChecksInPage(opts) {
  const SKIP = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'svg', 'SVG']);
  const OPEN = '⟦', CLOSE = '⟧';
  const vis = el => !!el && el.checkVisibility({ opacityProperty: true, visibilityProperty: true });
  const skipped = el => { for (let e = el; e; e = e.parentElement) if (SKIP.has(e.tagName) || e.namespaceURI === 'http://www.w3.org/2000/svg') return true; return false; };
  // the leak check reads SVG text (<text>, <title>, aria-label …); only script / style are skipped
  const skippedLeak = el => { for (let e = el; e; e = e.parentElement) if (/^(?:script|style|noscript|template)$/i.test(e.localName)) return true; return false; };
  const noTranslate = el => !!(el && el.closest('[translate="no"]'));
  const cssEsc = s => (window.CSS && CSS.escape ? CSS.escape(s) : s);
  const selectorOf = el => {
    const parts = [];
    for (let e = el; e && e !== document.body && e.nodeType === 1 && parts.length < 6; e = e.parentElement) {
      if (e.id) { parts.unshift('#' + cssEsc(e.id)); break; }
      let p = e.tagName.toLowerCase();
      const cls = [...e.classList].filter(c => /^[A-Za-z][\w-]*$/.test(c)).slice(0, 2);
      if (cls.length) p += '.' + cls.join('.');
      const sib = e.parentElement ? [...e.parentElement.children].filter(x => x.tagName === e.tagName) : [];
      if (sib.length > 1) p += `:nth-of-type(${sib.indexOf(e) + 1})`;
      parts.unshift(p);
    }
    return parts.join(' > ');
  };
  const i18nId = el => { const t = el && el.closest('[data-i18n-id]'); return t ? t.getAttribute('data-i18n-id') : null; };
  const ownText = el => { let s = ''; for (const n of el.childNodes) if (n.nodeType === 3) s += n.nodeValue; return s.replace(/\s+/g, ' ').trim(); };
  const isInline = el => getComputedStyle(el).display === 'inline' || getComputedStyle(el).display === 'contents';
  const blockOf = el => { let b = el; while (b && b !== document.body && isInline(b)) b = b.parentElement; return b; };
  // Bracket-pairing scope (leak check only; the overflow check measures on blockOf). One
  // sentence spans every inline-level box in it: display inline*, i.e. inline, inline-block,
  // inline-flex, inline-grid … (<kbd> is inline-grid, the <tier> slot .ttag is inline-flex) or
  // contents; a phrasing element hidden by display:none; a phrasing element (span, b, kbd …)
  // whose parent is display flex / grid / inline-flex / inline-grid, i.e. blockified as a flex
  // or grid item (.cmdk-foot{display:flex} > span > kbd: the span turns flex, the kbd grid);
  // and the direct children of an atomic inline box (display inline-*), which are blockified
  // as flex / grid items (.ttag > b, .r). Non-phrasing flex / grid items (div, button.nav-item,
  // li …) stay their own scopes. The scope is the nearest ancestor that is none of these.
  const PHRASING = /^(?:a|abbr|b|bdi|bdo|br|cite|code|data|dfn|em|i|kbd|label|mark|q|s|samp|small|span|strong|sub|sup|time|u|var|wbr)$/;
  const inlineLevel = el => {
    const d = getComputedStyle(el).display;
    if (d.startsWith('inline') || d === 'contents') return true;
    if (!PHRASING.test(el.localName)) return false;
    // a hidden phrasing element, or a phrasing element blockified as a flex / grid item
    return d === 'none' || (!!el.parentElement && /^(?:inline-)?(?:flex|grid)$/.test(getComputedStyle(el.parentElement).display));
  };
  const atomicInline = el => !!el && el !== document.body && getComputedStyle(el).display.startsWith('inline-');
  const pairScopeOf = el => { let b = el; while (b && b !== document.body && (inlineLevel(b) || atomicInline(b.parentElement))) b = b.parentElement; return b; };
  // message id of a T.t() string (--i18n-trace builds), for text that carries no data-i18n-id
  const traced = (window.T && T.i18n && typeof T.i18n.traced === 'function') ? T.i18n.traced() : null;
  const tracedId = text => { if (!traced || !text) return null; const l = traced[text] || traced[text.trim()]; return l && l.length ? l[0] : null; };

  // ---- overflow ------------------------------------------------------------------------
  // overflow-x / -y hidden or clip cut text off (truncation); auto or scroll only make it
  // scrollable (recorded in scrollable_x, not a failure)
  const CLIPS = new Set(['hidden', 'clip']);
  const truncations = [], ellipsized = [], scrollable = [];
  const seen = new Set();
  const textRects = box => {
    const rects = [];
    const tw = document.createTreeWalker(box, NodeFilter.SHOW_TEXT);
    for (let n = tw.nextNode(); n; n = tw.nextNode()) {
      if (!n.nodeValue.trim() || blockOf(n.parentElement) !== box || !vis(n.parentElement)) continue;
      const r = document.createRange();
      r.selectNodeContents(n);
      for (const q of r.getClientRects()) if (q.width > 0 && q.height > 0) rects.push(q);
    }
    return rects;
  };
  const padBox = el => {
    const r = el.getBoundingClientRect();
    const left = r.left + el.clientLeft, top = r.top + el.clientTop;
    return { left, right: left + el.clientWidth, top, bottom: top + el.clientHeight };
  };
  // nearest ancestor of `from` (inclusive) whose overflow on `axis` is not visible
  const clipper = (from, axis) => {
    for (let a = from; a && a !== document.documentElement; a = a.parentElement) {
      const v = getComputedStyle(a)[axis === 'x' ? 'overflowX' : 'overflowY'];
      if (v !== 'visible') return { el: a, value: v };
    }
    return null;
  };
  const beyondX = (l, r, b) => Math.max(r - b.right, b.left - l);
  // the content box in fractional pixels (border box less borders and padding). Chrome draws a
  // text-overflow ellipsis as soon as a line's text passes the content box by any fraction of a
  // pixel, padding or not (T-P5-13 fix round 1: es En espera 0.41 px, Error al iniciar 0.13 px, a
  // 150 px box with 24 px padding cutting 108.5 px of words were all missed by the 1 px padding-box
  // test), so an effective ellipsis box is measured against it with ELL_TOL (above LayoutUnit's
  // 1/64 px, below any visible cut)
  const ELL_TOL = 0.02;
  const contentBox = el => {
    const r = el.getBoundingClientRect(), cs = getComputedStyle(el), f = v => parseFloat(v) || 0;
    const left = r.left + f(cs.borderLeftWidth) + f(cs.paddingLeft), right = r.right - f(cs.borderRightWidth) - f(cs.paddingRight);
    const top = r.top + f(cs.borderTopWidth) + f(cs.paddingTop), bottom = r.bottom - f(cs.borderBottomWidth) - f(cs.paddingBottom);
    return { left, right, top, bottom };
  };
  // (T-P5-13, 决定_P5裁定 P5-e #5) an ellipsis may only shorten data. Catalog text that an
  // effective ellipsis cuts is a truncation (by: 'ellipsis'); data cut by an ellipsis stays
  // `ellipsized` (info). Catalog text = a text node drawn by the box, cut by it (horizontally: some
  // rect beyond the content box by > ELL_TOL; a line clamp: a line whose middle is beyond the
  // padding box), not under translate="no", that carries a
  // catalog id: data-i18n-id on its element or an ancestor (T.tx / T.ti), or a traced T.t string
  // (--i18n-trace builds), the whole node or one of its ' · ' parts (a label · sub pair joined in
  // one node). Without a trace build only data-i18n-id text is found.
  const catalogId = s => tracedId(s) || (s.includes(' · ') ? s.split(' · ').map(x => tracedId(x.trim())).find(Boolean) || null : null);
  const cutCatalog = (box, pb, axis, tol = 1) => {
    const tw = document.createTreeWalker(box, NodeFilter.SHOW_TEXT);
    for (let n = tw.nextNode(); n; n = tw.nextNode()) {
      const p = n.parentElement, s = n.nodeValue.replace(/\s+/g, ' ').trim();
      if (!s || !p || blockOf(p) !== box || !vis(p) || noTranslate(p)) continue;
      const r = document.createRange();
      r.selectNodeContents(n);
      // (a clamped line is hidden when its middle is beyond the box: a shown line's content area may
      // stand a pixel or two past its line box)
      const cut = [...r.getClientRects()].some(q => q.width > 0 && q.height > 0 && (axis === 'x' ? q.right > pb.right + tol || q.left < pb.left - tol : q.top + q.height / 2 > pb.bottom || q.top + q.height / 2 < pb.top));
      if (!cut) continue;
      const id = i18nId(p) || catalogId(s);
      if (id) return { id, text: s.slice(0, 120) };
    }
    return null;
  };
  for (const el of document.body.querySelectorAll('*')) {
    if (skipped(el) || !ownText(el) || !vis(el)) continue;
    const box = blockOf(el);
    if (!box || seen.has(box)) continue;
    seen.add(box);
    // visually hidden on purpose (.sr-only: a 1 px clipped box for screen readers): not a layout
    // overflow; its text is still read by the leak check
    const br = box.getBoundingClientRect();
    if (br.width <= 1 || br.height <= 1) continue;
    const rects = textRects(box);
    if (!rects.length) continue;
    const minL = Math.min(...rects.map(q => q.left)), maxR = Math.max(...rects.map(q => q.right));
    const cs = getComputedStyle(box);
    const ellX = cs.textOverflow === 'ellipsis' && cs.overflowX !== 'visible';
    const ellipsis = ellX || (cs.webkitLineClamp && cs.webkitLineClamp !== 'none');
    const base = { selector: selectorOf(box), text: ownText(el).slice(0, 120), id: i18nId(el) || tracedId(ownText(el)), scroll_width: box.scrollWidth, client_width: box.clientWidth, translate_no: noTranslate(el) };
    let hit = null, scroll = null;
    // horizontal: the box's own text past its padding box, else past the nearest clipping ancestor
    const pb = padBox(box);
    if (ellX) {
      // an effective text-overflow ellipsis: drawn when the text passes the content box by any
      // fraction (no scrollWidth gate: it is whole pixels and includes the padding)
      const cb = contentBox(box);
      if (maxR > cb.right + ELL_TOL || minL < cb.left - ELL_TOL) {
        const v = { by: 'self', overflow_x: cs.overflowX, over_px: Math.round(beyondX(minL, maxR, cb) * 100) / 100 };
        const cat = cutCatalog(box, cb, 'x', ELL_TOL);
        if (cat) truncations.push({ ...base, ...v, by: 'ellipsis', id: cat.id, text: cat.text }); else ellipsized.push({ ...base, ...v });
      }
    } else if (box.scrollWidth > box.clientWidth + 1 && (maxR > pb.right + 1 || minL < pb.left - 1)) {
      const v = { by: 'self', overflow_x: cs.overflowX, over_px: Math.round(beyondX(minL, maxR, pb)) };
      // an effective ellipsis is listed (info; catalog text cut by it is a truncation, T-P5-13) and
      // the box is still checked against its ancestors
      if (ellipsis) {
        const cat = cutCatalog(box, pb, 'x');
        if (cat) truncations.push({ ...base, ...v, by: 'ellipsis', id: cat.id, text: cat.text }); else ellipsized.push({ ...base, ...v });
      }
      else if (cs.overflowX === 'auto' || cs.overflowX === 'scroll') scroll = v;
      else if (cs.overflowX !== 'visible') hit = v;
      else {
        // text spilling out of a visible-overflow box: scrollable when the nearest non-visible
        // ancestor scrolls, a truncation when it clips or when nothing contains it
        const c = clipper(box.parentElement, 'x');
        if (c && !CLIPS.has(c.value)) scroll = { ...v, clip: selectorOf(c.el), clip_overflow_x: c.value }; else hit = v;
      }
    }
    if (!hit && !scroll) {
      const c = clipper(box.parentElement, 'x');
      // an effective ellipsis box draws its own text inside itself: what an ancestor can cut is
      // the box (border box), not the text behind the ellipsis
      const [l, r] = ellipsis ? [br.left, br.right] : [minL, maxR];
      if (c) {
        const ab = padBox(c.el);
        if (r > ab.right + 1 || l < ab.left - 1) {
          const v = { by: 'ancestor', clip: selectorOf(c.el), overflow_x: c.value, over_px: Math.round(beyondX(l, r, ab)) };
          if (CLIPS.has(c.value)) { hit = v; if (ellipsis) v.by = 'ancestor_of_ellipsis'; } else scroll = v;
        }
      }
    }
    // vertical: lines cut by the nearest ancestor (the box included) that clips vertically.
    // Clipped by itself: some text line inside its padding box and some beyond it by > 1 px.
    // Clipped by an ancestor: the box's own border box straddles the clip edge (inside by > 1 px
    // and beyond by > 1 px) and some text line is beyond it. A box wholly outside the clip (or
    // touching it by a subpixel) is hidden by design (odometer strips, roll swaps), not cut.
    // Text rects are content areas (taller than the line box), so they never decide "inside" for
    // an ancestor clip on their own.
    if (!hit) {
      const c = clipper(box, 'y');
      if (c && CLIPS.has(c.value)) {
        const ab = padBox(c.el);
        const cut = rects.filter(q => q.bottom > ab.bottom + 1 || q.top < ab.top - 1);
        const inside = c.el === box
          ? rects.some(q => q.bottom > ab.top + 1 && q.top < ab.bottom - 1)
          : Math.min(br.bottom, ab.bottom) - Math.max(br.top, ab.top) > 1 && (br.bottom > ab.bottom + 1 || br.top < ab.top - 1);
        if (inside && cut.length) hit = { by: c.el === box ? 'self_y' : 'ancestor_y', clip: selectorOf(c.el), overflow_y: c.value, over_px: Math.round(Math.max(...cut.map(q => Math.max(q.bottom - ab.bottom, ab.top - q.top)))) };
      }
    }
    if (hit && ellipsis && hit.by === 'self_y') {
      // a line clamp: catalog text in the clamped lines is a truncation as well (T-P5-13); a one-line
      // text-overflow box whose glyphs stand a pixel past its line box stays ellipsized (info)
      const clamp = !!(cs.webkitLineClamp && cs.webkitLineClamp !== 'none');
      const cat = clamp ? cutCatalog(box, pb, 'y') : null;
      if (cat) truncations.push({ ...base, ...hit, by: 'ellipsis', clamp: true, id: cat.id, text: cat.text }); else ellipsized.push({ ...base, ...hit });
    } else if (hit) truncations.push({ ...base, ...hit });
    else if (scroll) scrollable.push({ ...base, ...scroll });
  }

  // ---- box overflow (T-P5-11) -------------------------------------------------------------
  // A box that leaves its row, cell, card or sheet: the text rules above only see text cut off,
  // so a button pushed out of a sheet footer, or a link running past its column inside a page
  // that scrolls, passed while its label stayed readable (T-P5-06 §4, §6). Every visible element
  // that is not display inline / contents and draws text, or is a control (BOX_CTRL), or is the
  // root of a floating layer (absolute / fixed) showing text, is measured: its border box, widened
  // by its own text when that spills past the box (overflow-x visible) and narrowed by a declared
  // bleed (a negative horizontal margin, at most BLEED_MAX), against its own padding box when it
  // is itself the cell, else against the padding box
  // of every ancestor box on the way up (horizontal only). The
  // walk stops at a horizontal scroll container (content beyond it scrolls: scrollable_x is the
  // text rule's business), and at a floating layer: an absolutely positioned element is held to
  // its containing block (offsetParent) only, a fixed one to the viewport only, and an element
  // inside such a layer to the layer. Exempt, precisely: an element (or its subtree) marked
  // data-overflow-ok, and the components of BOX_OK (each with its reason, some with a px ceiling);
  // boxes of 1 px or less; a moved face (under a transform / translate, or aria-hidden / inert)
  // wholly outside its nearest clipping ancestor (hidden by design: a roll's resting face).
  // One finding per element and container: an element whose ancestor (below that container) is
  // already reported against it is not repeated; an element the text rules already reported is
  // not repeated either. Findings are truncations with by: 'box' (zh_CN / pseudo fail, the other
  // languages report to i18n_check, overflow_allow applies as for every truncation).
  const BOX_CTRL = 'button, a[href], input:not([type="hidden"]), select, textarea, [role="button"], [role="tab"], [role="link"], [role="switch"], [role="checkbox"], [role="radio"], [role="menuitem"], [role="option"]';
  const BOX_OK = [
    // [selector the element itself matches, why, ceiling px?] (T-P5-11 matrix classification, see
    // its report). Without a ceiling the element is skipped (what it holds is still measured
    // against it); with one it is measured and may stand out by at most that many px (fix round 1).
    ['.copy-face', 'the copy button\'s done face (components.css): a 78 px overlay clipped to the 28 px button by clip-path, revealed over the row on purpose; its label is measured against the face'],
    ['.set-sec-clear', 'a saved secret\'s 清除 link (settings.css): placed with right: -6px on the field\'s corner, its hover wash hangs 6 px past the field like every .link-btn; a computed offset cannot tell that from a misplaced box, so the component is named, and only those 6 px are allowed', 6],
    ['.set-lat-mk, .set-lat-mk > span', 'a latency axis marker (settings.css): a full-width track moved by translateX inside the clipped axis (overflow: clip), its label centred on the track\'s start by translate: -50%; the text rule checks the label against the axis clip'],
  ];
  // a declared bleed (a negative horizontal margin) is taken off its side up to this much: the
  // hover wash of a text button hangs 4-8 px past the text column so its label lines up (.link-btn
  // 6, .set-recent-all / .set-adv-t / .logs-rd-more / .tiers-back 8; fix round 1: was taken off
  // whole, whatever its size; a 6 px cap reported those four 8 px washes by 2 px). Larger negative
  // margins (.ov-confirm 15, .tiers-scroll 12, .tiers-imp-prev 10) are layout bleeds into a
  // padding and are measured like any box
  const BLEED_MAX = 8;
  const boxOkCap = el => {
    if (el.closest('[data-overflow-ok]')) return Infinity;
    const e = BOX_OK.find(([sel]) => el.matches(sel));
    return e ? (e[2] == null ? Infinity : e[2]) : 0;
  };
  const rectCache = new Map(), styleCache = new Map();
  const rectOf = el => { let r = rectCache.get(el); if (!r) { r = el.getBoundingClientRect(); rectCache.set(el, r); } return r; };
  const styleOf = el => { let s = styleCache.get(el); if (!s) { s = getComputedStyle(el); styleCache.set(el, s); } return s; };
  const notBox = el => { const d = styleOf(el).display; return d === 'inline' || d === 'contents' || d === 'none'; };
  const scrollsX = el => { const v = styleOf(el).overflowX; return v === 'auto' || v === 'scroll'; };
  const clipsX = el => CLIPS.has(styleOf(el).overflowX);
  const reportedText = new Set(truncations.map(t => t.selector));
  const boxReported = new Map(); // element → container element it was reported against
  const firstId = el => i18nId(el) || (el.querySelector('[data-i18n-id]') ? el.querySelector('[data-i18n-id]').getAttribute('data-i18n-id') : null) || tracedId(ownText(el)) || tracedId((el.textContent || '').replace(/\s+/g, ' ').trim());
  // the blocks that draw visible text (the text rule's blockOf: inline content is drawn by its
  // nearest non-inline ancestor), so a block whose words all sit in inline children is measured
  // too (fix round 1)
  const textBlocks = new Set();
  {
    const tw = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    for (let n = tw.nextNode(); n; n = tw.nextNode()) {
      const p = n.parentElement;
      if (!p || !n.nodeValue.trim() || skipped(p)) continue;
      const b = blockOf(p);
      if (b && b !== document.body) textBlocks.add(b);
    }
  }
  // a face rolled or slid out of its clip on purpose sits under a transform (or is hidden from
  // assistive technology); only such a box may stand wholly outside its clipping ancestor
  const movedFace = (el, clip) => {
    if (el.closest('[aria-hidden="true"], [inert]')) return true;
    for (let a = el; a && a !== clip; a = a.parentElement) { const s = styleOf(a); if ((s.transform && s.transform !== 'none') || (s.translate && s.translate !== 'none')) return true; }
    return false;
  };
  for (const el of document.body.querySelectorAll('*')) {
    if (skipped(el) || notBox(el) || !vis(el)) continue;
    const pos = styleOf(el).position;
    // measured: a block that draws text, a control, or the root of a floating layer that shows text
    const draws = textBlocks.has(el);
    if (!draws && !el.matches(BOX_CTRL) && !((pos === 'absolute' || pos === 'fixed') && (el.textContent || '').trim())) continue;
    const cap = boxOkCap(el);
    if (cap === Infinity) continue;
    const tol = 1 + cap;
    const r = rectOf(el);
    if (r.width <= 1 || r.height <= 1) continue;
    // the element's extent: its border box, and its own text where that is drawn past the box;
    // a negative horizontal margin is a declared bleed (.link-btn's hover wash hangs 6 px past the
    // text column so the label lines up): up to BLEED_MAX of it is taken off its side
    let left = r.left, right = r.right, tl = Infinity, tr = -Infinity;
    const visibleX = styleOf(el).overflowX === 'visible';
    if (visibleX && draws) for (const q of textRects(el)) { tl = Math.min(tl, q.left); tr = Math.max(tr, q.right); }
    if (tl < tr) { left = Math.min(left, tl); right = Math.max(right, tr); }
    left += Math.min(BLEED_MAX, Math.max(0, -parseFloat(styleOf(el).marginLeft) || 0));
    right -= Math.min(BLEED_MAX, Math.max(0, -parseFloat(styleOf(el).marginRight) || 0));
    // hidden by design: a moved face wholly outside the nearest horizontally clipping ancestor
    let clip = null;
    for (let a = el.parentElement; a && a !== document.documentElement; a = a.parentElement) if (styleOf(a).overflowX !== 'visible') { clip = a; break; }
    if (clip && clipsX(clip) && movedFace(el, clip)) { const cb = padBox(clip); if (right <= cb.left + 1 || left >= cb.right - 1) continue; }
    let container = null, over = 0;
    const test = a => {
      const b = padBox(a);
      const o = Math.max(right - b.right, b.left - left);
      if (o > tol) { container = a; over = o; return true; }
      return false;
    };
    // the element is itself the cell (fix round 1): a grid key, a flex status or a segment whose
    // own text runs past its own padding box into the next cell does not leave the row, and inside
    // a scrolling page the text rule only calls it scrollable_x (T-P5-06 §6's shape). Overflow-x
    // visible, text it draws, the text past its padding box by > 1 px: a box finding against itself.
    // Text that also runs past its nearest horizontal scroll container is that container's
    // scrollable content (a nowrap line in a scroller: scrollable_x), not this; text that stays
    // inside the scroller (.ovl-body: overflow-y auto, nothing to scroll sideways) overlaps the
    // next cell in plain view.
    if (visibleX && tl < tr) {
      const pb = padBox(el), o = Math.max(tr - pb.right, pb.left - tl);
      const sc = clip && scrollsX(clip) ? padBox(clip) : null;
      if (o > tol && (!sc || (tr <= sc.right + 1 && tl >= sc.left - 1))) { container = el; over = o; }
    }
    if (container) { /* reported against itself: no ancestor walk */ }
    else if (pos === 'fixed') {
      const vw = document.documentElement.clientWidth;
      const o = Math.max(right - vw, -left);
      if (o > tol) { container = document.documentElement; over = o; }
    } else if (pos === 'absolute') {
      const cb = el.offsetParent;
      if (cb && cb !== document.body && !scrollsX(cb)) test(cb);
    } else {
      for (let a = el.parentElement; a && a !== document.body && a !== document.documentElement; a = a.parentElement) {
        if (notBox(a)) continue;
        if (scrollsX(a)) break;               // scrollable content, not an overflow
        if (test(a)) break;
        const p = styleOf(a).position;
        if (p === 'absolute' || p === 'fixed') break; // the floating layer itself is measured on its own
      }
    }
    if (!container) continue;
    // one finding per cluster: an ancestor up to the container (included) is already reported
    // (the box that left its column carries the link inside it out with it)
    let dup = false;
    if (container !== el) for (let a = el.parentElement; a; a = a.parentElement) { if (boxReported.has(a)) { dup = true; break; } if (a === container) break; }
    boxReported.set(el, container);
    const selector = selectorOf(el);
    if (dup || reportedText.has(selector)) continue;
    truncations.push({ selector, text: (ownText(el) || (el.textContent || '').replace(/\s+/g, ' ').trim() || el.getAttribute('aria-label') || '').slice(0, 120), id: firstId(el),
      scroll_width: el.scrollWidth, client_width: el.clientWidth, translate_no: noTranslate(el),
      by: 'box', clip: container === document.documentElement ? 'viewport' : selectorOf(container), ...(container === el ? { own_cell: true } : {}), over_px: Math.round(over), box_width: Math.round(right - left), container_width: container.clientWidth });
  }

  // ---- host scroll ---------------------------------------------------------------------
  const dims = el => (el ? { scroll_width: el.scrollWidth, client_width: el.clientWidth, scroll_height: el.scrollHeight, client_height: el.clientHeight } : null);
  const host = document.getElementById('host');
  const scroll = { host: dims(host), body: dims(document.body), document: dims(document.scrollingElement || document.documentElement) };
  const hostScrollX = Object.entries(scroll).filter(([, d]) => d && d.scroll_width > d.client_width + 1).map(([k]) => k);

  // ---- leaks (pseudo only) ---------------------------------------------------------------
  const leaks = [];
  if (opts.pseudo) {
    const wl = (opts.whitelist || []).map(s => new RegExp(s, 'u'));
    const LETTER = /\p{L}/u;
    const STRIP = /^[\s\p{P}\p{S}]+|[\s\p{P}\p{S}]+$/gu;
    const badTokens = text => {
      const out = [];
      for (const raw of text.split(/\s+/)) {
        const tok = raw.replace(STRIP, '');
        if (!tok || !LETTER.test(tok) || wl.some(r => r.test(tok))) continue;
        // a token glued with punctuation ("Vanguard·Sentinel", "TCP/UDP") passes when every piece does
        const pieces = tok.split(/[\p{P}\p{S}]+/u).filter(Boolean);
        if (pieces.length > 1 && pieces.every(p => !LETTER.test(p) || wl.some(r => r.test(p)))) continue;
        out.push(tok);
      }
      return out;
    };
    const region = el => (el.closest('#pages') ? 'pages' : el.closest('#layer') ? 'layer' : 'shell');
    const leak = (el, item) => leaks.push({ ...item, selector: selectorOf(el), in_pages: !!el.closest('#pages'), region: region(el) });
    // Brackets pair over the text nodes of one pairing scope (pairScopeOf: a rich message split
    // over inline-level nodes, <kbd> and <tier> slots included); they never pair across scopes.
    // A scope left open (a cut message), or a ⟧ with nothing open, is an `unbalanced` leak, and
    // the depth starts again at 0 in the next scope, so one broken
    // message cannot hide the rest of the page.
    let depth = 0, block = null, openAt = null;
    const closeBlock = () => {
      if (depth > 0 && openAt) leak(openAt.el, { kind: 'unbalanced', side: 'open', depth, text: openAt.text });
      depth = 0; openAt = null;
    };
    // SVG <title> / <desc> are never drawn; they count as visible when their parent graphic is
    const svgMeta = el => el.namespaceURI === 'http://www.w3.org/2000/svg' && /^(?:title|desc)$/i.test(el.localName);
    const visText = el => (svgMeta(el) ? vis(el.parentElement) : vis(el));
    const tw = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    for (let n = tw.nextNode(); n; n = tw.nextNode()) {
      const p = n.parentElement;
      if (!p || skippedLeak(p) || noTranslate(p)) continue;
      const b = svgMeta(p) ? p : pairScopeOf(p);
      if (b !== block) { closeBlock(); block = b; }
      let outside = '', stray = false;
      for (const ch of n.nodeValue) {
        if (ch === OPEN) { if (depth++ === 0) openAt = { el: p, text: n.nodeValue.replace(/\s+/g, ' ').trim().slice(0, 120) }; outside += ' '; }
        else if (ch === CLOSE) { if (depth === 0) stray = true; else if (--depth === 0) openAt = null; outside += ' '; }
        else if (depth === 0) outside += ch;
      }
      if (stray) leak(p, { kind: 'unbalanced', side: 'close', text: n.nodeValue.replace(/\s+/g, ' ').trim().slice(0, 120) });
      if (!visText(p)) continue;
      const bad = badTokens(outside);
      if (bad.length) leak(p, { kind: 'text', text: n.nodeValue.replace(/\s+/g, ' ').trim().slice(0, 120), tokens: bad.slice(0, 8) });
    }
    closeBlock();
    const outsideOf = v => { let d = 0, out = ''; for (const ch of v) { if (ch === OPEN) { d++; out += ' '; } else if (ch === CLOSE) { d = Math.max(0, d - 1); out += ' '; } else if (d === 0) out += ch; } return out; };
    for (const el of document.body.querySelectorAll('[aria-label],[title],[placeholder],[alt],[data-tip],input[value]')) {
      if (skippedLeak(el) || noTranslate(el) || !vis(el)) continue;
      const attrs = ['aria-label', 'title', 'placeholder', 'alt', 'data-tip'];
      if (el.localName === 'input' && /^(?:button|submit|reset)$/i.test(el.type)) attrs.push('value');
      for (const attr of attrs) {
        const v = el.getAttribute(attr);
        if (!v) continue;
        const bad = badTokens(outsideOf(v));
        if (bad.length) leak(el, { kind: 'attr', attr, text: v.slice(0, 120), tokens: bad.slice(0, 8) });
      }
    }
    // CSS generated content (::before / ::after quoted strings; attr() values are read above)
    for (const el of document.body.querySelectorAll('*')) {
      if (skippedLeak(el) || noTranslate(el) || !vis(el)) continue;
      for (const pseudo of ['::before', '::after']) {
        const c = getComputedStyle(el, pseudo).content;
        if (!c || c === 'none' || c === 'normal' || !c.includes('"')) continue;
        const str = [...c.matchAll(/"((?:[^"\\]|\\.)*)"/g)].map(m => m[1]).join(' ');
        const bad = badTokens(outsideOf(str));
        if (bad.length) leak(el, { kind: 'css_content', pseudo, text: str.slice(0, 120), tokens: bad.slice(0, 8) });
      }
    }
  }

  // ---- garbage text (a failure since T-P4-07; data-tip read too) ----------------------------
  const garbage = [];
  // [object …] of any kind ([object HTMLSpanElement], [object DocumentFragment] — what a Node turned
  // into a string shows); SVG text, <title> and aria-label are read too (T-P4-07 fix round 1)
  const GARBAGE = /\bNaN\b|\bundefined\b|\[object [A-Za-z]+\]/;
  for (const el of document.body.querySelectorAll('*')) {
    if (skippedLeak(el) || !vis(el)) continue;
    const own = ownText(el);
    if (GARBAGE.test(own)) garbage.push({ selector: selectorOf(el), text: own.slice(0, 120) });
    for (const attr of ['aria-label', 'title', 'placeholder', 'data-tip']) { const v = el.getAttribute(attr); if (v && GARBAGE.test(v)) garbage.push({ selector: selectorOf(el), attr, text: v.slice(0, 120) }); }
  }

  // ---- injection ---------------------------------------------------------------------------
  const injection = [];
  for (const [rule, sel] of [['inline_script', 'script:not([src])'], ['onerror', '[onerror]'], ['onload', '[onload]'], ['img_x', 'img[src="x"]']]) {
    for (const el of document.querySelectorAll(sel)) injection.push({ rule, selector: selectorOf(el) });
  }
  if (window.__pwned !== undefined) injection.push({ rule: 'pwned', selector: 'window.__pwned' });

  return { truncations, ellipsized, scrollable_x: scrollable, scroll, host_scroll_x: hostScrollX, leaks, injection, garbage };
}

// ---------------------------------------------------------------------------------------------
// Visible message ids (--i18n-trace builds): elements with data-i18n-id, and T.t strings found
// in visible own text / text attributes through T.i18n.traced().
export function visibleIdsInPage() {
  const vis = el => el.checkVisibility({ opacityProperty: true, visibilityProperty: true });
  const ids = new Map();
  const add = (id, how) => { if (!ids.has(id)) ids.set(id, new Set()); ids.get(id).add(how); };
  for (const el of document.querySelectorAll('[data-i18n-id]')) if (vis(el)) add(el.getAttribute('data-i18n-id'), 'node');
  const traced = (window.T && T.i18n && typeof T.i18n.traced === 'function') ? T.i18n.traced() : {};
  const texts = new Set();
  for (const el of document.body.querySelectorAll('*')) {
    if (!vis(el)) continue;
    let own = '';
    for (const n of el.childNodes) if (n.nodeType === 3) own += n.nodeValue;
    if (own.trim()) texts.add(own.trim());
    for (const a of ['aria-label', 'title', 'placeholder', 'data-tip']) { const v = el.getAttribute(a); if (v) texts.add(v.trim()); }
  }
  for (const [s, list] of Object.entries(traced)) if (texts.has(s.trim())) for (const id of list) add(id, 'text');
  return [...ids].map(([id, how]) => ({ id, via: [...how].sort() })).sort((a, b) => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
}

// ---------------------------------------------------------------------------------------------
// Pending extraction (transition until the 04 series): a leak is "pending" when the file that
// renders its region still starts with @i18n-pending. Region: inside #pages → the page file;
// anywhere else (sidebar, top bar, banners, #layer) → the shell files.
export const PAGE_FILES = { overview: 'src/pages/overview.js', tiers: 'src/pages/tiers.js', profiles: 'src/pages/tiers.js', logs: 'src/pages/logs.js', settings: 'src/pages/settings.js' };
export const SHELL_FILES = ['src/index.html', 'src/core/core.js', 'src/core/app.js'];

export function pendingFiles(uiRoot) {
  const pending = new Set();
  for (const f of new Set([...Object.values(PAGE_FILES), ...SHELL_FILES])) {
    try {
      const first = readFileSync(join(uiRoot, f), 'utf8').replace(/^﻿/, '').split(/\r?\n/, 1)[0];
      if (PENDING_RE.test(first)) pending.add(f);
    } catch (e) { /* missing file: not pending */ }
  }
  return pending;
}

/** Split leaks into failing ones and ones whose region is still marked @i18n-pending. */
export function classifyLeaks(leaks, page, pending, { leakPolicy = 'auto' } = {}) {
  if (leakPolicy === 'hard') return { failing: leaks, pending: [] };
  const shellPending = SHELL_FILES.some(f => pending.has(f));
  const pagePending = pending.has(PAGE_FILES[page]);
  const out = { failing: [], pending: [] };
  for (const l of leaks) (l.in_pages ? pagePending : shellPending) ? out.pending.push(l) : out.failing.push(l);
  return out;
}

// ---------------------------------------------------------------------------------------------
// Fonts (CDP CSS.getPlatformFontsForNode). Probes are appended to the page for the measurement
// and removed again before the screenshot / text tree.
export const FONT_PROBES = {
  ja_JP: { body: 'ノードの状態を確認 設定 直骨次', mono: 'ログ 0123 状態' },
  ru_RU: { body: 'Статус узла: OK, Tesseract 0123', mono: 'Журнал 0123 ok' },
};
const JA_FORBIDDEN = /YaHei|SimSun|NSimSun/i;
const FONT_TARGETS = '__renderFontTargets'; // window property holding the text nodes to measure
let warnedNoTrace = false;

// Runs in the page: appends the probes and collects the text nodes to measure into
// window[FONT_TARGETS] (flat); returns one entry per element: [label, text, first, count].
function fontTargetsInPage({ lang, probes, extraSelector, key }) {
  const box = document.createElement('div');
  box.id = '__render_font_probes';
  box.style.cssText = 'position:fixed;left:0;bottom:0;z-index:2147483647;background:#fff';
  const a = document.createElement('span'); a.textContent = probes.body;
  const b = document.createElement('span'); b.className = 'mono'; b.textContent = probes.mono;
  box.append(a, b);
  document.body.append(box);
  // the "…" of a text-overflow: ellipsis box is drawn in the box's own font and CDP counts it for
  // the text node it cuts (a Russian note cut by a mono log cell read as Segoe + Cascadia): while
  // measuring, ellipses are turned off (a constructed sheet, CSP-safe; removed again afterwards)
  const noEllipsis = new CSSStyleSheet();
  noEllipsis.replaceSync('* { text-overflow: clip !important; }');
  document.adoptedStyleSheets = [...document.adoptedStyleSheets, noEllipsis];
  window[key + 'Sheet'] = noEllipsis;
  const design = window.__TESSERACT_DESIGN__;
  const trace = design && design.build ? !!design.build.trace : !!document.querySelector('[data-i18n-id]');
  const norm = s => s.replace(/\s+/g, ' ').trim();
  const tracedText = new Map(); // normalised T.t string → [id]
  if (trace && window.T && T.i18n && typeof T.i18n.traced === 'function') for (const [s, ids] of Object.entries(T.i18n.traced())) tracedText.set(norm(s), [...(tracedText.get(norm(s)) || []), ...ids]);
  const nodes = [], targets = [];
  const ownNodes = el => [...el.childNodes].filter(c => c.nodeType === 3 && c.nodeValue.trim());
  const add = (label, el, list, via) => {
    targets.push([label, norm(list.map(n => n.nodeValue).join('')).slice(0, 60), nodes.length, list.length, via]);
    nodes.push(...list);
  };
  add('probe-body', a, ownNodes(a), 'probe');
  add('probe-mono', b, ownNodes(b), 'probe');
  const extra = new Set(extraSelector ? document.querySelectorAll(extraSelector) : []);
  const LETTER = /\p{L}/u, KANA = /[぀-ヿ]/, CYR = /[Ѐ-ӿ]/, CJK = /[　-鿿＀-￯]/;
  const scope = lang === 'ja_JP' ? (trace ? 'i18n_trace' : 'kana_fallback') : 'cyrillic';
  // ja_JP: text that declares another language (lang="zh-CN" …) is drawn in that language's font
  // on purpose. (T-P5-07: the language list's own names, settings.app.lang.name.*, lost their id
  // exemption: T.ui.select puts them under translate="no" with their own lang, skipped below.)
  let n = 0, hiddenByContentVisibility = 0, exempt = 0;
  for (const el of document.body.querySelectorAll('*')) {
    if (extra.has(el) || el.closest('#__render_font_probes') || el.closest('[translate="no"]')) continue;
    if (/^(?:script|style|noscript|template)$/i.test(el.localName)) continue;
    const list = ownNodes(el);
    if (!list.length) continue;
    const own = list.map(c => c.nodeValue).join('');
    if (!LETTER.test(own)) continue;
    if (!el.checkVisibility({ opacityProperty: true, visibilityProperty: true })) continue;
    let via = null;
    // the trace scope is a superset of the old kana rule: a T.t string joined with other words (no
    // id, not a whole traced string) is still checked when it shows kana
    if (scope === 'i18n_trace') via = el.closest('[data-i18n-id]') ? 'id' : tracedText.has(norm(own)) ? 'traced' : KANA.test(own) ? 'kana' : null;
    else if (scope === 'kana_fallback') via = KANA.test(own) ? 'kana' : null;
    // mixed with CJK data: not a Russian sentence
    else via = CYR.test(own) && !CJK.test(own) ? 'cyrillic' : null;
    if (!via) continue;
    if (lang === 'ja_JP') {
      const l = el.closest('[lang]');
      if (l && !/^ja(?:-|$)/i.test(l.getAttribute('lang'))) { exempt++; continue; }
    }
    // a subtree skipped by content-visibility: auto (rows of a long virtualised list off screen)
    // is not painted, so no font is measured: skipped and counted
    if (!el.checkVisibility({ opacityProperty: true, visibilityProperty: true, contentVisibilityAuto: true })) { hiddenByContentVisibility++; continue; }
    add('page-' + (n++), el, list, via);
  }
  for (const el of extra) { const list = ownNodes(el); if (list.length) add('extra-' + (n++), el, list, 'extra'); }
  window[key] = nodes;
  return { targets, scope, trace, hiddenByContentVisibility, exempt };
}

/**
 * Returns {scope, note, checked, page_elements, text_nodes, via, exempt, unmeasured_skipped,
 * unmeasured_by, unmeasured, failures:[…], samples:{…}} or null when the language has no font rule.
 */
export async function fontChecks(context, page, lang, { extraSelector = null } = {}) {
  if (lang !== 'ja_JP' && lang !== 'ru_RU') return null;
  const { targets, scope, hiddenByContentVisibility, exempt } = await page.evaluate(fontTargetsInPage, { lang, probes: FONT_PROBES[lang], extraSelector, key: FONT_TARGETS });
  let note = null;
  if (scope === 'kana_fallback') {
    note = 'no --i18n-trace build: the ja_JP rule fell back to elements whose own text has kana (kanji-only catalog text is not checked)';
    if (!warnedNoTrace) { warnedNoTrace = true; console.warn(`fonts: ${note}`); }
  }
  await page.evaluate(() => document.fonts.ready);
  const cdp = await context.newCDPSession(page);
  const failures = [], samples = {}, unmeasured = [], via = {};
  let checked = 0, skipped = 0, textNodes = 0;
  try {
    await cdp.send('DOM.enable');
    await cdp.send('CSS.enable');
    await cdp.send('DOM.getDocument', { depth: 0 });
    // one CDP node per text node: the array's elements → remote objects → DOM node ids
    const { result } = await cdp.send('Runtime.evaluate', { expression: `window.${FONT_TARGETS}`, objectGroup: 'render-fonts' });
    const { result: props } = await cdp.send('Runtime.getProperties', { objectId: result.objectId, ownProperties: true });
    const objectAt = new Map(props.filter(p => /^\d+$/.test(p.name) && p.value && p.value.objectId).map(p => [Number(p.name), p.value.objectId]));
    const fontsOfText = async i => {
      const objectId = objectAt.get(i);
      if (!objectId) return [];
      const { nodeId } = await cdp.send('DOM.requestNode', { objectId });
      if (!nodeId) return [];
      return (await cdp.send('CSS.getPlatformFontsForNode', { nodeId })).fonts;
    };
    for (const [label, text, first, count, how] of targets) {
      const sum = new Map(); // family → {web, glyphs}
      for (let i = first; i < first + count; i++) {
        textNodes++;
        for (const f of await fontsOfText(i)) {
          const s = sum.get(f.familyName) || { web: !!f.isCustomFont, glyphs: 0 };
          s.glyphs += f.glyphCount;
          sum.set(f.familyName, s);
        }
      }
      const fonts = [...sum].map(([family, s]) => ({ family, web: s.web, glyphs: s.glyphs }));
      const names = fonts.filter(f => f.glyphs > 0).map(f => f.family);
      const probe = label.startsWith('probe-');
      if (!names.length) {
        // a probe that measured no font would let both rules pass without checking anything
        if (probe) failures.push({ rule: 'font_unmeasured', label, text, fonts: fonts.map(f => f.family) });
        else { skipped++; if (unmeasured.length < 10) unmeasured.push({ label, text }); }
        continue;
      }
      checked++;
      via[how] = (via[how] || 0) + 1;
      samples[label] = { text, via: how, fonts: fonts.map(f => `${f.family}${f.web ? ' (web)' : ''} ×${f.glyphs}`) };
      if (lang === 'ja_JP' && names.some(n => JA_FORBIDDEN.test(n))) failures.push({ rule: 'ja_no_yahei_simsun', label, text, fonts: names });
      if (lang === 'ru_RU' && new Set(names).size !== 1) failures.push({ rule: 'ru_single_font', label, text, fonts: names });
    }
  } finally {
    await cdp.send('Runtime.releaseObjectGroup', { objectGroup: 'render-fonts' }).catch(() => {});
    await cdp.detach().catch(() => {});
    await page.evaluate(key => {
      const p = document.getElementById('__render_font_probes');
      if (p) p.remove();
      const sheet = window[key + 'Sheet'];
      if (sheet) document.adoptedStyleSheets = document.adoptedStyleSheets.filter(s => s !== sheet);
      delete window[key];
      delete window[key + 'Sheet'];
    }, FONT_TARGETS);
  }
  return { scope, note, checked, page_elements: targets.filter(t => t[4] !== 'probe').length, text_nodes: textNodes, via, exempt,
    unmeasured_skipped: skipped + hiddenByContentVisibility, unmeasured_by: { content_visibility: hiddenByContentVisibility, no_font: skipped }, unmeasured, failures, samples };
}
