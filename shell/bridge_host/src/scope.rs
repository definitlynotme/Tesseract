//! v2 identity: lexical Windows paths, invariant UTF-16 mapping, framed SHA256.
use std::{io, ptr::null};
use windows_sys::Win32::{Globalization::*, Security::Cryptography::*};

fn normpath(raw: &str) -> String {
    let path = raw.replace('/', "\\");
    let (root, rest) = if let Some(tail) = path.strip_prefix("\\\\") {
        let mut split = tail.splitn(3, '\\');
        let host = split.next().unwrap_or("");
        let share = split.next().unwrap_or("");
        (format!("\\\\{host}\\{share}\\"), split.next().unwrap_or(""))
    } else if path.as_bytes().get(1) == Some(&b':') {
        let rooted = path[2..].starts_with('\\');
        (format!("{}{}", &path[..2], if rooted { "\\" } else { "" }), path[2..].trim_start_matches('\\'))
    } else {
        (if path.starts_with('\\') { "\\".into() } else { "".into() }, path.trim_start_matches('\\'))
    };
    let mut parts = Vec::new();
    for part in rest.split('\\') {
        match part {
            "" | "." => (),
            ".." if parts.last().is_some_and(|p| *p != "..") => { parts.pop(); },
            ".." if root.ends_with('\\') => (),
            _ => parts.push(part),
        }
    }
    format!("{root}{}", parts.join("\\"))
}

pub fn identity_path(path: &str) -> io::Result<String> {
    let spelling = normpath(path);
    let stripped = if spelling.get(..8).is_some_and(|s| s.eq_ignore_ascii_case("\\\\?\\UNC\\")) {
        format!("\\\\{}", &spelling[8..])
    } else if spelling.starts_with("\\\\?\\") && spelling.as_bytes().get(5) == Some(&b':') {
        spelling[4..].to_owned()
    } else { spelling };
    let source: Vec<u16> = normpath(&stripped).encode_utf16().chain(Some(0)).collect();
    unsafe {
        let locale = [0u16];
        let n = LCMapStringEx(locale.as_ptr(), LCMAP_UPPERCASE, source.as_ptr(), -1,
            std::ptr::null_mut(), 0, null(), null(), 0);
        if n == 0 { return Err(io::Error::last_os_error()); }
        let mut output = vec![0u16; n as usize];
        if LCMapStringEx(locale.as_ptr(), LCMAP_UPPERCASE, source.as_ptr(), -1,
            output.as_mut_ptr(), n, null(), null(), 0) != n { return Err(io::Error::last_os_error()); }
        output.pop();
        String::from_utf16(&output).map_err(io::Error::other)
    }
}

pub fn custom_id(config: &str, data: &str) -> io::Result<String> {
    let mut bytes = b"tesseract-instance-scope-v1\0".to_vec();
    for path in [config, data] {
        let mapped = identity_path(path)?;
        bytes.extend_from_slice(&(mapped.len() as u32).to_be_bytes());
        bytes.extend_from_slice(mapped.as_bytes());
    }
    unsafe {
        let mut algorithm = std::ptr::null_mut();
        let status = BCryptOpenAlgorithmProvider(&mut algorithm, BCRYPT_SHA256_ALGORITHM, null(), 0);
        if status < 0 { return Err(io::Error::other(format!("sha256 provider {status}"))); }
        let mut digest = [0u8; 32];
        let status = BCryptHash(algorithm, null(), 0, bytes.as_ptr(), bytes.len() as u32,
            digest.as_mut_ptr(), digest.len() as u32);
        BCryptCloseAlgorithmProvider(algorithm, 0);
        if status < 0 { return Err(io::Error::other(format!("sha256 {status}"))); }
        Ok(digest[..12].iter().map(|b| format!("{b:02x}")).collect())
    }
}

pub fn names(kind: &str, id: &str) -> io::Result<serde_json::Value> {
    if kind == "default" && id == "default" {
        return Ok(serde_json::json!({"legacy_mutex": "Local\\TesseractProxyManagerSingleInstance",
            "restore_event": "Local\\TesseractProxyManagerRestoreWindow", "runtime_mutex": "Local\\TesseractRuntime",
            "shell_mutex": "Local\\TesseractShell", "quitting_event": "Local\\TesseractShellQuitting"}));
    }
    let len = match kind { "audit" => 32, "custom" => 24, _ => 0 };
    if len == 0 || id.len() != len || !id.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)) {
        return Err(io::Error::new(io::ErrorKind::InvalidInput, "invalid_scope_id"));
    }
    let label = if kind == "audit" { "Audit" } else { "Custom" };
    let stem = format!("Local\\Tesseract{label}-{id}");
    Ok(serde_json::json!({"legacy_mutex": format!("{stem}-instance"),
        "restore_event": format!("{stem}-restore"), "runtime_mutex": format!("{stem}-runtime"),
        "shell_mutex": format!("{stem}-shell"), "quitting_event": format!("{stem}-quitting")}))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn shared_v3_vectors() {
        let vectors: serde_json::Value = serde_json::from_str(include_str!("../../../bridge_schema/instance_scope_vectors.json")).unwrap();
        assert_eq!(vectors["version"], 3);
        for case in vectors["cases"].as_array().unwrap() {
            let kind = case["kind"].as_str().unwrap();
            let id = case["id"].as_str().unwrap();
            if kind == "custom" {
                assert_eq!(custom_id(case["config_dir"].as_str().unwrap(), case["data_dir"].as_str().unwrap()).unwrap(), id);
                for (raw, expected) in [("config_dir", "normalized_config_dir"), ("data_dir", "normalized_data_dir")] {
                    if let Some(value) = case[expected].as_str() { assert_eq!(identity_path(case[raw].as_str().unwrap()).unwrap(), value); }
                }
            }
            assert_eq!(names(kind, id).unwrap(), case["names"]);
        }
        assert!(names("custom", "invalid").is_err());
        assert_eq!(identity_path("C:\\a\\.\\b\\..\\c\\").unwrap(), "C:\\A\\C");
    }
}
