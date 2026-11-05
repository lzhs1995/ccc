//! Executed as codex_goal_extension library tests, with the real SQLite runtime.
use super::*;
use codex_protocol::config_types::ModeKind;
use codex_protocol::protocol::SessionSource;
use codex_protocol::protocol::TokenUsage;
use codex_utils_absolute_path::test_support::PathExt;
use std::time::Duration;

#[derive(Debug)]
struct Sink;
impl codex_extension_api::ExtensionEventSink for Sink {
    fn emit(&self, _event: codex_protocol::protocol::Event) {}
    fn emit_warning(&self, _warning: codex_extension_api::ExtensionWarning) {}
}

#[tokio::test]
async fn settlement_wait_cancelled_after_real_sqlite_commit_does_not_double_charge()
-> anyhow::Result<()> {
    let temp = tempfile::TempDir::new()?;
    let db = codex_state::StateRuntime::init(
        codex_state::SqliteConfig::new_for_testing(temp.path().abs()),
        "test-provider".into(),
    )
    .await?;
    let thread = ThreadId::new();
    let metadata = codex_state::ThreadMetadataBuilder::new(
        thread,
        db.sqlite().home().join(format!("rollout-{thread}.jsonl")),
        chrono::Utc::now(),
        SessionSource::Cli,
    );
    db.upsert_thread(&metadata.build("test-provider")).await?;
    let goal = db
        .thread_goals()
        .insert_thread_goal(
            thread,
            "same original Goal",
            codex_state::ThreadGoalStatus::Active,
            Some(123),
        )
        .await?
        .expect("new original Goal");
    let accounting = Arc::new(GoalAccountingState::default());
    let handle = GoalRuntimeHandle::new(
        thread,
        db.clone(),
        GoalEventEmitter::new(Arc::new(Sink)),
        GoalMetrics::new(None),
        Weak::new(),
        accounting.clone(),
        GoalRuntimeConfig {
            analytics: GoalAnalytics::new(codex_analytics::AnalyticsEventsClient::disabled()),
            enabled: true,
            tools_available_for_thread: true,
            tools_visible_for_thread: true,
            root_accounting_state: None,
        },
    );
    accounting.start_turn("original-turn", ModeKind::Default, &TokenUsage::default());
    accounting.mark_turn_goal_active("original-turn", goal.goal_id.clone());
    let expected = handle
        .compaction_recovery_goal_snapshot()
        .await
        .map_err(anyhow::Error::msg)?;
    let reservation = handle
        .compaction_recovery_goal_guard(&expected)
        .await
        .map_err(anyhow::Error::msg)?;
    accounting.record_token_usage(
        "original-turn",
        &TokenUsage {
            input_tokens: 11,
            total_tokens: 11,
            ..TokenUsage::default()
        },
    );
    let pause = Arc::new(RecoveryPostCommitPause::default());
    *handle.inner.recovery_post_commit.lock().unwrap() = Some(pause.clone());
    let waiter_reservation = reservation.clone();
    let waiter = tokio::spawn(async move {
        waiter_reservation
            .settle_original_usage("original-turn", "original-attempt")
            .await
    });
    tokio::time::timeout(Duration::from_secs(5), pause.committed.notified()).await?;
    // The real additive transaction is committed, but its watermark is not.
    assert_eq!(
        db.thread_goals()
            .get_thread_goal(thread)
            .await?
            .unwrap()
            .tokens_used,
        11
    );
    assert_eq!(
        accounting
            .progress_snapshot("original-turn")
            .unwrap()
            .token_delta,
        11
    );
    // This is exactly the cancellation produced by timeout dropping the caller;
    // it must not abort the spawned commit-to-watermark accounting task.
    waiter.abort();
    assert!(waiter.await.unwrap_err().is_cancelled());
    drop(reservation);
    let ordinary_handle = handle.clone();
    let entered = Arc::new(tokio::sync::Notify::new());
    let other_entered = entered.clone();
    let ordinary = tokio::spawn(async move {
        other_entered.notify_one();
        ordinary_handle
            .account_active_goal_progress(
                "original-turn",
                "ordinary-original-tool",
                codex_state::GoalAccountingMode::ActiveOnly,
                BudgetLimitedGoalDisposition::ClearActive,
            )
            .await
    });
    entered.notified().await;
    tokio::task::yield_now().await;
    assert!(
        !ordinary.is_finished(),
        "original progress permit must cover commit-to-mark"
    );
    pause.resume.notify_one();
    tokio::time::timeout(Duration::from_secs(5), ordinary)
        .await??
        .map_err(anyhow::Error::msg)?;
    let after = db.thread_goals().get_thread_goal(thread).await?.unwrap();
    assert_eq!(after.goal_id, goal.goal_id);
    assert_eq!(after.objective, goal.objective);
    assert_eq!(after.token_budget, Some(123));
    assert_eq!(after.tokens_used, 11);
    assert_eq!(
        accounting
            .progress_snapshot("original-turn")
            .map(|s| s.token_delta)
            .unwrap_or(0),
        0
    );
    Ok(())
}
