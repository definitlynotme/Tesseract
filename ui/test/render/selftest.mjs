// Framework self-test of the render matrix (T-P4-10 "测试" and requirement 8).
//
//   node ui/test/render/run.mjs --self-test [--dist <dir> | --build-flags "…"] [--run <name>]
//
// Every block records {name, ok, detail}; the run fails when one block fails. Blocks:
//   exact_three        a synthetic page with one overflowing element, one leaked Chinese node,
//                      one translate="no" Chinese data node and one injected element (plus
//                      look-alikes that must stay quiet: ellipsis, .sr-only, rich pseudo text split
//                      over inline nodes, <kbd> (inline-grid) and a <tier> slot .ttag (inline-flex)
//                      whose items are blockified, phrasing elements blockified as items of a
//                      flex container (.cmdk-foot > span > kbd; flex > b), whitelisted tokens, a horizontally scrollable
//                      box): the checker reports exactly the three; plus two broken messages (a hidden unclosed ⟦,
//                      a visible cut ⟦…) each followed by a leaked node: both the unbalanced
//                      bracket and the leak after it are reported (7 findings in all)
//   overflow_rules     scroll containers are scrollable_x (not truncation), an ellipsis box cut
//                      by its ancestor and a block cut vertically are truncations, odometer-like
//                      strips wholly outside the clip and line-clamp are not
//   box_overflow       (T-P5-11) the box rule: T-P5-06 §4 (a reserved-width button pushing 取消
//                      out of a sheet footer) and §6 (默认 800 ms · 恢复默认 past its column in a
//                      scrolling page, which the text rule calls scrollable_x), an absolute
//                      decoration past its containing block and a fixed layer past the viewport are
//                      found (by: box); a scroll container's content, a data-overflow-ok pop-up, an
//                      absolute badge inside its block, a fixed tip inside the viewport, a roll face
//                      wholly outside its clip, a bleed inside the padding, .sr-only text, an
//                      ellipsis box and wrapping text are not; the same page with the causes taken
//                      away has no finding. Fix round 1 adds: a nowrap key pushing into its value
//                      (found against itself, own_cell; the text rule only says scrollable_x), a
//                      block whose text is all in inline children, an icon button pushed wholly out
//                      of a clipping row, .set-sec-clear 10 px out (its ceiling is 6) and a 12 px
//                      bleed (4 px over the 8 px cap) are found; .set-sec-clear at 6 px and a 6 px
//                      bleed are not
//   box_overflow_real  (T-P5-11) on the real settings page (en_US 1060): the T-P5-06 rule taken back
//                      (默认 800 ms · 恢复默认 on one line, a constructed sheet) is a box finding on
//                      #set-lat-def; the page as shipped has none there
//   catalog_ellipsis   (T-P5-13) an ellipsis over catalog text is a truncation (by: 'ellipsis'):
//                      a data-i18n-id element, text under a data-i18n-id ancestor, a traced T.t
//                      string, one ' · ' part of a joined label and a line clamp are found; data
//                      (translate="no", even under a data-i18n-id message), text with no catalog id,
//                      a catalog ellipsis box that fits and a box whose cut part is data only are
//                      not (they stay ellipsized / quiet). Fix round 1: boxes cut by 0.1 px and
//                      0.45 px, and a 24 px padded box whose words pass only its content box, are
//                      found; boxes exactly as wide as their words (with and without padding) are
//                      quiet
//   catalog_ellipsis_real (T-P5-13) on the real settings page (en_US 1060): the index labels' old
//                      one-line ellipsis put back (a constructed sheet) is a catalog-ellipsis
//                      finding on .set-toc-l; the page as shipped has none
//   leak_reach         SVG <text> / <title> / aria-label, input button values, ::after content
//                      and a stray ⟧ are reported; translate="no" SVG text is not
//   verdict_policy     overflow_allow; product-language truncations are report-only
//   planted_delta      the same four plants inside the real overview (pseudo, fixture mode): the
//                      findings added by the plants are exactly the three
//   host_scroll        a 4000 px wide block in the page makes the host-scroll check fire
//   console_pwned      console.error and window.__pwned in a cell are failures
//   unmatched_invoke   a command the scenario does not answer is a failure
//   leak_policy        @i18n-pending classification: page / shell regions, --leaks hard
//   stability          the same cell twice: identical text tree, pixel-identical screenshot
//   textree_format     textree.mjs rows == the P4-01 baseline capture function's rows
//   textree_diff       diffTrees: equal, allowed pair, plain change, fmt grouping, insertion
//   fonts_ja / fonts_ru  the font rules pass on the overview and catch a planted SimSun /
//                      YaHei (ja) and a mixed-font Russian sentence (ru)
//   fonts_ja_scope     (T-P5-07, the T-P5-03 counterexamples; --i18n-trace build) on the ja_JP
//                      overview, found by the rule's own scope (no extraSelector): kanji-only
//                      catalog text in YaHei / SimSun fails (設定, 状態), so does kana text in
//                      SimSun without an id and a language list name (settings.app.lang.name.*)
//                      left without translate="no" / lang (no exemption by id); quiet:
//                      translate="no", text with no id and no kana, lang="zh-CN" (counted in
//                      exempt), and catalog text whose SimSun is only in a translate="no" child
//                      (own text only; the whole element measures SimSun, proven by a raw CDP call);
//                      (fix round) the traced path: an element with no id whose own text is a
//                      kanji-only T.t string of the page (T.i18n.traced()) in YaHei fails, via traced
//   fonts_ja_fallback  a page with no trace and no data-i18n-id: the result names scope
//                      kana_fallback with a note; kanji-only SimSun is not seen, kana SimSun fails
//                      (the unseen kanji is a snapshot of a known gap, not the wanted behaviour)
//   fonts_ru_own_text  on the ru_RU overview: a sentence around a .mono child and a sentence cut
//                      by a monospace ellipsis box pass; raw CDP calls prove both are real traps
//                      (the element reads two fonts; with the ellipsis on, the text node reads the
//                      box's font for "…")
//   font_scope_summary (T-P5-07, T-P5-03 S5) renderCell records counts.font_scope (ja_JP i18n_trace,
//                      ru_RU cyrillic, none without a rule) and run.mjs fontScope counts them for
//                      summary.font_scope, listing the kana_fallback cells
//   csp_collector      (T-P4-08b) on the loopback server with the shell's CSP: each way markup
//                      can carry an inline style (innerHTML, a T.html template, setAttribute
//                      ('style'), an SVG style attribute, a <style> element) is one violation, and
//                      the CSSOM ways the page uses (style.cssText / setProperty, el.animate, a
//                      constructed sheet) are none; the page is served from 127.0.0.1 and booted
//                      in fixture mode under the policy
//   csp_planted_build  (T-P4-08b) a copy of the design build with one style="…" planted in
//                      index.html: the same cell rendered through renderCell fails with a csp
//                      finding, the unmodified build in the same cell has none
//   csp_policy_guard   (T-P4-08b fix round 1) strictCsp refuses 'unsafe-*' / nonce / hash /
//                      wildcard in any *-src* directive (style-src-attr / -elem, script-src-attr /
//                      -elem) and data: / blob: outside img-src / font-src; the shell's policy passes
//   csp_known_fail     (T-P4-08b fix round 1) a known_fail cell with a CSP violation still fails
//                      the --csp strict run
//   serve_scope        (T-P4-08b) serve.mjs: 127.0.0.1 only, the root only (.., %2e%2e, %5c,
//                      drive paths, absolute paths -> 403), no listing, a foreign Host -> 421,
//                      POST -> 405, the CSP <meta> first in <head> of .html only
//   ids_trace          (--i18n-trace builds) T.ti / T.t text in the page shows up in ids.json data
//   truncation_doc     truncation.json has the shape tools/i18n_check.mjs reads
import { cpSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync, readdirSync } from 'node:fs';
import { request } from 'node:http';
import { tmpdir } from 'node:os';
import { join, relative, sep } from 'node:path';
import { pathToFileURL } from 'node:url';
import { textTreeInPage as baselineTree } from '../baseline/capture.mjs';
import { cellChecksInPage, classifyLeaks, fontChecks, LEAK_WHITELIST, pendingFiles, visibleIdsInPage } from './checks.mjs';
import { diffTrees, fmtAllowances, textTreeInPage } from './textree.mjs';
import { cspServer, EVIDENCE, fontScope, launch, loadScenario, openCell, parseArgs, renderCell, REPO, runAction, runMatrix, runName, selectBuild, strictCsp, truncationDoc, UI } from './run.mjs';
import { serveStatic } from './serve.mjs';

const posix = p => p.split(sep).join('/');
const PSEUDO_OPTS = { pseudo: true, whitelist: LEAK_WHITELIST };

