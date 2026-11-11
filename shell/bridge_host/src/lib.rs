#![cfg(windows)]

use std::collections::VecDeque;
use std::ffi::{c_void, OsStr};
use std::io;
use std::mem::{size_of, zeroed};
use std::os::windows::ffi::OsStrExt;
use std::path::Path;
use std::ptr::{null, null_mut};
use std::time::{Duration, Instant};
use windows_sys::Win32::Foundation::*;
use windows_sys::Win32::Security::Authorization::*;
use windows_sys::Win32::Security::Cryptography::*;
use windows_sys::Win32::Security::*;
use windows_sys::Win32::Storage::FileSystem::*;
use windows_sys::Win32::System::Pipes::*;
use windows_sys::Win32::System::Threading::*;
use windows_sys::Win32::System::IO::*;

pub mod scope;

pub const MAX_FRAME: usize = 4 * 1024 * 1024;
const READ_CHUNK: usize = 64 * 1024;
const HELLO_TIMEOUT: Duration = Duration::from_secs(10);

fn wide(s: &OsStr) -> Vec<u16> {
    s.encode_wide().chain(Some(0)).collect()
}
fn error() -> io::Error {
    io::Error::last_os_error()
}
fn invalid(msg: &'static str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, msg)
}

#[derive(Debug)]
struct OwnedHandle(HANDLE);
impl OwnedHandle {
    fn new(h: HANDLE) -> io::Result<Self> {
        if h.is_null() || h == INVALID_HANDLE_VALUE {
            Err(error())
        } else {
            Ok(Self(h))
        }
    }
    fn raw(&self) -> HANDLE {
        self.0
    }
}
impl Drop for OwnedHandle {
    fn drop(&mut self) {
        unsafe {
            CloseHandle(self.0);
        }
    }
}

fn event() -> io::Result<OwnedHandle> {
    unsafe { OwnedHandle::new(CreateEventW(null(), 1, 0, null())) }
}

/// Returns an SDDL string solely for constructing a protected, one-ACE DACL.
fn current_user_sddl() -> io::Result<Vec<u16>> {
    unsafe {
        let mut token = null_mut();
        if OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &mut token) == 0 {
            return Err(error());
        }
        let token = OwnedHandle::new(token)?;
        let mut len = 0;
        GetTokenInformation(token.raw(), TokenUser, null_mut(), 0, &mut len);
        if len == 0 {
            return Err(error());
        }
        let mut user = vec![0usize; (len as usize).div_ceil(size_of::<usize>())];
        if GetTokenInformation(
            token.raw(),
            TokenUser,
            user.as_mut_ptr().cast(),
            len,
            &mut len,
        ) == 0
        {
            return Err(error());
        }
        let sid = (*(user.as_ptr() as *const TOKEN_USER)).User.Sid;
        let mut sid_string = null_mut();
        if ConvertSidToStringSidW(sid, &mut sid_string) == 0 {
            return Err(error());
        }
        let mut count = 0;
        while *sid_string.add(count) != 0 {
            count += 1;
        }
        let sid_text = String::from_utf16_lossy(std::slice::from_raw_parts(sid_string, count));
        LocalFree(sid_string.cast());
        Ok(wide(OsStr::new(&format!("D:P(A;;GA;;;{sid_text})"))))
    }
}

pub struct PrivatePipe {
    server: OwnedHandle,
    client: Option<OwnedHandle>,
    name: String,
}

