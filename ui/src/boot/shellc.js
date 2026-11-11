// Hook: shell-page contract (owner: T-P4-06; see boot/README.md).
// Installs T.shellc (shellc/index.js): notices queue and host:notify receipts, shell banners,
// quit answer, presence, navigation, language, connect declarations and host_ready.
// Design target, fixture mode: a scenario replays the shell, so the shell invokes the page makes
// on its own (host_notify_ready, host_set_page, host_ready, ...) are sent only when the scenario
// registered them (allow_host or an on_invoke {host}); the others are skipped and noted in
// T.shellc.diag(), so scenarios written before this ticket stay valid. The app target and the
// design simulator send them all.
import { installShellc } from '../shellc/index.js';

function fixtureHosts() {
  let sc = window.__TESSERACT_FIXTURE__;
  if (typeof sc === 'string') { try { sc = JSON.parse(sc); } catch (e) { sc = null; } }
  if (!sc || typeof sc !== 'object') return null;
  const names = new Set(Array.isArray(sc.allow_host) ? sc.allow_host.filter(n => typeof n === 'string') : []);
  for (const s of Array.isArray(sc.steps) ? sc.steps : []) if (s && s.on_invoke && typeof s.on_invoke.host === 'string') names.add(s.on_invoke.host);
  return names;
}

const hosts = __TARGET__ === 'design' && window.__TESSERACT_FIXTURE__ ? fixtureHosts() : null;
// 11-05 window-first launch: the launch screen in the app target (the shell opens the window
// while the runtime starts); the design target only when a test or preview asks for it
// (window.__TESSERACT_LAUNCH__ = true before the bundle runs).
const launch = __TARGET__ === 'app' || window.__TESSERACT_LAUNCH__ === true;
installShellc(window.T, Object.assign(hosts ? { allowHost: name => hosts.has(name) } : {}, { launch }));
