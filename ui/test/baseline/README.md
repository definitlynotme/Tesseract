# 原型基线（T-P4-01，架构 §1.4，批评 E1）

04 系列做消息抽取时用来比对的基线：design 目标（此时仍跑原型 `sim.js`）四个页面在 1280×800 下的截图和可见文本树快照。

| 文件 | 内容 |
|---|---|
| `<page>.textree.json` | 可见文本树快照，`page` ∈ `overview`、`tiers`、`logs`、`settings`。每行一个可见元素（DOM 顺序）：`[深度, 标签, 自身文本, aria-label, title, placeholder]`；「自身文本」是该元素直接子文本节点拼起来、空白折叠后的结果（子元素的文本记在子元素那一行，整棵树拼起来就是 `textContent`）。四项全空的元素不记。 |
| `<page>.png` | 同一时刻的截图（只作参考，比对以文本树为准） |
| `capture.mjs` | 产出脚本 |

## 产出条件

- `node ui/test/baseline/capture.mjs`（先 `npm --prefix ui run build:design`）；加 `--prototype-out <dir>` 时另抓原型目录 `.build/claude_ui_prototype/` 并逐页比对（文本树必须完全一致，像素差只作参考）。
- 本机 Chrome（`C:/Program Files/Google/Chrome/Application/chrome.exe`），playwright-core，无头。
- 确定性夹具 `ui/test/render/fixture_clock.js`（`addInitScript`）：冻结 `Date`、`performance.now`、`Math.random`（固定种子）、虚拟计时器，`prefers-reduced-motion: reduce`；上下文同时 `reducedMotion: 'reduce'`、`timezoneId: 'Asia/Shanghai'`、`locale: 'zh-CN'`、`deviceScaleFactor: 1`。
- 所有非 `file:` 请求一律中止（不联网）。T-P4-08a 之后字体从 `file:` 自托管加载（`ui/fonts/` 自带的 woff2），截图字体与外壳里看到的一致；T-P4-01 产出时 Google Fonts 加载不到、字体回落到本机字体，那一版截图的字体与现在不同。
- 每页一个新上下文，`file://…/index.html#<page>` 加载后推进虚拟时间 3000 ms，再等 200 ms 真实时间让 1 ms 的 WAAPI 动画收尾，然后抓取。
- 同一夹具跑两遍，两份文本树必须完全相同，否则脚本失败。

## 产出记录

- **现行基线：2026-10-07 由 T-P4-10 重新产出**（第四波裁定 02b #5、08a #6）：普通 design 构建（不带 trace），两遍抓取一致。与 T-P4-01 版的差异见 `.build/新界面迁移_工单/T-P4-10_报告.md` §7：文本树只有 overview 一行（02b sim 的「已验证 → 已确认」），像素差来自 08a 的字体自托管；旧文件存于 `evidence/T-P4-10/baseline_old/`。04 系列以这一版为准。
- T-P4-01 首次产出（详见 `.build/新界面迁移_工单/evidence/T-P4-01/prototype/compare.json`）：两遍文本树相同、截图字节相同；design 目标与原型的文本树完全一致，像素差 0。那时字体回落到本机字体（见上）。
