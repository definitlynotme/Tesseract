# Tesseract 新界面（`ui/`）

Tauri 2 外壳里的 Web 界面。设计规范见 `DESIGN.md`（约束性），与侧车的合同见 `../bridge_schema/PROTOCOL.md`（v1 唯一合同），架构见 `../.build/新界面迁移_工单/P4_设计/P4_架构.md`。原型 `../.build/claude_ui_prototype/` 只作设计参考，不再改。

- 全部文件 LF 换行、UTF-8 无 BOM。
- 依赖只用 `package-lock.json` 锁定的版本（esbuild、acorn、acorn-walk、@fontsource、playwright-core）。安装用 `npm --prefix ui ci --ignore-scripts`，首次需要联网，须经用户批准；之后任何依赖变更都要报 Claude 批准。
- 渲染、性能、可访问性、端到端测试用本机 Chrome（`C:/Program Files/Google/Chrome/Application/chrome.exe`，可用 `TESSERACT_UI_CHROME` 改）。没有 Chrome 时浏览器单测会 skip，而 `node --test` 与 `check.mjs` 都不把 skip 判为失败；第八波裁定要求「skip 只能是已登记条目」，所以：设了 `TESSERACT_UI_REQUIRE_CHROME=1` 时，`test/unit/chrome_required.test.mjs` 在找不到 Chrome 时直接失败（P7 的发布 CI 必须装 Chrome 并设这个变量）；没设时这条只打印诊断，skip 列表要**人工核对**（看 `node --test` 输出的 skip 行，只能是下表登记的那一条）。

## 1. 构建

| 命令 | 作用 |
|---|---|
| `npm --prefix ui run build` | 两个目标都构建：`dist/app`（进外壳）、`dist/design`（设计评审与渲染测试） |
| `npm --prefix ui run build -- --pseudo --i18n-trace` | 同上，design 目标另带伪语言与 `data-i18n-id`；**跑渲染矩阵前必须先这样构建** |
| `node ui/build.mjs --target app\|design [--out <dir>] [--clean] [--pseudo] [--i18n-trace] [--h-features all] [--check]` | 单目标；`--h-features all` 只给 design（把登记表里全部 B 项当已放行，用于验证 B 项控件的打开分支）；`--check` 只在内存构建并跑门禁 |
| `powershell -File scripts/shell_copy_ui.ps1` | 用当前 `ui/` 构建 app 目标到 `shell/app/ui/`（外壳嵌入它；`build.rs` 检查 `build-manifest.json` 的 `target='app'`） |

产物：每个目标一个带内容哈希的 IIFE、一个 CSS、每种语言一个 `locale-<lang>.<hash>.js`、字体文件和 `build-manifest.json`（`source_digest`、`demo_free`、`h_features`、`locales`）。设置 `SOURCE_DATE_EPOCH` 时两次构建逐字节相同。

## 2. 测试

| 命令 | 内容 |
|---|---|
| `node --test ui/test/unit/` | 单元测试（node:test）。skip 只允许已登记的条目：目前只有 `settings_i18n` 的「superseded by T-P4-05d」一条（人工核对；带 `TESSERACT_UI_REQUIRE_CHROME=1` 时缺 Chrome 直接失败，见开头） |
| `node ui/test/render/run.mjs --self-test` | 渲染框架自检 |
| `node ui/test/render/run.mjs --case smoke --leaks auto` | 冒烟格 |
| `node ui/test/render/run.mjs [--leaks auto]` | 渲染矩阵全量：全部用例 × zh_CN + pseudo × 1060x700、1280x800。zh_CN 与 pseudo 格内截断判失败 |
| `node ui/test/render/run.mjs --langs all` | 9 种产品语言 × 两种尺寸，另写 `truncation.json`（见 §2.1） |
| `node ui/test/render/run.mjs --csp strict` | 经本机回环 http 服务按外壳 CSP 加载，任何 CSP 违规判失败 |
| `node ui/test/render/run.mjs --build-flags "--h-features all" --case <名>` | 用打开全部 B 项的 design 构建渲染 |
| `node ui/test/perf/run.mjs --target design\|shell [--build]` | 长任务测量（架构 §10；shell 目标走外壳开发构建、custom 作用域） |
| `node ui/test/perf/settings_behavior.mjs`、`logs_behavior.mjs` | 配置页、日志页行为回归 |
| `node ui/test/a11y/run.mjs [--shell]` | 可访问性 |
| `powershell -File scripts/p4_ui_e2e.ps1 [-RealSidecar]` | 端到端（外壳开发构建 + CDP，custom 作用域与临时目录；会弹真实系统气泡并抢前台，由 Claude 在用户空闲时安排） |
| `.venv/Scripts/python.exe -B ui/tools/validate_ui_fixtures.py` | 全部场景 fixture 过 `bridge_protocol` 的校验器 |

