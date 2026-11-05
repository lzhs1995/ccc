//! Native once dispatch fence, scoped only to an admitted recovery factory.
//! HTTP retries and WebSocket fallback share the same consumed slot. A failed
//! send or receipt remains UNKNOWN; no alternate transport receives a new grant.
use std::future::Future;
use std::sync::Arc;
use std::sync::atomic::AtomicBool;
use std::sync::atomic::Ordering;
use tokio_util::sync::CancellationToken;

pub trait CompactionDispatchAuthority: Send + Sync {
    fn remaining(&self) -> Result<std::time::Duration, String>;
    fn validate_fresh(&self) -> Result<(), String>;
    fn persist_send_intent(&self) -> Result<(), String>;
    fn persist_sent(&self) -> Result<(), String>;
}
pub struct CompactionDispatch {
    authority: Arc<dyn CompactionDispatchAuthority>,
    consumed: AtomicBool,
    revoked: AtomicBool,
    cancellation: CancellationToken,
    confirmed: CancellationToken,
}
impl CompactionDispatch {
    pub fn new(authority: Arc<dyn CompactionDispatchAuthority>) -> Arc<Self> {
        Arc::new(Self {
            authority,
            consumed: AtomicBool::new(false),
            revoked: AtomicBool::new(false),
            cancellation: CancellationToken::new(),
            confirmed: CancellationToken::new(),
        })
    }
    /// Called at the actual original transport immediately before its send.
    fn check_live(&self) -> Result<(), String> {
        if self.revoked.load(Ordering::Acquire) || self.authority.remaining()?.is_zero() {
            return Err("original dispatch owner ended or grant expired".into());
        }
        Ok(())
    }
    pub fn before_send(&self) -> Result<(), String> {
        self.check_live()?;
        self.authority.validate_fresh()?;
        self.check_live()?;
        self.consumed
            .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
            .map_err(|_| "original replacement send consumed; reconcile UNKNOWN".to_owned())?;
        self.authority.persist_send_intent()?;
        // Both native attestation and filesystem publication may block. A
        // cancellation during either operation must not become a late send.
        self.authority.validate_fresh()?;
        self.check_live()
    }
    pub fn sent(&self) -> Result<(), String> {
        if !self.consumed.load(Ordering::Acquire) {
            return Err("native send was not admitted".into());
        }
        self.authority.persist_sent()?;
        self.confirmed.cancel();
        Ok(())
    }
    pub fn revoke(&self) {
        self.revoked.store(true, Ordering::Release);
        self.cancellation.cancel();
    }
    pub async fn confirmed(&self) {
        self.confirmed.cancelled().await;
    }
}
tokio::task_local! { static CURRENT: Arc<CompactionDispatch>; }
pub async fn dispatch_cancelled(guard: &Option<Arc<CompactionDispatch>>) {
    match guard {
        Some(guard) => guard.cancellation.cancelled().await,
        None => std::future::pending().await,
    }
}
pub(crate) fn current() -> Option<Arc<CompactionDispatch>> {
    CURRENT.try_with(Arc::clone).ok()
}
pub async fn scope<F: Future>(guard: Arc<CompactionDispatch>, future: F) -> F::Output {
    CURRENT.scope(guard, future).await
}

pub struct CompactionDispatchOwner(pub Arc<CompactionDispatch>);
impl Drop for CompactionDispatchOwner {
    fn drop(&mut self) {
        self.0.revoke();
    }
}

/// Tracks the native response tasks created while the original factory runs.
/// Cancellation of a borrowed factory alone is not proof that its spawned
/// provider or mapper has exited. Leases are acquired before spawn and finish
/// only after the owned task has dropped its transport resources.
pub struct CompactionTransportScope {
    pub cancel: CancellationToken,
    tasks: std::sync::atomic::AtomicUsize,
    changed: tokio::sync::Notify,
}
impl CompactionTransportScope {
    pub fn new() -> Arc<Self> {
        Arc::new(Self {
            cancel: CancellationToken::new(),
            tasks: std::sync::atomic::AtomicUsize::new(0),
            changed: tokio::sync::Notify::new(),
        })
    }
    pub fn lease(self: &Arc<Self>) -> CompactionTransportLease {
        self.tasks.fetch_add(1, Ordering::AcqRel);
        CompactionTransportLease(self.clone())
    }
    pub async fn drain(&self) {
        loop {
            let notified = self.changed.notified();
            if self.tasks.load(Ordering::Acquire) == 0 {
                return;
            }
            notified.await;
        }
    }
}
pub struct CompactionTransportOwner(pub Arc<CompactionTransportScope>);
impl Drop for CompactionTransportOwner {
    fn drop(&mut self) {
        self.0.cancel.cancel();
    }
}

