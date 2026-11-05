//! Optional native TUI control ingress. Listener readiness is a distinct runtime
//! capability; the foreground observation file alone never advertises it.
use super::compaction_authority::process_birth;
use super::compaction_authority::read_private;
use super::directory_identity;
use super::invalid;
use super::ForegroundObserver;
use serde_json::json;
use serde_json::Value;
use std::fs::OpenOptions;
use std::fs::{self};
use std::io::Read;
use std::io::Write;
use std::io::{self};
use std::os::fd::AsRawFd;
use std::os::unix::fs::MetadataExt;
use std::os::unix::fs::OpenOptionsExt;
use std::os::unix::fs::PermissionsExt;
use std::os::unix::net::UnixListener;
use std::os::unix::net::UnixStream;
use std::path::Path;
use std::path::PathBuf;
use std::sync::atomic::AtomicBool;
use std::sync::atomic::AtomicUsize;
use std::sync::atomic::Ordering;
use std::sync::mpsc;
use std::sync::Arc;
use std::time::Duration;
use std::time::Instant;

fn read_line_before(
    stream: &mut UnixStream,
    limit: usize,
    deadline: Instant,
    stop: Option<&AtomicBool>,
) -> io::Result<Vec<u8>> {
    let mut bytes = Vec::new();
    let mut one = [0u8; 1];
    loop {
        if stop.is_some_and(|flag| flag.load(Ordering::SeqCst)) {
            return Err(invalid("native control listener revoked"));
        }
        let remaining = deadline
            .checked_duration_since(Instant::now())
            .filter(|duration| !duration.is_zero())
            .ok_or_else(|| {
                io::Error::new(io::ErrorKind::TimedOut, "native socket total read deadline")
            })?;
        // Poll the sole reader's descriptor rather than changing SO_RCVTIMEO
        // after a peer may already have closed with a buffered final response.
        // Short slices observe shutdown without renewing the total deadline.
        let mut descriptor = libc::pollfd {
            fd: stream.as_raw_fd(),
            events: libc::POLLIN,
            revents: 0,
        };
        let ready = unsafe {
            libc::poll(
                &mut descriptor,
                1,
                remaining.as_millis().clamp(1, 50) as i32,
            )
        };
        if ready == 0 {
            continue;
        }
        if ready < 0 {
            let error = io::Error::last_os_error();
            if error.kind() == io::ErrorKind::Interrupted {
                continue;
            }
            return Err(error);
        }
        match stream.read(&mut one) {
            Ok(0) => return Err(invalid("incomplete native socket message")),
            Ok(_) => {}
            Err(error)
                if matches!(
                    error.kind(),
                    io::ErrorKind::WouldBlock
                        | io::ErrorKind::TimedOut
                        | io::ErrorKind::Interrupted
                ) =>
            {
                continue
            }
            Err(error) => return Err(error),
        }
        if Instant::now() >= deadline {
            return Err(io::Error::new(
                io::ErrorKind::TimedOut,
                "native socket total read deadline",
            ));
        }
        if one[0] == b'\n' {
            return Ok(bytes);
        }
        bytes.push(one[0]);
        if bytes.len() > limit {
            return Err(invalid("native socket message too large"));
        }
    }
}

#[derive(Debug)]
pub struct NativeControlRequest {
    pub body: Value,
    pub queue_clear: bool,
    pub peer_pid: u32,
    pub peer_uid: u32,
    pub reply: mpsc::SyncSender<Value>,
}

pub struct NativeControlServer {
    stop: Arc<AtomicBool>,
    path: PathBuf,
    inode: (u64, u64),
    capability: PathBuf,
    capability_inode: (u64, u64),
}
impl Drop for NativeControlServer {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::SeqCst);
        if fs::symlink_metadata(&self.path).is_ok_and(|m| (m.dev(), m.ino()) == self.inode) {
            let _ = fs::remove_file(&self.path);
        }
        if fs::symlink_metadata(&self.capability)
            .is_ok_and(|m| (m.dev(), m.ino()) == self.capability_inode)
        {
            let _ = fs::remove_file(&self.capability);
        }
    }
}

#[cfg(target_os = "macos")]
fn peer(stream: &UnixStream) -> io::Result<(u32, u32)> {
    let mut uid: libc::uid_t = 0;
    let mut gid: libc::gid_t = 0;
    if unsafe { libc::getpeereid(stream.as_raw_fd(), &mut uid, &mut gid) } != 0 {
        return Err(io::Error::last_os_error());
    }
    let mut pid: libc::pid_t = 0;
    let mut len = std::mem::size_of::<libc::pid_t>() as libc::socklen_t;
    if unsafe {
        libc::getsockopt(
            stream.as_raw_fd(),
            0,
            2,
            (&mut pid as *mut libc::pid_t).cast(),
            &mut len,
        )
    } != 0
        || len as usize != std::mem::size_of::<libc::pid_t>()
        || pid <= 1
    {
        return Err(invalid("native control peer PID unavailable"));
    }
    Ok((pid as u32, uid))
}
#[cfg(not(target_os = "macos"))]
fn peer(_stream: &UnixStream) -> io::Result<(u32, u32)> {
    Err(invalid("native control peer identity unsupported"))
}

