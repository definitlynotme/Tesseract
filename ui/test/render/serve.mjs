// Loopback static server for the render matrix's CSP run (T-P4-08b; P4 architecture §9,
// criticism E6).
//
// Why not file://: on a file: page `'self'` does not match the way it does on the shell's real
// origin (http://tauri.localhost/ as the T-P4-08b e2e saw the served document), so a strict policy proven on file: says little about the
// shell. This server gives the design build a real http origin: node:http on 127.0.0.1, a random
// port, one root directory (a snapshot of ui/dist/design/), nothing else.
//
//   const srv = await serveStatic(root, { csp });   // srv.origin = 'http://127.0.0.1:<port>'
//   …
//   await srv.close();
//
// Rules:
//   - binds 127.0.0.1 only, port 0 (the OS picks a free one);
//   - GET / HEAD only (405 otherwise); the Host header must be this origin's host:port (421
//     otherwise: no DNS-rebound name reaches the files);
//   - the path is percent-decoded once and must stay inside `root` after normalisation: `..`
//     segments, backslashes, NUL, drive letters / absolute paths and symlinks leading out of the
//     root are refused (403); directories are never listed (404); `/` is index.html;
//   - with `csp`, every .html response gets `<meta http-equiv="Content-Security-Policy">` as the
//     first element of <head> (the policy the shell sends; the run decides which); no other
//     rewriting;
//   - `X-Content-Type-Options: nosniff`, `Cache-Control: no-store`; a fixed MIME table (an unknown
//     extension is application/octet-stream).
// srv.log keeps {method, path, status} of every request (the run reports 4xx / 5xx).
import { createServer } from 'node:http';
import { realpathSync, statSync, readFileSync } from 'node:fs';
import { extname, isAbsolute, normalize, relative, resolve } from 'node:path';

export const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.map': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.ico': 'image/x-icon',
  '.woff2': 'font/woff2',
  '.woff': 'font/woff',
  '.txt': 'text/plain; charset=utf-8',
};

/** The CSP <meta> as the first element of <head>; throws when there is no <head>. */
export function injectCspMeta(html, policy) {
  if (/["<>]/.test(policy)) throw new Error('CSP policy must not contain " < >');
  if (!/<head(\s[^>]*)?>/i.test(html)) throw new Error('index.html has no <head>');
  return html.replace(/<head(\s[^>]*)?>/i, m => `${m}\n<meta http-equiv="Content-Security-Policy" content="${policy}">`);
}

/**
 * The file under `root` for a request path, or {status} when it is refused / missing.
 * `rootReal` is realpath(root).
 */
export function resolveRequestPath(rootReal, urlPath) {
  let p;
  try { p = decodeURIComponent(String(urlPath).split('?')[0].split('#')[0]); } catch (e) { return { status: 400 }; }
  if (p.includes('\0') || p.includes('\\') || !p.startsWith('/')) return { status: 403 };
  if (p.split('/').some(seg => seg === '..' || seg === '.')) return { status: 403 };
  if (/^\/+[A-Za-z]:/.test(p) || p.startsWith('//')) return { status: 403 };
  if (p.endsWith('/')) p += 'index.html';
  const rel = normalize(p.slice(1));
  if (!rel || isAbsolute(rel) || rel.startsWith('..')) return { status: 403 };
  const full = resolve(rootReal, rel);
  const inside = f => { const r = relative(rootReal, f); return !!r && !r.startsWith('..') && !isAbsolute(r); };
  if (!inside(full)) return { status: 403 };
  let real;
  try { real = realpathSync(full); } catch (e) { return { status: 404 }; }
  if (!inside(real)) return { status: 403 };
  try { if (!statSync(real).isFile()) return { status: 404 }; } catch (e) { return { status: 404 }; }
  return { file: real };
}

export function serveStatic(root, { csp = null, host = '127.0.0.1' } = {}) {
  if (host !== '127.0.0.1') throw new Error('serveStatic binds 127.0.0.1 only');
  const rootReal = realpathSync(resolve(root));
  if (!statSync(rootReal).isDirectory()) throw new Error(`${root} is not a directory`);
  const log = [];
  let origin = null, hostHeader = null;
  const server = createServer((req, res) => {
    const done = (status, body = '', headers = {}) => {
      log.push({ method: req.method, path: String(req.url).slice(0, 200), status });
      res.writeHead(status, { 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff', ...headers });
      res.end(req.method === 'HEAD' ? undefined : body);
    };
    if (req.method !== 'GET' && req.method !== 'HEAD') return done(405, 'method not allowed', { Allow: 'GET, HEAD', 'Content-Type': 'text/plain' });
    if (String(req.headers.host || '').toLowerCase() !== hostHeader) return done(421, 'misdirected request', { 'Content-Type': 'text/plain' });
    const r = resolveRequestPath(rootReal, req.url || '/');
    if (!r.file) return done(r.status, String(r.status), { 'Content-Type': 'text/plain' });
    const ext = extname(r.file).toLowerCase();
    let body;
    try { body = readFileSync(r.file); } catch (e) { return done(404, '404', { 'Content-Type': 'text/plain' }); }
    if (csp && ext === '.html') body = Buffer.from(injectCspMeta(body.toString('utf8'), csp), 'utf8');
    return done(200, body, { 'Content-Type': MIME[ext] || 'application/octet-stream', 'Content-Length': String(body.length) });
  });
  return new Promise((ok, bad) => {
    server.once('error', bad);
    server.listen(0, host, () => {
      const { port } = server.address();
      origin = `http://${host}:${port}`;
      hostHeader = `${host}:${port}`;
      ok({
        origin, port, root: rootReal, log,
        url: (path = '/') => origin + (path.startsWith('/') ? path : '/' + path),
        close: () => new Promise(r => { server.closeAllConnections?.(); server.close(() => r()); }),
      });
    });
  });
}
