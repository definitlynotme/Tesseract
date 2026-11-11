// V11: what the self-hosted fonts really cover (T-P4-08a; P4 architecture §8).
//
//   node ui/tools/font_cmap.mjs --report     coverage report + css/fonts.css consistency check
//   node ui/tools/font_cmap.mjs --emit-css   print the @font-face rules css/fonts.css should hold
//   node ui/tools/font_cmap.mjs <file.woff2> list one file's mapped code points as a unicode-range
//
// Reads WOFF2 with Node's own zlib.brotliDecompressSync and parses only the table
// directory and the cmap and name tables (no new dependency). --report prints, per font
// family, how many code points of U+0400–04FF (Cyrillic) and U+0370–03FF (Greek) are
// mapped, both for the files the build ships and for every subset file in the Fontsource
// package (a local font has no package: its file, how many code points and ideographs it
// maps, and its family name), and then checks every @font-face in css/fonts.css: the woff2
// it names is the one the build ships, and its unicode-range equals (cmap ∩ Fontsource
// subset range), for a local face its whole cmap, so no declared code point is missing from
// the font. Exit status 1 when a check fails.
import { brotliDecompressSync } from 'node:zlib';
import { existsSync, readFileSync, readdirSync } from 'node:fs';
import { basename, dirname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { FONT_FACES, FONT_PACKAGES, fontSourcePath, packageDir, packageOf } from '../build/fonts.mjs';

export const UI_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
export const BLOCKS = [
  { name: 'Cyrillic', from: 0x0400, to: 0x04FF },
  { name: 'Greek', from: 0x0370, to: 0x03FF },
];

// ---- WOFF2 --------------------------------------------------------------------------
const KNOWN_TAGS = ['cmap', 'head', 'hhea', 'hmtx', 'maxp', 'name', 'OS/2', 'post', 'cvt ', 'fpgm', 'glyf', 'loca', 'prep',
  'CFF ', 'VORG', 'EBDT', 'EBLC', 'gasp', 'hdmx', 'kern', 'LTSH', 'PCLT', 'VDMX', 'vhea', 'vmtx', 'BASE', 'GDEF', 'GPOS',
  'GSUB', 'EBSC', 'JSTF', 'MATH', 'CBDT', 'CBLC', 'COLR', 'CPAL', 'SVG ', 'sbix', 'acnt', 'avar', 'bdat', 'bloc', 'bsln',
  'cvar', 'fdsc', 'feat', 'fmtx', 'fvar', 'gvar', 'hsty', 'just', 'lcar', 'mort', 'morx', 'opbd', 'prop', 'trak', 'Zapf',
  'Silf', 'Glat', 'Gloc', 'Feat', 'Sill'];

function base128(buf, pos) {
  let v = 0;
  for (let i = 0; i < 5; i++) {
    const b = buf[pos.i++];
    if (i === 0 && b === 0x80) throw new Error('woff2: UIntBase128 with a leading zero');
    if (v & 0xFE000000) throw new Error('woff2: UIntBase128 overflow');
    v = (v << 7) | (b & 0x7F);
    if (!(b & 0x80)) return v >>> 0;
  }
  throw new Error('woff2: UIntBase128 longer than 5 bytes');
}

// Table directory + decompressed table stream. Returns Map(tag → Buffer) of the tables as
// stored (glyf/loca/hmtx may be in transformed form; cmap never is).
export function woff2Tables(buf) {
  buf = Buffer.from(buf);
  if (buf.toString('latin1', 0, 4) !== 'wOF2') throw new Error('not a WOFF2 file');
  const flavor = buf.toString('latin1', 4, 8);
  if (flavor === 'ttcf') throw new Error('woff2: font collections are not supported');
  const numTables = buf.readUInt16BE(12);
  const compressed = buf.readUInt32BE(20);
  const pos = { i: 48 };
  const dir = [];
  for (let n = 0; n < numTables; n++) {
    const flags = buf[pos.i++];
    const tag = (flags & 0x3F) === 0x3F ? buf.toString('latin1', pos.i, (pos.i += 4)) : KNOWN_TAGS[flags & 0x3F];
    if (!tag) throw new Error(`woff2: unknown table tag index ${flags & 0x3F}`);
    const version = flags >> 6;
    const origLength = base128(buf, pos);
    const transformed = tag === 'glyf' || tag === 'loca' ? version === 0 : version !== 0;
    const length = transformed ? base128(buf, pos) : origLength;
    dir.push({ tag, length });
  }
  const stream = brotliDecompressSync(buf.subarray(pos.i, pos.i + compressed));
  const tables = new Map();
  let off = 0;
  for (const t of dir) { tables.set(t.tag, stream.subarray(off, off + t.length)); off += t.length; }
  if (off > stream.length) throw new Error('woff2: table stream shorter than the directory says');
  return tables;
}

// ---- cmap ---------------------------------------------------------------------------
function subtableCodepoints(t, at, out) {
  const format = t.readUInt16BE(at);
  if (format === 4) {
    const segX2 = t.readUInt16BE(at + 6);
    const ends = at + 14, starts = ends + segX2 + 2, deltas = starts + segX2, ranges = deltas + segX2;
    for (let s = 0; s < segX2; s += 2) {
      const end = t.readUInt16BE(ends + s), start = t.readUInt16BE(starts + s);
      const delta = t.readInt16BE(deltas + s), ro = t.readUInt16BE(ranges + s);
      for (let c = start; c <= end && c !== 0xFFFF; c++) {
        let g;
        if (ro === 0) g = (c + delta) & 0xFFFF;
        else {
          g = t.readUInt16BE(ranges + s + ro + 2 * (c - start));
          if (g !== 0) g = (g + delta) & 0xFFFF;
        }
        if (g !== 0) out.add(c);
      }
    }
  } else if (format === 12 || format === 13) {
    const groups = t.readUInt32BE(at + 12);
    for (let i = 0; i < groups; i++) {
      const g = at + 16 + 12 * i;
      const start = t.readUInt32BE(g), end = t.readUInt32BE(g + 4), gid = t.readUInt32BE(g + 8);
      for (let c = start; c <= end; c++) if (format === 13 ? gid !== 0 : gid + (c - start) !== 0) out.add(c);
    }
  } else if (format === 6) {
    const first = t.readUInt16BE(at + 6), count = t.readUInt16BE(at + 8);
    for (let i = 0; i < count; i++) if (t.readUInt16BE(at + 10 + 2 * i) !== 0) out.add(first + i);
  } else if (format === 0) {
    for (let c = 0; c < 256; c++) if (t[at + 6 + c] !== 0) out.add(c);
  }
  // format 14 (variation sequences) maps no base code points; others do not occur in these fonts.
}

// Unicode code points the font maps to a real glyph (gid ≠ 0), sorted ascending: the union
// of every Unicode cmap subtable (platform 0, and platform 3 encodings 1 and 10).
export function cmapCodepoints(woff2) {
  const cmap = woff2Tables(woff2).get('cmap');
  if (!cmap) throw new Error('woff2: no cmap table');
  const out = new Set();
  const n = cmap.readUInt16BE(2);
  let unicode = 0;
  for (let i = 0; i < n; i++) {
    const rec = 4 + 8 * i;
    const platform = cmap.readUInt16BE(rec), encoding = cmap.readUInt16BE(rec + 2), offset = cmap.readUInt32BE(rec + 4);
    if (platform === 0 || (platform === 3 && (encoding === 1 || encoding === 10))) {
      unicode++;
      subtableCodepoints(cmap, offset, out);
    }
  }
  if (!unicode) throw new Error('woff2: cmap has no Unicode subtable');
  return [...out].sort((a, b) => a - b);
}

// ---- name ---------------------------------------------------------------------------
// The name table's records: [{platform, encoding, language, nameId, text}]; text is decoded
// for the Unicode and Windows platforms (UTF-16BE), null for any other.
export function nameRecords(woff2) {
  const t = woff2Tables(woff2).get('name');
  if (!t) throw new Error('woff2: no name table');
  const count = t.readUInt16BE(2), strings = t.readUInt16BE(4);
  const out = [];
  for (let i = 0; i < count; i++) {
    const r = 6 + 12 * i;
    const platform = t.readUInt16BE(r), encoding = t.readUInt16BE(r + 2), language = t.readUInt16BE(r + 4);
    const nameId = t.readUInt16BE(r + 6), length = t.readUInt16BE(r + 8), offset = t.readUInt16BE(r + 10);
    const raw = Buffer.from(t.subarray(strings + offset, strings + offset + length));
    const text = (platform === 0 || platform === 3) && length % 2 === 0 ? raw.swap16().toString('utf16le') : null;
    out.push({ platform, encoding, language, nameId, text });
  }
  return out;
}

// ---- unicode-range ------------------------------------------------------------------
const hex = c => c.toString(16).toUpperCase().padStart(4, '0');

export function parseUnicodeRange(text) {
  const out = new Set();
  for (const raw of String(text).split(',')) {
    const part = raw.trim().toUpperCase();
    if (!part) continue;
    const m = /^U\+([0-9A-F?]{1,6})(?:-([0-9A-F]{1,6}))?$/.exec(part);
    if (!m) throw new Error(`bad unicode-range item: ${raw}`);
    let lo, hi;
    if (m[1].includes('?')) {
      if (m[2]) throw new Error(`bad unicode-range item: ${raw}`);
      lo = parseInt(m[1].replace(/\?/g, '0'), 16); hi = parseInt(m[1].replace(/\?/g, 'F'), 16);
    } else { lo = parseInt(m[1], 16); hi = m[2] ? parseInt(m[2], 16) : lo; }
    if (hi < lo) throw new Error(`bad unicode-range item: ${raw}`);
    for (let c = lo; c <= hi; c++) out.add(c);
  }
  return [...out].sort((a, b) => a - b);
}

export function formatUnicodeRange(codepoints) {
  const cps = [...new Set(codepoints)].sort((a, b) => a - b);
  const parts = [];
  for (let i = 0; i < cps.length;) {
    let j = i;
    while (j + 1 < cps.length && cps[j + 1] === cps[j] + 1) j++;
    parts.push(i === j ? `U+${hex(cps[i])}` : `U+${hex(cps[i])}-${hex(cps[j])}`);
    i = j + 1;
  }
  return parts.join(', ');
}

const inBlock = (cps, b) => cps.filter(c => c >= b.from && c <= b.to);
const isIdeograph = c => (c >= 0x3400 && c <= 0x4DBF) || (c >= 0x4E00 && c <= 0x9FFF);

// The Fontsource subset ranges of a package (unicode.json).
export function subsetRanges(uiRoot, pkg) {
  return JSON.parse(readFileSync(join(packageDir(uiRoot, pkg), 'unicode.json'), 'utf8'));
}

// What a face's unicode-range must be: cmap ∩ the Fontsource subset range; for a local face
// (no package, the file is already cut to what it must cover) its whole cmap.
export function expectedRange(uiRoot, face) {
  if (face.local) return cmapCodepoints(readFileSync(fontSourcePath(uiRoot, face)));
  const subset = new Set(parseUnicodeRange(subsetRanges(uiRoot, face.pkg)[face.subset]));
  return cmapCodepoints(readFileSync(fontSourcePath(uiRoot, face))).filter(c => subset.has(c));
}

// Where a face comes from, for the comment above its rule.
function origin(face) {
  if (!face.local) return face.pkg;
  const p = packageOf(face);
  return `local: subset of ${p.upstream} ${p.version} (${p.tool})`;
}

// ---- css/fonts.css ------------------------------------------------------------------
// [{family, weight, style, display, url, file, range: string, codepoints: number[]}]
export function parseFontFaces(css) {
  const body = String(css).replace(/\/\*[\s\S]*?\*\//g, '');
  const faces = [];
  for (const m of body.matchAll(/@font-face\s*\{([^}]*)\}/g)) {
    const decl = {};
    for (const d of m[1].split(';')) {
      const i = d.indexOf(':');
      if (i > 0) decl[d.slice(0, i).trim().toLowerCase()] = d.slice(i + 1).trim();
    }
    const urls = [...(decl.src || '').matchAll(/url\(\s*(["']?)([^"')]+)\1\s*\)/g)].map(u => u[2]);
    faces.push({
      family: (decl['font-family'] || '').replace(/^["']|["']$/g, ''),
      weight: decl['font-weight'] || '', style: decl['font-style'] || '', display: decl['font-display'] || '',
      src: decl.src || '', urls, url: urls[0] || '', file: urls[0] ? basename(urls[0]) : '',
      range: decl['unicode-range'] || '', codepoints: decl['unicode-range'] ? parseUnicodeRange(decl['unicode-range']) : [],
    });
  }
  return faces;
}

export function emitCss(uiRoot = UI_ROOT) {
  return FONT_FACES.map(f => [
    `/* ${f.family} ${f.weight} · ${f.subset} · ${origin(f)} */`,
    '@font-face {',
    `  font-family: "${f.family}";`,
    '  font-style: normal;',
    `  font-weight: ${f.weight};`,
    '  font-display: swap;',
    `  src: url("../fonts/${f.file}") format("woff2");`,
    `  unicode-range: ${formatUnicodeRange(expectedRange(uiRoot, f))};`,
    '}',
  ].join('\n')).join('\n\n') + '\n';
}

// Problems with css/fonts.css (empty = consistent with the fonts the build ships).
// `cssText` replaces the file's contents (used by the tests to feed broken variants).
export function checkFontsCss(uiRoot = UI_ROOT, cssText = null) {
  const problems = [];
  const cssPath = join(uiRoot, 'css', 'fonts.css');
  if (cssText == null && !existsSync(cssPath)) return [`${cssPath} is missing`];
  const faces = parseFontFaces(cssText ?? readFileSync(cssPath, 'utf8'));
  if (faces.length !== FONT_FACES.length) problems.push(`fonts.css has ${faces.length} @font-face rules, the build ships ${FONT_FACES.length} files`);
  for (const f of FONT_FACES) {
    const face = faces.find(x => x.file === f.file);
    if (!face) { problems.push(`fonts.css has no @font-face for ${f.file}`); continue; }
    const where = `fonts.css ${f.file}`;
    if (face.urls.length !== 1 || face.url !== `../fonts/${f.file}`) problems.push(`${where}: src must be the single url("../fonts/${f.file}")`);
    if (face.family !== f.family) problems.push(`${where}: font-family ${face.family} ≠ ${f.family}`);
    if (face.weight !== f.weight) problems.push(`${where}: font-weight ${face.weight} ≠ ${f.weight}`);
    if (face.display !== 'swap') problems.push(`${where}: font-display must be swap`);
    if (!face.range) { problems.push(`${where}: unicode-range missing`); continue; }
    if (f.local && !existsSync(fontSourcePath(uiRoot, f))) { problems.push(`ui/fonts/${f.file} is missing (a source file of a local font; ${packageOf(f).tool} writes it)`); continue; }
    const cmap = new Set(cmapCodepoints(readFileSync(fontSourcePath(uiRoot, f))));
    const beyond = face.codepoints.filter(c => !cmap.has(c));
    if (beyond.length) problems.push(`${where}: unicode-range declares ${beyond.length} code points the font does not map (${formatUnicodeRange(beyond).slice(0, 120)})`);
    const want = formatUnicodeRange(expectedRange(uiRoot, f));
    if (formatUnicodeRange(face.codepoints) !== want) problems.push(`${where}: unicode-range ≠ ${f.local ? 'the cmap of the local font' : `cmap ∩ ${f.subset} subset`} (expected ${want.slice(0, 120)}…)`);
    if (f.local) continue; // the file in ui/fonts/ is the source itself
    const copy = join(uiRoot, 'fonts', f.file);
    if (existsSync(copy) && !readFileSync(copy).equals(readFileSync(fontSourcePath(uiRoot, f)))) problems.push(`ui/fonts/${f.file} differs from node_modules (stale copy; rebuild)`);
  }
  for (const face of faces) if (!FONT_FACES.some(f => f.file === face.file)) problems.push(`fonts.css names ${face.file || '(no url)'}, which the build does not ship`);
  return problems;
}

// Coverage of the two blocks, for the shipped files and every subset file of the packages.
// A local font has no package: its row covers the shipped files only (package* and
// subsets are null) and adds how many code points and ideographs they map, and the family
// name in the file.
export function coverage(uiRoot = UI_ROOT) {
  const rows = [];
  for (const p of FONT_PACKAGES) {
    if (p.local) {
      const shipped = FONT_FACES.filter(f => f.local && packageOf(f) === p);
      const files = shipped.map(f => readFileSync(fontSourcePath(uiRoot, f)));
      const cps = [...new Set(files.flatMap(b => cmapCodepoints(b)))].sort((a, b) => a - b);
      rows.push({
        family: p.family, pkg: null, local: true, version: p.version, license: p.license, upstream: p.upstream, tool: p.tool,
        shipped: shipped.map(f => f.file),
        names: [...new Set(files.flatMap(b => nameRecords(b).filter(r => r.nameId === 1 && r.text != null).map(r => r.text)))],
        codepoints: cps.length, ideographs: cps.filter(isIdeograph).length,
        shippedBlocks: Object.fromEntries(BLOCKS.map(b => [b.name, inBlock(cps, b).length])),
        packageBlocks: null, packageRanges: null, subsets: null,
      });
      continue;
    }
    const version = JSON.parse(readFileSync(join(packageDir(uiRoot, p.pkg), 'package.json'), 'utf8')).version;
    const shipped = FONT_FACES.filter(f => f.pkg === p.pkg);
    const shippedCps = new Set(shipped.flatMap(f => cmapCodepoints(readFileSync(fontSourcePath(uiRoot, f)))));
    const bySubset = new Map();
    const dir = join(packageDir(uiRoot, p.pkg), 'files');
    const subsets = Object.keys(subsetRanges(uiRoot, p.pkg)).sort((a, b) => b.length - a.length);
    for (const name of readdirSync(dir).filter(n => n.endsWith('.woff2')).sort()) {
      const rest = name.slice(p.id.length + 1);
      const subset = subsets.find(s => rest.startsWith(s + '-')) || '?';
      const set = bySubset.get(subset) || new Set();
      for (const c of cmapCodepoints(readFileSync(join(dir, name)))) set.add(c);
      bySubset.set(subset, set);
    }
    const all = new Set([...bySubset.values()].flatMap(s => [...s]));
    rows.push({
      family: p.family, pkg: p.pkg, version, license: p.license,
      shipped: shipped.map(f => f.file),
      shippedBlocks: Object.fromEntries(BLOCKS.map(b => [b.name, inBlock([...shippedCps], b).length])),
      packageBlocks: Object.fromEntries(BLOCKS.map(b => [b.name, inBlock([...all].sort((x, y) => x - y), b).length])),
      packageRanges: Object.fromEntries(BLOCKS.map(b => [b.name, formatUnicodeRange(inBlock([...all], b))])),
      subsets: Object.fromEntries([...bySubset].map(([s, set]) => [s, Object.fromEntries(BLOCKS.map(b => [b.name, inBlock([...set], b).length]))])),
    });
  }
  return rows;
}

function report(uiRoot) {
  const lines = [];
  for (const r of coverage(uiRoot)) {
    if (r.local) {
      lines.push(`== ${r.family} (local subset of ${r.upstream} ${r.version}, ${r.license}; ${r.tool})`);
      lines.push(`   shipped: ${r.shipped.join(', ')}: ${r.codepoints} code points, ${r.ideographs} of them CJK ideographs; name ID 1 ${r.names.map(n => JSON.stringify(n)).join(', ') || '(none)'}`);
      for (const b of BLOCKS) lines.push(`   ${b.name} U+${hex(b.from)}-${hex(b.to)}: shipped ${r.shippedBlocks[b.name]}/${b.to - b.from + 1}`);
      continue;
    }
    lines.push(`== ${r.family} (${r.pkg}@${r.version}, ${r.license})`);
    lines.push(`   shipped: ${r.shipped.join(', ')}`);
    for (const b of BLOCKS) {
      const size = b.to - b.from + 1;
      lines.push(`   ${b.name} U+${hex(b.from)}-${hex(b.to)}: shipped ${r.shippedBlocks[b.name]}/${size}; whole package ${r.packageBlocks[b.name]}/${size}${r.packageRanges[b.name] ? ` (${r.packageRanges[b.name]})` : ''}`);
    }
    lines.push(`   package subsets: ${Object.entries(r.subsets).map(([s, v]) => `${s}[${BLOCKS.map(b => `${b.name} ${v[b.name]}`).join(', ')}]`).join('; ')}`);
  }
  const problems = checkFontsCss(uiRoot);
  lines.push(`== css/fonts.css: ${problems.length ? 'MISMATCH' : 'unicode-range = cmap ∩ subset (a local face: its cmap) for every @font-face; nothing declared beyond the cmap'}`);
  for (const p of problems) lines.push(`   ${p}`);
  return { text: lines.join('\n'), ok: problems.length === 0 };
}

export function main(argv = process.argv.slice(2)) {
  if (argv[0] === '--report' && argv.length === 1) {
    const r = report(UI_ROOT);
    console.log(r.text);
    return r.ok ? 0 : 1;
  }
  if (argv[0] === '--emit-css' && argv.length === 1) { process.stdout.write(emitCss(UI_ROOT)); return 0; }
  if (argv.length === 1 && !argv[0].startsWith('--')) {
    console.log(formatUnicodeRange(cmapCodepoints(readFileSync(argv[0]))));
    return 0;
  }
  console.error('usage: node ui/tools/font_cmap.mjs --report | --emit-css | <file.woff2>');
  return 2;
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  try { process.exitCode = main(); } catch (e) { console.error(`font_cmap.mjs: ${e && e.stack || e}`); process.exitCode = 1; }
}
