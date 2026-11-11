// T-P4-04e: message extraction · settings and alerts (ui/src/pages/settings.js; zh_CN settings.json,
// alerts.json, catalog.json).
//
// Static: the three catalogs (shape, ICU, no _max, ids by namespace), ids used ↔ ids defined
// (literal ids and the dynamic families, the reserved ids for T-P4-05d / 05e listed), the catalog.*
// naming (bridge_catalog label_id: catalog.alerts.<type>[.<field>], choices <label_id>.choice.<v>;
// the names the backend has no label_id for: catalog.alerts.group / preset / sound.preset / scope),
// the counts as plural, Bastion's capacity as a parameter (D9), the catalog gate turned hard, no CJK
// literal and no @i18n-pending in settings.js, the demo module reached only inside if (__DEMO__),
// the app bundle free of the settings demo.
// Browser (local Chrome through playwright-core; skipped when Chrome is missing):
//   * regression (critique E1): the design target in sim mode under the deterministic fixture
//     renders the settings page with the T-P4-01 baseline text tree (test/baseline/
//     settings.textree.json), except the listed differences (SPLIT: a data value inside a message
//     moved into its own translate="no" node; the visible text is the same);
//   * the fake second language (pseudo) in fixture mode (ASCII-only frames,
//     test/fixtures/scenarios/settings_i18n_running.json): all seven sections and the Telegram
//     drawer show no CJK outside translate="no" and every text attribute comes from the catalog;
//   * a language switch with drafts keeps the drafts, the dirty marks, the typed add field, the
//     open drawer (tab and advanced block) and the scroll position (relocalize 'rerender'), and
//     back in zh_CN the page reads as before; also an open inline edit, a drawer field typed into
//     and the drawer's own scroll;
//   * a switch while the page is away (rerender on the next T.go): drafts kept, the page lands at
//     its top (not at the scroll of the page left; review A MUST 1), a {focus} landing still lands;
//   * a drawer reopened by a switch gives focus back to its opener (tile or in-use row) on Esc.
import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import * as acorn from 'acorn';
import * as walk from 'acorn-walk';
import { compile } from '../../build/locales.mjs';
import { buildTarget, writeOutput } from '../../build.mjs';
import { scanCjkJs, isPending } from '../../tools/scan.mjs';

const UI = new URL('../../', import.meta.url);
const read = rel => readFileSync(new URL(rel, UI), 'utf8');
const CHROME = process.env.TESSERACT_UI_CHROME || 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const skip = !existsSync(CHROME) && 'local Chrome not found';

const LOC = 'src/i18n/locales/zh_CN/';
const SETTINGS = JSON.parse(read(LOC + 'settings.json'));
const ALERTS = JSON.parse(read(LOC + 'alerts.json'));
const CATALOG = JSON.parse(read(LOC + 'catalog.json'));
const ALL = { ...SETTINGS, ...ALERTS, ...CATALOG };
const SRC = read('src/pages/settings.js');
const DEMO_SRC = read('src/design/settings_demo.js');
const zhAny = id => ['common', 'core', 'shell', 'tier', 'mode', 'fmt', 'settings', 'alerts', 'catalog']
  .some(ns => ns === id.split('.')[0] && id in JSON.parse(read(`${LOC}${ns}.json`)));

/* ------------------------------------------------------------------ the catalogs */
for (const [ns, cat, min] of [['settings', SETTINGS, 350], ['alerts', ALERTS, 130], ['catalog', CATALOG, 643]]) {
  test(`${ns}.json: {m, _desc} entries, no _max, every message compiles, ids are ${ns}.*`, () => {
    const ids = Object.keys(cat);
    assert.ok(ids.length >= min, `only ${ids.length} messages`);
    for (const id of ids) {
      const e = cat[id];
      assert.ok(id.startsWith(ns + '.'), id);
      const at = id.indexOf('.choice.');
      assert.match(at < 0 ? id : id.slice(0, at), /^[a-z0-9_]+(\.[a-z0-9_-]+)+$/, id);
      assert.deepEqual(Object.keys(e).sort(), ['_desc', 'm'], `${id}: keys`);
      assert.equal(typeof e.m, 'string'); assert.ok(e.m.length, `${id}: empty`);
      assert.ok(typeof e._desc === 'string' && e._desc.trim(), `${id}: _desc`);
      assert.doesNotThrow(() => compile(e.m, `zh_CN:${id}`), `${id}: ICU`);
    }
  });
}

// ids the page does not use yet: reserved for T-P4-05d / 05e (ticket requirement 4) and the
// catalog's import.specs / alerts.catalog entries the forms of 05e and the import page use
const RESERVED_SETTINGS = [
  // (F-04: settings.app.tray_on_autostart.disabled_hint is gone with the line it filled)
  'settings.app.tray_on_autostart.label',
  'settings.app.lang.reopen', 'settings.save.conflict', 'settings.save.retry_apply', 'settings.save.row.not_applied',
  'settings.save.row.pending', 'settings.save.state.not_applied', 'settings.save.state.pending', 'settings.save.state.pending_ready',
];
// the reason_code sets of backend_keys.py (alerts.test, alerts.onboarding*; T-P4-03)
const TEST_REASONS = ['alerts_desktop_busy', 'alerts_desktop_dropped', 'alerts_desktop_failed', 'alerts_desktop_receipt_timeout',
  'alerts_desktop_unavailable', 'alerts_test_delivery_failed', 'alerts_test_endpoint_changed', 'alerts_test_invalid_draft',
  'alerts_test_runtime_unavailable', 'alerts_test_saved_read_failed', 'alerts_test_timeout', 'alerts_test_unknown_target'];
