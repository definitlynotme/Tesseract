// Request and owner ids (T-P4-02a; P4 architecture section 2.4, P4 contract memo
// "page request_id"). One connection number per page load (random base36); request ids are
// p<conn>.<n>. Every id must match bridge_diagnostics._REQUEST_ID, the format the sidecar
// accepts for request ids (nodes.diagnose.request_id uses the same generator).

export const REQ_ID_RE = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;
export const isReqId = s => typeof s === 'string' && REQ_ID_RE.test(s);

function randomConn(random) {
  let n = 0;
  for (let i = 0; i < 4; i++) n = n * 1679616 + Math.floor(random() * 1679616); // 36^4 per round
  return Math.abs(n).toString(36).slice(0, 10) || '0';
}

const LABEL_RE = /[^A-Za-z0-9_.:-]/g;

export function createIds({ random = Math.random, conn } = {}) {
  const c = conn != null ? String(conn) : randomConn(random);
  if (!/^[a-z0-9]{1,16}$/.test(c)) throw new Error('ids: connection number must be 1-16 base36 characters');
  let req = 0, own = 0;
  const ids = {
    conn: c,
    // p<conn>.<n>
    next() {
      const id = `p${c}.${++req}`;
      if (!isReqId(id)) throw new Error('ids: generated request id out of format');
      return id;
    },
    // Page-local owner ids (P4 architecture section 2.4: a page owner is a local notion).
    pageOwner: `page.${c}`,
    owner(label) {
      const safe = String(label == null ? '' : label).replace(LABEL_RE, '_').slice(0, 64);
      const id = `o${c}.${++own}${safe ? '.' + safe : ''}`;
      return id.slice(0, 128);
    },
  };
  return ids;
}
