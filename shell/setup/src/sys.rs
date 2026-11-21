//! Windows facts and acts the window needs: the existing install, whether the app runs, the
//! WebView2 runtime, free space, the folder picker, launching (elevated or not).
use std::{ffi::OsStr, os::windows::ffi::OsStrExt, path::{Path, PathBuf}};
use windows::{
    core::{w, Interface, HSTRING, PCWSTR},
    Win32::{
        Foundation::{CloseHandle, ERROR_CANCELLED, FILETIME, HWND, WAIT_OBJECT_0},
        Globalization::GetUserDefaultUILanguage,
        Graphics::Dwm::{DwmSetWindowAttribute, DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_ROUND},
        Security::{GetLengthSid, GetTokenInformation, TokenElevation, TokenUser, TOKEN_ELEVATION, TOKEN_QUERY, TOKEN_USER},
        Storage::FileSystem::{GetDiskFreeSpaceExW, GetDriveTypeW, QueryDosDeviceW},
        System::{
            Com::{
                CoCreateInstance, CoInitializeEx, CoTaskMemFree, IDispatch, IServiceProvider, CLSCTX_INPROC_SERVER, CLSCTX_LOCAL_SERVER,
                COINIT_APARTMENTTHREADED, COINIT_DISABLE_OLE1DDE,
            },
            Environment::GetCommandLineW,
            JobObjects::IsProcessInJob,
            Registry::{RegGetValueW, HKEY, HKEY_CURRENT_USER, HKEY_LOCAL_MACHINE, REG_ROUTINE_FLAGS, RRF_RT_REG_SZ, RRF_SUBKEY_WOW6432KEY, RRF_SUBKEY_WOW6464KEY},
            RemoteDesktop::ProcessIdToSessionId,
            Threading::{
                CreateProcessW, GetCurrentProcess, GetCurrentProcessId, GetExitCodeProcess, GetProcessTimes, OpenEventW, OpenMutexW, OpenProcess, OpenProcessToken,
                QueryFullProcessImageNameW, WaitForSingleObject, CREATE_BREAKAWAY_FROM_JOB, INFINITE, PROCESS_INFORMATION, PROCESS_NAME_WIN32,
                PROCESS_QUERY_LIMITED_INFORMATION, STARTUPINFOW, SYNCHRONIZATION_SYNCHRONIZE,
            },
            Variant::{VariantClear, VARIANT, VT_BSTR, VT_I4},
        },
        UI::{
            Shell::{
                FileOpenDialog, IFileOpenDialog, IShellBrowser, IShellDispatch2, IShellFolderViewDual, IShellItem, IShellView, IShellWindows,
                SHCreateItemFromParsingName, ShellExecuteExW, ShellExecuteW, ShellWindows, FOS_FORCEFILESYSTEM, FOS_PATHMUSTEXIST, FOS_PICKFOLDERS,
                SEE_MASK_FLAG_NO_UI, SEE_MASK_NOASYNC, SEE_MASK_NOCLOSEPROCESS, SHELLEXECUTEINFOW, SID_STopLevelBrowser, SIGDN_FILESYSPATH,
                SVGIO_BACKGROUND, SWC_DESKTOP, SWFO_NEEDDISPATCH,
            },
            WindowsAndMessaging::SW_SHOWNORMAL,
        },
    },
};

/// The installer's AppId (Tesseract.iss / Tesseract-shell.iss) and Inno's uninstall key for it.
pub const UNINSTALL_KEY: &str = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\{7D0F8A35-2A48-44A8-A75A-1A2F8631BA72}_is1";
/// Held by the app (Qt and shell) for the default instance scope.
const APP_MUTEX: PCWSTR = w!("Local\\TesseractProxyManagerSingleInstance");
const WEBVIEW2_CLIENT: &str = r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}";

fn reg_string(root: HKEY, sub: &str, name: &str, flags: REG_ROUTINE_FLAGS) -> Option<String> {
    let (sub, name) = (HSTRING::from(sub), HSTRING::from(name));
    let mut size = 0u32;
    unsafe { RegGetValueW(root, &sub, &name, RRF_RT_REG_SZ | flags, None, None, Some(&mut size)) }.ok().ok()?;
    let mut buf = vec![0u16; size as usize / 2 + 1];
    unsafe { RegGetValueW(root, &sub, &name, RRF_RT_REG_SZ | flags, None, Some(buf.as_mut_ptr().cast()), Some(&mut size)) }.ok().ok()?;
    let len = buf.iter().position(|&c| c == 0).unwrap_or(buf.len());
    Some(String::from_utf16_lossy(&buf[..len]))
}

