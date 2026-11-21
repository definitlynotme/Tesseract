//! Closing a running Tesseract before the Inno package runs (11-19).
//!
//! Inno's own Restart Manager pass (`CloseApplications=yes`, `/CLOSEAPPLICATIONS`) cannot do it:
//! the sidecar `tesseract-runtime.exe` has no window, Restart Manager types it `RmUnknownApp`
//! ("can only be shut down by a forced shutdown"), and one such process makes `RmShutdown` give
//! up on the whole list. Inno then answers its suppressed "unable to automatically close all
//! applications" box with Abort, rolls back and exits 5. So an install over a Tesseract running in
//! the tray installed nothing, while this window said 安装时会先关闭它 (seen 11-18, Inno log:
//! "Some applications could not be shut down ... Defaulting to Abort").
//!
//! Here only this session's shell is registered, by its pid and start time (`RM_UNIQUE_PROCESS`),
//! so Restart Manager sees the shell alone (`RmMainWindow`) and asks it politely:
//! WM_QUERYENDSESSION / WM_ENDSESSION with ENDSESSION_CLOSEAPP, then WM_CLOSE, to each of its
//! top-level windows. (Registering the file `<dir>\Tesseract.exe` instead, as 0.1.4 did, makes
//! `RmShutdown` act on everything that has that file open: another user's copy, Explorer showing
//! the folder, a scanner. One it cannot close stops the whole shutdown; Explorer it closes and
//! never restarts.) Shells 0.1.0–0.1.3 take
//! that as a session end, 0.1.4 and later as an ordinary quit (endsession.rs): either way the
//! sidecar restores the system proxy and firewall, says bye and exits, and the shell quits. Then
//! this waits until no `.exe` / `.dll` under `<dir>` is in use, the files Inno's pass registers
//! (CloseApplicationsFilter's default), so that pass finds nothing to close. Never a forced
//! shutdown: killing the sidecar would skip its network recovery.
use std::{path::{Path, PathBuf}, time::{Duration, Instant}};
use windows::{
    core::{HSTRING, PCWSTR, PWSTR},
    Win32::{
        Foundation::{ERROR_FAIL_NOACTION_REBOOT, ERROR_MORE_DATA, ERROR_SUCCESS},
        System::RestartManager::{RmEndSession, RmGetList, RmRegisterResources, RmShutdown, RmStartSession, CCH_RM_SESSION_KEY, RM_PROCESS_INFO, RM_UNIQUE_PROCESS},
    },
};

/// Counted once `RmShutdown` returns, which is when the shell has exited, or after Restart
/// Manager's own 30 s. A 0.1.0–0.1.3 shell's session end has 4 s for the sidecar; from 0.1.4 the
/// shell quits normally and waits at most 12 s for its WebView2 browser (endsession.rs). The
/// sidecar's process ends right after its bye. Generous, because a declined close only costs a
/// retry.
pub const WAIT: Duration = Duration::from_secs(30);
/// How long the page waits when Restart Manager could not ask the shell at all (it runs elevated
/// while this window does not, or it is already on its way out and has no window left): enough
/// for a shell that is leaving, short for one that stays. The in-app updater's hand-over always
/// waits the full [`WAIT`] for the shell that has just quit.
pub const UNASKED_WAIT: Duration = Duration::from_secs(5);
const POLL: Duration = Duration::from_millis(250);

#[derive(Debug, PartialEq)]
pub enum Closed {
    /// Nothing under the folder is in use (also: no install there yet).
    NothingRunning,
    /// Something was running and has gone.
    Gone,
    /// Still in use when the wait ran out: what is left.
    StillRunning(Vec<User>),
    /// Restart Manager could not be asked; the caller goes on as before.
    Unknown,
}

