// Hook: design-target-only odds and ends (owner: T-P4-10; see boot/README.md).
//
// Render-test hooks for ui/test/render/ (P4 architecture §1.4, §12). Nothing here starts or
// changes the page: the fixture boot is boot/rt-design.js (T-P4-02a), ?lang is handled there
// too (FakeTransport rewrites hello.lang, criticism E7). This file only exposes
//
//   window.__TESSERACT_DESIGN__ = {
//     build:   {target, trace, h_features}   build constants the runner cannot read otherwise
//     fixture: bool                          the page booted in fixture mode
//     transport() -> {failures, invocations, instances} read-only copy of the bookkeeping of
//                                            every FakeTransport seen (fixture mode; unmatched
//                                            invokes are failures). Fixture mode with no
//                                            instance seen yet → failures [{kind: 'no_transport'}]
//     deliver(frame)                         fixture mode only: one Channel delivery to the
//                                            current instance, for a case's `deliver`
//   }
//
// The FakeTransport instance is handed over by boot/rt-design.js as T.rt.__fake (T-P4-H;
// 决定_第五波裁定 10-4); this file reads it when asked and never wraps FakeTransport.prototype.
// A transport built later (a rebuilt T.rt with its own __fake) is picked up on the next read;
// the instances seen are kept, so their failures still count. Instances registered in
// window.__TESSERACT_FAKES__ by boot/rt-design.js (handOver) are taken from there as well, so
// one replaced before any read is not lost (T-P4-H fix round 1, review S7-1).
import { parseFeatureDefine } from '../rt/features.js';

const fixture = !!window.__TESSERACT_FIXTURE__;
const seen = []; // every FakeTransport handed over, in order of registration / first read

// The current instance (fixture mode only: in sim mode the bookkeeping stays empty, as before).
function current() {
  const T = window.T;
  const fake = fixture && T && T.rt ? T.rt.__fake : null;
  if (fixture) for (const f of window.__TESSERACT_FAKES__ || []) if (f && !seen.includes(f)) seen.push(f);
  if (fake && !seen.includes(fake)) seen.push(fake);
  return fake || null;
}

const copy = v => (v === undefined ? v : JSON.parse(JSON.stringify(v)));

Object.defineProperty(window, '__TESSERACT_DESIGN__', {
  configurable: false,
  enumerable: false,
  writable: false,
  value: Object.freeze({
    build: Object.freeze({
      target: __TARGET__,
      trace: typeof __I18N_TRACE__ !== 'undefined' ? !!__I18N_TRACE__ : false,
      h_features: parseFeatureDefine(__H_FEATURES__),
    }),
    fixture,
    transport() {
      current();
      if (!seen.length) return { failures: fixture ? [{ kind: 'no_transport' }] : [], invocations: [], instances: 0 };
      return { failures: copy(seen.flatMap(t => t.failures || [])), invocations: copy(seen.flatMap(t => t.invocations || [])), instances: seen.length };
    },
    deliver(frame) {
      const fake = current();
      if (!fake) throw new Error('__TESSERACT_DESIGN__.deliver: no fixture transport');
      fake.deliver(frame);
    },
  }),
});
