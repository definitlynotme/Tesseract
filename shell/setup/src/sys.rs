//! Windows facts and acts the window needs: the existing install, whether the app runs, the
//! WebView2 runtime, free space, the folder picker, launching (elevated or not).
use std::{ffi::OsStr, os::windows::ffi::OsStrExt, path::{Path, PathBuf}};
use windows::{
    core::{w, HSTRING, PCWSTR},
    Win32::{
        Foundation::{CloseHandle, ERROR_CANCELLED, HWND, WAIT_OBJECT_0},
        Globalization::GetUserDefaultUILanguage,
        Graphics::Dwm::{DwmSetWindowAttribute, DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_ROUND},
        Storage::FileSystem::GetDiskFreeSpaceExW,
        System::{
            Com::{CoCreateInstance, CoInitializeEx, CoTaskMemFree, CLSCTX_INPROC_SERVER, COINIT_APARTMENTTHREADED, COINIT_DISABLE_OLE1DDE},
            Registry::{RegGetValueW, HKEY, HKEY_CURRENT_USER, HKEY_LOCAL_MACHINE, REG_ROUTINE_FLAGS, RRF_RT_REG_SZ, RRF_SUBKEY_WOW6432KEY, RRF_SUBKEY_WOW6464KEY},
            Threading::{GetExitCodeProcess, OpenMutexW, WaitForSingleObject, INFINITE, SYNCHRONIZATION_SYNCHRONIZE},
        },
        UI::{
            Shell::{
                FileOpenDialog, IFileOpenDialog, IShellItem, SHCreateItemFromParsingName, ShellExecuteExW, ShellExecuteW,
                FOS_FORCEFILESYSTEM, FOS_PATHMUSTEXIST, FOS_PICKFOLDERS, SEE_MASK_FLAG_NO_UI, SEE_MASK_NOASYNC, SEE_MASK_NOCLOSEPROCESS,
                SHELLEXECUTEINFOW, SIGDN_FILESYSPATH,
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

/// One argument for Inno's command line. Inno reads it with Delphi's ParamStr rules: a double
/// quote toggles quoting and backslashes are literal, so an argument with a space is wrapped
/// whole, and a double quote (never part of a path) is dropped.
pub fn quote(arg: &str) -> String {
    let arg: String = arg.chars().filter(|&c| c != '"').collect();
    if !arg.is_empty() && !arg.chars().any(|c| c == ' ' || c == '\t') { return arg; }
    format!("\"{arg}\"")
}

#[cfg(test)]
mod tests {
    use super::quote;
    #[test]
    fn quoting() {
        assert_eq!(quote("/VERYSILENT"), "/VERYSILENT");
        assert_eq!(quote(r"/DIR=C:\Program Files\Tesseract"), r#""/DIR=C:\Program Files\Tesseract""#);
        assert_eq!(quote(r"/LOG=C:\a b\x.log"), r#""/LOG=C:\a b\x.log""#);
        assert_eq!(quote(r#"a"b"#), "ab");
        assert_eq!(quote(""), r#""""#);
    }
}
