//! Native control authority. Same-UID JSON alone is not a live TUI selection.
//! Each grant is tied to the original controller process, native client birth,
//! current committed selection, and one immutable intent. The shared lock must
//! remain held through provider drain and replacement dispatch.
use super::directory_identity;
use super::full_identity;
use super::invalid;
use super::monotonic_nanoseconds;
use super::ForegroundObserver;
use serde_json::json;
use serde_json::Value;
use sha2::Digest;
use sha2::Sha256;
use std::fs::File;
use std::fs::OpenOptions;
use std::fs::{self};
use std::io::Read;
use std::io::Write;
use std::io::{self};
use std::os::fd::AsRawFd;
use std::os::unix::fs::MetadataExt;
use std::os::unix::fs::OpenOptionsExt;
use std::path::Path;
use std::path::PathBuf;

const LIMIT: usize = 64 * 1024;
const GRANT_NS: u64 = 15_000_000_000;
const MARKER: &[u8] = b"ccc-native-compaction-recovery-v1\n";

pub fn sha256(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}
fn identifier(v: &str) -> bool {
    !v.is_empty()
        && v.len() <= 128
        && v.bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"-_".contains(&b))
}
fn required<'a>(v: &'a Value, key: &str) -> io::Result<&'a str> {
    v[key]
        .as_str()
        .filter(|s| !s.is_empty())
        .ok_or_else(|| invalid("missing authority string"))
}
fn same(a: &Value, b: &Value, message: &str) -> io::Result<()> {
    if a == b {
        Ok(())
    } else {
        Err(invalid(message))
    }
}

/// Exact Darwin process birth uses the original public PROC_PIDTBSDINFO ABI.
/// Other targets fail closed until they have an equally exact implementation.
#[cfg(target_os = "macos")]
pub fn process_birth(pid: u32) -> io::Result<Value> {
    #[repr(C)]
    struct BsdInfo {
        flags: u32,
        status: u32,
        xstatus: u32,
        pid: u32,
        ppid: u32,
        ids: [u32; 7],
        comm: [u8; 16],
        name: [u8; 32],
        misc: [u32; 6],
        start_sec: u64,
        start_usec: u64,
    }
    unsafe extern "C" {
        fn proc_pidinfo(
            pid: i32,
            flavor: i32,
            arg: u64,
            buffer: *mut libc::c_void,
            size: i32,
        ) -> i32;
    }
    if pid <= 1 || pid > i32::MAX as u32 {
        return Err(invalid("invalid native PID"));
    }
    let mut info: BsdInfo = unsafe { std::mem::zeroed() };
    let size = std::mem::size_of::<BsdInfo>();
    let read = unsafe {
        proc_pidinfo(
            pid as i32,
            3,
            0,
            (&mut info as *mut BsdInfo).cast(),
            size as i32,
        )
    };
    if read != size as i32 || info.pid != pid || info.status == 5 || info.start_sec == 0 {
        return Err(invalid("native process birth unavailable"));
    }
    Ok(json!([info.start_sec, info.start_usec]))
}
#[cfg(not(target_os = "macos"))]
pub fn process_birth(_pid: u32) -> io::Result<Value> {
    Err(invalid("native recovery birth unsupported"))
}

pub fn read_private(path: &Path) -> io::Result<Vec<u8>> {
    if !path.is_absolute() {
        return Err(invalid("authority path must be absolute"));
    }
    let mut file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC | libc::O_NONBLOCK)
        .open(path)?;
    let before = file.metadata()?;
    if !before.is_file()
        || before.uid() != unsafe { libc::getuid() }
        || before.mode() & 0o077 != 0
        || before.nlink() != 1
        || before.len() > LIMIT as u64
    {
        return Err(invalid("authority is not bounded private regular data"));
    }
    let mut bytes = Vec::new();
    Read::by_ref(&mut file)
        .take(LIMIT as u64 + 1)
        .read_to_end(&mut bytes)?;
    if bytes.len() > LIMIT
        || full_identity(&before) != full_identity(&file.metadata()?)
        || full_identity(&before) != full_identity(&fs::symlink_metadata(path)?)
    {
        return Err(invalid("authority file changed during read"));
    }
    Ok(bytes)
}
fn directory(path: &Path) -> io::Result<File> {
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC)
        .open(path)?;
    let m = file.metadata()?;
    if !m.is_dir()
        || m.uid() != unsafe { libc::getuid() }
        || m.mode() & 0o077 != 0
        || directory_identity(&m) != directory_identity(&fs::symlink_metadata(path)?)
    {
        return Err(invalid("authority directory is not private and stable"));
    }
    Ok(file)
}
fn write_once(path: &Path, value: &Value) -> io::Result<String> {
    let parent = path
        .parent()
        .ok_or_else(|| invalid("missing grant parent"))?;
    let dir = directory(parent)?;
    let bytes = serde_json::to_vec(value).map_err(|_| invalid("grant encoding failed"))?;
    if bytes.len() > LIMIT {
        return Err(invalid("grant too large"));
    }
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC)
        .open(path)?;
    file.write_all(&bytes)?;
    file.sync_all()?;
    dir.sync_all()?;
    if read_private(path)? != bytes {
        return Err(invalid("grant changed after durable write"));
    }
    Ok(sha256(&bytes))
}

