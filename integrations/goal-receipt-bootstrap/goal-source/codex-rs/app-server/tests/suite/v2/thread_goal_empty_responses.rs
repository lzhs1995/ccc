//! Exercises the goal extension's no-activity breaker through the public API.

use anyhow::Result;
use app_test_support::MockResponsesConfig;
use app_test_support::TestAppServer;
use app_test_support::create_mock_responses_server_sequence;
use codex_app_server_protocol::ConfigBatchWriteParams;
use codex_app_server_protocol::ConfigEdit;
use codex_app_server_protocol::ConfigWriteResponse;
use codex_app_server_protocol::HooksListParams;
use codex_app_server_protocol::HooksListResponse;
use codex_app_server_protocol::MergeStrategy;
use codex_app_server_protocol::ThreadGoalSetResponse;
use codex_app_server_protocol::ThreadGoalStatus;
use codex_app_server_protocol::ThreadGoalUpdatedNotification;
use codex_app_server_protocol::ThreadStartParams;
use codex_app_server_protocol::ThreadStartResponse;
use codex_app_server_protocol::TurnCompletedNotification;
use codex_app_server_protocol::TurnStatus;
use codex_features::Feature;
use core_test_support::responses;
use pretty_assertions::assert_eq;
use serde_json::json;
use tempfile::TempDir;
use tokio::time::Duration;
use tokio::time::timeout;

#[test_case::test_case(None; "empty")]
#[test_case::test_case(Some("final_answer"); "draft")]
#[test_case::test_case(Some("commentary"); "commentary")]
#[test_case::test_case(Some("tool"); "tool")]
#[test_case::test_case(Some("interrupt"); "interrupt")]
#[tokio::test]
async fn empty_goal_continuations_block_after_three_without_activity(
    recovery: Option<&str>,
) -> Result<()> {
    let turns = match recovery {
        Some("interrupt") => 4,
        Some(_) => 6,
        None => 3,
    };
    let mut scripts = Vec::new();
    for turn in 1..=turns {
        let mut id = format!("response-{turn}");
        let mut events = vec![responses::ev_response_created(&id)];
        if recovery == Some("interrupt") {
            events.push(responses::ev_assistant_message(
                "progress",
                "Ready for the next step",
            ));
            events.push(responses::ev_completed_with_tokens(
                &id, /*total_tokens*/ 330_000,
            ));
            scripts.push(responses::sse(events));
            break;
        }
        if turn == 3
            && let Some(recovery) = recovery
        {
            if recovery == "tool" {
                events.push(responses::ev_function_call("get-goal", "get_goal", "{}"));
                events.push(responses::ev_completed(&id));
                scripts.push(responses::sse(events));
                id.push_str("-after-tool");
                events = vec![responses::ev_response_created(&id)];
            } else {
                let mut added = responses::ev_assistant_message("progress", "");
                added["type"] = json!("response.output_item.added");
                events.push(added);
                events.push(responses::ev_output_text_delta("Useful progress"));
                let mut event = responses::ev_assistant_message("progress", "Useful progress");
                event["item"]["phase"] = json!(recovery);
                events.push(event);
            }
        }
        let mut empty_final = responses::ev_assistant_message(&format!("empty-final-{turn}"), "");
        empty_final["item"]["phase"] = json!("final_answer");
        events.push(empty_final);
        events.push(responses::ev_completed(&id));
        scripts.push(responses::sse(events));
    }
    let server = create_mock_responses_server_sequence(scripts).await;
    let codex_home = TempDir::new()?;
    let mut config = MockResponsesConfig::new(&server.uri())
        .with_model("gpt-5.4")
        .enable_feature(Feature::Goals);
    if recovery == Some("interrupt") {
        config = config
            .enable_feature(Feature::CodexHooks)
            .with_root_config(
                "model_auto_compact_token_limit = 200000\nmodel_post_turn_compact_threshold_percent = 0",
            )
            .with_extra_config(
                r#"
[[hooks.PreCompact]]
matcher = "auto"
[[hooks.PreCompact.hooks]]
type = "command"
command = """printf '%s' '{"continue":false}'"""
command_windows = """Write-Output '{"continue":false}'"""
"#,
            );
    }
    config.write(codex_home.path())?;
    let mut mcp = TestAppServer::builder()
        .with_codex_home(codex_home.path())
        .without_managed_config()
        .build_initialized()
        .await?;
    if recovery == Some("interrupt") {
        let list = mcp
            .send_hooks_list_request(HooksListParams {
                cwds: vec![codex_home.path().to_path_buf()],
            })
            .await?;
        let HooksListResponse { data } = mcp.read_response(list).await?;
        let hook = &data[0].hooks[0];
        let trust = mcp
            .send_config_batch_write_request(ConfigBatchWriteParams {
                edits: vec![ConfigEdit {
                    key_path: "hooks.state".to_string(),
                    value: json!({hook.key.clone(): {"trusted_hash": hook.current_hash}}),
                    merge_strategy: MergeStrategy::Upsert,
                }],
                file_path: None,
                expected_version: None,
                reload_user_config: true,
            })
            .await?;
        let _: ConfigWriteResponse = mcp.read_response(trust).await?;
    }
    let request = mcp
        .send_thread_start_request_with_auto_env(ThreadStartParams::default())
        .await?;
    let ThreadStartResponse { thread, .. } = mcp.read_response(request).await?;
    let request = mcp
        .send_raw_request(
            "thread/goal/set",
            Some(json!({"threadId": thread.id, "objective": "Finish the work"})),
        )
        .await?;
    let _: ThreadGoalSetResponse = mcp.read_response(request).await?;

    for turn in 1..=turns {
        if turn == 3 && matches!(recovery, Some("final_answer" | "commentary")) {
            let delta: serde_json::Value = timeout(
                Duration::from_secs(30),
                mcp.read_notification("item/agentMessage/delta"),
            )
            .await??;
            assert_eq!(json!("Useful progress"), delta["delta"]);
        }
        let completed: TurnCompletedNotification = timeout(
            Duration::from_secs(30),
            mcp.read_notification("turn/completed"),
        )
        .await??;
        let expected = if recovery == Some("interrupt") && turn > 1 {
            TurnStatus::Interrupted
        } else {
            TurnStatus::Completed
        };
        assert_eq!(expected, completed.turn.status);
        assert_eq!(None, completed.turn.error);
    }
    timeout(Duration::from_secs(30), async {
        loop {
            let notification: ThreadGoalUpdatedNotification =
                mcp.read_notification("thread/goal/updated").await?;
            if notification.goal.status == ThreadGoalStatus::Blocked {
                return Ok::<_, anyhow::Error>(());
            }
        }
    })
    .await??;
    server.verify().await;
    Ok(())
}

