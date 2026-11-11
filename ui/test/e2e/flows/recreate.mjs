// Flow "recreate" (Claude supplement 10-13, 8th-wave ruling; 05c ruling: the sidebar badge of
// 运行日志 counts "unseen since this page loaded", a known limitation; T-P4-11 verifies what a
// WebView rebuild does to it).
//   Hard checks: 3 error records arriving while 总览 is shown put "3" on the 运行日志 sidebar
//   badge; opening 运行日志 clears it. Then the WebView is rebuilt twice, each time after the badge
//   was cleared by a visit:
//     A. the page reloads (the renderer's own reload, same epoch, the shell's cached replay);
//     B. this shell's WebView2 browser process is ended (only the one whose parent is this shell
//        and whose user data is this scope's): the shell logs webview_recreated and builds a new
//        window; the new page replays the same epoch.
//   Both must come back live on the same epoch with the replayed log tail.
//   Recorded, not asserted (the ruling asks to verify): the badge after A and after B, i.e.
//   whether the replayed tail is counted again as unseen (checks.badge_recount).
import { check, goPage, logsTotal, scopedBrowserPid, sleep, waitFor } from '../driver.mjs';

export const real = false;
export const skipReal = 'needs the stand-in sidecar (scripted error records)';
export const launches = [{ args: [], env: {}, options: { phase: 'running' } }];

const badgeNow = s => s.eval(() => {
  const b = document.querySelector('.nav-item[data-nav="logs"] .logs-navbadge');
  return b && !b._gone ? Number(b.textContent) : 0;
});
async function clearByVisit(s) {
  await goPage(s, 'logs');
  await waitFor(async () => (await badgeNow(s)) === 0, 'the badge cleared by a visit to 运行日志', 5000);
  await goPage(s, 'overview');
  await sleep(500);
  return badgeNow(s);
}
async function settled(s, label) {
  await s.until(() => window.T && window.T.rt && window.T.rt.state === 'live' && window.T.router && !!window.T.router.current, `${label}: page live`, 30000);
  await sleep(2500); // the replay, the log tail and one painted frame
  return {
    epoch: await s.eval(() => window.T.rt.epoch),
    page: await s.eval(() => window.T.router.current),
    badge: await badgeNow(s),
  };
}

export async function run(s) {
  const c = s.checks;
  await goPage(s, 'overview');
  await s.until(() => window.T.rt.state === 'live', 'live', 20000);
  c.epoch = await s.eval(() => window.T.rt.epoch);
  c.badge_start = await badgeNow(s);
  await s.control({ op: 'logs', n: 3, level: 'error' });
  await waitFor(async () => (await badgeNow(s)) === 3, 'badge 3 for three error records', 10000);
  c.badge_live = await badgeNow(s);
  await s.shot('badge-3');
  c.badge_after_visit = await clearByVisit(s);
  check(c.badge_after_visit === 0, `the badge came back after the visit: ${c.badge_after_visit}`);
  await goPage(s, 'logs');
  c.logs_total_before = await logsTotal(s);
  await goPage(s, 'overview');

  // A. the page reloads (same epoch)
  await s.page.reload({ waitUntil: 'domcontentloaded', timeout: 20000 }).catch(() => {});
  await s.findPage();
  c.reload = await settled(s, 'reload');
  check(c.reload.epoch === c.epoch, `the reload changed the epoch: ${c.reload.epoch}`);
  await s.shot('after-reload');
  c.badge_cleared_before_recreate = await clearByVisit(s);

  // B. the browser process ends: the shell builds a new window
  const pid = scopedBrowserPid(s);
  check(pid, 'no WebView2 browser process of this shell');
  c.killed_browser_pid = pid;
  const before = s.logFind(/^webview_recreated /).length;
  process.kill(pid);
  await s.logWait(/^webview_process_failed kind=browser_exited/, 'webview_process_failed browser_exited', 20000);
  await waitFor(() => s.logFind(/^webview_recreated /).length > before, 'webview_recreated in shell.log', 30000);
  c.recreated_line = s.logFind(/^webview_recreated /).pop().text;
  await s.reattach();
  c.recreate = await settled(s, 'recreate');
  c.recreate.new_browser_pid = scopedBrowserPid(s);
  check(c.recreate.new_browser_pid && c.recreate.new_browser_pid !== pid, `no new browser process: ${c.recreate.new_browser_pid}`);
  check(c.recreate.epoch === c.epoch, `the rebuilt page is on another epoch: ${c.recreate.epoch}`);
  await s.shot('after-recreate');
  await goPage(s, 'logs');
  c.logs_total_after_recreate = await logsTotal(s);
  check(c.logs_total_after_recreate >= c.logs_total_before, `the replayed log tail is shorter: ${c.logs_total_before} -> ${c.logs_total_after_recreate}`);
  await goPage(s, 'overview');
  // the finding (verify only): records seen before the rebuild counted again as unseen
  c.badge_recount = { after_reload: c.reload.badge, after_recreate: c.recreate.badge, seen_errors_before: 3,
    recounted: c.reload.badge > 0 || c.recreate.badge > 0 };
}
