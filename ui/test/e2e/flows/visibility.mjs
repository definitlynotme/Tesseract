// Flow "visibility": closed to the tray the page stops its work, and catches up when shown again
// (P4 architecture section 2.9 host:visibility, G57; shell/README T-P3-03: hidden -> WebView2
// controller IsVisible=false; core.js T.scopes). On 运行日志 with the service running (the
// stand-in sends service.state and traffic.tick every second, as the real sidecar does), the
// window is closed to the tray: presence and document are hidden and the page's scope timers are
// paused (T.scopes.paused). Two equal windows are measured while hidden with the CDP Performance
// counters: A quiet (heartbeats only), B with the stand-in pushing 50 log lines a second.
// Asserted: no animation frame in either; no layout at all; the log stream adds no style work
// (RecalcStyleCount of B not above A + 1); after the window is shown again the timers run again,
// frames are drawn and the log view holds every line sent meanwhile (data aligned).
// Known (T-P4-05a): window A is not zero - overview.js syncs its run overlay on every
// service.state and reads getComputedStyle(R.over) there (pages/overview.js:513), even when
// 总览 is not shown and the window is hidden: one forced style recalculation a second.
import { check, goPage, logsTotal, sleep, waitFor } from '../driver.mjs';

export const real = false;
export const skipReal = 'needs the stand-in sidecar (a scripted 50 lines/s log stream)';
export const launches = [{ args: [], env: {}, options: { phase: 'running' } }];
const WINDOW_MS = 5000;
const KEYS = ['LayoutCount', 'RecalcStyleCount', 'ScriptDuration', 'TaskDuration', 'Nodes'];

async function measure(s, ms) {
  const m0 = await s.metrics();
  await s.eval(() => { window.__e2eRaf = 0; if (!window.__e2eRafOn) { window.__e2eRafOn = true; const f = () => { window.__e2eRaf++; requestAnimationFrame(f); }; requestAnimationFrame(f); } });
  await sleep(ms);
  const m1 = await s.metrics();
  const d = Object.fromEntries(KEYS.map(k => [k, Math.round((m1[k] - m0[k]) * 1000) / 1000]));
  d.raf = await s.eval(() => window.__e2eRaf);
  return d;
}

export async function run(s) {
  const c = s.checks;
  await s.cdp.send('Performance.enable');
  await goPage(s, 'logs');
  await waitFor(async () => (await logsTotal(s)) >= 1, 'log lines on the page', 10000);
  const before = await logsTotal(s);
  c.total_before = before;
  s.native('close');
  await waitFor(() => !s.win().visible, 'window closed to the tray', 10000);
  await s.until(() => { const p = window.T.store.get('presence'); return p && p.visible === false && document.hidden; }, 'page presence and document hidden', 10000);
  c.scopes_paused = await s.eval(() => window.T.scopes.paused);
  check(c.scopes_paused === true, 'the page timers (T.scopes) are not paused while hidden');
  await sleep(1500);
  c.hidden_quiet = await measure(s, WINDOW_MS);
  await s.control({ op: 'log_rate', per_s: 50 });
  c.hidden_stream = await measure(s, WINDOW_MS);
  await s.control({ op: 'log_rate', per_s: 0 });
  c.window_ms = WINDOW_MS;
  check(c.hidden_quiet.raf === 0 && c.hidden_stream.raf === 0, `animation frames while hidden: ${c.hidden_quiet.raf} / ${c.hidden_stream.raf}`);
  check(c.hidden_quiet.LayoutCount === 0 && c.hidden_stream.LayoutCount === 0, `layouts while hidden: ${c.hidden_quiet.LayoutCount} / ${c.hidden_stream.LayoutCount}`);
  check(c.hidden_stream.RecalcStyleCount <= c.hidden_quiet.RecalcStyleCount + 1, `the 50 lines/s stream caused style work while hidden: ${c.hidden_stream.RecalcStyleCount} vs ${c.hidden_quiet.RecalcStyleCount} at rest`);
  await s.known('no_style_work_while_hidden', 'T-P4-05a', async () => check(c.hidden_quiet.RecalcStyleCount === 0, `RecalcStyleCount +${c.hidden_quiet.RecalcStyleCount} in ${WINDOW_MS} ms hidden at rest (overview.js:513 getComputedStyle on every service.state)`));
  s.native('restore');
  await waitFor(() => s.win().visible, 'window restored', 10000);
  await s.until(() => { const p = window.T.store.get('presence'); return p && p.visible === true && !window.T.scopes.paused; }, 'page visible, timers running again', 10000);
  // every line the stand-in sent (its watermark) is on the page
  const sent = await s.eval(() => window.T.rt.query('logs.tail', { n: 1 }).then(d => d.watermark));
  c.sent_total = sent;
  c.stream_rate_per_s = Math.round((sent - before) / (WINDOW_MS / 1000));
  check(c.stream_rate_per_s >= 45, `the stand-in streamed only ${c.stream_rate_per_s} lines/s`);
  await waitFor(async () => (await logsTotal(s)) >= sent, `all ${sent} lines on the page after the window shows`, 15000);
  c.total_after = await logsTotal(s);
  c.raf_after_restore = await s.eval(() => new Promise(r => { const n0 = window.__e2eRaf; setTimeout(() => r(window.__e2eRaf - n0), 500); }));
  check(c.raf_after_restore > 0, 'no animation frames after the window was shown again');
  await s.shot('logs-after-restore');
}