pub struct Existing { pub dir: String, pub version: String }

/// Development builds only (test/sandbox.iss): read the sandbox installer's per-user entry
/// (`TESSERACT_SETUP_TEST_KEY`, an HKCU subkey) and run it without the elevation prompt
/// (`TESSERACT_SETUP_NO_ELEVATE=1`). Release builds ignore both.
pub fn test_key() -> Option<String> {
    if cfg!(debug_assertions) { std::env::var("TESSERACT_SETUP_TEST_KEY").ok().filter(|k| !k.is_empty()) } else { None }
}
pub fn no_elevate() -> bool { cfg!(debug_assertions) && std::env::var("TESSERACT_SETUP_NO_ELEVATE").as_deref() == Ok("1") }

/// The install Inno registered (admin install: HKLM, 64-bit view).
pub fn existing_install() -> Option<Existing> {
    let (root, key, flags) = match test_key() {
        Some(k) => (HKEY_CURRENT_USER, k, REG_ROUTINE_FLAGS(0)),
        None => (HKEY_LOCAL_MACHINE, UNINSTALL_KEY.to_owned(), RRF_SUBKEY_WOW6464KEY),
    };
    let dir = reg_string(root, &key, "InstallLocation", flags)?;
    let dir = dir.trim().trim_end_matches('\\').to_owned();
    if dir.is_empty() { return None; }
    let version = reg_string(root, &key, "DisplayVersion", flags).unwrap_or_default();
    Some(Existing { dir, version: version.trim().to_owned() })
}

/// Microsoft's documented check, as the .iss does it: "pv" under the EdgeUpdate client key.
pub fn webview2_installed() -> bool {
    let present = |root, flags| reg_string(root, WEBVIEW2_CLIENT, "pv", flags).map(|v| { let v = v.trim(); !v.is_empty() && v != "0.0.0.0" }).unwrap_or(false);
    present(HKEY_LOCAL_MACHINE, RRF_SUBKEY_WOW6432KEY) || present(HKEY_CURRENT_USER, REG_ROUTINE_FLAGS(0)) || present(HKEY_LOCAL_MACHINE, RRF_SUBKEY_WOW6464KEY)
}

pub fn app_running() -> bool {
    match unsafe { OpenMutexW(SYNCHRONIZATION_SYNCHRONIZE, false, APP_MUTEX) } {
        Ok(h) => { unsafe { let _ = CloseHandle(h); } true }
        Err(_) => false,
    }
}

/// `%PUBLIC%\Desktop\Tesseract.lnk` (an admin install's {autodesktop} is the common desktop).
pub fn desktop_shortcut_exists() -> bool {
    std::env::var_os("PUBLIC").map(|p| PathBuf::from(p).join("Desktop").join("Tesseract.lnk").exists()).unwrap_or(false)
}

pub fn default_dir() -> String {
    if test_key().is_some() { return format!(r"{}\TesseractSetupSandbox\app", std::env::temp_dir().display().to_string().trim_end_matches('\\')); }
    let base = std::env::var("ProgramW6432").or_else(|_| std::env::var("ProgramFiles")).unwrap_or_else(|_| r"C:\Program Files".into());
    format!(r"{}\Tesseract", base.trim_end_matches('\\'))
}

/// The UI language: the app's nine (ui/src/i18n), else English.
pub fn ui_language() -> &'static str {
    match unsafe { GetUserDefaultUILanguage() } & 0x3ff {
        0x04 => "zh_CN",
        0x11 => "ja_JP",
        0x12 => "ko_KR",
        0x07 => "de_DE",
        0x0a => "es_ES",
        0x0c => "fr_FR",
        0x16 => "pt_BR",
        0x19 => "ru_RU",
        _ => "en_US",
    }
}

/// Free bytes for the caller on the volume of `dir` (the nearest existing ancestor).
pub fn free_bytes(dir: &str) -> Option<(u64, String)> {
    let mut probe = Path::new(dir);
    while !probe.exists() { probe = probe.parent()?; }
    let root = probe.ancestors().last()?.display().to_string();
    let mut free = 0u64;
    unsafe { GetDiskFreeSpaceExW(&HSTRING::from(probe.as_os_str()), Some(&mut free), None, None) }.ok()?;
    Some((free, root.trim_end_matches('\\').to_owned()))
}

pub fn round_corners(hwnd: isize) {
    let pref = DWMWCP_ROUND;
    unsafe { let _ = DwmSetWindowAttribute(HWND(hwnd as _), DWMWA_WINDOW_CORNER_PREFERENCE, (&pref as *const windows::Win32::Graphics::Dwm::DWM_WINDOW_CORNER_PREFERENCE).cast(), std::mem::size_of_val(&pref) as u32); }
}

