// Hook: design target runtime (owner: T-P4-02a, then T-P4-02b, then T-P4-07; see boot/README.md).
// Non-fixture branch (T-P4-02b, the default: sim mode): the design simulator (design/sim.js)
// stands behind a FakeTransport in sim mode and produces v1 frames; T.rt projects them exactly
// as in the app target (P4 architecture §1.4, §2.6). The simulator's prototype API stays on T
// for the design target's demo controls (design/demo.js); its commands go through T.rt.call.
// ?lang=<lang>|pseudo rewrites hello.lang (criticism E7).
// Fixture branch (T-P4-02a, the only fixture boot, criticism C2): when the page starts with
// window.__TESSERACT_FIXTURE__ (a scenario object or its JSON text, injected by the render
// tests with page.addInitScript), T.rt replays it through FakeTransport on a fake clock.
// ?lang=<lang>|pseudo rewrites hello.lang of the replay (criticism E7).
// In fixture mode design/sim.js is still imported (it installs its prototype API on T), but the
// simulator is never started (it starts on its backend's first replay, and only the sim branch
// asks for the backend); design/demo.js leaves its controls inert in fixture mode. The simulator
// and the fixture replay never run side by side.
// T-P4-07: the thin facade and the rt-to-prototype event alias of the transition are gone with
// their files (rt/legacy_sim.js, rt/alias.js); nothing listens to the prototype's events.
// Hand-over (T-P4-H; 决定_第五波裁定 10-4): in both branches the FakeTransport instance is
// exposed as T.rt.__fake (design target only: this file is not in the app entry), so
// boot/design.js reads its bookkeeping (failures, invocations) and delivers a case's frames
// without wrapping FakeTransport.prototype. Each instance is also registered at once in
// window.__TESSERACT_FAKES__ (design target only; T-P4-H fix round 1, review S7-1), so an
// instance counts even if T.rt is rebuilt before boot/design.js first reads it; code that
// builds another FakeTransport for the design target registers it the same way (handOver).
import { designBackend } from '../design/sim.js';
import { installRt } from '../rt/client.js';
import { createFakeClock } from '../rt/clock.js';
import { parseFeatureDefine } from '../rt/features.js';
import { FakeTransport } from '../rt/transport-fake.js';

const LANGS = ['zh_CN', 'en_US', 'ja_JP', 'ko_KR', 'fr_FR', 'de_DE', 'es_ES', 'pt_BR', 'ru_RU', 'pseudo'];
const T = window.T;
function handOver(fake) {
  T.rt.__fake = fake;
  (window.__TESSERACT_FAKES__ || (window.__TESSERACT_FAKES__ = [])).push(fake);
}
let fixture = window.__TESSERACT_FIXTURE__;
if (typeof fixture === 'string') fixture = JSON.parse(fixture);
if (fixture) {
  let lang = null;
  try { lang = new URLSearchParams(location.search).get('lang'); } catch (e) { lang = null; }
  const clock = createFakeClock(0);
  installRt(T, { clock, enabledFeatures: parseFeatureDefine(__H_FEATURES__) });
  const fake = new FakeTransport({ scenario: fixture, clock, lang: LANGS.includes(lang) ? lang : null });
  handOver(fake);
  T.rt.connect(fake);
} else {
  let lang = null;
  try { lang = new URLSearchParams(location.search).get('lang'); } catch (e) { lang = null; }
  const features = parseFeatureDefine(__H_FEATURES__);
  installRt(T, { enabledFeatures: features });
  // The simulator starts on the first replay, inside connect(): the store is filled before the
  // pages render.
  const sim = designBackend(T, { features });
  const fake = new FakeTransport({ mode: 'sim', sim, lang: LANGS.includes(lang) ? lang : null });
  handOver(fake);
  T.rt.connect(fake);
}
