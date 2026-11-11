//! T-P3-06 page host commands (PROTOCOL §10 「页面宿主命令」, §13 「保存授权与 host」; plan §7.4).
//!
//! `host.saveFile`, `host.openFolder`, `host.openUrl` and `host.reveal` run only in the shell and
//! are never forwarded to the sidecar. The page calls them through the same `rt` entry as runtime
//! commands; `rt` answers `{queued:true,id}` and the shell-made `res{id,req_id,owner,cmd,ok,...}`
//! follows on the page Channel (stamped with the current epoch).
//!
//! Save grants: the native dialog approves a target; the shell opens it for writing and duplicates
//! that handle into the current sidecar (`DuplicateHandle`); `shell.saveGrant{ref, purpose,
//! expires_at_ms, format, handle_id, for_owner}` carries the sidecar-side handle value as
//! `handle_id` and the page owner that asked for `host.saveFile` as `for_owner` (T-P2-31b; the
//! grant binds to it), on a request the shell sends as itself (`owner:"shell"`, like its other
//! requests, so a page owner revoke never swallows the reply the shell needs). Only
//! after `registered:true` does the page get the opaque `ref`; it never sees a path. The shell keeps
//! `ref → path` for `host.reveal`, which accepts a ref only after the export job that used it ended
//! `done` with that `ref` as its result.
//!
//! The approved target is opened without truncation (OPEN_ALWAYS): an existing file the user chose
//! to overwrite keeps its content until the writer (the sidecar's SaveTargetPort adapter) truncates
//! at its own write position, and a file this grant created is deleted again on any failure — by
//! delete-on-close through the shell's own handle, so a duplicate the sidecar may already hold
//! (a reply that came too late) cannot keep an empty file behind (review S5/S2).
use crate::shell_log;
use serde_json::{json, Value};
use std::{collections::{HashMap, VecDeque}, io, path::{Path, PathBuf}, sync::{atomic::{AtomicBool, AtomicUsize, Ordering},
    mpsc, Arc, Mutex, MutexGuard}, time::{Duration, SystemTime, UNIX_EPOCH}};

pub const SAVE_FILE: &str = "host.saveFile";
pub const OPEN_FOLDER: &str = "host.openFolder";
pub const OPEN_URL: &str = "host.openUrl";
pub const REVEAL: &str = "host.reveal";
pub const COMMANDS: [&str; 4] = [SAVE_FILE, OPEN_FOLDER, OPEN_URL, REVEAL];
pub const SAVE_GRANT: &str = "shell.saveGrant";
/// PROTOCOL §13 item 5: https only, default port, exact host names. Adding one changes the contract.
pub const URL_HOSTS: [&str; 5] = ["github.com", "docs.github.com", "t.me", "ntfy.sh", "pushover.net"];
/// PROTOCOL §13 item 3: a save ref expires after 5 minutes by default.
pub const GRANT_TTL_MS: u64 = 5 * 60 * 1000;
const GRANT_REPLY_WAIT: Duration = Duration::from_secs(10);
const INFLIGHT: usize = 8;
const OUTBOX: usize = 16;
const SAVES: usize = 64;

