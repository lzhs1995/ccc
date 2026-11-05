//! Native foreground authority is committed by the App, never by operation routing.

use super::App;
use crate::backend_id::BackendId;
use codex_protocol::ThreadId;
use std::path::Path;

#[cfg(unix)]
use ccc_native_client_foreground::ForegroundObserver;

#[derive(Clone, Copy)]
struct NativeSelection {
    thread_id: ThreadId,
    direct_input_allowed: bool,
}

/// Covers startup returns before App construction and every later error return.
pub(super) struct StartupForegroundGuard {
    #[cfg(unix)]
    observer: ForegroundObserver,
    enabled: bool,
    initialization_error: Option<String>,
}

impl StartupForegroundGuard {
    pub(super) fn from_env(home: &Path) -> Self {
        let requested = std::env::var_os("CODEX_CLIENT_THREAD_OBSERVER").as_deref()
            == Some(std::ffi::OsStr::new("1"));
        #[cfg(unix)]
        {
            Self::from_result(requested, ForegroundObserver::from_env(home))
        }
        #[cfg(not(unix))]
        {
            let _ = home;
            Self {
                enabled: false,
                initialization_error: requested.then(|| {
                    "native foreground observation is unavailable on this platform".into()
                }),
            }
        }
    }

    #[cfg(unix)]
    pub(super) fn from_result(
        requested: bool,
        observer: std::io::Result<ForegroundObserver>,
    ) -> Self {
        match observer {
            Ok(observer) => Self {
                observer,
                enabled: requested,
                initialization_error: None,
            },
            Err(error) => Self {
                observer: ForegroundObserver::disabled(),
                enabled: false,
                initialization_error: Some(error.to_string()),
            },
        }
    }

    pub(super) fn initialization_warning(&self) -> Option<String> {
        self.initialization_error.as_ref().map(|error| {
            format!("Automatic continuation is unavailable: {error}. Your session remains open.")
        })
    }

    pub(super) fn app_owned(&self) -> NativeForeground {
        NativeForeground {
            #[cfg(unix)]
            observer: self.observer.clone(),
            enabled: self.enabled,
            changing: true,
            ..Default::default()
        }
    }
}

impl Drop for StartupForegroundGuard {
    fn drop(&mut self) {
        #[cfg(unix)]
        if self.enabled {
            let _ = self.observer.revoke();
        }
    }
}

/// A proposal is produced only by completed native display flows. The library
/// owns the independently committed SID used by the one-second heartbeat.
pub(super) struct NativeForeground {
    #[cfg(unix)]
    observer: ForegroundObserver,
    enabled: bool,
    selection: Option<NativeSelection>,
    connected_owner: Option<BackendId>,
    connection_published: bool,
    changing: bool,
    failure_notice: Option<String>,
    failure_notice_shown: bool,
    #[cfg(unix)]
    control: Option<ccc_native_client_foreground::compaction_socket::NativeControlServer>,
    control_attempted: bool,
}

impl Default for NativeForeground {
    fn default() -> Self {
        Self {
            #[cfg(unix)]
            observer: ForegroundObserver::disabled(),
            enabled: false,
            selection: None,
            connected_owner: None,
            connection_published: false,
            changing: false,
            failure_notice: None,
            failure_notice_shown: false,
            #[cfg(unix)]
            control: None,
            control_attempted: false,
        }
    }
}

impl NativeForeground {
    pub(super) fn heartbeat_enabled(&self) -> bool {
        self.enabled
    }

    fn note_failure(&mut self, error: std::io::Error) {
        self.connection_published = false;
        if !self.failure_notice_shown {
            self.failure_notice = Some(format!(
                "Automatic continuation is unavailable: {error}. Your session remains open."
            ));
        }
    }

    #[cfg(all(unix, test))]
    pub(super) fn open_enabled(home: &Path) -> std::io::Result<Self> {
        Ok(Self {
            observer: ForegroundObserver::open_enabled(home)?,
            enabled: true,
            changing: true,
            ..Default::default()
        })
    }
}

