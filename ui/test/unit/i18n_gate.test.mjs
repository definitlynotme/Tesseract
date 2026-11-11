// T-P4-03: i18n_check.mjs (architecture §3.9) — every check has a passing and a failing fixture
// (ui/test/fixtures/i18n), levels come from i18n_gates.json, src_hash drift counts as missing,
// the CSS @i18n-pending exemption; backend_keys.py and the three catalogs on the real repository.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync, rmSync, writeFileSync, mkdtempSync, mkdirSync, utimesSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { HERE, REPO, UI, materialize } from '../fixtures/i18n/helpers.mjs';
import { CHECKS, ID_RE, runChecks, main, srcHash, parseNativeText } from '../../tools/i18n_check.mjs';
import { runRule } from '../../tools/scan.mjs';

const PY = join(REPO, '.venv', 'Scripts', 'python.exe');
const cases = readdirSync(HERE, { withFileTypes: true }).filter(d => d.isDirectory() && /^(fail|pass)_/.test(d.name)).map(d => d.name).sort();
const run = (fx, extra = {}) => runChecks({ ...fx.opts, ...extra });
const check = (res, id) => res.checks.find(c => c.id === id);

test('base fixture passes every check at p4 and p7', () => {
  const fx = materialize('base');
  try {
    for (const level of ['p4', 'p7']) {
      const res = run(fx, { level });
      assert.deepEqual(res.failed, [], `${level}: ${JSON.stringify(res.checks.filter(c => !c.ok))}`);
      assert.deepEqual(res.checks.map(c => c.id), CHECKS);
      assert.ok(res.checks.every(c => c.level === 'hard'));
    }
  } finally { rmSync(fx.dir, { recursive: true, force: true }); }
});

test('every check has a failing fixture, and each one fails', () => {
  const covered = new Set();
  for (const name of cases) {
    const fx = materialize(name);
    try {
      const res = run(fx);
      if (fx.expect) {
        covered.add(fx.expect);
        assert.ok(res.failed.includes(fx.expect), `${name}: expected ${fx.expect} to fail, failed = ${res.failed}`);
        assert.ok(check(res, fx.expect).count > 0, name);
      } else {
        assert.deepEqual(res.failed, [], `${name}: ${JSON.stringify(res.checks.filter(c => !c.ok).map(c => [c.id, c.items]))}`);
      }
    } finally { rmSync(fx.dir, { recursive: true, force: true }); }
  }
  assert.deepEqual(CHECKS.filter(c => !covered.has(c)), [], 'every check has a failing fixture');
});

test('specific findings: stale src_hash is missing, glossary allow list needs a reason, unresolved key site fails', () => {
  let fx = materialize('fail_missing_stale');
  try {
    const items = check(run(fx), 'missing').items;
    assert.deepEqual(items, [{ lang: 'ja_JP', id: 'common.action.save', why: 'stale src_hash' }]);
  } finally { rmSync(fx.dir, { recursive: true, force: true }); }
  fx = materialize('fail_missing_empty');
  try { assert.deepEqual(check(run(fx), 'missing').items, [{ lang: 'ko_KR', id: 'catalog.ss', why: 'empty' }]); } finally { rmSync(fx.dir, { recursive: true, force: true }); }
  fx = materialize('fail_glossary');
  try {
    const items = check(run(fx), 'glossary').items.map(i => `${i.lang}:${i.id}:${i.term_id}`).sort();
    assert.deepEqual(items, ['en_US:common.items:node', 'en_US:common.tier:tier_vanguard']);
  } finally { rmSync(fx.dir, { recursive: true, force: true }); }
  fx = materialize('fail_glossary_allow_without_reason');
  try { assert.deepEqual(check(run(fx), 'glossary').items.map(i => i.id), ['common.items'], 'an exception without a reason does not count'); } finally { rmSync(fx.dir, { recursive: true, force: true }); }
  fx = materialize('fail_backend_unresolved');
  try { assert.match(check(run(fx), 'backend_keys').items[0].msg, /unresolved dynamic key site bridge_z\.py:9/); } finally { rmSync(fx.dir, { recursive: true, force: true }); }
  fx = materialize('fail_backend_extra_errors');
  try {
    const bk = check(run(fx), 'backend_keys');
    assert.deepEqual(bk.extra_in_errors_json, ['errors.page_only']);
    assert.deepEqual(bk.items.map(i => i.id), ['errors.page_only'], 'a front-end id in errors.json fails the hard check (critique C6)');
  } finally { rmSync(fx.dir, { recursive: true, force: true }); }
  fx = materialize('fail_json_reserved');
  try {
    const res = run(fx);
    assert.deepEqual(check(res, 'json').items.map(i => i.id).sort(), ['__proto__', 'constructor']);
    assert.ok(!check(res, 'id_format').items.some(i => i.id === '__proto__'), 'reported once, as a json error');
  } finally { rmSync(fx.dir, { recursive: true, force: true }); }
  fx = materialize('fail_three_catalogs');
  try {
    const items = check(run(fx), 'three_catalogs').items.map(i => `${i.catalog}:${i.lang}:${i.msg.split(' ')[0]}`).sort();
    assert.deepEqual(items, ['native:ko_KR:placeholders', 'python:de_DE:key', 'python:fr_FR:placeholders']);
  } finally { rmSync(fx.dir, { recursive: true, force: true }); }
});

