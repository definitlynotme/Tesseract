// T-P4-05e: generates ui/test/fixtures/scenarios/alerts_*.json from settings_page_running.json and
// the design target's catalog (ui/src/design/catalog.json), and gives the 04e i18n scenarios the
// real catalog and alert data (out of scope, reported). Run: node ui/tools/fixture_gen/alerts.mjs --force [<ui dir>]
// (OUTDATED: see below)
// Collected into ui/tools/fixture_gen/ by T-P4-12 (第八波裁定 05a-§11.5); usage and state: ui/README.md.
import { readFileSync, writeFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

// OUTDATED (T-P4-12 fix 1): the catalog now carries the SMTP presets (T-P4-H3), so a run adds
// presets / preset_aliases to 21 scenarios; it is not idempotent for settings_i18n_* (appends steps
// again) and drops the later hand edit of alerts_a02_pair_failed. Running it overwrites
// the current fixtures, so it refuses to write without --force (ui/README.md §6). To compare, run it
// with --force on a COPY of the ui directory: node ui/tools/fixture_gen/alerts.mjs --force <copy of ui>
const ARGS = process.argv.slice(2);
if (!ARGS.includes('--force')) {
  console.error('alerts.mjs is outdated and would overwrite test/fixtures/scenarios (SMTP presets, settings_i18n_* not idempotent); see ui/README.md §6. ' +
    'Refusing to write. Re-run with --force (preferably on a copy of the ui directory) if you really mean it.');
  process.exit(2);
}
const POS = ARGS.filter(a => a !== '--force');
const UI = POS[0] || resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const SC = join(UI, 'test', 'fixtures', 'scenarios');
const read = n => JSON.parse(readFileSync(join(SC, `${n}.json`), 'utf8'));
const write = (n, o) => writeFileSync(join(SC, `${n}.json`), JSON.stringify(o, null, 1) + '\n');
const clone = v => JSON.parse(JSON.stringify(v));
const CAT = JSON.parse(readFileSync(join(UI, 'src', 'design', 'catalog.json'), 'utf8')).alerts;
const ENV = { epoch: 'rt-fixture-e1', produced_at_ms: 1790000000000, sent_at_ms: 1790000000001 };
const T0 = 1790000000000;

const URGENT = ['CRITICAL', 'EMERGENCY', 'KILL_SWITCH_ON'];
const ABNORMAL = ['WARNING', 'CRITICAL', 'EMERGENCY', 'RECOVERY', 'KILL_SWITCH_ON', 'KILL_SWITCH_OFF', 'KILL_SWITCH_DISABLED', 'KILL_SWITCH_OVERRIDE'];
export const CHANNELS = [
  { id: 'telegram-main', type: 'telegram', label: 'Phone', enabled: true, alert_types: ABNORMAL, config: { enabled: true, timeout: 10, parse_mode: 'HTML', chat_id: '-1001234567890' } },
  { id: 'telegram-ops', type: 'telegram', label: 'Ops', enabled: true, alert_types: ['CRITICAL', 'BACKUP_REMINDER'], config: { chat_id: '-1009876543210' } },
  { id: 'ntfy-1', type: 'ntfy', label: '', enabled: true, alert_types: [], config: { priority: 4, tags: ['warning'] } },
  { id: 'mail-1', type: 'email_smtp', label: 'Mail', enabled: false, alert_types: [], config: { host: 'smtp.example.com', port: 587, from_addr: 'alerts@example.com', starttls: true } },
  { id: 'desktop', type: 'desktop', label: '', enabled: true, alert_types: [], config: { enabled: true, timeout: 5 } },
];
export const SECRET_SET = ['alerts.telegram-main.bot_token', 'alerts.telegram-ops.bot_token', 'alerts.ntfy-1.server', 'alerts.ntfy-1.topic',
  'alerts.mail-1.username', 'alerts.mail-1.password', 'alerts.mail-1.to_addrs'];
const HISTORY = [
  { id: 'alert-1', seq: 1, severity: 'warn', event_id: 'mode.tier2_active', title: 'Fallback to Sentinel', detail: 'Vanguard is unavailable; traffic moved to Sentinel.', ts_ms: T0 - 3600000 },
  { id: 'alert-2', seq: 2, severity: 'critical', event_id: 'kill_switch.activated', title: 'Kill switch triggered', detail: 'Every tier is unavailable; outbound traffic is blocked.', ts_ms: T0 - 1800000 },
  { id: 'alert-3', seq: 3, severity: 'info', event_id: 'mode.recovery.vanguard', title: 'Vanguard recovered', detail: 'Traffic moved back to Vanguard.', ts_ms: T0 - 600000 },
];
const DELIVERY = [
  { id: 'delivery-telegram-main', channel_id: 'telegram-main', state: 'healthy', last_attempt_ms: T0 - 590000, last_success_ms: T0 - 590000, error: null },
  { id: 'delivery-desktop', channel_id: 'desktop', state: 'healthy', last_attempt_ms: T0 - 600000, last_success_ms: T0 - 600000, error: null },
  { id: 'delivery-ntfy-1', channel_id: 'ntfy-1', state: 'failed', last_attempt_ms: T0 - 1700000, last_success_ms: null, error: { code: 'io', message_key: 'errors.alerts.delivery_failed', params: {} } },
];
const RECORDS = [
  { id: 'delivery-r1', channel_id: 'telegram-main', state: 'healthy', last_attempt_ms: T0 - 590000, last_success_ms: T0 - 590000, error: null },
  { id: 'delivery-r2', channel_id: 'telegram-main', state: 'failed', last_attempt_ms: T0 - 1790000, last_success_ms: null, error: { code: 'io', message_key: 'errors.alerts.delivery_failed', params: {} } },
];
export const QR_SVG = '<svg xmlns="http://www.w3.org/2000/svg" width="29" height="29" class="segno"><path transform="scale(1)" class="qrline" stroke="#000" d="M4 4.5h7m1 0h1m2 0h2m1 0h7M4 5.5h1m5 0h1m2 0h2m3 0h1m5 0h1M4 6.5h1m1 0h3m1 0h1m1 0h1m1 0h1m2 0h1m1 0h3m1 0h1"/></svg>\n';
const OPEN_URL = 'https://t.me/example_tesseract_bot?start=fixture-start-value';

const evt = (topic, rev, data, kind = 'snapshot', seq) => Object.assign({ ...ENV, t: 'evt', topic, rev, kind, data }, seq != null ? { seq } : {});
const res = (cmd, data) => ({ ...ENV, t: 'res', id: 1, req_id: '$req_id', owner: '$owner', cmd, ok: true, data });
const resErr = (cmd, error) => ({ ...ENV, t: 'res', id: 1, req_id: '$req_id', owner: '$owner', cmd, ok: false, error });
let jobSeq = 0;
function job(name, op, end) {
  // ack + queued + running + (done | failed | nothing): the jobs topic's seq / rev run on per scenario
  const out = [{ ...ENV, t: 'ack', id: 2, req_id: '$req_id', owner: '$owner', accepted: true, op }];
  const edge = (state, extra) => { jobSeq++; return { ...ENV, t: 'evt', topic: 'jobs', rev: jobSeq, kind: 'edge', seq: jobSeq,
    data: Object.assign({ op, req_id: '$req_id', owner: '$owner', name, state, started_at_ms: state === 'queued' ? null : T0, progress: null, result: null, error: null }, extra || {}) }; };
  out.push(edge('queued'), edge('running'));
  if (end && end.done) out.push(edge('done', { result: end.done }));
  if (end && end.failed) out.push(edge('failed', { error: end.failed, result: end.result || null }));
  return out;
}
const onboard = (sid, state, o = {}) => ({ session_id: sid, owner: '$owner', state, allowed_actions: o.actions || (state === 'pending' ? ['poll', 'cancel', 'open_link', 'show_qr'] : state === 'ready' ? ['complete', 'cancel'] : []), expires_at_ms: o.expires || T0 + 600000, reason_code: o.reason || null });
const pairing = (o = {}) => ({ channel_type: 'telegram', mode: 'bot_start_qr', result_kind: 'channel', auxiliary: false, can_configure: true, open_url: o.url === undefined ? OPEN_URL : o.url,
  qr_payload: o.url === undefined ? OPEN_URL : '', qr_svg: o.svg === undefined ? QR_SVG : o.svg, qr_error: o.svg === null ? 'qr_unavailable' : '', required_inputs: [], prerequisite_specs: [], summary: {}, error: '' });

/* ---- the base: settings_page_running with the alert data ---- */
function withAlerts(sc, o = {}) {
  const out = clone(sc);
  const hello = out.steps.find(s => s.deliver && s.deliver.t === 'hello').deliver;
  for (const c of ['alerts.test', 'alerts.deliveries', 'alerts.onboarding.start', 'alerts.onboarding.poll', 'alerts.onboarding.complete', 'alerts.onboarding.cancel']) if (!hello.caps.commands.includes(c)) hello.caps.commands.push(c);
  for (const tp of ['alerts.history', 'alerts.delivery', 'alerts.onboarding']) if (!hello.caps.topics.includes(tp)) hello.caps.topics.push(tp);
  hello.caps.commands.sort(); hello.caps.topics.sort();
  for (const st of out.steps) {
    if (!st.on_invoke) continue;
    if (st.on_invoke.cmd === 'config.snapshot') {
      const d = st.reply[0].data;
      d.values.alerts = { channels: clone(o.channels || CHANNELS) };
      d.secret_fields_set = [...new Set([...(d.secret_fields_set || []), ...(o.secretSet || SECRET_SET)])].sort();
    }
    if (st.on_invoke.cmd === 'alerts.catalog') {
      if (o.catalogError) { st.reply = [resErr('alerts.catalog', o.catalogError)]; continue; }
      st.reply[0].data = Object.assign(clone(CAT), { acl: o.acl || { plaintext_storage: true, access_state: 'no_known_failures', unconfirmed_count: 0 } });
    }
  }
  // the topics after the first frames (before host:language)
  const at = out.steps.findIndex(s => s.deliver && s.deliver.t === 'host:language');
  const frames = [evt('alerts.history', 1, { items: clone(o.history || HISTORY), last_seq: (o.history || HISTORY).reduce((m, a) => Math.max(m, a.seq), 0) }),
    evt('alerts.delivery', 1, { items: clone(o.delivery || DELIVERY) }), evt('alerts.onboarding', 1, { sessions: [] })];
  out.steps.splice(at < 0 ? out.steps.length : at, 0, ...frames.map(f => ({ deliver: f })));
  for (let i = 0; i < (o.deliveries == null ? 6 : o.deliveries); i++) out.steps.push({ on_invoke: { cmd: 'alerts.deliveries' }, reply: [res('alerts.deliveries', { items: clone(RECORDS), next_cursor: null })] });
  return out;
}
const BASE = read('settings_page_running');
function scenario(name, desc, o, extra) {
  jobSeq = o && o.jobSeqFrom ? o.jobSeqFrom : 0;
  const sc = withAlerts(BASE, o || {});
  sc.name = name; sc.desc = desc;
  sc.steps.push(...(extra ? extra() : []));
  write(name, sc);
}
const SAVE_ALERTS = (rev, section) => ({ on_invoke: { cmd: 'settings.apply' }, reply: (() => {
  // a section saved but not applied ends the job failed, with the partial result (PROTOCOL §6, §15)
  const result = { rev, sections: [section], pending: null };
  const f = job('settings.apply', 'op-save-1', section.applied ? { done: result } : { failed: section.error, result });
  f.push({ ...ENV, t: 'evt', topic: 'config.changed', rev: 1, kind: 'edge', seq: 1, data: { rev, files: ['config'], sections: [section], pending: null, external_detection: 'bridge_writes_only' } });
  return f;
})() });
const ALERTS_OK = { section: 'alerts', saved: true, applied: true, effect: 'none', error: null };
const snapshotAgain = () => { const s = clone(withAlerts(BASE).steps.find(x => x.on_invoke && x.on_invoke.cmd === 'config.snapshot')); s.reply[0].data.rev = 8; return s; };

/* ---- the page and the save model ---- */
scenario('alerts_page', '提醒区：五个渠道（含同类型两个实例、自定义级别、整字段扣下的收件人）、最近提醒、送达状态、目录与明文说明', {});
scenario('alerts_empty', '提醒区：没有任何渠道、没有提醒记录（空状态）', { channels: [], secretSet: [], history: [], delivery: [] });
scenario('alerts_a01_save_checks', 'A-01 保存校验：http:// 只警告；启用但不完整、空范围阻止保存', {}, () => [SAVE_ALERTS(8, ALERTS_OK), snapshotAgain()]);
scenario('alerts_multi_instance', '同一类型两个实例分别编辑；保存只发改动的字段，其余行原样（{id,type}）', {}, () => [SAVE_ALERTS(8, ALERTS_OK), snapshotAgain()]);
scenario('alerts_a05_acl', 'A-05 明文存储说明 + ACL 未确认警告', { acl: { plaintext_storage: true, access_state: 'unconfirmed', unconfirmed_count: 2 } });
scenario('alerts_a06_cost', 'A-06 付费渠道（Pushover）新建默认只发紧急并提示费用；保存载荷', {}, () => [SAVE_ALERTS(8, ALERTS_OK), snapshotAgain()]);
scenario('alerts_reentry', '31b-10 改了端点（SMTP host）就要求重填秘密：测试不可用、保存被拦', {});
scenario('alerts_catalog_missing', '读不到 alerts.catalog：提醒区提示 + 重试', { catalogError: { code: 'runtime_unavailable', message_key: 'errors.runtime_unavailable', params: {} } });
scenario('alerts_delete_records', '删除渠道：保存成功但送达记录未能清理（delivery_records_delete_failed）单独提示', {}, () => [SAVE_ALERTS(8, { section: 'alerts', saved: true, applied: false, effect: 'none',
  error: { code: 'io', message_key: 'errors.settings.apply_failed', params: { section: 'alerts' }, reason_code: 'delivery_records_delete_failed' } }), snapshotAgain()]);

/* ---- tests ---- */
scenario('alerts_test_saved', 'A-07 用已保存配置测试（target_id + SecretEdit keep）：成功', {}, () => [{ on_invoke: { cmd: 'alerts.test' }, reply: job('alerts.test', 'op-test-1', { done: { ok: true, error: null, delivery_id: null } }) }]);
scenario('alerts_a07_test_reasons', 'A-07 测试失败：只有 reason_code（alerts_test_delivery_failed / alerts_test_timeout）', {}, () => [
  { on_invoke: { cmd: 'alerts.test' }, reply: job('alerts.test', 'op-test-1', { failed: { code: 'io', message_key: 'errors.io', params: {}, reason_code: 'alerts_test_delivery_failed' } }) },
  { on_invoke: { cmd: 'alerts.test' }, reply: job('alerts.test', 'op-test-2', { failed: { code: 'timeout', message_key: 'errors.timeout', params: {}, reason_code: 'alerts_test_timeout' } }) }]);
scenario('alerts_a08_desktop_dropped', 'A-08 桌面测试等外壳回执：dropped 如实显示「没有显示」', {}, () => [
  { on_invoke: { cmd: 'alerts.test' }, reply: job('alerts.test', 'op-test-1', { failed: { code: 'io', message_key: 'errors.io', params: {}, reason_code: 'alerts_desktop_dropped' } }) }]);
scenario('alerts_a04_lock', 'A-04 测试进行中锁定表单（作业不结束）', {}, () => [{ on_invoke: { cmd: 'alerts.test' }, reply: job('alerts.test', 'op-test-1', null) }]);

/* ---- pairing ---- */
const START = (o = {}) => ({ on_invoke: { cmd: 'alerts.onboarding.start' }, reply: [res('alerts.onboarding.start', { state: onboard('pair-1', 'pending'), pairing: pairing(o) })] });
const POLL = (state, o = {}) => ({ on_invoke: { cmd: 'alerts.onboarding.poll' }, reply: [res('alerts.onboarding.poll', { state: onboard('pair-1', state, o), pairing: {} })] });
const CANCEL = () => ({ on_invoke: { cmd: 'alerts.onboarding.cancel' }, reply: [res('alerts.onboarding.cancel', { state: onboard('pair-1', 'cancelled') })] });
const COMPLETE = () => ({ on_invoke: { cmd: 'alerts.onboarding.complete' }, reply: [res('alerts.onboarding.complete', { state: onboard('pair-1', 'configured'),
  draft: { id: 'telegram', type: 'telegram', label: 'Telegram', enabled: false, config: { enabled: false, bot_token: '7071829:FIXTURE-pair-token', chat_id: '-1002233445566', parse_mode: 'HTML', message_thread_id: '', silent: true, protect_content: false, disable_link_preview: true } }, saved: false })] });
scenario('alerts_a09_pair_complete', 'A-09 配对成功：start → poll ready → complete 草稿（saved:false）拆分进表单，保存才生效', {}, () => [START(), POLL('ready'), COMPLETE(), SAVE_ALERTS(8, ALERTS_OK), snapshotAgain()]);
scenario('alerts_a02_pair_expired', 'A-02 配对过期（10 分钟，onboarding_expired）', {}, () => [START(), POLL('expired', { reason: 'onboarding_expired' })]);
scenario('alerts_a02_pair_failed', 'A-02 配对失败（onboarding_remote_rejected，可再刷新）', {}, () => [START(), POLL('failed', { reason: 'onboarding_remote_rejected', actions: ['poll', 'cancel', 'open_link', 'show_qr'] }), POLL('failed', { reason: 'onboarding_remote_rejected', actions: ['cancel'] })]);
scenario('alerts_a02_pair_cancel', 'A-02 手动取消配对：发出一次 alerts.onboarding.cancel', {}, () => [START(), POLL('pending'), POLL('pending'), CANCEL(), CANCEL()]);
scenario('alerts_a02_qr_missing', 'G54 二维码无法显示（qr_svg 为空）：给链接与代码作替代', {}, () => [START({ svg: null }), POLL('pending'), POLL('pending'), CANCEL()]);
scenario('alerts_a10_pair_close', 'A-10 抽屉关闭：撤销 owner，只发一次 cancel，迟到的 poll 结果被丢弃', {}, () => [START(), { on_invoke: { cmd: 'alerts.onboarding.poll' } }, CANCEL(), CANCEL()]);
scenario('alerts_dialog_open', 'B8 打开配对链接时另一个原生对话框还开着（conflict / dialog_open）', {}, () => [START(), POLL('pending'), POLL('pending'), CANCEL(),
  { on_invoke: { cmd: 'host.openUrl' }, reply: [resErr('host.openUrl', { code: 'conflict', message_key: 'errors.host.conflict', params: {}, reason_code: 'dialog_open' })] }]);

/* ---- history (RT-6) and hostile text ---- */
scenario('alerts_history_stream', 'RT-6 最近提醒：edge 按序号追加；gap 后由新快照重种', {});
scenario('alerts_malicious_text', '不可信的提醒标题与正文（标签、脚本、事件属性）只当文本', { history: [
  { id: 'alert-9', seq: 9, severity: 'error', event_id: 'alerts.unknown', title: '<img src=x onerror="window.__pwned=1">Title', detail: '<script>window.__pwned=1</script><b onload="window.__pwned=1">detail</b>', ts_ms: T0 - 60000 }] });

/* ---- the 04e i18n scenarios: the real catalog and alert data (out of scope; T-P4-05e report) ---- */
for (const n of ['settings_i18n_running', 'settings_i18n_stopped']) {
  const sc = read(n);
  const out = withAlerts(sc, { deliveries: 0, channels: [CHANNELS[0]], secretSet: ['alerts.telegram-main.bot_token'] });
  // the deliveries query stays out of these (their hello does not list it)
  const hello = out.steps.find(s => s.deliver && s.deliver.t === 'hello').deliver;
  hello.caps.commands = hello.caps.commands.filter(c => c !== 'alerts.deliveries');
  // the drawer's test (settings_i18n_test_fail): the saved Telegram channel's test fails with a reason_code
  if (n === 'settings_i18n_running') {
    jobSeq = 0;
    for (let i = 0; i < 2; i++) out.steps.push({ on_invoke: { cmd: 'alerts.test' }, reply: job('alerts.test', `op-test-${i + 1}`, { failed: { code: 'io', message_key: 'errors.io', params: {}, reason_code: 'alerts_test_delivery_failed' } }) });
  }
  write(n, out);
}
console.log('ok');