pub struct CompactionGrantGuard {
    _lock: File,
    pub proof: Value,
}
impl CompactionGrantGuard {
    /// The original grant's remaining transaction budget, never a renewed lease.
    /// This does not attest authority; callers still validate at actual send.
    pub fn remaining(&self) -> io::Result<std::time::Duration> {
        let expires = self.proof["expiresMonotonicNs"]
            .as_u64()
            .ok_or_else(|| invalid("native grant expiry missing"))?;
        let remaining = expires
            .checked_sub(monotonic_nanoseconds()?)
            .filter(|value| *value != 0)
            .ok_or_else(|| invalid("native grant expired"))?;
        Ok(std::time::Duration::from_nanos(remaining))
    }

    pub fn validate_fresh(&self) -> io::Result<()> {
        let now = monotonic_nanoseconds()?;
        let mut earliest_expiry = self.proof["expiresMonotonicNs"].as_u64().unwrap_or(0);
        if self.proof["expiresMonotonicNs"]
            .as_u64()
            .is_none_or(|end| end <= now)
        {
            return Err(invalid("native grant no longer fresh"));
        }
        for (pid_key, birth_key) in [
            ("clientPid", "clientBirth"),
            ("controllerPid", "controllerBirth"),
        ] {
            let pid = self.proof[pid_key]
                .as_u64()
                .and_then(|n| u32::try_from(n).ok())
                .ok_or_else(|| invalid("invalid native grant PID"))?;
            same(
                &process_birth(pid)?,
                &self.proof[birth_key],
                "native owner disappeared",
            )?;
        }
        for (path_key, sha_key) in [
            ("intentPath", "intentSha256"),
            ("enrollmentPath", "enrollmentSha256"),
        ] {
            let bytes = read_private(Path::new(required(&self.proof, path_key)?))?;
            if sha256(&bytes) != required(&self.proof, sha_key)? {
                return Err(invalid("original authority changed"));
            }
            let value = parse(&bytes)?;
            earliest_expiry =
                earliest_expiry.min(value["expiresMonotonicNs"].as_u64().unwrap_or(0));
            if value["expiresMonotonicNs"]
                .as_u64()
                .is_none_or(|end| end <= now)
            {
                return Err(invalid("original enrollment or intent expired"));
            }
        }
        let lock_meta = fs::symlink_metadata(Path::new(required(&self.proof, "lockPath")?))?;
        if self.proof["lockDevice"].as_u64() != Some(lock_meta.dev())
            || self.proof["lockInode"].as_u64() != Some(lock_meta.ino())
        {
            return Err(invalid("native reservation lock replaced"));
        }
        let fg = parse(&read_private(Path::new(required(
            &self.proof,
            "foregroundPath",
        )?))?)?;
        let request = &self.proof["request"];
        let auth = &request["authority"];
        earliest_expiry = earliest_expiry.min(fg["monotonic_expires_ns"].as_u64().unwrap_or(0));
        if fg["control_eligible"] != true
            || fg["client_epoch"] != auth["expectedClientEpoch"]
            || fg["connection_epoch"] != auth["expectedConnectionEpoch"]
            || fg["selection_generation"] != auth["expectedSelectionGeneration"]
            || fg["thread_id"] != request["expected"]["threadId"]
            || fg["monotonic_expires_ns"]
                .as_u64()
                .is_none_or(|end| end <= now)
        {
            return Err(invalid("native selection revoked"));
        }
        let path = Path::new(required(&self.proof["request"]["authority"], "intentPath")?)
            .parent()
            .and_then(Path::parent)
            .ok_or_else(|| invalid("intent namespace missing"))?
            .join("grants")
            .join(format!(
                "{}.json",
                required(&self.proof["request"], "attemptId")?
            ));
        let bytes = read_private(&path)?;
        if parse(&bytes)? != self.proof {
            return Err(invalid("native issued proof changed"));
        }
        super::compaction_socket::attest_issued_grant(&self.proof, &path, &sha256(&bytes))?;
        if monotonic_nanoseconds()? >= earliest_expiry {
            return Err(invalid(
                "original authority expired during native attestation",
            ));
        }
        Ok(())
    }
}
impl Drop for CompactionGrantGuard {
    fn drop(&mut self) {
        unsafe {
            libc::flock(self._lock.as_raw_fd(), libc::LOCK_UN);
        }
    }
}
fn lock(path: &Path, exclusive: bool, create: bool) -> io::Result<File> {
    let mut opts = OpenOptions::new();
    opts.read(true)
        .write(true)
        .mode(0o600)
        .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC);
    if create {
        opts.create(true);
    }
    let file = opts.open(path)?;
    let before = file.metadata()?;
    if !before.is_file()
        || before.uid() != unsafe { libc::getuid() }
        || before.mode() & 0o077 != 0
        || before.nlink() != 1
        || full_identity(&before) != full_identity(&fs::symlink_metadata(path)?)
    {
        return Err(invalid("native control lock identity invalid"));
    }
    let op = if exclusive {
        libc::LOCK_EX
    } else {
        libc::LOCK_SH
    };
    if unsafe { libc::flock(file.as_raw_fd(), op | libc::LOCK_NB) } != 0 {
        return Err(io::Error::last_os_error());
    }
    if full_identity(&before) != full_identity(&fs::symlink_metadata(path)?) {
        return Err(invalid("native control lock replaced"));
    }
    Ok(file)
}
fn normalized(mut request: Value) -> Value {
    if let Some(authority) = request.get_mut("authority").and_then(Value::as_object_mut) {
        authority.remove("proofPath");
        authority.remove("proofSha256");
    }
    request
}
fn parse(bytes: &[u8]) -> io::Result<Value> {
    serde_json::from_slice(bytes).map_err(|_| invalid("invalid authority JSON"))
}

