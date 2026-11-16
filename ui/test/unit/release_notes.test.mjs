// Release notes in the page's language (src/i18n/release_notes.js; 11-16).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { releaseNotesFor, splitReleaseNotes } from '../../src/i18n/release_notes.js';

const NOTES = '[zh_CN]\n修复\n- 首次运行可以导入订阅\n\n[en_US]\nFixed\n- Subscriptions import on a first run\n';

test('notes with language sections show the page language\'s section', () => {
  assert.equal(releaseNotesFor(NOTES, 'zh_CN'), '修复\n- 首次运行可以导入订阅');
  assert.equal(releaseNotesFor(NOTES, 'en_US'), 'Fixed\n- Subscriptions import on a first run');
});

test('a language without a section gets English, else the first section', () => {
  assert.equal(releaseNotesFor(NOTES, 'ja_JP'), 'Fixed\n- Subscriptions import on a first run');
  assert.equal(releaseNotesFor(NOTES, null), 'Fixed\n- Subscriptions import on a first run');
  assert.equal(releaseNotesFor('[zh_CN]\n修复\n[de_DE]\nBehoben', 'fr_FR'), '修复');
  // an empty section is no section: the fallback applies
  assert.equal(releaseNotesFor('[ja_JP]\n\n[en_US]\nFixed', 'ja_JP'), 'Fixed');
});

test('text before the first marker belongs to every language', () => {
  const notes = 'Tesseract 0.2.0\n\n[zh_CN]\n修复\n[en_US]\nFixed';
  assert.equal(releaseNotesFor(notes, 'zh_CN'), 'Tesseract 0.2.0\n\n修复');
  assert.equal(releaseNotesFor(notes, 'en_US'), 'Tesseract 0.2.0\n\nFixed');
});

test('notes without a marker (0.1.1 and older) are shown whole', () => {
  const legacy = '修复：全新安装\n- 一\n\nFixed: a fresh install\n- one';
  assert.equal(releaseNotesFor(legacy, 'en_US'), legacy);
  assert.equal(releaseNotesFor(`\n${legacy}\n\n`, 'zh_CN'), legacy);
  assert.equal(splitReleaseNotes(legacy).sections, null);
});

test('nothing to show is null', () => {
  for (const notes of [null, undefined, '', '   \n', '[zh_CN]\n\n[en_US]\n  ']) assert.equal(releaseNotesFor(notes, 'zh_CN'), null, JSON.stringify(notes));
});

test('only a whole line of a bracketed language code is a marker', () => {
  const notes = '[zh_CN]\n- see [en_US] below\n[en-US]\n[EN_us]\n  [en_US]  \nFixed';
  assert.equal(releaseNotesFor(notes, 'zh_CN'), '- see [en_US] below\n[en-US]\n[EN_us]');
  assert.equal(releaseNotesFor(notes, 'en_US'), 'Fixed');
  assert.equal(releaseNotesFor('[zh_CN]\r\n修复\r\n[en_US]\r\nFixed', 'en_US'), 'Fixed');
});

test('a repeated marker continues its language\'s section', () => {
  assert.equal(releaseNotesFor('[en_US]\nA\n[zh_CN]\n甲\n[en_US]\nB', 'en_US'), 'A\nB');
});

test('the committed release notes with sections carry Simplified Chinese and English', () => {
  const dir = resolve(dirname(fileURLToPath(import.meta.url)), '../../../docs/release-notes');
  for (const name of readdirSync(dir).filter(n => n.endsWith('.txt'))) {
    const { sections } = splitReleaseNotes(readFileSync(join(dir, name), 'utf8'));
    if (!sections) continue;
    assert.ok(sections.has('zh_CN') && sections.has('en_US'), name);
  }
});
