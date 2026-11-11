//! T-P3-06 update hand-off (plan §2.2.5; PROTOCOL §11 `update.prepareExit`, §13 「更新退出与失败恢复」;
//! ruling P2-27: the shell sends `app.quit{reason:update}` itself after `exitResponse(accepted=true)`).
//!
//! 1. `update.prepareExit` (shell-only topic) → checks: not expired, no other quit, no other
//!    hand-off, the installer is inside the sidecar data directory, SHA-256 equals `sha256`, the
//!    Authenticode signature is not broken → `update.exitResponse{handoff_id, accepted, reason_code}`.
//! 2. The reply `{recorded:true}` to an acceptance → `app.quit{reason:"update"}` through the T-P3-04
//!    quit state machine (lifecycle.rs), which enters `Installing` once the sidecar has exited.
//! 3. Install: the sidecar stopped, said `bye` with exit code 0 and both recovery domains settled;
//!    no process image under the install roots is left (EnumProcesses + QueryFullProcessImageName;
//!    wait, then fail); the installer is opened deny-write, re-hashed and kept open until
//!    `CreateProcess` returned; `Command::new(installer_path).args(args)`.
//! 4. Any failure: back to Running (close-to-tray again), the window is restored, the sidecar is
//!    relaunched in the stopped state and `update.installResult{receipt_ref, launched:false,
//!    reason_code}` goes to the new epoch. Success: the shell exits 0 (no `installResult{true}`:
//!    the sidecar is gone; the next start reads the receipt, P2-26/27).
//! shell.log keeps only ids, the version and fixed result codes, never frames or paths.
use crate::shell_log;
use serde_json::{json, Value};
use std::{fs, io::{self, Read}, os::windows::{fs::OpenOptionsExt, io::AsRawHandle, process::CommandExt},
    path::{Path, PathBuf}, process::{Command, Stdio}, sync::{Arc, Mutex, MutexGuard}, time::{Duration, Instant}};

pub const TOPIC: &str = "update.prepareExit";
pub const EXIT_RESPONSE: &str = "update.exitResponse";
pub const INSTALL_RESULT: &str = "update.installResult";
/// Residual processes under the install roots: wait this long, then fail the hand-off.
pub const RESIDUAL_WAIT: Duration = Duration::from_secs(10);
/// `update.exitResponse` sent and no reply: after this (or the hand-off's own `expires_at_ms`,
/// whichever comes first) the hand-off is dropped, so a later `prepareExit` is not `handoff_busy`
/// for the rest of the epoch (review S1/S5). A late `{recorded:true}` then starts nothing.
pub const RESPONSE_WAIT: Duration = Duration::from_secs(30);
const RESIDUAL_POLL: Duration = Duration::from_millis(250);
const FILE_SHARE_READ: u32 = 1;
const ARG_LIMIT: usize = 16;
/// bye recovery states that leave nothing of ours behind (PROTOCOL §3).
const SETTLED: [&str; 3] = ["not_needed", "restored", "not_owned"];
const KEYS: [&str; 8] = ["handoff_id", "receipt_ref", "version", "verified", "expires_at_ms", "installer_path", "sha256", "args"];

/// One validated `update.prepareExit` DTO (PROTOCOL §11 shape and invariants).
#[derive(Clone, Debug, PartialEq)]
pub struct Handoff {
    pub handoff_id: String, pub receipt_ref: String, pub version: String, pub expires_at_ms: u64,
    pub installer_path: PathBuf, pub sha256: String, pub args: Vec<String>,
}
fn ident(value: &str, max: usize) -> bool {
    !value.is_empty() && value.len() <= max && value.bytes().all(|b| b.is_ascii_alphanumeric() || b"_.-".contains(&b))
}
/// `/[A-Z][A-Z0-9_]{0,31}`: an installer switch, never a path or shell text.
pub fn switch(arg: &str) -> bool {
    let b = arg.as_bytes();
    b.len() >= 2 && b.len() <= 33 && b[0] == b'/' && b[1].is_ascii_uppercase()
        && b[2..].iter().all(|c| c.is_ascii_uppercase() || c.is_ascii_digit() || *c == b'_')
}
pub fn installer_name(version: &str) -> String { format!("Tesseract-Setup-{version}-win-x64.exe") }
/// Drive-letter absolute Windows path (no UNC, `\\?\` or `\\.\`), no `.`/`..` component, file name
/// `Tesseract-Setup-<version>-win-x64.exe`.
pub fn path_shape(raw: &str, version: &str) -> Result<PathBuf, &'static str> {
    let b = raw.as_bytes();
    if raw.len() > 1024 || raw.chars().any(char::is_control) || raw.contains('/') { return Err("installer_path_invalid"); }
    if b.len() < 4 || !b[0].is_ascii_alphabetic() || b[1] != b':' || b[2] != b'\\' { return Err("installer_path_invalid"); }
    let parts: Vec<&str> = raw[3..].split('\\').collect();
    if parts.iter().any(|p| p.is_empty() || *p == "." || *p == "..") { return Err("installer_path_invalid"); }
    if parts.last() != Some(&installer_name(version).as_str()) { return Err("installer_name_invalid"); }
    Ok(PathBuf::from(raw))
}
impl Handoff {
    pub fn parse(data: &Value) -> Result<Self, &'static str> {
        let object = data.as_object().ok_or("invalid_handoff")?;
        if object.len() != KEYS.len() || !KEYS.iter().all(|k| object.contains_key(*k)) { return Err("invalid_handoff"); }
        let text = |k: &str| data[k].as_str().ok_or("invalid_handoff");
        let handoff_id = text("handoff_id")?; let receipt_ref = text("receipt_ref")?; let version = text("version")?;
        if !ident(handoff_id, 128) || !ident(version, 64) { return Err("invalid_handoff"); }
        if receipt_ref.is_empty() || receipt_ref.len() > 128 || !receipt_ref.bytes().all(|b| b.is_ascii_graphic()) { return Err("invalid_handoff"); }
        if data["verified"] != true { return Err("not_verified"); }
        let expires_at_ms = data["expires_at_ms"].as_u64().ok_or("invalid_handoff")?;
        let sha256 = text("sha256")?;
        if sha256.len() != 64 || !sha256.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)) { return Err("invalid_sha256"); }
        let args: Vec<String> = data["args"].as_array().ok_or("invalid_args")?.iter()
            .map(|a| a.as_str().filter(|s| switch(s)).map(String::from)).collect::<Option<_>>().ok_or("invalid_args")?;
        if args.is_empty() || args.len() > ARG_LIMIT { return Err("invalid_args"); }
        let installer_path = path_shape(text("installer_path")?, version)?;
        Ok(Self { handoff_id: handoff_id.into(), receipt_ref: receipt_ref.into(), version: version.into(), expires_at_ms, installer_path, sha256: sha256.into(), args })
    }
}
/// Accept conditions the shell owns: the hand-off has not expired and no other quit is running.
pub fn precheck(handoff: &Handoff, now_ms: u64, running: bool) -> Result<(), &'static str> {
    if handoff.expires_at_ms <= now_ms { Err("expired") } else if !running { Err("quit_in_progress") } else { Ok(()) }
}

