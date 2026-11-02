//! Prospective native TUI publication; no argv/rollout/focus selection inference.
//!
//! This module belongs in the TUI process. The App must clear before a display
//! transition, commit only a completed Live attachment, and heartbeat this
//! committed state. Temporary operation routing is never sampled here.

#![cfg(unix)]

use std::ffi::CString;
use std::fs::{self, File, Metadata, OpenOptions};
use std::io::{self, Read, Write};
use std::os::fd::{AsRawFd, FromRawFd};
use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde_json::{json, Value};
use uuid::Uuid;

pub const LEASE_MS: u64 = 5_000;
pub const HEARTBEAT: Duration = Duration::from_secs(1);
const MARKER: &[u8] = b"ccc-request-credentials-v1\n";
const MAX_RECORD_BYTES: usize = 4_096;

#[cfg(test)]
mod tests;

fn invalid(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

fn canonical_uuid(value: &str) -> bool {
    Uuid::parse_str(value)
        .map(|id| id.to_string() == value)
        .unwrap_or(false)
}

fn milliseconds() -> io::Result<u64> {
    let value = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|_| invalid("publication clock precedes epoch"))?
        .as_millis();
    u64::try_from(value).map_err(|_| invalid("publication clock overflow"))
}

fn monotonic_nanoseconds() -> io::Result<u64> {
    let mut value = libc::timespec { tv_sec: 0, tv_nsec: 0 };
    if unsafe { libc::clock_gettime(libc::CLOCK_MONOTONIC, &mut value) } != 0 {
        return Err(io::Error::last_os_error());
    }
    let seconds = u64::try_from(value.tv_sec).map_err(|_| invalid("negative monotonic clock"))?;
    let nanos = u64::try_from(value.tv_nsec).map_err(|_| invalid("negative monotonic fraction"))?;
    if nanos >= 1_000_000_000 { return Err(invalid("invalid monotonic fraction")); }
    seconds.checked_mul(1_000_000_000).and_then(|v| v.checked_add(nanos))
        .ok_or_else(|| invalid("monotonic clock overflow"))
}

fn directory_identity(info: &Metadata) -> (u64, u64, u32, u32) {
    (info.dev(), info.ino(), info.mode(), info.uid())
}

fn full_identity(info: &Metadata) -> (u64, u64, u32, u32, u64, i64, i64, i64, i64) {
    (
        info.dev(), info.ino(), info.mode(), info.uid(), info.len(),
        info.mtime(), info.mtime_nsec(), info.ctime(), info.ctime_nsec(),
    )
}

fn private_regular(info: &Metadata) -> bool {
    info.is_file()
        && info.uid() == unsafe { libc::getuid() }
        && info.mode() & 0o077 == 0
        && info.nlink() == 1
        && info.len() <= MAX_RECORD_BYTES as u64
}

fn relative_file(dir: &File, name: &str) -> io::Result<File> {
    let name = CString::new(name).map_err(|_| invalid("invalid relative observation name"))?;
    let fd = unsafe {
        libc::openat(
            dir.as_raw_fd(), name.as_ptr(),
            libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC,
        )
    };
    if fd < 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(unsafe { File::from_raw_fd(fd) })
}

trait Journal: Send {
    fn store(&mut self, record: &Value) -> io::Result<()>;
    fn invalidate_owned(&mut self) -> io::Result<()>;
}

struct FileJournal {
    home_path: PathBuf,
    directory_path: PathBuf,
    home: File,
    directory: File,
    pid: u32,
    epoch: String,
    name: String,
    published_identity: Option<(u64, u64, u32, u32, u64, i64, i64, i64, i64)>,
}

