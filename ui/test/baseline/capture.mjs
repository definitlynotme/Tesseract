// Prototype baseline capture (T-P4-01; P4 architecture §1.4, critique E1).
//
//   node ui/test/baseline/capture.mjs [--out <dir>] [--prototype-out <dir>] [--runs 2]
//
// Loads ui/dist/design/index.html (build it first: npm --prefix ui run build:design) from
// file:// in the local Chrome through playwright-core, with the deterministic fixture
// (test/render/fixture_clock.js), reduced motion, a fixed time zone and locale, 1280×800,
// and every non-file request aborted (no network; web fonts fall back to local fonts).
// For each page (overview, tiers, logs, settings) it writes <page>.textree.json (visible
// text tree) and <page>.png. It captures `--runs` times and fails unless the text trees of
// all runs are identical. With --prototype-out it also captures the prototype directory
// (.build/claude_ui_prototype) with the same fixture and compares the text trees (must be
// identical) and the pixels (reference only).
import { chromium } from 'playwright-core';
import { mkdirSync, writeFileSync, existsSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const UI = resolve(HERE, '..', '..');
const REPO = resolve(UI, '..');
const CHROME = process.env.TESSERACT_UI_CHROME || 'C:/Program Files/Google/Chrome/Application/chrome.exe';
export const PAGES = ['overview', 'tiers', 'logs', 'settings'];
export const VIEWPORT = { width: 1280, height: 800 };
export const SETTLE_MS = 3000; // virtual time advanced after load before capture

// Visible text tree: every visible element in DOM order with its own text (direct text
// nodes, whitespace collapsed) and its aria-label / title / placeholder. Elements with none
// of the four are omitted. Entry: [depth, tag, text, aria-label, title, placeholder].
export function textTreeInPage() {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const out = [];
  const visit = (el, depth) => {
    for (const c of el.children) {
      if (!c.checkVisibility({ opacityProperty: true, visibilityProperty: true })) {
        // visibility:hidden parents may still have visible children; display:none / opacity:0 do not.
        if (getComputedStyle(c).display !== 'none') visit(c, depth + 1);
        continue;
      }
      let own = '';
      for (const n of c.childNodes) if (n.nodeType === 3) own += n.nodeValue;
      const row = [depth, c.tagName.toLowerCase(), norm(own), norm(c.getAttribute('aria-label')), norm(c.getAttribute('title')), norm(c.getAttribute('placeholder'))];
      if (row[2] || row[3] || row[4] || row[5]) out.push(row);
      visit(c, depth + 1);
    }
  };
  visit(document.body, 0);
  return out;
}

export async function launch() {
  return chromium.launch({ executablePath: CHROME, headless: true, args: ['--disable-gpu', '--font-render-hinting=none'] });
}

export async function newContext(browser) {
  const context = await browser.newContext({
    viewport: VIEWPORT, deviceScaleFactor: 1, reducedMotion: 'reduce', locale: 'zh-CN', timezoneId: 'Asia/Shanghai', colorScheme: 'light',
  });
  await context.route('**/*', route => (route.request().url().startsWith('file:') ? route.continue() : route.abort()));
  await context.addInitScript({ path: join(UI, 'test', 'render', 'fixture_clock.js') });
  return context;
}

export async function capturePage(context, indexFile, pageId) {
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(String(e && e.message || e)));
  await page.goto(pathToFileURL(indexFile).href + '#' + pageId, { waitUntil: 'load' });
  await page.evaluate(ms => window.__fixtureClock.advance(ms), SETTLE_MS);
  await page.waitForTimeout(200); // real time, for 1 ms WAAPI animations (reduced motion) to finish
  await page.evaluate(() => window.__fixtureClock.advance(0));
  const tree = await page.evaluate(textTreeInPage);
  const png = await page.screenshot({ type: 'png' });
  const clockErrors = await page.evaluate(() => window.__fixtureClock.errors.slice());
  await page.close();
  return { tree, png, errors: [...errors, ...clockErrors] };
}

export async function captureAll(browser, indexFile) {
  const out = {};
  for (const id of PAGES) {
    const context = await newContext(browser); // fresh storage per page: same start state
    out[id] = await capturePage(context, indexFile, id);
    await context.close();
  }
  return out;
}

