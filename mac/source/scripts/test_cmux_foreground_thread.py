"""Native foreground evidence and caller-selection regression tests."""
import copy
import ctypes
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import cmux_daemon_identity as identity
import cmux_foreground_thread as foreground
import cmux_workspace_guard as guard
import test_cmux_daemon_identity as baseline

S, C = baseline.S, baseline.C


class EvidenceFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve() / '.codex'
        self.home.mkdir(mode=0o700)
        self.directory = self.home / 'credential-observations'
        self.directory.mkdir(mode=0o700)
        self.marker = self.directory / 'enabled-v1'
        self.marker.write_bytes(b'ccc-request-credentials-v1\n')
        self.marker.chmod(0o600)
        self.path = self.directory / 'client-30-thread.json'
        self.client = dict(pid=30, birth=[100, 100000], argv=['/bin/codex'],
                           env={'CODEX_HOME': str(self.home)})
        self.data = dict(schema=1, purpose='client_foreground_thread', pid=30,
                         client_epoch=C, thread_id=S, published_at_ms=100100)
        self.write()
        self.seen = patch.object(foreground, '_seen', set())
        self.seen.start()
        self.addCleanup(self.seen.stop)
        self.clock = patch.object(foreground.time, 'time', return_value=200)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def write(self, path=None, **changes):
        target = path or self.path
        target.write_text(json.dumps(dict(self.data, **changes)))
        target.chmod(0o600)

    def reject(self, pattern=None):
        with self.assertRaisesRegex((OSError, ValueError, KeyError), pattern or '.'):
            foreground.read(self.client)


