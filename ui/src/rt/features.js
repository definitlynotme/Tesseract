// Two-key release of prototype-only controls (T-P4-02a; P4 architecture section 5,
// criticism A2). T.rt.feature(name) is true only when
//   key 1: the sidecar declares the name in hello.caps.features, and
//   key 2: the build lists it in ENABLED_H_FEATURES (src/features.json, compiled in as the
//          frozen build constant H_FEATURES, see build.mjs defineFor).
// Names are checklist ids ('B-31'). A name missing from feature_registry.js is never on.
import { FEATURE_REGISTRY } from './feature_registry.js';

// The build constant __H_FEATURES__ is a JSON string of the name list (build.mjs defineFor);
// an array is accepted too. Anything unreadable enables nothing.
export function parseFeatureDefine(raw) {
  let v = raw;
  if (typeof v === 'string') { try { v = JSON.parse(v); } catch (e) { v = []; } }
  return Object.freeze(Array.isArray(v) ? v.filter(n => typeof n === 'string') : []);
}

export function createFeatures({ enabled = [], registry = FEATURE_REGISTRY, caps = () => null, diag = () => {} } = {}) {
  const allowed = Object.freeze([...new Set(Array.isArray(enabled) ? enabled.filter(n => typeof n === 'string') : [])]);
  const known = new Set(registry.map(r => r.name));
  const warned = new Set();
  return {
    enabled: allowed,
    registered: name => known.has(name),
    feature(name) {
      if (!known.has(name)) {
        if (!warned.has(name)) { warned.add(name); diag({ kind: 'feature_unregistered', name: String(name) }); }
        return false;
      }
      if (!allowed.includes(name)) return false;
      const c = caps();
      return !!(c && Array.isArray(c.features) && c.features.includes(name));
    },
  };
}
