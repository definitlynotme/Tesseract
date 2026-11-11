// F-06 (11-04): the motion of a start on 总览 (pages/overview.js glide / charRoll / openPill, core.js
// T.motion.wave), in the real design target with the simulator (local Chrome through playwright-core,
// file://, no network):
//   - wave: closed it holds nothing; open, a change waits for its distance from the origin (the far
//     brand longest, never past max) and nothing once the window has closed; reduced motion never
//     opens it;
//   - a start with motion: at the press the blocks after the deck glide (a transform added to their
//     place) and the three deck cards unroll (clip-path); when the start ends the wave is open from
//     the run pill; afterwards nothing is left running, the hero's name is one plain face (span.sw
//     holding only its text: no characters, no reader copy), the accent layer is gone and the chart
//     is live;
//   - a running → starting → running blip (the runtime checking again after a node change) opens no
//     wave, a start from stopped does;
//   - reduced motion: the same start runs none of these animations and ends in the same faces.
// Skipped when Chrome is not installed (TESSERACT_UI_CHROME overrides the path).
import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { CHROME, chromeAvailable } from '../fixtures/i18n/helpers.mjs';
import { buildTarget, writeOutput } from '../../build.mjs';

const skip = !chromeAvailable() && 'local Chrome not found';

let dir, browser, url;
before(async () => {
  if (skip) return;
  const { chromium } = await import('playwright-core');
  dir = mkdtempSync(join(tmpdir(), 'tesseract-start-motion-'));
  const { files } = await buildTarget({ target: 'design' });
  writeOutput(join(dir, 'design'), files);
  url = pathToFileURL(join(dir, 'design', 'index.html')).href + '?lang=zh_CN#overview';
  browser = await chromium.launch({ executablePath: CHROME, headless: true, args: ['--disable-gpu'] });
});
after(async () => {
  if (browser) await browser.close();
  if (dir) rmSync(dir, { recursive: true, force: true });
});

// 总览, stopped and at rest (the simulator boots running)
async function open(reducedMotion) {
  const context = await browser.newContext({ locale: 'zh-CN', viewport: { width: 1280, height: 800 }, reducedMotion });
  await context.route(/^(?!file:)/, r => r.abort());
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.goto(url);
  await page.waitForFunction(() => window.T && T.router.current === 'overview' && ['running', 'stopped'].includes(T.store.get('run')));
  // (a pointer click within 400 ms of the pill's last change is ignored by design: wait past it)
  await page.waitForTimeout(800);
  if (await page.evaluate(() => T.store.get('run')) !== 'stopped') {
    await page.click('.run-act');
    await page.waitForFunction(() => T.store.get('run') === 'stopped');
  }
  await page.waitForTimeout(1200);
  return { context, page, errors };
}

// a start from the 启动服务 pill, watched from inside the page: the animations F-06 adds (a glide is
// a transform added to a block's place, an unroll / opening / bloom a clip-path, the hero's characters,
// the chart's sweep) and whether the wave is open when the start ends
async function start(page) {
  await page.evaluate(() => {
    const seen = window.__f06 = { glide: [], unroll: 0, chars: 0, sweep: 0, clips: 0, waveAtEnd: null };
    const fx = a => a.effect && a.effect.target;
    const t0 = performance.now();
    const loop = () => {
      for (const a of document.getAnimations()) {
        const el = fx(a); if (!el) continue;
        if (a.effect.composite === 'add') { const id = el.id || el.className; if (!seen.glide.includes(id)) seen.glide.push(id); }
        if (el.classList && el.classList.contains('cr-c')) seen.chars++;
        if (el.classList && el.classList.contains('ch-cr') && document.querySelector('#ov-chart.is-sweep')) seen.sweep++;
        if (a.effect.getKeyframes().some(k => k.clipPath) && el.closest && (el.closest('#ov-deck') || el.closest('#run'))) seen.clips++;
      }
      seen.unroll = Math.max(seen.unroll, [...document.querySelectorAll('#ov-deck > .dcard')]
        .filter(c => c.getAnimations().some(a => a.effect.getKeyframes().some(k => k.clipPath))).length);
      if (performance.now() - t0 < 9000) requestAnimationFrame(loop);
    };
    requestAnimationFrame(loop);
    T.on('rt:state', ev => { if (ev.run === 'running' && ev.prev === 'starting' && seen.waveAtEnd == null) seen.waveAtEnd = T.motion.wave.delay(document.querySelector('.brand')); });
  });
  await page.click('.run-main');
  await page.waitForFunction(() => T.store.get('run') === 'running', null, { timeout: 15000 });
  await page.waitForTimeout(6500);
  return page.evaluate(() => {
    const name = document.getElementById('ov-name'), faces = [...name.children];
    return {
      seen: window.__f06,
      running: document.getAnimations().filter(a => a.playState === 'running').map(a => String(a.effect && a.effect.target && (a.effect.target.id || a.effect.target.className))),
      faces: faces.map(f => f.className), faceNodes: faces.map(f => [...f.childNodes].map(n => n.nodeType)),
      name: name.textContent, inner: name.querySelectorAll('.cr-c, .sr-only, .cr-v').length,
      pill: document.querySelector('#run .run-base .run-row:not(.is-out)').textContent.replace(/\d/g, '0'),
      state: document.getElementById('run').dataset.state, over: getComputedStyle(document.querySelector('#run .run-over')).opacity,
      bloom: document.querySelector('#run .run-base').classList.contains('is-bloom'), chart: document.getElementById('ov-chart').className,
    };
  });
}

