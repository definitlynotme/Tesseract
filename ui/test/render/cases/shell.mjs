// Render matrix of the shell layer (T-P4-06): the shell_* scenarios (ui/test/fixtures/scenarios,
// S1-S18 of mapping_core section 8, network_recovery failed / not_owned, a same-epoch reconnect,
// dialog_open, update.state=available) in zh_CN and the pseudo language at both sizes.
// Every cell first moves to an empty page (BLANK, titled as the overview), so its verdict is the
// shell's alone: sidebar (brand light, footer), top bar (toasts), banners, the command palette.
// The page regions belong to their 05x tickets. expect() checks the state in the page with the
// catalog's own words (T.t), so the same assertions hold in both languages.
//   node ui/test/render/run.mjs --page shell
const settle = { page_ms: 900 };
const BLANK = [{ eval: () => { T.page('t06_blank', { titleId: 'shell.page.overview.title' }); T.go('t06_blank', { instant: true }); } }, { page_ms: 600 }];
const openPalette = [{ eval: () => T.cmd.open() }, { page_ms: 400 }];

// Runs in the page: the shell layer's state, compared with the catalog's words.
function shellState() {
  const app = document.querySelector('#app');
  const txt = el => (el ? el.textContent.replace(/\s+/g, ' ').trim() : null);
  const banners = {};
  for (const b of document.querySelectorAll('#banners .banner')) {
    banners[b.dataset.id] = {
      kind: b.className.includes('banner-error') ? 'error' : b.className.includes('banner-info') ? 'info' : 'warning',
      sev: !!b.querySelector('.banner-sev'),
      title: txt(b.querySelector('.banner-title > span')), msg: txt(b.querySelector('.banner-msg')),
      actions: [...b.querySelectorAll('.banner-actions button')].map(x => txt(x)),
    };
  }
  const toasts = [...document.querySelectorAll('#toast-slot > .toast:not(.toast-more)')].filter(x => !x._gone).map(x => txt(x.querySelector('.toast-msg')));
  const more = document.querySelector('#toast-slot > .toast-more');
  const palette = {};
  for (const b of document.querySelectorAll('.cmdk-list .cmdk-item')) palette[b.getAttribute('aria-label')] = [b.getAttribute('aria-disabled') === 'true', txt(b.querySelector('.hint')) || ''];
  const foot = document.querySelector('.side-foot');
  const sw = el => { const all = el ? [...el.querySelectorAll('.sw')].filter(x => x.getAttribute('aria-hidden') !== 'true') : []; return all.length ? txt(all[all.length - 1]) : null; };
  return {
    run: app.dataset.run, ks: app.dataset.ks, stale: 'stale' in app.dataset, brandTitle: document.querySelector('.brand').getAttribute('title'),
    banners, toasts, more: more ? txt(more.querySelector('.toast-msg')) : null, palette,
    footVer: sw(foot && foot.querySelector('.ver')), footUpd: sw(foot && foot.querySelector('.foot-upd')),
    hosts: T.rt.__fake.invocations.filter(x => x.kind === 'host').map(x => [x.name, x.args || null]),
    pwned: window.__pwned === undefined ? null : window.__pwned,
  };
}
// Runs in the page: the catalog words the checks compare with.
function words(ids) {
  const out = {};
  for (const [k, [id, p]] of Object.entries(ids)) out[k] = T.t(id, p);
  return out;
}

// Runs in the page (T-P5-11 ruling 6): each banner's severity badge (.banner-sev) shows its words
// on one line, not broken inside a word, not cut.
export function bannerSevWhole() {
  const out = [];
  for (const b of document.querySelectorAll('#banners .banner-sev')) {
    if (!b.checkVisibility() || !b.textContent.trim()) continue;
    const rg = document.createRange();
    rg.selectNodeContents(b);
    const lines = new Set([...rg.getClientRects()].filter(r => r.width > 0).map(r => Math.round(r.top)));
    const r = b.getBoundingClientRect();
    if (lines.size > 1) out.push(`banner badge "${b.textContent.trim()}" breaks over ${lines.size} lines (${r.width.toFixed(1)} px wide)`);
    if (b.scrollWidth > b.clientWidth + 1) out.push(`banner badge "${b.textContent.trim()}" is cut (${b.scrollWidth} > ${b.clientWidth} px)`);
  }
  return out;
}

