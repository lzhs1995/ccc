use super::*;
use crate::session::compaction_recovery::CompactionIo;
use futures::future::BoxFuture;
use std::sync::atomic::AtomicUsize;
use std::sync::atomic::Ordering;

struct NativeFixtureIo {
    entered: Arc<tokio::sync::Notify>,
    release: Arc<tokio::sync::Notify>,
    collected: Arc<AtomicUsize>,
}
impl CompactionIo<usize> for NativeFixtureIo {
    fn make_stream(&mut self) -> BoxFuture<'_, CodexResult<crate::ResponseStream>> {
        Box::pin(async move {
            self.entered.notify_one();
            self.release.notified().await;
            let (_sender, receiver) = tokio::sync::mpsc::channel(1);
            Ok(crate::ResponseStream {
                rx_event: receiver,
                interrupt: None,
                consumer_dropped: tokio_util::sync::CancellationToken::new(),
                mapper_finished: None,
                provider_finished: None,
                provider_cancel: None,
            })
        })
    }
    fn collect<'a>(
        &'a mut self,
        _stream: &'a mut crate::ResponseStream,
    ) -> BoxFuture<'a, CodexResult<usize>> {
        Box::pin(async move { Ok(self.collected.fetch_add(1, Ordering::AcqRel) + 1) })
    }
}

#[tokio::test]
async fn native_compaction_factory_is_visible_before_headers_and_uninstrumented_stream_still_completes_once()
 {
    let (session, turn) = make_session_and_context().await;
    let session = Arc::new(session);
    session.observe_compaction_item(&turn.sub_id, "native-original-occurrence");
    let entered = Arc::new(tokio::sync::Notify::new());
    let release = Arc::new(tokio::sync::Notify::new());
    let collected = Arc::new(AtomicUsize::new(0));
    let worker = {
        let session = session.clone();
        let entered = entered.clone();
        let release = release.clone();
        let collected = collected.clone();
        let turn_id = turn.sub_id.clone();
        tokio::spawn(async move {
            session
                .collect_recoverable_compaction(
                    &turn_id,
                    NativeFixtureIo {
                        entered,
                        release,
                        collected,
                    },
                )
                .await
        })
    };
    entered.notified().await;
    let registry = session.compaction_recovery_registry();
    let snapshot = registry
        .live
        .lock()
        .unwrap()
        .as_ref()
        .unwrap()
        .control
        .snapshot()
        .unwrap();
    assert_eq!(snapshot.identity.occurrence, "native-original-occurrence");
    assert_eq!(
        snapshot.phase,
        crate::session::compaction_recovery_gate::Phase::Waiting
    );
    assert_eq!(collected.load(Ordering::Acquire), 0);
    release.notify_one();
    assert_eq!(worker.await.unwrap().unwrap(), 1);
    assert_eq!(collected.load(Ordering::Acquire), 1);
}

#[tokio::test]
async fn native_compaction_actor_refuses_control_without_original_authenticated_policy() {
    let (session, _turn) = make_session_and_context().await;
    let request=serde_json::from_value(serde_json::json!({"expected":{"nativeEpoch":"e","threadId":"t","turnId":"u","occurrence":"o","generation":0},
        "attemptId":"original","authority":{"expectedClientPid":2,"expectedClientBirth":[1,2],"expectedClientEpoch":"c","expectedConnectionEpoch":"x",
        "expectedSelectionGeneration":1,"intentPath":"/fixture/intent","intentSha256":"00","controllerId":"original","enrollmentId":"original",
        "proofPath":"/fixture/proof","proofSha256":"00","expectedGoal":null}})).unwrap();
    let error = session
        .handle_compaction_recovery(Some(request))
        .await
        .unwrap_err();
    assert!(error.contains("authenticated original recovery policy"));
    assert!(
        session
            .compaction_recovery_registry()
            .live
            .lock()
            .unwrap()
            .is_none()
    );
}

