// Tesseract Setup — the page (design: shell/setup/README.md).
// One window, one action. The action button becomes its own progress and comes back as the
// next action; the body cross-fades in place; the brand light lands when the work is done.
//
// Host contract (src/main.rs): the page posts {cmd} with window.ipc.postMessage — hello, shown,
// drag, min, close, pick {dir}, install {dir, desktop}, open, log, refresh — and the host calls
// window.__host(event) with {ev: init | dir | step | progress | done | failed | declined}.
// Opened in a plain browser (no window.ipc), a small simulator plays the host (end of file).
'use strict';

const $ = id => document.getElementById(id);
const win = $('win'), body = $('body'), footL = $('footL'), footR = $('footR'), dot = $('dot'), ver = $('ver');
const reduced = () => matchMedia('(prefers-reduced-motion: reduce)').matches;
const EASE_OUT = 'cubic-bezier(.2,.8,.2,1)', EASE_IN = 'cubic-bezier(.4,0,1,1)';
const HTML_LANG = { zh_CN: 'zh-CN', en_US: 'en', de_DE: 'de', es_ES: 'es', fr_FR: 'fr', pt_BR: 'pt-BR', ja_JP: 'ja', ko_KR: 'ko', ru_RU: 'ru' };

let S = STRINGS.en_US;
let facts = null;           // the host's init event
let phase = 'ready';        // ready | working | done | failed
let dir = '', desktop = false, act = null, shown = 0, stage = -1;

