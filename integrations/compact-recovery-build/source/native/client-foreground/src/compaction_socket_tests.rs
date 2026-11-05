use super::*;
use std::os::unix::fs::symlink;

#[test]
fn slow_trickle_does_not_extend_total_native_socket_read_deadline() {
    let (mut reader, mut writer) = UnixStream::pair().unwrap();
    let writing = std::thread::spawn(move || {
        for _ in 0..20 {
            if writer.write_all(b"x").is_err() {
                break;
            }
            std::thread::sleep(Duration::from_millis(10));
        }
    });
    let started = Instant::now();
    let error =
        read_line_before(&mut reader, 64, started + Duration::from_millis(70), None).unwrap_err();
    assert!(matches!(
        error.kind(),
        io::ErrorKind::TimedOut | io::ErrorKind::WouldBlock
    ));
    assert!(started.elapsed() < Duration::from_millis(300));
    drop(reader);
    writing.join().unwrap();
}

#[test]
fn revoked_listener_never_accepts_an_already_buffered_control_message() {
    let (mut reader, mut writer) = UnixStream::pair().unwrap();
    writer.write_all(b"{}\n").unwrap();
    let revoked = AtomicBool::new(true);
    let error = read_line_before(
        &mut reader,
        64,
        Instant::now() + Duration::from_secs(1),
        Some(&revoked),
    )
    .unwrap_err();
    assert!(error.to_string().contains("revoked"));
}
use std::os::unix::fs::PermissionsExt;
fn namespace() -> (tempfile::TempDir, PathBuf) {
    let home = tempfile::Builder::new()
        .prefix("ncr-")
        .tempdir_in("/private/tmp")
        .unwrap();
    let root = home
        .path()
        .join("credential-observations/compaction-recovery");
    fs::create_dir_all(&root).unwrap();
    fs::set_permissions(&root, fs::Permissions::from_mode(0o700)).unwrap();
    let mut marker = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(root.join("enabled-v1"))
        .unwrap();
    marker
        .write_all(b"ccc-native-compaction-recovery-v1\n")
        .unwrap();
    (home, root)
}
#[test]
fn real_socket_captures_kernel_peer_before_native_app_dispatch_and_exact_cleanup() {
    let (home, root) = namespace();
    let epoch = uuid::Uuid::new_v4().to_string();
    let (tx, rx) = mpsc::sync_channel(1);
    let server = NativeControlServer::open(
        home.path(),
        &epoch,
        ForegroundObserver::disabled(),
        move |r| {
            tx.send((r.peer_pid, r.peer_uid, r.queue_clear, r.body.clone()))
                .unwrap();
            r.reply.send(json!({"nativeAppObserved":true})).unwrap();
        },
    )
    .unwrap();
    let capability = root.join(format!("client-{}-control.json", std::process::id()));
    let published: Value = serde_json::from_slice(&fs::read(&capability).unwrap()).unwrap();
    let socket = PathBuf::from(published["socket"].as_str().unwrap());
    let mut stream = UnixStream::connect(&socket).unwrap();
    stream
        .set_read_timeout(Some(Duration::from_secs(2)))
        .unwrap();
    stream
        .write_all(b"{\"method\":\"compaction/recovery/query\"}\n")
        .unwrap();
    let mut line = String::new();
    use std::io::BufRead;
    std::io::BufReader::new(stream)
        .read_line(&mut line)
        .unwrap();
    assert_eq!(
        serde_json::from_str::<Value>(&line).unwrap()["nativeAppObserved"],
        true
    );
    let (pid, uid, queue_clear, body) = rx.recv_timeout(Duration::from_secs(2)).unwrap();
    assert_eq!(pid, std::process::id());
    assert_eq!(uid, unsafe { libc::getuid() });
    assert!(!queue_clear);
    assert_eq!(body["method"], "compaction/recovery/query");
    drop(server);
    assert!(!socket.exists());
    assert!(!capability.exists());
}
#[test]
fn existing_foreign_capability_is_preserved_and_new_socket_cleaned() {
    let (home, root) = namespace();
    let epoch = uuid::Uuid::new_v4().to_string();
    let capability = root.join(format!("client-{}-control.json", std::process::id()));
    fs::write(&capability, b"foreign owner").unwrap();
    assert!(
        NativeControlServer::open(home.path(), &epoch, ForegroundObserver::disabled(), |_| {})
            .is_err()
    );
    assert_eq!(fs::read(capability).unwrap(), b"foreign owner");
    assert!(!root
        .join(format!("c-{}-{}.sock", std::process::id(), &epoch[..8]))
        .exists());
}
#[test]
fn optin_marker_symlink_never_advertises_native_control() {
    let (home, root) = namespace();
    let marker = root.join("enabled-v1");
    let saved = root.join("original-marker");
    fs::rename(&marker, &saved).unwrap();
    symlink(saved, &marker).unwrap();
    assert!(NativeControlServer::open(
        home.path(),
        &uuid::Uuid::new_v4().to_string(),
        ForegroundObserver::disabled(),
        |_| {}
    )
    .is_err());
    assert!(!root
        .join(format!("client-{}-control.json", std::process::id()))
        .exists());
}
