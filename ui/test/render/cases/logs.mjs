// Render cases of T-P4-05c (运行日志 on real data): every boundary and failure state of 映射_logs §5
// (L-1 … L-13, O-1 … O-9), the export branches of §4, a same-epoch reconnect, a frozen sidecar
// without is_key_event / run_id, untrusted log text, and the 4,000-row stress and hidden-window
// scenarios that T-P4-09 measures. Scenarios: ui/test/fixtures/scenarios/logs_*.json (written by
// .build/新界面迁移_工单/evidence/T-P4-05c/gen_logs_fixtures.py, checked by
// tools/validate_ui_fixtures.py).
//
// Each case drives the page with the matrix's actions (clicks, page time, frames delivered after
// load) and its expect() asserts the state the mapping asks for. Expected words are read from the
// catalog in the page (T.t), so the same assertion holds in zh_CN and in the pseudo language.
// Besides the matrix's own checks, every cell asserts: no CJK text outside translate="no" in
// pseudo (logsExpect), and the page asked the sidecar only what the scenario registered
// (fixture_invoke, run.mjs).
import { readFileSync } from 'node:fs';
import { logsTextInPage } from './logs_i18n.mjs';

/* ---------------------------------------------------------------- in-page helpers (serialised) */
// delivers `total` MIHOMO raw lines (ASCII) from seq `from` as logs.batch frames of at most 1,000
// records (PROTOCOL §5.5), revs from rev0, through the fixture transport (cases/README.md deliver)
const deliverLines = (total, from, rev0 = 100) => ({
  eval: ([total, from, rev0]) => {
    const T0 = 1790654400000;
    let rev = rev0, seq = from, left = total;
    while (left > 0) {
      const n = Math.min(1000, left), records = [];
      for (let i = 0; i < n; i++, seq++) {
        records.push({ seq, ts_ms: T0 + seq * 5, rx_ms: T0 + seq * 5 + 3, source: 'MIHOMO', level: 'info', category: 'connection', key: 'log.raw', title: '', detail: '',
          raw: `[TCP] 127.0.0.1:${50000 + (seq % 9000)} --> late${seq % 13}.example.net:443`, is_key_event: false, run_id: 'run-42' });
      }
      window.__TESSERACT_DESIGN__.deliver({ t: 'evt', epoch: 'rt-fixture-e1', produced_at_ms: T0, sent_at_ms: T0 + 1, topic: 'logs.batch', rev, kind: 'edge', seq: rev,
        data: { from_seq: records[0].seq, records, dropped: 0, dropped_total: 0, gap: false } });
      rev++; left -= n;
    }
  },
  arg: [total, from, rev0],
});
const clickRowText = text => ({ eval: t => {
  const row = [...document.querySelectorAll('#logs-events .logs-row, #logs-rows .logs-rrow')].find(r => r.textContent.includes(t));
  if (!row) throw new Error('no row: ' + t);
  row.click();
}, arg: text });
const clickMenu = i => ({ eval: k => { const b = document.querySelector(`.menu .menu-i[data-i="${k}"]`); if (!b) throw new Error('no menu item ' + k); b.click(); }, arg: i });
// a notice (toast or banner) button whose label is the catalog's message `id`
const clickNoticeAction = id => ({ eval: k => {
  const want = T.t(k);
  const b = [...document.querySelectorAll('#toast-slot button, #banners button')].find(x => x.textContent.trim() === want);
  if (!b) throw new Error('no notice action ' + k);
  b.click();
}, arg: id });
const MORE = { bundle: 0, json: 1, text: 2, debug: 3, folder: 4, clear: 5 };
const settleMs = (ms = 400) => ({ page_ms: ms });

/* ---------------------------------------------------------------- expectations */
const CJK = /[　-〿㐀-䶿一-鿿豈-﫿＀-￯]/u;
const PLAIN = /^[\d\s:.,×·/–\-—%()（）]*$|^(?:Tesseract|Mihomo|Vanguard|Sentinel|Bastion|Gateway)$/u;

async function logsExpect({ page, lang }, extra) {
  const problems = [];
  const r = await page.evaluate(logsTextInPage);
  if (lang === 'pseudo') {
    for (const s of r.texts) if (CJK.test(s)) problems.push(`CJK text outside translate="no": ${s.slice(0, 60)}`);
    for (const [a, v] of r.attrs) if (CJK.test(v)) problems.push(`CJK ${a} outside translate="no": ${v.slice(0, 60)}`);
    for (const [a, v] of r.attrs) if (!v.includes('⟦') && !PLAIN.test(v)) problems.push(`${a} not from the catalog: ${v.slice(0, 60)}`);
  }
  let metrics = { texts: r.texts.length };
  if (extra) {
    const out = await extra(page, lang);
    const list = Array.isArray(out) ? out : (out && out.problems) || [];
    problems.push(...list);
    if (out && out.metrics) metrics = { ...metrics, ...out.metrics };
  }
  return { problems, metrics };
}
/* the page's state, read in the page */
function pageState() {
  const vis = el => !!el && !el.hidden && el.checkVisibility({ opacityProperty: true, visibilityProperty: true });
  const txt = el => (el ? el.textContent.replace(/\s+/g, ' ').trim() : '');
  const list = sel => [...document.querySelectorAll(sel)].filter(vis);
  const tr = window.__TESSERACT_DESIGN__.transport();
  return {
    rowsE: list('#logs-events .logs-row:not(.logs-kid)').length,
    rowsR: list('#logs-rows .logs-rrow').length,
    rawText: list('#logs-rows .logs-rrow').map(txt).join('\n'),
    eventsText: list('#logs-events .logs-row').map(txt).join('\n'),
    gaps: list('#logs-events .logs-gap').map(txt),
    gapsRaw: list('#logs-rows .logs-gap').map(txt),
    restarts: list('#logs-events .logs-restart').map(txt),
    bounds: list('#logs-events .logs-bound').map(txt),
    boundsRaw: list('#logs-rows .logs-bound').map(txt),
    origin: txt(list('#logs-events .logs-origin')[0]),
    skel: vis(document.querySelector('#logs-skel')),
    empty: vis(document.querySelector('#logs-empty')) ? txt(document.querySelector('#logs-empty')) : null,
    emptyTitle: txt(document.querySelector('#logs-empty .empty-t')),
    // F-01: an empty state is a title and its way out; the line under the title is only the
    // backend's reason of a failed seed
    emptySub: document.querySelector('#logs-empty .empty-s') ? txt(document.querySelector('#logs-empty .empty-s')) : null,
    emptyButtons: [...document.querySelectorAll('#logs-empty button')].map(txt),
    runM: txt(document.querySelector('#logs-run-m')),
    cntOk: vis(document.querySelector('#logs-cnt-ok')),
    runSep: vis(document.querySelector('#logs-run-sep')),
    upd: txt(document.querySelector('#logs-upd-l')),
    feedM: { dot: (document.querySelector('#logs-feed-m .sdot') || { className: '' }).className, text: txt(document.querySelector('#logs-feed-m')), reason: txt(document.querySelector('#logs-feed-m .logs-feed-r')), tip: (document.querySelector('#logs-feed-m') || { getAttribute: () => '' }).getAttribute('data-tip') },
    drop: vis(document.querySelector('#logs-drop')) ? txt(document.querySelector('#logs-drop')) : null,
    debug: vis(document.querySelector('#logs-debug')),
    stale: vis(document.querySelector('#logs-stale')),
    pill: vis(document.querySelector('#logs-pill')) && !document.querySelector('#logs-pill').classList.contains('is-away') ? txt(document.querySelector('#logs-pill')) : null,
    pillCut: txt(document.querySelector('.logs-pill-cut')),
    toasts: [...document.querySelectorAll('#toast-slot .toast')].map(txt),
    rings: document.querySelectorAll('#toast-slot .toast .n-ring').length,
    banners: [...document.querySelectorAll('#banners .banner')].map(txt),
    reader: vis(document.querySelector('.logs-reader')),
    menu: [...document.querySelectorAll('.menu .menu-i')].map(b => ({ i: +b.dataset.i, text: txt(b.querySelector('.menu-l')), hint: b.querySelector('.hint') ? txt(b.querySelector('.hint')) : null, disabled: b.getAttribute('aria-disabled') === 'true' })),
    readerD: document.querySelectorAll('.logs-reader .logs-rd-d').length,
    readerKeys: [...document.querySelectorAll('.logs-reader .logs-rd-kv dt')].filter(vis).map(txt),
    readerShM: document.querySelectorAll('.logs-reader .logs-rd-sh .sh-m').length,
    pop: txt(document.querySelector('.pop .logs-exp')),
    calls: tr.invocations.map(c => (c.kind === 'rt' ? c.cmd : c.name)),
    callArgs: tr.invocations.map(c => [c.kind === 'rt' ? c.cmd : c.name, c.args]),
    pwned: !!window.__pwned,
  };
}
const st = page => page.evaluate(pageState);
const tt = (page, id, p) => page.evaluate(([k, a]) => T.t(k, a || undefined), [id, p || null]);
// both sides normalised as pageState's txt() does (\s+ → one space, trimmed): a catalog message may
// group digits with U+202F / U+00A0 (fr_FR, ru_RU 「1 208」), which txt() has made a plain space
// (T-P5-04, L-4)
const ws = s => String(s == null ? '' : s).replace(/\s+/g, ' ').trim();
const has = (hay, needle) => ws(hay).includes(ws(needle));
const count = (arr, x) => arr.filter(v => v === x).length;
function noticeIs(s, text) { return s.toasts.some(x => has(x, text)) || s.banners.some(x => has(x, text)); }