pub struct AuthorizedCompaction {
    pub guard: CompactionGrantGuard,
    pub proof_path: PathBuf,
    pub proof_sha256: String,
}

impl ForegroundObserver {
    pub(crate) fn register_compaction_socket(
        &self,
        path: PathBuf,
        device: u64,
        inode: u64,
    ) -> io::Result<()> {
        let mut state = self
            .0
            .lock()
            .map_err(|_| invalid("foreground owner poisoned"))?;
        if state.control_socket.is_some() {
            return Err(invalid("native control socket already registered"));
        }
        state.control_socket = Some((path, device, inode));
        Ok(())
    }
    pub(crate) fn attest_compaction_grant(&self, path: &Path, hash: &str) -> io::Result<Value> {
        let state = self
            .0
            .lock()
            .map_err(|_| invalid("foreground owner poisoned"))?;
        let (owned_hash, proof) = state
            .compaction_grants
            .get(path)
            .ok_or_else(|| invalid("grant was not issued by this native TUI"))?;
        if owned_hash != hash || state.suspended {
            return Err(invalid("native issued grant mismatch"));
        }
        let request = &proof["request"];
        let auth = &request["authority"];
        if state.committed_thread.as_deref() != request["expected"]["threadId"].as_str()
            || Some(state.client_epoch.as_str()) != auth["expectedClientEpoch"].as_str()
            || state.connection_epoch.as_deref() != auth["expectedConnectionEpoch"].as_str()
            || Some(state.generation) != auth["expectedSelectionGeneration"].as_u64()
            || proof["expiresMonotonicNs"]
                .as_u64()
                .is_none_or(|end| monotonic_nanoseconds().map_or(true, |now| end <= now))
        {
            return Err(invalid("native issued authority no longer current"));
        }
        Ok(json!({"nativeIssued":true,"proofSha256":owned_hash}))
    }
    pub fn compaction_selection(&self) -> io::Result<Value> {
        let state = self
            .0
            .lock()
            .map_err(|_| invalid("foreground owner poisoned"))?;
        if state.journal.is_none() || state.suspended || state.committed_thread.is_none() {
            return Err(invalid("native selection unavailable"));
        }
        Ok(
            json!({"expectedClientPid":std::process::id(),"expectedClientBirth":process_birth(std::process::id())?,
            "expectedClientEpoch":state.client_epoch,"expectedConnectionEpoch":state.connection_epoch,
            "expectedSelectionGeneration":state.generation,"threadId":state.committed_thread}),
        )
    }
    pub fn compaction_client_epoch(&self) -> io::Result<String> {
        let state = self
            .0
            .lock()
            .map_err(|_| invalid("foreground owner poisoned"))?;
        if state.journal.is_none() {
            return Err(invalid("native observer unavailable"));
        }
        Ok(state.client_epoch.clone())
    }

