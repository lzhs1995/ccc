use super::*;
use std::sync::Barrier;
use std::sync::atomic::AtomicBool;
use std::sync::atomic::AtomicUsize;
use std::sync::atomic::Ordering;
use std::sync::mpsc;
use std::task::Wake;
use std::thread;

struct WakeCount(AtomicUsize);
impl Wake for WakeCount {
    fn wake(self: Arc<Self>) {
        self.0.fetch_add(1, Ordering::SeqCst);
    }
    fn wake_by_ref(self: &Arc<Self>) {
        self.0.fetch_add(1, Ordering::SeqCst);
    }
}
fn poll<F: Future>(future: &mut F) -> Poll<F::Output>
where
    F: Unpin,
{
    let waker = Waker::from(Arc::new(WakeCount(AtomicUsize::new(0))));
    Pin::new(future).poll(&mut Context::from_waker(&waker))
}
fn identity() -> Identity {
    Identity {
        native_epoch: "native-e".into(),
        thread_id: "thread-t".into(),
        turn_id: "turn-u".into(),
        occurrence: "item-o".into(),
        generation: 0,
    }
}
struct Pending {
    drops: Arc<AtomicUsize>,
}
impl Future for Pending {
    type Output = usize;
    fn poll(self: Pin<&mut Self>, _: &mut Context<'_>) -> Poll<usize> {
        Poll::Pending
    }
}
impl Drop for Pending {
    fn drop(&mut self) {
        self.drops.fetch_add(1, Ordering::SeqCst);
    }
}
fn pending() -> (Pending, Arc<AtomicUsize>) {
    let drops = Arc::new(AtomicUsize::new(0));
    (
        Pending {
            drops: drops.clone(),
        },
        drops,
    )
}
fn replacement<T>(value: Poll<WaitOutcome<T>>) -> ReplacementPermit {
    match value {
        Poll::Ready(WaitOutcome::Replacement {
            permit,
            raced_output: None,
        }) => permit,
        _ => panic!("expected drained permit"),
    }
}

#[test]
fn completion_wins_and_old_control_cannot_touch_result() {
    let id = identity();
    let (ctl, mut wait) = Control::begin(id.clone(), std::future::ready(17)).unwrap();
    assert!(matches!(
        poll(&mut wait),
        Poll::Ready(WaitOutcome::Completed(17))
    ));
    assert_eq!(ctl.claim(&id, "attempt"), Err(Refused::NotWaiting));
    assert_eq!(ctl.snapshot().unwrap().phase, Phase::Completed);
}

#[test]
fn exact_epoch_thread_turn_occurrence_generation_required() {
    let (future, _) = pending();
    let id = identity();
    let (ctl, _wait) = Control::begin(id.clone(), future).unwrap();
    let mut bad = id.clone();
    bad.native_epoch.push('x');
    assert_eq!(ctl.claim(&bad, "a"), Err(Refused::IdentityChanged));
    let mut bad = id.clone();
    bad.thread_id.push('x');
    assert_eq!(ctl.claim(&bad, "a"), Err(Refused::IdentityChanged));
    let mut bad = id.clone();
    bad.turn_id.push('x');
    assert_eq!(ctl.claim(&bad, "a"), Err(Refused::IdentityChanged));
    let mut bad = id.clone();
    bad.occurrence.push('x');
    assert_eq!(ctl.claim(&bad, "a"), Err(Refused::IdentityChanged));
    let mut bad = id.clone();
    bad.generation = 1;
    assert_eq!(ctl.claim(&bad, "a"), Err(Refused::IdentityChanged));
    assert_eq!(ctl.snapshot().unwrap().phase, Phase::Waiting);
    assert!(ctl.snapshot().unwrap().attempts_consumed.is_empty());
}

