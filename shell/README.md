# 私有管道外壳库

仅支持 Windows MSVC。构建和测试（从 `shell/` 目录）：

```powershell
cd shell
cargo test --offline
```

静态 CRT 的配置位于 `shell/.cargo/config.toml`；从仓库根目录使用 `--manifest-path` 不会加载此目录下的 Cargo 配置。库只负责管道、侧车启动和 NDJSON 字节分帧；协议对象校验由上层处理。`BridgeHost::launch` 的参数为侧车可执行文件和前置参数；库追加 `--ipc-handle` 与 `--shell-pid`。外壳应持续调用 `poll_event`，并在收到 `Disconnected` 或 `ChildExited` 后启动上层收尾流程。外壳不通过 Job 强杀侧车。

身份合同：外壳创建单实例、仅当前用户可访问且拒绝远程客户端的管道，并在启动侧车前先占用唯一客户端连接。客户端句柄只经 `PROC_THREAD_ATTRIBUTE_HANDLE_LIST` 继承给侧车；`CreateProcessW` 成功后，外壳立即关闭本地副本。`launch` 等待侧车首帧 `hello`，核对其中的 `runtime_pid` 与 `CreateProcessW` 返回的 PID；不符时断开管道、终止子进程并返回 `identity_mismatch`。客户端句柄由外壳创建，因此 `GetNamedPipeClientProcessId` 报告外壳 PID，不能用于核对侧车 PID。侧车仍应核对 `GetNamedPipeServerProcessId` 等于传入的 `--shell-pid`。

## P3 Tauri 外壳

产物暂名 `target/debug/tesseract-shell.exe`，不覆盖 Qt 的 `Tesseract.exe`。首次构建先从仓库根目录运行：

```powershell
npm --prefix ui ci --ignore-scripts   # 仅首次；包版本锁定在 ui/package-lock.json（10-07 用户批准的一次性联网安装）
./scripts/shell_copy_ui.ps1
cd shell
cargo build --offline -j 1
cargo test --offline -j 1
```

复制脚本（T-P4-01 起）先用 `ui/build.mjs --target app` 构建到 `ui/dist/app`，核对 `build-manifest.json` 的 `target=app` 和登记的每个文件都在，全部通过后才清空 `shell/app/ui`，再只复制 manifest 登记的文件和 manifest 本身；没有 `ui/node_modules` 时直接报错、不联网。`-Prototype` 是过渡期回退（T-P4-07 删除）：按字节复制原型的 `index.html`、`css/`、`js/`，没有 manifest，release 构建会拒绝。`app/build.rs` 将页面嵌入 exe；开发资源源文件在 `app/dev/`，发布资产排除全部 `dev-rt.*`。图标沿用 `static/app_icon.ico`。暂不使用 Tauri 打包器。

本阶段强制显式 custom 作用域。先创建绝对路径的配置、数据目录，准备自己的隔离 `config.yaml` 和 `sources.yaml`，再设置下列环境变量；不会自动读取正式配置。不要指向 `%APPDATA%/Tesseract` 或 `%LOCALAPPDATA%/Tesseract`。

```powershell
$env:TESSERACT_INSTANCE_SCOPE = "my-p3-test"
$env:TESSERACT_CONFIG_DIR = "C:/my-p3-test/config"
$env:TESSERACT_DATA_DIR = "C:/my-p3-test/data"
$env:TEMP = "C:/my-p3-test/temp"
$env:TMP = $env:TEMP
./target/debug/tesseract-shell.exe
# 开发通道页面：
./target/debug/tesseract-shell.exe --dev-rt
# 已有冻结侧车：
./target/debug/tesseract-shell.exe --sidecar "C:/path/dist-sidecar/tesseract-runtime/tesseract-runtime.exe"
```

### 分发构建（`--features distribution`，T-P6-06）与 `--print-scope`

`app/Cargo.toml` 的 feature `distribution` 默认关闭。只有 `scripts/build_shell_preview.py`（`dist-shell`，以后的正式发布脚本也一样）打开它；开发构建（`shell/target`、全部单测、e2e、p3 冒烟）的行为不变：只接受显式 custom 作用域，默认用仓库 `.venv` 的源码侧车，窗口标题 `Tesseract Shell (P3)`。

作用域判定（`main.rs` 的 `resolve_scope`，环境由参数注入，两种构建同一函数）：

| 环境 | 开发构建 | 分发构建 |
|---|---|---|
| 设了 `TESSERACT_ISOLATED_AUDIT_MANIFEST` | 拒绝 `explicit_custom_scope_required` | 同左 |
| 设了 `TESSERACT_INSTANCE_SCOPE`（空白 → `explicit_custom_scope_required`） | custom，规则同上文（含 `live_scope_forbidden`） | 同左 |
| 只设了 `TESSERACT_CONFIG_DIR` / `TESSERACT_DATA_DIR`，没有标签 | 拒绝 `scope_label_missing` | 同左 |
| 三者都没设 | 拒绝 `explicit_custom_scope_required` | **默认作用域**：`%APPDATA%\Tesseract`、`%LOCALAPPDATA%\Tesseract`（变量缺失或为空时 `USERPROFILE`（或 `HOMEDRIVE`+`HOMEPATH`）下的 `AppData\Roaming` / `AppData\Local`，与 `app_paths` 相同；不存在就创建），旧名字 `bridge_host::scope::names("default","default")` |

hello 必须报告同一个 `{kind,id}`（默认作用域是 `{default,default}`）；单实例名字与 WebView2 用户数据目录（`<data>\WebView2[-admin]\<id>`，默认作用域即 `%LOCALAPPDATA%\Tesseract\WebView2\default`）都取自这次判定。

侧车（分发构建）：默认作用域**总是**运行 `Tesseract.exe` 同目录的 `tesseract-runtime.exe`，带 `--sidecar` 即拒绝（`sidecar_override_forbidden`）；custom 作用域可用 `--sidecar`，不给时同样用同目录的。分发构建在判定作用域后立即做这项检查，早于建目录、写 shell.log 和取单实例名字，所以被拒绝时不在真实配置目录留下痕迹。被拒绝（作用域或侧车）时外壳只向 stderr 写一行后退出 1，GUI 子系统下用户看不到（弹窗提示是后续单）：排查时看 `Tesseract.exe --print-scope` 的 `error` / `sidecar_error`。分发构建不用源码侧车，`--dev-sidecar-script` 被拒绝。分发构建的窗口标题是 `Tesseract`，`--version` 打印 `Tesseract <app_version.txt>`，用法行为 `Usage: Tesseract.exe [--autostart] [--sidecar PATH] [--print-scope]`。

`--print-scope`（两种构建都有，与 `--version` 一样在 `run()` 最前面处理）：只把判定结果打印成一行 JSON 后退出——`build`（`distribution`/`development`）、`kind`、`id`、`config`、`data`、`names`（五个名字）、`webview2`、`sidecar`（`{mode:"frozen",path,exists}` / `{mode:"source"}` / `{mode:"script",path}`）、`sidecar_error`、`error`。判定成功退出 0，被拒绝退出 2（`error` 或 `sidecar_error` 写明代码）。不取任何互斥量或事件，不建目录，不写 shell.log，不启动任何进程。只在伪造的环境下用它（`APPDATA`、`LOCALAPPDATA` 指向临时目录，TESSERACT_* 都不设）：`build_shell_preview.py` 的门禁就是这样核对组装好的 `Tesseract.exe`。

单测顺序（`CARGO_TARGET_DIR` 为新目录时）：先 `cargo build -p bridge_host --bins`（scheduler 的一个测试要运行 `echo_sidecar.exe`），再 `cargo test --manifest-path app/Cargo.toml`，不带与带 `--features distribution` 各一次。

默认源码模式使用 `.venv` 依赖与 `runtime_main.pyw`。Windows venv 的 python.exe 是重定向器，会产生不同的子进程 PID；外壳先通过它查出 base interpreter，再直接启动该解释器，并配置仅包含仓库和 venv site-packages 的 PYTHONPATH，保持启动器 PID 身份校验。冻结模式 `--sidecar` 不改解释器路径。

窗口默认 1280×800、最小 1060×700（逻辑像素），Tauri/Tao 开启 Per-Monitor DPI v2。位置与尺寸写入该作用域的 `window-placement.json`；移除显示器后忽略不可见位置。WebView2 profile 位于该数据目录的 `WebView2/<scope.id>/`，配置目录不同而数据目录相同的作用域也不共用 profile。外壳日志位于 `logs/shell.log`，按大小滚动（1 MiB × 3 个文件，T-P3-04）；只记录固定诊断代码、数值退出状态、校验过的枚举字段和 panic 源文件名/行号，不落盘原始帧。

页面连接调用 `invoke('rt_connect', {channel})`，创建一条 `new window.__TAURI__.core.Channel()`；投递形状为 `{order,frame}`。重连先回放 hello、完整状态投影、同 epoch 作业历史和日志尾，再投递实时帧。`rt:reset` 同时经 Channel 与 Tauri event 发出；新 epoch 清理旧缓存，`runtime_gone` 结束旧等待。日志尾超过 256 批或 1 MiB 时用外壳扩展 `rt:log-gap` 明示事件区间、事件丢失计数与日志记录区间/计数，P4 消费者须支持它。作业缓存最多 65536 事件或 32 MiB；达到上限不静默丢弃，按协议失败清理侧车后重建停止状态会话（最多 3 次）。侧车运行中意外退出的停止状态重拉见下文 T-P3-04。

命令使用 `invoke('rt', {req_id,cmd,args,owner?})`；返回 `{queued:true,id}` 只表示进入外壳队列。`ack/res/jobs` 和主题帧均经同一 Channel。P1 只放行 `service.start/stop/restart` 与已接单、同 owner 的 `cancel {op}`；`app.quit` 只能由外壳的退出入口 `request_quit` 发出（托盘「退出」，或没有托盘时关闭窗口）。start/restart 最多 64 个待回执请求；stop/cancel 有独立控制队列和 32 个待回执名额，退出标志不占队列。状态主题有序缓存；作业 seq 失连续会记录 `sequence_gap`、结束等待者并清理后重建新 epoch，不自动启动服务。

CDP 只在 `debug_assertions` 构建且显式设置 `TESSERACT_SHELL_CDP_PORT`（1024–65535）时启用，绑定 loopback。WebView2 的环境覆盖在 `run()` 最开头清除（T-P3-07）：所有构建都删 `WEBVIEW2_PIPE_FOR_SCRIPT_DEBUGGER`、`WEBVIEW2_WAIT_FOR_SCRIPT_DEBUGGER`；发布构建另删 `WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS`、`WEBVIEW2_USER_DATA_FOLDER`、`WEBVIEW2_BROWSER_EXECUTABLE_FOLDER`、`WEBVIEW2_RELEASE_CHANNEL_PREFERENCE`，开发构建保留这四个。发布资源和命令依据同一编译态：无开发页面、无 `dev_quit`、无 CDP；关闭开发工具、浏览器快捷键、默认右键菜单、缩放、密码保存/自动填充、捏合与滑动导航、外部文件拖放。外部导航和新窗口只交给系统浏览器（限 HTTP/HTTPS 且无凭据），不在 WebView 内打开。

开发 CSP 临时允许 Google Fonts；发布 CSP 默认 self，保留 Tauri IPC 必需的 connect-src、内联样式与 data 图片例外，拒绝远程字体。P4 须自托管字体并去掉原型中的 Google Fonts/preconnect 标记。

端到端冒烟（仓库根目录）：

```powershell
./scripts/p3_shell_smoke.ps1
# 已构建时可加 -SkipBuild；冻结侧车可加 -Sidecar <exe绝对路径>。
```

