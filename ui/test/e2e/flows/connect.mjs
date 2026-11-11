// Flow "connect": connection and replay (P4 architecture sections 2.3, 2.9; criticism B1; 5th-wave
// ruling C3-2).
//  1. First load (the window the shell created): the page's host_ready uncloaks the window before
//     the shell's 1.5 s timeout - shell.log "window_revealed reason=ready waited_ms=N" (N is
//     measured by the shell from the window's creation, webview.rs start_reveal), never
//     "window_reveal_timeout"; the page itself called host_ready (T.shellc diag) and shows the
//     real run state. The new epoch's seed ends with rt_seed_done; with the real sidecar it must be
//     reason=all (C3-2) and its ms are recorded.
//  2. A reload observed from its first frame (E2E_INIT): the brand dot (#app[data-run]) starts at
//     unknown and turns into the actual state once the first service.state arrives; host:caps (with
//     rt_resync / host_ready and the rt:replayed event) and rt:replayed reach the page, rt:replayed
//     after hello, and the reload runs the same rt_connect replay as any page connection.
import { check, sleep } from '../driver.mjs';

export const real = true;
export const launches = [{ args: [], env: {}, options: { phase: 'running' } }];

export async function run(s, _launch, real) {
  const c = s.checks;
  const expected = real ? 'stopped' : 'running';
  // 1. the first load
  const reveal = await s.logWait(/^(window_revealed|window_reveal_timeout) /, 'window reveal', 20000);
  c.reveal_line = reveal[0].text;
  const ready = await s.logWait(/^host_ready\b/, 'host_ready in shell.log', 20000);
  // shell.log times (ms): the window, the first service.state the hub accepted (the tray's first
  // status icon), the page's host_ready
  const at = re => { const l = s.logFind(re)[0]; return l ? l.ms : null; };
  const win = at(/^shell_window_ready$/), state = at(/^tray_icon_state icon=(stopped|starting|running|failed)/);
  c.timeline_ms = { first_state_after_window: state && win ? state - win : null, host_ready_after_window: win ? ready[0].ms - win : null,
    host_ready_after_first_state: state ? ready[0].ms - state : null };
  const m = /^window_revealed reason=ready waited_ms=(\d+) ok=true$/.exec(reveal[0].text);
  if (real && !m) {
    // The real source-mode sidecar takes longer than 1.5 s to its first service.state; the shell
    // then uncloaks by its timeout. The page's part: host_ready right after that first state.
    c.reveal_by_timeout = true;
    check(/^window_reveal_timeout /.test(reveal[0].text), `unexpected reveal line: ${reveal[0].text}`);
    check(c.timeline_ms.host_ready_after_first_state !== null && c.timeline_ms.host_ready_after_first_state < 500,
      `host_ready ${c.timeline_ms.host_ready_after_first_state} ms after the first service.state`);
  } else {
    check(m, `window uncloaked by the 1.5 s timeout instead of host_ready: ${reveal[0].text}`);
    c.reveal_waited_ms = Number(m[1]);
    check(c.reveal_waited_ms < 1200, `host_ready uncloaked after ${c.reveal_waited_ms} ms: not clearly before the 1.5 s timeout`);
  }
  c.first_load = await s.eval(() => ({
    run: document.getElementById('app').dataset.run,
    host_ready_called: window.T.shellc.diag().some(d => d.kind === 'host' && d.name === 'host_ready'),
    host_caps: window.T.store.get('hostCaps'),
    epoch: window.T.rt.epoch,
  }));
  check(c.first_load.host_ready_called, 'the page did not call host_ready');
  check(c.first_load.run === expected, `first load: brand run=${c.first_load.run}, expected ${expected}`);
  const seed = await s.logWait(/^rt_seed_done /, 'rt_seed_done', 30000);
  c.seed_line = seed[0].text;
  const sm = /reason=(\w+) slots=(\d+) missing=(\d+) first_missing=(\S+) ms=(\d+)/.exec(c.seed_line);
  check(sm, `unexpected rt_seed_done line: ${c.seed_line}`);
  c.seed = { reason: sm[1], slots: Number(sm[2]), missing: Number(sm[3]), ms: Number(sm[5]) };
  check(c.seed.reason === 'all', `seed ended by ${c.seed.reason} (missing ${c.seed.missing}, first ${sm[4]})`);
  await s.shot('first-load');

  // 2. a reload observed from its first frame
  await s.reloadObserved();
  await s.until(() => window.__e2e.events.some(e => e.e === 'rt:replayed'), 'rt:replayed after the reload', 20000);
  await s.until(x => document.getElementById('app').dataset.run === x, `brand run ${expected} after the reload`, 15000, expected);
  await sleep(300);
  const ev = await s.events();
  const names = ev.map(e => e.e);
  c.reload_order = names.filter(n => ['rt:reset', 'host:caps', 'rt:caps', 'rt:replayed'].includes(n));
  const i = n => names.indexOf(n);
  check(i('rt:reset') >= 0 && i('rt:reset') < i('host:caps') && i('host:caps') < i('rt:caps') && i('rt:caps') < i('rt:replayed'),
    `replay order rt:reset < host:caps < hello < rt:replayed broken: ${c.reload_order.join(',')}`);
  const reset = ev.find(e => e.e === 'rt:reset');
  c.reload_reset = { reason: reset.reason, generation_changed: reset.generation_changed };
  check(reset.reason === 'reconnect' && reset.generation_changed === false, `reload rt:reset ${JSON.stringify(c.reload_reset)}`);
  const caps = ev.find(e => e.e === 'host:caps');
  c.host_caps = { invokes: caps.invokes, events: caps.events };
  check(caps.invokes.includes('rt_resync') && caps.invokes.includes('host_ready') && caps.events.includes('rt:replayed'), 'host:caps lacks rt_resync / host_ready / rt:replayed');
  const runs = await s.runs();
  c.reload_runs = runs;
  check(runs.length >= 2 && runs[0].run === 'unknown', `brand dot did not start at unknown: ${JSON.stringify(runs)}`);
  check(runs[runs.length - 1].run === expected, `brand dot ended at ${runs[runs.length - 1].run}`);
  check(runs.every(r => r.run === 'unknown' || r.run === expected), `brand dot passed through another state: ${JSON.stringify(runs)}`);
  const firstState = ev.find(e => e.e === 'rt:state');
  c.reload_first_state_instant = firstState ? firstState.instant : null;
  check(firstState && firstState.instant === true, 'the replayed service.state was not instant (a replay must not light up)');
  c.reload_same_epoch = (await s.eval(() => window.T.rt.epoch)) === c.first_load.epoch;
  check(c.reload_same_epoch, 'the reload changed the epoch');
  await s.shot('after-reload');
  // (Claude supplement 10-13, recorded only) how often gateway.current flips stale on the page
  // (rt/stale.js: the page-internal rt:stale {topic, stale}). The ruling asks to watch it during
  // Tier3 appends; the T-P2-33 sandbox has Tier3 off, so there are no appends to watch here - the
  // count is the idle baseline of the real sidecar (10 s more after the reload) or the stand-in.
  if (real) await sleep(10000);
  // (fix round 1: each flip with its time from the page load and relative to the reload's
  // rt:replayed, negative = inside the replay, for T-P4-12 / P6 to compare against)
  c.gateway_current_stale = await s.eval(() => {
    const all = window.__e2e.events;
    const replayed = all.find(e => e.e === 'rt:replayed');
    const ev = all.filter(e => e.e === 'rt:stale' && String(e.topic).startsWith('gateway.current'));
    return { flips: ev.length, to_stale: ev.filter(e => e.stale === true).length, window_ms: Math.round(performance.now() - window.__e2e.t0p),
      replayed_at_ms: replayed ? replayed.at : null,
      timeline: ev.map(e => ({ stale: e.stale, at_ms: e.at, from_replayed_ms: replayed ? e.at - replayed.at : null })),
      now: !!(window.T.store.get('stale') || {})['gateway.current'], tier3_appends_possible: false };
  });
}
