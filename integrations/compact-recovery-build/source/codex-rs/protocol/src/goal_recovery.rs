//! Conditional Goal admission receipts. A receipt is not model/tool progress.
use schemars::JsonSchema;
use serde::Deserialize;
use serde::Serialize;
use ts_rs::TS;

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize, JsonSchema, TS)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct GoalRecoveryRequest {
    pub thread_id: String,
    pub attempt_id: String,
    /// Native runtime identity returned by the read endpoint; never caller-invented.
    pub expected_runtime_id: String,
    pub expected_previous_turn_id: String,
    pub expected_goal_id: String,
    pub expected_goal_revision: i64,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize, JsonSchema, TS)]
#[serde(rename_all = "snake_case")]
pub enum GoalRecoveryPhase {
    /// No task release is implied. After cancellation this remains UNKNOWN.
    Intent,
    /// The mapping and Goal activation committed before task release.
    /// A crash can occur before or after release: this alone is not Started.
    Admitted,
    /// Core observed release, and this post-release observation committed.
    Started,
    /// Core definitively declined before committing admission.
    Rejected,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize, JsonSchema, TS)]
#[serde(rename_all = "camelCase")]
pub struct GoalRecoveryReceipt {
    pub request: GoalRecoveryRequest,
    pub phase: GoalRecoveryPhase,
    pub turn_id: Option<String>,
    pub admitted_goal_revision: Option<i64>,
    pub reason: Option<String>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize, JsonSchema, TS)]
#[serde(rename_all = "camelCase")]
pub struct GoalRecoverySnapshot {
    pub goal_id: String,
    pub revision: i64,
    pub status: String,
}