证据一律写到 `.build/新界面迁移_工单/evidence/`（渲染矩阵在 `evidence/render/<run>/`，每格有截图、可见文本树、`checks.json`，带 `--i18n-trace` 时另有 `ids.json`），不进源码树。

### 2.1 全语言矩阵与截断汇总的口径（P5、P7 用）

- `truncation.json` 只在 `--langs all` 时写；它的 `coverage.complete` 只有在**不带 `--case` / `--page`、9 种语言、两种尺寸都跑**时为 `true`。
- `tools/i18n_check.mjs` 自动查找时**只认 `complete:true` 且 `coverage.source_digest` 等于当前源码摘要（`build.mjs` 的 `sourceDigest`，覆盖 `src/`、`css/`、`build/`、`build.mjs`、`package-lock.json`）的那份**：取最新的一份；更新的局部运行计入 `skipped_partial`，完整但摘要对不上（旧构建）的计入 `skipped_stale`，都不读。找不到时报「no truncation summary」——p4 只报告，p7 失败。显式 `--truncation <文件>` 照读，结果带 `complete` 与 `digest_match`；`truncation` 为 hard 的级别（p7）下，不完整或摘要对不上的显式文件也判失败。`--source-digest <hex>` 可指定要比对的摘要（缺省为当前源码）。注意摘要不覆盖 `test/render/cases/`：改了渲染用例而源码没变时，旧汇总仍算当前，需要自己判断是否重跑。
- P7 转硬前的产出口径：在 `ui/` 源码冻结后，先 `npm --prefix ui run build -- --pseudo --i18n-trace`，再 `node ui/test/render/run.mjs --langs all --run full-<日期>`（不加任何过滤），用这次的 `truncation.json` 判 `truncation`（`i18n_gates.json` 里 p7 为 hard）；局部运行的汇总只作参考。
- 伪语言系数（`src/i18n/pseudo.js` 的 `PSEUDO`）：以 zh_CN 为底时每个汉字或全角符号 3 个带重音的拉丁字母、整体再加 10%；两个字以内的短串按每字 5 个字母计，且至少比原文多 4 个字母；以 en_US 为底时加 40%。依据：DESIGN §8 中最长的德语、俄语（T-P4-10 报告、第五波裁定 T-P4-10 #3）。

## 3. 门禁

`npm --prefix ui run check`（`tools/check.mjs`）按固定顺序跑：`build.mjs --check` → `scan.mjs` → `gen_rt_spec.py --check` → `validate_ui_fixtures.py` → `i18n_check.mjs --level p4`。

### 3.1 `tools/scan_gates.json`（源码与产物扫描）

```json
{ "cjk": "enforce", "demo": "enforce", "inject": "enforce", "style": "enforce", "features": "enforce",
  "features_pages": ["logs", "overview", "tiers", "settings", "shell"] }
```

- 每条规则 `report`（只打计数）或 `enforce`（有发现即失败）。P4 出口时全部 `enforce`。单独跑：`node ui/tools/scan.mjs --rule cjk|demo|inject|style|features|all [--mode report|enforce] [--json]`。
- `cjk`：JS 字符串、模板、正则，`index.html` 文本，CSS `content:` 里不得有中文（注释不查；`src/design/**`、`css/design.css`、目录文件除外）。
- `demo`：`dist/app` 里不得出现 `tools/demo_markers.json` 的标记，`build-manifest.demo_free` 必须为真。
- `inject`：注入规则（DESIGN §5.7）；例外只在 `tools/inject_allow.json`，每条写理由和行摘要，行内容一变例外自动失效。
- `style`：不得有 `style=""` 属性或 `setAttribute('style')`（CSP 不带 `'unsafe-inline'`）。
- `features`：`T.rt.feature('…')` 与 `src/features.json` 的名字都在 `src/rt/feature_registry.js` 登记；`features_pages` 里的页面，登记表中本页的每一项至少被引用一次。
- 规则：一张单只改属于自己的那一行，或在 `features_pages` 追加本页；改前重读文件。

### 3.2 `tools/i18n_gates.json`（目录检查的级别）

