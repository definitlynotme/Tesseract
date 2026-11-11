use std::env;
use std::mem::zeroed;
use std::ptr::null;
use std::time::Duration;
use windows_sys::Win32::Foundation::*;
use windows_sys::Win32::Storage::FileSystem::*;
use windows_sys::Win32::System::Threading::*;
use windows_sys::Win32::System::IO::*;

unsafe fn write_chunk(pipe: HANDLE, event: HANDLE, bytes: &[u8]) -> bool {
    let mut op: OVERLAPPED = zeroed();
    op.hEvent = event;
    ResetEvent(event);
    let mut sent = 0;
    if WriteFile(
        pipe,
        bytes.as_ptr().cast(),
        bytes.len() as u32,
        &mut sent,
        &mut op,
    ) == 0
    {
        if GetLastError() != ERROR_IO_PENDING {
            return false;
        }
        if GetOverlappedResult(pipe, &mut op, &mut sent, 1) == 0 {
            return false;
        }
    }
    sent == bytes.len() as u32
}

fn main() {
    let args: Vec<String> = env::args().collect();
    let value = args
        .windows(2)
        .find(|v| v[0] == "--ipc-handle")
        .expect("ipc handle")[1]
        .parse::<usize>()
        .unwrap();
    let shell_pid = args
        .windows(2)
        .find(|v| v[0] == "--shell-pid")
        .expect("shell pid")[1]
        .parse::<u32>()
        .unwrap();
    assert!(shell_pid > 0);
    let pipe = value as HANDLE;
    unsafe {
        let mut flags = 0;
        assert_ne!(GetHandleInformation(pipe, &mut flags), 0);
        assert_ne!(SetHandleInformation(pipe, HANDLE_FLAG_INHERIT, 0), 0);
        if let Ok(sentinel) = env::var("BRIDGE_TEST_SENTINEL") {
            let mut ignored = 0;
            assert_eq!(
                GetHandleInformation(sentinel.parse::<usize>().unwrap() as HANDLE, &mut ignored),
                0,
                "extra inherited handle"
            );
        }
        let mut peer = 0;
        assert_ne!(
            windows_sys::Win32::System::Pipes::GetNamedPipeServerProcessId(pipe, &mut peer),
            0
        );
        assert_eq!(peer, shell_pid);
        let event = CreateEventW(null(), 1, 0, null());
        assert!(!event.is_null());
        if let Some(position) = args.iter().position(|arg| arg == "--pid-file") {
            std::fs::write(&args[position + 1], GetCurrentProcessId().to_string()).unwrap();
        }
        let runtime_pid = if args.iter().any(|arg| arg == "--bad-pid") {
            0
        } else {
            GetCurrentProcessId()
        };
        let hello = format!("{{\"t\":\"hello\",\"runtime_pid\":{runtime_pid}}}\n");
        assert!(write_chunk(pipe, event, hello.as_bytes()));
        if args.iter().any(|arg| arg == "--exit-immediately") {
            CloseHandle(event);
            CloseHandle(pipe);
            return;
        }
        if args.iter().any(|arg| arg == "--bad-pid") {
            // Keep the inherited handle live until the shell rejects the hello.
            std::thread::sleep(Duration::from_secs(30));
            CloseHandle(event);
            CloseHandle(pipe);
            return;
        }
        if args.iter().any(|arg| arg == "--tail-and-exit") {
            assert!(write_chunk(pipe, event, b"{\"tail\":true}\n"));
            CloseHandle(event);
            CloseHandle(pipe);
            return;
        }
        if args.iter().any(|arg| arg == "--oversize-first") {
            let chunk = vec![b'x'; 65536];
            for _ in 0..64 {
                assert!(write_chunk(pipe, event, &chunk));
            }
            assert!(write_chunk(pipe, event, b"x\n{}\n"));
        }
        if args.iter().any(|arg| arg == "--pause-read") {
            std::thread::sleep(Duration::from_millis(750));
        }
        if args.iter().any(|arg| arg == "--pause-then-exit") {
            std::thread::sleep(Duration::from_millis(250));
            CloseHandle(event);
            CloseHandle(pipe);
            return;
        }
        let mut buffer = [0u8; 65536];
        loop {
            let mut op: OVERLAPPED = zeroed();
            op.hEvent = event;
            ResetEvent(event);
            let mut n = 0;
            if ReadFile(
                pipe,
                buffer.as_mut_ptr().cast(),
                buffer.len() as u32,
                &mut n,
                &mut op,
            ) == 0
            {
                if GetLastError() != ERROR_IO_PENDING {
                    break;
                }
                if GetOverlappedResult(pipe, &mut op, &mut n, 1) == 0 {
                    break;
                }
            }
            if n == 0 {
                break;
            }
            if !write_chunk(pipe, event, &buffer[..n as usize]) {
                break;
            }
        }
        CloseHandle(event);
        CloseHandle(pipe);
    }
}