// ------------------------------------------------------------------ installer bytes

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Signature { Valid, Unsigned, Invalid }
/// Authenticode policy: a broken or untrusted signature is refused; an unsigned installer is
/// accepted because official artifacts are Authenticode-unsigned by project policy
/// (scripts/build_windows.ps1). The bytes are pinned by the sidecar's signed manifest (sha256).
pub fn signature_allowed(signature: Signature) -> bool { signature != Signature::Invalid }

/// SHA-256 (lower hex) through CNG, read in 1 MiB blocks.
pub fn sha256_hex(reader: &mut impl Read) -> io::Result<String> {
    use windows::Win32::Security::Cryptography::*;
    struct Hash(BCRYPT_HASH_HANDLE);
    impl Drop for Hash { fn drop(&mut self) { unsafe { let _ = BCryptDestroyHash(self.0); } } }
    let status = |s: windows::Win32::Foundation::NTSTATUS| if s.is_ok() { Ok(()) } else { Err(io::Error::other("bcrypt_failed")) };
    let mut handle = BCRYPT_HASH_HANDLE::default();
    unsafe { status(BCryptCreateHash(BCRYPT_SHA256_ALG_HANDLE, &mut handle, None, None, 0))?; }
    let hash = Hash(handle);
    let mut buffer = vec![0u8; 1 << 20];
    loop {
        let read = reader.read(&mut buffer)?;
        if read == 0 { break; }
        unsafe { status(BCryptHashData(hash.0, &buffer[..read], 0))?; }
    }
    let mut digest = [0u8; 32];
    unsafe { status(BCryptFinishHash(hash.0, &mut digest, 0))?; }
    Ok(digest.iter().map(|b| format!("{b:02x}")).collect())
}
/// The final (resolved) path of an open file, so a reparse point cannot move it out of the root.
fn final_path(file: &fs::File) -> io::Result<PathBuf> {
    use windows::Win32::{Foundation::HANDLE, Storage::FileSystem::{GetFinalPathNameByHandleW, FILE_NAME_NORMALIZED}};
    let mut buffer = vec![0u16; 1024];
    let length = unsafe { GetFinalPathNameByHandleW(HANDLE(file.as_raw_handle()), &mut buffer, FILE_NAME_NORMALIZED) } as usize;
    if length == 0 || length >= buffer.len() { return Err(io::Error::other("final_path_failed")); }
    Ok(PathBuf::from(String::from_utf16_lossy(&buffer[..length])))
}
/// Opens the installer deny-write (FILE_SHARE_READ only: no writer, no rename or delete) and checks
/// it: inside `root` after every link is resolved, a regular file, SHA-256 equal, signature allowed.
/// The returned handle must stay open until `CreateProcess` returned.
pub fn verify(handoff: &Handoff, root: &Path, platform: &dyn Platform) -> Result<fs::File, &'static str> {
    let root = fs::canonicalize(root).map_err(|_| "data_root_invalid")?;
    let mut file = fs::OpenOptions::new().read(true).share_mode(FILE_SHARE_READ).open(&handoff.installer_path).map_err(|_| "installer_unreadable")?;
    if !file.metadata().map(|m| m.is_file()).unwrap_or(false) { return Err("installer_unreadable"); }
    let resolved = final_path(&file).map_err(|_| "installer_unreadable")?;
    if !resolved.starts_with(&root) || resolved == root { return Err("installer_outside_data"); }
    let digest = sha256_hex(&mut file).map_err(|_| "installer_unreadable")?;
    if digest != handoff.sha256 { return Err("hash_mismatch"); }
    if !signature_allowed(platform.signature(&file, &handoff.installer_path)) { return Err("signature_invalid"); }
    Ok(file)
}

// ------------------------------------------------------------------ platform port

