// ICU MessageFormat subset: parser, compiler and formatter (T-P4-03; P4 architecture §3.2, §3.3).
//
// Supported: {x} · {n, number} · {n, number, integer|percent} · {t, date, short|medium}
// · {t, time, short} · {n, plural, [offset:k] =0 {…} one {…} few {…} many {…} other {…}} with #
// · {x, select, a {…} other {…}} · ICU apostrophe quoting ('' → ', '{' … ' quotes)
// · tag placeholders <b> <em> <code> <kbd> <mono> (built in) and <tier> <link> <x> (caller slots).
// Rejected at compile time (with offset, line and column): selectordinal, spellout, ordinal,
// duration, number skeletons and other number styles, date/time pattern strings, plural/select
// nested deeper than 3 levels, unknown tags, tags with attributes, self-closing or unclosed tags.
//
// The same module runs at build time (Node, build/locales.mjs) and in the page; it has no DOM
// or Intl dependency of its own: formatting takes an environment of formatter callbacks.
//
// Compiled message (JSON-serialisable, compact):
//   message := string | part[]
//   part    := string | ['a', name] | ['n', name, style] | ['d', name, style] | ['t', name, style]
//            | ['p', name, offset, {key: message}] | ['s', name, {key: message}] | ['#']
//            | ['g', tag, message]

export const BUILTIN_TAGS = Object.freeze(['b', 'em', 'code', 'kbd', 'mono']);
export const SLOT_TAGS = Object.freeze(['tier', 'link', 'x']);
export const TAGS = Object.freeze([...BUILTIN_TAGS, ...SLOT_TAGS]);
export const PLURAL_CATEGORIES = Object.freeze(['zero', 'one', 'two', 'few', 'many', 'other']);
export const MAX_NESTING = 3;
const UNSUPPORTED_TYPES = new Set(['selectordinal', 'spellout', 'ordinal', 'duration', 'choice']);
const NUMBER_STYLES = new Set(['integer', 'percent']);
const DATE_STYLES = new Set(['short', 'medium']);
const TIME_STYLES = new Set(['short']);

export class IcuSyntaxError extends Error {
  constructor(message, source, offset) {
    const before = String(source).slice(0, offset);
    const line = before.split('\n').length;
    const col = offset - before.lastIndexOf('\n');
    super(`${message} (at ${line}:${col})`);
    this.name = 'IcuSyntaxError';
    this.reason = message;
    this.offset = offset;
    this.line = line;
    this.col = col;
  }
}

const isSpace = c => c === ' ' || c === '\t' || c === '\n' || c === '\r';
const NAME_RE = /[A-Za-z_][A-Za-z0-9_]*/y;
const KEY_RE = /=?[A-Za-z0-9_-]+/y;
const TAG_RE = /[A-Za-z][A-Za-z0-9_-]*/y;

