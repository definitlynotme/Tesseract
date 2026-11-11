// Pseudo language (T-P4-03; P4 architecture §3.8, critique E3).
//
// Built by `build.mjs --pseudo` as locale-pseudo.<hash>.js (design target, and the app target
// with --dev). Every message is wrapped in ⟦ ⟧ so text that bypasses the catalog stands out;
// ICU arguments, # and tag placeholders are kept as they are (only literal text runs change).
//
// Length model:
//   * from zh_CN (the case for all of P4): every CJK ideograph or full-width symbol becomes
//     ZH_CHARS_PER_CJK (3) accented Latin letters, then ZH_EXTRA (+10 %) padding is added.
//     「启动服务」→ 12 letters + 2 padding (German "Dienst starten" is 14). The factor is
//     sized after the longest languages in DESIGN §8 (German, Russian);
//   * from en_US (when a message has an English translation): letters get accents and the
//     text grows by EN_EXTRA (+40 %).
//   * short-string floor (决定_第五波裁定 10-3, T-P4-H): the factors above are too small for
//     short labels (「停止」→ 6 letters + 1, German "Beenden" is 7, Russian "Остановить" 10).
//     A text of at most SHORT_MAX (2) translatable characters counts SHORT_PER_CHAR (5) letters
//     per character (a CJK one becomes 5 letters instead of 3; padding makes up the rest), and
//     every pseudo text is at least MIN_GROWTH (4) letters longer than its translatable source.
//     「停止」→ 10 letters + 1 padding; "OK" (en base) → 2 letters + 8 padding; "Stop" → 4 + 4.
//   Translatable characters (T-P4-H fix round 1) are the literal text of the message, ICU
//   arguments, # and tags excluded:
//     - zh_CN base: only the CJK characters (the set below). Latin text in a zh_CN message is a
//       unit, protocol field or product token that every language keeps (" ms", "KB", "/s",
//       "TUN", "SNI"), so it is accented 1:1 and gets the +10 % padding but no floor;
//     - en_US base: every character.
//   Select / plural: the floors are judged per branch. A message whose top level holds exactly
//   one select or plural is measured once per branch (the text outside it plus that branch, along
//   its shortest nested path) and each branch carries its own padding at its end, so a short
//   state word chosen by a select (「系统」 next to "TUN") gets the short-string floor. A message
//   with two or more top-level selects / plurals keeps one padding at the end, sized on its
//   shortest path (known approximation; nothing in the zh_CN catalog needs more).

export const PSEUDO = Object.freeze({
  OPEN: '⟦',
  CLOSE: '⟧',
  ZH_CHARS_PER_CJK: 3,
  ZH_EXTRA: 0.10,
  EN_EXTRA: 0.40,
  PAD: '~',
  SHORT_MAX: 2,
  SHORT_PER_CHAR: 5,
  MIN_GROWTH: 4,
});

// CJK ideographs (incl. ext. A, compatibility, ext. B+), CJK symbols and punctuation,
// half/full-width forms — the same set as the cjk scan rule.
const span = (a, b) => String.fromCodePoint(a) + '-' + String.fromCodePoint(b);
// built from code points so this file stays free of CJK literals (cjk scan rule)
const CJK = new RegExp('[' + span(0x3000, 0x303f) + span(0x3400, 0x4dbf) + span(0x4e00, 0x9fff) + span(0xf900, 0xfaff)
  + span(0xff00, 0xffef) + span(0x20000, 0x2ffff) + ']', 'u');
const LOWER = 'abcdefghijklmnopqrstuvwxyz';
const ACCENT_LOWER = 'àƀçđéƒĝĥîĵķĺɱñöþǫŕšţûṽŵẋýž';
const ACCENT_UPPER = 'ÀƁÇĐÉƑĜĤÎĴĶĹḾÑÖÞǪŔŠŢÛṼŴẊÝŽ';

function accent(ch) {
  const lo = LOWER.indexOf(ch);
  if (lo >= 0) return ACCENT_LOWER[lo];
  const up = LOWER.indexOf(ch.toLowerCase());
  if (up >= 0 && ch !== ch.toLowerCase()) return ACCENT_UPPER[up];
  return ch;
}

/** One literal text run → [pseudo text, source weight for the padding]. A CJK character
 *  becomes `perCjk` letters (ZH_CHARS_PER_CJK, or SHORT_PER_CHAR for a short message). */
export function pseudoText(text, base, perCjk = PSEUDO.ZH_CHARS_PER_CJK) {
  let out = '', weight = 0;
  for (const ch of String(text)) {
    if (base === 'zh' && CJK.test(ch)) {
      const cp = ch.codePointAt(0);
      for (let k = 0; k < perCjk; k++) out += ACCENT_LOWER[(cp + k * 7) % 26];
      weight += perCjk;
    } else {
      out += accent(ch);
      weight += 1;
    }
  }
  return [out, weight];
}

/** Translatable characters of one literal run (see the file header). */
function translatable(text, base) {
  let n = 0;
  for (const ch of String(text)) if (base !== 'zh' || CJK.test(ch)) n++;
  return n;
}

const branchesOf = part => (part[0] === 'p' ? part[3] : part[0] === 's' ? part[2] : null);

/** Measure f over the literal runs of a compiled message along its shortest path (a select /
 *  plural contributes its smallest branch; tags are walked into). */
function shortest(msg, f) {
  if (typeof msg === 'string') return f(msg);
  if (!Array.isArray(msg)) return 0;
  let n = 0;
  for (const part of msg) {
    if (typeof part === 'string') { n += f(part); continue; }
    if (!Array.isArray(part)) continue;
    const br = branchesOf(part);
    if (br) { const v = Object.values(br).map(b => shortest(b, f)); n += v.length ? Math.min(...v) : 0; }
    else if (part[0] === 'g') n += shortest(part[2], f);
  }
  return n;
}