// The four plants (ids __st_*). Overflow and leak texts are distinct so each is one finding.
const PLANTS_HTML = `
  <p id="__st_overflow" style="width:60px;white-space:nowrap;overflow:hidden;margin:0">⟦ŵŵŵŵŵŵŵŵŵŵŵŵŵŵŵŵŵŵŵŵ⟧</p>
  <p id="__st_leak" style="margin:0">漏抽的中文节点</p>
  <div id="__st_data" translate="no" style="margin:0"><span>数据节点中文</span> <b>东京</b></div>
  <img id="__st_inject" src="x" alt="" width="1" height="1">`;

// broken messages: the bracket of a cut or hidden message must not hide the leaks after it
const UNBALANCED_HTML = `
  <span id="__st_open_hidden" style="display:none">⟦àbç</span>
  <p id="__st_after_hidden" style="margin:0">隐藏截断之后的漏抽</p>
  <p id="__st_open_cut" style="margin:0">⟦ŝţàŕţ ŝé…</p>
  <p id="__st_after_cut" style="margin:0">可见截断之后的漏抽</p>`;

// look-alikes that must not be reported
const QUIET_HTML = `
  <p id="ok_pseudo">⟦ŝţàŕţ ŝéŕṽîçé⟧</p>
  <p id="ok_rich">⟦àà <b>ƀƀ <i>ĉĉ</i></b> çç⟧</p>
  <p id="ok_kbd">⟦ƥŕéšš <kbd style="display:inline-grid">Ĉţŕĺ</kbd>+<kbd style="display:inline-grid">Ķ</kbd> ţö öƥéñ~⟧</p>
  <p id="ok_ttag">⟦ŝţàţûš <span class="ttag" style="display:inline-flex"><b>Sentinel</b><span class="r">ĝûàŕð</span></span> ŕéàðý~⟧</p>
  <div id="ok_flex_kbd" style="display:flex"><span style="display:inline-flex">⟦<kbd style="display:inline-grid">↑</kbd> šéĺéçţ⟧</span></div>
  <div id="ok_flex_b" style="display:flex">⟦ƥŕéšš <b>ƀƀ</b> ţö⟧</div>
  <p id="ok_tokens">203.0.113.24 · Vanguard · VLESS · 2001:db8::1 · AS64500 · TCP/UDP · 12:30</p>
  <p id="ok_ellipsis" style="width:60px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">⟦ŵŵŵŵŵŵŵŵŵŵŵŵŵŵŵŵŵŵ⟧</p>
  <p id="ok_sronly" style="position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap">⟦ŝŕ ŵŵŵŵŵŵŵŵŵŵŵŵŵŵ⟧</p>
  <p id="ok_hidden" style="display:none">隐藏的中文</p>
  <div id="ok_hscroll" style="width:100px;overflow-x:auto"><p style="white-space:nowrap;margin:0">⟦ŵŵŵŵŵŵŵŵŵŵŵŵŵŵŵŵŵŵŵŵŵŵ⟧</p></div>`;

const ids = list => list.map(x => x.selector).sort();

// One raw CDP font measurement of the node a page expression returns: no own-text split, no
// ellipsis guard (what the measurement before T-P5-03 saw), to prove that a plant is a real trap.
async function rawFonts(context, page, expression) {
  const cdp = await context.newCDPSession(page);
  try {
    await cdp.send('DOM.enable');
    await cdp.send('CSS.enable');
    await cdp.send('DOM.getDocument', { depth: 0 });
    const { result } = await cdp.send('Runtime.evaluate', { expression, objectGroup: 'selftest-fonts' });
    if (!result.objectId) throw new Error(`rawFonts: ${expression} is not a node`);
    const { nodeId } = await cdp.send('DOM.requestNode', { objectId: result.objectId });
    return (await cdp.send('CSS.getPlatformFontsForNode', { nodeId })).fonts.filter(f => f.glyphCount > 0).map(f => f.familyName).sort();
  } finally {
    await cdp.send('Runtime.releaseObjectGroup', { objectGroup: 'selftest-fonts' }).catch(() => {});
    await cdp.detach().catch(() => {});
  }
}

// T-P5-07 (from evidence/T-P5-03/fontproof.mjs and more.mjs): plants for the ja_JP scope. fail:
// must be reported (rule ja_no_yahei_simsun, by text); the others must stay quiet.
const YAHEI = "font-family:'Microsoft YaHei UI'", SIMSUN = 'font-family:SimSun';
const JA_SCOPE_PLANTS = [
  { id: '__st_ja_kanji_yahei', html: `<span id="__st_ja_kanji_yahei" data-i18n-id="__st.kanji_yahei" style="${YAHEI}">設定</span>`, fail: '設定' },
  { id: '__st_ja_kanji_simsun', html: `<span id="__st_ja_kanji_simsun" data-i18n-id="__st.kanji_simsun" style="${SIMSUN}">状態</span>`, fail: '状態' },
  { id: '__st_ja_kana_noid', html: `<span id="__st_ja_kana_noid" style="${SIMSUN}">ノードの確認</span>`, fail: 'ノードの確認' },
  { id: '__st_ja_langname', html: `<span id="__st_ja_langname" data-i18n-id="settings.app.lang.name.zh_cn" style="${YAHEI}">简体中文</span>`, fail: '简体中文' },
  { id: '__st_ja_dnt', html: `<span id="__st_ja_dnt" translate="no" data-i18n-id="__st.dnt" style="${SIMSUN}">設定</span>` },
  { id: '__st_ja_noid', html: `<span id="__st_ja_noid" style="${SIMSUN}">東西南北</span>` },
  { id: '__st_ja_zh', html: `<span id="__st_ja_zh" lang="zh-CN" data-i18n-id="__st.zh" style="${YAHEI}">繁体中文</span>`, exempt: true },
  { id: '__st_ja_mixed', html: `<span id="__st_ja_mixed" data-i18n-id="__st.mixed">接続先 <span translate="no" style="${SIMSUN}">东京节点</span> を確認</span>` },
];

