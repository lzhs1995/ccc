//! Actor-side registry for occurrence-bound provider-wait recovery.
//!
//! A native host must install a RecoveryPolicy after authenticating enrollment,
//! original controller, client selection/input and Goal authority. No policy is
//! installed by default. Collector integration must publish Control and consume
//! its permit only after actual mapper drain and durable native receipts.

use super::compaction_recovery_gate::Control;
use super::compaction_recovery_gate::Identity;
use super::compaction_recovery_gate::Phase;
use super::compaction_recovery_gate::ReplacementPermit;
use super::compaction_recovery_gate::WaitOutcome;
use super::session::Session;
use crate::client_common::ResponseEvent;
use crate::codex_thread::CodexThread;
use codex_api::compaction_dispatch::CompactionDispatch;
use codex_api::compaction_dispatch::CompactionDispatchAuthority;
use codex_api::compaction_dispatch::CompactionDispatchOwner;
use codex_protocol::compact_recovery::CompactionRecoveryIdentity;
use codex_protocol::compact_recovery::CompactionRecoveryRequest;
use codex_protocol::error::CodexErr;
use codex_protocol::error::CodexErrorDetails;
use codex_protocol::error::Result as CodexResult;
use codex_protocol::protocol::Op;
use futures::StreamExt;
use futures::future::BoxFuture;
use serde_json::Value;
use serde_json::json;
use std::sync::Arc;
use std::sync::Mutex;
use std::time::Duration;
use std::time::Instant;
use tokio::sync::oneshot;
use tokio_util::sync::CancellationToken;

/// Admission cannot be delegated to untrusted request booleans. The original
/// host provides a guard whose lifetime serializes Goal/client/input authority.
pub trait RecoveryPolicy: Send + Sync {
    fn admit<'a>(
        &'a self,
        request: &'a CompactionRecoveryRequest,
    ) -> BoxFuture<'a, Result<Arc<dyn CompactionDispatchAuthority>, String>>;
    fn persist_admission(&self, request: &CompactionRecoveryRequest) -> Result<String, String>;
    fn persist_replacement_dispatched(
        &self,
        expected: &CompactionRecoveryIdentity,
    ) -> Result<(), String>;
    fn persist_terminal(
        &self,
        expected: &CompactionRecoveryIdentity,
        attempt_id: Option<&str>,
        completed: bool,
    ) -> Result<(), String>;
    fn persist_drained(
        &self,
        expected: &CompactionRecoveryIdentity,
        attempt_id: &str,
    ) -> Result<(), String>;
    fn authorize_replacement<'a>(
        &'a self,
        expected: &'a CompactionRecoveryIdentity,
        attempt_id: &'a str,
    ) -> BoxFuture<'a, Result<Arc<dyn CompactionDispatchAuthority>, String>>;
}

/// Real transport and pure collector borrow their original native context.
/// BoxFuture makes the Send contract explicit without unstable async-closure
/// return-type bounds; neither method spawns a parallel compaction task.
pub(crate) trait CompactionIo<T>: Send {
    fn make_stream(&mut self) -> BoxFuture<'_, CodexResult<crate::ResponseStream>>;
    fn collect<'a>(
        &'a mut self,
        stream: &'a mut crate::ResponseStream,
    ) -> BoxFuture<'a, CodexResult<T>>;
}

pub struct RecoveryPolicyHandle(pub Arc<dyn RecoveryPolicy>);

pub(crate) struct LiveWait {
    pub(crate) control: Control,
    pub(crate) original_started: Instant,
    pub(crate) authority: Option<Arc<dyn CompactionDispatchAuthority>>,
    pub(crate) authority_deadline: Option<tokio::time::Instant>,
}

pub(crate) struct Registry {
    pub(crate) native_epoch: String,
    pub(crate) live: Mutex<Option<LiveWait>>,
    pub(crate) occurrence: Mutex<Option<(String, String, Instant)>>,
}

impl Default for Registry {
    fn default() -> Self {
        Self {
            native_epoch: uuid::Uuid::new_v4().to_string(),
            live: Mutex::new(None),
            occurrence: Mutex::new(None),
        }
    }
}

