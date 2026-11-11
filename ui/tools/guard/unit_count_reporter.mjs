// UI guard item 5 (T-P6-05, Claude ruling 1 of 10-26): a node --test reporter that counts, per test
// file, the test cases the parallel run actually ran, so the guard can check they are the same
// batch `node --test ui/test/unit/` collects (ui/tools/guard/unit_collect.mjs). Used next to the
// spec reporter:
//   node --test --test-reporter=spec --test-reporter-destination=stdout
//               --test-reporter=./ui/tools/guard/unit_count_reporter.mjs --test-reporter-destination=<json> ...
// Output: {"files": {"<abs path>": {"tests", "pass", "fail", "skipped", "todo", "subtests"}},
//          "total": n, "unattributed": n}
// A case is what the collector counts: a test() / it() that is not inside another test. A suite
// (describe) is not a case, the tests in it are; a subtest (t.test inside a test body) is not a
// case of its own (the collector never runs a test body, so it cannot see one; review A SHOULD 1):
// it is counted under "subtests" only. Completion events arrive children first (post-order), so
// each file's tree is rebuilt from the nesting levels. A file that fails to load shows up as a
// failing case of that file (node reports it so), never as missing silently.
export default async function* countReporter(source) {
  const pending = new Map(); // file -> [list of finished nodes per nesting level]
  let unattributed = 0;
  for await (const event of source) {
    if (event.type !== 'test:pass' && event.type !== 'test:fail') continue;
    const data = event.data || {};
    if (!data.file) { unattributed += 1; continue; }
    const levels = pending.get(data.file) || (pending.set(data.file, []), pending.get(data.file));
    const nesting = Number.isInteger(data.nesting) ? data.nesting : 0;
    const node = {
      suite: !!(data.details && data.details.type === 'suite'),
      skip: data.skip !== undefined && data.skip !== false,
      todo: data.todo !== undefined && data.todo !== false,
      pass: event.type === 'test:pass',
      children: levels[nesting + 1] || [],
    };
    levels[nesting + 1] = [];
    (levels[nesting] ||= []).push(node);
  }
  const files = {};
  let total = 0;
  for (const [file, levels] of pending) {
    const row = { tests: 0, pass: 0, fail: 0, skipped: 0, todo: 0, subtests: 0 };
    const visit = (node, insideTest) => {
      if (node.suite) { for (const c of node.children) visit(c, insideTest); return; }
      if (insideTest) row.subtests += 1;
      else {
        row.tests += 1;
        if (node.skip) row.skipped += 1;
        else if (node.todo) row.todo += 1;
        else if (node.pass) row.pass += 1;
        else row.fail += 1;
      }
      for (const c of node.children) visit(c, true);
    };
    for (const root of levels[0] || []) visit(root, false);
    files[file] = row;
    total += row.tests;
  }
  yield JSON.stringify({ files, total, unattributed }) + '\n';
}
