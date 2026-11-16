// Release notes in the page's language (Update.notes, Update.installed_notes; 11-16).
//
// A release's in-app notes (docs/release-notes/<version>.txt) may hold one section per
// language: a line that is only a language code in brackets ("[zh_CN]", "[en_US]") starts
// that language's section, which runs to the next such line. Text before the first marker
// belongs to every language. releaseNotesFor(text, lang) returns the shared text plus the
// section of `lang`, else of en_US, else the first section with text; text without a marker
// (0.1.1 and older releases) is returned as it is. Untrusted plain text in, plain text out:
// nothing here parses Markdown or HTML (R-09). null when there is nothing to show.
//
// Copies installed before 0.1.2 show the whole text, markers included, so a marker stays a
// readable label in a plain-text sheet and in the release page's Markdown.

const MARKER = /^\[([a-z]{2}_[A-Z]{2})\]$/;
export const FALLBACK_LANG = 'en_US';

/** {shared, sections: Map<lang, text>} (texts trimmed; null sections without any marker) */
export function splitReleaseNotes(text) {
  const lines = String(text == null ? '' : text).replace(/\r\n?/g, '\n').split('\n');
  const shared = [];
  const sections = new Map();
  let current = null;
  for (const line of lines) {
    const m = MARKER.exec(line.trim());
    if (m) {
      current = m[1];
      if (!sections.has(current)) sections.set(current, []);
    } else (current === null ? shared : sections.get(current)).push(line);
  }
  const join = list => list.join('\n').trim();
  if (!sections.size) return { shared: join(shared), sections: null };
  const out = new Map();
  for (const [lang, list] of sections) { const s = join(list); if (s) out.set(lang, s); }
  return { shared: join(shared), sections: out };
}

export function releaseNotesFor(text, lang) {
  const { shared, sections } = splitReleaseNotes(text);
  if (!sections) return shared || null;
  const pick = sections.get(lang) || sections.get(FALLBACK_LANG) || sections.values().next().value || '';
  const body = [shared, pick].filter(Boolean).join('\n\n');
  return body || null;
}