impl CodexThread {
    /// Uses the original Core actor queue. An accepted response is not a native
    /// drained/replacement receipt; callers must reconcile the original attempt.
    pub async fn compaction_recovery(
        &self,
        request: Option<CompactionRecoveryRequest>,
    ) -> CodexResult<Value> {
        let (reply, result) = oneshot::channel();
        self.submit(Op::CompactionRecovery { request, reply })
            .await?;
        result
            .await
            .map_err(|_| CodexErr::InternalAgentDied)?
            .map_err(CodexErr::InvalidRequest)
    }
}

impl Session {
    pub(crate) fn compaction_recovery_registry(&self) -> Arc<Registry> {
        self.services
            .thread_extension_data
            .get_or_init(Registry::default)
    }

    #[expect(
        clippy::await_holding_invalid_type,
        reason = "exact turn and input must remain stable through control admission"
    )]
    pub(crate) async fn handle_compaction_recovery(
        &self,
        request: Option<CompactionRecoveryRequest>,
    ) -> Result<Value, String> {
        tokio::time::timeout(
            Duration::from_secs(5),
            self.handle_compaction_recovery_inner(request),
        )
        .await
        .map_err(|_| {
            "native admission deadline expired; original intent is not replayable".to_owned()
        })?
    }

    async fn handle_compaction_recovery_inner(
        &self,
        request: Option<CompactionRecoveryRequest>,
    ) -> Result<Value, String> {
        // Host authority/Goal order precedes the actor lock, matching native
        // Goal mutation order. Never await Goal I/O with a std registry lock.
        let policy = self
            .services
            .thread_extension_data
            .get::<RecoveryPolicyHandle>();
        let mut authority = if let Some(request) = request.as_ref() {
            let policy = policy
                .as_ref()
                .ok_or("no authenticated original recovery policy")?;
            Some(policy.0.admit(request).await?)
        } else {
            None
        };
        let active = self.active_turn.lock().await;
        let turn = active.as_ref().ok_or("no active original turn")?;
        let task = turn.task.as_ref().ok_or("original task is finishing")?;
        if task.cancellation_token.is_cancelled() {
            return Err("original task cancelled".into());
        }
        let turn_state = turn.turn_state.lock().await;
        if !turn_state.pending_input.is_empty()
            || self.input_queue.has_pending_mailbox_items().await
        {
            return Err("original input is pending".into());
        }
        let registry = self.compaction_recovery_registry();
        let mut live = registry
            .live
            .lock()
            .map_err(|_| "native compaction registry poisoned")?;
        let live = live.as_mut().ok_or("no eligible original provider wait")?;
        let snapshot = live
            .control
            .snapshot()
            .map_err(|_| "native compaction state poisoned")?;
        if snapshot.identity.thread_id != self.thread_id().to_string()
            || snapshot.identity.turn_id != task.turn_context.sub_id
            || snapshot.identity.native_epoch != registry.native_epoch
        {
            return Err("native actor/turn identity changed".into());
        }
        let identity = CompactionRecoveryIdentity {
            native_epoch: snapshot.identity.native_epoch.clone(),
            thread_id: snapshot.identity.thread_id.clone(),
            turn_id: snapshot.identity.turn_id.clone(),
            occurrence: snapshot.identity.occurrence.clone(),
            generation: snapshot.identity.generation,
        };
        let elapsed_ms = live
            .original_started
            .elapsed()
            .as_millis()
            .min(u64::MAX as u128) as u64;
        let Some(request) = request else {
            return Ok(
                json!({"identity": identity, "phase": format!("{:?}", snapshot.phase),
                "elapsedMs": elapsed_ms, "minimumStallMs": 300_000,
                "remainingAttempts": 2usize.saturating_sub(snapshot.replacement_slots_consumed),
                "controlAvailable": self.services.thread_extension_data.get::<RecoveryPolicyHandle>().is_some()}),
            );
        };
        if request.expected != identity {
            return Err("original occurrence identity changed".into());
        }
        if elapsed_ms < 300_000 {
            return Err("original 300-second deadline not reached".into());
        }
        if snapshot.phase != Phase::Waiting {
            return Err("original provider is not waiting".into());
        }
        let policy = policy.ok_or("original policy disappeared")?;
        // A failed persistence may have become visible. The host never retries
        // this attempt blindly, regardless of whether the native gate was claimed.
        let receipt_path = policy.0.persist_admission(&request)?;
        let expected = Identity {
            native_epoch: identity.native_epoch,
            thread_id: identity.thread_id,
            turn_id: identity.turn_id,
            occurrence: identity.occurrence,
            generation: identity.generation,
        };
        let result = live.control.claim(&expected, &request.attempt_id);
        if result.is_ok() {
            let guard = authority.take().expect("authenticated original authority");
            live.authority_deadline = Some(tokio::time::Instant::now() + guard.remaining()?);
            live.authority = Some(guard);
        }
        Ok(
            json!({"attemptId": request.attempt_id, "accepted": result.is_ok(),
            "status": match result { Ok(()) => "claimed".to_owned(), Err(error) => format!("refused:{error:?}") },
            "receiptPath": receipt_path, "interruptionCompleted": false}),
        )
    }
}