export async function selfTest(o) {
  const snap = selectBuild(o);
  const distDir = snap.dir, manifest = snap.manifest;
  if (!(manifest.locales || {}).pseudo) rmSync(distDir, { recursive: true, force: true });
  if (!(manifest.locales || {}).pseudo) throw new Error('self-test needs the pseudo language: npm --prefix ui run build -- --pseudo --i18n-trace');
  const name = `${runName({ ...o, cases: ['selftest'] })}`;
  const outDir = o.out ? o.out : join(EVIDENCE, name);
  mkdirSync(outDir, { recursive: true });
  const blocks = [];
  const record = (n, ok, detail) => { blocks.push({ name: n, ok: !!ok, detail }); console.log(`${ok ? 'ok  ' : 'FAIL'} ${n}${ok ? '' : ' ' + JSON.stringify(detail).slice(0, 400)}`); };
  const tryBlock = async (n, fn) => { try { await fn(); } catch (e) { record(n, false, { error: String(e && e.stack || e) }); } };
  const running = loadScenario('rt_connect_replay');
  const stopped = loadScenario('rt_start_job');
  const env = { distDir, outDir: join(outDir, 'cells'), pending: pendingFiles(UI), leaks: 'auto', csp: false };
  const browser = await launch({ loopback: true }); // the csp_* blocks load from serve.mjs
  const tmp = mkdtempSync(join(tmpdir(), 'tesseract-render-selftest-'));
  try {
    // ---- exact_three (synthetic page) -------------------------------------------------------
    await tryBlock('exact_three', async () => {
      const css = readdirSync(distDir).filter(f => /^app\..*\.css$/.test(f)).map(f => `<link rel="stylesheet" href="${pathToFileURL(join(distDir, f)).href}">`).join('');
      const file = join(tmp, 'synthetic.html');
      writeFileSync(file, `<!doctype html><html lang="en-XA"><head><meta charset="utf-8">${css}</head><body><div class="host" id="host" style="width:600px;height:400px">${QUIET_HTML}${PLANTS_HTML}${UNBALANCED_HTML}</div></body></html>`);
      const context = await browser.newContext({ viewport: { width: 1280, height: 800 } });
      const page = await context.newPage();
      await page.goto(pathToFileURL(file).href);
      const c = await page.evaluate(cellChecksInPage, PSEUDO_OPTS);
      await context.close();
      const found = [...c.truncations.map(x => `overflow ${x.selector}`), ...c.leaks.map(x => `leak ${x.kind} ${x.selector}`), ...c.injection.map(x => `inject ${x.rule} ${x.selector}`), ...c.host_scroll_x.map(x => `host_scroll ${x}`)];
      const want = ['overflow #__st_overflow', 'leak text #__st_leak', 'inject img_x #__st_inject',
        'leak unbalanced #__st_open_hidden', 'leak text #__st_after_hidden', 'leak unbalanced #__st_open_cut', 'leak text #__st_after_cut'];
      const ok = found.length === want.length && want.every(w => found.includes(w)) && ids(c.ellipsized).join() === '#ok_ellipsis'
        && c.scrollable_x.some(x => x.clip === '#ok_hscroll');
      record('exact_three', ok, { found, want, ellipsized: ids(c.ellipsized), scrollable_x: c.scrollable_x.map(x => `${x.selector} ${x.clip || ''}`) });
    });

    // ---- overflow_rules (synthetic page) ------------------------------------------------------
    await tryBlock('overflow_rules', async () => {
      const W = '⟦ŵŵŵŵŵŵŵŵŵŵŵŵŵŵŵŵŵŵŵŵŵŵŵŵ⟧';
      const html = `
        <div id="r_scroll_anc" style="width:100px;overflow-x:auto"><p id="r_scroll_in" style="white-space:nowrap;margin:0">${W}</p></div>
        <p id="r_scroll_self" style="width:100px;overflow-x:scroll;white-space:nowrap;margin:0">${W}</p>
        <div id="r_ell_clip" style="width:80px;overflow:hidden"><span id="r_ell" style="display:block;width:200px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${W}</span></div>
        <div id="r_ell_ok" style="width:300px;overflow:hidden"><span id="r_ell2" style="display:block;width:100px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${W}</span></div>
        <div id="r_vclip" style="width:300px;height:20px;overflow:hidden"><p id="r_vcut" style="margin:0;line-height:16px">⟦ŀ1⟧<br>⟦ŀ2⟧<br>⟦ŀ3⟧</p></div>
        <div id="r_odo" style="height:16px;line-height:16px;overflow:hidden;width:40px"><div style="transform:translateY(-16px)"><span id="r_odo0" style="display:block">0</span><span id="r_odo1" style="display:block">1</span><span id="r_odo2" style="display:block">2</span></div></div>
        <p id="r_clamp" style="width:120px;margin:0;line-height:16px;display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2;overflow:hidden">⟦ŵŵŵ ŵŵŵ ŵŵŵ ŵŵŵ ŵŵŵ ŵŵŵ ŵŵŵ ŵŵŵ ŵŵŵ ŵŵŵ⟧</p>`;
      const file = join(tmp, 'overflow_rules.html');
      writeFileSync(file, `<!doctype html><html lang="en-XA"><head><meta charset="utf-8"></head><body><div id="host" style="width:600px">${html}</div></body></html>`);
      const context = await browser.newContext({ viewport: { width: 1280, height: 800 } });
      const page = await context.newPage();
      await page.goto(pathToFileURL(file).href);
      const c = await page.evaluate(cellChecksInPage, PSEUDO_OPTS);
      await context.close();
      const tr = c.truncations.map(x => `${x.selector} ${x.by}`).sort();
      const want = ['#r_ell ancestor_of_ellipsis', '#r_vcut ancestor_y'];
      const sc = ids(c.scrollable_x), el = ids(c.ellipsized);
      const ok = JSON.stringify(tr) === JSON.stringify(want) && sc.includes('#r_scroll_in') && sc.includes('#r_scroll_self')
        && el.includes('#r_ell') && el.includes('#r_ell2') && el.includes('#r_clamp');
      record('overflow_rules', ok, { truncations: tr, want, scrollable_x: sc, ellipsized: el });
    });

    // ---- box_overflow (T-P5-11, synthetic page) -----------------------------------------------
    // The two blind spots of T-P5-06: §4, a busy button whose reserved width pushes 取消 out of the
    // sheet footer (labels still readable), and §6, 默认 800 ms · 恢复默认 running past its readout
    // column inside a page that scrolls (the text rule calls it scrollable_x). Both must be found
    // with by: 'box'; the look-alikes after them must stay quiet.
    await tryBlock('box_overflow', async () => {
      const html = `
        <div id="b_page" style="width:560px;height:380px;overflow-y:auto">
          <div id="b_sheet" style="width:400px;padding:16px;border:1px solid #888">
            <div id="b_foot" style="display:flex;justify-content:flex-end;gap:8px"><button id="b_cancel">取消</button><button id="b_apply" style="min-width:470px">导入</button></div>
          </div>
          <div style="display:grid;grid-template-columns:120px minmax(0,1fr);width:400px">
            <div id="b_col" style="display:flex;flex-direction:column;min-width:0"><span id="b_def" style="display:flex;gap:4px;white-space:nowrap">默认 800 ms · <button id="b_restore" class="link-btn">恢复默认</button></span></div>
            <div>slider</div>
          </div>
          <div id="b_cb" style="position:relative;width:200px;height:20px"><span id="b_abs_bad" style="position:absolute;left:150px;top:0;white-space:nowrap">装饰越出所在定位块的文字</span></div>
          <span id="b_fixed_bad" style="position:fixed;left:1200px;top:0;white-space:nowrap">越出视口的浮层文字</span>
          <div style="display:grid;grid-template-columns:60px minmax(0,1fr);gap:8px;width:300px"><dt id="b_dt" style="white-space:nowrap">一个很长很长的键名</dt><dd style="margin:0">值</dd></div>
          <div id="b_card" style="width:200px"><div id="b_inl" style="width:320px"><span>只有行内子元素</span><span>的文字块</span></div></div>
          <div id="b_row" style="display:flex;width:120px;overflow:hidden"><span style="flex:none;width:120px">占满一行的文字</span><button id="b_icon_out" aria-label="被挤出行的图标按钮" style="flex:none;width:24px;height:16px"></button></div>
          <div id="b_field" style="position:relative;width:200px;height:20px"><a href="#" id="b_clear_far" class="set-sec-clear" style="position:absolute;right:-10px;top:0">清除</a></div>
          <div id="b_bleedc" style="width:200px"><div id="b_bleed_big" style="margin:0 -12px">超过六像素的出血</div></div>
          <div style="position:relative;width:200px;height:20px"><a href="#" id="q_clear" class="set-sec-clear" style="position:absolute;right:-6px;top:0">清除</a></div>
          <div style="width:200px"><div id="q_bleed_small" style="margin:0 -6px">六像素以内的出血</div></div>
          <div id="q_scroll" style="width:200px;overflow-x:auto"><table><tr><td style="white-space:nowrap">一张很宽的表格里的单元格文字，按设计横向滚动</td></tr></table></div>
          <div id="q_anchor" style="position:relative;width:60px"><button id="q_pop_ok" data-overflow-ok style="position:absolute;left:0;top:20px;width:300px">有意越出的弹出层</button></div>
          <div style="position:relative;width:200px;height:20px"><span id="q_abs_in" style="position:absolute;right:0;top:0">角标</span></div>
          <span id="q_tip" style="position:fixed;left:20px;bottom:20px">视口内的提示</span>
          <div style="width:120px;overflow:hidden;white-space:nowrap"><div style="display:flex;width:240px;transform:translateX(-120px)"><button id="q_roll_out" aria-label="停在框外的面" style="flex:none;width:120px;height:16px"></button><span id="q_roll_in" style="flex:none;width:120px">框内的面</span></div></div>
          <div style="width:200px;padding:0 16px"><div id="q_bleed" style="margin:0 -16px">通栏分隔条的文字</div></div>
          <p id="q_sronly" style="position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap">读屏专用的一长串文字</p>
          <div style="width:100px"><p id="q_ell" style="margin:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">省略号截断的数据名称很长很长</p></div>
          <div style="width:100px"><p id="q_wrap" style="margin:0">会换行的界面文字 会换行的界面文字</p></div>
        </div>`;
      const file = join(tmp, 'box_overflow.html');
      writeFileSync(file, `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"></head><body style="margin:0"><div id="host" style="width:600px">${html}</div></body></html>`);
      const context = await browser.newContext({ viewport: { width: 1280, height: 800 } });
      const page = await context.newPage();
      await page.goto(pathToFileURL(file).href);
      const c = await page.evaluate(cellChecksInPage, { pseudo: false });
      // the same page with the §4 / §6 causes taken away: nothing left
      await page.evaluate(() => { document.getElementById('b_apply').style.minWidth = ''; document.getElementById('b_def').style.whiteSpace = 'normal'; document.getElementById('b_def').style.flexWrap = 'wrap'; document.getElementById('b_abs_bad').remove(); document.getElementById('b_fixed_bad').remove(); ['b_dt', 'b_inl', 'b_icon_out', 'b_clear_far', 'b_bleed_big'].forEach(id => document.getElementById(id).remove()); });
      const fixed = await page.evaluate(cellChecksInPage, { pseudo: false });
      await context.close();
      const box = c.truncations.filter(x => x.by === 'box').map(x => `${x.selector} < ${x.clip}`).sort();
      // §6: the link (a nowrap button in the readout's flex line) is the box that leaves 默认 800 ms's
      // line; the text rule alone reports nothing there (the page scrolls)
      // fix round 1: a key pushing into its value (own cell), a block whose text is all inline, an
      // icon button pushed wholly out of a clipping row, .set-sec-clear past its 6 px ceiling and a
      // bleed past the 8 px cap
      const want = ['#b_abs_bad < #b_cb', '#b_apply < #b_foot', '#b_cancel < #b_foot', '#b_restore < #b_def', '#b_fixed_bad < viewport',
        '#b_dt < #b_dt', '#b_inl < #b_card', '#b_icon_out < #b_row', '#b_clear_far < #b_field', '#b_bleed_big < #b_bleedc'].sort();
      const textRule = c.scrollable_x.map(x => x.selector);
      const ok = JSON.stringify(box) === JSON.stringify(want) && c.truncations.length === want.length
        && !textRule.includes('#b_restore') && textRule.includes('#b_dt') && !c.truncations.some(x => x.by !== 'box')
        && fixed.truncations.length === 0;
      record('box_overflow', ok, { box, want, other: c.truncations.filter(x => x.by !== 'box').map(x => `${x.selector} ${x.by}`), text_rule_scrollable: textRule, after_fix: fixed.truncations.map(x => `${x.selector} ${x.by} ${x.clip}`) });
    });

    // ---- box_overflow_real (T-P5-11, the real settings page) --------------------------------
    // T-P5-06 §6 on the page itself: the readout column's 默认 800 ms · 恢复默认 put back on one
    // line (the rule T-P5-06 replaced, a constructed sheet) is a box finding in en_US 1060; the
    // page as shipped has none there.
    await tryBlock('box_overflow_real', async () => {
      // (the cell of T-P5-06's probe: after the conflict reload the latency is not the default, so
      // 恢复默认 shows beside 默认 800 ms)
      const c0 = (await import('./cases/settings.mjs')).default.find(c => c.name === 'settings_save_conflict');
      const cell = await openCell(browser, { distDir, scenario: loadScenario(c0.scenario), lang: 'en_US', size: '1060x700', page: 'settings' });
      for (const a of c0.actions || []) await runAction(cell.page, a);
      const at = c => c.truncations.filter(x => /set-lat-def/.test(x.selector)).map(x => `${x.selector} ${x.by} ${x.clip || ''}`);
      const before = await cell.page.evaluate(cellChecksInPage, { pseudo: false });
      // (T-P5-13: the restore link's own words wrap as well now; the plant takes that back too)
      await cell.page.evaluate(() => { const s = new CSSStyleSheet(); s.replaceSync('.set-lat-def, .set-lat-def .link-btn { flex-wrap: nowrap !important; white-space: nowrap !important; }'); document.adoptedStyleSheets = [...document.adoptedStyleSheets, s]; });
      const planted = await cell.page.evaluate(cellChecksInPage, { pseudo: false });
      await cell.context.close();
      const ok = at(before).length === 0 && at(planted).some(x => /^#set-lat-def( > [^ ]+)? box /.test(x));
      record('box_overflow_real', ok, { before: at(before), planted: at(planted) });
    });

    // ---- catalog_ellipsis (T-P5-13, synthetic page) -------------------------------------------
    await tryBlock('catalog_ellipsis', async () => {
      const W = 'Words of the catalog that run much longer than their box';
      const E = 'display:block;width:90px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin:0';
      // (fix round 1) boxes sized from their own words' width once laid out: data-cut px short of
      // the words (0.1 / 0.45: a cut of a fraction of a pixel; 6 inside 24 px padding: the words stay
      // inside the padding box, only the content box is passed), or exactly as wide (data-cut 0)
      const S = 'display:block;width:max-content;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin:0';
      const html = `
        <p id="c_id" data-i18n-id="x.id" style="${E}">${W}</p>
        <p id="c_anc" data-i18n-id="x.anc" style="${E}"><span>${W}</span></p>
        <p id="c_traced" style="${E}">Traced words of a T.t string that is long</p>
        <p id="c_part" style="${E}">Label · a sub part that runs long</p>
        <p id="c_clamp" data-i18n-id="x.clamp" style="width:120px;margin:0;line-height:16px;display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2;overflow:hidden">${W} ${W} ${W}</p>
        <p id="q_data" data-i18n-id="x.msg" style="${E}"><span translate="no">node-name-of-the-user-that-is-data</span></p>
        <p id="q_noid" style="${E}">${W}</p>
        <p id="q_fit" data-i18n-id="x.fit" style="${E}">Fits</p>
        <p id="q_mixed" data-i18n-id="x.mixed" style="${E}">Node <span translate="no">a-long-node-name-that-is-data-only</span></p>
        <p id="c_sub1" data-i18n-id="x.sub1" data-cut="0.1" style="${S}">Abcdefghijk lmnop</p>
        <p id="c_sub5" data-i18n-id="x.sub5" data-cut="0.45" style="${S}">Abcdefghijk lmnop</p>
        <p id="c_pad" data-i18n-id="x.pad" data-cut="6" data-pad="24" style="${S};box-sizing:border-box;padding:0 24px">Abcdefghijk lmnop</p>
        <p id="q_exact" data-i18n-id="x.exact" data-cut="0" style="${S}">Abcdefghijk lmnop</p>
        <p id="q_exact_pad" data-i18n-id="x.exact_pad" data-cut="0" data-pad="24" style="${S};box-sizing:border-box;padding:0 24px">Abcdefghijk lmnop</p>`;
      const file = join(tmp, 'catalog_ellipsis.html');
      writeFileSync(file, `<!doctype html><html lang="en"><head><meta charset="utf-8"></head><body><div id="host" style="width:600px">${html}</div></body></html>`);
      const context = await browser.newContext({ viewport: { width: 1280, height: 800 } });
      const page = await context.newPage();
      await page.goto(pathToFileURL(file).href);
      // the trace index of an --i18n-trace build (T.i18n.traced): text → ids
      await page.evaluate(() => { window.T = { i18n: { traced: () => ({ 'Traced words of a T.t string that is long': ['x.traced'], 'Label': ['x.label'] }) } }; });
      const sized = await page.evaluate(() => [...document.querySelectorAll('[data-cut]')].map(e => {
        const r = document.createRange(); r.selectNodeContents(e.firstChild);
        const w = r.getBoundingClientRect().width, pad = Number(e.dataset.pad || 0) * 2, cut = Number(e.dataset.cut);
        e.style.width = `${w + pad - cut}px`;
        const cs = getComputedStyle(e);
        const content = e.getBoundingClientRect().width - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight);
        return { id: e.id, words: w, content, short_by: Math.round((w - content) * 1000) / 1000, in_padding_box: w <= e.clientWidth - parseFloat(cs.paddingLeft) + 1 };
      }));
      const c = await page.evaluate(cellChecksInPage, { pseudo: false });
      await context.close();
      const tr = c.truncations.map(x => `${x.selector} ${x.by} ${x.id}${x.clamp ? ' clamp' : ''}`).sort();
      const want = ['#c_anc ellipsis x.anc', '#c_clamp ellipsis x.clamp clamp', '#c_id ellipsis x.id', '#c_pad ellipsis x.pad', '#c_part ellipsis x.label', '#c_sub1 ellipsis x.sub1', '#c_sub5 ellipsis x.sub5', '#c_traced ellipsis x.traced'];
      const el = ids(c.ellipsized);
      // the plants are what they say: the sub-pixel boxes are short by 0.05–0.5 px, the padded one
      // only past its content box, the exact ones not short at all
      const sz = Object.fromEntries(sized.map(x => [x.id, x]));
      const planted = sz.c_sub1.short_by > 0.05 && sz.c_sub1.short_by < 0.2 && sz.c_sub5.short_by > 0.3 && sz.c_sub5.short_by <= 0.5
        && sz.c_pad.short_by > 5 && sz.c_pad.in_padding_box && Math.abs(sz.q_exact.short_by) < 0.02 && Math.abs(sz.q_exact_pad.short_by) < 0.02;
      const ok = planted && JSON.stringify(tr) === JSON.stringify(want) && ['#q_data', '#q_noid', '#q_mixed'].every(x => el.includes(x))
        && !['#q_fit', '#q_exact', '#q_exact_pad'].some(x => el.includes(x)) && !tr.some(x => /q_/.test(x));
      record('catalog_ellipsis', ok, { truncations: tr, want, ellipsized: el, sized });
    });

    // ---- catalog_ellipsis_real (T-P5-13, the real settings page) -------------------------------
    await tryBlock('catalog_ellipsis_real', async () => {
      const cell = await openCell(browser, { distDir, scenario: loadScenario('settings_page_running'), lang: 'en_US', size: '1060x700', page: 'settings' });
      const at = c => c.truncations.filter(x => /set-toc-l/.test(x.selector)).map(x => `${x.selector} ${x.by} ${x.id}`);
      const before = await cell.page.evaluate(cellChecksInPage, { pseudo: false });
      await cell.page.evaluate(() => { const s = new CSSStyleSheet(); s.replaceSync('.set { grid-template-columns: var(--set-side) minmax(0, 1fr) !important; } .set-toc .toc-a { height: 30px !important; white-space: nowrap !important; } .set-toc-l { overflow: hidden !important; text-overflow: ellipsis !important; }'); document.adoptedStyleSheets = [...document.adoptedStyleSheets, s]; });
      const planted = await cell.page.evaluate(cellChecksInPage, { pseudo: false });
      await cell.context.close();
      const ok = at(before).length === 0 && at(planted).some(x => /set-toc-l[^ ]* ellipsis settings\.sec\./.test(x));
      record('catalog_ellipsis_real', ok, { before: at(before), planted: at(planted) });
    });

    // ---- leak_reach (synthetic page) ----------------------------------------------------------
    await tryBlock('leak_reach', async () => {
      const html = `
        <svg id="l_svg" width="200" height="40" aria-label="图表"><title id="l_svg_title">流量图</title><text id="l_svg_text" x="0" y="20">下行</text><text id="l_svg_ok" x="60" y="20">⟦ðŵñ⟧</text></svg>
        <svg id="l_svg_data" translate="no" width="200" height="40"><text x="0" y="20">东京节点</text></svg>
        <input id="l_btn" type="button" value="确定">
        <input id="l_txt" type="text" value="用户输入的中文">
        <style>#l_css::after { content: "伪元素中文"; } #l_css_ok::after { content: "⟦ƥŝ⟧"; }</style>
        <p id="l_css" style="margin:0">⟦ƀ⟧</p><p id="l_css_ok" style="margin:0">⟦ƀ⟧</p>
        <p id="l_stray" style="margin:0">⟦ŝŝ⟧ ⟧</p>`;
      const file = join(tmp, 'leak_reach.html');
      writeFileSync(file, `<!doctype html><html lang="en-XA"><head><meta charset="utf-8"></head><body><div id="host">${html}</div></body></html>`);
      const context = await browser.newContext({ viewport: { width: 1280, height: 800 } });
      const page = await context.newPage();
      await page.goto(pathToFileURL(file).href);
      const c = await page.evaluate(cellChecksInPage, PSEUDO_OPTS);
      await context.close();
      const found = c.leaks.map(x => `${x.kind}${x.attr ? ':' + x.attr : ''}${x.pseudo ? ':' + x.pseudo : ''} ${x.selector}`).sort();
      const want = ['attr:aria-label #l_svg', 'attr:value #l_btn', 'css_content:::after #l_css', 'text #l_svg_text', 'text #l_svg_title', 'unbalanced #l_stray'].sort();
      record('leak_reach', JSON.stringify(found) === JSON.stringify(want), { found, want });
    });

    // ---- verdict_policy (renderCell verdicts) ---------------------------------------------------
    await tryBlock('verdict_policy', async () => {
      const plant = { eval: () => { const p = document.createElement('p'); p.id = '__st_vp'; p.style.cssText = 'width:40px;white-space:nowrap;overflow:hidden;margin:0'; p.textContent = 'Überlange Beschriftung ohne Ende'; document.querySelector('#pages .page').prepend(p); } };
      const base = { file: 'selftest', scenario: stopped, page: 'overview', actions: [plant] };
      const zh = await renderCell(browser, { case: { ...base, name: 'vp_zh' }, lang: 'zh_CN', size: '1280x800' }, { ...env, fonts: false });
      const zhAllowed = await renderCell(browser, { case: { ...base, name: 'vp_zh_allowed', overflow_allow: [{ selector: '^#__st_vp$', why: 'selftest' }] }, lang: 'zh_CN', size: '1280x800' }, { ...env, fonts: false });
      const de = await renderCell(browser, { case: { ...base, name: 'vp_de' }, lang: 'de_DE', size: '1280x800' }, { ...env, fonts: false });
      const ok = !zh.ok && zh.failures.some(f => f.check === 'overflow' && f.selector === '#__st_vp')
        && zhAllowed.ok && zhAllowed.counts.overflow_allowed === 1
        && de.ok && de.counts.truncations_report_only >= 1 && de.truncations.some(t => t.selector === '#__st_vp');
      record('verdict_policy', ok, { zh: zh.failures, zh_allowed: zhAllowed.counts, de: de.counts, de_fail: de.failures });
    });

    // ---- planted_delta (real overview, pseudo) ------------------------------------------------
    await tryBlock('planted_delta', async () => {
      const cell = await openCell(browser, { distDir, scenario: running, lang: 'pseudo', size: '1280x800', page: 'overview' });
      const before = await cell.page.evaluate(cellChecksInPage, PSEUDO_OPTS);
      // appended in an element of its own tag, so no existing element's :nth-of-type selector moves
      await cell.page.evaluate(html => { const p = document.querySelector('#pages .page'); const d = document.createElement('st-plant'); d.style.display = 'block'; d.innerHTML = html; p.append(d); }, PLANTS_HTML);
      const after = await cell.page.evaluate(cellChecksInPage, PSEUDO_OPTS);
      await cell.context.close();
      const key = (k, x) => `${k} ${JSON.stringify(x)}`;
      const all = c => [...c.truncations.map(x => key('overflow', x)), ...c.leaks.map(x => key('leak', x)), ...c.injection.map(x => key('inject', x)), ...c.host_scroll_x.map(x => key('host_scroll', x))];
      const b = new Set(all(before));
      const delta = all(after).filter(x => !b.has(x));
      const has = (k, id) => delta.some(x => x.startsWith(k + ' ') && x.includes(`#${id}`));
      const ok = delta.length === 3 && has('overflow', '__st_overflow') && has('leak', '__st_leak') && has('inject', '__st_inject') && !delta.some(x => x.includes('__st_data'));
      record('planted_delta', ok, { delta, before_counts: { truncations: before.truncations.length, leaks: before.leaks.length, injection: before.injection.length } });
    });

    // ---- host_scroll ----------------------------------------------------------------------
    await tryBlock('host_scroll', async () => {
      const cell = await openCell(browser, { distDir, scenario: running, lang: 'zh_CN', size: '1060x700', page: 'overview' });
      const before = await cell.page.evaluate(cellChecksInPage, { pseudo: false });
      await cell.page.evaluate(() => { const d = document.createElement('div'); d.id = '__st_wide'; d.style.cssText = 'width:4000px;height:8px'; document.querySelector('#pages .page').append(d); });
      const after = await cell.page.evaluate(cellChecksInPage, { pseudo: false });
      await cell.context.close();
      record('host_scroll', before.host_scroll_x.length === 0 && after.host_scroll_x.includes('host'), { before: before.host_scroll_x, after: after.host_scroll_x, host: after.scroll.host });
    });

    // ---- console_pwned / unmatched_invoke (through renderCell, i.e. the real verdict code) ----
    await tryBlock('console_pwned', async () => {
      const r = await renderCell(browser, { case: { name: 'selftest_console', file: 'selftest', scenario: stopped, page: 'overview', actions: [{ eval: () => { console.error('selftest console error'); window.__pwned = 1; } }] }, lang: 'zh_CN', size: '1280x800' }, { ...env, fonts: false });
      const checks = r.failures.map(f => f.check);
      record('console_pwned', !r.ok && checks.includes('console') && checks.includes('inject') && checks.length === 2, { failures: r.failures });
    });
    await tryBlock('unmatched_invoke', async () => {
      const r = await renderCell(browser, { case: { name: 'selftest_invoke', file: 'selftest', scenario: stopped, page: 'overview', actions: [{ eval: () => { T.rt.call('service.stop').catch(() => {}); } }] }, lang: 'zh_CN', size: '1280x800' }, { ...env, fonts: false });
      const checks = r.failures.map(f => f.check);
      record('unmatched_invoke', !r.ok && checks.every(c => c === 'fixture_invoke') && checks.length >= 1, { failures: r.failures });
    });

    // ---- leak_policy ----------------------------------------------------------------------
    await tryBlock('leak_policy', async () => {
      const leaks = [{ selector: 'a', in_pages: true }, { selector: 'b', in_pages: false }];
      const r1 = classifyLeaks(leaks, 'overview', new Set(['src/pages/overview.js', 'src/core/core.js']));
      const r2 = classifyLeaks(leaks, 'overview', new Set(['src/core/core.js']));
      const r3 = classifyLeaks(leaks, 'overview', new Set());
      const r4 = classifyLeaks(leaks, 'overview', new Set(['src/pages/overview.js', 'src/core/core.js']), { leakPolicy: 'hard' });
      const r5 = classifyLeaks(leaks, 'profiles', new Set(['src/pages/tiers.js']));
      const ok = r1.failing.length === 0 && r1.pending.length === 2
        && ids(r2.failing).join() === 'a' && ids(r2.pending).join() === 'b'
        && r3.failing.length === 2 && r4.failing.length === 2
        && ids(r5.pending).join() === 'a' && ids(r5.failing).join() === 'b';
      record('leak_policy', ok, { r1, r2, r3, r4, r5, pending_now: [...env.pending].sort() });
    });

    // ---- stability ------------------------------------------------------------------------
    await tryBlock('stability', async () => {
      const smoke = (await import('./cases/smoke.mjs')).default;
      const detail = [];
      let ok = true;
      const byName = n => smoke.find(c => c.name === n);
      // the pseudo smoke cell carries a documented known failure (host_scroll); stability is about
      // rendering the same cell identically twice, so a known-fail case only needs identical output
      for (const [c, lang, size] of [[byName('overview_running'), 'zh_CN', '1280x800'], [byName('overview_stopped_pseudo'), 'pseudo', '1060x700']]) {
        const a = await renderCell(browser, { case: { ...c, file: 'selftest', name: `stab_${c.name}_1` }, lang, size }, env);
        const b = await renderCell(browser, { case: { ...c, file: 'selftest', name: `stab_${c.name}_2` }, lang, size }, env);
        const sameTree = JSON.stringify(a._tree) === JSON.stringify(b._tree), samePng = a._png.equals(b._png);
        const passes = r => r.ok || !!c.known_fail;
        detail.push({ case: c.name, lang, size, rows: a._tree.length, sameTree, samePng, a_ok: a.ok, b_ok: b.ok, known_fail: !!c.known_fail });
        ok = ok && sameTree && samePng && passes(a) && passes(b);
      }
      record('stability', ok, detail);
    });

    // ---- textree_format -----------------------------------------------------------------
    await tryBlock('textree_format', async () => {
      const cell = await openCell(browser, { distDir, scenario: running, lang: 'zh_CN', size: '1280x800', page: 'overview' });
      const mine = await cell.page.evaluate(textTreeInPage);
      const theirs = await cell.page.evaluate(baselineTree);
      await cell.context.close();
      record('textree_format', mine.length > 50 && JSON.stringify(mine) === JSON.stringify(theirs), { rows: mine.length });
    });

    // ---- textree_diff ----------------------------------------------------------------------
    await tryBlock('textree_diff', async () => {
      const a = [[1, 'span', '已验证 · 3 分钟前', '', '', ''], [2, 'span', '1000 B', '', '', ''], [2, 'b', 'x', '', '', '']];
      const b = [[1, 'span', '已确认 · 3 分钟前', '', '', ''], [2, 'span', '1,000 B', '', '', ''], [2, 'b', 'x', '', '', '']];
      const allow = [{ from: '已验证', to: '已确认', why: '02b' }, ...fmtAllowances()];
      const r1 = diffTrees(a, a);
      const r2 = diffTrees(a, b, { allow });
      const r3 = diffTrees(a, b, { allow: [] });
      const r4 = diffTrees(a, [...a.slice(0, 1), [1, 'i', 'new', '', '', ''], ...a.slice(1)], { allow });
      const r5 = diffTrees(a, b, { allow: [{ from: '已验证', to: '已确认', why: 'other page', page: 'tiers' }, ...fmtAllowances()], page: 'overview' });
      const ok = r1.equal && r1.allowed.length === 0 && r2.equal && r2.allowed.length === 2 && !r3.equal && r3.diff.length === 4
        && !r4.equal && r4.diff.length === 1 && r4.diff[0].startsWith('+ 2:') && !r5.equal && r5.changes === 2;
      record('textree_diff', ok, { r2: r2.allowed, r3: r3.diff, r4: r4.diff, r5: r5.diff });
    });

    // ---- fonts ----------------------------------------------------------------------------
    await tryBlock('fonts_ja', async () => {
      const cell = await openCell(browser, { distDir, scenario: running, lang: 'ja_JP', size: '1280x800', page: 'overview' });
      const good = await fontChecks(cell.context, cell.page, 'ja_JP');
      await cell.page.evaluate(() => {
        const p = document.querySelector('#pages .page');
        p.insertAdjacentHTML('afterbegin', '<span id="__st_simsun" style="font-family:SimSun">設定の状態</span><span id="__st_yahei" style="font-family:\'Microsoft YaHei UI\'">ノードの状態</span>');
      });
      const bad = await fontChecks(cell.context, cell.page, 'ja_JP', { extraSelector: '#__st_simsun, #__st_yahei' });
      await cell.context.close();
      const ok = good.failures.length === 0 && good.checked >= 2 && bad.failures.length === 2 && bad.failures.every(f => f.rule === 'ja_no_yahei_simsun');
      record('fonts_ja', ok, { good, bad: bad.failures });
    });
    await tryBlock('fonts_ru', async () => {
      const cell = await openCell(browser, { distDir, scenario: running, lang: 'ru_RU', size: '1280x800', page: 'overview' });
      const good = await fontChecks(cell.context, cell.page, 'ru_RU');
      await cell.page.evaluate(async () => {
        const p = document.querySelector('#pages .page');
        p.insertAdjacentHTML('afterbegin', '<span id="__st_mixed" style="font-family:\'Instrument Sans\', \'Segoe UI\'">Статус узла OK</span>');
        await document.fonts.load('16px "Instrument Sans"', 'OK');
      });
      const bad = await fontChecks(cell.context, cell.page, 'ru_RU', { extraSelector: '#__st_mixed' });
      await cell.context.close();
      const ok = good.failures.length === 0 && good.checked >= 2 && bad.failures.length === 1 && bad.failures[0].rule === 'ru_single_font';
      record('fonts_ru', ok, { good, bad: bad.failures });
    });

    // ---- fonts_ja_scope / fonts_ja_fallback / fonts_ru_own_text (T-P5-07) ------------------
    await tryBlock('fonts_ja_scope', async () => {
      const cell = await openCell(browser, { distDir, scenario: running, lang: 'ja_JP', size: '1280x800', page: 'overview' });
      const trace = await cell.page.evaluate(() => window.__TESSERACT_DESIGN__.build.trace);
      const good = await fontChecks(cell.context, cell.page, 'ja_JP');
      // (fix round) the traced path: no id, no kana, but the whole own text is a T.t string of this
      // page (T.i18n.traced()): a kanji-only ja catalog string in YaHei must fail with via traced
      // (the trace index keeps strings of earlier languages too: take one that T.t gives in ja_JP now)
      const tracedKanji = await cell.page.evaluate(() => Object.entries(T.i18n.traced())
        .filter(([s, idList]) => /^\p{Script=Han}{2,}$/u.test(s) && !['設定', '状態', '東西南北', '繁体中文'].includes(s) && idList.some(id => T.t(id) === s))
        .map(([s]) => s).sort()[0] || null);
      const plants = JA_SCOPE_PLANTS.concat(tracedKanji ? [{ id: '__st_ja_traced', html: `<span id="__st_ja_traced" style="${YAHEI}">${tracedKanji}</span>`, fail: tracedKanji, via: 'traced' }] : []);
      await cell.page.evaluate(html => document.querySelector('#pages .page:not([hidden])').insertAdjacentHTML('afterbegin', html), plants.map(p => p.html).join(''));
      const bad = await fontChecks(cell.context, cell.page, 'ja_JP');
      const mixedRaw = await rawFonts(cell.context, cell.page, 'document.getElementById("__st_ja_mixed")');
      await cell.context.close();
      const want = plants.filter(p => p.fail).map(p => p.fail).sort();
      const got = bad.failures.map(f => `${f.rule}:${f.text}`);
      const gotTexts = bad.failures.filter(f => f.rule === 'ja_no_yahei_simsun').map(f => f.text).sort();
      const tracedHit = tracedKanji ? bad.failures.filter(f => f.text === tracedKanji).map(f => (bad.samples[f.label] || {}).via) : [];
      const ok = trace && good.scope === 'i18n_trace' && good.failures.length === 0 && good.checked >= 2 && bad.scope === 'i18n_trace'
        && !!tracedKanji && JSON.stringify(tracedHit) === '["traced"]'
        && bad.failures.length === want.length && JSON.stringify(gotTexts) === JSON.stringify(want)
        && bad.exempt === good.exempt + plants.filter(p => p.exempt).length
        && mixedRaw.some(f => /SimSun/i.test(f));
      record('fonts_ja_scope', ok, { trace, good: { scope: good.scope, checked: good.checked, exempt: good.exempt, failures: good.failures }, want, got, traced: { text: tracedKanji, via: tracedHit }, exempt: bad.exempt, mixed_raw: mixedRaw });
    });
    await tryBlock('fonts_ja_fallback', async () => {
      // A SNAPSHOT OF A KNOWN GAP, not the wanted behaviour: without a trace build the rule falls
      // back to kana text only, so the kanji-only SimSun 「設定」 below is NOT reported. The block
      // asserts that (and the scope / note that make the fallback visible in summary.font_scope) so
      // that the gap cannot widen silently. When the fallback rule is improved to see kanji-only
      // text, change this assertion with it (「設定」 must then fail).
      const file = join(tmp, 'fonts_ja_fallback.html');
      writeFileSync(file, `<!doctype html><html lang="ja"><head><meta charset="utf-8"></head><body style="font-family:'Yu Gothic UI','Meiryo UI',sans-serif;font-size:16px"><div id="host">`
        + `<p id="__st_jf_kanji" style="${SIMSUN}">設定</p><p id="__st_jf_kana" style="${SIMSUN}">ノードの状態</p><p id="__st_jf_ok">ノードを確認</p></div></body></html>`);
      const context = await browser.newContext({ viewport: { width: 1280, height: 800 } });
      const page = await context.newPage();
      await page.goto(pathToFileURL(file).href);
      const r = await fontChecks(context, page, 'ja_JP');
      await context.close();
      const got = r.failures.map(f => `${f.rule}:${f.text}`);
      const ok = r.scope === 'kana_fallback' && typeof r.note === 'string' && /kana/.test(r.note) && got.length === 1 && got[0] === 'ja_no_yahei_simsun:ノードの状態';
      record('fonts_ja_fallback', ok, { scope: r.scope, note: r.note, checked: r.checked, via: r.via, got });
    });
    await tryBlock('fonts_ru_own_text', async () => {
      const cell = await openCell(browser, { distDir, scenario: running, lang: 'ru_RU', size: '1280x800', page: 'overview' });
      const good = await fontChecks(cell.context, cell.page, 'ru_RU');
      const cut = await cell.page.evaluate(async () => {
        const p = document.querySelector('#pages .page:not([hidden])');
        p.insertAdjacentHTML('afterbegin', '<p id="__st_ru_mixed">Порт <span class="mono">7890</span> открыт</p>'
          + '<div id="__st_ru_ell_box" style="font-family:Consolas;width:90px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis"><span id="__st_ru_ell" style="font-family:\'Segoe UI\'">(ещё 3 строки) и ещё много текста</span></div>');
        await document.fonts.ready;
        const b = document.getElementById('__st_ru_ell_box');
        return b.scrollWidth > b.clientWidth;
      });
      const bad = await fontChecks(cell.context, cell.page, 'ru_RU');
      // the traps, as the old measurement saw them (the guard sheet of fontChecks is gone again)
      const mixedRaw = await rawFonts(cell.context, cell.page, 'document.getElementById("__st_ru_mixed")');
      const ellRaw = await rawFonts(cell.context, cell.page, 'document.getElementById("__st_ru_ell").firstChild');
      const seen = Object.values(bad.samples).filter(s => /^Порт открыт$|^\(ещё 3 строки\)/.test(s.text)).map(s => `${s.text} ${s.fonts.join('+')}`);
      await cell.context.close();
      const ok = good.failures.length === 0 && bad.failures.length === 0 && cut && seen.length === 2
        && new Set(mixedRaw).size >= 2 && ellRaw.some(f => /Consolas/i.test(f)) && ellRaw.some(f => /Segoe/i.test(f));
      record('fonts_ru_own_text', ok, { good: good.failures, bad: bad.failures, cut, seen, mixed_raw: mixedRaw, ellipsis_raw: ellRaw });
    });
    await tryBlock('font_scope_summary', async () => {
      const cellOf = (lang, fonts) => renderCell(browser, { case: { name: `fs_${lang}${fonts === false ? '_nofonts' : ''}`, file: 'selftest', scenario: running, page: 'overview' }, lang, size: '1280x800' }, { ...env, fonts });
      const cells = [await cellOf('ja_JP'), await cellOf('ru_RU'), await cellOf('zh_CN'), await cellOf('ja_JP', false)];
      const fake = { case: 'fake', lang: 'ja_JP', size: '1060x700', counts: { font_scope: 'kana_fallback' } };
      const s = fontScope([...cells, fake]);
      const scopes = cells.map(c => c.counts.font_scope);
      const ok = JSON.stringify(scopes) === JSON.stringify(['i18n_trace', 'cyrillic', null, null])
        && s.i18n_trace === 1 && s.cyrillic === 1 && s.none === 2 && s.kana_fallback === 1 && JSON.stringify(s.fallback_cells) === '["fake ja_JP 1060x700"]';
      record('font_scope_summary', ok, { scopes, summary: s });
    });

    // ---- csp_collector (T-P4-08b) ----------------------------------------------------------
    await tryBlock('csp_collector', async () => {
      const srv = await cspServer(distDir);
      try {
        const cell = await openCell(browser, { distDir, scenario: running, lang: 'zh_CN', size: '1280x800', page: 'overview', csp: true, origin: srv.origin });
        const facts = await cell.page.evaluate(() => ({ href: location.href, meta: document.querySelector('meta[http-equiv="Content-Security-Policy"]')?.content || null,
          first: document.head.firstElementChild && document.head.firstElementChild.getAttribute('http-equiv'), fixture: !!(window.__TESSERACT_DESIGN__ && window.__TESSERACT_DESIGN__.fixture),
          before: window.__cspViolations.slice() }));
        // each plant runs alone; -> the violations it added
        const plants = {
          innerHTML: () => { const d = document.createElement('div'); d.innerHTML = '<span style="color:red">x</span>'; document.body.append(d); },
          t_html: () => { document.body.append(T.html`<span style="color:red">x</span>`); },
          setAttribute: () => { const e = document.createElement('span'); e.setAttribute('style', 'color:red'); document.body.append(e); },
          svg_attr: () => { const d = document.createElement('div'); d.innerHTML = '<svg><rect style="fill:red"/></svg>'; document.body.append(d); },
          style_element: () => { const e = document.createElement('style'); e.textContent = 'p{color:red}'; document.head.append(e); },
          // (fix round 1, S6: the Attr-node path and a computed name, which the static scan can only partly see)
          attr_node: () => { const e = document.createElement('span'); const a = document.createAttribute('style'); a.value = 'color:red'; e.setAttributeNode(a); document.body.append(e); },
          computed_name: () => { const e = document.createElement('span'); e.setAttribute(['sty', 'le'].join(''), 'color:red'); document.body.append(e); },
        };
        const quiet = {
          cssom: () => { const e = document.createElement('span'); e.style.cssText = 'color:red'; e.style.setProperty('--v', '1'); document.body.append(e); },
          t_h: () => { document.body.append(T.h('span', { style: { width: '10%', '--v': '2', marginLeft: 'auto' } }), T.h('span', { style: 'color:red' })); },
          // (fix round 1, S6: a style key in another case goes through the CSSOM too, never setAttribute)
          t_h_case: () => { document.body.append(T.h('span', { Style: 'color:red' }), T.h('span', { STYLE: { color: 'red', '--v': '3' } })); },
          animate: () => { const e = document.createElement('span'); document.body.append(e); e.animate([{ opacity: 0 }, { opacity: 1 }], 1); },
          constructed_sheet: () => { const sh = new CSSStyleSheet(); sh.replaceSync('p{outline:0}'); document.adoptedStyleSheets = [...document.adoptedStyleSheets, sh]; },
          clone: () => { const e = document.createElement('span'); e.style.color = 'red'; document.body.append(e.cloneNode(true)); },
        };
        const got = {};
        for (const [name, fn] of [...Object.entries(plants), ...Object.entries(quiet)]) {
          const n0 = await cell.page.evaluate(() => window.__cspViolations.length);
          await cell.page.evaluate(fn);
          await cell.page.waitForTimeout(60);
          got[name] = await cell.page.evaluate(n => window.__cspViolations.slice(n).map(v => v.directive), n0);
        }
        await cell.context.close();
        const plantsOk = Object.keys(plants).every(k => got[k].length === 1 && /^style-src/.test(got[k][0]));
        const quietOk = Object.keys(quiet).every(k => got[k].length === 0);
        const ok = facts.href.startsWith(srv.origin + '/') && facts.meta === srv.policy && facts.first === 'Content-Security-Policy' && facts.fixture
          && facts.before.length === 0 && plantsOk && quietOk && !/unsafe-inline/.test(srv.policy);
        record('csp_collector', ok, { origin: srv.origin, policy: srv.policy, facts: { ...facts, before: facts.before.length }, got });
      } finally { await srv.close(); }
    });

    // ---- csp_planted_build (T-P4-08b) ------------------------------------------------------
    await tryBlock('csp_planted_build', async () => {
      const dir = mkdtempSync(join(tmpdir(), 'tesseract-render-csp-plant-'));
      const servers = [];
      try {
        cpSync(distDir, dir, { recursive: true });
        const html = readFileSync(join(dir, 'index.html'), 'utf8');
        if (!/<body[\s>]/.test(html)) throw new Error('index.html has no <body>');
        writeFileSync(join(dir, 'index.html'), html.replace(/<body([^>]*)>/, '<body$1>\n<span id="__st_csp_plant" style="outline:0"></span>'));
        const cellOf = async root => {
          const srv = await cspServer(root); servers.push(srv);
          const out = join(outDir, 'csp_planted_build', root === dir ? 'planted' : 'clean');
          return renderCell(browser, { case: { name: 'csp_plant', file: 'selftest', scenario: running, page: 'overview' }, lang: 'zh_CN', size: '1280x800' },
            { ...env, distDir: root, outDir: out, csp: true, origin: srv.origin });
        };
        const planted = await cellOf(dir);
        const clean = await cellOf(distDir);
        const cspOf = c => c.failures.filter(f => f.check === 'csp');
        const ok = !planted.ok && cspOf(planted).length === 1 && /style-src/.test(cspOf(planted)[0].msg) && /__st_csp_plant|outline:0/.test(cspOf(planted)[0].msg)
          && clean.ok && clean.counts.csp === 0;
        record('csp_planted_build', ok, { planted: cspOf(planted), planted_failures: planted.failures.length, clean: { ok: clean.ok, csp: clean.counts.csp, failures: clean.failures.slice(0, 5) } });
      } finally {
        for (const s of servers) await s.close();
        rmSync(dir, { recursive: true, force: true });
      }
    });

    // ---- csp_policy_guard (T-P4-08b fix round 1, S1) ---------------------------------------
    await tryBlock('csp_policy_guard', async () => {
      const base = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'";
      const loose = [
        `${base}; style-src-attr 'unsafe-inline'`, `${base}; style-src-elem 'self' 'unsafe-inline'`, `${base}; script-src-attr 'unsafe-hashes'`,
        `${base}; script-src-elem 'self' 'nonce-abc'`, `${base}; style-src-elem 'sha256-abc='`, "default-src 'self'; script-src 'self' data:; style-src 'self'",
        "default-src 'self' data:; script-src 'self'; style-src 'self'", "default-src 'self'; script-src 'self'; style-src 'self' blob:",
        "default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; style-src 'self'", "default-src *; script-src 'self'; style-src 'self'",
        "default-src 'self'; script-src 'self'; style-src 'self' 'UNSAFE-INLINE'", "script-src 'self'; style-src 'self'",
      ];
      const refused = Object.fromEntries(loose.map(p => { try { strictCsp(p); return [p, false]; } catch (e) { return [p, true]; } }));
      const shell = strictCsp();
      const accepted = strictCsp(`${base}; font-src 'self' data:`);
      const ok = Object.values(refused).every(Boolean) && /style-src 'self' 'report-sample'/.test(shell) && /script-src 'self' 'report-sample'/.test(shell)
        && /img-src 'self' data:/.test(shell) && !/unsafe|nonce-|sha\d+-/i.test(shell) && /font-src 'self' data:/.test(accepted);
      record('csp_policy_guard', ok, { refused, shell });
    });

    // ---- csp_known_fail (T-P4-08b fix round 1, item 1) -------------------------------------
    // a known_fail cell with a CSP violation: the cell is listed as a known failure, yet the run
    // fails (runMatrix's exit verdict), and the violation is in summary.csp
    await tryBlock('csp_known_fail', async () => {
      const dir = mkdtempSync(join(tmpdir(), 'tesseract-render-csp-kf-'));
      try {
        mkdirSync(join(dir, 'cases'));
        writeFileSync(join(dir, 'cases', 'kf.mjs'), `export default [{ name: 'kf_csp', scenario: 'rt_connect_replay', page: 'overview', langs: ['zh_CN'], sizes: ['1280x800'],
  known_fail: { owner: 'selftest', why: 'csp_known_fail' },
  actions: [{ eval: () => { const e = document.createElement('span'); e.id = '__st_kf'; e.setAttribute('style', 'color:red'); document.body.append(e); } }] }];\n`);
        const quiet = { log: console.log }; console.log = () => {};
        let s;
        try { s = await runMatrix({ ...parseArgs([]), dist: distDir, dir: join(dir, 'cases'), csp: 'strict', langs: ['zh_CN'], sizes: ['1280x800'], out: join(dir, 'out') }); }
        finally { console.log = quiet.log; }
        const ok = s.ok === false && s.failed.length === 0 && s.known_failures.length === 1 && s.csp && s.csp.violations === 1
          && s.csp.known_fail_cells_with_violations.length === 1;
        record('csp_known_fail', ok, { ok: s.ok, failed: s.failed.length, known: s.known_failures.length, csp: s.csp && { violations: s.csp.violations, cells: s.csp.known_fail_cells_with_violations } });
      } finally { rmSync(dir, { recursive: true, force: true }); }
    });

    // ---- serve_scope (T-P4-08b) ------------------------------------------------------------
    await tryBlock('serve_scope', async () => {
      const root = mkdtempSync(join(tmpdir(), 'tesseract-render-serve-'));
      const outside = mkdtempSync(join(tmpdir(), 'tesseract-render-outside-'));
      mkdirSync(join(root, 'sub'));
      writeFileSync(join(root, 'index.html'), '<!doctype html><html><head><title>t</title></head><body>x</body></html>');
      writeFileSync(join(root, 'sub', 'a.js'), 'void 0;');
      writeFileSync(join(outside, 'secret.txt'), 'secret');
      const policy = strictCsp();
      const srv = await serveStatic(root, { csp: policy });
      const get = (path, { method = 'GET', host = `127.0.0.1:${srv.port}` } = {}) => new Promise((ok, bad) => {
        const r = request({ host: '127.0.0.1', port: srv.port, path, method, headers: { Host: host } }, res => {
          let body = ''; res.setEncoding('utf8'); res.on('data', d => { body += d; }); res.on('end', () => ok({ status: res.statusCode, type: res.headers['content-type'] || '', body }));
        });
        r.on('error', bad); r.end();
      });
      try {
        const rel = posix(relative(root, join(outside, 'secret.txt')));
        const got = {
          index: await get('/'), js: await get('/sub/a.js'), dir: await get('/sub/'), dir2: await get('/sub'),
          dotdot: await get('/../' + rel), enc: await get('/%2e%2e/' + rel), backslash: await get('/..%5c' + rel.replace(/\//g, '%5c')),
          drive: await get('/' + encodeURIComponent(join(outside, 'secret.txt'))), abs: await get('//' + join(outside, 'secret.txt').replace(/\\/g, '/')),
          nul: await get('/index.html%00.js'), missing: await get('/nope.js'), host: await get('/', { host: 'evil.example' }), post: await get('/', { method: 'POST' }),
        };
        const st = Object.fromEntries(Object.entries(got).map(([k, v]) => [k, v.status]));
        const head = /<head>\n<meta http-equiv="Content-Security-Policy" content="([^"]*)">/.exec(got.index.body);
        const ok = srv.origin.startsWith('http://127.0.0.1:') && got.index.status === 200 && head && head[1] === policy && /text\/html/.test(got.index.type)
          && got.js.status === 200 && got.js.body === 'void 0;' && /javascript/.test(got.js.type)
          && got.dir.status === 404 && got.dir2.status === 404 && got.missing.status === 404
          && ['dotdot', 'enc', 'backslash', 'drive', 'abs', 'nul'].every(k => got[k].status === 403 || got[k].status === 400) && Object.values(got).every(v => !v.body.includes('secret'))
          && got.host.status === 421 && got.post.status === 405;
        record('serve_scope', ok, { origin: srv.origin, status: st, meta: head ? head[1] === policy : false });
      } finally {
        await srv.close();
        rmSync(root, { recursive: true, force: true });
        rmSync(outside, { recursive: true, force: true });
      }
    });

    // ---- ids_trace -------------------------------------------------------------------------
    await tryBlock('ids_trace', async () => {
      const cell = await openCell(browser, { distDir, scenario: running, lang: 'zh_CN', size: '1280x800', page: 'overview' });
      const trace = await cell.page.evaluate(() => window.__TESSERACT_DESIGN__.build.trace);
      if (!trace) { await cell.context.close(); record('ids_trace', false, { error: 'build without --i18n-trace' }); return; }
      await cell.page.evaluate(() => {
        const p = document.querySelector('#pages .page');
        const a = document.createElement('span'); T.ti(a, 'common.action.refresh'); p.prepend(a);
        const b = document.createElement('span'); b.textContent = T.t('common.action.copy'); p.prepend(b);
        const h = document.createElement('span'); h.style.display = 'none'; T.ti(h, 'common.action.delete'); p.prepend(h);
      });
      const list = await cell.page.evaluate(visibleIdsInPage);
      await cell.context.close();
      const got = list.map(x => x.id);
      record('ids_trace', got.includes('common.action.refresh') && got.includes('common.action.copy') && !got.includes('common.action.delete'), { ids: list });
    });

    // ---- truncation_doc --------------------------------------------------------------------
    await tryBlock('truncation_doc', async () => {
      const cells = [{ lang: 'de_DE', page: 'overview', case: 'c', size: '1060x700', truncations: [{ selector: '#x', id: 'overview.run.stop', text: 't' }] }, { lang: 'pseudo', page: 'overview', case: 'c', size: '1060x700', truncations: [{ selector: '#y', id: null, text: 'p' }] }];
      const coverage = { cases: ['smoke/c'], pages: ['overview'], langs: ['zh_CN', 'de_DE', 'pseudo'], sizes: ['1060x700'], source_digest: 'x', h_features: [], build_flags: null, complete: false };
      const doc = truncationDoc(cells, ['zh_CN', 'de_DE', 'pseudo'], coverage);
      const ok = JSON.stringify(doc.langs) === '["zh_CN","de_DE"]' && doc.items.length === 1
        && ['lang', 'page', 'case', 'size', 'selector', 'id'].every(k => k in doc.items[0]) && doc.coverage.complete === false;
      record('truncation_doc', ok, doc);
    });
  } finally {
    await browser.close();
    rmSync(tmp, { recursive: true, force: true });
    rmSync(distDir, { recursive: true, force: true });
  }
  const ok = blocks.every(b => b.ok);
  writeFileSync(join(outDir, 'selftest.json'), JSON.stringify({ ok, source_digest: manifest.source_digest, blocks }, null, 1) + '\n');
  console.log(`${ok ? 'self-test: OK' : 'self-test: FAILED'} — ${blocks.filter(b => b.ok).length}/${blocks.length} blocks → ${posix(relative(REPO, join(outDir, 'selftest.json')))}`);
  return ok;
}
