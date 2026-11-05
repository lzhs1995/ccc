//! Candidate native mechanism for a single provider-wait phase of compaction.
//!
//! This module must wrap ONLY an owned, cancellation-safe provider future. No
//! history installation, token accounting, hook execution or task abortion may
//! occur inside that future. The official collectors must split these stages
//! before integration. Completion and recovery are linearized by the same lock.
//! This is an internal mechanism, NOT a public request authorization interface.
//! Core must admit the request under its active-turn/input locks first; the host
//! additionally owns selected-client, draft, dialog, Goal and original-intent
//! checks. In-memory receipts below are NOT durable native receipts.
//!
//! The native instance epoch is generated once by the original native actor.
//! `occurrence` is its original ContextCompactionItem UUID, never a screen label.
//! `generation` distinguishes replacement provider waits in that same item.

use std::collections::BTreeSet;
use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;
use std::sync::Mutex;
use std::task::Context;
use std::task::Poll;
use std::task::Waker;

#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct Identity {
    pub(crate) native_epoch: String,
    pub(crate) thread_id: String,
    pub(crate) turn_id: String,
    pub(crate) occurrence: String,
    pub(crate) generation: u64,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) enum Phase {
    Waiting,
    Claimed { attempt_id: String },
    Drained { attempt_id: String },
    Completed,
    Cancelled,
    Unknown,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct Snapshot {
    pub(crate) identity: Identity,
    pub(crate) phase: Phase,
    pub(crate) replacement_slots_consumed: usize,
    pub(crate) attempts_consumed: Vec<String>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Refused {
    InvalidIdentity,
    IdentityChanged,
    AttemptConsumed,
    NotWaiting,
    BudgetExhausted,
    Poisoned,
}

struct State {
    identity: Identity,
    phase: Phase,
    attempts: BTreeSet<String>,
    replacement_slots_consumed: usize,
    waker: Option<Waker>,
}

#[derive(Clone)]
pub(crate) struct Control(Arc<Mutex<State>>);

impl Control {
    /// The factory must provide native-owned nonempty identities. No generation
    /// or replacement budget is reconstructed from an old rollout or receipt.
    pub(crate) fn begin<F: Future>(
        identity: Identity,
        future: F,
    ) -> Result<(Self, ProviderWait<F>), Refused> {
        if identity.native_epoch.is_empty()
            || identity.thread_id.is_empty()
            || identity.turn_id.is_empty()
            || identity.occurrence.is_empty()
            || identity.generation != 0
        {
            return Err(Refused::InvalidIdentity);
        }
        let control = Self(Arc::new(Mutex::new(State {
            identity: identity.clone(),
            phase: Phase::Waiting,
            attempts: BTreeSet::new(),
            replacement_slots_consumed: 0,
            waker: None,
        })));
        let wait = ProviderWait {
            control: control.clone(),
            expected: identity,
            future: Some(Box::pin(future)),
            resolved: false,
        };
        Ok((control, wait))
    }

    /// A provider's ordinary retry in the same compaction item retains all
    /// original recovery attempts/budget and advances only the wait generation.
    pub(crate) fn followup<F: Future>(&self, future: F) -> Result<ProviderWait<F>, Refused> {
        let expected = {
            let mut state = self.0.lock().map_err(|_| Refused::Poisoned)?;
            if state.phase != Phase::Completed {
                return Err(Refused::NotWaiting);
            }
            state.identity.generation = state
                .identity
                .generation
                .checked_add(1)
                .ok_or(Refused::BudgetExhausted)?;
            state.phase = Phase::Waiting;
            state.identity.clone()
        };
        Ok(ProviderWait {
            control: self.clone(),
            expected,
            future: Some(Box::pin(future)),
            resolved: false,
        })
    }

    /// Factory and response collection are two stages of the same provider
    /// generation. A stage transition cannot reset attempts or advance the
    /// original recovery budget.
    pub(crate) fn next_stage<F: Future>(&self, future: F) -> Result<ProviderWait<F>, Refused> {
        let expected = {
            let mut state = self.0.lock().map_err(|_| Refused::Poisoned)?;
            if state.phase != Phase::Completed {
                return Err(Refused::NotWaiting);
            }
            state.phase = Phase::Waiting;
            state.identity.clone()
        };
        Ok(ProviderWait {
            control: self.clone(),
            expected,
            future: Some(Box::pin(future)),
            resolved: false,
        })
    }

    pub(crate) fn snapshot(&self) -> Result<Snapshot, Refused> {
        let state = self.0.lock().map_err(|_| Refused::Poisoned)?;
        Ok(Snapshot {
            identity: state.identity.clone(),
            phase: state.phase.clone(),
            replacement_slots_consumed: state.replacement_slots_consumed,
            attempts_consumed: state.attempts.iter().cloned().collect(),
        })
    }

    /// Call only after native actor admission. Success means CLAIMED, never
    /// cancellation completed, a replacement started, or a provider sent input.
    pub(crate) fn claim(&self, expected: &Identity, attempt_id: &str) -> Result<(), Refused> {
        if attempt_id.is_empty() {
            return Err(Refused::InvalidIdentity);
        }
        let waker = {
            let mut state = self.0.lock().map_err(|_| Refused::Poisoned)?;
            if &state.identity != expected {
                return Err(Refused::IdentityChanged);
            }
            if state.attempts.contains(attempt_id) {
                return Err(Refused::AttemptConsumed);
            }
            if state.phase != Phase::Waiting {
                return Err(Refused::NotWaiting);
            }
            // This is a ceiling, not a grant. The original controller's stricter
            // root deadline and attempts still apply at native admission.
            if state.replacement_slots_consumed >= 2 {
                return Err(Refused::BudgetExhausted);
            }
            state.attempts.insert(attempt_id.to_owned());
            state.phase = Phase::Claimed {
                attempt_id: attempt_id.to_owned(),
            };
            state.waker.take()
        };
        if let Some(waker) = waker {
            waker.wake();
        }
        Ok(())
    }
}

pub(crate) enum WaitOutcome<T> {
    Completed(T),
    Replacement {
        permit: ReplacementPermit,
        // A response that became ready while the claim won must still be
        // accounted for; it may not be installed as compacted history.
        raced_output: Option<T>,
    },
    Refused(Refused),
}

/// Owns the original provider future. A replacement permit cannot exist until
/// this future has been synchronously dropped. Dropping a join handle alone
/// does not cancel a spawned task and is therefore prohibited as the future.
pub(crate) struct ProviderWait<F: Future> {
    control: Control,
    expected: Identity,
    future: Option<Pin<Box<F>>>,
    resolved: bool,
}

impl<F: Future> Unpin for ProviderWait<F> {}

impl<F: Future> ProviderWait<F> {
    fn drain(
        &mut self,
        attempt_id: String,
        raced_output: Option<F::Output>,
    ) -> Poll<WaitOutcome<F::Output>> {
        // Drop outside the state lock: cancellation cleanup may itself read
        // native state. No replacement is authorized before drop returns.
        drop(self.future.take());
        let mut state = match self.control.0.lock() {
            Ok(state) => state,
            Err(_) => {
                self.resolved = true;
                return Poll::Ready(WaitOutcome::Refused(Refused::Poisoned));
            }
        };
        if state.identity != self.expected
            || state.phase
                != (Phase::Claimed {
                    attempt_id: attempt_id.clone(),
                })
        {
            self.resolved = true;
            return Poll::Ready(WaitOutcome::Refused(Refused::IdentityChanged));
        }
        state.phase = Phase::Drained {
            attempt_id: attempt_id.clone(),
        };
        state.waker = None;
        self.resolved = true;
        Poll::Ready(WaitOutcome::Replacement {
            permit: ReplacementPermit {
                control: self.control.clone(),
                expected: self.expected.clone(),
                attempt_id,
                consumed: false,
            },
            raced_output,
        })
    }
}

impl<F: Future> Future for ProviderWait<F> {
    type Output = WaitOutcome<F::Output>;

    fn poll(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Self::Output> {
        let this = self.get_mut();
        assert!(
            !this.resolved,
            "a completed native wait cannot be polled again"
        );
        let claimed = {
            let mut state = match this.control.0.lock() {
                Ok(state) => state,
                Err(_) => {
                    this.resolved = true;
                    return Poll::Ready(WaitOutcome::Refused(Refused::Poisoned));
                }
            };
            if state.identity != this.expected {
                this.resolved = true;
                return Poll::Ready(WaitOutcome::Refused(Refused::IdentityChanged));
            }
            match &state.phase {
                Phase::Claimed { attempt_id } => Some(attempt_id.clone()),
                Phase::Waiting => {
                    state.waker = Some(cx.waker().clone());
                    None
                }
                _ => {
                    this.resolved = true;
                    return Poll::Ready(WaitOutcome::Refused(Refused::NotWaiting));
                }
            }
        };
        if let Some(attempt_id) = claimed {
            return this.drain(attempt_id, None);
        }
        // Do not hold the state lock while polling transport. If a claim wins
        // during poll, even a concurrently Ready output must not be committed.
        let output = match this
            .future
            .as_mut()
            .expect("owned future")
            .as_mut()
            .poll(cx)
        {
            Poll::Pending => return Poll::Pending,
            Poll::Ready(output) => output,
        };
        let claimed = {
            let mut state = match this.control.0.lock() {
                Ok(state) => state,
                Err(_) => {
                    this.resolved = true;
                    return Poll::Ready(WaitOutcome::Refused(Refused::Poisoned));
                }
            };
            if state.identity != this.expected {
                this.resolved = true;
                return Poll::Ready(WaitOutcome::Refused(Refused::IdentityChanged));
            }
            match &state.phase {
                Phase::Claimed { attempt_id } => Some(attempt_id.clone()),
                Phase::Waiting => {
                    state.phase = Phase::Completed;
                    state.waker = None;
                    None
                }
                _ => {
                    this.resolved = true;
                    return Poll::Ready(WaitOutcome::Refused(Refused::NotWaiting));
                }
            }
        };
        if let Some(attempt_id) = claimed {
            return this.drain(attempt_id, Some(output));
        }
        this.resolved = true;
        drop(this.future.take());
        Poll::Ready(WaitOutcome::Completed(output))
    }
}

impl<F: Future> Drop for ProviderWait<F> {
    fn drop(&mut self) {
        if self.resolved {
            return;
        }
        if let Ok(mut state) = self.control.0.lock() {
            if state.identity == self.expected {
                state.phase = match state.phase {
                    Phase::Claimed { .. } | Phase::Drained { .. } => Phase::Unknown,
                    _ => Phase::Cancelled,
                };
                state.waker = None;
            }
        }
    }
}

/// A single-use native continuation capability, not serializable and not Clone.
/// The actor must durably record the original consumed intent and drained
/// receipt before it calls start. A dropped permit leaves UNKNOWN, not retryable.
pub(crate) struct ReplacementPermit {
    control: Control,
    expected: Identity,
    attempt_id: String,
    consumed: bool,
}

impl ReplacementPermit {
    pub(crate) fn attempt_id(&self) -> &str {
        &self.attempt_id
    }

    /// Construct the new future only after the original has been dropped.
    /// This records scheduling; actual provider dispatch and durable receipt
    /// must be recorded separately by the real transport.
    /// Reserve a replacement slot before invoking an async transport factory.
    /// A dropped or failed factory leaves UNKNOWN. No network I/O occurs here.
    pub(crate) fn reserve(mut self) -> Result<ReplacementReservation, Refused> {
        let expected = {
            let mut state = self.control.0.lock().map_err(|_| Refused::Poisoned)?;
            if state.identity != self.expected
                || state.phase
                    != (Phase::Drained {
                        attempt_id: self.attempt_id.clone(),
                    })
            {
                return Err(Refused::IdentityChanged);
            }
            if state.replacement_slots_consumed >= 2 {
                return Err(Refused::BudgetExhausted);
            }
            state.identity.generation += 1;
            state.replacement_slots_consumed += 1;
            state.phase = Phase::Unknown;
            state.identity.clone()
        };
        self.consumed = true;
        Ok(ReplacementReservation {
            control: self.control.clone(),
            expected,
        })
    }

    pub(crate) fn start<F: Future>(
        mut self,
        make_future: impl FnOnce() -> F,
    ) -> Result<ProviderWait<F>, Refused> {
        let expected = {
            let mut state = self.control.0.lock().map_err(|_| Refused::Poisoned)?;
            if state.identity != self.expected
                || state.phase
                    != (Phase::Drained {
                        attempt_id: self.attempt_id.clone(),
                    })
            {
                return Err(Refused::IdentityChanged);
            }
            if state.replacement_slots_consumed >= 2 {
                return Err(Refused::BudgetExhausted);
            }
            state.identity.generation += 1;
            state.replacement_slots_consumed += 1;
            // Stay unclaimable until the future factory finishes. If it panics,
            // this consumed occurrence remains UNKNOWN rather than becoming live.
            state.phase = Phase::Unknown;
            state.identity.clone()
        };
        let future = Box::pin(make_future());
        {
            let mut state = self.control.0.lock().map_err(|_| Refused::Poisoned)?;
            if state.identity != expected || state.phase != Phase::Unknown {
                return Err(Refused::IdentityChanged);
            }
            state.phase = Phase::Waiting;
        }
        self.consumed = true;
        Ok(ProviderWait {
            control: self.control.clone(),
            expected,
            future: Some(future),
            resolved: false,
        })
    }
}

impl Drop for ReplacementPermit {
    fn drop(&mut self) {
        if self.consumed {
            return;
        }
        if let Ok(mut state) = self.control.0.lock() {
            if state.identity == self.expected {
                state.phase = Phase::Unknown;
                state.waker = None;
            }
        }
    }
}

/// A consumed slot waiting for the real transport factory. Until attach returns,
/// no new occurrence is live and all requests for this generation are refused.
pub(crate) struct ReplacementReservation {
    control: Control,
    expected: Identity,
}
impl ReplacementReservation {
    pub(crate) fn attach<F: Future>(self, future: F) -> Result<ProviderWait<F>, Refused> {
        {
            let mut state = self.control.0.lock().map_err(|_| Refused::Poisoned)?;
            if state.identity != self.expected || state.phase != Phase::Unknown {
                return Err(Refused::IdentityChanged);
            }
            state.phase = Phase::Waiting;
        }
        Ok(ProviderWait {
            control: self.control.clone(),
            expected: self.expected,
            future: Some(Box::pin(future)),
            resolved: false,
        })
    }
}

#[cfg(test)]
#[path = "compaction_recovery_gate_tests.rs"]
mod tests;