test('wave: holds a change for its distance while open, nothing when closed or without motion', { skip, timeout: 120000 }, async () => {
  for (const rm of ['no-preference', 'reduce']) {
    const { context, page, errors } = await open(rm);
    const r = await page.evaluate(async () => {
      const W = T.motion.wave, near = document.getElementById('run'), far = document.querySelector('.brand');
      const closed = W.delay(far);
      const p = near.getBoundingClientRect();
      W.start({ x: p.left + p.width / 2, y: p.top + p.height / 2 }, { speed: 0.5, max: 460, span: 300 });
      const dn = W.delay(near), df = W.delay(far);
      await new Promise(res => setTimeout(res, 400));
      return { closed, dn, df, after: W.delay(far) };
    });
    if (rm === 'reduce') assert.deepEqual(r, { closed: 0, dn: 0, df: 0, after: 0 }, 'reduced motion never opens a wave');
    else {
      assert.equal(r.closed, 0, 'no wave open: nothing waits');
      assert.ok(r.dn < 20, `the origin's own element waits about nothing (${r.dn} ms)`);
      assert.ok(r.df > 300 && r.df <= 460, `the far brand waits for its distance, capped at max (${r.df} ms)`);
      assert.equal(r.after, 0, 'the window closes by itself');
    }
    assert.deepEqual(errors, []);
    await context.close();
  }
});

test('a start: the page glides and the deck unrolls at the press, the wave opens at the end, nothing is left running', { skip, timeout: 120000 }, async () => {
  const { context, page, errors } = await open('no-preference');
  const r = await start(page);
  assert.ok(r.seen.glide.includes('ov-strip') && r.seen.glide.some(g => /\bov-traffic\b/.test(g)), `the blocks after the deck glide: ${r.seen.glide.join(', ')}`);
  assert.equal(r.seen.unroll, 3, 'the three cards unroll from their bands');
  assert.ok(r.seen.chars > 0, 'the hero name rolls character by character');
  assert.ok(r.seen.sweep > 0, 'the chart sweeps into colour');
  assert.ok(r.seen.waveAtEnd > 0, `the wave is open from the pill when the start ends (brand waits ${r.seen.waveAtEnd} ms)`);
  assert.deepEqual(r.running, [], 'nothing is left running');
  assert.deepEqual(r.faces, ['sw cr-f'], 'one face, the carrier name');
  assert.deepEqual(r.faceNodes, [[3]], 'at rest the face is its text alone');
  assert.equal(r.inner, 0, 'no characters or reader copy left');
  assert.equal(r.state, 'running');
  assert.equal(r.over, '0', 'the accent layer is gone');
  assert.equal(r.bloom, false);
  assert.ok(!/\bis-(paused|sweep)\b/.test(r.chart), `the chart is live: ${r.chart}`);
  assert.deepEqual(errors, []);
  await context.close();
});

test('a running → starting → running blip opens no wave; a start from stopped does', { skip, timeout: 120000 }, async () => {
  const { context, page, errors } = await open('no-preference');
  await page.click('.run-main');
  await page.waitForFunction(() => T.store.get('run') === 'running', null, { timeout: 15000 });
  await page.waitForTimeout(2500);
  const r = await page.evaluate(() => {
    const far = document.querySelector('.brand'), edge = (run, prev) => T.emit('rt:state', { run, prev, cancelled: false, instant: false });
    edge('starting', 'running'); edge('running', 'starting');
    const blip = T.motion.wave.delay(far);
    edge('starting', 'stopped'); edge('running', 'starting');
    return { blip, fresh: T.motion.wave.delay(far) };
  });
  assert.equal(r.blip, 0, 'no wave after a blip');
  assert.ok(r.fresh > 0, `a start from stopped opens it (${r.fresh} ms)`);
  assert.deepEqual(errors, []);
  await context.close();
});

test('reduced motion: none of the start animations run and the start ends in the same faces', { skip, timeout: 120000 }, async () => {
  const runs = {};
  for (const rm of ['reduce', 'no-preference']) {
    const { context, page, errors } = await open(rm);
    runs[rm] = await start(page);
    assert.deepEqual(errors, []);
    await context.close();
  }
  const s = runs.reduce.seen;
  assert.deepEqual([s.glide, s.unroll, s.chars, s.sweep, s.clips, s.waveAtEnd], [[], 0, 0, 0, 0, 0], 'no glide, unroll, roll, sweep, opening or wave');
  for (const k of ['faces', 'faceNodes', 'name', 'inner', 'pill', 'state', 'over', 'bloom', 'chart']) assert.deepEqual(runs.reduce[k], runs['no-preference'][k], `${k} as with motion`);
});