struct Session(u32);
impl Session {
    fn start() -> Option<Self> {
        let mut handle = 0u32;
        let mut key = [0u16; CCH_RM_SESSION_KEY as usize + 1];
        (unsafe { RmStartSession(&mut handle, None, PWSTR(key.as_mut_ptr())) } == ERROR_SUCCESS).then_some(Session(handle))
    }
    fn register(&self, files: &[PathBuf]) -> bool {
        let names: Vec<HSTRING> = files.iter().map(|f| HSTRING::from(f.as_os_str())).collect();
        let ptrs: Vec<PCWSTR> = names.iter().map(|n| PCWSTR(n.as_ptr())).collect();
        (unsafe { RmRegisterResources(self.0, Some(&ptrs), None, None) }) == ERROR_SUCCESS
    }
    /// Registers one process, named by its pid and start time: a pid reused since is never it.
    fn register_process(&self, process: RM_UNIQUE_PROCESS) -> bool {
        (unsafe { RmRegisterResources(self.0, None, Some(&[process]), None) }) == ERROR_SUCCESS
    }
    /// The processes using the registered files, in every session.
    fn users(&self) -> Option<Vec<User>> {
        let (mut needed, mut reasons) = (0u32, 0u32);
        for _ in 0..8 {
            let mut infos: Vec<RM_PROCESS_INFO> = (0..needed).map(|_| RM_PROCESS_INFO::default()).collect();
            let mut count = needed;
            let list = if infos.is_empty() { None } else { Some(infos.as_mut_ptr()) };
            let rc = unsafe { RmGetList(self.0, &mut needed, &mut count, list, &mut reasons) };
            if rc == ERROR_SUCCESS {
                infos.truncate(count as usize);
                return Some(infos.iter().map(|i| {
                    let len = i.strAppName.iter().position(|&c| c == 0).unwrap_or(i.strAppName.len());
                    let pid = i.Process.dwProcessId;
                    // RM_INVALID_SESSION for a process it cannot place (the windowless sidecar):
                    // asked directly then
                    let session = if i.TSSessionId != u32::MAX { Some(i.TSSessionId) } else { crate::sys::session_of(pid) };
                    User { pid, session, name: String::from_utf16_lossy(&i.strAppName[..len]) }
                }).collect());
            }
            if rc != ERROR_MORE_DATA { return None; }
        }
        None
    }
}

/// A process Restart Manager says uses a registered file; `session` None when it cannot be told.
#[derive(Debug, Clone, PartialEq)]
pub struct User { pub pid: u32, pub session: Option<u32>, pub name: String }

/// The programs in `users`, each named once, for the page: Restart Manager's application name,
/// else the image's file name, else the pid.
pub fn names(users: &[User]) -> Vec<String> { names_with(users, crate::sys::image_path) }

fn names_with(users: &[User], image: impl Fn(u32) -> Option<PathBuf>) -> Vec<String> {
    let mut seen: Vec<String> = Vec::new();
    for u in users {
        let file = || image(u.pid).and_then(|p| p.file_name().map(|f| f.to_string_lossy().into_owned()));
        let name = Some(u.name.trim().to_owned()).filter(|n| !n.is_empty()).or_else(file).unwrap_or_else(|| format!("PID {}", u.pid));
        if !seen.contains(&name) { seen.push(name); }
    }
    seen
}
impl Drop for Session {
    fn drop(&mut self) { unsafe { let _ = RmEndSession(self.0); } }
}

/// Every `.exe` and `.dll` under `dir` (Inno's CloseApplicationsFilter default is
/// `*.exe,*.dll,*.chm`; the app ships no `.chm`), sorted.
pub fn watched_files(dir: &Path) -> Vec<PathBuf> {
    let (mut found, mut stack) = (Vec::new(), vec![dir.to_path_buf()]);
    while let Some(next) = stack.pop() {
        let Ok(entries) = std::fs::read_dir(&next) else { continue };
        for entry in entries.flatten() {
            let (path, Ok(kind)) = (entry.path(), entry.file_type()) else { continue };
            if kind.is_dir() { stack.push(path); continue; }
            let watched = path.extension().and_then(|e| e.to_str()).is_some_and(|e| e.eq_ignore_ascii_case("exe") || e.eq_ignore_ascii_case("dll"));
            if kind.is_file() && watched { found.push(path); }
        }
    }
    found.sort();
    found
}