脚本用临时配置、禁用 Tier3/来源/系统代理/提醒，使用 Node 24 内建 WebSocket 连开发 CDP，通过 dev-rt 页面连接与启停；核验原型、六个快照、白名单、缓存、Channel 顺序、作业终态、bye 和进程退出。只在内存收集帧，落盘受控 summary。空来源 start 的失败终态不会当成服务运行成功；stop 必须 done、bye 必须退出码 0。保留临时目录供审阅。

另存为、更新交接、开机自启参数由 T-P3-06 完成（见文末）。完整退出收尾、注销/关机、侧车崩溃与卡死已由 T-P3-04 完成（见下文）。隐身后显示、WebView2 故障处理（重载 / 重建 / 退避）、退出时等 BrowserProcessExited 由 T-P3-07 完成（见文末）。

## T-P3-02 单实例与旧 Qt 互操作

作用域名字合同为 v3；新增 shell_mutex 和 quitting_event，旧名字不变。外壳在 UI 线程取得 shell 所有权，再建立仅判存在的 legacy 门禁；源码/冻结侧车用 `--legacy-gate inherited` 取得 runtime 所有权并保留父外壳的 legacy 门禁。无父门禁返回 `legacy_gate_not_held`，独立 headless 仍默认 create。旧 Qt 的未拥有 runtime 不会被误认为可以接管；外壳会发出 auto-reset restore 事件然后以 0 退出。

恢复线程只等待 auto-reset 事件，通过主线程显示窗口、取消最小化并置前；`restore_event_received` 表示收到恢复事件。`single_instance::Quitting::mark_quitting()` 是 P3-04 的 manual-reset 退出标记接口，P3-03 的托盘恢复可复用同一窗口恢复动作。开发构建另有 `TESSERACT_SHELL_DEV_MARK_QUITTING=1` 冒烟开关；正式退出状态机（T-P3-04）在每次退出请求时置位它，用户在未保存提示中取消时用 `Quitting::clear()` 清除。另一个外壳退出中或残留侧车收尾时，最多等 30 秒；权限及未知错误记录固定代码、弹本机语言提示并失败退出。原生字符串按消息 ID 汇集于 native_text.rs；9 种语言都是权威（T-P5-01），继续在该文件里手工维护、不从前端目录生成（A16 裁定）；层级角色词和「后台服务」的译法与前端目录一致（T-P5-05），pt 的日志用词已统一为 registros，单测会对照前端目录的同一条（T-P5-09）。CLI `--version/-V/--help/-h` 在作用域解析前退出，不取实例锁。

普通 WebView2 profile 位于 `data/WebView2/<scope.id>/`，已提权进程使用 `data/WebView2-admin/<scope.id>/`。

本工单构建必须设置 `CARGO_BUILD_JOBS=2`，cargo 以 BelowNormal 优先级运行；每次构建起止 UTC 时间追加至 `.build/新界面迁移_工单/evidence/T-P3-02/cpu-windows.txt`。仅做增量 debug 构建，冒烟脚本不会构建。

```powershell
# 必须预先显式设置随机 custom 标签及临时目录；少一个变量就拒绝运行。
$Root = Join-Path ([IO.Path]::GetTempPath()) ('tesseract-p3-02-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path "$Root/config","$Root/data" -Force | Out-Null
$env:TESSERACT_INSTANCE_SCOPE = [Guid]::NewGuid().ToString('N')
$env:TESSERACT_CONFIG_DIR = "$Root/config"
$env:TESSERACT_DATA_DIR = "$Root/data"
./scripts/p3_single_instance_smoke.ps1
./scripts/p3_shell_smoke.ps1 -SkipBuild
```

单实例冒烟覆盖第二个外壳 5 秒内退出、收到恢复事件并验证前台窗口 PID、旧 Qt 替身、残留侧车、正在退出后的重开，以及父锁助手先退出而 inherited 侧车仍持门禁时旧 Qt 被阻止；逐个外壳使用 process_watch 的 Job 成员出生/退出清单，检查活动后代为零，并打开检查五个作用域名字均已不存在。配置与数据只写临时目录，证据统一写 `.build/新界面迁移_工单/evidence/T-P3-02/`。不使用默认名字，不启动正式 Qt，不修改现场配置。前台测试在自身的小型测试窗口接收点击输入，再授权第二次启动；仅向正式主窗口发送关闭消息。每个 watcher/helper 独立清理，错误、进程清单及五个名字检查写入各场景 result.json，失败结果也保留。旧 Qt 分支只发送恢复事件；新外壳分支另执行前台授权并保留失败关闭行为。

## T-P3-03 托盘、关到托盘、托盘提示与气泡

`app/src/tray.rs` 直接调用 `Shell_NotifyIconW`（不用 Tauri tray-icon），图标取 exe 内嵌的应用图标（资源 ID 32512）。托盘归一个隐藏的顶层窗口（类名 `TesseractShellTray`，WS_EX_TOOLWINDOW），因为只有顶层窗口收得到 `TaskbarCreated` 广播；以管理员运行时用 `ChangeWindowMessageFilterEx` 只放行 `TaskbarCreated` 和托盘回调两条消息。

- **显示时机**：主窗口第一次 `show()` 成功后，单次定时器延迟 250 ms 再 `NIM_ADD`（对应 Qt `STARTUP_TRAY_SHOW_DELAY_MS`）。250 ms 内就关窗口时，关闭处理会立即补加托盘（Qt `_ensure_tray_icon`）。
- **Explorer 重启**：收到 `TaskbarCreated` 后重新 `NIM_ADD`（同 ID 残留时先删后加），失败时每秒重试，最多 5 次。更新提示的 `NIM_MODIFY` 失败（Explorer 繁忙超时时图标其实还在）会立即重新加一次；仍失败才记 `tray_icon_modify_failed`，并走同一套每秒一次、最多 5 次的重加。托盘首次显示成功后立即刷新一次提示并挂上陈旧截止定时器，安装前已缓存的运行态也会按时转为离线。
- **左键 / 菜单**：使用 `NOTIFYICON_VERSION_4`；单击（`NIN_SELECT`，键盘为 `NIN_KEYSELECT`）与菜单「显示 Tesseract」都只是 SetEvent 本作用域的 restore 事件，由 P3-02 的 RestoreListener 在 UI 线程执行同一套 show → unminimize → set_focus。右键菜单两项「显示 Tesseract」「退出」，选择以 `WM_COMMAND`（1 = 显示，2 = 退出）回到托盘窗口。
- **退出接口**：`main.rs` 的 `QuitPort::request_quit(app, QuitReason)` 是外壳唯一的退出入口；T-P3-04 起它交给 `lifecycle.rs` 的退出状态机（见下文）；侧车没有在跑时直接以 0 退出。
- **关到托盘**：`tray::close_decision`（内部用纯函数 `tray::close_action`）决定关闭行为；只有普通关闭才会调用补加托盘。托盘可用时只隐藏窗口（不退出、不弹气泡）；托盘不可用走 `request_quit(WindowWithoutTray)`；退出已请求时不再拦截到托盘，等 worker 收尾退出；worker 已结束时按 P3-01 立即退出。
- **托盘提示**：三行 `Tesseract` / `当前层级：{层级名} {角色}` / `代理核心 运行中|已停止`。数据来自 hub 已接受的 `service.state`、`tiers.summary`（以及将来订阅的 `ui.language`）帧：bridge worker 在 `receive` 成功后把这几个主题交给 `tray::Feed`，再向托盘窗口 Post 一条刷新消息；不轮询。无值、`freshness.stale`、运行或启动中超过 3 秒没有新 `service.state`（单次截止定时器，不是轮询）、新 epoch 或侧车离开时，按 Qt 规则显示「离线」和「已停止」。文字超过 `szTip` 的 127 个 UTF-16 单位时截断并加「…」，不拆代理对。
- **语言**：`hello` 之前用 Windows UI 语言（§7.10），`hello.lang` 之后用它，`ui.language` 帧到达后更新菜单和提示。外壳目前没有订阅 `ui.language`（TOPICS 在 transport.rs），由集成单补订阅，托盘代码已能处理该帧。文案在 `native_text.rs`，9 种语言都是权威（T-P5-01），手工维护（A16 裁定）；层级角色词与前端 `tier.<tier>.role` 逐字相同（en 也是小写，如「Vanguard self-hosted」，T-P5-05）。
- **气泡**：`tray::show_balloon(title, text, severity)` 用 `NIF_INFO` 提交，返回值只在 Windows 接受时为 true（PROTOCOL §13）；没有托盘、标题和正文都为空、Windows 拒绝时为 false，结果以固定代码记入 shell.log。严重度用 `tray::Severity::parse`，按 PROTOCOL `alerts.desktop.severity`：`info` → 信息，`warn`（及 `warning`）→ 警告，`error` 与 `critical` → 错误（同 Qt），其余 → 信息。本单不接 `alerts.desktop`、不发回执。开发构建另有 `WM_APP+2` 触发入口（SendMessage 返回 1 表示已提交），整段 `#[cfg(debug_assertions)]`，发布构建没有。
- **隐藏时的开销**：主窗口子类拦 `WM_SHOWWINDOW`，隐藏时把 WebView2 控制器 `IsVisible` 设为 false，页面 `document.visibilityState` 变为 `hidden`，Chromium 停止动画帧并节流定时器；显示时恢复。外壳在 `window::window_visible()` 记录当前可见性，变化写 `window_shown` / `window_hidden`，`ui.presence` 由后续工单上报。

冒烟（仓库根目录，先按工单 CPU 约定增量构建 debug，脚本不构建；环境变量要求同 P3-02）：

```powershell
./scripts/p3_tray_smoke.ps1
```

脚本经 `process_watch.py` 启动 debug 外壳，断言：托盘图标已在 Explorer 注册（`Shell_NotifyIconGetRect`）且晚于首次显示 ≥ 250 ms；投递 `WM_CLOSE` 后进程与侧车仍在、窗口隐藏、页面 `visibilityState` 为 `hidden`（经开发 CDP）；投递 v4 `NIN_SELECT` 回调后窗口恢复且页面可见；模拟 `TaskbarCreated` 后图标重新注册；开发气泡入口返回 1；投递 v4 `WM_CONTEXTMENU` 回调弹出真实右键菜单，从本进程的 `#32768` 窗口经 `MN_GETHMENU` + `GetMenuStringW` 读回三项（含分隔线）及两项文字，须是 `native_text.rs` 中同一语言的「显示 / 退出」，再按 Esc 关闭；投递托盘「退出」命令后外壳退出码 0、侧车退出、图标移除、作用域 WebView 进程与 Job 内后代清零。右键菜单（T-P3-H 修复 1）开在主屏四角里离真实光标最远的一角（`checks.menu_anchor`、`cursor_at_menu`），避免桌面上的人松开鼠标时正好选中菜单项；外壳弹菜单时记 `tray_menu_shown x= y= cursor_x= cursor_y= lbutton= rbutton=`（打开瞬间的光标位置与左右键是否按下），菜单关闭时记 `tray_menu_closed ms=`；菜单打开期间若出现 `quit_requested_tray`，脚本直接报「菜单被用来退出」，可按这两行判断是不是真人操作。证据写到工单目录 `evidence/T-P3-03/tray-<scope>/`。

由于关闭窗口现在是隐藏，P3-02 单实例冒烟的 `close_window` 改为向托盘窗口投递「退出」命令，托盘窗口不存在时才退回 `WM_CLOSE`。T-P3-H 修复 1：托盘不存在时以及失败用例的收尾（`cleanup`）里，先给该外壳可见的 `#32770` 原生框发 `WM_CLOSE`（与 `p3_host_smoke.ps1` 的 `Cleanup-Shell` 相同），侧车握手失败（`hello_timeout`）的外壳不再带着原生框留在桌面上。

