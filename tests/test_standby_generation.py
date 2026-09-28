import copy
from pathlib import Path
import os
import select
import tempfile
import unittest
from unittest.mock import patch

from ccc_standby_generation import SCOPES, StandbyGeneration


class GenerationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.roots = {}
        for name in SCOPES:
            path = self.root / name; path.mkdir()
            (path / 'content').write_text('original')
            self.roots[name] = [path]
        self.settings = {'profile': 'local', 'environment': {'TEST_KEY': 'value'}}

    def pin(self, **kwargs):
        return StandbyGeneration(self.roots, lambda: copy.deepcopy(self.settings), **kwargs)

    def test_stable_inventory_and_no_writes(self):
        before = {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        pin = self.pin()
        self.assertEqual(pin.current(), pin.value)
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_each_dependency_change_invalidates(self):
        for name in SCOPES:
            with self.subTest(name=name):
                pin = self.pin(); p = self.roots[name][0] / 'content'
                p.write_text('changed')
                with self.assertRaises(ValueError): pin.current()
                p.write_text('original')
                with self.assertRaises(ValueError): pin.current()

    def test_unobserved_content_roundtrip_changes_generation(self):
        pin = self.pin(); p = self.roots['skills'][0] / 'content'
        p.write_text('different'); p.write_text('original')
        with self.assertRaises(ValueError): pin.current()

    def test_missing_config_appears(self):
        path = self.root / 'optional.toml'; self.roots['codex_config'].append(path)
        pin = self.pin(); path.write_text('enabled=true')
        with self.assertRaises(ValueError): pin.current()

    def test_added_and_removed_skill(self):
        pin = self.pin(); p = self.roots['skills'][0] / 'new.md'; p.write_text('new')
        with self.assertRaises(ValueError): pin.current()
        p.unlink()
        with self.assertRaises(ValueError): pin.current()

    def test_environment_and_profile_change_stays_invalid(self):
        pin = self.pin(); self.settings['profile'] = 'other'
        with self.assertRaises(ValueError): pin.current()
        self.settings['profile'] = 'local'
        with self.assertRaises(ValueError): pin.current()

    def test_symlink_target_is_included(self):
        target = self.root / 'external'; target.mkdir(); (target / 'skill').write_text('one')
        (self.roots['skills'][0] / 'linked').symlink_to(target, target_is_directory=True)
        pin = self.pin(); (target / 'skill').write_text('two')
        with self.assertRaises(ValueError): pin.current()

    def test_link_cycle_rejected(self):
        root = self.roots['skills'][0]; (root / 'cycle').symlink_to(root)
        with self.assertRaises(ValueError): self.pin()

    def test_failure_and_resource_limit_refuse(self):
        with self.assertRaises(ValueError): self.pin(max_entries=1)
        pin = self.pin()
        with patch.object(pin, '_snapshot', side_effect=OSError('unreadable')):
            with self.assertRaises(OSError): pin.current()
        with self.assertRaises(ValueError): pin.current()

    def test_change_during_other_file_read_detected(self):
        pin = self.pin(); real = pin.effective
        p = self.roots['rules'][0] / 'content'; calls = [0]
        def changing():
            calls[0] += 1
            if calls[0] == 2: p.write_text('changed late')
            return real()
        pin.effective = changing
        with self.assertRaises(ValueError):
            pin.current()

    @unittest.skipUnless(hasattr(select, 'kqueue') and hasattr(os, 'O_SYMLINK'), 'Darwin vnode events')
    def test_native_events_latch_roundtrip_and_skip_rescan(self):
        pin = self.pin(use_events=True); self.addCleanup(pin.close)
        with patch.object(pin, '_snapshot', side_effect=AssertionError('activation rescanned files')):
            self.assertEqual(pin.current(), pin.value)
            p = self.roots['skills'][0] / 'content'
            p.write_text('changed'); p.write_text('original')
            with self.assertRaises(ValueError): pin.current()
            with self.assertRaises(ValueError): pin.current()

    @unittest.skipUnless(hasattr(select, 'kqueue') and hasattr(os, 'O_SYMLINK'), 'Darwin vnode events')
    def test_native_events_detect_ancestor_swap(self):
        pin = self.pin(use_events=True); self.addCleanup(pin.close)
        p = self.roots['skills'][0]; moved = self.root / 'moved'
        p.rename(moved); p.symlink_to(moved, target_is_directory=True)
        with self.assertRaises(ValueError): pin.current()

    @unittest.skipUnless(hasattr(select, 'kqueue') and hasattr(os, 'O_SYMLINK'), 'Darwin vnode events')
    def test_native_events_optional_file_and_closed_watch(self):
        p = self.root / 'not-created/config.toml'
        self.roots['codex_config'].append(p)
        pin = self.pin(use_events=True); self.addCleanup(pin.close)
        p.parent.mkdir(); p.write_text('new')
        with self.assertRaises(ValueError): pin.current()
        pin.close()
        with self.assertRaises(ValueError): pin.current()

    def test_effective_callback_ancestor_swap_refused_same_call(self):
        pin = self.pin(); real = pin.effective; calls = [0]
        p = self.roots['rules'][0]; moved = self.root / 'moved'
        def changing():
            calls[0] += 1
            if calls[0] == 2:
                p.rename(moved); p.symlink_to(moved, target_is_directory=True)
            return real()
        pin.effective = changing
        with self.assertRaises(ValueError): pin.current()

    @unittest.skipUnless(hasattr(select, 'kqueue') and hasattr(os, 'O_SYMLINK'), 'Darwin vnode events')
    def test_unrelated_sibling_creation_does_not_invalidate_ancestors(self):
        pin = self.pin(use_events=True); self.addCleanup(pin.close)
        (self.root / 'unrelated.log').write_text('not a declared dependency')
        self.assertEqual(pin.current(), pin.value)


if __name__ == '__main__':
    unittest.main()