/// A real app-server/Core round trip: the conditional request's durable turn
/// must match the actual first new turn notification. Generic Goal activity is insufficient.
#[tokio::test]
async fn conditional_goal_receipt_binds_real_admission_and_duplicate_is_read_only() -> Result<()> {
    use codex_app_server_protocol::ThreadGoalRecoverResponse;
    use codex_app_server_protocol::ThreadGoalRecoveryReadResponse;
    use codex_protocol::goal_recovery::GoalRecoveryPhase;
    let scripts = (1..=6).map(|turn| {
        let id = format!("receipt-response-{turn}");
        let mut empty = responses::ev_assistant_message(&format!("receipt-empty-{turn}"), "");
        empty["item"]["phase"] = json!("final_answer");
        responses::sse(vec![responses::ev_response_created(&id), empty, responses::ev_completed(&id)])
    }).collect();
    let server = create_mock_responses_server_sequence(scripts).await;
    let codex_home = TempDir::new()?;
    MockResponsesConfig::new(&server.uri()).with_model("gpt-5.4").enable_feature(Feature::Goals).write(codex_home.path())?;
    let mut mcp = TestAppServer::builder().with_codex_home(codex_home.path())
        .without_managed_config().build_initialized().await?;
    let id = mcp.send_thread_start_request_with_auto_env(ThreadStartParams::default()).await?;
    let ThreadStartResponse { thread, .. } = mcp.read_response(id).await?;
    let id = mcp.send_raw_request("thread/goal/set", Some(json!({"threadId":thread.id,"objective":"Finish the receipt work"}))).await?;
    let _: ThreadGoalSetResponse = mcp.read_response(id).await?;
    let mut previous = String::new();
    for _ in 0..3 {
        let _: serde_json::Value = timeout(Duration::from_secs(30), mcp.read_notification("turn/started")).await??;
        let completed: TurnCompletedNotification = timeout(Duration::from_secs(30), mcp.read_notification("turn/completed")).await??;
        previous = completed.turn.id;
    }
    timeout(Duration::from_secs(30), async {
        loop {
            let notification: ThreadGoalUpdatedNotification = mcp.read_notification("thread/goal/updated").await?;
            if notification.goal.status == ThreadGoalStatus::Blocked { return Ok::<_,anyhow::Error>(()); }
        }
    }).await??;
    let id = mcp.send_raw_request("thread/goal/recovery/read", Some(json!({"threadId":thread.id,"attemptId":null}))).await?;
    let snapshot: ThreadGoalRecoveryReadResponse = mcp.read_response(id).await?;
    assert_eq!(snapshot.capability, "goal-recovery-receipt-v1");
    let goal = snapshot.snapshot.expect("existing blocked Goal");
    let params = json!({"threadId":thread.id,"attemptId":"one-original-native-attempt",
        "expectedRuntimeId":snapshot.runtime_id.expect("live runtime"),"expectedPreviousTurnId":previous,
        "expectedGoalId":goal.goal_id,"expectedGoalRevision":goal.revision});
    let id = mcp.send_raw_request("thread/goal/recover", Some(params.clone())).await?;
    let first: ThreadGoalRecoverResponse = mcp.read_response(id).await?;
    assert_eq!(first.receipt.phase, GoalRecoveryPhase::Started);
    let started: serde_json::Value = timeout(Duration::from_secs(30),mcp.read_notification("turn/started")).await??;
    assert_eq!(started["turn"]["id"], json!(first.receipt.turn_id));
    let id = mcp.send_raw_request("thread/goal/recover", Some(params)).await?;
    let duplicate: ThreadGoalRecoverResponse = mcp.read_response(id).await?;
    assert_eq!(duplicate.receipt,first.receipt);
    let id = mcp.send_raw_request("thread/goal/recovery/read",Some(json!({"threadId":thread.id,"attemptId":"one-original-native-attempt"}))).await?;
    let read: ThreadGoalRecoveryReadResponse = mcp.read_response(id).await?;
    assert_eq!(read.receipt,Some(first.receipt));
    for _ in 0..3 { let _: TurnCompletedNotification = timeout(Duration::from_secs(30),mcp.read_notification("turn/completed")).await??; }
    server.verify().await;
    Ok(())
}