pub(crate) struct ActiveRecoveryWait {
    pub(crate) control: Control,
    original_started: Instant,
}

impl Session {
    pub(crate) fn register_compaction_wait(
        &self,
        turn_id: &str,
        occurrence: &str,
        control: Control,
    ) -> CodexResult<ActiveRecoveryWait> {
        let registry = self.compaction_recovery_registry();
        let snapshot = control
            .snapshot()
            .map_err(|_| CodexErr::InvalidRequest("native gate poisoned".into()))?;
        if snapshot.identity.native_epoch != registry.native_epoch
            || snapshot.identity.thread_id != self.thread_id().to_string()
            || snapshot.identity.turn_id != turn_id
            || snapshot.identity.occurrence != occurrence
        {
            return Err(CodexErr::InvalidRequest(
                "original compaction publication changed".into(),
            ));
        }
        let mut live = registry
            .live
            .lock()
            .map_err(|_| CodexErr::InvalidRequest("native registry poisoned".into()))?;
        if live.as_ref().is_some_and(|old| {
            old.control.snapshot().is_ok_and(|old| {
                old.identity != snapshot.identity
                    && matches!(
                        old.phase,
                        Phase::Waiting | Phase::Claimed { .. } | Phase::Drained { .. }
                    )
            })
        }) {
            return Err(CodexErr::InvalidRequest(
                "original compaction control is unsettled".into(),
            ));
        }
        let original_started = Instant::now();
        *live = Some(LiveWait {
            control: control.clone(),
            original_started,
            authority: None,
            authority_deadline: None,
        });
        Ok(ActiveRecoveryWait {
            control,
            original_started,
        })
    }

    pub(crate) async fn finish_compaction_recovery_drain(
        &self,
        active_wait: &ActiveRecoveryWait,
        permit: &super::compaction_recovery_gate::ReplacementPermit,
    ) -> CodexResult<Arc<dyn CompactionDispatchAuthority>> {
        let snapshot = active_wait
            .control
            .snapshot()
            .map_err(|_| CodexErr::InvalidRequest("native gate poisoned".into()))?;
        let expected = CompactionRecoveryIdentity {
            native_epoch: snapshot.identity.native_epoch,
            thread_id: snapshot.identity.thread_id,
            turn_id: snapshot.identity.turn_id,
            occurrence: snapshot.identity.occurrence,
            generation: snapshot.identity.generation,
        };
        let policy = self
            .services
            .thread_extension_data
            .get::<RecoveryPolicyHandle>()
            .ok_or_else(|| CodexErr::InvalidRequest("original native policy disappeared".into()))?;
        policy
            .0
            .persist_drained(&expected, permit.attempt_id())
            .map_err(CodexErr::InvalidRequest)?;
        policy
            .0
            .authorize_replacement(&expected, permit.attempt_id())
            .await
            .map_err(CodexErr::InvalidRequest)
    }
}

impl Session {
    pub(crate) fn observe_compaction_item(&self, turn_id: &str, item_id: &str) {
        let registry = self.compaction_recovery_registry();
        if let Ok(mut occurrence) = registry.occurrence.lock() {
            if occurrence
                .as_ref()
                .is_none_or(|(turn, item, _)| turn != turn_id || item != item_id)
            {
                *occurrence = Some((turn_id.to_owned(), item_id.to_owned(), Instant::now()));
            }
        }
    }

