//! Retains queue-only mail while local sessions are unloaded.
//! Eviction transfers mail before releasing the recipient's residency guard.

use crate::PendingAgentMessage;
use crate::agent::api::CompactionMailboxReservation;
use crate::agent_communication::PENDING_MAILBOX_MESSAGES;
use codex_diagnostics::GaugeGuard;
use codex_protocol::ThreadId;
use codex_protocol::error::CodexErr;
use codex_protocol::error::Result;
use std::collections::HashMap;
use std::sync::Arc;
use std::sync::Mutex;
use std::sync::PoisonError;
use tokio::sync::watch;

pub(super) struct Mailboxes(Arc<Mutex<Option<HashMap<ThreadId, Mailbox>>>>);

impl Default for Mailboxes {
    fn default() -> Self {
        Self(Arc::new(Mutex::new(Some(HashMap::new()))))
    }
}

#[derive(Default)]
struct Mailbox {
    pending: Vec<StoredMail>,
    state: watch::Sender<bool>,
    delivery_gate: Arc<tokio::sync::Mutex<()>>,
}

struct ReservedMailbox {
    all: Arc<Mutex<Option<HashMap<ThreadId, Mailbox>>>>,
    agent: ThreadId,
    gate: Arc<tokio::sync::Mutex<()>>,
    _guard: tokio::sync::OwnedMutexGuard<()>,
}
impl CompactionMailboxReservation for ReservedMailbox {
    fn validate_current(&self) -> Result<()> {
        let all = self.all.lock().unwrap_or_else(PoisonError::into_inner);
        let mailbox = all
            .as_ref()
            .and_then(|all| all.get(&self.agent))
            .ok_or_else(|| CodexErr::InvalidRequest("original mailbox epoch closed".into()))?;
        if !Arc::ptr_eq(&self.gate, &mailbox.delivery_gate) || !mailbox.pending.is_empty() {
            return Err(CodexErr::InvalidRequest(
                "original mailbox epoch or input changed".into(),
            ));
        }
        Ok(())
    }
}

struct StoredMail {
    id: Option<String>,
    communication: PendingAgentMessage,
    _diagnostics_guard: GaugeGuard,
}

impl Mailboxes {
    /// This is the actual enqueue gate. Messages that arrive after this
    /// reservation remain owned by their original enqueue future; they are not
    /// inserted (and are never falsely reported absent) until dispatch releases.
    pub(super) fn reserve_empty(
        &self,
        agent: ThreadId,
    ) -> Result<Arc<dyn CompactionMailboxReservation>> {
        let mut all = self.0.lock().unwrap_or_else(PoisonError::into_inner);
        let mailbox = all
            .as_mut()
            .ok_or_else(|| CodexErr::InvalidRequest("agent runtime is shutting down".into()))?
            .entry(agent)
            .or_default();
        let guard = mailbox
            .delivery_gate
            .clone()
            .try_lock_owned()
            .map_err(|_| {
                CodexErr::InvalidRequest("original mailbox delivery already started".into())
            })?;
        if !mailbox.pending.is_empty() {
            return Err(CodexErr::InvalidRequest(
                "original controller mailbox input pending".into(),
            ));
        }
        Ok(Arc::new(ReservedMailbox {
            all: self.0.clone(),
            agent,
            gate: mailbox.delivery_gate.clone(),
            _guard: guard,
        }))
    }

    pub(super) async fn enqueue(
        &self,
        agent: ThreadId,
        id: Option<String>,
        messages: Vec<PendingAgentMessage>,
    ) -> Result<()> {
        let gate = {
            let mut all = self.0.lock().unwrap_or_else(PoisonError::into_inner);
            all.as_mut()
                .ok_or_else(|| CodexErr::InvalidRequest("agent runtime is shutting down".into()))?
                .entry(agent)
                .or_default()
                .delivery_gate
                .clone()
        };
        let _delivery = gate.lock().await;
        let mut mailboxes = self.0.lock().unwrap_or_else(PoisonError::into_inner);
        let mailbox = mailboxes
            .as_mut()
            .ok_or_else(|| CodexErr::InvalidRequest("agent runtime is shutting down".into()))?
            .get_mut(&agent)
            .filter(|mailbox| Arc::ptr_eq(&gate, &mailbox.delivery_gate))
            .ok_or_else(|| {
                CodexErr::InvalidRequest("original mailbox epoch removed or replaced".into())
            })?;
        mailbox
            .pending
            .extend(messages.into_iter().map(|communication| StoredMail {
                id: id.clone(),
                communication,
                _diagnostics_guard: PENDING_MAILBOX_MESSAGES.track(),
            }));
        mailbox.state.send_replace(!mailbox.pending.is_empty());
        Ok(())
    }

    pub(super) fn take(&self, agent: ThreadId) -> Vec<PendingAgentMessage> {
        let mut mailboxes = self.0.lock().unwrap_or_else(PoisonError::into_inner);
        let Some(mailbox) = mailboxes
            .as_mut()
            .and_then(|mailboxes| mailboxes.get_mut(&agent))
        else {
            return Vec::new();
        };
        let pending = std::mem::take(&mut mailbox.pending);
        mailbox.state.send_if_modified(std::mem::take);
        pending
            .into_iter()
            .map(|mail| {
                // Mail transferred during eviction was already received by the old session.
                if let Some(id) = mail.id {
                    crate::agent_communication::emit_agent_communication_receive(&id);
                }
                mail.communication
            })
            .collect()
    }

