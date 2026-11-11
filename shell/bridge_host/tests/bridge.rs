#![cfg(windows)]
use bridge_host::{Event, Framer, PrivatePipe, MAX_FRAME};
use std::env;
use std::mem::size_of;
use std::ptr::{null, null_mut};
use std::sync::Mutex;
use std::time::{Duration, Instant};
use windows_sys::Win32::Foundation::*;
use windows_sys::Win32::Security::Authorization::*;
use windows_sys::Win32::Security::*;
use windows_sys::Win32::Storage::FileSystem::*;
use windows_sys::Win32::System::Pipes::*;
use windows_sys::Win32::System::Threading::*;

static PROCESS_TEST: Mutex<()> = Mutex::new(());

fn process_test_lock() -> std::sync::MutexGuard<'static, ()> {
    PROCESS_TEST
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
}

struct Sentinel(HANDLE);
impl Sentinel {
    fn create() -> Self {
        unsafe {
            let handle = CreateEventW(null(), 1, 0, null());
            assert!(!handle.is_null());
            assert_ne!(
                SetHandleInformation(handle, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT),
                0
            );
            env::set_var("BRIDGE_TEST_SENTINEL", (handle as usize).to_string());
            Self(handle)
        }
    }
}
impl Drop for Sentinel {
    fn drop(&mut self) {
        unsafe {
            env::remove_var("BRIDGE_TEST_SENTINEL");
            CloseHandle(self.0);
        }
    }
}

#[test]
fn framing_is_bounded_and_utf8_strict() {
    let mut f = Framer::default();
    let mut input = vec![b'a'; MAX_FRAME + 10];
    input.extend_from_slice(b"\n{\"ok\":true}\n\xff\n");
    assert_eq!(
        f.push(&input),
        vec![
            Event::FrameTooLarge,
            Event::Frame("{\"ok\":true}\n".into()),
            Event::InvalidUtf8
        ]
    );
    assert_eq!(f.buffered_len(), 0);
    assert_eq!(f.push(&vec![b'x'; MAX_FRAME - 1]), Vec::<Event>::new());
    let exact = f.push(b"\n");
    assert_eq!(exact.len(), 1);
    assert!(matches!(&exact[0], Event::Frame(s) if s.len() == MAX_FRAME));
}

#[test]
fn private_pipe_launch_and_echo() {
    let _guard = process_test_lock();
    let _sentinel = Sentinel::create();
    unsafe {
        let pipe = PrivatePipe::create().unwrap();
        assert!(pipe.name().starts_with(r"\\.\pipe\tesseract-"));
        assert_dacl_current_user_only(pipe.server_handle());
        assert_pipe_busy(pipe.name());
        let mut host = pipe
            .launch(
                std::path::Path::new(env!("CARGO_BIN_EXE_echo_sidecar")),
                &[],
            )
            .unwrap();
        // Closing the parent's client copy does not invalidate the child's copy.
        assert_ne!(host.child_pid(), GetCurrentProcessId());
        assert_pipe_busy(host.pipe_name());
        host.send(b"{\"hello\":\"\xc3\xa9\"}\n").unwrap();
        let deadline = Instant::now() + Duration::from_secs(10);
        let mut seen = false;
        while Instant::now() < deadline {
            for event in host.poll_event().unwrap() {
                if event == Event::Frame("{\"hello\":\"é\"}\n".into()) {
                    seen = true;
                }
            }
            if seen {
                break;
            }
            std::thread::sleep(Duration::from_millis(10));
        }
        assert!(seen, "echo after parent client close");
        assert_eq!(host.reported_client_pid().unwrap(), GetCurrentProcessId());
        drop(host);
    }
}