    /// The factory and collector are the real original transport closures. The
    /// factory is called again only after both old task completion fences and
    /// the native durable drained receipt. No new task/turn/Goal is created.
    pub(crate) async fn collect_recoverable_compaction<T: Send>(
        &self,
        turn_id: &str,
        mut io: impl CompactionIo<T>,
    ) -> CodexResult<T> {
        let registry = self.compaction_recovery_registry();
        let (original_turn, occurrence, original_started) = registry
            .occurrence
            .lock()
            .map_err(|_| CodexErr::InvalidRequest("native occurrence poisoned".into()))?
            .clone()
            .ok_or_else(|| CodexErr::InvalidRequest("native item occurrence absent".into()))?;
        if original_turn != turn_id {
            return Err(CodexErr::InvalidRequest(
                "native item belongs to another turn".into(),
            ));
        }
        let _authority_cleanup = AuthorityCleanup(registry.clone());
        let mut active_wait: Option<ActiveRecoveryWait> = None;
        let mut replacement_authority: Option<Arc<dyn CompactionDispatchAuthority>> = None;
        let mut replacement_active_guard = None;
        let mut replacement_state_guard = None;
        let mut replacement_mail_guard = None;
        let mut replacement_controller_mail_guard = None;
        let mut recovery_deadline = None;
        // Keep the actor's active-turn lock from final admission through the
        // next provider dispatch. Never hold it while waiting for the response.
        let mut replacement_reservation: Option<
            super::compaction_recovery_gate::ReplacementReservation,
        > = None;
        loop {
            let is_replacement = replacement_reservation.is_some();
            let dispatch = replacement_authority.take().map(CompactionDispatch::new);
            let dispatch_owner = dispatch
                .as_ref()
                .map(|dispatch| CompactionDispatchOwner(dispatch.clone()));
            let transport_scope = codex_api::compaction_dispatch::CompactionTransportScope::new();
            let _transport_owner =
                codex_api::compaction_dispatch::CompactionTransportOwner(transport_scope.clone());
            // Register BEFORE the real HTTP headers/WebSocket handshake future.
            // Its owned tasks receive this exact scope before spawn.
            let factory = Box::pin(codex_api::compaction_dispatch::track_transport(
                transport_scope.clone(),
                async {
                    if let Some(dispatch) = dispatch.as_ref() {
                        tokio::time::timeout(
                            Duration::from_secs(5),
                            codex_api::compaction_dispatch::scope(
                                dispatch.clone(),
                                io.make_stream(),
                            ),
                        )
                        .await
                        .map_err(|_| {
                            CodexErr::InvalidRequest("replacement dispatch UNKNOWN".into())
                        })?
                    } else {
                        io.make_stream().await
                    }
                },
            ));
            let factory_wait = if let Some(reservation) = replacement_reservation.take() {
                reservation.attach(factory).map_err(|error| {
                    CodexErr::InvalidRequest(format!("native replacement refused: {error:?}"))
                })?
            } else {
                let identity = Identity {
                    native_epoch: registry.native_epoch.clone(),
                    thread_id: self.thread_id().to_string(),
                    turn_id: turn_id.to_owned(),
                    occurrence: occurrence.clone(),
                    generation: 0,
                };
                let previous = {
                    let live = registry
                        .live
                        .lock()
                        .map_err(|_| CodexErr::InvalidRequest("native registry poisoned".into()))?;
                    live.as_ref().and_then(|live| {
                        live.control
                            .snapshot()
                            .ok()
                            .filter(|old| {
                                old.identity.turn_id == turn_id
                                    && old.identity.occurrence == occurrence
                            })
                            .map(|_| live.control.clone())
                    })
                };
                let (control, wait) = if let Some(control) = previous {
                    let wait = control.followup(factory).map_err(|error| {
                        CodexErr::InvalidRequest(format!("original wait unsettled: {error:?}"))
                    })?;
                    (control, wait)
                } else {
                    Control::begin(identity, factory).map_err(|error| {
                        CodexErr::InvalidRequest(format!("native initial wait refused: {error:?}"))
                    })?
                };
                let mut active = self.register_compaction_wait(turn_id, &occurrence, control)?;
                active.original_started = original_started;
                if let Ok(mut live) = registry.live.lock() {
                    if let Some(live) = live.as_mut() {
                        live.original_started = original_started;
                    }
                }
                active_wait = Some(active);
                wait
            };
            let factory_outcome = if let Some(deadline) = recovery_deadline {
                tokio::time::timeout_at(deadline, factory_wait)
                    .await
                    .map_err(|_| {
                        CodexErr::InvalidRequest("original native recovery deadline UNKNOWN".into())
                    })?
            } else {
                factory_wait.await
            };
            let (mut stream, factory_claim) = match factory_outcome {
                WaitOutcome::Completed(result) => (Some(result?), None),
                WaitOutcome::Replacement {
                    permit,
                    raced_output,
                } => {
                    let stream = match raced_output {
                        Some(Ok(stream)) => Some(stream),
                        Some(Err(error)) => return Err(error),
                        None => None,
                    };
                    (stream, Some(permit))
                }
                WaitOutcome::Refused(error) => {
                    return Err(CodexErr::InvalidRequest(format!(
                        "native factory wait refused: {error:?}"
                    )));
                }
            };
            if let Some(dispatch) = dispatch.as_ref() {
                if tokio::time::timeout_at(
                    recovery_deadline
                        .expect("original recovery deadline")
                        .min(tokio::time::Instant::now() + Duration::from_secs(5)),
                    dispatch.confirmed(),
                )
                .await
                .is_err()
                {
                    transport_scope.cancel.cancel();
                    return Err(CodexErr::InvalidRequest(
                        "replacement native send receipt UNKNOWN".into(),
                    ));
                }
            }
            drop(dispatch_owner);
            drop(dispatch);
            drop(replacement_controller_mail_guard.take());
            drop(replacement_mail_guard.take());
            drop(replacement_state_guard.take());
            drop(replacement_active_guard.take());
            if is_replacement {
                if let Ok(mut live) = registry.live.lock() {
                    if let Some(live) = live.as_mut() {
                        live.authority.take();
                    }
                }
            }
            let (permit, raced_output) = if let Some(permit) = factory_claim {
                (permit, None)
            } else {
                let stream = stream.as_mut().expect("completed original factory");
                // Extension fixture streams remain normal streams; they do not
                // advertise a provider-drain capability they cannot prove.
                if stream.mapper_finished.is_none()
                    || stream.provider_finished.is_none()
                    || stream.provider_cancel.is_none()
                {
                    return io.collect(stream).await;
                }
                let wait = active_wait
                    .as_ref()
                    .expect("registered native factory")
                    .control
                    .next_stage(io.collect(stream))
                    .map_err(|error| {
                        CodexErr::InvalidRequest(format!(
                            "native collection transition refused: {error:?}"
                        ))
                    })?;
                match wait.await {
                    WaitOutcome::Completed(result) => {
                        if is_replacement {
                            if let Err(error) = self.persist_compaction_provider_terminal(
                                active_wait.as_ref().expect("original wait"),
                                None,
                                result.is_ok(),
                            ) {
                                tracing::warn!(%error, "native terminal receipt UNKNOWN; preserving received output and usage");
                            }
                        }
                        return result;
                    }
                    WaitOutcome::Refused(error) => {
                        return Err(CodexErr::InvalidRequest(format!(
                            "native compact wait refused: {error:?}"
                        )));
                    }
                    WaitOutcome::Replacement {
                        permit,
                        raced_output,
                    } => (permit, raced_output),
                }
            };
            let deadline = {
                let live = registry
                    .live
                    .lock()
                    .map_err(|_| CodexErr::InvalidRequest("native registry poisoned".into()))?;
                live.as_ref()
                    .and_then(|live| live.authority_deadline)
                    .ok_or_else(|| {
                        CodexErr::InvalidRequest("original native recovery deadline absent".into())
                    })?
            };
            recovery_deadline = Some(deadline);
            if let Some(result) = raced_output {
                // Completion already exists: preserve its sole original result
                // and accounting, consume this control intent, send no replacement.
                if let Err(error) = self.persist_compaction_provider_terminal(
                    active_wait.as_ref().expect("original wait"),
                    Some(permit.attempt_id()),
                    result.is_ok(),
                ) {
                    tracing::warn!(%error, "native race receipt UNKNOWN; preserving received output and usage");
                }
                drop(permit);
                return result;
            }
            if let Some(mut original) = stream.take() {
                let mapper = original.mapper_finished.clone().ok_or_else(|| {
                    CodexErr::InvalidRequest("original mapper fence unavailable".into())
                })?;
                let provider = original.provider_finished.clone().ok_or_else(|| {
                    CodexErr::InvalidRequest("original provider fence unavailable".into())
                })?;
                tokio::time::timeout_at(
                    deadline,
                    self.drain_original_compaction_provider(turn_id, &mut original),
                )
                .await
                .map_err(|_| {
                    CodexErr::InvalidRequest(
                        "original authority expired during drain UNKNOWN".into(),
                    )
                })??;
                drop(original);
                tokio::time::timeout_at(
                    deadline.min(tokio::time::Instant::now() + Duration::from_secs(5)),
                    async {
                        mapper.cancelled().await;
                        provider.cancelled().await;
                    },
                )
                .await
                .map_err(|_| CodexErr::InvalidRequest("original provider drain UNKNOWN".into()))?;
            } else {
                // The borrowed factory was synchronously dropped by the gate.
                // Stop and join every provider/mapper it actually spawned. This
                // does not invent a remote usage value when no response arrived.
                transport_scope.cancel.cancel();
            }
            tokio::time::timeout_at(
                deadline.min(tokio::time::Instant::now() + Duration::from_secs(5)),
                transport_scope.drain(),
            )
            .await
            .map_err(|_| {
                CodexErr::InvalidRequest("original native task scope drain UNKNOWN".into())
            })?;
            replacement_authority = Some(
                tokio::time::timeout_at(
                    deadline,
                    self.finish_compaction_recovery_drain(
                        active_wait.as_ref().expect("original registered wait"),
                        &permit,
                    ),
                )
                .await
                .map_err(|_| {
                    CodexErr::InvalidRequest(
                        "original replacement authority expired UNKNOWN".into(),
                    )
                })??,
            );
            let active = tokio::time::timeout_at(deadline, self.active_turn.lock())
                .await
                .map_err(|_| {
                    CodexErr::InvalidRequest("original actor lock deadline expired UNKNOWN".into())
                })?;
            let turn = active.as_ref().ok_or_else(|| {
                CodexErr::InvalidRequest("original turn exited during drain".into())
            })?;
            let task = turn.task.as_ref().ok_or_else(|| {
                CodexErr::InvalidRequest("original task finishing during drain".into())
            })?;
            if task.turn_context.sub_id != turn_id || task.cancellation_token.is_cancelled() {
                return Err(CodexErr::InvalidRequest(
                    "original task changed during drain".into(),
                ));
            }
            let state = tokio::time::timeout_at(deadline, turn.turn_state.clone().lock_owned())
                .await
                .map_err(|_| {
                    CodexErr::InvalidRequest(
                        "original turn input lock deadline expired UNKNOWN".into(),
                    )
                })?;
            if !state.pending_input.is_empty()
                || tokio::time::timeout_at(deadline, self.input_queue.has_pending_mailbox_items())
                    .await
                    .map_err(|_| {
                        CodexErr::InvalidRequest(
                            "original mailbox check deadline expired UNKNOWN".into(),
                        )
                    })?
            {
                return Err(CodexErr::InvalidRequest(
                    "original input arrived during drain".into(),
                ));
            }
            let mail =
                tokio::time::timeout_at(deadline, self.input_queue.compaction_mailbox_guard())
                    .await
                    .map_err(|_| {
                        CodexErr::InvalidRequest(
                            "original mailbox guard deadline expired UNKNOWN".into(),
                        )
                    })?
                    .map_err(CodexErr::InvalidRequest)?;
            replacement_controller_mail_guard = self
                .input_queue
                .reserve_controller_mailbox()
                .map_err(CodexErr::InvalidRequest)?;
            let cancellation = task.cancellation_token.clone();
            let controller_mail = self.input_queue.mailbox_updates();
            replacement_authority = Some(Arc::new(ActorDispatchAuthority {
                inner: replacement_authority.take().expect("original authority"),
                cancellation,
                controller_mail,
                controller_reservation: replacement_controller_mail_guard
                    .as_ref()
                    .map(Arc::downgrade),
            }));
            replacement_reservation = Some(permit.reserve().map_err(|error| {
                CodexErr::InvalidRequest(format!("native slot reservation refused: {error:?}"))
            })?);
            replacement_mail_guard = Some(mail);
            replacement_state_guard = Some(state);
            replacement_active_guard = Some(active);
        }
    }
}