    /// Used by the same TUI event loop after native draft/dialog barriers pass.
    /// Peer is obtained from the accepted native control socket, never JSON.
    pub fn authorize_compaction(
        &self,
        home: &Path,
        request: &Value,
        peer_pid: u32,
        peer_uid: u32,
    ) -> io::Result<AuthorizedCompaction> {
        let mut state = self
            .0
            .lock()
            .map_err(|_| invalid("foreground owner poisoned"))?;
        if state.journal.is_none() || state.suspended || peer_uid != unsafe { libc::getuid() } {
            return Err(invalid("native control authority unavailable"));
        }
        let auth = &request["authority"];
        let expected = &request["expected"];
        let thread = required(expected, "threadId")?;
        if state.committed_thread.as_deref() != Some(thread)
            || auth["expectedClientPid"].as_u64() != Some(std::process::id() as u64)
            || auth["expectedClientEpoch"].as_str() != Some(state.client_epoch.as_str())
            || auth["expectedConnectionEpoch"].as_str() != state.connection_epoch.as_deref()
            || auth["expectedSelectionGeneration"].as_u64() != Some(state.generation)
        {
            return Err(invalid("original committed native selection changed"));
        }
        let birth = process_birth(std::process::id())?;
        same(
            &auth["expectedClientBirth"],
            &birth,
            "original client birth changed",
        )?;
        let root = home.join("credential-observations/compaction-recovery");
        directory(&home.join("credential-observations"))?;
        directory(&root)?;
        for part in ["intents", "enrollments", "grants"] {
            directory(&root.join(part))?;
        }
        if read_private(&root.join("enabled-v1"))? != MARKER {
            return Err(invalid("native control not explicitly enabled"));
        }
        let attempt = required(request, "attemptId")?;
        let enrollment = required(auth, "enrollmentId")?;
        if !identifier(attempt) || !identifier(enrollment) {
            return Err(invalid("invalid original intent identifier"));
        }
        let intent_path = root.join("intents").join(format!("{attempt}.json"));
        if Path::new(required(auth, "intentPath")?) != intent_path {
            return Err(invalid("original intent outside enrolled namespace"));
        }
        let intent_bytes = read_private(&intent_path)?;
        if sha256(&intent_bytes) != required(auth, "intentSha256")? {
            return Err(invalid("original intent hash mismatch"));
        }
        let intent = parse(&intent_bytes)?;
        let mut intent_authority = normalized(request.clone())["authority"].clone();
        if let Some(object) = intent_authority.as_object_mut() {
            object.remove("intentSha256");
        }
        if intent["authority"] != intent_authority {
            return Err(invalid("original intent client/Goal authority changed"));
        }
        if intent["schema"] != 1
            || intent["purpose"] != "native_compaction_recovery_intent"
            || intent["state"] != "NATIVE_CONTROL_INTENT"
            || intent["attemptId"] != request["attemptId"]
            || intent["expected"] != request["expected"]
            || intent["controllerId"] != auth["controllerId"]
            || intent["enrollmentId"] != auth["enrollmentId"]
        {
            return Err(invalid("original controller intent binding mismatch"));
        }
        let peer_birth = process_birth(peer_pid)?;
        same(
            &intent["controllerPid"],
            &json!(peer_pid),
            "intent not owned by actual control peer",
        )?;
        same(
            &intent["controllerBirth"],
            &peer_birth,
            "original controller birth changed",
        )?;
        let enrollment_path = root.join("enrollments").join(format!("{enrollment}.json"));
        let enrollment_bytes = read_private(&enrollment_path)?;
        let enrolled = parse(&enrollment_bytes)?;
        if enrolled["schema"] != 1
            || enrolled["purpose"] != "native_compaction_recovery_enrollment"
            || enrolled["active"] != true
            || enrolled["controllerId"] != auth["controllerId"]
            || enrolled["controllerPid"] != json!(peer_pid)
            || enrolled["controllerBirth"] != peer_birth
            || enrolled["threadId"] != expected["threadId"]
        {
            return Err(invalid("original native recovery enrollment changed"));
        }
        let now = monotonic_nanoseconds()?;
        if enrolled["expiresMonotonicNs"]
            .as_u64()
            .is_none_or(|end| end <= now)
            || intent["expiresMonotonicNs"]
                .as_u64()
                .is_none_or(|end| end <= now)
        {
            return Err(invalid("original authority expired"));
        }
        let lock_path = root.join(format!(
            "client-{}-{}.lock",
            std::process::id(),
            state.client_epoch
        ));
        let file = lock(&lock_path, false, true)?;
        let info = file.metadata()?;
        let (socket_path, socket_device, socket_inode) = state
            .control_socket
            .as_ref()
            .ok_or_else(|| invalid("native control socket not registered"))?
            .clone();
        let proof = json!({"socketPath":socket_path,"socketDevice":socket_device,"socketInode":socket_inode,"schema":1,"purpose":"native_compaction_recovery_authority",
            "request":normalized(request.clone()),"clientPid":std::process::id(),"clientBirth":birth,
            "controllerPid":peer_pid,"controllerBirth":peer_birth,"controllerUid":peer_uid,
            "lockPath":lock_path,"lockDevice":info.dev(),"lockInode":info.ino(),
            "foregroundPath":home.join("credential-observations").join(format!("client-{}-thread.json",std::process::id())),
            "intentPath":intent_path,"intentSha256":sha256(&intent_bytes),
            "enrollmentPath":enrollment_path,"enrollmentSha256":sha256(&enrollment_bytes),
            "issuedMonotonicNs":now,"expiresMonotonicNs":now.checked_add(GRANT_NS).ok_or_else(||invalid("grant clock overflow"))?});
        let proof_path = root.join("grants").join(format!("{attempt}.json"));
        let proof_sha256 = write_once(&proof_path, &proof)?;
        state.compaction_grants.retain(|_, (_, old)| {
            old["expiresMonotonicNs"]
                .as_u64()
                .is_some_and(|end| end > now)
        });
        state
            .compaction_grants
            .insert(proof_path.clone(), (proof_sha256.clone(), proof.clone()));
        Ok(AuthorizedCompaction {
            guard: CompactionGrantGuard { _lock: file, proof },
            proof_path,
            proof_sha256,
        })
    }

