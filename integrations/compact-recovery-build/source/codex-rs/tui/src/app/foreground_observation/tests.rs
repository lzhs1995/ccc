//! Actual App attachment, routing and reconnect flows backed by a private file journal.

use super::*;
use crate::app::foreground_observation::NativeForeground;
use crate::app::foreground_observation::StartupForegroundGuard;
use crate::app::tests::disconnect::serve_reconnect_requests;
use crate::app::tests::session_lifecycle_requests::HistoryCapabilities;
use crate::app::tests::session_lifecycle_requests::recorded_params;
use crate::app::tests::session_lifecycle_requests::start_recording_app_server_with_history;
use crate::backend_id::BackendId;
use ccc_native_client_foreground::ForegroundObserver;
use codex_protocol::models::ActivePermissionProfile;
use pretty_assertions::assert_eq;
use std::fs::DirBuilder;
use std::fs::OpenOptions;
use std::io::Write;
use std::os::unix::fs::DirBuilderExt;
use std::os::unix::fs::OpenOptionsExt;
use tempfile::TempDir;
use tokio::net::TcpListener;
use tokio::task::JoinHandle;

fn observer_clock_ns() -> u64 {
    let mut stamp = libc::timespec {
        tv_sec: 0,
        tv_nsec: 0,
    };
    assert_eq!(
        unsafe { libc::clock_gettime(libc::CLOCK_MONOTONIC, &mut stamp) },
        0
    );
    u64::try_from(stamp.tv_sec).expect("nonnegative monotonic seconds") * 1_000_000_000
        + u64::try_from(stamp.tv_nsec).expect("nonnegative monotonic nanoseconds")
}

struct ObservationFiles {
    home: TempDir,
}

impl ObservationFiles {
    fn new() -> Result<Self> {
        let home = tempfile::tempdir()?;
        let directory = home.path().join("credential-observations");
        DirBuilder::new().mode(0o700).create(&directory)?;
        let mut marker = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(directory.join("enabled-v1"))?;
        marker.write_all(b"ccc-request-credentials-v1\n")?;
        Ok(Self { home })
    }

    fn marker_path(&self) -> PathBuf {
        self.home.path().join("credential-observations/enabled-v1")
    }

    fn record_path(&self) -> PathBuf {
        self.home.path().join(format!(
            "credential-observations/client-{}-thread.json",
            std::process::id()
        ))
    }

    fn record(&self) -> Result<serde_json::Value> {
        Ok(serde_json::from_slice(&std::fs::read(self.record_path())?)?)
    }

    fn assert_clear(&self) -> Result<()> {
        let record = self.record()?;
        assert!(record["thread_id"].is_null(), "{record}");
        assert_eq!(record["control_eligible"], false);
        Ok(())
    }

    fn assert_live(&self, thread_id: ThreadId) -> Result<serde_json::Value> {
        let record = self.record()?;
        assert_eq!(record["thread_id"], thread_id.to_string());
        assert_eq!(record["control_eligible"], true);
        assert_eq!(record["schema"], 2);
        assert_eq!(record["monotonic_clock"], "clock_monotonic");
        Ok(record)
    }
}

struct ForegroundFixture {
    app: Box<App>,
    events: mpsc::UnboundedReceiver<AppEvent>,
    _ops: mpsc::UnboundedReceiver<Op>,
    server: AppServerSession,
    tui: crate::tui::Tui,
    files: ObservationFiles,
}

impl ForegroundFixture {
    async fn new() -> Result<Self> {
        let (mut app, events, ops) = boxed_future!(make_test_app_with_channels()).await;
        app.local_settings.tui.show_tooltips = false;
        app.local_settings.tui.show_server_version_notice = false;
        let server =
            boxed_future!(crate::start_embedded_app_server_for_picker(&app.config)).await?;
        let files = ObservationFiles::new()?;
        app.foreground = NativeForeground::open_enabled(files.home.path())?;
        app.native_foreground_connected(server.backend_id());
        Ok(Self {
            app,
            events,
            _ops: ops,
            server,
            tui: crate::tui::test_support::make_test_tui()?,
            files,
        })
    }

    async fn enqueue(&mut self, thread_id: ThreadId, turns: Vec<Turn>) -> Result<()> {
        self.app.before_foreground_mutation("native_attach").await;
        boxed_future!(self.app.enqueue_primary_thread_session(
            test_thread_session(thread_id, self.app.config.cwd.to_path_buf()),
            turns,
        ))
        .await
    }

    async fn settle_events(&mut self) -> Result<()> {
        for _ in 0..32 {
            while let Ok(event) = self.events.try_recv() {
                self.app.before_foreground_mutation("app_event").await;
                let control = boxed_future!(self.app.handle_event(
                    &mut self.tui,
                    &mut self.server,
                    event,
                ))
                .await?;
                assert!(matches!(control, AppRunControl::Continue));
            }
            boxed_future!(self.app.drain_active_thread_events(&mut self.tui)).await?;
            if self.events.is_empty()
                && self
                    .app
                    .active_thread_rx
                    .as_ref()
                    .is_none_or(|rx| rx.is_empty())
            {
                return Ok(());
            }
        }
        color_eyre::eyre::bail!("foreground fixture did not finish its buffered App events")
    }

    fn checkpoint(&mut self, startup_ready: bool) {
        self.app.native_foreground_checkpoint(
            self.server.backend_id(),
            self.events.is_empty(),
            startup_ready,
        );
    }

    async fn finish(&mut self) -> Result<()> {
        boxed_future!(self.settle_events()).await?;
        self.checkpoint(/*startup_ready*/ true);
        Ok(())
    }
}

