//! Durable conditional Goal receipts on the existing Goal database.
//! All writes use a separate EXTRA connection. No transaction/commit is retried:
//! cancellation or a lost commit response is UNKNOWN and requires read-only lookup.
use super::GoalStore;
use anyhow::{Context, bail, ensure};
use codex_protocol::goal_recovery::{GoalRecoveryPhase, GoalRecoveryReceipt, GoalRecoveryRequest, GoalRecoverySnapshot};
use sqlx::{ConnectOptions, Connection, Row, SqliteConnection};
use sqlx::sqlite::{SqliteRow, SqliteSynchronous};

fn validate(request: &GoalRecoveryRequest) -> anyhow::Result<()> {
    ensure!(!request.attempt_id.is_empty() && request.attempt_id.len() <= 128
        && request.attempt_id.bytes().all(|c| c.is_ascii_alphanumeric() || c == b'_' || c == b'-'), "invalid attempt token");
    codex_protocol::ThreadId::try_from(request.thread_id.clone())?;
    uuid::Uuid::parse_str(&request.expected_previous_turn_id)?;
    uuid::Uuid::parse_str(&request.expected_runtime_id)?;
    ensure!(!request.expected_goal_id.is_empty() && request.expected_goal_revision > 0, "invalid Goal fence");
    Ok(())
}

fn decode(row: &SqliteRow) -> anyhow::Result<GoalRecoveryReceipt> {
    let phase: String = row.try_get("phase")?;
    Ok(GoalRecoveryReceipt {
        request: serde_json::from_str(&row.try_get::<String, _>("request_json")?)?,
        phase: match phase.as_str() {
            "intent" => GoalRecoveryPhase::Intent,
            "admitted" => GoalRecoveryPhase::Admitted,
            "started" => GoalRecoveryPhase::Started,
            "rejected" => GoalRecoveryPhase::Rejected,
            _ => bail!("invalid persisted Goal receipt phase"),
        },
        turn_id: row.try_get("turn_id")?,
        admitted_goal_revision: row.try_get("admitted_goal_revision")?,
        reason: row.try_get("reason")?,
    })
}

impl GoalStore {
    async fn recovery_connection(&self) -> anyhow::Result<SqliteConnection> {
        // Inherit the exact native Goal database path, but never create/recover a
        // different database and never change the ordinary pool's sync policy.
        let options = self.pool.connect_options().as_ref().clone()
            .create_if_missing(false).synchronous(SqliteSynchronous::Extra);
        let mut connection = options.connect().await?;
        let synchronous: i64 = sqlx::query_scalar("PRAGMA synchronous").fetch_one(&mut connection).await?;
        ensure!(synchronous == 3, "Goal receipt connection is not synchronous EXTRA");
        Ok(connection)
    }

    pub async fn goal_recovery_snapshot(&self, thread_id: &str) -> anyhow::Result<Option<GoalRecoverySnapshot>> {
        let row = sqlx::query("SELECT g.goal_id, g.status, r.revision FROM thread_goals g JOIN thread_goal_revisions r USING(thread_id) WHERE g.thread_id = ?")
            .bind(thread_id).fetch_optional(self.pool.as_ref()).await?;
        row.map(|row| Ok(GoalRecoverySnapshot { goal_id: row.try_get("goal_id")?, status: row.try_get("status")?, revision: row.try_get("revision")? })).transpose()
    }

    /// Pure lookup. It never upgrades Intent/Admitted to Started from nearby activity.
    pub async fn read_goal_recovery(&self, thread_id: &str, attempt_id: &str) -> anyhow::Result<Option<GoalRecoveryReceipt>> {
        let row = sqlx::query("SELECT * FROM thread_goal_recovery_receipts WHERE thread_id = ? AND attempt_id = ?")
            .bind(thread_id).bind(attempt_id).fetch_optional(self.pool.as_ref()).await?;
        row.as_ref().map(decode).transpose()
    }

