// Navigation between the shell and the page (T-P4-06; P4 architecture section 2.9, mapping_core
// R2, R5, R9; PROTOCOL section 13 host:navigate / host_set_page).
//
// host:navigate{page} (a click on a Windows notification; the window may not be visible yet,
// nothing here assumes it is):
//   overview -> T.go('overview')
//   alerts   -> T.go('settings', {focus: 'alerts'})
//   update   -> T.go('settings', {focus: 'update'})
// The settings page lands on the section itself (T-P4-05d). The same mapping turns a host:notify
// target into its "View" action (notify.js).
// A host:navigate while the page is busy (a modal dialog or drawer is open, or a draft is
// registered: busy()) is dropped, as the shell's own Ctrl+1...4 are (core/app.js): T.go would close
// the overlay without asking its beforeClose and lose the draft. The drop is noted
// (diag {kind:'navigate_held', page}); the window still comes to the front (the shell's part).
//
// host_set_page{page}: the routed page for ui.presence.page, whitelist
// overview|nodes|settings|logs|alerts|about|none. Router ids map as profiles -> nodes,
// settings with focus alerts -> alerts, overview / logs / settings as they are, anything else
// -> none. Reported on every page change once the page has a connection (a shell target that
// stays on the same page, e.g. settings -> alerts, included: goTarget), and again on every
// page connection, right after its host_notify_ready (a reloaded or reconnected page is "none"
// at the shell until it names its page again); a page change before the first connection is
// named by that connection. Errors are silent.

export const NAV_TARGETS = Object.freeze({
  overview: Object.freeze({ page: 'overview', params: Object.freeze({}) }),
  alerts: Object.freeze({ page: 'settings', params: Object.freeze({ focus: 'alerts' }) }),
  update: Object.freeze({ page: 'settings', params: Object.freeze({ focus: 'update' }) }),
});
export const PRESENCE_PAGES = Object.freeze(['overview', 'nodes', 'settings', 'logs', 'alerts', 'about', 'none']);

// router id (+ params) -> ui.presence.page
export function presencePage(id, params) {
  if (id === 'profiles' || id === 'tiers') return 'nodes';
  if (id === 'settings') return params && params.focus === 'alerts' ? 'alerts' : 'settings';
  if (id === 'overview' || id === 'logs') return id;
  return 'none';
}

// Ports: go(page, params) (T.go), host(name, args) -> Promise, current() -> {id, params} | null,
// busy() -> bool (an overlay is open or a draft is registered), diag(entry).
export function createNavigate({ go, host, current = () => null, busy = () => false, diag = () => {} }) {
  let reported = null;
  let last = null; // the latest page:change {id, params}
  let live = false; // a page connection has been declared (ready.js)

  function report(page, force) {
    if (!PRESENCE_PAGES.includes(page)) page = 'none';
    if (!force && page === reported) return;
    reported = page;
    Promise.resolve(host('host_set_page', { page })).catch(() => {});
  }

  // go a host target; goTarget.can(target) says whether the target is known. A go inside the
  // same page (settings -> settings{focus:'alerts'}) runs enter() again without a page:change
  // (core.js T.go), so the page it landed on is named here (host_set_page; deduplicated against
  // the page:change of a real page change; nothing when the go was held: false). T-P4-06 review
  // cont. 1 SHOULD-4, 8th-wave ruling T-P4-H2 item 2.
  function goTarget(target) {
    const t = Object.prototype.hasOwnProperty.call(NAV_TARGETS, target) ? NAV_TARGETS[target] : null;
    if (!t) return false;
    const params = Object.assign({}, t.params);
    const went = go(t.page, params);
    if (went !== false) {
      const c = current();
      last = c && c.id ? { id: c.id, params: c.params || {} } : { id: t.page, params };
      if (live) report(presencePage(last.id, last.params));
    }
    return true;
  }
  goTarget.can = target => Object.prototype.hasOwnProperty.call(NAV_TARGETS, target);

  return {
    goTarget,
    // host:navigate{page}
    navigate(f) {
      if (!f || typeof f.page !== 'string') return false;
      if (goTarget.can(f.page) && busy()) { diag({ kind: 'navigate_held', page: f.page }); return false; }
      return goTarget(f.page);
    },
    // page:change {id, params}
    pageChanged(e) {
      if (!e || typeof e.id !== 'string') return;
      last = { id: e.id, params: e.params || {} };
      if (live) report(presencePage(e.id, e.params));
    },
    // a new page connection: name the page again (before the router has shown a page there is
    // nothing to name yet: the first page:change reports it)
    connected() {
      live = true;
      const c = last || current();
      if (!c) { reported = null; return; }
      report(presencePage(c.id, c.params), true);
    },
    get reported() { return reported; },
  };
}