/* ---------------------------------------------------------------- allowances */
// Truncation by design (every language) and the pseudo language's widths (same entries and reasons
// as cases/logs_i18n.mjs, T-P4-04d): layout is T-P4-09's.
const DESIGN = [
  { selector: '^#logs-events > div\\.li\\.logs-row[^ ]* > span\\.logs-main[^ ]* > span\\.logs-[dt][^ ]* > button\\.ttag', why: '关键事件行单行省略：标题、详情是记录原文（数据，translate="no"），其中内嵌的层级标签（代号是数据，角色词 span.r 是目录文字）随所在数据行的省略号被截。决定_P5裁定 P5-e 第 5 条要求界面文字不以省略号显示；这里登记为已知例外，待 Claude 裁定（T-P5-13 修复记录第 1 轮 S1）：标签嵌在句中，不能像原始日志那样把数据先让、标签移到行尾；完整文字在阅读器里，标签的无障碍名称（logs.tier_link.aria）含角色词' },
];
const PSEUDO_LENGTH = [
  { selector: '^#logs-follow > span\\.lab', why: '伪语言：「实时跟随 / 已暂停」按钮的标签宽度，按设计定宽或单行；没有另开布局单，真实语言的溢出由 P5 用全语言矩阵复核（第九波裁定，T-P4-12）' },
  { selector: '^#logs-view > button\\.seg-btn', why: '伪语言：视图分段按钮宽度，按设计定宽或单行；没有另开布局单，真实语言的溢出由 P5 用全语言矩阵复核（第九波裁定，T-P4-12）' },
  { selector: '^#logs-level > button\\.seg-btn', why: '伪语言：级别分段按钮宽度，按设计定宽或单行；没有另开布局单，真实语言的溢出由 P5 用全语言矩阵复核（第九波裁定，T-P4-12）' },
  { selector: '^#logs-total$', why: '伪语言：页脚条数的宽度，按设计定宽或单行；没有另开布局单，真实语言的溢出由 P5 用全语言矩阵复核（第九波裁定，T-P4-12）' },
  { selector: '^#logs-rows > div\\.logs-chunk[^ ]* > div\\.logs-rrow[^ ]* > span\\.logs-c[^ ]* > span\\.sev', why: '伪语言：原始日志表的级别列按当前语言最宽的级别词定宽，上限 108px（T-P5-04）；伪语言的级别词（约 85px）超过上限。9 种真实语言都在上限内，由全语言矩阵核对' },
  { selector: '^#logs-events > div\\.li\\.logs-row[^ ]* > span\\.sev', why: '伪语言：关键事件行的级别列按当前语言最宽的级别词定宽，上限 88px（T-P5-04）；伪语言的级别词（约 85px）超过上限。9 种真实语言都在上限内，由全语言矩阵核对' },
  { selector: '^#mi\\d+ > span\\.hint', why: '伪语言：「更多」菜单项右侧的提示句末尾的波浪号补长不能换行，宽过菜单（T-P5-11 盒子越界规则；9 种真实语言在同一用例下都不越界，由扩到 9 种语言的正式矩阵核对）' },
  { selector: '^#layer > div\\.pop[^ ]* > div\\.logs-exp[^ ]* > div\\.callout[^ ]* > div\\.callout-body$', why: '伪语言：导出弹层里隐私提醒（logs.bundle.privacy）末尾的波浪号补长是一个不能换行的词，宽过提醒框（T-P5-11 第 1 轮修复：盒子越界规则量到它；9 种真实语言在同一用例下都不越界，由扩到 9 种语言的正式矩阵核对）' },
  { selector: '^#logs-empty > div\\.empty[^ ]* > div[^ ]* > div\\.empty-[ts]', why: '伪语言：日志空状态的标题与说明末尾的波浪号补长不能换行（T-P5-11 盒子越界规则；9 种真实语言在同一用例下都不越界，由扩到 9 种语言的正式矩阵核对）' },
  { selector: '^#logs-sc-raw > div\\.logs-grid > div\\.logs-gh[^ >]* > span:nth-of-type\\(1\\)$', why: '伪语言（T-P5-13 修复第 1 轮）：原始日志表「时间」列定宽 78px（放 hh:mm:ss 的等宽时刻；左 14px、右 10px 内边距，内容盒 54px），伪语言的列头「时间」补长后约 63px，以省略号结束。修复第 1 轮起检查器按内容盒、小数精度量省略号才量到它（旧检查器只比内边距盒、差 1px 以上才判）。9 种真实语言的列头都放得下，由全语言矩阵核对' },
  { selector: '^#logs-rows > div\\.logs-chunk[^ ]* > div\\.logs-rrow[^ ]* > span\\.logs-c\\.logs-src', why: '伪语言（T-P5-13 省略号规则）：原始日志表的来源列按当前语言最宽的来源词或列头定宽，上限 150px；伪语言的「未知来源」连同波浪号补长宽过上限，以省略号结束（悬停行时阅读器给全文）。9 种真实语言都在上限内、来源词不省略，由全语言矩阵核对' },
];

