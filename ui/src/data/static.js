/* Static metadata of the tiers and the network modes (T-P4-02b; P4 architecture §1.1
   data/static.js, criticism C3). Moved verbatim from design/data.js (tiers 13-22, modes
   66-70): ids, icons, fallback order, the caution / fixed flags, the administrator gate.
   Runtime facts are not here: a tier's `enabled` comes from tiers.summary (the design data
   adds a demo default), the real administrator state from network.mode.admin.
   T-P4-04a: the wording is in the catalog. label / short / desc (modes) are getters that read
   the message `mode.<id>.<field>` through T.t at read time (the id itself when no catalog is
   installed). Code that must follow a language switch reads the id or the getter again; a copy
   made with {...mode} keeps the text of the moment it was made. A tier has no words of its
   own: F-03 (user 10-28) removed its role / desc / blurb (「自建」, 「你自己的 VPS，最先使用」…),
   which were the product's advice, not interface text; its code name is the name.
   Importing this file (boot/data-app.js, boot/data-design.js) installs T.data.tiers,
   T.data.tier(id) and T.data.modes, so the app target has them without the demo data. */

// T.t when the i18n runtime is installed (boot/i18n.js); the message id otherwise
const say = (id, params) => {
  const T = typeof window !== 'undefined' ? window.T : null;
  return T && typeof T.t === 'function' ? T.t(id, params) : id;
};
// the tier code names: never translated, never inside a message (architecture §3.1)
export const NAMES = Object.freeze({ vanguard: 'Vanguard', sentinel: 'Sentinel', bastion: 'Bastion', gateway: 'Gateway' });
const worded = (meta, prefix, fields) => Object.freeze(Object.defineProperties({ ...meta }, Object.fromEntries(fields.map(f =>
  [f, { enumerable: true, get: () => say(`${prefix}.${f}`, NAMES) }]))));
const tier = meta => Object.freeze({ ...meta });
const mode = (id, meta) => worded(meta, `mode.${id}`, ['label', 'short', 'desc']);

/* Tier order is the fallback order. Gateway is outside it. */
export const TIERS = Object.freeze([
  tier({ id: 'vanguard', name: NAMES.vanguard, icon: 'vanguard', order: 1 }),
  tier({ id: 'sentinel', name: NAMES.sentinel, icon: 'sentinel', order: 2 }),
  tier({ id: 'bastion', name: NAMES.bastion, icon: 'bastion', order: 3, caution: true }),
  tier({ id: 'gateway', name: NAMES.gateway, icon: 'gateway', order: null, fixed: true }),
]);

/* tun and mixed (TUN + system proxy) both bring up the TUN adapter, so both need administrator rights */
export const MODES = Object.freeze({
  system: mode('system', { icon: 'mode-system' }),
  tun: mode('tun', { icon: 'mode-tun', admin: true }),
  mixed: mode('mixed', { icon: 'mode-mixed', admin: true }),
});

/* 接管方式 shows the saved mode as two switches: 系统代理 (on in system and mixed) and 虚拟网卡
   (on in tun and mixed). At least one stays on: switching off the only one that is on turns the
   other one on, so a press always leads to one of the three modes (and a non-administrator saved
   on tun can still go back to the system proxy: its 虚拟网卡 switch off → system) */
export const MODE_SWITCHES = Object.freeze(['system', 'tun']);
export const modeSwitchOn = (mode, k) => (k === 'system' ? mode === 'system' || mode === 'mixed' : mode === 'tun' || mode === 'mixed');
export function modeAfterSwitch(mode, k) {
  let sys = modeSwitchOn(mode, 'system'), tun = modeSwitchOn(mode, 'tun');
  if (k === 'system') sys = !sys; else tun = !tun;
  if (!sys && !tun) { if (k === 'system') tun = true; else sys = true; }
  return sys && tun ? 'mixed' : sys ? 'system' : 'tun';
}

// Install on T.data (created when absent). A later data file (the transitional design data)
// may replace T.data with a richer object built on these same constants.
export function installStatic(T) {
  if (!T) return null;
  const data = T.data || (T.data = {});
  if (!data.tiers) {
    // a writable copy that keeps the wording getters (a plain {...t} would freeze the text)
    data.tiers = TIERS.map(t => Object.defineProperties({}, Object.fromEntries(Object.entries(Object.getOwnPropertyDescriptors(t))
      .map(([k, d]) => [k, 'value' in d ? { ...d, writable: true, configurable: true } : { ...d, configurable: true }]))));
    data.tier = id => data.tiers.find(t => t.id === id);
  }
  if (!data.modes) data.modes = MODES;
  return data;
}

if (typeof window !== 'undefined' && window.T) installStatic(window.T);
