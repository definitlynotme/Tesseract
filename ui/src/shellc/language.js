// The page language (T-P4-06; P4 architecture sections 2.9, 3.5; criticism B14, B2).
//
// Two sources, both applied with T.i18n.set: store `lang` (written by the rt client from
// hello.lang, and from host:language) and the host:language{language} event itself. A language
// equal to the one applied or the one being loaded is not requested again, so the same value
// from hello and from host:language loads once. A failed switch forgets the request, so a later
// equal value retries; a finished switch forgets it too (only the one being loaded and the one
// applied, T.i18n.lang, are deduplicated: a language changed by another path since can be asked
// for again). The page never listens to the ui.language topic (the shell consumes it
// and forwards host:language; the page never receives it). Before hello the skeleton shows no
// words (index.html carries data-i18n hooks only; boot/i18n.js preloads the guess).
//
// Ports: store (T.store), on (T.on), i18n ({lang, set(lang) -> Promise}).

export function createLanguage({ store, on, i18n }) {
  let asked = null;

  function apply(lang) {
    if (typeof lang !== 'string' || !lang || !i18n || typeof i18n.set !== 'function') return false;
    if (lang === asked) return false;
    if (lang === i18n.lang && asked === null) return false;
    asked = lang;
    Promise.resolve().then(() => i18n.set(lang)).then(() => { if (asked === lang) asked = null; }, e => {
      // superseded: a later request took over; anything else: forget, so the same value retries
      if (!(e && e.code === 'superseded') && asked === lang) asked = null;
    });
    return true;
  }

  store.sub('lang', v => apply(v));
  on('host:language', f => { if (f) apply(f.language); });
  const early = store.get('lang');
  if (typeof early === 'string') apply(early);

  return { apply, get asked() { return asked; } };
}
