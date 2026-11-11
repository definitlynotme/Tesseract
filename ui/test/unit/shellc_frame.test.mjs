// 11-05 in-page title bar (shellc/frame.js; PROTOCOL section 13 host_window / host:window):
// the three window buttons ask the shell through host_window{action}; host:window{maximized}
// picks the maximize or restore glyph and words; index.html carries exactly these buttons, with
// catalog words, outside #app (never made inert by the launch cover) and out of the tab order.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createFrame, WINDOW_ACTIONS } from '../../src/shellc/frame.js';
import { HOST_INVOKES } from '../../src/shellc/index.js';
import { HOST_INVOKE_NAMES } from '../../src/rt/client.js';

const UI = new URL('../../', import.meta.url);

function fakeDom() {
  const attrs = new Map();
  let click = null;
  const button = action => ({
    action, closest: sel => (sel === '[data-wc]' ? button.cache[action] : null), getAttribute: n => (n === 'data-wc' ? action : null),
  });
  button.cache = {};
  for (const a of [...WINDOW_ACTIONS, 'hide']) button.cache[a] = button(a);
  const root = {
    setAttribute: (n, v) => attrs.set(n, v), getAttribute: n => attrs.get(n),
    addEventListener: (type, fn) => { if (type === 'click') click = fn; },
    querySelector: sel => (sel === '[data-wc="toggle_maximize"]' ? button.cache.toggle_maximize : null),
    contains: el => Object.values(button.cache).includes(el),
  };
  return {
    doc: { getElementById: id => (id === 'wc' ? root : null) }, root, attrs,
    click: action => click({ target: button.cache[action] }),
  };
}
function frameUnderTest() {
  const dom = fakeDom();
  const handlers = new Map();
  const calls = [], labels = [];
  const frame = createFrame({
    doc: dom.doc, on: (name, fn) => handlers.set(name, fn),
    host: (name, args) => { calls.push([name, args]); return Promise.resolve(null); },
    label: (el, id) => labels.push([el.action, id]),
  });
  return { dom, frame, calls, labels, send: f => handlers.get('host:window')(f) };
}

test('each window button asks the shell for its own action; nothing else is sent', () => {
  const t = frameUnderTest();
  for (const a of WINDOW_ACTIONS) t.dom.click(a);
  t.dom.click('hide');
  assert.deepEqual(t.calls, WINDOW_ACTIONS.map(action => ['host_window', { action }]));
  assert.deepEqual([...WINDOW_ACTIONS], ['minimize', 'toggle_maximize', 'close']);
});

test('host:window{maximized} swaps the glyph and the words of the maximize button, once per change', () => {
  const t = frameUnderTest();
  assert.equal(t.dom.attrs.get('data-max'), 'false', 'not maximized until the shell says so');
  assert.deepEqual(t.labels, []);
  t.send({ t: 'host:window', maximized: true, epoch: 'e' });
  assert.equal(t.dom.attrs.get('data-max'), 'true');
  assert.deepEqual(t.labels, [['toggle_maximize', 'shell.window.restore']]);
  t.send({ t: 'host:window', maximized: true, epoch: 'e' });
  for (const bad of [null, {}, { maximized: 'true' }, { maximized: 1 }]) t.send(bad);
  assert.equal(t.labels.length, 1, 'the same value or a malformed frame changes nothing');
  t.send({ t: 'host:window', maximized: false, epoch: 'e' });
  assert.equal(t.dom.attrs.get('data-max'), 'false');
  assert.deepEqual(t.labels.at(-1), ['toggle_maximize', 'shell.window.maximize']);
  assert.equal(t.frame.maximized, false);
});

test('without #wc (a page without a window) the module stays quiet', () => {
  const handlers = new Map();
  const frame = createFrame({ doc: { getElementById: () => null }, on: (n, fn) => handlers.set(n, fn), host: () => assert.fail('no host call') });
  handlers.get('host:window')({ maximized: true });
  assert.equal(frame.maximized, true);
  createFrame({ doc: null, on: () => {}, host: () => assert.fail('no host call') });
});

test('host_window is a page invoke on both lists (PROTOCOL section 13: ten names)', () => {
  assert.ok(HOST_INVOKES.includes('host_window') && HOST_INVOKE_NAMES.includes('host_window'));
  assert.equal(HOST_INVOKES.length, 10);
});

test('index.html: the three buttons, after #app, out of the tab order, labelled from the catalog', () => {
  const html = readFileSync(new URL('src/index.html', UI), 'utf8');
  const at = html.indexOf('<div class="wc" id="wc"');
  assert.ok(at > html.indexOf('<div id="layer"></div>') && at > html.indexOf('</main>'), '#wc is outside #app');
  const block = html.slice(at, html.indexOf('\n</div>', at));
  const buttons = [...block.matchAll(/<button [^>]*>/g)].map(m => m[0]);
  assert.deepEqual(buttons.map(b => b.match(/data-wc="([^"]+)"/)[1]), [...WINDOW_ACTIONS]);
  const zh = JSON.parse(readFileSync(new URL('src/i18n/locales/zh_CN/shell.json', UI), 'utf8'));
  for (const b of buttons) {
    assert.match(b, /tabindex="-1"/);
    const ids = b.match(/data-i18n-attr="aria-label:([^;"]+);data-tip:([^;"]+)"/);
    assert.ok(ids && ids[1] === ids[2] && zh[ids[1]], b);
  }
  assert.ok(zh['shell.window.restore'], 'the restore words exist for the maximized state');
});

test('shell.css: the title band drags, its controls and the window buttons do not; the buttons sit above every layer', () => {
  const css = readFileSync(new URL('css/shell.css', UI), 'utf8');
  const tokens = readFileSync(new URL('css/tokens.css', UI), 'utf8');
  assert.match(css, /\.topbar, \.side::before \{ -webkit-app-region: drag; app-region: drag; \}/);
  for (const sel of ['.topbar button', '.toast-slot > *', '.brand-mark', '.wc']) assert.ok(css.includes(sel), sel);
  assert.match(css, /app-region: no-drag/);
  const z = Number(tokens.match(/--z-frame:\s*(\d+)/)[1]);
  const launch = Number(css.match(/\.launch \{[^}]*z-index:\s*(\d+)/)[1]);
  const top = Math.max(...[...tokens.matchAll(/--z-[a-z]+:\s*(\d+)/g)].filter(m => !m[0].startsWith('--z-frame')).map(m => Number(m[1])));
  assert.ok(z > launch && z > top, `--z-frame ${z} above the launch ${launch} and every other layer ${top}`);
});
