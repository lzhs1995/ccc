//! Binds Core's original occurrence to the original native TUI grant and Goal.
//! Receipt files are write-once; an I/O ambiguity is never a replay grant.
use ccc_native_client_foreground::CompactionGrantGuard;
use ccc_native_client_foreground::verify_compaction_grant;
use codex_api::compaction_dispatch::CompactionDispatchAuthority;
use codex_core::RecoveryPolicy;
use codex_core::RecoveryPolicyHandle;
use codex_goal_extension::GoalRuntimeHandle;
use codex_protocol::compact_recovery::CompactionRecoveryIdentity;
use codex_protocol::compact_recovery::CompactionRecoveryRequest;
use futures::future::BoxFuture;
use serde_json::Value;
use serde_json::json;
use std::collections::BTreeMap;
use std::ffi::CString;
use std::fs::File;
use std::fs::OpenOptions;
use std::fs::{self};
use std::io::Write;
use std::os::fd::AsRawFd;
use std::os::fd::FromRawFd;
use std::os::unix::fs::MetadataExt;
use std::os::unix::fs::OpenOptionsExt;
use std::path::Path;
use std::sync::Arc;
use std::sync::Mutex;

pub(super) struct NativeCompactionPolicy {
    goal: Arc<GoalRuntimeHandle>,
    requests: Mutex<BTreeMap<String, CompactionRecoveryRequest>>,
    reservations:
        Mutex<BTreeMap<String, std::sync::Weak<codex_goal_extension::CompactionGoalReservation>>>,
}

impl NativeCompactionPolicy {
    pub(super) fn handle(goal: Arc<GoalRuntimeHandle>) -> RecoveryPolicyHandle {
        RecoveryPolicyHandle(Arc::new(Self {
            goal,
            requests: Mutex::new(BTreeMap::new()),
            reservations: Mutex::new(BTreeMap::new()),
        }))
    }
    fn request(&self, attempt: &str) -> Result<CompactionRecoveryRequest, String> {
        self.requests
            .lock()
            .map_err(|_| "native request ledger poisoned")?
            .get(attempt)
            .cloned()
            .ok_or_else(|| "original native request absent".into())
    }
    fn matching_request(
        &self,
        expected: &CompactionRecoveryIdentity,
    ) -> Result<CompactionRecoveryRequest, String> {
        let requests = self
            .requests
            .lock()
            .map_err(|_| "native request ledger poisoned")?;
        let mut found = requests.values().filter(|request| {
            request.expected.native_epoch == expected.native_epoch
                && request.expected.thread_id == expected.thread_id
                && request.expected.turn_id == expected.turn_id
                && request.expected.occurrence == expected.occurrence
                && request.expected.generation.checked_add(1) == Some(expected.generation)
        });
        let request = found
            .next()
            .cloned()
            .ok_or("original generation receipt absent")?;
        if found.next().is_some() {
            return Err("ambiguous original native request".into());
        }
        Ok(request)
    }
    fn receipt(
        request: &CompactionRecoveryRequest,
        phase: &str,
        expected: &CompactionRecoveryIdentity,
    ) -> Result<String, String> {
        if !request
            .attempt_id
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || b"-_".contains(&c))
            || request.attempt_id.is_empty()
            || request.attempt_id.len() > 128
        {
            return Err("invalid original attempt id".into());
        }
        let proof = Path::new(&request.authority.proof_path);
        let directory = proof.parent().ok_or("native proof directory absent")?;
        let name = format!("{}-{phase}.json", request.attempt_id);
        let value = json!({"schema":1,"purpose":"native_compaction_recovery_receipt",
            "phase":phase,"attemptId":request.attempt_id,"controllerId":request.authority.controller_id,
            "enrollmentId":request.authority.enrollment_id,"originalIntent":request.authority.intent_path,
            "originalIntentSha256":request.authority.intent_sha256,"expected":expected,
            "serverPid":std::process::id(),"originalAuthority":request.authority,
            "timestampMs":std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH)
                .map_err(|_| "native clock before epoch")?.as_millis()});
        let raw = serde_json::to_vec(&value).map_err(|error| error.to_string())?;
        write_once(directory, &name, &raw).map_err(|error| error.to_string())?;
        Ok(directory.join(name).to_string_lossy().into_owned())
    }
}