// Pixel comparison in the browser (no image library): count differing pixels.
async function pixelDiff(browser, a, b) {
  const page = await browser.newPage();
  const n = await page.evaluate(async ([x, y]) => {
    const load = src => new Promise((res, rej) => { const i = new Image(); i.onload = () => res(i); i.onerror = rej; i.src = src; });
    const [ia, ib] = await Promise.all([load(x), load(y)]);
    const w = Math.max(ia.width, ib.width), h = Math.max(ia.height, ib.height);
    const px = img => { const c = document.createElement('canvas'); c.width = w; c.height = h; const g = c.getContext('2d'); g.drawImage(img, 0, 0); return g.getImageData(0, 0, w, h).data; };
    const da = px(ia), db = px(ib);
    let diff = 0;
    for (let i = 0; i < da.length; i += 4) if (da[i] !== db[i] || da[i + 1] !== db[i + 1] || da[i + 2] !== db[i + 2] || da[i + 3] !== db[i + 3]) diff++;
    return { diff, total: w * h };
  }, ['data:image/png;base64,' + a.toString('base64'), 'data:image/png;base64,' + b.toString('base64')]);
  await page.close();
  return n;
}

function write(dir, shots) {
  mkdirSync(dir, { recursive: true });
  for (const [id, s] of Object.entries(shots)) {
    writeFileSync(join(dir, `${id}.textree.json`), JSON.stringify(s.tree, null, 0).replace(/\],\[/g, '],\n[') + '\n');
    writeFileSync(join(dir, `${id}.png`), s.png);
  }
}

const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

async function main(argv) {
  const o = { out: join(UI, 'test', 'baseline'), prototypeOut: null, runs: 2 };
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === '--out') o.out = resolve(argv[++i]);
    else if (argv[i] === '--prototype-out') o.prototypeOut = resolve(argv[++i]);
    else if (argv[i] === '--runs') o.runs = Math.max(2, Number(argv[++i]) || 2);
    else throw new Error(`unknown argument: ${argv[i]}`);
  }
  const designIndex = join(UI, 'dist', 'design', 'index.html');
  if (!existsSync(designIndex)) throw new Error('ui/dist/design/index.html missing: run npm --prefix ui run build:design');
  const browser = await launch();
  const summary = { viewport: VIEWPORT, settle_ms: SETTLE_MS, pages: {}, ok: true };
  try {
    const runs = [];
    for (let r = 0; r < o.runs; r++) runs.push(await captureAll(browser, designIndex));
    write(o.out, runs[0]);
    for (const id of PAGES) {
      const stable = runs.every(run => same(run[id].tree, runs[0][id].tree));
      const errors = runs[0][id].errors;
      summary.pages[id] = { entries: runs[0][id].tree.length, runs_identical: stable, png_identical_across_runs: runs.every(run => run[id].png.equals(runs[0][id].png)), page_errors: errors };
      if (!stable || errors.length) summary.ok = false;
    }
    if (o.prototypeOut) {
      const proto = await captureAll(browser, join(REPO, '.build', 'claude_ui_prototype', 'index.html'));
      write(o.prototypeOut, proto);
      for (const id of PAGES) {
        const textSame = same(proto[id].tree, runs[0][id].tree);
        const px = await pixelDiff(browser, proto[id].png, runs[0][id].png);
        Object.assign(summary.pages[id], { prototype_textree_identical: textSame, prototype_pixel_diff: px.diff, pixels: px.total, prototype_page_errors: proto[id].errors });
        if (!textSame) {
          summary.ok = false;
          const a = proto[id].tree.map(r => JSON.stringify(r)), b = runs[0][id].tree.map(r => JSON.stringify(r));
          const first = a.findIndex((x, i) => x !== b[i]);
          summary.pages[id].first_difference = { index: first, prototype: a[first] ?? null, design: b[first] ?? null };
        }
      }
      writeFileSync(join(o.prototypeOut, 'compare.json'), JSON.stringify(summary, null, 2) + '\n');
    }
  } finally {
    await browser.close();
  }
  console.log(JSON.stringify(summary, null, 2));
  return summary.ok ? 0 : 1;
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  main(process.argv.slice(2)).then(code => { process.exitCode = code; }, e => { console.error(e); process.exitCode = 1; });
}