pub fn is_host(cmd: &str) -> bool { cmd.starts_with("host.") }
fn now_ms() -> u64 { SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_millis() as u64 }

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Fail { pub code: &'static str, pub reason: &'static str }
const fn fail(code: &'static str, reason: &'static str) -> Fail { Fail { code, reason } }

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Purpose { Bundle, Filtered }
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Format { Zip, Json, Text, Csv }
impl Purpose { pub fn wire(self) -> &'static str { match self { Self::Bundle => "bundle", Self::Filtered => "filtered" } } }
impl Format {
    pub fn wire(self) -> &'static str { match self { Self::Zip => "zip", Self::Json => "json", Self::Text => "text", Self::Csv => "csv" } }
    pub fn extension(self) -> &'static str { match self { Self::Zip => "zip", Self::Json => "json", Self::Text => "txt", Self::Csv => "csv" } }
}
fn keys(args: &Value, expected: &[&str]) -> Result<(), Fail> {
    let object = args.as_object().ok_or(fail("invalid_type", "args_not_object"))?;
    for key in expected { if !object.contains_key(*key) { return Err(fail("missing_field", "missing_field")); } }
    if object.len() != expected.len() { return Err(fail("invalid_type", "unknown_field")); }
    Ok(())
}
/// `{purpose, format}`: bundle is zip only, filtered is json/text/csv only (PROTOCOL §13 item 4).
pub fn save_args(args: &Value) -> Result<(Purpose, Format), Fail> {
    keys(args, &["purpose", "format"])?;
    let purpose = match args["purpose"].as_str() { Some("bundle") => Purpose::Bundle, Some("filtered") => Purpose::Filtered, _ => return Err(fail("invalid_enum", "purpose")) };
    let format = match args["format"].as_str() { Some("zip") => Format::Zip, Some("json") => Format::Json, Some("text") => Format::Text, Some("csv") => Format::Csv, _ => return Err(fail("invalid_enum", "format")) };
    match (purpose, format) {
        (Purpose::Bundle, Format::Zip) | (Purpose::Filtered, Format::Json | Format::Text | Format::Csv) => Ok((purpose, format)),
        _ => Err(fail("validation", "purpose_format")),
    }
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Folder { Config, Data, Logs, Exports }
/// `{kind}` from the closed set only; anything else (a path, `..`, other keys) is `not_allowed`.
pub fn folder_args(args: &Value) -> Result<Folder, Fail> {
    keys(args, &["kind"]).map_err(|_| fail("not_allowed", "kind"))?;
    match args["kind"].as_str() {
        Some("config") => Ok(Folder::Config), Some("data") => Ok(Folder::Data), Some("logs") => Ok(Folder::Logs), Some("exports") => Ok(Folder::Exports),
        _ => Err(fail("not_allowed", "kind")),
    }
}
/// `{url}`: https, no userinfo, default port, host exactly one of [`URL_HOSTS`], no control or
/// whitespace characters. Returns the parsed URL and its host (shown and logged, never the URL).
pub fn url_args(args: &Value) -> Result<(String, &'static str), Fail> {
    keys(args, &["url"]).map_err(|_| fail("not_allowed", "url_args"))?;
    let raw = args["url"].as_str().ok_or(fail("not_allowed", "url_type"))?;
    if raw.is_empty() || raw.len() > 2048 || raw.chars().any(|c| c.is_control() || c.is_whitespace()) { return Err(fail("not_allowed", "url_text")); }
    let url: tauri::Url = raw.parse().map_err(|_| fail("not_allowed", "url_parse"))?;
    if url.scheme() != "https" { return Err(fail("not_allowed", "scheme")); }
    if !url.username().is_empty() || url.password().is_some() { return Err(fail("not_allowed", "userinfo")); }
    if url.port().is_some_and(|p| p != 443) { return Err(fail("not_allowed", "port")); }
    let host = url.host_str().and_then(|h| URL_HOSTS.iter().copied().find(|allowed| *allowed == h)).ok_or(fail("not_allowed", "host"))?;
    Ok((url.to_string(), host))
}
pub fn ref_args(args: &Value) -> Result<String, Fail> {
    keys(args, &["ref"]).map_err(|_| fail("not_allowed", "ref"))?;
    args["ref"].as_str().filter(|r| !r.is_empty() && r.len() <= 128).map(String::from).ok_or(fail("not_allowed", "ref"))
}
/// Local time parts (year, month, day, hour, minute, second).
pub type Stamp = (u16, u16, u16, u16, u16, u16);
/// `tesseract-diagnostics-YYYYmmdd-HHMMSS.zip` (diagnostic_bundle.default_bundle_name); filtered
/// exports `tesseract-logs-YYYYmmdd-HHMMSS.<json|txt|csv>`.
pub fn default_name(purpose: Purpose, format: Format, t: Stamp) -> String {
    let stem = match purpose { Purpose::Bundle => "tesseract-diagnostics", Purpose::Filtered => "tesseract-logs" };
    format!("{stem}-{:04}{:02}{:02}-{:02}{:02}{:02}.{}", t.0, t.1, t.2, t.3, t.4, t.5, format.extension())
}
/// The approved path must carry the format's extension (the dialog enforces it; checked again).
pub fn extension_matches(path: &Path, format: Format) -> bool {
    path.extension().and_then(|e| e.to_str()).is_some_and(|e| e.eq_ignore_ascii_case(format.extension()))
}

/// What the native save dialog is asked for.
#[derive(Clone, Debug, PartialEq)]
pub struct DialogSpec { pub purpose: Purpose, pub format: Format, pub file_name: String, pub folder: Option<PathBuf>, pub locale: &'static str }
/// An opened, approved target (not truncated). `raw` is the shell's handle; `created` says the file
/// did not exist before.
pub struct Target { pub raw: isize, pub created: bool, pub owned: Option<std::os::windows::io::OwnedHandle> }

/// Native effects (hostwin.rs); tests use a stand-in.
pub trait Desktop: Send + Sync {
    /// Some(path) when the user approved a target (overwrite already confirmed), None on cancel.
    fn save_dialog(&self, owner: isize, spec: &DialogSpec) -> io::Result<Option<PathBuf>>;
    /// Opens (creating if needed, never truncating) the approved file for writing (a disk file,
    /// not a device).
    fn create_target(&self, path: &Path) -> io::Result<Target>;
    /// Closes the shell's handle; a file this grant created is deleted once every handle to it
    /// (also a duplicate in the sidecar) is closed. An existing file is left as it was.
    fn discard(&self, target: Target, path: &Path);
    /// The native 「将在浏览器中打开 <host>」 question, owned by the main window; true = open.
    fn confirm_url(&self, owner: isize, host: &str, locale: &'static str) -> bool;
    fn open_url(&self, url: &str) -> io::Result<()>;
    fn open_folder(&self, dir: &Path) -> io::Result<()>;
    /// `explorer /select,<file>`.
    fn reveal(&self, file: &Path) -> io::Result<()>;
    /// Desktop known folder (the default folder of the bundle's save dialog).
    fn desktop_dir(&self) -> Option<PathBuf>;
    fn local_time(&self) -> Stamp;
    fn random_ref(&self) -> String;
}
/// The live sidecar process (main.rs `ChildSlot`).
pub trait SidecarPort: Send + Sync {
    fn pid(&self) -> Option<u32>;
    /// DuplicateHandle(shell handle → sidecar), same access, only into the live sidecar `pid`
    /// (checked under the same lock); returns the sidecar-side value.
    fn duplicate(&self, raw: isize, pid: u32) -> io::Result<u64>;
    /// Closes a duplicated handle inside the sidecar (DUPLICATE_CLOSE_SOURCE) when it was never
    /// sent — only while that same process (`pid`) is still the live sidecar, never in a successor.
    fn close_remote(&self, value: u64, pid: u32);
}

/// A shell request that needs the sidecar's reply (`shell.saveGrant`). The worker sends it on
/// the shell lane only for the session `epoch`; `Err` codes before sending say it never left.
pub struct Call { pub cmd: &'static str, pub args: Value, pub owner: String, pub epoch: String, pub reply: mpsc::Sender<Result<Value, &'static str>> }
#[derive(Default)]
pub struct Outbox(Mutex<VecDeque<Call>>);
impl Outbox {
    pub fn push(&self, call: Call) -> Result<(), Call> {
        let mut queue = self.0.lock().unwrap_or_else(|e| e.into_inner());
        if queue.len() >= OUTBOX { return Err(call); }
        queue.push_back(call); Ok(())
    }
    pub fn drain(&self) -> Vec<Call> { self.0.lock().unwrap_or_else(|e| e.into_inner()).drain(..).collect() }
}
pub fn call_frame(id: u64, call: &Call) -> Value {
    json!({"t":"req","id":id,"cmd":call.cmd,"args":call.args,"from":"shell","req_id":format!("shell-host-{id}"),"owner":call.owner})
}

#[derive(Clone, Debug, PartialEq)]
struct Grant { path: PathBuf, epoch: String, owner: String, exported: bool }
#[derive(Clone, Debug, Default)]
struct Session { epoch: String, pid: u32, can_grant: bool, live: bool }
#[derive(Default)]
struct State {
    config: PathBuf, data: PathBuf, log: Option<PathBuf>, window: isize, session: Session,
    saves: HashMap<String, Grant>, order: VecDeque<String>,
}

/// One page host request (`rt` with a `host.*` cmd).
#[derive(Clone, Debug)]
pub struct Request { pub id: u64, pub req_id: String, pub owner: String, pub cmd: String, pub args: Value }

pub struct Host {
    desktop: Box<dyn Desktop>,
    pub outbox: Outbox,
    state: Mutex<State>,
    publish: Mutex<Option<Box<dyn Fn(Value) + Send + Sync>>>,
    sidecar: Mutex<Option<Arc<dyn SidecarPort>>>,
    locale: Mutex<Option<Box<dyn Fn() -> &'static str + Send + Sync>>>,
    dialog_open: AtomicBool,
    inflight: AtomicUsize,
}
impl Host {
    pub fn new(desktop: Box<dyn Desktop>) -> Arc<Self> {
        Arc::new(Self { desktop, outbox: Outbox::default(), state: Mutex::new(State::default()), publish: Mutex::new(None),
            sidecar: Mutex::new(None), locale: Mutex::new(None), dialog_open: AtomicBool::new(false), inflight: AtomicUsize::new(0) })
    }
    fn state(&self) -> MutexGuard<'_, State> { self.state.lock().unwrap_or_else(|e| e.into_inner()) }
    /// Scope directories, the page publisher, the sidecar handle slot and the native locale.
    pub fn attach(&self, config: PathBuf, data: PathBuf, publish: Box<dyn Fn(Value) + Send + Sync>, sidecar: Arc<dyn SidecarPort>,
        locale: Box<dyn Fn() -> &'static str + Send + Sync>) {
        { let mut s = self.state(); s.log = Some(data.clone()); s.config = config; s.data = data; }
        *self.publish.lock().unwrap_or_else(|e| e.into_inner()) = Some(publish);
        *self.sidecar.lock().unwrap_or_else(|e| e.into_inner()) = Some(sidecar);
        *self.locale.lock().unwrap_or_else(|e| e.into_inner()) = Some(locale);
    }
    pub fn set_window(&self, hwnd: isize) { self.state().window = hwnd; }
    /// A sidecar session is live: refs of other epochs become invalid (PROTOCOL §13 item 4).
    pub fn session(&self, epoch: &str, pid: u32, can_grant: bool) {
        let mut s = self.state();
        s.session = Session { epoch: epoch.to_owned(), pid, can_grant, live: true };
        s.saves.retain(|_, g| g.epoch == epoch); let saves = &s.saves.keys().cloned().collect::<Vec<_>>();
        s.order.retain(|r| saves.contains(r));
    }
    pub fn session_gone(&self) { self.state().session.live = false; }
    fn log(&self, code: &str, fields: &[(&str, &str)]) {
        let dir = self.state().log.clone();
        if let Some(dir) = dir { shell_log::fields(&dir, code, fields); }
    }
    fn locale(&self) -> &'static str { self.locale.lock().unwrap_or_else(|e| e.into_inner()).as_ref().map_or("en_US", |l| l()) }
    fn emit(&self, frame: Value) { if let Some(publish) = self.publish.lock().unwrap_or_else(|e| e.into_inner()).as_ref() { publish(frame); } }

    /// `rt` entry for `host.*`: validated envelope, bounded concurrency, the work on its own thread.
    /// `owner` may be absent (PROTOCOL §6: `invoke('rt', {req_id,cmd,args,owner?})`) except for the
    /// two commands whose result is bound to it: `saveFile` (the grant) and `reveal` (review S4).
    pub fn submit(self: &Arc<Self>, request: Request) -> Result<(), String> {
        if !COMMANDS.contains(&request.cmd.as_str()) { return Err("not_allowed".into()); }
        if request.req_id.is_empty() || request.req_id.len() > 128 || request.owner.len() > 128 || !request.args.is_object() {
            return Err("invalid_type".into());
        }
        if request.owner.is_empty() && matches!(request.cmd.as_str(), SAVE_FILE | REVEAL) { return Err("missing_field".into()); }
        if self.inflight.fetch_add(1, Ordering::AcqRel) >= INFLIGHT { self.inflight.fetch_sub(1, Ordering::AcqRel); return Err("job_capacity".into()); }
        let this = self.clone();
        std::thread::spawn(move || {
            let result = this.run(&request);
            this.inflight.fetch_sub(1, Ordering::AcqRel);
            let outcome = match &result { Ok(_) => "ok", Err(f) => f.code };
            let reason = match &result { Ok(data) if data["cancelled"] == true || data["opened"] == false => "cancelled", Ok(_) => "-", Err(f) => f.reason };
            this.log("host_request", &[("cmd", &request.cmd), ("result", outcome), ("reason", reason)]);
            this.emit(response(&request, result));
        });
        Ok(())
    }
    /// The command itself (also called directly by tests).
    pub fn run(&self, request: &Request) -> Result<Value, Fail> {
        match request.cmd.as_str() {
            SAVE_FILE => self.save_file(request),
            OPEN_FOLDER => self.open_folder(&request.args),
            OPEN_URL => self.open_url(&request.args),
            REVEAL => self.reveal(request),
            _ => Err(fail("not_allowed", "cmd")),
        }
    }
    /// The scope directory of `kind` (T-P3-06 §2: 「各自对应作用域下的目录」); nothing outside the
    /// scope's config and data directories, whatever was saved where (review S3).
    fn folder(&self, kind: Folder) -> Result<PathBuf, Fail> {
        let (config, data) = { let s = self.state(); (s.config.clone(), s.data.clone()) };
        if config.as_os_str().is_empty() || data.as_os_str().is_empty() { return Err(fail("io", "scope_unknown")); }
        Ok(match kind { Folder::Config => config, Folder::Data => data, Folder::Logs => data.join("logs"), Folder::Exports => data.join("exports") })
    }
    fn open_folder(&self, args: &Value) -> Result<Value, Fail> {
        let kind = folder_args(args)?;
        let dir = self.folder(kind)?;
        if matches!(kind, Folder::Logs | Folder::Exports) { let _ = std::fs::create_dir_all(&dir); }
        if !dir.is_dir() { return Err(fail("io", "folder_missing")); }
        self.desktop.open_folder(&dir).map_err(|_| fail("io", "open_failed"))?;
        Ok(json!({"opened":true}))
    }
    fn open_url(&self, args: &Value) -> Result<Value, Fail> {
        let (url, host) = url_args(args).inspect_err(|f| self.log("host_open_url_refused", &[("reason", f.reason)]))?;
        // One native host dialog at a time (the save dialog included): a burst of openUrl from the
        // page never stacks confirmation boxes (review S6/S7).
        let Some(_open) = DialogGuard::take(&self.dialog_open) else { return Err(fail("conflict", "dialog_open")) };
        let window = self.state().window;
        if !self.desktop.confirm_url(window, host, self.locale()) { self.log("host_open_url", &[("host", host), ("result", "declined")]); return Ok(json!({"opened":false})); }
        self.desktop.open_url(&url).map_err(|_| fail("io", "open_failed"))?;
        self.log("host_open_url", &[("host", host), ("result", "opened")]);
        Ok(json!({"opened":true}))
    }
    fn reveal(&self, request: &Request) -> Result<Value, Fail> {
        let reference = ref_args(&request.args)?;
        let path = {
            let s = self.state();
            let grant = s.saves.get(&reference).filter(|g| g.epoch == s.session.epoch && g.owner == request.owner && g.exported).ok_or(fail("not_allowed", "unknown_ref"))?;
            grant.path.clone()
        };
        if !path.is_file() { return Err(fail("io", "file_missing")); }
        self.desktop.reveal(&path).map_err(|_| fail("io", "reveal_failed"))?;
        Ok(json!({"revealed":true}))
    }
    /// A `jobs` edge (or a batch holding some): a `done` logs.export* job whose result names a
    /// registered ref of the same owner makes that ref revealable.
    pub fn observe(&self, frame: &Value) {
        let mut items: Vec<&Value> = Vec::new();
        match frame["t"].as_str() {
            Some("evt") => items.push(frame),
            Some("batch") => if let Some(list) = frame["items"].as_array() { items.extend(list.iter()); },
            _ => return,
        }
        for item in items.into_iter().filter(|i| i["topic"] == "jobs") {
            let data = &item["data"];
            if data["state"] != "done" || !matches!(data["name"].as_str(), Some("logs.exportBundle" | "logs.exportFiltered")) { continue; }
            let Some(reference) = data["result"]["ref"].as_str() else { continue };
            let mut s = self.state();
            let epoch = s.session.epoch.clone();
            if let Some(grant) = s.saves.get_mut(reference) {
                // The edge must name the owner the grant was registered for (review S7).
                if grant.epoch == epoch && data["owner"].as_str() == Some(grant.owner.as_str()) { grant.exported = true; }
            }
        }
    }
    fn save_file(&self, request: &Request) -> Result<Value, Fail> {
        let (purpose, format) = save_args(&request.args)?;
        let session = self.state().session.clone();
        if !session.live { return Err(fail("runtime_unavailable", "sidecar_not_live")); }
        if !session.can_grant { return Err(fail("runtime_unavailable", "grant_unsupported")); }
        let Some(_open) = DialogGuard::take(&self.dialog_open) else { return Err(fail("conflict", "dialog_open")) };
        let folder = match purpose { Purpose::Bundle => self.desktop.desktop_dir(), Purpose::Filtered => Some(self.state().data.join("logs")).filter(|d| d.is_dir()) };
        let spec = DialogSpec { purpose, format, file_name: default_name(purpose, format, self.desktop.local_time()), folder, locale: self.locale() };
        let window = self.state().window;
        let Some(path) = self.desktop.save_dialog(window, &spec).map_err(|_| fail("io", "dialog_failed"))? else {
            return Ok(json!({"cancelled":true,"ref":null}));
        };
        if !extension_matches(&path, format) { return Err(fail("io", "extension_mismatch")); }
        let target = self.desktop.create_target(&path).map_err(|_| fail("io", "target_open_failed"))?;
        let sidecar = self.sidecar.lock().unwrap_or_else(|e| e.into_inner()).clone();
        let Some(sidecar) = sidecar.filter(|s| s.pid() == Some(session.pid)) else { self.desktop.discard(target, &path); return Err(fail("runtime_gone", "sidecar_changed")); };
        let handle_id = match sidecar.duplicate(target.raw, session.pid) { Ok(h) => h, Err(_) => { self.desktop.discard(target, &path); return Err(fail("io", "duplicate_failed")); } };
        let reference = self.desktop.random_ref();
        let (tx, rx) = mpsc::channel();
        let call = Call { cmd: SAVE_GRANT, owner: "shell".into(), epoch: session.epoch.clone(), reply: tx,
            args: json!({"ref":reference,"purpose":purpose.wire(),"expires_at_ms":now_ms()+GRANT_TTL_MS,"format":format.wire(),"handle_id":handle_id,
                "for_owner":request.owner}) };
        if self.outbox.push(call).is_err() { sidecar.close_remote(handle_id, session.pid); self.desktop.discard(target, &path); return Err(fail("job_capacity", "outbox_full")); }
        let reply = rx.recv_timeout(GRANT_REPLY_WAIT);
        let registered = matches!(&reply, Ok(Ok(frame)) if frame["t"] == "res" && frame["ok"] == true && frame["data"]["registered"] == true);
        if !registered {
            // Never sent because the shell lane was full: the duplicate is still ours to close. Not
            // sent because the session moved on (runtime_gone): that sidecar is quitting or gone and
            // its exit closes it. Sent and refused: the sidecar released it (bridge_exports
            // SaveGrantTable). Timed out: the sidecar owns whatever it received (expiry or exit).
            if let Ok(Err("job_capacity")) = reply { sidecar.close_remote(handle_id, session.pid); }
            // A grant registered after the timeout is never handed to the page: the sidecar
            // releases it at its expiry (or exit); a created file goes with its last handle.
            if matches!(reply, Err(mpsc::RecvTimeoutError::Timeout)) { self.log("host_save_grant_timeout", &[("purpose", purpose.wire())]); }
            self.desktop.discard(target, &path);
            return Err(match reply {
                Ok(Err(code)) => fail(code, "grant_not_sent"),
                Ok(Ok(_)) => fail("io", "grant_refused"),
                Err(mpsc::RecvTimeoutError::Timeout) => fail("io", "grant_timeout"),
                Err(mpsc::RecvTimeoutError::Disconnected) => fail("runtime_gone", "session_ended"),
            });
        }
        let mut s = self.state();
        // The epoch may have moved on while the grant was in flight (review S3): the page would get
        // a ref the hub stamps with the new epoch. Drop it; the old sidecar's exit releases its copy.
        if !s.session.live || s.session.epoch != session.epoch {
            drop(s); self.desktop.discard(target, &path);
            self.log("host_save_grant_stale_epoch", &[("purpose", purpose.wire())]);
            return Err(fail("runtime_gone", "epoch_changed"));
        }
        while s.saves.len() >= SAVES { if let Some(old) = s.order.pop_front() { s.saves.remove(&old); } else { break; } }
        s.saves.insert(reference.clone(), Grant { path, epoch: session.epoch, owner: request.owner.clone(), exported: false });
        s.order.push_back(reference.clone());
        drop(s);
        drop(target); // The sidecar's duplicate is the only writer from here on.
        Ok(json!({"cancelled":false,"ref":reference}))
    }
}
/// The one-native-dialog-at-a-time flag, released on drop.
struct DialogGuard<'a>(&'a AtomicBool);
impl<'a> DialogGuard<'a> {
    // Never `then_some(Self(..))`: the eagerly built guard would be dropped on the busy path and
    // release the flag its holder still owns.
    fn take(flag: &'a AtomicBool) -> Option<Self> { if flag.swap(true, Ordering::AcqRel) { None } else { Some(Self(flag)) } }
}
impl Drop for DialogGuard<'_> { fn drop(&mut self) { self.0.store(false, Ordering::Release); } }
/// The shell-made reply on the page Channel (PROTOCOL §6 `res`; the hub adds the epoch). An
/// absent page owner stays absent.
pub fn response(request: &Request, result: Result<Value, Fail>) -> Value {
    let now = now_ms();
    let mut frame = json!({"t":"res","id":request.id,"req_id":request.req_id,"cmd":request.cmd,"produced_at_ms":now,"sent_at_ms":now});
    if !request.owner.is_empty() { frame["owner"] = json!(request.owner); }
    match result {
        Ok(data) => { frame["ok"] = json!(true); frame["data"] = data; },
        Err(f) => { frame["ok"] = json!(false); frame["error"] = json!({"code":f.code,"message_key":format!("errors.host.{}",f.code),"params":{},"reason_code":f.reason}); },
    }
    frame
}

#[cfg(test)] pub mod tests {
    use super::*;
    #[derive(Default)]
    pub struct FakeDesktop {
        pub answer: Mutex<Option<PathBuf>>, pub confirm: AtomicBool, pub calls: Mutex<Vec<String>>, pub specs: Mutex<Vec<DialogSpec>>,
        pub discarded: Mutex<Vec<PathBuf>>, pub fail_create: AtomicBool,
        /// confirm_url blocks until this is cleared (to hold a dialog open in a test).
        pub hold_confirm: AtomicBool,
        /// Real target files (hostwin), so the worker test duplicates a real handle into the fixture.
        pub real: AtomicBool,
    }
    impl Desktop for Arc<FakeDesktop> {
        fn save_dialog(&self, _: isize, spec: &DialogSpec) -> io::Result<Option<PathBuf>> { self.specs.lock().unwrap().push(spec.clone()); Ok(self.answer.lock().unwrap().clone()) }
        fn create_target(&self, path: &Path) -> io::Result<Target> {
            if self.fail_create.load(Ordering::SeqCst) { return Err(io::Error::other("denied")); }
            if self.real.load(Ordering::SeqCst) { return crate::hostwin::Win32Desktop.create_target(path); }
            self.calls.lock().unwrap().push(format!("create {}", path.display())); Ok(Target { raw: 77, created: true, owned: None })
        }
        fn discard(&self, target: Target, path: &Path) {
            self.discarded.lock().unwrap().push(path.to_owned());
            if self.real.load(Ordering::SeqCst) { crate::hostwin::Win32Desktop.discard(target, path); }
        }
        fn confirm_url(&self, owner: isize, host: &str, _: &'static str) -> bool {
            self.calls.lock().unwrap().push(format!("confirm {host} owner={owner}"));
            while self.hold_confirm.load(Ordering::SeqCst) { std::thread::sleep(Duration::from_millis(5)); }
            self.confirm.load(Ordering::SeqCst)
        }
        fn open_url(&self, url: &str) -> io::Result<()> { self.calls.lock().unwrap().push(format!("url {url}")); Ok(()) }
        fn open_folder(&self, dir: &Path) -> io::Result<()> { self.calls.lock().unwrap().push(format!("folder {}", dir.display())); Ok(()) }
        fn reveal(&self, file: &Path) -> io::Result<()> { self.calls.lock().unwrap().push(format!("reveal {}", file.display())); Ok(()) }
        fn desktop_dir(&self) -> Option<PathBuf> { Some(std::env::temp_dir()) }
        fn local_time(&self) -> Stamp { (2026, 10, 6, 9, 5, 7) }
        fn random_ref(&self) -> String { "save-0123456789abcdef".into() }
    }
    #[derive(Default)]
    pub struct FakeSidecar { pub pid: u32, pub closed: Mutex<Vec<u64>> }
    impl SidecarPort for FakeSidecar {
        fn pid(&self) -> Option<u32> { Some(self.pid) }
        fn duplicate(&self, raw: isize, pid: u32) -> io::Result<u64> { assert_eq!(pid, self.pid); Ok(raw as u64 + 1000) }
        fn close_remote(&self, value: u64, pid: u32) { assert_eq!(pid, self.pid); self.closed.lock().unwrap().push(value); }
    }
    pub struct Rig { pub host: Arc<Host>, pub desktop: Arc<FakeDesktop>, pub sidecar: Arc<FakeSidecar>, pub frames: Arc<Mutex<Vec<Value>>>, pub dir: crate::update::tests::Dir }
    pub fn rig() -> Rig {
        let dir = crate::update::tests::temp_dir("host");
        let (config, data) = (dir.0.join("config"), dir.0.join("data"));
        std::fs::create_dir_all(&config).unwrap(); std::fs::create_dir_all(data.join("logs")).unwrap();
        let desktop = Arc::new(FakeDesktop::default());
        let host = Host::new(Box::new(desktop.clone()));
        let sidecar = Arc::new(FakeSidecar { pid: 42, ..Default::default() });
        let frames = Arc::new(Mutex::new(Vec::new())); let sink = frames.clone();
        host.attach(config, data, Box::new(move |f| sink.lock().unwrap().push(f)), sidecar.clone(), Box::new(|| "zh_CN"));
        host.session("one", 42, true);
        host.set_window(0x5150);
        Rig { host, desktop, sidecar, frames, dir }
    }
    pub fn request(cmd: &str, args: Value) -> Request { Request { id: 9, req_id: "r1".into(), owner: "page-1".into(), cmd: cmd.into(), args } }
    /// Answers the next outbox call like the sidecar would (or not at all).
    fn answer(host: &Arc<Host>, reply: Option<Result<Value, &'static str>>) -> std::thread::JoinHandle<Vec<Value>> {
        let host = host.clone();
        std::thread::spawn(move || {
            let deadline = std::time::Instant::now() + Duration::from_secs(5);
            loop {
                let calls = host.outbox.drain();
                if let Some(call) = calls.into_iter().next() {
                    let seen = vec![call_frame(5, &call), json!(call.epoch)];
                    if let Some(reply) = reply { let _ = call.reply.send(reply); } else { std::mem::forget(call.reply); }
                    return seen;
                }
                assert!(std::time::Instant::now() < deadline); std::thread::sleep(Duration::from_millis(5));
            }
        })
    }

    #[test] fn open_folder_accepts_only_the_four_kinds() {
        let r = rig();
        for kind in ["config", "data", "logs", "exports"] { assert_eq!(r.host.run(&request(OPEN_FOLDER, json!({"kind":kind}))), Ok(json!({"opened":true})), "{kind}"); }
        let calls = r.desktop.calls.lock().unwrap().clone();
        let data = r.dir.0.join("data");
        assert_eq!(calls, [format!("folder {}", r.dir.0.join("config").display()), format!("folder {}", data.display()),
            format!("folder {}", data.join("logs").display()), format!("folder {}", data.join("exports").display())], "each kind is its scope directory");
        assert!(data.join("exports").is_dir(), "exports is created on demand inside the data directory");
        for args in [json!({"kind":"C:\\Windows"}), json!({"kind":"../logs"}), json!({"kind":"logs","path":"C:\\"}), json!({"path":"C:\\"}), json!({"kind":null}), json!({})] {
            assert_eq!(r.host.run(&request(OPEN_FOLDER, args.clone())).unwrap_err().code, "not_allowed", "{args}");
        }
        assert_eq!(r.desktop.calls.lock().unwrap().len(), 4, "nothing opened for a refused kind");
    }
    #[test] fn open_url_whitelist_and_confirmation() {
        let r = rig();
        for url in ["http://github.com/x", "javascript:alert(1)", "file:///C:/x", "https://evil.com/", "https://github.com.evil.com/",
            "https://evilgithub.com/", "https://api.github.com/", concat!("https://user", "@github.com/"), "https://github.com:8443/", "https://github.com/ x",
            "https://github.com/\u{7}", "https://t.me./BotFather", "https://127.0.0.1/", "ftp://t.me/x", "HTTPS://", ""] {
            let result = r.host.run(&request(OPEN_URL, json!({"url":url})));
            assert_eq!(result.unwrap_err().code, "not_allowed", "{url}");
        }
        assert_eq!(r.host.run(&request(OPEN_URL, json!({"url":"https://github.com","x":1}))).unwrap_err().code, "not_allowed");
        assert!(r.desktop.calls.lock().unwrap().is_empty(), "no dialog for a refused link");
        // Allowed host: the user declines, then accepts.
        assert_eq!(r.host.run(&request(OPEN_URL, json!({"url":"https://t.me/BotFather"}))), Ok(json!({"opened":false})));
        r.desktop.confirm.store(true, Ordering::SeqCst);
        for url in ["https://github.com/definitlynotme/Tessract/releases", "https://GitHub.com:443/x", "https://docs.github.com/a", "https://ntfy.sh/app", "https://pushover.net/"] {
            assert_eq!(r.host.run(&request(OPEN_URL, json!({"url":url}))), Ok(json!({"opened":true})), "{url}");
        }
        let calls = r.desktop.calls.lock().unwrap().clone();
        assert_eq!(calls[0], "confirm t.me owner=20816", "only the host is shown; the main window owns the box");
        assert_eq!(calls[1], "confirm github.com owner=20816"); assert_eq!(calls[2], "url https://github.com/definitlynotme/Tessract/releases");
        assert_eq!(calls[4], "url https://github.com/x", "default port and case normalised");
        // Review S6/S7: one native box at a time; a second openUrl (or a save dialog) meanwhile is a conflict.
        r.desktop.hold_confirm.store(true, Ordering::SeqCst); r.desktop.calls.lock().unwrap().clear();
        let (host, first) = (r.host.clone(), std::thread::spawn({ let host = r.host.clone(); move || host.run(&request(OPEN_URL, json!({"url":"https://ntfy.sh/"}))) }));
        let deadline = std::time::Instant::now() + Duration::from_secs(5);
        while !r.desktop.calls.lock().unwrap().iter().any(|c| c == "confirm ntfy.sh owner=20816") { assert!(std::time::Instant::now() < deadline); std::thread::sleep(Duration::from_millis(5)); }
        assert_eq!(host.run(&request(OPEN_URL, json!({"url":"https://t.me/x"}))).unwrap_err(), fail("conflict", "dialog_open"));
        assert_eq!(host.run(&request(SAVE_FILE, json!({"purpose":"bundle","format":"zip"}))).unwrap_err(), fail("conflict", "dialog_open"));
        r.desktop.hold_confirm.store(false, Ordering::SeqCst);
        assert_eq!(first.join().unwrap(), Ok(json!({"opened":true})));
        assert_eq!(host.run(&request(OPEN_URL, json!({"url":"https://t.me/x"}))), Ok(json!({"opened":true})), "released afterwards");
    }
    #[test] fn save_file_cancel_has_no_side_effect() {
        let r = rig();
        assert_eq!(r.host.run(&request(SAVE_FILE, json!({"purpose":"bundle","format":"zip"}))), Ok(json!({"cancelled":true,"ref":null})));
        let spec = r.desktop.specs.lock().unwrap()[0].clone();
        assert_eq!(spec.file_name, "tesseract-diagnostics-20261006-090507.zip");
        assert_eq!((spec.format, spec.locale), (Format::Zip, "zh_CN"));
        assert!(r.desktop.calls.lock().unwrap().is_empty() && r.host.outbox.drain().is_empty(), "no file, no grant");
        for (args, code) in [(json!({"purpose":"bundle","format":"json"}), "validation"), (json!({"purpose":"filtered","format":"zip"}), "validation"),
            (json!({"purpose":"x","format":"zip"}), "invalid_enum"), (json!({"purpose":"bundle"}), "missing_field"), (json!({"purpose":"bundle","format":"zip","path":"C:\\x"}), "invalid_type")] {
            assert_eq!(r.host.run(&request(SAVE_FILE, args.clone())).unwrap_err().code, code, "{args}");
        }
        assert_eq!(default_name(Purpose::Filtered, Format::Text, (2026, 1, 2, 3, 4, 5)), "tesseract-logs-20260102-030405.txt");
    }
    #[test] fn save_file_confirm_registers_the_grant_before_returning_the_ref() {
        let r = rig();
        let exports = r.dir.0.join("exports"); std::fs::create_dir_all(&exports).unwrap();
        let chosen = exports.join("diag.zip");
        *r.desktop.answer.lock().unwrap() = Some(chosen.clone());
        let sidecar = answer(&r.host, Some(Ok(json!({"t":"res","ok":true,"data":{"registered":true}}))));
        let result = r.host.run(&request(SAVE_FILE, json!({"purpose":"bundle","format":"zip"}))).unwrap();
        assert_eq!(result, json!({"cancelled":false,"ref":"save-0123456789abcdef"}));
        let seen = sidecar.join().unwrap();
        let frame = &seen[0];
        assert_eq!((frame["cmd"].as_str(), frame["from"].as_str(), frame["owner"].as_str()), (Some("shell.saveGrant"), Some("shell"), Some("shell")));
        assert_eq!(frame["args"]["for_owner"], "page-1", "T-P2-31b: the grant binds to the asking page owner");
        assert_eq!(frame["args"]["handle_id"], 1077, "the sidecar-side duplicate, not the shell's handle");
        assert_eq!((frame["args"]["ref"].as_str(), frame["args"]["purpose"].as_str(), frame["args"]["format"].as_str()), (Some("save-0123456789abcdef"), Some("bundle"), Some("zip")));
        assert!(frame["args"]["expires_at_ms"].as_u64().unwrap() > now_ms() + GRANT_TTL_MS - 60_000);
        assert_eq!(seen[1], "one", "bound to the session epoch");
        assert!(!result.to_string().contains("exports"), "the page never sees the path");
        // Reveal: only after the export job reported this ref, only for the same owner and epoch.
        let reveal = |owner: &str| { let mut q = request(REVEAL, json!({"ref":"save-0123456789abcdef"})); q.owner = owner.into(); r.host.run(&q) };
        assert_eq!(reveal("page-1").unwrap_err().code, "not_allowed", "registered but not exported yet");
        // Review S7: an edge without the owner, or with another one, does not make it revealable.
        for owner in [json!(null), json!("page-2")] {
            let mut data = json!({"name":"logs.exportBundle","state":"done","result":{"ref":"save-0123456789abcdef"}});
            if !owner.is_null() { data["owner"] = owner; }
            r.host.observe(&json!({"t":"evt","topic":"jobs","data":data}));
            assert_eq!(reveal("page-1").unwrap_err().code, "not_allowed");
        }
        r.host.observe(&json!({"t":"evt","topic":"jobs","data":{"name":"logs.exportBundle","state":"done","owner":"page-1","result":{"ref":"save-0123456789abcdef"}}}));
        assert_eq!(reveal("page-2").unwrap_err().code, "not_allowed", "another owner");
        assert_eq!(reveal("page-1").unwrap_err(), fail("io", "file_missing"), "the fake path does not exist");
        assert_eq!(r.host.run(&request(REVEAL, json!({"ref":"save-unknown"}))).unwrap_err().code, "not_allowed");
        assert_eq!(r.host.run(&request(REVEAL, json!({"ref":"save-0123456789abcdef","path":"C:\\"}))).unwrap_err().code, "not_allowed");
        // The exports folder stays the scope's <data>/exports, wherever the user saved (review S3).
        r.host.run(&request(OPEN_FOLDER, json!({"kind":"exports"}))).ok();
        assert_eq!(r.desktop.calls.lock().unwrap().last().unwrap(), &format!("folder {}", r.dir.0.join("data").join("exports").display()));
        // A new epoch forgets the ref.
        r.host.session("two", 42, true);
        assert_eq!(r.host.state().saves.len(), 0);
    }
    #[test] fn save_file_refused_or_unsent_grants_are_cleaned_up() {
        let r = rig();
        *r.desktop.answer.lock().unwrap() = Some(PathBuf::from(r"C:\x\logs.json"));
        let filtered = json!({"purpose":"filtered","format":"json"});
        // Refused by the sidecar: the file is discarded; the sidecar already released its copy.
        let t = answer(&r.host, Some(Ok(json!({"t":"res","ok":false,"error":{"code":"not_allowed"}}))));
        assert_eq!(r.host.run(&request(SAVE_FILE, filtered.clone())).unwrap_err(), fail("io", "grant_refused"));
        t.join().unwrap();
        assert!(r.sidecar.closed.lock().unwrap().is_empty());
        // Not sent, the session moved on: never close a handle value in whatever process is live now.
        let t = answer(&r.host, Some(Err("runtime_gone")));
        assert_eq!(r.host.run(&request(SAVE_FILE, filtered.clone())).unwrap_err(), fail("runtime_gone", "grant_not_sent"));
        t.join().unwrap();
        assert!(r.sidecar.closed.lock().unwrap().is_empty());
        // Not sent, the shell lane was full (same session): the shell closes its duplicate itself.
        let t = answer(&r.host, Some(Err("job_capacity")));
        assert_eq!(r.host.run(&request(SAVE_FILE, filtered.clone())).unwrap_err(), fail("job_capacity", "grant_not_sent"));
        t.join().unwrap();
        assert_eq!(*r.sidecar.closed.lock().unwrap(), vec![1077]);
        assert_eq!(r.desktop.discarded.lock().unwrap().len(), 3);
        // Wrong extension, no sidecar session, unsupported grant, target refused.
        *r.desktop.answer.lock().unwrap() = Some(PathBuf::from(r"C:\x\logs.exe"));
        assert_eq!(r.host.run(&request(SAVE_FILE, filtered.clone())).unwrap_err().reason, "extension_mismatch");
        *r.desktop.answer.lock().unwrap() = Some(PathBuf::from(r"C:\x\logs.json"));
        r.desktop.fail_create.store(true, Ordering::SeqCst);
        assert_eq!(r.host.run(&request(SAVE_FILE, filtered.clone())).unwrap_err().reason, "target_open_failed");
        r.host.session("one", 42, false);
        assert_eq!(r.host.run(&request(SAVE_FILE, filtered.clone())).unwrap_err().reason, "grant_unsupported");
        r.host.session_gone();
        assert_eq!(r.host.run(&request(SAVE_FILE, filtered.clone())).unwrap_err().code, "runtime_unavailable");
        // The sidecar was replaced after hello (other pid): never duplicate into the wrong process.
        r.desktop.fail_create.store(false, Ordering::SeqCst);
        r.host.session("one", 43, true);
        assert_eq!(r.host.run(&request(SAVE_FILE, filtered.clone())).unwrap_err().reason, "sidecar_changed");
        // Review S3: registered, but the epoch moved on meanwhile: no ref, the target is discarded.
        r.host.session("one", 42, true);
        let before = r.desktop.discarded.lock().unwrap().len();
        let host = r.host.clone();
        let t = std::thread::spawn(move || {
            let deadline = std::time::Instant::now() + Duration::from_secs(5);
            loop {
                if let Some(call) = host.outbox.drain().into_iter().next() {
                    host.session_gone(); host.session("two", 42, true);
                    let _ = call.reply.send(Ok(json!({"t":"res","ok":true,"data":{"registered":true}})));
                    return;
                }
                assert!(std::time::Instant::now() < deadline); std::thread::sleep(Duration::from_millis(5));
            }
        });
        assert_eq!(r.host.run(&request(SAVE_FILE, filtered)).unwrap_err(), fail("runtime_gone", "epoch_changed"));
        t.join().unwrap();
        assert_eq!(r.desktop.discarded.lock().unwrap().len(), before + 1);
        assert!(r.host.state().saves.is_empty(), "no ref registered for the old epoch");
    }
    #[test] fn submit_publishes_a_res_frame_and_bounds_the_envelope() {
        let r = rig();
        r.host.submit(request(OPEN_FOLDER, json!({"kind":"logs"}))).unwrap();
        let deadline = std::time::Instant::now() + Duration::from_secs(5);
        while r.frames.lock().unwrap().is_empty() { assert!(std::time::Instant::now() < deadline); std::thread::sleep(Duration::from_millis(5)); }
        let frame = r.frames.lock().unwrap()[0].clone();
        assert_eq!((frame["t"].as_str(), frame["id"].as_u64(), frame["req_id"].as_str(), frame["owner"].as_str(), frame["cmd"].as_str(), frame["ok"].as_bool()),
            (Some("res"), Some(9), Some("r1"), Some("page-1"), Some("host.openFolder"), Some(true)));
        let refused = response(&request(OPEN_URL, json!({})), Err(fail("not_allowed", "host")));
        assert_eq!(refused["error"], json!({"code":"not_allowed","message_key":"errors.host.not_allowed","params":{},"reason_code":"host"}));
        // Review S4: owner may be absent for openFolder/openUrl (the reply then has none), not for
        // saveFile/reveal whose results are bound to it.
        let mut ownerless = request(OPEN_FOLDER, json!({"kind":"logs"})); ownerless.owner = String::new(); ownerless.req_id = "r2".into();
        r.host.submit(ownerless).unwrap();
        while !r.frames.lock().unwrap().iter().any(|f| f["req_id"] == "r2") { assert!(std::time::Instant::now() < deadline); std::thread::sleep(Duration::from_millis(5)); }
        let frame = r.frames.lock().unwrap().iter().find(|f| f["req_id"] == "r2").unwrap().clone();
        assert!(frame["ok"] == true && frame.get("owner").is_none(), "{frame}");
        for cmd in [SAVE_FILE, REVEAL] { let mut bad = request(cmd, json!({})); bad.owner = String::new(); assert_eq!(r.host.submit(bad), Err("missing_field".into())); }
        let mut long = request(OPEN_FOLDER, json!({"kind":"logs"})); long.owner = "x".repeat(129);
        assert_eq!(r.host.submit(long), Err("invalid_type".into()));
        assert_eq!(r.host.submit(request("host.exec", json!({}))), Err("not_allowed".into()));
        assert!(is_host("host.saveFile") && !is_host("service.start"));
    }
}
