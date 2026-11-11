// The quit question and the draft registry (T-P4-06; P4 architecture section 2.9, mapping_core
// Q1, S15; PROTOCOL section 13 host:confirmQuit).
//
// T.shellc.registerDraft(fn) -> unregister (core/README.md, cross-page interfaces): a drawer or
// session with an unsaved draft registers fn() -> number | boolean (its unsaved items; true
// counts as one, false / 0 / anything else as none) while it is open and calls unregister()
// when it closes (calling it again is harmless). fn that is not a function throws TypeError.
// core.js installs this registry's register as T.shellc.registerDraft.
//
// host:confirmQuit{token}: the shell is quitting and asks how many changes are unsaved. The
// answer host_confirm_quit{token, unsaved} goes out at once (well inside the shell's 3 s):
// unsaved = T.ui.saveBar.dirtyCount() + the registered drafts' items. A hook that throws counts
// as 0. The page never opens a dialog of its own: with unsaved > 0 the shell asks natively.
// stale_token / not_confirming and every other error are silent; one answer per token.

const U32_MAX = 0xFFFFFFFF;

function itemsOf(v) {
  if (v === true) return 1;
  if (typeof v === 'number' && Number.isFinite(v) && v > 0) return Math.floor(v);
  return 0;
}

export function createDrafts() {
  const entries = new Set();
  return {
    register(fn) {
      if (typeof fn !== 'function') throw new TypeError('T.shellc.registerDraft(fn): fn must be a function');
      const e = { fn };
      entries.add(e);
      return function unregister() { entries.delete(e); };
    },
    count() {
      let n = 0;
      for (const e of Array.from(entries)) {
        try { n += itemsOf(e.fn()); } catch (err) { setTimeout(() => { throw err; }); }
      }
      return n;
    },
    get size() { return entries.size; },
  };
}

// The page's one registry (core.js and the quit answer share it).
export const drafts = createDrafts();
export const registerDraft = fn => drafts.register(fn);

// Ports: host(name, args) -> Promise, dirtyCount() -> number (T.ui.saveBar.dirtyCount),
// drafts (createDrafts()).
export function createQuit({ host, dirtyCount = () => 0, drafts: reg = drafts }) {
  const answered = new Set();
  function unsaved() {
    let bar = 0;
    try { bar = itemsOf(dirtyCount()); } catch (err) { setTimeout(() => { throw err; }); }
    return Math.min(U32_MAX, bar + reg.count());
  }
  return {
    unsaved,
    // host:confirmQuit{token, epoch}
    confirm(f) {
      if (!f || !Number.isInteger(f.token) || f.token < 0) return null;
      if (answered.has(f.token)) return null;
      answered.add(f.token);
      const n = unsaved();
      Promise.resolve(host('host_confirm_quit', { token: f.token, unsaved: n })).catch(() => {});
      return n;
    },
  };
}