impl PrivatePipe {
    pub fn create() -> io::Result<Self> {
        let mut random = [0u8; 16];
        unsafe {
            let status = BCryptGenRandom(
                null_mut(),
                random.as_mut_ptr(),
                random.len() as u32,
                BCRYPT_USE_SYSTEM_PREFERRED_RNG,
            );
            if status != 0 {
                return Err(io::Error::other(format!("BCryptGenRandom: {status:#x}")));
            }
        }
        let name = format!(
            r"\\.\pipe\tesseract-{}",
            random
                .iter()
                .map(|b| format!("{b:02x}"))
                .collect::<String>()
        );
        let name_w = wide(OsStr::new(&name));
        let sddl = current_user_sddl()?;
        unsafe {
            let mut descriptor = null_mut();
            if ConvertStringSecurityDescriptorToSecurityDescriptorW(
                sddl.as_ptr(),
                SDDL_REVISION_1,
                &mut descriptor,
                null_mut(),
            ) == 0
            {
                return Err(error());
            }
            struct Descriptor(*mut c_void);
            impl Drop for Descriptor {
                fn drop(&mut self) {
                    unsafe {
                        LocalFree(self.0);
                    }
                }
            }
            let descriptor = Descriptor(descriptor);
            let attr = SECURITY_ATTRIBUTES {
                nLength: size_of::<SECURITY_ATTRIBUTES>() as u32,
                lpSecurityDescriptor: descriptor.0,
                bInheritHandle: 0,
            };
            let server = OwnedHandle::new(CreateNamedPipeW(
                name_w.as_ptr(),
                PIPE_ACCESS_DUPLEX | FILE_FLAG_FIRST_PIPE_INSTANCE | FILE_FLAG_OVERLAPPED,
                PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS,
                1,
                65536,
                65536,
                0,
                &attr,
            ))?;
            let client = OwnedHandle::new(CreateFileW(
                name_w.as_ptr(),
                GENERIC_READ | GENERIC_WRITE,
                0,
                null(),
                OPEN_EXISTING,
                FILE_FLAG_OVERLAPPED,
                null_mut(),
            ))?;
            // The shell occupies the only instance before any sidecar is started.
            if ConnectNamedPipe(server.raw(), null_mut()) != 0
                || GetLastError() != ERROR_PIPE_CONNECTED
            {
                return Err(error());
            }
            if SetHandleInformation(client.raw(), HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT) == 0 {
                return Err(error());
            }
            Ok(Self {
                server,
                client: Some(client),
                name,
            })
        }
    }

    pub fn name(&self) -> &str {
        &self.name
    }
    pub fn server_handle(&self) -> HANDLE {
        self.server.raw()
    }
    pub fn client_handle(&self) -> Option<HANDLE> {
        self.client.as_ref().map(OwnedHandle::raw)
    }

    /// Consumes the local client copy immediately after successful CreateProcessW.
    pub fn launch(mut self, exe: &Path, args: &[String]) -> io::Result<BridgeHost> {
        let client = self
            .client
            .as_ref()
            .ok_or_else(|| invalid("client already transferred"))?
            .raw();
        let read = PendingIo::new()?;
        let write = PendingIo::new()?;
        let mut attr_size = 0usize;
        unsafe {
            InitializeProcThreadAttributeList(null_mut(), 1, 0, &mut attr_size);
        }
        if attr_size == 0 {
            return Err(error());
        }
        let mut attr_buf = vec![0usize; attr_size.div_ceil(size_of::<usize>())];
        let attr = attr_buf.as_mut_ptr() as LPPROC_THREAD_ATTRIBUTE_LIST;
        unsafe {
            if InitializeProcThreadAttributeList(attr, 1, 0, &mut attr_size) == 0 {
                return Err(error());
            }
            struct AttributeGuard(LPPROC_THREAD_ATTRIBUTE_LIST);
            impl Drop for AttributeGuard {
                fn drop(&mut self) {
                    unsafe {
                        DeleteProcThreadAttributeList(self.0);
                    }
                }
            }
            let _guard = AttributeGuard(attr);
            let mut handles = [client];
            if UpdateProcThreadAttribute(
                attr,
                0,
                PROC_THREAD_ATTRIBUTE_HANDLE_LIST as usize,
                handles.as_mut_ptr().cast(),
                size_of::<HANDLE>(),
                null_mut(),
                null_mut(),
            ) == 0
            {
                return Err(error());
            }
            let mut cmd = quote(exe.as_os_str());
            for arg in args {
                cmd.push(' ');
                cmd.push_str(&quote(OsStr::new(arg)));
            }
            cmd.push_str(&format!(
                " --ipc-handle {} --shell-pid {}",
                client as usize,
                GetCurrentProcessId()
            ));
            let mut cmd_w = wide(OsStr::new(&cmd));
            let exe_w = wide(exe.as_os_str());
            let mut startup: STARTUPINFOEXW = zeroed();
            startup.StartupInfo.cb = size_of::<STARTUPINFOEXW>() as u32;
            startup.lpAttributeList = attr;
            let mut info: PROCESS_INFORMATION = zeroed();
            let ok = CreateProcessW(
                exe_w.as_ptr(),
                cmd_w.as_mut_ptr(),
                null(),
                null(),
                1,
                EXTENDED_STARTUPINFO_PRESENT | CREATE_NO_WINDOW,
                null(),
                null(),
                &startup.StartupInfo,
                &mut info,
            );
            if ok == 0 {
                return Err(error());
            }
            self.client.take(); // Close our copy; only the child retains the client handle.
            let process = OwnedHandle::new(info.hProcess)?;
            let thread = OwnedHandle::new(info.hThread)?;
            drop(thread);
            let host = BridgeHost {
                pipe: self,
                child: process,
                child_pid: info.dwProcessId,
                read,
                write,
                framer: Framer::default(),
                disconnected: false,
                child_exited: false,
                queued: VecDeque::new(),
                hello: serde_json::Value::Null,
            };
            let mut host = host;
            if let Err(e) = host.verify_hello() {
                // A failed identity handoff must not leave a live sidecar with
                // access to the inherited client handle.
                host.read.cancel(host.pipe.server.raw());
                DisconnectNamedPipe(host.pipe.server.raw());
                TerminateProcess(host.child.raw(), 1);
                WaitForSingleObject(host.child.raw(), 5000);
                return Err(e);
            }
            Ok(host)
        }
    }
}

