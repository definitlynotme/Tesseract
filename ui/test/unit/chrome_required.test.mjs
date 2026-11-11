// T-P4-12 fix 1 (第八波裁定 B SHOULD-3): the browser unit tests skip when the local Chrome is
// missing, and node --test does not count a skip as a failure. With TESSERACT_UI_REQUIRE_CHROME=1
// (P7 release CI must set it) a missing Chrome fails here instead, so "every skip is a registered
// entry" is enforced, not checked by hand. Without the variable this test only reports.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { CHROME, chromeAvailable } from '../fixtures/i18n/helpers.mjs';

const required = process.env.TESSERACT_UI_REQUIRE_CHROME === '1';

test('Chrome for the browser unit tests: required when TESSERACT_UI_REQUIRE_CHROME=1', t => {
  const found = chromeAvailable();
  if (!required) { t.diagnostic(`TESSERACT_UI_REQUIRE_CHROME not set; Chrome ${found ? 'found' : 'NOT found (browser tests will skip)'}: ${CHROME}`); return; }
  assert.ok(found, `TESSERACT_UI_REQUIRE_CHROME=1 but Chrome is not at ${CHROME} (set TESSERACT_UI_CHROME); the browser unit tests would skip`);
});
