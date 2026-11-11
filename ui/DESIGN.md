# Tesseract UI — design system spec (binding)

This is the contract every page builder follows. The overview page (`src/pages/overview.js`, `css/pages/overview.css`) is the reference for look and motion. The product lives in `ui/` (build, tests and gates: `ui/README.md`); the prototype in `.build/claude_ui_prototype/` (with its `gallery.html`, which shows every component live) stays as a read-only design reference.

> **Product version (T-P4-12, P4 exit).** Copied from the prototype's `DESIGN.md` and brought up to the product: §2 (modules, build, no external resources), §4.3 / §4.4 (no sidebar status card), §5.5 (`T.rt` instead of `T.sim`), the new §5.6 (i18n), §5.7 (untrusted text), §5.8 (prototype-only controls) and §8 (new copy: zh_CN with `_desc`). The contract behind it: `bridge_schema/PROTOCOL.md` (v1, the only contract) and `.build/新界面迁移_工单/P4_设计/P4_架构.md`. Where they disagree with this file, they win.

If a page needs something this spec does not provide, build it page-locally with a page-prefixed class (`.logs-reader`, `.tiers-group`), keep it inside the rules below, and report it so the integration pass can promote it. Shared files (`css/tokens.css`, `base.css`, `components.css`, `shell.css`, `src/core/*`, `src/rt/**`, `src/shellc/**`, `src/i18n/**`, `src/index.html`, the `boot/` mount files, the gate configs) are owned by named tickets (`ui/README.md`, mount points); a page builder changes only its own page files and its own message namespace.

---

## 1. Principles

1. **One light theme.** White main plane, Mist sidebar, hairlines by role. There is no dark mode, no theme switch and no `prefers-color-scheme` block. Nothing dark is larger than a chip or a keycap.
2. **No tech palette and no ornament.** No neon, cyan, glow, gradient chrome, glass, HUD grids or ink-wash. Black, white, one dark-blue accent and grey carry the UI, with a few low-saturation companions (slate blue, sand, clay) so it never reads as a black-and-white snapshot (F-02, user 10-28). Distinctiveness comes from proportion, type (including the voice face of §3.5), the accent, the 「豁然」 mark and the motion.
3. **Natural interaction, like a good modern website.** Every control answers a press on the next frame with a slight scale, then springs back without wobble. States glide instead of cutting: indicators slide, labels roll, lists FLIP, panels grow out of what was clicked. It stays calm: quick durations of 120–320 ms and nothing that loops while idle.
4. **The control that was pressed shows the result.** Busy, done and failed states morph on the control itself (`T.ui.busy`). Toasts are only for things that happen elsewhere.
5. **State is shown by shape and word, never by colour alone.** Every status carries a glyph shape plus a word; emphasis is typographic first (weight, the voice, a tracked mark), and the muted hue is the second signal. The accent is **never** a status colour (it marks 当前 / 承载流量, which is a role, not a health).
6. **Practical first.** Answer the question the user came with (connected? which tier? where do I exit? what broke and how do I fix it?), keep the answer visible, and never cover status with notices.
7. **No product advice on screen** (F-03, user 10-28). The interface says what is, what happened and what to do about a problem; it does not explain how the product works or advise how to use it (which nodes belong in a tier, what is recommended, what the product does automatically). A tier is named by its code name alone. Safety warnings with real consequences and the effect of a destructive or risky action stay.
8. **Honest data.** Never show raw internal values. Don't give invented numbers prominence, and don't disable things silently: hide what is irrelevant, or say why something is unavailable.

---

## 2. Files, load order, ownership