pub fn com_init() { unsafe { let _ = CoInitializeEx(None, COINIT_APARTMENTTHREADED | COINIT_DISABLE_OLE1DDE); } }

/// The system folder picker, owned by the window (blocks the calling thread, which must be
/// a COM STA thread other than the event loop's). Ok(None): cancelled.
pub fn pick_folder(owner: isize, start: &str) -> windows::core::Result<Option<String>> {
    unsafe {
        let dlg: IFileOpenDialog = CoCreateInstance(&FileOpenDialog, None, CLSCTX_INPROC_SERVER)?;
        dlg.SetOptions(dlg.GetOptions()? | FOS_PICKFOLDERS | FOS_FORCEFILESYSTEM | FOS_PATHMUSTEXIST)?;
        let mut probe = Path::new(start);
        while !probe.exists() { match probe.parent() { Some(p) => probe = p, None => break } }
        if probe.exists() {
            if let Ok(item) = SHCreateItemFromParsingName::<_, _, IShellItem>(&HSTRING::from(probe.as_os_str()), None) { let _ = dlg.SetFolder(&item); }
        }
        if let Err(e) = dlg.Show(Some(HWND(owner as _))) {
            if e.code() == ERROR_CANCELLED.to_hresult() { return Ok(None); }
            return Err(e);
        }
        let item = dlg.GetResult()?;
        let name = item.GetDisplayName(SIGDN_FILESYSPATH)?;
        let path = name.to_string();
        CoTaskMemFree(Some(name.0 as _));
        Ok(Some(path.map_err(|_| windows::core::Error::from(windows::Win32::Foundation::E_FAIL))?))
    }
}

fn wide(s: &OsStr) -> Vec<u16> { s.encode_wide().chain(Some(0)).collect() }

pub enum Run { Exited(u32), Declined, Failed(String) }

/// Runs `exe params` and waits for it. `elevate`: the "runas" verb (UAC). Declined: the user
/// said no to the elevation prompt.
pub fn run_and_wait(owner: isize, exe: &Path, params: &str, elevate: bool) -> Run {
    let file = wide(exe.as_os_str());
    let params = wide(OsStr::new(params));
    let dir = wide(exe.parent().unwrap_or(Path::new(".")).as_os_str());
    let mut info = SHELLEXECUTEINFOW {
        cbSize: std::mem::size_of::<SHELLEXECUTEINFOW>() as u32,
        fMask: SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC | SEE_MASK_FLAG_NO_UI,
        hwnd: HWND(owner as _),
        lpVerb: if elevate { w!("runas") } else { w!("open") },
        lpFile: PCWSTR(file.as_ptr()),
        lpParameters: PCWSTR(params.as_ptr()),
        lpDirectory: PCWSTR(dir.as_ptr()),
        nShow: SW_SHOWNORMAL.0,
        ..Default::default()
    };
    if let Err(e) = unsafe { ShellExecuteExW(&mut info) } {
        if e.code() == ERROR_CANCELLED.to_hresult() { return Run::Declined; }
        return Run::Failed(e.message().trim().to_owned());
    }
    if info.hProcess.is_invalid() { return Run::Failed("no process".into()); }
    let mut code = 1u32;
    unsafe {
        if WaitForSingleObject(info.hProcess, INFINITE) == WAIT_OBJECT_0 { let _ = GetExitCodeProcess(info.hProcess, &mut code); }
        let _ = CloseHandle(info.hProcess);
    }
    Run::Exited(code)
}

/// Opens a file or program as the user (the app after installing, the log in its editor).
pub fn open(path: &Path, cwd: Option<&Path>) -> bool {
    let cwd = cwd.map(|c| HSTRING::from(c.as_os_str()));
    let r = unsafe {
        ShellExecuteW(None, w!("open"), &HSTRING::from(path.as_os_str()), PCWSTR::null(),
            cwd.as_ref().map(|c| PCWSTR(c.as_ptr())).unwrap_or(PCWSTR::null()), SW_SHOWNORMAL)
    };
    r.0 as isize > 32
}

/// One argument for Inno's command line, read back by [`inno_params`]: a double quote toggles
/// quoting and backslashes are literal, so an argument with a space (any character up to one) is
/// wrapped whole, and a double quote (never part of a path) is dropped.
pub fn quote(arg: &str) -> String {
    let arg: String = arg.chars().filter(|&c| c != '"').collect();
    if !arg.is_empty() && !arg.chars().any(|c| c <= ' ') { return arg; }
    format!("\"{arg}\"")
}