- 形如 `{ "<check>": { "p4": "hard"|"report", "p7": "hard"|"report" } }`；文件里没有的检查一律按 hard。
- `node ui/tools/i18n_check.mjs --level p4|p7 [--report out.json] [--truncation <json>]`。P4 的硬项：json、icu、args、plural、desc、frozen、backend_keys、catalog 与 6 个 reason 命名空间；只报告：id_format、orphans、missing、glossary、truncation、three_catalogs。P7 起 orphans、missing、glossary、truncation 都转硬（`--level p7` 就是 P5 / P7 的验收口径）。
- 规则：某项转硬只改这一行；新增检查先在 `tools/i18n_check.mjs` 的 `CHECKS` 里加，再在这里给级别。

### 3.3 冻结（`src/i18n/frozen.json`）

`{ id: src_hash }`，`src_hash` 是 zh_CN `m` 的 sha256 前 16 位（`i18n_check.mjs` 的 `srcHash`）。P4 出口时 zh_CN 全部 13 个命名空间、2,773 条已冻结。改冻结 id 的文案或删除它，必须同一次改动里更新 `frozen.json` 并在报告里列出。

## 4. 目录

```
ui/
  build.mjs, build/{locales,fonts}.mjs   构建入口与两个插件
  src/
    index.html                模板（没有内联脚本和样式）
    entry/{app,design}.js     入口（固定，不改）
    boot/                     挂点文件（归属见 src/boot/README.md）
    core/{core,icons,app}.js  T.$ / T.html / T.ui / T.motion / 路由 / 通知 / 命令面板；外壳级接线
    data/static.js            层级与网络模式的静态元数据
    rt/                       T.rt 客户端（DESIGN §5.5）；spec.gen.json 只许 gen_rt_spec.py 生成
    shellc/                   外壳—页面合同（通知队列、横幅、退出确认、可见性、跳转、语言、更新提示）
    letter/                   给用户的信（侧栏产品名是入口；letter.js 页面与已读版本、art.js 插图；DESIGN §6.22）
    i18n/                     T.t / T.tx / T.ti、ICU、Intl、伪语言、glossary.json、frozen.json、locales/<lang>/<ns>.json
    pages/                    overview.js、tiers.js、logs.js（+ logs/）、settings.js（+ settings/）
    design/                   只进 design 目标：sim.js、frames.js、合成数据、演示控件、catalog.json
    features.json             ENABLED_H_FEATURES（钥匙 2，P4 出口为空）
  css/                        tokens / fonts / base / components / shell / pages/*；design.css 只进 design
  fonts/                      自托管字体，各带 OFL：Instrument Sans、Geist Mono 构建时从 @fontsource 复制；
                              Tesseract Kai（中文 voice，霞鹜文楷 GB 屏幕阅读版 1.530 的改名子集）是源文件，
                              由 tools/zh_voice_font.py 生成（zh_CN 文案出现新字时重跑，fonts.test.mjs 会提醒）
  test/
    unit/                     node:test
    fixtures/scenarios/       FakeTransport 回放场景（validate_ui_fixtures.py 校验）
    fixtures/malicious/       恶意文本场景
    fixtures/i18n/            i18n_check 的通过与失败夹具
    render/                   渲染矩阵（run.mjs、checks.mjs、textree.mjs、cases/*.mjs、fixture_clock.js）
    baseline/                 T-P4-01 / T-P4-10 的原型基线（logs_i18n、overview_i18n 的单测或用例仍比对对应基线；settings 那条已 skip，tiers 基线已无引用）
    perf/、a11y/、e2e/        性能、可访问性、端到端
  tools/                      check.mjs、scan.mjs、i18n_check.mjs、gen_rt_spec.py、validate_ui_fixtures.py、
                              backend_keys.py、py_catalog_dump.py、font_cmap.mjs、zh_voice_font.py、fixture_gen/（§6）
```

## 5. 挂点与文件归属（P4 结束时）

| 区域 | 归属单（P4） | 之后的规则 |
|---|---|---|
| `src/entry/*`、`package.json` scripts、`tools/check.mjs` | T-P4-01 | 不再改 |
| `src/boot/*.js` | 各挂点一张单（`src/boot/README.md`） | 只改自己名下的挂点 |
| `src/rt/**`、`boot/rt-*.js` | 02a（rt 层）、02b（design 适配）、H / H2（修补） | 不得有中文字面量；`spec.gen.json` 只许脚本生成 |
| `src/i18n/**`、`build/locales.mjs`、`tools/i18n_*` | T-P4-03 | 各页只改自己的命名空间；`frozen.json` 只追加或按 §3.3 同步 |
| `src/core/core.js` | 03（`T.fmt`、`T.html`）、04a（其余）、05d（`T.ui.saveBar`）、06（通知、横幅、路由、命令面板、`T.motion`、`T.ui.busy`） | 按区域改 |
| `src/shellc/**`、`boot/shellc.js`、`css/shell.css` | T-P4-06 | — |
| `src/pages/overview.js` 等各页及其 CSS、场景、`rt/project_<页>.js` | 05a 总览、05b 层级、05c 日志、05d / 05e 配置与提醒 | 本页单独占 |
| `fonts/`、`css/fonts.css`、`build/fonts.mjs` | T-P4-08a | — |
| `test/render/**` | T-P4-10 | 各页只改自己的 `cases/<页>.mjs` |
| `test/e2e/**`、`scripts/p4_ui_e2e.ps1` | T-P4-11 | — |
| `tools/scan_gates.json`、`tools/i18n_gates.json` | 共享 | 只改自己那一行（§3） |
| `DESIGN.md`、`README.md`、`tools/fixture_gen/` | T-P4-12 | — |

