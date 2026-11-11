// Render cases of T-P4-05d (the settings page on real data: the save model, every section, the
// application area, the language row, the {focus} landings). Scenarios:
// ui/test/fixtures/scenarios/settings_*.json (generated; ASCII data, the sidecar's masked source
// hint aside). Each case drives the page the way a user would (a switch, the save bar's 保存, a
// select) and asserts the page's own answer to the scenario's frames: the save bar's result, each
// row's label, the status line, the section's live state. Words are compared with the catalog
// (T.t in the page), so the same assertion holds in zh_CN and in the pseudo language.
//
// The B-item controls (architecture §5) are asserted by settings_features: present only when both
// keys hold (hello.caps.features in the scenario and the build's ENABLED_H_FEATURES:
// `run.mjs --build-flags "--h-features all"`), absent otherwise.

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { LANGS9 } from './overview_i18n.mjs';

const SEC = 'section[data-page="settings"]';
const SECTIONS = ['set-conn', 'set-route', 'set-dns', 'set-nodes', 'set-safety', 'set-alerts', 'set-app'];

/* Runs in the page: what the cases read (words compared with T.t there). */
export function settingsStateInPage() {
  const sec = document.querySelector('section[data-page="settings"]');
  // (T-P5-11: ASCII white space only: a no-break space of fr / ru (before : ; « ») stays, as in T.t's output)
  const txt = el => (el ? el.textContent.replace(/[ \t\n\r\f]+/g, ' ').trim() : null);
  const shown = el => !!(el && el.isConnected && el.checkVisibility({ opacityProperty: true, visibilityProperty: true }));
  const bar = document.querySelector('.savebar');
  const tags = {};
  for (const t of sec.querySelectorAll('.set-res-tag')) { const row = t.closest('[id]'); if (row) tags[row.id] = [...t.classList].find(c => c.startsWith('is-')) || ''; }
  const unknown = [...sec.querySelectorAll('.set-unk')].map(u => u.closest('[id]').id);
  return {
    sections: [...sec.querySelectorAll('.fsec[id]')].map(s => s.id),
    status: txt(sec.querySelector('#set-status')),
    statusKinds: [...sec.querySelectorAll('#set-status .callout')].map(c => c.className),
    bar: bar && shown(bar) ? {
      cls: bar.className, count: txt(bar.querySelector('.sb-count .sw:not([aria-hidden])') || bar.querySelector('.sb-count')),
      eff: txt(bar.querySelector('.sb-eff .sw:not([aria-hidden])') || bar.querySelector('.sb-eff')),
      save: txt(bar.querySelector('.sb-save .lab .sw:not([aria-hidden])') || bar.querySelector('.sb-save .lab')),
      saveShown: shown(bar.querySelector('.sb-save')), closeShown: shown(bar.querySelector('.sb-close')),
    } : null,
    barCount: window.T.ui.saveBarCurrent ? window.T.ui.saveBarCurrent.count : 0,
    dirtyCount: window.T.ui.saveBar.dirtyCount(),
    tags, unknown,
    live: Object.fromEntries(['set-ks-live', 'set-bastion-live', 'set-src-live', 'set-auto-live', 'set-mode-st', 'set-upd-st', 'set-lang-d', 'set-tray-d', 'set-auto-d', 'set-ver-core', 'set-lat-sum', 'set-bastionMin-row']
      .map(id => [id, txt(sec.querySelector(`#${id} .sw:not([aria-hidden])`) || sec.querySelector(`#${id}`))])),
    modeSt: (sec.querySelector('#set-mode-st') || {}).className || '',
    hiddenNotes: [...sec.querySelectorAll('.set-hidden-note')].filter(shown).map(n => n.id),
    sources: [...sec.querySelectorAll('#set-src-list .set-src-r')].map(r => txt(r.querySelector('.set-src-v'))),
    rules: [...sec.querySelectorAll('#set-rl-custom .set-rule')].map(r => txt(r.querySelector('.set-rule-v'))),
    geo: [...sec.querySelectorAll('#set-geo .set-geo-i')].map(c => c.className),
    geoErr: [...sec.querySelectorAll('#set-geo-err:not([hidden]) .set-geo-e')].map(txt),
    inert: Object.fromEntries(['ks', 'ksLan', 'lan', 'autostart', 'tray', 'bastionOn', 'latency'].map(k => {
      const sw = sec.querySelector(`.switch[data-k="${k}"]`) || (k === 'latency' ? sec.querySelector('.set-lat-top') : null);
      return [k, !!(sw && sw.closest('[inert]'))];
    })),
    trayOff: !!(sec.querySelector('#set-tray-row') || {}).classList?.contains('is-off'),
    active: document.activeElement ? (document.activeElement.id || document.activeElement.className) : null,
  };
}

/** the problems of one expectation list: [ok, message] pairs */
const check = pairs => pairs.filter(([ok]) => !ok).map(([, m]) => m);
const state = page => page.evaluate(settingsStateInPage);
const words = (page, list) => page.evaluate(l => l.map(([id, p]) => window.T.t(id, p || {})), list);
const run = (fn, ...more) => async ({ page }) => ({ problems: await fn(page, ...more) });

// user actions
const click = sel => [{ click: sel }, { page_ms: 400 }];
const save = [{ click: '.savebar .sb-save' }, { page_ms: 200 }, { advance_ms: 50 }, { page_ms: 300 }];
// (T-P5-11: the cases run in every language; in a cell whose language is the i-th option already,
// the next option is picked, so the language really changes in the draft)
const pickLang = i => [{ eval: () => { window.__l0 = window.T.i18n.lang; } }, { click: '#set-lang' }, { page_ms: 300 },
  { eval: n => {
    // (each option's name carries its own language: T.ui.select puts lang="en-US" … on it)
    const items = [...document.querySelectorAll('.menu .menu-i')], cur = window.T.i18n.lang.replace('_', '-');
    const langOf = x => (x.querySelector('[lang]') || x).getAttribute('lang');
    items[langOf(items[n]) === cur ? (n + 1) % items.length : n].click();
  }, arg: i }, { page_ms: 400 }];
const confirmYes = [{ click: '.ovl .dlg [data-a="yes"]' }, { page_ms: 400 }];

/* ---- fix round 1: scenarios derived from the generated ones (the page's request payloads are
   asserted from transport().invocations: what the user did must be what is sent) ---- */
const scenario = name => JSON.parse(readFileSync(fileURLToPath(new URL(`../../fixtures/scenarios/${name}.json`, import.meta.url)), 'utf8'));
const cloneJ = v => JSON.parse(JSON.stringify(v));
const ENV = { epoch: 'rt-fixture-e1', produced_at_ms: 1790000000000, sent_at_ms: 1790000000001 };
const handlerOf = (sc, cmd) => sc.steps.find(s => s.on_invoke && s.on_invoke.cmd === cmd);
/* the n-th settings.apply answer (n from 0): the base scenario's frames with the job's ids and
   sequence numbers moved on, the done result's sections / pending replaced, extra frames (a topic the
   sidecar records inside the job) put before the job's end; drop: no terminal frame (a test delivers it) */
function applyAnswer(sc, n, { rev, sections, pending = null, before = [], drop = false }) {
  const r = cloneJ(handlerOf(sc, 'settings.apply').reply);
  const out = [];
  for (const f of r) {
    if (f.t === 'ack') f.op = `op-save-${n + 1}`;
    if (f.t === 'evt' && f.topic === 'jobs') { f.data.op = `op-save-${n + 1}`; f.seq += n * 3; f.rev += n * 3; }
    if (f.t === 'evt' && f.topic === 'config.changed') { f.seq += n; f.rev += n; f.data.rev = rev; f.data.sections = cloneJ(sections); f.data.pending = cloneJ(pending); }
    if (f.t === 'evt' && f.topic === 'jobs' && (f.data.state === 'done' || f.data.state === 'failed')) {
      if (drop) continue;
      f.data.result = { rev, sections: cloneJ(sections), pending: cloneJ(pending) };
      out.push(...before);
    }
    if (drop && f.t === 'evt' && f.topic === 'config.changed') continue;
    out.push(f);
  }
  return { on_invoke: { cmd: 'settings.apply' }, reply: out };
}
/* a later config.snapshot answer: the base one with its rev and values changed */
function snapshotAnswer(sc, rev, edit) {
  const h = cloneJ(handlerOf(sc, 'config.snapshot'));
  h.reply[0].data.rev = rev;
  edit(h.reply[0].data.values);
  return h;
}
function withHandlers(sc, name, replace, extra) {
  const out = cloneJ(sc);
  out.name = name;
  out.steps = out.steps.filter(s => !(s.on_invoke && replace.includes(s.on_invoke.cmd))).concat(extra);
  return out;
}
const applied = page => page.evaluate(() => window.__TESSERACT_DESIGN__.transport().invocations.filter(c => c.kind === 'rt' && c.cmd === 'settings.apply').map(c => c.args));
const invoked = (page, cmd) => page.evaluate(c => window.__TESSERACT_DESIGN__.transport().invocations.filter(x => x.kind === 'rt' && x.cmd === c).length, cmd);
const pressSave = [{ press: 'Control+s' }, { page_ms: 400 }];

// the D8 save re-reads autostart.state (the shell's --autostart reads the new value)
const PAYLOAD_TRAY = (() => {
  const sc = scenario('settings_save_applied');
  return withHandlers(sc, 'settings_save_payload_port_tray', [], [cloneJ(handlerOf(sc, 'autostart.state'))]);
})();
// M1: the hidden list (killswitch.additional_whitelist withheld whole) with an answer to the save
const HIDDEN_SAVE = (() => {
  const sc = scenario('settings_hidden_list');
  return withHandlers(sc, 'settings_hidden_save', [], [applyAnswer(scenario('settings_save_applied'), 0,
    { rev: 8, sections: [{ section: 'killswitch', saved: true, applied: true, effect: 'none', error: null }] })]);
})();
// M2: the language saved while the job's end is delivered by the test (after host:language)
const LANG_OPEN = (() => {
  const sc = scenario('settings_lang_saved');
  return withHandlers(sc, 'settings_lang_host_first', ['settings.apply'], [applyAnswer(sc, 0, { rev: 8, sections: [], drop: true })]);
})();
// M3: two source saves; the sidecar records bastion.sources (new ids) inside each job, before its end
const SOURCES_TWICE = (() => {
  const base = scenario('settings_save_applied');
  const gh = { id: 'src-gh-1', kind: 'github_repos', display: 'GitHub', enabled: true, hint: 'e•••/f•••' };
  const sub = { id: 'src-sub-1', kind: 'subscription_urls', display: 'Subscription', enabled: true };
  const real1 = { id: 'src-sub-REAL', kind: 'subscription_urls', display: 'Subscription', enabled: true, hint: 's•••' };
  const real2 = { id: 'src-sub-REAL2', kind: 'subscription_urls', display: 'Subscription', enabled: true, hint: 'o•••' };
  const topic = (n, items) => ({ ...ENV, t: 'evt', topic: 'bastion.sources', rev: 2 + n, kind: 'snapshot', data: { items } });
  const sec = [{ section: 'sources', saved: true, applied: true, effect: 'reload', error: null }];
  return withHandlers(base, 'settings_sources_twice', ['settings.apply'], [
    applyAnswer(base, 0, { rev: 8, sections: sec, before: [topic(0, [gh, sub, real1])] }),
    applyAnswer(base, 1, { rev: 9, sections: sec, before: [topic(1, [gh, sub, real1, real2])] }),
    snapshotAnswer(base, 8, v => { v.sources = [gh, sub, real1]; }),
    snapshotAnswer(base, 9, v => { v.sources = [gh, sub, real1, real2]; }),
  ]);
})();
// M4: two deferred saves while the service starts; the ledger rebases intent-1 into intent-2
const PENDING_REBASE = (() => {
  const sc = scenario('settings_save_pending');
  const sec = [{ section: 'network', saved: true, applied: false, effect: 'deferred', error: null }];
  const pend = (id, rev) => ({ intent_id: id, config_rev: rev, generation: 4, state: 'pending', effect: 'restart', error: null });
  return withHandlers(sc, 'settings_pending_rebase', ['settings.apply'], [
    applyAnswer(sc, 0, { rev: 8, sections: sec, pending: pend('intent-1', 8) }),
    applyAnswer(sc, 1, { rev: 9, sections: sec, pending: pend('intent-2', 9) }),
  ]);
})();
// M5: a section saved but not applied that needs no Core (the system proxy override failed)
const NO_RETRY = (() => {
  const sc = scenario('settings_save_not_applied');
  return withHandlers(sc, 'settings_save_no_retry', ['settings.apply'], [applyAnswer(sc, 0, { rev: 8,
    sections: [{ section: 'network', saved: true, applied: false, effect: 'none', error: { code: 'io', message_key: 'errors.settings.apply_failed', params: {}, reason_code: 'system_proxy_override_failed' } }] })]);
})();
/* 续复核1 MUST 1: the config revision lives in the sidecar's memory, a new process counts from 0
   again. A save (rev 8), then the sidecar is gone (rt:reset runtime_gone) and a new one says hello
   in epoch e2; its snapshot reads rev 1: the next save's base_rev is 1, not the lost epoch's 8 */