/* ---------------------------------------------------------------- the hidden window, measured */
// logs_hidden_30s's own steps are all played while the page connects, in one run with no frame in
// between, so its end state cannot see a write made while the window was hidden. These cases keep
// only its connect and seed (the scenario is read and cut here, as smoke.mjs does: the 150
// batches would otherwise be a second copy of the same fixture) and play the 30 s themselves:
// hide the window, one batch every 200 ms of page time (rAF and timers run between batches),
// show it again; a MutationObserver counts the writes in each phase (review of T-P4-05c).
function hiddenScenario(name, { elsewhere = false } = {}) {
  const sc = JSON.parse(readFileSync(new URL('../../fixtures/scenarios/logs_hidden_30s.json', import.meta.url), 'utf8'));
  const steps = sc.steps.filter(s => !(s.deliver && (s.deliver.t === 'host:visibility' || s.deliver.topic === 'logs.batch')) && !('advance_ms' in s));
  // 总览 asks app.info once more when it is shown (the answer is the same)
  if (elsewhere) steps.push(...steps.filter(s => s.on_invoke && s.on_invoke.cmd === 'app.info'));
  return { ...sc, name, desc: `${sc.desc} (cut: connect and seed only; the case hides the window and plays the 30 s in page time)`, steps };
}
// withBadge: the page is elsewhere (the badge counts), three error key events arrive while hidden,
// the first in the first logs.batch frame (instant: the first frame of the topic, review S3)
const hiddenRun = withBadge => ({ eval: async badge => {
  const D = window.__TESSERACT_DESIGN__, C = window.__fixtureClock, E = 'rt-fixture-e1', T0 = 1790654400000;
  const sels = badge ? { badge: '.nav-item[data-nav="logs"]' } : { rows: '#logs-rows', events: '#logs-events', foot: '#logs-foot' };
  const tally = { hidden: {}, shown: {} };
  let phase = null;
  const add = (k, n) => { if (phase) tally[phase][k] = (tally[phase][k] || 0) + n; };
  const obs = Object.entries(sels).map(([k, sel]) => {
    const el = document.querySelector(sel); if (!el) throw new Error('no ' + sel);
    const o = new MutationObserver(ms => add(k, ms.length));
    o.observe(el, { childList: true, subtree: true, characterData: true, attributes: true });
    return [k, o];
  });
  const take = () => obs.forEach(([k, o]) => add(k, o.takeRecords().length));
  await C.advance(400);
  take();
  phase = 'hidden';
  D.deliver({ t: 'host:visibility', visible: false, epoch: E });
  await C.advance(0);
  const rec = (seq, err) => (err
    ? { seq, ts_ms: T0 + seq * 20, rx_ms: T0 + seq * 20 + 9, source: 'TESSERACT', level: 'error', category: 'update', key: 'update.sources_failed', title: 'Source refresh failed', detail: 'All 5 sources failed.', raw: '', is_key_event: true, run_id: 'run-42' }
    : { seq, ts_ms: T0 + seq * 20, rx_ms: T0 + seq * 20 + 9, source: 'MIHOMO', level: 'info', category: 'connection', key: 'log.raw', title: '', detail: '', raw: `[TCP] 127.0.0.1:${50000 + seq} --> h${seq % 60}.example.net:443`, is_key_event: false, run_id: 'run-42' });
  for (let i = 0; i < 150; i++) {
    const from = 15 + i * 10, records = [];
    for (let j = 0; j < 10; j++) records.push(rec(from + j, badge && j === 0 && i % 50 === 0));
    D.deliver({ t: 'evt', epoch: E, produced_at_ms: T0, sent_at_ms: T0 + 1, topic: 'logs.batch', rev: i + 1, kind: 'edge', seq: i + 1, data: { from_seq: from, records, dropped: 0, dropped_total: 0, gap: false } });
    // halfway: logs.feed changes (Mihomo reconnecting, 3 dropped): the footer must wait too (review S5)
    if (i === 75) D.deliver({ t: 'evt', epoch: E, produced_at_ms: T0, sent_at_ms: T0 + 1, topic: 'logs.feed', rev: 2, kind: 'snapshot',
      data: { state: 'reconnecting', level: 'info', updated_at_ms: T0, dropped: { debug: 0, info: 3, warn: 0, error: 0, critical: 0, total: 3 }, run_id: 'run-42' } });
    await C.advance(200);
  }
  take();
  phase = 'shown';
  D.deliver({ t: 'host:visibility', visible: true, epoch: E });
  await C.advance(400);
  take();
  obs.forEach(([, o]) => o.disconnect());
  const b = document.querySelector('.nav-item[data-nav="logs"] .logs-navbadge');
  window.__hiddenWrites = { tally, rows: [...document.querySelectorAll('#logs-rows .logs-rrow')].filter(r => r.checkVisibility()).length, badge: b ? b.textContent : null };
}, arg: withBadge });
const hiddenWrites = page => page.evaluate(() => window.__hiddenWrites);

/* ---------------------------------------------------------------- the cases */
const RAW = [{ click: '#logs-view [data-v="raw"]' }, settleMs()];
/* 待用户认可表第 11 行 (T-P4-H4): the filter's state as the popover shows it (chips pressed by kind),
   the filter button's badge, and the raw rows' text; the popover is closed again */
