# i18n gate fixtures (T-P4-03)

- `base/`: a complete mini ui root (9 languages, frozen.json, glossary.json, tools/i18n_gates.json with every
  check hard) plus `external/` inputs for the checks that normally run Python or read the shell
  (backend_keys.json, py_catalog.json, native_text.rs, truncation.json). Every check passes.
- `fail_<check>[_variant]/`: an overlay on base (`case.json` = {extends, expect, patch_meta?}); `expect` is
  the check that must fail. `pass_*` overlays must pass.
- `scan_css/`: cjk scan inputs for the CSS `@i18n-pending` exemption (first line only).

Regenerate only together with ui/test/unit/i18n_gate.test.mjs.
