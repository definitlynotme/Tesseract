// RtError and the error/reason text fallback chains (T-P4-02a; P4 architecture section 2.4,
// PROTOCOL section 7). `message` carries only the code, never user text; the text comes from
// errorText() / reasonText() through the i18n catalog.

const CODE_RE = /^[a-z][a-z0-9_]*$/;

export class RtError extends Error {
  constructor(code, extra = {}) {
    const c = typeof code === 'string' && code ? code : 'internal';
    super(c);
    this.name = 'RtError';
    this.code = c;
    this.message_key = typeof extra.message_key === 'string' && extra.message_key ? extra.message_key
      : (CODE_RE.test(c) ? `errors.${c}` : 'errors.unknown');
    this.params = extra.params && typeof extra.params === 'object' ? extra.params : {};
    if (extra.field != null) this.field = extra.field;
    if (extra.hold != null) this.hold = !!extra.hold;
    if (extra.reason_code != null) this.reason_code = extra.reason_code;
    if (extra.result !== undefined) this.result = extra.result;
    if (extra.skipped_reason != null) this.skipped_reason = extra.skipped_reason;
  }

  // From a protocol Error object {code, message_key, params, field?, hold?, detail?, reason_code?}.
  // `detail` is diagnostic only (PROTOCOL section 7) and is not carried into the page error.
  static from(err, extra = {}) {
    if (err instanceof RtError) return err;
    const e = err && typeof err === 'object' ? err : {};
    return new RtError(typeof e.code === 'string' ? e.code : (typeof err === 'string' ? err : 'internal'), {
      message_key: e.message_key, params: e.params, field: e.field, hold: e.hold, reason_code: e.reason_code, ...extra,
    });
  }
}

// A key counts as found when T.t would find it: T.i18n.hasAny(id) (current -> en_US -> zh_CN,
// the same chain as T.t; T-P4-H4, P5 #34). A runtime without hasAny falls back to
// T.i18n.has(id); otherwise T.t(id) counts as a hit when it returns something other than the
// id itself; without T.t nothing can be looked up and the first key of the chain is returned.
function lookup(T, chain, params) {
  const t = T && typeof T.t === 'function' ? T.t : null;
  if (!t) return chain[0];
  const i = T.i18n;
  const has = i && typeof i.hasAny === 'function' ? id => i.hasAny(id)
    : (i && typeof i.has === 'function' ? id => i.has(id) : null);
  for (const id of chain) {
    if (typeof id !== 'string' || !id) continue;
    if (has) { if (has(id)) return t(id, params); continue; }
    const s = t(id, params);
    if (typeof s === 'string' && s && s !== id) return s;
  }
  return t(chain[chain.length - 1], params);
}

// message_key -> errors.<code> -> errors.unknown (criticism B6).
export function errorText(T, err) {
  const e = err && typeof err === 'object' ? err : { code: String(err || 'internal') };
  const code = typeof e.code === 'string' && CODE_RE.test(e.code) ? e.code : null;
  const chain = [e.message_key, code ? `errors.${code}` : null, 'errors.unknown'].filter(Boolean);
  return lookup(T, chain, e.params || {});
}

// <ns>.<code> -> common.reason_unknown (criticism B7).
export function reasonText(T, ns, code, params) {
  const chain = [ns && code ? `${ns}.${code}` : null, 'common.reason_unknown'].filter(Boolean);
  return lookup(T, chain, params || { code: code == null ? '' : String(code) });
}
