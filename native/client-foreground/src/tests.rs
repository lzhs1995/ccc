use super::*;
use std::os::unix::fs::PermissionsExt;

const THREAD: &str = "01a19873-eb3b-7131-a406-09afdda989a6";
const NEXT: &str = "01a19265-b934-75f1-8dc0-0ad163e22222";

#[derive(Default)]
struct MemoryJournal {
    records: Vec<Value>,
    fail_store: bool,
    fail_after_store: bool,
    fail_invalidate: bool,
    invalidated: usize,
}

impl Journal for Arc<Mutex<MemoryJournal>> {
    fn store(&mut self, record: &Value) -> io::Result<()> {
        let mut value = self.lock().unwrap();
        if value.fail_store { return Err(invalid("injected pre-write failure")); }
        value.records.push(record.clone());
        if value.fail_after_store { return Err(invalid("injected post-write failure")); }
        Ok(())
    }

    fn invalidate_owned(&mut self) -> io::Result<()> {
        let mut value = self.lock().unwrap();
        if value.fail_invalidate { return Err(invalid("injected invalidation failure")); }
        value.invalidated += 1;
        Ok(())
    }
}

fn observer(journal: impl Journal + 'static) -> ForegroundObserver {
    ForegroundObserver(Arc::new(Mutex::new(State {
        journal: Some(Box::new(journal)), client_epoch: Uuid::new_v4().to_string(),
        connection_epoch: None, generation: 0, committed_thread: None,
        last_heartbeat: Instant::now(),
        suspended: false, positive_until: None,
    })))
}

fn memory() -> (ForegroundObserver, Arc<Mutex<MemoryJournal>>) {
    let journal = Arc::new(Mutex::new(MemoryJournal::default()));
    (observer(journal.clone()), journal)
}

fn pulse(value: &ForegroundObserver) -> io::Result<()> {
    value.0.lock().unwrap().last_heartbeat = Instant::now() - HEARTBEAT;
    value.heartbeat()
}

fn fixture() -> (tempfile::TempDir, ForegroundObserver, PathBuf) {
    let home = tempfile::tempdir().unwrap();
    fs::set_permissions(home.path(), fs::Permissions::from_mode(0o700)).unwrap();
    let directory = home.path().join("credential-observations");
    fs::create_dir(&directory).unwrap();
    fs::set_permissions(&directory, fs::Permissions::from_mode(0o700)).unwrap();
    let marker = directory.join("enabled-v1");
    fs::write(&marker, MARKER).unwrap();
    fs::set_permissions(&marker, fs::Permissions::from_mode(0o600)).unwrap();
    let epoch = Uuid::new_v4().to_string();
    let journal = FileJournal::open(home.path(), std::process::id(), &epoch).unwrap();
    let value = ForegroundObserver(Arc::new(Mutex::new(State {
        journal: Some(Box::new(journal)), client_epoch: epoch, connection_epoch: None,
        generation: 0, committed_thread: None, last_heartbeat: Instant::now(),
        suspended: false, positive_until: None,
    })));
    let path = directory.join(format!("client-{}-thread.json", std::process::id()));
    (home, value, path)
}

fn read(path: &Path) -> Value { serde_json::from_slice(&fs::read(path).unwrap()).unwrap() }

#[test]
fn connection_heartbeat_preserves_committed_epoch_and_generation() {
    let (value, journal) = memory();
    value.before_change("startup").unwrap();
    value.connected().unwrap();
    pulse(&value).unwrap();
    let records = &journal.lock().unwrap().records;
    let committed = &records[records.len() - 2];
    let heartbeat = records.last().unwrap();
    assert_eq!(committed["connection_epoch"], heartbeat["connection_epoch"]);
    assert_eq!(committed["selection_generation"], heartbeat["selection_generation"]);
    assert!(heartbeat["thread_id"].is_null());
    assert_eq!(heartbeat["expires_at_ms"].as_u64().unwrap() - heartbeat["published_at_ms"].as_u64().unwrap(), LEASE_MS);
}

#[test]
fn clear_precedes_thread_switch_and_heartbeat_never_samples_a_proposal() {
    let (value, journal) = memory();
    value.connected().unwrap();
    value.committed(Some(THREAD)).unwrap();
    value.before_change("switch").unwrap();
    pulse(&value).unwrap();
    assert!(journal.lock().unwrap().records.last().unwrap()["thread_id"].is_null());
    value.committed(Some(NEXT)).unwrap();
    assert_eq!(journal.lock().unwrap().records.last().unwrap()["thread_id"], NEXT);
}