## T-P3-01 Astra 修复与发布验收

冒烟通过 `app/process_watch.py` 挂起创建外壳，先加入不启用 kill-on-close 的匿名 Job Object，再恢复。完成端口记录后代出生/退出，持有进程句柄并保存 PID、创建时间、映像和父 PID。验收同时要求活动进程数为零、零活动通知和完整出生计数，避免退出后父 PID 遍历漏检。Job 仅用于测试，不改变产品的退出策略。

残留规则（决定_P3-06 第 6 条，T-P3-H）：外壳退出后，Job 里的其他后代必须在 10 秒内全部退出，否则立刻判残留；只有**本作用域的 WebView2** 有 20 秒宽限。本作用域的 WebView2 = 映像 `msedgewebview2.exe`、属于外壳的 Job（即外壳的进程树），并且命令行的 `--user-data-dir` 在 `<TESSERACT_DATA_DIR>\WebView2[-admin]` 之下（`--webview-data-dir` 可另指定）；读不到命令行或没有这个开关时，以进程树为准，`--user-data-dir` 指向别处的不算。清单里每条记录带 `webview_scope`（`user-data-dir`/`process-tree`/null），结果带 `webview_grace_used`、`last_exit_after_root_s`，失败时带 `residual_after_root_s`。各 p3 冒烟脚本自己的作用域 WebView2 检查用同一辨认方式、同样 20 秒，`p3_host_smoke.ps1` 原来的「10 + 30 秒」本地容忍已删除；脚本侧的 20 秒从外壳进程的退出时刻（`ExitTime`）起算，没有 `--user-data-dir` 时的退回规则不区分大小写（T-P3-H 修复 1）。为避免把正在退出或刚出生、出生通知还排在完成端口里的成员误算成「身份不明的残留」（T-P3-H 修复 1）：判定前先把完成端口里已排队的包取空；逐个句柄等待前后各查一次 Job 计数，身份不明的数量取 `min(前, 后) - 已识别存活数`；如果只是身份不明成员让结果变成失败，要再隔 ≥ 100 ms 复核一次仍成立才判失败。失败结果里的 `kernel_membership_complete` 按实际计算（每个成员都有记录，且仍有活动成员或已收到零活动通知），不再一律写 true。`--grace-selftest <目录>` 用改名为 `msedgewebview2.exe` 的 pythonw 副本（不启动浏览器）实测五种情形：本作用域迟退在宽限内通过、7 棵本作用域 WebView2 在宽限内相继退出也通过、超过宽限失败、别的用户数据目录不享受宽限、其他进程在 10 秒处立刻失败（自测把两个时限缩为 1 秒和 3 秒）。

`TESSERACT_P3_EVIDENCE_ROOT`（T-P3-H）：设置后，各 p3 冒烟（含单实例冒烟的 `single-instance-summary.json`）把证据写到这个目录，不再写进原工单的证据目录。

`scripts/p3_smoke_rounds.ps1 -EvidenceBase <目录> [-Rounds 1,2,3] [-Smokes shell,single_instance,tray,exit,notify,host] [-Label round]`（T-P3-H 修复 1）：验收用的轮次运行器。每个冒烟各用一个 `%TEMP%` 下的 custom 作用域（不碰默认作用域的互斥量和事件名），证据经 `TESSERACT_P3_EVIDENCE_ROOT` 写到 `<目录>/<label><轮>/<冒烟>/`；每轮开始前把机器负载（总 CPU、占用最高的 5 个进程）记进 `<目录>/smoke-runs.txt` 的 `LOAD` 行，便于把「别人占满 CPU 时侧车 hello 超时」这类环境失败与回归区分开。shell 冒烟带 `-SkipBuild`，需先按工单 CPU 约定构建 debug 外壳。`p3_shell_smoke.ps1`、`p3_tray_smoke.ps1` 和单实例冒烟（`p3_instance_smoke.py`）也像 `p3_exit_smoke.ps1` 一样先做一次字节码预热（`import bridge_main, desktop_runtime` 写进临时目录下的 `PYTHONPYCACHEPREFIX`，不写源码树，侧车以 `-B` 只读它）：否则每次启动真实侧车都要重新编译约 2 MB 的 `desktop_runtime.py`，机器被别的进程占满时单这一步就超过外壳 10 秒的 hello 窗口。外壳的 hello 超时不变（T-P3-H 修复 1）。

```powershell
# 仓库根目录：中间进程先退出、孙进程存活的反例
python -B shell/app/process_watch.py --counterexample .build/新界面迁移_工单/evidence/T-P3-01
# WebView2 宽限自测（T-P3-H）
python -B shell/app/process_watch.py --grace-selftest .build/新界面迁移_工单/evidence/T-P3-H/process-watch-selftest
# 从 shell/ 构建和测试，沿用静态 CRT
cargo build --release --offline -j 1
cargo test --release --offline -j 1
# 回到仓库根目录：标准 release 的实际交互验收
./.venv/Scripts/python.exe -B shell/app/release_acceptance.py .build/新界面迁移_工单/evidence/T-P3-01/release-run
```

`--verify-webview` 是仅 release、显式 custom 作用域的固定原生验收流程。`TESSERACT_SHELL_VERIFY_PORT` 指向测试程序的 loopback HTTP 端口（1024–65535），由脚本设置。它不接受任意脚本、URL、帧或页端调试命令，不开放 CDP；通过 WebView2 COM 回读设置、执行固定资源/命令/DOM 检查，由驱动发送真实 Windows 键盘和 Qt Windows OLE 文件拖入，然后正常退出并写入作用域的 `acceptance-result.json`。脚本会移动测试外壳，并在系统浏览器打开本地测试页。跨屏/DPI 实机验收需要相应物理显示器，单屏结果不能替代。

孤立非法帧记固定诊断后丢弃；序号失连续或作业容量耗尽才清理并重建会话。batch 先验证全部轻量版本、序号和容量元数据，再提交缓存，不复制整份作业历史。当前 overlapped 写入完成后按握手屏障、控制 FIFO、单个合并心跳、普通 FIFO 调度；普通队列满不影响控制读取。

GUI subsystem 的 release 启动源码侧车时，为不存在的标准流提供 NUL，避免 Python 无控制台导入失败；冻结侧车启动方式保持原样。发布资源请求显式拒绝 dev-rt，避免 Tauri 将缺失开发资源回退到首页并返回 200。

本轮未修改 Cargo.lock、未新增依赖。首次实现的必要锁文件生成更新，作为 SHOULD-1 的范围例外记录在工单报告；维护者登记仍由 Claude 按工单 README 流程处理。

## T-P3-04 退出收尾状态机、注销/关机、侧车崩溃与卡死

- **退出状态机**（`app/src/lifecycle.rs`，纯逻辑，可注入时钟）：Running → QuitRequested（问页面 / 未保存提示）→ QuitSent（`app.quit` 已交给 worker）→ WaitingSidecar（已收 `bye`）→ Exited。所有出口都经 `QuitPort::request_quit`：
  1. 置位 P3-02 的 quitting 标记；
  2. 页面已连接 Channel 时发 `host:confirmQuit{token,epoch}`，页面用 `invoke('host_confirm_quit',{token,unsaved})` 回复修改项数；最多等 3 秒，不回复或没有页面就继续。`unsaved>0` 弹原生「有 N 项更改没保存，仍然退出？」（默认「否」），取消则回到 Running 并清除 quitting 标记；
  3. 发 `app.quit{reason:"user"}`（宿主消失为 `shell_lost`，P3-06 的更新为 `update`）；
  4. 按进度等待：除 `pong`、`service.state`、`traffic.*`、`logs.batch` 这类采样帧外，任何侧车帧（jobs 边沿、`shutdown` 主题、回复、`bye`、`fatal`、按变化发布的主题）都算进度；收到 `bye` 之前不主动强杀。20 秒没有进度弹原生框：「继续等待」或「强制退出（网络设置可能没恢复）」，并给出数据目录下的 `restore_proxy.bat`、`unkill.bat`；
  5. 收到 `bye` 后等侧车进程退出，外壳用侧车的退出码退出（没有 `bye` 时不会报 0）。Tauri 的 `app.exit(code)` 只把 code 交给 `RunEvent::ExitRequested`，`run_return` 恒返回 0，所以状态机决定的退出码先记进 `ExitSlot`，`run()` 以它为准（第 1 轮修复 M1）；`bye.network_recovery` 的 proxy/firewall 状态写入 shell.log；
  6. 侧车没有在跑（崩溃后重拉间隙、重拉已用尽）时直接以 0 退出。
  外壳自己发出的 `app.quit` / `app.sessionEnd` 的 ack/res 不转给页面。
- **注销/关机**（`app/src/endsession.rs`）：启动时 `SetProcessShutdownParameters(0x3FF)`；主窗口加子类接 `WM_QUERYENDSESSION` / `WM_ENDSESSION`，返回值一律放行。已在退出中（含更新退出）只记录、不发命令；否则先 `ShutdownBlockReasonCreate("正在恢复网络设置")`，再把 `app.sessionEnd{reason:logoff|shutdown, budget_ms:4000}` 直接交给 worker 放到控制队列最前（不经 WebView，不排在 `app.quit` 后面）。`WM_ENDSESSION(TRUE)` 在预算加 500 ms 内等回复或 `bye`；回复带 `workers_pending:true` 时继续等 `bye`，直到预算用完。外壳已退出且不是本次 sessionEnd 时不等待。`WM_ENDSESSION(FALSE)`（Windows 取消了会话结束）时恢复 20 秒提示。记录 `writes_revoked`、proxy/firewall、`workers_pending`、是否超时，最后 `ShutdownBlockReasonDestroy`。
- **崩溃与卡死**（`app/src/crash.rs`）：`hello` 之前退出时弹原生框，附数据目录 `logs/sidecar-stderr.log` 的最后 20 行（仅显示，先脱敏，不写进 shell.log）和「打开日志目录」，然后退出。运行中意外退出（不是 0、不是 74、不在退出流程里）时给页面发 `host:banner{kind:"sidecar_crashed",state:"restarting"}`，在停止状态下重拉，5 分钟内最多 3 次，新 epoch 照常触发 `rt:reset`，新会话就绪后横幅变为 `restarted`（一次性通知，`active:false`，页面重连不再回放）；超过上限停止重拉，横幅 `unrecoverable`。0 与 74 跟随退出，不重拉。15 秒没有 `pong` 只发 `host:banner{kind:"sidecar_unresponsive",active:true,actions:["force_restart"]}`，不发 `app.quit`、不自动杀；页面 `invoke('host_force_restart')` 才 TerminateProcess 并按意外退出处理。`PBT_APMRESUMEAUTOMATIC` 和循环停顿超过 5 秒都会重新计时。横幅按 kind 缓存，页面重连时回放仍有效的横幅。
- **shell.log**（`app/src/shell_log.rs`）：1 MiB × 3 滚动（`shell.log`、`.1`、`.2`），行格式 `<毫秒> <代码>[ 键=值…]`，值只允许 `[A-Za-z0-9_.-]`，否则写 `invalid`。别的进程以只读共享方式占着文件时，打开失败会隔 10 ms 重试 3 次，不再静默丢行。
- **hello 之前的对话框**：侧车退出与「握手超时 / 握手不符」分两条文案；打开对话框前先释放本作用域的实例租约；TaskDialog 在 `TDN_CREATED` 和计时器里确保可见、置顶、前台（隐藏启动的 STARTUPINFO `SW_HIDE` 会被第一次 ShowWindow 消耗掉）。stderr 尾部的脱敏覆盖 `key: value`、`'key': 'value'`、`Bearer`/`Basic` 凭据、代理分享链接（`vmess://` 等一律 `scheme://***`）、JWT；键名单与 `bridge_vocab.SECRET_RECORDS` 由单测 `secret_keys_cover_bridge_vocab` 保持一致。