test('i18n_gates.json: one line switches a check between report and hard; p7 uses its own column; a missing line is hard', () => {
  const fx = materialize('fail_plural');
  try {
    const gatesPath = join(fx.dir, 'tools', 'i18n_gates.json');
    const gates = JSON.parse(readFileSync(gatesPath, 'utf8'));
    assert.ok(run(fx).failed.includes('plural'));
    gates.plural = { p4: 'report', p7: 'hard' };
    writeFileSync(gatesPath, JSON.stringify(gates, null, 2));
    let res = run(fx);
    assert.ok(!res.failed.includes('plural'), 'report: no failure');
    assert.equal(check(res, 'plural').level, 'report');
    assert.ok(check(res, 'plural').count > 0, 'still counted in the report');
    assert.ok(run(fx, { level: 'p7' }).failed.includes('plural'), 'p7 column is hard');
    delete gates.plural;
    gates.no_such_check = { p4: 'hard', p7: 'hard' };
    writeFileSync(gatesPath, JSON.stringify(gates, null, 2));
    res = run(fx);
    assert.ok(res.failed.includes('plural'), 'missing line → hard');
    assert.deepEqual(res.unknown_gates, ['no_such_check']);
  } finally { rmSync(fx.dir, { recursive: true, force: true }); }
});

test('a hard check that cannot run fails (truncation summary missing at p7)', () => {
  const fx = materialize('base');
  try {
    const gatesPath = join(fx.dir, 'tools', 'i18n_gates.json');
    const gates = JSON.parse(readFileSync(gatesPath, 'utf8'));
    gates.truncation = { p4: 'report', p7: 'hard' };
    writeFileSync(gatesPath, JSON.stringify(gates, null, 2));
    const opts = { truncation: join(fx.dir, 'external', 'nope.json') };
    assert.deepEqual(run(fx, opts).failed, []);
    const p7 = run(fx, { ...opts, level: 'p7' });
    assert.deepEqual(p7.failed, ['truncation']);
    assert.match(check(p7, 'truncation').error, /no truncation summary/);
  } finally { rmSync(fx.dir, { recursive: true, force: true }); }
});

test('a half-written or broken truncation summary is the check\'s error, not a crash of the gate', () => {
  const fx = materialize('base');
  try {
    const gatesPath = join(fx.dir, 'tools', 'i18n_gates.json');
    const gates = JSON.parse(readFileSync(gatesPath, 'utf8'));
    gates.truncation = { p4: 'report', p7: 'hard' };
    writeFileSync(gatesPath, JSON.stringify(gates, null, 2));
    for (const body of ['{"langs": ["zh_CN"], "items": [{"lang": "zh_C', '{"items": 3}', 'null']) {
      const truncation = join(fx.dir, 'external', 'broken.json');
      writeFileSync(truncation, body);
      const p4 = run(fx, { truncation });
      assert.deepEqual(p4.failed, [], body);
      assert.match(check(p4, 'truncation').error, /broken\.json/, body);
      assert.deepEqual(run(fx, { truncation, level: 'p7' }).failed, ['truncation'], body);
    }
  } finally { rmSync(fx.dir, { recursive: true, force: true }); }
});

