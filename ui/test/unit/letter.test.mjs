// 11-05 the letter for users (src/letter/letter.js, DESIGN §6.22): the product name in the sidebar
// opens it; an envelope marks an edition that has not been opened; the edition is a decision
// (LETTER_DIGEST pins its zh_CN words); every word it shows is in all nine catalogs; the standing
// hints it replaced are gone from the catalogs.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { UI_ROOT } from '../../build.mjs';
import { readCatalogs } from '../../build/locales.mjs';
import {
  CHROME_IDS, HEADING, LETTER_DIGEST, LETTER_EDITION, LETTER_IDS, PAGES, SIGNATURE, STORAGE_KEY, createLetterState,
} from '../../src/letter/letter.js';

const catalogs = readCatalogs(UI_ROOT);
const LANGS = Object.keys(catalogs);

function memStore(init) {
  const m = new Map(init ? Object.entries(init) : []);
  return { getItem: k => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)), map: m };
}

test('the edition is a decision: the zh_CN words of this edition are pinned', () => {
  const zh = catalogs.zh_CN;
  const text = LETTER_IDS.map(id => zh[id].text).join('\n');
  const digest = createHash('sha256').update(text).digest('hex').slice(0, 16);
  assert.equal(digest, LETTER_DIGEST,
    `the letter's words changed: if users should read it again, raise LETTER_EDITION (now ${LETTER_EDITION}); either way set LETTER_DIGEST to ${digest}`);
});

test('every word the letter shows is in all nine catalogs, and the namespace has no stray id', () => {
  assert.equal(LANGS.length, 9);
  const quoted = PAGES.flatMap(p => p.blocks.flatMap(b => (b.args ? Object.values(b.args) : [])));
  const used = [...LETTER_IDS, ...CHROME_IDS, ...quoted, 'mode.system.label', 'mode.tun.label', 'common.action.close'];
  for (const lang of LANGS) {
    const missing = used.filter(id => !catalogs[lang][id] || !String(catalogs[lang][id].text || '').trim());
    assert.deepEqual(missing, [], `${lang} lacks ${missing.join(', ')}`);
    const own = Object.keys(catalogs[lang]).filter(id => id.startsWith('letter.'));
    const stray = own.filter(id => !LETTER_IDS.includes(id) && !CHROME_IDS.includes(id));
    assert.deepEqual(stray, [], `${lang}: letter ids nothing shows`);
  }
  // the heading and the signature are not words of any catalog (never translated)
  assert.equal(HEADING, 'For users');
  assert.equal(SIGNATURE, 'Mark Joel Wexler');
});

test('the pages: six, the first a greeting with the letterhead, the last signed', () => {
  assert.equal(PAGES.length, 6);
  assert.deepEqual(PAGES[0].blocks.slice(0, 2).map(b => b.kind), ['letterhead', 'hello']);
  assert.equal(PAGES.at(-1).blocks.at(-1).kind, 'sign');
  for (const p of PAGES.slice(1)) assert.ok(p.title, `${p.name} has a title`);
});

test('unread until opened; remembered per edition; a new edition is unread again', () => {
  const s = memStore();
  const a = createLetterState({ storage: s });
  assert.equal(a.unread, true);
  a.markRead();
  assert.equal(a.unread, false);
  assert.deepEqual(JSON.parse(s.map.get(STORAGE_KEY)), { read: LETTER_EDITION });
  assert.equal(createLetterState({ storage: s }).unread, false, 'a new page session remembers it');
  const next = createLetterState({ storage: s, edition: LETTER_EDITION + 1 });
  assert.equal(next.unread, true, 'a new edition shows the envelope again');
  next.markRead();
  assert.equal(createLetterState({ storage: s }).unread, false, 'an older build stays read after a newer edition was read');
});

test('storage that fails or holds garbage: unread, then read for this page session only', () => {
  const broken = { getItem: () => { throw new Error('denied'); }, setItem: () => { throw new Error('denied'); } };
  const a = createLetterState({ storage: broken });
  assert.equal(a.unread, true);
  a.markRead();
  assert.equal(a.unread, false);
  assert.equal(createLetterState({ storage: memStore({ [STORAGE_KEY]: '{not json' }) }).unread, true);
  assert.equal(createLetterState({ storage: memStore({ [STORAGE_KEY]: '{"read":"1"}' }) }).unread, true);
  assert.equal(createLetterState({ storage: null }).unread, true);
});

test('index.html: the product name is the entry, the envelope is hidden until the page shows it', () => {
  const html = readFileSync(join(UI_ROOT, 'src', 'index.html'), 'utf8');
  const btn = html.match(/<button class="brand-name" id="letter-open"[^>]*>([\s\S]*?)<\/button>/);
  assert.ok(btn, 'the brand row has the #letter-open button');
  assert.match(btn[0], /title=""/, 'it hides the brand row title (the run state) from its own tooltip');
  assert.match(btn[1], /^<span class="wordmark">Tesseract<\/span>/, 'the wordmark is inside it, first');
  assert.match(btn[1], /<svg class="brand-mail"[^>]*aria-hidden="true"[^>]*hidden>/);
});

test('the standing hints the letter replaced are gone from every catalog and from frozen.json', () => {
  const gone = ['settings.conn.mode.system.desc', 'settings.conn.mode.tun.desc', 'settings.conn.ks.off.title', 'settings.conn.ks.off.body',
    'logs.reader.bastion.title', 'logs.reader.bastion.text'];
  for (const lang of LANGS) assert.deepEqual(gone.filter(id => catalogs[lang][id]), [], lang);
  const frozen = JSON.parse(readFileSync(join(UI_ROOT, 'src', 'i18n', 'frozen.json'), 'utf8'));
  assert.deepEqual(gone.filter(id => id in frozen), []);
});