const ONBOARDING_REASONS = ['onboarding_auxiliary_link', 'onboarding_blocked', 'onboarding_closed', 'onboarding_expired',
  'onboarding_failed', 'onboarding_inputs_required', 'onboarding_owner_revoked', 'onboarding_remote_rejected',
  'onboarding_request_cancelled', 'onboarding_unknown_state'];

const PAGE = SRC + '\n' + DEMO_SRC;
// (T-P4-05d) the page calls the runtime by name (settings.apply, settings.validate): command and
// query names of the v1 spec are not message ids
const SPEC = JSON.parse(read('src/rt/spec.gen.json'));
const RT_NAMES = new Set([...Object.keys(SPEC.commands), ...Object.keys(SPEC.queries), ...Object.keys(SPEC.topics)]);   // (T-P4-05e) topic names too
const literalIds = new Set([...PAGE.matchAll(/['"`]((?:settings|alerts|catalog|core|common|shell|tier|mode|fmt)\.[a-z0-9_.]*[a-z0-9_])['"`]/gi)].map(m => m[1]).filter(id => !RT_NAMES.has(id)));
const dynamicPrefixes = [...new Set([...PAGE.matchAll(/`((?:settings|alerts|catalog|tier|mode)\.[a-z0-9_.]*?)\$\{/g)].map(m => m[1]))];

test('every literal id the page uses is in a zh_CN catalog; every dynamic family has members', () => {
  // 10-30: the 提醒 section shrank from the channel UI to five switches (≈290 literal ids)
  assert.ok(literalIds.size > 250, `only ${literalIds.size} literal ids`);
  for (const id of literalIds) assert.ok(zhAny(id), `used but not in the catalog: ${id}`);
  assert.ok(dynamicPrefixes.length >= 20, dynamicPrefixes.join(' '));
  for (const p of dynamicPrefixes) {
    if (/^(?:tier|mode)\.$/.test(p)) continue;
    assert.ok(Object.keys(ALL).some(id => id.startsWith(p)), `no message for the family ${p}…`);
  }
});

test('every settings.* / alerts.* id is used by the page or reserved (05d / 05e)', () => {
  const used = id => literalIds.has(id) || dynamicPrefixes.some(p => id.startsWith(p));
  for (const id of RESERVED_SETTINGS) assert.ok(SETTINGS[id] && /预留/.test(SETTINGS[id]._desc), `reserved ${id}`);
  for (const c of TEST_REASONS) assert.ok(ALERTS[`alerts.test_reason.${c}`], c);
  for (const c of ONBOARDING_REASONS) assert.ok(ALERTS[`alerts.onboarding_reason.${c}`], c);
  const reserved = id => RESERVED_SETTINGS.includes(id) || /^alerts\.(?:test_reason|onboarding_reason)\./.test(id);
  const unused = Object.keys({ ...SETTINGS, ...ALERTS }).filter(id => !used(id) && !reserved(id));
  assert.deepEqual(unused, []);
});

test('catalog.*: the bridge_catalog label ids of all 15 channel types and 11 protocols; this ticket\'s names', () => {
  const chans = ['desktop', 'sound', 'telegram', 'whatsapp', 'slack', 'discord', 'teams', 'google_chat', 'ntfy', 'pushover', 'gotify',
    'email_smtp', 'email_sendgrid', 'sms_twilio', 'generic_webhook'];
  const protos = ['vless_reality', 'vmess', 'trojan', 'ss', 'anytls', 'naive', 'socks5', 'hysteria2', 'tuic', 'wireguard', 'argo_xhttp'];
  const types = ['WARNING', 'CRITICAL', 'EMERGENCY', 'RECOVERY', 'POOL_UPDATE', 'BACKUP_REMINDER', 'KILL_SWITCH_ON', 'KILL_SWITCH_OFF', 'KILL_SWITCH_DISABLED', 'KILL_SWITCH_OVERRIDE'];
  const groups = ['vanguard_nodes', 'route_health', 'emergency_fallback', 'protection', 'backup'];
  for (const c of chans) {
    for (const f of ['', '.enabled', '.label', '.alert_types', '.event_groups', '.timeout', '.retries']) assert.ok(CATALOG[`catalog.alerts.${c}${f}`], `${c}${f}`);
    for (const v of types) assert.ok(CATALOG[`catalog.alerts.${c}.alert_types.choice.${v}`], `${c} ${v}`);
    for (const g of groups) assert.ok(CATALOG[`catalog.alerts.${c}.event_groups.choice.${g}`], `${c} ${g}`);
  }
  for (const p of protos) {
    for (const f of ['', '.tier', '.name', '.server']) assert.ok(CATALOG[`catalog.${p}${f}`], `${p}${f}`);
  }
  assert.ok(CATALOG['catalog.notice.ss_none_unencrypted']);
  assert.ok(CATALOG['catalog.ss.method.choice.2022-blake3-aes-128-gcm']);
  // names without a backend label_id: groups, severity presets, sound presets, scope templates
  for (const g of ['built_in', 'chat', 'push', 'email', 'sms', 'generic']) assert.ok(CATALOG[`catalog.alerts.group.${g}`], g);
  for (const p of ['urgent', 'abnormal_recovery', 'all']) { assert.ok(CATALOG[`catalog.alerts.preset.${p}`], p); assert.ok(CATALOG[`catalog.alerts.preset.${p}.desc`], p); }
  for (const p of ['soft', 'standard', 'prominent']) assert.ok(CATALOG[`catalog.alerts.sound.preset.${p}`], p);
  for (const s of ['all', 'key_events']) assert.ok(CATALOG[`catalog.alerts.scope.${s}`], s);
  // (T-P4-05e) the page names each channel by the catalog's label_id (alerts.catalog); it has no
  // table of its own: types, fields and groups come from the catalog (pages/settings/alerts_form.js)
  assert.doesNotMatch(SRC, /\bbk: '[a-z_]+'/);
  assert.match(SRC, /labelId/);
  // the catalog coverage gate is hard from this ticket on (critique C7)
  const gates = JSON.parse(read('tools/i18n_gates.json'));
  assert.deepEqual(gates.catalog, { p4: 'hard', p7: 'hard' });
});

test('counts are plural; Bastion\'s pool size is a parameter (D9); the saved-message wording is in core.savebar', () => {
  for (const [id, e] of Object.entries({ ...SETTINGS, ...ALERTS })) {
    // n / count, and the named counts of the page (the re-check's node counts, the channels' ok / bad)
    for (const p of ['n', 'count', 'ok', 'slow', 'down', 'bad']) {
      if (new RegExp(`\\{${p}\\}`).test(e.m) && !/rule\.duplicate$/.test(id)) assert.fail(`${id}: {${p}} outside plural: ${e.m}`);
    }
  }
  const bastion = Object.entries(SETTINGS).filter(([id]) => id.startsWith('settings.bastion.min.'));
  assert.ok(bastion.some(([, e]) => e.m.includes('{capacity')));
  for (const [id, e] of bastion) assert.doesNotMatch(e.m, /60/, id);
  assert.match(SRC, /core\.savebar|T\.ui\.saveBar/);
});

test('no CJK literal and no @i18n-pending in settings.js', () => {
  assert.deepEqual(scanCjkJs(SRC, 'settings.js'), []);
  assert.equal(isPending(SRC, 'js'), false);
  assert.ok(!SRC.split('\n', 1)[0].includes('@i18n-pending'));
  // relocalize 'rerender' with its two hooks (architecture §3.7 item 4)
  assert.match(SRC, /relocalize: 'rerender'/);
  assert.match(SRC, /function snapshotState\(\)/);
  assert.match(SRC, /function restoreState\(s\)/);
});

test('design/settings_demo.js is reached only inside if (__DEMO__)', () => {
  const ast = acorn.parse(SRC, { ecmaVersion: 'latest', sourceType: 'module', locations: true });
  const guarded = anc => anc.some(n => (n.type === 'IfStatement' || n.type === 'ConditionalExpression' || n.type === 'LogicalExpression')
    && JSON.stringify(n.type === 'LogicalExpression' ? n.left : n.test).includes('"name":"__DEMO__"'));
  let uses = 0;
  walk.ancestor(ast, {
    MemberExpression(n, _, anc) {
      if (n.object.type === 'Identifier' && n.object.name === 'DEMO') { uses++; assert.ok(guarded(anc), `DEMO.${n.property.name} at line ${n.loc.start.line} outside if (__DEMO__)`); }
    },
  });
  // (T-P4-05d) the update notes, directories and rule-data demo left with the real data; (T-P4-05e)
  // the alert section's demo too: the page no longer reaches the demo module
  assert.equal(uses, 0, `DEMO uses: ${uses}`);
  assert.doesNotMatch(SRC, /settings_demo\.js/);
  assert.doesNotMatch(DEMO_SRC, /^\s*import\b[^;]*demo-data/m);
});

test('the app bundle carries no settings demo (alert history, release notes, directories)', async () => {
  const { files } = await buildTarget({ target: 'app' });
  const main = [...files.keys()].find(k => /^app\.[A-Z0-9]+\.js$/.test(k));
  const js = new TextDecoder().decode(files.get(main));
  // design/settings_demo.js's data and the demo's channel seeds (CH_BASE, now inside if (__DEMO__))
  // (the CJK markers reach a locale chunk escaped; the ASCII ones are the sharp ones)
  for (const marker of ['短信备用通道', 'link.example.org/tg', 'EXAMPLE-new-token', 'EXAMPLE-expired-token', 'settings.demo.update_fail', 'set-notes-h', '原型只有简体中文界面', 'AC00000000000000000000000000000000']) {
    assert.ok(!js.includes(marker), `app bundle contains ${marker}`);
  }
});

/* ------------------------------------------------------------------ in the browser */
let dir, browser, run, cases, designIndex;
before(async () => {
  if (skip) return;
  dir = mkdtempSync(join(tmpdir(), 'tesseract-settings-i18n-'));
  const { files } = await buildTarget({ target: 'design', pseudo: true });
  writeOutput(join(dir, 'design'), files);
  designIndex = join(dir, 'design', 'index.html');
  run = await import('../render/run.mjs');
  cases = await import('../render/cases/settings_i18n.mjs');
  browser = await run.launch();
});
after(async () => {
  if (browser) await browser.close();
  if (dir) rmSync(dir, { recursive: true, force: true });
});

const CJK = /[　-〿㐀-䶿一-鿿豈-﫿＀-￯]/u;
// the page and its drawer (the shell around it re-renders on its own and is T-P4-04a's)
const SCOPE = 'section[data-page="settings"], .ovl';

// The only accepted differences from the T-P4-01 baseline (sim mode, deterministic fixture): a data
// value inside a message (a retired channel's name, a delivery error from the provider) now sits in
// its own translate="no" node (critique E2), so one row of text becomes the message row plus the
// data row; what the user reads is the same.
const SPLIT = new Map([
  ['「短信备用通道」来自旧版本，新版本已不再使用。', [['「」来自旧版本，新版本已不再使用。', 0], ['短信备用通道', 1]]],
  ['上次发送失败：机器人 Token 无效（HTTP 401）', [['上次发送失败：', 0], ['机器人 Token 无效（HTTP 401）', 1]]],
  ['失败（HTTP 401）', [['失败（）', 0], ['HTTP 401', 1]]],
]);

// (T-P4-05d) superseded: the page now shows config.snapshot and the runtime's facts (the status line,
// read-only rows, per-action rule groups, the application area's real states), so its text tree is
// no longer the prototype's; the render cases of test/render/cases/settings.mjs take over (ruling
// asked in the T-P4-05d report)
const SUPERSEDED = 'superseded by T-P4-05d (real data): see test/render/cases/settings.mjs';
test('regression: sim mode, deterministic fixture: the settings text tree equals the T-P4-01 baseline (allowed: data values split into translate="no" nodes)', { skip: skip || SUPERSEDED }, async () => {
  const cap = await import('../baseline/capture.mjs');
  const context = await cap.newContext(browser);
  const r = await cap.capturePage(context, designIndex, 'settings');
  await context.close();
  assert.deepEqual(r.errors, []);
  const base = JSON.parse(read('test/baseline/settings.textree.json'));
  let split = 0;
  const expected = base.flatMap(row => {
    const s = SPLIT.get(row[2]);
    if (!s) return [row];
    split++;
    return s.map(([text, deeper]) => [row[0] + deeper, row[1], text, row[3], row[4], row[5]]);
  });
  assert.equal(split, 3, 'three baseline rows carry a data value');
  assert.equal(r.tree.length, expected.length);
  for (let i = 0; i < expected.length; i++) assert.deepEqual(r.tree[i], expected[i], `row ${i}`);
});

async function fixturePage(lang, size = '1280x800') {
  const cell = await run.openCell(browser, { distDir: join(dir, 'design'), scenario: cases.RUNNING, lang, size, page: 'settings' });
  if (lang === 'pseudo') await run.runAction(cell.page, { eval: cases.pseudoBreaks });
  return cell;
}
async function look(page, where) {
  const r = await page.evaluate(cases.settingsTextInPage);
  const strip = v => r.data.reduce((x, d) => x.split(d).join(''), v);
  assert.deepEqual(r.texts.filter(s => CJK.test(s)), [], `${where}: CJK text outside translate="no"`);
  assert.deepEqual(r.attrs.filter(([, v]) => CJK.test(strip(v))).map(([a, v]) => `${a}=${v}`), [], `${where}: CJK attributes`);
  const plain = r.attrs.filter(([, v]) => !v.includes('⟦') && !/^[\d\s:.,×·/–\-—%()]*$|^(?:Tesseract|Mihomo|Vanguard|Sentinel|Bastion|Gateway)$/.test(v));
  assert.deepEqual(plain, [], `${where}: attributes not from the catalog`);
  assert.ok(r.texts.filter(s => s.includes('⟦')).length > 100, `${where}: catalog text shown`);
  return r;
}

test('pseudo (fixture mode, ASCII frames): all seven sections and the Telegram drawer show no CJK outside translate="no"', { skip }, async () => {
  const cell = await fixturePage('pseudo');
  const { page } = cell;
  const ids = await page.evaluate(() => [...document.querySelectorAll('section[data-page="settings"] .fsec[id]')].map(s => s.id));
  assert.deepEqual(ids, ['set-conn', 'set-route', 'set-dns', 'set-nodes', 'set-safety', 'set-alerts', 'set-app']);
  await look(page, 'page');
  // (T-P5-04) the language list's shown choice is the language's own name, which no catalog
  // translates (settings.app.lang.name.*): the pseudo language keeps exactly that name, under
  // translate="no" with the language's own lang (the page's other text stays pseudo, above)
  const lang = await page.evaluate(() => {
    const v = document.querySelector('#set-lang .select-v');
    return { text: v.textContent.trim(), translate: v.getAttribute('translate'), lang: v.getAttribute('lang'), names: ['zh_cn', 'en_us', 'ja_jp', 'ko_kr', 'fr_fr', 'de_de', 'es_es', 'pt_br', 'ru_ru'].map(k => T.t(`settings.app.lang.name.${k}`)) };
  });
  assert.deepEqual(lang.names, ['简体中文', 'English', '日本語', '한국어', 'Français', 'Deutsch', 'Español', 'Português', 'Русский'], 'pseudo: the nine own names, not pseudo-localised');
  assert.deepEqual({ text: lang.text, translate: lang.translate, lang: lang.lang }, { text: '简体中文', translate: 'no', lang: 'zh-CN' });
  // the data is there, under translate="no" (the demo's alert history)
  const data = await page.evaluate(() => [...document.querySelectorAll('#set-recent [translate="no"]')].map(e => e.textContent).join(' | '));
  assert.match(data, /Vanguard/);
  await page.evaluate(() => document.querySelector('.set-ch[data-ch="telegram"]').click()); await run.runAction(page, { page_ms: 600 });
  await look(page, 'telegram drawer (quick setup)');
  await page.click('.ovl .drawer .set-ch-tabs .tab[data-v="manual"]'); await run.runAction(page, { page_ms: 600 });
  await page.click('.ovl .drawer .set-adv-t'); await run.runAction(page, { page_ms: 600 });
  await look(page, 'telegram drawer (manual, advanced)');
  assert.deepEqual(cell.errors, []);
  await cell.context.close();
});

// (T-P5-07) the open language menu: every option is the language's own name under translate="no"
// with its own lang (T.ui.select option lang / translate), so the general rule (no CJK outside
// translate="no", look()) holds with the menu open, with no exemption by id; choosing an option
// moves both attributes to the shown choice
const LANG_TAGS = { zh_CN: 'zh-CN', en_US: 'en-US', ja_JP: 'ja-JP', ko_KR: 'ko-KR', fr_FR: 'fr-FR', de_DE: 'de-DE', es_ES: 'es-ES', pt_BR: 'pt-BR', ru_RU: 'ru-RU' };
const OWN_NAMES = ['简体中文', 'English', '日本語', '한국어', 'Français', 'Deutsch', 'Español', 'Português', 'Русский'];
test('pseudo: the open language menu shows the nine own names under translate="no" with their lang; the choice carries them', { skip }, async () => {
  const cell = await fixturePage('pseudo');
  const { page } = cell;
  await page.click('#set-lang'); await run.runAction(page, { page_ms: 600 });
  const opts = await page.evaluate(() => [...document.querySelectorAll('.menu[role="listbox"] .menu-i')].map(b => {
    const l = b.querySelector('.menu-l');
    return { text: l.textContent.trim(), lang: l.getAttribute('lang'), translate: l.getAttribute('translate'), btnLang: b.getAttribute('lang'), btnTranslate: b.getAttribute('translate'), selected: b.getAttribute('aria-selected') };
  }));
  assert.deepEqual(opts.map(o => o.text), OWN_NAMES);
  assert.deepEqual(opts.map(o => o.lang), Object.values(LANG_TAGS));
  assert.ok(opts.every(o => o.translate === 'no'), 'translate="no" and lang sit on each label');
  // (T-P5-07 fix round) no hint: the option itself (role=option) carries them too
  assert.deepEqual(opts.map(o => [o.btnLang, o.btnTranslate]), Object.values(LANG_TAGS).map(t => [t, 'no']));
  const trig = await page.evaluate(() => [document.getElementById('set-lang').getAttribute('lang'), document.getElementById('set-lang').getAttribute('translate')]);
  assert.deepEqual(trig, [null, null], 'the select button itself carries neither');
  assert.deepEqual(opts.map(o => o.selected), OWN_NAMES.map((_, i) => String(i === 0)));
  await look(page, 'language menu open');
  await page.click('.menu[role="listbox"] .menu-i[data-i="8"]'); await run.runAction(page, { page_ms: 600 });
  const shown = await page.evaluate(() => { const v = document.querySelector('#set-lang .select-v'); return { text: v.querySelector('.sw:not([aria-hidden])').textContent, lang: v.getAttribute('lang'), translate: v.getAttribute('translate') }; });
  assert.deepEqual(shown, { text: 'Русский', lang: 'ru-RU', translate: 'no' });
  assert.deepEqual(cell.errors, []);
  await cell.context.close();
});

// (T-P5-07) core.js T.ui.select / T.ui.menu: an option's lang / translate reach its menu label and,
// while it is the choice, the shown value; an option without them (or the placeholder) leaves
// neither attribute; translate false / true are 'no' / 'yes'; the menu item (role=option) carries
// them as well when it has no hint
test('T.ui.select: option lang / translate on the menu labels and the shown value; none without them', { skip }, async () => {
  const cell = await fixturePage('zh_CN');
  const { page } = cell;
  const read = () => page.evaluate(() => {
    const v = document.querySelector('#__t_sel .select-v');
    const m = [...document.querySelectorAll('.menu[role="listbox"] .menu-l')].map(l => [l.getAttribute('lang'), l.getAttribute('translate')]);
    const it = [...document.querySelectorAll('.menu[role="listbox"] .menu-i')].map(l => [l.getAttribute('lang'), l.getAttribute('translate')]);
    return { v: [v.getAttribute('lang'), v.getAttribute('translate')], m, it };
  });
  await page.evaluate(() => {
    const b = document.createElement('button'); b.id = '__t_sel'; b.className = 'sm';
    document.querySelector('section[data-page="settings"]').prepend(b);
    window.__tSel = T.ui.select(b, { value: 'a', placeholder: 'ph', options: [
      { value: 'a', label: 'Deutsch', lang: 'de-DE', translate: 'no' },
      { value: 'b', label: 'plain' },
      { value: 'c', label: 'yes', lang: 'en', translate: true },
      { value: 'd', label: 'off', translate: false },
      { value: 'e', label: 'hinted', hint: 'h', lang: 'fr-FR', translate: 'no' },
    ] });
  });
  assert.deepEqual((await read()).v, ['de-DE', 'no'], 'the shown choice carries its option\'s attributes');
  await page.evaluate(() => window.__tSel.set('b', false));
  assert.deepEqual((await read()).v, [null, null], 'an option without them clears both');
  await page.evaluate(() => window.__tSel.set('c', false));
  assert.deepEqual((await read()).v, ['en', 'yes']);
  await page.evaluate(() => window.__tSel.set('zz', false));
  assert.deepEqual((await read()).v, [null, null], 'the placeholder carries neither');
  await page.click('#__t_sel'); await run.runAction(page, { page_ms: 400 });
  const open = await read();
  assert.deepEqual(open.m, [['de-DE', 'no'], [null, null], ['en', 'yes'], [null, 'no'], ['fr-FR', 'no']]);
  // (T-P5-07 fix round) the option itself too, unless it has a hint (the hint is UI text)
  assert.deepEqual(open.it, [['de-DE', 'no'], [null, null], ['en', 'yes'], [null, 'no'], [null, null]]);
  await page.keyboard.press('Escape'); await run.runAction(page, { page_ms: 400 });
  // the other selects of the page (no lang / translate in their options) are unchanged
  const others = await page.evaluate(() => [...document.querySelectorAll('section[data-page="settings"] .select:not(#__t_sel):not(#set-lang) .select-v')].map(v => [v.getAttribute('lang'), v.getAttribute('translate')]));
  assert.ok(others.length >= 2 && others.every(([l, t]) => l === null && t === null), JSON.stringify(others));
  assert.deepEqual(cell.errors, []);
  await cell.context.close();
});

test('a language switch with drafts keeps the drafts, dirty marks, typed text, open drawer and scroll; back in zh_CN the page reads as before', { skip }, async () => {
  const cell = await fixturePage('zh_CN');
  const { page } = cell;
  await page.click('.switch[data-k="ipv6"]'); await run.runAction(page, { page_ms: 600 });
  await page.click('.switch[data-k="lan"]'); await run.runAction(page, { page_ms: 600 });
  await page.fill('#set-rule-in', 'example.org'); await run.runAction(page, { page_ms: 600 });
  await page.evaluate(() => { document.querySelector('#host').scrollTop = 1400; }); await run.runAction(page, { page_ms: 300 });
  await page.evaluate(() => document.querySelector('.set-ch[data-ch="telegram"]').click()); await run.runAction(page, { page_ms: 600 });
  await page.click('.ovl .drawer .set-ch-tabs .tab[data-v="manual"]'); await run.runAction(page, { page_ms: 600 });
  await page.click('.ovl .drawer .set-adv-t'); await run.runAction(page, { page_ms: 600 });
  const s0 = await page.evaluate(cases.draftStateInPage);
  assert.equal(s0.switches.ipv6, 'true'); assert.equal(s0.switches.lan, 'true');
  assert.equal(s0.typed.rule, 'example.org');
  assert.ok(s0.dirty && s0.count >= 3, `dirty ${s0.dirty} count ${s0.count}`);
  assert.ok(s0.tocDots.includes('set-conn') && s0.tocDots.includes('set-route'), s0.tocDots.join(','));
  assert.ok(s0.drawer && s0.drawer.adv, 'drawer open with the advanced block');
  assert.equal(s0.scroll, 1400);
  const tree0 = await page.evaluate(cases.settingsTreeInPage, SCOPE);

  await page.evaluate(() => T.i18n.set('pseudo')); await run.runAction(page, { page_ms: 800 });
  const s1 = await page.evaluate(cases.draftStateInPage);
  assert.deepEqual(s1, s0, 'pseudo: the state a language switch must keep');
  const tab = await page.evaluate(() => (document.querySelector('.ovl .drawer .set-ch-tabs .tab[aria-selected="true"]') || {}).dataset);
  assert.equal(tab && tab.v, 'manual');
  const r = await page.evaluate(cases.settingsTextInPage);
  assert.deepEqual(r.texts.filter(s => CJK.test(s)), [], 'pseudo: CJK left after the switch');

  await page.evaluate(() => T.i18n.set('zh_CN')); await run.runAction(page, { page_ms: 800 });
  const s2 = await page.evaluate(cases.draftStateInPage);
  assert.deepEqual(s2, s0, 'zh_CN again: the same state');
  const tree2 = await page.evaluate(cases.settingsTreeInPage, SCOPE);
  assert.deepEqual(tree2, tree0, 'zh_CN again: the same text tree');
  assert.deepEqual(cell.errors, []);
  await cell.context.close();
});

test('a language switch keeps an open inline edit (public source), text typed into a drawer field and the drawer\'s own scroll', { skip }, async () => {
  const cell = await fixturePage('zh_CN');
  const { page } = cell;
  await page.evaluate(() => document.querySelector('#set-src-list .set-src-r [data-edit]').click()); await run.runAction(page, { page_ms: 600 });
  await page.evaluate(() => { const i = document.querySelector('#set-src-list .set-src-r.is-editing input'); i.value = 'https://edit.example/x'; i.dispatchEvent(new Event('input', { bubbles: true })); });
  await run.runAction(page, { page_ms: 600 });
  await page.evaluate(() => document.querySelector('.set-ch[data-ch="ntfy"]').click()); await run.runAction(page, { page_ms: 700 });
  await page.evaluate(() => {
    const i = document.querySelector('.ovl .drawer [data-f] input'); i.value = 'typed-mid-switch'; i.dispatchEvent(new Event('input', { bubbles: true }));
    document.querySelector('.ovl .drawer .ovl-body').scrollTop = 120;
  });
  await run.runAction(page, { page_ms: 600 });
  const state = () => page.evaluate(() => ({
    edit: (document.querySelector('#set-src-list .set-src-r.is-editing input') || {}).value || null,
    field: (document.querySelector('.ovl .drawer [data-f] input') || {}).value || null,
    scroll: (document.querySelector('.ovl .drawer .ovl-body') || {}).scrollTop || 0,
  }));
  const s0 = await state();
  assert.equal(s0.edit, 'https://edit.example/x'); assert.equal(s0.field, 'typed-mid-switch'); assert.ok(s0.scroll > 0, `drawer scroll ${s0.scroll}`);
  await page.evaluate(() => T.i18n.set('pseudo')); await run.runAction(page, { page_ms: 800 });
  assert.deepEqual(await state(), s0, 'pseudo: the same edit, field text and drawer scroll');
  assert.deepEqual(cell.errors, []);
  await cell.context.close();
});

// the page left scrolls #host (shared by every page): made tall here, as any page may be (the
// logs page fills the window and scrolls inside itself; its section is let grow for the test)
async function scrollAway(page, to = 'logs', top = 900) {
  await page.evaluate(id => T.go(id, { force: true }), to); await run.runAction(page, { page_ms: 800 });
  return page.evaluate(([id, y]) => {
    const sec = document.querySelector(`section[data-page="${id}"]`);
    sec.style.height = '4800px'; sec.style.flex = 'none';
    const h = document.querySelector('#host'); h.scrollTop = y; return h.scrollTop;
  }, [to, top]);
}
const kept = s => ({ switches: s.switches, typed: s.typed, changedRows: s.changedRows, tocDots: s.tocDots, dirty: s.dirty, count: s.count });

test('a language switch while the page is away (core rerender on the next T.go): drafts kept, the scroll is not the page left behind; a {focus} landing still lands', { skip }, async () => {
  const cell = await fixturePage('zh_CN');
  const { page } = cell;
  await page.click('.switch[data-k="ipv6"]'); await run.runAction(page, { page_ms: 600 });
  await page.fill('#set-rule-in', 'example.org'); await run.runAction(page, { page_ms: 600 });
  const s0 = await page.evaluate(cases.draftStateInPage);
  assert.ok(s0.dirty && s0.count >= 2, `dirty ${s0.dirty} count ${s0.count}`);

  assert.equal(await scrollAway(page), 900, 'the page left scrolls');
  await page.evaluate(() => T.i18n.set('pseudo')); await run.runAction(page, { page_ms: 800 });
  await page.evaluate(() => T.go('settings')); await run.runAction(page, { page_ms: 900 });
  const s1 = await page.evaluate(cases.draftStateInPage);
  assert.equal(s1.scroll, 0, 'the settings page lands at its top, not at the scroll of the page left');
  assert.deepEqual(kept(s1), kept(s0), 'pseudo: drafts, dirty marks and the save bar count kept');
  await run.runAction(page, { eval: cases.pseudoBreaks });
  const r = await page.evaluate(cases.settingsTextInPage);
  assert.deepEqual(r.texts.filter(s => CJK.test(s)), [], 'pseudo: CJK left after the switch');

  // the same path with a landing target: the glide starts from the page's top and ends at the row
  assert.equal(await scrollAway(page), 900);
  await page.evaluate(() => T.i18n.set('zh_CN')); await run.runAction(page, { page_ms: 800 });
  await page.evaluate(() => T.go('settings', { focus: 'dns' })); await run.runAction(page, { page_ms: 2500 });
  const land = await page.evaluate(() => {
    const h = document.querySelector('#host'), row = document.querySelector('#set-dns-row');
    return { top: Math.round(row.getBoundingClientRect().top - h.getBoundingClientRect().top), scroll: h.scrollTop };
  });
  assert.ok(land.scroll > 0 && land.top >= 0 && land.top <= 200, `the dns row in view: ${JSON.stringify(land)}`);
  assert.deepEqual(kept(await page.evaluate(cases.draftStateInPage)), kept(s0), 'zh_CN again: drafts kept');
  assert.deepEqual(cell.errors, []);
  await cell.context.close();
});

test('a drawer reopened by a language switch gives focus back to its opener (tile or in-use row) when it closes', { skip }, async () => {
  const cell = await fixturePage('zh_CN');
  const { page } = cell;
  for (const [sel, lang] of [['.set-ch[data-ch="ntfy"]', 'pseudo'], ['.set-chrow[data-ch="telegram"]', 'zh_CN']]) {
    await page.focus(`section[data-page="settings"] ${sel}`);
    await page.keyboard.press('Enter'); await run.runAction(page, { page_ms: 700 });
    assert.ok(await page.evaluate(() => !!document.querySelector('.ovl .drawer')), `${sel}: drawer open`);
    await page.evaluate(l => T.i18n.set(l), lang); await run.runAction(page, { page_ms: 800 });
    assert.ok(await page.evaluate(() => !!document.querySelector('.ovl .drawer')), `${sel}: drawer reopened after the switch`);
    await page.keyboard.press('Escape'); await run.runAction(page, { page_ms: 800 });
    const ae = await page.evaluate(s => {
      const a = document.activeElement;
      return { tag: a ? a.tagName : null, match: !!(a && a.isConnected && a.matches(`section[data-page="settings"] ${s}`)) };
    }, sel);
    assert.ok(ae.match, `${sel}: focus back on the opener, not ${ae.tag}`);
  }
  assert.deepEqual(cell.errors, []);
  await cell.context.close();
});

// (T-P5-04) relative dates: settings.time.today / yesterday / days_ago are written in their
// in-sentence form (「Updated today 11:00」); where one stands on its own (a Geo status) the page
// capitalises the first letter of the whole text shown, in the page's language. The running
// scenario with the rule data updated an hour before the fixture clock's start (2026-09-29
// 12:00 Asia/Shanghai): 「today 11:00」.
const GEO_TODAY = Date.UTC(2026, 8, 29, 3, 0, 0);
const CAP_EXPECT = {
  en_US: { inline: 'today 11:00', alone: 'Today 11:00' },
  de_DE: { inline: 'heute 11:00', alone: 'Heute 11:00' },
  fr_FR: { inline: 'aujourd’hui à 11:00', alone: 'Aujourd’hui à 11:00' },
  ja_JP: { inline: '今日 11:00', alone: '今日 11:00' },
};
for (const [lang, want] of Object.entries(CAP_EXPECT)) {
  test(`relative dates in ${lang}: in-sentence form inside a sentence, first letter capitalised when shown alone`, { skip }, async () => {
    const scenario = JSON.parse(JSON.stringify(cases.RUNNING).replaceAll('"updated_at_ms":1789996400000', `"updated_at_ms":${GEO_TODAY}`));
    const cell = await run.openCell(browser, { distDir: join(dir, 'design'), scenario, lang, size: '1280x800', page: 'settings' });
    const { page } = cell;
    const r = await page.evaluate(() => ({
      today: T.t('settings.time.today', { time: '11:00' }),
      link: document.querySelector('#set-geo-link-t').textContent,
      linkWant: T.t('settings.app.geo.link_updated', { when: T.t('settings.time.today', { time: '11:00' }) }),
      status: document.querySelector('#set-geo .set-geo-s').textContent.trim(),
    }));
    assert.equal(r.today, want.inline, 'the catalog writes the in-sentence form');
    assert.equal(r.link, `· ${r.linkWant}`, 'inside a sentence: as the catalog writes it');
    assert.ok(r.link.includes(want.inline), `inside a sentence: ${r.link}`);
    assert.equal(r.status, want.alone, 'shown alone: first letter capitalised');
    assert.deepEqual(cell.errors, []);
    await cell.context.close();
  });
}
// (T-P5-04 修复第 1 轮) the two other places a relative date stands alone, each guarded on its own:
//   * a Geo status overdue for an update: settings.app.geo.stale starts with {when} in these
//     catalogs, and the running scenario's rule data is 8 days old (「vor 8 Tagen · …」 in the
//     sentence of the catalog, 「Vor 8 Tagen · …」 shown); en_US starts with a digit (unchanged);
//   * a delivery record's time in a channel's drawer (alerts.deliveries; alerts_a01_save_checks
//     with its healthy record moved to today 11:00).
const STALE_EXPECT = {
  en_US: { status: '8 days ago · Update overdue', cell: 'Today 11:00' },
  de_DE: { status: 'Vor 8 Tagen · Aktualisierung überfällig', cell: 'Heute 11:00' },
};
const DELIVERED_AT = 1789999410000; // alerts_a01_save_checks: the healthy record of telegram-main
for (const [lang, want] of Object.entries(STALE_EXPECT)) {
  test(`relative dates in ${lang}: an overdue Geo status and a delivery record's time are capitalised as shown alone`, { skip }, async () => {
    const stale = await fixturePage(lang);
    const g = await stale.page.evaluate(() => ({
      status: document.querySelector('#set-geo .set-geo-s').textContent.trim(),
      inline: T.t('settings.app.geo.stale', { when: T.t('settings.time.days_ago', { n: 8 }) }),
    }));
    assert.equal(g.status, want.status, 'Geo status shown alone');
    assert.equal(g.inline.slice(1), g.status.slice(1), 'the rest is the catalog\'s sentence as written');
    assert.deepEqual(stale.errors, []);
    await stale.context.close();

    const text = read('test/fixtures/scenarios/alerts_a01_save_checks.json');
    assert.ok(text.includes(`"last_attempt_ms": ${DELIVERED_AT}`), 'the scenario still has the healthy record');
    const scenario = JSON.parse(text.replaceAll(`"last_attempt_ms": ${DELIVERED_AT}`, `"last_attempt_ms": ${GEO_TODAY}`));
    const cell = await run.openCell(browser, { distDir: join(dir, 'design'), scenario, lang, size: '1280x800', page: 'settings' });
    const { page } = cell;
    await page.evaluate(() => [...document.querySelectorAll('#set-ch-use .set-chrow')].find(r => r.dataset.id === 'telegram-main').click());
    await run.runAction(page, { page_ms: 900 });
    const d = await page.evaluate(() => ({
      cells: [...document.querySelectorAll('.ovl .drawer .set-ch-log dt')].map(e => e.textContent.trim()),
      inline: T.t('settings.time.today', { time: '11:00' }),
    }));
    assert.equal(d.inline, want.cell.charAt(0).toLocaleLowerCase(lang.replace('_', '-')) + want.cell.slice(1), 'the catalog writes the in-sentence form');
    assert.ok(d.cells.includes(want.cell), `the delivery record of today shown alone: ${JSON.stringify(d.cells)}`);
    assert.deepEqual(cell.errors, []);
    await cell.context.close();
  });
}
