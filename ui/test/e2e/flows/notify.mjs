// Flow "notify": host:notify and its receipt (PROTOCOL section 13; 5th-wave ruling 31b-3: the
// end-to-end covers the desktop-notice path "in front, page declared, rendered").
//  1. In front: the main window is brought to the foreground; the page has declared
//     host_notify_ready on its connection; an alerts.desktop from the sidecar is routed to the page
//     (shell.log notify_route ... route=in_app), rendered as a toast with its title, and the shell
//     sends alerts.desktopDelivered{delivered, in_app} to the sidecar.
//  2. At most 5 notices on the page at once: 7 more arrive together; never more than 5 toasts are
//     on screen, and the rest wait in one merged row.
//  3. Closed to the tray: the window is hidden, the next notice goes to the balloon
//     (alerts.desktopDelivered{delivered, windows}); the window is restored afterwards.
// Windows decides whether a background process may take the foreground: a refusal is reported as
// an environment failure ("foreground not granted"), never as a pass.
import { check, fail, sleep, waitFor } from '../driver.mjs';

export const real = false;
export const skipReal = 'needs the stand-in sidecar (scripted alerts.desktop deliveries)';
export const launches = [{ args: [], env: {}, options: { phase: 'running' } }];

const receipt = (s, id) => waitFor(() => s.requests().find(r => r.cmd === 'alerts.desktopDelivered' && r.args.delivery_id === id), `receipt for ${id}`, 10000);

export async function run(s) {
  const c = s.checks;
  await s.until(() => window.T.shellc.diag().some(d => d.kind === 'host' && d.name === 'host_notify_ready'), 'host_notify_ready declared', 10000);
  await s.logWait(/^notify_page_ready /, 'notify_page_ready', 10000);
  // 1. in front
  let front = false;
  for (let i = 0; i < 4 && !front; i++) { front = s.native('foreground').ok; if (!front) await sleep(500); }
  c.foreground = front;
  if (!front) fail('environment: foreground not granted to the test window');
  await sleep(400);
  const title = 'E2E notice in front';
  await s.control({ op: 'desktop', delivery_id: 'e2e-front-1', severity: 'info', title, detail: 'p4 e2e', target: 'overview' });
  const r1 = await receipt(s, 'e2e-front-1');
  c.front_receipt = r1.args;
  const route = s.logFind(/^notify_route delivery=e2e-front-1 /);
  c.front_route = route.length ? route[0].text : null;
  if (r1.args.surface !== 'in_app') {
    if (!s.win().foreground) fail(`environment: the foreground was taken during the notice (${c.front_route})`);
    fail(`a notice in front was not rendered on the page: ${JSON.stringify(r1.args)} (${c.front_route})`);
  }
  check(r1.args.outcome === 'delivered', `in-app receipt ${JSON.stringify(r1.args)}`);
  c.front_toast = await s.eval(t => [...document.querySelectorAll('#toast-slot > .toast')].some(e => e.textContent.includes(t)), title);
  check(c.front_toast, 'the notice title is not on the page');
  await s.shot('notice-in-front');
  // 2. seven together: never more than five on screen
  await s.eval(() => { window.__e2eToasts = 0; const o = new MutationObserver(() => { const n = [...document.querySelectorAll('#toast-slot > .toast:not(.toast-more)')].filter(e => !e._gone).length; if (n > window.__e2eToasts) window.__e2eToasts = n; }); o.observe(document.body, { subtree: true, childList: true, attributes: true, attributeFilter: ['class'] }); window.__e2eToastObs = o; });
  for (let i = 1; i <= 7; i++) await s.control({ op: 'desktop', delivery_id: `e2e-many-${i}`, severity: 'info', title: `E2E notice ${i}`, detail: '', target: 'overview' });
  await sleep(800);
  c.max_toasts = await s.eval(() => window.__e2eToasts);
  c.merge_row = await s.eval(() => { const m = document.querySelector('#toast-slot > .toast-more'); return m ? m.textContent.trim().slice(0, 120) : null; });
  await s.shot('many-notices');
  check(c.max_toasts <= 5, `more than 5 notices on screen: ${c.max_toasts}`);
  check(c.max_toasts >= 4, `the notices did not show: ${c.max_toasts} on screen`);
  const many = await waitFor(() => { const got = s.requests().filter(r => r.cmd === 'alerts.desktopDelivered' && /^e2e-many-/.test(r.args.delivery_id)); return got.length >= 7 ? got : null; }, 'receipts for the 7 notices', 15000);
  c.many_receipts = many.map(r => `${r.args.delivery_id}:${r.args.outcome}/${r.args.surface}`);
  const inApp = many.filter(r => r.args.surface === 'in_app').length;
  check(inApp >= 4 && inApp <= 7, `in-app receipts ${inApp} of 7`);
  await s.eval(() => { if (window.__e2eToastObs) window.__e2eToastObs.disconnect(); });
  // 3. closed to the tray: the balloon
  s.native('close');
  await waitFor(() => !s.win().visible, 'window closed to the tray', 10000);
  await s.until(() => window.T.store.get('presence') && window.T.store.get('presence').visible === false, 'page presence hidden', 10000);
  await s.control({ op: 'desktop', delivery_id: 'e2e-tray-1', severity: 'info', title: 'E2E notice in the tray', detail: '', target: 'overview' });
  const r3 = await receipt(s, 'e2e-tray-1');
  c.tray_receipt = r3.args;
  check(r3.args.outcome === 'delivered' && r3.args.surface === 'windows', `closed to the tray: ${JSON.stringify(r3.args)}`);
  s.native('restore');
  await waitFor(() => s.win().visible, 'window restored', 10000);
}
