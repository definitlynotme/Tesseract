// The in-page title bar (11-05; DESIGN section 6, PROTOCOL section 13 host_window / host:window).
//
// The shell's main window has no system caption (shell window.rs decorations(false); the DWM
// shadow, rounded corners and resize edges stay). The page draws the three window buttons (#wc in
// index.html, fixed over the top right corner, above the launch cover and every overlay, as a
// system caption would be) and marks the title band draggable with CSS app-region (shell.css), so
// dragging, double-click to maximize, Aero Snap and the system menu stay the system's own.
//
// A button asks the shell through host_window{action}: minimize, toggle_maximize, or close (the
// system close: the shell hides to the tray or quits, exactly as for Alt+F4). The shell's
// host:window{maximized} (its latest is replayed on every connect) picks the maximize or the
// restore glyph and words; before the first one the window is taken as not maximized.
//
// Ports: doc; on (T.on); host(name, args) -> Promise (shellc's host(): the name is checked and a
// failure is noted there); label(el, id) writes the button's aria-label and tooltip (T.ti).

export const WINDOW_ACTIONS = Object.freeze(['minimize', 'toggle_maximize', 'close']);

export function createFrame({ doc, on, host, label = () => {} }) {
  const root = doc && typeof doc.getElementById === 'function' ? doc.getElementById('wc') : null;
  let maximized = false;

  function paint() {
    if (!root) return;
    root.setAttribute('data-max', maximized ? 'true' : 'false');
    const toggle = root.querySelector('[data-wc="toggle_maximize"]');
    if (toggle) label(toggle, maximized ? 'shell.window.restore' : 'shell.window.maximize');
  }

  if (root) {
    root.addEventListener('click', e => {
      const b = e.target && typeof e.target.closest === 'function' ? e.target.closest('[data-wc]') : null;
      if (!b || !root.contains(b)) return;
      const action = b.getAttribute('data-wc');
      if (!WINDOW_ACTIONS.includes(action)) return;
      Promise.resolve(host('host_window', { action })).catch(() => {});
    });
  }
  on('host:window', f => {
    if (!f || typeof f.maximized !== 'boolean' || f.maximized === maximized) return;
    maximized = f.maximized;
    paint();
  });
  if (root) root.setAttribute('data-max', 'false');

  return { get maximized() { return maximized; } };
}