const ALERT_PARAMS = { category: 'alert', level: 'all', runOnly: false };
const keepFilter = key => ({ eval: k => {
  const btn = document.querySelector('#logs-filter');
  btn.click();
  const chips = [...document.querySelectorAll('.pop .fchip')].map(c => [c.dataset.k, c.dataset.v, c.getAttribute('aria-pressed') === 'true']);
  btn.click();
  const badge = document.querySelector('#logs-fbadge');
  window[k] = { chips, badge: badge && !badge.hidden ? badge.textContent : null, aria: btn.getAttribute('aria-label'),
    rows: [...document.querySelectorAll('#logs-rows .logs-rrow')].filter(r => r.checkVisibility()).map(r => r.textContent.replace(/\s+/g, ' ').trim()) };
}, arg: key });
const filterState = keepFilter('__h4f');
const pressChip = (k, v) => ({ eval: ([kind, val]) => {
  const btn = document.querySelector('#logs-filter');
  btn.click();
  const c = document.querySelector(`.pop .fchip[data-k="${kind}"][data-v="${val}"]`);
  if (c) c.click();
  btn.click();
}, arg: [k, v] });
async function categoryExpect(page) {
  const f = await page.evaluate(() => window.__h4f), mid = await page.evaluate(() => window.__h4mid || null), p = [];
  // (the _again case: the source chip really was chosen before the jump)
  if (mid && (mid.badge !== '1' || !mid.chips.some(c => c[0] === 'src' && c[1] === 'MIHOMO' && c[2]))) p.push(`before the jump: ${JSON.stringify(mid).slice(0, 300)}`);
  const pressed = f.chips.filter(c => c[2]).map(c => `${c[0]}:${c[1]}`);
  if (pressed.join() !== 'cat:alert') p.push(`chips pressed: ${pressed.join() || 'none'} (want cat:alert only)`);
  if (f.badge !== '1' || f.aria !== await tt(page, 'logs.bar.filter_aria', { n: 1 })) p.push(`filter button: ${JSON.stringify({ badge: f.badge, aria: f.aria })}`);
  if (f.rows.length !== 1 || !f.rows[0].includes('Alert delivered')) p.push(`raw rows: ${JSON.stringify(f.rows).slice(0, 300)} (want the one alert line)`);
  return p;
}
const CASES = [
  /* 待用户认可表第 11 行 (T-P4-H4): T.go('logs', {category}) opens the page filtered to that category
     (提醒区「查看全部」 sends {category:'alert', level:'all', runOnly:false}) */
  { name: 'logs_enter_category', scenario: 'logs_i18n_running', params: ALERT_PARAMS, actions: [...RAW, filterState],
    expect: ctx => logsExpect(ctx, categoryExpect) },
  // on the page already, with a source chosen: the jump is to the category's lines, the source goes
  { name: 'logs_enter_category_again', scenario: 'logs_i18n_running', actions: [...RAW, pressChip('src', 'MIHOMO'), settleMs(), keepFilter('__h4mid'), { go: 'logs', params: ALERT_PARAMS }, settleMs(), filterState],
    expect: ctx => logsExpect(ctx, categoryExpect) },
  /* L-1 */
  { name: 'logs_seed_loading', scenario: 'logs_seed_loading',
    expect: ctx => logsExpect(ctx, async page => {
      const s = await st(page), p = [];
      if (!s.skel) p.push('L-1: no loading skeleton');
      if (!has(s.upd, await tt(page, 'logs.load.loading'))) p.push(`L-1: footer says ${s.upd}`);
      if (s.rowsE || s.rowsR) p.push('L-1: live lines shown before the seed (they wait in the buffer)');
      if (!s.calls.includes('logs.tail')) p.push('L-1: logs.tail not asked');
      return p;
    }) },
  /* L-2 */
  { name: 'logs_seed_failed', scenario: 'logs_seed_failed',
    expect: ctx => logsExpect(ctx, async page => {
      let s = await st(page); const p = [];
      if (!s.empty || !has(s.empty, await tt(page, 'logs.load.failed_title'))) p.push(`L-2: empty state ${s.empty}`);
      const retry = await tt(page, 'logs.load.retry');
      if (!s.emptyButtons.includes(retry)) p.push(`L-2: no retry button (${s.emptyButtons})`);
      // the backend's reason (errors.*) stays under the title (F-01 keeps errors)
      if (!s.emptySub) p.push('L-2: the reason of the failed seed is not shown');
      // the retry asks again (the scenario's second logs.tail answers with the history)
      await page.evaluate(k => [...document.querySelectorAll('#logs-empty button')].find(b => b.textContent.trim() === k).click(), retry);
      await page.evaluate(() => window.__fixtureClock.advance(400)); await page.waitForTimeout(100);
      s = await st(page);
      if (s.empty || !s.rowsE) p.push(`L-2: the retry did not load the history (${s.rowsE} rows, empty ${s.empty})`);
      return p;
    }) },
  /* L-3 */
  { name: 'logs_seed_snapshot_retry', scenario: 'logs_seed_snapshot_retry', actions: RAW,
    expect: ctx => logsExpect(ctx, async page => {
      const s = await st(page), p = [];
      if (count(s.calls, 'logs.tail') !== 4) p.push(`L-3: logs.tail asked ${count(s.calls, 'logs.tail')} times (page 1, expired page 2, page 1, page 2)`);
      if (!has(s.rawText, 'Monitor heartbeat ok') || !has(s.rawText, 'Desktop runtime start requested')) p.push('L-3: the refetched group is not complete');
      if (s.empty || s.banners.length) p.push('L-3: the refetch was not silent');
      return p;
    }) },
  /* L-4 */
  { name: 'logs_seed_evicted', scenario: 'logs_seed_evicted',
    expect: ctx => logsExpect(ctx, async page => {
      const s = await st(page), want = await tt(page, 'logs.list.origin.evicted', { n: 1208 });
      return has(s.origin, want) ? [] : [`L-4: history row "${s.origin}" lacks "${want}"`];
    }) },
  /* L-5 */
  { name: 'logs_live_gap', scenario: 'logs_live_gap',
    expect: ctx => logsExpect(ctx, async page => {
      const s = await st(page), p = [];
      const want = [];
      for (const n of [3, 4, 5, 4]) want.push(await tt(page, 'logs.list.gap', { n }));
      if (JSON.stringify(s.gaps) !== JSON.stringify(want)) p.push(`L-5: gap rows ${JSON.stringify(s.gaps)}, expected ${JSON.stringify(want)}`);
      const dropped = await tt(page, 'logs.footer.dropped', { n: 9 });
      if (s.drop !== dropped) p.push(`L-5: footer dropped ${s.drop}, expected ${dropped} (logs.feed.dropped.total)`);
      return p;
    }) },
  { name: 'logs_live_gap_raw', scenario: 'logs_live_gap', actions: RAW,
    expect: ctx => logsExpect(ctx, async page => ((await st(page)).gapsRaw.length === 4 ? [] : ['L-5: gap rows missing from 原始日志 (both views show them)'])) },
  /* L-6 */
  { name: 'logs_source_boundary', scenario: 'logs_source_boundary',
    expect: ctx => logsExpect(ctx, async page => {
      const s = await st(page), p = [];
      const toStdout = await tt(page, 'logs.list.boundary', { to: 'stdout' }), toApi = await tt(page, 'logs.list.boundary', { to: 'mihomo_api' });
      if (s.bounds.length !== 2 || !has(s.bounds[0], toStdout) || !has(s.bounds[1], toApi)) p.push(`L-6: boundary rows ${JSON.stringify(s.bounds)}`);
      if (s.bounds.some(b => has(b, 'Core log source boundary'))) p.push('L-6: the bridge\'s fixed English title is shown');
      if (!has(s.feedM.dot, 'warn')) p.push(`L-6 / L-9: Mihomo on stdout is not amber (${s.feedM.dot})`);
      // F-01: the state word says it (经标准输出（断流中）); no tip explains it again
      if (s.feedM.tip != null) p.push(`L-9 / F-01: stdout tip "${s.feedM.tip}" (the state word alone)`);
      if (!has(s.feedM.text, await tt(page, 'logs.footer.feed.state', { state: 'stdout' }))) p.push(`L-9: Mihomo says ${s.feedM.text}`);
      // a filter does not hide them (PROTOCOL §16 LogPage)
      await page.click('#logs-level [data-v="error"]'); await page.evaluate(() => window.__fixtureClock.advance(400)); await page.waitForTimeout(80);
      if ((await st(page)).bounds.length !== 2) p.push('L-6: a level filter hid the boundary rows');
      await page.click('#logs-level [data-v="all"]'); await page.evaluate(() => window.__fixtureClock.advance(400));
      return p;
    }) },
  { name: 'logs_source_boundary_raw', scenario: 'logs_source_boundary', actions: RAW,
    expect: ctx => logsExpect(ctx, async page => ((await st(page)).boundsRaw.length === 2 ? [] : ['L-6: boundary rows missing from 原始日志'])) },
  /* L-7 */
  { name: 'logs_sidecar_restart', scenario: 'logs_sidecar_restart', actions: RAW,
    expect: ctx => logsExpect(ctx, async page => {
      const s = await st(page), p = [];
      const restarts = await page.evaluate(() => [...document.querySelectorAll('#logs-rows .logs-restart')].length);
      if (restarts !== 1) p.push(`L-7: ${restarts} restart separators in 原始日志`);
      if (has(s.rawText, 'Monitor heartbeat ok')) p.push('L-7: rows of the old generation kept');
      if (s.rowsR !== 5) p.push(`L-7: ${s.rowsR} rows of the new generation (3 seeded + 2 live)`);
      return p;
    }) },
  /* L-8 */
  { name: 'logs_mihomo_starting', scenario: 'logs_mihomo_starting', actions: [{ click: '#logs-more' }, settleMs()],
    expect: ctx => logsExpect(ctx, async page => {
      const s = await st(page), p = [];
      if (!has(s.feedM.dot, 'busy') || !has(s.feedM.text, await tt(page, 'logs.footer.feed.state', { state: 'connecting' }))) p.push(`L-8: Mihomo ${s.feedM.dot} ${s.feedM.text}`);
      const dbg = s.menu.find(m => m.i === MORE.debug);
      if (!dbg || !dbg.disabled) p.push('L-8: DEBUG capture offered while the service starts');
      return p;
    }) },
  /* L-9 */
  { name: 'logs_feed_reconnecting', scenario: 'logs_feed_reconnecting',
    expect: ctx => logsExpect(ctx, async page => {
      const s = await st(page);
      return has(s.feedM.dot, 'warn') && has(s.feedM.text, await tt(page, 'logs.footer.feed.state', { state: 'reconnecting' })) ? [] : [`L-9: Mihomo ${s.feedM.dot} ${s.feedM.text}`];
    }) },
  /* L-10 */
  { name: 'logs_feed_controller_disabled', scenario: 'logs_feed_controller_disabled',
    expect: ctx => logsExpect(ctx, async page => {
      const s = await st(page);
      return s.feedM.reason === await tt(page, 'logs.feed_reason.controller_disabled') ? [] : [`L-10: reason "${s.feedM.reason}"`];
    }) },
  /* L-11 */
  { name: 'logs_state_stale', scenario: 'logs_state_stale',
    expect: ctx => logsExpect(ctx, async page => ((await st(page)).stale ? [] : ['L-11: 数据未更新 not shown'])) },
  /* L-12 */
  { name: 'logs_paused_overflow', scenario: 'logs_paused_overflow', actions: [...RAW, { click: '#logs-follow' }, settleMs(), deliverLines(4300, 15), settleMs()],
    expect: ctx => logsExpect(ctx, async page => {
      const s = await st(page), p = [];
      const cut = await tt(page, 'logs.pill.cut', { n: 300 });
      if (s.pillCut !== cut) p.push(`L-12: pill says "${s.pillCut}", expected "${cut}"`);
      if (!s.pill) p.push('L-12: no held-lines pill');
      return p;
    }) },
  /* L-13 */
  { name: 'logs_live_title_language', scenario: 'logs_live_title_language',
    expect: ctx => logsExpect(ctx, async page => {
      const s = await st(page), p = [];
      if (!has(s.eventsText, 'Traffic on Sentinel')) p.push('L-13: the live key event is not shown as the bridge rendered it');
      if (s.calls.includes('logs.query')) p.push('L-13: logs.query asked (C1 renders live titles; no fallback)');
      return p;
    }) },

  /* O-1: the export branches (映射_logs §4) */
  { name: 'logs_export_filtered_done', scenario: 'logs_export_filtered_done', actions: [{ click: '#logs-more' }, settleMs(), clickMenu(MORE.json), settleMs(800)],
    expect: ctx => logsExpect(ctx, async page => {
      let s = await st(page); const p = [];
      const args = s.callArgs.filter(([c]) => c === 'logs.exportFiltered').map(([, a]) => a);
      const save = s.callArgs.filter(([c]) => c === 'host.saveFile').map(([, a]) => a);
      if (JSON.stringify(save) !== JSON.stringify([{ purpose: 'filtered', format: 'json' }])) p.push(`O-1: host.saveFile ${JSON.stringify(save)}`);
      if (args.length !== 1 || 'filter' in args[0] || !Array.isArray(args[0].seqs) || args[0].save_ref !== 'save-1') p.push(`O-1 / R9: logs.exportFiltered ${JSON.stringify(args)}`);
      else if (args[0].seqs.length !== s.rowsE) p.push(`O-1: ${args[0].seqs.length} seqs for the ${s.rowsE} key events shown`);
      const done = await tt(page, 'logs.export.done', { n: 12 }), miss = await tt(page, 'logs.export.missing', { n: 2 });
      if (!noticeIs(s, done) || !noticeIs(s, miss)) p.push(`O-1: notice ${JSON.stringify(s.toasts)} lacks "${done}" / "${miss}" (G41)`);
      // 在文件夹中显示 → host.reveal
      await page.evaluate(k => { const want = T.t(k); [...document.querySelectorAll('#toast-slot button')].find(x => x.textContent.trim() === want).click(); }, 'logs.export.reveal');
      await page.evaluate(() => window.__fixtureClock.advance(400)); await page.waitForTimeout(80);
      s = await st(page);
      if (!s.callArgs.some(([c, a]) => c === 'host.reveal' && a && a.ref === 'exp-1')) p.push('O-1: 在文件夹中显示 did not call host.reveal{ref}');
      return p;
    }) },
  { name: 'logs_export_bundle_done', scenario: 'logs_export_bundle_done', actions: [{ click: '#logs-more' }, settleMs(), clickMenu(MORE.bundle), settleMs(1200)],
    expect: ctx => logsExpect(ctx, async page => {
      const s = await st(page), p = [];
      if (!s.callArgs.some(([c, a]) => c === 'host.saveFile' && a.purpose === 'bundle' && a.format === 'zip')) p.push('O-1: host.saveFile{bundle, zip} not asked');
      if (!s.callArgs.some(([c, a]) => c === 'logs.exportBundle' && a.save_ref === 'save-2')) p.push('O-1: logs.exportBundle{save_ref} not asked');
      if (!has(s.pop, 'monitor.log')) p.push(`O-1: the bundle popover lists no files (${s.pop})`);
      if (has(s.pop, 'Desktop') || has(s.pop, '.zip')) p.push('O-1: the popover names a place or a file name the page cannot know');
      return p;
    }) },
  { name: 'logs_export_cancelled', scenario: 'logs_export_cancelled', actions: [{ click: '#logs-more' }, settleMs(), clickMenu(MORE.text), settleMs(800)],
    expect: ctx => logsExpect(ctx, async page => {
      const s = await st(page), p = [];
      if (s.calls.some(c => c.startsWith('logs.export'))) p.push('O-1: a cancelled Save As started an export');
      if (s.toasts.length || s.banners.length) p.push(`O-1: a cancelled Save As said something (${s.toasts} ${s.banners})`);
      return p;
    }) },
  { name: 'logs_export_dialog_open', scenario: 'logs_export_dialog_open', actions: [{ click: '#logs-more' }, settleMs(), clickMenu(MORE.json), settleMs(800)],
    expect: ctx => logsExpect(ctx, async page => {
      const s = await st(page), want = await tt(page, 'logs.export.dialog_open');
      return noticeIs(s, want) && !s.calls.some(c => c.startsWith('logs.export')) ? [] : [`B8: notices ${JSON.stringify(s.banners)} lack "${want}"`];
    }) },
  { name: 'logs_export_failed', scenario: 'logs_export_failed', actions: [{ click: '#logs-more' }, settleMs(), clickMenu(MORE.json), settleMs(800)],
    expect: ctx => logsExpect(ctx, async page => {
      const s = await st(page), want = await tt(page, 'logs.export.failed');
      return noticeIs(s, want) ? [] : [`O-1: failed job: banners ${JSON.stringify(s.banners)} lack "${want}"`];
    }) },
  { name: 'logs_export_interrupted', scenario: 'logs_export_interrupted', actions: [{ click: '#logs-more' }, settleMs(), clickMenu(MORE.json), settleMs(800)],
    expect: ctx => logsExpect(ctx, async page => {
      const s = await st(page), want = await tt(page, 'logs.export.interrupted');
      return noticeIs(s, want) ? [] : [`O-1: cancelled job: banners ${JSON.stringify(s.banners)} lack "${want}"`];
    }) },
  { name: 'logs_export_reveal_gone', scenario: 'logs_export_reveal_gone', actions: [{ click: '#logs-more' }, settleMs(), clickMenu(MORE.json), settleMs(800), clickNoticeAction('logs.export.reveal'), settleMs(800)],
    expect: ctx => logsExpect(ctx, async page => {
      const s = await st(page), want = await tt(page, 'logs.export.gone');
      return noticeIs(s, want) ? [] : [`O-1: host.reveal not_allowed: banners ${JSON.stringify(s.banners)} lack "${want}"`];
    }) },
  /* O-2 */
  { name: 'logs_export_empty', scenario: 'logs_export_empty', actions: [{ click: '#logs-more' }, settleMs()],
    expect: ctx => logsExpect(ctx, async page => {
      const s = await st(page), p = [];
      for (const k of ['json', 'text', 'clear']) { const m = s.menu.find(x => x.i === MORE[k]); if (!m || !m.disabled) p.push(`O-2: ${k} offered on an empty view`); }
      const b = s.menu.find(x => x.i === MORE.bundle);
      if (!b || b.disabled) p.push('O-2: the bundle needs no rows: it stays available');
      // F-01: the menu's only side note is the JSON export's count (no 日志、配置与运行状态, no
      // 服务运行时可用 on the disabled DEBUG item, no 可撤销，不删除)
      const hints = s.menu.filter(m => m.hint != null).map(m => [m.i, m.hint]);
      if (JSON.stringify(hints) !== JSON.stringify([[MORE.json, ws(await tt(page, 'logs.more.export_count', { n: 0 }))]])) p.push(`F-01: menu hints ${JSON.stringify(hints)}`);
      // F-01: nothing recorded yet, stopped: the empty state is its title alone; the run strip says
      // 上次运行 — with no 没有错误或警告; Mihomo's state is the word alone, without a tip
      if (s.emptyTitle !== ws(await tt(page, 'logs.empty.none.events_title')) || s.emptySub != null) p.push(`F-01: empty state ${JSON.stringify([s.emptyTitle, s.emptySub])}`);
      if (s.runM !== '—' || s.cntOk || s.runSep) p.push(`F-01: run strip ${JSON.stringify({ meta: s.runM, ok: s.cntOk, sep: s.runSep })}`);
      // (the dot, the name and the state word are adjacent spans: textContent has no space between them)
      if (s.feedM.text !== ws(`Mihomo${await tt(page, 'logs.footer.feed.state', { state: 'off' })}`) || s.feedM.tip != null) p.push(`F-01: Mihomo footer ${JSON.stringify(s.feedM)}`);
      return p;
    }) },
  /* F-01 (user feedback 10-27: no sentences that explain what the user already knows): the empty
     states read as a title and the way out, on the same empty, stopped scenario */
  { name: 'logs_empty_none_raw', scenario: 'logs_export_empty', actions: RAW,
    expect: ctx => logsExpect(ctx, async page => {
      const s = await st(page);
      return s.emptyTitle === ws(await tt(page, 'logs.empty.none.raw_title')) && s.emptySub == null && !s.emptyButtons.length ? [] : [`F-01: raw empty state ${JSON.stringify([s.emptyTitle, s.emptySub, s.emptyButtons])}`];
    }) },
  { name: 'logs_empty_paused', scenario: 'logs_export_empty', actions: [{ click: '#logs-follow' }, settleMs()],
    expect: ctx => logsExpect(ctx, async page => {
      const s = await st(page), p = [];
      if (s.emptyTitle !== ws(await tt(page, 'logs.empty.paused.title')) || s.emptySub != null) p.push(`F-01: paused empty state ${JSON.stringify([s.emptyTitle, s.emptySub])}`);
      if (JSON.stringify(s.emptyButtons) !== JSON.stringify([ws(await tt(page, 'common.action.continue'))])) p.push(`paused empty state: buttons ${JSON.stringify(s.emptyButtons)} (继续 only)`);
      return p;
    }) },
  { name: 'logs_empty_filtered', scenario: 'logs_export_empty', actions: [{ click: '#logs-level [data-v="error"]' }, settleMs()],
    expect: ctx => logsExpect(ctx, async page => {
      const s = await st(page), p = [];
      if (s.emptyTitle !== ws(await tt(page, 'logs.empty.filtered.title')) || s.emptySub != null) p.push(`F-01: filtered empty state ${JSON.stringify([s.emptyTitle, s.emptySub])}`);
      if (JSON.stringify(s.emptyButtons) !== JSON.stringify([ws(await tt(page, 'logs.empty.filtered.clear_filters'))])) p.push(`filtered empty state: buttons ${JSON.stringify(s.emptyButtons)} (清除筛选 only)`);
      return p;
    }) },
  /* F-01: a folded group of connection lines in the reader: its facts (time, range), no paragraph
     retelling them, and the 原文 heading without a count or 逐字 */
  { name: 'logs_reader_noise', scenario: 'logs_clear_undo', actions: [...RAW,
    { eval: () => { const r = document.querySelector('#logs-rows .logs-rrow.fold'); if (!r) throw new Error('no folded row'); r.click(); } }, settleMs()],
    expect: ctx => logsExpect(ctx, async page => {
      const s = await st(page), p = [];
      if (!s.reader) return ['the reader did not open on the folded row'];
      if (s.readerD) p.push(`F-01: ${s.readerD} description paragraph(s) in the reader of a folded group`);
      if (s.readerShM) p.push('F-01: the 原文 heading carries a side note');
      const want = [await tt(page, 'logs.reader.kv.time'), await tt(page, 'logs.reader.kv.range')].map(ws);
      if (JSON.stringify(s.readerKeys.slice(0, 2)) !== JSON.stringify(want)) p.push(`reader facts ${JSON.stringify(s.readerKeys)} (want ${JSON.stringify(want)} first)`);
      return p;
    }) },
  /* O-3 */
  { name: 'logs_clear_undo', scenario: 'logs_clear_undo', actions: [{ click: '#logs-more' }, settleMs(), clickMenu(MORE.clear), settleMs(300), deliverLines(3, 15), settleMs(300)],
    expect: ctx => logsExpect(ctx, async page => {
      let s = await st(page); const p = [];
      const undo = await tt(page, 'logs.empty.cleared.undo');
      if (!s.toasts.some(x => has(x, undo))) p.push(`O-3: no undo in a notice (${JSON.stringify(s.toasts)})`);
      if (!s.rings) p.push('O-3: the undo notice has no countdown ring');
      if (s.rowsE) p.push(`O-3: the cleared view shows ${s.rowsE} key events`);
      // F-01: the empty state says what happened (its title), nothing on how clearing works
      if (s.emptyTitle !== ws(await tt(page, 'logs.empty.cleared.title')) || s.emptySub != null) p.push(`O-3 / F-01: cleared empty state ${JSON.stringify([s.emptyTitle, s.emptySub])}`);
      // (three raw lines arrived meanwhile: the notice is still there)
      await page.evaluate(k => { const want = T.t(k); [...document.querySelectorAll('#toast-slot button')].find(x => x.textContent.trim() === want).click(); }, 'logs.empty.cleared.undo');
      await page.evaluate(() => window.__fixtureClock.advance(600)); await page.waitForTimeout(80);
      s = await st(page);
      if (!s.rowsE) p.push('O-3: the undo did not bring the key events back');
      return p;
    }) },
  /* O-4 */
  { name: 'logs_debug_failed', scenario: 'logs_debug_failed', actions: [{ click: '#logs-more' }, settleMs(), clickMenu(MORE.debug), settleMs(600)],
    expect: ctx => logsExpect(ctx, async page => {
      const s = await st(page), p = [];
      if (!noticeIs(s, await tt(page, 'logs.debug.failed'))) p.push(`O-4: banners ${JSON.stringify(s.banners)}`);
      if (s.debug) p.push('O-4: DEBUG chip on although logs.feed.level is info');
      return p;
    }) },
  /* O-5 */
  { name: 'logs_folder_failed', scenario: 'logs_folder_failed', actions: [{ click: '#logs-more' }, settleMs(), clickMenu(MORE.folder), settleMs(600)],
    expect: ctx => logsExpect(ctx, async page => {
      const s = await st(page), p = [];
      // the data folder (D17, G47; 8th-wave ruling T-P4-H2 item 5)
      if (!s.callArgs.some(([c, a]) => c === 'host.openFolder' && a.kind === 'data')) p.push('O-5: host.openFolder{kind:data} not asked');
      if (!noticeIs(s, await tt(page, 'logs.folder.failed'))) p.push(`O-5: banners ${JSON.stringify(s.banners)}`);
      return p;
    }) },
  /* T-P4-H2 item 5 (D17, G47): 打开日志文件夹 opens the data folder, as the old window did, and says so */
  { name: 'logs_folder_opened', scenario: 'logs_folder_opened', actions: [{ click: '#logs-more' }, settleMs(), clickMenu(MORE.folder), settleMs(600)],
    expect: ctx => logsExpect(ctx, async page => {
      const s = await st(page), p = [];
      const asked = s.callArgs.filter(([c]) => c === 'host.openFolder').map(([, a]) => a);
      if (JSON.stringify(asked) !== JSON.stringify([{ kind: 'data' }])) p.push(`H2-5: host.openFolder args ${JSON.stringify(asked)}, want [{kind:'data'}]`);
      if (!noticeIs(s, await tt(page, 'logs.folder.opened_data'))) p.push(`H2-5: notices ${JSON.stringify(s.toasts)} ${JSON.stringify(s.banners)}`);
      return p;
    }) },
  /* O-6 */
  { name: 'logs_locate_gone', scenario: 'logs_locate_gone', actions: [{ click: '#logs-follow' }, settleMs(), clickRowText('Starting service'), settleMs(), deliverLines(4200, 15), settleMs(),
    { eval: k => { const want = T.t(k); [...document.querySelectorAll('.logs-reader button')].find(b => b.textContent.trim() === want).click(); }, arg: 'logs.action.locate_raw' }, settleMs()],
    expect: ctx => logsExpect(ctx, async page => {
      const s = await st(page);
      return noticeIs(s, await tt(page, 'logs.locate.gone')) ? [] : [`O-6: toasts ${JSON.stringify(s.toasts)}`];
    }) },
  /* O-7 */
  { name: 'logs_copy_failed', scenario: 'logs_copy_failed', actions: [
    { eval: () => { navigator.clipboard.writeText = () => Promise.reject(new Error('denied')); document.execCommand = () => false; } },
    clickRowText('Traffic on Bastion'), settleMs(), { eval: () => document.querySelector('#logs-events .logs-row[aria-selected="true"]').focus() }, { press: 'Control+c' }, settleMs()],
    expect: ctx => logsExpect(ctx, async page => {
      const s = await st(page);
      return noticeIs(s, await tt(page, 'logs.copy.failed')) ? [] : [`O-7: banners ${JSON.stringify(s.banners)}`];
    }) },
  /* O-8 */
  { name: 'logs_multi_copy', scenario: 'logs_multi_copy', actions: [
    { eval: () => { window.__copied = null; navigator.clipboard.writeText = t => { window.__copied = t; return Promise.resolve(); }; } },
    clickRowText('Starting service'), settleMs(),
    { eval: () => { const r = [...document.querySelectorAll('#logs-events .logs-row')].find(x => x.textContent.includes('Alert delivered')); r.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, ctrlKey: true })); } },
    { eval: () => document.querySelector('#logs-events .logs-row[aria-selected="true"]').focus() }, { press: 'Control+c' }, settleMs()],
    expect: ctx => logsExpect(ctx, async page => {
      const s = await st(page), p = [];
      const picked = await page.evaluate(() => document.querySelectorAll('#logs-events .is-picked').length);
      if (picked !== 2) p.push(`O-8: ${picked} rows picked`);
      const copied = await page.evaluate(() => window.__copied);
      if (!copied || copied.split('\n').length !== 2 || !has(copied, 'Starting service') || !has(copied, 'Alert delivered')) p.push(`O-8: copied ${JSON.stringify(copied)}`);
      if (!noticeIs(s, await tt(page, 'logs.copy.done', { n: 2 }))) p.push(`O-8: toasts ${JSON.stringify(s.toasts)}`);
      return p;
    }) },
  /* O-9 (the language goes to the other one and back inside expect: the cell's language is checked before) */
  { name: 'logs_relabel_failed', scenario: 'logs_relabel_failed',
    expect: ctx => logsExpect(ctx, async page => {
      const p = [];
      const back = await page.evaluate(() => T.i18n.lang);
      await page.evaluate(l => T.i18n.set(l === 'pseudo' ? 'zh_CN' : 'pseudo'), back);
      await page.evaluate(() => window.__fixtureClock.advance(600)); await page.waitForTimeout(100);
      const s = await st(page);
      if (count(s.calls, 'logs.tail') !== 2) p.push(`O-9: logs.tail asked ${count(s.calls, 'logs.tail')} times`);
      if (!noticeIs(s, await tt(page, 'logs.load.relabel_failed'))) p.push(`O-9: banners ${JSON.stringify(s.banners)}`);
      if (!has(s.eventsText, 'Starting service')) p.push('O-9: the old titles are gone');
      await page.evaluate(l => T.i18n.set(l), back);
      await page.evaluate(() => window.__fixtureClock.advance(600));
      return p;
    }) },

  /* 对等台账 #181 (criticism D5): the reader's splitter is dragged and remembered (WebView
     localStorage; the docked reader is a side panel, so the size is its width) */
  { name: 'logs_reader_resize', scenario: 'logs_clear_undo', sizes: ['1280x800'], actions: [clickRowText('Traffic on Bastion'), settleMs()],
    expect: ctx => logsExpect(ctx, async page => {
      const p = [];
      const g = await page.evaluate(() => { const r = document.querySelector('.logs-reader .logs-rd-grip').getBoundingClientRect(); return { x: r.x + r.width / 2, y: r.y + r.height / 2, w: document.querySelector('.logs-reader').getBoundingClientRect().width }; });
      await page.mouse.move(g.x, g.y); await page.mouse.down(); await page.mouse.move(g.x - 60, g.y, { steps: 3 }); await page.mouse.move(g.x - 80, g.y); await page.mouse.up();
      await page.evaluate(() => window.__fixtureClock.advance(400)); await page.waitForTimeout(80);
      const after = await page.evaluate(() => ({ w: document.querySelector('.logs-reader').getBoundingClientRect().width, kept: localStorage.getItem('tesseract.logs.reader.width') }));
      if (Math.abs(after.w - (g.w + 80)) > 2) p.push(`reader width ${g.w} → ${after.w} after an 80 px drag`);
      if (Number(after.kept) !== Math.round(after.w)) p.push(`width not remembered (${after.kept})`);
      // double click: back to the default, and forgotten
      await page.mouse.dblclick(g.x - 80, g.y);
      await page.evaluate(() => window.__fixtureClock.advance(400));
      const reset = await page.evaluate(() => ({ w: document.querySelector('.logs-reader').getBoundingClientRect().width, kept: localStorage.getItem('tesseract.logs.reader.width') }));
      if (Math.abs(reset.w - g.w) > 2 || reset.kept !== null) p.push(`double click: ${JSON.stringify(reset)}, default ${g.w}`);
      return p;
    }) },
  /* criticism B9 */
  { name: 'logs_reconnect_same_epoch', scenario: 'logs_reconnect_same_epoch', actions: RAW,
    expect: ctx => logsExpect(ctx, async page => {
      const s = await st(page), p = [];
      const restarts = await page.evaluate(() => document.querySelectorAll('#logs-rows .logs-restart').length);
      if (restarts) p.push('B9: a same-epoch reconnect inserted a restart separator');
      // 14 seeded (the three connection timeouts fold into one row: 12) + 5 live, no duplicate
      if (s.rowsR !== 12 + 5) p.push(`B9: ${s.rowsR} raw rows (12 + 5 expected)`);
      if (s.gapsRaw.length) p.push('B9: a gap row after the replay');
      return p;
    }) },
  /* criticism B10 */
  { name: 'logs_key_event_absent', scenario: 'logs_key_event_absent',
    expect: ctx => logsExpect(ctx, async page => {
      const s = await st(page), p = [];
      // by key (PROTOCOL §5.5: absence is never read as false): the 6 key events and the three
      // connection timeouts (a classified key; two rows: the first two fold into ×2); the raw lines,
      // whose title is the message, are not events
      if (s.rowsE !== 8) p.push(`B10: ${s.rowsE} key-event rows (expected 8, by key)`);
      if (has(s.eventsText, 'Monitor heartbeat ok') || has(s.eventsText, 'late')) p.push('B10: a raw line with a title shows as a key event');
      const seps = await page.evaluate(() => document.querySelectorAll('#logs-events .logs-sep').length);
      if (seps < 1) p.push('B10: no run separator from runtime.start_requested');
      return p;
    }) },
  /* B-22 (criticism A2): the suggestion card only when the build and the sidecar both open B-22
     (run with --build-flags "--h-features all" to see it; a normal build must not show it) */
  { name: 'logs_b22_advice', scenario: 'logs_b22_advice', actions: [clickRowText('Start failed'), settleMs()],
    expect: ctx => logsExpect(ctx, async page => {
      const r = await page.evaluate(() => ({ on: T.rt.feature('B-22'), card: !!document.querySelector('.logs-reader .logs-fix') }));
      return r.on === r.card ? { problems: [], metrics: { b22: r.on } } : { problems: [`B-22 ${r.on ? 'open but no suggestion card' : 'closed but a suggestion card shows'}`] };
    }) },
  /* untrusted text (映射_logs §7): the matrix's injection check fails the cell; these are extra */
  { name: 'logs_malicious_text', scenario: 'logs_malicious_text', actions: [clickRowText('<img src="x"'), settleMs()],
    expect: ctx => logsExpect(ctx, async page => {
      const s = await st(page), p = [];
      if (s.pwned) p.push('window.__pwned set');
      const r = await page.evaluate(() => ({ img: document.querySelectorAll('#host img, .logs-reader img').length, iframe: document.querySelectorAll('iframe').length,
        b: [...document.querySelectorAll('b')].filter(x => x.textContent === 'bold?').length, title: document.querySelector('.logs-reader .ovl-title').textContent }));
      if (r.img || r.iframe || r.b) p.push(`markup from log text became elements: ${JSON.stringify(r)}`);
      if (!has(r.title, '<img src="x"')) p.push(`the reader title is not the literal text: ${r.title}`);
      return p;
    }) },
  { name: 'logs_malicious_text_raw', scenario: 'logs_malicious_text', actions: RAW,
    expect: ctx => logsExpect(ctx, async page => {
      const s = await st(page);
      return has(s.rawText, '<script>window.__pwned=1</script>') && !s.pwned ? [] : ['the raw table does not show the script text literally'];
    }) },

  /* T-P4-09 inputs (one size, both languages): the end state only; the timing is T-P4-09's */
  { name: 'logs_stress_4000', scenario: 'logs_stress_4000', sizes: ['1280x800'], actions: RAW,
    expect: ctx => logsExpect(ctx, async page => {
      const s = await page.evaluate(() => ({ chunks: document.querySelectorAll('#logs-rows .logs-chunk').length, rows: document.querySelectorAll('#logs-rows .logs-rrow').length }));
      const p = [];
      if (s.rows !== 4000) p.push(`stress: ${s.rows} raw rows in the DOM (4,000 kept after 4,500 arrived)`);
      return { problems: p, metrics: s };
    }) },
  { name: 'logs_hidden_30s', scenario: 'logs_hidden_30s', sizes: ['1280x800'], actions: RAW,
    expect: ctx => logsExpect(ctx, async page => {
      const n = await page.evaluate(() => [...document.querySelectorAll('#logs-rows .logs-rrow')].filter(r => r.checkVisibility()).length);
      return n === 12 + 1500 ? [] : [`hidden: ${n} raw rows after the window came back (12 + 1,500)`];
    }) },
  { name: 'logs_hidden_30s_writes', scenario: hiddenScenario('logs_hidden_30s_writes'), sizes: ['1280x800'], actions: [...RAW, hiddenRun(false)],
    expect: ctx => logsExpect(ctx, async page => {
      const w = await hiddenWrites(page), p = [], h = w.tally.hidden, s = w.tally.shown;
      for (const k of ['rows', 'events', 'foot']) if (h[k]) p.push(`hidden: ${h[k]} mutation(s) in ${k} while the window was hidden`);
      if (!s.rows) p.push('shown: the list was not written when the window came back');
      if (!s.foot) p.push('shown: the footer was not brought up to date when the window came back');
      if (w.rows !== 12 + 1500) p.push(`shown: ${w.rows} raw rows (12 + 1,500)`);
      return { problems: p, metrics: w.tally };
    }) },
];

