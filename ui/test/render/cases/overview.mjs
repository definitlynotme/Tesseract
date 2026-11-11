// Render matrix of the overview page (T-P4-05a): the overview_* scenarios (ui/test/fixtures/
// scenarios, mapping_overview section 4: run / start / stop, readiness, exit card, network mode,
// kill switch, pool, Bastion, protocols, Gateway, traffic, stale, rt:reset) in zh_CN and the
// pseudo language at both sizes.
//   npm --prefix ui run build -- --pseudo --i18n-trace
//   node ui/test/render/run.mjs --page overview
// Each case checks the page's state by message id (data-i18n-id of a rich face, the trace
// build's text -> id index for a T.t string), so the same assertions hold in both languages,
// plus data runs (addresses, node names) by text. Garbage (NaN / undefined on screen) fails here
// (ruling 5th wave 10-5: hard for the 05 series once 05a is in).
// B items (requirement 5): overview_b_items checks the B-01 branch both ways: hidden in the
// normal build, shown in a `--build-flags "--h-features all"` run.
import { readFileSync } from 'node:fs';
import { DEMO_BADGE, LANGS9, PSEUDO_OVERFLOW } from './overview_i18n.mjs';

const settle = { page_ms: 900 };
const click = sel => ({ eval: s => document.querySelector(s).click(), arg: sel });

// Runs in the page: every check of a case; returns the problems (strings).
function probe(checks) {
  const problems = [];
  const norm = s => String(s == null ? '' : s).replace(/\s+/g, ' ').trim();
  // the trace index by normalised text (T-P5-11: fr / de / es / ru put a no-break space before % or
  // :, which the page's text reads as a space after norm(); the index keys are the raw strings)
  const traced = {};
  for (const [k, v] of Object.entries(T.i18n.traced ? T.i18n.traced() : {})) traced[norm(k)] = [...(traced[norm(k)] || []), ...v];
  const face = el => {
    const all = [...el.querySelectorAll(':scope > .sw, :scope > .swap-box > .sw')].filter(x => x.getAttribute('aria-hidden') !== 'true');
    return all.length ? all[all.length - 1] : el;
  };
  const ids = el => {
    const out = new Set();
    const f = face(el);
    for (const x of [f, ...f.querySelectorAll('[data-i18n-id]')]) if (x.getAttribute && x.getAttribute('data-i18n-id')) out.add(x.getAttribute('data-i18n-id'));
    for (const s of [norm(f.textContent), ...[...f.childNodes].filter(n => n.nodeType === 3).map(n => norm(n.data))]) for (const id of traced[s] || []) out.add(id);
    // a T.t string written straight into the element (no roll)
    for (const id of traced[norm(el.textContent)] || []) out.add(id);
    return out;
  };
  const visible = el => !!el && !el.hidden && !el.closest('[hidden]') && el.checkVisibility({ opacityProperty: true, visibilityProperty: true });
  for (const c of checks) {
    const [kind, sel] = c;
    const el = sel === '#app' ? document.getElementById('app') : document.querySelector(sel);
    if (!el) { problems.push(`${sel}: missing`); continue; }
    const txt = norm(face(el).textContent);
    if (kind === 'id') { const got = ids(el); if (!got.has(c[2])) problems.push(`${sel}: expected ${c[2]}, got [${[...got].join(', ')}] "${txt}"`); }
    else if (kind === 'idin') {
      const got = new Set();
      const walk = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
      for (let n = walk.nextNode(); n; n = walk.nextNode()) {
        for (const id of traced[norm(n.data)] || []) got.add(id);
      }
      if (!got.has(c[2])) problems.push(`${sel}: no text of ${c[2]} inside, got [${[...got].join(', ')}]`);
    }
    else if (kind === 'noid') { if (ids(el).has(c[2])) problems.push(`${sel}: unexpected ${c[2]}`); }
    else if (kind === 'has') { if (!txt.includes(c[2])) problems.push(`${sel}: "${txt}" lacks "${c[2]}"`); }
    else if (kind === 'not') { if (txt.includes(c[2])) problems.push(`${sel}: "${txt}" has "${c[2]}"`); }
    else if (kind === 'attr') { const v = el.getAttribute(c[2]); if (v !== c[3]) problems.push(`${sel}[${c[2]}] is ${v}, expected ${c[3]}`); }
    else if (kind === 'cls') { if (el.classList.contains(c[2]) !== c[3]) problems.push(`${sel}.${c[2]} should be ${c[3]}`); }
    else if (kind === 'shown') { if (visible(el) !== c[2]) problems.push(`${sel} shown should be ${c[2]}`); }
    else if (kind === 'disabled') { if (el.disabled !== c[2]) problems.push(`${sel} disabled should be ${c[2]}`); }
    else if (kind === 'empty') { if (txt !== '') problems.push(`${sel}: expected empty, got "${txt}"`); }
    else problems.push(`unknown check ${kind}`);
  }
  // the deck: three fallback tiers only (gateway never a card), the runtime order's carrier first
  const cards = [...document.querySelectorAll('#ov-deck > .dcard')];
  if (cards.length !== 3 || cards.some(b => b.dataset.tier === 'gateway')) problems.push(`deck cards: ${cards.map(b => b.dataset.tier).join(',')}`);
  return problems;
}

/* Runs in the page (F-01): nothing with words lies over the chart's grid. The y labels sit left of
   the plot, the x labels under its base line; the hover readout is invisible without a pointer; no
   state lays a note over the grid (the paused / empty chart is quiet). Every overview cell checks it */
export function chartQuiet() {
  const box = document.getElementById('ov-chart');
  if (!box) return ['#ov-chart missing'];
  const lines = [...box.querySelectorAll('.ch-grid line')].map(l => l.getBoundingClientRect()).filter(r => r.width > 0);
  if (!lines.length) return [];
  const plot = { l: Math.min(...lines.map(r => r.left)), r: Math.max(...lines.map(r => r.right)), t: Math.min(...lines.map(r => r.top)), b: Math.max(...lines.map(r => r.bottom)) };
  const p = [];
  for (const el of box.querySelectorAll('*')) {
    if (el.closest('svg') || !el.checkVisibility({ opacityProperty: true, visibilityProperty: true })) continue;
    if (![...el.childNodes].some(n => n.nodeType === 3 && n.data.trim())) continue;
    const r = el.getBoundingClientRect();
    if (r.right > plot.l + 0.5 && r.left < plot.r - 0.5 && r.bottom > plot.t + 0.5 && r.top < plot.b - 0.5) p.push(`words over the chart's grid: "${el.textContent.trim().slice(0, 40)}" (${el.className || el.localName})`);
  }
  return p;
}
/* Runs in the page (F-01 round 1): no hole above the family rows when the exit card has no route
   (the band is gone, the rows sit under the head), and 协议分布 right under the status strip (it is
   no longer pinned to the column's foot). Every overview cell checks it */
