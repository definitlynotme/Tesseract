// Build inputs of Tesseract Setup.
//
// - app_version.txt (repo root): the version the window shows and the VERSIONINFO.
// - TESSERACT_SETUP_PAYLOAD: path of the Inno Setup package to embed. Unset (a development
//   build), nothing is embedded and the binary takes `--payload <path>` instead.
// - TESSERACT_SETUP_INSTALLED_BYTES: size of the installed files, for the free-space check
//   before elevation (0 or unset: no check; Inno still checks).
use sha2::{Digest, Sha256};
use std::{env, fs, path::PathBuf};

fn version() -> (String, [u16; 4]) {
    let root = PathBuf::from(env::var("CARGO_MANIFEST_DIR").unwrap()).join("..").join("..");
    let file = root.join("app_version.txt");
    println!("cargo:rerun-if-changed={}", file.display());
    let text = fs::read_to_string(&file).expect("app_version.txt");
    let display = text.trim().to_owned();
    let (core, rc) = match display.split_once("-rc.") { Some((c, r)) => (c, Some(r)), None => (display.as_str(), None) };
    let parts: Vec<u16> = core.split('.').map(|p| p.parse().expect("app_version.txt: x.y.z")).collect();
    assert_eq!(parts.len(), 3, "app_version.txt: x.y.z[-rc.N]");
    let build = rc.map(|r| r.parse().expect("app_version.txt: rc number")).unwrap_or(65535);
    (display, [parts[0], parts[1], parts[2], build])
}

fn rc_escape(s: &str) -> String { s.replace('\\', "\\\\").replace('"', "\"\"") }

fn main() {
    println!("cargo::rustc-check-cfg=cfg(has_payload)");
    println!("cargo:rerun-if-env-changed=TESSERACT_SETUP_PAYLOAD");
    println!("cargo:rerun-if-env-changed=TESSERACT_SETUP_INSTALLED_BYTES");
    println!("cargo:rerun-if-changed=setup.manifest");
    println!("cargo:rerun-if-changed=ui");

    let (display, v) = version();
    println!("cargo:rustc-env=SETUP_VERSION={display}");

    let bytes = env::var("TESSERACT_SETUP_INSTALLED_BYTES").ok().filter(|s| !s.trim().is_empty()).unwrap_or_else(|| "0".into());
    let bytes: u64 = bytes.trim().parse().expect("TESSERACT_SETUP_INSTALLED_BYTES: a byte count");
    println!("cargo:rustc-env=SETUP_INSTALLED_BYTES={bytes}");

    if let Some(path) = env::var_os("TESSERACT_SETUP_PAYLOAD").filter(|p| !p.is_empty()) {
        let path = fs::canonicalize(PathBuf::from(path)).expect("TESSERACT_SETUP_PAYLOAD: file not found");
        println!("cargo:rerun-if-changed={}", path.display());
        let data = fs::read(&path).expect("TESSERACT_SETUP_PAYLOAD: unreadable");
        assert!(data.starts_with(b"MZ"), "TESSERACT_SETUP_PAYLOAD is not an executable");
        let sha = Sha256::digest(&data).iter().map(|b| format!("{b:02x}")).collect::<String>();
        // dunce-style: include_bytes! does not take the \\?\ prefix canonicalize adds
        let shown = path.display().to_string();
        let shown = shown.strip_prefix(r"\\?\").unwrap_or(&shown).to_owned();
        println!("cargo:rustc-env=SETUP_PAYLOAD_PATH={shown}");
        println!("cargo:rustc-env=SETUP_PAYLOAD_SHA256={sha}");
        println!("cargo:rustc-cfg=has_payload");
    }

    // icon (ordinal 1, also the window icon), manifest, VERSIONINFO
    let out = PathBuf::from(env::var("OUT_DIR").unwrap());
    let dir = PathBuf::from(env::var("CARGO_MANIFEST_DIR").unwrap());
    let icon = dir.join("..").join("..").join("static").join("app_icon.ico");
    println!("cargo:rerun-if-changed={}", icon.display());
    let icon = fs::canonicalize(icon).expect("static/app_icon.ico");
    let icon = icon.display().to_string();
    let icon = icon.strip_prefix(r"\\?\").unwrap_or(&icon).to_owned();
    let manifest = fs::canonicalize(dir.join("setup.manifest")).unwrap().display().to_string();
    let manifest = manifest.strip_prefix(r"\\?\").unwrap_or(&manifest).to_owned();
    let [a, b, c, d] = v;
    let rc = format!(r#"#pragma code_page(65001)
1 ICON "{icon}"
1 24 "{manifest}"
1 VERSIONINFO
FILEVERSION {a},{b},{c},{d}
PRODUCTVERSION {a},{b},{c},{d}
FILEOS 0x40004
FILETYPE 0x1
BEGIN
  BLOCK "StringFileInfo"
  BEGIN
    BLOCK "040904B0"
    BEGIN
      VALUE "CompanyName", "Tesseract contributors"
      VALUE "FileDescription", "Tesseract Setup"
      VALUE "FileVersion", "{ver}"
      VALUE "ProductName", "Tesseract"
      VALUE "ProductVersion", "{ver}"
      VALUE "LegalCopyright", "Copyright (C) 2026 Tesseract contributors"
      VALUE "OriginalFilename", "Tesseract-Setup.exe"
      VALUE "InternalName", "tesseract-setup"
    END
  END
  BLOCK "VarFileInfo"
  BEGIN
    VALUE "Translation", 0x409, 1200
  END
END
"#, icon = rc_escape(&icon), manifest = rc_escape(&manifest), ver = rc_escape(&display));
    let rc_path = out.join("setup.rc");
    fs::write(&rc_path, rc).unwrap();
    embed_resource::compile(&rc_path, embed_resource::NONE).manifest_required().unwrap();
}