### 外壳 ↔ 页面约定（T-P3-04 拟定，待集成单写进 PROTOCOL §13）

只走 Tauri Channel / `invoke`，从不发给侧车。所有 `host:*` 帧都带外壳当前的 `epoch`。

| 方向 | 形状 | 语义 |
|---|---|---|
| 外壳 → 页面 | `host:confirmQuit{token:u64, epoch}` | 用户要退出，问页面有几项未保存的修改。页面应在 3 秒内回复，否则外壳当作 0 继续 |
| 页面 → 外壳 | `invoke('host_confirm_quit', {token, unsaved:u32})` | 回复上一条。token 过期返回 `stale_token`，当前不在询问中返回 `not_confirming` |
| 外壳 → 页面 | `host:banner{kind:"sidecar_crashed", active, state, exit_code:u32\|null, restarts, limit:3, epoch}` | `state`：`restarting`（`active:true`，正在停止状态下重拉）、`restarted`（`active:false`，一次性通知，页面自行淡出）、`unrecoverable`（`active:true`，5 分钟内已重拉 3 次，不再重拉） |
| 外壳 → 页面 | `host:banner{kind:"sidecar_unresponsive", active, actions, epoch}` | `active:true` 时 `actions:["force_restart"]`：15 秒没有 pong；恢复或会话结束时发 `active:false`、`actions:[]` |
| 页面 → 外壳 | `invoke('host_force_restart')` | 只在 `sidecar_unresponsive` 有效时接受，否则返回 `not_unresponsive`。外壳 TerminateProcess 侧车，再按意外退出处理 |

外壳按 `kind` 缓存最新一条横幅；页面重连时，回放末尾补发 `active:true` 的横幅，`active:false` 的不回放。

冒烟（仓库根目录，环境变量要求同 P3-02，脚本不构建）：

```powershell
./scripts/p3_exit_smoke.ps1   # 可用 -Scenarios tray-quit,sidecar-kill,session-end,quit-no-bye,quit-stall-force,prehello 选择场景
```

脚本开头做一次字节码预热：侧车以 `-B` 运行，没有字节码缓存时每次都要重新编译约 2 MB 的 `desktop_runtime.py`，在负载下单这一步就会超过 10 秒的 hello 窗口。预热把字节码写进临时目录下的 `PYTHONPYCACHEPREFIX`（不写源码树），各场景的侧车只读它。hello 之前就失败的场景重试一次；失败场景的收尾依次是：关闭本作用域外壳的对话框、恢复被挂起的侧车、托盘「退出」，最后才结束本作用域的测试外壳，每一步记进 `result.json` 的 `cleanup`。

第 1 轮修复新增三个场景：`quit-no-bye`（挂起侧车、托盘退出、`quit_sent` 后以退出码 3 结束侧车，外壳进程退出码必须是 3）；`quit-stall-force`（挂起侧车、托盘退出，20 秒后可见且置顶的停滞对话框出现，用 `TDM_CLICK_BUTTON` 点「强制退出」，外壳退出码 1）；`prehello`（`--sidecar whoami.exe` 在隐藏启动下让侧车在 hello 前退出，对话框必须可见且置顶，`WM_CLOSE` 后外壳以 1 退出，伪造 stderr 里的秘密不进 shell.log）。外壳退出码同时用 Job 记录的内核退出码核对。

原有三个场景各用独立作用域：托盘「退出」并经开发页回复 `host_confirm_quit`；杀掉侧车后核对停止状态重拉、新 epoch、`rt:reset` 与横幅，再以页面不回复（3 秒超时）退出；由脚本进程向主窗口发 `WM_QUERYENDSESSION` + `WM_ENDSESSION(logoff)`，核对 `app.sessionEnd` 的发送和回复。每个场景都核对外壳退出码等于侧车退出码、无残留进程。证据写到工单目录 `evidence/T-P3-04/exit-<scope>/`。真实注销验收放到 P7。

## T-P3-05 桌面通知、窗口可见性上报、语言切换

外壳在 hello 之后按 `hello.caps.topics` 订阅：P1 的 8 个主题，再加上侧车声明了的外壳专用主题 `alerts.desktop`、`ui.language`（没声明就不订阅，现在的真实侧车仍只声明 P1 主题）。这两个主题的事件在进入页面缓存之前就被拆出来（单条 `evt` 整条拿走，`batch` 只拿走这些条目，其余条目照常进页面），页面永远收不到它们。外壳专用命令 `alerts.desktopDelivered`、`ui.presence` 也只在 `hello.caps.commands` 声明后才发；侧车对它们的回复留在外壳，只记 shell.log。

- **通知路由**（`notify.rs`）：窗口在前台（可见、未最小化、是前台窗口），并且**当前这次页面连接**已调用 `host_notify_ready` 声明能渲染 → 页面内通知 `host:notify`；否则（后台、页面不在线、页面在线但没声明、页面重载后还没再次声明）→ 托盘气泡（T-P3-03 的图标）。`desktop-test-` 开头的投递（集成单加上 `force` 字段后改看该字段）一律走气泡。**10-30 起「否则」改走提醒卡片窗口，气泡只作卡片窗口的兜底，见文末「10-30 提醒卡片」。**
- **气泡在托盘的 UI 线程上提交**：worker 只把通知放进托盘队列（上限 64）并 `PostMessage`，`Shell_NotifyIconW` 只在托盘窗口线程上调用；提交有了结果之后才把回执放进待发队列。Explorer 卡住时不会拖住 worker 的管道读写、ping 和回执。
- **回执**（每个 `delivery_id` 只回执一次）：气泡提交成功 `delivered/windows`；气泡被拒或内容为空 `failed/windows`；页面 3 秒内确认 `delivered/in_app`；托盘或图标不可用、托盘队列已满、页面 3 秒不确认 `dropped/none`。新 epoch 会丢弃上一代未回执的 ID（发给新侧车只会是 conflict）。**侧车不会重投**：没收到回执的运行时提醒在侧车里 TTL 到期后就没了，只有更新提醒会在下次启动时再提供一次（`bridge_alerts_desktop._release_expired`）。回执通道溢出、退出期间跳过的回执同样如此。
- **点击气泡**：先走恢复窗口（与托盘左键同一路径，只是置位恢复事件，窗口异步显示）；`host:navigate{page: target}` 由可见性线程在看到窗口可见之后发出，排在同一次采样的 `host:visibility{true}` 之后。3 秒内窗口仍不可见（恢复失败）也照发，shell.log 记 `tray_balloon_navigate page= visible=false`。
- **气泡的点击目标按代次管理**（`tray::ClickTarget`，T-P3-H）：每次提交气泡（提醒气泡或普通气泡）都在图标锁内取下一个代次，并记下它的目标（普通气泡没有目标）。点击只认最新代次，每个气泡最多导航一次，shell.log 记 `tray_balloon_clicked gen= target=`。`NIN_BALLOONTIMEOUT`/`NIN_BALLOONHIDE` 不带气泡身份，被替换的旧气泡的 HIDE 可能在新气泡提交之后才到，所以不清除目标；普通气泡与提醒气泡在不同线程上交错提交时，也不会把目标配错代次。**限制**：Win10/11 会把旧气泡收进操作中心，用户在那里点一条旧通知时外壳收到的也是同一个 `NIN_BALLOONUSERCLICK`，无法分辨是哪一条，所以会导航到**最新**气泡的目标；如果最新气泡已经被点过（目标已取走）或是普通气泡，则只恢复窗口、不导航。代次目前只用于日志（`gen=`），普通 `show_balloon` 在发布版里没有调用方（只有开发入口 `WM_APP+2`）。
- **presence**（`presence.rs`）：worker 每 100 ms 读一次真实窗口状态（`IsWindowVisible` 且非 `IsIconic`；focused = 前台窗口是主窗口），值变化时发完整的 `ui.presence{visible, focused, page}`，每次 hello 之后重发一次完整值。`page` 只来自 `host_set_page`，只接受白名单 overview、nodes、settings、logs、alerts、about、none，并且只对报告它的那次页面连接有效：页面重载后、再次调用 `host_set_page` 之前报 `none`。shell.log 的 `presence_sent` 只在 visible 或 page 变化时写一行，只有 focused 变化的次数累计在下一行的 `focus_only=` 里。
- **可见性**：独立线程同样每 100 ms 采样，`visible` 变化时向页面发 `host:visibility{visible}`（关到托盘为 false，恢复为 true，最小化也算 false）。
- **语言**：`hello.lang` 和后续 `ui.language` 都更新外壳原生文案（托盘菜单、提示、对话框都读同一个 locale）；值有变化时才发 `host:language{language}`（启动时 hello.lang 与首个 `ui.language` snapshot 相同，页面只收到一次）。hello 之前沿用 §7.10 的系统语言。
- **外壳主题的序号**：`alerts.desktop`、`ui.language` 的 edge seq 必须递增；跳号照常接受，记 `shell_topic_seq_skip topic= expected= got=`；gap 按 hub 的规则校验 `from_seq`/`to_seq`/`dropped`，不合格记 `shell_topic_rejected code=sequence_gap`。外壳主题不会触发会话重建。batch 里的外壳条目先和整批一起核对 epoch 与时间戳，任何一条不合格整批拒绝（与 hub 的原子规则一致）。
- **退出进度**：`alerts.desktop` 和采样主题一样不算 app.quit 的进度，停止卡住时持续到来的提醒不会推迟 20 秒的停滞提示。
- **注销快路径修正**（裁定 P3-04 第 2 条）：已发 `app.quit`、还没收到 `bye` 时遇到 `WM_QUERYENDSESSION`，照样发 `app.sessionEnd`（排在控制队列最前，worker 尚未取走的 `app.quit` 作废）。只有已收到 `bye`、或处于更新退出流程时才直接放行。

### 外壳 ↔ 页面约定（T-P3-05 新增，待集成单写进 PROTOCOL §13）

| 方向 | 形状 | 语义 |
|---|---|---|
| 页面 → 外壳 | `invoke('host_notify_ready')` | 本次页面连接能渲染 `host:notify` 并会用 `host_notify_rendered` 确认。每次连接（含重载）都要调用一次；没调用的连接，前台通知一律走气泡。页面未连接时返回 `page_not_connected` |
| 外壳 → 页面 | `host:notify{delivery_id, severity, title, detail, target, epoch}` | 页面内通知。最多同时显示 5 条、超出合并、窗口隐藏时暂停计时，都由页面实现。**`title`、`detail` 是不可信文本**（来自提醒规则和节点名等），只能当纯文本渲染（`textContent`），不得用 `innerHTML` 或当作 HTML/Markdown 解析，与 PROTOCOL §5.5 对日志文本的要求一致 |
| 页面 → 外壳 | `invoke('host_notify_rendered', {delivery_id})` | 页面已经渲染出来。只接受外壳交给**同一次页面连接**、仍在 3 秒等待中的 ID；未知返回 `unknown_delivery`，已回执返回 `already_receipted`，页面重连过返回 `stale_page` |
| 外壳 → 页面 | `host:navigate{page, epoch}` | 用户点了通知气泡；`page` 是 `alerts.desktop.target`（overview、alerts、update）。顺序是「先请求恢复窗口，看到窗口可见（并发出 `host:visibility{true}`）之后再导航」；恢复失败时 3 秒后照发，所以 navigate **不保证**窗口可见 |
| 页面 → 外壳 | `invoke('host_set_page', {page})` | 页面当前路由页，白名单之外返回 `invalid_page` 且不改值。只对本次页面连接有效，重载后要再报一次 |
| 外壳 → 页面 | `host:language{language, epoch}` | 九语言之一；只在值变化时发，`rt:reset` 不清除它（页面保留上一个值）；外壳记住最新一条，页面重连时在回放末尾补发 |
| 外壳 → 页面 | `host:visibility{visible, epoch}` | 窗口可见性；同样只在变化时发、记住最新一条、重连时补发 |
| 页面 → 外壳 | `invoke('host_window', {action})` | 11-05 页面内标题栏的窗口按钮：`minimize`、`toggle_maximize`、`close`（其他值返回 `invalid_args`）。`close` 等同系统关闭（Alt+F4），照常经 `CloseRequested` 决定收进托盘还是退出 |
| 外壳 → 页面 | `host:window{maximized, epoch}` | 11-05：主窗口是否最大化（决定页面显示「最大化」还是「向下还原」）；尺寸变化且未最小化时发，只在变化时发、记住最新一条、重连时补发 |