const LANGS = ['zh_CN', 'en_US', 'ja_JP', 'ko_KR', 'fr_FR', 'de_DE', 'es_ES', 'pt_BR', 'ru_RU'];

// the badge while the page is elsewhere (总览): no wording of this page is on screen, so no pseudo
// cell (总览's own pseudo widths are its page's); T-P5-13: the nine languages
const BADGE_HIDDEN = { page: 'logs', name: 'logs_hidden_30s_badge', langs: LANGS, scenario: hiddenScenario('logs_hidden_30s_badge', { elsewhere: true }), sizes: ['1280x800'], actions: [{ go: 'overview' }, settleMs(), hiddenRun(true)],
    expect: async ({ page }) => {
      const w = await hiddenWrites(page), p = [];
      if (w.tally.hidden.badge) p.push(`hidden: ${w.tally.hidden.badge} mutation(s) of the sidebar badge while the window was hidden`);
      if (!w.tally.shown.badge) p.push('shown: the badge was not written when the window came back');
      if (w.badge !== '3') p.push(`badge ${w.badge} (3 errors, the first in the topic's first, instant, frame)`);
      return { problems: p, metrics: w.tally };
    } };

/* the whole path, T.go not replaced (T-P4-H4 修复第 1 轮): the window opens on the settings page,
   提醒区「查看全部」 is clicked there, and the logs page's first visit (render, then enter with the
   params) is the filtered one. (The button is hidden while the scenario has no recent alerts; a
   click on it still runs its handler) */
