// The counting stand-in for `node:test` (T-P6-05, ui/tools/guard/unit_collect.mjs). test() / it()
// register a case of the calling test file and never run it; describe() / suite() run their body so
// the cases inside are registered (and are not cases themselves, as in node's own count); hooks
// and mock are inert. Registrations go to globalThis.__guardUnitCases (file URL -> count).
const cases = (globalThis.__guardUnitCases ||= new Map());
const FILE_RE = /(file:\/\/\/[^\s)]*?\/ui\/test\/unit\/[^/\s)]+\.test\.mjs)/;

function caller() {
  const stack = new Error().stack || '';
  for (const line of stack.split('\n')) {
    const m = FILE_RE.exec(line);
    if (m) return m[1];
  }
  return '?';
}

function register() {
  const file = caller();
  cases.set(file, (cases.get(file) || 0) + 1);
}

function makeTest() {
  const fn = (..._args) => { register(); return Promise.resolve(); };
  fn.skip = fn; fn.todo = fn; fn.only = fn;
  return fn;
}

function makeSuite() {
  const fn = (...args) => {
    const body = args.find(a => typeof a === 'function');
    if (body) body({});
    return Promise.resolve();
  };
  fn.skip = fn; fn.todo = fn; fn.only = fn;
  return fn;
}

const inert = () => {};
const mockProxy = new Proxy(function () {}, { get: () => mockProxy, apply: () => mockProxy });

export const test = makeTest();
export const it = test;
export const describe = makeSuite();
export const suite = describe;
export const before = inert;
export const after = inert;
export const beforeEach = inert;
export const afterEach = inert;
export const mock = mockProxy;
export const snapshot = mockProxy;
export const run = inert;
export default test;
