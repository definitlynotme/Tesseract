// The notice card window's page (提醒卡片): shell/app/src/notice.rs builds the window (label
// `notice`, transparent, never activated) and talks to this page through one Channel:
//   notice_ready{channel}                  this page listens; the shell sends the waiting cards
//   {t:'notice:show', language, card}      a card (notify.rs Notice::card / shell_card)
//   {t:'notice:close', incident}           that incident's card goes, silently
//   {t:'notice:clear'}                     a new sidecar epoch: every card but the shell's goes
//   {t:'notice:language', language}        ui.language changed: the cards on screen are rewritten
// and the page answers with notice_resize / notice_rendered / notice_open / notice_dismiss
// (view.js). The cards follow the user's UI language: the same locale files and T.t as the main
// window (zh_CN is preloaded by notice.html; another language loads before the card that names
// it is drawn, at most LANGUAGE_WAIT_MS, then the card is drawn in the language there is).
// Frames are handled one after another in arrival order.
import '../core/icons.js';
import { install } from '../i18n/t.js';
import { createStack, normalize } from './model.js';
import { createView } from './view.js';

export const LANGUAGE_WAIT_MS = 1500;

const win = window;
const T = win.T;
const i18n = install(win);
const core = win.__TAURI__ && win.__TAURI__.core;
const motion = win.__TESSERACT_MOTION__;
const reduced = !!(motion && motion.reduced) || !!(win.matchMedia && win.matchMedia('(prefers-reduced-motion: reduce)').matches);
// every shell answer is best effort: a refused or failed invoke changes nothing on screen
const invoke = (name, args) => (core && typeof core.invoke === 'function'
  ? Promise.resolve().then(() => core.invoke(name, args || {})).catch(() => null) : Promise.resolve(null));
const view = createView({ doc: document, win, T, invoke, reduced, stack: createStack() });

async function language(lang) {
  if (typeof lang !== 'string' || !lang || lang === i18n.lang) return;
  let timer = 0;
  const late = new Promise(resolve => { timer = win.setTimeout(resolve, LANGUAGE_WAIT_MS); });
  try { await Promise.race([i18n.set(lang), late]); } catch (e) { /* the language there is stays */ }
  win.clearTimeout(timer);
}
async function handle(message) {
  if (!message || typeof message !== 'object') return;
  if (message.t === 'notice:show') {
    await language(message.language);
    const card = normalize(message.card);
    if (card) view.show(card);
  } else if (message.t === 'notice:close' && typeof message.incident === 'string') view.close(message.incident);
  else if (message.t === 'notice:clear') view.clear();
  else if (message.t === 'notice:language') await language(message.language);
}
let chain = Promise.resolve();
function receive(message) { chain = chain.then(() => handle(message)).catch(() => {}); }

if (core && typeof core.Channel === 'function') {
  const channel = new core.Channel();
  channel.onmessage = receive;
  invoke('notice_ready', { channel });
}
