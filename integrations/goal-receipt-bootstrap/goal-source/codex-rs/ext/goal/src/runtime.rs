use std::sync::Arc;
use std::sync::Weak;
use std::sync::atomic::AtomicBool;
use std::sync::atomic::Ordering;

use codex_core::StartIfIdleSubmission;
use codex_core::ThreadManager;
use codex_core::TurnInput;
use codex_core::TurnInputRequest;
use codex_core::TurnStartOptions;
use codex_protocol::ThreadId;
use codex_protocol::models::ResponseItem;
use codex_protocol::protocol::ThreadGoal;

use crate::accounting::BudgetLimitedGoalDisposition;
use crate::accounting::GoalAccountingState;
use crate::analytics::GoalAnalytics;
use crate::analytics::GoalEventAttribution;
use crate::events::GoalEventEmitter;
use crate::metrics::GoalMetrics;
use crate::steering::continuation_steering_item;
use crate::steering::objective_updated_steering_item;
use crate::tool::protocol_goal_from_state;
use tokio::sync::Semaphore;
use tokio::sync::SemaphorePermit;

#[derive(Clone)]
pub struct GoalRuntimeHandle {
    inner: Arc<GoalRuntimeInner>,
}

pub(crate) struct GoalRuntimeConfig {
    pub(crate) analytics: GoalAnalytics,
    pub(crate) enabled: bool,
    pub(crate) tools_available_for_thread: bool,
    pub(crate) tools_visible_for_thread: bool,
    pub(crate) root_accounting_state: Option<Arc<GoalAccountingState>>,
}

pub(crate) enum ActiveGoalStopReason {
    TurnError,
    UsageLimit,
    ExecutionUnavailable { expected_goal_id: String },
    EmptyResponse,
}

struct GoalRuntimeInner {
    thread_id: ThreadId,
    state_dbs: Arc<codex_state::StateRuntime>,
    analytics: GoalAnalytics,
    event_emitter: GoalEventEmitter,
    metrics: GoalMetrics,
    thread_manager: Weak<ThreadManager>,
    accounting_state: Arc<GoalAccountingState>,
    root_accounting_state: Option<Arc<GoalAccountingState>>,
    enabled: AtomicBool,
    tools_available_for_thread: bool,
    tools_visible_for_thread: bool,
    goal_state_lock: Arc<Semaphore>,
    recovery_runtime_id: String,
    #[cfg(test)]
    recovery_post_commit: std::sync::Mutex<Option<Arc<RecoveryPostCommitPause>>>,
}

pub(crate) struct AccountedGoalProgress {
    pub(crate) goal: ThreadGoal,
    pub(crate) goal_id: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PreviousGoalSnapshot {
    pub goal_id: String,
    pub status: codex_state::ThreadGoalStatus,
    pub objective: String,
}

impl From<&codex_state::ThreadGoal> for PreviousGoalSnapshot {
    fn from(goal: &codex_state::ThreadGoal) -> Self {
        Self {
            goal_id: goal.goal_id.clone(),
            status: goal.status,
            objective: goal.objective.clone(),
        }
    }
}

impl std::fmt::Debug for GoalRuntimeHandle {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("GoalRuntimeHandle").finish_non_exhaustive()
    }
}

impl GoalRuntimeHandle {
    pub(crate) fn new(
        thread_id: ThreadId,
        state_dbs: Arc<codex_state::StateRuntime>,
        event_emitter: GoalEventEmitter,
        metrics: GoalMetrics,
        thread_manager: Weak<ThreadManager>,
        accounting_state: Arc<GoalAccountingState>,
        config: GoalRuntimeConfig,
    ) -> Self {
        Self {
            inner: Arc::new(GoalRuntimeInner {
                thread_id,
                state_dbs,
                analytics: config.analytics,
                event_emitter,
                metrics,
                thread_manager,
                accounting_state,
                root_accounting_state: config.root_accounting_state,
                enabled: AtomicBool::new(config.enabled),
                tools_available_for_thread: config.tools_available_for_thread,
                tools_visible_for_thread: config.tools_visible_for_thread,
                goal_state_lock: Arc::new(Semaphore::new(/*permits*/ 1)),
                recovery_runtime_id: ThreadId::new().to_string(),
                #[cfg(test)]
                recovery_post_commit: std::sync::Mutex::new(None),
            }),
        }
    }

