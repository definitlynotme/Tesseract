// Flow "navigate": a click on the shell's balloon leads to 配置 · 提醒 (shell/README T-P3-05
// host:navigate; shellc/navigate.js NAV_TARGETS alerts -> settings{focus:alerts}; T-P4-05d focus
// landing). The window is closed to the tray; an alerts.desktop with target "alerts" goes to the
// balloon (receipt delivered/windows); a simulated balloon click (NIN_BALLOONUSERCLICK to the tray
// window) restores the window and the shell sends host:navigate{page:alerts} after
// host:visibility{true}; the page goes to 配置 with focus=alerts, reports the page as "alerts"
// (host_set_page) and the alerts section is in view.
import { check, goPage, sleep, waitFor } from '../driver.mjs';

export const real = false;
export const skipReal = 'needs the stand-in sidecar (a scripted alerts.desktop with a target)';
export const launches = [{ args: [], env: {}, options: { phase: 'running' } }];

export async function run(s) {
  const c = s.checks;
  await s.reloadObserved();
  await goPage(s, 'overview');
  s.native('close');
  await waitFor(() => !s.win().visible, 'window closed to the tray', 10000);
  const mark = await s.eval(() => window.__e2e.events.length);
  await s.control({ op: 'desktop', delivery_id: 'e2e-nav-1', severity: 'warn', title: 'E2E alert', detail: 'navigate', target: 'alerts' });
  const r = await waitFor(() => s.requests().find(x => x.cmd === 'alerts.desktopDelivered' && x.args.delivery_id === 'e2e-nav-1'), 'balloon receipt', 10000);
  c.receipt = r.args;
  check(r.args.outcome === 'delivered' && r.args.surface === 'windows', `balloon receipt ${JSON.stringify(r.args)}`);
  s.native('balloon_click');
  await s.logWait(/^tray_balloon_navigate page=alerts/, 'tray_balloon_navigate page=alerts', 10000);
  await waitFor(() => s.win().visible, 'window restored by the click', 10000);
  await s.until(m => window.__e2e.events.slice(m).some(e => e.e === 'host:navigate' && e.page === 'alerts'), 'host:navigate{alerts} on the page', 10000, mark);
  c.order = await s.eval(m => window.__e2e.events.slice(m).filter(e => e.e === 'host:visibility' || e.e === 'host:navigate').map(e => `${e.e}:${e.e === 'host:navigate' ? e.page : e.visible}`), mark);
  const nav = c.order.indexOf('host:navigate:alerts'), vis = c.order.lastIndexOf('host:visibility:true', nav);
  check(vis >= 0 && vis < nav, `host:navigate before host:visibility{true}: ${c.order.join(',')}`);
  await s.until(() => window.T.router.current === 'settings', 'the page went to 配置', 10000);
  c.params = await s.eval(() => { const p = window.T.router.pages.get('settings'); return p && p.ctx ? p.ctx.params : null; });
  check(c.params && c.params.focus === 'alerts', `配置 not focused on alerts: ${JSON.stringify(c.params)}`);
  await s.until(() => { const a = document.activeElement; return !!(a && a.closest && a.closest('#set-alerts')); }, 'the focus lands in the alerts section', 10000);
  // T-P4-05d focus landing: on the first visit of 配置 the page grows after the scroll target was
  // taken (the snapshot fills the rows), so the focused heading ends up below the fold.
  await s.known('alerts_in_view', 'T-P4-05d', () => s.until(() => { const el = document.getElementById('set-alerts'); if (!el) return false; const r = el.getBoundingClientRect(); return r.top >= 0 && r.top < innerHeight * 0.6; }, 'the alerts section in view', 6000));
  await sleep(300);
  c.section_top = await s.eval(() => Math.round(document.getElementById('set-alerts').getBoundingClientRect().top));
  c.viewport_height = await s.eval(() => innerHeight);
  c.reported = await s.eval(() => window.T.shellc.diag().filter(d => d.kind === 'host' && d.name === 'host_set_page').length);
  const presence = await waitFor(() => s.requests().filter(x => x.cmd === 'ui.presence' && x.args.page === 'alerts').pop(), 'ui.presence page=alerts', 10000);
  c.presence_page = presence.args.page;
  await s.shot('alerts-focused');
  await goPage(s, 'overview');
}