/** Parse ICU source into a compiled message. Throws IcuSyntaxError. */
export function parse(source) {
  const src = String(source);
  let i = 0;
  const fail = (msg, at = i) => { throw new IcuSyntaxError(msg, src, at); };
  const skip = () => { while (i < src.length && isSpace(src[i])) i++; };
  const match = re => { re.lastIndex = i; const m = re.exec(src); if (!m) return null; i += m[0].length; return m[0]; };
  const expect = ch => { if (src[i] !== ch) fail(src[i] === undefined ? `expected "${ch}" but the message ended` : `expected "${ch}"`); i++; };

  // ctx: { depth, plural: bool, close: '}' | '</tag>' | null }
  function message(ctx) {
    const parts = [];
    let text = '';
    const flush = () => { if (text) { parts.push(text); text = ''; } };
    while (i < src.length) {
      const c = src[i];
      if (c === "'") {
        const n = src[i + 1];
        if (n === "'") { text += "'"; i += 2; continue; }
        if (n === '{' || n === '}' || n === '<' || (n === '#' && ctx.plural)) {
          const start = i;
          i++;
          for (;;) {
            if (i >= src.length) fail('unterminated quote', start);
            if (src[i] === "'") {
              if (src[i + 1] === "'") { text += "'"; i += 2; continue; }
              i++;
              break;
            }
            text += src[i++];
          }
          continue;
        }
        text += c; i++; continue;
      }
      if (c === '{') { flush(); parts.push(argument(ctx)); continue; }
      if (c === '}') {
        if (ctx.close === '}') break;
        fail(ctx.close ? `unclosed tag <${ctx.close}>` : 'unmatched "}"');
      }
      if (c === '#' && ctx.plural) { flush(); parts.push(['#']); i++; continue; }
      if (c === '<') {
        if (src[i + 1] === '/') {
          if (ctx.close && ctx.close !== '}') break;
          fail('closing tag without an opening tag');
        }
        if (/[A-Za-z]/.test(src[i + 1] || '')) { flush(); parts.push(tag(ctx)); continue; }
        text += c; i++; continue;
      }
      text += c; i++;
    }
    flush();
    if (ctx.close === '}' && src[i] !== '}') fail('unclosed "{"');
    if (ctx.close && ctx.close !== '}' && src[i] !== '<') fail(`unclosed tag <${ctx.close}>`);
    return parts;
  }

  function tag(ctx) {
    const start = i;
    i++; // <
    const name = match(TAG_RE);
    if (!TAGS.includes(name)) fail(`unknown tag <${name}> (allowed: ${TAGS.join(', ')})`, start);
    if (src[i] === '/') fail(`self-closing tag <${name}/> is not supported`, start);
    if (src[i] !== '>') fail(`tag <${name}> must not have attributes`, start);
    i++;
    const kids = message({ depth: ctx.depth, plural: ctx.plural, close: name });
    if (src.slice(i, i + name.length + 3) !== `</${name}>`) fail(`unclosed tag <${name}>`, start);
    i += name.length + 3;
    return ['g', name, simplify(kids)];
  }

  function cases(ctx, kind, start) {
    const out = {};
    let any = false;
    for (;;) {
      skip();
      if (src[i] === '}') break;
      if (i >= src.length) fail('unclosed "{"', start);
      const keyAt = i;
      const key = match(KEY_RE);
      if (!key) fail(`expected a ${kind} case key`);
      if (kind === 'plural') {
        if (key.startsWith('=')) { if (!/^=\d+$/.test(key)) fail(`invalid plural key "${key}"`, keyAt); }
        else if (!PLURAL_CATEGORIES.includes(key)) fail(`unknown plural category "${key}"`, keyAt);
      } else if (key.startsWith('=')) fail(`invalid select key "${key}"`, keyAt);
      // the compiled cases are a plain object (and an object literal in the language file):
      // a "__proto__" key would set its prototype instead of adding a case
      if (key === '__proto__') fail(`"__proto__" is not allowed as a ${kind} case key`, keyAt);
      if (Object.prototype.hasOwnProperty.call(out, key)) fail(`duplicate ${kind} case "${key}"`, keyAt);
      skip();
      expect('{');
      out[key] = simplify(message({ depth: ctx.depth + 1, plural: kind === 'plural' ? true : ctx.plural, close: '}' }));
      expect('}');
      any = true;
    }
    if (!any || !('other' in out)) fail(`${kind} needs an "other" case`, start);
    return out;
  }

  function argument(ctx) {
    const start = i;
    i++; // {
    skip();
    const name = match(NAME_RE);
    if (!name) fail('expected an argument name');
    skip();
    if (src[i] === '}') { i++; return ['a', name]; }
    expect(',');
    skip();
    const typeAt = i;
    const type = match(NAME_RE);
    if (!type) fail('expected an argument type');
    if (UNSUPPORTED_TYPES.has(type)) fail(`"${type}" is not supported`, typeAt);
    skip();
    if (type === 'number' || type === 'date' || type === 'time') {
      let style = '';
      if (src[i] === ',') {
        i++; skip();
        if (src.startsWith('::', i)) fail('number/date skeletons are not supported');
        const styleAt = i;
        let raw = '';
        while (i < src.length && src[i] !== '}') raw += src[i++];
        style = raw.trim();
        const allowed = type === 'number' ? NUMBER_STYLES : type === 'date' ? DATE_STYLES : TIME_STYLES;
        if (!allowed.has(style)) fail(`unsupported ${type} style "${style}" (allowed: ${[...allowed].join(', ')})`, styleAt);
      }
      if (type === 'date' && !style) style = 'medium';
      if (type === 'time' && !style) style = 'short';
      expect('}');
      return [type === 'number' ? 'n' : type === 'date' ? 'd' : 't', name, style];
    }
    if (type === 'plural' || type === 'select') {
      if (ctx.depth + 1 > MAX_NESTING) fail(`plural/select nested deeper than ${MAX_NESTING} levels`, start);
      expect(',');
      skip();
      let offset = 0;
      if (type === 'plural' && src.startsWith('offset:', i)) {
        i += 7;
        const m = /\d+/y; m.lastIndex = i;
        const r = m.exec(src);
        if (!r) fail('offset needs a non-negative integer');
        offset = Number(r[0]); i += r[0].length;
      }
      const opts = cases(ctx, type, start);
      expect('}');
      return type === 'plural' ? ['p', name, offset, opts] : ['s', name, opts];
    }
    fail(`unknown argument type "${type}"`, typeAt);
  }

  const out = message({ depth: 0, plural: false, close: null });
  if (i < src.length) fail('unexpected input');
  return simplify(out);
}

function simplify(parts) {
  if (parts.length === 0) return '';
  if (parts.length === 1 && typeof parts[0] === 'string') return parts[0];
  return parts;
}

/** Compile with a location label (`file:id`) in the error message. */
export function compile(source, where) {
  try { return parse(source); } catch (e) {
    if (e instanceof IcuSyntaxError && where) {
      const err = new IcuSyntaxError(`${where}: ${e.reason}`, String(source), e.offset);
      err.where = where;
      throw err;
    }
    throw e;
  }
}