const FROM_SETTINGS = { name: 'logs_enter_category_from_settings', page: 'settings', scenario: 'logs_i18n_running',
  actions: [settleMs(), { eval: () => { window.__h4from = T.router.current; const b = document.querySelector('#set-recent-all'); window.__h4btn = !!b; if (b) b.click(); } }, settleMs(800),
    { eval: () => { window.__h4to = T.router.current; } }, ...RAW, filterState],
  expect: ctx => logsExpect(ctx, async page => {
    const nav = await page.evaluate(() => ({ from: window.__h4from, btn: window.__h4btn, to: window.__h4to })), p = [];
    if (nav.from !== 'settings' || !nav.btn || nav.to !== 'logs') p.push(`the jump from the settings page: ${JSON.stringify(nav)}`);
    return p.concat(await categoryExpect(page));
  }) };

/* the level column follows a language switch on the page that is open (T-P5-04 修复第 1 轮): zh_CN
   keeps 62 px; es_ES (the widest real level word, 「Depuración」) widens it within the 108 px cap and
   no badge spills out of its cell; back in zh_CN it is 62 px again. The width is the probe's, so
   no number of the font is written here beyond the bounds. T-P5-13: the cell starts in each of the
   nine languages (its column within 62–108 px, 62 in zh_CN), switches to es_ES (es_ES itself: to
   zh_CN) and back, and is where it started */
