//! Shared by build.rs and tests, so release exclusion tests exercise the build path.
use std::{fs, path::Path};
pub fn copy(source: &Path, dest: &Path, debug: bool) {
    fs::create_dir_all(dest).unwrap();
    for entry in fs::read_dir(source).unwrap() {
        let entry=entry.unwrap(); let name=entry.file_name();
        if !debug && name.to_string_lossy().starts_with("dev-rt.") { continue; }
        let target=dest.join(&name);
        if entry.file_type().unwrap().is_dir() { copy(&entry.path(),&target,debug); }
        else { fs::copy(entry.path(),target).unwrap(); }
    }
}
/// The release CSP. Nothing is stripped any more: the fonts are self-hosted (T-P4-08a, C3 item 8)
/// and `style-src` is `'self'` without `'unsafe-inline'` (T-P4-08b, P4 architecture §9), so the
/// development CSP in tauri.conf.json is the release one. T-P4-08b turns the hook into a guard:
/// a release build (build.rs) fails when the policy would allow inline content or name an
/// external origin (`check_csp`).
pub fn release_csp(csp: &str) -> String {
    if let Err(e) = check_csp(csp) { panic!("tauri.conf.json app.security.csp: {e}"); }
    csp.to_owned()
}
/// T-P4-08b: `default-src`, `script-src` and `style-src` are present; no `*-src*` directive
/// (`style-src-attr` / `-elem` and `script-src-attr` / `-elem` included, fix round 1) carries an
/// `'unsafe-…'` keyword, a nonce, a hash or a wildcard (Tauri adds its own hashes / nonces when it
/// serves the page; the configured policy must not need them); `data:` / `blob:` / `filesystem:`
/// appear only in `img-src` / `font-src`; and no directive names an external origin.
pub fn check_csp(csp: &str) -> Result<(), String> {
    let directives: Vec<Vec<&str>> = csp.split(';').map(|d| d.split_whitespace().collect::<Vec<_>>()).filter(|d| !d.is_empty()).collect();
    for name in ["default-src", "script-src", "style-src"] {
        if !directives.iter().any(|d| d[0].eq_ignore_ascii_case(name)) { return Err(format!("no {name}")); }
    }
    for d in &directives {
        let name = d[0].to_ascii_lowercase();
        if !name.contains("-src") { continue; }
        let scheme_ok = name == "img-src" || name == "font-src";
        let loose: Vec<&str> = d[1..].iter().copied().filter(|v| {
            let v = v.to_ascii_lowercase();
            let keyword = v.len() > 2 && v.starts_with('\'') && v.ends_with('\'');
            v.contains('*') || (keyword && v.contains("unsafe"))
                || v.starts_with("'nonce-") || v.starts_with("'sha256-") || v.starts_with("'sha384-") || v.starts_with("'sha512-")
                || (!scheme_ok && ["data:", "blob:", "filesystem:"].contains(&v.as_str()))
        }).collect();
        if !loose.is_empty() { return Err(format!("{name} allows inline content or a scheme: {}", loose.join(" "))); }
    }
    let external = external_sources(csp);
    if !external.is_empty() { return Err(format!("external origins: {}", external.join(" "))); }
    Ok(())
}
/// Every source of a CSP that is not the page's own origin, `data:`, `ipc:` or the IPC host.
pub fn external_sources(csp: &str) -> Vec<String> {
    csp.split(';').flat_map(|d| d.split_whitespace().skip(1)).filter(|s| !s.starts_with('\'') && *s != "data:" && *s != "ipc:" && *s != "http://ipc.localhost")
        .map(str::to_owned).collect()
}
#[cfg(test)] mod tests {
    use super::*;
    #[test] fn release_assets_exclude_every_debug_entry() {
        let dir=std::env::temp_dir().join(format!("p3-assets-{}-{}",std::process::id(),
            std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos()));
        let source=Path::new(env!("CARGO_MANIFEST_DIR")).join("ui");
        copy(&source,&dir,false);
        assert!(dir.join("index.html").is_file() && dir.join("build-manifest.json").is_file());
        let manifest:serde_json::Value=serde_json::from_slice(&fs::read(dir.join("build-manifest.json")).unwrap()).unwrap();
        assert_eq!(manifest["target"],"app");
        // T-P4-08b (P4 architecture §1.3): the embedded bundle certified itself demo-free at build
        // time (ui/build.mjs, the T-P4-07 hard gate), and no file of the simulator survives
        assert_eq!(manifest["demo_free"],true,"build-manifest.json demo_free");
        let mut names=Vec::new(); walk(&dir,&dir,&mut names);
        assert!(names.iter().any(|n| n=="index.html"),"{names:?}");
        // (fix round 1: the esbuild content hash, `[name].[hash]`, is left out of the check; it
        // could spell "sim" by chance and would then fail the same content every time)
        let sim:Vec<&String>=names.iter().filter(|n| named_sim(n)).collect();
        assert!(sim.is_empty(),"no file named after the simulator: {sim:?}");
        for entry in manifest["files"].as_array().unwrap() { assert!(!named_sim(entry["path"].as_str().unwrap()),"{entry}"); }
        for name in ["dev-rt.html","dev-rt.js","dev-rt.css"] { assert!(!dir.join(name).exists()); }
        fs::remove_dir_all(dir).unwrap();
    }
    /// The authored name of a bundle path (directories and file name, the extension included)
    /// without the esbuild `[hash]` segment (8 characters of A–Z / 0–9 after the first dot),
    /// contains "sim" in any case.
    fn named_sim(path:&str) -> bool {
        path.split('/').any(|part| part.split('.').enumerate()
            .filter(|(i,seg)| !(*i>0 && seg.len()==8 && seg.bytes().all(|c| c.is_ascii_uppercase() || c.is_ascii_digit())))
            .any(|(_,seg)| seg.to_ascii_lowercase().contains("sim")))
    }
    #[test] fn named_sim_ignores_the_content_hash() {
        assert!(!named_sim("app.XSIMAB2C.js") && !named_sim("assets/font.SIM23456.woff2") && !named_sim("locale-zh_CN.O5ERWFPT.js"));
        assert!(named_sim("sim.ABCDEFGH.js") && named_sim("design-sim.js") && named_sim("SIMULATOR.js") && named_sim("sim/data.json")
            && named_sim("app.sim.js") && named_sim("app.Simulate.js"));
    }
    fn walk(root:&Path,dir:&Path,out:&mut Vec<String>) {
        for entry in fs::read_dir(dir).unwrap() {
            let path=entry.unwrap().path();
            if path.is_dir() { walk(root,&path,out); } else { out.push(path.strip_prefix(root).unwrap().to_string_lossy().replace('\\',"/")); }
        }
    }
    /// T-P4-08b (P4 architecture §9): the development and the release CSP are the same policy,
    /// with `style-src 'self'` and no `'unsafe-inline'` anywhere; the release guard refuses a
    /// policy that would allow inline content or an external origin.
    #[test] fn csp_has_no_unsafe_inline_and_release_keeps_it() {
        let config:serde_json::Value=serde_json::from_str(include_str!("../tauri.conf.json")).unwrap();
        let dev=config["app"]["security"]["csp"].as_str().unwrap();
        let release=release_csp(dev);
        assert_eq!(release,dev,"release changes nothing");
        for csp in [dev,release.as_str()] {
            assert!(!csp.contains("unsafe-inline") && !csp.contains("unsafe-eval") && !csp.contains("unsafe-hashes"),"{csp}");
            assert!(!csp.contains("googleapis") && !csp.contains("gstatic"),"T-P4-C3: no Google font origin: {csp}");
            assert!(external_sources(csp).is_empty(),"no external origin at all: {:?}",external_sources(csp));
            assert!(csp.contains("style-src 'self';") && csp.contains("script-src 'self';") && csp.contains("font-src 'self';"),"{csp}");
            assert!(csp.contains("img-src 'self' data:;"),"the QR code data URI (T-P4-08b requirement 3): {csp}");
            assert_eq!(check_csp(csp),Ok(()));
        }
        assert_eq!(external_sources("style-src 'self' https://fonts.googleapis.com; img-src 'self' data:"),["https://fonts.googleapis.com"]);
        for loose in ["default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'",
            "default-src 'self'; script-src 'self' 'unsafe-eval'; style-src 'self'",
            "default-src 'self'; script-src 'self'; style-src 'self' 'nonce-abc'",
            "default-src 'self'; script-src 'self'; style-src 'self' 'sha256-abc='",
            "default-src 'self'; script-src 'self'; style-src 'self' 'UNSAFE-INLINE'",
            "default-src *; script-src 'self'; style-src 'self'",
            "default-src 'self'; script-src 'self'",
            "default-src 'self'; script-src 'self'; style-src 'self'; font-src https://fonts.gstatic.com",
            // fix round 1 (S1): the -attr / -elem directives and data: / blob: outside img / font
            "default-src 'self'; script-src 'self'; style-src 'self'; style-src-attr 'unsafe-inline'",
            "default-src 'self'; script-src 'self'; style-src 'self'; style-src-elem 'self' 'unsafe-inline'",
            "default-src 'self'; script-src 'self'; style-src 'self'; script-src-attr 'unsafe-hashes'",
            "default-src 'self'; script-src 'self'; style-src 'self'; script-src-elem 'self' 'nonce-abc'",
            "default-src 'self'; script-src 'self'; style-src 'self'; Style-Src-Elem 'sha256-abc='",
            "default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; style-src 'self'",
            "default-src 'self'; script-src 'self' data:; style-src 'self'",
            "default-src 'self' data:; script-src 'self'; style-src 'self'",
            "default-src 'self'; script-src 'self'; style-src 'self' blob:"] {
            assert!(check_csp(loose).is_err(),"{loose}");
        }
        assert_eq!(check_csp("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self' data:"),Ok(()));
        assert!(std::panic::catch_unwind(|| release_csp("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'")).is_err(),"the release build fails");
    }
}