**接线顺序（集成单与 P4 必须遵守）**：外壳已经用 `host_notify_ready` 兜住了「页面还不会渲染通知」的情况（这时走气泡），但侧车在 `hello.caps` 里声明 `alerts.desktop` 之前，P4 页面应当已经实现 `host:notify` 的渲染、`host_notify_rendered` 的确认和连接后的 `host_notify_ready`；只实现了一半（声明了 ready 却不确认）会让前台通知全部在 3 秒后变成 `dropped/none`，而侧车不会重投运行时提醒。

页面不能自己发回执：`rt` 的 `alerts.desktopDelivered` 返回 `not_allowed`；也不能声称窗口在前台：visible、focused 只来自外壳的窗口探测。

冒烟（环境变量要求同 P3-02，脚本不构建）：

```powershell
./scripts/p3_notify_smoke.ps1
```

debug 外壳以 `--dev-sidecar-script shell/app/src/notify_fixture.py` 运行侧车替身（release 构建拒绝这个参数）。替身在 hello 里声明上述主题和命令，记录收到的每一帧，并把脚本放进 `<data>/notify-fixture/push/` 的 JSON 逐个发出。脚本核对：hello 语言与后续 `ui.language`、presence 的首发和页面 ID 白名单、关到托盘后的 `host:visibility` 和 presence、隐藏时走气泡并回执 `delivered/windows`、模拟点击气泡后恢复窗口、`host:navigate` 排在 `host:visibility{true}` 之后、前台但页面没调用 `host_notify_ready` 时仍走气泡、调用之后页面内通知与 `delivered/in_app`、`host:language zh_CN` 只到达页面一次、页面沉默 3 秒后 `dropped/none`、强制测试走气泡、重复 ID 不再回执、页面伪造回执被拒，最后托盘退出、无残留进程。如果 Windows 不把前台交给测试窗口，脚本改为核对路由与探测到的状态一致，并在 result.json 记 `foreground=false`。「页面沉默」一步（T-P3-H 修复 1）发出前先把窗口拉回前台并等 presence `focused=true`；如果它的 `notify_route` 仍是 `focused=false`（别的窗口中途抢走了前台，气泡是正确结果），换一个投递 ID 重试一次，再失败就报「environment: the foreground was taken」，不报成合同错误；每次尝试记在 `checks.silent_tries`。证据写到工单目录 `evidence/T-P3-05/notify-<scope>/`。

## T-P3-06 host 命令、另存为授权、更新交接、开机自启参数

- **host 命令**（`app/src/host.rs`，Win32 效果在 `hostwin.rs`）：页面照常调 `invoke('rt', {req_id, cmd, args, owner})`，`cmd` 为 `host.saveFile` / `host.openFolder` / `host.openUrl` / `host.reveal` 时只在外壳执行、从不转给侧车；`rt` 先回 `{queued:true,id}`，结果是外壳生成的 `res{id,req_id,owner,cmd,ok,data|error}`，经同一 Channel 到达（带当前 epoch）。`owner` 可省（PROTOCOL §6 的 `owner?`），省略时回复里也没有 `owner`；只有结果绑定 owner 的 `saveFile`、`reveal` 必须带（否则 `missing_field`）。同时最多 8 个在途；原生框（另存为、openUrl 确认）同一时刻只有一个，另一个请求回 `conflict`（reason `dialog_open`）。错误 `message_key` 为 `errors.host.<code>`，另带 `reason_code`。
  - `host.openFolder{kind}`：只接受 `config`、`data`、`logs`（作用域 `data/logs`，D17）、`exports`（作用域 `data/exports`，不存在就建；不跟随用户另存到的目录，见报告「需要 Claude 裁定」）；其他值、多余字段一律 `not_allowed`。用 ShellExecute 打开。
  - `host.openUrl{url}`：https、无用户信息、默认端口，主机名精确属于 PROTOCOL §13 第 5 条的常量表 `github.com`、`docs.github.com`、`t.me`、`ntfy.sh`、`pushover.net`；其他一律 `not_allowed`，不弹框。通过后弹原生确认「将在浏览器中打开 <主机名>」（主窗口为 owner，只显示主机名，默认按钮是「取消」），确认才交给系统浏览器；`{opened:false}` 表示用户取消。shell.log 只记主机名。
  - `host.saveFile{purpose,format}`：bundle 只配 zip，filtered 只配 json/text/csv。IFileSaveDialog（覆盖确认、严格扩展名、只限文件系统），默认文件名 `tesseract-diagnostics-YYYYmmdd-HHMMSS.zip` / `tesseract-logs-YYYYmmdd-HHMMSS.<json|txt|csv>`，诊断包默认目录为桌面，日志导出为作用域 `logs`。取消 → `{cancelled:true,ref:null}`，不建文件、不登记。确认 → 外壳以 `GENERIC_WRITE|DELETE` 打开（OPEN_ALWAYS，**不截断**，只接受磁盘文件；写入方 = 侧车适配器从偏移 0 写、提交时 `SetEndOfFile` 截断），`DuplicateHandle` 复制到**当前侧车进程**（核对 pid），发仅壳命令 `shell.saveGrant{ref, purpose, expires_at_ms(+5 分钟), format, handle_id}`，`handle_id` 是句柄在侧车进程内的值，请求信封的 `owner` 为页面 owner（裁定 P2-19 第 2 条，`for_owner` 上线前）；`registered:true` 且会话 epoch 仍未变（变了 → `runtime_gone/epoch_changed`）之后才把不透明 `ref` 交给页面。任何失败（拒绝、未发出、10 秒超时、epoch 已变）：本次新建的文件经外壳自己的句柄设为关闭即删（侧车若已持有迟到登记的副本，副本关闭时一并删除），原有文件原样保留；超时记 `host_save_grant_timeout`，迟到的登记由侧车按过期释放。外壳自己的句柄在登记后关闭。页面永远拿不到路径。
  - `host.reveal{ref}`：只接受本 epoch、同一 owner 登记过、并且已经有 `logs.exportBundle/exportFiltered` 的 `done` 作业结果点名该 `ref` 的引用；用 `explorer /select,"<路径>"` 定位。其他一律 `not_allowed`。
- **更新交接**（`app/src/update.rs`，PROTOCOL §11/§13，裁定 P2-27）：侧车在 `hello.caps` 声明 `update.prepareExit` 时才订阅；该主题和 `alerts.desktop` 一样在进页面之前拆出。收到后核对：未过期、当前没有别的退出流程、没有别的交接、`installer_path` 为数据目录内的盘符绝对路径且文件名为 `Tesseract-Setup-<version>-win-x64.exe`、以拒绝写入方式打开后最终路径仍在数据目录内、SHA-256 相等、Authenticode 签名没有损坏（未签名按项目策略接受）。回 `update.exitResponse{handoff_id,accepted,reason_code?}`；收到同一 `handoff_id` 的 `{recorded:true}` 后外壳自己发 `app.quit{reason:"update"}`。30 秒（或到 `expires_at_ms`）没有答复 → 放弃该交接、记 `update_exit_response_timeout`，迟到的答复记 `update_exit_response_late`、不再触发退出。侧车退出后状态机进入 `Installing`（不退出）：要求侧车进程已结束、`bye` 退出码 0、proxy/firewall 为 `not_needed|restored|not_owned`；再自检残留进程（EnumProcesses + QueryFullProcessImageName；根目录：release 为外壳自身目录 ∪ `--sidecar` 的目录（无 `--sidecar` 时 ∪ 仓库 `bin`），debug 为 `--sidecar` 的目录或仓库 `bin`，debug 构建可用 `TESSERACT_SHELL_DEV_INSTALL_ROOT` 指定；任一根目录解析失败 → `residual_roots_unknown`，进程枚举失败 → `residual_unknown`，都按失败处理、不放行；最多等 10 秒）；最后以拒绝写入方式重开安装器、重算 SHA-256，持有该句柄直到 `Command::new(installer_path).args(args)` 返回。成功 → 外壳以 0 退出（不发 `installResult{true}`）。任一步失败 → 回到 Running（关窗重新进托盘）、清除 quitting 标记、恢复主窗口、在停止状态下重拉侧车（worker 只看 `Lifecycle::sidecar_exited` 返回的 `Exited::Installing` 和安装结束后的 `finished()`，安装线程先于 worker 结束也照样重拉），新 epoch 握手后发 `update.installResult{receipt_ref,launched:false,reason_code}`。shell.log 只记 handoff_id、版本和结果码。
- **`--autostart`**（D8）：带此参数时窗口先不显示；hello 之后外壳自己发查询 `config.snapshot`（from=shell），读 `values.ui.start_in_tray_on_autostart`。为 true 且托盘图标在 → 只留托盘；false、缺失、5 秒内无答复、侧车没声明该查询、托盘不可用 → 照常显示窗口。shell.log 记 `autostart_start setting=<true|false|unknown> window=<tray|shown>`。

冒烟（环境变量要求同 P3-02，脚本不构建）：

```powershell
./scripts/p3_host_smoke.ps1   # 可用 -Scenarios main,install-failure,autostart-tray,autostart-window 选择
```

debug 外壳以 `--dev-sidecar-script shell/app/src/host_fixture.py` 运行侧车替身：声明 `update.prepareExit`、`shell.saveGrant`、`update.exitResponse`、`update.installResult`、`config.snapshot`；收到 `shell.saveGrant` 时核对 `handle_id` 是本进程内的磁盘文件句柄、解析其路径并经它写入固定内容。脚本用 csc 现编一个「假安装器」（写标记文件），核对：openFolder 的 config、data、logs、exports 四种恰好打开作用域内的对应目录（检查并关闭 Explorer 窗口）、越界一律 `not_allowed`；openUrl 白名单外不弹框，白名单内弹框并关闭为 `opened:false`；另存为对话框先取消（无登记）、再输入路径确认（登记成功、侧车经复制的句柄写入、页面只有 ref）；reveal 在导出结果之前拒绝、之后定位；哈希不符的交接被拒且外壳继续运行，哈希相符的交接被接受、`app.quit{reason:update}`、假安装器带参数启动、外壳以 0 退出；安装根目录下留一个进程时安装失败、窗口从托盘恢复、侧车停止状态重拉并收到 `installResult{launched:false,reason_code:residual_processes}`；`--autostart` 两种设置。证据写到工单目录 `evidence/T-P3-06/host-<run>/`。

## T-P3-07 隐身后显示、WebView2 故障处理、发布构建的环境清理