/// Win32 effects of the hand-off; tests use a stand-in (never a real installer).
pub trait Platform: Send + Sync {
    /// Pids (other than `own`) whose image path lies under one of `roots`. Fails closed: no roots
    /// is `residual_roots_unknown`, a failed process enumeration `residual_unknown` (review S1/S2).
    fn residual(&self, roots: &[PathBuf], own: u32) -> Result<Vec<u32>, &'static str>;
    fn signature(&self, file: &fs::File, path: &Path) -> Signature;
    /// Creates the installer process; returns once `CreateProcess` returned.
    fn launch(&self, path: &Path, args: &[String]) -> io::Result<()>;
}
pub struct Win32Platform;
/// `C:\dir\` lower case, `\\?\` stripped, for prefix comparison of image paths.
pub fn path_prefix(path: &Path) -> String {
    let raw = path.to_string_lossy().replace('/', "\\");
    let mut text = raw.strip_prefix(r"\\?\").unwrap_or(&raw).to_lowercase();
    if !text.ends_with('\\') { text.push('\\'); }
    text
}
pub fn under(image: &str, prefixes: &[String]) -> bool {
    let image = path_prefix(Path::new(image));
    prefixes.iter().any(|p| image.starts_with(p.as_str()) && image.len() > p.len())
}
impl Platform for Win32Platform {
    fn residual(&self, roots: &[PathBuf], own: u32) -> Result<Vec<u32>, &'static str> {
        use windows::Win32::{Foundation::CloseHandle, System::{ProcessStatus::K32EnumProcesses,
            Threading::{OpenProcess, QueryFullProcessImageNameW, PROCESS_NAME_WIN32, PROCESS_QUERY_LIMITED_INFORMATION}}};
        let prefixes: Vec<String> = roots.iter().map(|r| path_prefix(r)).collect();
        if prefixes.is_empty() { return Err("residual_roots_unknown"); }
        let mut pids = vec![0u32; 4096];
        let count = loop {
            let bytes = (pids.len() * 4) as u32; let mut needed = 0u32;
            if !unsafe { K32EnumProcesses(pids.as_mut_ptr(), bytes, &mut needed) }.as_bool() { return Err("residual_unknown"); }
            if needed < bytes { break needed as usize / 4; }
            if pids.len() >= 1 << 20 { return Err("residual_unknown"); }
            pids.resize(pids.len() * 2, 0);
        };
        let mut found = Vec::new();
        for &pid in &pids[..count] {
            if pid == 0 || pid == own { continue; }
            let Ok(process) = (unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, false, pid) }) else { continue };
            let mut buffer = vec![0u16; 1024]; let mut size = buffer.len() as u32;
            let named = unsafe { QueryFullProcessImageNameW(process, PROCESS_NAME_WIN32, windows::core::PWSTR(buffer.as_mut_ptr()), &mut size) }.is_ok();
            unsafe { let _ = CloseHandle(process); }
            if named && under(&String::from_utf16_lossy(&buffer[..size as usize]), &prefixes) { found.push(pid); }
        }
        Ok(found)
    }
    fn signature(&self, file: &fs::File, path: &Path) -> Signature {
        use windows::Win32::{Foundation::{HANDLE, HWND, TRUST_E_NOSIGNATURE}, Security::WinTrust::*};
        let wide: Vec<u16> = path.as_os_str().encode_wide_z();
        let mut info = WINTRUST_FILE_INFO { cbStruct: std::mem::size_of::<WINTRUST_FILE_INFO>() as u32,
            pcwszFilePath: windows::core::PCWSTR(wide.as_ptr()), hFile: HANDLE(file.as_raw_handle()), ..Default::default() };
        let mut data = WINTRUST_DATA { cbStruct: std::mem::size_of::<WINTRUST_DATA>() as u32, dwUIChoice: WTD_UI_NONE,
            fdwRevocationChecks: WTD_REVOKE_NONE, dwUnionChoice: WTD_CHOICE_FILE, dwStateAction: WTD_STATEACTION_VERIFY,
            dwProvFlags: WTD_CACHE_ONLY_URL_RETRIEVAL, ..Default::default() };
        data.Anonymous.pFile = &mut info;
        let mut action = WINTRUST_ACTION_GENERIC_VERIFY_V2;
        let status = unsafe { WinVerifyTrust(HWND(-1isize as _), &mut action, &mut data as *mut _ as *mut _) };
        data.dwStateAction = WTD_STATEACTION_CLOSE;
        unsafe { let _ = WinVerifyTrust(HWND(-1isize as _), &mut action, &mut data as *mut _ as *mut _); }
        match status { 0 => Signature::Valid, s if s == TRUST_E_NOSIGNATURE.0 => Signature::Unsigned, _ => Signature::Invalid }
    }
    fn launch(&self, path: &Path, args: &[String]) -> io::Result<()> {
        // Leave a job the shell may sit in (a launcher's kill-on-close job would take the
        // installer with the exiting shell); a job without breakaway refuses it: plain launch.
        const CREATE_BREAKAWAY_FROM_JOB: u32 = 0x0100_0000;
        let spawn = |flags: u32| Command::new(path).args(args).stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null()).creation_flags(flags).spawn();
        match spawn(CREATE_BREAKAWAY_FROM_JOB) {
            Ok(_) => Ok(()),
            Err(e) if e.raw_os_error() == Some(5) => spawn(0).map(|_| ()),
            Err(e) => Err(e),
        }
    }
}
trait WideZ { fn encode_wide_z(&self) -> Vec<u16>; }
impl WideZ for std::ffi::OsStr {
    fn encode_wide_z(&self) -> Vec<u16> { use std::os::windows::ffi::OsStrExt; self.encode_wide().chain(Some(0)).collect() }
}

// ------------------------------------------------------------------ hand-off state