export function tightLayout() {
  const p = [];
  const exit = document.getElementById('ov-exit');
  if (exit && exit.classList.contains('is-noroute')) {
    const head = exit.querySelector(':scope > .ov-head').getBoundingClientRect();
    const stage = exit.querySelector(exit.dataset.state === 'ok' ? '.exit-full' : '.exit-idle');
    const row = stage && stage.querySelector('.ip-row');
    if (row && row.getBoundingClientRect().top - head.bottom > 16) p.push(`a ${Math.round(row.getBoundingClientRect().top - head.bottom)} px hole above IPv4`);
    const band = exit.querySelector('.exit-route');
    if (band && band.checkVisibility()) p.push('no route, but the route band is shown');
  }
  // (round 2) the front card has no empty second line: with nothing under its state pill, the
  // band ends under the pill row
  const deck = document.getElementById('ov-deck'), card = deck && deck.querySelector('.dcard.is-front');
  if (card && !deck.classList.contains('is-ghost')) {
    const node = card.querySelector('[data-node]'), row = card.querySelector('.dc-row1'), band = card.querySelector('.dcard-id');
    if (node && row && band && !node.textContent.trim()) {
      const gap = band.getBoundingClientRect().top - row.getBoundingClientRect().bottom;
      if (gap > 12) p.push(`an empty ${Math.round(gap)} px line under the front card's pill`);
    }
  }
  const strip = document.getElementById('ov-strip'), proto = document.getElementById('ov-proto-sec');
  if (strip && proto && proto.checkVisibility()) {
    const gap = proto.getBoundingClientRect().top - strip.getBoundingClientRect().bottom;
    if (gap > 20) p.push(`a ${Math.round(gap)} px gap above 协议分布`);
  }
  return p;
}
// Runs in the page (F-01): the badge of a result 刷新 confirmed says 已验证 (its words, no tip)
function verifiedBadge() {
  const prov = document.querySelector('#ov-prov'), txt = prov.textContent.replace(/\s+/g, ' ');
  const m = T.t('overview.exit.prov.ok', { how: 'manual', ago: '\u0001' }).split('\u0001'), a = T.t('overview.exit.prov.ok', { how: 'auto', ago: '\u0001' }).split('\u0001');
  const own = m.map((x, i) => (x !== a[i] ? x.replace(/\s+/g, ' ').trim() : '')).filter(Boolean);
  const p = own.length && own.every(x => txt.includes(x)) ? [] : [`trigger=manual: the badge says "${txt}"`];
  if (prov.hasAttribute('data-tip')) p.push(`the badge carries a tip: ${prov.getAttribute('data-tip')}`);
  return p;
}

const front = () => '#ov-deck > .dcard.is-front';
const frontPill = '#ov-deck > .dcard.is-front [data-pt]';
// one case: the scenario of the same name, the checks, optional actions and B-01 handling
const C = (name, checks, extra = {}) => ({
  name, scenario: name, page: 'overview',
  ...extra,
  expect: async ({ page, checks: mc }) => {
    if (extra.before) await page.evaluate(extra.before);
    const problems = await page.evaluate(probe, typeof checks === 'function' ? await page.evaluate(checks) : checks);
    // garbage is hard on the overview (ruling 5th wave 10-5)
    const g = mc && mc.garbage;
    if (Array.isArray(g) && g.length) problems.push(`garbage: ${JSON.stringify(g).slice(0, 300)}`);
    if (extra.also) problems.push(...await page.evaluate(extra.also));
    problems.push(...await page.evaluate(chartQuiet), ...await page.evaluate(tightLayout));
    return { problems };
  },
});

const RUN = s => ['attr', '#app', 'data-run', s];
// a gap frame of a state topic (PROTOCOL 4): the baseline is withdrawn, the topic is stale until a
// replacement arrives (the fake has no rt_resync: it reconnects with nothing to replay)
const gap = (topic, key) => ({ deliver: { t: 'evt', epoch: 'rt-fixture-e1', produced_at_ms: 1790000000000, sent_at_ms: 1790000000001, topic, ...(key ? { key } : {}),
  rev: 2, kind: 'gap', data: { from_seq: 1, to_seq: 1, dropped: 1 }, seq: 2 } });

/* B9 (criticism B9, review 05a S9): a same-epoch rt:reset{reconnect} that replays the scenario's
   replay frames; #app's run / brand point and the hero name must not flash. The case also checks
   that the replay really happened (one rt:reset with generation_changed=false), so the assertions
   cannot pass vacuously when the fake's replay or the fixture's replay key is gone; installed is a
   page function for the variant's own check (same rev deduplicated, newer rev installed). */
function reconnectCase(name, installed) {
  return C(name, [RUN('running'), ['attr', '#run', 'data-state', 'running'], ['has', '#ov-name', 'Vanguard'], ['has', '#ov-ip4', '203.0.113.24'], ['has', front(), 'HK-01']], {
    actions: [{ eval: () => {
      const app = document.getElementById('app'), name = document.getElementById('ov-name'), seen = window.__ovSeen = { run: [], ks: [], name: [], reset: [], tiers: 0 };
      window.__ovInstalled = ['the replay check did not run'];
      new MutationObserver(() => { seen.run.push(app.dataset.run); seen.ks.push(app.dataset.ks); }).observe(app, { attributes: true, attributeFilter: ['data-run', 'data-ks'] });
      new MutationObserver(() => seen.name.push(name.textContent)).observe(name, { subtree: true, childList: true, characterData: true });
      T.on('rt:reset', e => seen.reset.push(e.generation_changed));
      T.on('rt:tiers', () => { seen.tiers++; });
      T.rt.__fake.reconnect('t05a');
    } }, settle, { eval: installed }],
    also: () => {
      const s = window.__ovSeen || { run: [], ks: [], name: [], reset: [] }, p = [];
      if (s.run.some(r => r !== 'running')) p.push(`data-run flashed: ${s.run.join(',')}`);
      if (new Set(s.ks).size > 1) p.push(`data-ks flashed: ${s.ks.join(',')}`);
      if (s.name.length) p.push(`hero name rewritten: ${s.name.join(' | ')}`);
      if (s.reset.length !== 1 || s.reset[0] !== false) p.push(`replay: rt:reset seen [${s.reset.join(',')}], expected one with generation_changed=false`);
      return p.concat(window.__ovInstalled || []);
    },
  });
}
const PILL = s => ['attr', '#run', 'data-state', s];