代码在 `app/src/webview.rs`（纯逻辑与 Win32/WebView2 调用），接线在 `main.rs`、`window.rs`。

- **启动顺序**（方案 §2.2.1）：`run()` 一开始先清环境变量（见上文 CDP 一段）并设 AUMID `Tesseract.ProxyApp`，记 `webview_env_scrubbed name=`（每个被删的变量一行）和 `aumid_set ok=`；取得单实例之后用 `GetAvailableCoreWebView2BrowserVersionString` 检查运行时（`webview_runtime version=`）。没有运行时：记 `webview_runtime_missing hresult=`，先释放实例租约，再弹原生框（Windows UI 语言，§7.10），两个按钮「退出」（默认）/「打开下载页」，脚注写出链接；选「打开下载页」时链接只经 `host::url_args`（与 `host.openUrl` 同一白名单）后交给系统浏览器。随后以 1 退出，不启动侧车。链接暂为项目发布页 `https://github.com/definitlynotme/Tessract/releases/latest`（白名单里没有微软的主机，见报告裁定项）。开发构建可用 `TESSERACT_SHELL_DEV_WEBVIEW2_MISSING=1` 模拟没有运行时。
- **没有系统标题栏**（11-05）：主窗口 `decorations(false).shadow(true)`，并请求 `DWMWA_WINDOW_CORNER_PREFERENCE=DWMWCP_ROUND`：保留 DWM 阴影、Win11 圆角和窗外的缩放边（tao 的无边框阴影模式；顶边缩放由 Tauri 的 `TAURI_DRAG_RESIZE_BORDERS` 子窗口处理）。标题栏由页面画：标题带标 CSS `app-region: drag`，wry 打开了 WebView2 的非客户区支持，WebView2 对这片区域回 `HTCAPTION`，拖动、双击最大化、Aero Snap、右键系统菜单都是系统原生行为（11-05 实测：标题带空白处、页面标题、字标处 `WM_NCHITTEST`=2，按钮、品牌标、导航、页面=1）。窗口按钮经 `host_window` 调用；`host:window{maximized}` 由 `on_window_event` 的 `Resized` 发出（最小化时不发）。
- **隐身后显示**：主窗口建好后先 `DwmSetWindowAttribute(DWMWA_CLOAK)`，再按原逻辑 `show()`。窗口对 Win32 和 WebView2 是可见的（照常渲染、`requestAnimationFrame` 照常跑），只是 DWM 不显示。页面在 DOM 已按真实状态更新后调用 `invoke('host_ready')`，外壳取消隐身，记 `host_ready` 和 `window_revealed reason=ready waited_ms=`；1.5 秒内没收到也取消隐身，记 `window_reveal_timeout waited_ms=`。每个窗口只揭开一次，页面重载后再调 `host_ready` 不起作用。隐身期间（只认本进程的 DWM_CLOAKED_APP）presence 探针返回「无窗口」：不发 `ui.presence`、不发 `host:visibility`，前台通知走托盘气泡；揭开后的第一个值就是真实值。被 Shell 隐身的窗口（例如在其他虚拟桌面上）仍按 T-P3-05 的规则算不可见。以 `show=false` 建的窗口（`--autostart` 进托盘、在托盘里重建）创建时就把 WebView2 控制器设为 `IsVisible=false`，页面在托盘里不跑满帧 rAF；之后显示窗口时由 WM_SHOWWINDOW 设回。开发构建可用 `TESSERACT_SHELL_DEV_READY_TIMEOUT_MS`（100–60000）延长超时，供冒烟观察隐身阶段；发布构建忽略它。
- **渲染进程崩溃 / 无响应**（`ProcessFailed` 的 RenderProcessExited / RenderProcessUnresponsive）：`Reload()`，新页面照常 `rt_connect`，外壳从缓存回放同一 epoch 的状态；每分钟最多 3 次，超过只记 `webview_reload_limited`。shell.log：`webview_process_failed kind= remedy=`、`webview_reload kind= ok=`。子框架渲染进程、GPU、工具进程只记日志（WebView2 自己会重启它们）。
- **浏览器进程崩溃**（BrowserProcessExited 类）：在独立线程里销毁失效的主窗口，按原样重建（原来在托盘里的仍留在托盘，新窗口同样先隐身），重新挂上注销钩子、presence 和 host 的 owner 窗口；从开始重建到新窗口挂好之前，presence 探针和 host 的 owner 窗口都置空（不报告 presence，host 对话框无 owner）。每次失败的尝试若已建出窗口，先销毁并等 Tauri 注销 `main` 标签（最多 5 秒，超时记 `webview_label_still_registered`）再重试。第一次立即创建，失败后隔 0.5 s、1 s、2 s 再试，共 4 次（`webview_create_failed phase= attempt=`，成功记 `webview_recreated attempt=`）。重建期间没有窗口，Tauri 发出的「无窗口退出」请求被拦下（`exit_prevented_recreating`）。60 秒内最多重建 3 次，第 4 次浏览器丢失记 `webview_recreate_limited limit=3 window_s=60`，销毁窗口后直接按「全部失败」处理。外壳正在退出（`app.quit` 已发出、非更新退出）或 Windows 正在结束会话时不重建、不弹框，只记 `webview_recreate_skipped_quitting`。4 次都失败：`webview_recreate_exhausted`，把注销钩子挂到托盘的隐藏顶层窗口上（`session_end_hook_on_tray`，弹框期间注销照常处理），弹只有「退出」一个按钮的原生框，关闭后走正常退出流程（`QuitReason::WebViewLost`，线上 `app.quit{reason:"user"}`，先恢复网络再退出）。启动时创建主窗口也用同一套退避，但它在 `setup`（UI 线程）里执行，事件循环还没跑，Tauri 不会注销已销毁窗口的标签：启动时「build 成功、之后失败」的情况，后续重试会因标签仍被占用而失败（已知限制）。开发构建可用 `TESSERACT_SHELL_DEV_RECREATE_FAIL=1` 让每次重建在建窗口之前就失败，`=after_build` 让每次重建先真的建出窗口再失败，供冒烟走完这两条路径。
- **退出时等浏览器进程**（§7.9）：事件循环结束、托盘移除后，外壳关闭 WebView2 控制器，再在 UI 线程泵消息等当前环境的 `BrowserProcessExited`，上限 12 秒（本机实测关闭控制器后浏览器进程 0.2 秒到 20 秒以上才退出，约一半超过 3 秒，个别超过 12 秒，见报告）。记 `webview_browser_exit_wait result=exited|timeout|none ms=`。注销/关机时不等（`result=skipped_session_end`）；更新交接退出时也不等（`result=skipped_update`，安装器已在运行，可能要替换外壳文件）。开发构建可用 `TESSERACT_SHELL_DEV_EXIT_WAIT_IGNORE_EXITED=1` 让等待忽略 `BrowserProcessExited`、一直等到上限，供冒烟验证 `timeout` 分支。等待期间窗口已隐藏，实例的 quitting 标记已置位，第二次启动会等本进程退出后再创建。
- **浏览器启动参数**：自定义 `additional_browser_args` 时补回 wry 0.55.1 的默认值 `--disable-features=msWebOOUI,msPdfOOUI,msSmartScreenProtection`（`window::DEFAULT_ARGS`，T-P3-01 起已如此）。

### 外壳 ↔ 页面约定（T-P3-07 新增，待集成单写进 PROTOCOL §13）

- `invoke('host_ready')`：页面在首个 `service.state` 快照写入、当前页渲染完成之后调用一次；无参数，返回 `null`。外壳收到后取消主窗口的 DWM 隐身。迟到（1.5 秒超时之后）或重复调用都无害。
- 渲染进程重建或浏览器进程重建后，页面是全新加载，照常 `rt_connect` 取回放。

冒烟（仓库根目录，脚本默认先构建，已构建加 `-SkipBuild`）：

```powershell
./scripts/p3_shell_smoke.ps1 -SkipBuild                       # main、webview、missing、lost、lostbuilt 五个场景
./scripts/p3_shell_smoke.ps1 -SkipBuild -Scenarios main -ForceExitWaitTimeout   # main 走退出等待的 timeout 分支
./scripts/p3_shell_smoke.ps1 -SkipBuild -Scenarios webview,lost -EvidenceDir <目录>
```

- `main`：原有冒烟，另核对 AUMID、运行时版本、dev 页面不发 `host_ready` 时 1.5 秒超时揭开、退出时的等待有上限且记日志（等到 `BrowserProcessExited`，或 12 秒超时）。外壳退出最多等 30 秒（含 12 秒退出等待）；`-ForceExitWaitTimeout` 时只接受 `result=timeout`。超时的毫秒数按数值判断，12000–15000 都算（负载高时等待循环会因消息分发略超 12 秒，T-P3-H）。
- `webview`：DWMWA_CLOAKED 断言隐身期间不可见（Win32 仍可见）、`host_ready` 之后才可见；V2 的 rAF 测量（隐身、显示、`SetWindowPos` 隐藏、`ShowWindow(SW_HIDE)` 隐藏）；开发构建 CDP `Page.crash` 让渲染进程崩溃 → 重载、同 epoch 回放、侧车不受影响；杀掉本作用域的浏览器进程 → 窗口重建、重新隐身、同 epoch 回放；退出时的等待同上、无残留。
- `missing`：模拟没有运行时 → 原生框可见且置顶、不建窗口、不起侧车，关闭后以 1 退出、不打开浏览器。置顶检查最多等 3 秒（TDN_CREATED 置顶、200 ms 的 TDN_TIMER 补一次），结果旁记 `dialog_topmost_wait_ms`、`foreground_is_dialog`、`foreground_lock_timeout_ms`（T-P3-H）；`lost`/`lostbuilt` 同样。
- `lost`：每次重建都失败 → 4 次尝试的间隔约 0.5/1/2 秒、Tauri 的无窗口退出被拦下（`exit_prevented_recreating`）、注销钩子挂到托盘窗口、只有「退出」的原生框、关闭后 `app.quit` → `bye` → 侧车退出码 0、外壳以 0 退出、无残留。
- `lostbuilt`：同上，但每次尝试先真的建出窗口再失败：4 次都建出了窗口（`dev_recreate_fail_after_build`），说明每次重试前 `main` 标签都已释放；间隔另含建窗口的时间。

残留检查给本作用域的 `msedgewebview2` 进程 20 秒宽限（决定_P3-06 第 6 条）；辨认方式与 `process_watch.py` 相同（`--user-data-dir` 在本作用域的 `WebView2` 目录下），见 T-P3-01 一节的残留规则。

每个场景用 `%TEMP%` 下新建的 custom 作用域；`-EvidenceDir` 把 `result.json` 和 `shell.log` 复制过去。

## T-P4-C3 外壳配套：能力声明、回放结束标记、重取、H 白名单、托盘状态图标、减少动效、CSP

页面合同的形状与语义以 PROTOCOL §13 末尾的五条为准，这里只记外壳实现。