```
src/index.html (template)  css/index.css: tokens → fonts → base → components → shell → pages/*.css   (design adds css/design.css)
src/entry/app.js:    core → icons → boot/i18n → boot/data-app → boot/rt-app → boot/shellc → pages/* → core/app
src/entry/design.js: core → icons → boot/i18n → boot/data-design → boot/rt-design → boot/shellc → pages/* → core/app → boot/design
```
- **No external resources at all.** Fonts are self-hosted: Instrument Sans and Geist Mono from the locked `@fontsource` packages, and "Tesseract Kai", a renamed subset of 霞鹜文楷 GB 屏幕阅读版 (OFL) cut to the characters of the zh_CN strings by `tools/zh_voice_font.py` (`ui/fonts/`, built by `build/fonts.mjs`; a unit test fails when a zh_CN string uses a character the subset lacks); no CDN, no Google Fonts, no remote image. The shell's CSP (`shell/app/tauri.conf.json`) has `'self'` sources only and no `'unsafe-inline'`.
- **Modules, one bundle per target.** Source files are ES modules that still hang their API on the global `window.T` (D10: plain JS, no framework). `ui/build.mjs` (esbuild, versions locked in `ui/package-lock.json`) bundles each entry to one IIFE plus one CSS file with content-hashed names and fills them into `index.html`; no inline script or style is generated (CSP). A new module is imported by the page or core file that uses it; the entry files and the `package.json` scripts are fixed (P4 rule 10).
- **Two targets, one page code.** `app` is embedded in the Tauri shell (`TauriTransport`, minified, `__DEMO__ = false`, demo code stripped and gated by `scan.mjs --rule demo`); `design` is for design review and the render tests (`FakeTransport` with the frame-producing simulator `src/design/sim.js`, or a fixture scenario) and opens from `file://`. Both read the same store keys and `rt:*` events (§5.5).
- **Languages** are built into one classic-script file per language (`locale-<lang>.<hash>.js`, §5.6); zh_CN is preloaded before the first paint as its own file, not inside the main bundle (决定_第四波裁定 #1).

| You own | Contract |
|---|---|
| `css/pages/<page>.css` | Page-local classes only, all prefixed with the page name (`.logs-…`, `.tiers-…`, `.set-…`). Use tokens only; never hard-code a hex value. No inline `style=""` (CSP): set a custom property through the CSSOM (`el.style.setProperty('--v', x)`) or add a class. |
| `src/pages/<page>.js` (and `src/pages/<page>/**`) | Exactly one `T.page(id, def)`. Runtime data comes from the store keys and `T.rt` (§5.5); synthetic data lives only in `src/design/`. |
| `src/i18n/locales/zh_CN/<namespace>.json` | The page's own message namespace (§5.6). |

Page ids: `overview` · `profiles` (alias `tiers`) · `logs` · `settings`. Hash tokens: `#overview #tiers #logs #settings`.

---

## 3. Tokens (`css/tokens.css`; always reference the variable)

### 3.1 Surfaces and lines
| Token | Value | Use |
|---|---|---|
| `--c-bg` | `#FFFFFF` | Main plane, panels, menus, drawers |
| `--c-mist` | `#F7F7F8` | Sidebar ground only |
| `--c-well` | `#F2F3F5` | Inset wells: segmented track, chips, keycap-like tags, the menu highlight |
| `--c-well-2` | `#E7E9EC` | Empty tracks: meter and progress ground, slider track |
| `--c-tint-1` / `--c-tint-2` | `#FBFBFC` / `#F6F7F8` | Tier deck cards behind the front card (overview only) |
| `--c-surface` | `#FFFFFF` | The white of controls, cards, menus and the toast (= `--c-bg`, named by role) |
| `--c-surface-hover` / `--c-surface-press` | `#FAFAFB` / `#F5F6F7` | Hover / pressed on a white surface that is itself a button (strip cells, the Gateway row, the demo link, the mode confirmation's ground) |
| `--c-btn-hover` | `#F9F9FA` | Secondary `.btn` hover |
| `--c-ghost-1…3` | `#FCFCFD` `#FAFAFB` `#F8F8F9` | Ghosted surfaces: stopped deck bands, a gated option card, a folded table row |
| `--c-on-accent` / `--c-on-accent-2` | `#FFFFFF` / `rgba(255,255,255,.84)` | Text on the accent fill / its secondary figures (5.7:1) |
| `--c-scrim` | `rgba(21,23,26,.10)` | Scrim under modal overlays (also the palette) |
| `--c-line` | `rgba(21,23,26,.085)` | Default hairline: panel edges, row separators |
| `--c-line-2` | `.13` | Control edges: secondary button, chip-line |
| `--c-line-3` | `.20` | Field edges at rest (input, search, select); hover edge of other controls |
| `--c-line-4` | `.30` | Field edges on hover |
| `--c-wash` / `--c-wash-2` | `.045` / `.075` | Hover wash / pressed wash on ghost and icon buttons |
| `--c-hover` | `rgba(21,23,26,.035)` | Row hover in lists and tables |
| `--c-sel` / `--c-sel-hover` | `#EEF0F4` / `#E6E9EF` | Selected or open row (the focus role, so it is the accent tint) |
| `--c-hl` | `= --c-well` | Gliding keyboard highlight in menus and the palette |
| `--c-skel` | `#EEF0F2` | Skeleton blocks |
| `--c-changed` | `= --c-ink-2` | Unsaved-change dot (neutral, not a status) |
| `--c-dot-off` / `--c-ring-off` / `--c-ring-mute` / `--c-dot-dim` | `#A7ACB5` / `#9AA0AA` / `#8F96A1` / `#B9BEC6` | Neutral marks (never text): the stopped brand dot and unknown dots · hollow grey rings (未启用, filter chip off) · dashed / diamond marks (待检查, 草稿) · dimmed dots |
| `--c-switch-off` / `--c-switch-hover` | `#D9DCE1` / `#CDD1D7` | Switch track off / its hover. The off track has a `.22` ring and the knob a `.38` ring (3.2:1 on white), so an off switch never reads as empty space |
| `--c-grid` / `--c-grid-base` | `rgba(21,23,26,.07)` / `.14` | Chart grid lines / baseline |
| `--c-selection` | `rgba(43,58,96,.16)` | `::selection` |

Hairlines are drawn with `box-shadow: inset 0 0 0 1px var(--c-line)` (or `inset 0 1px 0` for a single edge). **Never nest bordered boxes.** Page stylesheets use these tokens only: if a near-white, a wash or a line you need is not here, it is one of these roles, so pick the role instead of mixing a value.

### 3.2 Text (every meaningful text is ≥ 4.5:1 on white and on Mist)
| Token | Value | Use |
|---|---|---|
| `--c-ink` | `#15171A` (17.9:1) | Primary text, values, titles |
| `--c-ink-2` | `#4F5560` (7.4:1) | Secondary text: descriptions, inactive labels, units |
| `--c-ink-3` | `#646B78` (5.4:1) | Tertiary: hints, key labels, timestamps, axis, meta |
| `--c-ink-4` | `#A3A8B2` | **Decoration and disabled only.** It never carries meaning. |

### 3.3 The accent (dark blue, 暗蓝)
| Token | Value |
|---|---|
| `--c-accent` | `#2B3A60` (11.2:1 on white; F-02, user 10-28: 黑白暗蓝灰为主. Was the slate-indigo #44507E of F-01 and #3B50A4 before it) |
| `--c-accent-hover` / `--c-accent-press` | `#263355` / `#202B48` |
| `--c-accent-ink` | `#2B3A60`, for text on the accent tint |
| `--c-accent-tint` / `--c-accent-tint-2` | `#EEF0F4` / `#DEE1E9` |
| `--c-accent-line` / `--c-accent-wash` | `#C7CCD8` / `rgba(43,58,96,.10)`, hover edge / hover wash of a control sitting on the accent tint |
| `--c-accent-off` | `#A9AFBE`, a disabled primary, a checked-disabled mark |

**Allowed uses only:** the brand dot, the primary action, progress fill, the active nav glyph, the selected option or section glyph, the download series, the focus ring, the 当前 marker (`.st-current`, the check in a select menu) and the 承载流量 mark (`.pill-ok`), checked checkboxes, radios and switches, and the selected row tint. **Never** use it for status such as healthy, warning or running.

### 3.4 Semantics (always paired with shape or word)
| Meaning | Text | Dot / fill | Tint | Mid tint (bars) |
|---|---|---|---|---|
| Healthy / 可用 / 正常 | `--c-ok #2F343B` (ink) | `--c-ok-dot #5A6B8C` (slate blue) | `--c-ok-tint` | `--c-ok-2` |
| Warning / 较慢 / 已回退 | `--c-warn #7A6342` (umber, 5.7:1) | `--c-warn-dot #B39B6E` (sand) | `--c-warn-tint` | `--c-warn-2` |
| Failure / 不可用 / 失败 (a node, a probe, a source) | `--c-fail #8C4E46` (clay, 6.4:1) | `--c-fail-dot #B27A71` | — | — |
| Error, invalid input, destructive act | `--c-err` (= `--c-fail`) | `--c-err-dot` | `--c-err-tint` | `--c-err-2` |

F-01 (user 10-27, option B) took the saturated green / amber / red out. F-02 (user 10-28): emphasis is typographic first (§3.5), and colour stays as **low-saturation companions of the dark blue**: never green, never a saturated red. The hues sit on marks (dots, rings, glyphs, icons, bars) and on short state words, which also take `--fw-semi` when they need a look; running text and titles stay ink (the one exception is the hero's state word 启动失败 / 出站已阻断, in clay). Bastion's 应急 role is a role, in the voice colour, not a warning.

Severity surfaces in content (callouts) use barely tinted `--c-ok-wash` / `--c-warn-wash` / `--c-err-wash` grounds with `--c-ok-line` / `--c-warn-line` / `--c-err-line` edges; their titles stay ink and the hue is the icon's (and the level word's). Banners float (F-05): they are pages of one white card, the hue on the icon and the level word only (§4.3). The quiet destructive button uses `--c-danger-line` at rest and `--c-err-wash-2` / `--c-err-line-2` on hover, the solid danger button `--c-err-hover` / `--c-err-press` / `--c-err-off`.

Data colours: `--c-series-down` (the accent), `--c-series-up` (mist blue `#8D9AB5`) with `--c-series-up-area`, and the categorical single-hue ramp `--c-ramp-1…5`, the accent's blue from `#3A4868` to `#DCE1EA` (总览's 协议分布 starts at `--c-ramp-2`: the least-needed panel carries no heavy fill). A pool on standby (`.meter.ink`) is neutral grey (`--c-ring-mute`), not the ramp.

### 3.5 Type
- Families (self-hosted, §2; per-language stacks in `css/tokens.css`: ja puts Yu Gothic UI before any Chinese face, ko Malgun Gothic, ru Segoe UI Variable / Cascadia Mono): `--font-sans` (Instrument Sans, then, for Chinese, 思源黑体 Noto Sans SC (part of Windows 11 24H2) and Microsoft YaHei UI), `--font-mono` (Geist Mono 500, used for IPs, ports, hosts, raw log text and rule values), `--font-brand` (the wordmark only) and the voice, `--font-voice` (below).
- Weights: `--fw-regular 400`, `--fw-medium 500`, `--fw-semi 600`. There are no other weights.
- **Chinese may have only two weights.** 思源黑体 (Noto Sans SC, variable) draws a real 500 and 600, but where it is missing Microsoft YaHei UI ships Regular and Bold only, so a 500 CJK label renders Regular and a 600 CJK title renders Bold. Build the medium tier from colour and size (`--c-ink` vs `--c-ink-2`, 14 vs 13), not from weight 500; keep 600 for short CJK titles (panel and section titles, the tier name, values such as 已布防).
- Tracking: `--track-tight -.022em` at 22 px and above. Monospace uses `-.01em`.
- Live numbers always use tabular figures (`.tnum`, or `font-variant-numeric: tabular-nums`).
- Technical text is verbatim: `.mono`, `.c-mono`, `code`, `pre` and `kbd` reset `word-spacing` (the body adds `.15em` so that ` · ` separators breathe next to CJK).

| Size | Token | Use |
|---|---|---|
| 12 | `--fs-12` | Hints, key labels, chips, badges, axis, meta, table header, 小号 captions |
| 13 | `--fs-13` | **Body.** Rows, buttons, inputs, descriptions, menus |
| 14 | `--fs-14` | Panel and section titles (`.panel-title`, `.sh-t`), nav, option-card titles, emphasised values |
| 15 | `--fs-15` | Top-bar page title (shell), drawer/sheet/dialog titles, settings section titles (`.fsec-t`) |
| 18 | `--fs-18` | Small figures (rare) |
| 24 | `--fs-24` | The IPv4 figure (mono) |
| 26 | `--fs-26` | Session totals (24 when the window is under 760 px tall) |
| 34 | `--fs-34` | The current tier name |

Pages do not add new sizes. Dense pages (lists, logs, settings) live in 12 / 13 / 14 with 15 for section titles. A single key figure may use 24–26 if the page has one.

**Emphasis (F-02, user 10-28: 突出信息优先用加粗、花体等写法，要优雅；不同语言效果不同).** Three tools, in this order, before any colour:
- **Strong:** `--fw-semi`. A state word that needs a look (不可用, 已触发, 未保存), a key value.
- **Mark:** 12 px, `--fw-semi`, `--track-mark` (.02em; zh .06em, ja .04em), **no fill and no capsule**. Run-state words and tags: 承载流量 (accent), 当前 (accent), 已回退 (umber), 未保存 (clay), 会重启服务, 待生效. `.pill`, `.st.chip-st`, `.prov`, `.set-eff`, `.set-res-tag`, `.banner-sev` and the logs footer tags are marks since F-02; the class names stay.
- **Voice (the 花体):** a second face for the few words that are a **provisional state** (待检查 · 待复检 · 草稿 · 待生效 · 状态可能已过期) or a **quoted reason** (TLS 连接失败, 更新失败). Never for running text, labels, values, data or buttons, and never bold. (It first carried the tier role words too; F-03 removed those from the interface.) The one other place is the letter for users (§6.22): its greeting and closing wish are the hand of a letter. The letter's Latin-only parts (heading, numbers, signature) use `--font-letter`, Sitka italic in every language.

| Language | Voice | Why |
|---|---|---|
| en, de, es, fr, pt, pseudo | Sitka Text italic (Georgia italic where Sitka is missing) | a true italic with screen hinting; Windows 8.1+ |
| ru | the same (Sitka has Cyrillic italics) | Cyrillic italic is the native emphasis |
| zh | "Tesseract Kai" (the bundled subset of 霞鹜文楷 GB 屏幕阅读版, a handwritten kai), upright, ×1.04, one ink darker; the system 楷体 behind it for a character outside the subset | the user: too upright a face reads badly in Simplified Chinese, and a handwritten face reads better; 思源宋体 and the system 楷体 were tried and looked odd |
| ja, ko | the UI face, upright | no fake oblique on kana or hangul; no Japanese / Korean serif or kai face is assumed (Yu Mincho draws too thin at 12–13 px) |

Tokens: `--font-voice`, `--voice-style`, `--voice-weight`, `--voice-scale`, `--voice-track`, `--c-voice` (ink-2; zh ink) for quoted reasons and 待生效, `--c-voice-3` (ink-3; zh ink-2) for provisional states. The voice sits in running text or alone in a status mark, where the baseline aligns by itself; don't put it beside other text in a row that centres its items (Sitka's tall ascent would leave it about .15em high). The utility classes are `.t-strong`, `.t-mark` and `.t-voice` (base.css).

### 3.6 Space (4 px grid)
`--sp-1 4 · --sp-2 8 · --sp-3 12 · --sp-4 16 · --sp-5 20 · --sp-6 24 · --sp-7 28 · --sp-8 32`

The rhythm used by the reference:
- Page padding: `20px var(--gut) 24px`; 16 / 16 when the window is under 760 px tall. `--gut` is 28, or 24 at 1180 px wide or less.
- Gap between major blocks: 16 (12 when under 760 px tall). Inside a panel: 16 px side padding and a 44 px head.
- Rows: 12 px side padding, 36 px tall (`--row-h`), 30 px dense (`--row-h-dense`), 48 px for two-line rows (`.li.two`).
- Controls: 32 px (`--ctl-h`), 28 px small (`--ctl-h-sm`), 24 px extra small. Gap between buttons: 6–8.
- Field: label → 6 → control → 6 → message. Fields grid gap: 12 × 16.
- Settings rows: 14 px vertical padding with a hairline between rows. Settings sections are 36 apart.

### 3.7 Radii and shadows (by role; never the same big radius and shadow on every block)
| Radius | Token | Use |
|---|---|---|
| 4 | `--r-xs` | Checkbox, keycaps, 20–24 px chips and buttons (`.btn-xs`, `.chip.sm`, `.icon-btn.sm`), small marks |
| 6 | `--r-ctl` | Buttons, inputs, rows, chips |
| 8 | `--r-md` | Nav items, palette rows, the segmented track, the top-bar command button, the chart tooltip |
| 10 | `--r-panel` | Panels, cards, option cards, menus, popovers, callouts |
| 12 | `--r-float` | Palette, dialogs, sheets, the save bar |
| 999 | `--r-pill` | Pills, switches, filter chips, badges, the run control |

| Shadow | Use |
|---|---|
| none + hairline | **Default for content.** Panels do not float. |
| `--sh-raise` | Selected thumb or indicator (segmented thumb, nav indicator) |
| `--sh-card` / `--sh-card-back` / `--sh-card-lift` | The tier deck only (front / behind / hover) |
| `--sh-pop` | Menus, popovers, the slider bubble, sparkline tips |
| `--sh-float` | Drawers, sheets, dialogs, toasts, the save bar |
| `--sh-drag` | A row being dragged |

Content panels do not lift on hover. Only clickable deck cards and dragged rows do.

### 3.8 Motion tokens
- `--ease-out` for entrances and micro changes, `--ease-in` for exits, `--ease-inout` for travel, `--ease-quart` for data morphs.
- `--spring` / `--spring-dur` are generated at boot as a CSS `linear()` curve from a damped spring (k 560, c 46), with no overshoot. `--spring-soft` is k 380, c 38.
- `--dur-press 0ms` · `--dur-micro 160ms` · `--dur-panel 240ms`. The press is not a transition: the pressed scale lands on the next frame (a transition would first wait for the compositor to start it, which on a busy machine took 100–600 ms), and the release springs back on `--spring`.
- JS spring presets in `T.motion.SP`: `press` (k560/c46), `soft` (k380/c38; panels, notices, FLIP), `glide` (k520/c44; indicators), `settle` (k300/c34; deck cards, shared elements, morphs). Each is `{ easing, duration }`.

### 3.9 Layout and stacking
- `--side-w` 232 (208 at 1180 px wide or less), `--top-h` 52, `--gut` 28/24, `--page-max` 1240.
- z-index: save bar 20, shared-element ghost 65, overlays 70, morph ghost 76, palette 80, menus and popovers 88, tooltip 90, launch cover 100, window buttons (`--z-frame`) 110.
- `--wc-cell` 46, `--wc-w` 138: the window buttons' cells over the top bar's right end (§4.5, the title bar).

---

## 4. Page structure

### 4.1 Registration
```js
(function () {
  'use strict';
  const T = window.T, S = T.store, M = T.motion;
  T.page('logs', {
    // the top-bar title is the message shell.page.<id>.title; pages never repeat it as a heading
    render(root, ctx) {                       // once, the first time the page is shown
      root.classList.add('is-fill');          // optional: fill the window and scroll inside (see 4.3)
      root.append(T.html`<div class="logs" data-enter>${T.tx('logs.empty.filtered.title')}</div>`);
      ctx.life.on('rt:logs', batch => …);     // lifetime subscriptions (the page keeps state while hidden)
    },
    enter(ctx) {                              // every visit; ctx.params, ctx.from, ctx.first, ctx.again
      ctx.scope.every(1000, tick);            // auto-disposed on leave
      ctx.scope.listen(window, 'resize', T.util.raf(layout));
    },
    leave(ctx) { /* stop anything frame-driven */ },
    beforeLeave(ctx, toId) { return bar && bar.dirty ? bar.confirmLeave() : true; },   // false | Promise<boolean>
  });
})();
```
- `ctx.scope` has `on(evt, fn)`, `sub(key, fn, {now})`, `every(ms, fn)`, `after(ms, fn)`, `listen(el, type, fn)` and `add(fn)`, and **all of them are disposed on leave**. `ctx.life` has the same API but is never disposed; use it inside `render()`.
- `T.go(id)` to the page already shown (its nav item clicked again, `Ctrl 1–4`, a palette command) disposes `ctx.scope`, gives `enter(ctx)` a fresh one and calls it again with `ctx.again = true` and the new `ctx.params`. **`enter()` must be idempotent when `ctx.again` is set:** apply the params, resync, but don't replay entrance motion (the overview skips its chart draw-in) and register scope listeners as usual (the old ones are gone).
- Mark the top-level blocks with `data-enter`. The router staggers them in (≤ 400 ms total) on every visit. Don't add your own page-entrance animation.

### 4.2 Params and hand-off contracts (already used by the shell and the overview)
| Page | Params | Notes |
|---|---|---|
| `profiles` | `{ tier: 'vanguard'\|'sentinel'\|'bastion'\|'gateway' }` | Focus that tier. The element carrying `data-flip-target="tier:<id>"` receives the shared-element hand-off from the 总览 tier deck, so put it on the tier switcher item or the header of that tier. |
| `settings` | `{ focus: 'killswitch' }` | Scroll (glide) to that section and give it a short wash. Section ids are the builder's own; map `killswitch` to the 连接 section. |
| `logs` | `{ level: 'issue'\|'error', runOnly: true }` | The palette's 本次运行的问题 uses both; the app calls `T.go('logs')` from 查看日志 in error banners. |
| any | `instant`, `force` (skip `beforeLeave`), `flipFrom` + `flipKey` | Router options. |

In-content links to another page use markup only: `<button data-go="profiles" data-go-tier="sentinel">`. `T.ui.tierTag(id, { link: true })` renders one.

### 4.3 Fitting the window (1280×800 design, 1060×700 minimum)
- The content area is the window minus the sidebar and the 52 px top bar. At 1060×700 that is 852×648. Subtract the page padding (24 px gutters, 16/16 vertical) and you have **804×616**. At 1280×800 you have **992×708**.
- **No horizontal scroll, ever.** Use `minmax(0, 1fr)` columns, `min-width: 0` on flex children, and `.ellipsis`/`.c-ell` for long values. Hide optional columns with `.wide-only` (hidden at 1180 px wide or less).
- **List-like pages (层级 lists, 运行日志)** fill the window and scroll inside: `root.classList.add('is-fill')`, lay the page out as a flex column, and give the scrolling region `flex: 1 1 0; min-height: 0; overflow: auto`. The host then never scrolls. Banners float above the page (F-05) and take no room. This is verified at both sizes.
- **Fixed-layout pages (总览)** fit exactly. When an inline confirmation takes room, the least-needed content yields first (the overview hides 协议分布), and content with a floor keeps it (the chart never drops below a ~110 px plot).
- **Long pages (配置)** scroll in the host (`#host`). A section index uses `position: sticky; top: 0` inside the page and `T.ui.toc(nav, { scroller: T.$('#host') })`.
- When the save bar is up, the shell adds 96 px of bottom padding to `.page`, so nothing ends up under it.
- Check both sizes with the tools: `scrollHeight == clientHeight` of `#host` on fill pages, and no element wider than its container.
- **Banners float; nothing moves for them (F-05, 11-04; the user found a bar that opens above the page and pushes it down inelegant, then a pile of cards inelegant too).** One white floating card `#notices` (a sibling of `#host` in `.main`, outside the scroller; 380 px; surface on `::before`) sits under the top bar's right end, `z-index: var(--z-notice)` (above the page and a docked panel, below the palette and menus), hidden while a modal overlay is open. It shows **one banner at a time**: each banner is a page of `#banners` (the live region), newest first; the shown page (`.is-on`) is in the flow, the others wait on top of it, hidden. A page reads like a system notification: a head line with the level (icon 14 px + 警告 / 错误 as a mark; an info banner has the icon alone) and the close button at its end, then the title (15 px semibold), the message (13 px ink-2) and the actions as words (the primary one in the accent, a soft wash on hover), all full width. From two banners on, dots in the head line (left of the close button, one per banner in its hue, the shown one full) page between them; ← → move along them. A switch slides the words 14 px across inside the card while the old page fades where it was, and the surface follows the new height (`scaleY` on `::before`). The capsule `#notice-pill` (left of 搜索与命令) stands for the banners while the card is tucked away: the most serious kind's icon (octagon / triangle / circle-i, in its hue) and, from two on, the count; its name and tooltip are the newest banner's title plus 另有 N 条提醒. A new banner becomes the page shown and opens the card by itself (a peek; focus stays where it is), with the card's right edge under the capsule's. A peek tucks back into the capsule after 6 s without the pointer or focus on the card (3 s once they leave; held while the window is hidden), unless an error is among the banners: then it stays until the user presses elsewhere, presses Esc in the card, or closes it. A press anywhere outside the card and the capsule tucks it; the capsule toggles it (from the keyboard, focus goes to the page shown). Closing the page shown brings the next (older) one. A banner replaced in place (same id) does not reopen the card.
- **Narrow fallback (< 1060 px wide; reachable only in the design build opened in a browser, e.g. a narrow preview panel or a phone; the shell window's minimum is 1060×700).** One breakpoint, `@media (max-width: 1059px)`, in `shell.css` and the page stylesheets: the sidebar becomes a band above the page (nav in a row, no digit hints; there is no sidebar status card, §4.5), `.app` is a flex column of exactly `100dvh`, and **`#host` stays the only scroller** (`overflow-y: auto; overflow-x: clip`); the document never scrolls. Fill pages behave as on the desktop (a very short host scrolls to the rest of a fill page, `min-height: 520px`), docked panels take the stage (a stage under 800 px gives the panel all of it), the log reader takes the whole log body when that body is under 760 px wide, the settings index becomes a wrapped row, and wide strips wrap instead of clipping. Verified at 375, 420, 760, 1000 and 1059 px.

### 4.4 The reference layout (总览)
One grid and one column line (`minmax(0,1.42fr) minmax(0,1fr)`, 1.2fr/1fr at ≤ 1180 px; column gap 32/28, row gap 24/16):
```
"hero  ctl"     当前层级 · the tier tag (34) · health hint      | 重启服务 on the column line · the run pill to the edge
"exit  deck"    当前出口 (frameless)                             | 回退顺序: the tier deck + the detached Gateway row
"traf  side"    流量 (frameless; the chart takes the leftover)   | the status strip (接管方式 · 熔断保护 · 节点) + 协议分布
```
- The hero names the tier by its code name at 34 px (F-03: no role word beside it; never a chip). Its hint states the tier's health in the node vocabulary (全部 2 个节点可用 / 1/2 个节点可用 / 已回退 · Vanguard 暂不可用) and, when the kill switch is off or not in effect, 「· 熔断保护已关闭 / 未生效」 in amber with an amber dot. On every other page the same warning is the brand dot: the shell sets `#app[data-ks]` and the 「豁然」 dot turns amber when the kill switch is off or not in effect (`data-ks` disabled / blocked / error) while the service is running or starting and the state is not stale, brick when it has triggered (whatever the run state; `css/shell.css` :20-27) (D20, page half; the tray icon carries the same state, C3). There is no sidebar status card (user decision of 09-29, G60). It does not repeat the exit's location: 当前出口 right below and the sidebar say it.
- The stopped deck states the real fallback order from store `bastion` and `ksEnabled`: 「启动后依次回退：Vanguard → Sentinel（→ Bastion when it is on）；都不可用时阻断出站」 (or 「…直接从本机发出」 with the kill switch off). Bastion's pool is neutral ink on standby; amber only while it carries traffic or is below its minimum.
- The left column tells the story (which tier → where traffic exits → how much flows); the right column is the control column. Every right-column object starts and ends on the column line, and the run pill is as wide as that column minus 重启服务 (fixed per window size, so it never moves under the cursor).
- **One framed object per role:** the deck (the only stacked, shadowed object), the status strip (one hairline frame) and the run pill. Content sections are frameless and sit on section heads (`.ov-head`, one 32 px band), so titles on a row share a baseline.
- The route line under 当前出口 and the Gateway row sit on the same line across the column line (both are `--dk-g` tall with a top hairline): 经由 … → 东京 Gateway 01 | Gateway 固定出口 当前 东京 Gateway 01.
- The strip's frame is drawn by its cells (each owns its sides and bottom), so when the mode confirmation opens inside 接管方式 and pushes the cells below, the frame travels with them.
- The strip's states: 接管方式 is two toggles that can both be on, 系统代理 and 虚拟网卡 (`.seg-multi`, `aria-pressed`; at least one stays on: switching off the only one that is on turns the other one on, data/static.js `modeAfterSwitch`). Without administrator rights 虚拟网卡 shows a lock glyph, and a press that would bring the TUN adapter up says why in the hint line (「虚拟网卡模式需要管理员权限」); while the saved mode has it on it can still be switched off (back to the system proxy); 熔断保护 reads 已布防 / 未生效 · 启动后自动布防 / 已触发 / 已关闭 / 未生效 · 需要管理员权限 (amber); 节点 counts your own + subscription nodes as dot rows, with Bastion's Garrison as a small meter (counted apart; its tip says only `Bastion · 45 可用`). The deck's Bastion band reads 未启用 · 公共节点，请勿登录, or `45 可用 · 待命` / `正在补充 · 12 可用` with a compact neutral meter (a pool of 60 is never 60 dots); stale, `45 可用 · 数据未更新`. Bastion is never a fraction (F-04 follow-up, 11-01): its target is not a total and its capacity is not a count, so the 层级 cell says `2 可用` and the Garrison panel `2 可用 · 目标 24`.
- Page builders: this is a reference for proportion and rhythm, not a template to copy. A list page is a toolbar plus one scroller; a settings page is a section index plus rows.

### 4.5 What the shell owns (don't duplicate it)
- The **top bar** (page title and 搜索与命令 Ctrl K), the brand's live light (the 「豁然」 dot follows the run state through `#app[data-run]` / `[data-ks]`; there is **no sidebar status card**, by the user's decision of 09-29: live state is read on 总览), the sidebar footer (version + the update state, rendered from store `update`; a click goes to 配置 · 应用) and the notice hosts (toasts in the top bar's centre, banners in the floating stack under the capsule at its right end).
- **The title bar is the page's (11-05; the user found the system caption out of place).** The shell's window has no system caption (`decorations(false)`; the DWM shadow, Win11 rounded corners and the resize edges stay). The band across the top of the window (the sidebar's brand row and the top bar, `--top-h`) is the title bar: it is marked `app-region: drag`, so WebView2 hands it to Windows as caption and dragging, double-click to maximize, Aero Snap and the system menu are the system's own. Every control placed in that band must say `app-region: no-drag` (shell.css does it for the top bar's buttons, toasts and the brand's mark; a new control there needs the same). The window buttons `#wc` (最小化 · 最大化/向下还原 · 关闭, `shellc/frame.js`, invoke `host_window`, frame `host:window{maximized}`) are fixed over the top right corner at `--z-frame`, above the launch cover and every overlay, out of the tab order like a system caption's. Each fills a 46 px cell to the window's edges (the corner pixel of a maximized window still closes it); what shows is the top bar's own shape: a rounded wash 36×30 (the 搜索与命令 button's height), glyphs on a 12 px grid in the icons' line. Close alone takes a hue, the clay fill, and only while pointed at. The top bar keeps `--wc-w` + 6 px free at its right end; a modal drawer opens under the band (`top: var(--top-h)`), as the docked one always did. Nothing else may be placed over the band's right end.
- The nav items stay on one line at every width: the label may shorten last, a badge a page adds after the label keeps its size, and the `Ctrl 1–4` hint floats over the item's right end, on the hovered or focused item only (all four appear while Ctrl is held for a moment).
- **Keyboard landing.** A navigation made from the keyboard (a link in a page, a banner action, the palette, Ctrl 1–4) never drops focus on `<body>`: focus goes to what the page names as its landing (`ctx.landing = el` in `enter()`, or `T.router.land(el)` once a glide has ended), else to the page itself (`tabindex=-1`, no ring), so the next Tab continues inside the page. 层级 lands on the tier's tab, 配置 on the control of the row it glides to. Focus already in the sidebar or the top bar stays there.
- Banner actions that navigate keep the banner until the page has really changed (`keep: true`, then `T.dismiss(id)` when `T.go()` returns true or on the next `page:change`): 配置 may hold the way with unsaved changes, and 留下 keeps the warning.
- Global keys: `Ctrl K` (palette), `Ctrl 1–4` (pages) and `Esc` (dismiss a toast; that Esc is then used up: it calls `preventDefault()`, so a page's own Esc listener checks `e.defaultPrevented` and does nothing more). The library adds `Ctrl F` (focuses the visible search field) and `Ctrl S` (saves when the save bar is up). Pages must not bind these keys.
- The shell only speaks for things that happen elsewhere: the 接管方式 toast is skipped on 总览 and 配置, where the control shows the result itself.
- Pages may add palette commands with `T.cmd.register({ id, group, label, icon, hint, keywords, run, when })`. Use `when: () => T.router.current === 'logs'` for page-only commands and group names such as `运行日志`. The palette shows each group once whatever order pages registered in (服务, 前往, then the others in registration order, 演示 last); `order` sorts inside a group. Typing ranks matches (label exact › prefix › word prefix › substring, then keywords, then hint and group) and the group holding the best match comes first. On open it pre-selects the first 前往 page that is not the one shown, never a command that changes state or leaves and runs something.

---

## 5. JavaScript API

### 5.1 DOM, events, store
```js
T.$(sel, root) / T.$$(sel, root)          // query / query-all (array)
T.esc(str) · T.raw(literal | T.icon(…))   // escape / mark trusted HTML: only a literal or an icon (§5.7)
T.html`<div>${value}</div>`               // → Element; strings escaped, a Node (T.tx, T.h) inserted as a node; tag use only
T.h('button', { class, text, style, dataset, on: { click }, onClick, 'aria-label': … }, ...children)   // no `html` key (§5.7)
T.on(evt, fn) → off · T.once · T.off · T.emit(evt, payload)
T.store.get(k) · set(k, v) · patch({…}) · update(k, fn) · sub(k, fn, { now }) → off · snapshot()
T.fmt.bytes(n) '7.29 GB' · bytesParts · rate '1.55 MB/s' · rateParts · ms(n) '42 ms' · ago(ts) (relative)
      time(ts) '09:12:03' · hm · clock(s) '02:14:13' · uptime(s) · pct · num
      latBand(ms, ceiling=800) 'ok'|'slow'|'down' · latWord(ms, ceiling)   // all through Intl and the fmt.* messages (§5.6)
T.util.clamp · lerp · rng(seed) · raf(fn)      // raf: coalesce to one call per frame
T.icon(name, size = 16, cls) → T.raw SVG · T.iconEl(name, size) → Element · T.icon.names
```
Icons (brand grammar: open round-capped strokes and one filled dot, on a 24-unit grid; tier silhouettes: Vanguard a flag, Sentinel a watch tower, Bastion a basin holding two dots (a temporary pool), Gateway one line ending in a pinned dot (the fixed final exit); modes: 系统代理 an arrow entering a window corner, 全局 a closed loop around a dot, 混合 two routes): navigation `overview tiers logs settings`; tiers `vanguard sentinel bastion gateway`; actions `start play stop power restart refresh copy check plus minus close trash edit search filter sort pause export import external link chevron chevron-left chevron-down chevron-up arrow arrow-down more grip eye eye-off undo bolt target wrap pin ban direct code list table history key user stack help`; domain `download upload shield killswitch mode-system mode-tun mode-mixed pool route globe pulse clock bell phone mail send folder lock lang`; severity `success info warning error dot`. Aliases include `locate→target`, `block→ban`, `diag→pulse`, `group→stack` and `delete→trash`.

### 5.2 Router, notices, overlays, palette
```js
T.page(id, def) · T.go(id, params) → true | false (held by beforeLeave) · T.router.current
  // navigation closes open menus, modeless panels AND modal overlays (a link inside a drawer never
  // leaves it open over the new page); pass keepOverlays: true to keep modal overlays
T.notify({ kind: 'success'|'info'|'warning'|'error', title, message, messageId, messageParams, actions: [{ label, primary, ghost, danger, keep, run }], sticky, timeout, id }) → { dismiss() }
  // title / message are plain text or a Node (T.tx); messageId + messageParams relabel in place on a language change
  // success/info → toast in the top-bar centre with a countdown ring (things that happened elsewhere)
  // warning/error → banner: a page of the floating card under the top bar's right end (covers, never pushes;
  //   one banner at a time, dots page between several; peeks, then tucks into the capsule in the top bar,
  //   §4.3); persistent; same id replaces in place
  //   (severity by shape + word: error = octagon + 错误, warning = triangle + 警告)
T.dismiss(id) · T.dismissToast() · T.notices.clearAll(instant)
T.confirm({ title, message, confirmLabel, cancelLabel, danger }) → Promise<boolean>      // never window.confirm
T.drawer.open(o) / T.drawer(o)   T.sheet.open(o) / T.sheet(o)
  o: { title, subtitle, width (px, or a function read on every placement when docked), height (sheet: fixed height,
       body scrolls), content: Element|html|fn(box, api),
       actions: [{ label, primary, ghost, danger, icon, left, keep, run(button) }], head: Element|html (head slot),
       from: Element (container morph from it; shrinks back into it on close), modal (drawer: false = side panel
       with no scrim; the page stays usable; closed on navigation), dock (modeless drawer: an Element, or a function
       returning one, to dock inside; see §6.15), focus, class, onClose(confirmed),
       beforeClose() → false | Promise<boolean> (asked by Esc, × and the scrim, not by api.close(): a
       half-finished edit asks 放弃未保存的更改？ like every other way out) }
  api: { el, head, body, foot, close(), setTitle(t), setSubtitle(t), setContent(c, { dir: -1|0|1, instant }),
         setActions(actions), action(i) → button, place() (re-place a docked panel), closed }
T.overlayOpen() → a MODAL overlay is open · T.overlays.closeModeless() · T.overlays.closeFloats()
  // while a modal overlay is open #app carries .has-modal: the save bar steps out of sight under it (§6.19)
T.cmd.register(cmd | [cmds]) · T.cmd.open() · close() · toggle() · isOpen
  // cmd: { id, group, label, icon, hint, keywords, run, disabled, when, mutates, runs, nav, order, tier }.
  // mutates: true marks a state-changing command (停止服务, 切换接管方式…) and runs: true one that leaves
  // the page and starts something (验证出口 IP); the palette never pre-selects either on open, so
  // Ctrl K → Enter can't cut the proxy. nav: the page a 前往 command goes to. order: its place in its
  // group. tier: the row shows T.ui.tierTag(tier) as its label.
T.router.land(el)   // focus el if the last navigation to this page came from the keyboard (see §4.5)
```
Esc closes the topmost thing in this order: an open menu or popover, then the top overlay. A search field with text clears first. A modeless panel ignores Esc typed into a field outside it.

### 5.3 `T.ui` — component behaviour
| Call | Returns | Notes |
|---|---|---|
| `busy(btn, job, { busy, done, fail, hold, failHold, stay, icon, busyIcon, doneIcon, failIcon, reserve })` | the job's Promise | `job` is a Promise or a function returning one. `done`/`fail` are strings or `fn(result/error)`; `fail` defaults to the catalog text of the error (`core.busy.failed` for `runtime_gone` / `cancelled` / `superseded`, else the rt error's `message_key`, else `error.message`), and `false` skips a face. It works on `.btn` (with or without a `.bi` icon slot) and on icon buttons (glyph morph plus aria-label). A click while busy joins the running job. The v1 form `busy(btn, { run, … })` still works. **Width (T-P5-10):** the button reserves the width of the widest face it knows, so it does not jump between states, but never more than its container gives it. The container is the button's parent (a sheet's footer, a list row's fixed action cell; the button never spreads over the row's other columns), then every box above it up to the first that clips: the reservation may not take the button, or a neighbour it pushes (a sheet's 取消), further out of any of them than it already was. The faces known at the click are reserved (the idle and busy faces, a string `done` / `fail`, and, when `fail` comes from the error, the short `core.busy.failed` word below). **The fail face:** when its whole words would take the button out of the container, the button shows the short word of `core.busy.failed` instead (by the error's code; `other` for every other error), and the whole words become the button's hover tip (`data-tip`, shown at once when the pointer still rests on the button or it has keyboard focus) and its accessible description (`aria-describedby`), so a screen reader reads the short word and the sentence together; both go away with the fail face and the button's own tip and description come back. When even the short word (with its glyph) does not fit, as in a list row's narrow action cell, the short word shows without its glyph (buttons without a `.bi` slot), then, last, the fail glyph alone, the short word staying the button's name (`.sr-only`) and the whole words its tip and description. Never an ellipsis on a reason, never a clipped face. Where the whole words fit, nothing changes. The busy and done faces have no such fallback: they rely on the catalog's short words (a `done` from a function is only capped in what it reserves). |
| `reserve(btn, labels)` | — | Fixes the button's width for every face before the first click (no width jump). Since T-P5-10 the reserved width has a ceiling, as in `busy()` above: never more than the button's container gives it (the reservation may not take the button, or a neighbour it pushes, further out of its parent or any box above it up to the first that clips), so a label wider than that room does not get its whole width reserved. |
| `seg(el, { value, onChange(v, prev) → false })` | `{ set(v, animate), get, place }` | `.seg > .seg-btn[data-v]`. ← → move focus and select (radiogroup); `onChange` may veto. `aria-busy="true"` on the `.seg` makes the thumb breathe while a change is in flight (the overview's mode switch). |
| `switch(el, { checked, onChange(v, prev) → false \| Promise, optimistic })` | `{ get, set, toggle }` | Markup alone works (see §6.3). A Promise holds the knob (breathing) until it resolves true. |
| `options(el, { value, onChange(v, prev) → false \| Promise })` | `{ get, set }` | Option cards. A Promise keeps the old choice until it resolves true. |
| `field(el, { validate(value, input) → '说明' \| null, hint, onInput })` | `{ validate(), error(msg), value, set(v) }` | Validates on blur, then live while invalid. |
| `number(el, { min, max, step, format, onChange })` | `{ get, set }` | Adds −/+ (hold to repeat) and ↑ ↓ (Shift ×10). |
| `search(el, { onInput(q), debounce, hotkey = true, placeholder })` | `{ count(visible, total), clear(), set(q), focus(), value }` | Ctrl F focuses the visible one. Esc clears, then blurs. |
| `select(el, { options: [{ value, label, hint, icon, disabled, lang, translate }], value, placeholder, placement, onChange(v, prev) → false })` | `{ get, set(v), setOptions(list) }` | A custom menu with the current value checked. An option's `lang` / `translate` (T-P5-07) go on its menu label, on the option itself when it has no hint or kbd, and, while it is the choice, on the shown value (`.select-v`). **Accessible name (T-P5-10):** closed, the button is named by its label and its current value: `aria-labelledby="<label> <.select-v>"`. The label is the caller's own `aria-labelledby`, else the button's `aria-label` (the button's own id comes first, so the aria-label keeps following the language), else its `<label for>`, else the `.field-lab` of the `.field` it sits in; with none of them the name is the value. Give a select one of these before calling `select()`: it reads the label once, so an `aria-label` set on the button afterwards takes the place of the whole name and hides the value. |
| `menu(anchor, items, { placement, minWidth, select, keyboard, gap, onClose })` | `Promise<item \| null>` plus `.close()` and `.el` | `anchor` is an Element or a rect `{left,top,right,bottom,width,height}` (context menus). Items: `{ label, value, icon, hint, kbd, checked, danger, disabled, run }`, `{ sep: true }` or `{ group: '名称' }`. `keyboard` defaults to how the menu was opened: Enter / Space highlights the first item. |
| `popover(anchor, content, { title, width, placement, class, onClose })` | `{ el, close(), place() }` | For custom floating content such as filters. |
| `closeFloats()` | — | Closes any open menu or popover. |
| `slider(el, { min, max, step, bigStep, value, label, format, bands: [{ to, cls }], marks: [{ v, label }], onInput, onChange })` | `{ get, set(v, animate) }` | `cls`: `ok`/`warn`/`err`/`mute`. `aria-disabled="true"` blocks pointer and keys. |
| `tabs(el, { value, onChange })` | `{ set, get, place }` | `.tab[aria-controls]` panels are shown and hidden for you. |
| `listNav(list, { rows = '.li', highlight = true, wrap, scroller, actions, onSelect(row, how), onOpen(row, how), onEscape })` | `{ get, select(rowOrIndex, { focus }), clear(), refresh() }` | `how` is `click`, `key` or `api`. Call `refresh()` after re-rendering rows (one pass: roles, `aria-selected`, stops); selection is kept by `data-key`. A select writes only the old and the new tab stop. `actions: true` (`.li-act button, .li-act a`) or a selector keeps row controls out of the tab order except on the selected row, so Tab leaves the list in one stop. Hidden rows are skipped by their flag, a hidden ancestor or `content-visibility: hidden` (no rect per row, so skipped chunks never render for a key press). The highlight is placed from layout offsets measured against the list (right inside paint-contained chunks and while rows glide). Fading copies of removed rows are never rows. For tables use `(tbody, { rows: 'tr', highlight: false })`. |
| `fold(root, { onToggle(toggle, open) })` | `{ toggle(el, open) }` | ×N folding (§6.12). Opening lets the rows below clear most of their travel (~100 ms) before the children fade in. |
| `sortable(list, { items, handle = '.grip', onEnd(items, from, to) })` | — | Drag to reorder, or Alt + ↑/↓ on the handle. While a handle is pressed or dragged no tooltip shows anywhere (rows move under the pointer). |
| `toc(nav, { scroller = #host, offset = 24, spyOffset, onChange(id) })` | `{ refresh(), set(id) }` | Section index with scroll spy. A click glides the section to `offset`. `spyOffset` (px, or a function read on every pass, e.g. `() => host.clientHeight * 0.5` for the window's midline) is the line the spy tests; default `offset`. The spy also looks again when sections change size without a scroll (a block opening, a list growing), once the glide has landed. |
| `spark(el, { values, min, max, tone: 'ink', area, dot, bands: [{ at, cls }], format(v, i), label })` | `{ update(values) }` | `null` values are gaps, drawn as down. |
| `saveBar({ count, effect, onSave, onUndo, doneText })` | the bar | See §6.19. `onSave` rejecting with `Object.assign(new Error(reason), { hold: true })` holds the save (validation) instead of failing it. |
| `meter(el, { value, max, min })` | `{ set(v, max, min) }` | |
| `progress(el)` | `{ set(0..1), pending(on), done() }` | |
| `status(key, { chip, label, last })` · `lat(ms, { last, ceiling, word })` · `dots(total, ok, { off, label, states, last })` · `tierTag(id, { link, icon, size })` · `kbd('Ctrl F')` · `skeleton(rows)` | `T.raw` HTML | Drop into `T.html` or template strings. `status(k, { last: true })` and `dots(…, { last: true })` are the last-known forms (§6.11); `dots(0, 0, { states })` draws one dot per status key. |
| `tech(text)` | `T.raw` HTML | The text escaped, with every IP (v4/v6, CIDR, :port), URL, host and domain wrapped in `.tech` (Geist Mono). Use it for any detail string that may carry a technical value (§6.12). |
| `callout({ kind, title, message, actions, small, icon })` · `empty({ icon, title, text, actions, row })` | Element | |
| `copy(btn, getText)` · `copyText(text)` → Promise · `hideTip()` | | |
| `saveBarCurrent` | the live bar or null | |

### 5.4 `T.motion`
```js
reduced                                   // live flag: the shell's __TESSERACT_MOTION__.reduced (TESSERACT_REDUCED_MOTION) OR prefers-reduced-motion
SP.press|soft|glide|settle → { easing, duration } · ease.out|in|inout|quart · springCurve(k, c)
anim(el, keyframes, opts)                 // WAAPI; 1 ms under reduced motion
onDone(animation, fn, fallbackMs)         // run once on finish/cancel (with a timeout fallback)
spring(el, { x, y, scale, sx, sy, rotate, opacity }, { k, c, instant, from, onRest })
                                          // JS spring writing translate/scale/rotate/opacity; velocity carries through re-targets
swapText(el, text, { dir: 1|-1, html, instant })   // overlapped vertical roll: the old label stays until the new one is in
                                          // old and new share one grid cell: the host becomes an inline grid,
                                          // or, when its own display wins (a flex row: .field-msg, .prov,
                                          // .dc-node), gets an inner .swap-box (decided on the first call).
                                          // Toggle classes on a host; never overwrite its className mid-roll.
odometer(el, text, { mode: 'forward'|'auto', label, instant })   // digit strips; aria-label = real value
tweenNumber(el, to, fmt, { dur, from, instant })   // one-off count (never for 1 Hz streams)
flip(els, mutate, { duration, easing })   // transform-only FLIP of given elements
stagger(els, { base = 30, step = 18, dur = 240, y = 8, max = 8 })   // entrance ≤ 400 ms
list(container, mutate, { key, stagger = 22, max, enter, leave, from, highlight, duration, enterDelay, followers })
                                          // enter / leave / reorder for a container's children (§7 rule 5):
                                          // a removed row fades where it was (120 ms) and whatever slides over
                                          // its place starts 70 ms later; enterDelay holds new rows back (ms);
                                          // followers: true | root Element glides what comes after the container
                                          // (later siblings, and its ancestors' later siblings up to root, default
                                          // the page) from where it was when the container changes height
wave.start({ x, y }, { speed = 0.5, max = 460, span = 1600 })   // F-06: one state change travelling outward from a point
                                          // (总览 opens it from the run pill when a real start ends). For `span` ms,
                                          // swapText and odometer hold an element's change for its distance
                                          // (speed ms per px, at most `max` ms); never opened under reduced motion
wave.delay(el)                            // ms el's change still waits (0 when no wave is open): for a page's own motion
                                          // (swapText takes { delay } to override it)
morph(src, target, { duration = 440 | 240 reversed, reverse, rect, land })   // container transform (drawers/sheets use it via `from`);
                                          // closing (240 ms, ease-in): the content is out by 35 % while the source's
                                          // face is in from 20–40 %, so the shrinking box is never empty
handoff(src, target, { rect, duration = 560 })   // shared element between pages (the router uses it for flipFrom): one
                                          // uniform scale centred on the target, travel over the whole duration on
                                          // SP.settle (~90 % in by ~240 ms), the ghost fades on its own track from 55 %
```

### 5.5 `T.rt`: the runtime client (read the store; never fake your own copy)

The prototype's `T.sim` is gone from the product (T-P4-07 removed `legacy_sim.js` and the `sim:*` aliases; `scan.mjs --rule demo` keeps `T.sim` / `sim:` out of the app bundle). Pages read **store keys** and listen to **`rt:*` events**; both are written by one place, the projection `src/rt/project.js` (v1 DTO → store keys, mapping in `映射_core` §1.2–1.3). Counts are always the runtime's own: there is no `recount()` / `usable()` in the page. The design target drives the same projection with v1 frames produced by `src/design/sim.js`, so design and product cannot drift apart.

Layers (`src/rt/`): transport (`transport-tauri.js` | `transport-fake.js`) → `client.js` (session, epoch, hello, `rt:reset`, `host:*`) + `calls.js` (req_id → Promise) → `topics.js` / `paging.js` (per `(epoch, topic, key)` cache) + `stale.js` → `project.js` (+ `project_overview.js`, `project_tiers.js`).

**API**
```js
T.rt.call(cmd, args, { owner, onProgress }) → Promise   // commands (job / res / private replies, PROTOCOL §2, §6)
T.rt.query(cmd, args, { owner }) → Promise              // same path for queries (config.snapshot, nodes.detail, app.info …)
T.rt.cancel(op | promise)                               // cmd:'cancel' with the original owner; the job's end state is the answer
T.rt.owner(label, { onRevoke }) → { id, revoke() }      // a page-local owner (drawer, side panel, pairing session):
                                                        //   revoke() rejects its in-flight promises with owner_revoked and drops late
                                                        //   frames; onRevoke sends the cancel the sidecar needs (P4 rule 15)
T.rt.feature('B-xx') → bool                             // both keys present (§5.8)
T.rt.can(cmd) · T.rt.has(topic) · T.rt.hostHas(invoke)  // hello.caps.commands / topics; host:caps.invokes (never infer a capability)
T.rt.topic(name, key?) → { get(), sub(fn) }             // the raw v1 DTO of a topic (lists keyed by tier), for pages that need rows
T.rt.requestResync(topic, key?)                         // ask the shell to redeliver a slot (rt_resync), rate-limited
T.rt.errorText(err) · T.rt.reasonText(ns, code, params) // RtError → message_key → errors.<code> → errors.unknown; reason_code → <ns>.<code>
T.rt.epoch · T.rt.diag()
T.rt.state                                              // client state: idle / connecting / reconnecting / resetting / live
T.rt.hello                                              // null until hello; then { app_version, lang, caps } (seed a page only once it is set)
T.rt.RtError                                            // the error class for promises a page rejects itself (e.g. new T.rt.RtError('not_allowed'))
```
Members outside this list are not for pages: `T.rt.connect(transport)` (boot only), `T.rt.transport` (diagnostics), `T.rt.reqId()`, and `T.rt.invokeHost(name, args)`, the raw shell invoke behind a whitelist of nine names (`HOST_INVOKE_NAMES` in `rt/client.js`: `rt`, `rt_connect`, `rt_resync`, `host_notify_ready`, `host_notify_rendered`, `host_confirm_quit`, `host_set_page`, `host_force_restart`, `host_ready`; any other name rejects with `not_allowed`). Only `shellc/` calls `invokeHost`; pages go through `T.shellc`.
`T.ui.busy(btn, T.rt.call(…))` shows progress and the result on the pressed control as before; after 10 s a running job reads 「仍在处理…」 (no client timeout: deadlines are the sidecar's).

**Store keys** (read-only for pages): `run` (`unknown` / `stopped` / `starting` / `running` / `stopping` / `failed`) · `phase` · `cancelling` · `restarting` · `uptime` · `failure` · `readiness` · `core` · `carrier` · `order` · `fallback` · `degraded` · `collected` · `tiers {id: {ok, total, enabled, state}}` · `pool` · `bastion` · `mode` · `modeKnown` · `modeBusy` · `admin` · `systemProxy {proxy_state, drift}` · `killswitch` (`armed` / `inactive` / `triggered` / `disabled` / `blocked` / `error`; armed only while running, triggered = `triggered ?? active`) · `ksEnabled` · `ksBlocked` · `exit` · `traffic` · `history` · `route` · `gateway` · `protocols` · `update` · `configRev` · `stale {topic: bool}` · `caps` · `hostCaps` · `lang` · `recovery` · `appVersion` · `presence`.

**Events** (every runtime event carries `instant`: true for the first frame of a topic after a connect or an `rt:reset`, and before `rt:replayed`; an instant frame brings no edge notice and no light-up):

| Event | Payload |
|---|---|
| `rt:state` | `{ run, prev, cancelled, instant }` |
| `rt:phase` · `rt:collect` · `rt:restart` | `{ phase }` · `{ tier }` · `{ state: 'begin'\|'done' }` |
| `rt:tick` · `rt:history` | `{ t, sample, traffic, running }` · `{ history, gap }` |
| `rt:tier` | `{ kind: 'fallback'\|'recover'\|'degraded', carrier, from?, wasFallback?, observed: true, source }` (the only source of the fallback banner; pages never derive `kind`) |
| `rt:tiers` · `rt:transition` · `rt:route` · `rt:gateway` · `rt:bastion` · `rt:protocols` · `rt:exit` · `rt:killswitch` · `rt:mode` · `rt:update` · `rt:config` · `rt:core` · `rt:job` | the matching store keys changed (`rt:mode {mode, busy}`, `rt:job {job}`, `rt:config {rev, sections, pending}`) |
| `rt:logs` · `rt:log-gap` | log batches and gaps for the log buffer |
| `rt:gap` | `{ topic, key, data, instant }`: a gap in an event-only topic (`topics.js`; a state topic's gap marks it stale instead). For `tier.transition` the client also tells the projection, which reconciles the next `tiers.summary` by level (`client.js` :76); pages normally do not listen to it |
| `rt:stale` | `{ topic, stale }` (`stale.js`: a region shows 「数据未更新」 and keeps the last value) |
| `rt:slot-stale` | `{ epoch, topic, key, reason, exhausted? }` from the shell's `rt:stale` (list slot resync; the shell layer shows 「数据可能过时 · 重新加载」) |
| `rt:reset` | `{ reason, epoch, generation_changed }`, see below |
| `rt:conn` · `rt:caps` · `rt:replayed` · `rt:bye` · `rt:fatal` | session events |

**Two kinds of `rt:reset`** (criticism B9): `generation_changed: true` (sidecar restarted: `runtime_gone` or a new epoch) rejects in-flight promises with `runtime_gone`, clears caches and private results, sets `run='unknown'` and every state topic stale until its first snapshot; a page drops what belonged to the old generation (log buffer, undo, selection, open drawers). `generation_changed: false` (same-epoch reconnect of the page channel) changes nothing: replayed frames are de-duplicated, pending promises stay.

**Stale.** Changing topics are not judged by silence: `service.state` is stale after 3 s without a frame while starting / running / stopping; `traffic.tick` only while the core runs; other state topics on a new epoch without snapshot, a gap, an unfinished page group, a `base_rev` mismatch or a generation mismatch. Logs and jobs are never stale (sequence numbers and gaps instead).

**Known limit (第五波 31b-19).** `nodes.detail` answers from the sidecar's last `nodes_latest`, which only a live `nodes.list` subscription keeps fresh. The shell subscribes every page topic after hello, so in the app it is fresh; a page must still prefer the list row when it is newer (`pickNewer`).

### 5.6 i18n: `T.t` / `T.tx` / `T.ti`

- **No literal user text in code.** `scan.mjs --rule cjk` (enforce) fails on CJK in JS strings, templates and regexes, in `index.html` text and in CSS `content:`; comments are not checked; `src/rt/**` and `src/shellc/**` never carry CJK. Data text (node, subscription and channel names, log text, IPs, protocol names, cities) is rendered with `translate="no"`; the leak checks only look at nodes without it.
```js
T.t(id, params?) → string                        // title / aria-label / placeholder / plain text (tags dropped, words kept)
T.tx(id, params?, slots?) → DocumentFragment     // rich text: tags become elements, params are text nodes
T.ti(el, id, params?, { attr }?) → el            // writes a static text (or one attribute) and registers it for in-place relabel
T.i18n.lang · T.i18n.set(lang) · T.i18n.onChange(fn) · T.i18n.has(id)
```
- **Message ids**: `<ns>.<block>.<item>[.<variant>]`, lower case, `_` inside a segment; suffixes `.title .desc .label .hint .aria .action .toast .confirm.{title,body,ok} .empty.{title,body}`. Namespaces: `common shell core fmt tier mode overview tiers logs settings alerts errors catalog`. Never the Chinese text as a key. Backend `message_key` values are ids as they are (`errors.*`, `catalog.*`); reason codes live in the page namespace (`tiers.reason.*`, `overview.exit_reason.*`, `alerts.test_reason.*`, `alerts.onboarding_reason.*`, `shell.host_reason.*`, `common.skipped.*`), each covered by a hard gate.
- **Whole sentences**: one message per sentence or per place (a state that changes the words in one place is one `select`), never fragments glued in code. ICU subset: `{x}`, `{n, number}`, `{t, date|time}`, `plural` (with `#`, `=0`, `offset:`), `select`; no `selectordinal`, no skeletons, nesting ≤ 3. `other` is always required.
- **Tag placeholders** instead of HTML: `<b> <em> <code> <kbd> <mono>` and caller-rendered slots `<tier> <link> <x>` (no attributes). `T.tx` builds DOM, nothing is parsed as HTML, so a translator gets the whole sentence and a parameter can never become markup.
- **`T.fmt` goes through `Intl`** (`NumberFormat`, `RelativeTimeFormat`, `DateTimeFormat` h23, `DisplayNames` for regions, `ListFormat`, `Collator` for sorting) with units and spacing from the `fmt.*` messages; caches are per language and cleared on a switch. Never `localeCompare(…, 'zh-CN')` or hand-written units.
- **Language source**: only `hello.lang`, then `host:language`; the page never switches optimistically. A switch relabels `T.ti` nodes in place, reruns store-driven painters (they subscribe `lang`) and, for pages declaring `relocalize: 'rerender'`, re-renders with `snapshotState()` / `restoreState()` so drafts survive.
- **Catalog files**: zh_CN `{ id: { m, _desc } }` is the source; the other languages are flat `{ id: text }` plus `_meta.json` `{ id: { src_hash, reused_from?, needs_review? } }`. `src_hash` = first 16 hex of sha256 of the zh_CN `m` (`i18n_check.mjs` `srcHash`); a translation whose `src_hash` no longer matches counts as missing.
- **Freeze**: `src/i18n/frozen.json` `{ id: src_hash }` holds every zh_CN id since the P4 exit (T-P4-12 froze the last namespaces). Changing or deleting a frozen id needs the matching `frozen.json` change in the same edit and a line in the ticket report; `i18n_check` fails otherwise.
- **Gates**: `node ui/tools/i18n_check.mjs --level p4|p7` (levels in `tools/i18n_gates.json`; missing, orphans, glossary and truncation are reports at p4 and hard from p7). Pseudo language (`--pseudo`): zh_CN base, 3 letters per CJK character + 10 %, short strings (≤ 2 characters) 5 letters per character and at least 4 letters longer; `⟦ ⟧` marks a message. `--i18n-trace` builds tag nodes with `data-i18n-id` for the render matrix's id → screenshot index.

### 5.7 Untrusted text

- **Untrusted sources**: node, subscription and public-source names; log lines; alert bodies; `host:notify` title / detail; release notes; error params; country codes; the pairing QR. They are text, always.
- **Rules (`scan.mjs --rule inject`, enforce since T-P4-07; exceptions only in `tools/inject_allow.json`, each with a reason and a line digest that lapses when the line changes; two today: the `T.html` and `T.icon` implementations)**:
  1. `innerHTML` / `outerHTML` / `insertAdjacentHTML` / `document.write` only with a literal without interpolation;
  2. `T.h`, `swapText`, `T.notify`, `T.drawer`, `T.sheet`, `T.popover` take no `html` key, and `content` / `head` / `message` are never an interpolated string (pass a Node from `T.html` / `T.tx` / `T.h`); aliases of these helpers are followed;
  3. `T.raw()` takes a literal or `T.icon()` only; `T.html` is a template tag only (any call form is refused, also at run time);
  4. no `eval`, `new Function` or string `setTimeout` / `setInterval`;
  5. no `setAttribute('on…')`; no `style` attribute (`scan.mjs --rule style`, CSP).
- Release notes render as plain text; the QR is an `<img src="data:image/svg+xml;base64,…">`, never inline SVG; private results (`nodes.reveal`, pairing) stay in the promise and the open panel, never in the store, an attribute or the diagnostics ring.
- The malicious fixtures (`ui/test/fixtures/malicious/`, render cases `test/render/cases/malicious.mjs` and the pages' own malicious cases) replay `<img onerror>`, `<script>`, `"><svg onload>`, `javascript:` links, ICU-sensitive `{ } ' #`, bidi controls and long strings through every listed field and assert that nothing executes and the text shows verbatim.

### 5.8 Prototype-only controls: `T.rt.feature('B-xx')` and the two keys

- A control that the prototype has but the backend cannot serve yet (a B item of the backend list) is **kept** in the page code inside `if (T.rt.feature('B-xx')) { … }`; it is not deleted and not wrapped in `__DEMO__` (P4 rule 14). Without both keys the page shows the item's fallback (hidden or read-only, as registered).
- **Key 1**: the sidecar lists the capability name in `hello.caps.features`. **Key 2**: `ui/src/features.json` (`ENABLED_H_FEATURES`, built in as `__H_FEATURES__`, empty at the P4 exit); Claude adds a name only after the user calls that item ready, and records it in `.build/CLAUDE_CHANGES.md`.
- Names are the backend list numbers (`'B-31'`); every name is registered in `src/rt/feature_registry.js` (`{name, b_item, page, controls, fallback}`); `scan.mjs --rule features` fails on an unregistered name in code or in `features.json`, and on a registered item of a finished page that no `T.rt.feature` call uses.
- Contract-field gaps (K-x / R-x) are not features: they open on field presence only ("shown when the field is there").
- Render cases check both sides: the default build hides the controls; a `--build-flags "--h-features all"` design build with a fixture declaring every B item shows them.

---

## 6. Components (markup, classes and a minimal example)

Every interactive element has hover, a press, a solid 2 px accent `:focus-visible` ring and a disabled state.
- **Press = scale.** Buttons (all `.btn` variants), `.icon-btn`, `.link-btn`, `.seg-btn`, `.chip-btn`, `.copy`, `.demo-link`, `.fchip`, `.select`, `.num-b`, `.tab`, `.ttag-btn`, `.switch` (its knob widens), option cards, checkboxes and radios, the nav items, the command button, the run pill, the tier deck cards, the Gateway row and anything with `[data-press]` drop to `scale: var(--press, .965)` on the next frame and spring back without overshoot on release. Wide row-like buttons (the status-strip cells) scale their content (`.cell-in`), not their frame.
- **Press = wash only** for row-like items, on purpose: menu items, palette rows, list rows, toc links and table rows. A scaled row reads as a glitch.
- An element that declares its own `transition` list (deck cards) adds its own `:active` rule with the same `--press` / `--dur-press` pair.

### 6.1 Buttons
```html
<button class="btn btn-primary" type="button"><span class="bi">${T.icon('import', 15)}</span><span class="lab">导入</span></button>
<button class="btn">编辑</button>                           <!-- secondary = the default .btn -->
<button class="btn btn-ghost">取消</button>
<button class="btn btn-danger">删除节点</button>            <!-- inside a confirmation only -->
<button class="btn btn-danger-soft">停用</button>           <!-- a destructive entry point in context -->
<button class="btn btn-icon" aria-label="更多操作" data-tip="更多操作">${T.icon('more')}</button>
<button class="icon-btn" aria-label="复制地址" data-tip="复制地址" data-tip-kbd="Ctrl C">${T.icon('copy')}</button>
<button class="link-btn">查看全部${T.icon('chevron', 14)}</button>
<button class="btn btn-primary">保存<kbd>Ctrl</kbd><kbd>S</kbd></button>
```
Sizes: `.btn` 32 · `.btn-sm` 28 · `.btn-xs` 24; add `.btn-block` for full width. Group buttons with `.btns` (gap 6). Demo-only controls (`.demo-link`, styled in `css/design.css`) exist only in the design target, inside `if (__DEMO__)`; prototype-only product controls follow §5.8. Use **one primary per region**. A label is a verb of 2–4 characters.

Busy → done / fail, on the button:
```js
T.ui.reserve(btn, ['检测中', '118 ms', 'TLS 连接失败']);          // optional: no width change on first click
btn.addEventListener('click', () => T.ui.busy(btn, () => diagnose(node), {
  busy: '检测中', done: ms => `${ms} ms`, fail: err => err.message,   // bounded reasons: 域名解析失败 / 连接超时 / 连接被拒绝 / TLS 连接失败 / 延迟超过上限
}));
```

### 6.2 Segmented control
```html
<span class="seg" aria-label="级别"><button class="seg-btn" data-v="all">全部</button><button class="seg-btn" data-v="issue">问题</button><button class="seg-btn" data-v="error">错误</button></span>
```
Variants: `.seg-lg` (28) and `.seg.block` (full width, equal parts). Content may include `.ic` and a `.n` count. Wire it with `T.ui.seg(el, { value, onChange })`; returning `false` vetoes, for example while waiting for an inline confirmation (add `.is-pending` to the asked button, as 总览 does). Use a segmented control for 2–4 mutually exclusive views or filters. For more choices use a select; for choices that need explanations use option cards.

### 6.3 Switch
```html
<label class="toggle"><span class="toggle-t">允许局域网连接<small>其他设备可以通过本机 7890 端口使用代理</small></span>
  <button class="switch" type="button" role="switch" aria-checked="false"></button></label>
```
Off, the track is `--c-switch-off` with a `.22` ring and the knob carries a `.38` ring (3.2:1 against white): an off switch never reads as empty space. Markup alone toggles `aria-checked` and fires a bubbling `change` event (`e.detail.checked`); listen on any ancestor. `.switch.sm` is the small size. Use `T.ui.switch(el, { onChange: v => v || T.confirm({…}) })` when turning something off needs confirmation. Add `data-manual` to opt out of the automatic toggling.

### 6.4 Checkbox and radio
```html
<label class="check"><input type="checkbox" checked><span class="check-t">只看本次运行<small>说明（可选）</small></span></label>
<label class="radio"><input type="radio" name="dns" value="enc"><span class="check-t">加密 DNS</span></label>
<div class="checks">…</div> <div class="checks row">…</div>
```
These are native inputs, so `checked`, `indeterminate`, `disabled` and `change` all work as usual.

### 6.5 Option cards (radio cards; unavailable options say why)
(11-04: 接管方式 itself is no longer option cards but two switches on 配置 and two toggles on 总览; the example below shows the component with its former first use.)
```html
<div class="opts" style="--opt-cols:3">
  <label class="opt"><input type="radio" name="mode" value="system" checked>
    <span class="opt-head">${T.icon('mode-system', 18)}<span class="opt-t">系统代理</span></span>
    <span class="opt-d">只接管支持系统代理的应用。</span></label>
  <label class="opt is-gated"><input type="radio" name="mode" value="tun" disabled>
    <span class="opt-head">${T.icon('mode-tun', 18)}<span class="opt-t">全局模式</span></span>
    <span class="opt-d">接管全部流量（TUN）。</span>
    <span class="opt-gate">${T.icon('lock', 13)}需要管理员权限 · 以管理员身份重新打开 Tesseract 后可选</span></label>
</div>
```
```js
T.ui.options(el, { value: S.get('mode'), onChange: v => S.get('run') === 'running'
  ? T.confirm(Object.assign({ confirmLabel: '切换' }, T.data.modeSwitch(v)))   // one wording for 总览, 配置 and the palette
  : true });
```

### 6.6 Fields (text, textarea, number) and inline validation
```html
<div class="field">                                   <!-- add .is-changed for the unsaved dot -->
  <label class="field-lab">混合端口 <span class="opt-l">可选说明</span></label>
  <div class="input num"><input value="7890"><span class="input-unit">端口</span></div>
  <div class="field-msg">1024–65535，默认 7890</div>    <!-- the hint; errors replace it in place -->
</div>
<div class="input mono"><span class="input-pre">域名后缀</span><input placeholder="example.com"></div>
<div class="input area mono"><textarea rows="4"></textarea></div>
<div class="fields" style="--f-cols:2">…fields…</div>
```
```js
T.ui.number(T.$('.input.num', el), { min: 1024, max: 65535 });
T.ui.field(el, { validate: v => +v === 25500 ? '与控制端口 25500 冲突，请换一个端口' : null });
```
Variants: `.input.sm` (28), `.input.mono`, `.input.area`, `.input.num`, and disabled or read-only inputs. Field edges (input, search, select) rest at `--c-line-3` and darken to `--c-line-4` on hover; `--c-line-2` is for hairlines and secondary buttons. An error message names the problem and the fix, and shows the octagon icon. The message line swaps hint ↔ error in one cell (nothing moves sideways); an error longer than the hint may add a line, and the field grows by exactly that line. Focus is a solid ring (1 px accent inside + 1 px outside); an invalid field's ring is brick.

### 6.7 Search
```html
<label class="search" style="width:240px"><input placeholder="搜索节点、地区、协议"></label>
```
```js
const s = T.ui.search(el, { onInput: q => { const n = render(q); s.count(n, total); } });
```
The icon, the 可见/总数 count, the clear button and the Ctrl F keycap are added for you. `.search.sm` is the small size. Only one search per screen should take Ctrl F; pass `hotkey: false` on the others.

### 6.8 Select (custom menu)
```html
<div class="field">                                   <!-- named by its .field-lab, then its value -->
  <label class="field-lab">DNS 模式</label>
  <button class="select" style="width:220px"></button>     <!-- .sm · .quiet (borderless, for toolbars) -->
</div>
<button class="select quiet sm" aria-label="排序"></button>  <!-- no visible label: an aria-label -->
```
A select always has a label, set before `T.ui.select()` runs (§5.3 `select()`): the `.field-lab` of the `.field` it sits in, a `<label for>`, an `aria-label`, or an `aria-labelledby` pointing at the text beside it. When it has more than one, core takes the first of `aria-labelledby`, `aria-label`, `<label for>`, `.field-lab`, in that order (§5.3). Its accessible name is that label followed by the current value.
```js
T.ui.select(el, { value: 'enc', options: [{ value: 'sys', label: '系统 DNS' }, { value: 'enc', label: '加密 DNS', hint: '推荐' }], onChange: v => … });
```

### 6.9 Slider
```html
<div id="ceil"></div>
```
```js
T.ui.slider(T.$('#ceil'), { min: 100, max: 5000, step: 50, value: 800, label: '延迟上限', format: v => `${v} ms`,
  marks: [{ v: 100, label: '100 ms' }, { v: 5000, label: '5000 ms' }], onInput: v => preview(v), onChange: v => draft('ceil', v) });
// banded track: bands: [{ to: 400, cls: 'ok' }, { to: 800, cls: 'warn' }, { to: 1600, cls: 'err' }]
```

### 6.10 Tabs
```html
<div class="tabs"><button class="tab" data-v="nodes" aria-controls="p-nodes">节点<span class="badge">12</span></button>…</div>
<div id="p-nodes">…</div>
```
`.tabs.sm` (32) is the small size. The underline is ink, not accent. Use tabs for sibling views of one object. Use a segmented control for filters.

### 6.11 Status vocabulary, latency, severity, badges, chips, tier tags
| Key | Word | Shape |
|---|---|---|
| `ok` | 可用 | ● filled slate-blue dot, word ink-2 |
| `slow` | 较慢 | ◐ half-filled sand dot, word umber |
| `down` | 不可用 | ○ heavy (2 px) clay ring, word clay, bold |
| `pending` | 待检查 | dashed grey ring, word in the voice |
| `recheck` | 待复检 | ↻ small arrow, word in the voice |
| `draft` | 草稿 | ◇ hollow diamond, word in the voice |
| `current` | 当前 | ◉ accent dot with a halo, word accent, bold, tracked |
| `off` | 未启用 | thin hollow grey ring |

```js
T.ui.status('slow')                  // <span class="st st-slow">较慢</span>
T.ui.status('ok', { chip: true })    // .st.chip-st: 22 px chip form (headers, drawers)
T.ui.lat(620)                        // .lat.lat-slow "620 ms"  (≤400 正常 ink · ≤800 较慢 umber, bold · >800 不可用 clay, bold)
T.ui.lat(null, { last: 214 })        // the last-known value, struck through and faint; the status column says 不可用
T.ui.status('ok', { last: true })    // .st.st-ok.st-last "上次可用": the service is stopped, so a result is the last one
                                     // (same shape, dimmed, and the word says so; ok / slow / down only)
```
- **Last known (service stopped):** statuses use `{ last: true }` (上次可用 / 上次较慢 / 上次不可用) and dot rows use `T.ui.dots(…, { last: true })`: the same shapes in `--c-dot-dim`. A stopped page never shows a live-looking green.
- Log severity and notices share one vocabulary: 信息 / 正常 / 警告 / 错误 (a warning banner says 警告, an error banner 错误).
- Log severity: `<span class="sev sev-warn">警告</span>`, one of `.sev` (信息, dot), `.sev-ok` (正常, circle-check), `.sev-warn` (警告, triangle), `.sev-error` (错误, octagon) and `.sev-debug` (调试, dash). Information rows are lighter (`.li.is-dim`); problem rows keep full ink.
- Badges: `.badge` (count), `.badge-accent`/`-ok`/`-warn`/`-err`/`-solid`; a warn / err badge keeps the well and says it in its digits' hue.
- Chips: `.chip` (well), `.chip-ok`/`-warn`/`-err`/`-accent`/`-line`, `.chip.sm`; a state chip says its state in a bold muted word, not with a tint (F-02). Chips are for data and tokens (a removable address, a rule's action), not for state tags. Removable: `<span class="chip chip-line">localhost<button class="chip-x" aria-label="移除 localhost">${T.icon('close', 12)}</button></span>`.
- Pills: `.pill` with `.sdot` (`pill-ok`/`-warn`/`-err`/`-line`), used for run-state words (承载流量, 已回退). Since F-02 a pill is a **mark**: no fill, no padding, a tracked word with its dot; `pill-ok` (承载流量) is the accent, `pill-warn` umber, `pill-err` clay, both bold, `pill-line` ink-3.
- Filter chips: `<button class="fchip" aria-pressed="true">出口<span class="n">9</span></button>` inside `.chips`. They toggle on their own and fire `change`.
- Status dots: `.sdot.ok|warn|err|off|fail|busy`. Dot rows: `T.ui.dots(5, 4)`, where a failed node is a heavy hollow clay ring and a missing tier is a dash. Per-node shapes: `T.ui.dots(0, 0, { states: ['ok', 'slow', 'down', 'pending'] })` → `i` (filled slate blue) · `i.slow` (half-filled sand) · `i.fail` (heavy hollow clay ring) · `i.pend` (dashed grey ring: 待检查 / 待复检) · `i.off` (thin grey ring: 草稿, off); add `last: true` for the dimmed last-known row (`i.last`). A pool of dozens of places (Bastion's 60) is a compact `.meter` with `ok/total · 待命`, never a dot row.
- Tier tag: `T.ui.tierTag('bastion')` renders `${icon} Bastion` (the code name at 600); `{ link: true }` makes it a button that jumps to 层级 with that tier, `{ icon: false }` fits it into running text. It is **the one way a tier is named anywhere**: the hero (at 34 px), the palette (`cmd.tier`), the deck, the 层级 switcher and sentences. F-03 (user 10-28): there is no role word beside the name any more (「自建 · 订阅 · 应急 · 固定出口」 were the product's advice on what to put in a tier); 「·」 separates facts (「Sentinel · 首尔 01」).
- A pool on standby (Bastion's) is a neutral `.meter.ink` with a neutral pill (待命, the deck's word); sand only while it carries traffic or is below its minimum.

### 6.12 Dense list
```html
<div class="list" style="--cols: minmax(160px,260px) 64px 96px 76px 64px minmax(0,1fr) 28px">
  <div class="li two" data-key="v-hk03">
    <span class="li-main"><span class="li-t">香港 IEPL 03</span><span class="li-sub">Vanguard</span></span>
    <span class="c-dim c-ell">香港</span><span class="c-dim c-ell">Hysteria2</span><span class="c-num">${T.ui.lat(42)}</span><span>${T.ui.status('current')}</span>
    <span class="li-act"><button class="icon-btn sm" aria-label="更多操作">${T.icon('more', 14)}</button></span>
  </div>
</div>
```
```js
const nav = T.ui.listNav(listEl, { actions: true, onSelect: (row, how) => reader.show(row.dataset.key, how), onOpen: row => openDetail(row), onEscape: closeReader });
// after re-rendering rows: nav.refresh();
```
- **Tab order:** the list is one stop (its selected row). Pass `actions` so the selected row's own controls follow it in the tab order and every other row's controls stay out of it (reachable by mouse, ↑ ↓ and the row menu); Tab then leaves the list for the next region in a stop or two.
- **Keep the data beside the name.** Cap the name column (`minmax(160px, 260px)`), put the data columns right after it and end the template with a flexible spacer before the actions (`… minmax(0,1fr) 28px`). `.li-act` always takes the last column, so the spacer column simply stays empty. A `1fr` name column would push the data ~460 px away at 1280.
- **Technical values are mono, even inside a sans row.** Any IP, port, host, domain or URL in a row, a detail line or a message goes through `T.ui.tech(text)` (or `.mono` / `.c-mono` when you place it yourself): in Instrument Sans `127.0.0.1:25500` reads as `127.0.01:25500`.
- Classes: `.list.dense` (30 px rows), `.li.two` (48 px, two lines), `.li.is-dim`, `.li.is-off` (a disabled node, content at 55 %), `.c-mono`, `.c-num`, `.c-dim`, `.c-ell`, `.li-act` (actions that appear on hover, focus or selection), `.list-g` (sticky group label), `.list-sep` (run separator 「运行开始 · 09:12:03」).
- Folding ×N: the toggle row gets `data-fold="f1" aria-expanded="false"` and contains `${T.icon('chevron', 14, 'fold-chev')}` plus a `<span class="badge">×30</span>`. Child rows get `data-fold-of="f1" hidden` and `.fold-kid`. Call `T.ui.fold(listEl)` once.
- New rows arriving (live logs, an import): `T.motion.list(listEl, () => prepend(rows), { highlight: true })`. When the user has scrolled up, don't move the content; show a 「12 条新日志 ↓」 pill instead.

### 6.13 Dense table
```html
<div class="tbl-wrap" style="flex:1 1 0;min-height:0">       <!-- in a fill page; a table on a scrolling page takes max-height (never a fixed height with an empty tail) -->
  <table class="tbl dense"><colgroup><col style="width:86px"><col style="width:74px"><col style="width:92px"><col></colgroup>
    <thead><tr><th>时间</th><th>级别</th><th>来源</th><th>消息</th></tr></thead>
    <tbody><tr data-key="…"><td class="c-mono c-time">09:31:40</td><td><span class="sev sev-warn">警告</span></td><td class="c-ell">Mihomo</td><td class="c-mono c-wrap">dial tcp api.example.com:443: i/o timeout</td></tr></tbody></table></div>
```
The header is sticky and the raw text is verbatim. Folding rows use `tr.fold` / `tr.fold-kid` with the same `data-fold` attributes. Keyboard: `T.ui.listNav(tbody, { rows: 'tr', highlight: false })`.

### 6.14 Key / value
```html
<dl class="kv lined" style="--kv-w:72px"><div><dt>协议</dt><dd>Hysteria2 → Shadowsocks</dd></div><div><dt>地址</dt><dd><span class="mono">hk03.•••••.example.net:443</span></dd></div></dl>
```
Mask hosts by default and reveal them with an `eye` icon button. Values can hold chips, status marks and copy buttons (`T.ui.copy`).

### 6.15 Drawer, side panel, sheet, confirm
```js
T.drawer.open({ from: row, title: n.name, subtitle: 'Vanguard · 香港', width: 440,
  head: String(T.ui.status('ok', { chip: true })),
  content: box => { box.innerHTML = …; T.ui.spark(T.$('[data-spk]', box), { values }); },
  actions: [{ label: '删除', danger: true, left: true, keep: true, run: () => T.confirm({ title: `删除「${n.name}」？`, message: '删除后需要重新导入才能恢复。', confirmLabel: '删除', danger: true }) },
            { label: '编辑', keep: true, run: startEdit }, { label: '完成', primary: true }] });

const reader = T.drawer.open({ modal: false, width: 380, title, content });   // side panel: no scrim; list stays usable
reader.setContent(next, { dir: 1 });                                         // follows ↑ ↓ in the list

// docked inside a frame (运行日志's reader): top, bottom and right edge are the element's (layout offsets,
// transforms ignored), the layer is clipped to it (the panel slides in from under its right edge), square
// with a left hairline instead of the float shadow; it re-places on resize and on the element's size change
// (banners float since F-05 and never move it). width may be a function.
const rd = T.drawer.open({ modal: false, dock: bodyEl, width: () => (innerWidth <= 1180 ? 344 : 384), title, content });

T.sheet.open({ from: importButton, title: '导入节点', subtitle: '粘贴链接、Clash YAML 或订阅地址', width: 600, height: 470, content, actions });
```
**One side-panel look.** A non-modal detail panel for the selected row is always the docked drawer: 运行日志's reader docks in the log panel's body, 层级's node panel docks in the stage (`dock: () => stageEl`), so both end on the page gutter, are square and carry the left hairline instead of the float shadow. A panel that holds an edit passes `beforeClose` so Esc and × ask 放弃未保存的更改？ like every other way out.
Use a drawer for detail about one item (node, channel editor, log reader). Use a sheet for a short task with steps (import). Use `T.confirm` for irreversible acts, with the act named in the button (删除 / 放弃更改 / 关闭保护), never 确定.

### 6.16 Menus, popovers, tooltips
```js
T.ui.menu(button, [{ label: '连通诊断', icon: 'pulse' }, { label: '复制地址', icon: 'copy', kbd: 'Ctrl C' }, { sep: true },
  { label: '删除', icon: 'trash', danger: true, run: () => … }], { placement: 'bottom-end' });
T.ui.popover(button, (el, api) => { el.append(filterChecks); }, { title: '显示这些分类', width: 220 });
```
Tooltips: `data-tip="…"`, with `data-tip-kbd="Ctrl K"` for keycaps and `data-tip-pos="bottom"`. A tooltip names the action or explains an abbreviation; it never holds information needed to decide something. Icon-only buttons always carry `aria-label` and `data-tip`.

### 6.17 Inline banners (callouts) and notices
```js
T.ui.callout({ kind: 'warning', title: 'Bastion 使用公共节点，请勿登录任何账号', message: '只在 Vanguard 与 Sentinel 都不可用时启用。', actions: [{ label: '启用', primary: true, run }] })
```
Markup: `.callout.callout-info|success|warning|error` with an icon, `.callout-body` > `.callout-t` + text, and `.callout-act`; `.callout.sm` is the small form. Each kind has its own shape: info is a circle-i, success a circle-check, warning a triangle and error an octagon. Callouts live in content and explain a state. Use `T.notify` for events.

The toast grows out of the top bar's centre: its surface (fill, ring and float shadow on `::before`) widens from `scaleX(.6)` with its edge and shadow on from the first frames, and the content fades in once the surface is open (≈ 140 ms). The countdown ring steps once a second under reduced motion.

### 6.18 Empty state and skeleton
```js
listEl.append(T.ui.empty({ icon: 'search', title: '没有匹配的节点', text: '试试其他关键词，或清除筛选条件。', actions: [{ label: '清除搜索', run: () => search.clear() }] }));
listEl.innerHTML = String(T.ui.skeleton(6));     // only while loading, then replace with rows + T.motion.stagger(rows)
```
An empty state says why the area is empty and what to do next. It uses the column form in list areas and `{ row: true }` in panels.

### 6.19 Settings rows and the save bar (one save model)
```html
<section class="fsec" id="sec-net"><div class="fsec-head"><div><h2 class="fsec-t">网络</h2><p class="fsec-d">端口和局域网访问。</p></div></div>
  <div class="frow" data-eff="reload"><div class="frow-l"><div class="frow-t">允许局域网连接</div><div class="frow-d">其他设备可通过本机端口使用代理</div></div>
    <div class="frow-c"><button class="switch" role="switch" aria-checked="false" aria-label="允许局域网连接"></button></div></div>
</section>
```
```js
// edits are drafts; after every edit recompute the changed set and the heaviest effect
row.classList.toggle('is-changed', changed);                       // neutral dot in the left gutter
bar = T.ui.saveBar({ count, effect: 'restart',                      // 'instant' 保存后立即生效 · 'reload' 保存后立即生效，连接不中断 · 'restart' 保存后会重启代理服务，流量中断约 1–2 秒
  onSave: () => apply(drafts),                                      // Promise; reject(new Error('端口 7891 已被其他程序占用，换一个端口后重试。')) to show the failure
  onUndo: () => revertDrafts() });                                  // count 0 → bar.update({ count: 0 }) hides it
// the page's beforeLeave: () => bar && bar.dirty ? bar.confirmLeave() : true
```
The bar slides up from the bottom of the main area, states the effect **in what the user will experience, not what the engine does** (the row chip says 保存后生效 / 会重启服务), runs its own progress while saving (正在应用… / 正在重启代理服务…), and turns into the result (「已保存 · 已生效，连接未中断」) before sliding away. On failure it shows the reason plus 重试 and 撤销. A save that validation stops before anything is applied is **held**, not failed: reject with `Object.assign(new Error('最少可用节点有问题：0 超出范围'), { hold: true })` and the bar reads 「还不能保存」 + the reason with a warning mark, its button still 保存 (the page shows the field). Focus that is inside the bar when it goes (撤销, a finished save) returns to where it came from, never to BODY. While a modal overlay is open (a drawer, a sheet, a confirmation) the bar steps out of sight (`#app.has-modal`) and comes back when the last modal closes; an edit made inside the drawer updates it meanwhile. `.frow.is-block` places the control under the label (editors, lists), and `.frow-sub` gives a full-width sub-area. The section index is `<nav class="toc"><a class="toc-a" data-target="sec-net">${icon}网络<span class="dot"></span></a>…</nav>` with `T.ui.toc(nav)`; add `.is-changed` on a link for sections with drafts.

### 6.20 Progress, meters, sparklines
```html
<span class="prog"><i></i></span>   <span class="meter lg"></span>   <div class="spk" style="height:48px"></div>
```
```js
T.ui.progress(el).set(0.4); p.pending(true); p.done();
T.ui.meter(el, { value: 45, max: 60, min: 8 });        // Bastion 的 Garrison 「正在补充 · 可用 45 · 目标 60 · 最少 8」
T.ui.spark(el, { values, min: 0, bands: [{ at: 400, cls: 'warn' }, { at: 800, cls: 'err' }], format: (v, i) => `${time(i)} · ${v == null ? '不可用' : v + ' ms'}` });
```
Meter tones are `.warn`, `.err`, `.ink` and `.accent` (the default is ok). Progress sizes are `.prog` (4) and `.prog.sm` (2).

### 6.21 Toolbars and section heads
`<div class="toolbar">search · seg · <span class="toolbar-sep"></span> · <span class="spacer"></span> · actions</div>` puts the controls on one 40 px line. **A page toolbar uses the 28 px controls**, so switching between list pages never makes the toolbar jump: `.search.sm`, `.seg` (not `.seg-lg`), `.btn.btn-sm` (15 px icons) and `.btn-sm.btn-icon`. The 32 px sizes are for forms, dialogs and empty states. Rare, confirmed destructive acts (停用 Gateway / 停用 Bastion) are quiet `link-btn`s at the end of their section, never the page's only red; the primary area leads with state and 导入. Section head: `<div class="sh"><span class="sh-t">延迟</span><span class="sh-m">最近 30 分钟</span><div class="sh-a">…</div></div>`. Panels: `.panel` > `.panel-head` (`.panel-title`, `.panel-meta`, `.panel-act`).

### 6.22 The letter for users (11-05; `src/letter/letter.js`, `art.js`, `css/letter.css`)
The user asked for a small, plain-spoken guide that reads like a letter, so that the interface can stay clean: **guidance and the reasons behind safety points live in the letter, not on screen** (F-03 / F-07; the screen keeps only what must be said at an action or an event: confirmations, notices, errors).
- **Entry:** the product name beside the brand mark is a button (`#letter-open`, `.brand-name`, no-drag in the title band; the brand row's layout is unchanged, so the launch still lands on it). Hover gives a wash and the tip 给用户的信; the palette has 打开给用户的信. An accent **envelope** (`.brand-mail`) sits after the name while this edition is unread; it arrives once after the launch hands the brand to the sidebar, and goes when the letter is opened. Read state is an edition number in WebView localStorage (`tesseract.letter`); a new `LETTER_EDITION` shows the envelope again. `letter.test.mjs` pins the zh_CN words of the edition (`LETTER_DIGEST`): changing them is a decision (raise the edition, or only update the digest for a small fix).
- **The letter:** one sheet of white paper (`--sh-float`) on the scrim, the sheets still to read peeking under it (`.lt-stack`, up to two), and a quiet pill of controls below (‹, one dot per page, 下一页 → 合上 as the primary on the last page; one width for both). Six pages: the letterhead (the 「豁然」 mark drawn in) + "For users" + greeting; three steps; the tiers; the two ways to take over traffic; things to keep in mind; tips, the closing wish and the signature "Mark Joel Wexler" with an ink flourish ending in the accent dot. The paper is as tall as the longest page in the current language (measured, within the window). ← → PageUp PageDown Home End page; Esc, ×, the scrim close; focus returns to the name.
- **Type:** the heading, page numbers, step numerals and signature in `--font-letter` (Sitka italic, Latin in every language, never translated); the greeting and the closing wish in the voice (§3.5: kai in zh, Sitka italic in Latin and Cyrillic, the UI face in ja / ko); body 14 / 24 in the UI face, notes 13 in ink-2; tier code names bold. The pages quote interface words (page names, buttons, mode names) through the interface's own messages, so the letter always says what the screen shows.
- **Drawings** (`art.js`): line drawings in the mark's hand (ink strokes, hairline greys, one accent dot): the three steps as rings, the tiers as a rail where the light steps down from Vanguard and back, the two capture modes as apps joined to Tesseract. Drawn complete at rest; each tells its story once when its page shows (§7 rule 12).

---

## 7. Motion rules

1. **Animate `transform` and `opacity` only.** Use the individual `translate`/`scale` properties or `transform`; never animate `width`, `height`, `top`, `left`, `margin`, `flex`/`flex-grow` or `grid` tracks. The complete list of foundation-owned exceptions (page code adds none):
   - `clip-path`: the run-pill fill, the white running pill opening out of the full one after ✓ 已就绪 and the start pill blooming back on a stop (F-06), a deck card unrolling from its band when the deck opens (F-06), the copy button's ✓ 已复制 reveal, the 接管方式 confirmation unrolling (and rolling up) with the cells it pushes.
   - Per-frame script, bounded: the chart's range morph and y-rescale rebuild one path per frame for ≤ 600 ms (a data morph has no transform equivalent); JS springs write `translate`/`scale` (indicators, highlights, slider thumbs). The chart's draw-in is a transform on its clip rect, not script.
   - The toast's countdown ring (`stroke-dashoffset`, 4–5 s, a 30 px ring; steps once a second under reduced motion).
   - The launch mark's two strokes drawing in once (`stroke-dashoffset`, 640 ms, a 58 px mark; rule 11).
   - The letter's drawings drawing their strokes in once when a page shows (`stroke-dashoffset` on `pathLength` 1: the letterhead mark, the capture-mode links, the signature's flourish; rule 12).
   - The sortable drop settles its `box-shadow` over 300 ms together with the transform.
   - The tier deck swaps its cards' `z-index` discretely at the midpoint of a pass (not an interpolated value).
2. **Timing.** A press lands on the next frame (`--dur-press` is 0; the release springs back). Micro changes take 120–200 ms, panels 200–280 ms, and shared elements and morphs 380–460 ms (`SP.settle`). Entrance staggers total ≤ 400 ms (`T.motion.stagger`). Exits are quicker than entrances (110–180 ms, `ease-in`).
3. **Springs settle without wobble**, using the presets only. JS springs (`T.motion.spring`) carry velocity when re-targeted; use them for anything the user can interrupt (indicators, highlights, slider thumbs).
4. **No blank frames.** Swap labels with `swapText` (old out while new in; they share one cell), cross-fade content pages (`setContent`), and keep content on cards while they move. A progress fill never drains backwards, and a solid control that turns into a track does it in one frame (the run pill), never through a fade. When navigation interrupts a page that is still staggering in, the router finishes its entrance first so the 110 ms fade-out starts from what is visible (measured with 8 navigations in 112 ms: the least visible frame is 0.54).
5. **Lists:** reorder, insert and remove with `T.motion.list`. Removed rows fade where they were (120 ms); the rest glide into place, starting 70 ms later so text never slides over a fading copy. When the list's height change moves what comes after it, pass `followers` (or glide it yourself) so the page below glides too instead of jumping. Don't re-render a visible list without it. The one-shot glow (`.is-new`) is transparent at rest, so a finished glow leaves no animation alive.
6. **Numbers:** totals roll forward with `odometer`. 1 Hz live values (rates, latency) update as plain text; never tween text every frame. Changed values on user action may roll (`odometer(…, { mode: 'auto' })`).
7. **Nothing loops while idle or hidden.** Spinners, `.prog.is-pending`, skeleton shimmer, `.switch[aria-busy]` and `.seg[aria-busy]` exist only while something is actually pending; a static "busy" sample in a reference page is frozen (`.is-frozen`). Stop frame loops and intervals in `leave()` (or use `ctx.scope`), and don't write a hidden page's DOM on 1 Hz events: skip while hidden and resync in `enter()` (the overview does both). The simulator's 1 s tick itself only runs while the service runs.
8. **Reduced motion:** `T.motion.*` helpers become instant automatically and CSS transitions collapse to 0.001 ms globally. State must still read without motion; timers step instead of gliding. Don't gate information on an animation finishing.
9. **Hover** only reveals affordances (row actions, chevrons, digit hints). Content panels don't lift.
10. **The start is the one choreographed moment** of a running app (the launch, rule 11, comes before it; F-06, user 11-04: 点击启动以后界面要有变化，不破坏简洁优雅，有一定的冲击力). It is told by the page's own elements, never by an added one (no glow, sweep bar, overlay or particles), in three beats after the brand mark's story (初极狭……豁然开朗):
   - **Press:** the pill becomes the progress (rule 4); the deck's cards unroll from their bands and everything below glides (`overview.js` glide: a block that grows or shrinks never makes what follows jump, at the start or at any other time); the hero's name changes character by character.
   - **Passage** (the whole start, tens of seconds on a real machine): quiet. The progress, the brand dot's breathing and the real facts as they come; no loop of its own.
   - **Open** (about 1.2 s, once, when a real start ends; not after a running → starting → running blip): ✓ 已就绪 on the full pill, then the white running pill opens out of its centre; the name rises out of its line; every change the page makes then travels outward from the pill (`T.motion.wave`), the brand dot last; the chart's colour runs across its grey history from the left; the protocol bar brightens segment by segment. A stop is the mirror without the wave: the start pill blooms out of the centre and the chart greys out.
   - Instant (final state at once) under reduced motion, while the page or the window is hidden and for replayed frames. Nothing is left running when it is over (rule 7).
11. **The launch** (11-05, user: 打开 app 时的几秒不要只看到光标打转). The shell opens the window while the runtime starts (its first hello takes 2.6–3.6 s on the user's machine), and until the page shows real state (host_ready's condition: the first `service.state` snapshot and the first page entered) the launch screen covers it (`shellc/launch.js`, app target only). It is the brand mark's own story and nothing else: no words but the wordmark, no progress bar, no spinner, no tips.
   - **Draw** (once, from when the shell's replay ends without state): the two strokes draw from the narrow passage out to the edges (640 ms), the wordmark comes up (420 ms, from 440 ms), the light lands (spark to dot, 560 ms on `--spring-soft`, from 560 ms).
   - **Wait:** only the light breathes (opacity 1 ↔ .38, 1.8 s), because something is pending (rule 7).
   - **Hand-off** (no earlier than 1.15 s after the draw starts, so the draw is never cut): the mark and wordmark travel as one transform onto the sidebar's own mark (`SP.settle`, the launch lockup is laid out at exactly twice the sidebar's size), the light takes the sidebar dot's colour, the white ground fades (300 ms from 120 ms), and the page's own arrival motion shows through it. The cover is removed in the frame the sidebar shows its own mark.
   - Real state already there (a reload, a recreated window): no launch at all. Reduced motion or a hidden window: drawn at once, removed at once. Fallbacks never hold the app: shown at 700 ms without a replay, gone 4 s after hello without state, 30 s at most. The page never adds a launch elsewhere (a crash relaunch keeps the banners of §6.17).
12. **The letter** (§6.22; user 11-05: 不如改成页面向左偏转然后消失，就好像翻阅一叠信件纸张一样). Its motion is leafing through a stack of letter paper, 2D only:
   - **Open:** the paper rises out of the scrim (soft spring), the sheets under it slide out to show how many are left, the first page's lines write themselves in (stagger) and the letterhead mark draws in. The envelope beside the name goes the moment it is opened.
   - **Forward** (520 ms): the sheet in hand tilts to the left from near its lower left corner and slides away, opaque until past halfway, then gone; the next sheet is already under it and its lines come in; one sheet fewer peeks under the paper. **Back** (480 ms): the sheet before comes in from the left and settles on top.
   - **Drawings** tell their story once per showing (the steps' rings and links, the light stepping down a tier and back, the capture links drawing in, the signature's flourish ending in the light), then rest; nothing loops (rule 7). A new turn ends the one in progress at once.
   - Reduced motion: pages swap at once, drawings rest; the envelope just shows.

---

## 8. Copy rules (zh_CN, written from the user's side of the screen)

- **New copy is written in zh_CN only**, as a message in the page's namespace (`ui/src/i18n/locales/zh_CN/<ns>.json`) with an `_desc` for the translator (where it shows, what the parameters are, how tight the space is; never `_max`). A page ticket does not touch the other eight languages (P5 drafts them); ids deleted or renamed in zh_CN become orphans that P5 cleans up. Changing the text of a frozen id means updating `src/i18n/frozen.json` in the same change and listing it in the report (§5.6).
- The rules below are written for zh_CN; the glossary (`src/i18n/glossary.json`) carries the agreed words for the other languages (tier codenames stay untranslated everywhere).

- Tier codenames stay English and stand alone: **Vanguard · Sentinel · Bastion · Gateway** (use `T.ui.tierTag`). F-03 (user 10-28): no role word beside them, and no sentence that explains what a tier is for or what to put in it.
- **One word per concept.** **层级** means Vanguard / Sentinel / Bastion / Gateway, named by the code name; **节点** means one server; **回退** is moving traffic to the next 层级 (层级回退, 已回退到 Sentinel); Bastion's automatic collection is **Garrison**, a code name like the tiers' (F-04, user 11-01: no internal term such as 自动节点池 / 节点池 / 应急池 on screen; where a thing needs a name it gets a code name, otherwise plain words such as 节点). **线路 is not used**, nor 主线路 / 前两层 / 应急线路 / 自建线路: write 所有层级都不可用, Vanguard 与 Sentinel, 已回退到 Bastion, 经当前层级的节点.
- **Engine vocabulary stays off user surfaces.** The engine is 代理核心 in every message (Mihomo only in the 来源 column, the log source feed and the version line); no 运行时, 缓冲区, 回退组, 名额, 未登记, 热重载 or internal ids (`#371`). Name data by what it answers (网站分类, IP 归属地) with the technical name as a small caption at most.
- The node status vocabulary is exactly **可用 / 较慢 / 不可用 / 待检查 / 待复检 / 草稿 / 当前** (plus 未启用 for a disabled tier). Latency bands are **正常 / 较慢 / 不可用**. Run states are 已停止 / 启动中 / 运行中 / 停止中 / 取消中, and every surface uses the same word (hero, pill, sidebar, deck). Never write "offline" or any other English state.
- **State only what is verified.** The exit location appears (当前出口, the hero hint) only once the exit has been checked; until then it reads 出口 验证中…. A known route may be shown as soon as the service runs. The exit's provenance has one tone: 已确认 · 刚刚 (the automatic check at start) or 已验证 · 3 分钟前 (刷新's full check). Never promise what the settings rule out (the stopped deck lists Bastion only when it is on).
- Placeholders are an em dash `—`, never `--`.
- **Never show raw internal values.** For example, write 已回退, not `tier1_healthy`; 屏蔽, not `REJECT`; 直连, not `DIRECT`; 域名后缀, not `DOMAIN-SUFFIX`; Shadowsocks, not `ss`; and nothing like `egress_proxy`. Raw text belongs only in the 原始日志 view and the reader's 原文 block, verbatim and monospace.
- **An error message names the problem and the fix:** 「订阅地址返回 403：令牌可能已过期。请在订阅商处重新复制地址后重试。」 Bounded diagnostic reasons are 域名解析失败 / 连接超时 / 连接被拒绝 / TLS 连接失败 / 延迟超过上限.
- In-progress text uses 「正在…」 with the ellipsis 「…」 (正在验证出口…). Results are past tense (已验证 · 刚刚, 已送达, 已保存 · 已生效，连接未中断).
- Confirmations: the title is a question naming the object (删除「东京 CN2 01」？), the message gives the consequence, and the confirm button names the act (删除 / 切换 / 放弃更改 / 关闭保护).
- Numbers carry units with a space: 42 ms · 7.29 GB · 1.55 MB/s · 2/2 可用 · 在线率 90%. Use `T.fmt`.
- Separators are ` · ` with spaces. Use no exclamation marks, no emoji and no "请" in front of every sentence. Keep labels short: a button is a 2–4 character verb, a hint is one line.
- Use RFC 5737 / 3849 addresses and reserved documentation domains only (`example.com` / `.net` / `.org` and their subdomains, or `*.example`), never real or merely registrable hosts (`example-cdn.net` is a real domain name; write `cdn.example.net`).
- Don't print syntax as decoration: a row of raw scheme or prefix chips (`ss://`, `domain:`) turns an input into a spec sheet. Say it in one hint line and put the list in a tooltip or a 写法 popover.

---

## 9. Do / don't (from the judged mockups)

**Do**
- Keep status visible. Notices push content down or sit in the top bar and never cover status cells, controls or the hero.
- Hide controls that don't apply (as 重启服务 is hidden when stopped) instead of showing dead disabled buttons. When something is unavailable for a reason, show the reason (as the admin-gated option card does).
- Make only the explicit control act: in a row or pill, the readout is not a button (the 停止 segment is the only stop).
- Show a stopped or paused state honestly: ghost it, label it (服务已停止，暂停采样), and keep its footprint.
- Give every meaningful text ≥ 4.5:1 contrast. Tertiary is `--c-ink-3`, never lighter; a secondary tone on a fill is a solid token (`--c-on-accent-2`), never `opacity` (opacity drops below 4.5:1 on the lighter side of a split control).
- Give one meaning to one dot: every "运行中" dot (hero, run pill) follows health (ok / warn / busy while restarting).
- Keep a switched-off thing quiet: Bastion's 未启用 state line is `--c-ink-3`, so sand or umber on screen means a real warning.
- Put `aria-label` with the real value on any animated number, and hide the digit strips (`odometer` does both).
- Use the brand-grammar icons for tiers and pages. Pair icons with words in navigation and lists.

**Don't**
- Use the accent as a status colour, or put it on large fills other than the primary button and the progress fill.
- Put a state on a tinted capsule (a pink 未保存, an amber 会重启服务): a state is a mark (§3.5). Use green, or a saturated red, anywhere; or a hue on running text and titles (the hero's state word is the one exception).
- Slant CJK (no fake oblique: `font-synthesis: none` stays on the body), bold the voice, or use the voice for labels, values, data or whole sentences.
- Add glow or halo shadows under buttons, accent-tinted drop shadows or hover lift on content panels. Don't use a coloured bar on the left of a rounded card.
- Put the same big radius and shadow on every block, or nest bordered boxes.
- Let labels go blank mid-swap, let progress drain backwards, or let a moving card lose its content.
- Leave a tooltip hanging after a press (the core mutes it), or put essential information only in a tooltip.
- Give invented data prominence (a big 延迟 figure, cross-check claims). Only real, brief-backed values get large type.
- Animate layout properties, run infinite animations while idle, or re-render and tween text every frame.
- Use 10–11 px text for meaning (the 11 px is only in keycaps and badge digits; tags such as 演示 and the banner's 错误 / 警告 are 12), or a translucent focus ring or glow (inputs and selects use a solid 2 px ring).
- Use Lucide-style stock metaphors (server rack, RSS arcs, tent, door, speedometer).
- Mark the active item three ways at once. One indicator is enough.

---

## 10. Hand-back checklist for a page builder
0. The gates are green: `npm --prefix ui run check` (cjk, demo, inject, style, features all `enforce`; the i18n p4 hard checks), `node --test ui/test/unit/` (a skip only when it is a registered one: checked by hand, since node:test does not fail a skip; with `TESSERACT_UI_REQUIRE_CHROME=1` a missing Chrome fails `chrome_required.test.mjs`) and the render matrix for the page in zh_CN and pseudo (`ui/README.md`).
1. Zero page errors, console errors, CSP violations and failed requests (the render matrix and its `--csp strict` mode check them).
2. It fits at 1280×800 and 1060×700 with no horizontal overflow. Fill pages have `#host` `scrollHeight == clientHeight`, also with a banner open. In the narrow fallback (375 / 420 / 760 px): no document scroll, the page scrolls in `#host`, every control is reachable.
3. After 5 s idle: zero running infinite animations (`document.getAnimations()`), including after leaving the page.
4. Every control has hover, press, focus-visible and disabled states. The press lands on the next frame (computed `scale` changes at once on pointerdown).
5. Busy → done happens on the control. Lists animate insert, remove and reorder. Nothing goes blank mid-transition.
6. The keyboard works: lists (↑ ↓ Enter Esc), the drawer (Esc), menus and Ctrl F. Focus returns to the opener when an overlay closes.
7. Copy passes §8: tiers are named by their code names alone and no line advises or explains the product (§1.7), there are no raw values, errors give the fix, every new message has `_desc`, data text carries `translate="no"` and nothing untrusted goes through HTML (§5.7).
8. It reads correctly under `prefers-reduced-motion: reduce`.
9. Report any page-local component that should be promoted to the library.