test('T-P4-12: found by itself, only a complete truncation summary counts; a newer partial run is skipped', () => {
  const fx = materialize('base');
  const repo = mkdtempSync(join(tmpdir(), 'tesseract-i18n-trunc-'));
  try {
    const base = join(repo, '.build', '新界面迁移_工单', 'evidence', 'render');
    const put = (run, doc, mtime) => {
      const d = join(base, run); mkdirSync(d, { recursive: true });
      const p = join(d, 'truncation.json'); writeFileSync(p, JSON.stringify(doc)); utimesSync(p, mtime, mtime);
    };
    const item = { case: 'c', lang: 'de_DE', size: '1280x800', id: 'x.y' };
    const cur = fx.opts.sourceDigest;
    put('full', { langs: ['de_DE'], items: [], coverage: { complete: true, source_digest: cur } }, 1000);
    put('partial', { langs: ['de_DE'], items: [item], coverage: { complete: false, source_digest: cur } }, 2000);
    put('legacy', { langs: ['de_DE'], items: [item] }, 3000);
    const { truncation: _drop, ...opts } = fx.opts;
    let c = check(runChecks({ ...opts, repo }), 'truncation');
    assert.equal(c.source.endsWith('full/truncation.json'), true, c.source);
    assert.equal(c.complete, true);
    assert.equal(c.digest_match, true);
    assert.equal(c.skipped_partial, 2);
    assert.equal(c.count, 0, 'the partial runs\' items are not read');
    rmSync(join(base, 'full'), { recursive: true, force: true });
    c = check(runChecks({ ...opts, repo }), 'truncation');
    assert.match(c.error, /no truncation summary/);
    assert.match(c.error, /2 newer partial/);
    // an explicit file is read as given and says so; where truncation is hard (this fixture: p4 too),
    // an incomplete one is the check's error
    c = check(runChecks({ ...opts, repo, truncation: join(base, 'partial', 'truncation.json') }), 'truncation');
    assert.equal(c.complete, false);
    assert.equal(c.count, 1);
    assert.match(c.error, /coverage\.complete is not true/);
  } finally { rmSync(fx.dir, { recursive: true, force: true }); rmSync(repo, { recursive: true, force: true }); }
});

test('T-P4-12 fix 1: a complete summary of another source digest is stale — skipped when found, an error at the hard level when explicit', () => {
  const fx = materialize('base');
  const repo = mkdtempSync(join(tmpdir(), 'tesseract-i18n-stale-'));
  try {
    const gatesPath = join(fx.dir, 'tools', 'i18n_gates.json');
    const gates = JSON.parse(readFileSync(gatesPath, 'utf8'));
    gates.truncation = { p4: 'report', p7: 'hard' };
    writeFileSync(gatesPath, JSON.stringify(gates, null, 2));
    const base = join(repo, '.build', '新界面迁移_工单', 'evidence', 'render');
    const put = (run, doc, mtime) => {
      const d = join(base, run); mkdirSync(d, { recursive: true });
      const p = join(d, 'truncation.json'); writeFileSync(p, JSON.stringify(doc)); utimesSync(p, mtime, mtime);
      return p;
    };
    const item = { case: 'c', lang: 'de_DE', size: '1280x800', id: 'x.y' };
    const current = put('current', { langs: ['de_DE'], items: [], coverage: { complete: true, source_digest: fx.opts.sourceDigest } }, 1000);
    const stale = put('stale', { langs: ['de_DE'], items: [item], coverage: { complete: true, source_digest: 'an-older-build' } }, 2000);
    const { truncation: _drop, ...opts } = fx.opts;
    // found by itself: the newer stale one is skipped and counted, the current one is read
    let c = check(runChecks({ ...opts, repo, level: 'p7' }), 'truncation');
    assert.equal(c.source.endsWith('current/truncation.json'), true, c.source);
    assert.equal(c.skipped_stale, 1);
    assert.equal(c.ok, true);
    rmSync(current);
    let res = runChecks({ ...opts, repo, level: 'p7' });
    c = check(res, 'truncation');
    assert.match(c.error, /no truncation summary/);
    assert.match(c.error, /1 complete but stale/);
    assert.ok(res.failed.includes('truncation'));
    // p4 (report): the same state is reported, not failed
    res = runChecks({ ...opts, repo, level: 'p4' });
    assert.ok(!res.failed.includes('truncation'));
    // explicit: digest_match false; p4 annotates only, p7 fails even with zero items
    writeFileSync(stale, JSON.stringify({ langs: ['de_DE'], items: [], coverage: { complete: true, source_digest: 'an-older-build' } }));
    c = check(runChecks({ ...opts, repo, truncation: stale, level: 'p4' }), 'truncation');
    assert.equal(c.digest_match, false);
    assert.equal(c.error, undefined);
    assert.equal(c.ok, true);
    res = runChecks({ ...opts, repo, truncation: stale, level: 'p7' });
    c = check(res, 'truncation');
    assert.equal(c.digest_match, false);
    assert.match(c.error, /stale build/);
    assert.ok(res.failed.includes('truncation'));
  } finally { rmSync(fx.dir, { recursive: true, force: true }); rmSync(repo, { recursive: true, force: true }); }
});

