/* ==========================================================================
   pages/logs/export.js — the logs page's export flow (T-P4-05c; 映射_logs §4, PROTOCOL §10,
   §13 save grant). No DOM: the page turns each outcome into words.

     exportLogs(rt, {purpose, format, seqs, owner, onJob}) → Promise<outcome>
       (onJob(promise): called once the job is sent, e.g. to show its progress)
       1. host.saveFile{purpose, format}: the shell's native Save As. The page never sees a path.
            cancelled:true            → {status:'cancelled'} (nothing starts, nothing is said)
            conflict + dialog_open    → {status:'dialog_open'} (another native dialog is open;
                                        criticism B8)
            runtime_gone / owner_revoked / runtime_unavailable
                                      → {status:'interrupted', error}: the sidecar went (a new
                                        generation ends every waiter, the host call too) or the
                                        shell's channel reconnected while the dialog was open;
                                        the dialog itself did not fail (review S4)
            any other error           → {status:'dialog_failed', error}
       2. the job: logs.exportBundle{save_ref} or logs.exportFiltered{save_ref, format, seqs}.
          Filtered exports always name the seqs of the current view (R9, 决定_第五波裁定 33-#7/#15:
          the page's clear watermark is local, a filter would not see it); never a filter.
            done                      → {status:'done', result}; a filtered result's
                                        missing_seqs (G41) are the records the sidecar no
                                        longer had
            ack refused / failed      → {status:'failed', error}
            cancelled / superseded / runtime_gone / owner_revoked / runtime_unavailable
                                      → {status:'interrupted', error}
     revealExport(rt, ref, owner) → Promise<{status:'revealed'|'gone'|'failed', error?}>
       host.reveal{ref}: revealed:false or not_allowed (a ref of an earlier sidecar, §13) → gone.
   ========================================================================== */

export const FORMATS = Object.freeze({ bundle: ['zip'], filtered: ['json', 'text'] });   // U5: no CSV
export const SEQS_MAX = 4000;
const INTERRUPTED = new Set(['cancelled', 'superseded', 'runtime_gone', 'owner_revoked', 'runtime_unavailable']);
// host.saveFile ended by the runtime rather than by the dialog (rt/calls.js rejectAll,
// endUnanswered): 导出已中断, not 无法打开保存对话框
const DIALOG_INTERRUPTED = new Set(['runtime_gone', 'owner_revoked', 'runtime_unavailable']);

const codeOf = e => (e && typeof e.code === 'string' ? e.code : 'internal');

export async function exportLogs(rt, { purpose, format, seqs, owner, onJob } = {}) {
  if (purpose !== 'bundle' && purpose !== 'filtered') throw new Error('exportLogs: purpose bundle | filtered');
  const fmt = purpose === 'bundle' ? 'zip' : format;
  if (!FORMATS[purpose].includes(fmt)) throw new Error(`exportLogs: format ${fmt} is not offered for ${purpose}`);
  let list = null;
  if (purpose === 'filtered') {
    list = Array.from(seqs || []).filter(s => Number.isInteger(s) && s >= 0);
    if (!list.length) return { status: 'empty' };
    if (list.length > SEQS_MAX) list = list.slice(-SEQS_MAX);
  }
  const opts = owner ? { owner } : {};
  let save;
  try {
    save = await rt.call('host.saveFile', { purpose, format: fmt }, opts);
  } catch (e) {
    if (codeOf(e) === 'conflict' && e.reason_code === 'dialog_open') return { status: 'dialog_open', error: e };
    if (DIALOG_INTERRUPTED.has(codeOf(e))) return { status: 'interrupted', error: e };
    return { status: 'dialog_failed', error: e };
  }
  if (!save || save.cancelled || typeof save.ref !== 'string' || !save.ref) return { status: 'cancelled' };
  const cmd = purpose === 'bundle' ? 'logs.exportBundle' : 'logs.exportFiltered';
  const args = purpose === 'bundle' ? { save_ref: save.ref } : { save_ref: save.ref, format: fmt, seqs: list };
  const job = rt.call(cmd, args, opts);
  // the caller may show the job's progress (the Save As dialog is closed by now)
  if (typeof onJob === 'function') { try { onJob(job); } catch (e) { /* the export goes on */ } }
  try {
    const result = await job;
    return { status: 'done', purpose, format: fmt, requested: list ? list.length : null, result: result || {} };
  } catch (e) {
    return { status: INTERRUPTED.has(codeOf(e)) ? 'interrupted' : 'failed', error: e };
  }
}

export async function revealExport(rt, ref, owner) {
  if (typeof ref !== 'string' || !ref) return { status: 'gone' };
  try {
    const r = await rt.call('host.reveal', { ref }, owner ? { owner } : {});
    return r && r.revealed ? { status: 'revealed' } : { status: 'gone' };
  } catch (e) {
    return codeOf(e) === 'not_allowed' ? { status: 'gone', error: e } : { status: 'failed', error: e };
  }
}
