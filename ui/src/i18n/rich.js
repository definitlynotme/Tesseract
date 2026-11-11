// Tag placeholders → DOM (T-P4-03; P4 architecture §3.3, §7).
//
// Input is icu.js formatParts() output: strings and {tag, kids}. Strings — including every
// parameter value — become text nodes; the whole path never parses HTML, so a parameter
// such as "<img src=x onerror=…>" can only ever be text.
//
// Built-in tags: b → <b>, em → <em>, code → <code class="tech">, kbd → <kbd>,
// mono → <span class="mono">. Slot tags (tier, link, x) call slots[tag](fragment) → Node,
// which lets the caller wrap the translated words (e.g. a clickable tier tag). A missing or
// misbehaving slot falls back to the plain children (and warns in development builds).

const BUILTIN = {
  b: d => d.createElement('b'),
  em: d => d.createElement('em'),
  code: d => { const e = d.createElement('code'); e.className = 'tech'; return e; },
  kbd: d => d.createElement('kbd'),
  mono: d => { const e = d.createElement('span'); e.className = 'mono'; return e; },
};

export function renderParts(parts, { doc, slots, warn } = {}) {
  const d = doc || document;
  const frag = d.createDocumentFragment();
  for (const p of parts) {
    if (typeof p === 'string') { frag.appendChild(d.createTextNode(p)); continue; }
    const kids = renderParts(p.kids, { doc: d, slots, warn });
    const make = BUILTIN[p.tag];
    if (make) {
      const el = make(d);
      el.appendChild(kids);
      frag.appendChild(el);
      continue;
    }
    const slot = slots && slots[p.tag];
    let node = null;
    if (typeof slot === 'function') {
      try { node = slot(kids); } catch (e) { if (warn) warn(`slot <${p.tag}> threw: ${e && e.message}`); }
      if (node != null && !(node instanceof d.defaultView.Node)) {
        if (warn) warn(`slot <${p.tag}> must return a Node`);
        node = null;
      }
    } else if (warn) warn(`no slot for <${p.tag}>`);
    // fallback renders the children afresh: a failed slot may have consumed `kids`
    frag.appendChild(node || renderParts(p.kids, { doc: d, slots, warn }));
  }
  return frag;
}