test('CLI: --report writes the JSON report; exit 1 on a hard failure, 0 otherwise, 2 on bad arguments', () => {
  const out = mkdtempSync(join(tmpdir(), 'tesseract-i18n-report-'));
  try {
    for (const [name, code] of [['base', 0], ['fail_icu', 1]]) {
      const fx = materialize(name);
      try {
        const report = join(out, `${name}.json`);
        const o = fx.opts;
        const status = main(['--quiet', '--root', o.root, '--backend-keys', o.backendKeys, '--py-catalog', o.pyCatalog, '--native-text', o.nativeText, '--truncation', o.truncation, '--source-digest', o.sourceDigest, '--report', report]);
        assert.equal(status, code, name);
        const doc = JSON.parse(readFileSync(report, 'utf8'));
        assert.equal(doc.level, 'p4');
        assert.equal(doc.failed.length > 0, code === 1);
      } finally { rmSync(fx.dir, { recursive: true, force: true }); }
    }
    assert.equal(main(['--quiet', '--level', 'p9']), 2);
    assert.equal(main(['--quiet', '--bogus']), 2);
  } finally { rmSync(out, { recursive: true, force: true }); }
});

test('cjk scan: CSS content: is exempt only with a first-line /* @i18n-pending */', () => {
  const r = runRule('cjk', { root: join(HERE, 'scan_css') });
  assert.deepEqual(r.findings.map(f => f.file).sort(), ['css/late_marker.css', 'css/plain.css']);
  assert.deepEqual(r.pending.map(f => f.file), ['css/pending.css']);
});

test('cjk rule is enforced and the repository passes it (only @i18n-pending files have hits)', () => {
  assert.equal(JSON.parse(readFileSync(join(UI, 'tools', 'scan_gates.json'), 'utf8')).cjk, 'enforce');
  const r = runRule('cjk', { root: UI });
  assert.deepEqual(r.findings, []);
  const pendingFiles = [...new Set(r.pending.map(f => f.file))].sort();
  // the pending set only shrinks as the 04 tickets land (T-P4-04a: a hard-coded list broke on
  // every extraction): no file outside the original nine may carry @i18n-pending hits
  const ORIGINAL = ['css/pages/logs.css', 'src/core/app.js', 'src/core/core.js', 'src/data/static.js', 'src/index.html', 'src/pages/logs.js', 'src/pages/overview.js', 'src/pages/settings.js', 'src/pages/tiers.js'];
  assert.deepEqual(pendingFiles.filter(f => !ORIGINAL.includes(f)), [], 'a new @i18n-pending file');
});

test('id format: dotted lower-case segments; catalog choice ids keep the backend value verbatim', () => {
  for (const id of ['common.action.save', 'errors.start.listener_conflict', 'catalog.ss.method.choice.2022-blake3-aes-128-gcm', 'catalog.alerts.desktop.alert_types.choice.KILL_SWITCH_ON']) assert.ok(ID_RE.test(id), id);
  for (const id of ['common', 'Common.save', 'common.Save', 'common..x', 'catalog.x.choice.a.b', 'catalog.x.choice.', 'common.x y']) assert.ok(!ID_RE.test(id), id);
});