/* T-P4-H4 (P5 #27): 启动 goes through the shell's command entry (T.shellc.run). The start-failure
   scenario with an answer to service.start (a scenario changed in the case: one handler added) */
const START_FAILURE = { code: 'runtime_listener_conflict', message_key: 'errors.start.listener_conflict', params: { kind: 'mixed', port: 7890 }, reason_code: 'RuntimeListenerPreflightError' };
const ENV = { epoch: 'rt-fixture-e1', produced_at_ms: 1790000000000, sent_at_ms: 1790000000001 };
function startAnswer(name, reply, base = 'overview_start_failed_listener_conflict') {
  const sc = JSON.parse(readFileSync(new URL(`../../fixtures/scenarios/${base}.json`, import.meta.url), 'utf8'));
  sc.name = name;
  sc.steps.push({ on_invoke: { cmd: 'service.start' }, reply });
  return sc;
}
const startJob = (state, n, extra = {}) => ({ ...ENV, t: 'evt', topic: 'jobs', rev: n, kind: 'edge', seq: n,
  data: { op: 'op-start-2', req_id: '$req_id', owner: '$owner', name: 'service.start', state, started_at_ms: state === 'queued' ? null : 1790000000000, progress: null, result: null, error: null, ...extra } });
// the start runs and fails again with the same failure (the identity the user closed)
const START_AGAIN_FAILS = startAnswer('overview_start_retry_banner', [
  { ...ENV, t: 'ack', id: 3, req_id: '$req_id', owner: '$owner', accepted: true, op: 'op-start-2' },
  startJob('queued', 1), startJob('running', 2), startJob('failed', 3, { error: START_FAILURE }),
]);
// the start is refused before it runs (lifecycle_busy)
const REFUSED_ACK = [{ ...ENV, t: 'ack', id: 3, req_id: '$req_id', owner: '$owner', accepted: false, skipped_reason: 'lifecycle_busy' }];
const START_REFUSED = startAnswer('overview_start_refused', REFUSED_ACK);
// the same from the stopped state (the hero's 启动, #run .run-main; T-P4-H4 修复第 1 轮)
const START_REFUSED_STOPPED = startAnswer('overview_start_refused_stopped', REFUSED_ACK, 'overview_stopped');
// (runs in the page) a start refused before it ran: one service.start, the command-error banner
// says it, the hero hint does not say it again
const refusedOnce = () => {
  const s = { banners: [...document.querySelectorAll('#banners .banner')].map(b => b.dataset.id),
    msg: (document.querySelector('#banners .banner[data-id="cmd-error"] .banner-msg') || {}).textContent || null,
    hint: document.querySelector('#ov-hint').textContent,
    starts: T.rt.__fake.invocations.filter(x => x.kind === 'rt' && x.cmd === 'service.start').map(x => x.args) }, p = [];
  const want = T.rt.errorText({ code: 'lifecycle_busy', message_key: 'errors.lifecycle_busy' });
  if (s.starts.length !== 1) p.push(`service.start sent ${s.starts.length} time(s)`);
  if (!s.banners.includes('cmd-error') || (s.msg || '').trim() !== want.trim()) p.push(`the command-error banner: ${JSON.stringify(s)}`);
  if (s.hint.replace(/\s+/g, '').includes(want.replace(/\s+/g, ''))) p.push(`the refusal is said twice (hero hint too): ${s.hint}`);
  return p;
};
const closeStartup = { eval: () => { const b = document.querySelector('#banners .banner[data-id="startup"] > .icon-btn'); if (b) b.click(); } };

