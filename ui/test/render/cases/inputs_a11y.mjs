// Render cases of T-P5-14 (P5-e leftovers) for the render matrix:
//
//   ks_keyword_variants (settings, the nine languages): the kill switch's keywords
//     (settings.conn.ks.kw.*) typed into the "other addresses" token field, as shown and in their
//     full-width, half-width katakana and NFD forms (IME full-width mode, ja IME F8, a copy from
//     macOS), each add the keyword's chip (data-key LocalSubnet / DefaultGateway) shown in the
//     catalog's word, and leave the field empty. settings.js compares both sides after
//     normalize('NFKC') and toLowerCase() (requirement 1; the pure half is
//     ui/test/unit/settings_ks_keywords.test.mjs). The typing runs in expect(), after the cell's
//     screenshot and checks.
//   tiers_diag_proto_name (tiers, the nine languages and pseudo): the diagnosis' protocol select
//     (.tiers-dsel) is named by its label .tiers-dopt-l, then its value: aria-labelledby =
//     "<label id> <.select-v id>" and Chrome's accessible name is the label's words followed by
//     the value (requirement 3).
import { RUNNING } from './settings_i18n.mjs';
import { PSEUDO_LENGTH as TIERS_PSEUDO, tiersWordsInPage, wordProblems } from './tiers_i18n.mjs';

const LANGS9 = ['zh_CN', 'en_US', 'de_DE', 'es_ES', 'fr_FR', 'ja_JP', 'ko_KR', 'pt_BR', 'ru_RU'];
const advance = (page, ms) => page.evaluate(n => window.__fixtureClock.advance(n), ms);

/* the forms a word takes from an IME or a copy (same as the unit test) */
const fullWidth = s => s.replace(/[!-~]/g, c => String.fromCharCode(c.charCodeAt(0) + 0xfee0));
const HALF = new Map();
for (let c = 0xff61; c <= 0xff9f; c++) { const h = String.fromCharCode(c); HALF.set(h.normalize('NFKC'), h); }
const halfKana = s => [...s].map(ch => {
  const d = ch.normalize('NFD'), h = HALF.get(d[0]), mark = d.slice(1);
  return h ? h + (mark === '゙' ? 'ﾞ' : mark === '゚' ? 'ﾟ' : mark) : ch;
}).join('');
function variants(w) {
  const seen = new Set();
  return [['as shown', w], ['full-width', fullWidth(w)], ['half-width katakana', halfKana(w)], ['NFD', w.normalize('NFD')]]
    .filter(([, v]) => !seen.has(v) && seen.add(v));
}

/* in page: type `w` into the field and press Enter */
function typeKeyword(w) {
  const box = document.querySelector('#set-tok-ksextra');
  const inp = box && box.querySelector('input');
  if (!inp) return 'no #set-tok-ksextra input';
  box.scrollIntoView({ block: 'center' });
  inp.value = w; inp.dispatchEvent(new Event('input', { bubbles: true }));
  inp.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
  return null;
}
/* in page: the chips now, then clear them */
function readChips() {
  const box = document.querySelector('#set-tok-ksextra');
  const inp = box.querySelector('input');
  const chips = [...box.querySelectorAll('.set-tok-i')].map(c => ({ key: c.dataset.key, text: c.querySelector('.set-tok-v').textContent }));
  const left = inp.value, invalid = !!box.closest('.is-invalid');
  for (const b of box.querySelectorAll('.chip-x')) b.click();
  inp.value = '';
  return { chips, left, invalid };
}

async function keywordExpect({ page, lang }) {
  const problems = [];
  const words = await page.evaluate(() => ({ LocalSubnet: T.t('settings.conn.ks.kw.local_subnet'), DefaultGateway: T.t('settings.conn.ks.kw.default_gateway') }));
  // start from an empty list (the fixture may hold chips)
  await page.evaluate(() => { for (const b of document.querySelectorAll('#set-tok-ksextra .chip-x')) b.click(); });
  await advance(page, 600);
  let tried = 0;
  for (const [key, w] of Object.entries(words)) {
    for (const [how, v] of variants(w)) {
      const err = await page.evaluate(typeKeyword, v);
      if (err) { problems.push(err); return { problems }; }
      await advance(page, 600);
      const r = await page.evaluate(readChips);
      await advance(page, 600);
      tried++;
      const code = [...v].map(c => c.codePointAt(0).toString(16)).join(' ');
      if (r.left !== '' || r.invalid) problems.push(`${lang} ${key} ${how} "${v}" (${code}): not added (left "${r.left}", invalid ${r.invalid})`);
      else if (r.chips.length !== 1 || r.chips[0].key !== key) problems.push(`${lang} ${key} ${how} "${v}": chips ${JSON.stringify(r.chips)}, want one ${key}`);
      else if (r.chips[0].text !== w) problems.push(`${lang} ${key} ${how}: the chip shows "${r.chips[0].text}", not the catalog word "${w}"`);
    }
  }
  return { problems, metrics: { ks_variants: tried } };
}