const E2 = 'rt-fixture-e2';
const inEpoch = (f, ep) => Object.assign(cloneJ(f), { epoch: ep });
const NET_APPLIED = [{ section: 'network', saved: true, applied: true, effect: 'reload', error: null }];
const EPOCH_FRAMES = (() => {
  const sc = scenario('settings_save_applied');
  // the new hello and the topics' first frames, as the shell replays them after a new epoch
  return sc.steps.filter(s => s.deliver && (s.deliver.t === 'hello' || s.deliver.t === 'evt')).map(s => inEpoch(s.deliver, E2));
})();
function epochScenario(name, first, newPending) {
  const sc = scenario('settings_save_applied');
  const again = cmd => { const h = cloneJ(handlerOf(sc, cmd)); h.reply = h.reply.map(f => inEpoch(f, E2)); return h; };
  const snap2 = snapshotAnswer(sc, 1, v => { v.network.allow_lan = true; }); snap2.reply = snap2.reply.map(f => inEpoch(f, E2));
  snap2.reply[0].data.pending = newPending;
  const second = applyAnswer(sc, 0, { rev: 2, sections: NET_APPLIED });
  second.reply = second.reply.map(f => { const g = inEpoch(f, E2); if (g.t === 'ack') g.op = 'op-save-2'; if (g.t === 'evt' && g.topic === 'jobs') g.data.op = 'op-save-2'; return g; });
  return withHandlers(sc, name, ['settings.apply'], [
    applyAnswer(sc, 0, Object.assign({ rev: 8 }, first)), second,
    snap2, again('autostart.state'), again('app.info'), again('geo.status'),
  ]);
}
const EPOCH_RESET = epochScenario('settings_epoch_reset', { sections: NET_APPLIED }, null);
/* rows a save left pending (intent-1 at rev 8), then a new generation whose snapshot carries an
   intent of its own (a gateway.select there, config_rev 1): the new process loaded the saved
   files, so the rows read 已生效 (service running), not 待生效 forever */
const EPOCH_PENDING = epochScenario('settings_epoch_pending', {
  sections: [{ section: 'network', saved: true, applied: false, effect: 'deferred', error: null }],
  pending: { intent_id: 'intent-1', config_rev: 8, generation: 4, state: 'pending', effect: 'restart', error: null },
}, { intent_id: 'intent-9', config_rev: 1, generation: 1, state: 'pending', effect: 'restart', error: null });
/* the same without a new epoch: the read after a conflict is the base whatever its rev (the
   sidecar just said the page's rev is not its own) */
const CONFLICT_LOWER = (() => {
  const sc = scenario('settings_save_conflict');
  const out = cloneJ(sc); out.name = 'settings_conflict_lower_rev';
  out.steps.filter(s => s.on_invoke && s.on_invoke.cmd === 'config.snapshot')[1].reply[0].data.rev = 1;
  out.steps.push(applyAnswer(scenario('settings_save_applied'), 1, { rev: 2, sections: NET_APPLIED }));
  return out;
})();

/* ---- T-P4-H4 (05d 遗留小修块) ---- */
const invokedArgs = (page, cmd) => page.evaluate(c => window.__TESSERACT_DESIGN__.transport().invocations.filter(x => x.kind === 'rt' && x.cmd === c).map(x => x.args), cmd);
// a dialog's answer, only when the dialog is there (a missing dialog fails the cell's checks, not the run)
const answer = a => [{ eval: x => { const b = document.querySelector(`.ovl .dlg [data-a="${x}"]`); if (b) b.click(); }, arg: a }, { page_ms: 400 }];
const answerYes = answer('yes'), answerNo = answer('no');
// a click on an element only when it is there (same reason)
const tap = sel => [{ eval: x => { const e = document.querySelector(x); if (e) e.click(); }, arg: sel }, { page_ms: 400 }];
// the bar and a row while the third result (重试应用) is up, kept for expect()
const keepMid = row => ({ eval: r => { const b = document.querySelector('.savebar'); window.__mid = { cls: b ? b.className : '', save: (b && b.querySelector('.sb-save .lab .sw:not([aria-hidden])') || {}).textContent || '', tag: (document.querySelector(`#${r} .set-res-tag`) || {}).className || '' }; }, arg: row });
// a settings.apply answer saved but not applied, the job's error the row's (bridge_settings
// _run_actions: errors.settings.apply_failed + reason_code)
function notAppliedAnswer(sc, sections) {
  const h = cloneJ(handlerOf(sc, 'settings.apply'));
  const end = h.reply.find(f => f.t === 'evt' && f.topic === 'jobs' && f.data.state === 'failed');
  end.data.result = { rev: 8, sections: cloneJ(sections), pending: null };
  end.data.error = cloneJ(sections.find(x => x.error).error);
  return h;
}
const APPLY_ERR = reason => ({ code: 'io', message_key: 'errors.settings.apply_failed', params: {}, reason_code: reason });
// ② the language saved but not switched (language_sync_failed): the bar says what the row says
const LANG_NOT_SWITCHED = (() => {
  const sc = scenario('settings_save_not_applied');
  return withHandlers(sc, 'settings_save_lang_not_switched', ['settings.apply'], [notAppliedAnswer(sc, [{ section: 'ui', saved: true, applied: false, effect: 'none', error: APPLY_ERR('language_sync_failed') }])]);
})();
// ② a section whose delivery records stayed (delivery_records_delete_failed): the bar says so and
// promises no restart. (The page's save is a switch that needs no Core, as in NO_RETRY; the answer's
// reason is what is under test, the alert drawer's own delete flow is alerts.mjs's)
const RECORDS_NOT_DELETED = (() => {
  const sc = scenario('settings_save_not_applied');
  return withHandlers(sc, 'settings_save_records_not_deleted', ['settings.apply'], [notAppliedAnswer(sc, [{ section: 'network', saved: true, applied: false, effect: 'none', error: APPLY_ERR('delivery_records_delete_failed') }])]);
})();
/* 修复第 1 轮: the ui section saved but its preferences not applied (preferences_apply_failed): the
   language was sent with it, yet reopening the window is not the remedy; the bar gives the reason */
const UI_PREFS_NOT_APPLIED = (() => {
  const sc = scenario('settings_save_not_applied');
  return withHandlers(sc, 'settings_save_ui_prefs_not_applied', ['settings.apply'], [notAppliedAnswer(sc, [{ section: 'ui', saved: true, applied: false, effect: 'none', error: APPLY_ERR('preferences_apply_failed') }])]);
})();
// 修复第 1 轮: an error with a sentence of its own (it ends with a full stop) put before the bar's
const OWN_SENTENCE = (() => {
  const sc = scenario('settings_save_not_applied');
  return withHandlers(sc, 'settings_save_reason_own_sentence', ['settings.apply'], [notAppliedAnswer(sc, [{ section: 'network', saved: true, applied: false, effect: 'none', error: { code: 'timeout', message_key: 'errors.timeout', params: {} } }])]);
})();
/* 配置整理 P2: the one list (routing.rule_list) and the rule sets (routing.providers) as the sidecar
   projects them: a direct suffix and, after it, a proxy host it covers; a combination; a rule set; a
   legacy rule the old compile dropped (off); an IP rule that also resolves host names. Two rule sets,
   one address withheld (credentials or a query: url_hidden) */
const RULE_LIST = (() => {
  const sc = scenario('settings_save_applied');
  return withHandlers(sc, 'settings_rule_list', ['config.snapshot'], [snapshotAnswer(sc, handlerOf(sc, 'config.snapshot').reply[0].data.rev, v => {
    v.routing.rule_list = [
      { to: 'direct', kind: 'suffix', value: 'example.cn' },
      { to: 'proxy', kind: 'domain', value: 'www.example.cn' },
      { to: 'reject', kind: 'and', of: [{ kind: 'dst-port', value: '443' }, { kind: 'network', value: 'udp' }] },
      { to: 'direct', kind: 'rule-set', value: 'cn-sites' },
      { to: 'direct', kind: 'dst-port', value: '22', off: true, note: 'dropped_policy' },
      { to: 'direct', kind: 'geoip', value: 'JP', resolve: true },
    ];
    v.routing.providers = [{ name: 'cn-sites', type: 'http', behavior: 'domain', format: 'yaml', url: 'https://rules.example.net/cn.yaml' },
      { name: 'private', type: 'http', behavior: 'classical', url_hidden: true }];
  })]);
})();
/* (in the page) the shown words of a field's message, its current swap layer, kept as window[key] */
const keepMsg = (field, key) => ({ eval: ([f, k]) => {
  const m = document.querySelector(`${f} .field-msg`), l = m && Array.from(m.querySelectorAll('.sw:not([aria-hidden])')).pop();
  window[k] = { msg: ((l || m || {}).textContent || '').trim(), invalid: document.querySelector(f).classList.contains('is-invalid') };
}, arg: [field, key] });
// a row's reason (its result tag's tip)
const rowTip = (page, row) => page.evaluate(r => { const e = document.querySelector(`#${r} .set-res-tag`); return e ? e.getAttribute('data-tip') : null; }, row);
/* ⑤ (05d 续2 SHOULD 1): rows a save left pending (intent-1), then a new generation; its snapshot is
   read before its service state, and a second save (deferred, intent-9) lands in that window. When
   the service state comes, only the old batch settles (已生效); the new one follows intent-9 */
const GEN_SECOND_BATCH = (() => {
  const sc = scenario('settings_save_applied');
  const again = cmd => { const h = cloneJ(handlerOf(sc, cmd)); h.reply = h.reply.map(f => inEpoch(f, E2)); return h; };
  const snap2 = snapshotAnswer(sc, 1, () => {}); snap2.reply = snap2.reply.map(f => inEpoch(f, E2));
  snap2.reply[0].data.pending = null;
  const second = applyAnswer(sc, 0, { rev: 2, sections: [{ section: 'network', saved: true, applied: false, effect: 'deferred', error: null }],
    pending: { intent_id: 'intent-9', config_rev: 2, generation: 1, state: 'pending', effect: 'restart', error: null } });
  second.reply = second.reply.map(f => { const g = inEpoch(f, E2); if (g.t === 'ack') g.op = 'op-save-2'; if (g.t === 'evt' && g.topic === 'jobs') g.data.op = 'op-save-2'; return g; });
  return withHandlers(sc, 'settings_gen_second_batch', ['settings.apply'], [
    applyAnswer(sc, 0, { rev: 8, sections: [{ section: 'network', saved: true, applied: false, effect: 'deferred', error: null }],
      pending: { intent_id: 'intent-1', config_rev: 8, generation: 4, state: 'pending', effect: 'restart', error: null } }),
    second, snap2, again('autostart.state'), again('app.info'), again('geo.status'),
  ]);
})();
// the new generation's frames without its service state (the case delivers it afterwards)
const E2_NO_STATE = EPOCH_FRAMES.filter(f => !(f.t === 'evt' && f.topic === 'service.state'));
const E2_STATE = EPOCH_FRAMES.filter(f => f.t === 'evt' && f.topic === 'service.state');
/* ⑥ (05d R4-3): rows left pending by intent-1 (config_rev 8); the page is left and entered again;
   the visit's snapshot carries an intent the page never saw (intent-7, config_rev 9: a gateway.select
   made elsewhere) that failed. Followed by intent id only, it does not relabel the rows */
