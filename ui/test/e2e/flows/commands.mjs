// Flow "commands": service.start / service.stop and how their job outcome shows on the page
// (P4 architecture sections 2.4, 2.9; banners.js startup failure). The commands go through
// T.shellc.run, the path the command palette and the shell's buttons use (T.rt.call with the
// generic error banner).
//   stand-in: stopped -> start (done: the brand dot passes starting and shows running) -> stop
//             (done: stopped; before it, a foreign service.stop job - another owner - fails live
//             after rt:replayed: the stop banner, taken down by the page's own stop) -> start
//             scripted to fail (no_sources): failed, and the
//             startup-failure banner is up with the failure's words;
//   real sidecar (T-P2-33 sandbox, no sources): start fails (the empty-source failure) and the
//             startup-failure banner is up; stop ends done and the dot shows stopped.
import { bannerInfo, bannerUp, check, goPage, sleep, waitFor } from '../driver.mjs';

export const real = true;
export const launches = [{ args: [], env: {}, options: { phase: 'stopped', start: 'done' } }];

async function runCmd(s, cmd, args = {}) {
  await s.eval(([c, a]) => {
    window.__e2eCmd = { state: 'pending' };
    window.T.shellc.run(c, a).then(r => { window.__e2eCmd = { state: 'resolved', result: r }; }, e => { window.__e2eCmd = { state: 'rejected', code: e && e.code, message_key: e && e.message_key, reason_code: e && e.reason_code }; });
  }, [cmd, args]);
  await s.until(() => window.__e2eCmd.state !== 'pending', `${cmd} job outcome`, 90000);
  return s.eval(() => window.__e2eCmd);
}
const runNow = s => s.eval(() => document.getElementById('app').dataset.run);
const stopArgs = s => s.eval(() => { const st = window.T.rt.topic('service.state').get(); const g = st && st.intent && st.intent.runtime_generation; return Number.isInteger(g) ? { expected_generation: g } : {}; });

