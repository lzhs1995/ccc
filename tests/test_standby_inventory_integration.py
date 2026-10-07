"""Actual private worker with preparation/identity policy, no native clients."""
import hashlib
import json
import os
import sys
import unittest
from unittest.mock import Mock, patch

import ccc_standby_inventory as inventory
import ccc_standby_prepare as prep
from tests import test_standby_prepare as prep_fixture
from tests import test_standby_identity as identity_fixture


class OwnerInventoryIntegrationTests(unittest.TestCase):
    setUp = prep_fixture.PreparationTests.setUp

    def new_owner(self):
        directory = self.root / 'new-owner'
        directory.mkdir(mode=0o700)
        return prep.PreparationOwner(self.config, self.job['id'], directory=directory,
            client=self.client, source_pin=self.pin, sessions_root=self.sessions,
            target_environment={'HOME': str(self.root)})

    def real_reader(self):
        reader = inventory.ProcessInventoryReader()
        self.addCleanup(reader.close)
        return reader

    def test_precheck_failure_starts_no_worker(self):
        self.pin.current.return_value = 'different-generation'
        with patch.object(prep, 'ProcessInventoryReader') as spawn:
            with self.assertRaisesRegex(ValueError, 'generation changed'):
                self.new_owner()
        spawn.assert_not_called()
        self.pin.close.assert_called_once()

    def test_worker_start_failure_closes_pin_before_any_inventory(self):
        self.inventory.reset_mock()
        with patch.object(prep, 'ProcessInventoryReader', side_effect=OSError('spawn refused')):
            with self.assertRaisesRegex(OSError, 'spawn refused'):
                self.new_owner()
        self.inventory.assert_not_called()
        self.pin.close.assert_called_once()

    def test_rollout_initialization_failure_reaps_real_worker(self):
        worker = self.real_reader()
        child = worker._child
        with patch.object(prep, 'ProcessInventoryReader', return_value=worker), \
                patch.object(prep, 'RolloutInventory', side_effect=OSError('rollout refused')):
            with self.assertRaisesRegex(OSError, 'rollout refused'):
                self.new_owner()
        self.assertIsNotNone(child.poll())
        self.assertTrue(child.stdin.closed and child.stdout.closed)
        self.pin.close.assert_called_once()

    def test_bridge_initialization_failure_releases_inventory_and_pin(self):
        worker = self.real_reader()
        child = worker._child
        with patch.object(prep, 'ProcessInventoryReader', return_value=worker), \
                patch.object(prep, 'GenerationBridge', side_effect=OSError('bridge refused')):
            with self.assertRaisesRegex(OSError, 'bridge refused'):
                self.new_owner()
        self.assertIsNotNone(child.poll())
        self.rollouts.close.assert_called_once()
        self.pin.close.assert_called_once()

    def test_real_reader_through_owner_is_fresh_and_closed_with_owner(self):
        if sys.platform != 'darwin':
            self.skipTest('real Darwin libproc')
        worker = self.real_reader()
        child = worker._child
        self.owner._inventory_process = worker
        file = self.root / 'writer.jsonl'
        with file.open('wb') as handle:
            info = os.fstat(handle.fileno())
            self.assertEqual(self.owner._files_reader(os.getpid(), identities=True)[file],
                             {'device': info.st_dev, 'inode': info.st_ino})
        self.assertNotIn(file, self.owner._files_reader(os.getpid(), identities=True))
        self.assertEqual(worker._sequence, 2)
        self.assertEqual(self.owner._files_reader.capacity, 1)
        self.assertFalse(self.owner._files_reader.waiters)
        self.owner.close()
        self.assertIsNotNone(child.poll())
        with self.assertRaisesRegex(ValueError, 'cancelled or closed'):
            self.owner._files_reader(os.getpid())

    def test_lifetime_revocation_after_real_response_rejects_result(self):
        worker = self.real_reader()
        allowed = [True]
        self.owner.bind_lifetime_guard(lambda: allowed[0])
        def read(*args, **kwargs):
            result = worker(*args, **kwargs)
            allowed[0] = False
            return result
        self.owner._inventory_process = read
        self.addCleanup(setattr, self.owner, '_inventory_process', worker)
        with self.assertRaisesRegex(ValueError, 'cancelled or closed'):
            self.owner._files_reader(os.getpid(), identities=True)
        self.assertEqual(worker._sequence, 1)
        self.assertEqual(self.owner._files_reader.capacity, 1)
        self.assertFalse(self.writes)

    def test_bridge_close_error_still_closes_worker_and_source_pin(self):
        worker = self.real_reader()
        child = worker._child
        self.owner._inventory_process = worker
        self.owner.bridge.close = Mock(side_effect=OSError('bridge close failed'))
        try:
            with self.assertRaisesRegex(OSError, 'bridge close failed'):
                self.owner.close()
        finally:
            self.owner.bridge.close.side_effect = None
        self.assertIsNotNone(child.poll())
        self.rollouts.close.assert_called_once()
        self.pin.close.assert_called_once()


class OriginalInventoryIntegrationTests(unittest.TestCase):
    file_identity = identity_fixture.StandbyIdentityTests.file_identity
    save_events = identity_fixture.StandbyIdentityTests.save_events
    inspect = identity_fixture.StandbyIdentityTests.inspect

    def setUp(self):
        if sys.platform != 'darwin':
            self.skipTest('real Darwin libproc')
        identity_fixture.StandbyIdentityTests.setUp(self)
        self.claim['bootstrap_pid'] = os.getpid()
        self.process['pid'] = os.getpid()
        self.claim_path.write_text(json.dumps(self.claim))
        self.sha = hashlib.sha256(self.claim_path.read_bytes()).hexdigest()
        self.lock_handle = self.lock.open('ab')
        self.log_handle = self.tui.open('ab')
        self.addCleanup(self.lock_handle.close)
        self.addCleanup(self.log_handle.close)
        self.reader = inventory.ProcessInventoryReader()
        self.addCleanup(self.reader.close)

    def test_original_identity_join_uses_two_fresh_real_worker_reads(self):
        result = self.inspect(files_reader=self.reader)
        self.assertEqual(result['session_id'], self.session)
        self.assertEqual(result['writer_identity'], self.file_identity(self.lock))
        self.assertEqual(self.reader._sequence, 2)
        self.assertFalse(result['readiness_proven'])

    def test_writer_closes_between_observations_cannot_publish_identity(self):
        with self.assertRaises(ValueError):
            self.inspect(files_reader=self.reader,
                         connected_check=lambda _identity: self.lock_handle.close())
        self.assertEqual(self.reader._sequence, 2)

    def test_process_changes_after_first_response_is_refused(self):
        def change(_identity):
            self.process['birth'][1] += 1
        with self.assertRaises(ValueError):
            self.inspect(files_reader=self.reader, connected_check=change)


if __name__ == '__main__':
    unittest.main()
