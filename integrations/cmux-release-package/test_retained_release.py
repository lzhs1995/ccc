"""Offline boundaries for retained-source validation. No App, network or GUI."""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
import zipfile

import retained_release as retained
import release_paired as paired
import release_smoke as smoke
import evidence_revalidation as exporter
import source_contract as shared


def archive_bytes(entries):
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w') as archive:
        for name, raw, mode in entries:
            row = zipfile.ZipInfo(name)
            row.external_attr = mode << 16
            archive.writestr(row, raw)
    return out.getvalue()


def fake_run(context, selection, *, source):
    prefix = 'source' if source else 'validation'
    return {'id': context['run_id'], 'run_attempt': context['run_attempt'], 'head_sha': context['head_sha'],
            'head_branch': selection[prefix + '_branch'], 'path': selection[prefix + '_workflow'],
            'repository': {'full_name': context['repository'], 'id': selection['repository_id']},
            'event': 'push', 'status': 'completed' if source else 'in_progress',
            'conclusion': 'failure' if source else None}


class RetainedReleaseBoundaries(unittest.TestCase):
    def setUp(self):
        self.selection = retained.pins()
        self.current = dict(self.selection['source_run'], run_id=999999999999,
                            head_sha='b' * 40, run_attempt=1)

    def test_source_and_validation_cannot_alias_or_use_foreign_context(self):
        self.assertEqual(retained.validate_context(self.current, self.selection)['source_run'],
                         self.selection['source_run'])
        for changes in ({'run_id': self.selection['source_run']['run_id']},
                        {'head_sha': self.selection['source_run']['head_sha']},
                        {'run_attempt': 2}, {'run_attempt': True}, {'repository': 'foreign/repo'},
                        {'head_sha': 'not-a-commit'}, {'run_id': -1}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                retained.validate_context(dict(self.current, **changes), self.selection)
        with self.assertRaises(ValueError):
            retained.validate_context(self.current, self.selection, branch='refs/heads/main')

    def test_source_api_keeps_known_failure_and_validation_has_its_own_run(self):
        source = fake_run(self.selection['source_run'], self.selection, source=True)
        unchanged = copy.deepcopy(source)
        retained.validate_run_api(source, self.selection['source_run'], self.selection, source=True)
        self.assertEqual(source, unchanged)
        for key, value in [('conclusion', 'success'), ('status', 'in_progress'), ('run_attempt', 2),
                           ('head_sha', self.current['head_sha']), ('path', self.selection['validation_workflow'])]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                retained.validate_run_api(dict(source, **{key: value}), self.selection['source_run'], self.selection, source=True)
        own = fake_run(self.current, self.selection, source=False)
        retained.validate_run_api(own, self.current, self.selection)
        with self.assertRaises(ValueError):
            retained.validate_run_api(source, self.current, self.selection)

    def test_artifact_substitution_expiry_and_oversize_are_refused(self):
        expected = self.selection['app_artifact']
        retained.validate_artifact(expected, expected, self.selection, limit=retained.MAX_APP)
        cases = [{'id': expected['id'] + 1}, {'name': 'another-app'}, {'digest': 'sha256:' + '0' * 64},
                 {'size_in_bytes': expected['size_in_bytes'] + 1}, {'size_in_bytes': retained.MAX_APP + 1},
                 {'expired': True}, {'archive_download_url': 'https://example.invalid/foreign.zip'},
                 {'workflow_run': dict(expected['workflow_run'], head_sha=self.current['head_sha'])},
                 {'workflow_run': dict(expected['workflow_run'], repository_id=1)}]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                retained.validate_artifact(dict(expected, **changes), expected, self.selection, limit=retained.MAX_APP)

    def small_archives(self, directory, *, outer_members=None, inner_override=None):
        inner = archive_bytes([('cmux.app/Contents/example', b'synthetic signed-tree placeholder', stat.S_IFREG | 0o644)])
        blob = archive_bytes(outer_members if outer_members is not None else [
            ('cmux-release.zip', inner, stat.S_IFREG | 0o644)])
        outer = Path(directory) / 'artifact.zip'; outer.write_bytes(blob)
        selected = copy.deepcopy(self.selection)
        selected['app_artifact'].update(size_in_bytes=len(blob), digest='sha256:' + hashlib.sha256(blob).hexdigest())
        selected['inner_archive'] = {'name': 'cmux-release.zip', 'bytes': len(inner),
                                      'sha256': hashlib.sha256(inner).hexdigest()}
        if inner_override:
            selected['inner_archive'].update(inner_override)
        return outer, selected, inner

    def test_exact_outer_and_inner_archive_are_both_required(self):
        with tempfile.TemporaryDirectory() as directory:
            outer, selected, inner = self.small_archives(directory)
            destination = Path(directory) / 'retained.zip'
            retained.extract_inner(outer, destination, selected)
            self.assertEqual(destination.read_bytes(), inner)
            selected['app_artifact']['digest'] = 'sha256:' + '0' * 64
            with self.assertRaisesRegex(ValueError, 'outer ZIP'):
                retained.extract_inner(outer, Path(directory) / 'foreign.zip', selected)

    def test_zip_extra_traversal_link_duplicate_and_size_are_refused(self):
        cases = [[('../escape', b'x', stat.S_IFREG | 0o644)],
                 [('cmux-release.zip', b'x', stat.S_IFLNK | 0o777)],
                 [('cmux-release.zip', b'x', stat.S_IFREG | 0o644), ('extra', b'x', stat.S_IFREG | 0o644)],
                 [('cmux-release.zip', b'x', stat.S_IFREG | 0o644), ('cmux-release.zip', b'x', stat.S_IFREG | 0o644)],
                 [('cmux-release.zip', b'x', stat.S_IFREG | 0o644)]]
        for entries in cases:
            with self.subTest(entries=entries), tempfile.TemporaryDirectory() as directory:
                import warnings
                with warnings.catch_warnings():
                    warnings.simplefilter('ignore', UserWarning)
                    outer, selected, _ = self.small_archives(directory, outer_members=entries)
                with self.assertRaises(ValueError):
                    retained.extract_inner(outer, Path(directory) / 'retained.zip', selected)
                self.assertFalse((Path(directory).parent / 'escape').exists())

    def test_inner_digest_cannot_be_replaced_with_outer_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            outer, selected, _ = self.small_archives(directory)
            selected['inner_archive']['sha256'] = selected['app_artifact']['digest'][7:]
            with self.assertRaisesRegex(ValueError, 'inner ZIP'):
                retained.extract_inner(outer, Path(directory) / 'retained.zip', selected)

    def test_download_timeout_is_retained_and_never_replayed(self):
        def timeout(argv, **kwargs):
            kwargs['stdout'].write(b'partial-original-response')
            raise subprocess.TimeoutExpired(argv, 1)
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(shared, 'original_admission', return_value=SimpleNamespace(runner_only=lambda: None)), \
             patch.object(retained.subprocess, 'run', side_effect=timeout) as command:
            root = Path(directory)
            with self.assertRaises(subprocess.TimeoutExpired):
                retained.fetch(root, 'once', 'synthetic/no-network', maximum=100, timeout=1)
            self.assertEqual((root / 'once/response.json').read_bytes(), b'partial-original-response')
            self.assertEqual(shared.read_json(root / 'once/RESULT.json')['status'], 'UNKNOWN')
            with self.assertRaises(FileExistsError):
                retained.fetch(root, 'once', 'synthetic/no-network', maximum=100, timeout=1)
            self.assertEqual(command.call_count, 1)

    def test_native_file_limit_bounds_an_oversized_child_response(self):
        original_run = subprocess.run
        def local_writer(argv, **kwargs):
            return original_run([sys.executable, '-BE', '-c',
                'import os; os.write(1,b"x"*64); os.write(1,b"overflow")'], **kwargs)
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(shared, 'original_admission', return_value=SimpleNamespace(runner_only=lambda: None)), \
             patch.object(retained.subprocess, 'run', side_effect=local_writer):
            with self.assertRaises(ValueError):
                retained.fetch(Path(directory), 'bounded', 'synthetic/no-network', maximum=64, timeout=5)
            receipt = shared.read_json(Path(directory) / 'bounded/RESULT.json')
            self.assertLessEqual(receipt['bytes'], 64)
            self.assertNotEqual(receipt['returncode'], 0)

    def peer_fixture(self, process_after=None, socket_after=None):
        uid = os.geteuid()
        row = {'pid': 321, 'ppid': 1, 'uid': uid, 'birth': [123, 456],
               'executable': '/Applications/cmux.app/Contents/MacOS/cmux'}
        info = SimpleNamespace(st_dev=1, st_ino=2, st_mode=stat.S_IFSOCK | 0o600,
            st_uid=uid, st_gid=20, st_nlink=1, st_size=0, st_mtime_ns=30, st_ctime_ns=40)
        final = SimpleNamespace(**(vars(info) | (socket_after or {})))
        path = MagicMock(); path.lstat.side_effect = [info, info, final]
        connection = MagicMock(); connection.__enter__.return_value = connection
        connection.getsockopt.return_value = (321).to_bytes(4, sys.byteorder, signed=True)
        return row, path, connection, row if process_after is None else row | process_after

    def call_peer(self, *, process_after=None, socket_after=None, executable_hash='a' * 64):
        row, path, connection, after = self.peer_fixture(process_after, socket_after)
        with patch.object(paired, 'Path', return_value=path), patch.object(paired.socket, 'socket', return_value=connection), \
             patch.object(paired, 'process_identity', side_effect=[row, after]), \
             patch.object(paired, 'digest', return_value=executable_hash):
            return paired.peer({'bundle_executable': 'cmux', 'application_sha256': 'a' * 64})

    def test_peer_process_change_keeps_the_original_refusal_and_fields(self):
        with self.assertRaises(paired.PeerObservationChanged) as raised:
            self.call_peer(process_after={'birth': [123, 457]})
        record = smoke.error_record(raised.exception)
        self.assertFalse(record['automatic_retry'])
        detail = record['peer_observation']
        self.assertEqual(detail['changed'], 'process')
        self.assertIsNone(detail['socket_after_hash'])
        self.assertNotEqual(detail['process_before'], detail['process_after'])

    @unittest.skipUnless(sys.platform == 'darwin', 'Darwin socket metadata regression')
    def test_same_mode_socket_chmod_during_peer_check_is_accepted(self):
        # Execute both original stat comparisons against a real local socket.
        # The peer and executable are synthetic; no existing socket is contacted.
        with tempfile.TemporaryDirectory(prefix='cmux-ctime-', dir='/private/tmp') as directory:
            path = Path(directory).resolve() / 'socket'
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                listener.bind(str(path))
                path.chmod(0o600)
                uid = os.geteuid()
                row = {'pid': 321, 'ppid': 1, 'uid': uid, 'birth': [123, 456],
                       'executable': '/Applications/cmux.app/Contents/MacOS/cmux'}
                changes = []
                def same_mode_chmod(*unused):
                    before = path.lstat()
                    time.sleep(.002)
                    path.chmod(0o600)
                    after = path.lstat()
                    self.assertEqual(before.st_ino, after.st_ino)
                    self.assertEqual(before.st_mode, after.st_mode)
                    self.assertNotEqual(before.st_ctime_ns, after.st_ctime_ns)
                    changes.append((before.st_ctime_ns, after.st_ctime_ns))
                connection = MagicMock()
                connection.__enter__.return_value = connection
                connection.getsockopt.return_value = (321).to_bytes(4, sys.byteorder, signed=True)
                connection.__exit__.side_effect = same_mode_chmod
                def hash_after_chmod(*unused):
                    same_mode_chmod()
                    return 'a' * 64
                with patch.object(paired, 'stable_socket_path', return_value=str(path)), \
                     patch.object(paired.socket, 'socket', return_value=connection), \
                     patch.object(paired, 'process_identity', return_value=row), \
                     patch.object(paired, 'digest', side_effect=hash_after_chmod):
                    self.assertEqual(paired.peer({'bundle_executable': 'cmux',
                        'application_sha256': 'a' * 64})['socket_inode'], path.lstat().st_ino)
                self.assertEqual(len(changes), 2)

    def test_socket_ctime_only_observation_is_accepted(self):
        self.assertEqual(self.call_peer(socket_after={'st_ctime_ns': 41})['pid'], 321)

    def test_changed_executable_hash_is_refused(self):
        with self.assertRaisesRegex(ValueError, 'peer executable does not match'):
            self.call_peer(executable_hash='b' * 64)

    def test_remaining_eight_socket_fields_still_refuse_changes(self):
        _, path, _, _ = self.peer_fixture()
        # The same observations accepted before this patch are still accepted.
        self.assertEqual(self.call_peer()['pid'], 321)
        original = dict(st_dev=1, st_ino=2, st_mode=stat.S_IFSOCK | 0o600, st_uid=os.geteuid(),
                        st_gid=20, st_nlink=1, st_size=0, st_mtime_ns=30)
        for field, value in original.items():
            with self.subTest(field=field), self.assertRaises(paired.PeerObservationChanged) as raised:
                self.call_peer(socket_after={field: value + 1})
            detail = raised.exception.observation
            self.assertEqual(detail['changed'], 'socket')
            self.assertEqual(detail['socket_before_hash'][field], value)
            self.assertEqual(detail['socket_after_hash'][field], value + 1)

    def test_wrong_source_package_is_refused_before_any_install_command(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(smoke, 'runtime_preflight', return_value={'home': directory}), \
             patch.object(paired, 'read_pin', return_value=({'run': self.current}, [])), \
             patch.object(shared, 'run_identity', return_value=self.current), \
             patch.object(smoke.package, 'command') as commands:
            with self.assertRaisesRegex(ValueError, 'not distinct'):
                smoke.runtime(Path(directory) / 'runtime', Path(directory) / 'package', Path(directory) / 'old-cli',
                              source_run=self.selection['source_run'], package_pin_sha256='a' * 64)
            commands.assert_not_called()

    def test_failure_export_keeps_both_contexts_and_excludes_full_archives(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve(); root = base / 'attempt'; root.mkdir()
            shared.save(root / 'RUN-CONTEXT.json', {'source_run': self.selection['source_run'], 'validation_run': self.current})
            (root / 'original-evidence').mkdir()
            shared.save(root / 'original-evidence/FAILURE.json', {'passed': False, 'run': self.selection['source_run']})
            (root / 'downloads/app-archive').mkdir(parents=True)
            (root / 'downloads/app-archive/response.zip').write_bytes(b'not an App; must not be exported')
            with patch.object(shared, 'original_admission', return_value=SimpleNamespace(runner_only=lambda: None)), \
                 patch.object(exporter.ci_revalidate, 'context', return_value=(self.selection, self.current)), \
                 patch.dict(os.environ, {'RUNNER_TEMP': str(base)}):
                result = exporter.export(root, base / 'evidence')
            self.assertFalse(result['new_validation_acceptance_observed'])
            self.assertTrue(result['original_source_failure_retained'])
            self.assertEqual(result['source_run'], self.selection['source_run'])
            self.assertEqual(result['validation_run'], self.current)
            self.assertFalse(list((base / 'evidence').rglob('*.zip')))


if __name__ == '__main__':
    unittest.main(verbosity=2)
