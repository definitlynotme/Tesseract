// Flow "autostart" (D8; criticism A5; shell/README T-P3-06 --autostart, p3_host_smoke.ps1
// autostart-tray / autostart-window). Three launches of the debug shell in one scope:
//   0  normal start: 配置 turns 开机自启 and 开机自启时进入托盘 on and saves (settings.apply
//      carries autostart.enabled and ui.start_in_tray_on_autostart = true);
//   1  --autostart: the shell queries config.snapshot itself (from=shell), logs
//      "autostart_start setting=true window=tray", the window is not shown and the tray icon is
//      registered with Explorer; the window is restored from the tray, 配置 turns
//      开机自启时进入托盘 off and saves;
//   2  --autostart again: "autostart_start setting=false window=shown" and the window shows.
// (The stand-in sidecar reports autostart.state supported, as an installed default scope would;
// the real sidecar keeps autostart read-only in custom scopes, 5th-wave ruling P3-06 item 7.)
import { check, saveSettings, setSwitch, settingsReady, sleep, waitFor } from '../driver.mjs';

export const real = false;
export const skipReal = 'custom scopes keep autostart read-only with the real sidecar (5th-wave ruling item 7)';
export const launches = [
  { args: [], env: {}, options: { phase: 'running', autostart_supported: true } },
  { args: ['--autostart'], env: {}, options: { phase: 'running', autostart_supported: true } },
  { args: ['--autostart'], env: {}, options: { phase: 'running', autostart_supported: true } },
];

const lastApply = s => s.requests().filter(r => r.cmd === 'settings.apply').pop();
const shellSnapshots = s => s.requests().filter(r => r.cmd === 'config.snapshot' && r.from === 'shell');

export async function run(s, launch) {
  const c = s.checks;
  if (launch === 0) {
    await settingsReady(s);
    await setSwitch(s, 'autostart', true);
    await setSwitch(s, 'tray', true);
    await saveSettings(s);
    const a = lastApply(s);
    c.saved = a && a.args.values;
    check(c.saved && c.saved.autostart && c.saved.autostart.enabled === true, `autostart not saved on: ${JSON.stringify(c.saved)}`);
    check(c.saved.ui && c.saved.ui.start_in_tray_on_autostart === true, `start_in_tray_on_autostart not saved on: ${JSON.stringify(c.saved)}`);
    await s.shot('saved-on');
    return;
  }
  const want = launch === 1 ? 'setting=true window=tray' : 'setting=false window=shown';
  const line = await s.logWait(new RegExp(`^autostart_start ${want}$`), `autostart_start ${want}`, 20000);
  c.autostart_line = line[line.length - 1].text;
  c.shell_snapshot_queries = shellSnapshots(s).filter(r => r.launch === Math.max(...s.requests().map(x => x.launch))).length;
  check(c.shell_snapshot_queries >= 1, 'the shell did not query config.snapshot itself');
  await sleep(800);
  const st = s.win();
  c.window = { visible: st.visible, tray_icon: st.tray_icon, cloaked: st.cloaked };
  if (launch === 1) {
    check(!st.visible, 'the window shows although D8 is on');
    await waitFor(() => s.win().tray_icon, 'tray icon registered', 10000);
    c.window.tray_icon = true;
    s.native('restore');
    await waitFor(() => s.win().visible, 'window restored from the tray', 10000);
    await settingsReady(s);
    await setSwitch(s, 'tray', false);
    await saveSettings(s);
    const a = lastApply(s);
    c.saved = a && a.args.values;
    check(c.saved && c.saved.ui && c.saved.ui.start_in_tray_on_autostart === false, `start_in_tray_on_autostart not saved off: ${JSON.stringify(c.saved)}`);
  } else {
    await waitFor(() => s.win().visible, 'the window shows with D8 off', 10000);
    c.window.visible = true;
    await s.shot('shown-with-d8-off');
  }
}
