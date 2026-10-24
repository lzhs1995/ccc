# 0.4.40: preserve authorized inquiries and the native client boundary

An explicitly authorized historical episode could pass complete file-protocol
verification yet lose reservation status inquiries because the lifecycle check
required its controller to equal the current release. The lifecycle now asks the
existing protocol verifier whether that exact episode is supported. It retains
the original controller and explicit reservation-inquiry enrollment. A historical
episode that never had the capability remains paused.

Tick, summary, scheduler and RELEASE observation use the same decision.
Within one sweep they reuse the same verification object; every reuse still
checks the verified members and parents, and authorization, enrollment, STOP,
expiry, binding, reservations and replies remain live. No historical code is
loaded, no enrollment is manufactured, and no terminal input is sent. A bound
reply is still consumed only by the original executor through its original
controller. Consumption does not release a reservation.

The embedded identity candidate also admitted an intermediate model CLI that
inherited its parent Codex variables. Review finding F98-1 was reproduced for
embedded and managed tools and hooks. All four paths now reject intervening
model clients and unknown wrappers. The resolver checks kernel-read executable
names, permitting supported shell/rtk intermediates and the leaf interpreter
only. Intermediate Node/Python launchers, version-named clients and misleading
argv labels are refused. Existing PID/birth, foreground, UUID, TTY and reread
checks remain. See [caller identity](shared-daemon-caller.md).

Validation reuses the completed 217-test reservation run and 2-test expiry/cache
run. The identity correction passed 432 related tests in 39.553 seconds.
Tests cover the four rejected model paths, supported wrappers, the leaf
interpreter, masked executable labels, absent selectors and nested intermediates.
Counts are per-run; prior baseline suites are not rerun solely for versioning.
The full CI matrix and original Claude's bounded delta review are recorded at
publication rather than inferred from these local results.

The unmerged 0.4.39 candidate is incorporated here, including native UUID alias
agreement and original embedded consumer-proof preservation. The reviewed
historical release manifest allowlist still ends at 0.4.37. Neither the
uninstalled 0.4.38 release nor the 0.4.39 candidate gains historical enrollment
authority from this update.

Installation, active-client automatic hooks, natural scheduler execution,
complete body reading and native callback reception are separate observations.
This release does not create a missing native control socket, revive a stopped
episode, rebind a managed proof or replay an old callback.
