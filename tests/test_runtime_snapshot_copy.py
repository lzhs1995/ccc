"""Durable runtime snapshots keep unknown transactions and independent evidence."""
import dataclasses
import json
import unittest

from cmux_codex_watch import TargetRuntime


class RuntimeSnapshotCopyTests(unittest.TestCase):
    def runtime(self):
        return TargetRuntime(
            delivery_status='unknown', send_attempt_id='original-attempt',
            claude_submit_phase='text_written', claude_submit_write_unknown=True,
            claude_submit_native_identity={'process': {'birth': [100, 25], 'pid': 1234},
                                          'target': {'surface_id': 'original-surface'}},
            codex_input_not_sent={'phase': 'original', 'evidence': [{'offset': 10}]},
            observation_timings={'read': [0.1, 0.2]},
            claude_hook_latency_ms=[2.0, 3.0])

    def test_snapshot_matches_existing_wire_schema_and_ignores_ephemeral_state(self):
        runtime = self.runtime()
        runtime._jev_pending_claude = {'event_id': 'memory-only'}
        snapshot = runtime.to_dict()
        self.assertEqual(snapshot, dataclasses.asdict(runtime))
        self.assertNotIn('_jev_pending_claude', snapshot)
        restored = TargetRuntime.from_dict(json.loads(json.dumps(snapshot)))
        self.assertEqual(restored.to_dict(), snapshot)
        self.assertTrue(restored.claude_submit_write_unknown)
        self.assertEqual(restored.send_attempt_id, 'original-attempt')

    def test_snapshot_and_live_evidence_cannot_mutate_each_other(self):
        runtime = self.runtime()
        snapshot = runtime.to_dict()
        snapshot['claude_submit_native_identity']['process']['birth'][0] = 200
        snapshot['codex_input_not_sent']['evidence'][0]['offset'] = 20
        self.assertEqual(runtime.claude_submit_native_identity['process']['birth'], [100, 25])
        self.assertEqual(runtime.codex_input_not_sent['evidence'][0]['offset'], 10)
        runtime.observation_timings['read'].append(0.3)
        runtime.claude_hook_latency_ms.append(4.0)
        self.assertEqual(snapshot['observation_timings']['read'], [0.1, 0.2])
        self.assertEqual(snapshot['claude_hook_latency_ms'], [2.0, 3.0])


if __name__ == '__main__':
    unittest.main()