    /// Returns created=false for an existing exact token; the caller must not submit again.
    pub async fn begin_goal_recovery(&self, request: &GoalRecoveryRequest) -> anyhow::Result<(GoalRecoveryReceipt, bool)> {
        validate(request)?;
        let encoded = serde_json::to_string(request)?;
        let mut connection = self.recovery_connection().await?;
        let mut tx = connection.begin_with("BEGIN IMMEDIATE").await?;
        if let Some(row) = sqlx::query("SELECT * FROM thread_goal_recovery_receipts WHERE attempt_id = ?")
            .bind(&request.attempt_id).fetch_optional(&mut *tx).await? {
            let receipt = decode(&row)?;
            ensure!(receipt.request == *request, "attempt token is already bound to another request");
            tx.rollback().await?;
            return Ok((receipt, false));
        }
        let snapshot = sqlx::query("SELECT g.goal_id, g.status, r.revision FROM thread_goals g JOIN thread_goal_revisions r USING(thread_id) WHERE g.thread_id = ?")
            .bind(&request.thread_id).fetch_optional(&mut *tx).await?.context("Goal absent")?;
        ensure!(snapshot.try_get::<String, _>("goal_id")? == request.expected_goal_id
            && snapshot.try_get::<String, _>("status")? == "blocked"
            && snapshot.try_get::<i64, _>("revision")? == request.expected_goal_revision, "Goal snapshot superseded");
        sqlx::query("INSERT INTO thread_goal_recovery_receipts(attempt_id, thread_id, goal_id, request_json, phase) VALUES(?, ?, ?, ?, 'intent')")
            .bind(&request.attempt_id).bind(&request.thread_id).bind(&request.expected_goal_id).bind(encoded).execute(&mut *tx).await?;
        tx.commit().await?;
        Ok((GoalRecoveryReceipt { request: request.clone(), phase: GoalRecoveryPhase::Intent,
            turn_id: None, admitted_goal_revision: None, reason: None }, true))
    }

    /// Called only from Core with its real reserved idle turn, before start_task.
    pub async fn admit_goal_recovery(&self, request: &GoalRecoveryRequest, turn_id: &str) -> anyhow::Result<()> {
        validate(request)?;
        uuid::Uuid::parse_str(turn_id)?;
        let mut connection = self.recovery_connection().await?;
        let mut tx = connection.begin_with("BEGIN IMMEDIATE").await?;
        let row = sqlx::query("SELECT * FROM thread_goal_recovery_receipts WHERE attempt_id = ?")
            .bind(&request.attempt_id).fetch_optional(&mut *tx).await?.context("missing original Goal intent")?;
        let receipt = decode(&row)?;
        ensure!(receipt.request == *request && receipt.phase == GoalRecoveryPhase::Intent
            && row.try_get::<i64, _>("fence_active")? == 1, "Goal intent already consumed or superseded");
        let result = sqlx::query("UPDATE thread_goals SET status = 'active', updated_at_ms = MAX(updated_at_ms + 1, ?) WHERE thread_id = ? AND goal_id = ? AND status = 'blocked' AND (token_budget IS NULL OR tokens_used < token_budget) AND EXISTS(SELECT 1 FROM thread_goal_revisions r WHERE r.thread_id = thread_goals.thread_id AND r.revision = ?) AND NOT EXISTS(SELECT 1 FROM thread_goal_continuation_deferrals d WHERE d.thread_id = thread_goals.thread_id)")
            .bind(chrono::Utc::now().timestamp_millis()).bind(&request.thread_id).bind(&request.expected_goal_id).bind(request.expected_goal_revision)
            .execute(&mut *tx).await?;
        ensure!(result.rows_affected() == 1, "Goal admission snapshot/budget/deferral changed");
        let revision: i64 = sqlx::query_scalar("SELECT revision FROM thread_goal_revisions WHERE thread_id = ?")
            .bind(&request.thread_id).fetch_one(&mut *tx).await?;
        sqlx::query("UPDATE thread_goal_recovery_receipts SET phase = 'admitted', turn_id = ?, admitted_goal_revision = ? WHERE attempt_id = ? AND phase = 'intent'")
            .bind(turn_id).bind(revision).bind(&request.attempt_id).execute(&mut *tx).await?;
        tx.commit().await?;
        Ok(())
    }

    /// Post-release evidence only. Failure leaves Admitted/UNKNOWN and the fence.
    pub async fn mark_goal_recovery_started(&self, request: &GoalRecoveryRequest, turn_id: &str) -> anyhow::Result<()> {
        let mut connection = self.recovery_connection().await?;
        let mut tx = connection.begin_with("BEGIN IMMEDIATE").await?;
        let result = sqlx::query("UPDATE thread_goal_recovery_receipts SET phase = 'started', fence_active = 0 WHERE attempt_id = ? AND thread_id = ? AND request_json = ? AND phase = 'admitted' AND turn_id = ?")
            .bind(&request.attempt_id).bind(&request.thread_id).bind(serde_json::to_string(request)?).bind(turn_id).execute(&mut *tx).await?;
        ensure!(result.rows_affected() == 1, "post-release receipt binding changed");
        tx.commit().await?;
        Ok(())
    }

