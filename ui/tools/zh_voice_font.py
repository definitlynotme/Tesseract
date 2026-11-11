"""Cut "Tesseract Kai", the zh voice font, from LXGW WenKai GB Screen (F-02).

The zh voice (css/tokens.css, :root:lang(zh) --font-voice) is a handwritten kai: 霞鹜文楷 GB
屏幕阅读版, LXGW WenKai GB Screen 1.530 (SIL OFL 1.1, https://github.com/lxgw/LxgwWenKai-Screen).
The whole font is 26 MB; the interface needs only the characters of its zh_CN catalogs, so
this tool subsets it to them. It writes two source files of the build (build/fonts.mjs only
reads them; nothing copies or regenerates them) and keeps one CSS declaration in step:

    ui/fonts/tesseract-kai-zh.woff2   the subset, WOFF2
    ui/fonts/OFL-tesseract-kai.txt    the upstream OFL.txt, byte for byte
    ui/css/fonts.css                  the unicode-range of the Tesseract Kai @font-face (= the cmap)

Usage (the system Python with fontTools and brotli; the repo .venv has neither):

    python -I ui/tools/zh_voice_font.py [--source <ttf>] [--license <OFL.txt>] [--check]

Re-run it whenever the zh_CN strings gain new characters: test/unit/fonts.test.mjs fails
while a CJK character of a zh_CN message is not mapped by the woff2 (it would silently fall
back to the system kai). --check writes nothing and exits 1 when a file is not what this
tool would write.

Source: LXGWWenKaiGBScreen.ttf and OFL.txt of the GitHub release v1.530 of
lxgw/LxgwWenKai-Screen, by default from .build/fonts-download/lxgw-wenkai-screen-v1.530/
(--license defaults to the OFL.txt next to --source). Both are only read, as data; their
sha256 are pinned in SOURCE and another file is refused. A new upstream version means
updating SOURCE here, the font entry in build/fonts.mjs and THIRD_PARTY_NOTICES.md together.

Characters: every character of the `m` text of the messages in
src/i18n/locales/zh_CN/*.json that lies in VOICE_RANGES (CJK Symbols and Punctuation, CJK
Unified Ideographs and Extension A, Halfwidth and Fullwidth Forms) or is one of VOICE_EXTRA
(the general punctuation Chinese text uses). Latin, digits and the rest stay in the UI face
(Instrument Sans). test/unit/fonts.test.mjs mirrors both tuples and checks they match.

Names: the OFL lets a Modified Version keep the original copyright and licence but not a
Reserved Font Name ('霞鹜', '霞鶩', '落霞孤鹜', '落霞孤鶩', 'LXGW'). The subset is renamed
"Tesseract Kai": the records that identify a font (IDENTIFYING_IDS) are dropped on every
platform and in every language and written again in English (Windows, 0x0409). Copyright
(0), version (5), manufacturer and designer (8, 9, 11, 12) and licence (13, 14) are kept as
they are; the sample text (19) is dropped (the subset cannot draw it); a description (10)
records where the subset comes from.

Layout: horizontal features only. The vertical ones (VERTICAL_FEATURES) and vhea/vmtx are
dropped: the interface never sets vertical text. The source carries no TrueType hinting
(no fpgm/prep/cvt/gasp, no glyph instructions), so there is none to drop.

Deterministic: head.modified stays the source's (no timestamp recalculation), so the same
source, catalogs, fontTools and brotli give the same bytes. A file is only rewritten when its
bytes change. Exit status: 0 done; 1 --check found a stale file; 2 bad arguments, source or
environment; 3 written, but the source does not map some character, or css/fonts.css has no
Tesseract Kai rule to update (add the one `node ui/tools/font_cmap.mjs --emit-css` prints).
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sys
from pathlib import Path

UI = Path(__file__).resolve().parent.parent
REPO = UI.parent
LOCALES = UI / "src" / "i18n" / "locales" / "zh_CN"
WOFF2 = UI / "fonts" / "tesseract-kai-zh.woff2"
LICENSE = UI / "fonts" / "OFL-tesseract-kai.txt"
CSS = UI / "css" / "fonts.css"

SOURCE = {
    "name": "LXGW WenKai GB Screen",
    "version": "1.530",
    "url": "https://github.com/lxgw/LxgwWenKai-Screen",
    "ttf_sha256": "0df4009ac9dce6eb3b8cb4c4e90de67bb9745e8992a255c03a58dd5fe3f1e503",
    "ofl_sha256": "ba38832f9d7e0ad601a1ab90d58f251ba957532885ef80dca050a611962fd8b6",
}
DEFAULT_SOURCE = REPO / ".build" / "fonts-download" / "lxgw-wenkai-screen-v1.530" / "LXGWWenKaiGBScreen.ttf"

# Character classes of the subset (test/unit/fonts.test.mjs mirrors both and checks them).
VOICE_RANGES = (
    (0x3000, 0x303F),  # CJK Symbols and Punctuation
    (0x3400, 0x4DBF),  # CJK Unified Ideographs Extension A
    (0x4E00, 0x9FFF),  # CJK Unified Ideographs
    (0xFF00, 0xFFEF),  # Halfwidth and Fullwidth Forms
)
VOICE_EXTRA = (0x00B7, 0x2014, 0x2018, 0x2019, 0x201C, 0x201D, 0x2026)  # middle dot, em dash, quotes, ellipsis

FAMILY = "Tesseract Kai"
STYLE = "Regular"
FULL_NAME = "Tesseract Kai Regular"
POSTSCRIPT_NAME = "TesseractKai-Regular"
RESERVED_FONT_NAMES = ("霞鹜", "霞鶩", "落霞孤鹜", "落霞孤鶩", "LXGW")
# family, subfamily, unique id, full name, PostScript name, typographic family / subfamily,
# compatible full name, CID findfont name, WWS family / subfamily, variations PS prefix
IDENTIFYING_IDS = frozenset({1, 2, 3, 4, 6, 16, 17, 18, 20, 21, 22, 25})
DESCRIPTION_ID = 10
SAMPLE_TEXT_ID = 19
VERTICAL_FEATURES = frozenset({"valt", "vchw", "vert", "vhal", "vkrn", "vpal", "vrt2"})
VERTICAL_TABLES = ("vhea", "vmtx", "VORG")

KAI_FACE = re.compile(r'@font-face\s*\{[^}]*url\(\s*"\.\./fonts/' + re.escape(WOFF2.name) + r'"\s*\)[^}]*\}')


class Refused(Exception):
    pass


def in_voice(cp: int) -> bool:
    return cp in VOICE_EXTRA or any(lo <= cp <= hi for lo, hi in VOICE_RANGES)


def catalog_codepoints(locales: Path = LOCALES) -> tuple[list[int], int]:
    """Sorted code points of the zh_CN messages in the voice classes, and the catalog count.

    Same files as build/locales.mjs readCatalogs (*.json not starting with '_'); an entry
    without a string `m` has no text there either.
    """
    files = sorted(p for p in locales.glob("*.json") if not p.name.startswith("_"))
    if not files:
        raise Refused(f"no zh_CN catalogs in {locales}")
    found: set[int] = set()
    for path in files:
        for entry in json.loads(path.read_bytes().decode("utf-8-sig")).values():
            text = entry.get("m") if isinstance(entry, dict) else None
            if isinstance(text, str):
                found.update(ord(ch) for ch in text if in_voice(ord(ch)))
    return sorted(found), len(files)


def read_pinned(path: Path, sha256: str, what: str) -> bytes:
    if not path.is_file():
        raise Refused(f"{what} not found: {path}")
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest != sha256:
        raise Refused(f"{path} has sha256 {digest}, not the pinned {what} of {SOURCE['name']} "
                      f"{SOURCE['version']} ({sha256}); a new version needs SOURCE updated")
    return data


def unicode_range(codepoints) -> str:
    """The CSS unicode-range of a set of code points (= font_cmap.mjs formatUnicodeRange)."""
    cps = sorted(set(codepoints))
    parts, i = [], 0
    while i < len(cps):
        j = i
        while j + 1 < len(cps) and cps[j + 1] == cps[j] + 1:
            j += 1
        parts.append(f"U+{cps[i]:04X}" if i == j else f"U+{cps[i]:04X}-{cps[j]:04X}")
        i = j + 1
    return ", ".join(parts)


def name_records(codepoints: list[int]) -> dict[int, str]:
    digest = hashlib.sha256(",".join(f"{c:04X}" for c in codepoints).encode("ascii")).hexdigest()[:8].upper()
    return {
        1: FAMILY,
        2: STYLE,
        3: f"{FULL_NAME}; {SOURCE['version']}; zh-ui subset {digest}",
        4: FULL_NAME,
        6: POSTSCRIPT_NAME,
        DESCRIPTION_ID: (f"{FAMILY} is {SOURCE['name']} {SOURCE['version']} ({SOURCE['url']}) cut to the "
                         "characters of the Tesseract zh_CN interface and renamed: a Modified Version "
                         "under the SIL Open Font License 1.1."),
    }


def rename(name_table, records: dict[int, str]) -> None:
    drop = IDENTIFYING_IDS | {DESCRIPTION_ID, SAMPLE_TEXT_ID}
    name_table.names = [r for r in name_table.names if r.nameID not in drop]
    for name_id, text in records.items():
        name_table.setName(text, name_id, 3, 1, 0x409)


def reserved_in_identifying(font) -> list[str]:
    bad = []
    for rec in font["name"].names:
        if rec.nameID in IDENTIFYING_IDS:
            text = rec.toUnicode()
            if any(r.casefold() in text.casefold() for r in RESERVED_FONT_NAMES):
                bad.append(f"name {rec.nameID} ({rec.platformID}/{rec.platEncID}/0x{rec.langID:04X}): {text}")
    return bad


def build_font(source: bytes, wanted: list[int]) -> dict:
    from fontTools import subset
    from fontTools.ttLib import TTFont

    font = TTFont(io.BytesIO(source), recalcTimestamp=False)
    mapped = font.getBestCmap()
    present = [c for c in wanted if c in mapped]
    missing = [c for c in wanted if c not in mapped]
    options = subset.Options()
    options.layout_features = [f for f in options.layout_features if f not in VERTICAL_FEATURES]
    options.drop_tables = [*options.drop_tables, *VERTICAL_TABLES]
    options.name_IDs = ["*"]
    options.name_languages = ["*"]
    options.flavor = "woff2"
    subsetter = subset.Subsetter(options=options)
    subsetter.populate(unicodes=present)
    subsetter.subset(font)
    records = name_records(present)
    rename(font["name"], records)
    font.flavor = "woff2"
    out = io.BytesIO()
    font.save(out)
    data = out.getvalue()

    # read the result back: exactly the requested cmap, and no Reserved Font Name left
    check = TTFont(io.BytesIO(data))
    if check.flavor != "woff2":
        raise RuntimeError("the output is not WOFF2")
    if sorted(check.getBestCmap()) != present:
        raise RuntimeError("the subset's cmap is not the requested character set")
    bad = reserved_in_identifying(check)
    if bad:
        raise RuntimeError("a Reserved Font Name is left in an identifying name record: " + "; ".join(bad))
    features = {}
    for tag in ("GSUB", "GPOS"):
        if tag in check and check[tag].table.FeatureList:
            features[tag] = sorted({fr.FeatureTag for fr in check[tag].table.FeatureList.FeatureRecord})
    return {
        "data": data, "present": present, "missing": missing, "records": records,
        "glyphs": check["maxp"].numGlyphs, "tables": sorted(t for t in check.keys() if t != "GlyphOrder"),
        "features": features,
    }


def patch_css(css: str, codepoints: list[int]) -> str | None:
    """css with the Tesseract Kai rule's unicode-range set to `codepoints` (None: no such rule)."""
    m = KAI_FACE.search(css)
    if not m:
        return None
    rule, n = re.subn(r"(unicode-range:\s*)[^;]*;", lambda d: f"{d.group(1)}{unicode_range(codepoints)};", m.group(0))
    if n != 1:
        return None
    return css[:m.start()] + rule + css[m.end():]