- **`host:caps` 与 `rt:replayed`**（`app/src/transport.rs`）：每次 `rt_connect` 的回放是 `rt:reset` → `host:caps` → `hello` → 各主题完整快照 → 作业 → 日志尾 → 有效横幅 → `host:language`/`host:visibility` → `rt:replayed`。`host:caps.invokes` 是常量 `HOST_INVOKES`，`main.rs` 的单测核对它与 debug、release 两张 invoke 表一致。新 epoch 的 seed 由 `Hub::reset` 开始：等 `hello.caps.topics` 里每个有完整基线的槽收到第一帧（或改走重取），最多 15 秒（`SEED_GRACE`），然后发 `rt:replayed`；工作线程每轮调用 `seed_tick`，并在 shell.log 记 `rt_seed_done reason=all|deadline slots missing first_missing ms`。真实侧车 29 槽在开发机上约 7 秒收齐。
- **`rt_resync{topic,key?}`**（`Hub::resync`，invoke 在 `main.rs`）：缓存是当前的就再投递一次（`{source:"cache"}`；状态主题的缓存若来自 `edge`，投递的副本改成 `kind:"snapshot"`、去掉 `seq`，缓存不变）；正在重取或分页组装中答 `pending`；否则由外壳发 `snapshot_req`（`request`），回来的快照经 `state_snapshot_res`（状态主题）或原有列表槽逻辑投递给页面。同槽 1 秒一次，超出 `rate_limited`。状态主题的缓存从不回退到更旧的 rev（防御：侧车给替代快照分配新的 wire rev，流上之后的帧总是更新）。
- **H 白名单**（`app/src/hgate.rs`，`build.rs`）：`build.rs` 读 `ui/build-manifest.json` 的 `h_features`（编号格式 `B-数字[小写字母]`，不合格就构建失败；没有清单的调试原型视为空），写成 `TESSERACT_H_FEATURES` 编进外壳；`H_COMMANDS` 是命令与编号的对应表，v1 为空。
- **D20 托盘状态图标**（`app/src/tray.rs`）：托盘图标按已接受的 `service.state` 推出的 `run`（unknown / stopped / starting / running / failed，`stopping` 显示为 stopped）换图，`killswitch.state` 为 disabled / blocked / error 时加琥珀色角标（与 `project.js` 的 killswitchOf 同一规则）；离线、陈旧（显式或 3 秒静默）、新 epoch 显示 unknown 且不带角标。提示文字不变。托盘只吃 hub 缓存实际接受的事实（`Hub::take_facts`：流上的帧、gap，以及 `rt_resync` 换回的替代快照；缓存拒收的较旧 rev 不算），由 `main.rs` 的 `accept_frame` 和 `snapshot_res` 分支交给 `Feed::apply`。图标由 `python .build/claude_brand_assets.py --tray shell/app/icons/tray [--preview <png>]` 生成（10 个 ICO，16–64 px 的 PNG 条目），编进外壳（`include_bytes!`），按托盘窗口当前 DPI 的小图标尺寸（`GetSystemMetricsForDpi(SM_CXSMICON, GetDpiForWindow)`，运行时取函数；取不到时用 `GetSystemMetrics`）选条目；`WM_DPICHANGED`、`WM_SETTINGCHANGE`、`WM_DISPLAYCHANGE`、`TaskbarCreated` 时尺寸变了就整组重载、重发当前图标、`DestroyIcon` 释放旧组，并记 `tray_icon_rescaled size=<px>`。某个图加载失败就用应用图标并记 `tray_status_icon_failed`。每次换图在 shell.log 记 `tray_icon_state icon=<run>[-ks]`。
- **减少动效**（`app/src/window.rs`）：只读 `TESSERACT_REDUCED_MOTION`，初始化脚本在输入防护之后注入 `window.__TESSERACT_MOTION__=Object.freeze({reduced})`，shell.log 记 `reduced_motion reduced=<bool>`。
- **CSP**：`tauri.conf.json` 的开发 CSP 去掉了 Google 字体两个源（字体已自托管）；`assets.rs` 的 `release_csp` 现在原样返回，留给 T-P4-08b 去掉 `style-src 'unsafe-inline'`。单测断言开发和 release CSP 都没有任何外部源。
- **语言映射向量**：`native_text.rs` 的单测读取 `bridge_schema/fixtures/lang_map_vectors.json`（T-P4-C1 新建）断言 LANGID 映射一致；文件不存在时打印 `SKIPPED` 并跳过（cargo 汇总仍计为 passed）；设 `TESSERACT_REQUIRE_LANG_VECTORS=1` 时文件缺失直接失败（集成与 CI 用）。读到文件时每个条目都必须被识别（结果字段取 `expected|locale|language|lang` 中第一个出现的，且必须是九种语言之一），否则失败，并打印 `CHECKED … <n> vectors`。

### 冒烟（`scripts/p3_shell_smoke.ps1`）

- `webview` 场景另核对 dev 页收到的回放以 `rt:reset, host:caps, hello` 开头、`host:caps` 含 `rt_resync` 与 `rt:replayed`、`rt:replayed` 在 `hello` 之后，并记录真实侧车的 `rt_seed_done` 行。
- 新场景 `c3`：调试外壳以 `--dev-sidecar-script shell/app/src/notify_fixture.py` 运行侧车替身，加载构建好的应用页（`index.html`，`T.rt`），`TESSERACT_REDUCED_MOTION=1`。核对：`T.rt.hostHas('rt_resync')`、`host:caps.events` 含 `rt:replayed`；`__TESSERACT_MOTION__` 为冻结的 `{reduced:true}`；会话里第一次实时 `tier.transition` 投影为 `instant:false` 并发出 `rt:tier{kind:'fallback'}`（02b 报告 §9 第 5 条的场景），页面重载后的第一次也是实时帧（应用包含 02b 规则时——按页面实际加载的脚本探测，记入 `checks.tier_rule`——或带 `-RequireTierRecover` 时，还要求恰好一条 `rt:tier{kind:'recover',instant:false}`）；`rt_resync` 依次答 `{source:"cache"}`、`rate_limited`，`jobs` 答 `not_allowed`；推送 `service.state`/`killswitch.state` 后 shell.log 依次出现 `tray_icon_state` 的 stopped、starting、running、running-ks、failed-ks、failed；托盘「退出」以 0 退出、无残留。

## T-P3-H2 外壳小修（rt:stale、CDP 崩溃注入、测试台偶发失败）

- **列表槽的陈旧通知**（`app/src/transport.rs`，31b-5）：外壳在两个时刻向页面发自生成的 `rt:stale{epoch,topic,key,reason}`（`key` 在非 keyed 主题上为 `null`，不带 `seq`/`rev`，不是侧车 gap）。`reason=resync`：补丁失配、`gap` 或 `snapshot_required` 之后，外壳发出第一条替代快照请求时（`Hub::take_requests`），一次重取过程只发一次；`reason=exhausted`：同一槽 3 次取不到时（上限不变，不做定时重试），同一状态只发一次。页面自己的 `rt_resync` 把计数清零，不发 `resync`；再次用尽时照发 `exhausted`。新的完整快照（重取成功、流上的快照、状态主题的新值）清除陈旧状态，新 epoch 全部清空；仍陈旧的槽在重连回放里（各主题快照之后、`rt:replayed` 之前）再发一次。`rt:stale` 已登记进 `host:caps.events`（11 项）。shell.log 记 `rt_stale topic= key= reason=`。PROTOCOL §13 有对应条目。页面的处理（「数据可能过时」与「重新加载」按钮）在 T-P4-06。
- **webview 场景的渲染进程崩溃**（`scripts/p3_shell_smoke.ps1` 的 `Invoke-RendererCrash`，31b-17，只改脚本）：实验（`evidence/T-P3-H2/crash-lab/`，侧车替身 + dev 页，每次新外壳）表明 `Page.crash` 每次都生效，但渲染进程随后停在崩溃处理（写 minidump）里，CPU 受限时 15–41 秒后才退出，外壳在进程退出后约 0.1 秒内就记 `render_exited`；原脚本 8 + 10 秒的窗口不够，重发对正在崩溃的进程也没有用。现在：先确认目标（`/json/list` 里恰好一个 dev-rt 页面 target，记下 id）；`Page.crash` 不再盲发，读回结果（`Inspector.targetCrashed`、`detached`、连接被关、拒绝答复或 3 秒无答复）；8 秒内没有 `render_exited` 时，在 60 秒预算内反复核对渲染进程：页面在同一 target 上仍能答 `1+1` → 用新会话对同一 target 重发一次（只一次）；进程在但不答 → 崩溃处理进行中，继续等；进程已没但外壳 5 秒内没记 `render_exited` → 直接失败（外壳问题，不掩盖）。预算用完仍未生效，才结束本作用域唯一的渲染进程（WebView2 同样报 `render_exited`）。每次尝试（方法、结果、等待时长、存活的渲染进程）、渲染进程消失与外壳记录的时刻（`renderer_gone_after_crash_ms`、`render_exited_after_crash_ms`）记在 `result.json` 的 `checks.page_crash`，生效的方法记在 `checks.page_crash_method`。
- **残留分类**（C3-8）：`process_watch.py` 判失败后先写一次结果（含 `residue_tree`：pid、父进程、WebView2 的 `--type`、父进程是否外壳/是否还在），再只观察（不杀）20 秒（`--residue-observe`），重写 `residue_class`：`shell_residue`（留下了非 WebView2 成员、别的 profile 的 WebView2 或身份不明成员，即外壳自己的进程树）、`webview2_slow_exit`（只剩本作用域 WebView2，观察期内自行退出，即 WebView2 运行时自身慢退）、`webview2_stuck`（观察期后仍在）。脚本侧（`scripts/p3_residue.ps1` 的 `Save-ResidueTree`，shell/tray/exit 冒烟共用）在「scoped WebView processes remain」时写 `residue-tree.json`：剩余进程树、外壳退出码与 shell.log 的 `webview_browser_exit_wait`/`shell_exit_` 行，以及观察期内各进程的退出时刻；外壳没退出或没记退出等待时判 `shell_residue`。两种情形冒烟都仍判失败，失败信息带分类。`--grace-selftest` 新增「观察期后仍在」与「观察期内自退」两种分类的实测；替身子进程改为睡到 root 启动它时定下的绝对时刻（原来按子进程自己启动后计时，机器忙时 7 个子进程启动滞后约 1 秒，偶发超出 3 秒宽限）。
- **单实例测试台的 PermissionError(13)**（`app/src/p3_instance_smoke.py`）：测试台轮询 shell.log、读 process_watch 的 JSON 时遇到共享冲突（`open()` 报 errno 13）改为短暂重试（50 ms × 40 次），每次都记进该用例 `result.json` 的 `transient_reads`（文件、第几次、调用处，以及经 Restart Manager 查到的占用进程 pid/名称）；失败时另记 `error_where`（出错的测试台帧 file:line）。自测：`python -B shell/app/src/p3_instance_smoke.py --read-selftest <目录>`（一个无共享打开 0.3 秒的文件，读成功且占用者是本进程）。

```powershell
python -B shell/app/process_watch.py --grace-selftest .build/新界面迁移_工单/evidence/T-P3-H2/process-watch-selftest
python -B shell/app/process_watch.py --counterexample .build/新界面迁移_工单/evidence/T-P3-H2/process-watch-counterexample
python -B shell/app/src/p3_instance_smoke.py --read-selftest .build/新界面迁移_工单/evidence/T-P3-H2/instance-read-selftest
```