impl RecoveryPolicy for NativeCompactionPolicy {
    fn admit<'a>(
        &'a self,
        request: &'a CompactionRecoveryRequest,
    ) -> BoxFuture<'a, Result<Arc<dyn CompactionDispatchAuthority>, String>> {
        Box::pin(async move {
            let expected = serde_json::to_value(request).map_err(|error| error.to_string())?;
            let client_guard = verify_compaction_grant(
                Path::new(&request.authority.proof_path),
                &request.authority.proof_sha256,
                &expected,
            )
            .map_err(|error| error.to_string())?;
            let remaining = client_guard
                .remaining()
                .map_err(|error| error.to_string())?;
            let goal = tokio::time::timeout(
                remaining,
                self.goal
                    .compaction_recovery_goal_guard(&request.authority.expected_goal),
            )
            .await
            .map_err(|_| "native original Goal authority deadline expired")??;
            self.reservations
                .lock()
                .map_err(|_| "native Goal ledger poisoned")?
                .insert(request.attempt_id.clone(), Arc::downgrade(&goal));
            Ok(Arc::new(NativeDispatchGuard {
                client: client_guard,
                _goal: Some(goal),
                request: request.clone(),
                replacement: None,
            }) as Arc<dyn CompactionDispatchAuthority>)
        })
    }
    fn persist_admission(&self, request: &CompactionRecoveryRequest) -> Result<String, String> {
        let mut requests = self
            .requests
            .lock()
            .map_err(|_| "native request ledger poisoned")?;
        if requests.contains_key(&request.attempt_id) {
            return Err("original attempt already consumed".into());
        }
        // Record in-memory before disk: a failed publication remains consumed.
        requests.insert(request.attempt_id.clone(), request.clone());
        Self::receipt(request, "admitted", &request.expected)
    }
    fn persist_drained(
        &self,
        expected: &CompactionRecoveryIdentity,
        attempt_id: &str,
    ) -> Result<(), String> {
        let request = self.request(attempt_id)?;
        if request.expected != *expected {
            return Err("original drain identity changed".into());
        }
        Self::receipt(&request, "drained", expected).map(|_| ())
    }
    fn authorize_replacement<'a>(
        &'a self,
        expected: &'a CompactionRecoveryIdentity,
        attempt_id: &'a str,
    ) -> BoxFuture<'a, Result<Arc<dyn CompactionDispatchAuthority>, String>> {
        Box::pin(async move {
            let request = self.request(attempt_id)?;
            if request.expected != *expected {
                return Err("original replacement identity changed".into());
            }
            let value = serde_json::to_value(&request).map_err(|error| error.to_string())?;
            let client_guard = verify_compaction_grant(
                Path::new(&request.authority.proof_path),
                &request.authority.proof_sha256,
                &value,
            )
            .map_err(|error| error.to_string())?;
            let goal = self
                .reservations
                .lock()
                .map_err(|_| "native Goal ledger poisoned")?
                .get(attempt_id)
                .and_then(std::sync::Weak::upgrade)
                .ok_or("original native Goal reservation disappeared")?;
            let remaining = client_guard
                .remaining()
                .map_err(|error| error.to_string())?;
            let updated = tokio::time::timeout(
                remaining,
                goal.settle_original_usage(&expected.turn_id, attempt_id),
            )
            .await
            .map_err(|_| "original native Goal settlement expired UNKNOWN")??;
            let name = format!("{}-goal-settled.json", request.attempt_id);
            let raw=serde_json::to_vec(&json!({"schema":1,"purpose":"native_compaction_goal_settlement","attemptId":attempt_id,
                "expected":expected,"originalGoal":request.authority.expected_goal,"settledGoal":updated,
                "intentSha256":request.authority.intent_sha256})).map_err(|e|e.to_string())?;
            write_once(
                Path::new(&request.authority.proof_path)
                    .parent()
                    .ok_or("native proof parent absent")?,
                &name,
                &raw,
            )
            .map_err(|e| e.to_string())?;
            // Keep original Goal permit through actual replacement dispatch.
            let mut replacement = expected.clone();
            replacement.generation = replacement
                .generation
                .checked_add(1)
                .ok_or("native generation overflow")?;
            Ok(Arc::new(NativeDispatchGuard {
                client: client_guard,
                _goal: Some(goal),
                request,
                replacement: Some(replacement),
            }) as Arc<dyn CompactionDispatchAuthority>)
        })
    }
    fn persist_replacement_dispatched(
        &self,
        expected: &CompactionRecoveryIdentity,
    ) -> Result<(), String> {
        let request = self.matching_request(expected)?;
        Self::receipt(&request, "replacement-dispatched", expected).map(|_| ())
    }
    fn persist_terminal(
        &self,
        expected: &CompactionRecoveryIdentity,
        attempt_id: Option<&str>,
        completed: bool,
    ) -> Result<(), String> {
        let request = if let Some(attempt_id) = attempt_id {
            let request = self.request(attempt_id)?;
            if request.expected != *expected {
                return Err("original terminal identity changed".into());
            }
            request
        } else {
            self.matching_request(expected)?
        };
        let phase = match (attempt_id.is_some(), completed) {
            (true, true) => "original-provider-completed",
            (true, false) => "original-provider-failed",
            (false, true) => "replacement-provider-completed",
            (false, false) => "replacement-provider-failed",
        };
        // Provider completion is not history-installation or Goal-completion proof.
        Self::receipt(&request, phase, expected).map(|_| ())
    }
}