#[test]
fn claim_is_not_drained_and_replacement_factory_follows_drop() {
    let (future, drops) = pending();
    let id = identity();
    let (ctl, mut wait) = Control::begin(id.clone(), future).unwrap();
    assert!(poll(&mut wait).is_pending());
    ctl.claim(&id, "a").unwrap();
    assert_eq!(drops.load(Ordering::SeqCst), 0);
    assert_eq!(
        ctl.snapshot().unwrap().phase,
        Phase::Claimed {
            attempt_id: "a".into()
        }
    );
    let permit = replacement(poll(&mut wait));
    assert_eq!(permit.attempt_id(), "a");
    assert_eq!(drops.load(Ordering::SeqCst), 1);
    let called = AtomicBool::new(false);
    let mut next = permit
        .start(|| {
            assert_eq!(drops.load(Ordering::SeqCst), 1);
            called.store(true, Ordering::SeqCst);
            std::future::ready(23)
        })
        .unwrap();
    assert!(called.load(Ordering::SeqCst));
    assert_eq!(ctl.snapshot().unwrap().identity.generation, 1);
    assert!(matches!(
        poll(&mut next),
        Poll::Ready(WaitOutcome::Completed(23))
    ));
}

#[test]
fn claim_wakes_original_wait() {
    let (future, _) = pending();
    let id = identity();
    let (ctl, mut wait) = Control::begin(id.clone(), future).unwrap();
    let wakes = Arc::new(WakeCount(AtomicUsize::new(0)));
    let waker = Waker::from(wakes.clone());
    assert!(
        Pin::new(&mut wait)
            .poll(&mut Context::from_waker(&waker))
            .is_pending()
    );
    ctl.claim(&id, "a").unwrap();
    assert_eq!(wakes.0.load(Ordering::SeqCst), 1);
}

#[test]
fn actual_concurrent_claim_keeps_raced_output_for_accounting_only() {
    struct Racing {
        entered: Arc<Barrier>,
        decided: mpsc::Receiver<()>,
        drops: Arc<AtomicUsize>,
    }
    impl Future for Racing {
        type Output = usize;
        fn poll(self: Pin<&mut Self>, _: &mut Context<'_>) -> Poll<usize> {
            self.entered.wait();
            self.decided.recv().unwrap();
            Poll::Ready(99)
        }
    }
    impl Drop for Racing {
        fn drop(&mut self) {
            self.drops.fetch_add(1, Ordering::SeqCst);
        }
    }
    let id = identity();
    let barrier = Arc::new(Barrier::new(2));
    let (tx, rx) = mpsc::channel();
    let drops = Arc::new(AtomicUsize::new(0));
    let (ctl, mut wait) = Control::begin(
        id.clone(),
        Racing {
            entered: barrier.clone(),
            decided: rx,
            drops: drops.clone(),
        },
    )
    .unwrap();
    let peer = thread::spawn(move || {
        barrier.wait();
        ctl.claim(&id, "racing-a").unwrap();
        tx.send(()).unwrap();
        ctl
    });
    let permit = match poll(&mut wait) {
        Poll::Ready(WaitOutcome::Replacement {
            permit,
            raced_output: Some(99),
        }) => permit,
        _ => panic!("raced known output must be retained for accounting"),
    };
    let ctl = peer.join().unwrap();
    assert_eq!(drops.load(Ordering::SeqCst), 1);
    assert_eq!(permit.attempt_id(), "racing-a");
    assert_eq!(
        ctl.snapshot().unwrap().phase,
        Phase::Drained {
            attempt_id: "racing-a".into()
        }
    );
}

#[test]
fn two_real_callers_can_consume_only_one_attempt() {
    let id = identity();
    let (future, _) = pending();
    let (ctl, _wait) = Control::begin(id.clone(), future).unwrap();
    let barrier = Arc::new(Barrier::new(3));
    let mut handles = Vec::new();
    for attempt in ["a", "b"] {
        let c = ctl.clone();
        let i = id.clone();
        let b = barrier.clone();
        handles.push(thread::spawn(move || {
            b.wait();
            c.claim(&i, attempt)
        }));
    }
    barrier.wait();
    let results: Vec<_> = handles.into_iter().map(|h| h.join().unwrap()).collect();
    assert_eq!(results.iter().filter(|r| r.is_ok()).count(), 1);
    assert_eq!(ctl.snapshot().unwrap().attempts_consumed.len(), 1);
}