const post = m => { if (window.ipc) window.ipc.postMessage(JSON.stringify(m)); else Sim.post(m); };
const esc = s => String(s == null ? '' : s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const t = (k, vars = {}) => (S[k] || STRINGS.en_US[k] || k).replace(/\{(\w+)\}/g, (_, n) => n in vars ? vars[n] : (n === 'v' ? facts.version : ''));
const isUp = () => facts.scene === 'update';
const isRe = () => facts.scene === 'reinstall';
const size = b => b >= 1e9 ? (b / 1e9).toFixed(1) + ' GB' : Math.max(1, Math.round(b / 1e6)) + ' MB';

// ---- motion helpers ------------------------------------------------------------------------
// swap the body: the old view fades where it was while the new one rises in (no blank frame)
function setBody(html) {
  const animate = !reduced() && !win.classList.contains('is-opening');
  const old = [...body.children].filter(n => !n.classList.contains('ghost'));
  if (old.length && animate) {
    const ghost = document.createElement('div');
    ghost.className = 'ghost';
    ghost.setAttribute('aria-hidden', 'true');
    old.forEach(n => ghost.appendChild(n));
    body.appendChild(ghost);
    ghost.animate([{ opacity: 1, translate: '0 0' }, { opacity: 0, translate: '0 -4px' }], { duration: 140, easing: EASE_IN, fill: 'forwards' })
      .finished.then(() => ghost.remove(), () => ghost.remove());
  } else old.forEach(n => n.remove());
  const tmp = document.createElement('div');
  tmp.innerHTML = html;
  [...tmp.children].forEach((n, i) => {
    body.insertBefore(n, body.querySelector('.ghost'));
    if (animate) n.animate([{ opacity: 0, translate: '0 6px' }, { opacity: 1, translate: '0 0' }], { duration: 260, delay: 70 + i * 40, easing: EASE_OUT, fill: 'backwards' });
  });
}

// swap a text cell: old out upward, new in from below, sharing one grid cell
function swapText(cell, html) {
  if (!cell) return;
  const cur = cell.lastElementChild;
  if (cur && cur.innerHTML === html) return;
  const n = document.createElement('span');
  n.innerHTML = html;
  cell.appendChild(n);
  if (!cur || reduced()) { if (cur) cur.remove(); return; }
  cur.animate([{ opacity: 1, translate: '0 0' }, { opacity: 0, translate: '0 -6px' }], { duration: 140, easing: EASE_IN, fill: 'forwards' })
    .finished.then(() => cur.remove(), () => cur.remove());
  n.animate([{ opacity: 0, translate: '0 6px' }, { opacity: 1, translate: '0 0' }], { duration: 220, delay: 40, easing: EASE_OUT, fill: 'backwards' });
}

// ---- the action: a button that becomes its own progress (ui/DESIGN.md §1-4, the run pill) --------
function makeAct() {
  const b = document.createElement('button');
  b.className = 'act';
  b.type = 'button';
  b.innerHTML = '<span class="act-fill"></span><span class="act-lab act-under"></span><span class="act-lab act-over" aria-hidden="true"></span>';
  b.addEventListener('click', () => { if (b.dataset.mode === 'button' && b._onClick) b._onClick(); });
  act = b;
  return b;
}
function actLabel(html) { act.querySelectorAll('.act-lab').forEach(c => swapText(c, html)); }
function actInstant(fn) {     // a solid control that turns into a track does it in one frame
  const parts = [act.querySelector('.act-fill'), act.querySelector('.act-over')];
  parts.forEach(p => { p.style.transition = 'none'; });
  fn();
  void act.offsetWidth;
  parts.forEach(p => { p.style.transition = ''; });
}
function actButton(label, onClick, kind = '') {
  act.className = 'act' + (kind ? ' is-' + kind : '');
  act.dataset.mode = 'button';
  act.disabled = false;
  act._onClick = onClick;
  actInstant(() => act.style.setProperty('--p', 1));
  actLabel(esc(label));
}
function actProgress(p) {    // never drains backwards
  if (act.dataset.mode !== 'track') {
    act.className = 'act';
    act.dataset.mode = 'track';
    shown = 0;
    actInstant(() => act.style.setProperty('--p', 0));
  }
  shown = Math.max(shown, Math.min(1, p));
  act.style.setProperty('--p', shown.toFixed(3));
  act.setAttribute('aria-valuenow', String(Math.round(shown * 100)));
  const txt = `<span class="num">${Math.round(shown * 100)}%</span>`;
  act.querySelectorAll('.act-lab').forEach(c => {
    const s = c.lastElementChild;
    if (s && s.querySelector('.num')) s.innerHTML = txt; else swapText(c, txt);
  });
}

// ---- views ---------------------------------------------------------------------------------
function footer(left, right) {
  footL.replaceChildren(...left);
  [...footR.children].forEach(n => { if (n !== act) n.remove(); });
  right.forEach(n => footR.insertBefore(n, act));
}
function ghostBtn(label, fn) {
  const b = document.createElement('button');
  b.className = 'btn'; b.type = 'button'; b.textContent = label; b.onclick = fn;
  return b;
}
function licenseLink() {
  const b = document.createElement('button');
  b.className = 'link'; b.type = 'button'; b.textContent = t('license'); b.onclick = openSheet;
  return b;
}
function setClose(visible) { $('wcClose').hidden = !visible; }

function renderReady(note) {
  phase = 'ready';
  setClose(true);
  dot.classList.remove('is-pending', 'is-landing', 'is-lit');
  ver.innerHTML = isUp() && facts.old
    ? `<span>${esc(facts.old)}</span><span class="arr">→</span><span>${esc(facts.version)}</span>`
    : esc(facts.version);
  const parts = [];
  if (facts.scene === 'fresh') {
    parts.push(`<div><div class="lab">${esc(t('loc'))}</div>
      <div class="field"><span class="path step" id="path"><span>${esc(dir)}</span></span><button class="btn-xs" id="chg" type="button">${esc(t('change'))}</button></div></div>`);
  } else {
    parts.push(`<div class="sub first">${esc(t('keep'))}</div>`);
  }
  // an existing desktop shortcut is kept as it is; otherwise the choice, off by default (.iss)
  if (!(facts.scene !== 'fresh' && facts.desktopExists)) {
    parts.push(`<label class="check"><input type="checkbox" id="desk" ${desktop ? 'checked' : ''}><span>${esc(t('shortcut'))}</span></label>`);
  }
  if (note) parts.push(`<div class="note is-err">${esc(note)}</div>`);
  else if (facts.running) parts.push(`<div class="note">${esc(t('running'))}</div>`);
  setBody(parts.join(''));
  const chg = $('chg');
  if (chg) chg.onclick = () => post({ cmd: 'pick', dir });
  const box = $('desk');
  if (box) box.onchange = () => { desktop = box.checked; };
  footer([licenseLink()], []);
  actButton(isUp() ? t('update') : isRe() ? t('reinstall') : t('install'), start);
}

function start() {
  phase = 'working';
  setClose(false);                                       // nothing to close into while files move
  dot.classList.add('is-pending');
  const head = isUp() ? t('w_update') : isRe() ? t('w_reinstall') : t('w_install');
  setBody(`<div class="title">${esc(head)}</div><div class="sub"><span class="step" id="step"></span></div>`);
  footer([], []);
  stage = -1;
  actProgress(0);
  swapText($('step'), esc(t('s_prepare')));
  post({ cmd: 'install', dir, desktop: facts.scene !== 'fresh' && facts.desktopExists ? true : desktop });
}

function onStep(step) {
  if (phase !== 'working') return;
  if (step === 'prepare') actProgress(.01);
  if (step === 'elevate') { actProgress(.02); swapText($('step'), esc(t('s_elevate'))); }
}

// stages from the .iss: 1 prepare (closes a running app), 2 files (permille), 3 after the files
function onProgress(st, permille) {
  if (phase !== 'working') return;
  if (st !== stage) {
    stage = st;
    const key = st === 1 ? (facts.running ? 's_close' : 's_prepare') : st === 2 ? 's_copy' : 's_links';
    swapText($('step'), esc(t(key)));
  }
  const p = st === 1 ? .03 : st === 2 ? .04 + .9 * (permille / 1000) : .96;
  actProgress(p);
}

function onDone(ev) {
  phase = 'done';
  setClose(true);
  dot.classList.remove('is-pending');
  actProgress(1);
  actLabel(`<svg class="tick" viewBox="0 0 16 16" aria-hidden="true"><path d="M3.5 8.4L6.6 11.4L12.5 5"/></svg>${esc(t(isUp() ? 'ok_update' : 'ok_install'))}`);
  dot.classList.add('is-lit');                           // the light lands
  if (!reduced()) { dot.classList.remove('is-landing'); void dot.getBBox(); dot.classList.add('is-landing'); }
  ver.textContent = facts.version;
  const title = isUp() ? t('d_update') : isRe() ? t('d_reinstall') : t('d_install');
  const sub = facts.scene === 'fresh' ? t(ev.desktop ? 'd_sub_desk' : 'd_sub') : t('d_sub_kept');
  setBody(`<div class="title">${esc(title)}</div><div class="sub">${esc(sub)}</div>`);
  setTimeout(() => {
    footer([], [ghostBtn(t('finish'), () => post({ cmd: 'close' }))]);
    actButton(t('open'), () => post({ cmd: 'open' }));
    act.focus({ preventScroll: true });
  }, 900);
}

function onFailed(ev) {
  phase = 'failed';
  setClose(true);
  dot.classList.remove('is-pending');
  const reason = {
    disk: () => t('e_disk', { need: size(ev.need), free: size(ev.free), drive: ev.drive }),
    dir: () => t('e_dir'),
    extract: () => t('e_extract', { detail: ev.detail }),
    launch: () => t('e_launch', { detail: ev.detail }),
    unchanged: () => t('e_unchanged'),
    rolledback: () => t('e_rolledback'),
    interrupted: () => t('e_interrupted'),
  }[ev.kind] || (() => t('e_unknown', { code: ev.code }));
  // what the machine is left with, said only where it is known
  const sub = ['disk', 'dir', 'extract', 'unchanged'].includes(ev.kind) ? t('sub_unchanged') : ev.kind === 'rolledback' ? t('sub_rolledback') : '';
  setBody(`<div class="title is-err">${esc(t('e_title'))}</div>
    <div class="sub reason">${esc(reason())}</div>${sub ? `<div class="sub">${esc(sub)}</div>` : ''}`);
  footer([], ev.log ? [ghostBtn(t('log'), () => post({ cmd: 'log' }))] : []);
  actButton(t('retry'), () => post({ cmd: 'refresh' }));
}

// ---- the licence sheet -----------------------------------------------------------------------
function openSheet() {
  win.classList.add('has-sheet');
  $('sheet').setAttribute('aria-hidden', 'false');
  $('sheetX').focus({ preventScroll: true });
}
function closeSheet() { win.classList.remove('has-sheet'); $('sheet').setAttribute('aria-hidden', 'true'); }
$('sheetX').onclick = closeSheet;
$('scrim').onclick = closeSheet;
// the licence file is hard-wrapped at 72 columns: reflow its paragraphs so they fill the sheet
fetch('license.txt').then(r => r.ok ? r.text() : '').then(text => {
  $('sheetBody').replaceChildren(...text.replace(/\r/g, '').split(/\n\s*\n/).map(par => {
    const lines = par.split('\n');
    const p = document.createElement('p');
    if (lines.every(l => /^\s{8,}\S/.test(l))) p.className = 'c';
    p.textContent = lines.map(l => l.trim()).join(' ');
    return p;
  }).filter(p => p.textContent));
}, () => {});

// ---- window ----------------------------------------------------------------------------------
$('wcClose').onclick = () => { if (phase !== 'working') post({ cmd: 'close' }); };
$('wcMin').onclick = () => post({ cmd: 'min' });
// the whole quiet ground moves the window (it has no title bar); controls and the sheet do not
win.addEventListener('mousedown', e => {
  if (e.button !== 0 || e.detail > 1) return;
  if (e.target.closest('button, input, label, a, .field, .sheet, .scrim')) return;
  post({ cmd: 'drag' });
});
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') closeSheet();
  if ((e.ctrlKey || e.metaKey) && ['r', 'p', 'f', 'g', 'j', 'u', '+', '-', '=', '0'].includes(e.key.toLowerCase())) e.preventDefault();
  if (e.key === 'F5' || e.key === 'F7') e.preventDefault();
});
document.addEventListener('contextmenu', e => { if (!e.target.closest('.sheet-b')) e.preventDefault(); });