class ForegroundEvidenceTests(EvidenceFixture, unittest.TestCase):
    def test_new_client_without_resume_uses_native_selection(self):
        result = foreground.read(self.client)
        self.assertEqual(result['thread_id'], S)
        self.assertEqual(result['client_epoch'], C)
        self.assertEqual(result['path'], str(self.path))

    def test_switched_thread_is_selected(self):
        self.assertEqual(foreground.read(self.client)['thread_id'], S)
        self.write(thread_id=C, published_at_ms=101000)
        self.assertEqual(foreground.read(self.client)['thread_id'], C)

    def test_explicit_clear_is_not_legacy_absence(self):
        self.write(thread_id=None)
        self.assertIsNone(foreground.read(self.client)['thread_id'])

    def test_home_fallback(self):
        self.client['env'] = {'HOME': str(self.home.parent)}
        self.assertEqual(foreground.read(self.client)['thread_id'], S)

    def test_credential_directory_override_cannot_redirect(self):
        self.client['env']['CODEX_CREDENTIAL_OBSERVATIONS_DIR'] = '/nonexistent'
        self.assertEqual(foreground.read(self.client)['thread_id'], S)

    def test_absent_legacy_observation(self):
        self.path.unlink()
        self.assertIsNone(foreground.read(self.client))

    def test_legacy_no_home(self):
        self.client['env'] = {}
        self.assertIsNone(foreground.read(self.client))

    def test_required_absence_denied(self):
        self.path.unlink()
        self.client['env']['CODEX_CLIENT_THREAD_OBSERVER'] = '1'
        self.reject('required foreground selection missing')

    def test_previously_observed_absence_denied(self):
        foreground.read(self.client)
        self.path.unlink()
        self.reject('required foreground selection missing')

    def test_required_no_home_denied(self):
        self.client['env'] = {'CODEX_CLIENT_THREAD_OBSERVER': '1'}
        self.reject('home missing')

    def test_bad_observer_mode_denied(self):
        self.client['env']['CODEX_CLIENT_THREAD_OBSERVER'] = '0'
        self.reject('observer mode')

    def test_relative_home_denied(self):
        self.client['env']['CODEX_HOME'] = 'relative'
        self.reject('not absolute')

    def test_app_server_is_not_foreground(self):
        self.client['argv'] = ['/bin/codex', 'app-server', '--managed-daemon']
        self.client['env']['CODEX_CLIENT_THREAD_OBSERVER'] = '1'
        self.path.unlink()
        self.assertIsNone(foreground.read(self.client))

    def test_wrong_or_boolean_schema_and_pid_denied(self):
        for key, value in [('schema', True), ('schema', 2), ('pid', True),
                           ('pid', 31), ('purpose', 'request')]:
            with self.subTest(key=key, value=value):
                self.write(**{key: value})
                self.reject('schema or PID mismatch')

    def test_invalid_epoch_and_thread_denied(self):
        for key in ('thread_id', 'client_epoch'):
            for value in (True, '', 'bad', S.upper()):
                if value == S:  # This fixture UUID contains no letters.
                    continue
                with self.subTest(key=key, value=value):
                    self.write(**{key: value})
                    self.reject()
        self.write(client_epoch=None)
        self.reject('epoch')

    def test_noncanonical_uuid_denied(self):
        for key in ('thread_id', 'client_epoch'):
            for value in ('AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA',
                          '{11111111-1111-4111-8111-111111111111}'):
                with self.subTest(key=key, value=value):
                    self.write(**{key: value})
                    self.reject('invalid foreground')

    def test_future_or_invalid_timestamp_denied(self):
        for stamp in (201001, True, 100100.0, '100100', None):
            with self.subTest(stamp=stamp):
                self.write(published_at_ms=stamp)
                self.reject('predates process or is in the future')

    def test_reused_pid_optional_old_record_is_legacy_absence(self):
        self.write(published_at_ms=100099)
        original = self.path.read_bytes()
        for _ in range(2):
            self.assertIsNone(foreground.read(self.client))
        self.assertEqual(foreground._seen, set())
        self.assertEqual(self.path.read_bytes(), original)
        self.path.unlink()
        self.assertIsNone(foreground.read(self.client))

    def test_reused_pid_does_not_inherit_previous_process_seen_state(self):
        foreground.read(self.client)
        self.client['birth'] = [101, 0]
        self.assertIsNone(foreground.read(self.client))
        self.assertEqual(foreground._seen, {(30, (100, 100000))})

    def test_required_old_record_cannot_be_legacy_absence(self):
        self.client['env']['CODEX_CLIENT_THREAD_OBSERVER'] = '1'
        self.write(published_at_ms=100099)
        self.reject('predates process')

    def test_current_selection_cannot_downgrade_to_old_record(self):
        foreground.read(self.client)
        self.write(published_at_ms=100099)
        self.reject('predates process')

    def test_old_record_does_not_hide_invalid_thread_or_permissions(self):
        self.write(published_at_ms=100099, thread_id='invalid')
        self.reject()
        self.write(published_at_ms=100099)
        self.path.chmod(0o644)
        self.reject('untrusted')

    def test_old_record_must_survive_atomic_reread(self):
        self.write(published_at_ms=100099)
        original = foreground._read
        calls = 0
        def read(path, limit):
            nonlocal calls
            if path == self.path:
                calls += 1
                if calls == 2:
                    temporary = self.directory / 'replacement'
                    self.write(temporary)
                    temporary.replace(self.path)
            return original(path, limit)
        with patch.object(foreground, '_read', side_effect=read):
            self.reject('source changed')
        self.assertEqual(calls, 2)
        self.assertEqual(foreground._seen, set())

    def test_optional_old_record_does_not_use_seen_capacity(self):
        foreground._seen.update((pid, (100, 0)) for pid in range(100, 4196))
        self.write(published_at_ms=100099)
        self.assertIsNone(foreground.read(self.client))
        self.assertEqual(len(foreground._seen), 4096)

    def test_missing_thread_is_rejected(self):
        del self.data['thread_id']
        self.write()
        self.reject('thread_id')

    def test_invalid_json_or_nonobject_denied(self):
        for raw in (b'{', b'[]', b'null', b'"value"'):
            with self.subTest(raw=raw):
                self.path.write_bytes(raw)
                self.reject()

    def test_bad_marker_denied(self):
        self.marker.write_bytes(b'ccc-request-credentials-v0\n')
        self.reject('not enabled')

    def test_missing_marker_denied(self):
        self.marker.unlink()
        self.reject()

    def test_readable_file_or_directory_denied(self):
        for path, mode in ((self.path, 0o644), (self.marker, 0o644),
                           (self.directory, 0o755), (self.home, 0o770)):
            with self.subTest(path=path):
                previous = path.stat().st_mode & 0o777
                path.chmod(mode)
                self.reject('untrusted')
                path.chmod(previous)

    def test_foreign_owner_denied(self):
        with patch.object(foreground.os, 'getuid', return_value=os.getuid() + 1):
            self.reject('untrusted')

    def test_symlink_record_denied(self):
        target = self.directory / 'target.json'
        self.path.rename(target)
        self.path.symlink_to(target)
        self.reject()

    def test_symlink_marker_denied(self):
        target = self.directory / 'marker-target'
        self.marker.rename(target)
        self.marker.symlink_to(target)
        self.reject()

    def test_symlink_directory_denied(self):
        target = self.home / 'observations-real'
        self.directory.rename(target)
        self.directory.symlink_to(target, target_is_directory=True)
        self.reject('untrusted')

    def test_oversize_record_denied(self):
        self.path.write_bytes(b' ' * 4097)
        self.reject('untrusted')

    def test_nonregular_record_denied_without_blocking(self):
        self.path.unlink()
        os.mkfifo(self.path, mode=0o600)
        self.reject('untrusted')

    def test_record_replaced_between_reads_denied(self):
        original = foreground._read
        calls = 0
        def read(path, limit):
            nonlocal calls
            if path == self.path:
                calls += 1
                if calls == 2:
                    temporary = self.directory / 'replacement'
                    self.write(temporary, thread_id=C)
                    temporary.replace(self.path)
            return original(path, limit)
        with patch.object(foreground, '_read', side_effect=read):
            self.reject('source changed')

    def test_marker_replaced_between_reads_denied(self):
        original = foreground._read
        calls = 0
        def read(path, limit):
            nonlocal calls
            if path == self.marker:
                calls += 1
                if calls == 2:
                    temporary = self.directory / 'replacement'
                    temporary.write_bytes(self.marker.read_bytes())
                    temporary.chmod(0o600)
                    temporary.replace(self.marker)
            return original(path, limit)
        with patch.object(foreground, '_read', side_effect=read):
            self.reject('source changed')

    def test_capacity_does_not_evict_observed_identity(self):
        foreground.read(self.client)
        foreground._seen.update((pid, (100, 0)) for pid in range(100, 4195))
        self.assertEqual(len(foreground._seen), 4096)
        self.assertEqual(foreground.read(self.client)['thread_id'], S)
        self.client['birth'] = [100, 0]
        self.reject('capacity exceeded')