/// What uses any of `files` now; None when Restart Manager cannot tell.
fn users(files: &[PathBuf]) -> Option<Vec<User>> {
    let session = Session::start()?;
    if !session.register(files) { return None; }
    session.users()
}

/// This session's Tesseract shell, as found before it is closed: what to open again afterwards,
/// and as whom (its account's SID and its elevation; None when this window cannot read them).
#[derive(Debug, Clone, PartialEq)]
pub struct Shell { pub pid: u32, pub elevated: Option<bool>, pub user: Option<Vec<u8>> }

/// The Tesseract from `dir` running in this session. Restart Manager lists every process that
/// uses `<dir>\Tesseract.exe`: in every session (another user's copy on a shared PC) and with any
/// module of it loaded. Only a process of this session whose image is that file counts. When that
/// cannot be told, None: a second start only wakes the first, or waits for one that is quitting
/// (single_instance.rs in the shell).
pub fn shell_here(dir: &Path) -> Option<Shell> {
    shell_pid(dir).map(|pid| Shell { pid, elevated: crate::sys::process_elevated(pid), user: crate::sys::process_user(pid) })
}

/// The pid of [`shell_here`].
fn shell_pid(dir: &Path) -> Option<u32> {
    let exe = dir.join("Tesseract.exe");
    let here = crate::sys::session_id()?;
    let list = users(std::slice::from_ref(&exe))?;
    find_shell(&list, here, &exe, crate::sys::image_path)
}

/// The Restart Manager session [`close_app`] shuts down: the shell `pid` alone, named by its pid
/// and start time. None when the process is gone (its start time cannot be read) or Restart
/// Manager cannot be asked.
fn shutdown_session(pid: u32) -> Option<Session> {
    let started = crate::sys::process_start_time(pid)?;
    let session = Session::start()?;
    session.register_process(RM_UNIQUE_PROCESS { dwProcessId: pid, ProcessStartTime: started }).then_some(session)
}

/// Whether the Tesseract from `dir` runs in this session (before opening it again).
pub fn shell_running(dir: &Path) -> bool { shell_here(dir).is_some() }

/// The decision of [`shell_here`], apart from Win32: the pid of this session's shell.
fn find_shell(list: &[User], here: u32, exe: &Path, image: impl Fn(u32) -> Option<PathBuf>) -> Option<u32> {
    list.iter().find(|u| u.session == Some(here) && image(u.pid).is_some_and(|i| crate::sys::same_file(&i, exe))).map(|u| u.pid)
}

/// True when something uses the app's files under `dir` (the page then says 正在关闭 Tesseract…).
pub fn in_use(dir: &Path) -> bool {
    let files = watched_files(dir);
    !files.is_empty() && users(&files).is_some_and(|u| !u.is_empty())
}

/// Closes the Tesseract that uses `dir`, politely (see the module doc), and waits up to `wait`
/// until nothing uses the folder's `.exe` / `.dll` files any more; `unasked` instead when Restart
/// Manager could not ask the shell at all. Never shorter for any other reason: the sidecar leaves
/// a moment after the shell, and Inno must not start before it has.
pub fn close_app(dir: &Path, wait: Duration, unasked: Duration) -> Closed {
    let files = watched_files(dir);
    if files.is_empty() { return Closed::NothingRunning; }
    match users(&files) {
        None => return Closed::Unknown,
        Some(u) if u.is_empty() => return Closed::NothingRunning,
        Some(_) => {},
    }
    // This session's shell alone (see the module doc). None here (another session's copy, or only
    // the sidecar left of a shell that has gone): nothing to ask, the unasked wait. A shell already
    // on its way out (no window left) or one that runs elevated while this window does not make
    // Restart Manager do nothing (ERROR_FAIL_NOACTION_REBOOT); the wait then covers a shell that is
    // leaving. When Restart Manager cannot be asked at all, the full wait, as before.
    let asked = match shell_pid(dir) {
        None => false,
        Some(pid) => match shutdown_session(pid) {
            Some(session) => (unsafe { RmShutdown(session.0, 0, None) }) != ERROR_FAIL_NOACTION_REBOOT,
            None => true,
        },
    };
    let deadline = Instant::now() + if asked { wait } else { unasked };
    loop {
        match users(&files) {
            Some(u) if u.is_empty() => return Closed::Gone,
            left if Instant::now() >= deadline => return Closed::StillRunning(left.unwrap_or_default()),
            _ => std::thread::sleep(POLL),
        }
    }
}

