/* ==========================================================================
   design/settings_demo.js — the 配置 page's demo data (T-P4-04e)
   --------------------------------------------------------------------------
   Moved out of pages/settings.js unchanged (映射_settings §6): the example alert history and
   delivery results, the release notes, the data directories, the update and rule-data demo
   values, the legacy alert configuration's name, the quick-setup demo link and tokens, the
   prototype's notes, and the run-log lines the page writes into the simulator (titles in the
   simulator's own words: design/logs_demo.js maps them by title). This is demo data (P4
   architecture §6): it renders under translate="no", is used only inside if (__DEMO__) (the app
   target's bundle drops it) and is deleted with the page's demo code (T-P4-05d / 05e / 07).
   ========================================================================== */

export const MIN = 60000, HOUR = 3600000, DAY = 86400000;

/* ---- alerts ---- */
/** the example delivery results (each speaks for the values it was sent with) */
export const TOKEN_NOTE = '机器人 Token 无效（HTTP 401）';
export const TOKEN_FAIL = '发送失败 · Token 无效';
export const DELIVERY = NOW => [
  ['desktop', { st: 'ok', at: NOW - 12 * MIN }],
  ['telegram', { st: 'err', at: NOW - 15 * MIN, note: TOKEN_NOTE }],
];
/** the example alert history (titles and details are the runtime's text in the real app) */
export const recent = NOW => [
  { id: 'a1', ts: NOW - 12 * MIN, lv: 'ok', title: 'Vanguard 自建 已恢复', detail: '香港 IEPL 03 已恢复，42 ms', dl: [['desktop', 'ok']] },
  { id: 'a2', ts: NOW - 15 * MIN, lv: 'warn', title: 'Vanguard 自建 降级', detail: '香港 IEPL 03 延迟 1240 ms，已回退到 Sentinel 订阅', dl: [['desktop', 'ok'], ['telegram', 'err', 'HTTP 401']] },
  { id: 'a3', ts: NOW - 134 * MIN, lv: 'info', title: '服务已启动', detail: '系统代理 · 已接管', dl: [['desktop', 'ok']] },
];
/** the entry a test adds to the history */
export const testRecord = (id, ts, ch, chName, ok, note) => ({ id, ts, lv: 'info', title: '测试消息', detail: `来自配置页 · ${chName}`, dl: [[ch, ok ? 'ok' : 'err', note]] });
/** the legacy configuration found at start (retired channel type) */
export const RETIRED_NAME = '短信备用通道';
/** quick setup (Telegram): the demo link and the tokens the demo chat «answers» with */
export const QUICK = { link: 'https://link.example.org/tg/tesseract_bot?start=4f2a9c', token: '7071829:EXAMPLE-new-token', chat: '-1002233445566' };

/* ---- 应用 ---- */
export const CORE_VERSION = 'v1.19.2';
export const UPDATE = { version: '0.2.0', sizeMb: 38, date: '2026-09-24', failReason: '连接超时' };
export const RULE_DATA_FAIL = '连接超时';
/** rule data: when each set was last updated */
export const GEO_AT = NOW => ({ GeoSite: NOW - 9 * HOUR, GeoIP: NOW - 9 * HOUR, MMDB: NOW - 9 * HOUR, ASN: NOW - 3 * DAY });
export const DIRS = { config: 'C:\\Users\\user\\AppData\\Roaming\\Tesseract', data: 'C:\\Users\\user\\AppData\\Local\\Tesseract', core: 'C:\\Program Files\\Tesseract\\bin\\mihomo.exe' };
/** the release notes (GitHub's text in the real app, shown as plain text there) */
export const NOTES = `<div class="set-notes" translate="no"><div class="set-notes-h">新功能</div><ul><li>提醒与通知支持快速配置：扫码即可连接 Telegram。</li><li>分流规则可以拖动调整顺序，并显示匹配顺序。</li></ul>
        <div class="set-notes-h">改进</div><ul><li>冷启动时更快找到可用的 Bastion 应急 节点。</li><li>配置页改动先成为草稿，保存前说明影响。</li></ul>
        <div class="set-notes-h">修复</div><ul><li>切换网络模式后系统代理偶尔没有恢复的问题。</li></ul></div>`;
/** the one-time notes after the simulated install: the prototype says what the real app does */
export const UPDATED_NOTE = '<p class="set-notes-demo" translate="no"><span class="set-demo-tag">演示</span>真实应用会先关闭、安装，重新打开后显示这份说明。</p>';
/** appended to a save result that changed the language */
export const LANG_NOTE = ' · 原型只有简体中文界面';

