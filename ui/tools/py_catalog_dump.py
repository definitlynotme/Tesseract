"""Dump the Python (Qt-era) translation catalog for the three-catalog check (T-P4-03).

P4 architecture §3.9: i18n_check compares the key sets and ``str.format`` placeholders of
the nine languages in ``ui_i18n`` (``TRANSLATIONS_EN`` + ``locales/*.json`` overlays; the
source language's keys are the zh_CN texts themselves).

``ui_i18n`` is imported directly: it has no PySide6/Qt import (checked statically below and
again at run time — the dump refuses to run if Qt was loaded). Babel is optional there.

    python -B ui/tools/py_catalog_dump.py [--repo <dir>]

prints {"source", "languages": {lang: {key: [placeholder names]}}, "qt_free", "static_qt_imports"}.
Nothing is written.
"""
from __future__ import annotations

import argparse
import ast
import json
import string
import sys
from pathlib import Path

QT_MODULES = ("PySide6", "PySide2", "PyQt5", "PyQt6", "shiboken6")


def placeholders(text: str) -> list[str]:
    """Field names of a str.format template ('' for positional {}), sorted, with repeats."""
    names = []
    try:
        for _literal, field, _spec, _conv in string.Formatter().parse(text):
            if field is not None:
                names.append(field.split(".", 1)[0].split("[", 1)[0])
    except ValueError:
        names.append("<invalid format string>")
    return sorted(names)


def static_qt_imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [a.name for a in node.names if a.name.split(".")[0] in QT_MODULES]
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] in QT_MODULES:
            found.append(node.module)
    return found


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--repo", default=str(Path(__file__).resolve().parents[2]))
    args = ap.parse_args(argv)
    repo = Path(args.repo).resolve()
    static = static_qt_imports(repo / "ui_i18n.py")
    sys.path.insert(0, str(repo))
    import ui_i18n  # noqa: E402

    loaded_qt = sorted(name for name in sys.modules if name.split(".")[0] in QT_MODULES)
    if static or loaded_qt:
        sys.stderr.write(f"ui_i18n depends on Qt: static={static} loaded={loaded_qt}\n")
        return 2
    source = ui_i18n.SOURCE_LANGUAGE
    catalogs = ui_i18n.TRANSLATIONS_BY_LANGUAGE
    languages = {source: {key: placeholders(key) for key in ui_i18n.TRANSLATIONS_EN}}
    for lang in ui_i18n.SUPPORTED_LANGUAGES:
        if lang == source:
            continue
        languages[lang] = {key: placeholders(value) for key, value in catalogs.get(lang, {}).items()}
    # Overlay keys that the runtime drops (not in TRANSLATIONS_EN) are orphans worth reporting.
    orphans = {lang: sorted(set(over) - set(ui_i18n.TRANSLATIONS_EN))
               for lang, over in ui_i18n.TRANSLATION_OVERLAYS.items()}
    json.dump({"source": source, "languages": languages, "overlay_orphans": orphans,
               "qt_free": True, "static_qt_imports": static}, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