/// This process's command line as Windows passed it.
pub fn command_line() -> Vec<u16> { unsafe { GetCommandLineW().as_wide().to_vec() } }

/// The parameters this window was given: Inno's split ([`inno_params`]), except for a line that
/// escapes a quote the C runtime's way, `\"` before a non-blank (a path ending in a backslash only
/// ever puts a blank or the end there). Launchers that write such lines (PowerShell 7.3+, .NET's
/// ArgumentList, Python's subprocess) mean the C runtime's split, which 0.1.0–0.1.3 used for every
/// line; each parameter is then passed on to Inno with [`quote`].
pub fn params() -> Vec<String> {
    let line = command_line();
    if escapes_quotes(&line) { std::env::args_os().skip(1).map(|a| a.to_string_lossy().into_owned()).collect() } else { inno_params(&line) }
}

fn escapes_quotes(line: &[u16]) -> bool {
    line.windows(3).any(|w| w[0] == u16::from(b'\\') && w[1] == u16::from(b'"') && w[2] > 0x20)
}

/// The parameters of a command line split the way Inno Setup splits its own (GetParamStr in its
/// Shared.CommonFunc; the [Code] ParamStr reads the same): a parameter runs to the next character
/// at or below a space outside double quotes, each double quote toggles quoting and is dropped,
/// and a backslash is just a backslash. The first parameter, the program, is left out. Read this
/// way, a deployment's `/DIR="C:\Program Files\Tesseract\" /VERYSILENT` means to this window what
/// it means to Inno; the C runtime's rules would read `\"` as a quote and swallow `/VERYSILENT`.
pub fn inno_params(line: &[u16]) -> Vec<String> {
    let (mut params, mut i, mut program) = (Vec::new(), 0, true);
    let at = |i: usize| line.get(i).copied().unwrap_or(0);
    loop {
        let (mut param, mut quoted) = (Vec::new(), false);
        while at(i) != 0 && (at(i) > 0x20 || quoted) {
            if at(i) == u16::from(b'"') { quoted = !quoted } else { param.push(at(i)) }
            i += 1;
        }
        if !program { params.push(String::from_utf16_lossy(&param)); }
        program = false;
        while at(i) != 0 && at(i) <= 0x20 { i += 1; }
        if at(i) == 0 { return params; }
    }
}

/// The Terminal Services session this process runs in.
pub fn session_id() -> Option<u32> { session_of(unsafe { GetCurrentProcessId() }) }

/// The Terminal Services session a process runs in; None when it cannot be asked.
pub fn session_of(pid: u32) -> Option<u32> {
    let mut id = 0u32;
    unsafe { ProcessIdToSessionId(pid, &mut id) }.ok().map(|_| id)
}

/// The Win32 path of a process's image; None when it cannot be asked (gone, or not ours to ask).
pub fn image_path(pid: u32) -> Option<PathBuf> {
    let process = unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, false, pid) }.ok()?;
    let mut buf = vec![0u16; 32_768];
    let mut len = buf.len() as u32;
    let read = unsafe { QueryFullProcessImageNameW(process, PROCESS_NAME_WIN32, windows::core::PWSTR(buf.as_mut_ptr()), &mut len) };
    unsafe { let _ = CloseHandle(process); }
    read.ok()?;
    Some(PathBuf::from(String::from_utf16_lossy(&buf[..len as usize])))
}

/// When a process started (GetProcessTimes); with its pid, how Restart Manager names one process
/// (`RM_UNIQUE_PROCESS`). None when it cannot be asked (gone, or not ours to ask).
pub fn process_start_time(pid: u32) -> Option<FILETIME> {
    let process = unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, false, pid) }.ok()?;
    let (mut created, mut exited, mut kernel, mut user) = (FILETIME::default(), FILETIME::default(), FILETIME::default(), FILETIME::default());
    let read = unsafe { GetProcessTimes(process, &mut created, &mut exited, &mut kernel, &mut user) };
    unsafe { let _ = CloseHandle(process); }
    read.ok().map(|_| created)
}