    pub(crate) fn set_enabled(&self, enabled: bool) {
        self.inner.enabled.store(enabled, Ordering::Relaxed);
    }

    pub(crate) fn is_enabled(&self) -> bool {
        self.inner.enabled.load(Ordering::Relaxed)
    }

    pub(crate) fn tools_visible(&self) -> bool {
        self.is_enabled() && self.inner.tools_visible_for_thread
    }

    pub(crate) fn tools_available(&self) -> bool {
        self.is_enabled() && self.inner.tools_available_for_thread
    }

    pub(crate) fn thread_id(&self) -> ThreadId {
        self.inner.thread_id
    }

    pub(crate) fn accounting_state(&self) -> Arc<GoalAccountingState> {
        Arc::clone(&self.inner.accounting_state)
    }

    pub(crate) fn root_accounting_state(&self) -> Option<Arc<GoalAccountingState>> {
        self.inner.root_accounting_state.clone()
    }

    pub(crate) async fn clear_pending_turn_start_options(&self) {
        let Some(thread_manager) = self.inner.thread_manager.upgrade() else {
            return;
        };
        let Ok(thread) = thread_manager.get_thread(self.inner.thread_id).await else {
            return;
        };
        thread.thread_extension_data().remove::<TurnStartOptions>();
    }