const CASES = [
  // ---- run / start / stop (G05, B, S-R*)
  // F-01 (user feedback 10-27): stopped, the page states facts only. No hint under the name (and no
  // lone dot), no sentence in the exit card, no forecast route, no fallback order on the front card,
  // no 服务未启动 beside the deck title, no kill switch / mode / pool sentences, a quiet chart
  C('overview_stopped', [RUN('stopped'), PILL('stopped'), ['id', '#ov-name', 'overview.hero.state'], ['empty', '#ov-hint'], ['cls', '.ov-hint', 'is-empty', true],
    ['shown', '#ov-dot', false], ['cls', '#ov-dot', 'off', true], ['disabled', '#run .run-main', false],
    ['shown', '#ov-exit .idle-msg', false], ['id', '#ov-prov', 'overview.exit.prov.idle'], ['shown', '#ov-refresh', false], ['empty', '#ov-route'],
    ['empty', '#ov-proto-total'], ['id', '#ov-pool-n', 'overview.pool.value'], ['empty', '#ov-pool-hint'], ['shown', '#ov-ks-hint', false], ['shown', '#ov-mode-hint', false],
    // (round 1: the front card folds to its tier band, the route band goes, the hero says 未启动)
    ['shown', '#ov-deck > .dcard.is-front .dcard-top', false], ['cls', '#ov-deck', 'is-ghost', true], ['cls', '#ov-exit', 'is-noroute', true], ['shown', '#ov-exit .exit-route', false],
    ['id', '#ov-deck > .dcard[data-tier="bastion"] [data-stat]', 'overview.deck.stat.unknown'],
    ['empty', '#ov-deck > .dcard[data-tier="vanguard"] [data-node]'], ['empty', '#ov-deck-meta'], ['id', '#ov-scope', 'overview.traffic.cumulative'], ['cls', '#ov-chart', 'is-paused', true]],
  { also: () => (document.getElementById('ov-name').textContent.trim() === T.t('overview.hero.state', { state: 'stopped' }) ? [] : ['the hero says 未启动']) }),
  // (F-01: the pill says the phase; the hero and the front card do not repeat it; the protocol
  // total is a dash until the Core has loaded the configuration)
  C('overview_start_collecting', [RUN('starting'), PILL('starting'), ['id', '#ov-name', 'overview.hero.state'], ['empty', '#ov-hint'], ['shown', '#ov-dot', false],
    ['has', '#run .run-base .run-step', '1/3'], ['idin', '#run .run-base .run-row:not(.is-out)', 'overview.phase.collect.label'], ['id', '#ov-deck > .dcard[data-tier="vanguard"] [data-stat]', 'overview.deck.stat.collecting'],
    ['empty', '#ov-deck > .dcard.is-front [data-node]'], ['cls', '#ov-deck', 'is-bare', true], ['shown', frontPill, true], ['shown', '#ov-deck > .dcard.is-front [data-node]', false], ['shown', '#ov-exit .idle-msg', false], ['id', '#ov-prov', 'overview.exit.prov.idle'], ['id', '#ov-pool-n', 'overview.pool.value'], ['empty', '#ov-pool-hint'],
    ['id', '#ov-proto-total', 'overview.deck.stat.unknown'], ['cls', '#ov-chart', 'is-paused', true]]),
  C('overview_start_standby', [RUN('starting'), ['empty', '#ov-hint'], ['has', '#run .run-base .run-step', '2/3'], ['idin', '#run .run-base .run-row:not(.is-out)', 'overview.phase.verify.label'],
    ['id', '#ov-deck > .dcard[data-tier="vanguard"] [data-stat]', 'overview.deck.stat.usable'], ['id', '#ov-deck > .dcard[data-tier="sentinel"] [data-stat]', 'overview.deck.stat.collecting'],
    ['id', '#ov-gate-stat', 'overview.gate.stat.verifying'], ['shown', '#ov-refresh', true]]),
  C('overview_ready_validating', [RUN('running'), PILL('running'), ['id', '#ov-hint', 'overview.hero.hint.validating'], ['cls', '#ov-dot', 'busy', true], ['has', '#ov-name', 'Vanguard']]),
  C('overview_cancelling', [RUN('starting'), PILL('stopping'), ['idin', '#run .run-base .run-row:not(.is-out)', 'overview.run.stopping'], ['id', '#ov-name', 'overview.hero.state'],
    ['empty', '#ov-hint'], ['id', frontPill, 'overview.deck.pill'], ['empty', '#ov-deck > .dcard.is-front [data-node]'], ['cls', '#ov-deck', 'is-bare', true], ['shown', frontPill, true], ['shown', '#ov-deck > .dcard.is-front [data-node]', false], ['disabled', '#run .run-act', true], ['disabled', '#run .run-main', true],
    ['shown', '#ov-refresh', true], ['disabled', '#ov-refresh', true]]),
  C('overview_running', [RUN('running'), PILL('running'), ['has', '#ov-name', 'Vanguard'], ['id', '#ov-hint', 'overview.hero.hint.nodes'], ['cls', '#ov-dot', 'ok', true],
    ['has', '#ov-ip4', '203.0.113.24'], ['has', '#ov-ip6', '2001:db8::24'], ['attr', '#ov-ip4', 'translate', 'no'], ['id', '#ov-prov', 'overview.exit.prov.ok'],
    ['id', '#ov-same', 'overview.exit.same_location'], ['cls', '#ov-exit', 'is-last', false], ['shown', '#ov-refresh', true], ['disabled', '#ov-refresh', false],
    ['id', '#ov-route', 'overview.exit.route.direct'], ['has', '#ov-route', 'HK-01'], ['not', '#ov-route', 'GW-Tokyo'],
    ['has', front(), 'HK-01'], ['has', front(), 'vless'], ['id', '#ov-deck > .dcard.is-front [data-stat]', 'overview.deck.stat.usable'],
    ['id', '#ov-gate-stat', 'overview.gate.stat.off'], ['id', '#ov-mode-hint', 'overview.mode.hint.system'], ['has', '#ov-mode-hint', '127.0.0.1:7890'],
    ['id', '#ov-ks-t', 'overview.ks.state'], ['shown', '#ov-ks-hint', false], ['id', '#ov-pool-hint', 'overview.pool.online'], ['empty', '#ov-proto-total'], ['has', '#ov-pleg', 'vless'],
    ['id', '#ov-scope', 'overview.traffic.cumulative'], ['cls', '#ov-chart', 'is-empty', false], ['cls', '#ov-chart', 'is-paused', false], ['shown', '#ov-dot', true],
    ['shown', '#ov-deck > .dcard.is-front .dcard-top', true], ['cls', '#ov-deck', 'is-bare', false], ['cls', '#ov-exit', 'is-noroute', false], ['shown', '#ov-exit .exit-route', true]],
  // (F-01: no tip explaining how the exit check or Gateway works; 刷新 in its icon form is named by
  // its label, T-P5-13)
  { also: () => ['#ov-prov', '#ov-refresh', '.ip-net', '#ov-gate'].map(s => document.querySelector(s))
    .filter(el => el.hasAttribute('data-tip') && !(el.id === 'ov-refresh' && el.classList.contains('is-icon') && el.getAttribute('data-tip') === T.t('overview.exit.refresh.label')))
    .map(el => `${el.id || el.className} carries a tip: ${el.getAttribute('data-tip')}`) }),
  /* T-P5-13 fix round 1 (S3): form (b) of the run pill, which no language of the nine reaches at
     the shipped sizes. The run word is made wider than the pill's room (a constructed letter-spacing,
     as a long translation would be), then a resize (layoutDeck → fitRun): 重启服务 gives way to its
     icon first, the word still does not fit and steps out. It stays readable for a screen reader
     (in the live base layer, visually hidden: not display:none, not aria-hidden), it is the pill's
     tip, and the state dot and the uptime stay. 重启服务's icon form is named by its label, its tip
     leads with it */
  C('overview_run_word_out', [RUN('running'), PILL('running'), ['cls', '#run', 'is-word-out', true], ['cls', '#ov-restart', 'is-icon', true]], {
    scenario: 'overview_running',
    actions: [{ eval: () => {
      const s = new CSSStyleSheet(); s.replaceSync('#run .run-word { letter-spacing: 60px; }');
      document.adoptedStyleSheets = [...document.adoptedStyleSheets, s];
      window.dispatchEvent(new Event('resize'));
    } }, settle],
    also: () => {
      const p = [], w = T.t('overview.run.running'), lab = T.t('overview.restart.label');
      const run = document.getElementById('run'), rs = document.getElementById('ov-restart');
      if (run.getAttribute('data-tip') !== w) p.push(`the pill's tip is "${run.getAttribute('data-tip')}", expected the run word "${w}"`);
      const word = document.querySelector('#run .run-base .run-row:not(.is-out) .run-word');
      if (!word) return [...p, 'no run word in the base layer'];
      const cs = getComputedStyle(word);
      if (word.textContent.trim() !== w) p.push(`the run word is "${word.textContent}"`);
      if (cs.display === 'none' || cs.visibility !== 'visible' || word.closest('[aria-hidden="true"], [hidden], [inert]')) p.push('the run word is not read by a screen reader');
      if (!word.closest('[aria-live]')) p.push('the run word left the live layer');
      if (word.getBoundingClientRect().width > 1.5) p.push(`the run word is still drawn (${word.getBoundingClientRect().width} px)`);
      const up = document.querySelector('#run .run-base .run-row:not(.is-out) .run-up'), dot = document.querySelector('#run .run-base .run-row:not(.is-out) .sdot');
      if (!up || !up.checkVisibility() || !up.textContent.trim()) p.push('the uptime is gone');
      if (!dot || !dot.checkVisibility()) p.push('the state dot is gone');
      if (rs.getAttribute('aria-label') !== lab) p.push(`重启服务 icon: aria-label "${rs.getAttribute('aria-label')}"`);
      if (!(rs.getAttribute('data-tip') || '').startsWith(lab)) p.push(`重启服务 icon: tip "${rs.getAttribute('data-tip')}" does not lead with its label`);
      return p;
    },
  }),
  ...['activation_hold', 'controller_hold', 'facility_hold'].map(s => C(`overview_ready_${s}`, [RUN('running'), ['id', '#ov-hint', 'overview.hero.hint.hold'], ['cls', '#ov-dot', 'warn', true]])),
  C('overview_ready_data_plane_failed', [RUN('running'), ['id', '#ov-hint', 'overview.hero.hint.data_plane_failed'], ['cls', '#ov-dot', 'err', true]]),
  C('overview_ready_fail_closed', [RUN('running'), ['id', '#ov-name', 'overview.hero.state'], ['cls', '#ov-name', 'is-err', true], ['id', '#ov-hint', 'overview.hero.hint.blocked'], ['cls', '#ov-dot', 'err', true]]),
  ...['listener_conflict', 'insufficient_nodes', 'core_timeout', 'geodata_needs_route', 'admin_required'].map(k => C(`overview_start_failed_${k}`, () => {
    const f = T.store.get('failure');
    return [['attr', '#app', 'data-run', 'failed'], ['attr', '#run', 'data-state', 'failed'], ['idin', '#run .run-base .run-row:not(.is-out)', 'overview.run.failed'], ['idin', '#run .run-base .run-row:not(.is-out)', 'overview.run.retry'],
      ['id', '#ov-name', 'overview.hero.state'], ['cls', '#ov-name', 'is-err', true], ['has', '#ov-hint', T.rt.errorText(f).replace(/\s+/g, ' ').trim()],
      ['attr', '#ov-hint', 'data-tip', T.rt.errorText(f)], ['cls', '#ov-dot', 'err', true], ['disabled', '#run .run-act', false]];
  })),
  C('overview_cleanup_pending', [RUN('failed'), PILL('failed'), ['idin', '#run .run-base .run-row:not(.is-out)', 'overview.run.retry_link'],
    ['idin', '#run .run-base .run-row:not(.is-out) .run-seg', 'overview.run.stop'], ['disabled', '#run .run-main', false], ['disabled', '#run .run-act', false]]),
  C('overview_stop_failed', [RUN('running'), PILL('failed'), ['idin', '#run .run-base .run-row:not(.is-out)', 'overview.run.stop_failed'], ['id', '#ov-hint', 'overview.hero.hint.stop_failed'],
    ['cls', '#ov-dot', 'err', true], ['disabled', '#run .run-act', false]], { actions: [click('#run .run-act'), settle] }),
  // refused before it ran (lifecycle_busy, PROTOCOL 7): a note in the hero, no 停止失败
  C('overview_stop_refused', () => [['attr', '#app', 'data-run', 'running'], ['attr', '#run', 'data-state', 'running'], ['noid', '#ov-hint', 'overview.hero.hint.stop_failed'],
    ['has', '#ov-hint', T.rt.errorText({ code: 'lifecycle_busy', message_key: 'errors.lifecycle_busy' }).replace(/\s+/g, ' ').trim()]], { actions: [click('#run .run-act'), settle] }),

  // P5 #27 (T-P4-H4): the user closed the startup banner, then 启动 (the pill's retry) fails again with
  // the same failure: the banner comes back (the shell's runCmd), one service.start {}, no second banner
  C('overview_start_retry_banner', [RUN('failed')], {
    scenario: START_AGAIN_FAILS,
    actions: [settle, closeStartup, { page_ms: 500 }, { eval: () => { window.__h4closed = [...document.querySelectorAll('#banners .banner')].map(b => b.dataset.id); } },
      click('#run .run-act'), { advance_ms: 50 }, settle],
    also: () => {
      // (runs in the page: the shell banners by id, the hero hint, the service.start calls)
      const s = { banners: [...document.querySelectorAll('#banners .banner')].map(b => b.dataset.id),
        msg: (document.querySelector('#banners .banner[data-id="cmd-error"] .banner-msg') || {}).textContent || null,
        hint: document.querySelector('#ov-hint').textContent,
        starts: T.rt.__fake.invocations.filter(x => x.kind === 'rt' && x.cmd === 'service.start').map(x => x.args) }, p = [];
      if ((window.__h4closed || []).includes('startup')) p.push(`the startup banner did not close: ${window.__h4closed}`);
      if (s.starts.length !== 1 || JSON.stringify(s.starts[0]) !== '{}') p.push(`service.start sent: ${JSON.stringify(s.starts)}`);
      if (!s.banners.includes('startup')) p.push(`the startup banner is not back after the same failure: ${s.banners}`);
      if (s.banners.includes('cmd-error')) p.push('a second, generic banner for a start failure');
      return p;
    },
  }),
  // a start refused before it ran: told once, by the shell's command-error banner (not again in the hero)
  C('overview_start_refused', [RUN('failed')], {
    scenario: START_REFUSED,
    actions: [settle, click('#run .run-act'), { advance_ms: 50 }, settle],
    also: refusedOnce,
  }),
  // the same from the stopped state: the hero's 启动 (#run .run-main) takes the same path
  C('overview_start_refused_stopped', [RUN('stopped'), PILL('stopped')], {
    scenario: START_REFUSED_STOPPED,
    actions: [settle, click('#run .run-main'), { advance_ms: 50 }, settle],
    also: refusedOnce,
  }),

  // ---- exit card (G07, G09, S-E*)
  C('overview_exit_ok', [['id', '#ov-prov', 'overview.exit.prov.ok'], ['has', '#ov-ip4', '203.0.113.24'], ['cls', '#ov-exit', 'is-last', false], ['shown', '#ov-src4', false], ['shown', '.ip-meta .ip-net', false], ['shown', '#ov-loc', true]]),
  C('overview_exit_running', [['id', '#ov-prov', 'overview.exit.prov.pending'], ['disabled', '#ov-refresh', true], ['cls', '#ov-exit', 'is-pending', true], ['cls', '#ov-exit', 'is-last', true],
    ['shown', '#ov-exit .ip-row .last-tag', true], ['has', '#ov-ip4', '203.0.113.24']]),
  // (F-01: while a lookup runs, the badge says so; the card adds no sentence)
  C('overview_exit_upgrading', [['id', '#ov-prov', 'overview.exit.prov.pending'], ['shown', '#ov-exit .idle-msg', false], ['disabled', '#ov-refresh', true]]),
  C('overview_exit_partial', [['has', '#ov-ip4', '203.0.113.24'], ['id', '#ov-ip6', 'overview.exit_reason.identity_timeout'], ['not', '#ov-ip6', '2001:'], ['shown', '#ov-copy6', false],
    ['shown', '#ov-copy4', true], ['id', '#ov-prov', 'overview.exit.prov.ok']]),
  C('overview_exit_v6_only', [['id', '#ov-ip4', 'overview.exit_reason.identity_unavailable'], ['has', '#ov-ip6', '2001:db8::24'], ['shown', '#ov-copy4', false], ['shown', '#ov-same', false]],
    { also: () => { const p = []; const loc = document.querySelector('#ov-loc'); if (!loc.textContent.trim()) p.push('IPv6 only: the location line names its country'); return p; } }),
  C('overview_exit_failed', [['id', '#ov-prov', 'overview.exit_reason.identity_timeout'], ['cls', '#ov-prov', 'err', true], ['cls', '#ov-exit', 'is-last', true],
    ['shown', '#ov-exit .ip-row .last-tag', true], ['has', '#ov-ip4', '203.0.113.24']]),
  C('overview_exit_failed_noprev', [['id', '#ov-idle-t', 'overview.exit_reason.exit_unreachable'], ['id', '#ov-idle-s', 'errors.exit.exit_unreachable'], ['attr', '#ov-exit', 'data-state', 'none']]),
  C('overview_exit_cancelled', [['id', '#ov-prov', 'overview.exit.prov.ok'], ['cls', '#ov-exit', 'is-last', true], ['has', '#ov-ip4', '203.0.113.24']]),
  C('overview_exit_superseded', [['id', '#ov-prov', 'overview.exit.prov.ok'], ['cls', '#ov-exit', 'is-last', true], ['has', '#ov-ip4', '203.0.113.24']]),
  C('overview_exit_expired', [['id', '#ov-prov', 'overview.exit.prov.expired'], ['has', '#ov-ip4', '203.0.113.24'], ['cls', '#ov-prov', 'warn', true]]),
  C('overview_exit_changed', [['id', '#ov-prov', 'overview.exit_reason.route_changed'], ['cls', '#ov-exit', 'is-last', true], ['has', '#ov-ip4', '203.0.113.24']]),
  C('overview_exit_conflict', [['id', '#ov-idle-t', 'overview.exit_reason.identity_conflict'], ['attr', '#ov-exit', 'data-state', 'none'], ['shown', '#ov-ip4', false]]),
  C('overview_exit_rotating', [['id', '#ov-idle-t', 'overview.exit_reason.identity_rotating'], ['attr', '#ov-exit', 'data-state', 'none'], ['shown', '#ov-ip4', false]]),
  C('overview_exit_warn', [['id', '#ov-prov', 'overview.exit_reason.system_proxy_capture_inactive'], ['cls', '#ov-prov', 'warn', true], ['has', '#ov-ip4', '203.0.113.24'],
    ['noid', '#ov-prov', 'overview.exit.prov.ok']]),
  C('overview_exit_fallback_verified', [['id', '#ov-prov', 'overview.exit_reason.native_fallback_verified'], ['cls', '#ov-prov', 'ok', true], ['has', '#ov-ip4', '203.0.113.24']]),
  C('overview_exit_route_contract_failed', [['id', '#ov-idle-t', 'overview.exit_reason.route_contract_failed'], ['id', '#ov-idle-s', 'errors.exit.route_contract_failed']]),
  C('overview_exit_waiting', [['id', '#ov-prov', 'overview.exit.prov.pending'], ['shown', '#ov-exit .idle-msg', false]]),
  C('overview_exit_retrying', [['id', '#ov-prov', 'overview.exit.prov.pending'], ['shown', '#ov-exit .idle-msg', false]]),
  C('overview_exit_idle', [['id', '#ov-prov', 'overview.exit.prov.idle'], ['shown', '#ov-exit .idle-msg', false], ['disabled', '#ov-refresh', false]]),
  C('overview_exit_manual', [['id', '#ov-prov', 'overview.exit.prov.ok']], { also: verifiedBadge }),
  C('overview_exit_deferred', [RUN('starting'), ['id', '#ov-prov', 'overview.exit.prov.pending'], ['shown', '#ov-exit .idle-msg', false], ['disabled', '#ov-refresh', true]],
    { also: () => (document.querySelector('#ov-prov').textContent.includes(T.t('overview.exit.prov.pending', { phase: 'deferred', how: 'auto' })) ? [] : ['deferred: the badge says 就绪后查询']) }),
  // G09, automatic lookup off: nothing is waiting for a lookup, so the badge says 未查询 (F-01: no
  // sentence on where to switch it on, no 点刷新查看 beside 刷新)
  C('overview_auto_off', [['shown', '#ov-exit .idle-msg', false], ['disabled', '#ov-refresh', false]], { also: () => (document.querySelector('#ov-prov').textContent.trim() === T.t('overview.exit.prov.idle', { state: 'other' }) ? [] : ['G09: the badge says 未查询']) }),
  C('overview_c2_fields', [['has', '#ov-loc', 'Tokyo'], ['has', '#ov-loc', 'Asia/Tokyo'], ['has', '#ov-net-t', 'AS64500'], ['has', '#ov-net-t', 'Example Networks'], ['id', '#ov-net-type', 'overview.exit.net_type'],
    ['shown', '#ov-src4', true], ['shown', '#ov-src6', false], ['attr', '#ov-loc', 'translate', 'no'], ['attr', '#ov-net-t', 'translate', 'no'], ['id', '#ov-pool-hint', 'overview.pool.online']],
  { also: () => (document.querySelector('#ov-pool-hint').textContent.includes(T.t('overview.pool.online', { rate: 5 / 7, pending: 3 })) ? [] : ['C2 item 7: 3 个待复检']) }),

  // ---- strip: network mode (G08, S-M*), kill switch (J), pool, Bastion
  C('overview_mode_drift', [['id', '#ov-mode-hint', 'overview.mode.hint.drift'], ['shown', '#ov-mode-hint .ov-relink', true]]),
  C('overview_mode_proxy_unknown', [['id', '#ov-mode-hint', 'overview.mode.hint.proxy_unknown']]),
  // (接管方式 is two toggles since 配置整理 P1, data/static.js modeAfterSwitch: these scenarios start
  // on 系统代理 alone, so pressing 虚拟网卡 leads to mixed, and pressing 系统代理, the only one on,
  // turns it off and 虚拟网卡 on: tun)
  C('overview_mode_admin_required', [RUN('stopped'), ['id', '#ov-mode-hint', 'overview.mode.hint.gate_note']], { actions: [click('#ov-mode [data-v="system"]'), settle] }),
  C('overview_mode_cleanup_pending', [RUN('stopped'), ['id', '#ov-mode-hint', 'overview.mode.note.cleanup_pending']], { actions: [click('#ov-mode [data-v="tun"]'), settle] }),
  C('overview_mode_not_applied', [RUN('running'), ['id', '#ov-mode-hint', 'overview.mode.note.not_applied']],
    { actions: [click('#ov-mode [data-v="system"]'), { page_ms: 300 }, click('.ov-confirm [data-a="ok"]'), settle],
      also: () => (document.querySelector('#ov-mode-hint').getAttribute('data-tip') === T.rt.errorText({ code: 'io', message_key: 'errors.mode.apply_failed' }) ? [] : ['apply_error in the tip']) }),
  C('overview_mode_next_start', [RUN('stopped'), ['id', '#ov-mode-hint', 'overview.mode.note.next_start']], { actions: [click('#ov-mode [data-v="tun"]'), settle] }),
  C('overview_ks_disabled', [['id', '#ov-ks-t', 'overview.ks.state'], ['cls', '#ov-ks-dot', 'off', true], ['id', '#ov-hint', 'overview.hero.hint.nodes'], ['cls', '#ov-dot', 'warn', true]],
    { also: () => (document.querySelector('#ov-ks-t').textContent.includes(T.t('overview.ks.state', { state: 'off' })) ? [] : ['已关闭']) }),
  C('overview_ks_blocked', [['id', '#ov-ks-t', 'overview.ks.state'], ['cls', '#ov-ks-dot', 'warn', true], ['cls', '#ov-dot', 'warn', true]],
    { also: () => (document.querySelector('#ov-ks-hint').textContent.includes(T.t('overview.ks.hint', { state: 'blocked' })) ? [] : ['需要管理员权限']) }),
  C('overview_ks_error', [['id', '#ov-ks-t', 'overview.ks.state'], ['cls', '#ov-ks-dot', 'err', true], ['cls', '#ov-ks-val', 'is-err', true]],
    { also: () => (document.querySelector('#ov-ks-hint').getAttribute('data-tip') === T.rt.errorText({ code: 'io', message_key: 'errors.killswitch.command_failed' }) ? [] : ['the error message in the tip']) }),
  // (F-01: 已触发 and the hero say what happened; the cell adds no sentence)
  C('overview_ks_triggered', [['id', '#ov-ks-t', 'overview.ks.state'], ['cls', '#ov-ks-dot', 'err', true], ['id', '#ov-name', 'overview.hero.state'], ['id', '#ov-hint', 'overview.hero.hint.blocked'], ['shown', '#ov-ks-hint', false]]),
  // (T-P5-11 fix round 1, ruling 2: the warning is interface text and shows whole in every
  // language at both sizes, never a short form with the sentence as a tip)
  C('overview_bastion_off', [['id', '#ov-deck > .dcard[data-tier="bastion"] [data-stat]', 'overview.deck.stat.bastion_off']],
    { also: () => {
      // the whole sentence on the band (no ellipsis, no tip standing in for it)
      const st = document.querySelector('#ov-deck > .dcard[data-tier="bastion"] [data-stat]');
      const face = [...st.querySelectorAll('.sw')].filter(x => !x._out).pop() || st;
      const rg = document.createRange(); rg.selectNodeContents(face);
      const over = rg.getBoundingClientRect().width - st.getBoundingClientRect().width;
      // (the pseudo language pads every message with tildes past any band: its cell keeps the page's
      // pseudo allowances, PSEUDO_OVERFLOW; the nine real languages are held to the whole sentence)
      const pseudo = /⟦/.test(face.textContent);
      const p = over > 0.5 && !pseudo ? [`Bastion band cuts "${face.textContent}" by ${over.toFixed(1)} px`] : [];
      if (st.hasAttribute('data-tip')) p.push('the Bastion band carries a tip');
      return p;
    } }),
  C('overview_bastion_collecting', [['has', '#ov-name', 'Bastion'], ['id', '#ov-deck > .dcard[data-tier="bastion"] [data-stat]', 'overview.deck.stat.bastion_filling'], ['id', '#ov-hint', 'overview.hero.hint.fallback'],
    ['has', front(), 'PUB-01']]),
  C('overview_bastion_low', [['id', '#ov-deck > .dcard[data-tier="bastion"] [data-stat]', 'overview.deck.stat.bastion_low'], ['cls', '#ov-deck > .dcard[data-tier="bastion"] [data-stat]', 'warn', true]]),
  C('overview_proto_other', [['has', '#ov-pleg', 'vless']], { also: () => { const rows = document.querySelectorAll('#ov-pleg .prow'); return rows.length === 6 && rows[5].textContent.includes(T.t('overview.proto.other')) ? [] : [`protocol rows: ${rows.length}, the last one is 其他`]; } }),
  C('overview_proto_empty', [['id', '#ov-pleg', 'overview.proto.empty']]),
  C('overview_gateway_missing', [['id', '#ov-gate-stat', 'overview.gate.stat.missing'], ['has', '#ov-gate-stat', 'GW-Tokyo'], ['id', '#ov-name', 'overview.hero.state'],
    ['id', '#ov-route', 'overview.exit.route.unconfirmed']]),
  // Gateway as the exit, the runtime's shape (review 05a M1)
  C('overview_gateway_carrier', [RUN('running'), ['has', '#ov-name', 'Gateway'], ['empty', '#ov-hint'], ['cls', '#ov-dot', 'ok', true],
    ['attr', front(), 'data-tier', 'vanguard'], ['id', frontPill, 'overview.deck.pill_unconfirmed'], ['id', '#ov-deck > .dcard.is-front [data-stat]', 'overview.deck.stat.unconfirmed'],
    ['idin', '#ov-deck > .dcard.is-front [data-node]', 'overview.deck.node.unconfirmed'], ['not', front(), 'GW-Tokyo'], ['id', '#ov-deck > .dcard[data-tier="sentinel"] [data-stat]', 'overview.deck.stat.standby'],
    ['id', '#ov-route', 'overview.exit.route.gateway'], ['has', '#ov-route', 'GW-Tokyo'], ['noid', '#ov-route', 'overview.exit.route.direct'],
    ['id', '#ov-gate-stat', 'overview.gate.stat.current'], ['has', '#ov-gate-stat', 'GW-Tokyo'], ['empty', '#ov-deck-meta']]),
  C('overview_gateway_off', [['id', '#ov-gate-stat', 'overview.gate.stat.off'], ['id', '#ov-route', 'overview.exit.route.direct']]),
  C('overview_tier_fallback', [['has', '#ov-name', 'Sentinel'], ['id', '#ov-hint', 'overview.hero.hint.fallback'], ['has', '#ov-hint', 'Vanguard'], ['attr', front(), 'data-tier', 'sentinel'],
    ['id', '#ov-deck-meta', 'overview.deck.meta.fallback'], ['id', '#ov-deck > .dcard[data-tier="vanguard"] [data-stat]', 'overview.deck.stat.down']]),
  C('overview_tier_unconfigured', [['id', '#ov-deck > .dcard[data-tier="vanguard"] [data-stat]', 'overview.deck.stat.unconfigured']]),
  C('overview_traffic_error', [['id', '#ov-scope', 'overview.traffic.error'], ['cls', '#ov-scope', 'warn', true]]),
  C('overview_history_single', [RUN('running'), ['cls', '#ov-chart', 'is-empty', true]], { also: () => ([...document.querySelectorAll('#ov-chart path')].some(p => /NaN/.test(p.getAttribute('d') || '')) ? ['NaN in a chart path'] : []) }),

  // ---- stale, rt:reset
  C('overview_stale_regions', [RUN('running'), ['has', front(), 'HK-01'], ['idin', '#ov-deck > .dcard.is-front [data-node]', 'overview.common.stale'],
    ['id', '#ov-deck > .dcard[data-tier="bastion"] [data-stat]', 'overview.common.stale_after'], ['has', '#ov-deck > .dcard[data-tier="bastion"] [data-stat]', '25'], ['not', '#ov-deck > .dcard[data-tier="bastion"] [data-stat]', '/60'],
    ['has', '#ov-route', 'HK-01'], ['idin', '#ov-route', 'overview.common.stale']],
  { actions: [gap('route.published'), gap('nodes.list', 'vanguard'), gap('bastion.pool'), settle],
    // the three topics really went stale (the marks are not vacuous); the Bastion band keeps its
    // numbers and says they are stale, whole (T-P5-11 fix round 1: the figure and the mark both shown;
    // F-04 follow-up: the figure is the available count, never over the capacity)
    also: () => {
      const stl = T.store.get('stale') || {}, p = ['route.published', 'nodes.list/vanguard', 'bastion.pool'].filter(k => !stl[k]).map(k => `${k} is not stale`);
      const st = document.querySelector('#ov-deck > .dcard[data-tier="bastion"] [data-stat]');
      const face = [...st.querySelectorAll('.sw')].filter(x => !x._out).pop() || st;
      const rg = document.createRange(); rg.selectNodeContents(face);
      const over = rg.getBoundingClientRect().width - st.getBoundingClientRect().width;
      // (the pseudo language pads every message with tildes past any band: its cell keeps the page's
      // pseudo allowances, PSEUDO_OVERFLOW; the nine real languages are held to the whole sentence)
      const pseudo = /⟦/.test(face.textContent);
      if (over > 0.5 && !pseudo) p.push(`Bastion band cuts "${face.textContent}" by ${over.toFixed(1)} px`);
      if (st.hasAttribute('data-tip')) p.push('the Bastion band carries a tip');
      return p;
    } }),
  C('overview_stale_gateway', [RUN('running'), ['has', '#ov-gate-stat', 'GW-Tokyo'], ['idin', '#ov-gate-stat', 'overview.common.stale'],
    ['has', '#ov-route', 'GW-Tokyo'], ['idin', '#ov-route', 'overview.common.stale']]),
  C('overview_stale', [RUN('running'), ['id', '#ov-hint', 'overview.hero.hint.stale'], ['id', '#ov-scope', 'overview.common.stale'], ['has', '#ov-ip4', '203.0.113.24']]),
  // S-R7: a lost generation shows nothing of it; the name and the pill say 正在连接 (F-01: the hint
  // line does not say it a third time)
  C('overview_reset_generation', [RUN('unknown'), ['idin', '#run .run-base .run-row:not(.is-out)', 'overview.run.connecting'], ['id', '#ov-name', 'overview.hero.state'], ['empty', '#ov-hint'],
    ['attr', '#ov-exit', 'data-state', 'none'], ['shown', '#ov-ip4', false], ['not', '#ov-deck', 'HK-01'], ['id', '#ov-pool-n', 'overview.pool.value'], ['cls', '#ov-chart', 'is-paused', true]]),
  reconnectCase('overview_reconnect_same_epoch', () => {
    // same rev: every replayed frame is deduplicated (nothing is installed again)
    const s = window.__ovSeen || { tiers: -1 };
    window.__ovInstalled = s.tiers === 0 ? [] : [`same rev: rt:tiers fired ${s.tiers} time(s), expected 0`];
  }),
  reconnectCase('overview_reconnect_newrev', () => {
    // newer rev, same content: the topic cache installed the frames again (review 05a S9)
    const s = window.__ovSeen || { tiers: 0 };
    window.__ovInstalled = s.tiers >= 1 ? [] : ['newer rev: the replay installed nothing (rt:tiers never fired)'];
  }),

  // ---- B items (requirement 5): B-01 latency rising, hidden unless both keys allow it
  C('overview_b_items', [RUN('running')], {
    also: () => {
      const on = T.rt.feature('B-01'), p = [];
      const pill = document.querySelector('#ov-deck > .dcard.is-front [data-pt]').textContent;
      const slow = T.t('overview.deck.pill', { state: 'slow' });
      if (on !== pill.includes(slow)) p.push(`B-01 ${on ? 'on' : 'off'}: front pill "${pill}"`);
      const hint = document.querySelector('#ov-hint').textContent, rising = T.t('overview.hero.hint.degraded', { tier: 'Vanguard', ks: 'none' });
      if (on !== (hint.replace(/\s+/g, '') === rising.replace(/\s+/g, ''))) p.push(`B-01 ${on ? 'on' : 'off'}: hero hint "${hint}"`);
      const scope = document.querySelector('#ov-scope').textContent;
      if (T.rt.feature('B-02b') !== scope.includes(T.t('overview.traffic.scope', { scope: 'current' }))) p.push(`B-02b: scope "${scope}"`);
      return p;
    },
  }),
];

// zh_CN strict; pseudo with the overview's documented allowances (overview_i18n.mjs). T-P5-11
// (ruling 3): the real-language cell runs in all nine languages with no allowance (the pseudo
// allowances could hide a real overflow; truncations of the eight other languages go to i18n_check)
export default CASES.flatMap(c => [
  { ...c, langs: LANGS9 },
  { ...c, name: `${c.name}_pseudo`, langs: ['pseudo'], leak_allow: [DEMO_BADGE], overflow_allow: PSEUDO_OVERFLOW },
]);