function check(fn, ids = {}, also = null) {
  return async ({ page }) => {
    const s = await page.evaluate(shellState);
    const w = await page.evaluate(words, ids);
    const problems = [];
    const ok = (cond, msg) => { if (!cond) problems.push(`${msg} :: ${JSON.stringify(s).slice(0, 600)}`); };
    fn(s, w, ok);
    if (also) problems.push(...await page.evaluate(also));
    return { problems };
  };
}
const NOT_RESTORED = /已恢复|已确认|restored/;
const hostNames = s => s.hosts.map(h => h[0]);

const CASES = [
  // S1 connecting: hello, no snapshot. Grey still light, service commands disabled, no host_ready.
  { name: 'shell_connecting', actions: openPalette, ids: { start: ['shell.palette.cmd.svc_toggle', { action: 'start' }], title: ['shell.brand.title', { run: 'unknown' }] },
    expect: (s, w, ok) => {
      ok(s.run === 'unknown' && s.ks === 'unknown', 'data-run / data-ks unknown');
      ok(s.brandTitle === w.title, 'brand title says connecting');
      ok(s.palette[w.start] && s.palette[w.start][0] === true, 'start disabled while unknown');
      ok(hostNames(s).includes('host_notify_ready') && !hostNames(s).includes('host_ready'), 'notify_ready, no host_ready');
    } },
  // S2 start failure: failed light, startup banner (retry, view logs)
  { name: 'shell_start_failed', ids: { title: ['shell.startup.title'], retry: ['common.action.retry'], logs: ['common.action.view_logs'], msg: ['errors.start.listener_conflict', { kind: 'mixed', port: 7890 }] },
    expect: (s, w, ok) => {
      ok(s.run === 'failed', 'data-run failed');
      const b = s.banners.startup;
      ok(b && b.kind === 'error' && b.title === w.title, 'startup banner');
      ok(b && b.actions.join('|') === `${w.retry}|${w.logs}`, 'retry + view logs');
      ok(hostNames(s).includes('host_ready'), 'host_ready after the snapshot');
    } },
  // S3 stop failure (live job edge) and pending cleanup
  { name: 'shell_stop_failed', ids: { stop: ['shell.stop.failed.title'], cleanup: ['shell.stop.cleanup.title'], action: ['shell.stop.action'] },
    expect: (s, w, ok) => {
      ok(s.banners.stop && s.banners.stop.title === w.stop, 'stop failure banner');
      ok(s.banners.cleanup && s.banners.cleanup.title === w.cleanup && s.banners.cleanup.actions[0] === w.action, 'cleanup banner keeps 停止服务');
    } },
  // S4 cancelling a start
  { name: 'shell_cancelling', actions: openPalette, ids: { cancel: ['shell.palette.cmd.svc_toggle', { action: 'cancel' }] },
    expect: (s, w, ok) => ok(s.palette[w.cancel] && s.palette[w.cancel][0] === true, 'start / stop disabled while cancelling') },
  // S5 sidecar crashed: restarting, unrecoverable, restarted
  { name: 'shell_sidecar_restarting', ids: { title: ['shell.sidecar.restarting.title'], body: ['shell.sidecar.restarting.body', { n: 1, limit: 3 }] },
    expect: (s, w, ok) => ok(s.banners.sidecar && s.banners.sidecar.title === w.title && s.banners.sidecar.msg === w.body, 'restarting banner') },
  { name: 'shell_sidecar_unrecoverable', ids: { title: ['shell.sidecar.unrecoverable.title'], logs: ['shell.action.open_logs'] },
    expect: (s, w, ok) => ok(s.banners.sidecar && s.banners.sidecar.kind === 'error' && s.banners.sidecar.title === w.title && s.banners.sidecar.actions[0] === w.logs, 'unrecoverable banner, open the log folder') },
  { name: 'shell_sidecar_restarted', ids: { done: ['shell.sidecar.restarted'] },
    expect: (s, w, ok) => { ok(!s.banners.sidecar, 'banner gone'); ok(s.toasts.includes(w.done), 'one-time success toast'); } },
  // S6 unresponsive: force restart
  { name: 'shell_sidecar_unresponsive', ids: { title: ['shell.sidecar.unresponsive.title'], force: ['shell.sidecar.force_restart'] },
    expect: (s, w, ok) => ok(s.banners.unresponsive && s.banners.unresponsive.title === w.title && s.banners.unresponsive.actions[0] === w.force, 'force restart') },
  // S7 runtime gone: a new epoch, the light back to unknown until a snapshot
  { name: 'shell_runtime_gone',
    expect: (s, w, ok) => { ok(s.run === 'unknown', 'unknown after the generation change'); ok(!s.banners.fallback && !s.banners.startup, 'old-epoch banners gone'); } },
  // S8 stale service state
  { name: 'shell_stale', ids: { stale: ['shell.brand.stale'] },
    expect: (s, w, ok) => { ok(s.stale, 'data-stale'); ok(s.brandTitle === w.stale, 'brand title says out of date'); } },
  // S9 network recovery: failed (unconfirmed, data folder) and not_owned (its own sentence)
  { name: 'shell_recovery_failed', ids: { title: ['shell.recovery.title'], data: ['shell.action.open_data'] },
    expect: (s, w, ok) => {
      const b = s.banners.recovery;
      ok(b && b.title === w.title && b.actions[0] === w.data, 'unconfirmed recovery, open the data folder');
      ok(b && !NOT_RESTORED.test(`${b.title} ${b.msg}`.replace(/不需要恢复/g, '')), 'never says restored');
    } },
  { name: 'shell_recovery_not_owned', ids: { title: ['shell.recovery.not_owned.title'], body: ['shell.recovery.not_owned.body'] },
    expect: (s, w, ok) => {
      const b = s.banners.recovery;
      ok(b && b.title === w.title && b.msg === w.body && !b.actions.length, 'not_owned sentence, no action');
      ok(b && !/恢复失败/.test(`${b.title} ${b.msg}`), 'not "recovery failed"');
    } },
  // S10 observed tier fallback
  // (T-P5-11 ruling 6: the severity badge beside the title, pt_BR Aviso at 1060, is never broken
  // inside a word: one line, its words whole; every language, both sizes)
  { name: 'shell_tier_fallback', ids: { title: ['shell.notice.fallback.title', { to: 'Sentinel 订阅' }] },
    expect: (s, w, ok) => ok(s.banners.fallback && s.banners.fallback.kind === 'warning', 'fallback banner'), also: bannerSevWhole },
  // S11 mode switch failure: the generic command error banner
  { name: 'shell_mode_failed', actions: [{ eval: () => { T.shellc.run('mode.set', { mode: 'system' }); } }, settle], ids: { title: ['shell.command_error.title'], msg: ['errors.admin_required'] },
    expect: (s, w, ok) => ok(s.banners['cmd-error'] && s.banners['cmd-error'].title === w.title && s.banners['cmd-error'].msg === w.msg, 'command error banner') },
  // S12 caps absent: the commands the runtime does not offer are listed and disabled
  { name: 'shell_caps_absent', actions: openPalette, ids: { exit: ['shell.palette.cmd.svc_exit'], tun: ['shell.palette.cmd.mode_tun'], stop: ['shell.palette.cmd.svc_toggle', { action: 'stop' }] },
    expect: (s, w, ok) => {
      ok(s.palette[w.exit] && s.palette[w.exit][0] === true, 'exit.verify absent: disabled');
      ok(s.palette[w.tun] && s.palette[w.tun][0] === true, 'mode.set absent: disabled');
      ok(s.palette[w.stop] && s.palette[w.stop][0] === false, 'service.stop offered');
    } },
  // S13 update states in the footer
  ...[['checking', 'checking'], ['downloading', 'dl'], ['verified', 'verify'], ['handoff', 'ready'], ['failed', 'fail'], ['updated', 'updated']].map(([sc, st]) => ({
    name: `shell_update_${sc}`, ids: { upd: ['shell.foot.update.label', { state: st, version: '' }], done: ['shell.update.done', { previous: '1.0.0', current: '1.1.0' }] },
    expect: (s, w, ok) => {
      ok(s.footUpd === w.upd, `footer word ${st}`);
      if (sc === 'updated') { ok(s.toasts.includes(w.done), 'update finished notice'); ok(s.footVer === 'Tesseract 1.1.0', 'footer version from current_version'); }
    } })),
  // new version: one banner, two actions (installable) / one (not installable)
  // (F-01: information, not a warning: the neutral banner without a level word)
  { name: 'shell_update_available', ids: { title: ['shell.update.found.title', { version: '1.1.0' }], view: ['shell.update.view'], install: ['shell.update.install'] },
    expect: (s, w, ok) => {
      ok(s.banners.update && s.banners.update.title === w.title && s.banners.update.actions.join('|') === `${w.view}|${w.install}`, 'view + install');
      ok(s.banners.update && s.banners.update.kind === 'info' && !s.banners.update.sev, 'an info banner, no level word');
    } },
  { name: 'shell_update_available_not_installable', ids: { view: ['shell.update.view'] },
    expect: (s, w, ok) => ok(s.banners.update && s.banners.update.actions.join('|') === w.view, 'view only') },
  // S14 notices: five at once and a merge row; a hidden window; malicious text
  { name: 'shell_notify_overflow', ids: { more: ['shell.notice.more', { count: 2 }] },
    expect: (s, w, ok) => {
      ok(s.toasts.length === 5, 'five toasts');
      ok(s.more === w.more, 'merge row: 2 more');
      const r = s.hosts.filter(h => h[0] === 'host_notify_rendered').map(h => h[1].delivery_id);
      ok(r.join() === 'd-1,d-2,d-3,d-4,d-5', 'receipts only for the five on screen');
    } },
  { name: 'shell_notify_hidden',
    expect: (s, w, ok) => {
      ok(s.banners['host-notify:h-1'], 'shown once visible');
      ok(s.hosts.some(h => h[0] === 'host_notify_rendered' && h[1].delivery_id === 'h-1'), 'receipted after it showed');
    } },
  { name: 'shell_notify_malicious',
    expect: (s, w, ok) => {
      ok(s.pwned === null, 'nothing ran');
      ok(s.banners['host-notify:m-1'] && s.banners['host-notify:m-1'].title.startsWith('<img src=x'), 'title shown as text');
    } },
  // S15 / S16 quit question: the answer counts the save bar and the drafts
  { name: 'shell_confirm_quit', expect: (s, w, ok) => ok(s.hosts.some(h => h[0] === 'host_confirm_quit' && h[1].token === 7 && h[1].unsaved === 0), 'answered: 0 unsaved') },
  { name: 'shell_savebar_quit',
    actions: [{ eval: () => { T.ui.saveBar({ count: 3, effect: 'instant', onSave: () => Promise.resolve(), onUndo: () => {} }); window.__TESSERACT_DESIGN__.deliver({ t: 'host:confirmQuit', token: 8, epoch: 'rt-fixture-e1' }); } }, settle],
    expect: (s, w, ok) => ok(s.hosts.some(h => h[0] === 'host_confirm_quit' && h[1].token === 8 && h[1].unsaved === 3), 'answered: 3 unsaved') },
  // S17 command errors: job_capacity, and a host command's reason (dialog_open)
  { name: 'shell_cmd_error', actions: [{ eval: () => { T.shellc.run('service.restart', {}); } }, settle, { eval: () => { T.shellc.openFolder('logs'); } }, settle],
    ids: { title: ['shell.command_error.title'], dialog: ['shell.host_reason.dialog_open'] },
    expect: (s, w, ok) => ok(s.banners['cmd-error'] && s.banners['cmd-error'].msg === w.dialog, 'dialog_open wording') },
  // S18 copy exit IP: disabled while the identity is in conflict, no IP shown
  { name: 'shell_copy_ip_unavailable', actions: openPalette, ids: { copy: ['shell.palette.cmd.svc_copy'] },
    expect: (s, w, ok) => ok(s.palette[w.copy] && s.palette[w.copy][0] === true && s.palette[w.copy][1] === '', 'copy disabled, no address') },
  // same-epoch reconnect: banners stay, the page declares itself again
  { name: 'shell_reconnect_same_epoch', actions: [{ eval: () => T.rt.__fake.reconnect('t06') }, settle], ids: { title: ['shell.startup.title'] },
    expect: (s, w, ok) => {
      ok(s.banners.startup && s.banners.recovery, 'startup and recovery banners kept');
      ok(hostNames(s).filter(n => n === 'host_notify_ready').length === 2, 'host_notify_ready again');
      ok(hostNames(s).filter(n => n === 'host_ready').length === 1, 'host_ready once');
    } },
  // D20 the page half: kill switch off while running (amber), triggered (brick)
  { name: 'shell_ks_disabled', expect: (s, w, ok) => ok(s.run === 'running' && s.ks === 'disabled', 'data-ks disabled') },
  { name: 'shell_ks_triggered', expect: (s, w, ok) => ok(s.ks === 'triggered', 'data-ks triggered') },
];

export default CASES.map(c => ({
  name: c.name, scenario: c.name, page: 'shell',
  actions: [...BLANK, ...(c.actions || [])],
  expect: check(c.expect, c.ids, c.also || null),
}));
