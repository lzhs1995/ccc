"""Real private plans/settlement validation; synthetic native/UI fixture data."""
import copy
import json
import unittest
import uuid
from unittest.mock import patch

import ccc_standby_settlement as settlement
from tests import test_standby_settlement as fixtures
from tools import standby_run_manifest as manifest


class RunManifestTests(unittest.TestCase):
    start_native = fixtures.SettlementTests.start_native
    hook_fixture = fixtures.SettlementTests.hook_fixture
    bind = fixtures.SettlementTests.bind
    task = fixtures.SettlementTests.task
    stamp = fixtures.SettlementTests.stamp
    complete = fixtures.SettlementTests.complete

    def setUp(self):
        fixtures.SettlementTests.setUp(self)
        self.directory = self.worker.path.parent.resolve()/'run-evidence'
        self.directory.mkdir(mode=0o700)
        self.batch_id = str(uuid.uuid4())
        self.batches = [dict(batch_id=self.batch_id, workspace_id=self.wid, mode='b', slots=50)]

    def declare(self, batches=None, at=9):
        return manifest.declare(self.directory, self.batches if batches is None else batches,
                                clock=lambda: self.stamp(at))

    def settle(self):
        self.complete()
        return settlement.settle(self.bridge, authorized=lambda: True)

    def bind_run(self, batch_id=None):
        return manifest.RunManifest(self.directory).bind(batch_id or self.batch_id,
            self.config, self.worker.job['id'], clock=lambda: self.stamp(12))

    def test_declared_five_workspaces_two_batches_each_preserves_500(self):
        batches = [dict(batch_id=str(uuid.uuid4()), workspace_id=wid, mode='b', slots=50)
                   for wid in [str(uuid.uuid4()).upper() for _ in range(5)] for _ in range(2)]
        plan = self.declare(batches)
        self.assertEqual(plan['planned_sessions'], 500)
        self.assertEqual(sorted(plan['workspace_slots'].values()), [100]*5)
        self.assertTrue(all('action_id' not in row for row in plan['batches']))
        with self.assertRaises((OSError, ValueError)):
            self.declare(batches[:1])
        self.assertEqual(manifest.RunManifest(self.directory).plan, plan)

    def test_real_settlement_binds_original_ui_once_and_restart_preserves_time(self):
        self.declare()
        saved = self.settle()
        first = self.bind_run()
        self.assertEqual(first['action_id'], self.action)
        self.assertEqual(first['activation_ui_sha256'], saved['activation_ui_sha256'])
        with patch.object(manifest, 'write_once', side_effect=AssertionError('must not rewrite')):
            self.assertEqual(self.bind_run(), first)
        self.assertFalse(self.client.sent)

    def test_plan_after_real_input_cannot_retroactively_claim_coverage(self):
        self.declare(at=10)
        self.settle()
        with self.assertRaises(ValueError): self.bind_run()
        self.assertFalse(list(self.directory.glob('batch-*.json')))

    def test_original_action_cannot_fill_two_declared_batches(self):
        second = dict(self.batches[0], batch_id=str(uuid.uuid4()))
        self.declare(self.batches+[second])
        self.settle()
        self.bind_run()
        with self.assertRaisesRegex(ValueError, 'already consumed'):
            self.bind_run(second['batch_id'])

    def test_unknown_batch_and_wrong_workspace_reject(self):
        self.declare([dict(self.batches[0], workspace_id=str(uuid.uuid4()))])
        self.settle()
        with self.assertRaises(ValueError): self.bind_run(str(uuid.uuid4()))
        with self.assertRaises(ValueError): self.bind_run()

    def test_changed_plan_and_original_ui_reject(self):
        self.declare()
        reader = manifest.RunManifest(self.directory)
        plan = copy.deepcopy(reader.plan)
        plan['batches'] = []
        reader.path.write_text(json.dumps(plan))
        with self.assertRaises(ValueError): reader.current()
        reader.path.write_bytes(reader.raw)
        self.settle()
        self.bridge.receipt.write_text('{}')
        with self.assertRaises((KeyError, ValueError)): self.bind_run()

    def test_declared_topology_duplicate_case_and_slot_count_reject(self):
        duplicate = dict(self.batches[0])
        variants = [[], self.batches+[duplicate], [dict(duplicate, slots=49)],
                    [dict(duplicate, mode='B')],
                    [dict(duplicate, workspace_id=self.wid.lower()),
                     dict(duplicate, batch_id=str(uuid.uuid4()), workspace_id=self.wid.upper())]]
        for rows in variants:
            with self.subTest(rows=rows), self.assertRaises(ValueError): self.declare(rows)

    def test_changed_settlement_during_final_read_rejects(self):
        self.declare()
        self.settle()
        original = manifest.read_settlement
        calls = []
        def changed(*args):
            value, sha = original(*args)
            calls.append(True)
            return value, ('0'*64 if len(calls) == 2 else sha)
        with patch.object(manifest, 'read_settlement', side_effect=changed):
            with self.assertRaisesRegex(ValueError, 'changed before binding'):
                self.bind_run()
        self.assertFalse(list(self.directory.glob('batch-*.json')))

    def test_storage_failure_never_claims_binding(self):
        self.declare()
        self.settle()
        with patch.object(manifest, 'write_once', side_effect=OSError('full')):
            with self.assertRaises(OSError): self.bind_run()
        self.assertFalse(list(self.directory.glob('batch-*.json')))

    def test_existing_binding_cannot_be_reused_under_changed_plan(self):
        self.declare()
        self.settle()
        self.bind_run()
        path = self.directory/f'batch-{self.batch_id}.json'
        value = json.loads(path.read_text())
        value['plan_sha256'] = '0'*64
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, 'differs from original run plan'):
            self.bind_run()

    def test_competing_binder_cannot_write_while_lock_held(self):
        self.declare()
        self.settle()
        with manifest.core.FileLock(self.directory/'run-bind.lock'):
            with self.assertRaises(RuntimeError): self.bind_run()
        self.assertFalse(list(self.directory.glob('batch-*.json')))

    def test_binding_changed_during_settlement_revalidation_is_rejected(self):
        self.declare()
        self.settle()
        self.bind_run()
        path = self.directory/f'batch-{self.batch_id}.json'
        original = manifest.read_settlement
        calls = []
        def changed(*args):
            result = original(*args)
            calls.append(True)
            if len(calls) == 2:
                value = json.loads(path.read_text())
                value['action_id'] = str(uuid.uuid4())
                path.write_text(json.dumps(value))
            return result
        with patch.object(manifest, 'read_settlement', side_effect=changed):
            with self.assertRaisesRegex(ValueError, 'binding changed'):
                self.bind_run()

    def test_binding_removed_during_revalidation_is_not_recreated(self):
        self.declare()
        self.settle()
        self.bind_run()
        path = self.directory/f'batch-{self.batch_id}.json'
        original = manifest.read_settlement
        calls = []
        def removed(*args):
            result = original(*args)
            calls.append(True)
            if len(calls) == 2:
                path.unlink()
            return result
        with patch.object(manifest, 'read_settlement', side_effect=removed):
            with self.assertRaises((ValueError, OSError)):
                self.bind_run()
        self.assertFalse(path.exists())

    def test_malformed_declaration_never_writes_plan(self):
        for rows in (None, {}, [None], [{}], [dict(self.batches[0], slots='50')]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                manifest.declare(self.directory, rows, clock=lambda: self.stamp(9))
        self.assertFalse((self.directory/'run-plan.json').exists())

    def resolve(self):
        return manifest.RunManifest(self.directory).resolve(clock=lambda: self.stamp(13))

    def test_resolve_original_complete_set_and_restart_without_rewriting(self):
        self.declare()
        self.settle()
        binding = self.bind_run()
        result = self.resolve()
        self.assertEqual(result['planned_sessions'], 50)
        self.assertEqual(result['batches'][0]['binding'], binding)
        self.assertFalse(result['run_terminal'])
        with patch.object(manifest, 'write_once', side_effect=AssertionError('rewrite')):
            self.assertEqual(self.resolve(), result)
        self.assertFalse(self.client.sent)

    def test_resolve_missing_second_batch_cannot_shrink_scope(self):
        self.declare(self.batches+[dict(self.batches[0], batch_id=str(uuid.uuid4()))])
        self.settle()
        self.bind_run()
        with self.assertRaisesRegex(ValueError, 'complete declared'):
            self.resolve()
        self.assertFalse((self.directory/'run-bindings.json').exists())

    def test_resolve_duplicate_job_in_second_binding_rejected(self):
        other = dict(self.batches[0], batch_id=str(uuid.uuid4()))
        self.declare(self.batches+[other])
        self.settle()
        value = self.bind_run()
        value['batch_id'] = other['batch_id']
        manifest.write_once(self.directory/f"batch-{other['batch_id']}.json", value)
        with self.assertRaisesRegex(ValueError, 'duplicate original'):
            self.resolve()
        self.assertFalse((self.directory/'run-bindings.json').exists())

    def test_resolve_reopens_originals_even_after_prior_success(self):
        self.declare()
        self.settle()
        self.bind_run()
        self.resolve()
        self.bridge.receipt.write_text('{}')
        with self.assertRaises((ValueError, KeyError)):
            self.resolve()

    def test_resolve_rejects_binding_changed_during_last_original_read(self):
        self.declare()
        self.settle()
        self.bind_run()
        path = self.directory/f'batch-{self.batch_id}.json'
        original = manifest.read_settlement
        calls = []
        def changed(*args):
            result = original(*args)
            calls.append(True)
            if len(calls) == 2:
                path.write_text('{}')
            return result
        with patch.object(manifest, 'read_settlement', side_effect=changed):
            with self.assertRaisesRegex(ValueError, 'bindings changed'):
                self.resolve()
        self.assertFalse((self.directory/'run-bindings.json').exists())

    def test_resolve_unplanned_extra_binding_and_storage_failure_reject(self):
        self.declare()
        self.settle()
        self.bind_run()
        extra = self.directory/f'batch-{uuid.uuid4()}.json'
        extra.write_text('{}')
        with self.assertRaisesRegex(ValueError, 'complete declared'):
            self.resolve()
        extra.unlink()
        with patch.object(manifest, 'write_once', side_effect=OSError('full')):
            with self.assertRaises(OSError): self.resolve()
        self.assertFalse((self.directory/'run-bindings.json').exists())

    def test_resolved_timestamp_cannot_precede_last_binding(self):
        self.declare()
        self.settle()
        self.bind_run()
        result = self.resolve()
        result['resolved'] = self.stamp(11)
        (self.directory/'run-bindings.json').write_text(json.dumps(result))
        with self.assertRaises(ValueError): self.resolve()


if __name__ == '__main__':
    unittest.main()