#[tokio::test]
async fn startup_commits_after_event_drain_and_actual_draft_restoration() -> Result<()> {
    let mut fixture = boxed_future!(ForegroundFixture::new()).await?;
    let thread_id = ThreadId::new();
    boxed_future!(fixture.enqueue(thread_id, Vec::new())).await?;
    fixture.files.assert_clear()?;
    fixture.checkpoint(/*startup_ready*/ false);
    fixture.files.assert_clear()?;
    boxed_future!(fixture.settle_events()).await?;

    let (mut startup_pane, _startup_events) = super::startup::startup_bottom_pane();
    startup_pane.set_composer_text("retained startup draft".into(), Vec::new(), Vec::new());
    let mut draft = Some(startup_pane.composer_draft_snapshot());
    let mut submitted = false;
    fixture
        .app
        .before_foreground_mutation("startup_input_ready")
        .await;
    fixture
        .app
        .chat_widget
        .restore_startup_input_when_ready(&mut draft, &mut submitted);
    assert!(draft.is_none());
    fixture.checkpoint(draft.is_none());
    fixture.files.assert_live(thread_id)?;
    assert_eq!(
        fixture.app.chat_widget.composer_text_with_pending(),
        "retained startup draft"
    );
    fixture.server.shutdown().await?;
    Ok(())
}

#[tokio::test]
async fn delayed_startup_external_writer_or_parent_ownership_never_commits() -> Result<()> {
    for external_writer in [false, true] {
        let mut fixture = boxed_future!(ForegroundFixture::new()).await?;
        let thread_id = ThreadId::new();
        boxed_future!(fixture.enqueue(thread_id, Vec::new())).await?;
        fixture.files.assert_clear()?;
        if external_writer {
            fixture
                .app
                .ensure_thread_channel(thread_id)
                .mark_external_writer();
            fixture.app.chat_widget.show_external_writer_thread();
        } else {
            fixture.app.mark_primary_thread_parent_owned(thread_id);
        }
        boxed_future!(fixture.finish()).await?;
        fixture.files.assert_clear()?;
        assert_eq!(fixture.app.active_thread_id, Some(thread_id));
        fixture.server.shutdown().await?;
    }
    Ok(())
}

#[tokio::test]
async fn buffered_replay_and_thread_switch_resets_finish_before_publication() -> Result<()> {
    let mut fixture = boxed_future!(ForegroundFixture::new()).await?;
    let first = ThreadId::new();
    boxed_future!(fixture.enqueue(first, Vec::new())).await?;
    boxed_future!(fixture.finish()).await?;
    let previous = fixture.files.assert_live(first)?;

    let started = boxed_future!(fixture.server.start_thread(&fixture.app.config)).await?;
    let second = started.session.thread_id;
    let mut session = test_thread_session(second, fixture.app.config.cwd.to_path_buf());
    session.active_permission_profile = None;
    fixture.app.thread_event_channels.insert(
        second,
        ThreadEventChannel::new_with_session(
            THREAD_EVENT_CHANNEL_CAPACITY,
            session,
            vec![test_turn("saved", TurnStatus::Completed, Vec::new())],
        ),
    );
    fixture
        .app
        .upsert_agent_picker_thread(second, None, None, /*is_closed*/ false);
    fixture.app.before_foreground_mutation("switch").await;
    boxed_future!(
        fixture
            .app
            .select_agent_thread(&mut fixture.tui, &mut fixture.server, second)
    )
    .await?;
    assert!(fixture.app.pending_thread_switch_resets > 0);
    fixture.checkpoint(/*startup_ready*/ true);
    fixture.files.assert_clear()?;

    while let Ok(event) = fixture.events.try_recv() {
        fixture.app.before_foreground_mutation("app_event").await;
        boxed_future!(
            fixture
                .app
                .handle_event(&mut fixture.tui, &mut fixture.server, event)
        )
        .await?;
        fixture.checkpoint(/*startup_ready*/ true);
        if fixture.app.pending_thread_switch_resets != 0
            || fixture.app.initial_history_replay_buffer.is_some()
            || !fixture.events.is_empty()
        {
            fixture.files.assert_clear()?;
        }
    }
    boxed_future!(fixture.finish()).await?;
    let current = fixture.files.assert_live(second)?;
    assert_eq!(current["client_epoch"], previous["client_epoch"]);
    assert!(current["selection_generation"].as_u64() > previous["selection_generation"].as_u64());
    fixture.server.shutdown().await?;
    Ok(())
}

#[tokio::test]
async fn ineligible_or_incomplete_native_attachments_stay_clear() -> Result<()> {
    for denial in [
        "missing-channel",
        "missing-receiver",
        "closed",
        "parent-owned",
        "blocked-widget",
        "external-writer",
        "replay-only",
        "offline",
        "failed",
        "pending-start",
        "pending-reset",
        "pending-replay",
        "missing-proposal",
        "foreign-owner",
    ] {
        let mut fixture = boxed_future!(ForegroundFixture::new()).await?;
        let thread_id = ThreadId::new();
        boxed_future!(fixture.enqueue(thread_id, Vec::new())).await?;
        boxed_future!(fixture.finish()).await?;
        fixture.files.assert_live(thread_id)?;
        fixture.app.before_foreground_mutation(denial).await;
        match denial {
            "missing-channel" => {
                fixture.app.thread_event_channels.remove(&thread_id);
            }
            "missing-receiver" => fixture.app.active_thread_rx = None,
            "closed" => fixture.app.mark_agent_picker_thread_closed(thread_id),
            "parent-owned" => fixture.app.agent_navigation.mark_parent_owned(thread_id),
            "blocked-widget" => fixture.app.chat_widget.set_parent_owned_thread(),
            "external-writer" => fixture
                .app
                .ensure_thread_channel(thread_id)
                .mark_external_writer(),
            "replay-only" => fixture
                .app
                .ensure_thread_channel(thread_id)
                .mark_replay_only(),
            "offline" => fixture.app.reconnect.offline = true,
            "failed" => fixture.app.reconnect.failed = true,
            "pending-start" => fixture.app.pending_startup_thread_start = true,
            "pending-reset" => fixture.app.pending_thread_switch_resets = 1,
            "pending-replay" => fixture.app.begin_initial_history_replay_buffer(),
            "missing-proposal" => fixture.app.forget_native_foreground_selection(),
            "foreign-owner" => {}
            _ => unreachable!(),
        }
        let owner = if denial == "foreign-owner" {
            BackendId::new()
        } else {
            fixture.server.backend_id()
        };
        fixture.app.native_foreground_checkpoint(
            owner,
            fixture.events.is_empty(),
            /*startup_ready*/ true,
        );
        fixture.files.assert_clear()?;
        assert_eq!(fixture.app.active_thread_id, Some(thread_id), "{denial}");
        fixture.server.shutdown().await?;
    }
    Ok(())
}

