"""Synthetic fifty-task receipts; real private files, locks and validation."""
import copy
import unittest
import uuid
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import cmux_codex_watch as core
import ccc_standby_settlement as settlement
from ccc_standby_timing import _read
from tests import test_standby_timing as fixtures


class SettlementTests(unittest.TestCase):
    setUp = fixtures.TimingTests.setUp
    start_native = fixtures.TimingTests.start_native
    hook_fixture = fixtures.TimingTests.hook_fixture
    bind = fixtures.TimingTests.bind
    task = fixtures.TimingTests.task
    stamp = fixtures.TimingTests.stamp

    def complete(self):
        self.bridge.committed()
        self.task(turn=str(uuid.uuid4()))
        self.observer.poll(0, release=True)
        root = self.worker.path.parent.resolve()
        task, _ = _read(root / 'standby-first-task-0.json')
        observation, _ = _read(root / 'standby-first-observation-0.json')
        # These are synthetic first-task observations, not native evidence.
        for i, original in enumerate(self.observer.originals[1:], 1):
            t, o = copy.deepcopy(task), copy.deepcopy(observation)
            turn = str(uuid.uuid4())
            t.update(original=original)
            t['confirmation'].update(session_id=original['session_id'], task_id=turn)
            o.update(original=original, task_id=turn)
            core.atomic_write_json(root / f'standby-first-task-{i}.json', t)
            core.atomic_write_json(root / f'standby-first-observation-{i}.json', o)
        self.bridge.clock = lambda: self.stamp(11)
        self.bridge.finish(outcome='complete')

    def test_complete_released_originals_settle_without_job_mutation(self):
        self.complete()
        before = self.worker.path.read_bytes()
        record = settlement.settle(self.bridge, authorized=lambda: True)
        self.assertFalse(record['run_terminal'])
        self.assertFalse(record['job_terminal'])
        self.assertEqual(settlement.read_settlement(self.config, self.worker.job['id'])[0], record)
        self.assertEqual(settlement.settle(self.bridge, authorized=lambda: True), record)
        self.assertEqual(self.worker.path.read_bytes(), before)
        self.assertFalse(self.client.sent)

    def test_ack_without_complete_terminal_cannot_settle(self):
        self.bridge.committed()
        self.bridge.clock = lambda: self.stamp(11)
        self.bridge.finish(outcome='timeout', reason='no native tasks')
        with self.assertRaises(ValueError):
            settlement.settle(self.bridge, authorized=lambda: True)
        self.assertFalse((self.bridge.directory / 'submission-settled.json').exists())

    def test_hold_and_pause_refuse_even_after_fifty_task_receipts(self):
        self.complete()
        sid = self.observer.originals[0]['surface_id']
        self.store.mutate(lambda c: c['workspace_rules'][0].setdefault('batch_start_holds', {}).update(
            {sid: {'job_id': self.worker.job['id'], 'index': 0}}))
        with self.assertRaises(ValueError): settlement.settle(self.bridge, authorized=lambda: True)
        self.store.mutate(lambda c: c['workspace_rules'][0]['batch_start_holds'].clear())
        self.store.mutate(lambda c: c.update(global_paused=True))
        with self.assertRaises(ValueError): settlement.settle(self.bridge, authorized=lambda: True)

    def test_revocation_and_storage_failure_never_produce_settlement(self):
        self.complete()
        with self.assertRaises(ValueError): settlement.settle(self.bridge, authorized=lambda: False)
        with patch.object(settlement, 'write_once', side_effect=OSError('disk full')):
            with self.assertRaises(OSError): settlement.settle(self.bridge, authorized=lambda: True)
        self.assertFalse((self.bridge.directory / 'submission-settled.json').exists())

    def test_later_receipt_or_transcript_replacement_rejects(self):
        self.complete()
        settlement.settle(self.bridge, authorized=lambda: True)
        self.transcript.write_bytes(b'replaced original transcript\n')
        with self.assertRaises(ValueError):
            settlement.read_settlement(self.config, self.worker.job['id'])

    def test_persisted_settlement_cannot_claim_run_terminal(self):
        self.complete()
        record = settlement.settle(self.bridge, authorized=lambda: True)
        record['run_terminal'] = True
        core.atomic_write_json(self.bridge.directory / 'submission-settled.json', record)
        with self.assertRaises(ValueError):
            settlement.read_settlement(self.config, self.worker.job['id'])

    def endpoint(self):
        from ccc_standby_service import ServiceEndpoint
        self.complete()
        record = settlement.settle(self.bridge, authorized=lambda: True)
        state = dict(self.bridge.selected, state='first_tasks_observed',
            submission_settlement=record, job_terminal=False, run_terminal=False)
        temporary = tempfile.TemporaryDirectory(prefix='ccc-settle-', dir='/tmp')
        self.addCleanup(temporary.cleanup)
        endpoint = ServiceEndpoint(SimpleNamespace(selected=self.bridge.selected,
            status=lambda: copy.deepcopy(state)), Path(temporary.name).resolve(),
            binding_path=self.bridge.directory / 'owner.json')
        self.addCleanup(endpoint.close)
        return endpoint, state

    def test_live_private_owner_then_closed_owner(self):
        endpoint, state = self.endpoint()
        with patch.object(settlement, 'boot_id', return_value=self.bridge.selected['boot_id']):
            self.assertEqual(settlement.live_settlement(self.config, self.worker.job['id'])[0],
                state['submission_settlement'])
            endpoint.close()
            with self.assertRaises((OSError, ValueError)):
                settlement.live_settlement(self.config, self.worker.job['id'])

    def test_live_owner_must_report_exact_settlement(self):
        _, state = self.endpoint()
        state['submission_settlement']['run_terminal'] = True
        with patch.object(settlement, 'boot_id', return_value=self.bridge.selected['boot_id']):
            with self.assertRaises(ValueError):
                settlement.live_settlement(self.config, self.worker.job['id'])

    def test_boot_change_before_or_during_status_refuses(self):
        self.endpoint()
        original = self.bridge.selected['boot_id']
        for boots in [('other',), (original, 'other')]:
            with self.subTest(boots=boots), patch.object(settlement, 'boot_id', side_effect=boots):
                with self.assertRaises(ValueError):
                    settlement.live_settlement(self.config, self.worker.job['id'])

    def test_read_verification_does_not_recreate_missing_terminal(self):
        self.complete()
        self.bridge.terminal.unlink()
        with self.assertRaises(ValueError):
            self.bridge.finish(outcome='complete', persist=False)
        self.assertFalse(self.bridge.terminal.exists())


if __name__ == '__main__':
    unittest.main()
