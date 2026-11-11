/* ==========================================================================
   app.js — boot: wire the shell (run-state marker, command palette, shortcuts, global
   notices, sidebar footer), restore the page from location.hash
   (#overview · #tiers · #logs · #settings).
   T-P4-04a: every word is a catalog message (shell.*, tier.*, mode.*, common.*); the words
   that stay on screen follow i18n:change. Palette groups are ids, keywords are the catalog's
   shell.palette.keywords.<cmd> (each language its own; pinyin only in zh_CN) plus fixed
   English ids. T-P4-07: the demo palette command and the QA hooks moved to the design target's
   design/demo.js (no demo code in this file; the shell.demo.* messages are gone).
   T-P4-06: no simulator call (the design simulator starts on its first replay, critique C11);
   runtime facts only, from the rt layer: store keys of rt/project.js and rt:* events (never
   the prototype's). The shell-page contract itself (notices queue, host:* frames, banners of the shell
   layer, quit, presence, language, ready) is in shellc/ (boot/shellc.js).
   11-05: the product name beside the brand mark opens the letter for users (letter/letter.js).
   ========================================================================== */
import { installLetter } from '../letter/letter.js';
(function () {
  'use strict';
  const T = window.T, S = T.store, D = T.data, M = T.motion;
  const appEl = T.$('#app');

  /* the static words of index.html (data-i18n / data-i18n-attr) are written from the catalog
     here and rewritten in place on every language switch (T.i18n relabel) */
  if (T.i18n) T.i18n.relabel(document);

  /* a tier in a sentence: its code name, never translated (F-03: no role word beside it) */
  // (the ids come from runtime events: a tier or mode the static metadata does not know is shown
  // as its raw value, never as a message id)
  const tierName = id => (D.tier(id) ? D.tier(id).name : String(id));
  const tierLabel = tierName;
  const modeLabel = id => (D.modes && Object.prototype.hasOwnProperty.call(D.modes, id) ? T.t(`mode.${id}.label`) : String(id));
  const rt = T.rt || null;
  const can = cmd => !!(rt && rt.can(cmd));
  const raw = topic => (rt ? rt.topic(topic).get() : null);
  // a command from the shell: its error goes to the shell layer's generic error banner (S17)
  const run = (cmd, args) => (T.shellc && T.shellc.run ? T.shellc.run(cmd, args) : (rt ? rt.call(cmd, args || {}) : Promise.reject(new Error('runtime_unavailable'))));

  /* ------------------------------------------------ shell state (mapping_core A1–A4, S1, S8)
     The sidebar carries no status card (the user reads live state on 总览). The app root carries
     run and kill-switch state for CSS: the brand's 「豁然」 dot is the live light.
     data-run: unknown (no snapshot yet: grey, still) · starting · running · stopping · stopped ·
     failed. data-ks (D20, the page half): armed · inactive · triggered (brick) · disabled /
     blocked / error (amber: the kill switch is off or not in force) · unknown. data-stale: the
     service state is out of date (S8): the dot no longer claims to run (grey), the title says why.
     The brand's title says the run state (shell.brand.title) or that the state is out of date. */
  const brandEl = T.$('.brand');
  const KS = ['armed', 'inactive', 'triggered', 'disabled', 'blocked', 'error'];
  function syncShell() {
    const r = S.get('run') || 'unknown';
    const ks = S.get('killswitch');
    const st = S.get('stale') || {};
    const stale = r !== 'unknown' && !!st['service.state'];
    appEl.dataset.run = r;
    appEl.dataset.ks = KS.includes(ks) ? ks : 'unknown';
    if (stale) appEl.dataset.stale = ''; else delete appEl.dataset.stale;
    if (brandEl) brandEl.title = stale ? T.t('shell.brand.stale') : T.t('shell.brand.title', { run: String(r) });
  }
  ['run', 'killswitch', 'stale'].forEach(k => S.sub(k, syncShell));
  // the brand's live light switches on once when the service comes up (one shot; nothing loops;
  // a replayed or first frame is instant: no light-up, mapping_core A4). F-06: when 总览 sent the
  // start's wave out from the run pill, the light is its last note (the brand is the farthest point);
  // the next frame asks, so the page has opened the wave whichever handler ran first
  T.on('rt:state', ev => {
    syncShell();
    if (!brandEl || ev.instant || ev.run !== 'running' || M.reduced) return;
    requestAnimationFrame(() => setTimeout(() => {
      brandEl.classList.remove('is-lit'); void brandEl.offsetWidth; brandEl.classList.add('is-lit');
      setTimeout(() => brandEl.classList.remove('is-lit'), 700);
    }, M.wave.delay(brandEl)));
  });

  /* ------------------------------------------------ global notices (things that happen elsewhere; A5–A8) */
  const onOverview = () => T.router.current === 'overview';
  /* a banner action that navigates: the banner goes once the page has really changed (配置 may hold
     the way with unsaved changes; 留下 keeps the banner, 离开 later takes it away) */
  function goThen(id, page, params) {
    if (T.go(page, params)) { T.dismiss(id); return; }
    const off = T.on('page:change', e => { off(); if (e.id === page) T.dismiss(id); });
  }
  // replayed frames and the first frames after a generation change are instant: no notice
  // a service that is no longer running carries no traffic: the tier banner goes as soon as run
  // leaves running (stopping, stopped, failed, or unknown after a generation change), replayed
  // frames included (T-P4-H report section 7 item 5); a pending one is not shown
  const OFF_RUN = ['stopping', 'stopped', 'failed', 'unknown'];
  T.on('rt:state', ev => {
    if (OFF_RUN.includes(ev.run)) { clearTimeout(tierTimer); T.dismiss('fallback'); lastFallback = null; }
    if (ev.instant) return;
    if (ev.run === 'running' && ev.prev === 'starting' && !onOverview()) T.notify({ kind: 'success', messageId: 'shell.notice.started' });
    if (ev.run === 'stopped' && !ev.cancelled && !onOverview()) T.notify({ kind: 'info', messageId: 'shell.notice.stopped' });
  });
  // tier changes, driven by rt:tier alone (5th-wave ruling 02b-1: the rt layer reconciles
  // tier.transition with tiers.summary). The words say what was observed (PROTOCOL §5.3) and
  // name the tiers from the event: `carrier` now carries the traffic, `from` is where it came
  // from. On the overview the deck finishes its pass (~620 ms) before a banner pushes the page.
  // An instant fallback only brings a banner still up to date (rewords it in place); an instant
  // recover takes it down without a "recovered" toast (rt/project.js; 7th-wave ruling T-P4-H #3),
  // so does a quiet one (into Gateway). A fallback that left a Gateway carries no `from`: the
  // banner names Vanguard as the tier not available (PROXY-FALLBACK's order; T-P4-H2 item 3).
  // Only fallback / recover touch the pending notice: another kind (degraded) neither cancels a
  // banner that is about to show nor takes one down, and a latency recovery (wasFallback:false,
  // a tier that was only slow) leaves a fallback banner up, so a tier.transition that arrives
  // before a late tiers.summary keeps its banner (7th-wave ruling T-P4-H #3).
  let tierTimer = 0, lastFallback = null;
  function fallbackBanner(f) {
    lastFallback = f;
    T.notify({ kind: 'warning', id: 'fallback', title: T.t('shell.notice.fallback.title', { to: tierLabel(f.to) }),
      message: T.t('shell.notice.fallback.body', { from: tierLabel(f.from), to: tierLabel(f.to), from_name: tierName(f.from) }),
      // the banner goes only once the page has really changed (配置 may hold the way with unsaved changes)
      actions: [{ label: T.t('shell.notice.fallback.action'), keep: true, run: () => goThen('fallback', 'profiles', { tier: f.from }) }],
      onDismiss: () => { lastFallback = null; } });
  }
  // a banner still up when the language changes is worded again (replaced in place, same id)
  T.on('i18n:change', () => { if (lastFallback && T.notices.banners.has('fallback')) fallbackBanner(lastFallback); });
  const tierWait = () => (onOverview() && !M.reduced ? 680 : 0);
  // (a catalog message: the toast is reworded in place on a language switch; the tier label in
  // its params is the one of the moment)
  const recoverToast = (e, kind) => { const t = e.carrier || 'vanguard'; const messageParams = { kind, tier: tierLabel(t), name: tierName(t) };
    return { kind: 'success', message: T.t('shell.notice.recover', messageParams), messageId: 'shell.notice.recover', messageParams }; };
  T.on('rt:tier', e => {
    if (!e || (e.kind !== 'fallback' && e.kind !== 'recover')) return;
    // a tier that was only slow has just recovered its latency: a toast of its own, nothing else
    if (e.kind === 'recover' && e.wasFallback === false) {
      if (!e.instant) { const o = recoverToast(e, 'latency'); setTimeout(() => T.notify(o), tierWait()); }
      return;
    }
    clearTimeout(tierTimer);
    if (e.kind === 'fallback') {
      const f = { to: e.carrier || 'sentinel', from: e.from || (lastFallback && lastFallback.from) || 'vanguard' };
      if (e.instant) { if (T.notices.banners.has('fallback')) fallbackBanner(f); return; }
      tierTimer = setTimeout(() => fallbackBanner(f), tierWait());
    } else {
      // a real fallback switched back; instant (a replay or the first frame of a new epoch): the
      // banner goes, no "recovered" toast. quiet (the user's Gateway took over, rt/project.js;
      // 8th-wave ruling T-P4-H2 item 3): the banner goes and nothing is said either, the tier
      // under the Gateway is not known
      T.dismiss('fallback'); lastFallback = null;
      if (e.instant || e.quiet) return;
      const o = recoverToast(e, 'other');
      tierTimer = setTimeout(() => T.notify(o), tierWait());
    }
  });
  // 总览's strip and 配置's mode card show the result on the control itself: no second word from the shell
  T.on('rt:mode', e => { if (!e.instant && !e.busy && !onOverview() && T.router.current !== 'settings') T.notify({ kind: 'info', message: T.t('shell.notice.mode_switched', { mode: modeLabel(e.mode) }) }); });

  /* ------------------------------------------------ sidebar footer: version + update state (A9–A13, §3.4)
     The version is the running app's: Update.current_version, else hello.app_version (the same
     fact as app.info.version, bridge_main APP_VERSION). The update word follows update.state
     (store `update`, rt/project.js): checking · found (with the version) · dl (no percentage,
     B-30(c)) · verify (downloaded and verified: ready to install) · ready (installing) · fail ·
     latest; updated when Update.previous_version says this start is a finished update (C1 item 7);
     disabled when the build has no in-app update (Update.enabled false); unknown while no
     update.state has arrived (nothing is claimed). A click goes to 配置 · 应用. */
  const foot = T.$('.side-foot');
  const FOOT_STATES = ['found', 'fail', 'dl', 'verify', 'ready', 'checking', 'latest'];
  function footModel() {
    const u = S.get('update');
    const d = raw('update.state');
    const version = (d && typeof d.current_version === 'string' && d.current_version) || S.get('appVersion') || '';
    let state;
    if (!u || !d) state = 'unknown';
    else if (d.enabled === false) state = 'disabled';
    else if (u.state === 'latest' && typeof d.previous_version === 'string' && d.previous_version) state = 'updated';
    else state = FOOT_STATES.includes(u.state) ? u.state : 'unknown';
    if (state === 'found' && !u.latest) state = 'found_nover';
    return { version, state, latest: (u && u.latest) || '' };
  }
  function syncFoot(animate) {
    if (!foot) return;
    const m = footModel();
    if (!foot._built) {
      foot._built = true;
      // (the product name and the version are data: translate="no")
      foot.innerHTML = '<span class="ver" translate="no"></span><button class="foot-upd" type="button" data-go="settings" data-go-focus="update"></button>';
    }
    const ver = foot.querySelector('.ver'), st = foot.querySelector('.foot-upd');
    // (in a narrow sidebar the product name yields to the news: 0.1.0 · 有新版本 0.2.0)
    // (text only, no markup: the product-name prefix is put in front of the new version node)
    M.swapText(ver, m.version || 'Tesseract', { instant: !animate });
    const sws = ver.querySelectorAll('.sw'), cur = sws[sws.length - 1];
    if (m.version && cur && !cur.querySelector('.ver-pre')) cur.prepend(T.h('span', { class: 'ver-pre' }, 'Tesseract '));
    foot.classList.toggle('has-news', !['latest', 'unknown', 'disabled'].includes(m.state));
    const word = T.t('shell.foot.update.label', { state: m.state, version: m.latest });
    M.swapText(st, word, { instant: !animate });
    st.classList.toggle('is-found', m.state === 'found' || m.state === 'found_nover');
    st.classList.toggle('is-fail', m.state === 'fail');
    st.classList.toggle('is-busy', ['checking', 'dl', 'verify', 'ready'].includes(m.state));
    st.setAttribute('aria-label', T.t('shell.foot.update.aria', { status: word }));
  }
  S.sub('update', () => syncFoot(true));
  S.sub('appVersion', () => syncFoot(false));
  // the words that stay on screen follow the language (ruling 4th wave T-P4-03 #2: i18n:change)
  T.on('i18n:change', () => { syncShell(); syncFoot(false); });

  /* ------------------------------------------------ command palette: the service commands (P4–P9, S12, S18)
     A command the runtime does not offer (hello.caps) stays listed and disabled (never inferred);
     so does one the state does not allow. Errors go to the shell's generic error banner. */
  const gated = mode => !!(D.modes[mode] && D.modes[mode].admin && S.get('admin') === false);
  function askMode(mode) {
    if (gated(mode) || !can('mode.set')) return;
    if (S.get('run') !== 'running') { run('mode.set', { mode }); return; }
    // one confirmation for every place a mode is switched while running (mode.switch.confirm.*)
    T.confirm({ title: T.t('mode.switch.confirm.title', { label: modeLabel(mode) }), message: T.t('mode.switch.confirm.body', { mode }), confirmLabel: T.t('mode.switch.confirm.ok') })
      .then(ok => { if (ok) run('mode.set', { mode }); });
  }
  const modeKnown = () => S.get('modeKnown') !== false && S.get('mode') != null;
  const modeHint = mode => (modeKnown() && S.get('mode') === mode ? T.t('shell.palette.hint.current') : gated(mode) ? T.t('shell.palette.hint.admin') : '');
  const runState = () => S.get('run') || 'unknown';
  const cleanup = () => !!(S.get('core') && S.get('core').cleanup_pending);
  // what the start / stop command does now: stop while running (or while a cleanup is pending),
  // cancel while starting, start otherwise (failed included: 启动服务 is the retry)
  const toggleAction = () => {
    const r = runState();
    if (r === 'running' || (cleanup() && r !== 'starting')) return 'stop';
    if (r === 'starting') return 'cancel';
    return 'start';
  };
  const toggleCmd = () => (toggleAction() === 'start' ? 'service.start' : 'service.stop');
  const stopArgs = () => { const s = raw('service.state'); const g = s && s.intent && s.intent.runtime_generation; return Number.isInteger(g) ? { expected_generation: g } : {}; };
  // the exit address that may be copied: a current, verified IPv4 that is not in conflict,
  // rotating or out of date (G07, mapping_core P7 / S18)
  const UNSURE_EXIT = ['identity_conflict', 'identity_rotating', 'identity_stale'];
  function copyableIp() {
    if (runState() !== 'running') return null;
    const st = S.get('stale') || {};
    if (st['exit.identity']) return null;
    const d = raw('exit.identity');
    const cur = d && d.current;
    if (!cur || (cur.status !== 'ok' && cur.status !== 'partial') || cur.fresh === false) return null;
    if (UNSURE_EXIT.includes(cur.reason_code)) return null;
    const v4 = cur.v4 || {};
    return v4.status === 'ok' && typeof v4.address === 'string' && v4.address ? v4.address : null;
  }
  const exitBusy = () => { const e = S.get('exit'); return !!(e && e.state === 'pending'); };
  // the words of a command (read when the palette opens: they follow the language)
  const cmdLabel = c => () => T.t(`shell.palette.cmd.${c}`);
  // keywords: fixed English ids (every language) + the language's own words (pinyin only in zh_CN)
  const kw = (c, ids) => () => `${ids} ${T.t(`shell.palette.keywords.${c}`)}`;
  T.cmd.register([
    { id: 'svc-toggle', group: 'service', label: () => T.t('shell.palette.cmd.svc_toggle', { action: toggleAction() }), icon: () => (toggleAction() === 'start' ? 'start' : 'stop'),
      keywords: kw('svc_toggle', 'start stop'), mutates: true,
      disabled: () => { const r = runState(); return r === 'unknown' || r === 'stopping' || !!S.get('cancelling') || !can(toggleCmd()); },
      run: () => (toggleAction() === 'start' ? run('service.start', {}) : run('service.stop', stopArgs())) },
    { id: 'svc-restart', group: 'service', label: cmdLabel('svc_restart'), icon: 'restart', keywords: kw('svc_restart', 'restart'), mutates: true,
      disabled: () => runState() !== 'running' || !!S.get('restarting') || !can('service.restart'), run: () => run('service.restart', {}) },
    // (runs: it leaves the page and starts something, so it is never the pre-selected command)
    { id: 'svc-exit', group: 'service', label: cmdLabel('svc_exit'), icon: 'globe', keywords: kw('svc_exit', 'exit ip refresh'), runs: true,
      disabled: () => runState() !== 'running' || exitBusy() || !can('exit.verify'),
      run: () => { T.go('overview'); run('exit.verify', { trigger: 'manual' }); } },
    { id: 'svc-copy', group: 'service', label: cmdLabel('svc_copy'), hint: () => copyableIp() || '', icon: 'copy', keywords: kw('svc_copy', 'copy ip'), disabled: () => !copyableIp(),
      run: () => { const ip = copyableIp(); if (ip) T.ui.copyText(ip).then(() => T.notify({ kind: 'success', message: T.t('shell.palette.copied', { ip }) })); } },
    // (nav: the page it goes to; the palette opens on the first page that is not the one shown)
    { id: 'go-overview', group: 'go', nav: 'overview', label: cmdLabel('go_overview'), icon: 'overview', hint: 'Ctrl 1', keywords: kw('go_overview', 'overview'), run: () => T.go('overview') },
    { id: 'go-tiers', group: 'go', nav: 'profiles', label: cmdLabel('go_profiles'), icon: 'tiers', hint: 'Ctrl 2', keywords: kw('go_profiles', 'tiers'), run: () => T.go('profiles') },
    { id: 'go-logs', group: 'go', nav: 'logs', label: cmdLabel('go_logs'), icon: 'logs', hint: 'Ctrl 3', keywords: kw('go_logs', 'logs'), run: () => T.go('logs') },
    { id: 'go-settings', group: 'go', nav: 'settings', label: cmdLabel('go_settings'), icon: 'settings', hint: 'Ctrl 4', keywords: kw('go_settings', 'settings'), run: () => T.go('settings') },
    // 全局 and 混合 need administrator rights: without them (network.mode.admin false) the command
    // stays listed, disabled, and says why (D7: wording only, no elevation button)
    { id: 'mode-system', group: 'mode', label: cmdLabel('mode_system'), icon: 'mode-system', hint: () => modeHint('system'), keywords: kw('mode_system', 'mode system proxy'), mutates: true,
      disabled: () => !can('mode.set') || (modeKnown() && S.get('mode') === 'system') || !!S.get('modeBusy'), run: () => askMode('system') },
    { id: 'mode-tun', group: 'mode', label: cmdLabel('mode_tun'), icon: 'mode-tun', hint: () => modeHint('tun'), keywords: kw('mode_tun', 'mode tun global'), mutates: true,
      disabled: () => !can('mode.set') || (modeKnown() && S.get('mode') === 'tun') || !!S.get('modeBusy') || gated('tun'), run: () => askMode('tun') },
    { id: 'mode-mixed', group: 'mode', label: cmdLabel('mode_mixed'), icon: 'mode-mixed', hint: () => modeHint('mixed'), keywords: kw('mode_mixed', 'mode mixed'), mutates: true,
      disabled: () => !can('mode.set') || (modeKnown() && S.get('mode') === 'mixed') || !!S.get('modeBusy') || gated('mixed'), run: () => askMode('mixed') },
  ]);
  /* ------------------------------------------------ keyboard */
  const PAGES = ['overview', 'profiles', 'logs', 'settings'];
  document.addEventListener('keydown', e => {
    const k = e.key.toLowerCase();
    if ((e.ctrlKey || e.metaKey) && !e.altKey && k === 'k') { e.preventDefault(); if (!T.overlayOpen()) T.cmd.toggle(); return; }
    if ((e.ctrlKey || e.metaKey) && !e.altKey && ['1', '2', '3', '4'].includes(k)) {
      e.preventDefault();
      if (T.cmd.isOpen) T.cmd.close();
      if (!T.overlayOpen()) T.go(PAGES[+k - 1]);
      return;
    }
    // an Esc that dismisses a toast is used up: page listeners later in the chain see defaultPrevented
    if (e.key === 'Escape' && !e.defaultPrevented && !T.cmd.isOpen && !T.overlayOpen() && T.notices.toast) { e.preventDefault(); T.dismissToast(); }
  });
  T.$('#cmd-open').addEventListener('click', () => T.cmd.open());
  T.letter = installLetter(T);
  // holding Ctrl for a moment shows all four page hints in the sidebar (a quick Ctrl K / Ctrl 1 doesn't flash them)
  const navEl = T.$('#nav');
  let keysT = 0;
  const keysOff = () => { clearTimeout(keysT); keysT = 0; if (navEl) navEl.classList.remove('is-keys'); };
  document.addEventListener('keydown', e => {
    if (e.key === 'Control' && !e.repeat && !e.altKey && !e.shiftKey) { clearTimeout(keysT); keysT = setTimeout(() => { if (navEl) navEl.classList.add('is-keys'); }, 450); }
    else if (e.key !== 'Control' && !['1', '2', '3', '4'].includes(e.key)) keysOff();
  });
  document.addEventListener('keyup', e => { if (e.key === 'Control') keysOff(); });
  window.addEventListener('blur', keysOff);

  /* ------------------------------------------------ boot the router */
  syncShell();
  syncFoot(false);
  T.router.start('overview');
})();