#[tokio::test]
async fn heartbeat_then_ready_active_thread_close_withdraws_before_drain() -> Result<()> {
    let mut fixture = boxed_future!(ForegroundFixture::new()).await?;
    let thread_id = ThreadId::new();
    boxed_future!(fixture.enqueue(thread_id, Vec::new())).await?;
    boxed_future!(fixture.finish()).await?;
    let before = fixture.files.assert_live(thread_id)?;
    fixture.app.native_foreground_heartbeat();
    boxed_future!(fixture.app.drain_active_thread_events(&mut fixture.tui)).await?;
    assert_eq!(
        fixture.files.record()?["selection_generation"],
        before["selection_generation"]
    );

    fixture.app.thread_event_channels[&thread_id]
        .sender
        .send(ThreadBufferedEvent::Notification(Box::new(
            ServerNotification::ThreadClosed(codex_app_server_protocol::ThreadClosedNotification {
                thread_id: thread_id.to_string(),
            }),
        )))
        .await?;
    fixture.files.assert_live(thread_id)?;
    assert!(
        !fixture
            .app
            .agent_navigation
            .get(&thread_id)
            .unwrap()
            .is_closed
    );
    boxed_future!(fixture.app.drain_active_thread_events(&mut fixture.tui)).await?;
    fixture.files.assert_clear()?;
    let exit = std::iter::from_fn(|| fixture.events.try_recv().ok())
        .find(|event| matches!(event, AppEvent::Exit(_)))
        .expect("a live primary close must request TUI exit");
    fixture
        .app
        .before_foreground_mutation("resolved_exit")
        .await;
    let control = boxed_future!(fixture.app.handle_event(
        &mut fixture.tui,
        &mut fixture.server,
        exit
    ))
    .await?;
    assert!(matches!(control, AppRunControl::Exit(_)));
    fixture.app.withdraw_native_foreground_for_teardown().await;
    fixture.files.assert_clear()?;
    fixture.server.shutdown().await?;
    Ok(())
}

async fn reconnect_fixture(
    fixture: &mut ForegroundFixture,
    thread_id: ThreadId,
) -> Result<JoinHandle<Result<Vec<String>>>> {
    let listener = TcpListener::bind("127.0.0.1:0").await?;
    let endpoint = crate::resolve_remote_addr(&format!("ws://{}", listener.local_addr()?))?;
    let cwd = fixture.app.config.cwd.clone();
    let thread = serde_json::json!({
        "id": thread_id, "sessionId": thread_id, "preview": "foreground reconnect", "ephemeral": false,
        "modelProvider": "test-provider", "createdAt": 1, "updatedAt": 2,
        "status": {"type": "idle"}, "cwd": cwd, "cliVersion": "0.0.0", "source": "cli", "turns": [],
    });
    let server = tokio::spawn(async move {
        let (stream, _) = listener.accept().await?;
        serve_reconnect_requests(tokio_tungstenite::accept_async(stream).await?, |request| {
            std::future::ready(Some(match request.method.as_str() {
                "thread/resume" => {
                    assert_eq!(request.params.as_ref().unwrap()["threadId"], thread_id.to_string());
                    serde_json::json!({"result": {"thread": thread.clone(), "model": "gpt-test",
                        "modelProvider": "test-provider", "cwd": cwd, "approvalPolicy": "never",
                        "approvalsReviewer": "user", "sandbox": {"type": "readOnly"}, "reasoningEffort": null}})
                }
                "thread/read" => serde_json::json!({"result": {"thread": thread.clone()}}),
                "thread/list" | "thread/loaded/list" | "thread/turns/list" => {
                    serde_json::json!({"result": {"data": [], "nextCursor": null}})
                }
                "thread/goal/get" => serde_json::json!({"result": {"goal": null}}),
                _ => serde_json::json!({"error": {"code": -32601, "message": "unsupported test request"}}),
            }))
        }).await
    });
    let owner = fixture.server.backend_id();
    fixture.app.app_server_target = AppServerTarget::Remote { endpoint };
    fixture.app.before_foreground_mutation("reconnect").await;
    assert!(fixture.app.begin_reconnect());
    fixture.files.assert_clear()?;
    let connected = tokio::time::timeout(
        Duration::from_secs(15),
        boxed_future!(crate::app::reconnect::reconnect(
            owner,
            fixture.app.app_server_target.clone(),
            fixture.app.config.clone(),
            fixture.app.local_settings.clone(),
            Some(thread_id),
            /*remote_cwd*/ None,
            fixture.server.thread_tool_transport(),
            crate::app::reconnect::ReconnectPresentation::Conversation,
        )),
    )
    .await??;
    boxed_future!(fixture.app.finish_reconnect(
        &mut fixture.tui,
        &mut fixture.server,
        &mut fixture.events,
        connected,
        CODEX_CLI_VERSION,
    ))
    .await?;
    assert_eq!(fixture.server.backend_id(), owner);
    fixture.app.native_foreground_connected(owner);
    boxed_future!(fixture.finish()).await?;
    Ok(server)
}