class ForegroundIdentityTests(EvidenceFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        fixture = baseline.DaemonTests()
        fixture.setUp()
        self.fixture = fixture
        self.processes = fixture.processes
        self.processes[30].update(copy.deepcopy(self.client))
        self.processes[30]['env'].update(fixture.client_env)
        self.chain = [self.processes[20], self.processes[10]]

    def collect(self, pids=None):
        with patch.object(identity, 'client_candidates', return_value=pids or ['10', '30']), \
             patch.object(identity, 'process', side_effect=lambda pid, **kwargs: copy.deepcopy(self.processes[pid])), \
             patch.object(identity.subprocess, 'run', return_value=SimpleNamespace(stdout='ttys1')):
            return identity._collect_client(S, self.chain, self.processes[10])

    def test_new_thread_collects_and_resolves_real_uuid(self):
        proof = self.collect()
        self.assertEqual(proof['client']['pid'], 30)
        self.assertEqual(proof['foreground']['thread_id'], S)
        resolved, env = identity.resolve(self.fixture.identity, self.fixture.tree, self.fixture.env, proof)
        self.assertEqual(resolved['caller']['surface_id'], C)
        self.assertEqual(env['CMUX_SURFACE_ID'], C)
        self.assertEqual(guard.resolve_snapshot(resolved, self.fixture.tree, 'surface:2', env=env)['caller_surface_uuid'], C)
        with self.assertRaises(guard.WorkspaceScopeError):
            guard.resolve_snapshot(resolved, self.fixture.tree, 'surface:46', env=env)

    def test_observed_thread_overrides_old_resume_argv(self):
        self.processes[30]['argv'] = ['/bin/codex', 'resume', C]
        self.assertEqual(self.collect()['client']['pid'], 30)

    def test_switched_away_or_cleared_cannot_fall_back_to_argv(self):
        self.processes[30]['argv'] = ['/bin/codex', 'resume', S]
        for thread in (C, None):
            with self.subTest(thread=thread):
                self.write(thread_id=thread)
                with self.assertRaisesRegex(identity.IdentityError, 'unique'):
                    self.collect()

    def test_required_missing_cannot_fall_back_to_argv(self):
        self.processes[30]['argv'] = ['/bin/codex', 'resume', S]
        self.processes[30]['env']['CODEX_CLIENT_THREAD_OBSERVER'] = '1'
        self.path.unlink()
        with self.assertRaisesRegex(identity.IdentityError, 'selection unavailable'):
            self.collect()

    def test_legacy_resume_proof_shape_preserved(self):
        self.processes[30]['argv'] = ['/bin/codex', 'resume', S]
        self.path.unlink()
        self.assertNotIn('foreground', self.collect())

    def test_two_foreground_clients_same_session_denied(self):
        self.processes[31] = dict(copy.deepcopy(self.processes[30]), pid=31)
        self.write(self.directory / 'client-31-thread.json', pid=31)
        with self.assertRaisesRegex(identity.IdentityError, 'unique'):
            self.collect(['30', '31'])

    def test_foreground_plus_legacy_duplicate_denied(self):
        self.processes[31] = dict(copy.deepcopy(self.processes[30]), pid=31,
                                  argv=['/bin/codex', 'resume', S])
        with self.assertRaisesRegex(identity.IdentityError, 'unique'):
            self.collect(['30', '31'])

    def test_switch_during_inventory_denied(self):
        original = foreground.read
        calls = 0
        def read(client):
            nonlocal calls
            if client['pid'] == 30:
                calls += 1
                if calls == 2:
                    self.write(thread_id=C)
            return original(client)
        with patch.object(foreground, 'read', side_effect=read):
            with self.assertRaisesRegex(identity.IdentityError, 'changed during inventory'):
                self.collect()

    def test_invalid_unrelated_selection_is_excluded_without_editing_evidence(self):
        self.processes[31] = dict(copy.deepcopy(self.processes[30]), pid=31)
        path = self.directory / 'client-31-thread.json'
        self.write(path, pid=31, thread_id='invalid')
        before = (path.read_bytes(), path.stat().st_ino)
        proof = self.collect(['30', '31'])
        self.assertEqual(proof['client']['pid'], 30)
        self.assertEqual(proof['foreground']['thread_id'], S)
        self.assertEqual((path.read_bytes(), path.stat().st_ino), before)

    def test_nineteen_foreign_future_timestamps_do_not_veto_valid_caller(self):
        pids = ['30']
        for pid in range(31, 50):
            self.processes[pid] = dict(copy.deepcopy(self.processes[30]), pid=pid)
            self.write(self.directory / f'client-{pid}-thread.json', pid=pid,
                       published_at_ms=999999)
            pids.append(str(pid))
        proof = self.collect(pids)
        self.assertEqual(proof['client']['pid'], 30)
        self.assertEqual(proof['foreground']['thread_id'], S)

    def test_invalid_own_timestamp_cannot_fall_back_to_resume(self):
        self.processes[30]['argv'] = ['/bin/codex', 'resume', S]
        self.write(published_at_ms=999999)
        with self.assertRaisesRegex(identity.IdentityError, 'selection unavailable.*future'):
            self.collect()

    def test_invalid_foreign_record_does_not_hide_valid_duplicate(self):
        for pid in (31, 32):
            self.processes[pid] = dict(copy.deepcopy(self.processes[30]), pid=pid)
            self.write(self.directory / f'client-{pid}-thread.json', pid=pid,
                       thread_id=S if pid == 31 else 'invalid')
        with self.assertRaisesRegex(identity.IdentityError, 'unique'):
            self.collect(['32', '30', '31'])

    def test_unrelated_required_missing_selection_does_not_veto(self):
        self.processes[31] = dict(copy.deepcopy(self.processes[30]), pid=31)
        self.processes[31]['env']['CODEX_CLIENT_THREAD_OBSERVER'] = '1'
        self.assertEqual(self.collect(['31', '30'])['client']['pid'], 30)

    def test_foreign_failures_do_not_change_selected_proof(self):
        self.processes[31] = dict(copy.deepcopy(self.processes[30]), pid=31)
        path = self.directory / 'client-31-thread.json'
        expected = self.collect(['30', '31'])
        for raw in ('{', '[]', json.dumps(dict(self.data, pid=31, published_at_ms=999999))):
            with self.subTest(raw=raw):
                path.write_text(raw)
                path.chmod(0o600)
                self.assertEqual(self.collect(['30', '31']), expected)

    def test_foreign_recheck_error_does_not_veto_selected_caller(self):
        self.processes[31] = dict(copy.deepcopy(self.processes[30]), pid=31)
        self.write(self.directory / 'client-31-thread.json', pid=31, thread_id=C)
        original = foreground.read
        for error in (OSError('unreadable'), ValueError('bad timestamp'),
                      KeyError('thread_id'), TypeError('malformed')):
            with self.subTest(error=error):
                calls = 0
                def read(client):
                    nonlocal calls
                    if client['pid'] == 31:
                        calls += 1
                        if calls == 2:
                            raise error
                    return original(client)
                with patch.object(foreground, 'read', side_effect=read):
                    self.assertEqual(self.collect(['30', '31'])['client']['pid'], 30)

    def test_selected_recheck_error_remains_fatal(self):
        original = foreground.read
        calls = 0
        def read(client):
            nonlocal calls
            if client['pid'] == 30:
                calls += 1
                if calls == 2:
                    raise ValueError('bad selected timestamp')
            return original(client)
        with patch.object(foreground, 'read', side_effect=read):
            with self.assertRaisesRegex(identity.IdentityError, 'recheck failed'):
                self.collect()

    def test_invalid_foreign_record_recovering_to_duplicate_is_denied(self):
        self.processes[31] = dict(copy.deepcopy(self.processes[30]), pid=31)
        path = self.directory / 'client-31-thread.json'
        self.write(path, pid=31, thread_id='invalid')
        original = foreground.read
        calls = 0
        def read(client):
            nonlocal calls
            if client['pid'] == 31:
                calls += 1
                if calls == 2:
                    self.write(path, pid=31)
            return original(client)
        with patch.object(foreground, 'read', side_effect=read):
            with self.assertRaisesRegex(identity.IdentityError, 'second active client'):
                self.collect(['30', '31'])

    def test_unrelated_switch_between_other_threads_does_not_veto(self):
        self.processes[31] = dict(copy.deepcopy(self.processes[30]), pid=31)
        path = self.directory / 'client-31-thread.json'
        self.write(path, pid=31, thread_id=None)
        original = foreground.read
        calls = 0
        def read(client):
            nonlocal calls
            if client['pid'] == 31:
                calls += 1
                if calls == 2:
                    self.write(path, pid=31, thread_id=C)
            return original(client)
        with patch.object(foreground, 'read', side_effect=read):
            self.assertEqual(self.collect(['30', '31'])['client']['pid'], 30)

    def test_unrelated_reused_pid_does_not_block_current_caller(self):
        self.processes[31] = dict(copy.deepcopy(self.processes[30]), pid=31)
        self.write(self.directory / 'client-31-thread.json', pid=31,
                   published_at_ms=100099)
        proof = self.collect(['30', '31'])
        self.assertEqual(proof['client']['pid'], 30)
        self.assertEqual(proof['foreground']['thread_id'], S)
        resolved, env = identity.resolve(self.fixture.identity, self.fixture.tree,
                                         self.fixture.env, proof)
        self.assertEqual(resolved['caller']['surface_id'], C)
        self.assertEqual(env['CMUX_SURFACE_ID'], C)
        self.assertNotIn((31, (100, 100000)), foreground._seen)

    def test_old_record_cannot_select_its_previous_thread(self):
        self.write(published_at_ms=100099)
        with self.assertRaisesRegex(identity.IdentityError, 'unique'):
            self.collect()

    def test_optional_old_record_keeps_exact_legacy_resume(self):
        self.write(published_at_ms=100099, thread_id=C)
        self.processes[30]['argv'] += ['resume', S]
        proof = self.collect()
        self.assertEqual(proof['client']['pid'], 30)
        self.assertNotIn('foreground', proof)

    def test_old_record_cannot_hide_duplicate_live_resume_client(self):
        self.processes[31] = dict(copy.deepcopy(self.processes[30]), pid=31,
                                  argv=['/bin/codex', 'resume', S])
        self.write(self.directory / 'client-31-thread.json', pid=31,
                   published_at_ms=100099, thread_id=C)
        with self.assertRaisesRegex(identity.IdentityError, 'unique'):
            self.collect(['30', '31'])

    def test_required_unrelated_old_record_is_excluded(self):
        self.processes[31] = dict(copy.deepcopy(self.processes[30]), pid=31)
        self.processes[31]['env']['CODEX_CLIENT_THREAD_OBSERVER'] = '1'
        self.write(self.directory / 'client-31-thread.json', pid=31,
                   published_at_ms=100099)
        self.assertEqual(self.collect(['30', '31'])['client']['pid'], 30)

    def test_foreground_proof_drift_fails_final_snapshot(self):
        proof = self.collect()
        changed = copy.deepcopy(proof)
        changed['foreground']['client_epoch'] = S
        with patch.object(guard.daemon_identity, 'collect', side_effect=[proof, proof, changed]), \
             patch.object(guard, '_read_json_command', side_effect=[self.fixture.identity, self.fixture.tree,
                                                                    self.fixture.identity, self.fixture.tree]), \
             patch.dict(os.environ, self.fixture.env, clear=True):
            with self.assertRaisesRegex(guard.WorkspaceScopeError, 'final check'):
                guard.caller_snapshot()

    def test_kernel_args_expose_only_foreground_location_allowlist(self):
        raw = (bytes(ctypes.c_int(1)) + b'/bin/codex\0\0/bin/codex\0'
               + b'HOME=/home/test\0CODEX_HOME=/codex\0CODEX_CLIENT_THREAD_OBSERVER=1\0'
               + b'API_KEY=do-not-export\0CODEX_CREDENTIAL_OBSERVATIONS_DIR=/untrusted\0\0')
        argv, env = identity._args(raw)
        self.assertEqual(argv, ['/bin/codex'])
        self.assertEqual(env, dict(HOME='/home/test', CODEX_HOME='/codex', CODEX_CLIENT_THREAD_OBSERVER='1'))


if __name__ == '__main__':
    unittest.main()
