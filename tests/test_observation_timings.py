"""Observation timings distinguish work from waiting and reject stale publishers."""
import contextlib
import copy
import json
import tempfile
import unittest
from unittest.mock import Mock, patch

import cmux_codex_watch as core
from tests.test_watch import FakeClient, armed_daemon, grid_payload


class ManualClock:
    def __init__(self):
        self.elapsed_ms = 0

    def advance(self, milliseconds):
        self.elapsed_ms += milliseconds

    def monotonic(self):
        return 100 + self.elapsed_ms / 1000

    def time(self):
        return 1000 + self.elapsed_ms / 1000


class ObservationTimingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.client = FakeClient(grid_payload([]))
        self.daemon = armed_daemon(directory.name, self.client)
        self.addCleanup(self.daemon._delivery_store.close)
        self.addCleanup(self.daemon._process_snapshots.close)
        self.target = self.daemon.config["targets"][0]
        self.runtime = core.TargetRuntime(
            native_failure_at=1, read_duration_ms=888,
            observation_completed_at=999, observation_interval_ms=555,
            observation_timings={"source": "baseline"},
        )
        self.daemon.runtime[self.target["surface_id"]] = self.runtime
        self.state = core.ScreenState("recoverable_error", message_kind="codex")
        self.clock = ManualClock()
        clocks = patch.multiple(core.time, monotonic=self.clock.monotonic, time=self.clock.time)
        clocks.start()
        self.addCleanup(clocks.stop)

        # All external observations and input boundaries are in-memory fixtures.
        # Keep _process_one_target's actual branching, locking and error paths.
        self.daemon._observation_client = Mock(return_value=self.client)
        self.daemon._argv_startup_observation_held = Mock(return_value=False)
        self.daemon._active_send_target = Mock(side_effect=self.cost(31, self.target))
        self.client.tree = Mock(side_effect=self.cost(29, {}))
        self.daemon._observe_target_viewport = Mock(side_effect=self.cost(11, self.state))
        self.daemon._claude_process_observation = Mock(side_effect=self.cost(13))
        self.daemon._apply_claude_process_observation = Mock(side_effect=self.cost(17))
        self.daemon._recover_stranded_codex_queue = Mock(side_effect=lambda t, r, s, *args: s)
        self.daemon._reconcile_codex_delivery = Mock(return_value=False)
        self.daemon._reconcile_claude_submit = Mock(return_value=False)
        self.daemon._maybe_send_deferred_claude_stop = Mock(return_value=False)
        self.daemon._codex_turn_ready = Mock(side_effect=self.cost(19, True))
        self.daemon._handle_state = Mock(side_effect=self.cost(23))

    def tearDown(self):
        self.assertEqual(self.client.sent, [])
        self.assertEqual(self.client.sent_text, [])
        self.assertEqual(self.client.sent_keys, [])

    def cost(self, milliseconds, result=None, error=None):
        def operation(*args, **kwargs):
            self.clock.advance(milliseconds)
            if error is not None:
                raise error
            return result
        return operation

    def observe(self, source="periodic", current=lambda: True):
        method = self.daemon._scheduled_native if source == "native" else self.daemon._scheduled_observe
        return method(self.target, current)

    def metrics(self, runtime=None):
        runtime = runtime or self.runtime
        return copy.deepcopy((runtime.observation_timings, runtime.read_duration_ms,
                              runtime.observation_completed_at, runtime.observation_interval_ms))

    def surface_lock_cost(self, wait_ms=0, release_ms=0, error=None):
        depth = 0
        @contextlib.contextmanager
        def acquire(surface_id):
            nonlocal depth
            self.assertEqual(surface_id, self.target["surface_id"])
            outer = depth == 0
            if outer:
                self.clock.advance(wait_ms)
            if error is not None:
                raise error
            depth += 1
            try:
                yield
            finally:
                depth -= 1
                if outer:
                    self.clock.advance(release_ms)
        self.daemon._surface_lock = acquire

    def test_periodic_stages_exclude_lock_wait_from_reconciliation(self):
        self.surface_lock_cost(wait_ms=7)
        self.assertIs(self.observe(), self.state)
        timing = self.runtime.observation_timings
        self.assertEqual(timing["source"], "periodic")
        self.assertEqual(timing["outcome"], "completed")
        self.assertEqual(timing["stages_ms"], {
            "viewport_classify": 11, "process_identity": 13,
            "surface_lock_wait": 14, "reconcile": 17, "turn_gate": 19,
        })
        self.assertEqual(timing["total_ms"], 74)
        self.assertEqual(timing["unattributed_ms"], 0)
        self.assertEqual(self.runtime.read_duration_ms, 74)
        self.assertEqual(self.runtime.observation_interval_ms, 1074)
        self.assertEqual(self.runtime.observation_completed_at, timing["completed_at"])

    def test_native_total_includes_outer_lock_and_admission_but_legacy_scope_does_not(self):
        self.surface_lock_cost(wait_ms=37, release_ms=41)
        self.observe("native")
        timing = self.runtime.observation_timings
        self.assertEqual(timing["source"], "native")
        self.assertEqual(timing["outcome"], "completed")
        self.assertEqual(timing["stages_ms"], {
            "surface_lock_wait": 37, "admission": 31, "tree": 29,
            "viewport_classify": 11, "process_identity": 13,
            "reconcile": 17, "handle_state": 23,
        })
        self.assertEqual(timing["total_ms"], 202)
        self.assertEqual(timing["unattributed_ms"], 41)
        self.assertEqual(self.runtime.read_duration_ms, 93)
        self.assertAlmostEqual(self.runtime.observation_completed_at, 1000.161)
        self.assertAlmostEqual(timing["completed_at"], 1000.202)

    def test_periodic_legacy_scope_includes_policy_read_but_excludes_standby_hint(self):
        self.runtime.native_failure_at = 0
        self.daemon._standby_periodic_hint = Mock()
        self.daemon._standby_periodic_hint.covered.side_effect = self.cost(41, False)
        key = self.daemon._observation_policy.key
        first = True
        def policy_key(target):
            nonlocal first
            if first:
                first = False
                self.clock.advance(37)
            return key(target)
        with patch.object(self.daemon._observation_policy, "key", side_effect=policy_key):
            self.observe()
        self.assertEqual(self.runtime.read_duration_ms, 97)
        self.assertEqual(self.runtime.observation_timings["total_ms"], 138)
        self.assertEqual(self.runtime.observation_timings["unattributed_ms"], 78)

    def test_standby_skip_is_diagnostic_without_refreshing_read_freshness(self):
        self.runtime.native_failure_at = 0
        self.daemon._standby_periodic_hint = Mock()
        self.daemon._standby_periodic_hint.covered.side_effect = self.cost(41, True)
        before = self.metrics()[1:]
        self.assertIsNone(self.observe())
        timing = self.runtime.observation_timings
        self.assertEqual(timing["outcome"], "standby_covered")
        self.assertEqual(timing["total_ms"], 41)
        self.assertEqual(timing["stages_ms"], {})
        self.assertEqual(self.metrics()[1:], before)
        self.daemon._observe_target_viewport.assert_not_called()

    def test_native_rejected_admission_does_not_claim_a_new_read(self):
        self.surface_lock_cost(wait_ms=37)
        self.daemon._active_send_target.side_effect = self.cost(31)
        before = self.metrics()[1:]
        self.observe("native")
        timing = self.runtime.observation_timings
        self.assertEqual(timing["outcome"], "admission_rejected")
        self.assertEqual(timing["total_ms"], 68)
        self.assertEqual(timing["stages_ms"], {"surface_lock_wait": 37, "admission": 31})
        self.assertEqual(self.metrics()[1:], before)
        self.client.tree.assert_not_called()

    def test_native_admission_exception_is_timed_and_propagated(self):
        error = RuntimeError("admission failed")
        self.daemon._active_send_target.side_effect = self.cost(31, error=error)
        before = self.metrics()[1:]
        with self.assertRaises(RuntimeError) as raised:
            self.observe("native")
        self.assertIs(raised.exception, error)
        self.assertEqual(self.runtime.observation_timings["outcome"], "error")
        self.assertEqual(self.runtime.observation_timings["stages_ms"]["admission"], 31)
        self.assertEqual(self.metrics()[1:], before)

    def test_native_lock_failure_is_timed_without_claiming_a_read(self):
        error = RuntimeError("lock failed")
        self.surface_lock_cost(wait_ms=37, error=error)
        before = self.metrics()[1:]
        with self.assertRaises(RuntimeError) as raised:
            self.observe("native")
        self.assertIs(raised.exception, error)
        timing = self.runtime.observation_timings
        self.assertEqual(timing["outcome"], "error")
        self.assertEqual(timing["stages_ms"], {"surface_lock_wait": 37})
        self.assertEqual(timing["total_ms"], 37)
        self.assertEqual(self.metrics()[1:], before)
        self.daemon._active_send_target.assert_not_called()

    def test_native_tree_exception_retains_tree_and_legacy_elapsed_time(self):
        error = RuntimeError("tree failed")
        self.client.tree.side_effect = self.cost(29, error=error)
        with self.assertRaises(RuntimeError) as raised:
            self.observe("native")
        self.assertIs(raised.exception, error)
        timing = self.runtime.observation_timings
        self.assertEqual(timing["outcome"], "error")
        self.assertEqual(timing["stages_ms"]["tree"], 29)
        self.assertEqual(timing["total_ms"], 60)
        self.assertEqual(self.runtime.read_duration_ms, 29)
        self.daemon._observe_target_viewport.assert_not_called()

    def test_unhandled_pipeline_errors_record_the_failed_stage(self):
        cases = (
            ("_observe_target_viewport", "viewport_classify", "periodic", 11),
            ("_claude_process_observation", "process_identity", "periodic", 13),
            ("_apply_claude_process_observation", "reconcile", "periodic", 17),
            ("_codex_turn_ready", "turn_gate", "periodic", 19),
            ("_handle_state", "handle_state", "native", 23),
        )
        for method, stage, source, elapsed in cases:
            with self.subTest(stage=stage):
                error = RuntimeError(stage + " failed")
                with patch.object(self.daemon, method, side_effect=self.cost(elapsed, error=error)):
                    with self.assertRaises(RuntimeError) as raised:
                        self.observe(source)
                self.assertIs(raised.exception, error)
                timing = self.runtime.observation_timings
                self.assertEqual(timing["outcome"], "error")
                self.assertEqual(timing["stages_ms"][stage], elapsed)
                self.assertEqual(timing["unattributed_ms"], 0)

    def test_refresh_retry_accumulates_viewport_process_and_reconciliation(self):
        self.client.ping = Mock(side_effect=self.cost(5, True))
        self.daemon._refresh_workspace = Mock(side_effect=self.cost(7, True))
        self.daemon._apply_claude_runtime_guards = Mock(side_effect=[
            core.CmuxError("fixture lookup failed"), self.state,
        ])
        self.assertIs(self.observe(), self.state)
        stages = self.runtime.observation_timings["stages_ms"]
        self.assertEqual(stages["viewport_classify"], 22)
        self.assertEqual(stages["process_identity"], 26)
        self.assertEqual(stages["reconcile"], 34)
        self.assertEqual(stages["ping"], 5)
        self.assertEqual(stages["workspace_refresh"], 7)
        self.assertEqual(self.runtime.observation_timings["total_ms"], 113)
        self.assertEqual(self.runtime.observation_timings["unattributed_ms"], 0)

    def test_failed_initial_and_retry_reads_accumulate_ping_and_viewport(self):
        self.daemon._observe_target_viewport.side_effect = self.cost(
            11, error=core.CmuxError("fixture lookup failed"))
        self.client.ping = Mock(side_effect=self.cost(5, True))
        self.daemon._refresh_workspace = Mock(side_effect=self.cost(7, True))
        self.daemon._pause_missing_or_error = Mock()
        self.observe()
        stages = self.runtime.observation_timings["stages_ms"]
        self.assertEqual(stages["viewport_classify"], 22)
        self.assertEqual(stages["ping"], 10)
        self.assertEqual(stages["workspace_refresh"], 7)
        self.daemon._pause_missing_or_error.assert_called_once()

    def test_working_observation_does_not_invent_unused_stage_timings(self):
        self.daemon._observe_target_viewport.side_effect = self.cost(
            11, core.ScreenState("working", message_kind="codex"))
        self.assertIsNone(self.observe())
        timing = self.runtime.observation_timings
        self.assertEqual(timing["outcome"], "completed")
        self.assertEqual(timing["total_ms"], 28)
        self.assertNotIn("process_identity", timing["stages_ms"])
        self.assertNotIn("turn_gate", timing["stages_ms"])
        self.assertNotIn("handle_state", timing["stages_ms"])
        self.assertEqual(self.runtime.observed_state, "working")

    def test_older_periodic_completion_cannot_replace_newer_native_metrics(self):
        latest = []
        first = True
        def viewport(*args):
            nonlocal first
            if first:
                first = False
                self.clock.advance(7)
                self.observe("native")
                latest.append(self.metrics())
                self.clock.advance(101)
            else:
                self.clock.advance(11)
            return self.state
        self.daemon._observe_target_viewport.side_effect = viewport
        self.observe()
        self.assertEqual(self.metrics(), latest[0])
        self.assertEqual(self.runtime.observation_timings["source"], "native")

    def test_older_native_completion_cannot_replace_newer_periodic_metrics(self):
        latest = []
        def tree():
            self.observe()
            latest.append(self.metrics())
            self.clock.advance(101)
            return {}
        self.client.tree.side_effect = tree
        self.observe("native")
        self.assertEqual(self.metrics(), latest[0])
        self.assertEqual(self.runtime.observation_timings["source"], "periodic")

    def test_invalidated_observations_cannot_publish_metrics(self):
        for source in ("periodic", "native"):
            with self.subTest(source=source):
                current = [True]
                def viewport(*args):
                    self.clock.advance(11)
                    current[0] = False
                    return self.state
                self.daemon._observe_target_viewport.side_effect = viewport
                before = self.metrics()
                self.observe(source, lambda: current[0])
                self.assertEqual(self.metrics(), before)

    def test_policy_replacement_revokes_timing_publication(self):
        for source in ("periodic", "native"):
            with self.subTest(source=source):
                policy = self.daemon._observation_policy = Mock()
                policy.key.return_value = object()
                def viewport(*args):
                    self.clock.advance(11)
                    policy.key.return_value = object()
                    return self.state
                self.daemon._observe_target_viewport.side_effect = viewport
                before = self.metrics()
                self.observe(source)
                self.assertEqual(self.metrics(), before)

    def test_replaced_runtime_is_not_updated_by_an_old_observation(self):
        for source in ("periodic", "native"):
            with self.subTest(source=source):
                self.daemon.runtime[self.target["surface_id"]] = self.runtime
                replacement = core.TargetRuntime(read_duration_ms=456)
                before, replacement_before = self.metrics(), self.metrics(replacement)
                def process(*args, **kwargs):
                    self.clock.advance(11)
                    self.daemon.runtime[self.target["surface_id"]] = replacement
                with patch.object(self.daemon, "_process_one_target", side_effect=process):
                    self.observe(source)
                self.assertEqual(self.metrics(), before)
                self.assertEqual(self.metrics(replacement), replacement_before)

    def test_never_started_observation_keeps_prior_metrics(self):
        for source in ("periodic", "native"):
            with self.subTest(source=source):
                before = self.metrics()
                self.observe(source, lambda: False)
                self.assertEqual(self.metrics(), before)
        self.daemon._observation_client.assert_not_called()
        self.daemon._observe_target_viewport.assert_not_called()

    def test_snapshot_serializes_without_the_private_generation_token(self):
        self.observe()
        persisted = json.loads(json.dumps(self.runtime.to_dict()))
        self.assertNotIn("_observation_timing_token", persisted)
        restored = core.TargetRuntime.from_dict(persisted)
        self.assertEqual(self.metrics(restored), self.metrics())
        self.assertEqual(core.TargetRuntime.from_dict({}).observation_timings, {})

    def test_process_once_callers_can_omit_timing_collection(self):
        candidate = self.daemon._process_one_target(
            self.target, self.client, None, "", defer_send=True)
        self.assertIs(candidate, self.state)
        self.assertEqual(self.runtime.observation_timings, {"source": "baseline"})


if __name__ == "__main__":
    unittest.main()