/** Translatable source characters of a compiled message along its shortest path (arguments,
 *  # and tags excluded; Latin text excluded for a zh_CN base). */
export function sourceLength(msg, base = 'zh') {
  return shortest(msg, s => translatable(s, base));
}

const letters = msg => shortest(msg, s => [...s].length);
const isShort = n => n > 0 && n <= PSEUDO.SHORT_MAX;
const perCjkFor = n => (isShort(n) ? PSEUDO.SHORT_PER_CHAR : PSEUDO.ZH_CHARS_PER_CJK);
const extraOf = (weight, base) => Math.ceil(weight * (base === 'zh' ? PSEUDO.ZH_EXTRA : PSEUDO.EN_EXTRA));
// letters the floors ask of n translatable characters (0 when there is nothing to translate)
const floorOf = n => (n > 0 ? Math.max(n + PSEUDO.MIN_GROWTH, isShort(n) ? n * PSEUDO.SHORT_PER_CHAR : 0) : 0);
// letters n translatable characters actually become at perCjk letters each
const grownOf = (n, base, perCjk) => n * (base === 'zh' ? perCjk : 1);
const padOf = (weight, base, n, perCjk) => PSEUDO.PAD.repeat(Math.max(extraOf(weight, base), floorOf(n) - grownOf(n, base, perCjk)));
const appendPad = (msg, pad) => (!pad ? msg : typeof msg === 'string' ? msg + pad : [...msg, pad]);

function transform(msg, base, perCjk) {
  let weight = 0;
  const run = s => { const [t, w] = pseudoText(s, base, perCjk); weight += w; return t; };
  const walk = m => {
    if (typeof m === 'string') return run(m);
    return m.map(part => {
      if (typeof part === 'string') return run(part);
      switch (part[0]) {
        case 'p': return ['p', part[1], part[2], Object.fromEntries(Object.entries(part[3]).map(([k, v]) => [k, walk(v)]))];
        case 's': return ['s', part[1], Object.fromEntries(Object.entries(part[2]).map(([k, v]) => [k, walk(v)]))];
        case 'g': return ['g', part[1], walk(part[2])];
        default: return part;
      }
    });
  };
  return [walk(msg), weight];
}

/** Exactly one top-level select / plural: every branch is measured with the text around it and
 *  padded on its own (see the file header). */
function perBranch(parts, at, base) {
  const sel = parts[at];
  const br = branchesOf(sel);
  const outer = parts.filter((_, i) => i !== at);
  const nOut = sourceLength(outer, base);
  const nOf = Object.fromEntries(Object.entries(br).map(([k, v]) => [k, nOut + sourceLength(v, base)]));
  // the shared text grows like the shortest branch path that has something to translate
  const paths = Object.values(nOf).filter(n => n > 0);
  const perOut = perCjkFor(paths.length ? Math.min(...paths) : 0);
  const [outBody] = transform(outer, base, perOut);
  const wOut = letters(outBody);
  const branches = {};
  for (const [k, v] of Object.entries(br)) {
    const n = nOf[k], perB = perCjkFor(n);
    const [body] = transform(v, base, perB);
    const grown = grownOf(nOut, base, perOut) + grownOf(n - nOut, base, perB);
    const pad = PSEUDO.PAD.repeat(Math.max(extraOf(wOut + letters(body), base), floorOf(n) - grown));
    branches[k] = appendPad(body, pad);
  }
  const out = [...outBody];
  out.splice(at, 0, sel[0] === 'p' ? ['p', sel[1], sel[2], branches] : ['s', sel[1], branches]);
  return out;
}

/** Pseudo-localise one compiled message; base is 'zh' (zh_CN source) or 'en' (en_US). */
export function pseudoMessage(msg, base = 'zh') {
  if (Array.isArray(msg)) {
    const at = msg.map((p, i) => (Array.isArray(p) && branchesOf(p) ? i : -1)).filter(i => i >= 0);
    if (at.length === 1) return [PSEUDO.OPEN, ...perBranch(msg, at[0], base), PSEUDO.CLOSE];
  }
  const n = sourceLength(msg, base);
  const perCjk = perCjkFor(n);
  const [body, weight] = transform(msg, base, perCjk);
  // weight = letters of the pseudo body (every branch); the padding brings the total up to the floors
  const pad = padOf(weight, base, n, perCjk);
  const parts = typeof body === 'string' ? [body] : body;
  return [PSEUDO.OPEN, ...parts, pad + PSEUDO.CLOSE];
}

/* Ids that are never pseudo-localised (T-P5-04): the language list's own names of the languages,
   settings.app.lang.name.<code>. Their zh_CN _desc: every catalog writes each language by its own
   name, untranslated (Simplified Chinese keeps its Chinese name in all of them), so the pseudo language shows them
   as they are, like any real language does. */
export const PSEUDO_KEEP = /^settings\.app\.lang\.name\.[a-z]{2}_[a-z]{2}$/;

/** {id: msg} for every zh_CN id; English text is the base where it exists. */
export function pseudoCatalog(zh, en = {}) {
  const out = {};
  for (const id of Object.keys(zh).sort()) {
    const hasEn = Object.prototype.hasOwnProperty.call(en, id);
    out[id] = PSEUDO_KEEP.test(id) ? (hasEn ? en[id] : zh[id]) : pseudoMessage(hasEn ? en[id] : zh[id], hasEn ? 'en' : 'zh');
  }
  return out;
}