    pub(crate) async fn goal_state_permit(&self) -> Result<SemaphorePermit<'_>, String> {
        self.inner
            .goal_state_lock
            .acquire()
            .await
            .map_err(|err| err.to_string())
    }

    /// Reserve both actual Goal mutation and progress accounting, in the
    /// existing Goal→progress order. Token events may still accumulate their
    /// native delta; only this reservation can settle it before replacement.
    pub async fn compaction_recovery_goal_guard(
        &self,
        expected: &serde_json::Value,
    ) -> Result<Arc<CompactionGoalReservation>, String> {
        let goal = self
            .inner
            .goal_state_lock
            .clone()
            .acquire_owned()
            .await
            .map_err(|e| e.to_string())?;
        let progress = self
            .inner
            .accounting_state
            .owned_progress_accounting_permit()
            .await
            .map_err(|e| e.to_string())?;
        let actual = compaction_goal_value(
            self.inner
                .state_dbs
                .thread_goals()
                .get_thread_goal(self.inner.thread_id)
                .await
                .map_err(|e| e.to_string())?,
        );
        validate_compaction_goal(&actual, expected)?;
        Ok(Arc::new(CompactionGoalReservation {
            runtime: self.clone(),
            _goal: goal,
            _progress: progress,
            expected: actual,
            settled: std::sync::Mutex::new(None),
        }))
    }

    pub async fn compaction_recovery_goal_snapshot(&self) -> Result<serde_json::Value, String> {
        let _permit = self.goal_state_permit().await?;
        let goal = self
            .inner
            .state_dbs
            .thread_goals()
            .get_thread_goal(self.inner.thread_id)
            .await
            .map_err(|error| error.to_string())?;
        Ok(compaction_goal_value(goal))
    }

    pub async fn prepare_external_goal_mutation(&self) -> Result<(), String> {
        if !self.is_enabled() {
            return Ok(());
        }
        // Invalidate the old turn before the persisted objective/status changes.
        self.inner.accounting_state.reset_empty_responses();

        if let Some(turn_id) = self.inner.accounting_state.current_turn_id() {
            self.account_active_goal_progress(
                turn_id.as_str(),
                &format!("{turn_id}:external-goal-mutation"),
                codex_state::GoalAccountingMode::ActiveOnly,
                BudgetLimitedGoalDisposition::ClearActive,
            )
            .await?;
            return Ok(());
        }

        self.account_idle_goal_progress(
            &format!("{}:external-goal-mutation", self.inner.thread_id),
            codex_state::GoalAccountingMode::ActiveOnly,
            BudgetLimitedGoalDisposition::ClearActive,
        )
        .await?;
        Ok(())
    }

    pub async fn apply_external_goal_set(
        &self,
        goal: codex_state::ThreadGoal,
        previous_goal: Option<PreviousGoalSnapshot>,
    ) -> Result<(), String> {
        if !self.is_enabled() {
            return Ok(());
        }

        self.inner.accounting_state.reset_empty_responses();
        let replaced_existing_goal = previous_goal
            .as_ref()
            .is_some_and(|previous_goal| previous_goal.goal_id != goal.goal_id);
        if previous_goal.is_none() || replaced_existing_goal {
            self.inner.metrics.record_created();
            self.inner
                .analytics
                .created(&goal, GoalEventAttribution::NoTurn);
        }
        let previous_status = previous_goal
            .as_ref()
            .and_then(|previous_goal| (!replaced_existing_goal).then_some(previous_goal.status));
        self.inner
            .metrics
            .record_resumed_if_status_changed(previous_status, goal.status);
        self.inner
            .metrics
            .record_terminal_if_status_changed(previous_status, &goal);
        self.inner
            .analytics
            .status_changed(&goal, previous_status, GoalEventAttribution::NoTurn);
        let objective_changed = previous_goal.as_ref().is_some_and(|previous_goal| {
            !replaced_existing_goal && previous_goal.objective != goal.objective
        });
        match goal.status {
            codex_state::ThreadGoalStatus::Active => {
                if self.inner.accounting_state.current_turn_id().is_some() {
                    let _ = self
                        .inner
                        .accounting_state
                        .mark_current_turn_goal_active(goal.goal_id.clone());
                } else {
                    self.inner
                        .accounting_state
                        .mark_idle_goal_active(goal.goal_id.clone());
                }
                if objective_changed {
                    let item = objective_updated_steering_item(&protocol_goal_from_state(goal));
                    self.inject_active_turn_steering(item).await;
                }
                self.continue_if_idle().await?;
            }
            codex_state::ThreadGoalStatus::BudgetLimited => {
                if self.inner.accounting_state.current_turn_id().is_none() {
                    self.inner.accounting_state.clear_active_goal();
                }
            }
            codex_state::ThreadGoalStatus::Paused
            | codex_state::ThreadGoalStatus::Blocked
            | codex_state::ThreadGoalStatus::UsageLimited
            | codex_state::ThreadGoalStatus::Complete => {
                self.inner.accounting_state.clear_active_goal();
            }
        }
        Ok(())
    }

    pub async fn apply_external_goal_clear(
        &self,
        goal: codex_state::ThreadGoal,
    ) -> Result<(), String> {
        if !self.is_enabled() {
            return Ok(());
        }

        self.inner.analytics.cleared(&goal);
        self.inner.accounting_state.clear_active_goal();
        Ok(())
    }

    pub async fn usage_limit_active_goal_for_turn(&self, turn_id: &str) -> Result<(), String> {
        self.stop_active_goal_for_turn(turn_id, ActiveGoalStopReason::UsageLimit)
            .await
    }

    /// Accounts the ending turn and stops its active goal after an error or repeated empty output.
    pub(crate) async fn stop_active_goal_for_turn(
        &self,
        turn_id: &str,
        reason: ActiveGoalStopReason,
    ) -> Result<(), String> {
        if !self.is_enabled() {
            return Ok(());
        }

        // Hold this through accounting and the status update so external goal
        // mutations and idle continuation cannot interleave between them.
        let _goal_state_permit = self.goal_state_permit().await?;
        let accounting = &self.inner.accounting_state;
        let _turn_start_permit = if matches!(reason, ActiveGoalStopReason::EmptyResponse)
            && accounting.interrupted_turn_id().as_deref() == Some(turn_id)
        {
            Some(
                accounting
                    .turn_start_permit()
                    .await
                    .map_err(|err| err.to_string())?,
            )
        } else {
            None
        };
        let Some(accounting_goal_id) = self
            .inner
            .accounting_state
            .current_active_goal_id_for_turn(turn_id)
        else {
            return Ok(());
        };
        if let ActiveGoalStopReason::ExecutionUnavailable { expected_goal_id } = &reason
            && accounting_goal_id != *expected_goal_id
        {
            return Ok(());
        }

        let (event_name, status, expected_goal_id) = match reason {
            ActiveGoalStopReason::TurnError => {
                ("turn-error", codex_state::ThreadGoalStatus::Blocked, None)
            }
            ActiveGoalStopReason::UsageLimit => (
                "usage-limit",
                codex_state::ThreadGoalStatus::UsageLimited,
                None,
            ),
            ActiveGoalStopReason::EmptyResponse => {
                let Some(expected_goal_id) = accounting.empty_response_goal(turn_id) else {
                    return Ok(());
                };
                if accounting_goal_id != expected_goal_id {
                    return Ok(());
                }
                (
                    "empty-response",
                    codex_state::ThreadGoalStatus::Blocked,
                    Some(expected_goal_id),
                )
            }
            ActiveGoalStopReason::ExecutionUnavailable { expected_goal_id } => (
                "execution-unavailable",
                codex_state::ThreadGoalStatus::Blocked,
                Some(expected_goal_id),
            ),
        };
        self.account_active_goal_progress(
            turn_id,
            &format!("{turn_id}:{event_name}-progress"),
            codex_state::GoalAccountingMode::ActiveOnly,
            BudgetLimitedGoalDisposition::ClearActive,
        )
        .await?;

        let Some(active_goal) = self
            .inner
            .state_dbs
            .thread_goals()
            .get_thread_goal(self.thread_id())
            .await
            .map_err(|err| err.to_string())?
        else {
            self.inner.accounting_state.clear_active_goal();
            return Ok(());
        };
        if expected_goal_id
            .as_ref()
            .is_some_and(|expected_goal_id| active_goal.goal_id != *expected_goal_id)
        {
            return Ok(());
        }
        let can_stop = active_goal.status == codex_state::ThreadGoalStatus::Active
            || (active_goal.status == codex_state::ThreadGoalStatus::BudgetLimited
                && status == codex_state::ThreadGoalStatus::UsageLimited);
        if !can_stop {
            self.inner.accounting_state.clear_active_goal();
            return Ok(());
        }
        let previous_status = Some(active_goal.status);
        let Some(goal) = self
            .inner
            .state_dbs
            .thread_goals()
            .update_thread_goal(
                self.thread_id(),
                codex_state::GoalUpdate {
                    objective: None,
                    status: Some(status),
                    token_budget: None,
                    expected_goal_id: Some(active_goal.goal_id),
                },
            )
            .await
            .map_err(|err| err.to_string())?
        else {
            return Ok(());
        };
        self.inner
            .metrics
            .record_terminal_if_status_changed(previous_status, &goal);
        self.inner.analytics.status_changed(
            &goal,
            previous_status,
            GoalEventAttribution::Turn(turn_id),
        );
        self.inner.accounting_state.clear_active_goal();
        let goal = protocol_goal_from_state(goal);
        self.inner.event_emitter.thread_goal_updated(
            format!("{turn_id}:{event_name}"),
            Some(turn_id.to_string()),
            goal,
        );
        Ok(())
    }

    pub async fn restore_after_resume(&self) -> Result<(), String> {
        if !self.is_enabled() {
            return Ok(());
        }

        let goal = self
            .inner
            .state_dbs
            .thread_goals()
            .get_thread_goal(self.thread_id())
            .await
            .map_err(|err| err.to_string())?;
        match goal {
            Some(goal) if goal.status == codex_state::ThreadGoalStatus::Active => {
                self.inner
                    .accounting_state
                    .mark_idle_goal_active(goal.goal_id);
                self.inner.metrics.record_resumed();
            }
            Some(_) | None => self.inner.accounting_state.clear_active_goal(),
        }
        Ok(())
    }


    pub fn goal_recovery_runtime_id(&self) -> &str {
        &self.inner.recovery_runtime_id
    }

    /// The owned worker retains the original Goal permit even if the RPC caller
    /// disconnects. Dropping a response never cancels/retries the admitted input.
    pub async fn recover_goal_with_receipt(
        &self,
        request: codex_protocol::goal_recovery::GoalRecoveryRequest,
    ) -> Result<codex_protocol::goal_recovery::GoalRecoveryReceipt, String> {
        let runtime = self.clone();
        tokio::spawn(async move { runtime.recover_goal_once(request).await })
            .await.map_err(|error| format!("Goal receipt worker UNKNOWN: {error}"))?
    }

    async fn recover_goal_once(
        &self,
        request: codex_protocol::goal_recovery::GoalRecoveryRequest,
    ) -> Result<codex_protocol::goal_recovery::GoalRecoveryReceipt, String> {
        let _permit = self.goal_state_permit().await?;
        let store = self.inner.state_dbs.thread_goals();
        if let Some(receipt) = store.read_goal_recovery(&request.thread_id, &request.attempt_id)
            .await.map_err(|error| error.to_string())? {
            if receipt.request != request { return Err("attempt token changed".to_string()); }
            return Ok(receipt);
        }
        if request.thread_id != self.thread_id().to_string()
            || request.expected_runtime_id != self.inner.recovery_runtime_id
            || !self.tools_available() {
            return Err("Goal recovery runtime identity/capability changed".to_string());
        }
        let manager = self.inner.thread_manager.upgrade().ok_or("thread manager unavailable")?;
        let thread = manager.get_thread(self.inner.thread_id).await.map_err(|error| error.to_string())?;
        let (receipt, created) = store.begin_goal_recovery(&request).await.map_err(|error| error.to_string())?;
        if !created { return Ok(receipt); }
        let goal = store.get_thread_goal(self.thread_id()).await.map_err(|error| error.to_string())?
            .ok_or("Goal disappeared after durable intent; UNKNOWN")?;
        let start_options = thread.thread_extension_data().get::<TurnStartOptions>()
            .map(|options| options.as_ref().clone()).unwrap_or_default();
        let item = continuation_steering_item(&protocol_goal_from_state(goal), thread.config().await.update_plan_enabled);
        let mut input = TurnInputRequest::new(TurnInput::ResponseItem(item)).on_start(TurnStartOptions {
            turn_trigger: Some("goal".to_string()), ..start_options
        });
        input.goal_recovery = Some(request.clone());
        // The native Goal stop path takes the same permit, so even a fast task
        // cannot finish accounting before this single admission is identified.
        match thread.start_turn_if_idle(input).await {
            Ok(StartIfIdleSubmission::Started { turn_id, .. }) => {
                self.inner.accounting_state.reset_empty_responses();
                self.inner.accounting_state.mark_goal_continuation(turn_id.clone());
                // Preserve normal Goal UI/rollout projection. This event is not
                // the durable receipt; the native store remains authoritative.
                if let Ok(Some(goal)) = store.get_thread_goal(self.thread_id()).await {
                    self.inner.event_emitter.thread_goal_updated(
                        format!("goal-recovery:{}", request.attempt_id),
                        Some(turn_id), protocol_goal_from_state(goal));
                }
            }
            Ok(StartIfIdleSubmission::NotSubmitted { reason }) => {
                store.reject_goal_recovery(&request, &format!("{reason:?}"))
                    .await.map_err(|error| format!("Goal rejection persistence UNKNOWN: {error}"))?;
            }
            Err(error) => {
                // Persistence/admission may have committed. Never convert this
                // into Rejected, and never submit another turn under this token.
                tracing::warn!(%error, "Goal receipt admission outcome UNKNOWN; read original token");
            }
        }
        store.read_goal_recovery(&request.thread_id, &request.attempt_id)
            .await.map_err(|error| error.to_string())?.ok_or("durable Goal intent disappeared".to_string())
    }

    pub(crate) async fn continue_if_idle(&self) -> Result<(), String> {
        if !self.tools_available() {
            self.inner.accounting_state.clear_active_goal();
            return Ok(());
        }
        // Hold this through the read/start window so external set/clear cannot
        // change the goal after we read it but before the continuation launches.
        let _goal_state_permit = self.goal_state_permit().await?;

        // A release with a missing durable acknowledgement cannot silently
        // become another automatic Goal turn. Ordinary explicit input remains usable.
        if self.inner.state_dbs.thread_goals().has_goal_recovery_fence(&self.thread_id().to_string())
            .await.map_err(|error| error.to_string())? {
            return Ok(());
        }
        if self
            .inner
            .state_dbs
            .thread_goals()
            .has_thread_goal_continuation_deferral(self.thread_id())
            .await
            .map_err(|err| err.to_string())?
        {
            return Ok(());
        }

        let Some(thread_manager) = self.inner.thread_manager.upgrade() else {
            tracing::debug!("skipping goal continuation because thread manager is unavailable");
            return Ok(());
        };
        let Ok(thread) = thread_manager.get_thread(self.inner.thread_id).await else {
            tracing::debug!("skipping goal continuation because live thread is unavailable");
            return Ok(());
        };

        let Some(goal) = self
            .inner
            .state_dbs
            .thread_goals()
            .get_thread_goal(self.thread_id())
            .await
            .map_err(|err| err.to_string())?
        else {
            self.inner.accounting_state.clear_active_goal();
            return Ok(());
        };
        if goal.status != codex_state::ThreadGoalStatus::Active {
            self.inner.accounting_state.clear_active_goal();
            return Ok(());
        }
        let start_options = thread
            .thread_extension_data()
            .get::<TurnStartOptions>()
            .map(|options| options.as_ref().clone())
            .unwrap_or_default();
        let item = continuation_steering_item(
            &protocol_goal_from_state(goal),
            thread.config().await.update_plan_enabled,
        );

        match thread
            .start_turn_if_idle(
                TurnInputRequest::new(TurnInput::ResponseItem(item)).on_start(TurnStartOptions {
                    turn_trigger: Some("goal".to_string()),
                    ..start_options
                }),
            )
            .await
        {
            Ok(StartIfIdleSubmission::Started { turn_id, .. }) => {
                // Turn-stop evaluation takes the same permit, so even a fast response
                // cannot finish before this host-admitted continuation is identified.
                self.inner.accounting_state.mark_goal_continuation(turn_id);
            }
            Ok(StartIfIdleSubmission::NotSubmitted { reason }) => {
                tracing::debug!(
                    ?reason,
                    "skipping goal continuation because automatic idle work was rejected"
                );
            }
            Err(error) => {
                tracing::debug!(
                    %error,
                    "skipping goal continuation because turn input submission failed"
                );
            }
        }

        let current_turn_is_goal_active = self
            .inner
            .accounting_state
            .current_turn_id()
            .or_else(|| self.inner.accounting_state.interrupted_turn_id())
            .is_some_and(|turn_id| {
                self.inner
                    .accounting_state
                    .current_active_goal_id_for_turn(turn_id.as_str())
                    .is_some()
            });
        if !current_turn_is_goal_active {
            self.inner
                .accounting_state
                .reset_idle_progress_baseline_and_clear_active_goal();
        }
        Ok(())
    }

    pub(crate) async fn inject_active_turn_steering(&self, item: ResponseItem) {
        let Some(thread_manager) = self.inner.thread_manager.upgrade() else {
            tracing::debug!("skipping goal steering because thread manager is unavailable");
            return;
        };
        let Ok(thread) = thread_manager.get_thread(self.inner.thread_id).await else {
            tracing::debug!("skipping goal steering because live thread is unavailable");
            return;
        };
        if thread.inject_if_running(vec![item]).await.is_err() {
            tracing::debug!("skipping goal steering because no turn is active");
        }
    }

    pub(crate) async fn account_active_goal_progress(
        &self,
        turn_id: &str,
        event_id: &str,
        mode: codex_state::GoalAccountingMode,
        budget_limited_goal_disposition: BudgetLimitedGoalDisposition,
    ) -> Result<Option<AccountedGoalProgress>, String> {
        let accounting = self.accounting_state();
        let _accounting_permit = accounting
            .progress_accounting_permit()
            .await
            .map_err(|err| err.to_string())?;
        self.account_active_goal_progress_reserved(
            turn_id,
            event_id,
            mode,
            budget_limited_goal_disposition,
        )
        .await
    }

    async fn account_active_goal_progress_reserved(
        &self,
        turn_id: &str,
        event_id: &str,
        mode: codex_state::GoalAccountingMode,
        budget_limited_goal_disposition: BudgetLimitedGoalDisposition,
    ) -> Result<Option<AccountedGoalProgress>, String> {
        let accounting = self.accounting_state();
        let Some(snapshot) = accounting.progress_snapshot(turn_id) else {
            return Ok(None);
        };
        let previous_status = self
            .current_goal_status_for_metrics(Some(snapshot.expected_goal_id.as_str()))
            .await?;
        let outcome = self
            .inner
            .state_dbs
            .thread_goals()
            .account_thread_goal_usage(
                self.thread_id(),
                snapshot.time_delta_seconds,
                snapshot.token_delta,
                mode,
                Some(snapshot.expected_goal_id.as_str()),
            )
            .await
            .map_err(|err| err.to_string())?;
        // The test pauses after the real SQLite commit and before the original
        // in-memory watermark. No hook is compiled into production.
        #[cfg(test)]
        {
            let pause = self.inner.recovery_post_commit.lock().unwrap().take();
            if let Some(pause) = pause {
                pause.committed.notify_one();
                pause.resume.notified().await;
            }
        }
        Ok(match outcome {
            codex_state::GoalAccountingOutcome::Updated(goal) => {
                let goal_id = goal.goal_id.clone();
                self.inner
                    .metrics
                    .record_terminal_if_status_changed(previous_status, &goal);
                self.inner
                    .analytics
                    .usage_accounted(&goal, GoalEventAttribution::Turn(turn_id));
                self.inner.analytics.status_changed(
                    &goal,
                    previous_status,
                    GoalEventAttribution::Turn(turn_id),
                );
                accounting.mark_progress_accounted_for_status(
                    turn_id,
                    &snapshot,
                    goal.status,
                    budget_limited_goal_disposition,
                );
                let goal = protocol_goal_from_state(goal);
                self.inner.event_emitter.thread_goal_updated(
                    event_id.to_string(),
                    Some(turn_id.to_string()),
                    goal.clone(),
                );
                Some(AccountedGoalProgress { goal, goal_id })
            }
            codex_state::GoalAccountingOutcome::Unchanged(_) => None,
        })
    }

    async fn account_idle_goal_progress(
        &self,
        event_id: &str,
        mode: codex_state::GoalAccountingMode,
        budget_limited_goal_disposition: BudgetLimitedGoalDisposition,
    ) -> Result<Option<AccountedGoalProgress>, String> {
        let accounting = self.accounting_state();
        let _accounting_permit = accounting
            .progress_accounting_permit()
            .await
            .map_err(|err| err.to_string())?;
        let Some(snapshot) = accounting.idle_progress_snapshot() else {
            return Ok(None);
        };
        let previous_status = self
            .current_goal_status_for_metrics(Some(snapshot.expected_goal_id.as_str()))
            .await?;
        let outcome = self
            .inner
            .state_dbs
            .thread_goals()
            .account_thread_goal_usage(
                self.thread_id(),
                snapshot.time_delta_seconds,
                snapshot.token_delta,
                mode,
                Some(snapshot.expected_goal_id.as_str()),
            )
            .await
            .map_err(|err| err.to_string())?;
        Ok(match outcome {
            codex_state::GoalAccountingOutcome::Updated(goal) => {
                let goal_id = goal.goal_id.clone();
                self.inner
                    .metrics
                    .record_terminal_if_status_changed(previous_status, &goal);
                self.inner
                    .analytics
                    .usage_accounted(&goal, GoalEventAttribution::NoTurn);
                self.inner.analytics.status_changed(
                    &goal,
                    previous_status,
                    GoalEventAttribution::NoTurn,
                );
                accounting.mark_idle_progress_accounted_for_status(
                    &snapshot,
                    goal.status,
                    budget_limited_goal_disposition,
                );
                let goal = protocol_goal_from_state(goal);
                self.inner.event_emitter.thread_goal_updated(
                    event_id.to_string(),
                    /*turn_id*/ None,
                    goal.clone(),
                );
                Some(AccountedGoalProgress { goal, goal_id })
            }
            codex_state::GoalAccountingOutcome::Unchanged(_) => {
                accounting.reset_idle_progress_baseline_and_clear_active_goal();
                None
            }
        })
    }

    async fn current_goal_status_for_metrics(
        &self,
        expected_goal_id: Option<&str>,
    ) -> Result<Option<codex_state::ThreadGoalStatus>, String> {
        let goal = self
            .inner
            .state_dbs
            .thread_goals()
            .get_thread_goal(self.thread_id())
            .await
            .map_err(|err| err.to_string())?;
        Ok(goal.and_then(|goal| {
            expected_goal_id
                .is_none_or(|expected_goal_id| goal.goal_id == expected_goal_id)
                .then_some(goal.status)
        }))
    }
}