def write_if_changed(path: Path, data: bytes) -> bool:
    if path.is_file() and path.read_bytes() == data:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()
    return True


def rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO).as_posix()
    except ValueError:
        return str(path)


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--source", type=Path, default=DEFAULT_SOURCE, help="LXGWWenKaiGBScreen.ttf of the pinned release")
    ap.add_argument("--license", type=Path, default=None, help="its OFL.txt (default: next to --source)")
    ap.add_argument("--check", action="store_true", help="write nothing; exit 1 when a file is not what this tool writes")
    args = ap.parse_args(argv)
    try:
        import brotli  # noqa: F401  (fontTools needs it for WOFF2)
        import fontTools  # noqa: F401
    except ImportError as e:
        print(f"zh_voice_font: {e}; use the system Python that has fontTools and brotli", file=sys.stderr)
        return 2
    try:
        ttf = read_pinned(args.source, SOURCE["ttf_sha256"], "font")
        ofl = read_pinned(args.license or args.source.with_name("OFL.txt"), SOURCE["ofl_sha256"], "OFL.txt")
        wanted, catalogs = catalog_codepoints()
    except Refused as e:
        print(f"zh_voice_font: {e}", file=sys.stderr)
        return 2

    r = build_font(ttf, wanted)
    present, missing, data = r["present"], r["missing"], r["data"]
    css_old = CSS.read_bytes().decode("utf-8") if CSS.is_file() else ""
    css_new = patch_css(css_old, present)
    ideographs = sum(1 for c in present if 0x3400 <= c <= 0x4DBF or 0x4E00 <= c <= 0x9FFF)

    print(f"source: {SOURCE['name']} {SOURCE['version']} ({rel(args.source)}, sha256 {SOURCE['ttf_sha256'][:16]}…)")
    print(f"characters: {len(wanted)} in {catalogs} zh_CN catalogs; subset maps {len(present)} "
          f"({ideographs} ideographs, {len(present) - ideographs} punctuation and symbols)")
    print("missing from the source: " + (" ".join(f"U+{c:04X} {chr(c)}" for c in missing) if missing else "none"))
    features = "; ".join(tag + " " + " ".join(tags) for tag, tags in r["features"].items()) or "none"
    print(f"glyphs: {r['glyphs']}; tables: {' '.join(r['tables'])}; features: {features}")
    for name_id, text in r["records"].items():
        print(f"name {name_id}: {text}")

    outputs = [(WOFF2, data), (LICENSE, ofl)]
    if css_new is not None:
        outputs.append((CSS, css_new.encode("utf-8")))
    stale = []
    for path, blob in outputs:
        same = path.is_file() and path.read_bytes() == blob
        if args.check:
            state = "up to date" if same else "STALE"
            if not same:
                stale.append(path)
        else:
            state = "written" if write_if_changed(path, blob) else "unchanged"
        size = f"{len(blob)} bytes, sha256 {hashlib.sha256(blob).hexdigest()}" if path != CSS else "Tesseract Kai unicode-range"
        print(f"{rel(path)}: {size} ({state})")
    if css_new is None:
        print(f"{rel(CSS)}: no @font-face for {WOFF2.name} with one unicode-range; add the rule that "
              "`node ui/tools/font_cmap.mjs --emit-css` prints", file=sys.stderr)
    if args.check:
        return 1 if stale or css_new is None else 0
    return 3 if missing or css_new is None else 0


if __name__ == "__main__":
    sys.exit(main())