#[tokio::test]
async fn actual_reconnect_rotates_connection_uuid_with_same_backend_and_sid() -> Result<()> {
    let mut fixture = boxed_future!(ForegroundFixture::new()).await?;
    let thread_id = ThreadId::new();
    boxed_future!(fixture.enqueue(thread_id, Vec::new())).await?;
    boxed_future!(fixture.finish()).await?;
    let before = fixture.files.assert_live(thread_id)?;
    let server = boxed_future!(reconnect_fixture(&mut fixture, thread_id)).await?;
    let after = fixture.files.assert_live(thread_id)?;
    assert_eq!(after["client_epoch"], before["client_epoch"]);
    assert_ne!(after["connection_epoch"], before["connection_epoch"]);
    assert!(after["selection_generation"].as_u64() > before["selection_generation"].as_u64());
    fixture.server.shutdown().await?;
    let methods = tokio::time::timeout(Duration::from_secs(5), server).await???;
    assert!(methods.iter().any(|method| method == "thread/resume"));
    Ok(())
}

#[tokio::test]
async fn failed_named_profile_verification_denies_a_live_channel_and_survives_reconnect()
-> Result<()> {
    let mut fixture = boxed_future!(ForegroundFixture::new()).await?;
    let (server, requests, proxy) = boxed_future!(start_recording_app_server_with_history(
        &fixture.app.config,
        HistoryCapabilities::ResumeFails,
        /*blocked_thread_list*/ None,
        /*failed_thread_name*/ None,
        crate::app_server_session::ThreadParamsMode::Embedded,
        LoaderOverrides::without_managed_config_for_tests(),
    ))
    .await?;
    fixture.server.shutdown().await?;
    fixture.server = server;
    fixture
        .app
        .native_foreground_connected(fixture.server.backend_id());
    let started = boxed_future!(fixture.server.start_thread(&fixture.app.config)).await?;
    let thread_id = started.session.thread_id;
    let mut cached = started.session;
    cached.active_permission_profile = Some(ActivePermissionProfile::new("unverified-named"));
    let primary = ThreadId::new();
    boxed_future!(fixture.enqueue(primary, Vec::new())).await?;
    boxed_future!(fixture.finish()).await?;
    fixture.files.assert_live(primary)?;
    fixture.app.thread_event_channels.insert(
        thread_id,
        ThreadEventChannel::new_with_session(THREAD_EVENT_CHANNEL_CAPACITY, cached, Vec::new()),
    );
    fixture
        .app
        .upsert_agent_picker_thread(thread_id, None, None, /*is_closed*/ false);
    fixture
        .app
        .before_foreground_mutation("select_named_profile")
        .await;
    boxed_future!(fixture.app.select_agent_thread(
        &mut fixture.tui,
        &mut fixture.server,
        thread_id
    ))
    .await?;
    boxed_future!(fixture.finish()).await?;
    assert_eq!(fixture.app.active_thread_id, Some(thread_id));
    assert_eq!(
        fixture.app.thread_event_channels[&thread_id].attachment(),
        ThreadEventAttachment::Live
    );
    assert_eq!(recorded_params(&requests, "thread/resume").len(), 1);
    assert!(
        !fixture
            .app
            .native_foreground_selection_allows_direct_input(thread_id)
    );
    fixture.files.assert_clear()?;

    let server = boxed_future!(reconnect_fixture(&mut fixture, thread_id)).await?;
    assert_eq!(
        fixture.app.thread_event_channels[&thread_id].attachment(),
        ThreadEventAttachment::Live
    );
    assert!(
        !fixture
            .app
            .native_foreground_selection_allows_direct_input(thread_id)
    );
    fixture.files.assert_clear()?;
    fixture.server.shutdown().await?;
    tokio::time::timeout(Duration::from_secs(5), server).await???;
    proxy.abort();
    Ok(())
}

#[tokio::test]
async fn refused_clear_and_invalidation_drain_lease_without_mutating_selected_thread() -> Result<()>
{
    let mut fixture = boxed_future!(ForegroundFixture::new()).await?;
    let previous_thread = ThreadId::new();
    boxed_future!(fixture.enqueue(previous_thread, Vec::new())).await?;
    boxed_future!(fixture.finish()).await?;
    let previous = fixture.files.assert_live(previous_thread)?;
    let original_bytes = std::fs::read(fixture.files.record_path())?;
    // This invalidates both publication and owned invalidation, retaining the exact positive inode.
    std::fs::write(fixture.files.marker_path(), b"refused\n")?;
    let expires_ns = previous["monotonic_expires_ns"].as_u64().unwrap();
    {
        let mut gate = boxed_future!(
            fixture
                .app
                .before_foreground_mutation("switch_after_io_failure")
        );
        if tokio::time::timeout(Duration::from_millis(50), &mut gate)
            .await
            .is_ok()
        {
            assert!(observer_clock_ns() >= expires_ns);
        }
    }
    assert_eq!(fixture.app.active_thread_id, Some(previous_thread));
    assert_eq!(fixture.app.chat_widget.thread_id(), Some(previous_thread));
    assert_eq!(std::fs::read(fixture.files.record_path())?, original_bytes);
    let remaining = Duration::from_nanos(expires_ns.saturating_sub(observer_clock_ns()));
    tokio::time::timeout(
        remaining + Duration::from_secs(2),
        fixture
            .app
            .before_foreground_mutation("switch_after_io_failure"),
    )
    .await?;
    assert!(observer_clock_ns() >= expires_ns);
    boxed_future!(fixture.app.store_active_thread_receiver()).await;
    boxed_future!(fixture.app.reset_thread_event_state()).await;
    let next_thread = ThreadId::new();
    boxed_future!(fixture.app.enqueue_primary_thread_session(
        test_thread_session(next_thread, fixture.app.config.cwd.to_path_buf()),
        Vec::new(),
    ))
    .await?;
    boxed_future!(fixture.finish()).await?;
    assert_eq!(fixture.app.active_thread_id, Some(next_thread));
    fixture.app.native_foreground_heartbeat();
    assert_eq!(std::fs::read(fixture.files.record_path())?, original_bytes);

    std::fs::write(fixture.files.marker_path(), b"ccc-request-credentials-v1\n")?;
    fixture.app.native_foreground_heartbeat();
    fixture.files.assert_clear()?;
    fixture.app.before_foreground_mutation("fresh_rearm").await;
    boxed_future!(fixture.finish()).await?;
    let recovered = fixture.files.assert_live(next_thread)?;
    assert_eq!(recovered["client_epoch"], previous["client_epoch"]);
    assert_ne!(recovered["connection_epoch"], previous["connection_epoch"]);
    fixture.server.shutdown().await?;
    Ok(())
}