#[test]
fn failed_withdrawal_stops_renewal_and_bounds_the_mutation_drain() {
    let (value, journal) = memory();
    value.connected().unwrap();
    value.committed(Some(THREAD)).unwrap();
    { let mut state = journal.lock().unwrap(); state.fail_store = true; state.fail_invalidate = true; }
    assert!(value.before_change("switch").is_err());
    assert!(value.0.lock().unwrap().committed_thread.is_none());
    assert!(value.is_suspended());
    let before = value.remaining_lease();
    assert!(before > Duration::ZERO && before <= Duration::from_millis(LEASE_MS));
    assert_eq!(journal.lock().unwrap().records.last().unwrap()["thread_id"], THREAD);
    assert!(pulse(&value).is_err());
    assert!(value.remaining_lease() <= before);
    assert!(value.committed(Some(NEXT)).is_err());
    assert_eq!(journal.lock().unwrap().records.iter().filter(|r| r["thread_id"] == THREAD).count(), 1);
    value.0.lock().unwrap().positive_until = Some(Instant::now() - Duration::from_millis(1));
    assert_eq!(value.remaining_lease(), Duration::ZERO);
}

#[test]
fn successful_invalidation_allows_clear_after_store_failure() {
    let (value, journal) = memory();
    value.connected().unwrap();
    value.committed(Some(THREAD)).unwrap();
    journal.lock().unwrap().fail_store = true;
    value.before_change("switch").unwrap();
    assert!(value.0.lock().unwrap().committed_thread.is_none());
    assert_eq!(journal.lock().unwrap().invalidated, 1);
}

#[test]
fn disconnect_drops_positive_memory_even_when_both_withdrawals_fail() {
    let (value, journal) = memory();
    value.connected().unwrap();
    value.committed(Some(THREAD)).unwrap();
    { let mut state = journal.lock().unwrap(); state.fail_store = true; state.fail_invalidate = true; }
    assert!(value.disconnected().is_err());
    assert!(value.0.lock().unwrap().committed_thread.is_none());
    journal.lock().unwrap().fail_store = false;
    pulse(&value).unwrap();
    let record = journal.lock().unwrap().records.last().unwrap().clone();
    assert!(record["thread_id"].is_null());
    assert!(record["connection_epoch"].is_null());
}

#[test]
fn failed_positive_durability_withdraws_at_a_later_generation_without_replay() {
    let (value, journal) = memory();
    value.connected().unwrap();
    journal.lock().unwrap().fail_after_store = true;
    assert!(value.committed(Some(THREAD)).is_err());
    journal.lock().unwrap().fail_after_store = false;
    pulse(&value).unwrap();
    let records = &journal.lock().unwrap().records;
    let positive = records.iter().find(|r| r["thread_id"] == THREAD).unwrap();
    let last = records.last().unwrap();
    assert!(last["thread_id"].is_null());
    assert!(last["selection_generation"].as_u64() > positive["selection_generation"].as_u64());
    assert_eq!(records.iter().filter(|r| r["thread_id"] == THREAD).count(), 1);
}

#[test]
fn failed_connected_durability_never_rolls_epoch_back_on_heartbeat() {
    let (value, journal) = memory();
    value.before_change("startup").unwrap();
    journal.lock().unwrap().fail_after_store = true;
    assert!(value.connected().is_err());
    journal.lock().unwrap().fail_after_store = false;
    pulse(&value).unwrap();
    let records = &journal.lock().unwrap().records;
    let attempted = records.iter().find(|r| r["reason"] == "connected_clear").unwrap();
    let last = records.last().unwrap();
    assert_eq!(attempted["connection_epoch"], last["connection_epoch"]);
    assert!(last["selection_generation"].as_u64() >= attempted["selection_generation"].as_u64());
    assert!(last["thread_id"].is_null());
}

#[test]
fn positive_requires_connected_canonical_thread() {
    let (value, _) = memory();
    assert!(value.committed(Some(THREAD)).is_err());
    value.connected().unwrap();
    assert!(value.committed(Some("invalid")).is_err());
    assert!(value.committed(Some(&THREAD.to_uppercase())).is_err());
    assert!(value.0.lock().unwrap().committed_thread.is_none());
}

#[test]
fn private_atomic_file_heartbeat_keeps_semantics_while_changing_inode() {
    let (_home, value, path) = fixture();
    value.before_change("startup").unwrap();
    value.connected().unwrap();
    value.committed(Some(THREAD)).unwrap();
    let first = read(&path);
    let old = fs::metadata(&path).unwrap();
    pulse(&value).unwrap();
    let next = read(&path);
    let new = fs::metadata(&path).unwrap();
    assert_ne!(old.ino(), new.ino());
    assert_eq!(new.mode() & 0o777, 0o600);
    assert_eq!(new.nlink(), 1);
    for name in ["thread_id", "client_epoch", "connection_epoch", "selection_generation", "control_eligible"] {
        assert_eq!(first[name], next[name]);
    }
    assert!(next.get("compact_interrupt_capability").is_none());
    assert_eq!(next["monotonic_clock"], "clock_monotonic");
    assert_eq!(next["monotonic_expires_ns"].as_u64().unwrap()
        - next["monotonic_published_ns"].as_u64().unwrap(), LEASE_MS * 1_000_000);
    assert!(next["monotonic_published_ns"].as_u64().unwrap() <= monotonic_nanoseconds().unwrap());
}