test('srcHash and the Rust catalog parser', () => {
  assert.equal(srcHash('保存'), srcHash('保存'));
  assert.notEqual(srcHash('保存'), srcHash('保存 '));
  assert.match(srcHash('x'), /^[0-9a-f]{16}$/);
  const rs = parseNativeText('pub const LOCALES:[&str;9]=["zh_CN","en_US","de_DE","es_ES","fr_FR","ja_JP","ko_KR","pt_BR","ru_RU"];\n' +
    'Message{id:"a",text:["一\\n{x}","b\\u{a0}{x}","c \\"q\\" {x}","d","e","f","g","h","i"]},');
  assert.deepEqual(rs.locales.length, 9);
  assert.deepEqual(rs.messages, [{ id: 'a', texts: ['一\n{x}', 'b {x}', 'c "q" {x}', 'd', 'e', 'f', 'g', 'h', 'i'] }]);
});

test('backend_keys.py on the repository: required keys, f-string expansion, per-domain counts, nothing unresolved', () => {
  const r = spawnSync(PY, ['-B', join(UI, 'tools', 'backend_keys.py'), '--strict'], { cwd: REPO, encoding: 'utf8', maxBuffer: 64 << 20, env: { ...process.env, PYTHONIOENCODING: 'utf-8' } });
  assert.equal(r.status, 0, r.stderr);
  const d = JSON.parse(r.stdout);
  for (const k of ['errors.runtime_gone', 'errors.settings.conflict', 'errors.exit.unavailable', 'errors.unknown', 'errors.start.listener_conflict', 'errors.host.not_allowed', 'errors.bridge.not_cancellable']) assert.ok(d.errors[k], k);
  const expanded = Object.entries(d.errors).filter(([, src]) => src.some(s => s.startsWith('expanded:')));
  assert.ok(expanded.some(([k]) => k === 'errors.mode.restart_failed'), 'f-string errors.{domain}.{reason} expanded per call site');
  assert.ok(expanded.some(([k]) => k === 'errors.exit.native_fallback_verified'), 'loop over FALLBACK_FLAGS expanded');
  assert.ok(!('errors.mode.io' in d.errors), 'reason given at every call site: the code is not used as a key');
  // parser.NodeValidationError(code, …) → ContentIssue(code=exc.code) → bridge_import._issue_error (fix round 1, MUST-2)
  for (const k of ['errors.import.missing_password', 'errors.import.missing_uuid', 'errors.import.missing_cipher_or_password', 'errors.import.missing_reality_keys',
    'errors.import.invalid_vmess_alter_id', 'errors.import.missing_tuic_credentials', 'errors.import.incomplete_socks5_credentials',
    'errors.import.missing_wireguard_keys', 'errors.import.missing_wireguard_address']) assert.ok(d.errors[k], k);
  assert.deepEqual(d.unresolved, []);
  assert.equal(d.qt_free, true);
  for (const dom of ['start', 'bridge', 'host', 'settings', 'geo', 'exit', 'update', 'killswitch', 'import', 'autostart', 'alerts', 'diagnose', 'mode', 'nodes', 'subscriptions', 'gateway', 'config', '(code)']) assert.ok(d.errors_by_domain[dom] > 0, dom);
  assert.ok(d.errors['errors.config.local_read'].some(s => s.startsWith('literal:desktop_runtime.py:')), 'keys built outside bridge_*.py are found');
  assert.ok(d.errors['errors.settings.apply_superseded'].some(s => s.startsWith('rule:runtime_settings_apply.py:')), 'apply_error message values from every caller');
  assert.ok(d.errors['errors.bridge.job_capacity'].some(s => s.startsWith('shell:transport.rs:')), 'shell literal keys');
  assert.equal(Object.values(d.errors_by_domain).reduce((a, b) => a + b, 0), Object.keys(d.errors).length);
  assert.ok(d.reasons['common.skipped'].includes('not_cancellable'));
  assert.ok(d.catalog.label.includes('catalog.ss') || d.catalog.label.some(l => l.startsWith('catalog.')));
  assert.ok(d.catalog.choice.every(c => /\.choice\./.test(c)) && d.catalog.notice.every(c => c.startsWith('catalog.notice.')));
});