    pub(super) fn watch(&self, agent: ThreadId) -> watch::Receiver<bool> {
        let mut mailboxes = self.0.lock().unwrap_or_else(PoisonError::into_inner);
        match mailboxes.as_mut() {
            Some(mailboxes) => {
                // Private delegates and cancelled startups can stop without explicit removal.
                // Retain unread mail and live subscriptions, but reclaim abandoned empty entries.
                mailboxes.retain(|_, mailbox| {
                    Arc::strong_count(&mailbox.delivery_gate) > 1
                        || !mailbox.pending.is_empty()
                        || mailbox.state.receiver_count() != 0
                });
                mailboxes.entry(agent).or_default().state.subscribe()
            }
            None => watch::channel(/*init*/ false).1,
        }
    }

    pub(super) async fn remove(&self, agent: ThreadId) {
        // Physical removal shares dispatch/enqueue's original gate. A remove
        // requested during a dispatch linearizes after that bounded lease.
        let gate = {
            let all = self.0.lock().unwrap_or_else(PoisonError::into_inner);
            all.as_ref()
                .and_then(|all| all.get(&agent))
                .map(|mailbox| mailbox.delivery_gate.clone())
        };
        let Some(gate) = gate else {
            return;
        };
        let _delivery = gate.lock().await;
        let removed = {
            let mut all = self.0.lock().unwrap_or_else(PoisonError::into_inner);
            all.as_mut().and_then(|all| {
                if all
                    .get(&agent)
                    .is_some_and(|mailbox| Arc::ptr_eq(&gate, &mailbox.delivery_gate))
                {
                    all.remove(&agent)
                } else {
                    None
                }
            })
        };
        // Pending extensions may run destructors; never run these under map lock.
        drop(removed);
    }

    pub(super) fn close(&self) {
        // Detach under the lock, then notify readers and drop pending mail outside it.
        let mailboxes = self.0.lock().unwrap_or_else(PoisonError::into_inner).take();
        if let Some(mailboxes) = mailboxes {
            for mailbox in mailboxes.values() {
                mailbox.state.send_replace(false);
            }
        }
    }
}

#[cfg(test)]
mod recovery_tests {
    use super::*;
    #[tokio::test]
    async fn original_mailbox_enqueue_linearizes_after_native_dispatch_guard_release() {
        let mailboxes = Arc::new(Mailboxes::default());
        let id = ThreadId::new();
        let watch = mailboxes.watch(id);
        let guard = mailboxes.reserve_empty(id).unwrap();
        assert!(mailboxes.reserve_empty(id).is_err());
        let other = mailboxes.clone();
        let pending = tokio::spawn(async move {
            let communication = codex_protocol::protocol::InterAgentCommunication::new(
                codex_protocol::AgentPath::root(),
                codex_protocol::AgentPath::root(),
                Vec::new(),
                "preserved original mail".into(),
                false,
            );
            other
                .enqueue(id, None, vec![communication.into()])
                .await
                .unwrap();
        });
        tokio::task::yield_now().await;
        assert!(!pending.is_finished());
        assert!(!*watch.borrow());
        assert!(mailboxes.take(id).is_empty());
        drop(guard);
        pending.await.unwrap();
        assert!(*watch.borrow());
        assert!(mailboxes.reserve_empty(id).is_err());
        assert_eq!(mailboxes.take(id).len(), 1);
        assert!(!*watch.borrow());
        assert!(mailboxes.reserve_empty(id).is_ok());
    }
    fn message() -> PendingAgentMessage {
        codex_protocol::protocol::InterAgentCommunication::new(
            codex_protocol::AgentPath::root(),
            codex_protocol::AgentPath::root(),
            Vec::new(),
            "original epoch mail".into(),
            false,
        )
        .into()
    }

    #[tokio::test]
    async fn remove_waits_for_original_dispatch_and_old_waiter_cannot_enter_recreated_mailbox() {
        let mailboxes = Mailboxes::default();
        let id = ThreadId::new();
        let old_watch = mailboxes.watch(id);
        let reservation = mailboxes.reserve_empty(id).unwrap();
        let mut remove = Box::pin(mailboxes.remove(id));
        assert!(futures::poll!(remove.as_mut()).is_pending());
        let mut old_enqueue = Box::pin(mailboxes.enqueue(id, None, vec![message()]));
        assert!(futures::poll!(old_enqueue.as_mut()).is_pending());
        reservation.validate_current().unwrap();
        assert!(!*old_watch.borrow());
        drop(reservation);
        remove.await;
        let new_watch = mailboxes.watch(id);
        let new_reservation = mailboxes.reserve_empty(id).unwrap();
        // The old future now owns the old gate. It must not use map.entry to
        // deliver into this new generation, whose distinct gate is held here.
        let error = old_enqueue.await.unwrap_err();
        assert!(error.to_string().contains("removed or replaced"));
        new_reservation.validate_current().unwrap();
        assert!(!*new_watch.borrow());
        assert!(mailboxes.take(id).is_empty());
        assert!(old_watch.has_changed().is_err());
    }

    #[tokio::test]
    async fn shutdown_revokes_original_reservation_and_waiting_enqueue_without_new_epoch() {
        let mailboxes = Mailboxes::default();
        let id = ThreadId::new();
        let reservation = mailboxes.reserve_empty(id).unwrap();
        let mut enqueue = Box::pin(mailboxes.enqueue(id, None, vec![message()]));
        assert!(futures::poll!(enqueue.as_mut()).is_pending());
        mailboxes.close();
        assert!(reservation.validate_current().is_err());
        assert!(mailboxes.reserve_empty(id).is_err());
        assert!(!*mailboxes.watch(id).borrow());
        drop(reservation);
        assert!(enqueue.await.is_err());
        assert!(mailboxes.take(id).is_empty());
    }
}