    /// Only a definite pre-admission NotSubmitted may reach this method.
    pub async fn reject_goal_recovery(&self, request: &GoalRecoveryRequest, reason: &str) -> anyhow::Result<()> {
        let mut connection = self.recovery_connection().await?;
        let mut tx = connection.begin_with("BEGIN IMMEDIATE").await?;
        let result = sqlx::query("UPDATE thread_goal_recovery_receipts SET phase = 'rejected', reason = ?, fence_active = 0 WHERE attempt_id = ? AND thread_id = ? AND request_json = ? AND phase = 'intent'")
            .bind(reason).bind(&request.attempt_id).bind(&request.thread_id).bind(serde_json::to_string(request)?).execute(&mut *tx).await?;
        ensure!(result.rows_affected() == 1, "cannot reject a potentially admitted request");
        tx.commit().await?;
        Ok(())
    }

    pub async fn has_goal_recovery_fence(&self, thread_id: &str) -> anyhow::Result<bool> {
        sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM thread_goal_recovery_receipts WHERE thread_id = ? AND fence_active = 1)")
            .bind(thread_id).fetch_one(self.pool.as_ref()).await.map_err(Into::into)
    }

    /// A later accepted user Goal mutation may proceed. Preserve receipt phase and
    /// turn binding: this clears its automatic-work fence, never fabricates an ACK.
    pub async fn supersede_goal_recovery_fences(&self, thread_id: &str, reason: &str) -> anyhow::Result<()> {
        if !self.has_goal_recovery_fence(thread_id).await? { return Ok(()); }
        let mut connection = self.recovery_connection().await?;
        let mut tx = connection.begin_with("BEGIN IMMEDIATE").await?;
        sqlx::query("UPDATE thread_goal_recovery_receipts SET fence_active = 0, reason = ? WHERE thread_id = ? AND fence_active = 1")
            .bind(reason).bind(thread_id).execute(&mut *tx).await?;
        tx.commit().await?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::SqliteConfig;
    use crate::StateRuntime;
    use crate::ThreadGoalStatus;
    use codex_utils_absolute_path::test_support::PathExt;

    async fn fixture() -> (Arc<StateRuntime>, GoalRecoveryRequest) {
        let root = crate::runtime::test_support::unique_temp_dir();
        let state = StateRuntime::init(SqliteConfig::new_for_testing(root.as_path().abs()), "test".to_string()).await.unwrap();
        let thread = codex_protocol::ThreadId::new();
        let metadata = crate::runtime::test_support::test_thread_metadata(&root, thread, root.join("workspace"));
        state.upsert_thread(&metadata).await.unwrap();
        let goal = state.thread_goals().replace_thread_goal(thread, "keep objective and budget", ThreadGoalStatus::Blocked, Some(1000)).await.unwrap();
        let snapshot = state.thread_goals().goal_recovery_snapshot(&thread.to_string()).await.unwrap().unwrap();
        let request = GoalRecoveryRequest { thread_id: thread.to_string(), attempt_id: "original-token".to_string(),
            expected_runtime_id: uuid::Uuid::new_v4().to_string(), expected_previous_turn_id: uuid::Uuid::new_v4().to_string(),
            expected_goal_id: goal.goal_id, expected_goal_revision: snapshot.revision };
        (state, request)
    }
    use std::sync::Arc;

    #[tokio::test]
    async fn recovery_token_exact_duplicate_never_creates_second_intent() {
        let (state, request) = fixture().await;
        let store = state.thread_goals();
        assert!(store.begin_goal_recovery(&request).await.unwrap().1);
        assert!(!store.begin_goal_recovery(&request).await.unwrap().1);
        let mut changed = request.clone(); changed.expected_previous_turn_id = uuid::Uuid::new_v4().to_string();
        assert!(store.begin_goal_recovery(&changed).await.is_err());
        assert_eq!(store.read_goal_recovery(&request.thread_id, &request.attempt_id).await.unwrap().unwrap().phase, GoalRecoveryPhase::Intent);
    }

    #[tokio::test]
    async fn admitted_mapping_is_unknown_until_post_release_commit() {
        let (state, request) = fixture().await;
        let store = state.thread_goals();
        let connection = store.recovery_connection().await.unwrap();
        drop(connection); // opening verifies the actual connection PRAGMA.
        store.begin_goal_recovery(&request).await.unwrap();
        let turn = uuid::Uuid::new_v4().to_string();
        store.admit_goal_recovery(&request, &turn).await.unwrap();
        let admitted = store.read_goal_recovery(&request.thread_id, &request.attempt_id).await.unwrap().unwrap();
        assert_eq!(admitted.phase, GoalRecoveryPhase::Admitted);
        assert_eq!(admitted.turn_id.as_deref(), Some(turn.as_str()));
        assert!(store.has_goal_recovery_fence(&request.thread_id).await.unwrap());
        assert!(!store.begin_goal_recovery(&request).await.unwrap().1);
        store.mark_goal_recovery_started(&request, &turn).await.unwrap();
        assert!(!store.has_goal_recovery_fence(&request.thread_id).await.unwrap());
        assert_eq!(store.read_goal_recovery(&request.thread_id, &request.attempt_id).await.unwrap().unwrap().phase, GoalRecoveryPhase::Started);
        let goal = store.get_thread_goal(codex_protocol::ThreadId::try_from(request.thread_id.clone()).unwrap()).await.unwrap().unwrap();
        assert_eq!(goal.objective, "keep objective and budget");
        assert_eq!(goal.token_budget, Some(1000)); assert_eq!(goal.tokens_used, 0);
    }

    #[tokio::test]
    async fn stale_usage_revision_refuses_admission_without_changing_goal() {
        let (state, request) = fixture().await;
        let store = state.thread_goals();
        store.begin_goal_recovery(&request).await.unwrap();
        sqlx::query("UPDATE thread_goals SET tokens_used = tokens_used + 1 WHERE thread_id = ?")
            .bind(&request.thread_id).execute(store.pool.as_ref()).await.unwrap();
        assert!(store.admit_goal_recovery(&request, &uuid::Uuid::new_v4().to_string()).await.is_err());
        assert_eq!(store.goal_recovery_snapshot(&request.thread_id).await.unwrap().unwrap().status, "blocked");
        assert_eq!(store.read_goal_recovery(&request.thread_id, &request.attempt_id).await.unwrap().unwrap().phase, GoalRecoveryPhase::Intent);
    }

    #[tokio::test]
    async fn admitted_receipt_survives_normal_usage_and_goal_replacement() {
        let (state, request) = fixture().await;
        let store = state.thread_goals(); let turn = uuid::Uuid::new_v4().to_string();
        store.begin_goal_recovery(&request).await.unwrap(); store.admit_goal_recovery(&request, &turn).await.unwrap();
        sqlx::query("UPDATE thread_goals SET tokens_used = tokens_used + 1 WHERE thread_id = ?")
            .bind(&request.thread_id).execute(store.pool.as_ref()).await.unwrap();
        store.mark_goal_recovery_started(&request, &turn).await.unwrap();
        let before = store.read_goal_recovery(&request.thread_id, &request.attempt_id).await.unwrap();
        store.replace_thread_goal(codex_protocol::ThreadId::try_from(request.thread_id.clone()).unwrap(), "new objective", ThreadGoalStatus::Blocked, None).await.unwrap();
        assert_eq!(store.read_goal_recovery(&request.thread_id, &request.attempt_id).await.unwrap(), before);
    }

    #[tokio::test]
    async fn admission_transaction_failure_rolls_back_goal_activation() {
        let (state, request) = fixture().await;
        let store = state.thread_goals(); store.begin_goal_recovery(&request).await.unwrap();
        sqlx::query("CREATE TRIGGER fail_admit BEFORE UPDATE OF phase ON thread_goal_recovery_receipts WHEN NEW.phase = 'admitted' BEGIN SELECT RAISE(ABORT, 'injected receipt write failure'); END")
            .execute(store.pool.as_ref()).await.unwrap();
        assert!(store.admit_goal_recovery(&request, &uuid::Uuid::new_v4().to_string()).await.is_err());
        assert_eq!(store.goal_recovery_snapshot(&request.thread_id).await.unwrap().unwrap().status, "blocked");
        assert_eq!(store.read_goal_recovery(&request.thread_id, &request.attempt_id).await.unwrap().unwrap().phase, GoalRecoveryPhase::Intent);
    }

    #[tokio::test]
    async fn post_release_write_failure_keeps_admitted_and_rejects_false_not_sent() {
        let (state, request) = fixture().await;
        let store = state.thread_goals(); let turn = uuid::Uuid::new_v4().to_string();
        store.begin_goal_recovery(&request).await.unwrap(); store.admit_goal_recovery(&request, &turn).await.unwrap();
        sqlx::query("CREATE TRIGGER fail_started BEFORE UPDATE OF phase ON thread_goal_recovery_receipts WHEN NEW.phase = 'started' BEGIN SELECT RAISE(ABORT, 'injected post-release failure'); END")
            .execute(store.pool.as_ref()).await.unwrap();
        assert!(store.mark_goal_recovery_started(&request, &turn).await.is_err());
        assert!(store.reject_goal_recovery(&request, "not sent").await.is_err());
        assert_eq!(store.read_goal_recovery(&request.thread_id, &request.attempt_id).await.unwrap().unwrap().phase, GoalRecoveryPhase::Admitted);
        assert!(store.has_goal_recovery_fence(&request.thread_id).await.unwrap());
    }
}
