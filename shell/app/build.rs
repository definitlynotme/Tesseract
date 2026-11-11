use std::{env, fs, path::{Path, PathBuf}};
use serde_json::{json, Value};
#[path="src/assets.rs"] mod assets;
#[allow(dead_code)] #[path="src/hgate.rs"] mod hgate;
/// T-P6-01: the JSON merge patch (RFC 7386) that tauri-build applies to `TAURI_CONFIG`.
/// T-P6-06: a patch may not delete keys (no `null`, see `has_null`): the merged result is handed
/// back to tauri-build as `TAURI_CONFIG`, which merges it onto tauri.conf.json again, so a key
/// deleted here would come back.
fn merge(target: &mut Value, patch: &Value) {
    let Value::Object(entries) = patch else { *target = patch.clone(); return; };
    if !target.is_object() { *target = json!({}); }
    let object = target.as_object_mut().unwrap();
    for (key, value) in entries {
        if value.is_null() { object.remove(key); } else { merge(object.entry(key.clone()).or_insert(Value::Null), value); }
    }
}
/// T-P6-06: whether a JSON value holds a `null` anywhere (a merge-patch deletion).
fn has_null(value: &Value) -> bool {
    match value {
        Value::Null => true,
        Value::Array(items) => items.iter().any(has_null),
        Value::Object(entries) => entries.values().any(has_null),
        _ => false,
    }
}
/// T-P6-01 (plan §9.3): `app_version.txt` (the release version contract of app_info.py,
/// `x.y.z` or `x.y.z-rc.N`) → the version string and the four Windows fields
/// (x.y.z.65535 for a release, x.y.z.N for rc N).
fn release_version(text: &str) -> Result<(String, [u16; 4]), String> {
    let display = text.trim();
    let number = |part: &str, max: u32| -> Result<u16, String> {
        let canonical = !part.is_empty() && part.bytes().all(|b| b.is_ascii_digit()) && (part == "0" || !part.starts_with('0'));
        match part.parse::<u32>() { Ok(n) if canonical && n <= max => Ok(n as u16), _ => Err(format!("app_version.txt: not a canonical x.y.z or x.y.z-rc.n: {display:?}")) }
    };
    let (core, rc) = match display.split_once("-rc.") { Some((core, rc)) => (core, Some(rc)), None => (display, None) };
    let parts = core.split('.').map(|part| number(part, 65535)).collect::<Result<Vec<_>, _>>()?;
    if parts.len() != 3 { return Err(format!("app_version.txt: not a canonical x.y.z or x.y.z-rc.n: {display:?}")); }
    let build = match rc { Some(rc) => number(rc, 65534)?, None => 65535 };
    Ok((display.to_owned(), [parts[0], parts[1], parts[2], build]))
}
/// T-P6-01 (plan §9.3, one VERSIONINFO only): tauri-build writes the single VERSIONINFO, with the
/// strings from `config.version` (x.y.z[-rc.N]) but the fixed fields x.y.z.0 (tauri-winres takes
/// the fourth field from SemVer build metadata only). This overrides that one field in the
/// compiled resource (`OUT_DIR/resource.lib`, embed-resource's .res, linked after this script)
/// for FILEVERSION and PRODUCTVERSION. The key `VS_VERSION_INFO` must occur once, with the
/// VS_FIXEDFILEINFO signature at its fixed offset and x.y.z already in place.
fn override_windows_build(res: &Path, version: [u16; 4]) -> Result<(), String> {
    let mut bytes = fs::read(res).map_err(|e| format!("{}: {e}", res.display()))?;
    let key: Vec<u8> = "VS_VERSION_INFO\0".encode_utf16().flat_map(u16::to_le_bytes).collect();
    let keys: Vec<usize> = bytes.windows(key.len()).enumerate().filter(|(_, w)| *w == key.as_slice()).map(|(i, _)| i).collect();
    // wLength, wValueLength, wType (6 bytes), the 32-byte key, 2 bytes of padding → VS_FIXEDFILEINFO.
    let &[key_at] = keys.as_slice() else { return Err(format!("{}: {} VS_VERSION_INFO keys, expected 1", res.display(), keys.len())); };
    let fixed = key_at + key.len() + 2;
    let word = |bytes: &[u8], at: usize| bytes.get(at..at + 4).map(|b| u32::from_le_bytes(b.try_into().unwrap()));
    if word(&bytes, fixed) != Some(0xFEEF_04BD) { return Err(format!("{}: no VS_FIXEDFILEINFO after VS_VERSION_INFO", res.display())); }
    let ms = (u32::from(version[0]) << 16) | u32::from(version[1]);
    let ls = (u32::from(version[2]) << 16) | u32::from(version[3]);
    // dwFileVersionMS/LS at +8/+12, dwProductVersionMS/LS at +16/+20.
    for at in [fixed + 8, fixed + 16] {
        if word(&bytes, at) != Some(ms) || word(&bytes, at + 4).map(|v| v >> 16) != Some(u32::from(version[2])) {
            return Err(format!("{}: the fixed version is not {}.{}.{}.*", res.display(), version[0], version[1], version[2]));
        }
        bytes[at + 4..at + 8].copy_from_slice(&ls.to_le_bytes());
    }
    fs::write(res, bytes).map_err(|e| format!("{}: {e}", res.display()))
}
/// `path` relative to `base` (both absolute), or None when they share no root (another drive).
fn relative_path(path: &Path, base: &Path) -> Option<PathBuf> {
    use std::path::Component;
    let path: Vec<Component> = path.components().collect();
    let base: Vec<Component> = base.components().collect();
    let common = path.iter().zip(&base).take_while(|(a, b)| a == b).count();
    let rooted = |c: &Component| matches!(c, Component::Prefix(_) | Component::RootDir);
    if common == 0 || !base[..common].iter().any(rooted) || base[common..].iter().any(rooted) {
        return None;
    }
    let mut relative = PathBuf::new();
    for _ in common..base.len() { relative.push(".."); }
    for part in &path[common..] { relative.push(part.as_os_str()); }
    Some(relative)
}