/// Two paths name the same file: resolved (short names, links, case) when both exist.
pub fn same_file(a: &Path, b: &Path) -> bool {
    fn key(p: &Path) -> String {
        let resolved = std::fs::canonicalize(p).unwrap_or_else(|_| p.to_path_buf()).to_string_lossy().into_owned();
        let plain = match resolved.strip_prefix(r"\\?\UNC\") { Some(rest) => format!(r"\\{rest}"), None => resolved.strip_prefix(r"\\?\").map(str::to_owned).unwrap_or(resolved) };
        plain.to_lowercase()
    }
    key(a) == key(b)
}

/// In a user's session: a service's (session 0) has no desktop to open the app on.
pub fn in_user_session() -> bool { matches!(session_id(), Some(id) if id != 0) }

/// What a process's token says, read with `read`; None when the token cannot be opened.
fn with_token<T>(process: windows::Win32::Foundation::HANDLE, read: impl FnOnce(windows::Win32::Foundation::HANDLE) -> Option<T>) -> Option<T> {
    let mut token = windows::Win32::Foundation::HANDLE::default();
    unsafe { OpenProcessToken(process, TOKEN_QUERY, &mut token) }.ok()?;
    let value = read(token);
    unsafe { let _ = CloseHandle(token); }
    value
}

fn token_elevated(token: windows::Win32::Foundation::HANDLE) -> Option<bool> {
    let mut elevation = TOKEN_ELEVATION::default();
    let mut len = 0u32;
    let out: *mut TOKEN_ELEVATION = &mut elevation;
    unsafe { GetTokenInformation(token, TokenElevation, Some(out.cast()), std::mem::size_of::<TOKEN_ELEVATION>() as u32, &mut len) }.ok()?;
    Some(elevation.TokenIsElevated != 0)
}

/// The account's SID, as bytes.
fn token_user(token: windows::Win32::Foundation::HANDLE) -> Option<Vec<u8>> {
    let mut len = 0u32;
    let _ = unsafe { GetTokenInformation(token, TokenUser, None, 0, &mut len) };
    if len == 0 { return None; }
    let mut buf = vec![0u64; (len as usize).div_ceil(8)]; // TOKEN_USER holds a pointer: 8-aligned
    unsafe { GetTokenInformation(token, TokenUser, Some(buf.as_mut_ptr().cast()), len, &mut len) }.ok()?;
    let sid = unsafe { (*buf.as_ptr().cast::<TOKEN_USER>()).User.Sid };
    let size = unsafe { GetLengthSid(sid) } as usize;
    (size > 0).then(|| unsafe { std::slice::from_raw_parts(sid.0 as *const u8, size) }.to_vec())
}

fn with_process<T>(pid: u32, read: impl FnOnce(windows::Win32::Foundation::HANDLE) -> Option<T>) -> Option<T> {
    let process = unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, false, pid) }.ok()?;
    let value = with_token(process, read);
    unsafe { let _ = CloseHandle(process); }
    value
}

/// This window runs elevated: Run as administrator, an elevated console, SYSTEM (or UAC off).
pub fn elevated() -> bool { with_token(unsafe { GetCurrentProcess() }, token_elevated).unwrap_or(false) }

/// The account this window runs as.
pub fn own_user() -> Option<Vec<u8>> { with_token(unsafe { GetCurrentProcess() }, token_user) }

/// Whether a process runs elevated; None when it cannot be asked (an elevated one, from here).
pub fn process_elevated(pid: u32) -> Option<bool> { with_process(pid, token_elevated) }

/// The account a process runs as; None when it cannot be asked.
pub fn process_user(pid: u32) -> Option<Vec<u8>> { with_process(pid, token_user) }

/// Whether the desktop's Explorer runs elevated (UAC off); None without one.
pub fn explorer_elevated() -> Option<bool> {
    use windows::Win32::UI::WindowsAndMessaging::{GetShellWindow, GetWindowThreadProcessId};
    let shell = unsafe { GetShellWindow() };
    if shell.0.is_null() { return None; }
    let mut pid = 0u32;
    unsafe { GetWindowThreadProcessId(shell, Some(&mut pid)) };
    if pid == 0 { None } else { process_elevated(pid) }
}

/// The default scope's shell is on its way out: its quitting event is set (a tray quit, a session
/// end, an update quit). Other scopes (development, the sandbox) are not seen here.
pub fn shell_quitting() -> bool { event_set(w!("Local\\TesseractShellQuitting")) }

fn event_set(name: PCWSTR) -> bool {
    match unsafe { OpenEventW(SYNCHRONIZATION_SYNCHRONIZE, false, name) } {
        Ok(event) => {
            let set = unsafe { WaitForSingleObject(event, 0) } == WAIT_OBJECT_0;
            unsafe { let _ = CloseHandle(event); }
            set
        }
        Err(_) => false,
    }
}

