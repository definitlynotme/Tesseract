//! T-P4-C3 item 4 (P4 architecture section 5, PROTOCOL section 3): the shell half of the two-key
//! release of H (prototype-only) features. Key 1 is `hello.caps.features` (the sidecar implements
//! it); key 2 is the front-end build's user-approved list `ENABLED_H_FEATURES`, which the UI build
//! writes into `shell/app/ui/build-manifest.json` as `h_features` (the one canonical path,
//! criticism B13) and build.rs compiles into the shell (`TESSERACT_H_FEATURES`). An H command
//! passes the page whitelist only when both keys hold its feature, named by its checklist item
//! (`B-31`, `B-30a`).
//!
//! v1 has no H command yet ([`H_COMMANDS`] is empty): this file only builds the mechanism, which
//! must be in place before the first H feature is released. Releasing one means adding its
//! `(command, feature)` row here and the command to the sidecar's caps, in that feature's ticket.
//!
//! Shared with build.rs (`#[path]`), so it uses nothing from the crate.
use serde_json::Value;

/// `(page command, feature)` of every H command. Empty in v1.
pub const H_COMMANDS: &[(&str, &str)] = &[];

#[cfg_attr(not(test), allow(dead_code))]
/// Whether `name` is a checklist item id: `B-` + digits + an optional lower-case sub-item letter.
pub fn feature_name(name: &str) -> bool {
    let Some(rest) = name.strip_prefix("B-") else { return false };
    let digits = rest.trim_end_matches(|c: char| c.is_ascii_lowercase());
    let suffix = rest.len() - digits.len();
    !digits.is_empty() && digits.len() <= 3 && digits.bytes().all(|b| b.is_ascii_digit()) && suffix <= 1
}

#[cfg_attr(not(test), allow(dead_code))]
/// `h_features` of a UI build manifest. A missing manifest (debug builds of the prototype) or a
/// missing key means no feature; anything malformed fails the build instead of passing silently.
pub fn manifest_features(manifest: Option<&Value>) -> Result<Vec<String>, String> {
    let Some(manifest) = manifest else { return Ok(Vec::new()) };
    let list = match manifest.get("h_features") {
        None | Some(Value::Null) => return Ok(Vec::new()),
        Some(Value::Array(list)) => list,
        Some(_) => return Err("h_features must be an array of checklist ids".into()),
    };
    let mut out: Vec<String> = Vec::new();
    for item in list {
        let name = item.as_str().ok_or("h_features entries must be strings")?;
        if !feature_name(name) { return Err(format!("h_features entry is not a checklist id (B-xx): {name:?}")); }
        if !out.iter().any(|n| n == name) { out.push(name.to_owned()); }
    }
    Ok(out)
}

/// The compiled-in second key (build.rs writes `TESSERACT_H_FEATURES`, comma separated).
#[allow(dead_code)]
pub fn build_features() -> Vec<&'static str> {
    option_env!("TESSERACT_H_FEATURES").unwrap_or("").split(',').filter(|s| !s.is_empty()).collect()
}

/// `None`: `cmd` is not an H command (the ordinary whitelist decides). `Some(allowed)`: an H
/// command, allowed only when its feature is in `hello.caps.features` and in `build` (and the
/// command itself in `hello.caps.commands`, as for every page command).
#[allow(dead_code)]
pub fn allowed(cmd: &str, table: &[(&str, &str)], build: &[&str], hello: &Value) -> Option<bool> {
    let (_, feature) = table.iter().find(|(c, _)| *c == cmd)?;
    let has = |list: &Value, name: &str| list.as_array().is_some_and(|v| v.iter().any(|x| x == name));
    Some(has(&hello["caps"]["features"], feature) && build.contains(feature) && has(&hello["caps"]["commands"], cmd))
}

#[cfg(test)] mod tests {
    use super::*;
    use serde_json::json;
    const TABLE: &[(&str, &str)] = &[("nodes.reorder", "B-31"), ("tiers.pin", "B-30a")];
    fn hello(features: &[&str], commands: &[&str]) -> Value { json!({"caps":{"features":features,"commands":commands}}) }
    #[test] fn both_keys_are_required() {
        let all = ["nodes.reorder", "tiers.pin"];
        // (sidecar caps.features, build h_features) -> allowed
        for (caps, build, expected) in [
            (&["B-31"][..], &["B-31"][..], true),
            (&["B-31"][..], &[][..], false),
            (&[][..], &["B-31"][..], false),
            (&[][..], &[][..], false),
            (&["B-30a"][..], &["B-31"][..], false),
            (&["B-31", "B-30a"][..], &["B-31", "B-30a"][..], true),
        ] {
            assert_eq!(allowed("nodes.reorder", TABLE, build, &hello(caps, &all)), Some(expected), "{caps:?} {build:?}");
        }
        assert_eq!(allowed("nodes.reorder", TABLE, &["B-31"], &hello(&["B-31"], &[])), Some(false), "the command must be in caps.commands too");
        assert_eq!(allowed("tiers.pin", TABLE, &["B-30a"], &hello(&["B-30a"], &all)), Some(true), "sub-item names");
        assert_eq!(allowed("settings.apply", TABLE, &["B-31"], &hello(&["B-31"], &all)), None, "not an H command");
        assert_eq!(allowed("nodes.reorder", TABLE, &["B-31"], &json!({})), Some(false), "a hello without caps");
    }
    #[test] fn v1_has_no_h_command_and_the_build_key_parses() {
        assert!(H_COMMANDS.is_empty(), "v1 whitelist has no H command (T-P4-C3)");
        for cmd in ["settings.apply", "service.start", "nodes.update"] { assert_eq!(allowed(cmd, H_COMMANDS, &["B-31"], &hello(&["B-31"], &[cmd])), None); }
        // The build key of this very build comes from shell/app/ui/build-manifest.json.
        for name in build_features() { assert!(feature_name(name), "{name}"); }
    }
    #[test] fn manifest_h_features_are_strict_checklist_ids() {
        assert_eq!(manifest_features(None).unwrap(), Vec::<String>::new());
        assert_eq!(manifest_features(Some(&json!({"target":"app"}))).unwrap(), Vec::<String>::new());
        assert_eq!(manifest_features(Some(&json!({"h_features":[]}))).unwrap(), Vec::<String>::new());
        assert_eq!(manifest_features(Some(&json!({"h_features":["B-31","B-30a","B-31"]}))).unwrap(), ["B-31", "B-30a"]);
        for bad in [json!({"h_features":"B-31"}), json!({"h_features":[31]}), json!({"h_features":["b-31"]}),
            json!({"h_features":["B-"]}), json!({"h_features":["B-31,B-32"]}), json!({"h_features":["B-3ab"]}), json!({"h_features":["H-1"]})] {
            assert!(manifest_features(Some(&bad)).is_err(), "{bad}");
        }
        // The canonical manifest of this checkout parses.
        let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("ui/build-manifest.json");
        if let Ok(bytes) = std::fs::read(path) {
            let manifest: Value = serde_json::from_slice(&bytes).unwrap();
            let names = manifest_features(Some(&manifest)).unwrap();
            assert_eq!(names.iter().map(String::as_str).collect::<Vec<_>>(), build_features(), "compiled key = manifest h_features");
        }
    }
}