let opened = false;
window.__host = ev => {
  switch (ev.ev) {
    case 'init': {
      const first = !facts;
      facts = ev;
      S = STRINGS[ev.lang] || STRINGS.en_US;
      if (first) {
        document.documentElement.lang = HTML_LANG[ev.lang] || 'en';
        $('sheetT').textContent = t('license');
        $('wcMin').setAttribute('aria-label', t('min'));
        $('wcClose').setAttribute('aria-label', t('close'));
        dir = ev.dir;
        footR.replaceChildren(makeAct());
      }
      if (first && !reduced()) win.classList.add('is-opening');
      renderReady();
      if (first) {
        // show the window once the faces are in, so the first frame is the drawn one
        const show = () => { if (opened) return; opened = true; post({ cmd: 'shown' }); setTimeout(() => win.classList.remove('is-opening'), 1300); };
        (document.fonts ? document.fonts.ready : Promise.resolve()).then(() => requestAnimationFrame(show));
        setTimeout(show, 800);
      }
      break;
    }
    case 'dir': dir = ev.dir; swapText($('path'), esc(dir)); break;
    case 'step': onStep(ev.step); break;
    case 'progress': onProgress(ev.stage, ev.permille); break;
    case 'done': onDone(ev); break;
    case 'failed': onFailed(ev); break;
    case 'declined': renderReady(t('declined')); break;
  }
};

