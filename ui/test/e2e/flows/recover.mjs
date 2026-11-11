// Flow "recover": a fallback and its recovery across a page reload (5th-wave ruling C3-6, "reload
// then recover"; P4 architecture section 2.3 rt:replayed; rt/project.js tier level reconciliation,
// T-P4-H). With the service running on Vanguard, the stand-in sends a live tier.transition
// vanguard -> sentinel and then, as the sidecar does, a tiers.summary that carries the fallback
// (carrier sentinel, fallback true). The page reloads (full replay ending with rt:replayed): no
// rt:tier is announced live from the replay, and the cached fallback is the page's level. The next
// live tier.transition sentinel -> vanguard is announced as exactly one rt:tier
// {kind: recover, instant: false, wasFallback: true}, and the fallback banner goes.
import { check, goPage, sleep } from '../driver.mjs';

export const real = false;
export const skipReal = 'needs the stand-in sidecar (scripted tier transitions)';
export const launches = [{ args: [], env: {}, options: { phase: 'running' } }];

const transition = (from, to) => ({ op: 'push', topic: 'tier.transition', kind: 'edge', data: { from, to, observed_at_ms: Date.now(), authority: 'observed' } });

export async function run(s) {
  const c = s.checks;
  await s.reloadObserved();
  await goPage(s, 'logs');
  const base = await s.eval(() => window.T.rt.topic('tiers.summary').get());
  check(base && base.carrier === 'vanguard' && !base.fallback, `the stand-in does not start on Vanguard: ${JSON.stringify(base && { carrier: base.carrier, fallback: base.fallback })}`);
  let mark = await s.eval(() => window.__e2e.events.length);
  await s.control(transition('vanguard', 'sentinel'));
  await s.until(m => window.__e2e.events.slice(m).some(e => e.e === 'rt:tier'), 'rt:tier fallback', 10000, mark);
  await s.control({ op: 'push', topic: 'tiers.summary', kind: 'snapshot', data: { ...base, carrier: 'sentinel', fallback: true, order: ['sentinel', 'vanguard', 'bastion', 'gateway'] } });
  await sleep(500);
  c.fallback = await s.eval(m => window.__e2e.events.slice(m).filter(e => e.e === 'rt:tier').map(e => ({ kind: e.kind, instant: e.instant })), mark);
  check(c.fallback.length === 1 && c.fallback[0].kind === 'fallback' && c.fallback[0].instant === false, `live fallback: ${JSON.stringify(c.fallback)}`);
  await s.until(() => window.T.notices.banners.has('fallback'), 'the fallback banner', 5000);
  // reload: the replay carries the fallback summary
  await s.reloadObserved();
  await s.until(() => window.__e2e.events.some(e => e.e === 'rt:replayed'), 'rt:replayed', 15000);
  await sleep(500);
  c.replay_tier = await s.eval(() => window.__e2e.events.filter(e => e.e === 'rt:tier').map(e => ({ kind: e.kind, instant: e.instant })));
  check(c.replay_tier.every(e => e.instant === true), `a replayed tier frame was announced live: ${JSON.stringify(c.replay_tier)}`);
  mark = await s.eval(() => window.__e2e.events.length);
  await s.control(transition('sentinel', 'vanguard'));
  await s.until(m => window.__e2e.events.slice(m).some(e => e.e === 'rt:transition'), 'rt:transition after the reload', 10000, mark);
  await sleep(800);
  const after = await s.eval(m => ({
    transitions: window.__e2e.events.slice(m).filter(e => e.e === 'rt:transition').map(e => ({ instant: e.instant })),
    tier: window.__e2e.events.slice(m).filter(e => e.e === 'rt:tier').map(e => ({ kind: e.kind, instant: e.instant })),
  }), mark);
  c.after_reload = after;
  c.recover_banner_gone = !(await s.eval(() => window.T.notices.banners.has('fallback')));
  check(after.transitions.length === 1 && after.transitions[0].instant === false, `the transition after the reload is not live: ${JSON.stringify(after.transitions)}`);
  check(after.tier.length === 1 && after.tier[0].kind === 'recover' && after.tier[0].instant === false, `recovery after the reload: ${JSON.stringify(after.tier)}`);
  check(c.recover_banner_gone, 'the fallback banner stayed after the recovery');
  await s.shot('recovered');
}