impl NativeControlServer {
    /// Called exactly once by the actual App. Only the caller may advertise
    /// actor support after its backend capability check also succeeds.
    pub fn open(
        home: &Path,
        client_epoch: &str,
        observer: ForegroundObserver,
        dispatch: impl Fn(NativeControlRequest) + Send + Sync + 'static,
    ) -> io::Result<Self> {
        let root = home.join("credential-observations/compaction-recovery");
        if read_private(&root.join("enabled-v1"))? != b"ccc-native-compaction-recovery-v1\n" {
            return Err(invalid("native control not enabled"));
        }
        let dir = OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC)
            .open(&root)?;
        let info = dir.metadata()?;
        if !info.is_dir()
            || info.uid() != unsafe { libc::getuid() }
            || info.mode() & 0o077 != 0
            || directory_identity(&info) != directory_identity(&fs::symlink_metadata(&root)?)
        {
            return Err(invalid("invalid native control namespace"));
        }
        if uuid::Uuid::parse_str(client_epoch).is_err() {
            return Err(invalid("invalid native client epoch"));
        }
        let path = root.join(format!(
            "c-{}-{}.sock",
            std::process::id(),
            &client_epoch[..8]
        ));
        // Do not unlink or reuse a possibly active socket from a previous owner.
        if path.exists() {
            return Err(invalid("native control socket already exists"));
        }
        let listener = UnixListener::bind(&path)?;
        let m = fs::symlink_metadata(&path)?;
        let inode = (m.dev(), m.ino());
        let capability = root.join(format!("client-{}-control.json", std::process::id()));
        let mut owned = Self {
            stop: Arc::new(AtomicBool::new(false)),
            path: path.clone(),
            inode,
            capability: capability.clone(),
            capability_inode: (0, 0),
        };
        fs::set_permissions(&path, fs::Permissions::from_mode(0o600))?;
        listener.set_nonblocking(true)?;
        let bytes=serde_json::to_vec(&json!({"schema":1,"purpose":"native_compaction_recovery_control",
            "pid":std::process::id(),"birth":process_birth(std::process::id())?,"client_epoch":client_epoch,
            "socket":path,"socket_device":m.dev(),"socket_inode":m.ino(),
            "methods":["compaction/recovery/query","compaction/recovery/control"],
            "controlRequiresNativeBackend":true})).map_err(|_|invalid("native capability encoding failed"))?;
        let mut publication = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC)
            .open(&capability)?;
        let cm = publication.metadata()?;
        owned.capability_inode = (cm.dev(), cm.ino());
        publication.write_all(&bytes)?;
        publication.sync_all()?;
        dir.sync_all()?;
        observer.register_compaction_socket(path.clone(), inode.0, inode.1)?;
        let worker_stop = owned.stop.clone();
        let dispatch = Arc::new(dispatch);
        let active = Arc::new(AtomicUsize::new(0));
        std::thread::Builder::new()
            .name("ccc-native-control".into())
            .spawn(move || {
                while !worker_stop.load(Ordering::SeqCst) {
                    match listener.accept() {
                        Ok((mut stream, _)) => {
                            if active.fetch_add(1, Ordering::SeqCst) >= 8 {
                                active.fetch_sub(1, Ordering::SeqCst);
                                continue;
                            }
                            let dispatch = dispatch.clone();
                            let observer = observer.clone();
                            let active = active.clone();
                            let stop = worker_stop.clone();
                            std::thread::spawn(move || {
                                let result = (|| -> io::Result<Value> {
                                    let deadline = Instant::now() + Duration::from_secs(2);
                                    stream.set_write_timeout(Some(Duration::from_secs(2)))?;
                                    let (peer_pid, peer_uid) = peer(&stream)?;
                                    if peer_uid != unsafe { libc::getuid() } {
                                        return Err(invalid("foreign native control peer"));
                                    }
                                    let birth = process_birth(peer_pid)?;
                                    let bytes = read_line_before(
                                        &mut stream,
                                        65536,
                                        deadline,
                                        Some(&stop),
                                    )?;
                                    let body: Value = serde_json::from_slice(&bytes)
                                        .map_err(|_| invalid("invalid native control JSON"))?;
                                    if process_birth(peer_pid)? != birth {
                                        return Err(invalid("native control peer changed"));
                                    }
                                    if stop.load(Ordering::SeqCst) {
                                        return Err(invalid("native control listener revoked"));
                                    }
                                    if body["method"] == "authority/verify" {
                                        let path = body["proofPath"]
                                            .as_str()
                                            .ok_or_else(|| invalid("proof path missing"))?;
                                        let sha = body["proofSha256"]
                                            .as_str()
                                            .ok_or_else(|| invalid("proof hash missing"))?;
                                        let mut attested = observer
                                            .attest_compaction_grant(Path::new(path), sha)?;
                                        attested["challenge"] = body["challenge"].clone();
                                        return Ok(attested);
                                    }
                                    let (tx, rx) = mpsc::sync_channel(1);
                                    dispatch(NativeControlRequest {
                                        body,
                                        queue_clear: false,
                                        peer_pid,
                                        peer_uid,
                                        reply: tx,
                                    });
                                    rx.recv_timeout(Duration::from_secs(12))
                                        .map_err(|_| invalid("native control result UNKNOWN"))
                                })();
                                let reply = match result {
                                    Ok(v) => v,
                                    Err(e) => json!({"error":e.to_string(),"replayAllowed":false}),
                                };
                                if let Ok(mut bytes) = serde_json::to_vec(&reply) {
                                    bytes.push(b'\n');
                                    let _ = stream.write_all(&bytes);
                                }
                                active.fetch_sub(1, Ordering::SeqCst);
                            });
                        }
                        Err(e) if e.kind() == io::ErrorKind::WouldBlock => {
                            std::thread::sleep(Duration::from_millis(20))
                        }
                        Err(_) => break,
                    }
                }
            })?;
        Ok(owned)
    }
}