#[derive(Clone, Debug, PartialEq)]
enum Stage {
    Idle,
    /// Checks running on their own thread.
    Verifying(Handoff),
    /// The answer is ready for the worker to send.
    Decided { handoff: Handoff, reason: Option<&'static str> },
    /// `update.exitResponse` sent at `since`; waiting for `{recorded:true}` (bounded, see
    /// [`RESPONSE_WAIT`]).
    Responding { handoff: Handoff, accepted: bool, since: Instant },
    /// Recorded acceptance: `app.quit{reason:update}` is under way.
    Accepted(Handoff),
    Installing(Handoff),
}
#[derive(Clone, Debug, PartialEq)]
struct Bye { exit_code: Option<i64>, proxy: String, firewall: String }
#[derive(Default)]
struct Facts { bye: Option<Bye>, gone: Option<(bool, Option<u32>)> }
struct State { stage: Stage, facts: Facts, failure: Option<(String, &'static str)> }

pub struct Config { pub data: PathBuf, pub roots: Vec<PathBuf>, pub log: Option<PathBuf>, pub residual_wait: Duration }
pub struct Update { state: Mutex<State>, config: Mutex<Config>, platform: Box<dyn Platform> }
impl Update {
    pub fn new(platform: Box<dyn Platform>) -> Arc<Self> {
        Arc::new(Self { state: Mutex::new(State { stage: Stage::Idle, facts: Facts::default(), failure: None }),
            config: Mutex::new(Config { data: PathBuf::new(), roots: Vec::new(), log: None, residual_wait: RESIDUAL_WAIT }), platform })
    }
    pub fn configure(&self, data: PathBuf, roots: Vec<PathBuf>, log: Option<PathBuf>) {
        let mut config = self.config.lock().unwrap_or_else(|e| e.into_inner());
        config.data = data; config.roots = roots; config.log = log;
    }
    #[cfg(test)] pub fn set_residual_wait(&self, wait: Duration) { self.config.lock().unwrap().residual_wait = wait; }
    fn state(&self) -> MutexGuard<'_, State> { self.state.lock().unwrap_or_else(|e| e.into_inner()) }
    fn log(&self, code: &str, fields: &[(&str, &str)]) {
        let dir = self.config.lock().unwrap_or_else(|e| e.into_inner()).log.clone();
        if let Some(dir) = dir { shell_log::fields(&dir, code, fields); }
    }
    /// A new sidecar session: an unanswered hand-off belonged to the dead epoch.
    pub fn session(&self) {
        let mut state = self.state();
        if matches!(state.stage, Stage::Verifying(_) | Stage::Decided{..} | Stage::Responding{..}) { state.stage = Stage::Idle; drop(state); self.log("update_handoff_dropped_new_epoch", &[]); }
    }
    pub fn busy(&self) -> bool { self.state().stage != Stage::Idle }
    /// Drops a `Responding` hand-off whose reply did not come within [`RESPONSE_WAIT`] or before
    /// its `expires_at_ms` (worker loop and every new `prepareExit`). True when one was dropped.
    pub fn expire_response(&self, now: Instant, now_ms: u64) -> bool {
        let mut state = self.state();
        let Stage::Responding { handoff, since, .. } = &state.stage else { return false };
        if now.saturating_duration_since(*since) < RESPONSE_WAIT && handoff.expires_at_ms > now_ms { return false; }
        let id = handoff.handoff_id.clone();
        state.stage = Stage::Idle; drop(state);
        self.log("update_exit_response_timeout", &[("handoff", &id)]);
        true
    }
    /// One `update.prepareExit` edge. Returns an immediate decline (exitResponse args) when another
    /// hand-off is in flight or the DTO is unusable but names its `handoff_id`; otherwise the
    /// checks run on their own thread and `take_response` yields the answer.
    pub fn on_prepare_exit(self: &Arc<Self>, data: &Value, now_ms: u64, running: bool) -> Option<Value> {
        let handoff = match Handoff::parse(data) {
            Ok(h) => h,
            Err(reason) => {
                self.log("update_handoff_rejected", &[("reason", reason)]);
                let id = data["handoff_id"].as_str().filter(|id| ident(id, 128))?;
                return Some(response(id, false, Some(reason)));
            },
        };
        self.log("update_handoff_received", &[("handoff", &handoff.handoff_id), ("version", &handoff.version)]);
        self.expire_response(Instant::now(), now_ms);
        {
            let mut state = self.state();
            if state.stage != Stage::Idle {
                drop(state); self.log("update_handoff_declined", &[("handoff", &handoff.handoff_id), ("reason", "handoff_busy")]);
                return Some(response(&handoff.handoff_id, false, Some("handoff_busy")));
            }
            if let Err(reason) = precheck(&handoff, now_ms, running) {
                state.stage = Stage::Decided { handoff, reason: Some(reason) }; return None;
            }
            state.stage = Stage::Verifying(handoff.clone());
        }
        let this = self.clone();
        std::thread::spawn(move || {
            let data = this.config.lock().unwrap_or_else(|e| e.into_inner()).data.clone();
            let reason = verify(&handoff, &data, this.platform.as_ref()).err();
            let mut state = this.state();
            if state.stage == Stage::Verifying(handoff.clone()) { state.stage = Stage::Decided { handoff, reason }; }
        });
        None
    }
    /// The decided answer, re-checked against the clock and the quit machine at send time.
    pub fn take_response(&self, now_ms: u64, running: bool) -> Option<Value> {
        let mut state = self.state();
        let Stage::Decided { handoff, reason } = state.stage.clone() else { return None };
        let reason = reason.or_else(|| precheck(&handoff, now_ms, running).err());
        let frame = response(&handoff.handoff_id, reason.is_none(), reason);
        state.stage = Stage::Responding { handoff: handoff.clone(), accepted: reason.is_none(), since: Instant::now() };
        drop(state);
        self.log("update_exit_response", &[("handoff", &handoff.handoff_id), ("accepted", if reason.is_none() {"true"} else {"false"}), ("reason", reason.unwrap_or("-"))]);
        Some(frame)
    }
    /// The sidecar's reply to the `update.exitResponse` of `handoff_id`. True: the acceptance is
    /// recorded, the caller starts `app.quit{reason:update}` now. A reply that no longer matches
    /// the waiting hand-off (it timed out, or another one is waiting) starts nothing.
    pub fn on_response_reply(&self, handoff_id: &str, recorded: bool) -> bool {
        let mut state = self.state();
        let waiting = match &state.stage { Stage::Responding { handoff, accepted, .. } if handoff.handoff_id == handoff_id => Some((handoff.clone(), *accepted)), _ => None };
        let Some((handoff, accepted)) = waiting else {
            drop(state); self.log("update_exit_response_late", &[("handoff", handoff_id)]); return false;
        };
        let go = accepted && recorded;
        state.stage = if go { Stage::Accepted(handoff.clone()) } else { Stage::Idle };
        state.facts = Facts::default();
        drop(state);
        self.log("update_exit_response_reply", &[("handoff", &handoff.handoff_id), ("recorded", if recorded {"true"} else {"false"})]);
        go
    }
    /// The quit machine refused `app.quit{reason:update}` (another quit had begun meanwhile).
    pub fn quit_refused(&self) {
        let mut state = self.state();
        if let Stage::Accepted(handoff) = state.stage.clone() {
            state.stage = Stage::Idle; drop(state);
            self.log("update_handoff_abandoned", &[("handoff", &handoff.handoff_id), ("reason", "quit_in_progress")]);
        }
    }
    pub fn accepted(&self) -> bool { matches!(self.state().stage, Stage::Accepted(_)) }
    /// `bye` of the sidecar that is quitting for the update.
    pub fn on_bye(&self, frame: &Value) {
        let mut state = self.state();
        if !matches!(state.stage, Stage::Accepted(_)) { return; }
        let domain = |d: &str| frame["network_recovery"][d]["state"].as_str().unwrap_or("missing").to_owned();
        state.facts.bye = Some(Bye { exit_code: frame["exit_code"].as_i64(), proxy: domain("proxy"), firewall: domain("firewall") });
    }
    /// The sidecar process is gone (`stopped`: its handle signalled) with `code`.
    pub fn sidecar_gone(&self, stopped: bool, code: Option<u32>) {
        let mut state = self.state();
        if matches!(state.stage, Stage::Accepted(_)) { state.facts.gone = Some((stopped, code)); }
    }
    /// The install step (lifecycle `Action::Install`), on its own thread. Ok: the installer process
    /// was created. Err: the reason code; the failure is kept for `update.installResult`.
    pub fn install(&self) -> Result<(), &'static str> {
        let (handoff, facts) = {
            let mut state = self.state();
            let Stage::Accepted(handoff) = state.stage.clone() else { return Err("no_handoff") };
            state.stage = Stage::Installing(handoff.clone());
            (handoff, std::mem::take(&mut state.facts))
        };
        let result = self.install_checked(&handoff, facts);
        let mut state = self.state();
        state.stage = Stage::Idle;
        match result {
            Ok(()) => { drop(state); self.log("update_installer_launched", &[("handoff", &handoff.handoff_id), ("version", &handoff.version)]); Ok(()) },
            Err(reason) => {
                state.failure = Some((handoff.receipt_ref.clone(), reason)); drop(state);
                self.log("update_install_failed", &[("handoff", &handoff.handoff_id), ("version", &handoff.version), ("reason", reason)]);
                Err(reason)
            },
        }
    }
    fn install_checked(&self, handoff: &Handoff, facts: Facts) -> Result<(), &'static str> {
        let (stopped, code) = facts.gone.ok_or("sidecar_running")?;
        if !stopped { return Err("sidecar_running"); }
        let bye = facts.bye.ok_or("bye_missing")?;
        if code != Some(0) || bye.exit_code != Some(0) { return Err("sidecar_exit_nonzero"); }
        if !SETTLED.contains(&bye.proxy.as_str()) || !SETTLED.contains(&bye.firewall.as_str()) { return Err("network_not_recovered"); }
        let (data, roots, wait) = { let c = self.config.lock().unwrap_or_else(|e| e.into_inner()); (c.data.clone(), c.roots.clone(), c.residual_wait) };
        // Without known roots the self-check would pass vacuously: fail closed (review S1/S2).
        if roots.is_empty() { return Err("residual_roots_unknown"); }
        let deadline = Instant::now() + wait;
        loop {
            let left = self.platform.residual(&roots, std::process::id())?;
            if left.is_empty() { break; }
            if Instant::now() >= deadline {
                self.log("update_residual_processes", &[("count", &left.len().to_string())]);
                return Err("residual_processes");
            }
            std::thread::sleep(RESIDUAL_POLL);
        }
        // Held deny-write from the re-hash until CreateProcess returned.
        let locked = verify(handoff, &data, self.platform.as_ref())?;
        let launched = self.platform.launch(&handoff.installer_path, &handoff.args);
        drop(locked);
        launched.map_err(|_| "launch_failed")
    }
    /// After a failed install, the first session of the relaunched sidecar reports it once.
    pub fn take_failure(&self) -> Option<Value> {
        self.state().failure.take().map(|(receipt_ref, reason)| json!({"receipt_ref":receipt_ref,"launched":false,"reason_code":reason}))
    }
}
pub fn response(handoff_id: &str, accepted: bool, reason: Option<&str>) -> Value {
    let mut args = json!({"handoff_id":handoff_id,"accepted":accepted});
    if let Some(reason) = reason { args["reason_code"] = json!(reason); }
    args
}
/// A shell-only command request (PROTOCOL §10: from=shell, owner shell).
pub fn request_frame(id: u64, cmd: &str, args: Value) -> Value {
    json!({"t":"req","id":id,"cmd":cmd,"args":args,"from":"shell","req_id":format!("shell-update-{id}"),"owner":"shell"})
}
/// Residual-process roots (review S1/S2):
/// - a debug override (`TESSERACT_SHELL_DEV_INSTALL_ROOT`, debug builds only) replaces the rest;
/// - release builds: the shell's own directory (the install directory `{app}`), plus the
///   `--sidecar` directory when one is given, else the source-mode `bin` (Mihomo);
/// - debug builds: the `--sidecar` directory, else the source-mode `bin` (the shell's own
///   `target\debug` also holds running test binaries, so it is not a root there).
///
/// Every named root must resolve; if any does not, the result is empty and the install fails
/// closed with `residual_roots_unknown` instead of checking a partial set.
pub fn install_roots(shell_exe: &Path, sidecar: Option<&Path>, source_bin: Option<&Path>, release: bool, debug_override: Option<PathBuf>) -> Vec<PathBuf> {
    let mut named: Vec<Option<PathBuf>> = Vec::new();
    if let Some(root) = debug_override { named.push(Some(root)); }
    else {
        if release { named.push(shell_exe.parent().map(Path::to_path_buf)); }
        match (sidecar, source_bin) {
            (Some(sidecar), _) => named.push(sidecar.parent().map(Path::to_path_buf)),
            (None, Some(bin)) => named.push(Some(bin.to_path_buf())),
            (None, None) => {},
        }
    }
    let mut roots: Vec<PathBuf> = Vec::new();
    for root in named {
        let Some(resolved) = root.filter(|r| !r.as_os_str().is_empty()).and_then(|r| fs::canonicalize(r).ok()) else { return Vec::new() };
        if !roots.contains(&resolved) { roots.push(resolved); }
    }
    roots
}

#[cfg(test)] pub mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};
    pub const VERSION: &str = "9.9.9";
    /// Stand-in platform: scripted residual rounds, signature and launch outcome; records launches.
    pub struct Fake { pub residual_rounds: AtomicUsize, pub signature: Signature, pub fail_launch: bool, pub fail_enum: bool, pub launches: Mutex<Vec<(PathBuf, Vec<String>)>> }
    impl Fake { pub fn new() -> Self { Self { residual_rounds: AtomicUsize::new(0), signature: Signature::Unsigned, fail_launch: false, fail_enum: false, launches: Mutex::new(Vec::new()) } } }
    impl Platform for Arc<Fake> {
        fn residual(&self, roots: &[PathBuf], _: u32) -> Result<Vec<u32>, &'static str> {
            assert!(!roots.is_empty(), "install_checked never asks without roots");
            if self.fail_enum { return Err("residual_unknown"); }
            if self.residual_rounds.load(Ordering::SeqCst) == 0 { return Ok(Vec::new()); }
            self.residual_rounds.fetch_sub(1, Ordering::SeqCst); Ok(vec![4242])
        }
        fn signature(&self, _: &fs::File, _: &Path) -> Signature { self.signature }
        fn launch(&self, path: &Path, args: &[String]) -> io::Result<()> {
            // The installer is still held deny-write here.
            assert!(fs::OpenOptions::new().write(true).open(path).is_err(), "the installer must be locked during CreateProcess");
            self.launches.lock().unwrap().push((path.to_owned(), args.to_vec()));
            if self.fail_launch { Err(io::Error::from_raw_os_error(193)) } else { Ok(()) }
        }
    }
    pub struct Dir(pub PathBuf);
    impl Drop for Dir { fn drop(&mut self) { let _ = fs::remove_dir_all(&self.0); } }
    pub fn temp_dir(tag: &str) -> Dir {
        static NEXT: AtomicUsize = AtomicUsize::new(0);
        let dir = std::env::temp_dir().join(format!("tesseract-update-{tag}-{}-{}", std::process::id(), NEXT.fetch_add(1, Ordering::SeqCst)));
        fs::create_dir_all(&dir).unwrap(); Dir(fs::canonicalize(&dir).unwrap())
    }
    /// An installer stand-in under `<data>/updates` and its hand-off DTO.
    pub fn installer(data: &Path, bytes: &[u8]) -> (PathBuf, Value) {
        let dir = data.join("updates"); fs::create_dir_all(&dir).unwrap();
        let path = dir.join(installer_name(VERSION)); fs::write(&path, bytes).unwrap();
        let sha = sha256_hex(&mut fs::File::open(&path).unwrap()).unwrap();
        let shown = path.to_string_lossy().trim_start_matches(r"\\?\").to_owned();
        (path, json!({"handoff_id":"update-handoff-1","receipt_ref":"receipt-1","version":VERSION,"verified":true,
            "expires_at_ms":u64::MAX/2,"installer_path":shown,"sha256":sha,"args":["/SILENT","/SUPPRESSMSGBOXES","/NORESTART"]}))
    }
    fn wait_response(update: &Update) -> Value {
        let deadline = Instant::now() + Duration::from_secs(10);
        loop {
            if let Some(r) = update.take_response(1, true) { return r; }
            assert!(Instant::now() < deadline, "no decision"); std::thread::sleep(Duration::from_millis(5));
        }
    }
    fn bye(code: i64, proxy: &str) -> Value { json!({"t":"bye","exit_code":code,"network_recovery":{"proxy":{"state":proxy},"firewall":{"state":"not_needed"}}}) }
    fn armed(fake: &Arc<Fake>, data: &Path, bytes: &[u8]) -> Arc<Update> {
        let update = Update::new(Box::new(fake.clone()));
        update.configure(data.to_owned(), vec![data.to_owned()], None);
        let (_, dto) = installer(data, bytes);
        assert!(update.on_prepare_exit(&dto, 1, true).is_none());
        assert_eq!(wait_response(&update), json!({"handoff_id":"update-handoff-1","accepted":true}));
        assert!(update.on_response_reply("update-handoff-1", true), "a recorded acceptance starts app.quit{{reason:update}}");
        update
    }

    #[test] fn dto_shape_and_installer_path_policy() {
        let dir = temp_dir("shape"); let (_, dto) = installer(&dir.0, b"x");
        let h = Handoff::parse(&dto).unwrap();
        assert_eq!((h.version.as_str(), h.args.len()), (VERSION, 3));
        for (key, value) in [("verified", json!(false)), ("sha256", json!("ABC")), ("args", json!([])), ("args", json!(["--silent"])),
            ("args", json!(["/SILENT && calc"])), ("args", json!(["C:\\x.exe"])), ("handoff_id", json!("a b")), ("expires_at_ms", json!(-1))] {
            let mut bad = dto.clone(); bad[key] = value.clone();
            assert!(Handoff::parse(&bad).is_err(), "{key}={value}");
        }
        let mut extra = dto.clone(); extra["path"] = json!("x"); assert!(Handoff::parse(&extra).is_err(), "closed key set");
        let name = installer_name(VERSION);
        for raw in [format!(r"\\server\share\{name}"), format!(r"\\?\C:\d\{name}"), format!(r"\\.\C:\d\{name}"), format!(r"C:\d\..\{name}"),
            format!("C:/d/{name}"), format!(r"d\{name}"), r"C:\d\Tesseract-Setup-1.0.0-win-x64.exe".into(), format!(r"C:\d\.\{name}"), format!("C:\\d\\\n{name}")] {
            assert!(path_shape(&raw, VERSION).is_err(), "{raw}");
        }
        assert!(path_shape(&format!(r"C:\Users\user\AppData\Local\Tesseract\updates\{name}"), VERSION).is_ok());
        assert!(switch("/SILENT") && switch("/NORESTART") && switch("/X") && !switch("/") && !switch("/silent") && !switch(&format!("/{}", "A".repeat(33))));
    }
    #[test] fn decline_when_expired_or_another_quit_or_another_handoff() {
        let dir = temp_dir("decline"); let (_, dto) = installer(&dir.0, b"x");
        let update = Update::new(Box::new(Arc::new(Fake::new()))); update.configure(dir.0.clone(), vec![], None);
        // Expired at arrival.
        let mut old = dto.clone(); old["expires_at_ms"] = json!(5);
        assert!(update.on_prepare_exit(&old, 10, true).is_none());
        assert_eq!(update.take_response(10, true).unwrap(), json!({"handoff_id":"update-handoff-1","accepted":false,"reason_code":"expired"}));
        assert!(!update.on_response_reply("update-handoff-1", true)); assert!(!update.busy());
        // Another quit is running.
        assert!(update.on_prepare_exit(&dto, 10, false).is_none());
        assert_eq!(update.take_response(10, false).unwrap()["reason_code"], "quit_in_progress");
        update.on_response_reply("update-handoff-1", true);
        // Verified, but expired / a quit began before the answer went out: re-checked at send time.
        assert!(update.on_prepare_exit(&dto, 10, true).is_none());
        let deadline = Instant::now() + Duration::from_secs(10);
        while matches!(update.state().stage, Stage::Verifying(_)) { assert!(Instant::now() < deadline); std::thread::sleep(Duration::from_millis(5)); }
        assert_eq!(update.take_response(u64::MAX, true).unwrap()["reason_code"], "expired");
        update.on_response_reply("update-handoff-1", true);
        // A second hand-off while one is in flight is declined at once.
        assert!(update.on_prepare_exit(&dto, 10, true).is_none());
        let mut second = dto.clone(); second["handoff_id"] = json!("update-handoff-2");
        assert_eq!(update.on_prepare_exit(&second, 10, true).unwrap(), json!({"handoff_id":"update-handoff-2","accepted":false,"reason_code":"handoff_busy"}));
        // A refused reply (conflict) or a new epoch ends the hand-off.
        update.session(); assert!(!update.busy());
        // An unusable DTO that still names its id is declined with the reason.
        let mut broken = dto.clone(); broken["sha256"] = json!("zz");
        assert_eq!(update.on_prepare_exit(&broken, 10, true).unwrap()["reason_code"], "invalid_sha256");
    }
    #[test] fn decline_on_hash_mismatch_outside_root_or_broken_signature() {
        let dir = temp_dir("hash"); let (path, dto) = installer(&dir.0, b"installer");
        let fake = Arc::new(Fake::new());
        let update = Update::new(Box::new(fake.clone())); update.configure(dir.0.clone(), vec![], None);
        let mut wrong = dto.clone(); wrong["sha256"] = json!("0".repeat(64));
        update.on_prepare_exit(&wrong, 1, true);
        assert_eq!(wait_response(&update)["reason_code"], "hash_mismatch"); update.on_response_reply("update-handoff-1", true);
        // The known data root is another directory: the installer lies outside it.
        let other = temp_dir("other"); update.configure(other.0.clone(), vec![], None);
        update.on_prepare_exit(&dto, 1, true);
        assert_eq!(wait_response(&update)["reason_code"], "installer_outside_data"); update.on_response_reply("update-handoff-1", true);
        let fake = Arc::new(Fake { signature: Signature::Invalid, ..Fake::new() });
        let update = Update::new(Box::new(fake)); update.configure(dir.0.clone(), vec![], None);
        update.on_prepare_exit(&dto, 1, true);
        assert_eq!(wait_response(&update)["reason_code"], "signature_invalid");
        assert!(fs::metadata(&path).is_ok());
        assert!(signature_allowed(Signature::Unsigned) && signature_allowed(Signature::Valid) && !signature_allowed(Signature::Invalid));
    }
    #[test] fn install_launches_with_the_args_while_the_installer_is_locked() {
        let dir = temp_dir("launch"); let fake = Arc::new(Fake::new());
        let update = armed(&fake, &dir.0, b"installer");
        update.on_bye(&bye(0, "restored")); update.sidecar_gone(true, Some(0));
        assert_eq!(update.install(), Ok(()));
        let launches = fake.launches.lock().unwrap();
        assert_eq!(launches.len(), 1);
        assert_eq!(launches[0].1, ["/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART"]);
        assert!(update.take_failure().is_none() && !update.busy());
        drop(launches);
        // Unlocked again once CreateProcess returned.
        assert!(fs::OpenOptions::new().write(true).open(dir.0.join("updates").join(installer_name(VERSION))).is_ok());
    }
    #[test] fn install_fails_closed_and_reports_once() {
        type Case = (&'static str, Box<dyn Fn(&Arc<Fake>, &Update, &Path)>);
        let cases: Vec<Case> = vec![
            ("bye_missing", Box::new(|_, u, _| u.sidecar_gone(true, Some(0)))),
            ("sidecar_running", Box::new(|_, u, _| { u.on_bye(&bye(0, "restored")); u.sidecar_gone(false, None); })),
            ("sidecar_exit_nonzero", Box::new(|_, u, _| { u.on_bye(&bye(74, "restored")); u.sidecar_gone(true, Some(74)); })),
            ("network_not_recovered", Box::new(|_, u, _| { u.on_bye(&bye(0, "failed")); u.sidecar_gone(true, Some(0)); })),
            ("residual_processes", Box::new(|f, u, _| { f.residual_rounds.store(1000, Ordering::SeqCst); u.on_bye(&bye(0, "not_owned")); u.sidecar_gone(true, Some(0)); })),
            // Review S1/S2: a self-check that cannot run fails closed instead of passing.
            ("residual_roots_unknown", Box::new(|_, u, data| { u.configure(data.to_owned(), vec![], None); u.on_bye(&bye(0, "restored")); u.sidecar_gone(true, Some(0)); })),
            ("hash_mismatch", Box::new(|_, u, data| {
                // Replaced between the acceptance and the launch: the re-hash catches it.
                fs::write(data.join("updates").join(installer_name(VERSION)), b"tampered").unwrap();
                u.on_bye(&bye(0, "restored")); u.sidecar_gone(true, Some(0));
            })),
        ];
        for (expected, setup) in cases {
            let dir = temp_dir(expected); let fake = Arc::new(Fake::new());
            let update = armed(&fake, &dir.0, b"installer"); update.set_residual_wait(Duration::from_millis(300));
            setup(&fake, &update, &dir.0);
            assert_eq!(update.install(), Err(expected));
            assert!(fake.launches.lock().unwrap().is_empty(), "{expected}: nothing launched");
            assert_eq!(update.take_failure(), Some(json!({"receipt_ref":"receipt-1","launched":false,"reason_code":expected})));
            assert_eq!(update.take_failure(), None, "reported once");
            assert!(!update.busy());
        }
        // Process enumeration failed: residual_unknown, nothing launched.
        let dir = temp_dir("enum"); let fake = Arc::new(Fake { fail_enum: true, ..Fake::new() });
        let update = armed(&fake, &dir.0, b"installer");
        update.on_bye(&bye(0, "restored")); update.sidecar_gone(true, Some(0));
        assert_eq!(update.install(), Err("residual_unknown"));
        assert!(fake.launches.lock().unwrap().is_empty());
        assert_eq!(update.take_failure().unwrap()["reason_code"], "residual_unknown");
        // Residual processes that leave within the wait: the launch goes ahead.
        let dir = temp_dir("residual-ok"); let fake = Arc::new(Fake::new());
        let update = armed(&fake, &dir.0, b"installer");
        fake.residual_rounds.store(2, Ordering::SeqCst);
        update.on_bye(&bye(0, "restored")); update.sidecar_gone(true, Some(0));
        assert_eq!(update.install(), Ok(()));
        // CreateProcess fails: the failure is reported with launch_failed.
        let dir = temp_dir("launch-failed"); let fake = Arc::new(Fake { fail_launch: true, ..Fake::new() });
        let update = armed(&fake, &dir.0, b"installer");
        update.on_bye(&bye(0, "restored")); update.sidecar_gone(true, Some(0));
        assert_eq!(update.install(), Err("launch_failed"));
        assert_eq!(update.take_failure().unwrap()["reason_code"], "launch_failed");
        assert_eq!(fake.launches.lock().unwrap().len(), 1);
        // A refused quit abandons the accepted hand-off.
        let dir = temp_dir("refused"); let fake = Arc::new(Fake::new());
        let update = armed(&fake, &dir.0, b"installer"); update.quit_refused();
        assert!(!update.busy() && update.install() == Err("no_handoff"));
    }
    #[test] fn real_stand_in_process_starts_from_a_locked_copy() {
        // 「替身进程」: a copy of whoami.exe named like the installer; never a real installer.
        let dir = temp_dir("real");
        let system = PathBuf::from(std::env::var_os("SystemRoot").unwrap()).join("System32").join("whoami.exe");
        let (path, mut dto) = installer(&dir.0, &fs::read(&system).unwrap());
        dto["args"] = json!(["/UPN"]);
        let handoff = Handoff::parse(&dto).unwrap();
        let locked = verify(&handoff, &dir.0, &Win32Platform).expect("verified copy");
        assert!(fs::OpenOptions::new().write(true).open(&path).is_err(), "deny-write while held");
        assert!(fs::rename(&path, dir.0.join("moved.exe")).is_err(), "no rename while held");
        assert!(Win32Platform.launch(&path, &handoff.args).is_ok(), "CreateProcess works on the deny-write handle");
        drop(locked);
        let mut tampered = handoff.clone(); tampered.sha256 = "1".repeat(64);
        assert_eq!(verify(&tampered, &dir.0, &Win32Platform).err(), Some("hash_mismatch"));
        assert_eq!(Win32Platform.residual(&[dir.0.join("none")], std::process::id()), Ok(vec![]));
        assert_eq!(Win32Platform.residual(&[], std::process::id()), Err("residual_roots_unknown"), "no roots: never a vacuous pass");
    }
    #[test] fn residual_prefix_matching_is_component_wise() {
        let prefixes = vec![path_prefix(Path::new(r"\\?\C:\Program Files\Tesseract"))];
        assert!(under(r"C:\Program Files\Tesseract\bin\mihomo.exe", &prefixes));
        assert!(under(r"c:\program files\tesseract\Tesseract.exe", &prefixes));
        assert!(!under(r"C:\Program Files\Tesseract2\x.exe", &prefixes));
        assert!(!under(r"C:\Program Files\Tesseract", &prefixes));
        // Roots: every named root must resolve, else none (the install then fails closed).
        let dir = temp_dir("roots"); let (app, side, bin) = (dir.0.join("app"), dir.0.join("side"), dir.0.join("bin"));
        for d in [&app, &side, &bin] { fs::create_dir_all(d).unwrap(); }
        let (shell, sidecar) = (app.join("Tesseract.exe"), side.join("runtime.exe"));
        assert_eq!(install_roots(&shell, Some(&sidecar), Some(&bin), true, None), vec![app.clone(), side.clone()], "release: shell dir and --sidecar dir");
        assert_eq!(install_roots(&shell, None, Some(&bin), true, None), vec![app.clone(), bin.clone()], "release source mode: shell dir and bin");
        assert_eq!(install_roots(&shell, Some(&app.join("side.exe")), None, true, None), vec![app.clone()], "the same directory once");
        assert_eq!(install_roots(&shell, Some(&sidecar), Some(&bin), false, None), vec![side.clone()], "debug: no target\\debug root");
        assert_eq!(install_roots(&shell, None, Some(&bin), false, None), vec![bin.clone()]);
        assert_eq!(install_roots(&shell, None, Some(&bin), false, Some(side.clone())), vec![side.clone()], "debug override");
        assert!(install_roots(&shell, None, Some(&dir.0.join("missing")), true, None).is_empty(), "an unresolvable root: none at all");
        assert!(install_roots(&shell, None, None, false, Some(dir.0.join("missing"))).is_empty());
        assert!(install_roots(&shell, None, None, false, None).is_empty());
        let sha = sha256_hex(&mut &b"abc"[..]).unwrap();
        assert_eq!(sha, "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
    }
    /// Review S1/S5: an exitResponse that is never answered does not block the epoch.
    #[test] fn an_unanswered_exit_response_times_out() {
        let dir = temp_dir("respond"); let (_, dto) = installer(&dir.0, b"installer");
        let update = Update::new(Box::new(Arc::new(Fake::new()))); update.configure(dir.0.clone(), vec![dir.0.clone()], None);
        assert!(update.on_prepare_exit(&dto, 1, true).is_none());
        assert_eq!(wait_response(&update)["accepted"], true);
        let sent = Instant::now();
        assert!(!update.expire_response(sent + RESPONSE_WAIT - Duration::from_secs(1), 1), "still within the window");
        let mut second = dto.clone(); second["handoff_id"] = json!("update-handoff-2");
        assert_eq!(update.on_prepare_exit(&second, 1, true).unwrap()["reason_code"], "handoff_busy");
        assert!(update.expire_response(sent + RESPONSE_WAIT + Duration::from_secs(1), 1), "30 s without a reply");
        assert!(!update.busy());
        assert!(!update.on_response_reply("update-handoff-1", true), "a late recorded:true starts nothing");
        assert!(!update.accepted());
        // The hand-off's own expiry ends the wait earlier.
        assert!(update.on_prepare_exit(&second, 1, true).is_none());
        assert_eq!(wait_response(&update)["handoff_id"], "update-handoff-2");
        assert!(update.expire_response(Instant::now(), u64::MAX / 2), "expires_at_ms reached");
        // A reply naming another hand-off does not take over the waiting one.
        assert!(update.on_prepare_exit(&dto, 1, true).is_none());
        assert_eq!(wait_response(&update)["handoff_id"], "update-handoff-1");
        assert!(!update.on_response_reply("update-handoff-2", true));
        assert!(update.busy() && update.on_response_reply("update-handoff-1", true));
    }
    /// The PE certificate table (IMAGE_DIRECTORY_ENTRY_SECURITY): (file offset, size).
    fn certificate_table(bytes: &[u8]) -> Option<(usize, usize)> {
        let le32 = |at: usize| bytes.get(at..at + 4).map(|b| u32::from_le_bytes([b[0], b[1], b[2], b[3]]) as usize);
        let pe = le32(0x3c)?;
        let optional = pe + 24;
        let directories = match bytes.get(optional..optional + 2)? { [0x0b, 0x02] => optional + 112, [0x0b, 0x01] => optional + 96, _ => return None };
        let (offset, size) = (le32(directories + 4 * 8)?, le32(directories + 4 * 8 + 4)?);
        (offset > 0 && size > 16 && offset + size <= bytes.len()).then_some((offset, size))
    }
    /// An embedded-Authenticode-signed binary on this machine (the WebView2 runtime the shell needs).
    fn signed_sample() -> Option<PathBuf> {
        let base = PathBuf::from(std::env::var_os("ProgramFiles(x86)")?).join("Microsoft").join("EdgeWebView").join("Application");
        let mut versions: Vec<PathBuf> = fs::read_dir(base).ok()?.filter_map(|e| e.ok().map(|e| e.path().join("msedgewebview2.exe"))).filter(|p| p.is_file()).collect();
        versions.sort(); versions.pop()
    }
    /// Review S4: the real WinVerifyTrust branches. A copy of a signed system binary is `Valid`;
    /// one byte changed in its signed content or in its certificate table is `Invalid`; the
    /// unsigned stand-in is `Unsigned`.
    #[test] fn real_authenticode_valid_invalid_and_unsigned() {
        let Some(sample) = signed_sample() else { panic!("no embedded-signed msedgewebview2.exe found (the WebView2 runtime is required)") };
        let dir = temp_dir("sign");
        let bytes = fs::read(&sample).unwrap();
        let check = |name: &str, content: &[u8]| {
            let path = dir.0.join(name); fs::write(&path, content).unwrap();
            let file = fs::File::open(&path).unwrap();
            Win32Platform.signature(&file, &path)
        };
        assert_eq!(check("valid.exe", &bytes), Signature::Valid, "an intact signed copy");
        let (offset, size) = certificate_table(&bytes).expect("an embedded certificate table");
        // Signed content (the DOS stub text, covered by the Authenticode digest).
        let mut content = bytes.clone(); content[0x4e] ^= 0x01;
        assert_eq!(check("content.exe", &content), Signature::Invalid, "a changed signed byte");
        // The certificate table: a byte inside the PKCS#7 signature blob.
        let mut table = bytes.clone(); table[offset + size / 2] ^= 0x01;
        assert_eq!(check("table.exe", &table), Signature::Invalid, "a changed certificate-table byte");
        let unsigned = PathBuf::from(std::env::var_os("SystemRoot").unwrap()).join("System32").join("whoami.exe");
        assert_eq!(check("unsigned.exe", &fs::read(unsigned).unwrap()), Signature::Unsigned, "catalog-signed only: no embedded signature");
    }
}