impl FileJournal {
    fn open(home: &Path, pid: u32, epoch: &str) -> io::Result<Self> {
        if !home.is_absolute() || unsafe { libc::getuid() } != unsafe { libc::geteuid() } {
            return Err(invalid("foreground home/owner is not a native user directory"));
        }
        let home_file = OpenOptions::new().read(true)
            .custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC)
            .open(home)?;
        let name = CString::new("credential-observations").expect("constant");
        let fd = unsafe {
            libc::openat(home_file.as_raw_fd(), name.as_ptr(),
                libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC)
        };
        if fd < 0 {
            return Err(io::Error::last_os_error());
        }
        let value = Self {
            home_path: home.to_path_buf(),
            directory_path: home.join("credential-observations"),
            home: home_file,
            directory: unsafe { File::from_raw_fd(fd) },
            pid,
            epoch: epoch.to_owned(),
            name: format!("client-{pid}-thread.json"),
            published_identity: None,
        };
        value.validate()?;
        Ok(value)
    }

    fn validate(&self) -> io::Result<()> {
        let home = self.home.metadata()?;
        let directory = self.directory.metadata()?;
        if !home.is_dir() || !directory.is_dir()
            || home.uid() != unsafe { libc::getuid() }
            || directory.uid() != home.uid() || directory.mode() & 0o077 != 0
            || directory_identity(&home) != directory_identity(&fs::symlink_metadata(&self.home_path)?)
            || directory_identity(&directory) != directory_identity(&fs::symlink_metadata(&self.directory_path)?)
        {
            return Err(invalid("foreground observation directory identity changed"));
        }
        let mut marker = relative_file(&self.directory, "enabled-v1")?;
        let before = marker.metadata()?;
        if !private_regular(&before) || before.len() != MARKER.len() as u64 {
            return Err(invalid("foreground observation opt-in is not private"));
        }
        let mut bytes = Vec::new();
        Read::by_ref(&mut marker).take(MARKER.len() as u64 + 1).read_to_end(&mut bytes)?;
        if bytes != MARKER
            || full_identity(&before) != full_identity(&marker.metadata()?)
            || full_identity(&before) != full_identity(&fs::symlink_metadata(self.directory_path.join("enabled-v1"))?)
        {
            return Err(invalid("foreground observation opt-in changed"));
        }
        Ok(())
    }

    fn owned_record(&self) -> io::Result<Option<(File, Metadata)>> {
        let mut file = match relative_file(&self.directory, &self.name) {
            Ok(file) => file,
            Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
            Err(error) => return Err(error),
        };
        let before = file.metadata()?;
        if !private_regular(&before) {
            return Err(invalid("foreground record is not private regular data"));
        }
        let mut bytes = Vec::new();
        Read::by_ref(&mut file).take(MAX_RECORD_BYTES as u64 + 1).read_to_end(&mut bytes)?;
        if bytes.len() > MAX_RECORD_BYTES
            || full_identity(&before) != full_identity(&file.metadata()?)
            || full_identity(&before) != full_identity(&fs::symlink_metadata(self.directory_path.join(&self.name))?)
        {
            return Err(invalid("foreground record changed during owned read"));
        }
        let record: Value = serde_json::from_slice(&bytes).map_err(|_| invalid("invalid owned record"))?;
        if record["pid"].as_u64() != Some(u64::from(self.pid))
            || record["client_epoch"].as_str() != Some(self.epoch.as_str())
            || record["purpose"].as_str() != Some("client_foreground_thread")
            || self.published_identity.is_some_and(|expected| expected != full_identity(&before))
        {
            return Err(invalid("foreground record belongs to another native owner"));
        }
        Ok(Some((file, before)))
    }
}

impl Journal for FileJournal {
    fn store(&mut self, record: &Value) -> io::Result<()> {
        self.validate()?;
        let bytes = serde_json::to_vec(record).map_err(|_| invalid("foreground encoding failed"))?;
        if bytes.len() > MAX_RECORD_BYTES {
            return Err(invalid("foreground record exceeded bounded size"));
        }
        // Replacements must be the exact last native-owned publication. An
        // unrelated private record is preserved, even at this PID pathname.
        let previous = self.owned_record()?.map(|(_, info)| full_identity(&info));
        if previous != self.published_identity {
            return Err(invalid("foreground destination ownership changed"));
        }
        let temporary_name = format!(".client-{}-{}.tmp", self.pid, Uuid::new_v4());
        let temporary = CString::new(temporary_name.clone()).expect("generated ASCII name");
        let destination = CString::new(self.name.clone()).expect("generated ASCII name");
        let fd = unsafe {
            libc::openat(self.directory.as_raw_fd(), temporary.as_ptr(),
                libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL | libc::O_NOFOLLOW | libc::O_CLOEXEC,
                0o600)
        };
        if fd < 0 {
            return Err(io::Error::last_os_error());
        }
        let mut file = unsafe { File::from_raw_fd(fd) };
        let result = (|| {
            file.write_all(&bytes)?;
            file.sync_all()?;
            self.validate()?;
            if self.owned_record()?.map(|(_, info)| full_identity(&info)) != previous {
                return Err(invalid("foreground destination changed before publication"));
            }
            let result = unsafe {
                libc::renameat(self.directory.as_raw_fd(), temporary.as_ptr(),
                    self.directory.as_raw_fd(), destination.as_ptr())
            };
            if result != 0 {
                return Err(io::Error::last_os_error());
            }
            // Track even a publication whose later durability check fails.
            // Its owner can still withdraw that exact attempted inode.
            self.published_identity = Some(full_identity(&file.metadata()?));
            self.directory.sync_all()?;
            self.validate()?;
            let (_, published) = self.owned_record()?
                .ok_or_else(|| invalid("native publication disappeared"))?;
            if full_identity(&published) != full_identity(&file.metadata()?) {
                return Err(invalid("native publication replaced before commit"));
            }
            Ok(())
        })();
        // Remove only this attempt's unique temporary name. No recursive or
        // wildcard cleanup ever touches another observation or client file.
        unsafe { libc::unlinkat(self.directory.as_raw_fd(), temporary.as_ptr(), 0); }
        result
    }