export async function run(s, _launch, real) {
  const c = s.checks;
  await s.reloadObserved();
  await goPage(s, 'overview');
  await s.until(() => document.getElementById('app').dataset.run === 'stopped', 'stopped at first', 20000);
  if (!real) {
    let mark = await s.eval(() => window.__e2e.runs.length);
    c.start = await runCmd(s, 'service.start');
    check(c.start.state === 'resolved', `service.start: ${JSON.stringify(c.start)}`);
    await s.until(() => document.getElementById('app').dataset.run === 'running', 'running after start', 10000);
    c.start_runs = (await s.runs()).slice(mark).map(r => r.run);
    check(c.start_runs.includes('starting') && c.start_runs[c.start_runs.length - 1] === 'running', `start runs ${c.start_runs}`);
    await s.shot('started');
    // (Claude supplement 10-13) a service.stop this page did not start (owner 'shell', as the
    // shell's own requests, e.g. the tray's stop: notify.rs / main.rs / host.rs), failing live after rt:replayed: the stop banner (banners.js job(): failed and not
    // instant) with the job's error words; the page's own successful stop below takes it down.
    const evMark = await s.eval(() => window.__e2e.events.length);
    c.replayed_before_ext_stop = await s.eval(() => window.__e2e.events.some(e => e.e === 'rt:replayed'));
    check(c.replayed_before_ext_stop, 'rt:replayed did not come before the foreign stop job');
    check(!(await bannerUp(s, 'stop')), 'a stop banner before the foreign stop failed');
    await s.control({ op: 'job', name: 'service.stop', state: 'failed', owner: 'shell', req_id: 'shell-stop-1',
      error: { code: 'lifecycle_busy', message_key: 'errors.bridge.lifecycle_busy', params: {} } });
    await s.until(() => window.T.notices.banners.has('stop'), 'the stop-failure banner for the foreign stop', 10000);
    c.ext_stop = await bannerInfo(s, 'stop');
    c.ext_stop.job_event = await s.eval(m => window.__e2e.events.slice(m).filter(e => e.e === 'rt:job').map(e => ({ instant: e.instant })), evMark);
    const want = await s.eval(() => ({ title: window.T.t('shell.stop.failed.title'), message: window.T.t('errors.bridge.lifecycle_busy') }));
    check(c.ext_stop.title === want.title, `stop banner title ${JSON.stringify(c.ext_stop.title)}`);
    check(c.ext_stop.message === want.message, `stop banner message ${JSON.stringify(c.ext_stop.message)} (expected the job's error ${JSON.stringify(want.message)})`);
    check(c.ext_stop.elements === 1, `${c.ext_stop.elements} stop banners`);
    check(c.ext_stop.job_event.length >= 1 && c.ext_stop.job_event.every(e => e.instant === false), `the foreign job was not a live edge: ${JSON.stringify(c.ext_stop.job_event)}`);
    check((await runNow(s)) === 'running', 'the service left running after a foreign stop failed');
    await s.shot('foreign-stop-failed');
    mark = await s.eval(() => window.__e2e.runs.length);
    c.stop = await runCmd(s, 'service.stop', await stopArgs(s));
    check(c.stop.state === 'resolved', `service.stop: ${JSON.stringify(c.stop)}`);
    await s.until(() => document.getElementById('app').dataset.run === 'stopped', 'stopped after stop', 10000);
    c.stop_runs = (await s.runs()).slice(mark).map(r => r.run);
    check(c.stop_runs.includes('stopping') && c.stop_runs[c.stop_runs.length - 1] === 'stopped', `stop runs ${c.stop_runs}`);
    await waitFor(async () => !(await bannerUp(s, 'stop')), 'the stop banner goes after a stop that ended done', 5000);
    await s.control({ op: 'options', values: { start: 'failed' } });
  }
  c.failed_start = await runCmd(s, 'service.start');
  check(c.failed_start.state === 'rejected', `the failing start resolved: ${JSON.stringify(c.failed_start)}`);
  await s.until(() => document.getElementById('app').dataset.run === 'failed', 'failed after the failing start', 30000);
  await s.until(() => window.T.notices.banners.has('startup'), 'startup-failure banner', 10000);
  const banner = await s.eval(() => {
    const b = window.T.notices.banners.get('startup'); const el = b && b.el;
    const msg = el && el.querySelector('.banner-msg');
    return { text: el ? el.textContent.trim().slice(0, 300) : null, message: msg ? msg.textContent.trim() : null, unknown: window.T.t('errors.unknown').trim() };
  });
  c.startup_banner = banner.text;
  c.startup_banner_message = banner.message;
  await s.shot('start-failed');
  // the banner names the failure, not the errors.unknown fallback
  const named = () => check(banner.message && !banner.message.includes(banner.unknown),
    `the startup-failure banner shows the errors.unknown fallback instead of the failure: ${JSON.stringify(banner.message)}`);
  // (real sidecar sandbox runs en_US, whose P4 catalog is empty: errorText's T.i18n.has only looks
  // at the current language and falls to errors.unknown - report section 9 item 3, T-P4-02a / 03)
  if (real) await s.known('startup_banner_names_failure', 'T-P4-02a/03', async () => named());
  else named();
  if (real) {
    c.stop = await runCmd(s, 'service.stop', await stopArgs(s));
    check(c.stop.state === 'resolved', `service.stop: ${JSON.stringify(c.stop)}`);
    await s.until(() => document.getElementById('app').dataset.run === 'stopped', 'stopped after stop', 30000);
    c.run_after_stop = await runNow(s);
    await sleep(500);
    c.banner_after_stop = await bannerUp(s, 'startup');
    check(c.run_after_stop === 'stopped', `run after stop: ${c.run_after_stop}`);
    check(c.banner_after_stop === false, 'the startup-failure banner is still up after a successful stop');
  }
}