#[test]
fn oversized_pipe_frame_recovers_at_next_lf() {
    let _guard = process_test_lock();
    let pipe = PrivatePipe::create().unwrap();
    let mut host = pipe
        .launch(
            std::path::Path::new(env!("CARGO_BIN_EXE_echo_sidecar")),
            &["--oversize-first".into()],
        )
        .unwrap();
    let deadline = Instant::now() + Duration::from_secs(10);
    let (mut oversized, mut recovered) = (false, false);
    while Instant::now() < deadline && !(oversized && recovered) {
        for event in host.poll_event().unwrap() {
            if event == Event::FrameTooLarge {
                oversized = true;
            }
            if event == Event::Frame("{}\n".into()) {
                recovered = true;
            }
        }
        std::thread::sleep(Duration::from_millis(1));
    }
    assert!(
        oversized && recovered,
        "oversize frame must be discarded through LF"
    );
}

#[test]
fn exit_and_pipe_break_are_reported() {
    let _guard = process_test_lock();
    let pipe = PrivatePipe::create().unwrap();
    let mut host = pipe
        .launch(
            std::path::Path::new(env!("CARGO_BIN_EXE_echo_sidecar")),
            &["--exit-immediately".into()],
        )
        .unwrap();
    let deadline = Instant::now() + Duration::from_secs(10);
    let (mut exited, mut broken) = (false, false);
    while Instant::now() < deadline && !(exited && broken) {
        for event in host.poll_event().unwrap() {
            if matches!(event, Event::ChildExited(0)) {
                exited = true;
            }
            if event == Event::Disconnected {
                broken = true;
            }
        }
        std::thread::sleep(Duration::from_millis(10));
    }
    assert!(
        exited && broken,
        "child exit and broken pipe must both be visible"
    );
}

#[test]
fn last_frame_survives_peer_exit() {
    let _guard = process_test_lock();
    let pipe = PrivatePipe::create().unwrap();
    let mut host = pipe
        .launch(
            std::path::Path::new(env!("CARGO_BIN_EXE_echo_sidecar")),
            &["--tail-and-exit".into()],
        )
        .unwrap();
    let deadline = Instant::now() + Duration::from_secs(10);
    let (mut frame, mut exited, mut broken) = (false, false, false);
    while Instant::now() < deadline && !(frame && exited && broken) {
        for event in host.poll_event().unwrap() {
            if event == Event::Frame("{\"tail\":true}\n".into()) {
                frame = true;
            }
            if matches!(event, Event::ChildExited(0)) {
                exited = true;
            }
            if event == Event::Disconnected {
                broken = true;
            }
        }
        std::thread::sleep(Duration::from_millis(10));
    }
    assert!(
        frame && exited && broken,
        "tail frame must precede disconnect without loss"
    );
}

#[test]
fn pending_write_is_single_and_delivered_once_after_read_resumes() {
    let _guard = process_test_lock();
    let pipe = PrivatePipe::create().unwrap();
    let mut host = pipe
        .launch(
            std::path::Path::new(env!("CARGO_BIN_EXE_echo_sidecar")),
            &["--pause-read".into()],
        )
        .unwrap();
    let mut frame = vec![b'x'; MAX_FRAME - 1];
    frame.push(b'\n');
    assert!(host.send(&frame).unwrap(), "first write was submitted");
    assert!(
        !host.poll_write().unwrap(),
        "full pipe must leave a pending write"
    );
    assert!(
        !host.send(b"{\"second\":true}\n").unwrap(),
        "second write must be refused"
    );
    let deadline = Instant::now() + Duration::from_secs(20);
    let (mut completed, mut received) = (false, 0);
    while Instant::now() < deadline && !(completed && received == 1) {
        completed = host.poll_write().unwrap();
        for event in host.poll_event().unwrap() {
            if let Event::Frame(echo) = event {
                assert_eq!(echo.as_bytes(), frame);
                received += 1;
            }
        }
        std::thread::sleep(Duration::from_millis(2));
    }
    assert!(
        completed && received == 1,
        "pending write completes and echoes once"
    );
    let quiet_until = Instant::now() + Duration::from_millis(200);
    while Instant::now() < quiet_until {
        for event in host.poll_event().unwrap() {
            assert!(!matches!(event, Event::Frame(_)), "duplicate echo");
        }
        std::thread::sleep(Duration::from_millis(2));
    }
}