#[tokio::test]
async fn successful_retry_after_refused_withdrawal_uses_a_fresh_connection_epoch() -> Result<()> {
    let mut fixture = boxed_future!(ForegroundFixture::new()).await?;
    let thread_id = ThreadId::new();
    boxed_future!(fixture.enqueue(thread_id, Vec::new())).await?;
    boxed_future!(fixture.finish()).await?;
    let before = fixture.files.assert_live(thread_id)?;
    let marker = fixture.files.marker_path();
    std::fs::write(&marker, b"refused\n")?;
    let repair = tokio::spawn(async move {
        tokio::time::sleep(Duration::from_millis(100)).await;
        std::fs::write(marker, b"ccc-request-credentials-v1\n")
    });
    fixture
        .app
        .before_foreground_mutation("recovering_clear")
        .await;
    repair.await??;
    boxed_future!(fixture.finish()).await?;
    let after = fixture.files.assert_live(thread_id)?;
    assert_ne!(after["connection_epoch"], before["connection_epoch"]);
    fixture.server.shutdown().await?;
    Ok(())
}

#[tokio::test]
async fn actual_parked_voice_operation_restores_the_visible_selection() -> Result<()> {
    let mut fixture = boxed_future!(ForegroundFixture::new()).await?;
    let voice_owner = ThreadId::new();
    boxed_future!(fixture.enqueue(voice_owner, Vec::new())).await?;
    boxed_future!(fixture.finish()).await?;
    crate::chatwidget::activate_voice_for_thread(&mut fixture.app.chat_widget, voice_owner);
    let (replacement, _, _, _) =
        boxed_future!(crate::chatwidget::tests::make_chatwidget_manual_with_sender()).await;
    fixture
        .app
        .before_foreground_mutation("display_new_thread")
        .await;
    boxed_future!(fixture.app.store_active_thread_receiver()).await;
    boxed_future!(fixture.app.reset_thread_event_state()).await;
    fixture.app.replace_chat_widget(replacement);
    let visible = ThreadId::new();
    boxed_future!(fixture.enqueue(visible, Vec::new())).await?;
    boxed_future!(fixture.finish()).await?;
    assert_eq!(fixture.app.voice_owner_thread_id(), Some(voice_owner));
    fixture.files.assert_live(visible)?;

    fixture
        .app
        .before_foreground_mutation("parked_voice_operation")
        .await;
    boxed_future!(fixture.app.handle_event(
        &mut fixture.tui,
        &mut fixture.server,
        AppEvent::CodexOp(Op::RealtimeConversationStop {
            thread_id: voice_owner
        }),
    ))
    .await?;
    assert_eq!(fixture.app.active_thread_id, Some(visible));
    assert_eq!(fixture.app.chat_widget.thread_id(), Some(visible));
    assert!(
        fixture
            .app
            .native_foreground_selection_allows_direct_input(visible)
    );
    boxed_future!(fixture.finish()).await?;
    fixture.files.assert_live(visible)?;
    fixture.app.native_foreground_heartbeat();
    fixture.files.assert_live(visible)?;
    fixture.server.shutdown().await?;
    Ok(())
}

#[tokio::test]
async fn startup_guard_and_teardown_revoke_while_app_clone_remains_alive() -> Result<()> {
    let mut fixture = boxed_future!(ForegroundFixture::new()).await?;
    let files = ObservationFiles::new()?;
    let guard = StartupForegroundGuard::from_result(
        true,
        ForegroundObserver::open_enabled(files.home.path()),
    );
    fixture.app.foreground = guard.app_owned();
    fixture
        .app
        .native_foreground_connected(fixture.server.backend_id());
    let thread_id = ThreadId::new();
    boxed_future!(fixture.enqueue(thread_id, Vec::new())).await?;
    boxed_future!(fixture.finish()).await?;
    files.assert_live(thread_id)?;
    drop(guard);
    files.assert_clear()?;
    assert!(files.record()?["connection_epoch"].is_null());
    fixture.app.native_foreground_heartbeat();
    files.assert_clear()?;
    fixture.app.withdraw_native_foreground_for_teardown().await;
    files.assert_clear()?;
    assert!(
        !fixture
            .app
            .native_foreground_selection_allows_direct_input(thread_id)
    );
    fixture.server.shutdown().await?;
    Ok(())
}

#[tokio::test]
async fn dropping_app_revokes_while_the_startup_guard_still_owns_state() -> Result<()> {
    let mut fixture = boxed_future!(ForegroundFixture::new()).await?;
    let files = ObservationFiles::new()?;
    let guard = StartupForegroundGuard::from_result(
        true,
        ForegroundObserver::open_enabled(files.home.path()),
    );
    fixture.app.foreground = guard.app_owned();
    fixture
        .app
        .native_foreground_connected(fixture.server.backend_id());
    let thread_id = ThreadId::new();
    boxed_future!(fixture.enqueue(thread_id, Vec::new())).await?;
    boxed_future!(fixture.finish()).await?;
    files.assert_live(thread_id)?;
    drop(fixture.app);
    files.assert_clear()?;
    assert!(files.record()?["connection_epoch"].is_null());
    drop(guard);
    files.assert_clear()?;
    fixture.server.shutdown().await?;
    Ok(())
}