#[test]
fn no_replay_after_claim_or_changed_generation() {
    let id = identity();
    let (future, _) = pending();
    let (ctl, mut wait) = Control::begin(id.clone(), future).unwrap();
    ctl.claim(&id, "a").unwrap();
    assert_eq!(ctl.claim(&id, "a"), Err(Refused::AttemptConsumed));
    let permit = replacement(poll(&mut wait));
    let (future, _) = pending();
    let _next = permit.start(|| future).unwrap();
    assert_eq!(ctl.claim(&id, "b"), Err(Refused::IdentityChanged));
    let current = ctl.snapshot().unwrap().identity;
    assert_eq!(ctl.claim(&current, "a"), Err(Refused::AttemptConsumed));
}

#[test]
fn dropping_claimed_future_is_unknown_not_replayable() {
    let id = identity();
    let (future, drops) = pending();
    let (ctl, wait) = Control::begin(id.clone(), future).unwrap();
    ctl.claim(&id, "a").unwrap();
    drop(wait);
    assert_eq!(drops.load(Ordering::SeqCst), 1);
    assert_eq!(ctl.snapshot().unwrap().phase, Phase::Unknown);
    assert_eq!(ctl.claim(&id, "a"), Err(Refused::AttemptConsumed));
    assert_eq!(ctl.claim(&id, "b"), Err(Refused::NotWaiting));
}

#[test]
fn dropping_unclaimed_future_cancels_without_consuming_attempt() {
    let id = identity();
    let (future, _) = pending();
    let (ctl, wait) = Control::begin(id.clone(), future).unwrap();
    drop(wait);
    assert_eq!(ctl.snapshot().unwrap().phase, Phase::Cancelled);
    assert!(ctl.snapshot().unwrap().attempts_consumed.is_empty());
    assert_eq!(ctl.claim(&id, "a"), Err(Refused::NotWaiting));
}

#[test]
fn dropping_drained_permit_keeps_consumed_intent_unknown() {
    let id = identity();
    let (future, _) = pending();
    let (ctl, mut wait) = Control::begin(id.clone(), future).unwrap();
    ctl.claim(&id, "a").unwrap();
    drop(replacement(poll(&mut wait)));
    assert_eq!(ctl.snapshot().unwrap().phase, Phase::Unknown);
    assert_eq!(ctl.claim(&id, "b"), Err(Refused::NotWaiting));
}

#[test]
fn replacement_ceiling_never_resets() {
    let (future, _) = pending();
    let (ctl, mut wait) = Control::begin(identity(), future).unwrap();
    for attempt in ["a", "b"] {
        let id = ctl.snapshot().unwrap().identity;
        ctl.claim(&id, attempt).unwrap();
        let permit = replacement(poll(&mut wait));
        let (future, _) = pending();
        wait = permit.start(|| future).unwrap();
    }
    let state = ctl.snapshot().unwrap();
    assert_eq!(state.replacement_slots_consumed, 2);
    assert_eq!(state.identity.generation, 2);
    assert_eq!(
        ctl.claim(&state.identity, "c"),
        Err(Refused::BudgetExhausted)
    );
}

#[test]
fn panicking_factory_does_not_publish_live_replacement() {
    let (future, _) = pending();
    let (ctl, mut wait) = Control::begin(identity(), future).unwrap();
    ctl.claim(&identity(), "a").unwrap();
    let permit = replacement(poll(&mut wait));
    let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        permit.start(|| -> std::future::Ready<usize> { panic!("fixture factory failure") })
    }));
    assert!(result.is_err());
    let state = ctl.snapshot().unwrap();
    assert_eq!(state.phase, Phase::Unknown);
    assert_eq!(state.replacement_slots_consumed, 1);
    assert_eq!(ctl.claim(&state.identity, "b"), Err(Refused::NotWaiting));
}