const BLANK_PAGE = [{ eval: () => { if (!window.__h4blank) { window.__h4blank = true; T.page('h4_blank', { titleId: 'shell.page.overview.title' }); } T.go('h4_blank', { instant: true }); } }, { page_ms: 600 }];
const REVISIT = (() => {
  const sc = scenario('settings_save_pending');
  const snap2 = snapshotAnswer(sc, 9, () => {});
  snap2.reply[0].data.pending = { intent_id: 'intent-7', config_rev: 9, generation: 4, state: 'failed', effect: 'restart', error: { code: 'io', message_key: 'errors.settings.apply_failed', params: {} } };
  return withHandlers(sc, 'settings_revisit_foreign_intent', [], [snap2, ...['autostart.state', 'app.info', 'geo.status', 'alerts.catalog'].map(c => cloneJ(handlerOf(sc, c)))]);
})();
/* ⑦ (05d §9-3): Geo 已禁用 learnt from a refused geo.update is forgotten on a configuration change
   made elsewhere (config.changed rev > base) and on a new generation */
const GEO_EXTERNAL = (() => {
  const sc = scenario('settings_geo_disabled');
  return withHandlers(sc, 'settings_geo_disabled_external', [], [snapshotAnswer(sc, 8, () => {})]);
})();
const GEO_EPOCH = (() => {
  const sc = scenario('settings_geo_disabled');
  const again = cmd => { const h = cloneJ(handlerOf(sc, cmd)); h.reply = h.reply.map(f => inEpoch(f, E2)); return h; };
  const snap2 = snapshotAnswer(sc, 1, () => {}); snap2.reply = snap2.reply.map(f => inEpoch(f, E2));
  return withHandlers(sc, 'settings_geo_disabled_epoch', [], [snap2, again('autostart.state'), again('app.info'), again('geo.status')]);
})();
const geoState = () => ({ disabled: document.querySelector('#set-geo-btn').disabled, err: document.querySelectorAll('#set-geo-err:not([hidden]) .set-geo-e').length });
const keepGeo = { eval: () => { window.__geo = { disabled: document.querySelector('#set-geo-btn').disabled, err: document.querySelectorAll('#set-geo-err:not([hidden]) .set-geo-e').length }; } };