#[test]
fn pending_read_and_write_are_reaped_on_host_drop() {
    if env::var_os("BRIDGE_DROP_WORKER").is_none() {
        let mut child = std::process::Command::new(env::current_exe().unwrap())
            .args(["--exact", "pending_read_and_write_are_reaped_on_host_drop", "--nocapture"])
            .env("BRIDGE_DROP_WORKER", "1").spawn().unwrap();
        let deadline = Instant::now() + Duration::from_secs(20);
        loop {
            if let Some(status) = child.try_wait().unwrap() { assert!(status.success()); return; }
            if Instant::now() > deadline {
                child.kill().unwrap(); child.wait().unwrap(); panic!("Drop external timeout");
            }
            std::thread::sleep(Duration::from_millis(10));
        }
    }
    let _guard = process_test_lock();
    let pipe = PrivatePipe::create().unwrap();
    let mut host = pipe
        .launch(
            std::path::Path::new(env!("CARGO_BIN_EXE_echo_sidecar")),
            &["--pause-read".into()],
        )
        .unwrap();
    assert!(
        host.poll_read().unwrap().is_empty(),
        "read should be pending"
    );
    let mut frame = vec![b'x'; MAX_FRAME - 1];
    frame.push(b'\n');
    assert!(host.send(&frame).unwrap());
    assert!(!host.poll_write().unwrap(), "write should be pending");
    let child = unsafe { OpenProcess(0x0010_0000, 0, host.child_pid()) }; // SYNCHRONIZE
    assert!(!child.is_null());
    let started = Instant::now();
    drop(host);
    assert!(
        started.elapsed() < Duration::from_secs(3),
        "cancel and reap must finish promptly"
    );
    let status = unsafe { WaitForSingleObject(child, 5000) };
    unsafe { CloseHandle(child) };
    assert_eq!(
        status, WAIT_OBJECT_0,
        "sidecar must observe the broken pipe and exit"
    );
}

#[test]
fn child_exit_during_pending_write_is_reported() {
    let _guard = process_test_lock();
    let pipe = PrivatePipe::create().unwrap();
    let mut host = pipe
        .launch(
            std::path::Path::new(env!("CARGO_BIN_EXE_echo_sidecar")),
            &["--pause-then-exit".into()],
        )
        .unwrap();
    let mut frame = vec![b'x'; MAX_FRAME - 1];
    frame.push(b'\n');
    assert!(host.send(&frame).unwrap());
    assert!(
        !host.poll_write().unwrap(),
        "write should be pending before peer exit"
    );
    let deadline = Instant::now() + Duration::from_secs(5);
    let (mut exited, mut broken) = (false, false);
    while Instant::now() < deadline && !(exited && broken) {
        for event in host.poll_event().unwrap() {
            exited |= matches!(event, Event::ChildExited(0));
            broken |= event == Event::Disconnected;
        }
        std::thread::sleep(Duration::from_millis(5));
    }
    assert!(
        exited && broken,
        "child exit and disconnection must both be reported"
    );
    assert!(
        host.poll_write().is_err(),
        "pending write must complete with broken pipe"
    );
    let started = Instant::now();
    drop(host);
    assert!(started.elapsed() < Duration::from_secs(3));
}

