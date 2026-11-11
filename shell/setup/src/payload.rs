//! The Inno Setup package this window drives: embedded at build time (release), or a file
//! named on the command line (`--payload <path>`, development builds only).
//!
//! It is written to a fresh folder under %TEMP%, then held open with read-only sharing until
//! it has exited, so nothing can replace the file between the hash check and the elevated start.
use sha2::{Digest, Sha256};
use std::{fs::{self, File, OpenOptions}, io::{self, Read, Write}, os::windows::fs::OpenOptionsExt, path::{Path, PathBuf}};

const FILE_SHARE_READ: u32 = 0x1;

pub enum Source {
    #[allow(dead_code)]
    Embedded { bytes: &'static [u8], sha256: &'static str },
    #[allow(dead_code)]
    File(PathBuf),
}

impl Source {
    pub fn locate(arg: Option<PathBuf>) -> Option<Source> {
        #[cfg(has_payload)]
        {
            let _ = arg;
            return Some(Source::Embedded { bytes: include_bytes!(env!("SETUP_PAYLOAD_PATH")), sha256: env!("SETUP_PAYLOAD_SHA256") });
        }
        #[cfg(not(has_payload))]
        arg.filter(|p| p.is_file()).map(Source::File)
    }
}

pub struct Extracted {
    dir: PathBuf,
    path: PathBuf,
    lock: Option<File>,
}

impl Extracted {
    pub fn path(&self) -> &Path { &self.path }
}

impl Drop for Extracted {
    fn drop(&mut self) {
        self.lock.take();
        let _ = fs::remove_dir_all(&self.dir);
    }
}

fn hex(bytes: &[u8]) -> String { bytes.iter().map(|b| format!("{b:02x}")).collect() }

pub fn extract(source: &Source) -> io::Result<Extracted> {
    let stamp = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_nanos()).unwrap_or(0);
    let dir = std::env::temp_dir().join(format!("Tesseract-Setup-{}-{stamp}", std::process::id()));
    fs::create_dir(&dir)?;
    let path = dir.join("tesseract-installer.exe");
    let mut out = Extracted { dir, path, lock: None };
    let expected = match source {
        Source::Embedded { bytes, sha256 } => {
            let mut f = OpenOptions::new().write(true).create_new(true).open(&out.path)?;
            f.write_all(bytes)?;
            f.sync_all()?;
            Some(*sha256)
        }
        Source::File(from) => { fs::copy(from, &out.path)?; None }
    };
    // from here on the file cannot be written, renamed or deleted until we let go of it
    let mut lock = OpenOptions::new().read(true).share_mode(FILE_SHARE_READ).open(&out.path)?;
    if let Some(expected) = expected {
        let mut hasher = Sha256::new();
        let mut buf = vec![0u8; 1 << 20];
        loop { let n = lock.read(&mut buf)?; if n == 0 { break; } hasher.update(&buf[..n]); }
        let actual = hex(&hasher.finalize());
        if !actual.eq_ignore_ascii_case(expected) {
            return Err(io::Error::new(io::ErrorKind::InvalidData, "installer hash mismatch"));
        }
    }
    out.lock = Some(lock);
    Ok(out)
}
