# 渲染矩阵的场景用例（T-P4-10）

`node ui/test/render/run.mjs` 读取本目录下的每个 `*.mjs`，每个文件默认导出一个用例数组。页面工单各自建 `cases/<page>.mjs`（`overview.mjs`、`tiers.mjs`、`logs.mjs`、`settings.mjs`、`shell.mjs`……），本单只提供框架和 `smoke.mjs`。

## 用例格式

```js
export default [
  {
    name: 'start_failed_listener_conflict',   // 可省略：默认取场景名；只允许 [A-Za-z0-9_.-]
    scenario: 'start_failed_listener_conflict', // ui/test/fixtures/scenarios/<名>.json，或者直接给场景对象
    page: 'overview',                         // overview | profiles（层级）| logs | settings
    params: { tier: 'sentinel' },             // 可选：加载后 T.go(page, params)
    actions: [                                // 可选：按顺序执行，每步之后推进 0 ms 虚拟时间
      { advance_ms: 3200 },                   // T.clock（FakeTransport 的假时钟）和页面计时器一起推进
      { page_ms: 500 },                       // 只推进页面计时器（fixture_clock.js）
      { deliver: { t: 'evt', topic: 'service.state', /* … */ } }, // 加载后再投递一帧
      { click: '#ov-restart' }, { hover: '.x' }, { press: 'Escape' }, { fill: ['#q', 'abc'] },
      { go: 'settings', params: { section: 'alerts' } },
      { eval: () => { /* 在页面里执行 */ }, arg: null },
    ],
    langs: ['zh_CN', 'pseudo'],               // 可选：只在这些语言里跑（与 --langs 取交集）
    sizes: ['1060x700'],                      // 可选：只在这些尺寸里跑（与 --sizes 取交集）
    leak_allow: ['^Mihomo$'],                 // 可选：本用例额外的漏抽白名单（整词正则）
    overflow_allow: [                         // 可选：本用例允许的截断（选择器正则 + 理由），命中的记入 overflow_allowed，不判失败
      { selector: '^#lg-list ', why: '日志正文按设计横向截断，悬停看全文' },
    ],
    garbage_allow: [                          // 可选：本用例允许的「乱码词」（选择器正则 + 理由），例如数据本身就是 undefined 这个词
      { selector: '^#lg-list ', why: '…' },
    ],
    known_fail: { owner: 'T-P4-05a', why: '…' }, // 可选：已知失败。照常渲染和检查，失败格不计入退出码，
                                              // 写进 summary.known_failures；修好后通过的格列在 known_fail_passed，届时删掉这一项
    expect: async ({ page, context, tree, checks, lang, size, scenario }) => {
      // 可选：返回 {problems: [字符串…], metrics: {…}}；抛异常或 problems 非空即本格失败
      return { problems: [] };
    },
  },
];
```

- **场景**沿用 `P4_架构.md` §2.7 的格式（`deliver` / `advance_ms` / `jump_ms` / `on_invoke`），步骤在页面连接时一次放完；`advance_ms` 驱动注入的假时钟，不真等。页面在加载过程中发出、场景里没有登记 `on_invoke` 的调用记为失败（`fixture_invoke`）。新场景文件放 `ui/test/fixtures/scenarios/`，由 `tools/validate_ui_fixtures.py` 校验；只在用例里临时改一两帧时可以像 `smoke.mjs` 那样读入既有场景后改写（写明理由）。
- **命名**：场景名用「页面_状态_细节」，例如 `overview_start_failed_listener_conflict`、`tiers_bastion_empty_pool`、`logs_gap_after_reconnect`；用例名默认与场景名相同。每页按映射文档「边界与失败状态」表各补一组。
- **数据类文本**（节点名、订阅名、日志正文、IP、协议名、演示数据）由渲染代码加 `translate="no"`；漏抽检查不读这些子树。白名单之外的数据词不要加进 `leak_allow`，应该加 `translate="no"`。
- `expect` 在截图、文本树、机器检查都完成之后运行，可以改动页面（例如量宽度），不影响本格的截图和快照。

## 运行

```
npm --prefix ui run build -- --pseudo --i18n-trace          # design 目标带伪语言和 id 追踪
node ui/test/render/run.mjs --case smoke                    # 默认 zh_CN、pseudo × 1060x700、1280x800
node ui/test/render/run.mjs --page overview --langs all     # 9 种语言；同时写 truncation.json 给 i18n_check
node ui/test/render/run.mjs --case smoke --langs all,pseudo --sizes 1280x800
node ui/test/render/run.mjs --build-flags "--h-features all"   # 另建一份私有 design 构建（临时目录，缺 --pseudo / --i18n-trace 时自动补上）再渲染
node ui/test/render/run.mjs --case smoke --csp strict       # 严格 CSP：回环 http（serve.mjs）+ 外壳自己的 CSP <meta>（T-P4-08b）
node ui/test/render/run.mjs --case smoke --repeat 2         # 每格跑两遍：文本树、截图必须逐字节一致
node ui/test/render/run.mjs --self-test                     # 框架自检
```

产出在 `.build/新界面迁移_工单/evidence/render/<run>/`（不进仓库；`--run <名>` 指定目录名）：

