//! Exact native compaction control identity. These values describe a running
//! native actor; a rollout item by itself is not a current control capability.
use serde::Deserialize;
use serde::Serialize;

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct CompactionRecoveryIdentity {
    pub native_epoch: String,
    pub thread_id: String,
    pub turn_id: String,
    pub occurrence: String,
    pub generation: u64,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct CompactionRecoveryRequest {
    pub expected: CompactionRecoveryIdentity,
    pub attempt_id: String,
    pub authority: CompactionRecoveryAuthority,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct CompactionRecoverySnapshot {
    pub identity: CompactionRecoveryIdentity,
    pub phase: String,
    pub elapsed_ms: u64,
    pub minimum_stall_ms: u64,
    pub remaining_attempts: usize,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct CompactionRecoveryAdmission {
    pub attempt_id: String,
    pub accepted: bool,
    pub status: String,
    pub receipt_path: Option<String>,
    /// Admission never proves stream cancellation or replacement dispatch.
    pub interruption_completed: bool,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct CompactionRecoveryAuthority {
    pub expected_client_pid: u32,
    pub expected_client_birth: Vec<i64>,
    pub expected_client_epoch: String,
    pub expected_connection_epoch: String,
    pub expected_selection_generation: u64,
    pub intent_path: String,
    pub intent_sha256: String,
    pub controller_id: String,
    pub enrollment_id: String,
    pub proof_path: String,
    pub proof_sha256: String,
    pub expected_goal: serde_json::Value,
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn native_occurrence_identity_does_not_accept_foreign_fields_or_missing_generation() {
        let mut value = serde_json::json!({"nativeEpoch":"e","threadId":"t","turnId":"u","occurrence":"o","generation":0});
        let parsed: CompactionRecoveryIdentity = serde_json::from_value(value.clone()).unwrap();
        assert_eq!(parsed.generation, 0);
        value["legacyThread"] = serde_json::json!("other");
        assert!(serde_json::from_value::<CompactionRecoveryIdentity>(value.clone()).is_err());
        value.as_object_mut().unwrap().remove("legacyThread");
        value.as_object_mut().unwrap().remove("generation");
        assert!(serde_json::from_value::<CompactionRecoveryIdentity>(value).is_err());
    }
}