    fn invalidate_owned(&mut self) -> io::Result<()> {
        self.validate()?;
        let Some((file, before)) = self.owned_record()? else { return Ok(()); };
        let current = fs::symlink_metadata(self.directory_path.join(&self.name))?;
        if full_identity(&current) != full_identity(&before)
            || full_identity(&file.metadata()?) != full_identity(&before)
        {
            return Err(invalid("owned publication changed before invalidation"));
        }
        let name = CString::new(self.name.clone()).expect("generated ASCII name");
        if unsafe { libc::unlinkat(self.directory.as_raw_fd(), name.as_ptr(), 0) } != 0 {
            return Err(io::Error::last_os_error());
        }
        self.published_identity = None;
        self.directory.sync_all()?;
        Ok(())
    }
}

struct State {
    journal: Option<Box<dyn Journal>>,
    client_epoch: String,
    connection_epoch: Option<String>,
    generation: u64,
    committed_thread: Option<String>,
    last_heartbeat: Instant,
    suspended: bool,
    positive_until: Option<Instant>,
}

impl State {
    fn record(&self, generation: u64, thread: Option<&str>, reason: &str) -> io::Result<Value> {
        let stamp = milliseconds()?;
        let monotonic = monotonic_nanoseconds()?;
        Ok(json!({
            "schema": 2, "purpose": "client_foreground_thread", "pid": std::process::id(),
            "client_epoch": self.client_epoch, "connection_epoch": self.connection_epoch,
            "selection_generation": generation, "published_at_ms": stamp,
            "expires_at_ms": stamp.checked_add(LEASE_MS).ok_or_else(|| invalid("lease clock overflow"))?,
            "monotonic_clock": "clock_monotonic",
            "monotonic_published_ns": monotonic,
            "monotonic_expires_ns": monotonic.checked_add(LEASE_MS * 1_000_000)
                .ok_or_else(|| invalid("monotonic lease overflow"))?,
            "thread_id": thread, "selection_status": if thread.is_some() { "live" } else { "clear" },
            "control_eligible": thread.is_some(), "reason": reason,
        }))
    }

    fn next_generation(&self) -> io::Result<u64> {
        self.generation.checked_add(1).filter(|value| *value < (1u64 << 63))
            .ok_or_else(|| invalid("native selection generation exhausted"))
    }

    fn store(&mut self, record: &Value) -> io::Result<()> {
        // An attempted positive rename can be visible even if a later fsync
        // fails. Start this conservative Instant drain after encoding, before
        // the write; CLOCK_MONOTONIC wire expiry is never later than this.
        if record["thread_id"].as_str().is_some() {
            self.positive_until = Some(Instant::now() + Duration::from_millis(LEASE_MS));
        }
        if let Some(journal) = &mut self.journal { journal.store(record)?; }
        self.last_heartbeat = Instant::now();
        Ok(())
    }

    fn clear_before_change(&mut self, reason: &str) -> io::Result<()> {
        // Observation I/O failure must not make normal TUI work fatal. Stop
        // renewal immediately; the App can await remaining_lease(), then
        // proceed suspended. A paired monotonic consumer refuses the stale
        // publication after this bounded drain even if the wall clock moves.
        self.committed_thread = None;
        self.suspended = true;
        let generation = self.next_generation()?;
        self.generation = generation;
        let record = self.record(generation, None, reason)?;
        if let Err(error) = self.store(&record) {
            if let Some(journal) = &mut self.journal {
                if journal.invalidate_owned().is_err() { return Err(error); }
            }
        }
        self.positive_until = None;
        self.suspended = false;
        Ok(())
    }

    fn revoke(&mut self, reason: &str) -> io::Result<()> {
        // Connection loss is an external fact, not a cancellable UI switch.
        // Never heartbeat the previous positive selection after that fact.
        self.connection_epoch = None;
        self.clear_before_change(reason)
    }
}

impl Drop for State {
    fn drop(&mut self) { let _ = self.revoke("owner_exit"); }
}

#[derive(Clone)]
pub struct ForegroundObserver(Arc<Mutex<State>>);

impl Default for ForegroundObserver {
    fn default() -> Self { Self::disabled() }
}

impl ForegroundObserver {
    pub fn disabled() -> Self {
        Self(Arc::new(Mutex::new(State {
            journal: None, client_epoch: Uuid::new_v4().to_string(), connection_epoch: None,
            generation: 0, committed_thread: None, last_heartbeat: Instant::now(),
            suspended: false, positive_until: None,
        })))
    }