// Windows CommandLineToArgvW escaping, including trailing backslashes before a quote.
fn quote(value: &OsStr) -> String {
    let s = value.to_string_lossy();
    let mut out = String::from("\"");
    let mut slashes = 0;
    for c in s.chars() {
        if c == '\\' {
            slashes += 1;
            continue;
        }
        if c == '"' {
            out.push_str(&"\\".repeat(slashes * 2 + 1));
        } else {
            out.push_str(&"\\".repeat(slashes));
        }
        slashes = 0;
        out.push(c);
    }
    out.push_str(&"\\".repeat(slashes * 2));
    out.push('"');
    out
}

struct PendingIo {
    overlapped: Box<OVERLAPPED>,
    event: OwnedHandle,
    buffer: Vec<u8>,
    active: bool,
    completed: Option<usize>,
}
impl PendingIo {
    fn new() -> io::Result<Self> {
        let event = event()?;
        let mut overlapped: OVERLAPPED = unsafe { zeroed() };
        overlapped.hEvent = event.raw();
        Ok(Self {
            overlapped: Box::new(overlapped),
            event,
            buffer: Vec::new(),
            active: false,
            completed: None,
        })
    }
    fn reset(&mut self) {
        *self.overlapped = unsafe { zeroed() };
        self.overlapped.hEvent = self.event.raw();
        self.completed = None;
        unsafe {
            ResetEvent(self.event.raw());
        }
    }
    fn result(&mut self, pipe: HANDLE) -> io::Result<Option<usize>> {
        if let Some(n) = self.completed.take() {
            return Ok(Some(n));
        }
        let mut n = 0;
        unsafe {
            if GetOverlappedResult(pipe, &mut *self.overlapped, &mut n, 0) != 0 {
                self.active = false;
                return Ok(Some(n as usize));
            }
            let e = error();
            if e.raw_os_error() == Some(ERROR_IO_INCOMPLETE as i32) {
                Ok(None)
            } else {
                self.active = false;
                Err(e)
            }
        }
    }
    fn cancel(&mut self, pipe: HANDLE) {
        if self.active {
            unsafe {
                CancelIoEx(pipe, &*self.overlapped);
                // ERROR_NOT_FOUND may mean completion raced with cancellation.
                // Reap either outcome before freeing the OVERLAPPED or buffer.
                let mut n = 0;
                GetOverlappedResult(pipe, &mut *self.overlapped, &mut n, 1);
            }
            self.active = false;
        }
    }
}