#[tokio::test]
async fn real_initialization_failure_is_visible_and_ordinary_app_work_continues() -> Result<()> {
    let mut fixture = boxed_future!(ForegroundFixture::new()).await?;
    let failed = ObservationFiles::new()?;
    std::fs::remove_file(failed.marker_path())?;
    let guard = StartupForegroundGuard::from_result(
        true,
        ForegroundObserver::open_enabled(failed.home.path()),
    );
    let warning = guard
        .initialization_warning()
        .expect("opt-in failure must be visible");
    fixture.app.foreground = guard.app_owned();
    assert!(!fixture.app.foreground.heartbeat_enabled());
    fixture.app.chat_widget.add_error_message(warning.clone());
    let thread_id = ThreadId::new();
    boxed_future!(fixture.enqueue(thread_id, Vec::new())).await?;
    boxed_future!(fixture.finish()).await?;
    assert_eq!(fixture.app.active_thread_id, Some(thread_id));
    assert_eq!(fixture.app.chat_widget.thread_id(), Some(thread_id));
    assert!(warning.contains("Automatic continuation is unavailable"));
    assert!(!failed.record_path().exists());
    fixture.server.shutdown().await?;
    Ok(())
}

#[tokio::test]
async fn native_compaction_event_preserves_real_draft_and_never_withdraws_selection() -> Result<()>
{
    let mut fixture = boxed_future!(ForegroundFixture::new()).await?;
    let thread_id = ThreadId::new();
    boxed_future!(fixture.enqueue(thread_id, Vec::new())).await?;
    boxed_future!(fixture.finish()).await?;
    let before = fixture.files.assert_live(thread_id)?;
    let (mut startup_pane, _startup_events) = super::startup::startup_bottom_pane();
    startup_pane.set_composer_text("keep my real draft".into(), Vec::new(), Vec::new());
    let mut draft = Some(startup_pane.composer_draft_snapshot());
    let mut submitted = false;
    fixture
        .app
        .chat_widget
        .restore_startup_input_when_ready(&mut draft, &mut submitted);
    let (reply, result) = std::sync::mpsc::sync_channel(1);
    let event = AppEvent::NativeCompactionControl(
        ccc_native_client_foreground::compaction_socket::NativeControlRequest {
            body: serde_json::json!({"method":"compaction/recovery/query", "threadId":thread_id.to_string(),
                "clientEpoch":before["client_epoch"]}),
            queue_clear: true,
            peer_pid: std::process::id(),
            peer_uid: unsafe { libc::getuid() },
            reply,
        },
    );
    assert!(event.is_native_compaction_control());
    boxed_future!(
        fixture
            .app
            .handle_event(&mut fixture.tui, &mut fixture.server, event)
    )
    .await?;
    assert!(
        result.recv_timeout(Duration::from_secs(1))?["error"]
            .as_str()
            .unwrap()
            .contains("draft")
    );
    assert_eq!(
        fixture.app.chat_widget.composer_text_with_pending(),
        "keep my real draft"
    );
    assert_eq!(
        fixture.files.record()?["selection_generation"],
        before["selection_generation"]
    );
    fixture.server.shutdown().await?;
    Ok(())
}

#[tokio::test]
async fn native_compaction_control_refuses_queued_events_and_old_client_epoch() -> Result<()> {
    let mut fixture = boxed_future!(ForegroundFixture::new()).await?;
    let thread_id = ThreadId::new();
    boxed_future!(fixture.enqueue(thread_id, Vec::new())).await?;
    boxed_future!(fixture.finish()).await?;
    let before = fixture.files.assert_live(thread_id)?;
    for (queue_clear, epoch) in [
        (false, before["client_epoch"].clone()),
        (true, serde_json::json!("old-client")),
    ] {
        let (reply, result) = std::sync::mpsc::sync_channel(1);
        let event = AppEvent::NativeCompactionControl(
            ccc_native_client_foreground::compaction_socket::NativeControlRequest {
                body: serde_json::json!({"method":"compaction/recovery/control", "threadId":thread_id.to_string(),"clientEpoch":epoch}),
                queue_clear,
                peer_pid: std::process::id(),
                peer_uid: unsafe { libc::getuid() },
                reply,
            },
        );
        boxed_future!(
            fixture
                .app
                .handle_event(&mut fixture.tui, &mut fixture.server, event)
        )
        .await?;
        assert!(result.recv_timeout(Duration::from_secs(1))?["error"].is_string());
        assert_eq!(
            fixture.files.record()?["selection_generation"],
            before["selection_generation"]
        );
    }
    fixture.server.shutdown().await?;
    Ok(())
}

#[tokio::test]
async fn actual_terminal_event_waits_for_native_shared_lock_before_editing_draft() -> Result<()> {
    use std::os::fd::AsRawFd;
    let mut fixture = boxed_future!(ForegroundFixture::new()).await?;
    let thread_id = ThreadId::new();
    boxed_future!(fixture.enqueue(thread_id, Vec::new())).await?;
    boxed_future!(fixture.finish()).await?;
    let before = fixture.files.assert_live(thread_id)?;
    let root = fixture
        .files
        .home
        .path()
        .join("credential-observations/compaction-recovery");
    DirBuilder::new().mode(0o700).create(&root)?;
    let lock = OpenOptions::new()
        .read(true)
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(root.join(format!(
            "client-{}-{}.lock",
            std::process::id(),
            before["client_epoch"].as_str().unwrap()
        )))?;
    // This is the same kernel SH reservation the verified native actor retains.
    // No grant or native acceptance is fabricated by this App call-path test.
    assert_eq!(
        unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_SH | libc::LOCK_NB) },
        0
    );
    let draft = fixture.app.chat_widget.composer_text_with_pending();
    {
        let pending = async {
            fixture
                .app
                .before_foreground_mutation("terminal_event")
                .await;
            fixture
                .app
                .handle_tui_event(
                    &mut fixture.tui,
                    &mut fixture.server,
                    TuiEvent::Key(crossterm::event::KeyEvent::new(
                        crossterm::event::KeyCode::Char('x'),
                        crossterm::event::KeyModifiers::NONE,
                    )),
                )
                .await
        };
        assert!(
            tokio::time::timeout(Duration::from_millis(80), boxed_future!(pending))
                .await
                .is_err()
        );
    }
    assert_eq!(fixture.app.chat_widget.composer_text_with_pending(), draft);
    assert_eq!(fixture.app.active_thread_id, Some(thread_id));
    assert_eq!(
        fixture.files.record()?["selection_generation"],
        before["selection_generation"]
    );
    assert_eq!(unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_UN) }, 0);
    fixture
        .app
        .before_foreground_mutation("terminal_event")
        .await;
    boxed_future!(fixture.app.handle_tui_event(
        &mut fixture.tui,
        &mut fixture.server,
        TuiEvent::Key(crossterm::event::KeyEvent::new(
            crossterm::event::KeyCode::Char('x'),
            crossterm::event::KeyModifiers::NONE
        ))
    ))
    .await?;
    assert_eq!(
        fixture.app.chat_widget.composer_text_with_pending(),
        format!("{draft}x")
    );
    fixture.server.shutdown().await?;
    Ok(())
}

