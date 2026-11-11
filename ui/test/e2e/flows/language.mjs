// Flow "language" (P4 architecture sections 2.9, 3.7; shell/README T-P3-05; criticism B3).
//   stand-in, launch 0 (hello.caps declares ui.language): with a draft on 配置, the sidecar sends
//     ui.language en_US; the shell forwards host:language; <html lang> and T.i18n change, the page
//     relabels in place (i18n:change; 配置 re-renders its rows - a marked row node is replaced -
//     while the page itself is not reloaded: a window marker survives), the draft survives (same
//     dirty count, the switch still flipped); then back to zh_CN the same way. The en_US catalog
//     is complete since P5 (T-P5-03, P5 §6 #18), so the words are asserted: the page text changed
//     (checks.en.words_changed), and two samples - the page title in the crumb
//     (shell.page.settings.title) and the language row's title (settings.app.lang.title) - read
//     the en_US catalog's words, not zh_CN's; back in zh_CN they read the zh_CN words again (the
//     catalogs are read from ui/src/i18n/locales, the source the shell's page was built from).
//   stand-in, launch 1 (hello.caps WITHOUT ui.language - the shell neither subscribes nor
//     forwards it): 配置 saves another language; 3 s later the language row says to reopen the
//     window (settings.app.lang.reopen) and the page language has not changed.
//   real sidecar (T-P2-33 sandbox): 配置 saves en_US; the sidecar records ui.language, the shell
//     forwards host:language and the page switches; then zh_CN is saved back.
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { REPO, check, chooseLanguage, dirtyCount, htmlLang, makeDraft, saveSettings, settingsReady, sleep, switchOn, undoDrafts, waitFor } from '../driver.mjs';

export const real = true;
export const launches = real => (real ? [{ args: [], env: {}, options: {} }]
  : [{ args: [], env: {}, options: { phase: 'running', ui_language: true, lang: 'zh_CN' } },
    { args: [], env: {}, options: { phase: 'running', ui_language: false, lang: 'zh_CN' } }]);

const lastSubscribe = s => s.requests().filter(r => r.t === 'subscribe').pop() || {};
const pageText = s => s.eval(() => document.body.innerText);
// the spot-checked words: [where, catalog file, id] (zh_CN entries are {m, _desc})
const SAMPLES = [['crumb', 'shell', 'shell.page.settings.title'], ['lang_row', 'settings', 'settings.app.lang.title']];
const catalogText = (lang, file, id) => {
  const v = JSON.parse(readFileSync(join(REPO, 'ui', 'src', 'i18n', 'locales', lang, `${file}.json`), 'utf8').replace(/^\uFEFF/, ''))[id];
  return v && typeof v === 'object' ? v.m : v;
};
const expectedWords = lang => Object.fromEntries(SAMPLES.map(([k, file, id]) => [k, catalogText(lang, file, id)]));
// the words on the page now (the crumb's entering span, not one still leaving)
const sampleWords = s => s.eval(() => {
  const norm = x => (x ? x.textContent.replace(/\s+/g, ' ').trim() : null);
  return { crumb: norm(document.querySelector('#crumb span:not(.leaving)')), lang_row: norm(document.querySelector('#set-lang-row .set-t')) };
});
const sameWords = (a, b) => SAMPLES.every(([k]) => a[k] === b[k]);
const isLang = (l, lang) => l.i18n === lang && l.store === lang && l.html.replace('_', '-').toLowerCase() === lang.replace('_', '-').toLowerCase();

export async function run(s, launch, real) {
  const c = s.checks;
  await s.reloadObserved();
  await settingsReady(s);
  c.start = await htmlLang(s);
  if (real) return realSave(s, c);
  if (launch === 0) return switchByShell(s, c);
  return reopenNote(s, c);
}

