"""Keep slow native initialization outside the finite-check HTTP cohort."""
import copy
import unittest
import uuid

import ccc_access_service as service
import ccc_workspace_batch as batch
import cmux_codex_watch as core
from tests import test_workspace_batch as fixtures


class AccessBatchPreparationTests(unittest.TestCase):
    def setUp(self):
        fixtures.WorkspaceBatchTests.setUp(self)
        # Descriptor-only setup works on both supported test interpreters and
        # does not launch a gateway, native process or network connection.
        policy = service.Policy(self.wid, self.worker.job['id'])
        descriptor = {'version': service.VERSION, 'mode': service.MODE,
            'config_path': str(self.config.resolve()), 'policy': service.asdict(policy)}
        service.create_private(self.worker.path.parent / 'access.json', descriptor)
        self.worker.job.update(access_mode=service.MODE, access_policy={
            'mode': service.MODE, 'version': service.VERSION, 'max_attempts': policy.max_attempts,
            'max_output_tokens': policy.max_output_tokens, 'descriptor_sha256': service.descriptor_sha(descriptor)})
        self.worker.save()
        create = self.client.new_codex_surface
        def create_with_birth(*args):
            sid = create(*args)
            self.client.states[sid]['process_start'] = self.now - 1
            return sid
        self.client.new_codex_surface = create_with_birth

    def test_native_preparation_can_exceed_http_deadline_without_sending_any_prompt(self):
        began = self.now
        first_sent = []
        def on_send():
            self.assertTrue(batch.access_cohort_prepared(self.worker.job))
            first_sent.append(self.now)
        self.client.on_send = on_send
        for _ in range(120):
            self.now += 10  # Fair allocation among twenty workspaces is slower.
            more = self.worker.step()
            if not batch.access_cohort_prepared(self.worker.job):
                self.assertEqual(self.client.sent, [])
            if not more:
                break
        self.assertEqual(len(self.client.sent), 50)
        self.assertEqual(batch.counts(self.worker.job)['started'], 50)
        self.assertGreater(first_sent[0] - began, 490)
        self.assertEqual(len(list(self.worker.path.parent.glob('access-session-*.json'))), 50)
        self.assertFalse((self.worker.path.parent / 'access-journal.jsonl').exists())

    def test_prepared_sessions_release_cold_start_capacity_before_any_model_request(self):
        began = self.now
        while self.now - began < 10 and len(self.client.calls) < 6:
            self.now += .5
            self.worker.step()
        self.assertGreaterEqual(len(self.client.calls), 6)
        self.assertLess(self.now - began, batch.STARTUP_LEASE_SEC)
        self.assertEqual(self.client.sent, [])
        self.assertGreaterEqual(sum(s['phase'] == 'access_ready' for s in self.worker.job['slots']), 4)

    def test_paused_or_changed_prepared_session_cannot_receive_the_initial_prompt(self):
        # Reach the durable barrier without taking the next submission step.
        while not batch.access_cohort_prepared(self.worker.job):
            self.now += 1
            self.worker.step()
        self.assertEqual(self.client.sent, [])
        slot = self.worker.job['slots'][0]
        self.store.mutate(lambda value: value['workspace_rules'][0].update(paused=True))
        self.worker.step()
        self.assertEqual(self.client.sent, [])
        self.store.mutate(lambda value: value['workspace_rules'][0].update(paused=False))
        slot = self.worker.job['slots'][-1]
        self.client.states[slot['surface_id']]['session_id'] = str(uuid.uuid4())
        self.now += 1
        self.worker.step()
        self.assertEqual(self.client.sent, [])

    def test_incomplete_or_duplicate_prepared_slots_do_not_release_the_batch(self):
        slots = [{'index': i, 'surface_id': str(uuid.uuid4()), 'access_ready_at': 1,
                  'phase': 'access_ready'} for i in range(50)]
        self.assertTrue(batch.access_cohort_prepared({'slots': slots}))
        self.assertFalse(batch.access_cohort_prepared({'slots': slots[:-1]}))
        duplicate = copy.deepcopy(slots)
        duplicate[-1] = dict(duplicate[0])
        self.assertFalse(batch.access_cohort_prepared({'slots': duplicate}))

    def test_moved_prepared_surface_prevents_the_cohort_without_a_replacement(self):
        while not batch.access_cohort_prepared(self.worker.job):
            self.now += 1
            self.worker.step()
        removed = self.worker.job['slots'][0]['surface_id']
        self.client.calls.remove(removed)
        self.now += 6
        self.worker.step()
        self.assertEqual(self.worker.job['status'], 'waiting')
        self.assertEqual(self.worker.job['slots'][0]['surface_id'], removed)
        self.assertEqual(len(self.client.calls), 49)
        self.assertEqual(self.client.sent, [])

    def test_worker_restart_preserves_half_prepared_and_submitted_sessions(self):
        for _ in range(10):
            self.now += 1
            self.worker.step()
        original_ids = set(self.client.calls)
        self.assertGreater(len(original_ids), 4)
        self.assertEqual(self.client.sent, [])
        old = self.worker
        self.worker = batch.BatchWorker(self.config, old.job['id'], client=self.client, queue=self.client,
                                       clock=lambda: self.now, pty_probe=lambda: True)
        self.addCleanup(self.worker.cache.close)
        for _ in range(120):
            self.now += 1
            if not self.worker.step():
                break
        self.assertEqual(len(self.client.calls), 50)
        self.assertTrue(original_ids <= set(self.client.calls))
        self.assertEqual(len(self.client.sent), 50)
        restarted = batch.BatchWorker(self.config, old.job['id'], client=self.client, queue=self.client,
                                       clock=lambda: self.now, pty_probe=lambda: True)
        self.addCleanup(restarted.cache.close)
        self.assertFalse(restarted.step())
        self.assertEqual(len(self.client.calls), 50)
        self.assertEqual(len(self.client.sent), 50)


if __name__ == '__main__':
    unittest.main()