pub struct CompactionTransportLease(Arc<CompactionTransportScope>);
impl Drop for CompactionTransportLease {
    fn drop(&mut self) {
        if self.0.tasks.fetch_sub(1, Ordering::AcqRel) == 1 {
            self.0.changed.notify_one();
        }
    }
}
tokio::task_local! { static TRANSPORT: Arc<CompactionTransportScope>; }
pub fn transport_scope() -> Option<Arc<CompactionTransportScope>> {
    TRANSPORT.try_with(Arc::clone).ok()
}
pub async fn track_transport<F: Future>(
    scope: Arc<CompactionTransportScope>,
    future: F,
) -> F::Output {
    TRANSPORT.scope(scope, future).await
}
pub async fn cancelled(scope: &Option<Arc<CompactionTransportScope>>) {
    match scope {
        Some(scope) => scope.cancel.cancelled().await,
        None => std::future::pending().await,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::AtomicUsize;
    struct Authority {
        fresh: AtomicBool,
        intents: AtomicUsize,
        receipts: AtomicUsize,
        fail_receipt: bool,
    }
    impl CompactionDispatchAuthority for Authority {
        fn remaining(&self) -> Result<std::time::Duration, String> {
            Ok(std::time::Duration::from_secs(15))
        }
        fn validate_fresh(&self) -> Result<(), String> {
            if self.fresh.load(Ordering::Acquire) {
                Ok(())
            } else {
                Err("expired".into())
            }
        }
        fn persist_send_intent(&self) -> Result<(), String> {
            self.intents.fetch_add(1, Ordering::AcqRel);
            Ok(())
        }
        fn persist_sent(&self) -> Result<(), String> {
            if self.fail_receipt {
                return Err("I/O unknown".into());
            }
            self.receipts.fetch_add(1, Ordering::AcqRel);
            Ok(())
        }
    }
    fn authority(fail_receipt: bool) -> Arc<Authority> {
        Arc::new(Authority {
            fresh: AtomicBool::new(true),
            intents: AtomicUsize::new(0),
            receipts: AtomicUsize::new(0),
            fail_receipt,
        })
    }
    #[test]
    fn expired_at_real_send_does_not_consume_or_emit_intent() {
        let a = authority(false);
        let d = CompactionDispatch::new(a.clone());
        a.fresh.store(false, Ordering::Release);
        assert!(d.before_send().is_err());
        assert_eq!(a.intents.load(Ordering::Acquire), 0);
    }
    #[test]
    fn owner_drop_refuses_queued_websocket_command() {
        let a = authority(false);
        let d = CompactionDispatch::new(a.clone());
        drop(CompactionDispatchOwner(d.clone()));
        assert!(d.before_send().is_err());
        assert_eq!(a.intents.load(Ordering::Acquire), 0);
    }
    #[test]
    fn send_failure_consumes_same_attempt_across_http_websocket_fallback() {
        let a = authority(false);
        let d = CompactionDispatch::new(a.clone());
        d.before_send().unwrap();
        assert!(d.before_send().is_err());
        assert_eq!(a.intents.load(Ordering::Acquire), 1);
        assert!(!d.confirmed.is_cancelled());
    }
    #[test]
    fn receipt_failure_never_confirms_or_permits_retry() {
        let a = authority(true);
        let d = CompactionDispatch::new(a);
        d.before_send().unwrap();
        assert!(d.sent().is_err());
        assert!(!d.confirmed.is_cancelled());
        assert!(d.before_send().is_err());
    }
    #[test]
    fn transport_receipt_requires_prior_send_intent() {
        let a = authority(false);
        let d = CompactionDispatch::new(a.clone());
        assert!(d.sent().is_err());
        assert_eq!(a.receipts.load(Ordering::Acquire), 0);
    }
    #[tokio::test]
    async fn scoped_factory_keeps_actual_guard_and_confirmed_receipt() {
        let a = authority(false);
        let d = CompactionDispatch::new(a.clone());
        assert!(current().is_none());
        scope(d.clone(), async {
            let actual = current().unwrap();
            actual.before_send().unwrap();
            actual.sent().unwrap();
        })
        .await;
        d.confirmed().await;
        assert!(current().is_none());
        assert_eq!(a.receipts.load(Ordering::Acquire), 1);
    }
    #[test]
    fn two_transport_threads_admit_exactly_one_send() {
        let a = authority(false);
        let d = CompactionDispatch::new(a.clone());
        let threads: Vec<_> = (0..2)
            .map(|_| {
                let d = d.clone();
                std::thread::spawn(move || d.before_send().is_ok())
            })
            .collect();
        let admitted = threads
            .into_iter()
            .map(|t| usize::from(t.join().unwrap()))
            .sum::<usize>();
        assert_eq!(admitted, 1);
        assert_eq!(a.intents.load(Ordering::Acquire), 1);
    }
    #[tokio::test]
    async fn factory_task_scope_requires_actual_task_drop_after_cancel() {
        let scope = CompactionTransportScope::new();
        let lease = scope.lease();
        scope.cancel.cancel();
        assert_eq!(scope.tasks.load(Ordering::Acquire), 1);
        drop(lease);
        scope.drain().await;
        assert_eq!(scope.tasks.load(Ordering::Acquire), 0);
    }
    #[tokio::test]
    async fn original_scope_is_inherited_only_by_its_factory() {
        let scope = CompactionTransportScope::new();
        assert!(transport_scope().is_none());
        track_transport(scope.clone(), async {
            assert!(Arc::ptr_eq(&transport_scope().unwrap(), &scope));
        })
        .await;
        assert!(transport_scope().is_none());
    }
    #[test]
    fn owner_revoked_during_native_attestation_cannot_send_late() {
        struct Blocking {
            entered: Arc<std::sync::Barrier>,
            release: Arc<std::sync::Barrier>,
            intents: AtomicUsize,
        }
        impl CompactionDispatchAuthority for Blocking {
            fn remaining(&self) -> Result<std::time::Duration, String> {
                Ok(std::time::Duration::from_secs(15))
            }
            fn validate_fresh(&self) -> Result<(), String> {
                self.entered.wait();
                self.release.wait();
                Ok(())
            }
            fn persist_send_intent(&self) -> Result<(), String> {
                self.intents.fetch_add(1, Ordering::AcqRel);
                Ok(())
            }
            fn persist_sent(&self) -> Result<(), String> {
                panic!("late send forbidden")
            }
        }
        let a = Arc::new(Blocking {
            entered: Arc::new(std::sync::Barrier::new(2)),
            release: Arc::new(std::sync::Barrier::new(2)),
            intents: AtomicUsize::new(0),
        });
        let d = CompactionDispatch::new(a.clone());
        let owner = CompactionDispatchOwner(d.clone());
        let worker = std::thread::spawn(move || d.before_send());
        a.entered.wait();
        drop(owner);
        a.release.wait();
        assert!(worker.join().unwrap().is_err());
        assert_eq!(a.intents.load(Ordering::Acquire), 0);
    }
    #[test]
    fn owner_revoked_during_durable_intent_cannot_send_late() {
        struct Blocking {
            entered: Arc<std::sync::Barrier>,
            release: Arc<std::sync::Barrier>,
        }
        impl CompactionDispatchAuthority for Blocking {
            fn remaining(&self) -> Result<std::time::Duration, String> {
                Ok(std::time::Duration::from_secs(15))
            }
            fn validate_fresh(&self) -> Result<(), String> {
                Ok(())
            }
            fn persist_send_intent(&self) -> Result<(), String> {
                self.entered.wait();
                self.release.wait();
                Ok(())
            }
            fn persist_sent(&self) -> Result<(), String> {
                panic!("late send forbidden")
            }
        }
        let a = Arc::new(Blocking {
            entered: Arc::new(std::sync::Barrier::new(2)),
            release: Arc::new(std::sync::Barrier::new(2)),
        });
        let d = CompactionDispatch::new(a.clone());
        let owner = CompactionDispatchOwner(d.clone());
        let worker = std::thread::spawn(move || d.before_send());
        a.entered.wait();
        drop(owner);
        a.release.wait();
        assert!(worker.join().unwrap().is_err());
    }
}
