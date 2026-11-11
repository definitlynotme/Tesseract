//! §7.11 shell.log: `data/logs/shell.log`, size-rolled (1 MiB x 3 files: shell.log, .1, .2).
//! Only fixed internal codes and validated `key=value` fields are written; never raw frames,
//! page text, paths from the sidecar or secrets.
use std::{fs, io::Write, path::Path, sync::Mutex};

pub const MAX_BYTES: u64 = 1024 * 1024;
/// Files kept in total, including the live `shell.log`.
pub const KEEP: usize = 3;
static WRITE: Mutex<()> = Mutex::new(());

fn now_ms() -> u64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap_or_default().as_millis() as u64
}
/// A field value as written to shell.log: bounded, `[A-Za-z0-9_.-]` only, else `invalid`.
pub fn token(value: &str) -> String {
    if !value.is_empty() && value.len() <= 64 && value.bytes().all(|b| b.is_ascii_alphanumeric() || b"_.-".contains(&b)) {
        value.to_owned()
    } else { "invalid".into() }
}
/// Rolls `shell.log` → `.1` → … → `.{keep-1}` before a write would pass `max` bytes.
pub fn append(logs: &Path, line: &str, max: u64, keep: usize) {
    let _guard = WRITE.lock().unwrap_or_else(|e| e.into_inner());
    let _ = fs::create_dir_all(logs);
    let path = logs.join("shell.log");
    let size = fs::metadata(&path).map(|m| m.len()).unwrap_or(0);
    if size > 0 && size + line.len() as u64 + 1 > max && keep > 1 {
        let _ = fs::remove_file(logs.join(format!("shell.log.{}", keep - 1)));
        for index in (1..keep - 1).rev() {
            let _ = fs::rename(logs.join(format!("shell.log.{index}")), logs.join(format!("shell.log.{}", index + 1)));
        }
        let _ = fs::rename(&path, logs.join("shell.log.1"));
    }
    // A reader holding the file without FILE_SHARE_WRITE (e.g. .NET ReadAllText) makes the open
    // fail with a sharing violation for a moment; retry briefly instead of dropping the line.
    for attempt in 0..=OPEN_RETRIES {
        match fs::OpenOptions::new().create(true).append(true).open(&path) {
            Ok(mut file) => { let _ = writeln!(file, "{line}"); return; },
            Err(_) if attempt < OPEN_RETRIES => std::thread::sleep(std::time::Duration::from_millis(10)),
            Err(_) => return,
        }
    }
}
/// Extra open attempts (10 ms apart) when another process holds shell.log.
const OPEN_RETRIES: usize = 3;
/// `<epoch ms> <code>`: the smoke scripts match a code at the end of the line.
pub fn code(data: &Path, code: &str) { append(&data.join("logs"), &format!("{} {code}", now_ms()), MAX_BYTES, KEEP); }
/// `<epoch ms> <code> k=v …` with every value passed through [`token`].
pub fn fields(data: &Path, code: &str, fields: &[(&str, &str)]) {
    let mut line = format!("{} {code}", now_ms());
    for (key, value) in fields { line.push_str(&format!(" {key}={}", token(value))); }
    append(&data.join("logs"), &line, MAX_BYTES, KEEP);
}

#[cfg(test)] mod tests {
    use super::*;
    #[test] fn rolls_by_size_and_keeps_three_files() {
        let root = std::env::temp_dir().join(format!("tesseract-shell-log-{}-{}", std::process::id(), now_ms()));
        struct Cleanup(std::path::PathBuf);
        impl Drop for Cleanup { fn drop(&mut self) { let _ = fs::remove_dir_all(&self.0); } }
        let _cleanup = Cleanup(root.clone());
        let line = "x".repeat(99); // 100 bytes with the newline
        for n in 0..10 { append(&root, &format!("{n}{line}"), 250, 3); }
        let names: Vec<String> = { let mut v: Vec<_> = fs::read_dir(&root).unwrap().map(|e| e.unwrap().file_name().into_string().unwrap()).collect(); v.sort(); v };
        assert_eq!(names, ["shell.log", "shell.log.1", "shell.log.2"], "exactly KEEP files survive");
        for name in &names { assert!(fs::metadata(root.join(name)).unwrap().len() <= 250, "{name} stays within max"); }
        // Newest lines live in shell.log, the oldest kept ones in .2; nothing is reordered.
        assert!(fs::read_to_string(root.join("shell.log")).unwrap().starts_with('8'));
        assert!(fs::read_to_string(root.join("shell.log.1")).unwrap().starts_with('6'));
        assert!(fs::read_to_string(root.join("shell.log.2")).unwrap().starts_with('4'));
        assert_eq!((MAX_BYTES, KEEP), (1024 * 1024, 3));
    }
    #[test] fn a_briefly_locked_log_keeps_the_line() {
        let root = std::env::temp_dir().join(format!("tesseract-shell-log-lock-{}-{}", std::process::id(), now_ms()));
        struct Cleanup(std::path::PathBuf);
        impl Drop for Cleanup { fn drop(&mut self) { let _ = fs::remove_dir_all(&self.0); } }
        let _cleanup = Cleanup(root.clone());
        append(&root, "first", MAX_BYTES, KEEP);
        // Share mode read only (like .NET File.ReadAllText): appends fail until it is closed.
        use std::os::windows::fs::OpenOptionsExt;
        let reader = fs::OpenOptions::new().read(true).share_mode(1).open(root.join("shell.log")).unwrap();
        let release = std::thread::spawn(move || { std::thread::sleep(std::time::Duration::from_millis(15)); drop(reader); });
        append(&root, "second", MAX_BYTES, KEEP);
        release.join().unwrap();
        assert_eq!(fs::read_to_string(root.join("shell.log")).unwrap(), "first\nsecond\n");
    }
    #[test] fn field_values_are_tokens_only() {
        assert_eq!(token("not_needed"), "not_needed");
        assert_eq!(token("restored-1.0"), "restored-1.0");
        for bad in ["", "a b", "C:\\Users\\user", "https://u:p@h/", "{\"raw\":1}", &"y".repeat(65)] { assert_eq!(token(bad), "invalid", "{bad}"); }
    }
}
