// Paged snapshots and id-keyed patches (T-P4-02a; PROTOCOL sections 4 and 11, P4 architecture
// section 2.5). applyPatch is the same merge as bridge_protocol.apply_patch: the unit tests
// run both over one vector set exported by ui/tools/validate_ui_fixtures.py.

const isInt = v => Number.isInteger(v) && v >= 0;
const isStr = v => typeof v === 'string' && v.length > 0; // bridge_protocol._string: non-empty

// bridge_protocol._patch_lists_valid
export function patchListsValid(changed, removed) {
  if (!Array.isArray(changed) || !Array.isArray(removed)) return false;
  if (changed.some(it => !it || typeof it !== 'object' || Array.isArray(it) || !isStr(it.id))) return false;
  if (removed.some(id => !isStr(id))) return false;
  const ids = changed.map(it => it.id);
  if (new Set(ids).size !== ids.length || new Set(removed).size !== removed.length) return false;
  return !ids.some(id => removed.includes(id));
}

const copy = v => JSON.parse(JSON.stringify(v));

// bridge_protocol.apply_patch(baseline {rev, items}, patch {base_rev, rev, changed, removed})
// -> {merged: {rev, items} | null, needsSnapshot: bool}. Inputs are never modified. Order:
// surviving rows keep their place, replaced rows stay in place, new rows are appended.
export function applyPatch(baseline, patch) {
  const fail = { merged: null, needsSnapshot: true };
  if (!baseline || typeof baseline !== 'object' || !patch || typeof patch !== 'object') return fail;
  const { rev: cur } = baseline;
  const { base_rev: base, rev: next, changed, removed } = patch;
  const old = baseline.items;
  if (!isInt(cur) || !isInt(base) || !isInt(next) || base !== cur || next <= base
    || !Array.isArray(old) || !Array.isArray(changed) || !Array.isArray(removed)) return fail;
  if (old.some(it => !it || typeof it !== 'object' || Array.isArray(it) || !isStr(it.id)) || !patchListsValid(changed, removed)) return fail;
  const ids = old.map(it => it.id);
  if (new Set(ids).size !== ids.length) return fail;
  const rows = new Map(old.map(it => [it.id, copy(it)]));
  for (const id of removed) rows.delete(id);
  for (const it of changed) rows.set(it.id, copy(it));
  return { merged: { rev: next, items: [...rows.values()] }, needsSnapshot: false };
}

// One page group per (topic, key): pages must share snapshot_id and rev, run 0, 1, 2, ...
// and end with complete=true; only then is the whole list installed at once. Anything else
// (out of order, a missing page, a different snapshot_id / rev) discards the group.
export function createPageBuffer() {
  let group = null;
  return {
    get active() { return !!group; },
    clear() { group = null; },
    // -> {status:'buffering'} | {status:'complete', rev, items} | {status:'broken', reason}
    add(frame) {
      const page = frame.page || {};
      const items = frame.data && Array.isArray(frame.data.items) ? frame.data.items : null;
      if (!isStr(page.snapshot_id) || !isInt(page.index) || typeof page.complete !== 'boolean' || !items) {
        group = null; return { status: 'broken', reason: 'page_shape' };
      }
      if (page.index === 0) group = { id: page.snapshot_id, rev: frame.rev, next: 0, items: [] };
      if (!group) return { status: 'broken', reason: 'page_missing_start' };
      if (page.snapshot_id !== group.id || frame.rev !== group.rev || page.index !== group.next) {
        const reason = page.snapshot_id !== group.id || frame.rev !== group.rev ? 'page_mixed_group' : 'page_out_of_order';
        group = null; return { status: 'broken', reason };
      }
      group.items.push(...items);
      group.next++;
      if (!page.complete) return { status: 'buffering' };
      const done = { status: 'complete', rev: group.rev, items: group.items };
      group = null;
      const seen = new Set();
      for (const it of done.items) {
        if (!it || !isStr(it.id) || seen.has(it.id)) return { status: 'broken', reason: 'page_duplicate_id' };
        seen.add(it.id);
      }
      return done;
    },
  };
}
