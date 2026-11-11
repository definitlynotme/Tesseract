// Source and bundle scanners for the P4 gates (T-P4-01; P4 architecture §3.9, §5, §6, §7, §9).
//
//   node tools/scan.mjs [--rule cjk|demo|inject|style|features|all|<a,b,...>]
//                       [--mode report|enforce] [--json] [--root <ui dir>] [--dist <dir>]
//
// Each rule's mode comes from tools/scan_gates.json unless --mode overrides it.
// report: print counts, exit 0.  enforce: exit 1 when the rule has findings, or when it
// could not run (demo: no bundle / no build-manifest.json under --dist).
// The functions below are exported for tests and for build.mjs --check.
import { readFileSync, existsSync, readdirSync, statSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { join, relative, resolve, dirname, isAbsolute, sep } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import * as acorn from 'acorn';
import * as walk from 'acorn-walk';

export const RULES = ['cjk', 'demo', 'inject', 'style', 'features'];
export const UI_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');

// CJK unified ideographs (incl. ext. A, compatibility, ext. B+), CJK symbols and
// punctuation, half/full-width forms (architecture §3.9).
const CJK_RE = /[　-〿㐀-䶿一-鿿豈-﫿＀-￯]|[\u{20000}-\u{2FFFF}]/u;
export const hasCjk = s => CJK_RE.test(String(s));

const posix = p => p.split(sep).join('/');
const read = p => readFileSync(p, 'utf8').replace(/^﻿/, '');

export function lineDigest(line) {
  return createHash('sha256').update(String(line).trim(), 'utf8').digest('hex').slice(0, 16);
}

function walkFiles(dir, pred, out = []) {
  if (!existsSync(dir)) return out;
  for (const name of readdirSync(dir).sort()) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) walkFiles(p, pred, out);
    else if (pred(p)) out.push(p);
  }
  return out;
}

function parseJs(source) {
  return acorn.parse(source, { ecmaVersion: 'latest', sourceType: 'module', locations: true, allowHashBang: true });
}

function snippet(s, n = 60) {
  const t = String(s).replace(/\s+/g, ' ').trim();
  return t.length > n ? t.slice(0, n) + '…' : t;
}

// ---- @i18n-pending (first line only; JS, HTML and CSS comment forms) -------------
export function isPending(source, kind) {
  const first = String(source).replace(/^﻿/, '').split('\n', 1)[0].trim();
  if (kind === 'js') return /^\/\/\s*@i18n-pending\b/.test(first) || /^\/\*\s*@i18n-pending\s*\*\/$/.test(first);
  if (kind === 'html') return /^<!--\s*@i18n-pending\s*-->$/.test(first);
  if (kind === 'css') return /^\/\*\s*@i18n-pending\s*\*\/$/.test(first);
  return false;
}

// ---- cjk -------------------------------------------------------------------------
// String literals, template strings and regex literals; comments are not checked.
export function scanCjkJs(source, file = '<js>') {
  const out = [];
  walk.full(parseJs(source), node => {
    if (node.type === 'Literal') {
      const text = node.regex ? node.regex.pattern : node.value;
      if ((typeof node.value === 'string' || node.regex) && hasCjk(text)) {
        out.push({ rule: 'cjk', file, line: node.loc.start.line, col: node.loc.start.column + 1,
          kind: node.regex ? 'regex' : 'string', text: snippet(text) });
      }
    } else if (node.type === 'TemplateLiteral') {
      const text = node.quasis.map(q => q.value.cooked ?? q.value.raw).join('${}');
      if (hasCjk(text)) {
        out.push({ rule: 'cjk', file, line: node.loc.start.line, col: node.loc.start.column + 1, kind: 'template', text: snippet(text) });
      }
    }
  });
  return out;
}

// Replace a matched span with spaces, keeping newlines so line numbers survive.
const blank = m => m.replace(/[^\n]/g, ' ');
const lineAt = (s, i) => s.slice(0, i).split('\n').length;

// index.html: text nodes, plus the user-visible attributes aria-label / title /
// placeholder / alt (kind 'attr'). Comments, <script> and <style> bodies are skipped.
export function scanCjkHtml(source, file = '<html>') {
  const s = String(source).replace(/<!--[\s\S]*?-->/g, blank)
    .replace(/<script\b[\s\S]*?<\/script>/gi, blank).replace(/<style\b[\s\S]*?<\/style>/gi, blank);
  const out = [];
  for (const m of s.matchAll(/>([^<]+)</g)) {
    if (hasCjk(m[1])) out.push({ rule: 'cjk', file, line: lineAt(s, m.index + 1), col: 1, kind: 'text', text: snippet(m[1]) });
  }
  for (const m of s.matchAll(/\s(aria-label|title|placeholder|alt)\s*=\s*("([^"]*)"|'([^']*)')/gi)) {
    const v = m[3] ?? m[4] ?? '';
    if (hasCjk(v)) out.push({ rule: 'cjk', file, line: lineAt(s, m.index + 1), col: 1, kind: 'attr', text: `${m[1]}=${snippet(v)}` });
  }
  return out.sort((a, b) => a.line - b.line);
}