const levelCol = () => {
  const body = document.querySelector('#logs-body');
  const col = document.querySelector('.logs-gh > :nth-child(2)').getBoundingClientRect().width;
  let spill = 0;
  for (const c of document.querySelectorAll('#logs-rows .logs-rrow > .logs-c:nth-child(2)')) {
    const s = c.querySelector('.sev');
    if (s && s.getBoundingClientRect().right > c.getBoundingClientRect().right - 10 + 0.01) spill++;
  }
  return { lang: T.i18n.lang, col, lvw: body.style.getPropertyValue('--logs-lvw'), spill };
};
const LEVEL_COL = { page: 'logs', name: 'logs_level_col_language', langs: LANGS, scenario: 'logs_i18n_running', actions: RAW, overflow_allow: DESIGN,
  expect: ctx => logsExpect(ctx, async page => {
    const p = [], seen = [];
    const at = async lang => {
      if (lang) { await page.evaluate(l => T.i18n.set(l), lang); await page.evaluate(() => window.__fixtureClock.advance(600)); await page.waitForTimeout(100); }
      const r = await page.evaluate(levelCol);
      seen.push(r);
      return r;
    };
    const start = await at(null), L = start.lang, other = L === 'es_ES' ? 'zh_CN' : 'es_ES';
    const mid = await at(other), back = await at(L);
    const zhOk = r => Math.round(r.col) === 62, esOk = r => r.col > 62 && r.col <= 108 && parseFloat(r.lvw) > 24;
    if (L === 'zh_CN' ? !zhOk(start) : L === 'es_ES' ? !esOk(start) : !(Math.round(start.col) >= 62 && start.col <= 108)) p.push(`${L}: level column ${start.col}px, --logs-lvw ${start.lvw} (${L === 'zh_CN' ? '62' : '62–108'} px expected)`);
    if (mid.lang !== other || !(other === 'es_ES' ? esOk(mid) : zhOk(mid))) p.push(`${other}: level column ${mid.col}px, --logs-lvw ${JSON.stringify(mid.lvw)} (${other === 'es_ES' ? 'more than 62, at most 108, the widest level word measured' : '62 expected'})`);
    for (const r of [start, mid, back]) if (r.spill) p.push(`${r.lang}: ${r.spill} level badge(s) spill out of their cell`);
    if (back.lang !== L || Math.abs(back.col - start.col) > 0.5 || back.lvw !== start.lvw) p.push(`back in ${L}: level column ${back.col}px, --logs-lvw ${back.lvw} (${start.col} px, ${start.lvw})`);
    return { problems: p, metrics: { levelCol: seen.map(r => `${r.lang} ${r.col}px ${r.lvw}`).join('; ') } };
  }) };

export default [...CASES.flatMap(c => [
  { page: 'logs', ...c, langs: LANGS, overflow_allow: DESIGN },
  { page: 'logs', ...c, name: c.name + '_pseudo', langs: ['pseudo'], overflow_allow: [...DESIGN, ...PSEUDO_LENGTH] },
]), BADGE_HIDDEN, LEVEL_COL,
  // (the page it opens on is the settings page: run under --page settings)
  // (T-P5-11, ruling 3: it has a pseudo cell, so its real-language cell runs in all nine languages)
  { ...FROM_SETTINGS, langs: LANGS, overflow_allow: DESIGN },
  { ...FROM_SETTINGS, name: FROM_SETTINGS.name + '_pseudo', langs: ['pseudo'], overflow_allow: [...DESIGN, ...PSEUDO_LENGTH] }];