impl Session {
    fn persist_compaction_provider_terminal(
        &self,
        active: &ActiveRecoveryWait,
        attempt_id: Option<&str>,
        completed: bool,
    ) -> CodexResult<()> {
        let snapshot = active
            .control
            .snapshot()
            .map_err(|_| CodexErr::InvalidRequest("native gate poisoned".into()))?;
        let expected = CompactionRecoveryIdentity {
            native_epoch: snapshot.identity.native_epoch,
            thread_id: snapshot.identity.thread_id,
            turn_id: snapshot.identity.turn_id,
            occurrence: snapshot.identity.occurrence,
            generation: snapshot.identity.generation,
        };
        let policy = self
            .services
            .thread_extension_data
            .get::<RecoveryPolicyHandle>()
            .ok_or_else(|| CodexErr::InvalidRequest("original native policy disappeared".into()))?;
        policy
            .0
            .persist_terminal(&expected, attempt_id, completed)
            .map_err(CodexErr::InvalidRequest)
    }

    async fn drain_original_compaction_provider(
        &self,
        turn_id: &str,
        stream: &mut crate::ResponseStream,
    ) -> CodexResult<()> {
        let turn_context = self.turn_context_for_sub_id(turn_id).await.ok_or_else(|| {
            CodexErr::InvalidRequest("original turn changed during compact drain".into())
        })?;
        let retire = stream.provider_cancel.clone().ok_or_else(|| {
            CodexErr::InvalidRequest("provider retirement fence unavailable".into())
        })?;
        let finished = stream.provider_finished.clone().ok_or_else(|| {
            CodexErr::InvalidRequest("provider completion fence unavailable".into())
        })?;
        let deadline = tokio::time::Instant::now() + Duration::from_secs(5);
        if let Some(interrupt) = stream.interrupt.take() {
            // Reuse the actual WebSocket protocol: validate interruption ACK,
            // retain completion usage, and retire the socket on failed drain.
            let _ = interrupt.send(codex_api::ResponseInterrupt::Cancel { deadline });
        } else {
            // HTTP has no server interrupt ACK. Retire only its owned response
            // task, then drain every event already accepted by the native mapper.
            retire.cancel();
        }
        let mut forced_retirement = false;
        loop {
            let event = if forced_retirement {
                tokio::time::timeout_at(deadline + Duration::from_secs(1), stream.next())
                    .await
                    .map_err(|_| CodexErr::InvalidRequest("native provider drain UNKNOWN".into()))?
            } else {
                match tokio::time::timeout_at(deadline, stream.next()).await {
                    Ok(event) => event,
                    Err(_) => {
                        retire.cancel();
                        forced_retirement = true;
                        continue;
                    }
                }
            };
            let Some(event) = event else {
                break;
            };
            match event {
                Ok(ResponseEvent::Completed {
                    response_id,
                    token_usage,
                    usage_metadata,
                    ..
                }) => {
                    self.record_observed_response_completed(
                        &turn_context,
                        &response_id,
                        token_usage.as_ref(),
                        usage_metadata.as_ref(),
                    )
                    .await;
                    self.update_token_usage_info(&turn_context, token_usage.as_ref())
                        .await?;
                    // The mapper remains open until provider terminal shutdown;
                    // do not discard another queued accounting event on first completion.
                }
                Err(error) => {
                    if let CodexErrorDetails::ResponseIncomplete(incomplete) = error.details() {
                        self.record_incomplete_response_usage(
                            &turn_context,
                            &turn_context.initial_settings,
                            incomplete,
                        )
                        .await;
                        self.send_token_count_event(&turn_context).await;
                    }
                }
                _ => {}
            }
        }
        tokio::time::timeout_at(deadline + Duration::from_secs(1), finished.cancelled())
            .await
            .map_err(|_| CodexErr::InvalidRequest("provider exit remains UNKNOWN".into()))?;
        Ok(())
    }
}