/// The folder an Inno command line installs into: its `/DIR=` when given, else the existing
/// install (Inno's UsePreviousAppDir). A `/DIR=expand:…` names it with Inno's constants
/// ({autopf}…), which only Inno can expand: that is taken to be the existing install too.
pub fn target_dir(args: &[String], existing: Option<&str>) -> Option<PathBuf> {
    let given = args.iter().rev().find_map(|a| {
        let (switch, value) = a.split_once('=')?;
        switch.eq_ignore_ascii_case("/DIR").then(|| value.trim_matches('"').trim_end_matches('\\').to_owned())
    });
    let expands = |d: &str| d.get(..7).is_some_and(|p| p.eq_ignore_ascii_case("expand:"));
    given.filter(|d| !d.is_empty() && !expands(d)).or_else(|| existing.map(str::to_owned)).map(PathBuf::from)
}

fn has(args: &[String], switch: &str) -> bool { args.iter().any(|a| a.eq_ignore_ascii_case(switch)) }

/// `/NOCLOSEAPPLICATIONS`: leave running applications as Inno would. Inno ignores it next to
/// `/CLOSEAPPLICATIONS`, and so does this window.
pub fn keep_running(args: &[String]) -> bool { has(args, "/NOCLOSEAPPLICATIONS") && !has(args, "/CLOSEAPPLICATIONS") }

/// `/RESTARTAPPLICATIONS`: the in-app updater's run (the .iss [Run] entry reopens the app on it).
pub fn restarts_app(args: &[String]) -> bool { has(args, "/RESTARTAPPLICATIONS") }

/// `/NORESTARTAPPLICATIONS`: what was closed for the install stays closed after a good one.
pub fn no_restart(args: &[String]) -> bool { has(args, "/NORESTARTAPPLICATIONS") }

/// A command line that installs without a wizard (`/SILENT` or `/VERYSILENT`, no `/?` or
/// `/HELP`): only then is a running Tesseract closed up front. A wizard (Inno's own Preparing
/// page lists what to close) or a help box installs nothing on its own.
pub fn silent_install(args: &[String]) -> bool {
    (has(args, "/SILENT") || has(args, "/VERYSILENT")) && !has(args, "/?") && !has(args, "/HELP")
}

