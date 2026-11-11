// Flow "reconnect": a same-epoch reconnect (P4 architecture section 2.3; criticism B9).
// The page is made to call rt_connect again through its own recovery path: a second Channel is
// attached from the test (rt_connect with a throw-away Channel), so the shell's replay goes there
// and the page's own Channel falls silent while the shell's rt:reset Tauri event still arrives; the
// page's TauriTransport sees no Channel copy within 1.5 s, calls it stuck and reconnects
// (transport-tauri.js listenResetEvent -> reconnect('channel_stuck')), getting the full same-epoch
// replay. Asserted: rt:reset{reason:reconnect, generation_changed:false}; the brand dot never
// turns grey (no data-run=unknown, no data-stale) during the whole reconnect; the log view is not
// cleared and gets no restart separator (.logs-restart); the startup-failure banner (an epoch
// banner, banners.js EPOCH_BANNERS) stays up; the epoch is unchanged; the page declares the new
// connection to the shell again (host_notify_ready).
// None of these is judged on the end state alone (fix round 1): a same-epoch replay re-runs
// banners.js syncStartup and refills the log view from the replayed tail, so "taken down, then put
// back" or "cleared, then refilled" would look the same at the end. So the banner must be the very
// same handle (core.js banner(): every put of an id makes a new handle and element) still in the
// document, and a MutationObserver started before the trigger samples #logs-total on every DOM
// change (and the old values of every change inside it) - its lowest value must stay >= the
// count before the reconnect.
import { bannerUp, check, goPage, logsTotal, sleep, waitFor } from '../driver.mjs';

export const real = false;
export const skipReal = 'needs the stand-in sidecar (a scripted start failure keeps an epoch banner up)';
export const launches = [{ args: [], env: {}, options: { phase: 'failed' } }];

