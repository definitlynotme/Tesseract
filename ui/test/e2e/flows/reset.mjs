// Flow "reset": a generation change, rt:reset with a new epoch (P4 architecture section 2.3;
// shell/README T-P3-04 crash restart). A page command is left in flight (the stand-in accepts
// exit.verify and never finishes it), then the stand-in sidecar exits without bye (code 3): the
// shell sends rt:reset{runtime_gone}, restarts the sidecar in the stopped state and replays the new
// epoch. Asserted: the in-flight call ends with runtime_gone; the page gets
// rt:reset{generation_changed:true}; the brand dot goes back to unknown (the skeleton state, with
// the skeleton placeholders counted) and then shows the new epoch's state (stopped); the shell's
// sidecar_crashed banner goes restarting -> restarted; the epoch changed and a second stand-in
// launch answered.
import { check, sleep, waitFor } from '../driver.mjs';

export const real = false;
export const skipReal = 'needs the stand-in sidecar (a scripted sidecar crash)';
export const launches = [{ args: [], env: {}, options: { phase: 'running', hold: ['exit.verify'] } }];

export async function run(s) {
  const c = s.checks;
  await s.reloadObserved();
  await s.until(() => document.getElementById('app').dataset.run === 'running', 'running', 15000);
  const before = await s.eval(() => ({ epoch: window.T.rt.epoch, runs: window.__e2e.runs.length, events: window.__e2e.events.length }));
  c.epoch_before = before.epoch;
  await s.eval(() => {
    window.__e2eCall = { state: 'pending' };
    window.T.rt.call('exit.verify', { trigger: 'manual' }).then(r => { window.__e2eCall = { state: 'resolved', r }; }, e => { window.__e2eCall = { state: 'rejected', code: e && e.code }; });
  });
  await waitForRequest(s, 'exit.verify');
  await sleep(300);
  check((await s.eval(() => window.__e2eCall.state)) === 'pending', 'exit.verify was not in flight');
  await s.control({ op: 'crash', code: 3 });
  await s.until(() => window.__e2eCall.state !== 'pending', 'the in-flight call ends', 15000);
  c.in_flight = await s.eval(() => window.__e2eCall);
  check(c.in_flight.state === 'rejected' && c.in_flight.code === 'runtime_gone', `in-flight call: ${JSON.stringify(c.in_flight)}`);
  await s.until(e => window.T.rt.state === 'live' && window.T.rt.epoch !== e, 'a new epoch', 30000, before.epoch);
  await s.until(() => document.getElementById('app').dataset.run === 'stopped', 'the new epoch shows stopped', 15000);
  await s.until(n => window.__e2e.events.slice(n).some(e => e.e === 'host:banner' && e.state === 'restarted'), 'sidecar_crashed restarted', 15000, before.events);
  await sleep(500);
  const after = await s.eval(b => ({
    epoch: window.T.rt.epoch,
    resets: window.__e2e.events.slice(b.events).filter(e => e.e === 'rt:reset').map(e => ({ reason: e.reason, generation_changed: e.generation_changed, epoch: e.epoch })),
    banners: window.__e2e.events.slice(b.events).filter(e => e.e === 'host:banner').map(e => ({ kind: e.kind, state: e.state })),
    runs: window.__e2e.runs.slice(b.runs),
  }), before);
  c.after = after;
  c.fixture_launches = s.fixtureLaunches();
  check(after.epoch !== before.epoch, 'the epoch did not change');
  check(after.resets.some(r => r.generation_changed === true), `no rt:reset{generation_changed:true}: ${JSON.stringify(after.resets)}`);
  const unknownAt = after.runs.findIndex(r => r.run === 'unknown');
  check(unknownAt >= 0, `the brand dot never went back to unknown: ${JSON.stringify(after.runs)}`);
  check(after.runs.slice(unknownAt).some(r => r.run === 'stopped'), `no recovery after the skeleton: ${JSON.stringify(after.runs)}`);
  c.skeleton_placeholders_at_unknown = after.runs[unknownAt].skel;
  check(after.banners.some(b => b.kind === 'sidecar_crashed' && b.state === 'restarting'), `no sidecar_crashed restarting banner: ${JSON.stringify(after.banners)}`);
  check(c.fixture_launches >= 2, 'the stand-in sidecar was not restarted');
  await s.shot('new-epoch');
}

function waitForRequest(s, cmd) {
  return waitFor(() => s.requests().some(r => r.cmd === cmd), `${cmd} reached the sidecar`, 10000);
}
