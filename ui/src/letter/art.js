// The letter's drawings (11-05; letter/letter.js, DESIGN §6.22). Line drawings in the brand's own
// hand: ink strokes, hairline greys and one accent dot, nothing filled but the dot. Each drawing is
// returned as { el, play() }: el is drawn in its final state (what reduced motion and a static
// screenshot see); play() tells its small story once when its page is shown, then rests. Nothing
// loops (DESIGN §7 rule 7). Strokes draw in with stroke-dashoffset on pathLength 1 (§7 rule 1).
//
// Words in a drawing are the interface's own: tier code names (never translated) and the two
// capture modes' labels, passed in by the caller.

const NS = 'http://www.w3.org/2000/svg';

function svgEl(doc, tag, attrs, kids) {
  const el = doc.createElementNS(NS, tag);
  if (attrs) for (const k in attrs) el.setAttribute(k, String(attrs[k]));
  if (kids) for (const c of kids) if (c) el.append(c);
  return el;
}

// a stroke drawn from its start (pathLength 1), from delay ms, over dur ms
function drawIn(M, el, delay, dur) {
  return M.anim(el, [{ strokeDashoffset: 1 }, { strokeDashoffset: 0 }], { duration: dur, delay, easing: M.ease.out, fill: 'backwards' });
}

/* -- the letterhead: the 「豁然」 mark, drawn the way the launch draws it (two strokes open from the
   narrow passage out to the edges, then the light lands) ----------------------------------------- */
const MARK_PATHS = ['M42.5 90V47A17 17 0 0 0 25.5 30H6', 'M57.5 90V47A17 17 0 0 1 74.5 30H94'];
export function letterhead(T, doc) {
  const paths = MARK_PATHS.map(d => svgEl(doc, 'path', { d, pathLength: 1 }));
  const dot = svgEl(doc, 'circle', { class: 'lt-mark-dot', cx: 80, cy: 11, r: 5.8 });
  const el = svgEl(doc, 'svg', { class: 'lt-mark', viewBox: '0 0 100 100', 'aria-hidden': 'true' }, [...paths, dot]);
  const play = () => {
    const M = T.motion;
    // each stroke starts at the bottom (the narrow passage), so drawing from its start opens outwards
    paths.forEach(p => drawIn(M, p, 80, 520));
    M.anim(dot, [{ opacity: 0, transform: 'scale(.3)' }, { opacity: 1, transform: 'none' }], { duration: M.SP.soft.duration, delay: 460, easing: M.SP.soft.easing, fill: 'backwards' });
  };
  return { el, play };
}

/* -- 三步开始: import → start → running, three rings joined by hairlines ------------------------- */
export function stepsArt(T, doc) {
  const ring = (cls, kid) => T.h('span', { class: `lt-st-ring ${cls}` }, kid);
  const run = T.h('span', { class: 'lt-st-run' });
  const rings = [ring('is-import', T.iconEl('import', 18)), ring('is-start', T.iconEl('start', 18)), ring('is-running', run)];
  const links = [T.h('span', { class: 'lt-st-link' }), T.h('span', { class: 'lt-st-link' })];
  const el = T.h('div', { class: 'lt-art lt-steps-art', 'aria-hidden': 'true' }, rings[0], links[0], rings[1], links[1], rings[2]);
  const play = () => {
    const M = T.motion;
    rings.forEach((r, i) => M.anim(r, [{ opacity: 0, transform: 'scale(.82)' }, { opacity: 1, transform: 'none' }], { duration: M.SP.soft.duration, delay: 120 + i * 260, easing: M.SP.soft.easing, fill: 'backwards' }));
    links.forEach((l, i) => M.anim(l, [{ transform: 'scaleX(0)' }, { transform: 'none' }], { duration: 260, delay: 240 + i * 260, easing: M.ease.out, fill: 'backwards' }));
    M.anim(run, [{ opacity: 0, transform: 'scale(.3)' }, { opacity: 1, transform: 'none' }], { duration: M.SP.soft.duration, delay: 760, easing: M.SP.soft.easing, fill: 'backwards' });
  };
  return { el, play };
}

/* -- 一层接一层: three tiers on one rail. The light sits on Vanguard; Vanguard's way breaks, the light
   steps down to Sentinel; Vanguard recovers and the light goes back (the letter's "恢复了，再换回来") */
