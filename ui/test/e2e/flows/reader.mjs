// Flow "reader" (Claude supplement 10-13, 8th-wave ruling; 对等台账 #181, decided as the docked
// reader's width): the log reader's width survives a shell restart.
//   launch 0: 运行日志, a record clicked opens the reader (pages/logs.js showReader, T.drawer
//             docked); its grip (.logs-rd-grip, role separator) is widened with the keyboard
//             (ArrowLeft, 24 px a step; saved to the WebView's localStorage on every step); the
//             panel's width and the stored value are recorded. Then the tray Quit ends the shell.
//   launch 1: a new shell process on the same scope (same WebView2 user data): the reader opened
//             again has the width set in launch 0, which is not the default width.
import { check, fail, goPage, sleep } from '../driver.mjs';

export const real = false;
export const skipReal = 'the reader width is the page\'s own (WebView localStorage); the stand-in gives the records to open';
export const launches = [
  { args: [], env: {}, options: { phase: 'running' } },
  { args: [], env: {}, options: { phase: 'running' } },
];

const KEY = 'tesseract.logs.reader.width';
const STEPS = 4;

async function openReader(s) {
  await goPage(s, 'logs');
  await s.control({ op: 'logs', n: 5 });
  await s.until(() => Array.from(document.querySelectorAll('#logs-events .li[data-key]')).some(r => r.checkVisibility({ visibilityProperty: true, opacityProperty: true })), 'visible log rows on 运行日志', 15000);
  await sleep(300);
  // the last record of the event view (pages/logs.js ROWSEL.events; the raw view is hidden)
  await s.eval(() => { const rows = Array.from(document.querySelectorAll('#logs-events .li[data-key]')).filter(r => !r.hidden && r.checkVisibility({ visibilityProperty: true, opacityProperty: true }) && r.getBoundingClientRect().height > 0); const r = rows[rows.length - 1]; if (r) r.setAttribute('data-e2e-row', '1'); });
  await s.page.click('[data-e2e-row="1"]');
  await s.until(() => { const p = document.querySelector('.logs-reader'); return !!(p && p.querySelector('.logs-rd-grip') && p.getBoundingClientRect().width > 0); }, 'the log reader is open', 10000);
  await sleep(600); // the panel slides in (T.drawer dock)
  return readerFacts(s);
}
const readerFacts = s => s.eval(k => {
  const p = document.querySelector('.logs-reader');
  let stored = null; try { stored = localStorage.getItem(k); } catch (e) { stored = 'unreadable'; }
  const body = document.querySelector('.logs-reader') && document.querySelector('.logs-reader').parentElement;
  return { width: p ? Math.round(p.getBoundingClientRect().width) : null, stored, body_width: body ? Math.round(body.clientWidth) : null, inner_width: innerWidth };
}, KEY);

export async function run(s, launch) {
  const c = s.checks;
  if (launch === 0) {
    c.opened = await openReader(s);
    check(c.opened.stored === null, `a stored reader width before any drag: ${c.opened.stored}`);
    check(c.opened.body_width >= 760, `the window is too narrow for a docked reader (body ${c.opened.body_width} px)`);
    await s.page.focus('.logs-reader .logs-rd-grip');
    for (let i = 0; i < STEPS; i++) { await s.page.keyboard.press('ArrowLeft'); await sleep(120); }
    await sleep(500);
    c.widened = await readerFacts(s);
    check(c.widened.width >= c.opened.width + STEPS * 24 - 2, `the grip did not widen the reader: ${c.opened.width} -> ${c.widened.width}`);
    check(c.widened.stored !== null && Math.abs(Number(c.widened.stored) - c.widened.width) <= 1, `the width was not stored: ${c.widened.stored} vs ${c.widened.width}`);
    await s.shot('reader-widened');
    // for launch 1 (the evidence directory is the flow's)
    s.note('reader_width', { width: c.widened.width, stored: c.widened.stored, default: c.opened.width, shell_pid: s.pid });
    return;
  }
  const prev = s.recall('reader_width');
  if (!prev) fail('launch 0 left no reader width');
  c.prev = prev;
  c.after_restart = await openReader(s);
  check(s.pid !== prev.shell_pid, 'launch 1 is not a new shell process');
  check(c.after_restart.stored === prev.stored, `the stored width did not survive the shell restart: ${prev.stored} -> ${c.after_restart.stored}`);
  check(Math.abs(c.after_restart.width - prev.width) <= 1, `the reader opened ${c.after_restart.width} px wide after the restart (${prev.width} px set before, default ${prev.default})`);
  check(c.after_restart.width !== prev.default, 'the reader opened at the default width');
  await s.shot('reader-after-restart');
}