/// Starts `exe` with this window's token but outside its job (CREATE_BREAKAWAY_FROM_JOB); false
/// when the job does not allow that.
pub fn open_breakaway(exe: &Path, dir: &Path) -> bool {
    let mut line = wide(OsStr::new(&format!("\"{}\"", exe.display())));
    let startup = STARTUPINFOW { cb: std::mem::size_of::<STARTUPINFOW>() as u32, ..Default::default() };
    let mut started = PROCESS_INFORMATION::default();
    let made = unsafe {
        CreateProcessW(&HSTRING::from(exe.as_os_str()), Some(windows::core::PWSTR(line.as_mut_ptr())), None, None, false,
            CREATE_BREAKAWAY_FROM_JOB, None, &HSTRING::from(dir.as_os_str()), &startup, &mut started)
    };
    if made.is_ok() { unsafe { let _ = CloseHandle(started.hThread); let _ = CloseHandle(started.hProcess); } }
    made.is_ok()
}

/// This window runs inside a job object (PowerShell's Start-Process -Wait, a deployment agent): a
/// program it starts joins the job, which then waits for it or ends it.
pub fn in_job() -> bool {
    let mut inside = windows::core::BOOL::default();
    unsafe { IsProcessInJob(GetCurrentProcess(), None, &mut inside) }.is_ok() && inside.as_bool()
}

/// Starts `exe` the way the signed-in user would from the desktop: through Explorer (its
/// IShellDispatch2::ShellExecute), so with Explorer's unelevated token and outside this window's
/// job. `show` is a SW_ value. False when the desktop's Explorer cannot be asked (another shell,
/// or none) or does not answer within 10 s (a hung Explorer must not hold the install up).
pub fn open_as_desktop_user(exe: &Path, args: &str, dir: &Path, show: i32) -> bool {
    let (exe, args, dir) = (exe.to_path_buf(), args.to_owned(), dir.to_path_buf());
    let (done, answer) = std::sync::mpsc::channel();
    std::thread::spawn(move || { let _ = done.send(desktop_start(&exe, &args, &dir, show)); });
    answer.recv_timeout(std::time::Duration::from_secs(10)).unwrap_or(false)
}

fn desktop_start(exe: &Path, args: &str, dir: &Path, show: i32) -> bool {
    fn text(s: &str) -> VARIANT {
        let mut v = VARIANT::default();
        unsafe {
            let inner = &mut *v.Anonymous.Anonymous;
            inner.vt = VT_BSTR;
            inner.Anonymous.bstrVal = std::mem::ManuallyDrop::new(windows::core::BSTR::from(s));
        }
        v
    }
    fn number(n: i32) -> VARIANT {
        let mut v = VARIANT::default();
        unsafe {
            let inner = &mut *v.Anonymous.Anonymous;
            inner.vt = VT_I4;
            inner.Anonymous.lVal = n;
        }
        v
    }
    // its own thread: its own apartment, left again once every COM object is gone
    let entered = unsafe { CoInitializeEx(None, COINIT_APARTMENTTHREADED | COINIT_DISABLE_OLE1DDE) }.is_ok();
    let run = || -> windows::core::Result<()> {
        unsafe {
            let windows: IShellWindows = CoCreateInstance(&ShellWindows, None, CLSCTX_LOCAL_SERVER)?;
            let (desktop, empty) = (number(0), VARIANT::default()); // CSIDL_DESKTOP
            let mut hwnd = 0i32;
            let found: IDispatch = windows.FindWindowSW(&desktop, &empty, SWC_DESKTOP, &mut hwnd, SWFO_NEEDDISPATCH)?;
            let browser: IShellBrowser = found.cast::<IServiceProvider>()?.QueryService(&SID_STopLevelBrowser)?;
            let view: IShellView = browser.QueryActiveShellView()?;
            let background: IDispatch = view.GetItemObject(SVGIO_BACKGROUND)?;
            let shell: IShellDispatch2 = background.cast::<IShellFolderViewDual>()?.Application()?.cast()?;
            let mut values = [text(args), text(&dir.to_string_lossy()), text("open"), number(show)];
            let started = shell.ShellExecute(&windows::core::BSTR::from(exe.to_string_lossy().as_ref()), &values[0], &values[1], &values[2], &values[3]);
            for v in &mut values { let _ = VariantClear(v); }
            started
        }
    };
    let started = run().is_ok();
    if entered { unsafe { windows::Win32::System::Com::CoUninitialize() }; }
    started
}