fn main() {
    println!("cargo:rerun-if-changed=dev");
    println!("cargo:rerun-if-env-changed=TAURI_CONFIG");
    println!("cargo:rerun-if-changed=../../app_version.txt");
    let debug = env::var_os("CARGO_CFG_DEBUG_ASSERTIONS").is_some();
    println!("cargo:rustc-env=P3_ASSET_DEBUG={debug}");
    let mut config: Value = serde_json::from_slice(&fs::read("tauri.conf.json").unwrap()).unwrap();
    // T-P6-01: an outside TAURI_CONFIG is merged exactly as tauri-build merges it (it would be
    // shadowed by the one set below otherwise). Its `build.frontendDist` may name a private,
    // already built app target instead of shell/app/ui (scripts/build_shell_preview.ps1), so a
    // preview build never writes the directory the debug end-to-end runs fill.
    if let Ok(patch) = env::var("TAURI_CONFIG") {
        let patch: Value = serde_json::from_str(&patch).unwrap_or_else(|e| panic!("TAURI_CONFIG is not JSON: {e}"));
        if has_null(&patch) { panic!("TAURI_CONFIG may not delete keys (null): tauri-build would merge them back from tauri.conf.json"); }
        merge(&mut config, &patch);
    }
    let ui: PathBuf = match config["build"]["frontendDist"].as_str() {
        Some(dir) if !dir.contains("://") => PathBuf::from(dir),
        _ => panic!("build.frontendDist must be one directory (the built app target), not {}", config["build"]["frontendDist"]),
    };
    println!("cargo:rerun-if-changed={}", ui.display());
    let manifest_path = ui.join("build-manifest.json");
    // T-P6-01: the release version contract (app_version.txt) is the one version source; the
    // Cargo package version only names the crate.
    let (version, windows_version) = release_version(&fs::read_to_string("../../app_version.txt").unwrap_or_else(|e| panic!("app_version.txt: {e}")))
        .unwrap_or_else(|e| panic!("{e}"));
    config["version"] = json!(version);
    // T-P4-01: a release build embeds only the built app target (P4 architecture section 1.3).
    let manifest = fs::read(&manifest_path).ok()
        .and_then(|b| serde_json::from_slice::<serde_json::Value>(&b).ok());
    if !debug {
        if manifest.as_ref().and_then(|m| m["target"].as_str()) != Some("app") {
            panic!("{} is missing or its target is not 'app': run scripts/shell_copy_ui.ps1 before a release build", manifest_path.display());
        }
    }
    // T-P4-C3 item 4: the build's H feature key (ENABLED_H_FEATURES) from the one canonical
    // manifest path; the shell allows an H command only with it and hello.caps.features.
    let h_features = hgate::manifest_features(manifest.as_ref())
        .unwrap_or_else(|e| panic!("{}: {e}", manifest_path.display()));
    println!("cargo:rustc-env=TESSERACT_H_FEATURES={}", h_features.join(","));
    let out = std::path::PathBuf::from(env::var_os("OUT_DIR").unwrap());
    let assets = out.join("ui");
    // OUT_DIR is Cargo owned, never the prototype or source ui directory.
    if assets.exists() { fs::remove_dir_all(&assets).unwrap(); }
    assets::copy(&ui, &assets, debug);
    if debug { assets::copy(Path::new("dev"), &assets, true); }
    // A Windows absolute path string is parsed as a URL with scheme "c".
    // A Files array unambiguously embeds this directory on every drive.
    // The config (frontendDist included) is embedded in Tesseract.exe: name the directory
    // relative to this crate when it can be (same drive), so the build machine's absolute
    // path, with its user name, is not published.
    let manifest_dir = PathBuf::from(env::var_os("CARGO_MANIFEST_DIR").unwrap());
    let embedded = relative_path(&assets, &manifest_dir).unwrap_or_else(|| assets.clone());
    config["build"]["frontendDist"] = serde_json::json!([embedded]);
    if !debug {
        let csp = assets::release_csp(config["app"]["security"]["csp"].as_str().unwrap());
        config["app"]["security"]["csp"] = serde_json::json!(csp);
    }
    // Use original config parent for icons/capabilities, override assets only.
    env::set_var("TAURI_CONFIG", serde_json::to_string(&config).unwrap());
    tauri_build::try_build(tauri_build::Attributes::new()
        .codegen(tauri_build::CodegenContext::new())).unwrap();
    if env::var("CARGO_CFG_TARGET_OS").as_deref() == Ok("windows") && env::var("CARGO_CFG_TARGET_ENV").as_deref() == Ok("msvc") {
        override_windows_build(&out.join("resource.lib"), windows_version).unwrap_or_else(|e| panic!("version resource: {e}"));
    }
}