#[test]
fn existing_foreign_owner_is_neither_replaced_nor_unlinked() {
    let (_home, value, path) = fixture();
    let foreign = json!({"pid": std::process::id(), "client_epoch": Uuid::new_v4().to_string(),
        "purpose": "client_foreground_thread", "thread_id": NEXT});
    fs::write(&path, serde_json::to_vec(&foreign).unwrap()).unwrap();
    fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
    let old = fs::metadata(&path).unwrap();
    assert!(value.before_change("startup").is_err());
    drop(value);
    assert_eq!(read(&path), foreign);
    assert_eq!(fs::metadata(&path).unwrap().ino(), old.ino());
}

#[test]
fn replacement_of_owned_record_is_preserved_on_heartbeat_and_exit() {
    let (_home, value, path) = fixture();
    value.connected().unwrap();
    value.committed(Some(THREAD)).unwrap();
    let saved = read(&path);
    fs::remove_file(&path).unwrap();
    fs::write(&path, serde_json::to_vec(&saved).unwrap()).unwrap();
    fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
    let replacement = fs::metadata(&path).unwrap().ino();
    assert!(pulse(&value).is_err());
    drop(value);
    assert_eq!(fs::metadata(&path).unwrap().ino(), replacement);
    assert_eq!(read(&path), saved);
}

#[test]
fn destination_symlink_never_modifies_its_target() {
    let (home, value, path) = fixture();
    let victim = home.path().join("victim");
    fs::write(&victim, b"preserve").unwrap();
    std::os::unix::fs::symlink(&victim, &path).unwrap();
    assert!(value.before_change("startup").is_err());
    drop(value);
    assert_eq!(fs::read(&victim).unwrap(), b"preserve");
    assert!(fs::symlink_metadata(&path).unwrap().file_type().is_symlink());
}

#[test]
fn directory_swap_cannot_redirect_publication_or_cleanup() {
    let (home, value, path) = fixture();
    value.connected().unwrap();
    value.committed(Some(THREAD)).unwrap();
    let original = path.parent().unwrap();
    let renamed = home.path().join("original-directory");
    fs::rename(original, &renamed).unwrap();
    fs::create_dir(original).unwrap();
    fs::set_permissions(original, fs::Permissions::from_mode(0o700)).unwrap();
    let victim = original.join(path.file_name().unwrap());
    fs::write(&victim, b"unrelated").unwrap();
    assert!(pulse(&value).is_err());
    drop(value);
    assert_eq!(fs::read(&victim).unwrap(), b"unrelated");
    assert_eq!(read(&renamed.join(path.file_name().unwrap()))["thread_id"], THREAD);
}

#[test]
fn marker_opt_out_prevents_positive_renewal() {
    let (_home, value, path) = fixture();
    value.connected().unwrap();
    value.committed(Some(THREAD)).unwrap();
    fs::remove_file(path.parent().unwrap().join("enabled-v1")).unwrap();
    let old = fs::metadata(&path).unwrap().ino();
    assert!(pulse(&value).is_err());
    assert_eq!(fs::metadata(&path).unwrap().ino(), old);
}

#[test]
fn last_owner_drop_revokes_but_a_borrowed_clone_drop_does_not() {
    let (_home, value, path) = fixture();
    value.connected().unwrap();
    value.committed(Some(THREAD)).unwrap();
    let borrowed = value.clone();
    drop(borrowed);
    assert_eq!(read(&path)["thread_id"], THREAD);
    drop(value);
    let record = read(&path);
    assert!(record["thread_id"].is_null());
    assert!(record["connection_epoch"].is_null());
    assert_eq!(record["reason"], "owner_exit");
}

#[test]
fn explicit_open_keeps_opt_in_and_does_not_mutate_process_environment() {
    let (home, _unused, path) = fixture();
    let before = std::env::var_os("CODEX_CLIENT_THREAD_OBSERVER");
    let value = ForegroundObserver::open_enabled(home.path()).unwrap();
    assert_eq!(std::env::var_os("CODEX_CLIENT_THREAD_OBSERVER"), before);
    assert_eq!(read(&path)["reason"], "startup");
    value.connected().unwrap();
    value.committed(Some(THREAD)).unwrap();
    assert_eq!(read(&path)["thread_id"], THREAD);
    drop(value);
    fs::remove_file(path.parent().unwrap().join("enabled-v1")).unwrap();
    assert!(ForegroundObserver::open_enabled(home.path()).is_err());
}

#[test]
fn failed_heartbeat_never_recovers_a_previous_positive_implicitly() {
    let (value, journal) = memory();
    value.connected().unwrap();
    value.committed(Some(THREAD)).unwrap();
    { let mut state = journal.lock().unwrap(); state.fail_store = true; state.fail_invalidate = true; }
    assert!(pulse(&value).is_err());
    assert!(value.is_suspended());
    let before = value.remaining_lease();
    { let mut state = journal.lock().unwrap(); state.fail_store = false; state.fail_invalidate = false; }
    pulse(&value).unwrap();
    assert!(journal.lock().unwrap().records.last().unwrap()["thread_id"].is_null());
    assert!(!value.is_suspended());
    assert!(value.remaining_lease() < before);
    assert_eq!(journal.lock().unwrap().records.iter().filter(|r| r["thread_id"] == THREAD).count(), 1);
}