const RELAY = [{ name: 'Vanguard', y: 20 }, { name: 'Sentinel', y: 56 }, { name: 'Bastion', y: 92 }];
export function relayArt(T, doc) {
  const x0 = 14, r0 = 118, r1 = 348;
  const kids = [svgEl(doc, 'path', { class: 'lt-rl-rail', d: `M${x0} ${RELAY[0].y}V${RELAY[2].y}` })];
  const on = [];
  RELAY.forEach((row, i) => {
    kids.push(svgEl(doc, 'path', { class: `lt-rl-way${i === 2 ? ' is-spare' : ''}`, d: `M${r0} ${row.y}H${r1}` }));
    const lit = svgEl(doc, 'path', { class: 'lt-rl-on', d: `M${r0} ${row.y}H${r1}` });
    if (i !== 0) lit.classList.add('is-rest');
    on.push(lit); kids.push(lit);
    kids.push(svgEl(doc, 'circle', { class: 'lt-rl-node', cx: x0, cy: row.y, r: 5.5 }));
    const label = svgEl(doc, 'text', { class: 'lt-rl-name', x: x0 + 16, y: row.y + 4.4 });
    label.textContent = row.name;
    kids.push(label);
  });
  const light = svgEl(doc, 'circle', { class: 'lt-rl-light', cx: x0, cy: RELAY[0].y, r: 3.4 });
  kids.push(light);
  const el = T.h('div', { class: 'lt-art lt-relay', 'aria-hidden': 'true' },
    svgEl(doc, 'svg', { viewBox: '0 0 360 108', translate: 'no' }, kids));
  // one timeline (ms) for every part, so no part's held state covers another's: Vanguard's way goes
  // out, the light steps down, Sentinel's way lights; then Vanguard is back and the light returns
  const SPAN = 3600;
  const track = (el, prop, points) => T.motion.anim(el, points.map(([ms, v]) => ({ [prop]: v, offset: ms / SPAN, easing: T.motion.ease.inout })), { duration: SPAN });
  const play = () => {
    const dy = `translateY(${RELAY[1].y - RELAY[0].y}px)`;
    track(light, 'transform', [[0, 'none'], [1100, 'none'], [1640, dy], [2860, dy], [3400, 'none'], [SPAN, 'none']]);
    track(on[0], 'opacity', [[0, 1], [600, 1], [840, 0], [2780, 0], [3060, 1], [SPAN, 1]]);
    track(on[1], 'opacity', [[0, 0], [1280, 0], [1540, 1], [2800, 1], [3040, 0], [SPAN, 0]]);
  };
  return { el, play };
}

/* -- 两种接管方式: a column of apps and Tesseract. The system proxy reaches the apps that follow it
   (the dashed ones don't); the virtual network adapter reaches every one of them ---------------- */
export function modesArt(T, doc, labels) {
  const cells = [];
  const HUB = { x: 112, y: 41 };
  const side = (n, label, delay0) => {
    const kids = [];
    const links = [];
    for (let i = 0; i < 5; i++) {
      const x = 6, y = 3 + i * 16;
      const joined = i < n;
      kids.push(svgEl(doc, 'rect', { class: `lt-md-app${joined ? '' : ' is-out'}`, x, y, width: 12, height: 12, rx: 3.5 }));
      if (joined) {
        const sx = x + 12, sy = y + 6;
        const p = svgEl(doc, 'path', { class: 'lt-md-link', d: `M${sx + 3} ${sy}C${sx + 44} ${sy} 70 ${HUB.y} ${HUB.x - 12} ${HUB.y}`, pathLength: 1 });
        links.push(p); kids.push(p);
      }
    }
    kids.push(svgEl(doc, 'circle', { class: 'lt-md-hub', cx: HUB.x, cy: HUB.y, r: 9 }));
    kids.push(svgEl(doc, 'circle', { class: 'lt-md-dot', cx: HUB.x, cy: HUB.y, r: 3 }));
    const cell = T.h('figure', { class: 'lt-md' }, svgEl(doc, 'svg', { viewBox: '0 0 132 82' }, kids), T.h('figcaption', null, label));
    cells.push({ links, delay0 });
    return cell;
  };
  const el = T.h('div', { class: 'lt-art lt-modes', 'aria-hidden': 'true' }, side(3, labels.system, 120), side(5, labels.tun, 480));
  const play = () => {
    const M = T.motion;
    cells.forEach(c => c.links.forEach((p, i) => drawIn(M, p, c.delay0 + i * 50, 420)));
  };
  return { el, play };
}

/* -- the signature's flourish: one ink stroke that ends in the light ------------------------------ */
export function flourish(T, doc) {
  const path = svgEl(doc, 'path', { class: 'lt-fl-line', d: 'M3 15C26 7 48 19 78 13S132 5 166 11', pathLength: 1 });
  const dot = svgEl(doc, 'circle', { class: 'lt-fl-dot', cx: 178, cy: 9.5, r: 2.9 });
  const el = svgEl(doc, 'svg', { class: 'lt-flourish', viewBox: '0 0 184 22', 'aria-hidden': 'true' }, [path, dot]);
  const play = () => {
    const M = T.motion;
    drawIn(M, path, 420, 680);
    M.anim(dot, [{ opacity: 0, transform: 'scale(.3)' }, { opacity: 1, transform: 'none' }], { duration: M.SP.soft.duration, delay: 1040, easing: M.SP.soft.easing, fill: 'backwards' });
  };
  return { el, play };
}
