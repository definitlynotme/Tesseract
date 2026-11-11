// Connect-time declarations and the ready signal (T-P4-06; P4 architecture section 2.9,
// mapping_core N4, Q2; PROTOCOL section 13 host_notify_ready / host_ready; criticism B1).
//
// Every page connection (each replay starts with rt:reset, a same-epoch reconnect and a reload
// included) first declares host_notify_ready (this connection renders host:notify and sends
// receipts: notify.js, which is in this ticket with its tests, 5th-wave ruling 31b-3), then names
// its page (navigate.js connected()). A page connection is one rt_connect (the shell's
// page_generation): the page load, and each rt:conn connecting / reconnecting; its first rt:reset
// declares. A later rt:reset in the same connection (rt:reset{runtime_gone}: a new sidecar
// epoch) is no new page connection and declares nothing again; an rt:reset{reconnect} always
// is one (the shell sends it only at the start of a connection's replay). An rt:caps (hello) with no
// rt:reset since the last connect / reconnect counts as a connection too (a shell that sends no
// rt:reset first).
//
// host_ready: once per page load, unconditionally (never gated by T.rt.hostHas: without
// host:caps that is always false and the window would wait the full 1.5 s, criticism B1), in the
// frame after both the first service.state snapshot is in the store and the current page has
// run enter() (the router's first page:change), or (11-05 window-first launch, app target) after
// the launch screen is shown while the runtime is still starting (launch.js; the caller runs
// check() then). Any error (no such command, not_allowed) is silent.
//
// Ports: host(name, args) -> Promise; onConnected() (navigate + notify bookkeeping);
// hasSnapshot() -> bool; onSnapshot(fn) -> off; pageEntered() -> bool; onPage(fn) -> off;
// frame(fn) (requestAnimationFrame; the next frame); launchShown() -> bool (default false).

export function createReady({ host, onConnected = () => {}, hasSnapshot, onSnapshot, pageEntered, onPage, frame, launchShown = () => false }) {
  let connects = 0, fresh = true, readyCalled = false, scheduled = false;

  function connected() {
    connects++;
    fresh = false;
    Promise.resolve(host('host_notify_ready')).catch(() => {});
    onConnected();
  }

  function maybeReady() {
    if (readyCalled || scheduled) return;
    if (!launchShown() && (!hasSnapshot() || !pageEntered())) return;
    scheduled = true;
    frame(() => {
      readyCalled = true;
      Promise.resolve(host('host_ready')).catch(() => {});
    });
  }

  const offs = [onSnapshot(maybeReady), onPage(maybeReady)];

  return {
    // rt:reset (the first frame of every replay): only the first one of a page connection.
    // rt:reset{reason:'reconnect'} is always a new page connection: the shell sends it only at
    // the start of a connect()'s replay (transport.rs replay), so a second one while the client
    // is still reconnecting (rt:conn does not repeat an unchanged state) declares again
    // (T-P4-06 review cont. 1 SHOULD-3, 8th-wave ruling T-P4-H2 item 2)
    reset(e) { if (fresh || (e && e.reason === 'reconnect')) connected(); },
    // rt:conn {state}: a connect / reconnect starts; the next rt:reset or hello is a new connection
    conn(e) { if (e && (e.state === 'connecting' || e.state === 'reconnecting')) fresh = true; },
    // rt:caps (hello): a connection whose replay did not start with rt:reset
    hello() { if (fresh) connected(); },
    check: maybeReady,
    get connects() { return connects; },
    get readyCalled() { return readyCalled; },
    dispose() { offs.forEach(off => { if (typeof off === 'function') off(); }); },
  };
}
