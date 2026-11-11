// Flow "quit": the quit question with unsaved settings (shell/README T-P3-04; P4 architecture
// section 2.9 host:confirmQuit; shellc/quit.js). 配置 holds a draft (one switch flipped); the tray's
// 「退出」 makes the shell ask the page (host:confirmQuit), the page answers host_confirm_quit with
// its unsaved count, and the shell's native box appears naming that count. The box is a
// MessageBoxW (dialog.rs unsaved(): MB_YESNO, default No), so 「否」 is pressed with its button id
// (WM_COMMAND IDNO = 7; TDM_CLICK_BUTTON only applies to task dialogs). The shell then stays in
// Running: same process, page still live, a query still answered, no app.quit sent to the
// sidecar. The draft is undone at the end so the script's own tray Quit leaves at once.
import { check, dirtyCount, fail, makeDraft, settingsReady, sleep, undoDrafts, waitFor } from '../driver.mjs';

export const real = false;
export const skipReal = 'needs the stand-in sidecar (a scripted config snapshot to draft on)';
export const launches = [{ args: [], env: {}, options: { phase: 'running' } }];

export async function run(s) {
  const c = s.checks;
  await settingsReady(s);
  c.draft_key = await makeDraft(s);
  c.unsaved = await s.eval(() => window.T.shellc.unsaved());
  c.dirty = await dirtyCount(s);
  check(c.unsaved >= 1, `no unsaved count: ${c.unsaved}`);
  const answers = () => s.eval(() => window.T.shellc.diag().filter(d => d.kind === 'host' && d.name === 'host_confirm_quit').length);
  const before = await answers();
  check(s.native('tray_quit').ok, 'tray window not found');
  await waitFor(async () => (await answers()) > before, 'the page answers host_confirm_quit', 10000);
  const box = await waitFor(() => { const st = s.win(); return st.dialogs.length ? st.dialogs[0] : null; }, 'the shell\'s native unsaved box', 10000);
  c.dialog = { title: box.title, text: box.text, buttons: box.buttons };
  await s.shot('quit-asked');
  const count = (box.text.match(/\d+/g) || []).map(Number);
  check(count.includes(c.unsaved), `the box does not name ${c.unsaved} unsaved changes: ${JSON.stringify(box.text)}`);
  check(box.buttons.some(b => b.id === 7), `no 「否」 (IDNO) button: ${JSON.stringify(box.buttons)}`);
  s.native('click', ['--button', '7']);
  await waitFor(() => s.win().dialogs.length === 0, 'the box closes', 10000);
  await sleep(1500);
  c.after = s.win();
  check(c.after.main && c.after.visible, 'the main window is gone after 「否」');
  check(!s.requests().some(r => r.cmd === 'app.quit'), 'app.quit reached the sidecar although the user said no');
  const lines = s.logFind(/quit/).map(l => l.text);
  c.quit_log = lines.slice(-6);
  // the shell's own record (lifecycle.rs): the page's answer with its unsaved count, then the cancel
  const is = code => l => l === code || l.startsWith(code + ' ');
  const reply = lines.findLastIndex(is(`quit_confirm_reply_${Math.min(c.unsaved, 999)}`));
  check(reply >= 0, `shell.log has no quit_confirm_reply_${c.unsaved}: ${JSON.stringify(c.quit_log)}`);
  check(lines.slice(reply + 1).some(is('quit_cancelled')), `shell.log has no quit_cancelled after the reply: ${JSON.stringify(c.quit_log)}`);
  c.still_live = await s.eval(() => window.T.rt.state);
  check(c.still_live === 'live', `page state after 「否」: ${c.still_live}`);
  c.query_after = await s.eval(() => window.T.rt.query('app.info', {}).then(d => !!d, e => 'ERR:' + (e && e.code)));
  check(c.query_after === true, `the shell does not answer after 「否」: ${c.query_after}`);
  c.dirty_after = await dirtyCount(s);
  check(c.dirty_after === c.dirty, 'the draft was lost by the quit question');
  await undoDrafts(s);
  if ((await s.eval(() => window.T.shellc.unsaved())) !== 0) fail('drafts left after undo');
}
