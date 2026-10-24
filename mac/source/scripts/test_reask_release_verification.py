"""The file scheduler authenticates the complete release without installer I/O."""
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import configuration_guardian as guardian
import executor_reask_scheduler as scheduler


class ReadOnlyReleaseTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name).resolve()
        self.source = self.home / '.local/share/multi-agent-collaboration/releases/test/source'
        self.source.mkdir(parents=True)
        self.manifest = self.source.parent / 'MANIFEST.json'
        self.current_path = self.home / 'CURRENT.json'
        self.files = {}
        for name, raw in {
            'scripts/configuration_guardian.py': Path(guardian.__file__).read_bytes(),
            'scripts/executor_reask_scheduler.py': Path(scheduler.__file__).read_bytes(),
            'scripts/manage_install.py': b'raise RuntimeError("INSTALLER_EXECUTED")\n',
            'references/contract.md': b'original contract\n',
        }.items():
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
            path.chmod(0o444)
            self.files[name] = dict(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
        self.current = dict(source=str(self.source), python=sys.executable)
        self.pin_manifest()
        self.contexts = ExitStack()
        self.addCleanup(self.contexts.close)
        self.contexts.enter_context(patch.object(Path, 'home', return_value=self.home))
        self.contexts.enter_context(patch.object(guardian, '__file__',
                                      str(self.source / 'scripts/configuration_guardian.py')))
        self.contexts.enter_context(patch.object(scheduler, '__file__',
                                      str(self.source / 'scripts/executor_reask_scheduler.py')))

    def pin_manifest(self):
        self.manifest.write_text(json.dumps(dict(files=self.files)))
        self.current['manifest_sha256'] = hashlib.sha256(self.manifest.read_bytes()).hexdigest()
        self.current_path.write_text(json.dumps(self.current))

    def test_one_complete_verification_without_importing_the_installer(self):
        with patch.object(guardian, 'read_json', wraps=guardian.read_json) as reads:
            self.assertEqual(scheduler.approved_release(self.current_path), self.current)
        self.assertEqual([c.args[0] for c in reads.call_args_list],
                         [self.current_path, self.manifest])
        # Configuration repair still loads its installer, after the same checks.
        with self.assertRaisesRegex(RuntimeError, 'INSTALLER_EXECUTED'):
            guardian.load_release(self.home, self.current_path)

    def test_each_call_rehashes_an_unrelated_member_even_at_the_same_size(self):
        self.assertEqual(scheduler.approved_release(self.current_path), self.current)
        member = self.source / 'references/contract.md'
        raw = member.read_bytes()
        member.chmod(0o644)
        member.write_bytes(b'x' * len(raw))
        member.chmod(0o444)
        with self.assertRaisesRegex(ValueError, 'content mismatch'):
            scheduler.approved_release(self.current_path)

    def test_writable_members_and_changed_manifest_are_refused(self):
        member = self.source / 'references/contract.md'
        member.chmod(0o644)
        with self.assertRaisesRegex(ValueError, 'writable'):
            scheduler.approved_release(self.current_path)
        member.chmod(0o444)
        self.manifest.write_text(json.dumps(dict(files={})))
        with self.assertRaisesRegex(ValueError, 'manifest hash mismatch'):
            scheduler.approved_release(self.current_path)

    def test_missing_scheduler_pin_cannot_be_filled_by_an_unverified_second_read(self):
        self.files.pop('scripts/executor_reask_scheduler.py')
        self.pin_manifest()
        with self.assertRaisesRegex(ValueError, 'scheduler is not pinned'):
            scheduler.approved_release(self.current_path)

    def test_wrong_running_scheduler_and_python_are_refused(self):
        with patch.object(scheduler, '__file__', str(self.home / 'other-scheduler.py')):
            with self.assertRaisesRegex(ValueError, 'scheduler is not pinned'):
                scheduler.approved_release(self.current_path)
        self.current['python'] = str(self.home / 'other-python')
        self.pin_manifest()
        with self.assertRaisesRegex(ValueError, 'Python does not match'):
            scheduler.approved_release(self.current_path)


if __name__ == '__main__':
    unittest.main()