// ---- formatting ----------------------------------------------------------------------
// env: { number(n, style), date(v, style), time(v, style), plural(n) → category,
//        missing?(name) → string }
// Returns an array of strings and {tag, kids} nodes (adjacent strings merged).
export function formatParts(msg, params, env, pound) {
  const out = [];
  const push = s => { if (s === '') return; if (typeof out[out.length - 1] === 'string') out[out.length - 1] += s; else out.push(s); };
  const p = params || {};
  const arg = name => (Object.prototype.hasOwnProperty.call(p, name) ? p[name] : undefined);
  const missing = name => (env.missing ? env.missing(name) : '');
  if (typeof msg === 'string') { push(msg); return out; }
  for (const part of msg) {
    if (typeof part === 'string') { push(part); continue; }
    switch (part[0]) {
      case 'a': { const v = arg(part[1]); push(v === undefined || v === null ? missing(part[1]) : String(v)); break; }
      case 'n': { const v = arg(part[1]); push(v === undefined || v === null ? missing(part[1]) : env.number(Number(v), part[2])); break; }
      case 'd': { const v = arg(part[1]); push(v === undefined || v === null ? missing(part[1]) : env.date(v, part[2])); break; }
      case 't': { const v = arg(part[1]); push(v === undefined || v === null ? missing(part[1]) : env.time(v, part[2])); break; }
      case '#': push(pound === undefined ? '#' : env.number(pound, '')); break;
      case 'p': {
        const v = arg(part[1]);
        if (v === undefined || v === null) { push(missing(part[1])); break; }
        const n = Number(v);
        const opts = part[3];
        const exact = '=' + n;
        const key = Object.prototype.hasOwnProperty.call(opts, exact) ? exact
          : (() => { const cat = env.plural(n - part[2]); return Object.prototype.hasOwnProperty.call(opts, cat) ? cat : 'other'; })();
        for (const x of formatParts(opts[key], p, env, n - part[2])) typeof x === 'string' ? push(x) : out.push(x);
        break;
      }
      case 's': {
        const v = arg(part[1]);
        const opts = part[2];
        const key = v !== undefined && v !== null && Object.prototype.hasOwnProperty.call(opts, String(v)) ? String(v) : 'other';
        for (const x of formatParts(opts[key], p, env, pound)) typeof x === 'string' ? push(x) : out.push(x);
        break;
      }
      case 'g': out.push({ tag: part[1], kids: formatParts(part[2], p, env, pound) }); break;
      default: throw new Error(`corrupt compiled message part: ${JSON.stringify(part)}`);
    }
  }
  return out;
}

/** Plain text: tags removed, their content kept. */
export function partsToText(parts) {
  let s = '';
  for (const x of parts) s += typeof x === 'string' ? x : partsToText(x.kids);
  return s;
}

export function format(msg, params, env) { return partsToText(formatParts(msg, params, env)); }

// ---- static analysis (gates, pseudo) -------------------------------------------------
/** {args: {name: Set(types)}, tags: Set, selects: {name: Set(keys)}, plurals: [{name, keys}], depth} */
export function analyze(msg) {
  const info = { args: {}, tags: new Set(), selects: {}, plurals: [] };
  const addArg = (name, type) => { (info.args[name] ||= new Set()).add(type); };
  const walk = m => {
    if (typeof m === 'string') return;
    for (const part of m) {
      if (typeof part === 'string') continue;
      switch (part[0]) {
        case 'a': addArg(part[1], 'text'); break;
        case 'n': addArg(part[1], 'number'); break;
        case 'd': case 't': addArg(part[1], 'datetime'); break;
        case 'p': addArg(part[1], 'plural'); info.plurals.push({ name: part[1], keys: Object.keys(part[3]) }); for (const k in part[3]) walk(part[3][k]); break;
        case 's': addArg(part[1], 'select'); (info.selects[part[1]] ||= new Set()); for (const k in part[2]) { info.selects[part[1]].add(k); walk(part[2][k]); } break;
        case 'g': info.tags.add(part[1]); walk(part[2]); break;
        default: break;
      }
    }
  };
  walk(msg);
  return info;
}

/** Copy of msg with every literal text run passed through fn (args, tags, # untouched). */
export function mapText(msg, fn) {
  if (typeof msg === 'string') return fn(msg);
  return msg.map(part => {
    if (typeof part === 'string') return fn(part);
    switch (part[0]) {
      case 'p': return ['p', part[1], part[2], Object.fromEntries(Object.entries(part[3]).map(([k, v]) => [k, mapText(v, fn)]))];
      case 's': return ['s', part[1], Object.fromEntries(Object.entries(part[2]).map(([k, v]) => [k, mapText(v, fn)]))];
      case 'g': return ['g', part[1], mapText(part[2], fn)];
      default: return part;
    }
  });
}

/** Does the compiled message contain any tag? */
export function hasTags(msg) { return analyze(msg).tags.size > 0; }