const CASES = [
  // ---- the page -------------------------------------------------------------------------
  {
    name: 'settings_page_running', scenario: 'settings_page_running',
    expect: run(async page => {
      const s = await state(page);
      const [unnamed, hint] = await words(page, [['settings.bastion.src.unnamed', { index: 2 }]]).then(w => [w[0], 'e•••/f•••']);
      return check([
        [SECTIONS.every(id => s.sections.includes(id)), `sections: ${s.sections}`],
        [!s.status, `status line: ${s.status}`],
        [!s.unknown.length, `read-only rows: ${s.unknown}`],
        [s.live['set-ver-core'] === '1.19.27', `core version: ${s.live['set-ver-core']}`],
        [/100/.test(s.live['set-bastionMin-row']), `bastion minimum hint without the capacity: ${s.live['set-bastionMin-row']}`],
        [s.sources[0] === hint && s.sources[1] === unnamed, `sources: ${s.sources}`],
        [JSON.stringify(s.rules) === '["example.cn","CN","example.io","ads"]', `the rule list in match order: ${JSON.stringify(s.rules)}`],
        [!s.inert.ks && !s.inert.lan && !s.inert.tray && !s.inert.bastionOn, `editable rows inert: ${JSON.stringify(s.inert)}`],
        [!s.bar && s.dirtyCount === 0, 'a save bar without drafts'],
      ]);
    }),
  },
  {
    name: 'settings_page_stopped', scenario: 'settings_page_stopped',
    expect: run(async page => {
      const s = await state(page);
      const [unk, modeSt] = await words(page, [['settings.bastion.live.unknown'], ['settings.conn.mode.st.stopped', { mode: await page.evaluate(() => window.T.t(`mode.${window.T.store.get('mode')}.label`)) }]]);
      // F-01: without a pool the minimum's row has no line (it said 「上限要等服务运行后才知道」), the kill
      // switch shows no state word while the service is stopped (it said 「服务已停止 · 启动后布防」), and the
      // mode line names the mode only
      const r = await page.evaluate(() => {
        const d = document.querySelector('#set-bastionMin-row .frow-d'), ks = document.querySelector('#set-ks-live');
        return { minBlank: !!d && d.classList.contains('is-blank') && !d.checkVisibility() && !d.textContent.trim(), ksHidden: !!ks && ks.hidden && !ks.checkVisibility() };
      });
      return check([[s.live['set-bastion-live'] === unk, `bastion live: ${s.live['set-bastion-live']}`],
        [r.minBlank, `bastion minimum: no line without a pool: ${s.live['set-bastionMin-row']}`], [r.ksHidden, `ks live hidden while stopped: ${s.live['set-ks-live']}`],
        [s.live['set-mode-st'] === modeSt, `mode status: ${s.live['set-mode-st']} (want ${modeSt})`]]);
    }),
  },
  {
    name: 'settings_page_starting', scenario: 'settings_page_starting',
    expect: run(async page => { const s = await state(page); return check([[/\bsdot busy\b|busy/.test(await page.evaluate(() => document.querySelector('#set-mode-st').innerHTML)), `mode status while starting: ${s.live['set-mode-st']}`]]); }),
  },
  // ---- the save model ------------------------------------------------------------------------
  {
    name: 'settings_save_not_applied', scenario: 'settings_save_not_applied', actions: [...click('.switch[data-k="lan"]'), ...save],
    expect: run(async page => {
      const s = await state(page);
      const [na, retry] = await words(page, [['core.savebar.not_applied'], ['settings.save.retry_apply']]);
      return check([[s.bar && /is-partial/.test(s.bar.cls), `bar: ${JSON.stringify(s.bar)}`], [s.bar && s.bar.count === na, 'bar words'],
        [s.bar && s.bar.save === retry && s.bar.closeShown, 'retry / close'], [s.tags['set-lan-row'] === 'is-not_applied', `row tags: ${JSON.stringify(s.tags)}`],
        [s.dirtyCount === 0, 'nothing dirty in the third result']]);
    }),
  },
  {
    name: 'settings_save_pending', scenario: 'settings_save_pending',
    actions: [...click('.switch[data-k="lan"]'), ...save, { eval: () => { window.__mid = { cls: document.querySelector('.savebar').className, tag: (document.querySelector('#set-lan-row .set-res-tag') || {}).className }; } },
      { deliver: { t: 'evt', epoch: 'rt-fixture-e1', produced_at_ms: 1790000000500, sent_at_ms: 1790000000501, topic: 'config.changed', rev: 2, kind: 'edge', seq: 2,
        data: { rev: 8, files: ['config'], sections: [{ section: 'network', saved: true, applied: true, effect: 'restart', error: null }], external_detection: 'bridge_writes_only',
          pending: { intent_id: 'intent-1', config_rev: 8, generation: 4, state: 'applied', effect: 'restart', error: null } } } }, { page_ms: 300 }],
    expect: run(async page => {
      const mid = await page.evaluate(() => window.__mid);
      const s = await state(page);
      return check([[/is-saved/.test(mid.cls), `bar after the save: ${mid.cls}`], [/is-pending/.test(mid.tag || ''), `row while pending: ${mid.tag}`],
        [s.tags['set-lan-row'] === 'is-applied', `row after pending applied: ${JSON.stringify(s.tags)}`]]);
    }),
  },
  {
    name: 'settings_save_pending_failed', scenario: 'settings_save_pending_failed',
    actions: [...click('.switch[data-k="lan"]'), ...save,
      { deliver: { t: 'evt', epoch: 'rt-fixture-e1', produced_at_ms: 1790000000500, sent_at_ms: 1790000000501, topic: 'config.changed', rev: 1, kind: 'edge', seq: 1,
        data: { rev: 8, files: ['config'], sections: [{ section: 'network', saved: true, applied: false, effect: 'deferred', error: null }], external_detection: 'bridge_writes_only',
          pending: { intent_id: 'intent-1', config_rev: 8, generation: 4, state: 'failed', effect: 'restart', error: { code: 'io', message_key: 'errors.settings.apply_failed', params: {} } } } } }, { page_ms: 300 }],
    expect: run(async page => { const s = await state(page); return check([[s.tags['set-lan-row'] === 'is-not_applied', `row after the intent failed: ${JSON.stringify(s.tags)}`]]); }),
  },
  {
    name: 'settings_save_stopped', scenario: 'settings_save_stopped', actions: [...click('.switch[data-k="lan"]'), ...save],
    expect: run(async page => {
      const s = await state(page);
      const [next] = await words(page, [['settings.save.done.next_start_short']]);
      return check([[s.bar && /is-saved/.test(s.bar.cls) && s.bar.eff === next, `bar: ${JSON.stringify(s.bar)}`], [s.tags['set-lan-row'] === 'is-next_start', `rows: ${JSON.stringify(s.tags)}`]]);
    }),
  },
  {
    // (fix round 1, SHOULD) the third result stays past the page's delayed re-track while only the
    // unsaved section's draft remains, and its sentence counts that draft
    name: 'settings_save_partial', scenario: 'settings_save_partial', actions: [...pickLang(1), ...click('.switch[data-k="lan"]'), ...save, { page_ms: 1600 }],
    expect: run(async page => {
      const s = await state(page);
      const [left] = await words(page, [['settings.save.left_unsaved', { reason: '', count: 1 }]]);
      return check([[s.bar && /is-partial/.test(s.bar.cls), `bar: ${JSON.stringify(s.bar)}`], [s.bar && s.bar.eff.replace(/[⟦⟧]/g, '').includes(left.replace(/[⟦⟧]/g, '').trim()), `the bar counts the unsaved draft: ${s.bar && s.bar.eff} / ${left}`],
        [s.tags['set-lang-row'] === 'is-applied' && s.tags['set-lan-row'] === 'is-failed', `each row its own result: ${JSON.stringify(s.tags)}`]]);
    }),
  },
  {
    name: 'settings_save_applied', scenario: 'settings_save_applied', actions: [...pickLang(1), ...click('.switch[data-k="lan"]'), ...save],
    expect: run(async page => {
      const s = await state(page);
      // the payload: the snapshot's rev as base_rev, one change per section, only the changed fields
      const sent = await applied(page);
      const by = sec => sent[0] && sent[0].changes.find(c => c.section === sec);
      return check([[s.bar && /is-saved/.test(s.bar.cls), `bar: ${JSON.stringify(s.bar)}`], [s.tags['set-lan-row'] === 'is-applied', `rows: ${JSON.stringify(s.tags)}`],
        [sent.length === 1 && sent[0].base_rev === 7 && sent[0].changes.length === 2, `request: ${JSON.stringify(sent)}`],
        [by('network') && JSON.stringify(by('network').values) === '{"allow_lan":true}' && !by('network').secrets, `network change: ${JSON.stringify(by('network'))}`],
        [by('ui') && Object.keys(by('ui').values).join() === 'language' && typeof by('ui').values.language === 'string' && by('ui').values.language !== 'zh_CN', `ui change: ${JSON.stringify(by('ui'))}`]]);
    }),
  },
  {
    name: 'settings_save_hold_field', scenario: 'settings_save_hold_field', actions: [{ fill: ['#set-port-mixed', '7891'] }, { page_ms: 400 }, ...save, { page_ms: 1200 }],
    expect: run(async page => {
      const s = await state(page);
      const row = await page.evaluate(() => ({ invalid: document.querySelector('#set-portMixed-row').classList.contains('is-invalid'), msg: document.querySelector('#set-portMixed-row .frow-d').textContent }));
      const [held] = await words(page, [['core.savebar.held']]);
      return check([[s.bar && /is-held/.test(s.bar.cls) && s.bar.count === held, `bar: ${JSON.stringify(s.bar)}`], [row.invalid, `the port row: ${JSON.stringify(row)}`], [s.dirtyCount > 0, 'the draft stays']]);
    }),
  },
  {
    name: 'settings_save_conflict', scenario: 'settings_save_conflict', actions: [...click('.switch[data-k="lan"]'), ...save, { page_ms: 400 }],
    expect: run(async page => {
      const s = await state(page);
      const [title] = await words(page, [['settings.save.conflict']]);
      const lat = await page.evaluate(() => document.querySelector('#set-lat-v').textContent);
      return check([[s.bar && s.bar.count === title, `bar: ${JSON.stringify(s.bar)}`], [/1200/.test(lat), `a row without a draft follows the snapshot: ${lat}`], [s.dirtyCount > 0, 'the draft stays']]);
    }),
  },
  {
    name: 'settings_save_lifecycle_busy', scenario: 'settings_save_lifecycle_busy', actions: [...click('.switch[data-k="lan"]'), ...save],
    expect: run(async page => {
      const s = await state(page);
      const [msg] = await words(page, [['settings.save.hold_starting']]);
      return check([[s.bar && /is-held/.test(s.bar.cls) && s.bar.eff === msg, `bar: ${JSON.stringify(s.bar)}`]]);
    }),
  },
  {
    name: 'settings_save_port_conflict', scenario: 'settings_save_port_conflict', actions: [{ fill: ['#set-port-mixed', '7891'] }, { page_ms: 400 }, ...save],
    expect: run(async page => { const s = await state(page); return check([[s.bar && /is-partial/.test(s.bar.cls) && !!s.bar.eff, `bar says why: ${JSON.stringify(s.bar)}`], [s.tags['set-portMixed-row'] === 'is-not_applied', `rows: ${JSON.stringify(s.tags)}`]]); }),
  },
  {
    name: 'settings_save_recheck', scenario: 'settings_save_recheck',
    actions: [{ eval: () => document.querySelector('#set-lat-sl').focus() }, { press: 'ArrowRight' }, { page_ms: 400 }, ...save],
    expect: run(async page => {
      const s = await state(page);
      const [done, rc] = await words(page, [['settings.save.done.recheck'], ['settings.quality.rechecking']]);
      return check([[s.bar && s.bar.eff === done, `bar: ${JSON.stringify(s.bar)}`], [(s.live['set-lat-sum'] || '').includes(rc), `latency summary: ${s.live['set-lat-sum']}`]]);
    }),
  },
  {
    name: 'settings_sources_hold', scenario: 'settings_sources_hold',
    actions: [{ fill: ['#set-src-in', 'https://share.example.net/a.txt'] }, { press: 'Enter' }, { page_ms: 400 }, ...save, { page_ms: 1200 }],
    expect: run(async page => {
      const s = await state(page);
      // a new source: its value only as a SecretEdit whose target_id is the row's id
      const src = ((await applied(page))[0] || { changes: [] }).changes.find(c => c.section === 'sources');
      const fresh = src ? src.values.items.filter(i => !/^src-/.test(i.id)) : [];
      return check([[s.bar && /is-held/.test(s.bar.cls), `bar: ${JSON.stringify(s.bar)}`], [s.sources.length === 3, `sources: ${s.sources}`],
        [src && fresh.length === 1 && src.secrets.length === 1 && src.secrets[0].target_id === fresh[0].id && src.secrets[0].action === 'set' && src.secrets[0].field === 'value', `sources change: ${JSON.stringify(src)}`],
        [src && !JSON.stringify(src.values).includes('share.example.net'), 'the value only in secrets']]);
    }),
  },
  // ---- the language row (architecture §3.7) --------------------------------------------------
  {
    name: 'settings_lang_saved', scenario: 'settings_lang_saved', actions: [...pickLang(1), ...save, { page_ms: 3200 }],
    expect: run(async page => {
      const s = await state(page);
      const [reopen] = await words(page, [['settings.app.lang.reopen']]);
      const [lang, l0] = await page.evaluate(() => [window.T.i18n.lang, window.__l0]);
      return check([[s.live['set-lang-d'] === reopen, `language row: ${s.live['set-lang-d']}`], [lang === l0, 'the page does not switch by itself']]);
    }),
  },
  {
    name: 'settings_lang_failed', scenario: 'settings_lang_failed', actions: [...pickLang(1), ...save],
    expect: run(async page => {
      const s = await state(page);
      const sel = await page.evaluate(() => document.querySelector('#set-lang .select-v').textContent);
      const [zh] = await words(page, [['settings.app.lang.name.zh_cn']]);
      return check([[s.bar && /is-failed/.test(s.bar.cls), `bar: ${JSON.stringify(s.bar)}`], [sel.includes(zh), `the choice rolled back: ${sel}`],
        [await page.evaluate(() => document.querySelector('#set-lang-row').querySelector('.frow-d .ic') !== null), 'the row says why']]);
    }),
  },
  // ---- status line, read-only rows, hidden lists ----------------------------------------------
  {
    name: 'settings_read_errors', scenario: 'settings_read_errors', actions: [...click('#set-status [data-act="open-config"]')],
    expect: run(async page => {
      const s = await state(page);
      const li = await page.evaluate(() => document.querySelectorAll('#set-status .set-st-errs li').length);
      const path = await page.evaluate(() => /[A-Za-z]:\\|\/Users\//.test(document.querySelector('#set-status').textContent));
      return check([[s.statusKinds.some(c => /callout-error/.test(c)), 'the error bar'], [li === 2, `errors listed: ${li}`], [!path, 'no local path (G48)']]);
    }),
  },
  {
    name: 'settings_snapshot_failed', scenario: 'settings_snapshot_failed',
    expect: run(async page => {
      const s = await state(page);
      return check([[s.statusKinds.some(c => /callout-error/.test(c)), `the error bar: ${s.status}`], [s.inert.lan && s.inert.ks, `contract rows read-only: ${JSON.stringify(s.inert)}`],
        [await page.evaluate(() => !!document.querySelector('#set-status [data-act="retry"]')), 'retry']]);
    }),
  },
  {
    name: 'settings_frozen_sidecar', scenario: 'settings_frozen_sidecar',
    expect: run(async page => {
      const s = await state(page);
      return check([[s.unknown.includes('set-ksLan-row') && s.unknown.includes('set-bastionOn-row') && s.unknown.includes('set-egress-row'), `read-only rows: ${s.unknown}`],
        [s.inert.ksLan && s.inert.bastionOn && !s.inert.lan, `inert: ${JSON.stringify(s.inert)}`]]);
    }),
  },
  {
    name: 'settings_hidden_list', scenario: 'settings_hidden_list',
    actions: [{ fill: ['#set-tok-ksextra input', '198.51.100.7'] }, { press: 'Enter' }, { page_ms: 400 }],
    expect: run(async page => {
      const s = await state(page);
      const [title] = await words(page, [['settings.hidden.confirm.title']]);
      const dlg = await page.evaluate(() => (document.querySelector('.ovl .dlg .dlg-title') || {}).textContent || '');
      return check([[s.hiddenNotes.includes('set-ksExtra-hidden'), `hidden notes: ${s.hiddenNotes}`], [dlg.includes(title), `asks before replacing the list: ${dlg}`]]);
    }),
  },
  // ---- connection -------------------------------------------------------------------------------
  {
    name: 'settings_mode_cleanup', scenario: 'settings_mode_cleanup', actions: [{ click: '.switch[data-mode="tun"]' }, { page_ms: 400 }, ...confirmYes, { advance_ms: 50 }, { page_ms: 300 }],
    expect: run(async page => { const s = await state(page); return check([[/is-err/.test(s.modeSt), `mode status: ${s.modeSt} ${s.live['set-mode-st']}`]]); }),
  },
  {
    name: 'settings_mode_admin_required', scenario: 'settings_mode_admin_required', actions: [{ click: '.switch[data-mode="system"]' }, { advance_ms: 50 }, { page_ms: 400 }],
    expect: run(async page => { const s = await state(page); return check([[/is-err/.test(s.modeSt), `mode status: ${s.modeSt} ${s.live['set-mode-st']}`]]); }),
  },
  {
    name: 'settings_mode_no_admin', scenario: 'settings_mode_no_admin',
    expect: run(async page => {
      const [gate] = await words(page, [['settings.conn.mode.gate']]);
      // (接管方式 switches, mode system: both locked, since 系统代理 can only lead to tun or mixed and
      // 虚拟网卡 is off; the line under 虚拟网卡 says why)
      const r = await page.evaluate(() => ({ sw: ['system', 'tun'].map(k => document.querySelector(`.switch[data-mode="${k}"]`).getAttribute('aria-disabled')),
        on: ['system', 'tun'].map(k => document.querySelector(`.switch[data-mode="${k}"]`).getAttribute('aria-checked')),
        gate: (n => (n && !n.hidden ? n.textContent : ''))(document.querySelector('#set-gate-tun')) }));
      const btn = await page.evaluate(() => [...document.querySelectorAll('#set-conn button')].some(b => /admin/i.test(b.id)));
      return check([[r.sw.every(x => x === 'true') && r.on.join() === 'true,false' && r.gate.includes(gate), `locked switches: ${JSON.stringify(r)}`], [!btn, 'no reopen-as-administrator button (D7)']]);
    }),
  },
  {
    name: 'settings_ks_error', scenario: 'settings_ks_error',
    expect: run(async page => { const s = await state(page); const [w] = await words(page, [['settings.conn.ks.live.error']]); return check([[s.live['set-ks-live'] === w, `ks live: ${s.live['set-ks-live']}`]]); }),
  },
  {
    name: 'settings_ks_triggered', scenario: 'settings_ks_triggered',
    expect: run(async page => { const s = await state(page); const [w] = await words(page, [['settings.conn.ks.live.triggered']]); return check([[s.live['set-ks-live'] === w, `ks live: ${s.live['set-ks-live']}`]]); }),
  },
  {
    name: 'settings_drift', scenario: 'settings_drift',
    expect: run(async page => { const s = await state(page); const [w] = await words(page, [['settings.conn.mode.st.drift']]); return check([[s.live['set-mode-st'] === w && /is-gated/.test(s.modeSt), `mode status: ${s.live['set-mode-st']}`]]); }),
  },
  // ---- Bastion -------------------------------------------------------------------------------------
  {
    name: 'settings_bastion_capacity', scenario: 'settings_bastion_capacity', actions: [{ fill: ['#set-bastion-min', '50'] }, { page_ms: 200 }, { eval: () => document.querySelector('#set-bastion-min').blur() }, { page_ms: 400 }],
    expect: run(async page => {
      const r = await page.evaluate(() => ({ invalid: document.querySelector('#set-bastionMin-row').classList.contains('is-invalid'), msg: document.querySelector('#set-bastionMin-row .frow-d').textContent }));
      return check([[r.invalid && /40/.test(r.msg) && !/60/.test(r.msg), `the upper bound is the capacity: ${JSON.stringify(r)}`]]);
    }),
  },
  // ---- application area -------------------------------------------------------------------------
  ...[['unsupported', 'settings.app.autostart.unsupported'], ['error', 'settings.app.autostart.read_failed'], ['mismatch', 'settings.app.autostart.mismatch', { target: 'legacy_qt' }], ['off', 'settings.app.autostart.off']].map(([k, id, p]) => ({
    name: `settings_autostart_${k}`, scenario: `settings_autostart_${k}`,
    expect: run(async page => {
      const s = await state(page);
      const [w] = await words(page, [[id, p]]);
      const pairs = [[s.live['set-auto-live'] === w, `autostart live: ${s.live['set-auto-live']}`]];
      if (k === 'unsupported') pairs.push([s.inert.autostart, 'the switch is read-only']);
      // (F-04: the disabled D8 row has no line saying why)
      if (k === 'unsupported' || k === 'off') pairs.push([s.trayOff && s.inert.tray && !s.live['set-tray-d'], `D8 row disabled, no reason line: ${s.live['set-tray-d']}`]);
      return check(pairs);
    }),
  })),
  {
    name: 'settings_core_unknown', scenario: 'settings_core_unknown',
    expect: run(async page => { const s = await state(page); const [w] = await words(page, [['settings.app.version.core_unknown']]); return check([[s.live['set-ver-core'] === w, `core version: ${s.live['set-ver-core']}`]]); }),
  },
  {
    name: 'settings_update_available', scenario: 'settings_update_available', actions: [...click('#set-upd-notes'), { page_ms: 400 }],
    expect: run(async page => {
      const r = await page.evaluate(() => {
        const n = document.querySelector('.ovl .set-notes-text');
        return { text: n ? n.textContent : null, kids: n ? n.children.length : -1, install: !document.querySelector('#set-upd-install').hidden };
      });
      return check([[r.text && r.text.includes('<img src=x') && r.kids === 0, `release notes as plain text: ${JSON.stringify(r)}`], [r.install, '立即更新 shown']]);
    }),
  },
  {
    name: 'settings_update_not_installable', scenario: 'settings_update_not_installable',
    expect: run(async page => {
      const r = await page.evaluate(() => ({ page: !document.querySelector('#set-upd-page').hidden, install: !document.querySelector('#set-upd-install').hidden }));
      return check([[r.page && !r.install, `release page instead of install: ${JSON.stringify(r)}`]]);
    }),
  },
  ...[['downloading', () => !document.querySelector('#set-upd-prog').hidden && document.querySelector('#set-upd-check').hidden],
    ['handoff', () => document.querySelector('#set-upd-check').hidden],
    ['failed', () => document.querySelector('#set-upd-st').classList.contains('is-err')],
    ['disabled', () => document.querySelector('#set-upd-check').disabled],
    ['updated', () => /1\.0\.0/.test(document.querySelector('#set-upd-st').textContent)]].map(([k, fn]) => ({
    name: `settings_update_${k}`, scenario: `settings_update_${k}`,
    expect: run(async page => check([[await page.evaluate(fn), `update ${k}: ${await page.evaluate(() => document.querySelector('#set-upd-st').textContent)}`]])),
  })),
  {
    name: 'settings_geo_partial', scenario: 'settings_geo_partial',
    expect: run(async page => { const s = await state(page); return check([[s.geo.length === 4 && s.geo.filter(c => /is-fail/.test(c)).length === 1, `geo cards: ${s.geo}`], [s.geoErr.length === 1, `geo errors: ${s.geoErr}`]]); }),
  },
  {
    name: 'settings_geo_stale', scenario: 'settings_geo_stale',
    expect: run(async page => { const t = await page.evaluate(() => document.querySelector('#set-geo').textContent); const [w] = await words(page, [['settings.app.geo.stale', { when: '' }]]); return check([[t.includes(w.replace(/^\W+/, '').slice(-6)), `stale: ${t}`]]); }),
  },
  {
    name: 'settings_geo_needs_route', scenario: 'settings_geo_needs_route', actions: [...click('#set-geo-btn'), { advance_ms: 50 }, { page_ms: 300 }],
    expect: run(async page => check([[await page.evaluate(() => document.querySelector('#set-geo-btn').classList.contains('is-fail')), 'the update button failed']])),
  },
  {
    name: 'settings_geo_disabled', scenario: 'settings_geo_disabled', actions: [...click('#set-geo-btn'), { advance_ms: 50 }, { page_ms: 4000 }],
    expect: run(async page => { const s = await state(page); return check([[await page.evaluate(() => document.querySelector('#set-geo-btn').disabled), 'the update button rests'], [s.geoErr.length === 1, `the disabled reason: ${s.geoErr}`]]); }),
  },
  {
    // (11-04: no 数据目录 row; the configuration folder is offered only when a file cannot be read,
    // on the status line, so that is the folder button the host refuses here)
    name: 'settings_dialog_open', scenario: 'settings_dialog_open', actions: [...click('#set-status [data-act="open-config"]'), { page_ms: 200 }],
    expect: run(async page => {
      const r = await page.evaluate(() => {
        const e = document.querySelector('#set-status [data-act="open-config"]');
        return { fail: e.classList.contains('is-fail'), text: e.textContent,
          want: window.T.shellc.errorText({ code: 'conflict', message_key: 'errors.host.conflict', reason_code: 'dialog_open' }) };
      });
      return check([[r.fail && r.text.includes(r.want), `dialog_open: ${JSON.stringify(r)}`]]);
    }),
  },
  // ---- fix round 1 (M1–M5): what is sent, and what the rows say afterwards ------------------------
  {
    // M1: text typed into a list withheld whole, saved with Ctrl S: the question comes before
    // anything is sent; only after 替换整张列表 does the payload carry the list
    name: 'settings_hidden_ctrl_s', scenario: HIDDEN_SAVE,
    actions: [{ fill: ['#set-tok-ksextra input', '198.51.100.7'] }, { page_ms: 400 }, ...pressSave,
      { eval: () => { window.__m1 = { sent: window.__TESSERACT_DESIGN__.transport().invocations.filter(c => c.cmd === 'settings.apply').length, dlg: (document.querySelector('.ovl .dlg .dlg-title') || {}).textContent || '' }; } },
      ...confirmYes, { advance_ms: 50 }, { page_ms: 400 }],
    expect: run(async page => {
      const mid = await page.evaluate(() => window.__m1);
      const [title] = await words(page, [['settings.hidden.confirm.title']]);
      const sent = await applied(page);
      const ks = sent[0] && sent[0].changes.find(c => c.section === 'killswitch');
      return check([[mid.sent === 0, `settings.apply before the question was answered: ${mid.sent}`], [mid.dlg.includes(title), `the question: ${mid.dlg}`],
        [sent.length === 1 && ks && JSON.stringify(ks.values) === JSON.stringify({ additional_whitelist: ['198.51.100.7'] }) && sent[0].base_rev === 7, `payload after the answer: ${JSON.stringify(sent)}`],
        [await page.evaluate(() => document.querySelectorAll('.ovl .dlg').length === 0), 'one question, answered once']]);
    }),
  },
  {
    // M1: declining keeps the list: nothing is sent, the save is held at the list's row
    name: 'settings_hidden_ctrl_s_declined', scenario: HIDDEN_SAVE,
    actions: [{ fill: ['#set-tok-ksextra input', '198.51.100.7'] }, { page_ms: 400 }, ...pressSave, { click: '.ovl .dlg [data-a="no"]' }, { page_ms: 600 }],
    expect: run(async page => {
      const s = await state(page);
      const [held, why] = await words(page, [['core.savebar.held'], ['settings.hidden.hold']]);
      return check([[(await applied(page)).length === 0, 'nothing sent'], [s.bar && s.bar.count === held && s.bar.eff === why, `bar: ${JSON.stringify(s.bar)}`]]);
    }),
  },
  {
    // M2: host:language (store + event, as rt/client.js does) before the job's end: no 重新打开窗口
    name: 'settings_lang_host_first', scenario: LANG_OPEN,
    actions: [{ eval: () => { window.__l0 = window.T.i18n.lang; } }, ...pickLang(1), ...save,
      { eval: () => {
        const T = window.T, call = window.__TESSERACT_DESIGN__.transport().invocations.filter(c => c.cmd === 'settings.apply').pop();
        const lang = call.args.changes[0].values.language;
        T.store.set('lang', lang); T.emit('host:language', { t: 'host:language', language: lang });
        window.__TESSERACT_DESIGN__.deliver({ epoch: 'rt-fixture-e1', produced_at_ms: 1790000000000, sent_at_ms: 1790000000001, t: 'evt', topic: 'jobs', rev: 3, kind: 'edge', seq: 3,
          data: { op: 'op-save-1', req_id: call.req_id, owner: call.owner, name: 'settings.apply', state: 'done', started_at_ms: 1790000000000, progress: null,
            result: { rev: 8, sections: [{ section: 'ui', saved: true, applied: true, effect: 'ui', error: null }], pending: null }, error: null } });
      } },
      { page_ms: 3400 }, { advance_ms: 100 },
      { eval: () => { const d = document.querySelector('#set-lang-d'); window.__m2 = { d: d ? d.textContent : '', reopen: window.T.t('settings.app.lang.reopen') }; } },
      { eval: () => { window.T.store.set('lang', window.__l0); } }, { page_ms: 1200 }],
    expect: run(async page => {
      const r = await page.evaluate(() => window.__m2);
      return check([[!r.d.includes(r.reopen), `the language row after host:language came first: ${r.d}`]]);
    }),
  },
  {
    // M3: add a source, save; add another, save: the second request uses the sidecar's ids
    // (from the snapshot read after the first save), never the page's temporary new-src-* without
    // a value; the saved value is not shown on the row
    name: 'settings_sources_twice', scenario: SOURCES_TWICE,
    actions: [{ fill: ['#set-src-in', 'https://share.example.net/a.txt'] }, { press: 'Enter' }, { page_ms: 400 }, ...save, { page_ms: 600 }, { advance_ms: 3000 }, { page_ms: 400 },
      { eval: () => { window.__m3 = [...document.querySelectorAll('#set-src-list .set-src-r')].map(r => r.textContent + ' ' + [...r.querySelectorAll('[aria-label]')].map(b => b.getAttribute('aria-label')).join(' ')); } },
      { fill: ['#set-src-in', 'https://other.example.org/b.txt'] }, { press: 'Enter' }, { page_ms: 400 }, ...save, { page_ms: 600 }],
    expect: run(async page => {
      const rows = await page.evaluate(() => window.__m3);
      const sent = await applied(page);
      const second = sent[1] && sent[1].changes.find(c => c.section === 'sources');
      const items = second ? second.values.items : [], secrets = (second && second.secrets) || [];
      const orphan = items.filter(i => /^new-src-/.test(i.id) && !secrets.some(x => x.target_id === i.id));
      return check([[sent.length === 2, `two saves: ${sent.length}`], [!rows.some(r => r.includes('share.example.net')), `the saved value shown after the save: ${JSON.stringify(rows)}`],
        [items.some(i => i.id === 'src-sub-REAL') && !orphan.length, `second request's items: ${JSON.stringify(items)} secrets: ${JSON.stringify(secrets.map(x => x.target_id))}`],
        [!JSON.stringify(second ? second.values : {}).includes('example.org'), 'a value only in secrets'],
        [sent[1] && sent[1].base_rev === 8, `base_rev of the second save: ${sent[1] && sent[1].base_rev}`]]);
    }),
  },
  {
    // M4: two saves while the service starts; the second rebases the intent (intent-2): its applied
    // relabels the rows of both saves
    name: 'settings_pending_rebase', scenario: PENDING_REBASE,
    actions: [...click('.switch[data-k="lan"]'), ...save, { advance_ms: 3000 }, { page_ms: 400 }, ...click('.switch[data-k="ipv6"]'), ...save,
      { eval: () => { window.__m4 = Object.fromEntries(['set-lan-row', 'set-ipv6-row'].map(id => [id, (document.querySelector(`#${id} .set-res-tag`) || {}).className || ''])); } },
      { deliver: { t: 'evt', epoch: 'rt-fixture-e1', produced_at_ms: 1790000000500, sent_at_ms: 1790000000501, topic: 'config.changed', rev: 3, kind: 'edge', seq: 3,
        data: { rev: 9, files: [], sections: [], external_detection: 'bridge_writes_only',
          pending: { intent_id: 'intent-2', config_rev: 9, generation: 4, state: 'applied', effect: 'restart', error: null } } } }, { page_ms: 300 }],
    expect: run(async page => {
      const mid = await page.evaluate(() => window.__m4);
      const s = await state(page);
      return check([[/is-pending/.test(mid['set-lan-row']) && /is-pending/.test(mid['set-ipv6-row']), `rows while pending: ${JSON.stringify(mid)}`],
        [s.tags['set-lan-row'] === 'is-applied' && s.tags['set-ipv6-row'] === 'is-applied', `rows after the successor applied: ${JSON.stringify(s.tags)}`]]);
    }),
  },
  {
    // M5: a section saved but not in effect that needs no Core: no 重试应用 (a resubmission would
    // change nothing and read 已生效), only 关闭; Ctrl S does not send anything again
    name: 'settings_save_no_retry', scenario: NO_RETRY, actions: [...click('.switch[data-k="egress"]'), ...save, ...pressSave],
    expect: run(async page => {
      const s = await state(page);
      return check([[s.bar && /is-partial/.test(s.bar.cls) && !s.bar.saveShown && s.bar.closeShown, `bar: ${JSON.stringify(s.bar)}`],
        [s.tags['set-egress-row'] === 'is-not_applied', `rows: ${JSON.stringify(s.tags)}`],
        [(await applied(page)).length === 1 && (await invoked(page, 'service.restart')) === 0, 'nothing sent again'],
        // ② (T-P4-H4): the reason is the system proxy's, and the sentence promises no restart
        [s.bar && s.bar.eff === (await page.evaluate(() => window.T.t('settings.save.not_applied_saved', { reason: window.T.t('settings.save.not_applied_reason.system_proxy') }))), `the bar's sentence: ${s.bar && s.bar.eff}`],
        // (修复第 1 轮) the row's tip says the same reason, not apply_failed's restart promise
        [(await rowTip(page, 'set-egress-row')) === (await page.evaluate(() => window.T.t('settings.save.not_applied_reason.system_proxy'))), `the row's tip: ${await rowTip(page, 'set-egress-row')}`]]);
    }),
  },
  // ---- 重试应用 (T-P4-H4, 05d 遗留 ①): it restarts the proxy service, so every path asserts what is
  // sent and how often: the confirm, service.restart {} once, the rows' and the bar's answer ----
  {
    name: 'settings_save_retry_ok', scenario: 'settings_save_retry_ok',
    actions: [...click('.switch[data-k="lan"]'), ...save, keepMid('set-lan-row'), ...click('.savebar .sb-save'),
      { eval: () => { const d = document.querySelector('.ovl .dlg .dlg-title > span'); window.__dlg = d ? d.textContent : null; } }, ...answerYes, { advance_ms: 50 }, { page_ms: 300 },
      { eval: () => { const b = document.querySelector('.savebar'); window.__end = { cls: b ? b.className : '', eff: (b && b.querySelector('.sb-eff .sw:not([aria-hidden])') || {}).textContent || '' }; } }],
    expect: run(async page => {
      const mid = await page.evaluate(() => window.__mid), end = await page.evaluate(() => window.__end), dlg = await page.evaluate(() => window.__dlg);
      const s = await state(page);
      const [retry, title, done] = await words(page, [['settings.save.retry_apply'], ['settings.save.retry_confirm.title'], ['core.savebar.done.restart']]);
      const restarts = await invokedArgs(page, 'service.restart'), sent = await applied(page);
      return check([[/is-partial/.test(mid.cls) && mid.save === retry && /is-not_applied/.test(mid.tag), `the third result before the retry: ${JSON.stringify(mid)}`],
        [dlg === title, `the retry asks first: ${dlg}`],
        [restarts.length === 1 && JSON.stringify(restarts[0]) === '{}', `service.restart sent: ${JSON.stringify(restarts)}`],
        [sent.length === 1, `settings.apply is not sent again: ${sent.length}`],
        [s.tags['set-lan-row'] === 'is-applied', `the row after the restart: ${JSON.stringify(s.tags)}`],
        [/is-saved/.test(end.cls) && end.eff === done, `the bar after the restart: ${JSON.stringify(end)}`]]);
    }),
  },
  {
    name: 'settings_save_retry_declined', scenario: 'settings_save_retry_ok',
    actions: [...click('.switch[data-k="lan"]'), ...save, ...click('.savebar .sb-save'), ...answerNo, { advance_ms: 50 }, { page_ms: 300 }],
    expect: run(async page => {
      const s = await state(page);
      const [retry] = await words(page, [['settings.save.retry_apply']]);
      return check([[(await invoked(page, 'service.restart')) === 0, 'declined: no service.restart'], [(await applied(page)).length === 1, 'settings.apply sent once'],
        [s.tags['set-lan-row'] === 'is-not_applied', `the row stays not applied: ${JSON.stringify(s.tags)}`],
        [s.bar && /is-partial/.test(s.bar.cls) && s.bar.saveShown && s.bar.save === retry, `重试应用 stays offered: ${JSON.stringify(s.bar)}`]]);
    }),
  },
  {
    // the restart job failed (timeout; its partial result applied=false: the Core did not come up)
    name: 'settings_save_retry_failed', scenario: 'settings_save_retry_failed',
    actions: [...click('.switch[data-k="lan"]'), ...save, ...click('.savebar .sb-save'), ...answerYes, { advance_ms: 50 }, { page_ms: 300 }],
    expect: run(async page => {
      const s = await state(page);
      const [retry, why] = await words(page, [['settings.save.retry_apply'], ['errors.timeout']]);
      const restarts = await invokedArgs(page, 'service.restart');
      return check([[restarts.length === 1 && JSON.stringify(restarts[0]) === '{}', `service.restart sent: ${JSON.stringify(restarts)}`], [(await applied(page)).length === 1, 'settings.apply sent once'],
        [s.tags['set-lan-row'] === 'is-not_applied', `the row stays not applied: ${JSON.stringify(s.tags)}`],
        [s.bar && /is-partial/.test(s.bar.cls) && s.bar.eff === why && s.bar.save === retry, `the bar says why and offers 重试应用 again: ${JSON.stringify(s.bar)}`]]);
    }),
  },
  {
    // the sources' reload failed (effect reload, source_reload_failed): 重试应用 is offered; a restart
    // loads the saved sources
    name: 'settings_save_retry_sources', scenario: 'settings_save_retry_sources',
    actions: [{ fill: ['#set-src-in', 'https://share.example.net/a.txt'] }, { press: 'Enter' }, { page_ms: 400 }, ...save, { page_ms: 400 }, keepMid('set-sources-row'),
      ...click('.savebar .sb-save'), ...answerYes, { advance_ms: 50 }, { page_ms: 300 }],
    expect: run(async page => {
      const mid = await page.evaluate(() => window.__mid);
      const s = await state(page);
      const [retry] = await words(page, [['settings.save.retry_apply']]);
      const restarts = await invokedArgs(page, 'service.restart'), sent = await applied(page);
      const src = sent[0] && sent[0].changes.find(c => c.section === 'sources');
      return check([[/is-partial/.test(mid.cls) && mid.save === retry && /is-not_applied/.test(mid.tag), `重试应用 offered for the sources: ${JSON.stringify(mid)}`],
        [sent.length === 1 && !!src && sent[0].changes.length === 1, `settings.apply (the sources only): ${JSON.stringify(sent)}`],
        [restarts.length === 1 && JSON.stringify(restarts[0]) === '{}', `service.restart sent: ${JSON.stringify(restarts)}`],
        [s.tags['set-sources-row'] === 'is-applied', `the sources row after the restart: ${JSON.stringify(s.tags)}`]]);
    }),
  },
  // ② the sections that need no Core say their own reason and promise nothing (PROTOCOL §15)
  {
    name: 'settings_save_lang_not_switched', scenario: LANG_NOT_SWITCHED, actions: [...pickLang(1), ...save],
    expect: run(async page => {
      const s = await state(page);
      const [reopen] = await words(page, [['settings.app.lang.reopen']]);
      return check([[s.bar && /is-partial/.test(s.bar.cls) && !s.bar.saveShown && s.bar.eff === reopen, `the bar says what the language row says: ${JSON.stringify(s.bar)}`],
        [(await invoked(page, 'service.restart')) === 0, 'no service.restart']]);
    }),
  },
  {
    name: 'settings_save_records_not_deleted', scenario: RECORDS_NOT_DELETED, actions: [...click('.switch[data-k="egress"]'), ...save],
    expect: run(async page => {
      const s = await state(page);
      const [want, old] = await page.evaluate(() => [window.T.t('settings.save.not_applied_saved', { reason: window.T.t('settings.save.not_applied_reason.delivery_records') }), window.T.t('errors.settings.apply_failed')]);
      const tip = await rowTip(page, 'set-egress-row'), why = await page.evaluate(() => window.T.t('settings.save.not_applied_reason.delivery_records'));
      return check([[s.bar && s.bar.eff === want && !s.bar.eff.includes(old), `the bar's sentence: ${s.bar && s.bar.eff}`], [s.bar && !s.bar.saveShown, 'no 重试应用'],
        [tip === why, `the row's tip (修复第 1 轮): ${tip}`]]);
    }),
  },
  {
    // 修复第 1 轮: the language sent, the ui section's preferences not applied: no 重新打开窗口
    name: 'settings_save_ui_prefs_not_applied', scenario: UI_PREFS_NOT_APPLIED, actions: [...pickLang(1), ...save],
    expect: run(async page => {
      const s = await state(page);
      const [want, reopen] = await page.evaluate(() => [window.T.t('settings.save.not_applied_saved', { reason: window.T.t('settings.save.not_applied_reason.other') }), window.T.t('settings.app.lang.reopen')]);
      return check([[s.bar && /is-partial/.test(s.bar.cls) && !s.bar.saveShown && s.bar.eff === want && s.bar.eff !== reopen, `the bar's sentence: ${JSON.stringify(s.bar)}`],
        [(await invoked(page, 'service.restart')) === 0, 'no service.restart']]);
    }),
  },
  {
    // 修复第 1 轮: the error's own sentence before the bar's full stop: its own stop is dropped (no doubled full stop)
    name: 'settings_save_reason_own_sentence', scenario: OWN_SENTENCE, actions: [...click('.switch[data-k="egress"]'), ...save],
    expect: run(async page => {
      const s = await state(page);
      const [own, want] = await page.evaluate(() => { const e = window.T.t('errors.timeout'); const r = e.replace(/[\s.!\u3002\uFF0E\uFF01]+$/u, ''); return [e, window.T.t('settings.save.not_applied_saved', { reason: r })]; });
      return check([[s.bar && s.bar.eff === want, `the bar's sentence: ${s.bar && s.bar.eff} (the error: ${own})`],
        [s.bar && !/(\u3002|\.)\s*(\u3002|\.)/u.test(s.bar.eff), `a doubled full stop: ${s.bar && s.bar.eff}`]]);
    }),
  },
  // 配置整理 P2: the rules are one list in match order, each filled in (当 [种类] [内容] 时 [动作]);
  // nothing of the old layout is left (the per-action groups and their ↑, 写法, 配置文件中的规则 and
  // its 打开配置目录). A rule an earlier rule of another action covers reads 不会生效; a combination
  // shows its conditions; a legacy rule the old compile dropped stays 未启用 until an action is
  // picked; the guard refuses a LAN address unless direct; a rule set still in use stays. The save
  // sends the whole list and the rule sets (a withheld address by name only).
  {
    name: 'settings_rule_list', scenario: RULE_LIST,
    actions: [
      { eval: () => {
        const q = s => document.querySelector(s), txt = e => (e ? e.textContent.replace(/[ \t\n\r\f]+/g, ' ').trim() : null);
        const tip = (r, s) => { const e = r.querySelector(s); return e ? e.getAttribute('data-tip') : null; };
        window.__a = {
          old: ['#set-adv', '#set-adv-open', '#set-pfx-help', '.set-rgrp', '#set-rl-custom [data-up]', '#set-rl-direct'].filter(s => q(s)),
          rows: Array.from(document.querySelectorAll('#set-rl-custom .set-rule')).map(r => ({
            v: r.querySelector('.set-cond') ? null : txt(r.querySelector('.set-rule-v')), kind: txt(r.querySelector('.set-rule-kt')),
            conds: r.querySelectorAll('.set-cond').length, off: r.classList.contains('is-off'), offTip: tip(r, '.set-rule-off'),
            sh: tip(r, '.set-rule-sh:not(.set-rule-off)'), res: !!r.querySelector('.set-rule-res') })),
          sets: Array.from(document.querySelectorAll('#set-sets-list .set-set-r')).map(r => [txt(r.querySelector('.set-set-n')), txt(r.querySelector('.set-set-u'))]),
          dirty: window.T.ui.saveBar.dirtyCount() };
      } },
      // a LAN address with the default action (proxy) is refused; picking 直连 lifts the note, and it is added
      { fill: ['#set-rule-in', '192.168.1.0/24'] }, { page_ms: 200 }, ...click('#set-rule-add'), keepMsg('#set-rule-f', '__lan'),
      ...click('#set-rule-act .seg-btn[data-v="direct"]'), keepMsg('#set-rule-f', '__lan2'), ...click('#set-rule-add'),
      // the off row: an action picked from its chip turns it on
      ...tap('#set-rl-custom .set-rule.is-off .set-act'),
      { eval: () => { const w = window.T.t('settings.route.act.block.label'), i = Array.from(document.querySelectorAll('.menu .menu-i')).find(x => x.textContent.trim() === w); if (i) i.click(); } },
      { page_ms: 400 },
      // a rule set still in use stays; a new one is added
      { eval: () => { const r = Array.from(document.querySelectorAll('#set-sets-list .set-set-r')).find(x => x.querySelector('.set-set-n').textContent === 'cn-sites'); if (r) r.querySelector('[data-del]').click(); } },
      { page_ms: 400 }, keepMsg('#set-set-f', '__inUse'),
      { fill: ['#set-set-name', 'ads'] }, { fill: ['#set-set-url', 'https://rules.example.net/ads.mrs'] }, { page_ms: 200 }, ...click('#set-set-add'),
      { eval: () => { window.__b = { rows: document.querySelectorAll('#set-rl-custom .set-rule').length, off: document.querySelectorAll('#set-rl-custom .set-rule.is-off').length,
        sets: document.querySelectorAll('#set-sets-list .set-set-r').length }; } },
      ...save],
    expect: run(async page => {
      const { a, b, lan, lan2, inUse } = await page.evaluate(() => ({ a: window.__a, b: window.__b, lan: window.__lan, lan2: window.__lan2, inUse: window.__inUse }));
      const w = await page.evaluate(() => ({
        shadow: window.T.t('settings.route.shadow.tip', { rule: `${window.T.t('settings.route.act.direct.label')} · suffix:example.cn` }),
        off: window.T.t('settings.route.off.dropped_policy'), all: window.T.t('settings.route.combo.all'), hidden: window.T.t('settings.route.sets.url_hidden'),
        lan: window.T.t('settings.route.rule.err.local_only'), inUse: window.T.t('settings.route.sets.err.in_use', { v: 'cn-sites' }) }));
      const sent = await applied(page);
      const routing = sent[0] && sent[0].changes.find(c => c.section === 'routing');
      const list = [
        { to: 'direct', kind: 'ip-cidr', value: '192.168.1.0/24' },
        { to: 'direct', kind: 'suffix', value: 'example.cn' },
        { to: 'proxy', kind: 'domain', value: 'www.example.cn' },
        { to: 'reject', kind: 'and', of: [{ kind: 'dst-port', value: '443' }, { kind: 'network', value: 'udp' }] },
        { to: 'direct', kind: 'rule-set', value: 'cn-sites' },
        { to: 'reject', kind: 'dst-port', value: '22' },
        { to: 'direct', kind: 'geoip', value: 'JP', resolve: true }];
      const sets = [{ name: 'cn-sites', url: 'https://rules.example.net/cn.yaml', behavior: 'domain', format: 'yaml' }, { name: 'private' },
        { name: 'ads', url: 'https://rules.example.net/ads.mrs', behavior: 'domain', format: 'mrs' }];
      const v = routing ? routing.values : {};
      return check([
        [a.old.length === 0, `nothing of the old layout: ${a.old}`],
        [JSON.stringify(a.rows.map(r => r.v)) === '["example.cn","www.example.cn",null,"cn-sites","22","JP"]', `the list in match order: ${JSON.stringify(a.rows)}`],
        [a.rows[1].sh === w.shadow && !a.rows[0].sh, `the covered proxy rule reads 不会生效 with its cover: ${JSON.stringify(a.rows[1])} / ${w.shadow}`],
        [a.rows[2].conds === 2 && a.rows[2].kind === w.all, `the combination shows its conditions: ${JSON.stringify(a.rows[2])}`],
        [a.rows[4].off && a.rows[4].offTip === w.off && !a.rows.some((r, i) => i !== 4 && r.off), `the dropped legacy rule is 未启用: ${JSON.stringify(a.rows[4])}`],
        [a.rows[5].res && !a.rows[0].res, `the IP rule that resolves host names is marked: ${JSON.stringify(a.rows[5])}`],
        [JSON.stringify(a.sets) === JSON.stringify([['cn-sites', 'https://rules.example.net/cn.yaml'], ['private', w.hidden]]), `rule sets: ${JSON.stringify(a.sets)}`],
        [a.dirty === 0, `nothing changed on entry: ${a.dirty}`],
        [lan && lan.invalid && lan.msg === w.lan && lan2 && !lan2.invalid, `a LAN address is refused unless direct: ${JSON.stringify([lan, lan2])} / ${w.lan}`],
        [inUse && inUse.invalid && inUse.msg === w.inUse, `a rule set in use stays: ${JSON.stringify(inUse)} / ${w.inUse}`],
        [b && b.rows === 7 && b.off === 0 && b.sets === 3, `after the edits: ${JSON.stringify(b)}`],
        [JSON.stringify(Object.keys(v).sort()) === '["providers","rule_list"]' && JSON.stringify(v.rule_list) === JSON.stringify(list) && JSON.stringify(v.providers) === JSON.stringify(sets),
          `the whole list and the rule sets are sent: ${JSON.stringify(v)}`]]);
    }),
  },
  // ⑤ a save in the new generation's window starts its own batch
  {
    name: 'settings_gen_second_batch', scenario: GEN_SECOND_BATCH,
    actions: [...click('.switch[data-k="lan"]'), ...save, { page_ms: 400 },
      { deliver: { t: 'rt:reset', epoch: E2, reason: 'runtime_gone' } }, ...E2_NO_STATE.map(f => ({ deliver: f })), { page_ms: 800 },
      ...click('.switch[data-k="ipv6"]'), ...save, { page_ms: 400 },
      { eval: () => { window.__mid = { lan: (document.querySelector('#set-lan-row .set-res-tag') || {}).className || '', ipv6: (document.querySelector('#set-ipv6-row .set-res-tag') || {}).className || '', run: window.T.store.get('run') }; } },
      ...E2_STATE.map(f => ({ deliver: f })), { page_ms: 400 }],
    expect: run(async page => {
      const mid = await page.evaluate(() => window.__mid);
      const s = await state(page);
      const sent = await applied(page);
      return check([[sent.length === 2 && sent[1].base_rev === 1, `the second save in the new generation: ${JSON.stringify(sent.map(x => x.base_rev))}`],
        [/is-pending/.test(mid.ipv6) && (mid.run == null || mid.run === 'unknown'), `before the service state: ${JSON.stringify(mid)}`],
        [s.tags['set-lan-row'] === 'is-applied', `the old batch settles: ${JSON.stringify(s.tags)}`],
        [s.tags['set-ipv6-row'] === 'is-pending', `the new batch follows its own intent, not 已生效: ${JSON.stringify(s.tags)}`]]);
    }),
  },
  // ⑥ the visit's first snapshot follows by intent id only
  {
    name: 'settings_revisit_foreign_intent', scenario: REVISIT,
    actions: [...click('.switch[data-k="lan"]'), ...save, { page_ms: 400 }, ...BLANK_PAGE, { go: 'settings' }, { page_ms: 800 }],
    expect: run(async page => {
      const s = await state(page);
      const snaps = await invoked(page, 'config.snapshot');
      return check([[snaps === 2, `config.snapshot reads (two visits): ${snaps}`],
        [s.tags['set-lan-row'] === 'is-pending', `an intent the page never saw does not relabel its rows: ${JSON.stringify(s.tags)}`]]);
    }),
  },
  // ⑦ Geo 已禁用 is forgotten on an external change and on a new generation
  {
    name: 'settings_geo_disabled_external', scenario: GEO_EXTERNAL,
    actions: [...click('#set-geo-btn'), { advance_ms: 50 }, { page_ms: 4000 }, keepGeo,
      // (the topic's first frame after a connect is instant, as a seed: the external write is the second)
      { deliver: { t: 'evt', ...ENV, topic: 'config.changed', rev: 1, kind: 'edge', seq: 1, data: { rev: 7, files: ['config'], sections: [], external_detection: 'bridge_writes_only', pending: null } } }, { page_ms: 200 },
      { deliver: { t: 'evt', ...ENV, topic: 'config.changed', rev: 2, kind: 'edge', seq: 2, data: { rev: 8, files: ['config'], sections: [], external_detection: 'bridge_writes_only', pending: null } } }, { page_ms: 600 }],
    expect: run(async page => {
      const mid = await page.evaluate(() => window.__geo), now = await page.evaluate(geoState);
      return check([[mid.disabled && mid.err === 1, `disabled after the refusal: ${JSON.stringify(mid)}`], [!now.disabled && now.err === 0, `offered again after the external change: ${JSON.stringify(now)}`],
        [(await invoked(page, 'config.snapshot')) === 2, 'the external change is read']]);
    }),
  },
  {
    name: 'settings_geo_disabled_epoch', scenario: GEO_EPOCH,
    actions: [...click('#set-geo-btn'), { advance_ms: 50 }, { page_ms: 4000 }, keepGeo,
      { deliver: { t: 'rt:reset', epoch: E2, reason: 'runtime_gone' } }, ...EPOCH_FRAMES.map(f => ({ deliver: f })), { page_ms: 800 }],
    expect: run(async page => {
      const mid = await page.evaluate(() => window.__geo), now = await page.evaluate(geoState);
      return check([[mid.disabled && mid.err === 1, `disabled after the refusal: ${JSON.stringify(mid)}`], [!now.disabled && now.err === 0, `offered again in the new generation: ${JSON.stringify(now)}`]]);
    }),
  },
  // 待用户认可表第 11 行 (T-P4-H4): 提醒区「查看全部」 goes to the logs page filtered to 运行告警, every
  // level and run (logs.mjs logs_enter_category checks what the logs page does with it)
  {
    // (T-P5-13: the nine languages; the target and its params do not depend on the language)
    name: 'settings_recent_all_category', scenario: 'settings_page_running', langs: LANGS9,
    actions: [{ eval: () => { const go = T.go; window.__goArgs = null; T.go = (p, a) => { window.__goArgs = [p, a]; return false; }; try { document.querySelector('#set-recent-all').click(); } finally { T.go = go; } } }],
    expect: run(async page => {
      const g = await page.evaluate(() => window.__goArgs);
      return check([[!!g && g[0] === 'logs' && JSON.stringify(g[1]) === '{"category":"alert","level":"all","runOnly":false}', `查看全部: ${JSON.stringify(g)}`]]);
    }),
  },
  // ⑧ a source row's buttons are named by kind and number, no state word (a draft is not 已设置)
  {
    name: 'settings_sources_row_aria', scenario: 'settings_page_running',
    actions: [{ fill: ['#set-src-in', 'https://share.example.net/a.txt'] }, { press: 'Enter' }, { page_ms: 400 }],
    expect: run(async page => {
      const r = await page.evaluate(() => [...document.querySelectorAll('#set-src-list .set-src-r')].map((row, i) => ({
        labels: [...row.querySelectorAll('[aria-label]')].map(b => b.getAttribute('aria-label')),
        want: window.T.t('settings.bastion.src.row_aria', { index: i + 1 }), unnamed: window.T.t('settings.bastion.src.unnamed', { index: i + 1 }) })));
      return check([[r.length === 3, `three rows: ${r.length}`],
        ...r.map((x, i) => [x.labels.length > 0 && x.labels.every(l => l.includes(x.want) && !l.includes(x.unnamed)), `row ${i + 1}: ${JSON.stringify(x)}`])]);
    }),
  },
  {
    // 续复核1 MUST 1: a new sidecar epoch restarts the config revision; the page forgets the lost
    // epoch's rev (rt:reset generation_changed), reads the new snapshot (rev 1) on the new hello's
    // rt:caps, and the next save sends base_rev 1
    name: 'settings_epoch_reset', scenario: EPOCH_RESET,
    actions: [...click('.switch[data-k="lan"]'), ...save, { page_ms: 400 },
      { deliver: { t: 'rt:reset', epoch: E2, reason: 'runtime_gone' } },
      ...EPOCH_FRAMES.map(f => ({ deliver: f })), { page_ms: 800 },
      ...click('.switch[data-k="ipv6"]'), ...save, { page_ms: 400 }],
    expect: run(async page => {
      const sent = await applied(page);
      const s = await state(page);
      const snaps = await page.evaluate(() => window.__TESSERACT_DESIGN__.transport().invocations.filter(c => c.kind === 'rt' && c.cmd === 'config.snapshot').length);
      const net = sent[1] && sent[1].changes.find(c => c.section === 'network');
      return check([[sent.length === 2 && sent[0].base_rev === 7 && sent[1].base_rev === 1, `base_rev of the two saves: ${JSON.stringify(sent.map(x => x.base_rev))}`],
        [snaps === 2, `config.snapshot reads (the visit, the new generation): ${snaps}`],
        [net && JSON.stringify(net.values) === '{"ipv6":false}', `the second change: ${JSON.stringify(net)}`],
        [s.bar && /is-saved/.test(s.bar.cls), `bar after the second save: ${JSON.stringify(s.bar)}`]]);
    }),
  },
  {
    // 续复核1 MUST 1 (the rows a save left pending): settled by the new generation's snapshot
    name: 'settings_epoch_pending', scenario: EPOCH_PENDING,
    actions: [...click('.switch[data-k="lan"]'), ...save, { page_ms: 400 },
      { eval: () => { window.__e1 = (document.querySelector('#set-lan-row .set-res-tag') || {}).className || ''; } },
      { deliver: { t: 'rt:reset', epoch: E2, reason: 'runtime_gone' } },
      ...EPOCH_FRAMES.map(f => ({ deliver: f })), { page_ms: 800 }],
    expect: run(async page => {
      const mid = await page.evaluate(() => window.__e1);
      const s = await state(page);
      return check([[/is-pending/.test(mid), `the row after the deferred save: ${mid}`],
        [s.tags['set-lan-row'] === 'is-applied', `the row after the new generation's snapshot: ${JSON.stringify(s.tags)}`]]);
    }),
  },
  {
    // 续复核1 MUST 1: the read after a conflict is taken even when its rev is lower than the page's
    name: 'settings_conflict_lower_rev', scenario: CONFLICT_LOWER,
    actions: [...click('.switch[data-k="lan"]'), ...save, { page_ms: 600 }, ...click('.switch[data-k="ipv6"]'), ...save, { page_ms: 400 }],
    expect: run(async page => {
      const sent = await applied(page);
      return check([[sent.length === 2 && sent[0].base_rev === 7 && sent[1].base_rev === 1, `base_rev of the two saves: ${JSON.stringify(sent.map(x => x.base_rev))}`]]);
    }),
  },
  {
    // the payload of the read/write-asymmetric port (read network.subscription.port, written
    // network.subscription_port) and of D8 (ui.start_in_tray_on_autostart)
    name: 'settings_save_payload_port_tray', scenario: PAYLOAD_TRAY,
    actions: [{ fill: ['#set-port-sub', '25600'] }, { page_ms: 400 }, ...click('.switch[data-k="tray"]'), ...save],
    expect: run(async page => {
      const sent = await applied(page);
      const by = sec => sent[0] && sent[0].changes.find(c => c.section === sec);
      return check([[by('network') && JSON.stringify(by('network').values) === '{"subscription_port":25600}', `network change: ${JSON.stringify(by('network'))}`],
        [by('ui') && JSON.stringify(by('ui').values) === '{"start_in_tray_on_autostart":true}', `ui change: ${JSON.stringify(by('ui'))}`]]);
    }),
  },
  {
    // (fix round 1, SHOULD / B-29) the source check is the backend's: an http(s) URL with a host
    // (localhost with a port too), a GitHub mirror URL, a Telegram name
    name: 'settings_sources_backend_rules', scenario: 'settings_page_running',
    actions: [{ fill: ['#set-src-in', 'http://localhost:25500/sub'] }, { press: 'Enter' }, { page_ms: 400 }],
    expect: run(async page => {
      const s = await state(page);
      const bad = await page.evaluate(() => document.querySelector('#set-src-in').closest('.field, .set-field, [class*="field"]')?.classList.contains('is-invalid') || false);
      return check([[s.sources.length === 3 && s.sources[2].includes('localhost:25500'), `sources: ${s.sources}`], [!bad, 'no field error']]);
    }),
  },
  // ---- {focus} landings (host:navigate, the update banner) -----------------------------------------
  {
    name: 'settings_focus_alerts', scenario: 'settings_focus', params: { focus: 'alerts' }, actions: [{ page_ms: 1500 }],
    expect: run(async page => {
      const r = await page.evaluate(() => { const h = document.querySelector('#host'), t = document.querySelector('#set-alerts'); return { top: Math.round(t.getBoundingClientRect().top - h.getBoundingClientRect().top), focus: !!document.activeElement.closest('#set-alerts .fsec-head') }; });
      return check([[r.focus, 'focus on the alerts heading'], [r.top >= 0 && r.top < 120, `the alerts section in view: ${r.top}`]]);
    }),
  },
  {
    name: 'settings_focus_update', scenario: 'settings_focus', params: { focus: 'update' }, actions: [{ page_ms: 1500 }],
    expect: run(async page => {
      const r = await page.evaluate(() => { const h = document.querySelector('#host'), t = document.querySelector('#set-upd-row'), b = t.getBoundingClientRect(), hb = h.getBoundingClientRect(); return { top: Math.round(b.top - hb.top), bottom: Math.round(hb.bottom - b.bottom) }; });
      return check([[r.top >= 0 && r.bottom >= 0, `the update row in view: ${JSON.stringify(r)}`]]);
    }),
  },
  // ---- a language switch keeps the drafts (relocalize 'rerender') -----------------------------------
  {
    // (T-P5-13: the nine languages; the page switches to pseudo and back to the cell's own language)
    name: 'settings_lang_switch_drafts', scenario: 'settings_page_running', langs: LANGS9,
    actions: [...click('.switch[data-k="lan"]'), { fill: ['#set-rule-in', 'example.net'] }, { page_ms: 400 },
      { eval: () => { window.__before = { lang: window.T.i18n.lang, count: window.T.ui.saveBarCurrent.count, lan: document.querySelector('.switch[data-k="lan"]').getAttribute('aria-checked') }; } },
      { eval: () => window.T.i18n.set('pseudo') }, { page_ms: 900 }, { eval: () => window.T.i18n.set(window.__before.lang) }, { page_ms: 900 }],
    expect: run(async page => {
      const r = await page.evaluate(() => ({ before: window.__before, lang: window.T.i18n.lang, count: window.T.ui.saveBarCurrent ? window.T.ui.saveBarCurrent.count : 0,
        lan: document.querySelector('.switch[data-k="lan"]').getAttribute('aria-checked'), rule: document.querySelector('#set-rule-in').value }));
      return check([[r.lang === r.before.lang && r.count === r.before.count && r.lan === 'true' && r.rule === 'example.net', `drafts after two switches: ${JSON.stringify(r)}`]]);
    }),
  },
  // ---- B items (architecture §5): both keys or nothing ------------------------------------------------
  {
    name: 'settings_features', scenario: 'settings_features',
    expect: run(async page => {
      const r = await page.evaluate(() => ({ build: (window.__TESSERACT_DESIGN__.build.h_features || []).slice(),
        last: !!document.querySelector('.set-src-last'),
        paused: !document.querySelector('#set-alertsPaused-row').hidden }));
      // (B-28, the compatibility rules' 恢复默认, went with the purged stage: 11-01; B-24's 打开日志目录
      // with the 数据目录 row: 11-04)
      const on = ['B-07', 'B-31'].every(n => r.build.includes(n));
      return check([[r.last === on && r.paused === on, `B controls ${on ? 'missing' : 'shown'} (build ${r.build.length} names): ${JSON.stringify(r)}`]]);
    }),
  },
];

// pseudo-language widths (every entry says why): none so far beyond the page's own wrapping
const PSEUDO_LENGTH = [
  { selector: '^#set-(?:dns-mode|src-type|res-type|lang) > span\\.select-v', why: '伪语言：下拉框当前值定宽，按设计定宽或单行；没有另开布局单，真实语言的溢出由 P5 用全语言矩阵复核（第九波裁定，T-P4-12）' },
  { selector: '^#set-(?:rule-kind|rule-pick|set-behav)-v > span\\.sw$', why: '伪语言：分流规则的种类、内容和规则集内容下拉框当前值定宽（配置整理 P2；真实语言默认值最长的俄语「Автоопределение」在 148px 内放得下），选项全名在菜单里' },
  { selector: '^#set-rl-custom > div\\.li\\.set-rule[^ ]* > span\\.set-rule-k[^ ]* > span\\.set-rule-res', why: '伪语言：规则行「解析域名」小标记在窄窗口的种类列里放不下（配置整理 P2；真实语言九种都放得下）' },
  { selector: '^#set-rl-cm > span\\.sw', why: '伪语言：自定义规则条数（窄窗口）定宽，按设计定宽或单行；没有另开布局单，真实语言的溢出由 P5 用全语言矩阵复核（第九波裁定，T-P4-12）' },
  { selector: '^#set-geo > div\\.set-geo-i[^ ]* > span\\.set-geo-n', why: '伪语言：规则数据四个卡片的标题（窄窗口）定宽，按设计定宽或单行；没有另开布局单，真实语言的溢出由 P5 用全语言矩阵复核（第九波裁定，T-P4-12）' },
  // the 提醒与通知 section is T-P4-05e's (still the design data here): the same allowances as T-P4-04e's
  { selector: '^#set-ch-gal > div\\.set-chg[^ ]* > span\\.set-chg-l', why: '伪语言：提醒方式图库的分组名列定宽（提醒区归 T-P4-05e）' },
  { selector: '^#set-recent > div\\.li\\.two[^ ]* > span\\.sev', why: '伪语言：最近提醒的级别列定宽（提醒区归 T-P4-05e）' },
  { selector: '^#set-[A-Za-z]+-row > div\\.frow-l[^ ]* > div\\.frow-t[^ ]* > span\\.set-t$', why: '伪语言：设置行标题末尾的波浪号补长不能换行（真实语言的长标题已可换行）（T-P5-11 盒子越界规则；9 种真实语言在同一用例下都不越界，由扩到 9 种语言的正式矩阵核对）' },
  { selector: '^#set-upd-(?:row > div\\.frow-l[^ ]* > div\\.frow-d\\.set-ver|st > span\\.swap-box > span\\.sw$)', why: '伪语言（T-P5-13）：「版本与更新」行的三个按钮（更新说明、安装更新、检查更新）的伪语言标签都不能换行，占去整行，左侧的版本行与检查状态句只剩约 8px，伪语言补长的波浪号词越出；配置页目录列在伪语言里宽到上限（+28px）后更明显。9 种真实语言同一用例不越界，由全语言矩阵核对' },
];
// the network-mode cards' notes and a confirm dialog's message have no break opportunity in the
// pseudo language (one long accented word; see settings_i18n.mjs pseudoBreaks): the same
// test-only style; real languages wrap at spaces
function pseudoBreaks() {
  // (a constructed style sheet, not a <style> element: the --csp strict run (T-P4-08b) serves the
  // shell's style-src 'self', which blocks an inline <style> but not the CSSOM)
  const s = new CSSStyleSheet();
  s.replaceSync('.ovl .dlg-msg { overflow-wrap: anywhere; }');
  document.adoptedStyleSheets = [...document.adoptedStyleSheets, s];
}
const DEMO_BADGE = ['^演示$'];

export default CASES.flatMap(c => {
  const base = { page: 'settings', ...c };
  if (c.langs) return [{ ...base, overflow_allow: [] }];
  // T-P5-11 (ruling 3): the real-language cell runs in all nine languages, with no allowance
  return [
    { ...base, langs: LANGS9, overflow_allow: [] },
    { ...base, name: `${c.name}_pseudo`, langs: ['pseudo'], actions: [{ eval: pseudoBreaks }, { page_ms: 100 }, ...(c.actions || [])], overflow_allow: PSEUDO_LENGTH, leak_allow: DEMO_BADGE },
  ];
});