// CSS: the value of every `content:` declaration (comments skipped).
export function scanCjkCss(source, file = '<css>') {
  const s = String(source).replace(/\/\*[\s\S]*?\*\//g, blank);
  const out = [];
  for (const m of s.matchAll(/(^|[;{\s])content\s*:\s*([^;}]*)/g)) {
    if (hasCjk(m[2])) out.push({ rule: 'cjk', file, line: lineAt(s, m.index + m[1].length), col: 1, kind: 'css-content', text: snippet(m[2]) });
  }
  return out;
}

// ---- inject (architecture §7 lint rules 1–4, plus setAttribute('on…')) ------------
// T-P4-07: a hard gate. Besides the direct forms (T.h(…), T.ui.popover(…), M.swapText(…),
// T.raw(…)) the helpers are followed through the names they are bound to in the same file
// (file-wide, no scope analysis): `const { h } = T`, `const { h: mk } = T`, `const h = T.h`,
// `const U = T.ui; U.popover(…)`, `const { ui: { popover } } = window.T`, `const R = T.raw`
// (决定_第三波裁定 T-P4-01 item 6). An `html` key is looked for anywhere in the arguments
// (`swapText(el, x, Object.assign({ html: true }, o))`), not inside function bodies.
// T-P4-07 fix round 1: T.html itself is followed too (its aliases, .call / .apply / .bind, and T.html
// handed over as a value, e.g. Reflect.apply(T.html, …) or arr.map(T.html)): T.html is only ever a
// template tag, any call form is an html-sink (the runtime throws on it as well). T.icon is followed
// so that T.raw(I(…)) counts as an icon only when I is bound to T.icon in that file (directly or as
// a one-call wrapper `(n, s) => T.icon(n, s)`). Names bound to `document` (`const doc = document`)
// are followed for document.write / writeln.
const HELPERS = new Set(['h', 'notify', 'drawer', 'sheet', 'popover']);
const ALIASABLE = new Set([...HELPERS, 'swapText', 'raw', 'html', 'icon']);
const GLOBAL_OBJECTS = new Set(['window', 'globalThis', 'self']);
const TEXT_KEYS = new Set(['content', 'head', 'message']);

const isPlainString = n => (n.type === 'Literal' && typeof n.value === 'string')
  || (n.type === 'TemplateLiteral' && n.expressions.length === 0);
const isInterpolatedString = n => (n.type === 'TemplateLiteral' && n.expressions.length > 0)
  || (n.type === 'BinaryExpression' && n.operator === '+' && (isStringish(n.left) || isStringish(n.right)));
const isStringish = n => isPlainString(n) || isInterpolatedString(n);
const propName = n => (n && n.type === 'MemberExpression'
  ? (n.computed ? (n.property.type === 'Literal' ? String(n.property.value) : null) : n.property.name) : null);
const keyName = p => (p.type === 'Property' ? (p.key.type === 'Identifier' && !p.computed ? p.key.name
  : (p.key.type === 'Literal' ? String(p.key.value) : null)) : null);
function rootName(n) {
  while (n && n.type === 'MemberExpression') n = n.object;
  return n && n.type === 'Identifier' ? n.name : null;
}
// the member chain of n from its base: window.T.ui → ['window', 'T', 'ui'] (null when not a plain chain)
function chainOf(n) {
  const out = [];
  while (n && n.type === 'MemberExpression') { const p = propName(n); if (p === null) return null; out.unshift(p); n = n.object; }
  if (!n || n.type !== 'Identifier') return null;
  out.unshift(n.name);
  return out;
}

// Names bound to T (or a member of it) and names bound to one of the helpers, collected to a
// fixed point so an alias of an alias counts too.
export function collectInjectAliases(ast) {
  const roots = new Set(['T']);
  const fns = new Map(); // local name → helper ('h', 'notify', 'drawer', 'sheet', 'popover', 'swapText', 'raw', 'html', 'icon')
  const tRooted = n => {
    const c = chainOf(n);
    if (!c) return false;
    if (roots.has(c[0])) return true;
    return GLOBAL_OBJECTS.has(c[0]) && c[1] === 'T';
  };
  let changed = true;
  const addRoot = name => { if (!roots.has(name)) { roots.add(name); changed = true; } };
  const addFn = (name, helper) => { if (fns.get(name) !== helper) { fns.set(name, helper); changed = true; } };
  const bindPattern = (pat) => {
    // pat is destructured from a T-rooted object
    if (!pat || pat.type !== 'ObjectPattern') return;
    for (const prop of pat.properties) {
      if (prop.type !== 'Property') continue;
      const k = keyName(prop);
      let v = prop.value;
      if (v && v.type === 'AssignmentPattern') v = v.left;
      if (!v) continue;
      if (v.type === 'ObjectPattern') { bindPattern(v); continue; }
      if (v.type !== 'Identifier') continue;
      addRoot(v.name);
      if (k && ALIASABLE.has(k)) addFn(v.name, k);
    }
  };
  const docs = new Set(['document']);
  const addDoc = name => { if (!docs.has(name)) { docs.add(name); changed = true; } };
  const isDoc = n => { const c = chainOf(n); return !!c && ((c.length === 1 && docs.has(c[0])) || (c.length === 2 && GLOBAL_OBJECTS.has(c[0]) && c[1] === 'document')); };
  const isIconRef = n => n && n.type === 'MemberExpression' && propName(n) === 'icon' && tRooted(n.object);
  // (n, s, c) => T.icon(n, s || 16, c), or a function whose body is `return T.icon(…)`
  const iconWrapper = f => {
    if (!f || (f.type !== 'ArrowFunctionExpression' && f.type !== 'FunctionExpression')) return false;
    let b = f.body;
    if (b.type === 'BlockStatement') { if (b.body.length !== 1 || b.body[0].type !== 'ReturnStatement') return false; b = b.body[0].argument; }
    return !!b && b.type === 'CallExpression' && (isIconRef(b.callee) || (b.callee.type === 'Identifier' && fns.get(b.callee.name) === 'icon'));
  };
  const bind = (target, init) => {
    if (!target || !init) return;
    if (target.type === 'Identifier' && isDoc(init)) { addDoc(target.name); return; }
    if (target.type === 'Identifier' && iconWrapper(init)) { addFn(target.name, 'icon'); return; }
    if (!tRooted(init)) {
      // const sw = M.swapText (any object): swapText is ours whatever it hangs on
      if (target.type === 'Identifier' && propName(init) === 'swapText') addFn(target.name, 'swapText');
      return;
    }
    if (target.type === 'ObjectPattern') { bindPattern(target); return; }
    if (target.type !== 'Identifier') return;
    addRoot(target.name);
    const p = init.type === 'MemberExpression' ? propName(init) : null;
    if (p && ALIASABLE.has(p)) addFn(target.name, p);
  };
  while (changed) {
    changed = false;
    walk.full(ast, node => {
      if (node.type === 'VariableDeclarator') bind(node.id, node.init);
      else if (node.type === 'AssignmentExpression' && node.operator === '=') bind(node.left, node.right);
    });
  }
  return { roots, fns, tRooted, docs };
}

// Object literals anywhere in an argument, not inside a function body.
function objectsIn(n, out = []) {
  if (!n || typeof n.type !== 'string') return out;
  if (n.type === 'ArrowFunctionExpression' || n.type === 'FunctionExpression') return out;
  if (n.type === 'ObjectExpression') out.push(n);
  for (const k of Object.keys(n)) {
    if (k === 'loc' || k === 'start' || k === 'end') continue;
    const v = n[k];
    if (Array.isArray(v)) { for (const x of v) if (x && typeof x.type === 'string') objectsIn(x, out); }
    else if (v && typeof v.type === 'string') objectsIn(v, out);
  }
  return out;
}

export function scanInjectJs(source, file = '<js>') {
  const out = [];
  const add = (node, check, text) => out.push({ rule: 'inject', file, line: node.loc.start.line, col: node.loc.start.column + 1, check, text });
  const ast = parseJs(source);
  const { fns, tRooted, docs } = collectInjectAliases(ast);
  // "T.h", "T.ui.drawer", "M.swapText", "swapText", "T.html", "T.icon", an alias of one of them →
  // the helper's name
  const helperName = callee => {
    if (!callee) return null;
    if (callee.type === 'Identifier') {
      if (callee.name === 'swapText') return 'swapText';
      const k = fns.get(callee.name);
      return k ? (k === 'swapText' ? 'swapText' : 'T.' + k) : null;
    }
    if (callee.type !== 'MemberExpression') return null;
    const p = propName(callee);
    if (p === 'swapText') return 'swapText';
    if ((HELPERS.has(p) || p === 'raw' || p === 'html' || p === 'icon') && tRooted(callee.object)) return 'T.' + p;
    return null;
  };
  const isIconCall = n => n.type === 'CallExpression' && helperName(n.callee) === 'T.icon';
  const isFnLiteral = n => n && (n.type === 'ArrowFunctionExpression' || n.type === 'FunctionExpression');
  const HTML_PROPS = new Set(['innerHTML', 'outerHTML', 'srcdoc']);
  const HTML_CALLS = new Set(['createContextualFragment', 'parseFromString', 'setHTMLUnsafe', 'setHTML', 'parseHTMLUnsafe']);
  // document.write / doc.write (doc bound to document in this file) / window.document.write
  const isDocWrite = c => {
    const ch = chainOf(c);
    if (!ch || (ch[ch.length - 1] !== 'write' && ch[ch.length - 1] !== 'writeln')) return false;
    return (ch.length === 2 && docs.has(ch[0])) || (ch.length === 3 && GLOBAL_OBJECTS.has(ch[0]) && ch[1] === 'document');
  };
  const chainIs = (n, ...names) => { const ch = chainOf(n); return !!ch && ch.length === names.length && ch.every((x, i) => x === names[i]); };
  walk.full(ast, node => {
    if (node.type === 'AssignmentExpression') {
      const p = propName(node.left);
      if (HTML_PROPS.has(p) && !isPlainString(node.right)) add(node, 'html-sink', `${p} = <dynamic>`);
    } else if (node.type === 'CallExpression' || node.type === 'NewExpression') {
      const c = node.callee;
      const p = propName(c);
      const args = node.arguments;
      // eval / Function, also reached through a global object (window.eval, globalThis.Function, self[…]).
      const evalName = c.type === 'Identifier' ? c.name
        : (GLOBAL_OBJECTS.has(rootName(c)) && c.object.type === 'Identifier' ? p : null);
      // T.html is a template tag only: called as a function, through .call / .apply / .bind, or
      // handed over as a value (Reflect.apply(T.html, …), arr.map(T.html)) it is an html-sink
      if (helperName(c) === 'T.html') add(node, 'html-sink', 'T.html(<not a tagged template>)');
      else if ((p === 'call' || p === 'apply' || p === 'bind') && helperName(c.object) === 'T.html') add(node, 'html-sink', `T.html.${p}(…)`);
      for (const a of args) if ((a.type === 'Identifier' || a.type === 'MemberExpression') && helperName(a) === 'T.html') add(a, 'html-sink', 'T.html passed as a value');
      if (node.type === 'NewExpression') {
        if (evalName === 'Function') add(node, 'eval', 'new Function');
        return;
      }
      if (evalName === 'eval' || evalName === 'Function') add(node, 'eval', evalName + '()');
      if (p === 'insertAdjacentHTML' && args[1] && !isPlainString(args[1])) add(node, 'html-sink', 'insertAdjacentHTML(<dynamic>)');
      if (HTML_CALLS.has(p) && args[0] && !isPlainString(args[0])) add(node, 'html-sink', `${p}(<dynamic>)`);
      if (isDocWrite(c) && args.some(a => !isPlainString(a))) add(node, 'html-sink', `document.${p}(<dynamic>)`);
      // Object.assign(el, { innerHTML: x }) / Reflect.set(el, 'innerHTML', x)
      if (chainIs(c, 'Object', 'assign')) {
        for (const a of args.slice(1)) {
          if (a.type !== 'ObjectExpression') continue;
          for (const prop of a.properties) { const k = keyName(prop); if (HTML_PROPS.has(k) && !isPlainString(prop.value)) add(prop, 'html-sink', `Object.assign(…, {${k}: <dynamic>})`); }
        }
      }
      if (chainIs(c, 'Reflect', 'set') && args[1] && args[1].type === 'Literal' && HTML_PROPS.has(String(args[1].value)) && args[2] && !isPlainString(args[2])) add(node, 'html-sink', `Reflect.set(…, '${args[1].value}', <dynamic>)`);
      const timer = c.type === 'Identifier' ? c.name : p;
      if ((timer === 'setTimeout' || timer === 'setInterval') && args[0] && isStringish(args[0])) add(node, 'eval', `${timer}(<string>)`);
      if (p === 'setAttribute' && args[0] && args[0].type === 'Literal' && /^(on|srcdoc$)/i.test(String(args[0].value))) add(node, 'on-attr', `setAttribute('${args[0].value}')`);
      const h = helperName(c);
      if (h === 'T.html' || h === 'T.icon') return;
      if (h === 'T.raw') {
        const a = args[0];
        if (a && !isPlainString(a) && !isIconCall(a)) add(node, 'raw', 'T.raw(<dynamic>)');
        return;
      }
      // T.h(tag, {onclick: s}) / {srcdoc: s}: T.h throws on an on* attribute that is not a function
      // and on srcdoc (T-P4-07 fix round 1); the lint reports an on* value that is not a function
      // literal, and any srcdoc
      if (h === 'T.h' && args[1] && args[1].type === 'ObjectExpression') {
        for (const prop of args[1].properties) {
          const k = keyName(prop);
          if (k === null || k === 'on') continue;
          if (/^srcdoc$/i.test(k)) add(prop, 'html-sink', `T.h({${k}})`);
          else if (/^on/i.test(k) && !isFnLiteral(prop.value)) add(prop, 'on-attr', `T.h({${k}: <not a function>})`);
        }
      }
      // T.popover(anchor, content): a string content is text since T-P4-07; an interpolated one
      // is still reported (architecture §7 rule 2)
      if (h === 'T.popover' && args[1] && isInterpolatedString(args[1])) add(args[1], 'helper-string', 'T.popover(<anchor>, <interpolated>)');
      if (h) {
        for (const a of args) {
          for (const obj of objectsIn(a)) {
            const direct = obj === a;
            for (const prop of obj.properties) {
              const k = keyName(prop);
              if (k === 'html') add(prop, 'helper-html', `${h}({html})`);
              else if (direct && TEXT_KEYS.has(k) && isInterpolatedString(prop.value)) add(prop, 'helper-string', `${h}({${k}: <interpolated>})`);
            }
          }
        }
      }
    }
  });
  return out;
}

// ---- style (CSP: style="…" in templates/strings, setAttribute('style')) -----------
// T-P4-08b (enforce; CSP style-src 'self', P4 architecture §9): a style attribute in markup
// strings (any case, quoted or not: `STYLE=x` parses as one too), setAttribute / setAttributeNS
// with a constant 'style' name, an Attr named 'style' (createAttribute / createAttributeNS, the
// setAttributeNode / setNamedItem path), and creating a <style> element (createElement /
// createElementNS('…', 'style')). Styles go through the CSSOM (el.style.cssText / setProperty,
// el.animate) or a class. A dynamic attribute name (setAttribute(k, v), 'sty' + 'le') cannot be
// judged here: the --csp strict render run catches what it does at run time.
export function scanStyleJs(source, file = '<js>') {
  const out = [];
  const add = (node, check, text) => out.push({ rule: 'style', file, line: node.loc.start.line, col: node.loc.start.column + 1, check, text });
  const STYLE_ATTR = /(^|[\s"'<\/])style\s*=/i;
  // a constant string argument: a literal or a template without expressions
  const constArg = a => (a && a.type === 'Literal' && typeof a.value === 'string' ? a.value
    : a && a.type === 'TemplateLiteral' && a.expressions.length === 0 ? (a.quasis[0].value.cooked ?? a.quasis[0].value.raw) : null);
  const isStyle = a => { const v = constArg(a); return v !== null && v.trim().toLowerCase() === 'style'; };
  walk.full(parseJs(source), node => {
    if (node.type === 'Literal' && typeof node.value === 'string' && STYLE_ATTR.test(node.value)) add(node, 'style-attr', snippet(node.value));
    else if (node.type === 'TemplateElement' && STYLE_ATTR.test(node.value.cooked ?? node.value.raw)) add(node, 'style-attr', snippet(node.value.raw));
    else if (node.type === 'CallExpression') {
      const name = propName(node.callee);
      if (name === 'setAttribute' && isStyle(node.arguments[0])) add(node, 'set-style', "setAttribute('style')");
      else if (name === 'setAttributeNS' && isStyle(node.arguments[1])) add(node, 'set-style', "setAttributeNS(…, 'style')");
      // (fix round 1, S6: an Attr node named style, attached by setAttributeNode / setNamedItem)
      else if (name === 'createAttribute' && isStyle(node.arguments[0])) add(node, 'set-style', "createAttribute('style')");
      else if (name === 'createAttributeNS' && isStyle(node.arguments[1])) add(node, 'set-style', "createAttributeNS(…, 'style')");
      else if (name === 'createElement' && isStyle(node.arguments[0])) add(node, 'style-element', "createElement('style')");
      else if (name === 'createElementNS' && isStyle(node.arguments[1])) add(node, 'style-element', "createElementNS(…, 'style')");
    }
  });
  return out;
}

export function scanStyleHtml(source, file = '<html>') {
  const s = String(source).replace(/<!--[\s\S]*?-->/g, blank);
  const out = [];
  for (const m of s.matchAll(/<[a-zA-Z][^>]*[\s\/]style\s*=/gi)) out.push({ rule: 'style', file, line: lineAt(s, m.index), col: 1, check: 'style-attr', text: snippet(m[0]) });
  for (const m of s.matchAll(/<style\b/gi)) out.push({ rule: 'style', file, line: lineAt(s, m.index), col: 1, check: 'style-element', text: '<style>' });
  return out;
}

// ---- demo (bundle markers + sources, architecture §6) -----------------------------
// T-P4-07: a hard gate in three parts.
//   bundle   the markers of tools/demo_markers.json in the app bundle's top-level *.js / *.css;
//   sources  the `source` markers (T.sim, T.D, sim:, window.demo, scenario(, @i18n-pending) and
//            the demo class tokens in what the app target is built from: every *.js under
//            ui/src and src/index.html (comments included) except src/design/ and
//            src/i18n/locales/, every *.css under ui/css except css/design.css (class tokens as
//            selectors, comments skipped; css_text such as @i18n-pending anywhere);
//   imports  the app entry's static import graph (src/entry/app.js on) reaches no file under
//            src/design/ (the app target imports nothing from design/).
// Fix round 1: JS is matched after decoding \uXXXX escapes (decodeJsEscapes; a locale chunk's
// catalog is a string inside a string); message_ids are id prefixes (overview.demo., shell.demo.);
// the design-only hook files outside src/design/ (source.design_only, each with a reason) are
// exempt from the sources part as long as the app entry does not reach them; require('…') is an
// import.
// loadDemoMarkers(root) records the tree it read the lists from (a non-enumerable `root`), and
// scanDemoFiles(files, markers) then adds the sources and imports parts of that tree, so the
// in-memory gate of build.mjs --check (demoFindings → scanDemoFiles) and build-manifest.demo_free
// cover all three. Marker objects built by hand (no root) check the given files only.
export function loadDemoMarkers(root = UI_ROOT) {
  const markers = JSON.parse(read(join(root, 'tools', 'demo_markers.json')));
  Object.defineProperty(markers, 'root', { value: root, enumerable: false });
  return markers;
}

const escRe = s => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
// JS escapes decoded before matching (T-P4-07 fix round 1): a minified bundle writes non-ASCII as
// \uXXXX, and a locale chunk carries its catalog as a string inside a string (\\uXXXX), so a CJK
// marker such as 原型演示 or a message id is only visible after decoding \+uXXXX and \+u{…}.
// A decoded line break is left escaped so line numbers stay those of the file.
export function decodeJsEscapes(text) {
  const keep = (m, cp) => (cp === 0x0a || cp === 0x0d || cp === 0x2028 || cp === 0x2029 || cp > 0x10ffff ? m : String.fromCodePoint(cp));
  return String(text)
    .replace(/\\+u\{([0-9a-fA-F]{1,6})\}/g, (m, h) => keep(m, parseInt(h, 16)))
    .replace(/\\+u([0-9a-fA-F]{4})/g, (m, h) => keep(m, parseInt(h, 16)));
}
// Returns findings for one bundle file (JS or CSS text). JS is matched after decodeJsEscapes;
// build.mjs --check (demoFindings → scanDemoFiles) and scan.mjs --rule demo both come here.
export function scanDemoText(text, file, markers, kind = file.endsWith('.css') ? 'css' : 'js') {
  const out = [];
  if (kind === 'js') text = decodeJsEscapes(text);
  const count = (re, marker) => {
    let n = 0, firstLine = 0;
    for (const m of String(text).matchAll(re)) { if (!n) firstLine = lineAt(text, m.index); n++; }
    if (n) out.push({ rule: 'demo', file, line: firstLine, col: 1, marker, count: n });
  };
  if (kind === 'js') {
    for (const t of markers.text || []) count(new RegExp(escRe(t), 'g'), t);
    for (const r of markers.regex || []) count(new RegExp(r, 'g'), `/${r}/`);
    for (const id of markers.message_ids || []) count(new RegExp(escRe(id), 'g'), id);
  } else {
    for (const t of markers.css_text || []) count(new RegExp(escRe(t), 'g'), t);
  }
  for (const c of markers.css_classes || []) {
    // CSS: .cls and #id selectors, and [id=…] / [class~=…] / [class*=…] attribute selectors
    // (ov-demo and logs-demo are element ids). JS: the name as a whole token.
    const e = escRe(c);
    const re = kind === 'css'
      ? new RegExp(`[.#]${e}(?![\\w-])|\\[\\s*(?:id|class)\\s*[~*|^$]?=\\s*["']?${e}(?![\\w-])`, 'g')
      : new RegExp(`(?<![\\w-])${e}(?![\\w-])`, 'g');
    count(re, (kind === 'css' ? '.' : '') + c);
  }
  return out;
}

// The sources part: findings carry check 'source' and the file path relative to ui/.
export function scanDemoSources(root = UI_ROOT, markers = loadDemoMarkers(root)) {
  const src = markers.source || {};
  const jsMarkers = { text: src.text || [], regex: src.regex || [], css_classes: markers.css_classes || [] };
  const cssClasses = { css_classes: markers.css_classes || [] };
  const cssText = { css_text: [...new Set([...(markers.css_text || []), ...(src.text || []).filter(t => t.startsWith('@'))])] };
  const rel = p => posix(relative(root, p));
  // design-only hook files outside src/design/ (boot/*-design.js, boot/design.js, entry/design.js:
  // P4 architecture §1.1.1, only the design entry imports them) are exempt by name, each with its
  // reason (T-P4-07 fix round 1); an exempt file that the app entry reaches, or an entry without
  // a reason, is a finding.
  const out = [];
  const designOnly = new Map();
  for (const e of src.design_only || []) {
    const f = e && typeof e.file === 'string' ? e.file : null;
    if (!f) continue;
    if (!(typeof e.reason === 'string' && e.reason.trim())) out.push({ rule: 'demo', file: f, line: 1, col: 1, check: 'source', marker: 'design_only entry without a reason', count: 1 });
    designOnly.set(f, e.reason);
  }
  if (designOnly.size) {
    const reached = appImportGraph(root).files;
    for (const f of designOnly.keys()) if (reached.has(f)) out.push({ rule: 'demo', file: f, line: 1, col: 1, check: 'source', marker: 'design_only file reached from the app entry', count: 1 });
  }
  const files = [
    ...walkFiles(join(root, 'src'), p => p.endsWith('.js')).map(rel).filter(f => !/^src\/(design|i18n\/locales)\//.test(f) && !designOnly.has(f)).map(f => [f, 'js']),
    ...(existsSync(join(root, 'src', 'index.html')) ? [['src/index.html', 'js']] : []),
    ...walkFiles(join(root, 'css'), p => p.endsWith('.css')).map(rel).filter(f => f !== 'css/design.css').map(f => [f, 'css']),
  ];
  for (const [f, kind] of files) {
    const text = read(join(root, f));
    const found = kind === 'js' ? scanDemoText(text, f, jsMarkers, 'js')
      : [...scanDemoText(text.replace(/\/\*[\s\S]*?\*\//g, blank), f, cssClasses, 'css'), ...scanDemoText(text, f, cssText, 'css')];
    out.push(...found.map(x => ({ ...x, check: 'source' })));
  }
  return out;
}

// The imports part: every relative static import (import / export … from / import('…')) from
// src/entry/app.js on; a target under src/design/ is a finding at the importing line.
// A literal require('…') counts like an import (T-P4-07 fix round 1; design/logs_demo.js once used
// that form). appImportGraph also returns the files reached (paths relative to ui/), which the
// sources part uses to confirm that its design-only exemptions are not part of the app target.
export function scanAppImports(root = UI_ROOT) {
  return appImportGraph(root).findings;
}

export function appImportGraph(root = UI_ROOT) {
  const entry = join(root, 'src', 'entry', 'app.js');
  if (!existsSync(entry)) return { findings: [], files: new Set() };
  const design = join(root, 'src', 'design');
  const inDesign = p => { const r = relative(design, p); return !!r && !r.startsWith('..') && !isAbsolute(r); };
  const seen = new Set(), out = [];
  const visit = file => {
    if (seen.has(file)) return;
    seen.add(file);
    if (!/\.m?js$/.test(file) || !existsSync(file)) return;
    let ast;
    try { ast = parseJs(read(file)); } catch (e) { return; }
    const specs = [];
    walk.full(ast, n => {
      if ((n.type === 'ImportDeclaration' || n.type === 'ExportNamedDeclaration' || n.type === 'ExportAllDeclaration') && n.source && typeof n.source.value === 'string') specs.push([n.source.value, n.loc.start.line]);
      else if (n.type === 'ImportExpression' && n.source && n.source.type === 'Literal' && typeof n.source.value === 'string') specs.push([n.source.value, n.loc.start.line]);
      else if (n.type === 'CallExpression' && n.callee.type === 'Identifier' && n.callee.name === 'require' && n.arguments[0]
        && n.arguments[0].type === 'Literal' && typeof n.arguments[0].value === 'string') specs.push([n.arguments[0].value, n.loc.start.line]);
    });
    for (const [spec, line] of specs) {
      if (!spec.startsWith('.')) continue;
      const target = resolve(dirname(file), spec);
      if (inDesign(target)) out.push({ rule: 'demo', file: posix(relative(root, file)), line, col: 1, check: 'app-import', marker: `imports ${posix(relative(root, target))}`, count: 1 });
      visit(target);
    }
  };
  visit(entry);
  return { findings: out, files: new Set([...seen].map(p => posix(relative(root, p)))) };
}

// files: [{path, text}] (top level of the app bundle: *.js and *.css). With markers from
// loadDemoMarkers(root), the sources and imports parts of that tree are added.
export function scanDemoFiles(files, markers) {
  const bundle = files.filter(f => /\.(js|css)$/.test(f.path)).flatMap(f => scanDemoText(f.text, f.path, markers));
  return markers && markers.root ? [...bundle, ...scanDemoSources(markers.root, markers), ...scanAppImports(markers.root)] : bundle;
}

// ---- features (architecture §5) ---------------------------------------------------
// The registry is a page script (src/rt/feature_registry.js, T-P4-02a); entries are the
// object literals with string `name` and `page` properties.
export function parseFeatureRegistry(source) {
  const out = [];
  walk.full(parseJs(source), node => {
    if (node.type !== 'ObjectExpression') return;
    const get = k => {
      const p = node.properties.find(x => keyName(x) === k);
      return p && p.value.type === 'Literal' && typeof p.value.value === 'string' ? p.value.value : null;
    };
    const name = get('name'), page = get('page'), noControl = get('no_control');
    if (name && page) out.push(noControl ? { name, page, no_control: noControl } : { name, page });
  });
  return out;
}

export function readFeatureRegistry(root = UI_ROOT) {
  const p = join(root, 'src', 'rt', 'feature_registry.js');
  return existsSync(p) ? parseFeatureRegistry(read(p)) : [];
}

// T.rt.feature('B-xx') call sites. A non-literal argument is reported as dynamic.
// demo: true when the call sits under a __DEMO__ test (the then-branch of an if / ?: whose test,
// or the right of an && / || whose left, names __DEMO__): a B item must not hide behind the
// build flag (P4 rule 14, T-P4-07).
const mentionsDemo = n => { let hit = false; walk.full(n, x => { if (x.type === 'Identifier' && x.name === '__DEMO__') hit = true; }); return hit; };
export function findFeatureCalls(source, file = '<js>') {
  const out = [];
  walk.ancestor(parseJs(source), {
    CallExpression(node, _, anc) {
      if (propName(node.callee) !== 'feature') return;
      if (propName(node.callee.object) !== 'rt') return;
      const a = node.arguments[0];
      const demo = anc.some((x, i) => {
        const child = anc[i + 1];
        if (!child) return false;
        if ((x.type === 'IfStatement' || x.type === 'ConditionalExpression') && child === x.consequent) return mentionsDemo(x.test);
        if (x.type === 'LogicalExpression' && child === x.right) return mentionsDemo(x.left);
        return false;
      });
      out.push({ file, line: node.loc.start.line, name: a && a.type === 'Literal' && typeof a.value === 'string' ? a.value : null, demo });
    },
  });
  return out;
}

// The page a source file belongs to, for the per-page reference check (T-P4-07 fix round 1):
// pages/<p>.js, pages/<p>/** and rt/project_<p>.js belong to <p>; shellc/** and core/app.js to the
// shell; anything else to no page (its calls count for no page).
export function featurePageOf(file) {
  const f = String(file).replace(/\\/g, '/').replace(/^ui\//, '');
  const m = /^src\/pages\/([a-z0-9_]+)(?:\.js$|\/)/.exec(f) || /^src\/rt\/project_([a-z0-9_]+)\.js$/.exec(f);
  if (m) return m[1];
  if (/^src\/shellc\//.test(f) || f === 'src/core/app.js') return 'shell';
  return null;
}

// registry: [{name, page, no_control?}], enabled: features.json, calls: [{file,line,name}],
// pages: features_pages. Per page (T-P4-07 fix round 1): an entry of a page in features_pages must
// be referenced by a T.rt.feature call in that page's own files (featurePageOf), not merely by a
// call of the same name on another page; an entry whose page has no gated control of its own says
// why in `no_control` and is exempt. A call in a page's files whose name is registered only for
// other pages is a finding too (call-other-page).
export function checkFeatures({ registry, enabled, calls, pages }) {
  const out = [];
  const names = new Set(registry.map(r => r.name));
  for (const n of enabled) if (!names.has(n)) out.push({ rule: 'features', file: 'src/features.json', line: 1, col: 1, check: 'enabled-unregistered', text: n });
  for (const c of calls) {
    if (c.name === null) out.push({ rule: 'features', file: c.file, line: c.line, col: 1, check: 'dynamic-name', text: 'T.rt.feature(<dynamic>)' });
    else if (!names.has(c.name)) out.push({ rule: 'features', file: c.file, line: c.line, col: 1, check: 'call-unregistered', text: c.name });
    if (c.demo) out.push({ rule: 'features', file: c.file, line: c.line, col: 1, check: 'in-demo', text: `T.rt.feature('${c.name}') under __DEMO__` });
  }
  const used = new Set(calls.map(c => `${featurePageOf(c.file)}|${c.name}`));
  const regPages = new Map();
  for (const r of registry) { if (!regPages.has(r.name)) regPages.set(r.name, new Set()); regPages.get(r.name).add(r.page); }
  for (const c of calls) {
    const pg = featurePageOf(c.file);
    if (c.name !== null && pg && pages.includes(pg) && regPages.has(c.name) && !regPages.get(c.name).has(pg)) {
      out.push({ rule: 'features', file: c.file, line: c.line, col: 1, check: 'call-other-page', text: `${c.name} is registered for ${[...regPages.get(c.name)].join(', ')}, not ${pg}` });
    }
  }
  for (const r of registry) {
    if (!pages.includes(r.page) || r.no_control) continue;
    if (!used.has(`${r.page}|${r.name}`)) out.push({ rule: 'features', file: 'src/rt/feature_registry.js', line: 1, col: 1, check: 'unreferenced', text: `${r.name} (${r.page})` });
  }
  return out;
}

// ---- allow list (inject_allow.json) -----------------------------------------------
// Entry: {file, check?, line_sha256, count?, reason}. Matches a finding in the same file
// (and check, when given) whose source line hashes to line_sha256; a changed line no
// longer matches, so the exception lapses on its own. One entry covers at most `count`
// distinct source lines (default 1): when the same line text appears more often in the
// file, the extra occurrences stay findings (add a count, or one entry per line).
export function applyAllow(findings, allow, lineOf) {
  const kept = [], allowed = [];
  const covered = new Map(); // entry → Set(line numbers it already covers)
  for (const f of findings) {
    const digest = lineDigest(lineOf(f.file, f.line));
    const hit = allow.find(a => {
      if (a.file !== f.file || (a.check && a.check !== f.check) || a.line_sha256 !== digest) return false;
      const lines = covered.get(a) || new Set();
      return lines.has(f.line) || lines.size < (Number.isInteger(a.count) && a.count > 0 ? a.count : 1);
    });
    if (hit) {
      if (!covered.has(hit)) covered.set(hit, new Set());
      covered.get(hit).add(f.line);
      allowed.push({ ...f, reason: hit.reason });
    } else kept.push(f);
  }
  return { kept, allowed };
}

// ---- file sets ----------------------------------------------------------------------
function srcJsFiles(root) {
  return walkFiles(join(root, 'src'), p => p.endsWith('.js')).map(p => posix(relative(root, p)));
}
const CJK_EXCLUDE = [/^src\/i18n\/locales\//, /^src\/design\//, /^css\/design\.css$/, /^test\//];
const cjkExcluded = rel => CJK_EXCLUDE.some(re => re.test(rel));
// Directories where @i18n-pending is not honoured (architecture §3.9, overview §3 item 12).
const CJK_NO_PENDING = [/^src\/rt\//, /^src\/shellc\//];

// ---- rule runners -------------------------------------------------------------------
export function runRule(rule, { root = UI_ROOT, dist } = {}) {
  const text = rel => read(join(root, rel));
  const result = { rule, findings: [], pending: [], allowed: [], skipped: null };
  if (rule === 'cjk') {
    const files = [
      ...srcJsFiles(root).map(f => [f, 'js']),
      ...(existsSync(join(root, 'src', 'index.html')) ? [['src/index.html', 'html']] : []),
      ...walkFiles(join(root, 'css'), p => p.endsWith('.css')).map(p => [posix(relative(root, p)), 'css']),
    ].filter(([f]) => !cjkExcluded(f));
    for (const [f, kind] of files) {
      const s = text(f);
      const found = kind === 'js' ? scanCjkJs(s, f) : kind === 'html' ? scanCjkHtml(s, f) : scanCjkCss(s, f);
      const pending = isPending(s, kind);
      if (pending && CJK_NO_PENDING.some(re => re.test(f))) {
        // src/rt and src/shellc never get the exemption (architecture §3.9): the marker
        // itself is a violation and the file's hits count as findings.
        result.findings.push({ rule: 'cjk', file: f, line: 1, col: 1, kind: 'pending-marker', text: '@i18n-pending is not allowed here' }, ...found);
      } else {
        (pending ? result.pending : result.findings).push(...found);
      }
    }
  } else if (rule === 'inject' || rule === 'style') {
    for (const f of srcJsFiles(root).filter(f => !/^src\/i18n\/locales\//.test(f))) {
      result.findings.push(...(rule === 'inject' ? scanInjectJs(text(f), f) : scanStyleJs(text(f), f)));
    }
    if (rule === 'style' && existsSync(join(root, 'src', 'index.html'))) result.findings.push(...scanStyleHtml(text('src/index.html'), 'src/index.html'));
    if (rule === 'inject') {
      const allowPath = join(root, 'tools', 'inject_allow.json');
      const allow = existsSync(allowPath) ? JSON.parse(read(allowPath)).entries || [] : [];
      const lines = new Map();
      const lineOf = (f, n) => { if (!lines.has(f)) lines.set(f, text(f).split('\n')); return lines.get(f)[n - 1] ?? ''; };
      const { kept, allowed } = applyAllow(result.findings, allow, lineOf);
      result.findings = kept; result.allowed = allowed;
    }
  } else if (rule === 'demo') {
    // The sources and imports parts need no bundle: they are reported even when the bundle
    // part cannot run (T-P4-07).
    result.findings = [...scanDemoSources(root), ...scanAppImports(root)];
    // Bundle part: only the files build-manifest.json lists (top-level *.js / *.css), so stale
    // hashed files left in the directory are not scanned. No bundle, no manifest, a non-app
    // manifest or a listed file that is missing → `skipped`; enforce mode fails on that.
    const dir = dist ? resolve(dist) : join(root, 'dist', 'app');
    const rel = posix(relative(root, dir));
    const where = rel && !rel.startsWith('..') && !isAbsolute(rel) ? rel : posix(dir);
    const manifestPath = join(dir, 'build-manifest.json');
    if (!existsSync(dir)) { result.skipped = `no bundle at ${where}`; return result; }
    if (!existsSync(manifestPath)) { result.skipped = `no build-manifest.json in ${where}`; return result; }
    let manifest;
    try { manifest = JSON.parse(read(manifestPath)); } catch (e) { result.skipped = `unreadable build-manifest.json in ${where}`; return result; }
    if (manifest.target !== 'app') { result.skipped = `build-manifest.json in ${where} has target ${JSON.stringify(manifest.target)}, expected "app"`; return result; }
    const listed = (Array.isArray(manifest.files) ? manifest.files : []).map(f => f && f.path)
      .filter(p => typeof p === 'string' && !p.includes('/') && /\.(js|css)$/.test(p)).sort();
    const missing = listed.filter(p => !existsSync(join(dir, p)));
    if (missing.length) { result.skipped = `files listed in build-manifest.json are missing: ${missing.join(', ')}`; return result; }
    if (!listed.length) { result.skipped = `build-manifest.json in ${where} lists no *.js / *.css`; return result; }
    // (a copy without the recorded root: the sources and imports parts are done above)
    result.findings.push(...scanDemoFiles(listed.map(n => ({ path: n, text: read(join(dir, n)) })), { ...loadDemoMarkers(root) }));
    // the bundle certified itself demo-free at build time (build.mjs); a manifest that says
    // otherwise is a finding (an older manifest without the field is left to the markers)
    if (manifest.demo_free === false) result.findings.push({ rule: 'demo', file: `${where}/build-manifest.json`, line: 1, col: 1, check: 'manifest', marker: 'demo_free is false', count: 1 });
  } else if (rule === 'features') {
    const gates = loadGates(root);
    const enabledPath = join(root, 'src', 'features.json');
    const enabled = existsSync(enabledPath) ? JSON.parse(read(enabledPath)) : [];
    const calls = srcJsFiles(root).flatMap(f => findFeatureCalls(text(f), f));
    result.findings = checkFeatures({ registry: readFeatureRegistry(root), enabled, calls, pages: gates.features_pages || [] });
  } else {
    throw new Error(`unknown rule: ${rule}`);
  }
  return result;
}

export function loadGates(root = UI_ROOT) {
  const p = join(root, 'tools', 'scan_gates.json');
  return existsSync(p) ? JSON.parse(read(p)) : {};
}

const findingWeight = f => f.count || 1;
export function summarize(result) {
  const byFile = {};
  for (const f of result.findings) byFile[f.file] = (byFile[f.file] || 0) + findingWeight(f);
  return { count: result.findings.reduce((n, f) => n + findingWeight(f), 0), by_file: byFile };
}

// ---- CLI -----------------------------------------------------------------------------
function parseArgs(argv) {
  const o = { rule: 'all', mode: null, json: false, root: UI_ROOT, dist: null };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--rule') o.rule = argv[++i];
    else if (a === '--mode') o.mode = argv[++i];
    else if (a === '--json') o.json = true;
    else if (a === '--root') o.root = resolve(argv[++i]);
    else if (a === '--dist') o.dist = argv[++i];
    else throw new Error(`unknown argument: ${a}`);
  }
  if (o.mode && !['report', 'enforce'].includes(o.mode)) throw new Error(`--mode must be report|enforce`);
  return o;
}

export function main(argv = process.argv.slice(2)) {
  let o;
  try { o = parseArgs(argv); } catch (e) { console.error(String(e.message)); return 2; }
  const rules = o.rule === 'all' ? RULES : o.rule.split(',');
  for (const r of rules) if (!RULES.includes(r)) { console.error(`unknown rule: ${r}`); return 2; }
  const gates = loadGates(o.root);
  const report = { rules: {}, failed: false };
  for (const rule of rules) {
    const mode = o.mode || gates[rule] || 'report';
    const res = runRule(rule, { root: o.root, dist: o.dist });
    const sum = summarize(res);
    // A rule that could not run (demo without a bundle) cannot pass an enforced gate.
    const failed = mode === 'enforce' && (sum.count > 0 || !!res.skipped);
    report.failed ||= failed;
    report.rules[rule] = { mode, count: sum.count, by_file: sum.by_file, pending: res.pending.length,
      allowed: res.allowed.length, skipped: res.skipped, failed, findings: res.findings, pending_findings: res.pending };
  }
  if (o.json) {
    process.stdout.write(JSON.stringify(report, null, 2) + '\n');
  } else {
    for (const [rule, r] of Object.entries(report.rules)) {
      const extra = [r.pending ? `pending ${r.pending}` : '', r.allowed ? `allowed ${r.allowed}` : '', r.skipped ? `skipped: ${r.skipped}` : ''].filter(Boolean).join(', ');
      console.log(`[scan] ${rule} (${r.mode})${r.failed ? ' FAILED' : ''}: ${r.count}${extra ? ` (${extra})` : ''}`);
      for (const [f, n] of Object.entries(r.by_file)) console.log(`  ${f}: ${n}`);
    }
  }
  return report.failed ? 1 : 0;
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  process.exitCode = main();
}