#[test]
fn invalid_initial_identity_and_nonzero_generation_rejected() {
    let mut id = identity();
    id.native_epoch.clear();
    assert!(matches!(
        Control::begin(id, std::future::ready(0)),
        Err(Refused::InvalidIdentity)
    ));
    let mut id = identity();
    id.generation = 1;
    assert!(matches!(
        Control::begin(id, std::future::ready(0)),
        Err(Refused::InvalidIdentity)
    ));
}

#[test]
fn future_error_is_preserved_and_is_not_native_retry_authorization() {
    let (ctl, mut wait) = Control::begin(
        identity(),
        std::future::ready(Err::<usize, _>("provider-error")),
    )
    .unwrap();
    assert!(matches!(
        poll(&mut wait),
        Poll::Ready(WaitOutcome::Completed(Err("provider-error")))
    ));
    assert_eq!(ctl.claim(&identity(), "a"), Err(Refused::NotWaiting));
    assert!(ctl.snapshot().unwrap().attempts_consumed.is_empty());
}

#[test]
fn slot_is_consumed_before_async_factory_and_failure_cannot_replay() {
    let id = identity();
    let (future, _) = pending();
    let (ctl, mut wait) = Control::begin(id.clone(), future).unwrap();
    ctl.claim(&id, "factory-attempt").unwrap();
    let reservation = replacement(poll(&mut wait)).reserve().unwrap();
    let snapshot = ctl.snapshot().unwrap();
    assert_eq!(snapshot.phase, Phase::Unknown);
    assert_eq!(snapshot.replacement_slots_consumed, 1);
    assert_eq!(snapshot.identity.generation, 1);
    assert_eq!(
        ctl.claim(&snapshot.identity, "new-attempt"),
        Err(Refused::NotWaiting)
    );
    drop(reservation);
    assert_eq!(ctl.snapshot().unwrap().phase, Phase::Unknown);
    assert_eq!(
        ctl.claim(&snapshot.identity, "factory-attempt"),
        Err(Refused::AttemptConsumed)
    );
}

#[test]
fn successful_factory_attaches_only_reserved_generation() {
    let id = identity();
    let (future, drops) = pending();
    let (ctl, mut wait) = Control::begin(id.clone(), future).unwrap();
    ctl.claim(&id, "a").unwrap();
    let reservation = replacement(poll(&mut wait)).reserve().unwrap();
    assert_eq!(drops.load(Ordering::SeqCst), 1);
    let mut next = reservation.attach(std::future::ready(31)).unwrap();
    assert_eq!(ctl.snapshot().unwrap().identity.generation, 1);
    assert!(matches!(
        poll(&mut next),
        Poll::Ready(WaitOutcome::Completed(31))
    ));
}

#[test]
fn factory_collection_transition_preserves_generation_and_consumed_budget() {
    let (control, mut first) = Control::begin(identity(), std::future::ready(7)).unwrap();
    assert!(matches!(
        poll(&mut first),
        Poll::Ready(WaitOutcome::Completed(7))
    ));
    let before = control.snapshot().unwrap();
    let mut collect = control.next_stage(std::future::pending::<()>()).unwrap();
    let now = control.snapshot().unwrap();
    assert_eq!(before.identity, now.identity);
    assert_eq!(
        before.replacement_slots_consumed,
        now.replacement_slots_consumed
    );
    control
        .claim(&now.identity, "same-provider-collection")
        .unwrap();
    assert!(matches!(
        poll(&mut collect),
        Poll::Ready(WaitOutcome::Replacement { .. })
    ));
}
