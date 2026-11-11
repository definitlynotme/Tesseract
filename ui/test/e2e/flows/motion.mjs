// Flow "motion": reduced motion from the shell (criticism D4; T-P4-C3 item 7; shellc/presence.js
// createReducedMotion). The shell is started with TESSERACT_REDUCED_MOTION=1: it logs
// "reduced_motion reduced=true", injects the frozen window.__TESSERACT_MOTION__ = {reduced:true}
// before the page, and the page's T.motion.reduced is true.
import { check } from '../driver.mjs';

export const real = false;
export const skipReal = 'the stand-in sidecar is enough (a shell start option only)';
export const launches = [{ args: [], env: { TESSERACT_REDUCED_MOTION: '1' }, options: { phase: 'running' } }];

export async function run(s) {
  const c = s.checks;
  await s.logWait(/^reduced_motion reduced=true$/, 'reduced_motion reduced=true', 10000);
  c.injected = await s.eval(() => ({ value: window.__TESSERACT_MOTION__, frozen: Object.isFrozen(window.__TESSERACT_MOTION__) }));
  check(c.injected.value && c.injected.value.reduced === true && c.injected.frozen, `injected motion flag ${JSON.stringify(c.injected)}`);
  c.t_motion_reduced = await s.eval(() => window.T.motion.reduced);
  check(c.t_motion_reduced === true, `T.motion.reduced is ${c.t_motion_reduced}`);
  await s.shot('reduced-motion');
}