    pub fn from_env(home: &Path) -> io::Result<Self> {
        if std::env::var_os("CODEX_CLIENT_THREAD_OBSERVER").as_deref() != Some(std::ffi::OsStr::new("1")) {
            return Ok(Self::disabled());
        }
        Self::open_enabled(home)
    }

    /// Explicit opt-in for isolated App filesystem tests. Production uses
    /// from_env; this still requires the exact private enabled-v1 marker.
    pub fn open_enabled(home: &Path) -> io::Result<Self> {
        let epoch = Uuid::new_v4().to_string();
        let journal = FileJournal::open(home, std::process::id(), &epoch)?;
        let value = Self(Arc::new(Mutex::new(State {
            journal: Some(Box::new(journal)), client_epoch: epoch,
            connection_epoch: None, generation: 0, committed_thread: None,
            last_heartbeat: Instant::now(),
            suspended: false, positive_until: None,
        })));
        value.before_change("startup")?;
        Ok(value)
    }

    pub fn before_change(&self, reason: &str) -> io::Result<()> {
        self.0.lock().map_err(|_| invalid("native publication owner poisoned"))?
            .clear_before_change(reason)
    }

    /// Must drain before selection mutation after a refused withdrawal.
    /// No positive renewal is possible while this instance is suspended.
    pub fn remaining_lease(&self) -> Duration {
        self.0.lock().map(|state| state.positive_until
            .map(|until| until.saturating_duration_since(Instant::now()))
            .unwrap_or_default())
            .unwrap_or(Duration::from_millis(LEASE_MS))
    }

    pub fn is_suspended(&self) -> bool {
        self.0.lock().map(|state| state.suspended).unwrap_or(true)
    }

    pub fn connected(&self) -> io::Result<()> {
        let mut state = self.0.lock().map_err(|_| invalid("native publication owner poisoned"))?;
        state.clear_before_change("new_connection")?;
        let next_epoch = Uuid::new_v4().to_string();
        let generation = state.next_generation()?;
        state.connection_epoch = Some(next_epoch);
        // Advance semantics even if a durability error follows rename. A
        // later clear heartbeat must never roll back an attempted epoch.
        state.generation = generation;
        let record = match state.record(generation, None, "connected_clear") {
            Ok(record) => record,
            Err(error) => { state.suspended = true; return Err(error); }
        };
        if let Err(error) = state.store(&record) {
            state.suspended = true;
            return Err(error);
        }
        Ok(())
    }

    /// Called only after a completed native Live display attachment. Null is
    /// required for missing/replay-only/external-writer or disconnected state.
    pub fn committed(&self, thread: Option<&str>) -> io::Result<()> {
        let mut state = self.0.lock().map_err(|_| invalid("native publication owner poisoned"))?;
        if thread.is_some_and(|value| !canonical_uuid(value)) {
            return Err(invalid("native thread identifier is not canonical"));
        }
        if state.suspended {
            if thread.is_none() { return state.clear_before_change("display_withdrawn"); }
            return Err(invalid("native foreground publication is suspended"));
        }
        if thread.is_some() && state.connection_epoch.is_none() {
            return Err(invalid("native display has no connected generation"));
        }
        if state.committed_thread.as_deref() == thread {
            return Ok(());
        }
        let generation = state.next_generation()?;
        let record = state.record(generation, thread, "display_commit")?;
        // Failed positive publication never installs a new in-memory thread
        // for later heartbeat. Its preceding clear remains authoritative.
        if let Err(error) = state.store(&record) {
            state.generation = generation;
            state.committed_thread = None;
            let _ = state.clear_before_change("publication_failed");
            return Err(error);
        }
        state.generation = generation;
        state.committed_thread = thread.map(str::to_owned);
        Ok(())
    }

    pub fn disconnected(&self) -> io::Result<()> {
        self.0.lock().map_err(|_| invalid("native publication owner poisoned"))?
            .revoke("disconnected")
    }

    pub fn revoke(&self) -> io::Result<()> {
        self.0.lock().map_err(|_| invalid("native publication owner poisoned"))?
            .revoke("owner_exit")
    }

    pub fn heartbeat(&self) -> io::Result<()> {
        let mut state = self.0.lock().map_err(|_| invalid("native publication owner poisoned"))?;
        if state.last_heartbeat.elapsed() < HEARTBEAT { return Ok(()); }
        if state.suspended { return state.clear_before_change("publication_retry"); }
        let record = match state.record(state.generation.max(1), state.committed_thread.as_deref(), "heartbeat") {
            Ok(record) => record,
            Err(error) => {
                state.suspended = true;
                state.committed_thread = None;
                return Err(error);
            }
        };
        if let Err(error) = state.store(&record) {
            let _ = state.clear_before_change("heartbeat_failed");
            return Err(error);
        }
        Ok(())
    }
}