#[test]
fn forged_hello_is_rejected_and_child_is_terminated() {
    let _guard = process_test_lock();
    let pipe = PrivatePipe::create().unwrap();
    let pid_file = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("target")
        .join(format!("forged-hello-{}.pid", std::process::id()));
    let _ = std::fs::remove_file(&pid_file);
    let mut server_copy = null_mut();
    unsafe {
        assert_ne!(
            DuplicateHandle(
                GetCurrentProcess(),
                pipe.server_handle(),
                GetCurrentProcess(),
                &mut server_copy,
                0,
                0,
                DUPLICATE_SAME_ACCESS
            ),
            0
        );
    }
    let started = Instant::now();
    let error = pipe
        .launch(
            std::path::Path::new(env!("CARGO_BIN_EXE_echo_sidecar")),
            &[
                "--bad-pid".into(),
                "--pid-file".into(),
                pid_file.to_string_lossy().into_owned(),
            ],
        )
        .err()
        .expect("forged hello must fail launch");
    assert_eq!(error.kind(), std::io::ErrorKind::PermissionDenied);
    assert_eq!(error.to_string(), "identity_mismatch");
    assert!(started.elapsed() < Duration::from_secs(5));
    let pid: u32 = std::fs::read_to_string(&pid_file).unwrap().parse().unwrap();
    std::fs::remove_file(&pid_file).unwrap();
    unsafe {
        let child = OpenProcess(0x0010_0000, 0, pid); // SYNCHRONIZE
        assert!(
            child.is_null(),
            "forged sidecar survived identity rejection"
        );
        assert_eq!(GetLastError(), ERROR_INVALID_PARAMETER, "must not be ACCESS_DENIED");
        let mut available = 0;
        assert_eq!(
            PeekNamedPipe(
                server_copy,
                null_mut(),
                0,
                null_mut(),
                &mut available,
                null_mut()
            ),
            0
        );
        assert!(matches!(
            GetLastError(),
            ERROR_PIPE_NOT_CONNECTED | ERROR_BAD_PIPE
        ));
        CloseHandle(server_copy);
    }
}

fn assert_pipe_busy(name: &str) {
    use std::os::windows::ffi::OsStrExt;
    let wide: Vec<u16> = std::ffi::OsStr::new(name)
        .encode_wide()
        .chain(Some(0))
        .collect();
    unsafe {
        let third = CreateFileW(
            wide.as_ptr(),
            GENERIC_READ | GENERIC_WRITE,
            0,
            null(),
            OPEN_EXISTING,
            FILE_FLAG_OVERLAPPED,
            null_mut(),
        );
        assert_eq!(third, INVALID_HANDLE_VALUE, "third client connected");
        assert!(matches!(
            GetLastError(),
            ERROR_PIPE_BUSY | ERROR_ACCESS_DENIED
        ));
    }
}

unsafe fn assert_dacl_current_user_only(pipe: HANDLE) {
    let mut dacl = null_mut();
    let mut descriptor = null_mut();
    let status = GetSecurityInfo(
        pipe,
        SE_KERNEL_OBJECT,
        DACL_SECURITY_INFORMATION,
        null_mut(),
        null_mut(),
        &mut dacl,
        null_mut(),
        &mut descriptor,
    );
    assert_eq!(status, 0);
    assert!(!dacl.is_null());
    let acl = &*dacl;
    assert_eq!(acl.AceCount, 1);
    let mut ace = null_mut();
    assert_ne!(GetAce(dacl, 0, &mut ace), 0);
    let allowed = &*(ace as *const ACCESS_ALLOWED_ACE);
    assert_eq!(allowed.Header.AceType, 0); // ACCESS_ALLOWED_ACE_TYPE
    let ace_sid = &allowed.SidStart as *const u32 as *mut _;
    let mut token = null_mut();
    assert_ne!(
        OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &mut token),
        0
    );
    let mut len = 0;
    GetTokenInformation(token, TokenUser, null_mut(), 0, &mut len);
    let mut user = vec![0usize; (len as usize).div_ceil(size_of::<usize>())];
    assert_ne!(
        GetTokenInformation(token, TokenUser, user.as_mut_ptr().cast(), len, &mut len),
        0
    );
    let sid = (*(user.as_ptr() as *const TOKEN_USER)).User.Sid;
    assert_ne!(EqualSid(ace_sid, sid), 0);
    CloseHandle(token);
    LocalFree(descriptor);
}