/// Authenticate issuance against the live TUI's in-memory grant registry. A
/// same-UID file with plausible fields cannot manufacture this native reply.
pub(crate) fn attest_issued_grant(proof: &Value, path: &Path, sha: &str) -> io::Result<()> {
    let socket = Path::new(
        proof["socketPath"]
            .as_str()
            .ok_or_else(|| invalid("grant socket missing"))?,
    );
    let before = fs::symlink_metadata(socket)?;
    use std::os::unix::fs::FileTypeExt;
    if !before.file_type().is_socket()
        || before.uid() != unsafe { libc::getuid() }
        || before.mode() & 0o077 != 0
        || proof["socketDevice"].as_u64() != Some(before.dev())
        || proof["socketInode"].as_u64() != Some(before.ino())
    {
        return Err(invalid("native authority socket changed"));
    }
    let context = |stage: &str, error: io::Error| {
        io::Error::new(error.kind(), format!("native attestation {stage}: {error}"))
    };
    let mut stream = UnixStream::connect(socket).map_err(|e| context("connect", e))?;
    stream
        .set_read_timeout(Some(Duration::from_secs(2)))
        .map_err(|e| context("read timeout", e))?;
    stream
        .set_write_timeout(Some(Duration::from_secs(2)))
        .map_err(|e| context("write timeout", e))?;
    let (pid, uid) = peer(&stream).map_err(|e| context("kernel peer", e))?;
    if uid != unsafe { libc::getuid() }
        || proof["clientPid"] != json!(pid)
        || process_birth(pid)? != proof["clientBirth"]
    {
        return Err(invalid("authority peer is not original native TUI"));
    }
    let challenge = uuid::Uuid::new_v4().to_string();
    let mut bytes=serde_json::to_vec(&json!({"method":"authority/verify","proofPath":path,"proofSha256":sha,"challenge":challenge})).map_err(|_|invalid("attestation encoding failed"))?;
    bytes.push(b'\n');
    stream
        .write_all(&bytes)
        .map_err(|e| context("write challenge", e))?;
    let reply = read_line_before(
        &mut stream,
        4096,
        Instant::now() + Duration::from_secs(2),
        None,
    )
    .map_err(|e| context("read response", e))?;
    let value: Value =
        serde_json::from_slice(&reply).map_err(|_| invalid("native attestation invalid"))?;
    let after = fs::symlink_metadata(socket)?;
    if value["nativeIssued"] != true
        || value["proofSha256"] != sha
        || value["challenge"] != challenge
        || (before.dev(), before.ino()) != (after.dev(), after.ino())
        || process_birth(pid)? != proof["clientBirth"]
    {
        return Err(invalid("native TUI did not attest original grant"));
    }
    Ok(())
}

#[cfg(test)]
#[path = "compaction_socket_tests.rs"]
mod tests;