struct AuthorityCleanup(Arc<Registry>);
impl Drop for AuthorityCleanup {
    fn drop(&mut self) {
        if let Ok(mut live) = self.0.live.lock() {
            if let Some(live) = live.as_mut() {
                live.authority.take();
            }
        }
    }
}

struct ActorDispatchAuthority {
    inner: Arc<dyn CompactionDispatchAuthority>,
    cancellation: CancellationToken,
    controller_mail: Option<tokio::sync::watch::Receiver<bool>>,
    controller_reservation:
        Option<std::sync::Weak<dyn crate::agent::api::CompactionMailboxReservation>>,
}
impl ActorDispatchAuthority {
    fn validate_controller_epoch(&self) -> Result<(), String> {
        if let Some(reservation) = &self.controller_reservation {
            reservation
                .upgrade()
                .ok_or("original mailbox reservation released")?
                .validate_current()
                .map_err(|error| error.to_string())?;
        }
        Ok(())
    }
}
impl CompactionDispatchAuthority for ActorDispatchAuthority {
    fn remaining(&self) -> Result<Duration, String> {
        self.inner.remaining()
    }
    fn validate_fresh(&self) -> Result<(), String> {
        if self.cancellation.is_cancelled()
            || self
                .controller_mail
                .as_ref()
                .is_some_and(|mail| *mail.borrow())
        {
            return Err("original turn cancelled or mailbox input arrived".into());
        }
        self.validate_controller_epoch()?;
        self.inner.validate_fresh()?;
        self.validate_controller_epoch()?;
        if self.cancellation.is_cancelled()
            || self
                .controller_mail
                .as_ref()
                .is_some_and(|mail| *mail.borrow())
        {
            return Err(
                "original turn cancelled or controller mailbox changed during attestation".into(),
            );
        }
        Ok(())
    }
    fn persist_send_intent(&self) -> Result<(), String> {
        self.inner.persist_send_intent()
    }
    fn persist_sent(&self) -> Result<(), String> {
        self.inner.persist_sent()
    }
}