#[tokio::test]
async fn animation_draws_retain_original_generation_under_native_shared_lock() -> Result<()> {
    use std::os::fd::AsRawFd;
    let mut fixture = boxed_future!(ForegroundFixture::new()).await?;
    let thread_id = ThreadId::new();
    boxed_future!(fixture.enqueue(thread_id, Vec::new())).await?;
    boxed_future!(fixture.finish()).await?;
    let before = fixture.files.assert_live(thread_id)?;
    let root = fixture
        .files
        .home
        .path()
        .join("credential-observations/compaction-recovery");
    DirBuilder::new().mode(0o700).create(&root)?;
    let lock = OpenOptions::new()
        .read(true)
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(root.join(format!(
            "client-{}-{}.lock",
            std::process::id(),
            before["client_epoch"].as_str().unwrap()
        )))?;
    fixture.app.local_settings.tui.animations = true;
    for _ in 0..8 {
        let before = fixture.files.assert_live(thread_id)?;
        assert_eq!(
            unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_SH | libc::LOCK_NB) },
            0
        );
        // This is the exact startup gate followed by the real Draw handler.
        // The old unconditional gate times out while this real SH is held.
        tokio::time::timeout(
            Duration::from_millis(80),
            boxed_future!(fixture.app.before_foreground_tui_event(
                Some(&TuiEvent::Draw),
                fixture.server.backend_id(),
                fixture.events.is_empty(),
                true,
            )),
        )
        .await?;
        fixture.files.assert_live(thread_id)?;
        boxed_future!(fixture.app.handle_tui_event(
            &mut fixture.tui,
            &mut fixture.server,
            TuiEvent::Draw,
        ))
        .await?;
        fixture.checkpoint(true);
        assert_eq!(
            fixture.files.record()?["selection_generation"],
            before["selection_generation"]
        );
        assert_eq!(
            fixture.files.record()?["connection_epoch"],
            before["connection_epoch"]
        );
        assert!(fixture.app.chat_widget.native_compaction_input_clear());
        assert!(
            fixture
                .app
                .native_compaction_backend_matches(fixture.server.backend_id())
        );
        assert_eq!(unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_UN) }, 0);
        // Newly queued side effects use the existing guarded event handlers;
        // they are not treated as additional presentation-only frames.
        boxed_future!(fixture.finish()).await?;
    }
    fixture.server.shutdown().await?;
    Ok(())
}

#[tokio::test]
async fn non_draw_events_and_terminal_eof_still_withdraw_before_handling() -> Result<()> {
    use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
    for event in [
        Some(TuiEvent::Key(KeyEvent::new(
            KeyCode::Char('x'),
            KeyModifiers::NONE,
        ))),
        Some(TuiEvent::Paste("kept input".into())),
        Some(TuiEvent::Resize(ratatui::layout::Size::new(80, 24))),
        Some(TuiEvent::Mouse(crossterm::event::MouseEvent {
            kind: crossterm::event::MouseEventKind::Moved,
            column: 0,
            row: 0,
            modifiers: KeyModifiers::NONE,
        })),
        Some(TuiEvent::FocusLost),
        Some(TuiEvent::FocusGained),
        Some(TuiEvent::Resume),
        None,
    ] {
        let mut fixture = boxed_future!(ForegroundFixture::new()).await?;
        let thread_id = ThreadId::new();
        boxed_future!(fixture.enqueue(thread_id, Vec::new())).await?;
        boxed_future!(fixture.finish()).await?;
        let before = fixture.files.assert_live(thread_id)?;
        fixture
            .app
            .before_foreground_tui_event(event.as_ref(), fixture.server.backend_id(), true, true)
            .await;
        fixture.files.assert_clear()?;
        assert!(
            fixture.files.record()?["selection_generation"].as_u64()
                > before["selection_generation"].as_u64()
        );
        assert!(
            !fixture
                .app
                .native_compaction_backend_matches(fixture.server.backend_id())
        );
        fixture.server.shutdown().await?;
    }
    Ok(())
}

#[tokio::test]
async fn draw_with_pending_input_or_transition_still_withdraws() -> Result<()> {
    for denial in [
        "queue",
        "startup",
        "foreign-owner",
        "pending-paste",
        "editor",
        "replay",
        "offline",
    ] {
        let mut fixture = boxed_future!(ForegroundFixture::new()).await?;
        let thread_id = ThreadId::new();
        boxed_future!(fixture.enqueue(thread_id, Vec::new())).await?;
        boxed_future!(fixture.finish()).await?;
        fixture.files.assert_live(thread_id)?;
        match denial {
            "pending-paste" => {
                fixture.app.pending_right_click_paste = Some(
                    crate::app::right_click_paste::PendingPaste::for_foreground_gate_test(
                        thread_id,
                    ),
                )
            }
            "editor" => fixture
                .app
                .chat_widget
                .set_external_editor_state(crate::chatwidget::ExternalEditorState::Requested),
            "replay" => fixture.app.begin_initial_history_replay_buffer(),
            "offline" => fixture.app.reconnect.offline = true,
            _ => {}
        }
        let owner = if denial == "foreign-owner" {
            BackendId::new()
        } else {
            fixture.server.backend_id()
        };
        fixture
            .app
            .before_foreground_tui_event(
                Some(&TuiEvent::Draw),
                owner,
                denial != "queue",
                denial != "startup",
            )
            .await;
        fixture.files.assert_clear()?;
        assert!(
            !fixture
                .app
                .native_compaction_backend_matches(fixture.server.backend_id()),
            "{denial}"
        );
        fixture.server.shutdown().await?;
    }
    Ok(())
}