fn compaction_goal_value(goal: Option<codex_state::ThreadGoal>) -> serde_json::Value {
    match goal {
        None => serde_json::Value::Null,
        Some(goal) => serde_json::json!({
            "threadId": goal.thread_id.to_string(), "goalId": goal.goal_id,
            "objective": goal.objective, "status": goal.status.as_str(),
            "tokenBudget": goal.token_budget, "tokensUsed": goal.tokens_used,
            "timeUsedSeconds": goal.time_used_seconds,
            "createdAtMs": goal.created_at.timestamp_millis(),
            "updatedAtMs": goal.updated_at.timestamp_millis(),
        }),
    }
}
fn validate_compaction_goal(
    actual: &serde_json::Value,
    expected: &serde_json::Value,
) -> Result<(), String> {
    if actual != expected {
        return Err("original Goal revision or usage changed".into());
    }
    if actual
        .get("status")
        .and_then(serde_json::Value::as_str)
        .is_some_and(|status| status != "active")
    {
        return Err("original Goal is not active".into());
    }
    Ok(())
}

/// Native-only lease. Neither JSON nor a caller boolean can construct it.
pub struct CompactionGoalReservation {
    runtime: GoalRuntimeHandle,
    _goal: tokio::sync::OwnedSemaphorePermit,
    _progress: tokio::sync::OwnedSemaphorePermit,
    expected: serde_json::Value,
    settled: std::sync::Mutex<Option<serde_json::Value>>,
}
impl CompactionGoalReservation {
    pub async fn settle_original_usage(
        self: &Arc<Self>,
        turn_id: &str,
        attempt_id: &str,
    ) -> Result<serde_json::Value, String> {
        {
            let mut settled = self
                .settled
                .lock()
                .map_err(|_| "native Goal reservation poisoned")?;
            if settled.is_some() {
                return Err("original Goal settlement already consumed".into());
            }
            *settled = Some(serde_json::Value::Null);
        }
        let reservation = Arc::clone(self);
        let turn_id = turn_id.to_owned();
        let attempt_id = attempt_id.to_owned();
        // Cancellation of the native controller wait must not cancel between
        // SQLite's additive commit and the original accounting watermark.
        // Dropping this JoinHandle DETACHES the task. It owns ONLY Goal/progress
        // permits, never a native client SH lock or provider dispatch authority.
        // Ordinary accounting waits for its exact original progress permit.
        tokio::spawn(async move {
            reservation
                .settle_original_usage_owned(&turn_id, &attempt_id)
                .await
        })
        .await
        .map_err(|error| format!("original Goal accounting task failed UNKNOWN: {error}"))?
    }