    /// Each TUI mutation must acquire this before changing committed selection.
    /// WouldBlock is retried by the async App; it never permits bypassing a live
    /// server reservation. No sleep is performed while holding State's mutex.
    pub fn try_compaction_mutation_lock(
        &self,
        home: &Path,
    ) -> io::Result<Option<CompactionGrantGuard>> {
        let state = self
            .0
            .lock()
            .map_err(|_| invalid("foreground owner poisoned"))?;
        let root = home.join("credential-observations/compaction-recovery");
        if !root.exists() {
            return Ok(None);
        }
        directory(&root)?;
        let path = root.join(format!(
            "client-{}-{}.lock",
            std::process::id(),
            state.client_epoch
        ));
        let file = lock(&path, true, true)?;
        Ok(Some(CompactionGrantGuard {
            _lock: file,
            proof: Value::Null,
        }))
    }
}

/// App-server calls this at admission and keeps its guard until dispatch. It
/// must additionally bind the original Goal snapshot under Goal's own lock.
pub fn verify_compaction_grant(
    path: &Path,
    sha: &str,
    expected: &Value,
) -> io::Result<CompactionGrantGuard> {
    let bytes = read_private(path)?;
    if sha256(&bytes) != sha {
        return Err(invalid("native grant hash mismatch"));
    }
    let proof = parse(&bytes)?;
    if proof["schema"] != 1
        || proof["purpose"] != "native_compaction_recovery_authority"
        || proof["request"] != normalized(expected.clone())
    {
        return Err(invalid("native grant request binding mismatch"));
    }
    let root = path
        .parent()
        .and_then(Path::parent)
        .ok_or_else(|| invalid("native grant root missing"))?;
    directory(root)?;
    directory(&root.join("grants"))?;
    directory(&root.join("intents"))?;
    directory(&root.join("enrollments"))?;
    if root.file_name().and_then(|v| v.to_str()) != Some("compaction-recovery")
        || read_private(&root.join("enabled-v1"))? != MARKER
    {
        return Err(invalid("native grant namespace unavailable"));
    }
    let auth = &expected["authority"];
    let attempt = required(expected, "attemptId")?;
    let enrollment = required(auth, "enrollmentId")?;
    if !identifier(attempt)
        || !identifier(enrollment)
        || path != root.join("grants").join(format!("{attempt}.json"))
    {
        return Err(invalid("native grant is not original attempt path"));
    }
    let client = auth["expectedClientPid"]
        .as_u64()
        .ok_or_else(|| invalid("client PID missing"))?;
    let epoch = required(auth, "expectedClientEpoch")?;
    if !super::canonical_uuid(epoch) {
        return Err(invalid("client epoch is not canonical"));
    }
    let expected_lock = root.join(format!("client-{client}-{epoch}.lock"));
    let lock_path = Path::new(required(&proof, "lockPath")?);
    if lock_path != expected_lock
        || Path::new(required(&proof, "intentPath")?)
            != root.join("intents").join(format!("{attempt}.json"))
        || Path::new(required(&proof, "enrollmentPath")?)
            != root.join("enrollments").join(format!("{enrollment}.json"))
        || Path::new(required(&proof, "foregroundPath")?)
            != root
                .parent()
                .unwrap()
                .join(format!("client-{client}-thread.json"))
    {
        return Err(invalid(
            "native grant paths do not match original namespace",
        ));
    }
    let file = lock(lock_path, false, false)?;
    let info = file.metadata()?;
    if proof["lockDevice"].as_u64() != Some(info.dev())
        || proof["lockInode"].as_u64() != Some(info.ino())
    {
        return Err(invalid("native grant lock replaced"));
    }
    let now = monotonic_nanoseconds()?;
    let issued = proof["issuedMonotonicNs"]
        .as_u64()
        .ok_or_else(|| invalid("missing grant time"))?;
    let expires = proof["expiresMonotonicNs"]
        .as_u64()
        .ok_or_else(|| invalid("missing grant expiry"))?;
    if now < issued || now >= expires || expires - issued > GRANT_NS {
        return Err(invalid("native grant expired"));
    }
    for (pid_key, birth_key) in [
        ("clientPid", "clientBirth"),
        ("controllerPid", "controllerBirth"),
    ] {
        let pid = proof[pid_key]
            .as_u64()
            .and_then(|n| u32::try_from(n).ok())
            .ok_or_else(|| invalid("invalid grant PID"))?;
        same(
            &process_birth(pid)?,
            &proof[birth_key],
            "native grant process generation changed",
        )?;
    }
    let auth = &expected["authority"];
    same(
        &proof["clientPid"],
        &auth["expectedClientPid"],
        "client grant PID mismatch",
    )?;
    same(
        &proof["clientBirth"],
        &auth["expectedClientBirth"],
        "client grant birth mismatch",
    )?;
    for (path_key, sha_key) in [
        ("intentPath", "intentSha256"),
        ("enrollmentPath", "enrollmentSha256"),
    ] {
        if sha256(&read_private(Path::new(required(&proof, path_key)?))?)
            != required(&proof, sha_key)?
        {
            return Err(invalid("original intent/enrollment changed"));
        }
    }
    let fg = parse(&read_private(Path::new(required(
        &proof,
        "foregroundPath",
    )?))?)?;
    if fg["schema"] != 2
        || fg["purpose"] != "client_foreground_thread"
        || fg["control_eligible"] != true
        || fg["pid"] != auth["expectedClientPid"]
        || fg["client_epoch"] != auth["expectedClientEpoch"]
        || fg["connection_epoch"] != auth["expectedConnectionEpoch"]
        || fg["selection_generation"] != auth["expectedSelectionGeneration"]
        || fg["thread_id"] != expected["expected"]["threadId"]
        || fg["selection_status"] != "live"
        || fg["monotonic_clock"] != "clock_monotonic"
        || fg["monotonic_expires_ns"]
            .as_u64()
            .is_none_or(|end| end <= now)
    {
        return Err(invalid(
            "original live native selection no longer committed",
        ));
    }
    if read_private(path)? != bytes {
        return Err(invalid("native grant changed before authorization"));
    }
    super::compaction_socket::attest_issued_grant(&proof, path, sha)?;
    let guard = CompactionGrantGuard { _lock: file, proof };
    guard.validate_fresh()?;
    Ok(guard)
}

#[cfg(test)]
#[path = "compaction_authority_tests.rs"]
mod tests;