// ---- a stand-in host for a plain browser (design review only; never runs in the product) ----
// ?lang=zh_CN&scene=fresh|update|reinstall&running=1&fail=4|disk|decline
const Sim = (() => {
  const q = new URLSearchParams(location.search);
  const scene = q.get('scene') || 'fresh';
  const say = ev => setTimeout(() => window.__host(ev), 0);
  const init = () => ({ ev: 'init', lang: q.get('lang') || 'zh_CN', version: '0.1.0', scene, old: scene === 'update' ? '0.0.9' : scene === 'reinstall' ? '0.1.0' : null,
    dir: 'C:\\Program Files\\Tesseract', desktopExists: false, running: q.get('running') === '1' });
  return {
    post(m) {
      if (m.cmd === 'hello' || m.cmd === 'refresh') say(init());
      if (m.cmd === 'pick') say({ ev: 'dir', dir: m.dir.startsWith('C:') ? 'D:\\Apps\\Tesseract' : 'C:\\Program Files\\Tesseract' });
      if (m.cmd === 'install') {
        const fail = q.get('fail');
        if (fail === 'disk') return say({ ev: 'failed', kind: 'disk', need: 312e6, free: 118e6, drive: 'C:' });
        say({ ev: 'step', step: 'prepare' });
        setTimeout(() => say({ ev: 'step', step: 'elevate' }), 300);
        if (fail === 'decline') return setTimeout(() => say({ ev: 'declined' }), 1400);
        let pm = 0;
        setTimeout(() => say({ ev: 'progress', stage: 1, permille: 0 }), 1200);
        const tick = () => {
          pm = Math.min(1000, pm + 40 + Math.random() * 90);
          if (fail === '4' && pm > 420) return say({ ev: 'failed', kind: 'rolledback', code: 4, log: 'x.log' });
          say({ ev: 'progress', stage: 2, permille: Math.round(pm) });
          if (pm < 1000) setTimeout(tick, 180 + Math.random() * 200);
          else { say({ ev: 'progress', stage: 3, permille: 1000 }); setTimeout(() => say({ ev: 'done', dir: 'C:\\Program Files\\Tesseract', desktop: true }), 500); }
        };
        setTimeout(tick, 1700);
      }
    },
  };
})();

post({ cmd: 'hello' });