#[cfg(test)]
mod authority_tests {
    use super::*;
    struct SlowAuthority {
        entered: Arc<std::sync::Barrier>,
        release: Arc<std::sync::Barrier>,
    }
    impl CompactionDispatchAuthority for SlowAuthority {
        fn remaining(&self) -> Result<Duration, String> {
            Ok(Duration::from_secs(15))
        }
        fn validate_fresh(&self) -> Result<(), String> {
            self.entered.wait();
            self.release.wait();
            Ok(())
        }
        fn persist_send_intent(&self) -> Result<(), String> {
            panic!("mail change must reject before intent")
        }
        fn persist_sent(&self) -> Result<(), String> {
            panic!("mail change must reject before send")
        }
    }
    #[test]
    fn original_controller_mail_arriving_during_native_attestation_rejects_dispatch() {
        let inner = Arc::new(SlowAuthority {
            entered: Arc::new(std::sync::Barrier::new(2)),
            release: Arc::new(std::sync::Barrier::new(2)),
        });
        let (sender, receiver) = tokio::sync::watch::channel(false);
        let authority = ActorDispatchAuthority {
            inner: inner.clone(),
            cancellation: CancellationToken::new(),
            controller_mail: Some(receiver),
            controller_reservation: None,
        };
        let worker = std::thread::spawn(move || authority.validate_fresh());
        inner.entered.wait();
        sender.send_replace(true);
        inner.release.wait();
        assert!(
            worker
                .join()
                .unwrap()
                .unwrap_err()
                .contains("during attestation")
        );
    }
    struct EpochReservation(Arc<std::sync::atomic::AtomicBool>);
    impl crate::agent::api::CompactionMailboxReservation for EpochReservation {
        fn validate_current(&self) -> CodexResult<()> {
            if self.0.load(std::sync::atomic::Ordering::SeqCst) {
                Err(CodexErr::InvalidRequest(
                    "original mailbox epoch closed".into(),
                ))
            } else {
                Ok(())
            }
        }
    }
    #[test]
    fn original_mailbox_epoch_closed_during_native_attestation_rejects_dispatch() {
        let inner = Arc::new(SlowAuthority {
            entered: Arc::new(std::sync::Barrier::new(2)),
            release: Arc::new(std::sync::Barrier::new(2)),
        });
        let closed = Arc::new(std::sync::atomic::AtomicBool::new(false));
        let reservation: Arc<dyn crate::agent::api::CompactionMailboxReservation> =
            Arc::new(EpochReservation(closed.clone()));
        let authority = ActorDispatchAuthority {
            inner: inner.clone(),
            cancellation: CancellationToken::new(),
            controller_mail: None,
            controller_reservation: Some(Arc::downgrade(&reservation)),
        };
        let worker = std::thread::spawn(move || authority.validate_fresh());
        inner.entered.wait();
        closed.store(true, std::sync::atomic::Ordering::SeqCst);
        inner.release.wait();
        assert!(worker.join().unwrap().unwrap_err().contains("epoch closed"));
    }
}
