import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import ccc_codex_queue as native
if __package__:
    from . import test_codex_native_files as file_tests
else:
    import test_codex_native_files as file_tests


class VnodeParentPassTests(unittest.TestCase):
    def test_case_unicode_links_missing_and_dotdot_match_original(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'MixedCase').touch()
            (root/'中文').touch()
            (root/'link').symlink_to('MixedCase')
            (root/'broken').symlink_to('missing')
            (root/'loop').symlink_to('loop')
            resolver = native._VnodePathPass()
            for path in [root, root/'MixedCase', root/'mixedcase', root/'中文',
                         root/'link', root/'broken', root/'loop', root/'absent',
                         root/'MixedCase'/'child', root/'..']:
                with self.subTest(path=str(path)):
                    try:
                        expected = path.resolve()
                    except (OSError, RuntimeError) as error:
                        # Python <3.13 raises on a non-strict symlink loop;
                        # newer pathlib returns the unresolved path instead.
                        with self.assertRaises(type(error)) as caught:
                            resolver.resolve(path)
                        self.assertEqual(str(caught.exception), str(error))
                    else:
                        self.assertEqual(resolver.resolve(path), expected)
            resolver.verify()

    def test_shared_parent_is_resolved_and_rechecked_in_each_new_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = [root/str(i) for i in range(4)]
            for path in paths:
                path.touch()
            original = Path.resolve
            calls = []
            def resolve(path, *args, **kwargs):
                calls.append(path)
                return original(path, *args, **kwargs)
            with patch.object(Path, 'resolve', resolve):
                for _ in range(2):
                    resolver = native._VnodePathPass()
                    for path in paths:
                        self.assertEqual(resolver.resolve(path), original(path))
                    resolver.verify()
            self.assertEqual(calls, [root]*4)

    def test_parent_changed_within_pass_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('a', 'b'):
                (root/name).mkdir()
                (root/name/'writer').touch()
            link = root/'parent'
            link.symlink_to('a', target_is_directory=True)
            resolver = native._VnodePathPass()
            resolver.resolve(link/'writer')
            link.unlink()
            link.symlink_to('b', target_is_directory=True)
            with self.assertRaises(native.VnodeInventoryChanged):
                resolver.verify()

    def test_parent_changed_between_inventory_passes_is_rejected(self):
        self._inventory_change(parent=True)

    def test_leaf_changed_between_inventory_passes_is_rejected(self):
        self._inventory_change(parent=False)

    def _inventory_change(self, *, parent):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('a', 'b'):
                (root/name).mkdir()
                (root/name/'writer').touch()
            link = root/'link'
            link.symlink_to('a' if parent else 'a/writer', target_is_directory=parent)
            path = link/'writer' if parent else link
            fixture = file_tests.NativeFileTests()
            fixture.setUp()
            fixture.entries = [(3, 1)]
            fixture.files = {3: (3, os.fsencode(path))}
            count = 0
            def read(*args):
                nonlocal count
                count += 1
                result = fixture.vnode(*args)
                if count == 2:
                    link.unlink()
                    link.symlink_to('b' if parent else 'b/writer', target_is_directory=parent)
                return result
            with patch.object(native, '_proc_pidinfo', side_effect=fixture.descriptors), \
                    patch.object(native, '_proc_pidfdinfo', side_effect=read):
                with self.assertRaises(native.VnodeInventoryChanged):
                    native.process_writable_files(123, identities=True)
            self.assertEqual(count, 2)

    def test_failed_lstat_preserves_original_resolver_error(self):
        error = RuntimeError('resolver error')
        with patch.object(Path, 'lstat', side_effect=OSError('missing')), \
                patch.object(Path, 'resolve', side_effect=error):
            with self.assertRaises(RuntimeError) as caught:
                native._VnodePathPass().resolve(Path('/example'))
            self.assertIs(caught.exception, error)


if __name__ == '__main__':
    unittest.main()