struct NativeDispatchGuard {
    client: CompactionGrantGuard,
    _goal: Option<Arc<codex_goal_extension::CompactionGoalReservation>>,
    request: CompactionRecoveryRequest,
    replacement: Option<CompactionRecoveryIdentity>,
}
impl CompactionDispatchAuthority for NativeDispatchGuard {
    fn remaining(&self) -> Result<std::time::Duration, String> {
        self.client.remaining().map_err(|error| error.to_string())
    }
    fn validate_fresh(&self) -> Result<(), String> {
        self.client
            .validate_fresh()
            .map_err(|error| error.to_string())
    }
    fn persist_send_intent(&self) -> Result<(), String> {
        let expected = self
            .replacement
            .as_ref()
            .ok_or("admission guard cannot send")?;
        NativeCompactionPolicy::receipt(&self.request, "replacement-send-intent", expected)
            .map(|_| ())
    }
    fn persist_sent(&self) -> Result<(), String> {
        let expected = self
            .replacement
            .as_ref()
            .ok_or("admission guard cannot send")?;
        NativeCompactionPolicy::receipt(&self.request, "replacement-dispatched", expected)
            .map(|_| ())
    }
}

/// Directory-relative syscalls bind publication to the opened original inode.
/// A crash can leave a private temporary; neither a partial target nor an old
/// target is accepted as a new receipt. Parent replacement is reported UNKNOWN.
fn write_once(directory: &Path, name: &str, raw: &[u8]) -> std::io::Result<()> {
    let before = fs::symlink_metadata(directory)?;
    if !before.is_dir()
        || before.uid() != unsafe { libc::geteuid() }
        || before.mode() & 0o077 != 0
        || directory.canonicalize()? != directory
    {
        return Err(std::io::Error::other(
            "unsafe original native receipt directory",
        ));
    }
    let dir = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_DIRECTORY | libc::O_CLOEXEC)
        .open(directory)?;
    let opened = dir.metadata()?;
    if opened.ino() != before.ino() || opened.dev() != before.dev() {
        return Err(std::io::Error::other("native receipt directory changed"));
    }
    if name.is_empty() || name.contains('/') || name == "." || name == ".." {
        return Err(std::io::Error::other("unsafe receipt name"));
    }
    let target = CString::new(name).map_err(std::io::Error::other)?;
    let temp = CString::new(format!(".{name}.{}.tmp", uuid::Uuid::new_v4()))
        .map_err(std::io::Error::other)?;
    let fd = unsafe {
        libc::openat(
            dir.as_raw_fd(),
            temp.as_ptr(),
            libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL | libc::O_NOFOLLOW | libc::O_CLOEXEC,
            0o600,
        )
    };
    if fd < 0 {
        return Err(std::io::Error::last_os_error());
    }
    let mut file = unsafe { File::from_raw_fd(fd) };
    file.write_all(raw)?;
    file.sync_all()?;
    if unsafe {
        libc::linkat(
            dir.as_raw_fd(),
            temp.as_ptr(),
            dir.as_raw_fd(),
            target.as_ptr(),
            0,
        )
    } != 0
    {
        return Err(std::io::Error::last_os_error());
    }
    dir.sync_all()?;
    if unsafe { libc::unlinkat(dir.as_raw_fd(), temp.as_ptr(), 0) } != 0 {
        return Err(std::io::Error::last_os_error());
    }
    dir.sync_all()?;
    let after = fs::symlink_metadata(directory)?;
    if after.ino() != before.ino() || after.dev() != before.dev() {
        return Err(std::io::Error::other(
            "native receipt parent changed; reconcile original inode",
        ));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;
    use std::os::unix::fs::symlink;
    #[test]
    fn native_receipt_is_atomic_private_and_never_overwrites_original_attempt() {
        let root = tempfile::tempdir().unwrap();
        fs::set_permissions(root.path(), fs::Permissions::from_mode(0o700)).unwrap();
        let directory = root.path().canonicalize().unwrap();
        write_once(&directory, "original.json", b"{\"phase\":\"admitted\"}").unwrap();
        assert!(write_once(&directory, "original.json", b"changed").is_err());
        assert_eq!(
            fs::read(directory.join("original.json")).unwrap(),
            b"{\"phase\":\"admitted\"}"
        );
        assert_eq!(
            fs::metadata(directory.join("original.json"))
                .unwrap()
                .mode()
                & 0o077,
            0
        );
    }
    #[test]
    fn native_receipt_rejects_symlink_parent_and_path_escape() {
        let root = tempfile::tempdir().unwrap();
        let directory = root.path().canonicalize().unwrap();
        fs::set_permissions(&directory, fs::Permissions::from_mode(0o700)).unwrap();
        symlink(&directory, directory.join("alias")).unwrap();
        assert!(write_once(&directory.join("alias"), "bad.json", b"{}").is_err());
        assert!(write_once(&directory, "../bad.json", b"{}").is_err());
    }
}
