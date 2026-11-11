//! T-P3-06 Win32 effects of the page host commands (host.rs `Desktop`): the IFileSaveDialog, the
//! approved target file, ShellExecute, `explorer /select,`, the known Desktop folder, local time and
//! the opaque save ref.
use crate::{dialog, host::{Desktop, DialogSpec, Format, Purpose, Stamp, Target}, native_text};
use std::{io, os::windows::{io::FromRawHandle, process::CommandExt}, path::{Path, PathBuf}};
use windows::core::{HSTRING, PCWSTR};
use windows::Win32::Foundation::HWND;

pub struct Win32Desktop;
fn title_id(purpose: Purpose) -> &'static str { match purpose { Purpose::Bundle => "save_bundle_title", Purpose::Filtered => "save_logs_title" } }
fn filter_id(format: Format) -> &'static str {
    match format { Format::Zip => "filter_zip", Format::Json => "filter_json", Format::Text => "filter_text", Format::Csv => "filter_csv" }
}
fn shell_execute(target: &HSTRING) -> io::Result<()> {
    use windows::Win32::UI::{Shell::ShellExecuteW, WindowsAndMessaging::SW_SHOWNORMAL};
    let result = unsafe { ShellExecuteW(None, windows::core::w!("open"), target, PCWSTR::null(), PCWSTR::null(), SW_SHOWNORMAL) };
    if result.0 as isize > 32 { Ok(()) } else { Err(io::Error::other("shell_execute_failed")) }
}
impl Desktop for Win32Desktop {
    fn save_dialog(&self, owner: isize, spec: &DialogSpec) -> io::Result<Option<PathBuf>> {
        use windows::Win32::{Foundation::ERROR_CANCELLED, System::Com::*, UI::Shell::{Common::COMDLG_FILTERSPEC, *}};
        // The dialog needs its own STA; this request thread is ours alone.
        let initialized = unsafe { CoInitializeEx(None, COINIT_APARTMENTTHREADED | COINIT_DISABLE_OLE1DDE) }.is_ok();
        let result = (|| -> windows::core::Result<Option<PathBuf>> {
            unsafe {
                let dialog: IFileSaveDialog = CoCreateInstance(&FileSaveDialog, None, CLSCTX_INPROC_SERVER)?;
                let label = HSTRING::from(native_text::text(filter_id(spec.format), spec.locale));
                let pattern = HSTRING::from(format!("*.{}", spec.format.extension()));
                dialog.SetFileTypes(&[COMDLG_FILTERSPEC { pszName: PCWSTR(label.as_ptr()), pszSpec: PCWSTR(pattern.as_ptr()) }])?;
                dialog.SetFileTypeIndex(1)?;
                dialog.SetDefaultExtension(&HSTRING::from(spec.format.extension()))?;
                dialog.SetFileName(&HSTRING::from(spec.file_name.as_str()))?;
                dialog.SetTitle(&HSTRING::from(native_text::text(title_id(spec.purpose), spec.locale)))?;
                let options = dialog.GetOptions()?;
                dialog.SetOptions(options | FOS_OVERWRITEPROMPT | FOS_STRICTFILETYPES | FOS_FORCEFILESYSTEM | FOS_NOREADONLYRETURN
                    | FOS_PATHMUSTEXIST | FOS_NOCHANGEDIR | FOS_DONTADDTORECENT)?;
                if let Some(folder) = &spec.folder {
                    if let Ok(item) = SHCreateItemFromParsingName::<_, _, IShellItem>(&HSTRING::from(folder.as_os_str()), None::<&IBindCtx>) { let _ = dialog.SetDefaultFolder(&item); }
                }
                let owner = (owner != 0).then_some(HWND(owner as _));
                match dialog.Show(owner) {
                    Ok(()) => {},
                    Err(e) if e.code() == ERROR_CANCELLED.to_hresult() => return Ok(None),
                    Err(e) => return Err(e),
                }
                let raw = dialog.GetResult()?.GetDisplayName(SIGDN_FILESYSPATH)?;
                let path = raw.to_string(); CoTaskMemFree(Some(raw.0 as _));
                Ok(Some(PathBuf::from(path.map_err(|_| windows::core::Error::from(windows::Win32::Foundation::E_FAIL))?)))
            }
        })();
        if initialized { unsafe { CoUninitialize(); } }
        result.map_err(io::Error::other)
    }
    fn create_target(&self, path: &Path) -> io::Result<Target> {
        use windows::Win32::{Foundation::{CloseHandle, GetLastError, ERROR_ALREADY_EXISTS, GENERIC_WRITE},
            Storage::FileSystem::{CreateFileW, GetFileType, DELETE, FILE_ATTRIBUTE_NORMAL, FILE_READ_ATTRIBUTES, FILE_SHARE_READ, FILE_TYPE_DISK, OPEN_ALWAYS}};
        let wide = HSTRING::from(path.as_os_str());
        unsafe {
            // Overwrite was confirmed in the dialog, but nothing is truncated here (OPEN_ALWAYS): the
            // writer truncates once it really writes, so a refused or late grant leaves an existing
            // file intact (review S5/S2). DELETE lets a created file be removed again on failure.
            let handle = CreateFileW(&wide, GENERIC_WRITE.0 | DELETE.0 | FILE_READ_ATTRIBUTES.0, FILE_SHARE_READ, None, OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, None)?;
            let created = GetLastError() != ERROR_ALREADY_EXISTS;
            if GetFileType(handle) != FILE_TYPE_DISK { let _ = CloseHandle(handle); return Err(io::Error::other("not_a_disk_file")); }
            Ok(Target { raw: handle.0 as isize, created, owned: Some(std::os::windows::io::OwnedHandle::from_raw_handle(handle.0)) })
        }
    }
    fn discard(&self, target: Target, path: &Path) {
        use windows::Win32::{Foundation::HANDLE, Storage::FileSystem::{FileDispositionInfo, SetFileInformationByHandle, FILE_DISPOSITION_INFO}};
        if !target.created { return drop(target); }
        // Delete-on-close through our own handle: the file goes with the last handle to it, also a
        // duplicate the sidecar may still hold (a grant registered after the shell gave up). A
        // path-based delete would fail on that open duplicate (no FILE_SHARE_DELETE) and leave an
        // empty file behind.
        let info = FILE_DISPOSITION_INFO { DeleteFile: true };
        let marked = unsafe { SetFileInformationByHandle(HANDLE(target.raw as _), FileDispositionInfo, &info as *const _ as *const _, std::mem::size_of::<FILE_DISPOSITION_INFO>() as u32) }.is_ok();
        drop(target);
        if !marked { let _ = std::fs::remove_file(path); }
    }
    fn confirm_url(&self, owner: isize, host: &str, locale: &'static str) -> bool { dialog::confirm_url(locale, host, owner) }
    fn open_url(&self, url: &str) -> io::Result<()> { shell_execute(&HSTRING::from(url)) }
    fn open_folder(&self, dir: &Path) -> io::Result<()> { shell_execute(&HSTRING::from(dir.as_os_str())) }
    fn reveal(&self, file: &Path) -> io::Result<()> {
        let windir = std::env::var_os("SystemRoot").map(PathBuf::from).unwrap_or_else(|| PathBuf::from(r"C:\Windows"));
        // A Windows path cannot contain '"': quoting cannot be broken out of.
        std::process::Command::new(windir.join("explorer.exe")).raw_arg(format!("/select,\"{}\"", file.display())).spawn().map(|_| ())
    }
    fn desktop_dir(&self) -> Option<PathBuf> {
        use windows::Win32::{System::Com::CoTaskMemFree, UI::Shell::{FOLDERID_Desktop, SHGetKnownFolderPath, KF_FLAG_DEFAULT}};
        unsafe {
            let raw = SHGetKnownFolderPath(&FOLDERID_Desktop, KF_FLAG_DEFAULT, None).ok()?;
            let path = raw.to_string().ok(); CoTaskMemFree(Some(raw.0 as _));
            path.map(PathBuf::from)
        }
    }
    fn local_time(&self) -> Stamp {
        let t = unsafe { windows::Win32::System::SystemInformation::GetLocalTime() };
        (t.wYear, t.wMonth, t.wDay, t.wHour, t.wMinute, t.wSecond)
    }
    fn random_ref(&self) -> String {
        use windows::Win32::Security::Cryptography::{BCryptGenRandom, BCRYPT_USE_SYSTEM_PREFERRED_RNG};
        let mut bytes = [0u8; 16];
        let status = unsafe { BCryptGenRandom(None, &mut bytes, BCRYPT_USE_SYSTEM_PREFERRED_RNG) };
        if status.is_err() {
            // Never a predictable ref: fall back to the OS-seeded hasher keys.
            use std::hash::{BuildHasher, RandomState};
            for (i, chunk) in bytes.chunks_mut(8).enumerate() { chunk.copy_from_slice(&RandomState::new().hash_one(i).to_le_bytes()); }
        }
        format!("save-{}", bytes.iter().map(|b| format!("{b:02x}")).collect::<String>())
    }
}

#[cfg(test)] mod tests {
    use super::*;
    #[test] fn refs_are_random_and_targets_are_disk_files_created_once() {
        let a = Win32Desktop.random_ref(); let b = Win32Desktop.random_ref();
        assert!(a.starts_with("save-") && a.len() == 37 && a != b);
        let dir = crate::update::tests::temp_dir("target");
        let path = dir.0.join("out.zip");
        let target = Win32Desktop.create_target(&path).unwrap();
        assert!(target.created && path.is_file());
        assert!(std::fs::OpenOptions::new().write(true).open(&path).is_err(), "no second writer while granted");
        Win32Desktop.discard(target, &path);
        assert!(!path.exists(), "a file this grant created is removed on failure");
        // A duplicate still open elsewhere (the sidecar after a late registration): the created file
        // is gone with that last handle, never left behind empty.
        let target = Win32Desktop.create_target(&path).unwrap();
        let duplicate = duplicate_here(target.raw);
        Win32Desktop.discard(target, &path);
        drop(duplicate);
        assert!(!path.exists(), "deleted on the last close");
        std::fs::write(&path, b"old content").unwrap();
        let target = Win32Desktop.create_target(&path).unwrap();
        assert!(!target.created, "an existing (overwrite-confirmed) file");
        assert_eq!(std::fs::metadata(&path).unwrap().len(), 11, "not truncated by the grant");
        Win32Desktop.discard(target, &path);
        assert_eq!(std::fs::read(&path).unwrap(), b"old content", "an existing file is left as it was");
        assert!(Win32Desktop.create_target(Path::new(r"\\.\NUL")).is_err(), "devices are refused");
        let (y, m, d, ..) = Win32Desktop.local_time(); assert!(y >= 2024 && (1..=12).contains(&m) && (1..=31).contains(&d));
        assert!(Win32Desktop.desktop_dir().is_some());
    }
    /// DuplicateHandle within this process (stands in for the copy in the sidecar).
    fn duplicate_here(raw: isize) -> std::os::windows::io::OwnedHandle {
        use windows::Win32::{Foundation::{DuplicateHandle, DUPLICATE_SAME_ACCESS, HANDLE}, System::Threading::GetCurrentProcess};
        let mut out = HANDLE::default();
        unsafe {
            DuplicateHandle(GetCurrentProcess(), HANDLE(raw as _), GetCurrentProcess(), &mut out, 0, false, DUPLICATE_SAME_ACCESS).unwrap();
            std::os::windows::io::OwnedHandle::from_raw_handle(out.0)
        }
    }
}