#[derive(Debug, PartialEq, Eq)]
pub enum Event {
    Frame(String),
    FrameTooLarge,
    InvalidUtf8,
    Disconnected,
    ChildExited(u32),
}

#[derive(Default)]
pub struct Framer {
    bytes: Vec<u8>,
    discarding: bool,
}
impl Framer {
    pub fn push(&mut self, bytes: &[u8]) -> Vec<Event> {
        let mut events = Vec::new();
        for &b in bytes {
            if self.discarding {
                if b == b'\n' {
                    self.discarding = false;
                }
                continue;
            }
            if self.bytes.len() == MAX_FRAME {
                self.bytes.clear();
                self.discarding = b != b'\n';
                events.push(Event::FrameTooLarge);
                continue;
            }
            self.bytes.push(b);
            if b == b'\n' {
                match String::from_utf8(std::mem::take(&mut self.bytes)) {
                    Ok(line) => events.push(Event::Frame(line)),
                    Err(_) => events.push(Event::InvalidUtf8),
                }
            }
        }
        events
    }
    pub fn buffered_len(&self) -> usize {
        self.bytes.len()
    }
}

pub struct BridgeHost {
    pipe: PrivatePipe,
    child: OwnedHandle,
    child_pid: u32,
    read: PendingIo,
    write: PendingIo,
    framer: Framer,
    disconnected: bool,
    child_exited: bool,
    queued: VecDeque<Event>,
    hello: serde_json::Value,
}
impl BridgeHost {
    pub fn hello(&self) -> &serde_json::Value { &self.hello }
    pub fn child_pid(&self) -> u32 {
        self.child_pid
    }
    pub fn pipe_name(&self) -> &str {
        self.pipe.name()
    }
    pub fn reported_client_pid(&self) -> io::Result<u32> {
        let mut pid = 0;
        unsafe {
            if GetNamedPipeClientProcessId(self.pipe.server.raw(), &mut pid) == 0 {
                Err(error())
            } else {
                Ok(pid)
            }
        }
    }
    fn verify_hello(&mut self) -> io::Result<()> {
        let deadline = Instant::now() + HELLO_TIMEOUT;
        loop {
            let mut accepted = false;
            for (index, event) in self.poll_read()?.into_iter().enumerate() {
                if index == 0 {
                    let valid = match &event {
                        Event::Frame(line) => serde_json::from_str::<serde_json::Value>(line)
                            .ok()
                            .is_some_and(|hello| {
                                hello.get("t").and_then(|v| v.as_str()) == Some("hello")
                                    && hello.get("runtime_pid").and_then(|v| v.as_u64())
                                        == Some(self.child_pid as u64)
                            }),
                        _ => false,
                    };
                    if !valid {
                        return Err(io::Error::new(
                            io::ErrorKind::PermissionDenied,
                            "identity_mismatch",
                        ));
                    }
                    if let Event::Frame(ref line) = event {
                        self.hello = serde_json::from_str(line).map_err(io::Error::other)?;
                    }
                    accepted = true;
                } else {
                    self.queued.push_back(event);
                }
            }
            if accepted {
                return Ok(());
            }
            if Instant::now() >= deadline {
                return Err(io::Error::new(io::ErrorKind::TimedOut, "hello_timeout"));
            }
            std::thread::sleep(Duration::from_millis(2));
        }
    }
    pub fn start_read(&mut self) -> io::Result<()> {
        if self.read.active || self.read.completed.is_some() {
            return Err(io::Error::new(io::ErrorKind::WouldBlock, "read pending"));
        }
        self.read.reset();
        self.read.buffer.resize(READ_CHUNK, 0);
        let mut n = 0;
        unsafe {
            if ReadFile(
                self.pipe.server.raw(),
                self.read.buffer.as_mut_ptr().cast(),
                READ_CHUNK as u32,
                &mut n,
                &mut *self.read.overlapped,
            ) != 0
            {
                self.read.completed = Some(n as usize);
                return Ok(());
            }
            let e = error();
            if e.raw_os_error() == Some(ERROR_IO_PENDING as i32) {
                self.read.active = true;
                Ok(())
            } else {
                Err(e)
            }
        }
    }
    pub fn poll_read(&mut self) -> io::Result<Vec<Event>> {
        if !self.read.active && self.read.completed.is_none() {
            if let Err(e) = self.start_read() {
                if pipe_error(&e) {
                    self.disconnected = true;
                    return Ok(vec![Event::Disconnected]);
                }
                return Err(e);
            }
        }
        match self.read.result(self.pipe.server.raw()) {
            Ok(None) => Ok(Vec::new()),
            Ok(Some(0)) => {
                self.disconnected = true;
                Ok(vec![Event::Disconnected])
            }
            Ok(Some(n)) => {
                let mut events = self.framer.push(&self.read.buffer[..n]);
                if let Err(e) = self.start_read() {
                    if pipe_error(&e) {
                        self.disconnected = true;
                        events.push(Event::Disconnected);
                    } else {
                        return Err(e);
                    }
                }
                Ok(events)
            }
            Err(e) if pipe_error(&e) => {
                self.disconnected = true;
                Ok(vec![Event::Disconnected])
            }
            Err(e) => Err(e),
        }
    }
    /// At most one submitted write. A pending write keeps its buffer alive.
    pub fn send(&mut self, line: &[u8]) -> io::Result<bool> {
        if self.write.active {
            return Ok(false);
        }
        if line.len() > MAX_FRAME
            || line.last() != Some(&b'\n')
            || line[..line.len() - 1].contains(&b'\n')
            || std::str::from_utf8(line).is_err()
        {
            return Err(invalid("invalid NDJSON frame"));
        }
        self.write.reset();
        self.write.buffer = line.to_vec();
        let mut n = 0;
        unsafe {
            if WriteFile(
                self.pipe.server.raw(),
                self.write.buffer.as_ptr().cast(),
                line.len() as u32,
                &mut n,
                &mut *self.write.overlapped,
            ) != 0
            {
                if n != line.len() as u32 {
                    return Err(io::Error::new(
                        io::ErrorKind::WriteZero,
                        "partial pipe write",
                    ));
                }
                return Ok(true);
            }
            let e = error();
            if e.raw_os_error() == Some(ERROR_IO_PENDING as i32) {
                self.write.active = true;
                Ok(true)
            } else {
                Err(e)
            }
        }
    }
    pub fn poll_write(&mut self) -> io::Result<bool> {
        if !self.write.active {
            return Ok(true);
        }
        match self.write.result(self.pipe.server.raw()) {
            Ok(None) => Ok(false),
            Ok(Some(n)) if n == self.write.buffer.len() => Ok(true),
            Ok(Some(_)) => Err(io::Error::new(
                io::ErrorKind::WriteZero,
                "partial pipe write",
            )),
            Err(e) => Err(e),
        }
    }
    pub fn poll_event(&mut self) -> io::Result<Vec<Event>> {
        let mut events: Vec<Event> = self.queued.drain(..).collect();
        if !self.child_exited {
            let status = unsafe { WaitForSingleObject(self.child.raw(), 0) };
            if status == WAIT_OBJECT_0 {
                let mut code = 0;
                if unsafe { GetExitCodeProcess(self.child.raw(), &mut code) } == 0 {
                    return Err(error());
                }
                self.child_exited = true;
                events.push(Event::ChildExited(code));
            } else if status == WAIT_FAILED {
                return Err(error());
            }
        }
        if !self.disconnected {
            events.extend(self.poll_read()?);
        }
        Ok(events)
    }
}
impl Drop for BridgeHost {
    fn drop(&mut self) {
        self.read.cancel(self.pipe.server.raw());
        self.write.cancel(self.pipe.server.raw());
        // Dropping pipe and process handles does not kill the sidecar.
    }
}
fn pipe_error(e: &io::Error) -> bool {
    matches!(e.raw_os_error(), Some(109 | 232 | 233 | 995))
}
