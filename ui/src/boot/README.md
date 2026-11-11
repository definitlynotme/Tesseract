# 挂点文件与归属（P4 架构 §1.1.1）

入口 `src/entry/app.js`、`src/entry/design.js` 只有固定的副作用导入行，由 T-P4-01 一次写定，之后各单**不改入口**，只改自己名下的挂点文件。
每个挂点同一时间只归一张单；下表「归属」一列按先后顺序排列。

| 挂点 | 导入它的入口 | T-P4-01 的初始内容 | 归属（依次） |
|---|---|---|---|
| `boot/i18n.js` | app、design | 空 | T-P4-03 |
| `boot/data-app.js` | app | `import '../design/data.js'`（过渡：页面仍读 `T.data`） | T-P4-02b 改为 `import '../data/static.js'` + 过渡导入；T-P4-07 删掉过渡导入（已删：app 目标不导入 `design/` 下任何文件） |
| `boot/data-design.js` | design | `import '../design/data.js'` | T-P4-02b；T-P4-07 加 `import '../design/demo.js'`（演示控件经 `T.ext` / `T.cmd` 注册） |
| `boot/rt-app.js` | app | `import '../design/sim.js'`（原型行为） | T-P4-02a：删掉 sim，改为 `legacy_sim.js` + `alias.js` + `T.rt.connect(new TauriTransport())`；T-P4-07 删 `legacy_sim`、`alias`（已删，两个文件一并删除） |
| `boot/rt-design.js` | design | `import '../design/sim.js'` | T-P4-02a 加 fixture 模式（`window.__TESSERACT_FIXTURE__`）；T-P4-02b 非 fixture 时改为 sim 模式；T-P4-07 去掉 `legacy_sim`、`alias` |
| `boot/shellc.js` | app、design | 空 | T-P4-06 |
| `boot/design.js` | design（末尾） | 空（design 专用杂项，例如 `?lang` 处理） | T-P4-10 |
| `build/locales.mjs` | `build.mjs` | 空插件（`prepare()` 返回空结果） | T-P4-03 |
| `build/fonts.mjs` | `build.mjs` | 空插件（`prepare()` 返回空结果） | T-P4-08a |
| `package.json` scripts | — | `build`、`build:app`、`build:design`、`check`、`test`、`test:render`、`test:e2e`、`test:perf`、`test:a11y` 一次写全；脚本文件还不存在时由 `tools/run_if_exists.mjs` 打印「未实现」并返回 0 | 只有 T-P4-01；之后的单只新建脚本文件，不改 `package.json` |
| `tools/check.mjs` | `npm run check` | 固定顺序：`build.mjs --check` → `scan.mjs` → `gen_rt_spec.py --check` → `validate_ui_fixtures.py` → `i18n_check.mjs --level p4`；工具不存在就打印「未实现」跳过 | 只有 T-P4-01；规则转硬只改 `tools/scan_gates.json` / `tools/i18n_gates.json` 里属于自己的那一行 |

## 入口顺序

与原型 `index.html:53-61` 的 `<script>` 顺序等价：

```
core → icons → boot/i18n → boot/data-* → boot/rt-* → boot/shellc → pages(overview, tiers, logs, settings) → core/app
design 目标末尾另加 boot/design
```

## 构建插件接口（`build/locales.mjs`、`build/fonts.mjs`）

两个文件都默认导出 `async prepare(ctx)`，`ctx = {target, uiRoot, pseudo, i18nTrace, dev}`，返回（字段都可省略）：

- `define`：合并进 esbuild `define`（例如 `__LOCALES__`，值是 JSON 字符串）；
- `plugins`：追加的 esbuild 插件；
- `loader`：合并进 esbuild `loader`（例如 `{'.woff2': 'file'}`）；
- `files`：额外输出的文件 `[{path, contents}]`（`path` 相对输出目录，正斜杠；`contents` 为 `Uint8Array` 或字符串），会写进输出目录并登记进 `build-manifest.json`；
- `scripts`：要在主脚本之前用 `<script src>` 引用的文件路径（必须同时出现在 `files` 里）；
- `locales`：写进 `build-manifest.json` 的 `{lang: file}` 映射（只有 `locales.mjs` 用）。

`build.mjs` 只负责合并这些结果，不认识语言和字体的细节。