- 每格 `<用例>/<语言>/<WxH>/`：`screenshot.png`、`textree.json`、`checks.json`（全部检查结果、字体样本、度量）、`ids.json`（只在 `--i18n-trace` 构建里出现：这一格可见的消息 id，供 T-P4-12 生成「id → 截图」索引）；
- `summary.json`：每格结果、失败格及其截图路径、平均每格耗时；
- `truncation.json`：只在 `--langs all` 时写，`tools/i18n_check.mjs` 的 `truncation` 检查读最新的一份。文件带 `coverage`（`cases`、`pages`、`langs`、`sizes`、`source_digest`、`h_features`、`build_flags`、`complete`）；只有不带 `--case` / `--page` 过滤、9 种语言、两种尺寸都跑了的运行 `complete` 才是 `true`。

退出码非 0 表示有失败格。

## 机器检查（每格都跑）

| 检查 | 规则 | 失败条件 |
|---|---|---|
| overflow | 有自身可见文字的元素（行内元素按最近的非行内祖先量）。横向：文字超出自身内边距盒（`scrollWidth > clientWidth + 1` 且文字矩形越界 > 1 px），或超出最近一个横向不为 `visible` 的祖先 > 1 px。纵向：最近一个纵向裁切（`overflow-y: hidden / clip`，含自身）的祖先里，同一个盒有的行在里面、有的行越出底边或顶边 > 1 px（整盒都在外面的是按设计隐藏，例如里程表数字条，不算）。省略号盒再用自身边框盒对裁切祖先判一次 | 裁切方式是 `hidden` / `clip` 且没有生效的省略号（`text-overflow: ellipsis` 或 `-webkit-line-clamp`；生效的只记 `ellipsized`）；省略号盒自身被祖先裁掉记截断（`by: ancestor_of_ellipsis`）。`auto` / `scroll` 只是可滚动，记 `scrollable_x`，不算失败。1 px 以下的 `.sr-only` 盒不量。用例的 `overflow_allow` 命中的记入 `overflow_allowed`。**语言**：只有 `zh_CN`、`pseudo` 格判失败；其余 8 种语言的截断只写进计数、`checks.json` 和 `truncation.json`，级别与豁免归 `i18n_check`（`i18n_gates.json` 的 `truncation`：P4 报告、P7 硬判；豁免 `tools/i18n_allow.json`） |
| host_scroll | `#host`、`body`、文档 | 任一 `scrollWidth > clientWidth + 1` |
| leak | 只在 `pseudo`：可见文本节点（含 SVG 的 `<text>`、`<title>`）、CSS `::before` / `::after` 的引号内容、可见元素的 `aria-label` / `title` / `placeholder` / `alt` / `data-tip` 和按钮型 `<input>` 的 `value`，`⟦ ⟧` 之外不得有字母文字；`translate="no"` 子树不读；白名单见 `checks.mjs` 的 `LEAK_WHITELIST`（层级代号、产品名、协议名、IP、AS 号、版本号、十六进制 id）。括号只在同一个块内配对（富文本拆成几个行内节点仍能配对），不跨块；块结束时仍未闭合，或出现没有对应 `⟦` 的 `⟧`，记一条 `unbalanced`，下一块从深度 0 重新开始。不读：其他 `<input>` 的值、canvas、图片 | 有不在白名单的词，或括号失衡。**过渡**：所在区域的源文件首行仍是 `@i18n-pending` 时记为 `leaks_pending`，不算失败（页面区 `#pages` → `pages/<page>.js`，其余 → `index.html` / `core.js` / `app.js`；每条记录带 `region`：`pages` / `layer` / `shell`）；`--leaks hard` 时一律失败 |
| inject | `script:not([src])`、`[onerror]`、`[onload]`、`img[src="x"]`、`window.__pwned` | 出现任何一个 |
| console | `console.error`、页面异常、假计时器回调异常、未登记的 fixture 调用、任何非 `file:` 网络请求（`--csp strict` 时本格自己的回环源除外） | 出现任何一个 |
| csp | 只在 `--csp strict`：构建快照经 `serve.mjs`（`node:http`，只绑 127.0.0.1、随机端口、只服务这一个目录）加载，每个 `.html` 的 `<head>` 首位注入外壳 `shell/app/tauri.conf.json` 的 CSP（另加 `'report-sample'`，不放宽任何来源；策略里 style-src / script-src 带 `'unsafe-inline'`、nonce、hash 时拒绝运行）。用例里只给测试用的样式改用构造样式表（`new CSSStyleSheet()` + `adoptedStyleSheets`），不插 `<style>` | 任何 `securitypolicyviolation`（`checks.json` 记指令、被拦元素、样本、源文件行号） |
| font | 只在 `ja_JP`、`ru_RU`（Chrome 断网启动）：ja 的探针文字和页面里含假名的元素不得用 Microsoft YaHei / SimSun；ru 的探针句子和页面里的俄文元素整句只用一种字体 | 违反；探针一种字体都没测到（`font_unmeasured`） |
| garbage | 可见文字和可见元素的 `aria-label` / `title` / `placeholder` / `data-tip` 里的 `NaN` / `undefined` / `[object Object]`（整词；`translate="no"` 的数据也读） | 出现任何一个（T-P4-07 起硬判，决定_第八波裁定）；用例的 `garbage_allow` 命中的记入 `garbage_allowed` |