/// A folder on a local fixed disk, which the elevated installer sees as well: not a network drive
/// and not a SUBST or mapped letter (UAC gives the elevated token a drive map of its own).
pub fn local_disk(dir: &Path) -> bool {
    let path = dir.to_string_lossy();
    let mut chars = path.chars();
    let (Some(letter), Some(':')) = (chars.next(), chars.next()) else { return false };
    if !letter.is_ascii_alphabetic() { return false; }
    const DRIVE_FIXED: u32 = 3;
    if unsafe { GetDriveTypeW(&HSTRING::from(format!("{letter}:\\"))) } != DRIVE_FIXED { return false; }
    let mut target = [0u16; 1024];
    let len = unsafe { QueryDosDeviceW(&HSTRING::from(format!("{letter}:")), Some(&mut target)) } as usize;
    len > 0 && !String::from_utf16_lossy(&target[..len.min(target.len())]).starts_with(r"\??\")
}

#[cfg(test)]
mod tests {
    use super::*;
    fn split(line: &str) -> Vec<String> { inno_params(&line.encode_utf16().collect::<Vec<_>>()) }

    #[test]
    fn quoting() {
        assert_eq!(quote("/VERYSILENT"), "/VERYSILENT");
        assert_eq!(quote(r"/DIR=C:\Program Files\Tesseract"), r#""/DIR=C:\Program Files\Tesseract""#);
        assert_eq!(quote(r"/LOG=C:\a b\x.log"), r#""/LOG=C:\a b\x.log""#);
        assert_eq!(quote(r#"a"b"#), "ab");
        assert_eq!(quote(""), r#""""#);
        assert_eq!(quote("a\u{1}b"), "\"a\u{1}b\"", "Inno splits at every character up to a space");
    }

    #[test]
    fn the_command_line_is_split_as_inno_splits_it() {
        // the C runtime would read `\"` as a quote and make one parameter of the rest
        assert_eq!(split(r#""C:\Users\Default User\Tesseract-Setup.exe" /DIR="C:\Program Files\Tesseract\" /VERYSILENT"#),
            [r"/DIR=C:\Program Files\Tesseract\", "/VERYSILENT"]);
        // the in-app updater's line (std::process::Command quoting, nothing to escape)
        assert_eq!(split(r#""C:\Users\user\AppData\Local\Tesseract\updates\Tesseract-Setup-0.1.5-win-x64.exe" /SILENT /SUPPRESSMSGBOXES /CLOSEAPPLICATIONS /RESTARTAPPLICATIONS /NORESTART"#),
            ["/SILENT", "/SUPPRESSMSGBOXES", "/CLOSEAPPLICATIONS", "/RESTARTAPPLICATIONS", "/NORESTART"]);
        assert_eq!(split(r#"setup.exe --payload "D:\a b\p.exe"  "/LOG=C:\a b\x.log"	/SP-"#), ["--payload", r"D:\a b\p.exe", r"/LOG=C:\a b\x.log", "/SP-"]);
        // empty parameters count, quotes inside a parameter toggle, an open quote runs to the end
        assert_eq!(split(r#"x.exe "" /DIR=C:\"Program Files"\T "/LANG=en"#), ["", r"/DIR=C:\Program Files\T", "/LANG=en"]);
        assert_eq!(split("x.exe"), Vec::<String>::new());
        assert_eq!(split("x.exe   "), Vec::<String>::new());
        assert_eq!(split(" /SILENT"), ["/SILENT"], "an empty program name, as Inno's NewParamStr reads it");
        // whatever quote() makes, Inno reads back unchanged
        for arg in [r"/DIR=C:\Program Files\Tesseract\", "/VERYSILENT", "", r"/LOG=C:\a b\x.log", "tab\there"] {
            assert_eq!(split(&format!("x.exe {}", quote(arg))), [arg]);
        }
    }

    #[test]
    fn a_line_escaped_the_c_runtime_way_is_read_that_way() {
        let escapes = |line: &str| escapes_quotes(&line.encode_utf16().collect::<Vec<_>>());
        // PowerShell 7.3+, .NET ArgumentList, Python subprocess: an argument that holds quotes
        assert!(escapes(r#"setup.exe /VERYSILENT "/DIR=\"D:\My Apps\Tesseract\"""#));
        // a path ending in a backslash, before a blank or the end: Inno's reading stays
        assert!(!escapes(r#""C:\a b\setup.exe" /DIR="C:\Program Files\Tesseract\" /VERYSILENT"#));
        assert!(!escapes(r#"setup.exe /DIR="C:\Program Files\Tesseract\""#));
        assert!(!escapes(r#""C:\Users\user\Tesseract-Setup-0.1.5-win-x64.exe" /SILENT /SUPPRESSMSGBOXES /CLOSEAPPLICATIONS /RESTARTAPPLICATIONS /NORESTART"#));
    }

    #[test]
    fn this_process_facts() {
        assert!(session_id().is_some());
        let me = std::env::current_exe().unwrap();
        assert!(same_file(&image_path(std::process::id()).expect("own image"), &me));
        assert!(same_file(&me, Path::new(&me.to_string_lossy().to_uppercase())), "case does not matter");
        assert!(!same_file(&me, &me.with_file_name("other.exe")));
        assert!(image_path(u32::MAX - 3).is_none());
        assert_eq!(process_elevated(std::process::id()), Some(elevated()));
        assert!(process_elevated(u32::MAX - 3).is_none());
        assert!(own_user().is_some_and(|sid| sid.len() >= 8 && sid[0] == 1), "a SID, revision 1");
        assert_eq!(process_user(std::process::id()), own_user());
        assert!(process_user(u32::MAX - 3).is_none());
        let _ = (in_job(), explorer_elevated());
        // the system drive is a local fixed disk; a share, a missing letter and a relative path are not
        let system = std::env::var("SystemDrive").unwrap_or_else(|_| "C:".into());
        assert!(local_disk(Path::new(&format!(r"{system}\Windows"))));
        assert!(!local_disk(Path::new(r"\\server\share\temp")) && !local_disk(Path::new("temp")));
        let unused = ('D'..='Z').rev().find(|l| unsafe { GetDriveTypeW(&HSTRING::from(format!("{l}:\\"))) } <= 1);
        if let Some(l) = unused { assert!(!local_disk(Path::new(&format!(r"{l}:\temp")))); }
    }

    #[test]
    fn a_set_event_reads_as_quitting() {
        // a name of this test's own: the real shell's event must never be touched from here
        use windows::Win32::System::Threading::{CreateEventW, SetEvent};
        let name = HSTRING::from(format!("Local\\TesseractSetupTest-{}", std::process::id()));
        assert!(!event_set(PCWSTR(name.as_ptr())), "no such event");
        let event = unsafe { CreateEventW(None, true, false, &name) }.unwrap();
        assert!(!event_set(PCWSTR(name.as_ptr())));
        unsafe { SetEvent(event) }.unwrap();
        assert!(event_set(PCWSTR(name.as_ptr())));
        unsafe { let _ = CloseHandle(event); }
    }

    #[test]
    fn breaking_away_starts_outside_a_job_that_allows_it() {
        // Outside a job the flag changes nothing; a job that forbids it makes CreateProcess refuse.
        let whoami = PathBuf::from(std::env::var("SystemRoot").unwrap()).join(r"System32\whoami.exe");
        assert!(open_breakaway(&whoami, &std::env::temp_dir()) || in_job());
    }

    #[test]
    fn explorer_starts_a_program_as_the_desktop_user() {
        // Only where the desktop's Explorer runs (not on a build agent without one).
        let shell = unsafe { windows::Win32::UI::WindowsAndMessaging::GetShellWindow() };
        if shell.0.is_null() { return; }
        let mut explorer = 0u32;
        unsafe { windows::Win32::UI::WindowsAndMessaging::GetWindowThreadProcessId(shell, Some(&mut explorer)) };
        let out = std::env::temp_dir().join(format!("tesseract-setup-explorer-{}.txt", std::process::id()));
        let _ = std::fs::remove_file(&out);
        // A hidden, short PowerShell that writes down its parent: started through Explorer, that is
        // Explorer, not this test.
        let ps = PathBuf::from(std::env::var("SystemRoot").unwrap()).join(r"System32\WindowsPowerShell\v1.0\powershell.exe");
        let args = format!("-NoProfile -NonInteractive -Command \"(Get-CimInstance Win32_Process -Filter ('ProcessId=' + $PID)).ParentProcessId | Set-Content -LiteralPath '{}'\"", out.display());
        assert!(open_as_desktop_user(&ps, &args, &std::env::temp_dir(), 0), "Explorer took the start");
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(60);
        let parent = loop {
            if let Ok(text) = std::fs::read_to_string(&out) { if let Ok(pid) = text.trim().trim_start_matches('\u{feff}').parse::<u32>() { break pid; } }
            assert!(std::time::Instant::now() < deadline, "the program never ran");
            std::thread::sleep(std::time::Duration::from_millis(200));
        };
        let _ = std::fs::remove_file(&out);
        eprintln!("explorer pid {explorer}; the started program's parent {parent}");
        assert_eq!(parent, explorer, "started by Explorer, so with its token and outside this test's job");
    }
}
