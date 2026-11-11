/* ==========================================================================
   icons.js — T.icon(name, size = 16, cls = '')
   Brand grammar: open, round-capped strokes + ONE small filled dot (.d),
   drawn on a 24-unit grid. Stroke weight adapts to the rendered size so the
   device stroke stays ~1.3–1.6 px from 14 to 24 px.
   Returns a T.raw() value: drop it into T.html`` (T-P4-07: no innerHTML anywhere else).
   Tier silhouettes: Vanguard = flag (own ground), Sentinel = watch tower,
   Bastion = a basin holding two dots (a temporary pool), Gateway = one line
   ending in a pinned dot (the fixed final exit). Modes: system proxy = an
   arrow entering a window corner, TUN = a closed loop around a dot (everything
   captured), mixed = two routes.
   ========================================================================== */
(function () {
  'use strict';
  const P = {
    /* ---- navigation ---- */
    overview: '<rect x="4.8" y="4.8" width="6" height="6" rx="1.8"/><rect x="13.2" y="4.8" width="6" height="6" rx="1.8"/><rect x="4.8" y="13.2" width="6" height="6" rx="1.8"/><circle class="d" cx="16.2" cy="16.2" r="2.1"/>',
    tiers: '<path d="M12 4.4L19.2 8.2L12 12L4.8 8.2Z"/><path d="M4.8 12.2L12 16L19.2 12.2"/><circle class="d" cx="12" cy="19.7" r="1.4"/>',
    logs: '<circle class="d" cx="5.6" cy="7" r="1.45"/><path d="M9.4 7H18.6"/><path d="M5.4 12H18.6"/><path d="M5.4 17H14.2"/>',
    settings: '<path d="M4.8 8H8.1"/><path d="M12.9 8H19.2"/><circle cx="10.5" cy="8" r="2.35"/><path d="M4.8 16H11.1"/><path d="M15.9 16H19.2"/><circle class="d" cx="13.5" cy="16" r="2.35"/>',

    /* ---- tiers ---- */
    vanguard: '<path d="M7.4 19.4V6.8"/><path d="M7.4 6.8H17.4L15 10.5L17.4 14.2H7.4"/><circle class="d" cx="7.4" cy="4.2" r="1.3"/>',
    sentinel: '<path d="M6.6 19.4H17.4"/><path d="M8.7 19.4L12 7.6L15.3 19.4"/><path d="M9.9 15H14.1"/><circle class="d" cx="12" cy="4.6" r="1.35"/>',
    bastion: '<path d="M5.6 6.8V13.6A4.4 4.4 0 0 0 10 18H14A4.4 4.4 0 0 0 18.4 13.6V6.8"/><circle class="d" cx="10.3" cy="14.3" r="1.5"/><circle class="d" cx="13.9" cy="10.6" r="1.5"/>',
    gateway: '<path d="M4.6 17H7.8C10.6 17 11 12 13.8 12H15.4"/><path d="M18.8 5.8V8.2"/><path d="M18.8 15.8V18.2"/><circle class="d" cx="18.8" cy="12" r="2"/>',

    /* ---- run / actions ---- */
    start: '<path d="M17.14 8.53A6.4 6.4 0 1 0 16.8 16.31"/><path d="M10.6 9.2L14.8 12L10.6 14.8"/><circle class="d" cx="18.6" cy="12.4" r="1.15"/>',
    play: '<path d="M8.6 6.6L17.2 12L8.6 17.4Z"/>',
    stop: '<rect class="d" x="7.4" y="7.4" width="9.2" height="9.2" rx="2.2"/>',
    power: '<path d="M13.01 5.9A6.4 6.4 0 1 0 18.28 11.64"/><path d="M12 8.8V15.2"/>',
    restart: '<path d="M18.4 12.6A6.4 6.4 0 1 1 16.4 7.4"/><path d="M17.1 4.3V7.9H13.5"/><circle class="d" cx="12" cy="12.4" r="1.3"/>',
    refresh: '<path d="M18.4 12.6A6.4 6.4 0 1 1 16.4 7.4"/><path d="M17.1 4.3V7.9H13.5"/>',
    copy: '<rect x="9" y="9" width="10" height="10" rx="2.2"/><path d="M15 5.8H7.6A1.8 1.8 0 0 0 5.8 7.6V15"/>',
    check: '<path d="M6 12.6L10 16.4L18 8.2"/>',
    plus: '<path d="M12 5.8V18.2"/><path d="M5.8 12H18.2"/>',
    close: '<path d="M7.4 7.4L16.6 16.6"/><path d="M16.6 7.4L7.4 16.6"/>',
    trash: '<path d="M5.2 7.4H18.8"/><path d="M9.6 7.4V5.6H14.4V7.4"/><path d="M7 7.4L7.8 18A1.6 1.6 0 0 0 9.4 19.4H14.6A1.6 1.6 0 0 0 16.2 18L17 7.4"/>',
    edit: '<path d="M14.8 5.6L18.4 9.2L9.4 18.2L5.2 18.8L5.8 14.6Z"/><path d="M12.8 7.6L16.4 11.2"/>',
    search: '<circle cx="10.8" cy="10.8" r="5.6"/><path d="M15 15L19.2 19.2"/>',
    filter: '<path d="M4.8 7H19.2"/><path d="M7.6 12H16.4"/><path d="M10.4 17H13.6"/>',
    sort: '<path d="M8 18.4V5.6"/><path d="M4.8 8.8L8 5.6L11.2 8.8"/><path d="M16 5.6V18.4"/><path d="M12.8 15.2L16 18.4L19.2 15.2"/>',
    pause: '<path d="M9 6.6V17.4"/><path d="M15 6.6V17.4"/>',
    export: '<path d="M8.6 10.4H7.4A1.8 1.8 0 0 0 5.6 12.2V17.4A1.8 1.8 0 0 0 7.4 19.2H16.6A1.8 1.8 0 0 0 18.4 17.4V12.2A1.8 1.8 0 0 0 16.6 10.4H15.4"/><path d="M12 4.8V14"/><path d="M8.9 7.8L12 4.7L15.1 7.8"/>',
    import: '<path d="M5.6 13.4V17.4A1.8 1.8 0 0 0 7.4 19.2H16.6A1.8 1.8 0 0 0 18.4 17.4V13.4"/><path d="M12 4.6V14.2"/><path d="M8.9 11.1L12 14.2L15.1 11.1"/>',
    external: '<path d="M13.6 5.6H18.4V10.4"/><path d="M18.2 5.8L11.4 12.6"/><path d="M16.4 13.8V17.2A2 2 0 0 1 14.4 19.2H6.8A2 2 0 0 1 4.8 17.2V9.6A2 2 0 0 1 6.8 7.6H10.2"/>',
    link: '<path d="M10.4 13.6A3.4 3.4 0 0 0 15.2 13.6L17.6 11.2A3.4 3.4 0 0 0 12.8 6.4L11.6 7.6"/><path d="M13.6 10.4A3.4 3.4 0 0 0 8.8 10.4L6.4 12.8A3.4 3.4 0 0 0 11.2 17.6L12.4 16.4"/>',
    chevron: '<path d="M10 7.2L14.8 12L10 16.8"/>',
    'chevron-left': '<path d="M14 7.2L9.2 12L14 16.8"/>',
    'chevron-down': '<path d="M7.2 10L12 14.8L16.8 10"/>',
    'chevron-up': '<path d="M7.2 14L12 9.2L16.8 14"/>',
    arrow: '<path d="M5.4 12H18.4"/><path d="M14 7.6L18.4 12L14 16.4"/>',
    'arrow-down': '<path d="M12 5.4V18.2"/><path d="M7.6 13.8L12 18.2L16.4 13.8"/>',
    more: '<circle class="d" cx="6.4" cy="12" r="1.45"/><circle class="d" cx="12" cy="12" r="1.45"/><circle class="d" cx="17.6" cy="12" r="1.45"/>',
    grip: '<circle class="d" cx="9.2" cy="7" r="1.3"/><circle class="d" cx="14.8" cy="7" r="1.3"/><circle class="d" cx="9.2" cy="12" r="1.3"/><circle class="d" cx="14.8" cy="12" r="1.3"/><circle class="d" cx="9.2" cy="17" r="1.3"/><circle class="d" cx="14.8" cy="17" r="1.3"/>',
    eye: '<path d="M3.8 12C6.2 7.6 17.8 7.6 20.2 12C17.8 16.4 6.2 16.4 3.8 12Z"/><circle class="d" cx="12" cy="12" r="2.2"/>',
    'eye-off': '<path d="M3.8 12C6.2 7.6 17.8 7.6 20.2 12"/><path d="M6.4 15.2L4.9 16.9"/><path d="M12 16.4V18.6"/><path d="M17.6 15.2L19.1 16.9"/>',
    minus: '<path d="M6 12H18"/>',
    undo: '<path d="M9.4 6.6L5.6 10.4L9.4 14.2"/><path d="M5.8 10.4H14A4.5 4.5 0 0 1 14 19.4H11.4"/>',
    bolt: '<path d="M13.2 4.6L7.2 13.1H12L10.8 19.4L16.8 10.9H12Z"/>',
    target: '<circle cx="12" cy="12" r="6"/><path d="M12 3.8V6"/><path d="M12 18V20.2"/><path d="M3.8 12H6"/><path d="M18 12H20.2"/><circle class="d" cx="12" cy="12" r="1.7"/>',
    wrap: '<path d="M5 6.8H19"/><path d="M5 11.8H15.4A2.7 2.7 0 0 1 15.4 17.2H11.6"/><path d="M13.6 15.2L11.6 17.2L13.6 19.2"/><path d="M5 17.2H8"/>',
    pin: '<path d="M9.4 4.8H14.6"/><path d="M10.4 4.8V9.8L7.6 13.4H16.4L13.6 9.8V4.8"/><path d="M12 13.4V19.4"/>',
    ban: '<circle cx="12" cy="12" r="7.2"/><path d="M7 17L17 7"/>',
    direct: '<path d="M4.8 12H15.8"/><path d="M12.4 8.4L16 12L12.4 15.6"/><circle class="d" cx="19.2" cy="12" r="1.35"/>',
    code: '<path d="M9 7.6L4.8 12L9 16.4"/><path d="M15 7.6L19.2 12L15 16.4"/>',
    list: '<circle class="d" cx="6" cy="7.2" r="1.35"/><path d="M9.8 7.2H18.6"/><path d="M9.8 12H18.6"/><path d="M9.8 16.8H18.6"/>',
    table: '<rect x="4.8" y="5.6" width="14.4" height="12.8" rx="2.2"/><path d="M4.8 10.2H19.2"/><path d="M10.2 10.2V18.4"/>',
    history: '<path d="M5.7 12.4A6.4 6.4 0 1 0 7.6 7.4"/><path d="M5.4 4.6V8.2H9"/><path d="M12 8.6V12.2L14.4 13.8"/>',
    key: '<circle cx="8.4" cy="12" r="3.4"/><path d="M11.8 12H19.2"/><path d="M16.2 12V15"/><path d="M19.2 12V14.2"/>',
    user: '<circle cx="12" cy="8.8" r="3.4"/><path d="M5.8 19.2C6.8 15.8 9 14.2 12 14.2C15 14.2 17.2 15.8 18.2 19.2"/>',
    stack: '<rect x="5" y="8.6" width="14" height="10.4" rx="2.2"/><path d="M7.6 5.4H16.4"/><circle class="d" cx="15.6" cy="13.8" r="1.3"/>',
    help: '<circle cx="12" cy="12" r="7.6"/><path d="M9.8 9.9A2.3 2.3 0 1 1 12.9 12.1C12.3 12.4 12 12.9 12 13.6"/><circle class="d" cx="12" cy="16.4" r="1.1"/>',

    /* ---- metrics / domain ---- */
    download: '<path d="M12 5.8V15.4"/><path d="M8.8 12.2L12 15.4L15.2 12.2"/><path d="M7.74 18.5A4.7 1.9 0 0 0 16.26 18.5"/>',
    upload: '<path d="M12 18.2V8.6"/><path d="M8.8 11.8L12 8.6L15.2 11.8"/><path d="M16.26 5.5A4.7 1.9 0 0 0 7.74 5.5"/>',
    shield: '<path d="M12 4.6L18.2 7V12.2C18.2 15.8 15.6 18.2 12 19.4C8.4 18.2 5.8 15.8 5.8 12.2V7Z"/><circle class="d" cx="12" cy="12" r="1.7"/>',
    killswitch: '<path d="M12 5L17.8 7.5L17 14L12 18.8L6.8 14L6.2 7.5L12 5"/><path d="M8.8 15.5L15.5 8.2"/>',
    'mode-system': '<path d="M12.4 5.8H16.6A2.4 2.4 0 0 1 19 8.2V16.2A2.4 2.4 0 0 1 16.6 18.6H8.6A2.4 2.4 0 0 1 6.2 16.2V12.2"/><path d="M4.8 4.8L10.6 10.6"/><path d="M10.8 6.6V10.8H6.6"/><circle class="d" cx="14.8" cy="14.6" r="1.35"/>',
    'mode-tun': '<rect x="5" y="5" width="14" height="14" rx="4.6"/><circle class="d" cx="12" cy="12" r="2.1"/>',
    'mode-mixed': '<path d="M5.4 8.4H10.8"/><path d="M10.8 8.4C12.8 8.4 12.8 5.8 17.4 5.8"/><path d="M5.4 15.6H10.8"/><path d="M10.8 15.6C12.8 15.6 12.8 18.2 17.4 18.2"/><circle class="d" cx="17.4" cy="12" r="1.35"/>',
    pool: '<circle cx="7.6" cy="7.6" r="2.3"/><circle cx="16.4" cy="7.6" r="2.3"/><circle cx="7.6" cy="16.4" r="2.3"/><circle class="d" cx="16.4" cy="16.4" r="2.3"/>',
    route: '<circle class="d" cx="5.4" cy="8" r="1.5"/><path d="M8.2 8H10.2C13.4 8 11.8 16 15.2 16H16.6"/><circle cx="19" cy="16" r="2"/>',
    globe: '<circle cx="12" cy="12" r="7.4"/><path d="M4.6 12H19.4"/><path d="M12 4.6C9.7 7 9.7 17 12 19.4"/><path d="M12 4.6C14.3 7 14.3 17 12 19.4"/>',
    pulse: '<path d="M4.4 12.6H7.7L9.8 8.1L13.1 16.6L15 12.6H17"/><circle class="d" cx="19.5" cy="12.6" r="1.35"/>',
    clock: '<circle cx="12" cy="12" r="7.4"/><path d="M12 7.8V12L14.8 13.8"/>',
    bell: '<path d="M7.2 16.2V11A4.8 4.8 0 0 1 16.8 11V16.2L18.2 17.8H5.8Z"/><circle class="d" cx="12" cy="20.1" r="1.25"/>',
    phone: '<rect x="7.4" y="4.4" width="9.2" height="15.2" rx="2.2"/><circle class="d" cx="12" cy="16.4" r="1.15"/>',
    mail: '<rect x="4.6" y="6.4" width="14.8" height="11.2" rx="2"/><path d="M5.4 7.6L12 12.6L18.6 7.6"/>',
    send: '<path d="M19.2 4.8L4.8 10.9L10.9 13.1L13.1 19.2Z"/><path d="M10.9 13.1L15.2 8.8"/>',
    folder: '<path d="M4.8 8V17A1.8 1.8 0 0 0 6.6 18.8H17.4A1.8 1.8 0 0 0 19.2 17V10.2A1.8 1.8 0 0 0 17.4 8.4H12L10.2 6.2H6.6A1.8 1.8 0 0 0 4.8 8Z"/>',
    lock: '<rect x="6" y="10.6" width="12" height="8.6" rx="2"/><path d="M8.8 10.6V8.2A3.2 3.2 0 0 1 15.2 8.2V10.6"/><circle class="d" cx="12" cy="14.9" r="1.3"/>',
    lang: '<path d="M4.8 6.8H13.2"/><path d="M9 5V6.8"/><path d="M11.8 6.8C11 10.8 8.4 13.6 5.4 15"/><path d="M7.2 9.8C8.4 12 10.2 13.6 12.2 14.4"/><path d="M13.2 19.2L16.2 11.4L19.2 19.2"/><path d="M14.2 16.8H18.2"/>',

    /* ---- notice severities (shape-coded) ---- */
    success: '<circle cx="12" cy="12" r="7.6"/><path d="M8.6 12.3L11 14.7L15.6 9.8"/>',
    info: '<circle cx="12" cy="12" r="7.6"/><path d="M12 11.2V16"/><circle class="d" cx="12" cy="8.2" r="1.15"/>',
    warning: '<path d="M10.4 5.3A1.85 1.85 0 0 1 13.6 5.3L19.7 16.2A1.85 1.85 0 0 1 18.1 19H5.9A1.85 1.85 0 0 1 4.3 16.2Z"/><path d="M12 9.8V13.4"/><circle class="d" cx="12" cy="16.1" r="1.1"/>',
    error: '<path d="M9 4.6H15L19.4 9V15L15 19.4H9L4.6 15V9Z"/><path d="M12 8.2V12.6"/><circle class="d" cx="12" cy="15.6" r="1.15"/>',
    dot: '<circle class="d" cx="12" cy="12" r="3"/>',
  };
  const ALIAS = { profiles: 'tiers', layers: 'tiers', 'chevron-right': 'chevron', x: 'close', ok: 'success', err: 'error', warn: 'warning', 'mode-sys': 'mode-system', 'mode-mix': 'mode-mixed', system: 'mode-system', tun: 'mode-tun', mixed: 'mode-mixed',
    locate: 'target', block: 'ban', reject: 'ban', subscription: 'stack', group: 'stack', diag: 'pulse', test: 'pulse', draft: 'edit', delete: 'trash', add: 'plus', reset: 'undo' };

  function strokeFor(size) {
    if (size <= 14) return 2.2;
    if (size <= 16) return 2.05;
    if (size <= 18) return 1.95;
    if (size <= 20) return 1.85;
    return 1.75;
  }
  // T-P4-07 (P4 architecture §7): the one place a Raw is minted from a built string (the
  // exception in tools/inject_allow.json): the body is this file's static table (an unknown name
  // falls back to the dot), the size is a number, the extra class names are escaped.
  const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  const attr = v => String(v).replace(/[&<>"']/g, c => ESC[c]);
  function icon(name, size, cls) {
    size = Number(size) > 0 ? Number(size) : 16;
    const key = Object.prototype.hasOwnProperty.call(P, name) ? name : Object.prototype.hasOwnProperty.call(ALIAS, name) ? ALIAS[name] : null;
    const body = (key && P[key]) || P.dot;
    const s = `<svg class="ic ic-${key || 'dot'}${cls ? ' ' + attr(cls) : ''}" width="${size}" height="${size}" viewBox="0 0 24 24" stroke-width="${strokeFor(size)}" aria-hidden="true" focusable="false">${body}</svg>`;
    return window.T && T.raw ? T.raw(s) : s;
  }
  function iconEl(name, size, cls) {
    return T.html`${icon(name, size, cls)}`;
  }
  window.T = window.T || {};
  T.icon = icon;
  T.iconEl = iconEl;
  T.icon.names = Object.keys(P);
})();