#[tokio::test]
async fn held_ascii_draw_waits_for_original_lock_before_flushing_input() -> Result<()> {
    use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
    use std::os::fd::AsRawFd;
    let mut fixture = boxed_future!(ForegroundFixture::new()).await?;
    let thread_id = ThreadId::new();
    boxed_future!(fixture.enqueue(thread_id, Vec::new())).await?;
    boxed_future!(fixture.finish()).await?;
    fixture
        .app
        .before_foreground_tui_event(
            Some(&TuiEvent::Key(KeyEvent::new(
                KeyCode::Char('x'),
                KeyModifiers::NONE,
            ))),
            fixture.server.backend_id(),
            true,
            true,
        )
        .await;
    boxed_future!(fixture.app.handle_tui_event(
        &mut fixture.tui,
        &mut fixture.server,
        TuiEvent::Key(KeyEvent::new(KeyCode::Char('x'), KeyModifiers::NONE)),
    ))
    .await?;
    assert!(fixture.app.chat_widget.is_in_paste_burst());
    fixture.checkpoint(true);
    let before = fixture.files.assert_live(thread_id)?;
    let root = fixture
        .files
        .home
        .path()
        .join("credential-observations/compaction-recovery");
    DirBuilder::new().mode(0o700).create(&root)?;
    let lock = OpenOptions::new()
        .read(true)
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(root.join(format!(
            "client-{}-{}.lock",
            std::process::id(),
            before["client_epoch"].as_str().unwrap()
        )))?;
    assert_eq!(
        unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_SH | libc::LOCK_NB) },
        0
    );
    assert!(
        tokio::time::timeout(
            Duration::from_millis(80),
            boxed_future!(fixture.app.before_foreground_tui_event(
                Some(&TuiEvent::Draw),
                fixture.server.backend_id(),
                true,
                true
            ))
        )
        .await
        .is_err()
    );
    assert!(fixture.app.chat_widget.is_in_paste_burst());
    assert_eq!(fixture.app.chat_widget.composer_text_with_pending(), "x");
    assert_eq!(unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_UN) }, 0);
    fixture
        .app
        .before_foreground_tui_event(
            Some(&TuiEvent::Draw),
            fixture.server.backend_id(),
            true,
            true,
        )
        .await;
    fixture.files.assert_clear()?;
    boxed_future!(fixture.app.handle_tui_event(
        &mut fixture.tui,
        &mut fixture.server,
        TuiEvent::Draw
    ))
    .await?;
    assert!(!fixture.app.chat_widget.is_in_paste_burst());
    assert_eq!(fixture.app.chat_widget.composer_text_with_pending(), "x");
    fixture.server.shutdown().await?;
    Ok(())
}

#[tokio::test]
async fn delayed_approval_draw_is_not_treated_as_presentation_only() -> Result<()> {
    let mut fixture = boxed_future!(ForegroundFixture::new()).await?;
    let thread_id = ThreadId::new();
    boxed_future!(fixture.enqueue(thread_id, Vec::new())).await?;
    boxed_future!(fixture.finish()).await?;
    let (mut pane, _events) = super::startup::startup_bottom_pane();
    pane.set_composer_text(String::new(), Vec::new(), Vec::new());
    let mut draft = pane.composer_draft_snapshot();
    draft.last_composer_activity_at = Some(Instant::now());
    fixture
        .app
        .chat_widget
        .restore_startup_draft_when_ready(&mut Some(draft));
    let request = exec_approval_request(thread_id, "turn-draw", "call-draw", None);
    let _ = fixture
        .app
        .pending_app_server_requests
        .note_server_request(&request);
    fixture.app.enqueue_primary_thread_request(request).await?;
    let event = fixture.app.active_thread_rx.as_mut().unwrap().try_recv()?;
    fixture.app.handle_thread_event_now(event);
    assert!(fixture.app.chat_widget.has_pending_protected_request());
    assert!(fixture.app.chat_widget.composer_is_empty());
    assert!(!fixture.app.chat_widget.has_active_modal());
    fixture
        .app
        .before_foreground_tui_event(
            Some(&TuiEvent::Draw),
            fixture.server.backend_id(),
            true,
            true,
        )
        .await;
    fixture.files.assert_clear()?;
    assert_eq!(fixture.app.chat_widget.composer_text_with_pending(), "");
    fixture.server.shutdown().await?;
    Ok(())
}

#[tokio::test]
async fn draw_never_treats_an_uncommitted_proposal_as_live_selection() -> Result<()> {
    let mut fixture = boxed_future!(ForegroundFixture::new()).await?;
    let thread_id = ThreadId::new();
    boxed_future!(fixture.enqueue(thread_id, Vec::new())).await?;
    boxed_future!(fixture.finish()).await?;
    fixture.files.assert_live(thread_id)?;
    // A library-level clear must remain authoritative even if App's proposal
    // and connected-owner fields still describe the same visible thread.
    fixture
        .app
        .native_compaction_observer()
        .before_change("test_authoritative_clear")?;
    fixture.files.assert_clear()?;
    fixture
        .app
        .before_foreground_tui_event(
            Some(&TuiEvent::Draw),
            fixture.server.backend_id(),
            true,
            true,
        )
        .await;
    assert!(
        !fixture
            .app
            .native_compaction_backend_matches(fixture.server.backend_id())
    );
    fixture.files.assert_clear()?;
    boxed_future!(fixture.app.handle_tui_event(
        &mut fixture.tui,
        &mut fixture.server,
        TuiEvent::Draw
    ))
    .await?;
    fixture.checkpoint(true);
    fixture.files.assert_live(thread_id)?;
    fixture.server.shutdown().await?;
    Ok(())
}