test('backend_keys.py evaluator: per-call-site expansion, and an unresolvable site is reported', () => {
  const dir = mkdtempSync(join(tmpdir(), 'tesseract-bk-'));
  try {
    writeFileSync(join(dir, 'bridge_demo.py'), [
      'def _err(code, domain, reason=None):',
      '    return {"message_key": f"errors.{domain}.{reason or code}"}',
      'def a(x):',
      '    _err("io", "demo", "read_failed")',
      '    _err("conflict", "demo")',
      '    r = "late" if x else "early"',
      '    _err("io", "demo", r)',
      'def b(obj):',
      '    return {"message_key": "errors.other." + obj.reason}',
      '',
    ].join('\n'));
    const code = [
      'import sys, json; sys.path.insert(0, sys.argv[1]); import backend_keys as b',
      'from pathlib import Path',
      'mod = b.Module(Path(sys.argv[2]) / "bridge_demo.py")',
      'out = []',
      'for s in b.dynamic_sites(mod):',
      '    try: out.append(sorted(v for v in b.site_values(s, mod) if isinstance(v, str)))',
      '    except b.Unresolved as e: out.append("unresolved")',
      'print(json.dumps(out))',
    ].join('\n');
    const r = spawnSync(PY, ['-B', '-c', code, join(UI, 'tools'), dir], { encoding: 'utf8' });
    assert.equal(r.status, 0, r.stderr);
    assert.deepEqual(JSON.parse(r.stdout), [['errors.demo.conflict', 'errors.demo.early', 'errors.demo.late', 'errors.demo.read_failed'], 'unresolved']);
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

test('backend_keys.py reason sources: a non-constant reason argument without a declared rule is unresolved, not a note', () => {
  const dir = mkdtempSync(join(tmpdir(), 'tesseract-bk-'));
  try {
    writeFileSync(join(dir, 'parser.py'), [
      'class NodeValidationError(ValueError):',
      '    def __init__(self, code, message): self.code = code',
      'def _validate_node_for_runtime(issue):',
      '    raise NodeValidationError(issue.code, "declared")',
      'def check(x):',
      '    raise NodeValidationError("missing_uuid", "constant")',
      'def brand_new_family(obj):',
      '    raise NodeValidationError(obj.code, "undeclared")',
      '',
    ].join('\n'));
    const code = [
      'import sys, json; sys.path.insert(0, sys.argv[1]); import backend_keys as b',
      'from pathlib import Path',
      'mod = b.Module(Path(sys.argv[2]) / "parser.py")',
      'unresolved = []',
      'found = b.reason_constants(mod, "NodeValidationError", index=0, unresolved=unresolved)',
      'print(json.dumps([sorted(found), [u["site"] for u in unresolved]]))',
    ].join('\n');
    const r = spawnSync(PY, ['-B', '-c', code, join(UI, 'tools'), dir], { encoding: 'utf8' });
    assert.equal(r.status, 0, r.stderr);
    assert.deepEqual(JSON.parse(r.stdout), [['missing_uuid'], ['parser.py:8 brand_new_family() -> NodeValidationError()']]);
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

test('the repository passes the p4 gate; errors.json holds backend keys only; three catalogs are reported', () => {
  const res = runChecks({ level: 'p4' });
  assert.deepEqual(res.failed, [], JSON.stringify(res.checks.filter(c => c.level === 'hard' && !c.ok)));
  const bk = check(res, 'backend_keys');
  assert.deepEqual(bk.extra_in_errors_json, [], 'errors.json is frozen to backend message keys (critique C6)');
  assert.equal(check(res, 'id_format').count, 0);
  const tri = check(res, 'three_catalogs');
  assert.equal(tri.python.qt_free, true);
  assert.equal(tri.native.messages > 30, true);
  assert.deepEqual(tri.items.filter(i => i.catalog === 'native'), [], 'native_text.rs: 9 non-empty texts with equal placeholders');
  assert.equal(res.counts.zh_CN.errors, bk.keys);
});