/* in page: the protocol select's label and value ids */
function dselInPage() {
  const p = document.querySelector('.tiers-panel');
  const b = p && p.querySelector('.tiers-dopt:not([hidden]) .tiers-dsel');
  if (!b) return { problems: ['K-9: no protocol select in the open panel'] };
  const lab = b.parentElement.querySelector(':scope > .tiers-dopt-l'), v = b.querySelector('.select-v');
  const ids = (b.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean);
  const problems = [];
  if (!lab || !lab.id) problems.push('the label .tiers-dopt-l has no id');
  if (!v || !v.id) problems.push('the value .select-v has no id');
  if (lab && v && JSON.stringify(ids) !== JSON.stringify([lab.id, v.id])) problems.push(`aria-labelledby "${ids.join(' ')}", want "${lab.id} ${v.id}"`);
  if (b.hasAttribute('aria-label')) problems.push('an aria-label would take the place of the name');
  if (ids.some(id => document.querySelectorAll(`[id="${id}"]`).length !== 1)) problems.push(`an id of aria-labelledby is not unique: ${ids.join(' ')}`);
  const sp = s => (s || '').replace(/\s+/g, ' ').trim();
  return { problems, label: sp(lab && lab.textContent), want: sp(T.t('tiers.panel.diag.proto')), value: sp(v && v.textContent) };
}
async function axName(context, page, sel) {
  const cdp = await context.newCDPSession(page);
  try {
    const { root } = await cdp.send('DOM.getDocument', { depth: 0 });
    const { nodeId } = await cdp.send('DOM.querySelector', { nodeId: root.nodeId, selector: sel });
    if (!nodeId) return null;
    const { nodes } = await cdp.send('Accessibility.getPartialAXTree', { nodeId, fetchRelatives: false });
    const n = nodes[0];
    return { name: n && n.name ? n.name.value : '', role: n && n.role ? n.role.value : '' };
  } finally { await cdp.detach(); }
}
async function dselExpect({ page, context, lang }) {
  const d = await page.evaluate(dselInPage);
  const problems = [...d.problems];
  const shown = await page.evaluate(() => { const s = document.querySelector('section[data-page="profiles"]'); return !!s && !s.hidden; });
  if (!shown) problems.push('the tiers page is not shown');
  if (lang === 'zh_CN' || lang === 'pseudo') problems.push(...wordProblems(await page.evaluate(tiersWordsInPage), lang));
  if (!d.problems.length) {
    if (d.label !== d.want) problems.push(`the label reads "${d.label}", not tiers.panel.diag.proto "${d.want}"`);
    if (!d.value) problems.push('the select shows no value');
    const ax = await axName(context, page, '.tiers-panel .tiers-dopt:not([hidden]) .tiers-dsel');
    const sp = s => (s || '').replace(/\s+/g, ' ').trim();
    if (!ax) problems.push('the select is not in the accessibility tree');
    else if (sp(ax.name) !== sp(`${d.label} ${d.value}`)) problems.push(`accessible name "${ax.name}", want the label then the value "${d.label} ${d.value}"`);
    return { problems, metrics: { dsel_name: ax && ax.name, dsel_role: ax && ax.role } };
  }
  return { problems };
}

const tiersPanel = [{ click: '.tiers-row[data-key="v-hk03"] .tiers-name' }, { page_ms: 700 }];
const dsel = { name: 'tiers_diag_proto_name', scenario: 'tiers_running', page: 'tiers', sizes: ['1280x800'], actions: tiersPanel, expect: dselExpect };

export default [
  { name: 'ks_keyword_variants', scenario: RUNNING, page: 'settings', langs: LANGS9, sizes: ['1280x800'], expect: keywordExpect },
  { ...dsel, langs: LANGS9 },
  { ...dsel, name: `${dsel.name}_pseudo`, langs: ['pseudo'], overflow_allow: TIERS_PSEUDO },
];
