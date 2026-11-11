// Language file loader (T-P4-03; P4 architecture §3.6).
//
// Each language is a classic script `locale-<lang>.<hash>.js` that calls
// T.i18n.register(lang, catalog) (or queues itself on T.__i18nq when it runs before the
// runtime). Loading = inserting <script src> (allowed by script-src 'self', works from
// file://), so no fetch and no eval. The promise resolves once the script has registered
// its language, and rejects when the file is unknown, fails to load, or loads without
// registering. A failed load can be retried.

export function createLoader({ doc, files, isRegistered }) {
  const pending = new Map();
  return {
    has: lang => Object.prototype.hasOwnProperty.call(files, lang),
    load(lang) {
      if (isRegistered(lang)) return Promise.resolve(lang);
      if (pending.has(lang)) return pending.get(lang);
      const file = Object.prototype.hasOwnProperty.call(files, lang) ? files[lang] : null;
      if (!file) return Promise.reject(Object.assign(new Error(`no language file for ${lang}`), { code: 'unknown_language' }));
      const p = new Promise((resolve, reject) => {
        const s = doc.createElement('script');
        s.src = file;
        s.async = true;
        const done = () => { s.onload = s.onerror = null; if (s.parentNode) s.parentNode.removeChild(s); };
        s.onload = () => {
          done();
          if (isRegistered(lang)) resolve(lang);
          else reject(Object.assign(new Error(`${file} did not register ${lang}`), { code: 'not_registered' }));
        };
        s.onerror = () => { done(); reject(Object.assign(new Error(`failed to load ${file}`), { code: 'load_failed' })); };
        (doc.head || doc.documentElement).appendChild(s);
      });
      pending.set(lang, p);
      p.catch(() => {}).finally(() => pending.delete(lang));
      return p;
    },
  };
}