impl App {
    /// Resolve the event first. This gate must never surround an idle select!.
    /// A refused withdrawal leaves the selected UI untouched until its last
    /// positive monotonic lease has drained; normal work can then proceed with
    /// observation suspended. No error here exits the user's TUI.
    pub(super) async fn before_foreground_mutation(&mut self, reason: &str) {
        #[cfg(unix)]
        if self.foreground.enabled {
            let mut withdrawal = self.foreground.observer.before_change(reason);
            while withdrawal
                .as_ref()
                .is_err_and(|error| error.kind() == std::io::ErrorKind::WouldBlock)
            {
                // The original Core reservation owns a bounded drain/dispatch.
                // Never bypass this lock based on the observer lease timeout.
                self.native_foreground_heartbeat();
                tokio::time::sleep(std::time::Duration::from_millis(20)).await;
                withdrawal = self.foreground.observer.before_change(reason);
            }
            if withdrawal.is_err() {
                self.foreground.connection_published = false;
                let remaining = self.foreground.observer.remaining_lease();
                if !remaining.is_zero() {
                    tokio::time::sleep(remaining).await;
                }
                if let Err(retry_error) = self.foreground.observer.before_change(reason) {
                    self.foreground.note_failure(retry_error);
                }
            }
        }
        #[cfg(not(unix))]
        let _ = reason;
        self.foreground.changing = true;
    }

    /// Event drains can run after the heartbeat or a presentation-only tick.
    /// Call this only once an event or receiver closure has actually resolved.
    pub(super) async fn before_foreground_event_drain(&mut self) {
        if !self.foreground.changing {
            self.before_foreground_mutation("active_event_drain").await;
        }
    }

    pub(super) fn native_foreground_connected(&mut self, owner: BackendId) {
        self.foreground.connected_owner = Some(owner);
        self.foreground.connection_published = false;
        #[cfg(unix)]
        if self.foreground.enabled {
            match self.foreground.observer.connected() {
                Ok(()) => self.foreground.connection_published = true,
                Err(error) => self.foreground.note_failure(error),
            }
        }
    }

    /// Transport loss is an external fact, so stop renewal before updating UI
    /// eligibility even when a null record cannot be stored.
    pub(super) fn native_foreground_disconnected(&mut self) {
        self.foreground.connected_owner = None;
        self.foreground.connection_published = false;
        #[cfg(unix)]
        if self.foreground.enabled
            && let Err(error) = self.foreground.observer.disconnected()
        {
            self.foreground.note_failure(error);
        }
    }

    pub(super) fn forget_native_foreground_selection(&mut self) {
        self.foreground.selection = None;
    }

    pub(super) fn propose_native_foreground_selection(
        &mut self,
        thread_id: ThreadId,
        direct_input_allowed: bool,
    ) {
        self.foreground.selection = Some(NativeSelection {
            thread_id,
            direct_input_allowed,
        });
    }

    pub(super) fn native_foreground_selection_allows_direct_input(
        &self,
        thread_id: ThreadId,
    ) -> bool {
        self.foreground.selection.is_some_and(|selection| {
            selection.thread_id == thread_id && selection.direct_input_allowed
        })
    }

    pub(super) fn native_foreground_has_barrier(
        &self,
        queue_empty: bool,
        startup_ready: bool,
    ) -> bool {
        !queue_empty
            || !startup_ready
            || self.pending_startup_thread_start
            || self.pending_thread_switch_resets != 0
            || self.initial_history_replay_buffer.is_some()
            || self.backtrack_render_pending
            || !self.pending_primary_events.is_empty()
            || self.pending_open_resume_picker
            || self.pending_working_directory_change.is_some()
            || self.pending_start_managed_worktree.is_some()
            || self.pending_managed_worktree_creation
            || self.pending_managed_worktree_created.is_some()
            || self.pending_managed_worktree_transition.is_some()
            || self.pending_managed_worktree_attach.is_some()
            || self.startup_pending_protected_request
            || self.has_queued_startup_protected_request()
            || self.chat_widget.has_pending_protected_request()
            || self.chat_widget.has_active_view()
    }

