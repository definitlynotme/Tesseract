// Module hooks for ui/tools/guard/unit_collect.mjs (T-P6-05): inside ui/test/unit, `node:test`
// resolves to the counting stand-in, so importing a test file registers its cases without running
// any of them. Everything else resolves as usual.
const UNIT = '/ui/test/unit/';

export async function initialize(data) {
  globalThis.__guardFakeTestUrl = data.fakeUrl;
}

export async function resolve(specifier, context, nextResolve) {
  if (specifier === 'node:test' && context.parentURL && context.parentURL.replace(/\\/g, '/').includes(UNIT)) {
    return { url: globalThis.__guardFakeTestUrl, shortCircuit: true };
  }
  return nextResolve(specifier, context);
}
