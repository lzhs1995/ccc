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

    def test_ancestor_link_tracks_identity_without_unrelated_target_contents(self):
        target = self.root / 'ancestor-target'; target.mkdir()
        declared = target / 'declared'; declared.write_text('config')
        os.mkfifo(target / 'unrelated-fifo')
        link = self.root / 'ancestor-link'; link.symlink_to(target, target_is_directory=True)
        roots = {scope: [link / 'declared'] for scope in SCOPES}
        pin = StandbyGeneration(roots, lambda: self.settings)
        self.assertEqual(pin.current(), pin.value)
        (target / 'unrelated-file').write_text('unrelated')
        self.assertEqual(pin.current(), pin.value)
        link.rename(self.root / 'old-link')
        link.symlink_to(target, target_is_directory=True)
        with self.assertRaises(ValueError):
            pin.current()

    def test_failure_and_resource_limit_refuse(self):
        with self.assertRaises(ValueError): self.pin(max_entries=1)
        pin = self.pin()
        with patch.object(pin, '_snapshot', side_effect=OSError('unreadable')):
            with self.assertRaises(OSError): pin.current()
        with self.assertRaises(ValueError): pin.current()

    @unittest.skipUnless(hasattr(select, 'kqueue') and hasattr(os, 'O_SYMLINK'), 'Darwin vnode events')
    def test_each_intermediate_link_is_watched_without_sibling_recursion(self):
        for leaf in (False, True):
            with self.subTest(leaf=leaf), tempfile.TemporaryDirectory(dir=self.root) as temp:
                root = Path(temp)
                target = root / 'target'; target.mkdir()
                (target / 'declared').write_text('config')
                os.mkfifo(target / 'unrelated')
                a, b = root / 'a', root / 'b'
                b.symlink_to('target/declared' if leaf else 'target')
                a.symlink_to('b')
                dependency = a if leaf else a / 'declared'
                pin = StandbyGeneration({scope: [dependency] for scope in SCOPES},
                                        lambda: self.settings, use_events=True)
                self.addCleanup(pin.close)
                self.assertIn(str(b), pin._inventory_paths)
                self.assertEqual(pin.current(), pin.value)
                replacement = root / 'replacement'
                replacement.symlink_to('target/declared' if leaf else 'target')
                os.replace(replacement, b)
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
    def test_capture_cannot_pin_an_effective_value_outside_its_snapshot(self):
        # A transient B between the two baseline scans and watch-arm scan
        # previously became the fast-path baseline for an A generation.
        calls = [0]
        def effective():
            calls[0] += 1
            return {'profile': 'B' if calls[0] == 5 or calls[0] >= 8 else 'A'}
        pin = None
        try:
            with self.assertRaises(ValueError):
                pin = StandbyGeneration(self.roots, effective, use_events=True)
                pin.current()
        finally:
            if pin is not None:
                pin.close()

    @unittest.skipUnless(hasattr(select, 'kqueue') and hasattr(os, 'O_SYMLINK'), 'Darwin vnode events')
    def test_unrelated_sibling_creation_does_not_invalidate_ancestors(self):
        pin = self.pin(use_events=True); self.addCleanup(pin.close)
        (self.root / 'unrelated.log').write_text('not a declared dependency')
        self.assertEqual(pin.current(), pin.value)

    @unittest.skipUnless(hasattr(select, 'kqueue') and hasattr(os, 'O_SYMLINK'), 'Darwin vnode events')
    def test_missing_source_allows_job_siblings_but_observed_appearance_latches(self):
        optional = self.root / 'optional/config.toml'
        self.roots['codex_config'].append(optional)
        pin = self.pin(use_events=True); self.addCleanup(pin.close)
        with patch.object(pin, '_snapshot', side_effect=AssertionError('hot rescan')):
            unrelated = self.root / 'standby'; unrelated.mkdir()
            (unrelated / 'receipt.json').write_text('{}')
            (self.root / 'log.txt').write_text('unrelated')
            self.assertEqual(pin.current(), pin.value)
            optional.parent.mkdir()
            with self.assertRaises(ValueError): pin.current()
            optional.parent.rmdir()
            with self.assertRaises(ValueError): pin.current()

    @unittest.skipUnless(hasattr(select, 'kqueue') and hasattr(os, 'O_SYMLINK'), 'Darwin vnode events')
    def test_missing_parent_permissions_and_identity_still_invalidate(self):
        for change in ('permissions', 'replace'):
            with self.subTest(change=change), tempfile.TemporaryDirectory(dir=self.root) as temp:
                parent = Path(temp) / 'ancestor'; parent.mkdir(mode=0o700)
                roots = {**self.roots, 'codex_config': [parent / 'config.toml']}
                pin = StandbyGeneration(roots, lambda: self.settings, use_events=True)
                self.addCleanup(pin.close)
                if change == 'permissions':
                    parent.chmod(0o755)
                    parent.chmod(0o700)
                else:
                    saved = parent.with_name('saved'); parent.rename(saved)
                    parent.symlink_to(saved, target_is_directory=True)
                with self.assertRaises(ValueError): pin.current()

    @unittest.skipUnless(hasattr(select, 'kqueue') and hasattr(os, 'O_SYMLINK'), 'Darwin vnode events')
    def test_ensure_app_dir_avoids_redundant_attribute_event(self):
        from cmux_codex_watch import ensure_app_dir
        app = self.root / 'app'; app.mkdir(mode=0o700)
        roots = {**self.roots, 'codex_config': [app / 'optional.toml']}
        pin = StandbyGeneration(roots, lambda: self.settings, use_events=True)
        self.addCleanup(pin.close)
        ensure_app_dir(app)
        self.assertEqual(pin.current(), pin.value)
        app.chmod(0o755)
        ensure_app_dir(app)
        self.assertEqual(app.stat().st_mode & 0o7777, 0o700)
        with self.assertRaises(ValueError): pin.current()


if __name__ == '__main__':
    unittest.main()