struct TestAuthority;
impl codex_api::compaction_dispatch::CompactionDispatchAuthority for TestAuthority {
    fn remaining(&self) -> Result<Duration, String> {
        Ok(Duration::from_secs(15))
    }
    fn validate_fresh(&self) -> Result<(), String> {
        Ok(())
    }
    fn persist_send_intent(&self) -> Result<(), String> {
        panic!("actor admission test must not send")
    }
    fn persist_sent(&self) -> Result<(), String> {
        panic!("actor admission test must not send")
    }
}
struct TestOriginalPolicy(AtomicUsize);
impl crate::RecoveryPolicy for TestOriginalPolicy {
    fn admit<'a>(
        &'a self,
        _: &'a codex_protocol::compact_recovery::CompactionRecoveryRequest,
    ) -> BoxFuture<
        'a,
        Result<Arc<dyn codex_api::compaction_dispatch::CompactionDispatchAuthority>, String>,
    > {
        Box::pin(async {
            Ok(Arc::new(TestAuthority)
                as Arc<
                    dyn codex_api::compaction_dispatch::CompactionDispatchAuthority,
                >)
        })
    }
    fn persist_admission(
        &self,
        _: &codex_protocol::compact_recovery::CompactionRecoveryRequest,
    ) -> Result<String, String> {
        self.0.fetch_add(1, Ordering::AcqRel);
        Ok("test-only-receipt".into())
    }
    fn persist_replacement_dispatched(
        &self,
        _: &codex_protocol::compact_recovery::CompactionRecoveryIdentity,
    ) -> Result<(), String> {
        panic!("no replacement in actor test")
    }
    fn persist_terminal(
        &self,
        _: &codex_protocol::compact_recovery::CompactionRecoveryIdentity,
        _: Option<&str>,
        _: bool,
    ) -> Result<(), String> {
        Ok(())
    }
    fn persist_drained(
        &self,
        _: &codex_protocol::compact_recovery::CompactionRecoveryIdentity,
        _: &str,
    ) -> Result<(), String> {
        panic!("no drain in actor test")
    }
    fn authorize_replacement<'a>(
        &'a self,
        _: &'a codex_protocol::compact_recovery::CompactionRecoveryIdentity,
        _: &'a str,
    ) -> BoxFuture<
        'a,
        Result<Arc<dyn codex_api::compaction_dispatch::CompactionDispatchAuthority>, String>,
    > {
        Box::pin(async { Err("no replacement in actor test".into()) })
    }
}

#[tokio::test]
async fn native_actor_300_deadline_exact_occurrence_and_once_claim_use_original_turn() {
    let (session, turn, _events) = make_session_and_context_with_rx().await;
    session
        .spawn_task(
            Arc::clone(&turn),
            Vec::new(),
            NeverEndingTask {
                kind: TaskKind::Regular,
                listen_to_cancellation_token: true,
            },
        )
        .await;
    let policy = Arc::new(TestOriginalPolicy(AtomicUsize::new(0)));
    session
        .services
        .thread_extension_data
        .insert(crate::RecoveryPolicyHandle(policy.clone()));
    let registry = session.compaction_recovery_registry();
    let identity = crate::session::compaction_recovery_gate::Identity {
        native_epoch: registry.native_epoch.clone(),
        thread_id: session.thread_id().to_string(),
        turn_id: turn.sub_id.clone(),
        occurrence: "native-original-item".into(),
        generation: 0,
    };
    let (control, _owned_wait) = crate::session::compaction_recovery_gate::Control::begin(
        identity.clone(),
        std::future::pending::<()>(),
    )
    .unwrap();
    session
        .register_compaction_wait(&turn.sub_id, "native-original-item", control)
        .unwrap();
    let request:codex_protocol::compact_recovery::CompactionRecoveryRequest=serde_json::from_value(serde_json::json!({
        "expected":{"nativeEpoch":identity.native_epoch,"threadId":identity.thread_id,"turnId":identity.turn_id,"occurrence":identity.occurrence,"generation":0},
        "attemptId":"original-attempt","authority":{"expectedClientPid":2,"expectedClientBirth":[1,2],"expectedClientEpoch":"c","expectedConnectionEpoch":"x","expectedSelectionGeneration":1,
            "intentPath":"/fixture/intent","intentSha256":"00","controllerId":"original","enrollmentId":"original","proofPath":"/fixture/proof","proofSha256":"00","expectedGoal":null}})).unwrap();
    registry
        .live
        .lock()
        .unwrap()
        .as_mut()
        .unwrap()
        .original_started = std::time::Instant::now() - Duration::from_secs(299);
    assert!(
        session
            .handle_compaction_recovery(Some(request.clone()))
            .await
            .unwrap_err()
            .contains("300-second")
    );
    assert_eq!(policy.0.load(Ordering::Acquire), 0);
    registry
        .live
        .lock()
        .unwrap()
        .as_mut()
        .unwrap()
        .original_started = std::time::Instant::now() - Duration::from_secs(301);
    let mut stale = request.clone();
    stale.expected.occurrence = "other-item".into();
    assert!(
        session
            .handle_compaction_recovery(Some(stale))
            .await
            .unwrap_err()
            .contains("occurrence")
    );
    let admitted = session
        .handle_compaction_recovery(Some(request.clone()))
        .await
        .unwrap();
    assert_eq!(admitted["accepted"], true);
    assert_eq!(admitted["interruptionCompleted"], false);
    assert!(
        session
            .handle_compaction_recovery(Some(request))
            .await
            .is_err()
    );
    assert_eq!(policy.0.load(Ordering::Acquire), 1);
    assert_eq!(
        session
            .active_turn
            .lock()
            .await
            .as_ref()
            .unwrap()
            .task
            .as_ref()
            .unwrap()
            .turn_context
            .sub_id,
        turn.sub_id
    );
    session.abort_all_tasks(TurnAbortReason::Replaced).await;
}