修复第 1 轮（评审意见）：
- `process_watch.py` 按**判失败那一刻**的身份不明成员数分类（观察期结束时的值另记 `unidentified_after_observe`）：身份不明成员触发失败、又在观察期内退出，仍判 `shell_residue`；没有任何已识别残留时也判 `shell_residue`。`--grace-selftest` 新增 `unidentified-leaves` 实测。
- 各冒烟等 process_watch 退出的时间由 30 秒改为 `$InventoryWaitMs`（55 秒＝30 + 观察 20 + 余量，定义在 `scripts/p3_residue.ps1`，host/notify 冒烟也点源它）；仍超时时用 `Inventory-TimeoutMessage` 读已写出的清单，有 `residue_class` 就按残留报错。main 场景失败时也写 `result.json` 并把证据复制到 `-EvidenceDir`。
- `Invoke-RendererCrash` 的兜底「结束渲染进程」保留（重载路径照样测到），但用到它时 webview 场景在末尾**判失败**（`checks.page_crash_fallback`）；`page_crash_resent` 只在真的又发了一次（`page_crash_fresh_session`/`renderer_terminate`）时为 true；`renderer_gone_after_crash_ms` 在等待循环退出后补测。`p3_smoke_rounds.ps1` 的 smoke-runs.txt 每行追加 `page_crash_method=`（兜底时另加 `PAGE_CRASH_FALLBACK`）与证据里的 `residue_class=`。
- 外壳 hub：`snapshot_required` 的 `params.tier` 不合法（keyed 主题不是四个层级之一，非 keyed 主题带了 tier）时整帧丢弃，记 `snapshot_required_invalid_slot`；状态主题的替代快照判 `stale_snapshot` 时，缓存已是当前的就结束这次重取，否则算一次失败并再请求（3 次为限）；用尽后、替代请求还没被丢弃的窗口期里，页面 `rt_resync` 得到 `request`（重新开始）而不是 `pending`。
- 测试台清理：单实例测试台在外壳本身（pid + 创建时刻）还活着时持续关闭失败框（不再只看 watcher 是否在），写文件也走重试与占用者取证（`write_shared`，`transient_reads` 里 `op:"write"`）；exit 冒烟在「suspended job launch」超时后从 root.json 找回迟到启动的外壳并照常清理。

修复第 3 轮（决定_第六波裁定）：
- **锁屏时 single_instance 记为跳过**：`p3_instance_smoke.py` 开跑前与用例失败时取证工作站是否锁屏（本会话里有 `LogonUI.exe`，经 `WTSEnumerateProcesses` 按会话查；或前台窗口属于 `LockApp.exe`/`LogonUI.exe`），`second-shell` 的前台授权点击被锁屏盖住时抛 `EnvironmentLocked`，该用例记 `skipped: environment_locked`（清理必须干净），其余用例照跑且必须通过。只有「没通过的用例全都是被锁屏挡住的」时，整次结果才写 `smoke-skipped.json`（`reason: environment_locked`，含锁屏证据与各用例结果）并以 0 退出，`p3_smoke_rounds.ps1` 记为 `SKIP … skipped=environment_locked blocked_by_lock=…`，不计失败；锁屏期间别的原因的失败（例如 `hello_timeout`）照旧判失败，第一个这样的失败即停。自测：`python -B shell/app/src/p3_instance_smoke.py --lock-selftest <目录>`。工作站解锁且空闲时 single_instance 要另补跑 3 轮（P7 前必须补齐）。
- **迟到外壳**：启动等 root.json 超时（「suspended job launch」）后，watcher 仍可能把外壳拉起来，外壳停在原生失败框里留在桌面。`scripts/p3_residue.ps1` 新增 `Resolve-LateShell`（等 watcher 至多 30 秒写出 root.json，按 pid + 创建时刻认定，不认复用的 pid）与 `Close-LateShell`（关原生框 → 托盘退出或关主窗口 → 等 30 秒 → 最后才结束本作用域的测试外壳；步骤写 `late-shell.json`）；shell（main、webview、missing、lost/lostbuilt、c3）、tray、host、notify 冒烟的清理在不知道外壳时都调用它（exit 冒烟第 1 轮已有同样处理）。单实例测试台的清理在外壳 pid 未知时也先等 watcher 写出 root.json。
- PROTOCOL §13 `rt_resync` 条与 `transport.rs` 的 `state_snapshot_res` 注释按 T-P2-H2 之后的语义改正：被替代快照覆盖的旧边沿与 `gap` 直接丢弃，只有出现更新的边沿时流上才会再出现值。

## 10-30 提醒卡片（notice 窗口）

方案 `.build/提醒模块重设计_20261030.md` §4、§10；外观与动效按 `.build/claude_notice_mockup/`。代码在 `app/src/notify.rs`（路由、暂存、回执）、`app/src/notice.rs`（卡片窗口）、`ui/src/notice/`（卡片页）、`ui/src/shellc/notify.js`（应用内）。

- **`alerts.desktop.notice`**（可选对象）：`kind`（outage、capture_lost、start_failed、fallback、unstable、update、preview）、`incident`（ASCII，≤64）、`state`（open、update、resolved、closed）、`group`（connection、tier_fallback、update、preview）、`title_id`，可选的 `detail_id` / `reason_id` / `fix_id`（缺省与 null 同义）、`params`（≤8 个，字符串或整数）、`action`（缺省或 `"view"`）、`sound`、`sticky`、`ttl_ms`。非 sticky 的 `ttl_ms` 为 0 时取默认（信息 8 s、警告 10 s、恢复 6 s），其余夹在 2–120 s；sticky 为 0。对象不合格时整帧照旧按 `title` / `detail` 文字显示，shell.log 记 `notify_notice_invalid code=`。`title` / `detail` 只作气泡兜底和目录缺词时的兜底。
- **路由**：窗口在前台且页面已声明 → 应用内 `host:notify`（带上 `notice`）；否则，以及 `force`（设置页的预览提醒）→ 卡片窗口；Windows 报告用户忙（`SHQueryUserNotificationState` 为 QUNS_BUSY、QUNS_RUNNING_D3D_FULL_SCREEN、QUNS_PRESENTATION_MODE、QUNS_QUIET_TIME）→ 暂存（预览从不暂存）。暂存每个事故只留最新一帧、最多 20 个，被取代的帧回执 `dropped/none`；有暂存时 worker 每 5 秒问一次 Windows，空闲后最后状态为 open/update 的重新路由显示，resolved/closed 的不单独显示、悄悄撤掉它们的卡片或横幅并回执 `dropped/none`，期间有恢复时再加一张汇总卡（`notice.summary.*`，参数 count、resolved；不响、不回执）。closed 不显示任何东西：撤掉该事故的卡片或横幅，立即回执 `dropped/none`。
- **一个事故只在一处**：事故进了应用内，卡片窗口撤掉它的卡；事故改走卡片窗口，页面的横幅用外壳自己的 `host:notify`（id `shell-clear-N`，`notice.state=closed`，页面不确认）撤掉。新 epoch 时卡片窗口撤掉旧侧车的卡（外壳自己的卡保留），页面在 `rt:reset{generation_changed}` 时撤掉全部提醒横幅与 toast。
- **回执**：卡片真正上屏（页面 `notice_rendered` 且窗口可见）→ `delivered/windows`；卡片窗口建不起来或渲染不了 → 照旧走托盘气泡，结果照旧；应用内仍由页面确认 `delivered/in_app`；什么都没显示 → `dropped/none`。每个 delivery_id 只回执一次；新 epoch 之后才到的旧卡片结果不再回执（Notifier 按会话代次过滤）。
- **提示音**：`notice.sound` 为 true 的帧真正显示时（卡片上屏，或页面确认 `host_notify_rendered` 时 `shown` 不为 false）播一次 `PlaySoundW("Notification.Default", SND_ALIAS | SND_ASYNC | SND_NODEFAULT)`；暂存期间和汇总卡不响。`host_notify_rendered` 新增可选参数 `shown`：总览页上 connection / tier_fallback 的 open、update 不显示（主状态已显示），页面照样确认但带 `shown:false`，外壳照常回执、不响。
- **服务意外停止**：侧车崩溃（`crash_restart`）且主窗口不在前台 → 外壳自己出一张 `service_stopped` 卡（错误、常驻、按钮「查看」，没有 delivery_id，不回执；用户忙时暂存，跨过崩溃后的重拉）。卡片窗口不可用时不退回气泡（原生文案里没有这条）。
- **卡片窗口**（`notice.rs`）：label `notice`，加载内嵌的 `notice.html`。无边框、透明、置顶、不进任务栏；`WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW`（每次显示前补齐，tao 改写样式也不怕），`SW_SHOWNOACTIVATE` 显示，所有 `SetWindowPos` 带 `SWP_NOACTIVATE`，`WM_MOUSEACTIVATE` 回 `MA_NOACTIVATE`；万一仍被激活，立刻把前台还给原来的窗口（每次显示和显示期间每秒记一次「上一个前台窗口」，shell.log `notice_focus_returned`）。宽 396 CSS px（卡片 360 + 贴屏边 12 + 阴影 24，与 `notice.css` 的 `--nc-width` 由单测对齐），高为页面报告的卡片堆高度；放在主窗口所在显示器（主窗口隐藏或最小化时用主显示器）工作区右下角，按该显示器 DPI 换算（跨 DPI 时先移过去再设一次精确尺寸）。第一张卡来时在 UI 线程创建（和主窗口同一个 WebView2 用户数据目录与浏览器参数，同一个浏览器进程；同样关闭右键菜单、开发者工具（debug 除外）、加速键、缩放、自动填充、外部拖放），最后一张卡离开 60 秒后销毁；退出时与主窗口一样先关掉它的 WebView 控制器。
- **兜底**：内嵌资源里**没有** `notice.html`（按精确键名查；Tauri 对缺失的路径会回落到 index.html，绝不能把主页面装进卡片窗口）→ 不建窗口、每张卡退回气泡（shell.log 只记一次 `notice_window_unavailable reason=page_missing`）；窗口建不起来 → 退回气泡，5 分钟内不再尝试；8 秒内页面没调用 `notice_ready` 或 4 秒内没渲染某张卡 → 退回气泡并销毁窗口，下一张卡重建。
- **命令与权限**：页面 ↔ 外壳为一条 Channel 加五个命令 `notice_ready{channel}`、`notice_rendered{delivery_id}`、`notice_resize{height}`（CSS px，0 = 空，隐藏窗口并开始 60 秒计时）、`notice_open{delivery_id}`（按托盘恢复的路径恢复并聚焦主窗口，窗口可见后 `host:navigate` 到该卡的 target）、`notice_dismiss{delivery_id}`；外壳 → 页面为 `{t:"notice:show", language, card}`、`{t:"notice:close", incident}`、`{t:"notice:clear"}`、`{t:"notice:language", language}`（`ui.language` 变化时，已显示的卡片随之改写）。应用没有自己命令的 ACL 清单，能力文件管不到应用命令，所以 `main.rs` 的每个命令都核对调用方窗口 label：`notice` 窗口只能调这五个，`main` 窗口只能调 `HOST_INVOKES`（单测核对两张调用表和每个命令的 label 检查）。`capabilities/notice.json` 只给 `notice` 窗口、不授任何权限，已登记在 `tauri.conf.json`。CSP 不变。
- **卡片页**（`ui/src/notice/`，`ui/build.mjs` 只为 app 目标在 index.html 旁产出 `notice.html` + `notice.<hash>.js/.css`，build-manifest.json 一并登记）：tokens、字体、`icons.js` 的图标（用 createElementNS 重建，不经 innerHTML）、同一套语言文件与 `T.t`，卡片跟随外壳的语言（hello.lang / ui.language）。最多 3 张、最新在最下、更早的合并成「还有 N 条」；非常驻卡的倒计时环就是计时器，鼠标在卡片堆上时全部暂停；× 关闭；卡片正文或「查看」→ `notice_open`；顶行是品牌标记、Tesseract、级别词（错误 / 警告）、时间（刚刚 / n 分钟前）；恢复时同一张卡原地变成恢复态；减少动效时只淡入淡出。文字一律是文本节点。
- **托盘小点**：本轮未做（托盘图标是 10 个预生成的 ICO，加小点要再生成一套图标）。
- 本轮没有复制资源到 `shell/app/ui`，所以现在内嵌资源里没有 `notice.html`，运行时按上面的兜底全部走气泡；下次 `scripts/shell_copy_ui.ps1` 之后卡片窗口才生效。