    /// Called after the whole mutable step, including deferred early-continue
    /// paths, transcript replay/reset processing and startup-input restoration.
    pub(super) fn native_foreground_checkpoint(
        &mut self,
        owner: BackendId,
        queue_empty: bool,
        startup_ready: bool,
    ) {
        if !self.foreground.changing {
            return;
        }
        let thread = self.foreground.selection.filter(|selection| {
            selection.direct_input_allowed
                && self.foreground.connected_owner == Some(owner)
                && !self.reconnect.offline
                && !self.reconnect.failed
                && !self.native_foreground_has_barrier(queue_empty, startup_ready)
                && self.active_thread_id == Some(selection.thread_id)
                && self.chat_widget.thread_id() == Some(selection.thread_id)
                && self
                    .active_thread_rx
                    .as_ref()
                    .is_some_and(|rx| rx.is_empty())
                && self
                    .thread_event_channels
                    .get(&selection.thread_id)
                    .is_some_and(|channel| {
                        channel.attachment() == super::ThreadEventAttachment::Live
                    })
                && !self.thread_unavailable(selection.thread_id)
                && !self.agent_navigation.is_parent_owned(selection.thread_id)
                && !self
                    .agent_navigation
                    .get(&selection.thread_id)
                    .is_some_and(|entry| entry.is_closed)
                && self.chat_widget.accepts_native_direct_input()
                && !self.chat_widget.is_external_writer_view()
        });
        #[cfg(unix)]
        if self.foreground.enabled && !self.foreground.observer.is_suspended() {
            if thread.is_some() && !self.foreground.connection_published {
                self.native_foreground_connected(owner);
            }
            if !self.foreground.observer.is_suspended() {
                let thread = thread.map(|selection| selection.thread_id.to_string());
                if let Err(error) = self.foreground.observer.committed(thread.as_deref()) {
                    self.foreground.note_failure(error);
                }
            }
        }
        #[cfg(not(unix))]
        let _ = thread;
        self.foreground.changing = false;
        self.ensure_native_compaction_control();
        self.show_native_foreground_failure();
    }

    /// The heartbeat deliberately has no access to active/widget routing IDs.
    pub(super) fn native_foreground_heartbeat(&mut self) {
        #[cfg(unix)]
        if self.foreground.enabled
            && let Err(error) = self.foreground.observer.heartbeat()
        {
            self.foreground.note_failure(error);
        }
        self.show_native_foreground_failure();
    }

    fn show_native_foreground_failure(&mut self) {
        if let Some(message) = self.foreground.failure_notice.take() {
            self.foreground.failure_notice_shown = true;
            self.chat_widget.add_error_message(message);
        }
    }

    pub(super) async fn withdraw_native_foreground_for_teardown(&mut self) {
        self.before_foreground_mutation("teardown").await;
        self.forget_native_foreground_selection();
        self.native_foreground_disconnected();
    }
}

impl App {
    pub(super) fn ensure_native_compaction_control(&mut self) {
        #[cfg(unix)]
        if self.foreground.enabled
            && !self.foreground.control_attempted
            && self.foreground.observer.compaction_selection().is_ok()
            && self
                .config
                .codex_home
                .join("credential-observations/compaction-recovery/enabled-v1")
                .exists()
        {
            self.foreground.control_attempted = true;
            let epoch = match self.foreground.observer.compaction_client_epoch() {
                Ok(epoch) => epoch,
                Err(_) => return,
            };
            let tx = self.app_event_tx.clone();
            match ccc_native_client_foreground::compaction_socket::NativeControlServer::open(
                &self.config.codex_home,
                &epoch,
                self.foreground.observer.clone(),
                move |request| {
                    tx.send(crate::app_event::AppEvent::NativeCompactionControl(request));
                },
            ) {
                Ok(control) => self.foreground.control = Some(control),
                Err(error) => self
                    .chat_widget
                    .add_error_message(format!("Native compaction control unavailable: {error}")),
            }
        }
    }

    pub(super) fn native_compaction_backend_matches(&self, owner: BackendId) -> bool {
        self.foreground.connected_owner == Some(owner)
            && self.foreground.connection_published
            && !self.foreground.changing
    }

    #[cfg(unix)]
    pub(super) fn native_compaction_observer(
        &self,
    ) -> &ccc_native_client_foreground::ForegroundObserver {
        &self.foreground.observer
    }
}
