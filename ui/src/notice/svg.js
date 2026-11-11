// SVG nodes for the notice cards without markup sinks (DESIGN §5.7: no innerHTML here).
//
// iconNode(doc, markup) rebuilds an icon of ui/src/core/icons.js (T.icon returns its markup
// string when core.js, and with it T.raw, is not loaded, as in this window) with
// createElementNS: only <svg> and its <path> / <circle> / <rect> shapes, only the drawing
// attributes. markNode(doc) is the 「豁然」 brand mark of the sidebar (src/index.html).

export const SVG_NS = 'http://www.w3.org/2000/svg';
const SHAPES = new Set(['path', 'circle', 'rect']);
const SHAPE_ATTRS = new Set(['d', 'cx', 'cy', 'r', 'x', 'y', 'width', 'height', 'rx', 'class']);
const ROOT_ATTRS = ['class', 'width', 'height', 'viewBox', 'stroke-width'];

function attrs(source) {
  const out = {};
  for (const m of String(source).matchAll(/([a-zA-Z][\w-]*)="([^"<>]*)"/g)) out[m[1]] = m[2];
  return out;
}

export function iconNode(doc, markup) {
  const svg = doc.createElementNS(SVG_NS, 'svg');
  const root = /^\s*<svg\b([^>]*)>([\s\S]*)<\/svg>\s*$/.exec(String(markup));
  if (root) {
    const own = attrs(root[1]);
    for (const k of ROOT_ATTRS) if (own[k] != null) svg.setAttribute(k, own[k]);
    for (const m of root[2].matchAll(/<([a-z]+)\b([^<>]*?)\/>/g)) {
      if (!SHAPES.has(m[1])) continue;
      const shape = doc.createElementNS(SVG_NS, m[1]);
      for (const [k, v] of Object.entries(attrs(m[2]))) if (SHAPE_ATTRS.has(k)) shape.setAttribute(k, v);
      svg.appendChild(shape);
    }
  }
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('focusable', 'false');
  return svg;
}

export function markNode(doc) {
  const svg = doc.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('class', 'nc-mark');
  svg.setAttribute('viewBox', '0 0 100 100');
  svg.setAttribute('aria-hidden', 'true');
  const path = doc.createElementNS(SVG_NS, 'path');
  path.setAttribute('d', 'M42.5 90V47A17 17 0 0 0 25.5 30H6M57.5 90V47A17 17 0 0 1 74.5 30H94');
  const dot = doc.createElementNS(SVG_NS, 'circle');
  dot.setAttribute('cx', '80'); dot.setAttribute('cy', '11'); dot.setAttribute('r', '7');
  svg.appendChild(path);
  svg.appendChild(dot);
  return svg;
}

/** The countdown ring around the icon (a 34-unit box, radius 16). */
export const RING_RADIUS = 16;
export function ringNode(doc) {
  const svg = doc.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('class', 'nc-ring');
  svg.setAttribute('viewBox', '0 0 34 34');
  svg.setAttribute('aria-hidden', 'true');
  const circle = doc.createElementNS(SVG_NS, 'circle');
  circle.setAttribute('cx', '17'); circle.setAttribute('cy', '17'); circle.setAttribute('r', String(RING_RADIUS));
  svg.appendChild(circle);
  return svg;
}
