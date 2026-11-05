use super::*;
use std::os::unix::fs::symlink;
use std::os::unix::fs::PermissionsExt;
struct Fixture {
    _socket: super::super::compaction_socket::NativeControlServer,
    _home: tempfile::TempDir,
    home: PathBuf,
    observer: ForegroundObserver,
    request: Value,
}

#[test]
fn original_grant_remaining_budget_decreases_and_never_renews_expired_authority() {
    let f = fixture();
    let mut grant = f
        .observer
        .authorize_compaction(&f.home, &f.request, std::process::id(), unsafe {
            libc::getuid()
        })
        .unwrap();
    let before = grant.guard.remaining().unwrap();
    std::thread::sleep(std::time::Duration::from_millis(5));
    let after = grant.guard.remaining().unwrap();
    assert!(after < before);
    assert!(before <= std::time::Duration::from_secs(15));
    // This unit-only local guard field changes no native proof file or receipt.
    grant.guard.proof["expiresMonotonicNs"] = json!(monotonic_nanoseconds().unwrap());
    assert!(grant.guard.remaining().is_err());
}

#[test]
fn enrollment_expiry_during_live_attestation_cannot_authorize_late_send() {
    let f = fixture();
    let enrollment_path = f
        .home
        .join("credential-observations/compaction-recovery/enrollments/test-enrollment.json");
    let mut enrollment: Value =
        serde_json::from_slice(&fs::read(&enrollment_path).unwrap()).unwrap();
    enrollment["expiresMonotonicNs"] = json!(monotonic_nanoseconds().unwrap() + 100_000_000);
    fs::write(&enrollment_path, serde_json::to_vec(&enrollment).unwrap()).unwrap();
    let grant = f
        .observer
        .authorize_compaction(&f.home, &f.request, std::process::id(), unsafe {
            libc::getuid()
        })
        .unwrap();
    let held_native_state = f.observer.0.lock().unwrap();
    let validation = std::thread::spawn(move || grant.guard.validate_fresh());
    std::thread::sleep(std::time::Duration::from_millis(150));
    drop(held_native_state);
    assert!(validation.join().unwrap().is_err());
}
fn private_dir(path: &Path) {
    fs::create_dir_all(path).unwrap();
    fs::set_permissions(path, fs::Permissions::from_mode(0o700)).unwrap();
}
fn private_bytes(path: &Path, bytes: &[u8]) {
    let mut f = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(path)
        .unwrap();
    f.write_all(bytes).unwrap();
    f.sync_all().unwrap();
}
fn fixture() -> Fixture {
    let h = tempfile::Builder::new()
        .prefix("nca-")
        .tempdir_in("/private/tmp")
        .unwrap();
    let home = h.path().canonicalize().unwrap();
    let observations = home.join("credential-observations");
    private_dir(&observations);
    private_bytes(&observations.join("enabled-v1"), super::super::MARKER);
    let root = observations.join("compaction-recovery");
    private_dir(&root);
    private_bytes(&root.join("enabled-v1"), MARKER);
    for part in ["intents", "enrollments", "grants"] {
        private_dir(&root.join(part));
    }
    let observer = ForegroundObserver::open_enabled(&home).unwrap();
    observer.connected().unwrap();
    let sid = uuid::Uuid::new_v4().to_string();
    observer.committed(Some(&sid)).unwrap();
    let mut authority = observer.compaction_selection().unwrap();
    authority.as_object_mut().unwrap().remove("threadId");
    authority["controllerId"] = json!("test-controller");
    authority["enrollmentId"] = json!("test-enrollment");
    authority["intentPath"] = json!(root.join("intents/test-attempt.json"));
    authority["expectedGoal"] = Value::Null;
    let expected = json!({"nativeEpoch":"actor-test","threadId":sid,"turnId":"turn-test","occurrence":"compact-test","generation":0});
    let until = monotonic_nanoseconds().unwrap() + 60_000_000_000;
    let birth = process_birth(std::process::id()).unwrap();
    let intent = json!({"schema":1,"purpose":"native_compaction_recovery_intent","state":"NATIVE_CONTROL_INTENT",
        "attemptId":"test-attempt","expected":expected,"controllerId":"test-controller","enrollmentId":"test-enrollment",
        "controllerPid":std::process::id(),"controllerBirth":birth,"expiresMonotonicNs":until,"authority":authority});
    let bytes = serde_json::to_vec(&intent).unwrap();
    private_bytes(&root.join("intents/test-attempt.json"), &bytes);
    authority["intentSha256"] = json!(sha256(&bytes));
    let enrollment = json!({"schema":1,"purpose":"native_compaction_recovery_enrollment","active":true,
        "controllerId":"test-controller","controllerPid":std::process::id(),"controllerBirth":birth,
        "threadId":sid,"expiresMonotonicNs":until});
    private_bytes(
        &root.join("enrollments/test-enrollment.json"),
        &serde_json::to_vec(&enrollment).unwrap(),
    );
    let epoch = observer.compaction_client_epoch().unwrap();
    let socket = super::super::compaction_socket::NativeControlServer::open(
        &home,
        &epoch,
        observer.clone(),
        |_| {},
    )
    .unwrap();
    Fixture {
        _socket: socket,
        _home: h,
        home,
        observer,
        request: json!({"expected":expected,"attemptId":"test-attempt","authority":authority}),
    }
}
fn authorize(f: &Fixture) -> AuthorizedCompaction {
    f.observer
        .authorize_compaction(&f.home, &f.request, std::process::id(), unsafe {
            libc::getuid()
        })
        .unwrap()
}
#[test]
fn actual_native_grant_and_server_lock_exclude_tui_change_until_dispatch_guard_drop() {
    let f = fixture();
    let grant = authorize(&f);
    let server =
        verify_compaction_grant(&grant.proof_path, &grant.proof_sha256, &f.request).unwrap();
    assert_eq!(
        f.observer
            .before_change("test mutation")
            .unwrap_err()
            .kind(),
        io::ErrorKind::WouldBlock
    );
    drop(grant.guard);
    assert_eq!(
        f.observer
            .before_change("test mutation")
            .unwrap_err()
            .kind(),
        io::ErrorKind::WouldBlock
    );
    server.validate_fresh().unwrap();
    drop(server);
    f.observer.before_change("after original dispatch").unwrap();
    assert!(f.observer.compaction_selection().is_err());
}
#[test]
fn consumed_original_intent_cannot_mint_second_native_grant() {
    let f = fixture();
    let first = authorize(&f);
    drop(first.guard);
    assert!(f
        .observer
        .authorize_compaction(&f.home, &f.request, std::process::id(), unsafe {
            libc::getuid()
        })
        .is_err());
}
#[test]
fn same_sid_wrong_selection_epoch_rejected_before_once_file() {
    let mut f = fixture();
    f.request["authority"]["expectedSelectionGeneration"] = json!(0);
    assert!(f
        .observer
        .authorize_compaction(&f.home, &f.request, std::process::id(), unsafe {
            libc::getuid()
        })
        .is_err());
    assert!(!f
        .home
        .join("credential-observations/compaction-recovery/grants/test-attempt.json")
        .exists());
}
#[test]
fn real_peer_birth_and_uid_are_required() {
    let f = fixture();
    assert!(f
        .observer
        .authorize_compaction(&f.home, &f.request, 1, unsafe { libc::getuid() })
        .is_err());
    assert!(f
        .observer
        .authorize_compaction(
            &f.home,
            &f.request,
            std::process::id(),
            unsafe { libc::getuid() } + 1
        )
        .is_err());
}
#[test]
fn changed_original_intent_and_symlink_are_rejected() {
    let f = fixture();
    let path = Path::new(f.request["authority"]["intentPath"].as_str().unwrap());
    let saved = path.with_extension("saved");
    fs::rename(path, &saved).unwrap();
    symlink(&saved, path).unwrap();
    assert!(f
        .observer
        .authorize_compaction(&f.home, &f.request, std::process::id(), unsafe {
            libc::getuid()
        })
        .is_err());
}
#[test]
fn server_rejects_different_attempt_goal_or_tampered_proof() {
    let f = fixture();
    let grant = authorize(&f);
    let mut wrong = f.request.clone();
    wrong["authority"]["expectedGoal"] = json!({"revision":2});
    assert!(verify_compaction_grant(&grant.proof_path, &grant.proof_sha256, &wrong).is_err());
    assert!(verify_compaction_grant(&grant.proof_path, "wrong-sha", &f.request).is_err());
}
#[test]
fn native_disconnect_revokes_existing_grant_even_while_reserved() {
    let f = fixture();
    let grant = authorize(&f);
    let server =
        verify_compaction_grant(&grant.proof_path, &grant.proof_sha256, &f.request).unwrap();
    f.observer.disconnected().unwrap();
    assert!(server.validate_fresh().is_err());
}
#[test]
fn replaced_native_control_lock_does_not_authorize_old_guard() {
    let f = fixture();
    let grant = authorize(&f);
    let server =
        verify_compaction_grant(&grant.proof_path, &grant.proof_sha256, &f.request).unwrap();
    let path = Path::new(server.proof["lockPath"].as_str().unwrap());
    fs::remove_file(path).unwrap();
    private_bytes(path, b"");
    assert!(server.validate_fresh().is_err());
    assert!(verify_compaction_grant(&grant.proof_path, &grant.proof_sha256, &f.request).is_err());
}
#[test]
fn plausible_same_uid_forged_grant_is_rejected_by_real_native_memory_attestation() {
    let f = fixture();
    let grant = authorize(&f);
    let path = grant.proof_path.clone();
    let hash = grant.proof_sha256.clone();
    drop(grant.guard);
    // Keep the actual file, process birth, socket, selection and lock unchanged;
    // only the true native issuer's in-memory grant is absent.
    f.observer.0.lock().unwrap().compaction_grants.clear();
    assert!(verify_compaction_grant(&path, &hash, &f.request).is_err());
}
#[test]
fn native_authority_attestation_still_responds_while_control_request_waits_for_app() {
    let f = fixture();
    let grant = authorize(&f);
    let socket = Path::new(grant.guard.proof["socketPath"].as_str().unwrap());
    use std::os::unix::net::UnixStream;
    let mut pending = UnixStream::connect(socket).unwrap();
    pending
        .write_all(b"{\"method\":\"compaction/recovery/control\"}\n")
        .unwrap();
    // This fixture intentionally never answers the normal App request.
    // Native issuance attestation uses a separate bounded worker, not AppEvent.
    let server =
        verify_compaction_grant(&grant.proof_path, &grant.proof_sha256, &f.request).unwrap();
    server.validate_fresh().unwrap();
    drop(pending);
}