    async fn settle_original_usage_owned(
        &self,
        turn_id: &str,
        attempt_id: &str,
    ) -> Result<serde_json::Value, String> {
        let before = compaction_goal_value(
            self.runtime
                .inner
                .state_dbs
                .thread_goals()
                .get_thread_goal(self.runtime.thread_id())
                .await
                .map_err(|e| e.to_string())?,
        );
        validate_compaction_goal(&before, &self.expected)?;
        self.runtime
            .account_active_goal_progress_reserved(
                turn_id,
                &format!("{turn_id}:compact-recovery:{attempt_id}"),
                codex_state::GoalAccountingMode::ActiveOnly,
                BudgetLimitedGoalDisposition::ClearActive,
            )
            .await?;
        let after = compaction_goal_value(
            self.runtime
                .inner
                .state_dbs
                .thread_goals()
                .get_thread_goal(self.runtime.thread_id())
                .await
                .map_err(|e| e.to_string())?,
        );
        if before.is_null() != after.is_null() {
            return Err("original Goal appeared or disappeared".into());
        }
        if !before.is_null() {
            for key in [
                "threadId",
                "goalId",
                "objective",
                "tokenBudget",
                "createdAtMs",
            ] {
                if before[key] != after[key] {
                    return Err("external Goal identity changed under native reservation".into());
                }
            }
            if after["status"] != "active" {
                return Err("original Goal budget or status no longer permits replacement".into());
            }
            for key in ["tokensUsed", "timeUsedSeconds"] {
                if after[key].as_i64() < before[key].as_i64() {
                    return Err("original Goal usage regressed".into());
                }
            }
        }
        *self
            .settled
            .lock()
            .map_err(|_| "native Goal reservation poisoned")? = Some(after.clone());
        Ok(after)
    }
}

#[cfg(test)]
#[derive(Default)]
struct RecoveryPostCommitPause {
    committed: tokio::sync::Notify,
    resume: tokio::sync::Notify,
}

#[cfg(test)]
#[path = "compaction_recovery_tests.rs"]
mod compaction_recovery_tests;
