"""Real Darwin directory events on small synthetic files; no native launch."""
import copy
import os
from pathlib import Path
import select
import tempfile
import unittest
import uuid
from unittest import mock

from ccc_standby_rollouts import RolloutInventory
from tests import test_standby_identity as identity_fixture


@unittest.skipUnless(hasattr(select, 'kqueue'), 'Darwin kqueue required')
class RolloutInventoryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name).resolve() / 'native'
        self.root = self.home / 'sessions'
        self.root.mkdir(parents=True)
        self.session = str(uuid.uuid4())

    def make(self, **kwargs):
        result = RolloutInventory(self.root, **kwargs)
        self.addCleanup(result.close)
        return result

    def rollout(self, root=None, session=None, compressed=False):
        path = (root or self.root) / ('rollout-old-date-' + (session or self.session)
                                     + '.jsonl' + ('.zst' if compressed else ''))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'{}\n')
        return path

    def test_existing_nested_and_archived_compressed_rollouts_block(self):
        self.rollout(self.root / '2020' / '01' / '02')
        archived = str(uuid.uuid4())
        self.rollout(self.home / 'archived_sessions', archived, True)
        pin = self.make()
        self.assertFalse(pin.absent(self.root, self.session))
        self.assertFalse(pin.absent(self.root, archived))
        self.assertTrue(pin.absent(self.root, str(uuid.uuid4())))

    def test_quiet_hot_checks_do_not_scan_history_or_read_transcripts(self):
        self.rollout(self.root / '2021' / '10', str(uuid.uuid4()))
        pin = self.make()
        with (mock.patch('os.scandir', side_effect=AssertionError('history scan')),
              mock.patch.object(Path, 'read_bytes', side_effect=AssertionError('transcript read'))):
            for _ in range(20):
                self.assertTrue(pin.absent(self.root, self.session))

    def test_closed_writer_after_capture_is_seen_and_cannot_revive(self):
        pin = self.make()
        path = self.rollout()
        self.assertFalse(pin.absent(self.root, self.session))
        path.unlink()
        self.assertFalse(pin.absent(self.root, self.session))

    def test_unrelated_active_session_does_not_invalidate_other_slots(self):
        pin = self.make()
        other = str(uuid.uuid4())
        self.rollout(session=other)
        self.assertTrue(pin.absent(self.root, self.session))
        self.assertFalse(pin.absent(self.root, other))
        self.rollout()
        self.assertFalse(pin.absent(self.root, self.session))

    def test_native_home_sibling_directory_creation_is_not_rollout_change(self):
        pin = self.make()
        sibling = self.home.parent / '.cmuxterm'
        sibling.mkdir()
        self.assertTrue(pin.absent(self.root, self.session))
        sibling.rmdir()
        self.assertTrue(pin.absent(self.root, self.session))
        self.rollout()
        self.assertFalse(pin.absent(self.root, self.session))

    def test_ancestor_permission_round_trip_still_invalidates(self):
        pin = self.make()
        ancestor = self.home.parent
        mode = ancestor.stat().st_mode & 0o777
        ancestor.chmod(mode ^ 0o020)
        ancestor.chmod(mode)
        with self.assertRaises(ValueError):
            pin.absent(self.root, self.session)
        with self.assertRaises(ValueError):
            pin.absent(self.root, self.session)

    def test_future_date_directories_and_late_archive_are_monitored(self):
        pin = self.make()
        self.rollout(self.root / '2031' / '01' / '01')
        self.assertFalse(pin.absent(self.root, self.session))
        archived = str(uuid.uuid4())
        self.rollout(self.home / 'archived_sessions', archived, True)
        self.assertFalse(pin.absent(self.root, archived))

    def test_only_changed_directory_is_rescanned(self):
        old = self.root / 'old'
        self.rollout(old, str(uuid.uuid4()))
        recent = self.root / 'recent'
        recent.mkdir()
        pin = self.make()
        calls = []
        scan = pin._scan_tree
        def recording(path, budget):
            calls.append(path)
            return scan(path, budget)
        self.rollout(recent)
        with mock.patch.object(pin, '_scan_tree', side_effect=recording):
            self.assertFalse(pin.absent(self.root, self.session))
        self.assertEqual(calls, [recent])

    def test_creation_between_arming_and_inventory_is_included(self):
        arm = RolloutInventory._arm
        def changing(pin, path, **kwargs):
            arm(pin, path, **kwargs)
            if path == self.root and not list(self.root.iterdir()):
                self.rollout()
        with mock.patch.object(RolloutInventory, '_arm', new=changing):
            pin = self.make()
        self.assertFalse(pin.absent(self.root, self.session))

    def test_root_rename_and_symlink_back_to_same_inode_is_permanent(self):
        pin = self.make()
        moved = self.home / 'old-sessions'
        self.root.rename(moved)
        self.root.symlink_to(moved, target_is_directory=True)
        with self.assertRaises(ValueError): pin.absent(self.root, self.session)
        self.root.unlink()
        moved.rename(self.root)
        with self.assertRaises(ValueError): pin.absent(self.root, self.session)

    def test_directory_inode_replacement_blocks_even_with_same_names(self):
        child = self.root / '2020'
        child.mkdir()
        pin = self.make()
        child.rename(self.root / 'moved')
        child.mkdir()
        with self.assertRaises(ValueError): pin.absent(self.root, self.session)

    def test_other_home_and_restart_are_rejected(self):
        pin = self.make()
        with self.assertRaises(ValueError): pin.absent(self.home / 'other', self.session)
        with self.assertRaises(ValueError): pin.absent(self.root, self.session)
        pin = self.make()
        with mock.patch('os.getpid', return_value=os.getpid() + 1):
            with self.assertRaises(ValueError): pin.absent(self.root, self.session)

    def test_event_query_failure_never_falls_back_to_cached_absence(self):
        pin = self.make()
        queue = pin._queue
        pin._queue = mock.Mock(wraps=queue)
        pin._queue.control.side_effect = OSError('event query failed')
        with self.assertRaises(OSError): pin.absent(self.root, self.session)
        pin._queue.control.side_effect = None
        with self.assertRaises(ValueError): pin.absent(self.root, self.session)

    def test_close_releases_all_descriptors_and_refuses_reuse(self):
        pin = self.make()
        fds = tuple(pin._paths)
        pin.close()
        pin.close()
        for fd in fds:
            with self.assertRaises(OSError): os.fstat(fd)
        with self.assertRaises(ValueError): pin.absent(self.root, self.session)

    def test_bounds_and_links_fail_closed(self):
        with self.assertRaises(ValueError): self.make(max_directories=1)
        outside = self.home / 'outside'
        outside.mkdir()
        (self.root / 'linked').symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError): self.make()
        (self.root / 'linked').unlink()
        pin = self.make(max_update_entries=1)
        self.rollout(session=str(uuid.uuid4()))
        self.rollout(session=str(uuid.uuid4()))
        with self.assertRaises(ValueError): pin.absent(self.root, self.session)

    def test_identity_uses_live_index_without_glob_and_keeps_final_recheck(self):
        native = identity_fixture.StandbyIdentityTests()
        native.setUp()
        self.addCleanup(native.doCleanups)
        pin = RolloutInventory(native.sessions)
        self.addCleanup(pin.close)
        with mock.patch.object(Path, 'rglob', side_effect=AssertionError('history scan')):
            row = native.inspect(rollout_absent=pin.absent)
        self.assertFalse(row['readiness_proven'])
        calls = []
        def files(*args, **kwargs):
            if calls:
                (native.sessions / (native.session + '.jsonl.zst')).write_bytes(b'x')
            calls.append(1)
            return copy.deepcopy(native.files)
        with self.assertRaises(ValueError):
            native.inspect(files_reader=files, rollout_absent=pin.absent)


if __name__ == '__main__':
    unittest.main()