export async function run(s) {
  const c = s.checks;
  await s.reloadObserved();
  await s.until(() => window.__e2e.events.some(e => e.e === 'rt:replayed'), 'replayed', 20000);
  await goPage(s, 'logs');
  await s.control({ op: 'logs', n: 20 });
  await waitFor(async () => (await logsTotal(s)) >= 20, 'log lines on the page', 10000);
  await s.until(() => window.T.notices.banners.has('startup'), 'startup failure banner', 10000);
  const before = {
    epoch: await s.eval(() => window.T.rt.epoch), total: await logsTotal(s),
    separators: await s.eval(() => document.querySelectorAll('.logs-restart').length),
    readies: await s.eval(() => window.T.shellc.diag().filter(d => d.kind === 'host' && d.name === 'host_notify_ready').length),
    run: await s.eval(() => document.getElementById('app').dataset.run),
  };
  c.before = before;
  await s.shot('before-reconnect');
  // the same banner handle and the log count over the whole reconnect (not only at the end)
  await s.eval(() => {
    window.__e2eBanner = window.T.notices.banners.get('startup');
    const num = txt => { const n = (String(txt || '').replace(/[,.\s\u00a0\u202f']/g, '').match(/\d+/g) || []).map(Number); return n.length ? Math.max(...n) : 0; };
    const inTotal = n => { const e = n && (n.nodeType === 1 ? n : n.parentElement); return !!(e && e.closest && e.closest('#logs-total')); };
    const tot0 = document.getElementById('logs-total');
    const w = window.__e2eLogWatch = { min: tot0 ? num(tot0.textContent) : 0, start: tot0 ? num(tot0.textContent) : 0, samples: 0, missing: 0, low_at: null };
    const note = (v, why) => { w.samples++; if (v < w.min) { w.min = v; w.low_at = why; } };
    w.mo = new MutationObserver(recs => {
      for (const r of recs) {
        if (r.type === 'characterData' && inTotal(r.target)) note(num(r.oldValue), 'old text');
        else if (r.type === 'childList' && inTotal(r.target) && r.removedNodes.length) note(num(Array.from(r.removedNodes).map(n => n.textContent).join('')), 'removed nodes');
      }
      const tot = document.getElementById('logs-total');
      if (!tot) { w.missing++; note(0, 'footer gone'); } else note(num(tot.textContent), 'current');
    });
    w.mo.observe(document.body, { subtree: true, childList: true, characterData: true, characterDataOldValue: true });
  });
  // The transport treats a Tauri-event rt:reset within 1.5 s of its Channel's own rt:reset as that
  // Channel's copy: start the stray connect well after the page's last replay.
  await s.until(() => { const r = window.__e2e.events.filter(e => e.e === 'rt:reset').pop(); return !r || performance.now() - (r.at + (window.__e2e.t0p || 0)) > 2500; }, 'quiet after the last replay', 10000);
  const mark = await s.eval(() => { const n = window.__e2e.runs.length; const e = window.__e2e.events.length;
    const ch = new window.__TAURI__.core.Channel(); ch.onmessage = () => {};
    window.__e2eStray = ch; window.__TAURI__.core.invoke('rt_connect', { channel: ch }); return { runs: n, events: e }; });
  await s.until(m => window.__e2e.events.slice(m.events).some(e => e.e === 'rt:conn' && e.state === 'reconnecting'), 'the page reconnects (channel stuck)', 10000, mark);
  await s.until(m => window.__e2e.events.slice(m.events).some(e => e.e === 'rt:reset') && window.T.rt.state === 'live', 'same-epoch replay', 15000, mark);
  await s.until(m => window.__e2e.events.slice(m.events).some(e => e.e === 'rt:replayed'), 'rt:replayed of the reconnect', 15000, mark);
  await sleep(1500);
  const after = await s.eval(m => ({
    events: window.__e2e.events.slice(m.events).filter(e => /^rt:(conn|reset|replayed)$/.test(e.e)),
    runs: window.__e2e.runs.slice(m.runs),
    epoch: window.T.rt.epoch,
    separators: document.querySelectorAll('.logs-restart').length,
    readies: window.T.shellc.diag().filter(d => d.kind === 'host' && d.name === 'host_notify_ready').length,
    run: document.getElementById('app').dataset.run,
  }), mark);
  after.total = await logsTotal(s);
  after.banner = await bannerUp(s, 'startup');
  after.same_banner = await s.eval(() => { const b = window.T.notices.banners.get('startup'); return !!(b && b === window.__e2eBanner && window.__e2eBanner.el && window.__e2eBanner.el.isConnected); });
  after.log_watch = await s.eval(() => { const w = window.__e2eLogWatch; w.mo.disconnect(); return { start: w.start, min: w.min, samples: w.samples, missing: w.missing, low_at: w.low_at }; });
  c.after = after;
  const reset = after.events.find(e => e.e === 'rt:reset');
  check(reset.reason === 'reconnect' && reset.generation_changed === false, `rt:reset ${JSON.stringify(reset)}`);
  check(after.epoch === before.epoch, 'the epoch changed');
  check(after.runs.every(r => r.run !== 'unknown' && !r.stale), `the brand dot flashed: ${JSON.stringify(after.runs)}`);
  check(after.run === before.run, `brand run ${before.run} -> ${after.run}`);
  check(after.total >= before.total, `the log view was cleared: ${before.total} -> ${after.total}`);
  check(after.log_watch.samples > 0, 'the log count was never sampled during the reconnect');
  check(after.log_watch.min >= before.total && after.log_watch.missing === 0,
    `the log view was cleared during the reconnect (then refilled): lowest count ${after.log_watch.min} (${after.log_watch.low_at}) < ${before.total}`);
  check(after.separators === before.separators, `a restart separator was inserted: ${before.separators} -> ${after.separators}`);
  check(after.banner, 'the startup-failure banner was withdrawn');
  check(after.same_banner, 'the startup-failure banner was taken down and put back during the reconnect (a new handle)');
  check(after.readies > before.readies, 'the page did not declare the new connection (host_notify_ready)');
  await s.shot('after-reconnect');
}