/// An explicit `/LOG` or `/LOG=<file>` on the command line.
pub fn logs_itself(args: &[String]) -> bool {
    args.iter().any(|a| a.eq_ignore_ascii_case("/LOG") || a.get(..5).is_some_and(|p| p.eq_ignore_ascii_case("/LOG=")))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    fn temp(tag: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("tesseract-setup-rm-{tag}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn watched_files_are_the_exe_and_dll_files_inno_registers() {
        let dir = temp("watched");
        fs::create_dir_all(dir.join("_internal").join("bin")).unwrap();
        for f in ["Tesseract.exe", "tesseract-runtime.exe", "LICENSE", "_internal/python312.dll", "_internal/x.pyd", "_internal/bin/mihomo.EXE", "_internal/a.txt"] {
            fs::write(dir.join(f), b"x").unwrap();
        }
        let names: Vec<String> = watched_files(&dir).iter().map(|p| p.strip_prefix(&dir).unwrap().to_string_lossy().replace('\\', "/")).collect();
        assert_eq!(names, ["Tesseract.exe", "_internal/bin/mihomo.EXE", "_internal/python312.dll", "tesseract-runtime.exe"]);
        assert!(watched_files(&dir.join("missing")).is_empty());
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn restart_manager_sees_a_running_image_and_nothing_else() {
        // Real Restart Manager, nothing shut down: this test binary uses its own image.
        let me = std::env::current_exe().unwrap();
        let found = users(std::slice::from_ref(&me)).expect("Restart Manager answers");
        let here = crate::sys::session_id().unwrap();
        assert!(found.iter().any(|u| u.pid == std::process::id() && u.session == Some(here)), "{found:?}");
        // a running image of this session counts as a shell running from its folder; a folder
        // nobody runs from does not
        let dir = me.parent().unwrap();
        assert_eq!(find_shell(&found, here, &me, crate::sys::image_path), Some(std::process::id()));
        assert_eq!(find_shell(&found, here.wrapping_add(1), &me, crate::sys::image_path), None, "another session's copy is not ours");
        assert!(!shell_running(&dir.join("no-such-folder")));
        // Files nobody has open: nothing to close, nothing waited for.
        let dir = temp("idle");
        fs::write(dir.join("Tesseract.exe"), b"not running").unwrap();
        fs::write(dir.join("helper.dll"), b"not loaded").unwrap();
        assert!(!in_use(&dir));
        assert_eq!(close_app(&dir, Duration::from_secs(1), Duration::from_secs(1)), Closed::NothingRunning);
        assert_eq!(close_app(&dir.join("missing"), Duration::from_secs(1), Duration::from_secs(1)), Closed::NothingRunning);
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn the_shutdown_session_holds_the_shell_alone() {
        // 0.1.5: RmShutdown acts on everything its session lists. With the file <dir>\Tesseract.exe
        // registered (0.1.4) that was every process holding it open (Explorer, a scanner, another
        // user's copy); the session close_app shuts down lists the shell alone. Real Restart
        // Manager, nothing shut down: a stand-in shell (ping.exe copied as Tesseract.exe) and this
        // test holding a plain handle on the same file.
        struct Running(std::process::Child);
        impl Drop for Running { fn drop(&mut self) { let _ = self.0.kill(); let _ = self.0.wait(); } }
        let dir = temp("shutdown");
        let exe = dir.join("Tesseract.exe");
        fs::copy(PathBuf::from(std::env::var_os("SystemRoot").unwrap()).join(r"System32\PING.EXE"), &exe).unwrap();
        let null = std::process::Stdio::null;
        let shell = Running(std::process::Command::new(&exe).args(["-n", "60", "127.0.0.1"]).stdin(null()).stdout(null()).stderr(null()).spawn().unwrap());
        let holder = fs::File::open(&exe).unwrap();
        let pids = |list: Vec<User>| { let mut p: Vec<u32> = list.iter().map(|u| u.pid).collect(); p.sort(); p.dedup(); p };
        let by_file = pids(users(std::slice::from_ref(&exe)).expect("Restart Manager answers"));
        assert!(by_file.contains(&shell.0.id()) && by_file.contains(&std::process::id()), "the file has both users: {by_file:?}");
        let session = shutdown_session(shell.0.id()).expect("a session for the running shell");
        assert_eq!(pids(session.users().expect("Restart Manager answers")), [shell.0.id()], "only the shell would be shut down");
        assert!(shutdown_session(u32::MAX - 3).is_none(), "no such process: no session");
        drop((session, holder, shell));
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn only_this_sessions_image_of_the_exe_is_the_running_shell() {
        let exe = PathBuf::from(r"C:\Program Files\Tesseract\Tesseract.exe");
        let user = |pid, session| User { pid, session: Some(session), name: format!("p{pid}") };
        let image = |pid: u32| match pid {
            10 | 20 => Some(PathBuf::from(r"c:\program files\TESSERACT\tesseract.exe")),
            30 => Some(PathBuf::from(r"C:\Windows\explorer.exe")),   // a handle on the file, not its image
            _ => None,                                               // cannot be asked
        };
        assert_eq!(find_shell(&[user(30, 1), user(10, 1)], 1, &exe, image), Some(10));
        assert_eq!(find_shell(&[user(20, 2)], 1, &exe, image), None, "another user's copy on a shared PC");
        assert_eq!(find_shell(&[user(30, 1), user(40, 1)], 1, &exe, image), None, "another program using the file");
        assert_eq!(find_shell(&[], 1, &exe, image), None);
        // the page names each program once: Restart Manager's name, else the image's file name
        // (the windowless sidecar has none), else the pid
        let named = |pid, name: &str| User { pid, session: Some(1), name: name.into() };
        let images = |pid: u32| (pid == 9).then(|| PathBuf::from(r"C:\Program Files\Tesseract\tesseract-runtime.exe"));
        assert_eq!(names_with(&[named(7, "Backup Agent"), named(8, " Backup Agent "), named(9, ""), named(5, ""), named(4, "Python")], images),
            ["Backup Agent", "tesseract-runtime.exe", "PID 5", "Python"]);
    }

    #[test]
    fn the_sidecar_without_a_session_from_restart_manager_is_asked_directly() {
        // RM_INVALID_SESSION is resolved by ProcessIdToSessionId: this process's own session.
        assert_eq!(crate::sys::session_of(std::process::id()), crate::sys::session_id());
        assert_eq!(crate::sys::session_of(u32::MAX - 3), None);
    }

    #[test]
    fn target_dir_and_switches() {
        let args = |a: &[&str]| a.iter().map(|s| s.to_string()).collect::<Vec<_>>();
        let existing = Some(r"C:\Program Files\Tesseract");
        assert_eq!(target_dir(&args(&["/SILENT", "/RESTARTAPPLICATIONS"]), existing), Some(PathBuf::from(r"C:\Program Files\Tesseract")));
        assert_eq!(target_dir(&args(&["/VERYSILENT", r"/DIR=D:\Apps\Tesseract\"]), existing), Some(PathBuf::from(r"D:\Apps\Tesseract")));
        assert_eq!(target_dir(&args(&[r#"/dir="E:\T""#]), None), Some(PathBuf::from(r"E:\T")));
        assert_eq!(target_dir(&args(&["/SILENT"]), None), None);
        assert!(keep_running(&args(&["/VERYSILENT", "/noCloseApplications"])) && !keep_running(&args(&["/CLOSEAPPLICATIONS"])));
        assert!(!keep_running(&args(&["/VERYSILENT", "/CLOSEAPPLICATIONS", "/NOCLOSEAPPLICATIONS"])), "Inno ignores it next to /CLOSEAPPLICATIONS");
        assert!(no_restart(&args(&["/VERYSILENT", "/NoRestartApplications"])) && !no_restart(&args(&["/VERYSILENT", "/RESTARTAPPLICATIONS"])));
        // Inno's constants are Inno's to expand: the existing install is meant
        assert_eq!(target_dir(&args(&["/VERYSILENT", r"/DIR=expand:{autopf}\Tesseract"]), existing), Some(PathBuf::from(r"C:\Program Files\Tesseract")));
        assert_eq!(target_dir(&args(&[r"/DIR=EXPAND:{commonpf64}\Tesseract"]), None), None);
        // the in-app updater's exact switches (bridge_update.INSTALLER_ARGS) reopen; a deployment's do not
        assert!(restarts_app(&args(&["/SILENT", "/SUPPRESSMSGBOXES", "/CLOSEAPPLICATIONS", "/RESTARTAPPLICATIONS", "/NORESTART"])));
        assert!(!restarts_app(&args(&["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/NOCANCEL", "/SP-", "/CLOSEAPPLICATIONS", "/NORESTARTAPPLICATIONS"])));
        // only a silent install closes a running Tesseract up front; help or a wizard never does
        assert!(silent_install(&args(&["/SILENT", "/RESTARTAPPLICATIONS"])) && silent_install(&args(&["/verysilent"])));
        assert!(!silent_install(&args(&["/?"])) && !silent_install(&args(&["/LANG=english"])) && !silent_install(&args(&["/SILENT", "/HELP"])));
        assert!(logs_itself(&args(&["/SILENT", r"/LOG=C:\x.log"])) && logs_itself(&args(&["/log"])) && !logs_itself(&args(&["/SILENT", "/LOGX"])));
    }
}