## 6. fixture 生成器（`tools/fixture_gen/`）

05a–05e 的场景生成器（原先留在会话 scratchpad 或 evidence 里，第八波裁定 05a-§11.5 要求统一收进这里）。都以 ui 目录为第一个参数（省略时取脚本所在的 `ui/`），**直接写 `test/fixtures/scenarios/`**。已过时的三个（`tiers.mjs`、`tiers_fix1.py`、`alerts.mjs`）不带 `--force` 时拒绝写入并退出（T-P4-12 修复轮 1），要比对请带 `--force` 在 ui 目录的副本上跑。改场景时优先改生成器再重新生成；先在副本里跑、和现有文件比对，再决定是否覆盖。

| 文件 | 生成的场景 | 用法 | 与现有 fixture 的一致性（T-P4-12 在副本里重跑后比对） |
|---|---|---|---|
| `overview.py`（05a） | `overview_*.json` 71 个 | `.venv/Scripts/python.exe -B ui/tools/fixture_gen/overview.py [ui [名字 …]]`（不带参数取脚本所在的 ui；只生成部分场景时必须先给 ui 目录） | 71/71 逐字节一致 |
| `logs.py`（05c） | `logs_*.json` 34 个 | `.venv/Scripts/python.exe -B ui/tools/fixture_gen/logs.py [ui]` | 34/34 一致（`logs_folder_failed` 的 `kind:'data'` 已随第八波 #5 补进生成器） |
| `settings.py`（05d） | `settings_*.json` 47 个 | `.venv/Scripts/python.exe -B ui/tools/fixture_gen/settings.py [ui]` | 47/47 一致 |
| `settings_i18n_patch.py`（05d） | 给 04e 的 `settings_i18n_{running,stopped}` 补 05d 页面要读的帧与应答 | `.venv/Scripts/python.exe -B ui/tools/fixture_gen/settings_i18n_patch.py [ui]` | 幂等（已打过补丁的文件跳过） |
| `tiers.mjs`（05b） | `tiers_*.json` 13 个；第二个参数目录另写 `frames.json`（渲染用例用的帧） | `node ui/tools/fixture_gen/tiers.mjs --force [ui] [frames 输出目录]`（不带 `--force` 拒绝写入） | **已过时，只作参考**：T-P4-H2 手改了其中 11 个（`gateway.current` 的 selected/effective 按运行时不变式改为 null、desc 补充），重跑会把 H2 修掉的承载层与 effective 矛盾带回来 |
| `tiers_fix1.py`（05b 修复轮） | 新增 `tiers_import_unapplied.json`、给 `tiers_reset.json` 加 `nodes.reveal` 应答 | `.venv/Scripts/python.exe -B ui/tools/fixture_gen/tiers_fix1.py --force [scenarios 目录]`（不带 `--force` 拒绝写入） | 跟在 `tiers.mjs` 之后，状态同上 |
| `alerts.mjs`（05e） | `alerts_*.json` 22 个，并给 04e 的 `settings_i18n_*` 换上真实 catalog 与提醒数据 | `node ui/tools/fixture_gen/alerts.mjs --force [ui]`（不带 `--force` 拒绝写入） | **会改动现有 fixture，先别重跑**：生成器从 `src/design/catalog.json` 取 catalog，而 T-P4-H3 已把 SMTP 服务商预设加进 catalog，重跑会给 21 个场景加上 `presets` / `preset_aliases`；对 `settings_i18n_*` 不幂等（重复追加步骤）；`alerts_a02_pair_failed` 的后续手改也会丢 |

未收录：06 的 `shell_*` 生成器与 07 的恶意 fixture 生成器仍在原会话 scratchpad（07 报告写明「生成脚本不进仓库，每个文件的 desc 写明出处」）。
