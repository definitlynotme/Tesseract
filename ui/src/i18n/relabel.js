// In-place relabel after a language switch (T-P4-03; P4 architecture §3.7).
//
// T.ti(el, id, params?, {attr}) writes text (or one attribute) and marks the element:
//   data-i18n="<id>"                          the element's text content
//   data-i18n-attr="aria-label:<id>;title:<id>"  attributes
// Parameters are kept in a WeakMap (never serialised into the DOM). relabel(root) walks the
// marked elements and rewrites them without rebuilding the DOM.
//
// The page-level fallback (architecture §3.7 item 4) is a contract for the router (T-P4-04a):
//   T.page(id, { relocalize: 'rerender', snapshotState(), restoreState(state), … })
// relocalizePage(def, rerender) runs snapshotState → rerender → restoreState.

const memory = new WeakMap(); // el → { text: params|undefined, attrs: {attr: params} }

function slot(el) { let m = memory.get(el); if (!m) memory.set(el, m = { text: undefined, attrs: {} }); return m; }

function parseAttrs(value) {
  const out = [];
  for (const pair of String(value || '').split(';')) {
    const at = pair.indexOf(':');
    if (at > 0) out.push([pair.slice(0, at).trim(), pair.slice(at + 1).trim()]);
  }
  return out;
}

/** Record a text binding and write it. */
export function bindText(el, id, params, write) {
  el.setAttribute('data-i18n', id);
  slot(el).text = params || undefined;
  write(el, id, params);
}

/** Record an attribute binding and write it. */
export function bindAttr(el, attr, id, params, write) {
  const pairs = parseAttrs(el.getAttribute('data-i18n-attr')).filter(([a]) => a !== attr);
  pairs.push([attr, id]);
  el.setAttribute('data-i18n-attr', pairs.map(([a, i]) => `${a}:${i}`).join(';'));
  slot(el).attrs[attr] = params || undefined;
  write(el, attr, id, params);
}

/** Rewrite every marked element under root (root included). Returns the number of elements. */
export function relabel(root, { writeText, writeAttr }) {
  if (!root || !root.querySelectorAll) return 0;
  const els = Array.from(root.querySelectorAll('[data-i18n],[data-i18n-attr]'));
  if (root.matches && root.matches('[data-i18n],[data-i18n-attr]')) els.unshift(root);
  for (const el of els) {
    const m = memory.get(el) || { text: undefined, attrs: {} };
    const id = el.getAttribute('data-i18n');
    if (id) writeText(el, id, m.text);
    for (const [attr, attrId] of parseAttrs(el.getAttribute('data-i18n-attr'))) writeAttr(el, attr, attrId, m.attrs[attr]);
  }
  return els.length;
}

/** The T.page relocalize contract: keep drafts, scroll and open state across a re-render. */
export function relocalizePage(def, rerender) {
  if (!def || def.relocalize !== 'rerender') return false;
  const state = typeof def.snapshotState === 'function' ? def.snapshotState() : undefined;
  rerender();
  if (typeof def.restoreState === 'function') def.restoreState(state);
  return true;
}