/* ---- the run log the page writes into the simulator (B-19: the runtime's events in the real app) ---- */
const CH_NAME = { desktop: '桌面通知', sound: '声音提示', telegram: 'Telegram', whatsapp: 'WhatsApp', slack: 'Slack', discord: 'Discord', teams: 'Microsoft Teams', gchat: 'Google Chat', ntfy: 'ntfy', pushover: 'Pushover', gotify: 'Gotify', smtp: 'SMTP 邮件', sendgrid: 'SendGrid 邮件', twilio: 'Twilio 短信', webhook: '通用 Webhook' };
const KEY_LABEL = {
  ks: '熔断保护', ksLan: '熔断放行局域网', ksDns: '熔断放行 DNS', autoExit: '自动查询出口 IP', ksDnsList: '熔断放行的 DNS 服务器', ksExtra: '熔断放行的其他地址',
  'pend:ksDns': '待添加的放行 DNS', 'pend:ksExtra': '待添加的放行地址',
  rules: '自定义路由规则', order: '规则组的匹配顺序', 'pend:rule': '待添加的规则', bypass: '系统代理绕过', 'pend:bypass': '待添加的绕过地址',
  lan: '允许局域网连接', ipv6: 'IPv6', dnsRules: 'DNS 遵循路由规则', egress: '出口自动恢复', egressRetry: '恢复后重新获取应急来源',
  portMixed: '代理端口', portSub: '订阅服务端口', dnsMode: 'DNS 解析方式', dnsList: '自定义 DNS 解析器', 'pend:res': '待添加的 DNS 解析器',
  latency: '延迟上限', bastionOn: 'Bastion 应急池', bastionMin: 'Bastion 应急池最少可用节点', sources: 'Bastion 应急池公共来源',
  'pend:src': '待添加的公共来源', 'pend:srcEdit': '公共来源的修改',
  alertsPaused: '暂停全部提醒', retired: '清理旧版提醒配置', lang: '界面语言', autostart: '开机自启',
};
const keyLabel = k => KEY_LABEL[k] || (k.startsWith('ch:') ? `${CH_NAME[k.slice(3)] || k.slice(3)}提醒` : k);
const labels = keys => keys.map(keyLabel).filter((x, i, a) => a.indexOf(x) === i).join('、');

let logN = 0;
/** the sim log is the prototype's one record of what happened */
export function log(T, level, category, title, detail, source) {
  const F = T.fmt, ts = Date.now();
  const rec = { id: `LS${++logN}`, ts, level, source: source || 'Tesseract', category, title, detail: detail || '',
    raw: `${F.time(ts)} ${level.toUpperCase().padEnd(5)} [${category}] ${title}${detail ? ' · ' + detail : ''}` };
  const L = T.sim.logs; L.push(rec); if (L.length > 240) L.shift();
  T.emit('sim:log', rec);
}
export const LOG = {
  reloaded: (T, keys) => log(T, 'info', 'runtime', '代理核心已重新加载配置', labels(keys), 'Mihomo'),
  saved: (T, keys) => log(T, 'info', 'runtime', '配置已保存', labels(keys)),
  killswitch: (T, on, admin, live) => {
    if (on && !admin) log(T, 'warn', 'killswitch', '熔断保护已开启，但没有生效', '需要管理员权限才能写入 Windows 防火墙规则');
    else log(T, on ? 'info' : 'warn', 'killswitch', on ? (live ? '熔断保护已布防' : '熔断保护已开启') : '熔断保护已关闭', on ? '所有层级都不可用时阻断出站' : '所有层级都不可用时不再阻断出站');
  },
  latency: (T, v) => log(T, 'info', 'health', '延迟上限已更新', `${v} ms · 正常 ≤ ${Math.round(v / 2)} ms`),
  bastion: (T, on) => log(T, 'info', 'tier', on ? 'Bastion 应急池已启用' : 'Bastion 应急池已关闭', on ? '只在 Vanguard 与 Sentinel 都不可用时接管 · 请勿登录任何账号' : '不再收集公共节点'),
  updated: (T, v) => log(T, 'info', 'runtime', `已更新到 Tesseract ${v}`, '安装包已通过签名校验'),
  ruleData: (T, failed) => log(T, failed ? 'warn' : 'info', 'source', failed ? '规则数据部分更新失败' : '规则数据更新完成', failed ? '网络运营商（ASN）· 连接超时' : '网站分类 · IP 归属地 · 国家与地区 · 网络运营商'),
  updateFailed: T => log(T, 'warn', 'runtime', '检查更新失败', '连接超时'),
  updateFound: (T, v) => log(T, 'info', 'runtime', '发现新版本', `Tesseract ${v} · 已签名`),
};
