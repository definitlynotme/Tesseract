// Smoke cases of the render matrix (T-P4-10): the overview while running and while stopped,
// plus the unmodified running replay (one-bucket / one-point history: the chart's empty state,
// T-P4-05a; its known_fail is gone).
// Scenarios are the T-P4-02a fixtures (validated by tools/validate_ui_fixtures.py):
//   rt_connect_replay — the shell's replay of a running service (service.state running/ready)
//   rt_start_job      — a stopped service (service.start is registered but not invoked here)
//
// The overview-running case also measures the pseudo-language length model (critique E3,
// T-P4-10 requirement 6) on real overview buttons: natural width of the zh_CN label, the pseudo
// label (src/i18n/pseudo.js, the same function the build uses) and German / Russian reference
// labels, all in the button's own computed font. The German and Russian strings are reference
// translations written for this measurement only; they are not catalog entries.
import { readFileSync } from 'node:fs';
// Pseudo-cell allowances are the overview's own (T-P4-04b; 决定_第六波裁定 T-P4-04a/04b): the design-only
// 演示 badge and ellipsis-by-design overflow with named owners. host_scroll is fixed (T-P4-05a: the
// strip's segments, labels and the restart button yield inside their column), so no known_fail.
import { DEMO_BADGE, LANGS9, PSEUDO_OVERFLOW } from './overview_i18n.mjs';
// F-01: no words over the chart's grid in any of the smoke states (the paused chart is quiet)
import { chartQuiet } from './overview.mjs';

// src/*.js are untyped ES modules (no "type": "module" for ui/): load like build/locales.mjs does
const { pseudoMessage } = await import('data:text/javascript;base64,' + readFileSync(new URL('../../../src/i18n/pseudo.js', import.meta.url)).toString('base64'));

export const LENGTH_PROBES = [
  { selector: '#ov-restart .lab', zh: '重启服务', de: 'Dienst neu starten', ru: 'Перезапустить службу' },
  { selector: '#ov-refresh .lab', zh: '刷新', de: 'Aktualisieren', ru: 'Обновить' },
  { selector: '.run-seg span', zh: '停止', de: 'Beenden', ru: 'Остановить' },
  { selector: '.run-row.center span', zh: '启动服务', de: 'Dienst starten', ru: 'Запустить службу' },
];

const pseudoOf = zh => pseudoMessage(zh, 'zh').filter(p => typeof p === 'string').join('');

async function measureLengths({ page }) {
  const probes = LENGTH_PROBES.map(p => ({ ...p, pseudo: pseudoOf(p.zh) }));
  const rows = await page.evaluate(list => {
    const out = [];
    for (const p of list) {
      const el = document.querySelector(p.selector);
      if (!el || !el.checkVisibility()) continue;
      const cs = getComputedStyle(el);
      const m = document.createElement('span');
      m.style.cssText = 'position:absolute;left:-9999px;top:0;white-space:nowrap';
      for (const k of ['fontFamily', 'fontSize', 'fontWeight', 'fontStyle', 'letterSpacing', 'textTransform', 'fontFeatureSettings', 'fontVariationSettings']) m.style[k] = cs[k];
      document.body.append(m);
      const w = s => { m.textContent = s; return Math.round(m.getBoundingClientRect().width * 10) / 10; };
      const row = { selector: p.selector, zh: p.zh, pseudo: p.pseudo, de: p.de, ru: p.ru, w_zh: w(p.zh), w_pseudo: w(p.pseudo), w_de: w(p.de), w_ru: w(p.ru), chars: { zh: [...p.zh].length, pseudo: [...p.pseudo].length, de: [...p.de].length, ru: [...p.ru].length } };
      m.remove();
      row.pseudo_over_de = Math.round((row.w_pseudo / row.w_de) * 100) / 100;
      row.pseudo_over_ru = Math.round((row.w_pseudo / row.w_ru) * 100) / 100;
      out.push(row);
    }
    return out;
  }, probes);
  return { metrics: { pseudo_length: rows } };
}

// rt_connect_replay carries a one-bucket / one-point traffic.history. The prototype chart
// (pages/overview.js mono(), core.js spark) divides by n - 1 and writes "LNaN" into the path,
// which Chrome reports as a console error (a real finding for T-P4-05a: an empty / one-point
// history needs its own empty state). The smoke case is about the framework, so it replays the
// same frames with a fuller history: 12 five-minute buckets and 30 one-second points, same
// shape (PROTOCOL traffic.history: h24[{t_ms, down_bps, up_bps, sample_count}], live[{t_ms,
// down_bps, up_bps}]).
function runningScenario() {
  const sc = JSON.parse(readFileSync(new URL('../../fixtures/scenarios/rt_connect_replay.json', import.meta.url), 'utf8'));
  const step = sc.steps.find(s => s.deliver && s.deliver.topic === 'traffic.history');
  const at = step.deliver.data.sampled_at_ms;
  const wave = (i, base) => Math.round(base * (1 + 0.5 * Math.sin(i / 2)));
  step.deliver.data.h24 = Array.from({ length: 12 }, (_, i) => ({ t_ms: at - (12 - i) * 300000, down_bps: wave(i, 4096), up_bps: wave(i + 3, 1024), sample_count: 300 }));
  step.deliver.data.live = Array.from({ length: 30 }, (_, i) => ({ t_ms: at - (30 - i) * 1000, down_bps: wave(i, 8192), up_bps: wave(i + 5, 2048) }));
  return { ...sc, name: 'smoke_overview_running', desc: `${sc.desc} (smoke: fuller traffic.history)` };
}

export const PSEUDO_SMOKE = {
  leak_allow: [DEMO_BADGE],
  overflow_allow: PSEUDO_OVERFLOW,
};

const expectRun = want => async ctx => {
  const problems = [];
  const run = await ctx.page.evaluate(() => document.getElementById('app').getAttribute('data-run'));
  if (run !== want) problems.push(`#app data-run is ${run}, expected ${want}`);
  problems.push(...await ctx.page.evaluate(chartQuiet));
  const r = await measureLengths(ctx);
  return { problems, metrics: r.metrics };
};

// zh_CN cells stay strict (no allowance); the pseudo cells carry the overview's documented allowances.
// T-P5-11 (ruling 3): the real-language cells run in all nine languages, no allowance.
export default [
  { name: 'overview_running', scenario: runningScenario(), page: 'overview', langs: LANGS9, expect: expectRun('running') },
  { name: 'overview_running_pseudo', scenario: runningScenario(), page: 'overview', langs: ['pseudo'], expect: expectRun('running'), ...PSEUDO_SMOKE },
  {
    // the unmodified replay: a one-bucket / one-point traffic.history. T-P4-05a gives it the
    // chart's empty state (no "LNaN" path, no NaN on screen); strict in zh_CN, the overview's
    // pseudo allowances in pseudo
    name: 'overview_running_raw',
    scenario: 'rt_connect_replay',
    page: 'overview',
    langs: LANGS9,
    expect: expectRun('running'),
  },
  { name: 'overview_running_raw_pseudo', scenario: 'rt_connect_replay', page: 'overview', langs: ['pseudo'], expect: expectRun('running'), ...PSEUDO_SMOKE },
  { name: 'overview_stopped', scenario: 'rt_start_job', page: 'overview', langs: LANGS9, expect: expectRun('stopped') },
  { name: 'overview_stopped_pseudo', scenario: 'rt_start_job', page: 'overview', langs: ['pseudo'], expect: expectRun('stopped'), ...PSEUDO_SMOKE },
];
