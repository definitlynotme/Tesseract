// New-version banner and "update finished" notice (T-P4-06; P4 architecture section 2.9;
// mapping_core A12, section 3.4; G11, G51; criticism D1; PROTOCOL section 16 Update).
//
// update.state.state = 'available' with a version, the first time that version is seen: one
// info banner (kind info + banner: no level word, F-01) with two actions, "View update" -> T.go('settings', {focus: 'update'}) and
// "Update now" -> app.update.install (after a confirmation: installing quits the app). The second
// action is offered only when T.rt.can('app.update.install') and the Update does not say it cannot
// be installed in the app (installable === false, or enabled === false). Each version is
// announced once: until the backend's "already announced" fact (B-30(b)) reaches the page, the
// page remembers the announced versions in WebView localStorage (every read / write in
// try/catch; when storage fails, once per page session). The banner goes when the update is no
// longer available (a newer state, a check that found nothing) or the user closes it.
//
// Update.previous_version (C1 item 7, shown as soon as the field is there): a one-time success
// notice "updated from {previous} to {current}", remembered per current version the same way.
//
// A banner that left the screen in any way (closed, "View update", installing, T.dismiss) is
// not shown again for its version: a language switch rewords it only while it is on screen.
//
// Ports: notify(o), dismiss(id), t(id, params), can(cmd), call(cmd, args) -> Promise,
// confirm(o) -> Promise<bool> (T.confirm), go(page, params), storage ({getItem, setItem}) or
// null, commandError(err), onScreen(id) -> bool (T.notices.banners.has(id); default: always
// true).

export const STORAGE_KEY = 'tesseract.shellc.update';
export const REMEMBER = 16;

function readStore(storage) {
  try {
    const raw = storage && storage.getItem(STORAGE_KEY);
    const v = raw ? JSON.parse(raw) : null;
    return { announced: Array.isArray(v && v.announced) ? v.announced.filter(x => typeof x === 'string') : [], done: Array.isArray(v && v.done) ? v.done.filter(x => typeof x === 'string') : [] };
  } catch (e) { return null; }
}

export function createUpdateNotice({ notify, dismiss, t, can = () => false, call, confirm, go, storage = null, commandError = () => {}, onScreen = () => true }) {
  const session = { announced: new Set(), done: new Set() };
  let shownFor = null;
  // the banner left the screen (its "View update" action has no keep, a timeout, T.dismiss): forget it
  const settle = () => { if (shownFor && !onScreen('update')) shownFor = null; return shownFor; };

  function remembered(list, v) {
    if (session[list].has(v)) return true;
    const s = readStore(storage);
    return !!(s && s[list].includes(v));
  }
  function remember(list, v) {
    session[list].add(v);
    try {
      const s = readStore(storage) || { announced: [], done: [] };
      if (!s[list].includes(v)) s[list].push(v);
      s[list] = s[list].slice(-REMEMBER);
      if (storage) storage.setItem(STORAGE_KEY, JSON.stringify(s));
    } catch (e) { /* storage unavailable: this page session only */ }
  }

  const installable = d => can('app.update.install') && d.installable !== false && d.enabled !== false;

  function install(btn) {
    return Promise.resolve(confirm({ title: t('shell.update.confirm.title', { version: shownFor ? shownFor.version : '' }), message: t('shell.update.confirm.body'), confirmLabel: t('shell.update.confirm.ok') }))
      .then(ok => {
        if (!ok) return false;
        dismiss('update');
        return Promise.resolve(call('app.update.install', {})).then(() => true, err => { commandError(err); return false; });
      });
  }

  function banner(d) {
    shownFor = { version: d.version, current: d.current_version || null, installable: installable(d) };
    const o = () => {
      const actions = [{ label: t('shell.update.view'), keep: false, run: () => go('settings', { focus: 'update' }) }];
      if (shownFor.installable) actions.push({ label: t('shell.update.install'), primary: true, keep: true, run: install });
      // (F-01: a new version is information, not a warning: a banner without a level word)
      return { id: 'update', kind: 'info', banner: true, title: t('shell.update.found.title', { version: shownFor.version }),
        message: t('shell.update.found.body', { version: shownFor.version, current: shownFor.current || '', has_current: shownFor.current ? 'yes' : 'no' }),
        actions, onDismiss: () => { shownFor = null; } };
    };
    shownFor.build = o;
    notify(o());
  }

  return {
    // the raw update.state DTO (every complete value, replayed ones included)
    state(d) {
      if (!d || typeof d !== 'object') return;
      settle();
      if (d.state === 'available' && typeof d.version === 'string' && d.version) {
        if (shownFor && shownFor.version === d.version) {
          const inst = installable(d);
          if (inst !== shownFor.installable) { shownFor.installable = inst; notify(shownFor.build()); }
        } else if (!remembered('announced', d.version)) {
          remember('announced', d.version);
          banner(d);
        }
      } else if (shownFor && !d.update_available && d.state !== 'downloading' && d.state !== 'verified' && d.state !== 'handoff') {
        shownFor = null;
        dismiss('update');
      }
      const prev = typeof d.previous_version === 'string' && d.previous_version ? d.previous_version : null;
      const cur = typeof d.current_version === 'string' && d.current_version ? d.current_version : null;
      if (prev && cur && !remembered('done', cur)) {
        remember('done', cur);
        // (sticky: a catalog message, relabelled in place on a language switch)
        notify({ kind: 'success', sticky: true, message: t('shell.update.done', { previous: prev, current: cur }), messageId: 'shell.update.done', messageParams: { previous: prev, current: cur } });
      }
    },
    relocalize() { if (settle() && shownFor.build) notify(shownFor.build()); },
    install,
    get shownFor() { return settle() ? shownFor.version : null; },
  };
}
