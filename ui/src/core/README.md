# ui/src/core

- `core.js`：`window.T`，各页共用的运行时（DOM、事件、store、动效、组件库 `T.ui`、路由、通知、浮层、命令面板）。文件头注释是完整的接口清单。
- `app.js`：外壳接线（品牌点、侧栏页脚、全局通知、命令面板的外壳命令、快捷键、路由启动、演示钩子）。
- `icons.js`：图标。

文字一律走消息目录（T-P4-04a）：外壳层用 `shell.*`，组件默认词和保存条用 `core.*`，层级角色词用 `tier.*`，网络模式用 `mode.*`，通用按钮词用 `common.*`；`ui/src/i18n/locales/zh_CN/<ns>.json` 是源语言。

## 跨页接口

T-P4-04a 先建好下面三项（架构 §2.9，批评 C9），让 05b、05d、05e 与 06 并行时不互相等待。签名不变；行为由所列的单补齐或覆盖。

### `T.ui.saveBar.dirtyCount() → number`

- 当前保存条里未保存的更改项数，与保存条上显示的「N 项更改未保存」是同一个数。
- 没有保存条、保存条已收起、或已保存成功（`state === 'saved'`）时为 `0`；保存中、保存失败、「还不能保存」时仍计入（更改还没生效）。
- 实现：`core.js` 保存条区（`saveBar.dirtyCount`）。T-P4-05d 改造保存条时保持签名与上述语义。
- 用途：外壳退出询问 `host:confirmQuit` 的 `unsaved` = `dirtyCount()` + 已登记的抽屉草稿数（T-P4-06）。

### `T.shellc.registerDraft(fn) → unregister`

- 抽屉或会话里有未保存的草稿时登记：`fn() → number | boolean`，返回该抽屉未保存的项数（`true` 记 1 项，`false` / `0` 记 0）。抽屉关闭时调用返回的 `unregister()`（重复调用无害）。
- `fn` 不是函数时抛 `TypeError`。
- 现状：`core.js` 里是空实现（只校验参数，返回空的 `unregister`）。T-P4-06 在 `boot/shellc.js`（在 `core.js` 之后执行）里**无条件**赋值 `T.shellc.registerDraft` 为真实实现，并据此计算 `unsaved`。
- 调用方：05b（层级页抽屉）、05d、05e（配置与提醒的抽屉）直接调用，不判断实现是否存在。

### 路由的 `focus` 参数

- 约定：`T.go(page, { focus })`；页面在 `enter(ctx)` 里读 `ctx.params.focus`（首次渲染时 `render(root, ctx)` 的 `ctx.params` 里也有）。
- 标记写法同样有效：`<button data-go="settings" data-go-focus="update">` → `T.go('settings', { focus: 'update' })`（`data-go-<名字>` 都会成为参数）。
- 路由只负责把参数原样传到页面，不解释它。配置页的落点（`alerts`、`update`）由 T-P4-05d 实现；外壳的 `host:navigate{page:'alerts'|'update'}` 由 T-P4-06 映射成上面的调用。
- 同一页面再次 `T.go` 时，`enter(ctx)` 以 `ctx.again = true` 和新的 `ctx.params` 再跑一次。
- `focus` 是一次性意图：只在导航（`T.go`）时传给页面；切换语言时的页面重绘不重放它（见下节）。

## 切换语言时的页面重绘

- 路由在 `i18n:change` 时更新面包屑、窗口标题（`shell.window.title`）和各页 `<section>` 的无障碍名称；页面标题取 `def.titleId`，否则四个外壳页面取 `shell.page.<id>.title`，否则 `def.title`。
- 页面声明 `T.page(id, { relocalize: 'rerender', snapshotState() → state, restoreState(state) })` 时（架构 §3.7 第 4 条）：
  - 两条路径的顺序相同：`snapshotState()` →（`leave`）→ `render` → `enter` → `restoreState(state)`。每个钩子都有异常保护（与路由其他钩子相同：异步重抛），一个钩子抛错不会打断语言切换或导航。
  - 正在显示的页面立即重绘：`snapshotState()` → `leave(ctx)` → 换新的 `life` 作用域并清空页面根 → `render(root, ctx)`（`ctx.relocalized = true`，`ctx.params = {}`）→ `enter(ctx)`（`ctx.relocalized = true`，**`ctx.params = {}`**）→ `restoreState(state)`。
    - 重绘不是导航：`render` 和 `enter` 拿不到上次导航的一次性意图（例如 `focus` 落点），页面状态（滚动位置、草稿、展开项）只经 `restoreState` 恢复。上次导航的参数以只读（冻结）的 `ctx.navParams` 给出，仅供参考，页面不应据此重放落点。
    - 重绘会走一遍 `leave(ctx)`（`ctx` 是页面当前的 ctx，没有专门标记），`render` 时页面处于「已离开」状态，要到 `enter` 才恢复。所以页面的 `leave` 不应丢弃草稿、收起保存条或清掉其他要跨重绘保留的状态；要跨重绘保留的状态放进 `snapshotState` / `restoreState`。
    - 重绘之后同页再次 `T.go`（`ctx.again = true`）是导航，不是重绘：路由会去掉上次重绘留在 ctx 上的 `relocalized` 和 `navParams`，`enter` 拿到的是这次导航的参数。之后再重绘时，`ctx.navParams` 就是这次导航的参数。
    - 重绘前焦点在页面内、重绘后丢到 `<body>` 时，路由把焦点交给 `ctx.landing`（页面在 `enter` 或 `restoreState` 里可以设置），否则交给页面根（`tabIndex = -1`）；`restoreState` 自己把焦点放到页面内某处的，以它为准。
  - 已渲染但没在显示的页面标记为待重绘，下次 `T.go` 进入时：`snapshotState()` → `render(root, ctx)`（`ctx.relocalized = true`，`ctx.params` 为这次导航的参数）→ 路由把它设为当前页 → `enter(ctx)`（这次导航的参数，`ctx.relocalized` 不设）→ `restoreState(state)`。`restoreState` 运行时 `T.router.current` 已是本页。
  - 没有声明的页面只靠 `data-i18n` 就地换字（`T.ti`）和自己订阅的 `i18n:change`。
