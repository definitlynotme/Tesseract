// `node --test ui/test/unit/` (the documented command, P4 architecture §12): Node 24
// resolves a directory argument as a module, i.e. this file. It loads every
// *.test.mjs next to it, so new test files need no registration here.
'use strict';
const { readdirSync } = require('node:fs');
const { join } = require('node:path');
const { pathToFileURL } = require('node:url');

(async () => {
  for (const name of readdirSync(__dirname).filter(n => n.endsWith('.test.mjs')).sort()) {
    await import(pathToFileURL(join(__dirname, name)).href);
  }
})().catch(err => { console.error(err); process.exitCode = 1; });
