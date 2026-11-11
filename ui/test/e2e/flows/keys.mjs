// Flow "keys": the page's shortcuts in the shell (core/app.js keyboard; shell window.rs GUARDS).
// Keys are sent through CDP (Input.dispatchKeyEvent) to the page: Ctrl+K opens the command
// palette and Esc closes it; Ctrl+1..4 go to 总览 / 层级 / 运行日志 / 配置; Esc takes a toast down;
// F5 and Ctrl+R do not reload the page (the shell's input guards prevent them; checked with a
// marker on window and the navigation entries, which a reload would reset).
// Scope of the F5 / Ctrl+R half (fix round 1): CDP Input.dispatchKeyEvent does not go through
// WebView2's browser accelerator-key path (window.rs SetAreBrowserAcceleratorKeysEnabled; the
// Policy turns them off in release and leaves them on in this debug build), so "no reload" alone
// could hold even without any shell guard. What this flow proves is the page-side JS guard: the
// keys reach the page (window capture listener counts 3) and are stopped before the bubble phase
// (count 0), i.e. window.rs GUARDS is installed. The accelerator keys being off in release is
// covered by the shell's own checks (acceptance.rs browser_keys), not here.
import { check, goPage, sleep } from '../driver.mjs';

export const real = false;
export const skipReal = 'the stand-in sidecar is enough (page keys only)';
export const launches = [{ args: [], env: {}, options: { phase: 'running' } }];

export async function run(s) {
  const c = s.checks;
  const kb = s.page.keyboard;
  await goPage(s, 'overview');
  await s.page.bringToFront().catch(() => {});
  await s.eval(() => { document.activeElement && document.activeElement.blur && document.activeElement.blur(); });
  await kb.press('Control+k');
  await s.until(() => window.T.cmd.isOpen === true, 'Ctrl+K opens the palette', 5000);
  c.palette_open = true;
  await s.shot('palette');
  await kb.press('Escape');
  await s.until(() => window.T.cmd.isOpen === false, 'Esc closes the palette', 5000);
  c.palette_closed = true;
  c.pages = [];
  for (const [key, id] of [['1', 'overview'], ['2', 'profiles'], ['3', 'logs'], ['4', 'settings'], ['1', 'overview']]) {
    await kb.press(`Control+${key}`);
    await s.until(i => window.T.router.current === i, `Ctrl+${key} -> ${id}`, 5000, id);
    c.pages.push(`${key}:${id}`);
    await sleep(250);
  }
  await s.eval(() => window.T.notify({ kind: 'info', message: 'e2e' }));
  await s.until(() => !!window.T.notices.toast, 'a toast is up', 5000);
  await kb.press('Escape');
  await s.until(() => !window.T.notices.toast, 'Esc takes the toast down', 5000);
  c.toast_esc = true;
  // (a window capture listener sees each key reach the page; a window bubble listener only runs
  // when nothing stopped the key on the way - the shell's guard calls stopImmediatePropagation)
  await s.eval(() => {
    window.__e2eMark = 'kept'; window.__e2eKeys = { reached: 0, passed: 0 };
    const reload = e => e.key === 'F5' || ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'r');
    window.addEventListener('keydown', e => { if (reload(e)) window.__e2eKeys.reached++; }, true);
    window.addEventListener('keydown', e => { if (reload(e)) window.__e2eKeys.passed++; });
  });
  const nav0 = await s.eval(() => performance.getEntriesByType('navigation').length + ':' + performance.timeOrigin);
  for (const k of ['F5', 'Control+r', 'Control+R']) { await kb.press(k); await sleep(1200); }
  c.after_reload_keys = await s.eval(() => ({ mark: window.__e2eMark || null, nav: performance.getEntriesByType('navigation').length + ':' + performance.timeOrigin, keys: window.__e2eKeys }));
  check(c.after_reload_keys.mark === 'kept' && c.after_reload_keys.nav === nav0, `F5 / Ctrl+R reloaded the page: ${JSON.stringify(c.after_reload_keys)}`);
  check(c.after_reload_keys.keys.reached === 3 && c.after_reload_keys.keys.passed === 0, `the shell's guard did not take F5 / Ctrl+R: ${JSON.stringify(c.after_reload_keys.keys)}`);
}