async function switchByShell(s, c) {
  check((lastSubscribe(s).topics || []).includes('ui.language'), 'the shell did not subscribe ui.language');
  const key = await makeDraft(s);
  const dirty = await dirtyCount(s);
  const on = await switchOn(s, key);
  const text0 = await pageText(s);
  const zhWords = expectedWords('zh_CN'), enWords = expectedWords('en_US');
  check(SAMPLES.every(([k]) => zhWords[k] && enWords[k] && zhWords[k] !== enWords[k]), `the sampled catalog words are missing or equal: ${JSON.stringify({ zhWords, enWords })}`);
  c.words_start = await sampleWords(s);
  check(sameWords(c.words_start, zhWords), `配置 does not start in zh_CN words: ${JSON.stringify(c.words_start)}, expected ${JSON.stringify(zhWords)}`);
  const mark = await s.eval(() => { window.__e2eLangMark = 'kept'; const r = document.getElementById('set-lang-row'); if (r) r.setAttribute('data-e2e-mark', '1');
    return { events: window.__e2e ? window.__e2e.events.filter(e => e.e === 'i18n:change').length : 0 }; });
  await s.control({ op: 'language', language: 'en_US' });
  await s.until(() => window.T.i18n && window.T.i18n.lang === 'en_US', 'page switched to en_US', 10000);
  await waitFor(async () => isLang(await htmlLang(s), 'en_US'), '<html lang> en_US', 5000);
  await sleep(800);
  c.en = { lang: await htmlLang(s), dirty: await dirtyCount(s), on: await switchOn(s, key),
    in_place: await s.eval(m => ({ page_kept: window.__e2eLangMark === 'kept', row_rerendered: !document.querySelector('#set-lang-row[data-e2e-mark]'),
      i18n_change_events: window.__e2e ? window.__e2e.events.filter(e => e.e === 'i18n:change').length - m.events : null }), mark) };
  c.en.words_changed = (await pageText(s)) !== text0;
  c.en.words = await sampleWords(s);
  await s.shot('en_US');
  check(c.en.words_changed, 'the page text did not change on the switch to en_US');
  check(sameWords(c.en.words, enWords), `配置 is not in en_US words: ${JSON.stringify(c.en.words)}, expected ${JSON.stringify(enWords)}`);
  check(c.en.in_place.page_kept, 'the page was reloaded for the language switch');
  check(c.en.in_place.row_rerendered, '配置 was not re-rendered in the new language');
  check(c.en.in_place.i18n_change_events >= 1, 'no i18n:change on the page');
  check(c.en.dirty === dirty && c.en.on === on, `the draft was lost on the language switch: dirty ${dirty} -> ${c.en.dirty}`);
  check((await s.eval(() => window.T.router.current)) === 'settings', 'the page left 配置');
  await s.control({ op: 'language', language: 'zh_CN' });
  await waitFor(async () => isLang(await htmlLang(s), 'zh_CN'), 'back to zh_CN', 10000);
  // (relabel is synchronous with the language change; the crumb's leaving span is excluded)
  await waitFor(async () => sameWords(await sampleWords(s), zhWords), '配置 back in zh_CN words', 5000).catch(() => {});
  c.back = { lang: await htmlLang(s), dirty: await dirtyCount(s), words: await sampleWords(s) };
  check(c.back.dirty === dirty, 'the draft was lost on the way back');
  check(sameWords(c.back.words, zhWords), `配置 did not return to zh_CN words: ${JSON.stringify(c.back.words)}, expected ${JSON.stringify(zhWords)}`);
  c.host_language_frames = (await s.events('host:language')).length;
  await undoDrafts(s);
}

async function reopenNote(s, c) {
  check(!(lastSubscribe(s).topics || []).includes('ui.language'), 'the shell subscribed an undeclared ui.language');
  const target = c.start.i18n === 'en_US' ? 'ja_JP' : 'en_US';
  c.target = target;
  await chooseLanguage(s, target);
  await saveSettings(s);
  const saved = s.requests().filter(r => r.cmd === 'settings.apply').pop();
  c.saved = saved && saved.args;
  check(saved && saved.args.values && saved.args.values.ui && saved.args.values.ui.language === target, `settings.apply did not carry ui.language ${target}`);
  const t0 = Date.now();
  const reopen = await s.eval(() => window.T.t('settings.app.lang.reopen'));
  await s.until(r => { const d = document.getElementById('set-lang-d'); return d && d.textContent.includes(r); }, 'the reopen note', 8000, reopen);
  c.note_after_ms = Date.now() - t0;
  check(c.note_after_ms >= 2500, `the reopen note came before the 3 s wait: ${c.note_after_ms} ms`);
  c.after = await htmlLang(s);
  check(c.after.i18n === c.start.i18n && c.after.html === c.start.html, `the page language changed: ${JSON.stringify(c.start)} -> ${JSON.stringify(c.after)}`);
  await s.shot('reopen-note');
}

async function realSave(s, c) {
  // (the hardened sandbox's own language first changes, then it is saved back)
  const start = c.start.i18n;
  const hostLanguage = () => s.eval(() => (window.__e2e ? window.__e2e.events.filter(e => e.e === 'host:language').length : 0));
  for (const target of [start === 'zh_CN' ? 'en_US' : 'zh_CN', start]) {
    const before = await hostLanguage();
    await chooseLanguage(s, target);
    await saveSettings(s);
    await waitFor(async () => isLang(await htmlLang(s), target), `page switched to ${target}`, 15000);
    // the switch came through the shell (the sidecar's ui.language forwarded as host:language)
    await waitFor(async () => (await hostLanguage()) > before, `host:language after saving ${target}`, 10000);
    c[target] = await htmlLang(s);
    await s.shot(target);
  }
  c.host_language_frames = await hostLanguage();
}
