"""Existing batch button routing with a real Unix endpoint; no native input."""
import copy
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from tests.context_fixture import enter_context
from unittest.mock import patch
import uuid

import cmux_codex_watch as core
import ccc_workspace_batch as batch
import ccc_standby_entry as entry
import ccc_standby_launch as launch
import ccc_standby_service as service
from ccc_native_standby import POLICY
from ccc_private_check import POLICY as CHECK_POLICY
from ccc_standby_timing import ORIGIN_PHASES


class Owner:
    def __init__(self, selected):
        self.selected = selected
        self.phase, self.action = 'ready', None
        self.calls = []
        self.on_status = lambda: None

    def status(self):
        self.on_status()
        return {**self.selected, 'state': self.phase, 'action_id': self.action,
                'job_terminal': False, 'run_terminal': False}

    def activate(self, origin, *, action_guard):
        if not action_guard():
            raise ValueError('revoked')
        self.calls.append(copy.deepcopy(origin))
        self.action, self.phase = origin['action_id'], 'activation_queued'
        return self.status()


class EntryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ccc-entry-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.config = self.root / 'config.json'
        self.wid, self.jid, self.boot = (str(uuid.uuid4()) for _ in range(3))
        self.store = core.ConfigStore(self.config)
        self.store.mutate(lambda c: c.update(mode='armed', global_paused=False,
            workspace_rules=[{'workspace_id': self.wid, 'enabled': True, 'paused': False,
                'last_batch_id': self.jid, 'active_batch_id': self.jid}]))
        self.job = {'id': self.jid, 'workspace_id': self.wid, 'config_path': str(self.config),
            'standby_policy': POLICY, 'standby_mode': 'b', 'status': 'pending',
            'standby_cohort_id': str(uuid.uuid4()), 'standby_generation': 'a' * 64,
            'standby_boot_id': self.boot, 'initial_prompt': batch.PROMPT,
            'cwd_policy': batch.EMPTY_CWD_POLICY, 'check_retry_policy': CHECK_POLICY,
            'native_runtime_policy': batch.NATIVE_RUNTIME_POLICY,
            'slots': [{'index': i, 'launch_id': str(uuid.uuid4())} for i in range(50)]}
        self.jobfile = batch.job_path(self.config, self.jid)
        core.atomic_write_json(self.jobfile, self.job)
        self.before = self.jobfile.read_bytes()
        self.owner = Owner(launch.policy(self.job, self.config))
        self.binding = entry.owner_path(self.config, self.jid)
        self.binding.parent.mkdir(mode=0o700)
        self.socket_temp = tempfile.TemporaryDirectory(prefix='ccc-entry-', dir='/private/tmp')
        self.addCleanup(self.socket_temp.cleanup)
        self.endpoint = service.ServiceEndpoint(self.owner, Path(self.socket_temp.name),
                                                binding_path=self.binding)
        self.addCleanup(self.endpoint.close)
        self.hashes = {'runtime.py': 'a' * 64}
        enter_context(self, patch.object(entry.ui, 'source_hashes', return_value=self.hashes))
        enter_context(self, patch.object(entry.ui, 'boot_id', return_value=self.boot))
        enter_context(self, patch.object(batch, 'workspace_record', return_value={'workspace_id': self.wid}))
        self.launch = enter_context(self, patch.object(batch, '_launch', side_effect=AssertionError('cold launch')))
        self.origin = {'version': 1, 'action_id': str(uuid.uuid4()), 'workspace_id': self.wid,
            'mode': 'b', 'input_kind': 'keyboard', 'row_kind': 'group', 'source_hashes': self.hashes,
            'events': [{'phase': phase, 'wall': 100+i*.01, 'monotonic': 10+i*.01, 'boot_id': self.boot}
                       for i, phase in enumerate(ORIGIN_PHASES)]}

    def invoke(self, **kw):
        values = {'private_check': True, 'ui_trace': self.origin}
        values.update(kw)
        return batch.start(self.config, self.wid, **values)

    def hint_fixture(self):
        targets = [{'surface_id': str(uuid.uuid4()), 'workspace_id': self.wid} for _ in range(50)]
        holds = {t['surface_id']: {'job_id': self.jid, 'index': i} for i, t in enumerate(targets)}
        self.store.mutate(lambda c: c['workspace_rules'][0].update(batch_start_holds=holds))
        for i, t in enumerate(targets):
            core.atomic_write_json(self.jobfile.parent / f'surface-{i}.json',
                {**t, 'job_id': self.jid, 'index': i, 'launch_id': self.job['slots'][i]['launch_id']})
        return entry.PeriodicObservationHint(), targets

    def isolated_hint_status(self):
        # Cache and authorization tests retain real files, socket identity and
        # owner callbacks, but must not depend on the best-effort 50 ms RPC.
        clock = enter_context(self, patch.object(entry, 'time'))
        clock.monotonic.return_value = 100.0
        def status(spec_path, sha, operation, *, status_timeout):
            self.assertEqual((spec_path, sha, operation),
                             (self.endpoint.spec_path, self.endpoint.sha256, 'status'))
            self.assertEqual(status_timeout, .05)
            return self.owner.status()
        rpc = enter_context(self, patch.object(entry, 'request', side_effect=status))
        return clock, rpc

    def test_periodic_hint_coalesces_original_fifty_without_activation(self):
        hint, targets = self.hint_fixture()
        _, rpc = self.isolated_hint_status()
        self.assertTrue(all(hint.covered(self.config, t) for t in targets))
        self.assertEqual(rpc.call_count, 1)
        self.assertEqual(self.owner.calls, [])

    def test_periodic_hint_only_covers_pre_activation_phases(self):
        hint, targets = self.hint_fixture()
        _, rpc = self.isolated_hint_status()
        for phase in ('ready', 'activation_queued', 'preparing', 'activating', 'failed', 'closed'):
            with self.subTest(phase=phase):
                self.owner.phase = phase
                hint.cache.clear()
                self.assertEqual(hint.covered(self.config, targets[0]), phase in {'ready', 'activation_queued'})
        self.assertEqual(rpc.call_count, 6)

    def test_periodic_hint_expiry_observes_phase_change(self):
        hint, targets = self.hint_fixture()
        clock, rpc = self.isolated_hint_status()
        self.assertTrue(hint.covered(self.config, targets[0]))
        self.owner.phase = 'activating'
        clock.monotonic.return_value = 100.101
        self.assertFalse(hint.covered(self.config, targets[0]))
        self.assertEqual(rpc.call_count, 2)

    def test_periodic_hint_hold_release_and_pause_invalidate_cache(self):
        self.isolated_hint_status()
        for change in ('hold', 'pause', 'target', 'active'):
            with self.subTest(change=change):
                hint, targets = self.hint_fixture()
                original = self.store.load()
                self.assertTrue(hint.covered(self.config, targets[0]))
                def mutate(c):
                    rule = c['workspace_rules'][0]
                    if change == 'hold':
                        rule['batch_start_holds'].clear()
                    elif change == 'pause':
                        c['global_paused'] = True
                    elif change == 'target':
                        c['targets'] = [{**targets[0], 'paused': True}]
                    else:
                        rule['active_batch_id'] = str(uuid.uuid4())
                self.store.mutate(mutate)
                self.assertFalse(hint.covered(self.config, targets[0]))
                core.atomic_write_json(self.config, original)

    def test_periodic_hint_live_callback_drift_rejected(self):
        hint, targets = self.hint_fixture()
        _, rpc = self.isolated_hint_status()
        self.owner.on_status = lambda: self.store.mutate(lambda c: c.update(global_paused=True))
        self.assertFalse(hint.covered(self.config, targets[0]))
        self.assertTrue(self.store.load()['global_paused'])
        self.assertEqual(rpc.call_count, 1)

    def test_cached_configuration_permissions_are_rechecked_before_coverage(self):
        hint, targets = self.hint_fixture()
        _, rpc = self.isolated_hint_status()
        self.assertTrue(hint.covered(self.config, targets[0]))
        self.config.chmod(0o644)
        self.assertFalse(hint.covered(self.config, targets[0]))
        self.assertEqual(rpc.call_count, 1)

    def test_periodic_hint_registration_boot_and_foreign_owner_rejected(self):
        hint, targets = self.hint_fixture()
        _, rpc = self.isolated_hint_status()
        self.assertFalse(hint.covered(self.config, {**targets[0], 'workspace_id': str(uuid.uuid4())}))
        with patch.object(entry.ui, 'boot_id', return_value=str(uuid.uuid4())):
            self.assertFalse(hint.covered(self.config, targets[0]))
        receipt = self.jobfile.parent / 'surface-0.json'
        original = receipt.read_bytes()
        value = core.load_json(receipt, {})
        core.atomic_write_json(receipt, {**value, 'launch_id': str(uuid.uuid4())})
        self.assertFalse(hint.covered(self.config, targets[0]))
        receipt.write_bytes(original)
        self.owner.selected = {**self.owner.selected, 'cohort_id': str(uuid.uuid4())}
        self.assertFalse(hint.covered(self.config, targets[0]))
        self.assertEqual(rpc.call_count, 1)

    def test_periodic_hint_missing_or_slow_service_falls_back(self):
        hint, targets = self.hint_fixture()
        self.owner.on_status = lambda: time.sleep(.12)
        started = time.monotonic()
        self.assertFalse(hint.covered(self.config, targets[0]))
        self.assertLess(time.monotonic() - started, .11)
        self.endpoint.close()
        self.assertFalse(hint.covered(self.config, targets[0]))

    def test_short_status_deadline_cannot_apply_to_activation(self):
        with self.assertRaises(ValueError):
            service.request(self.endpoint.spec_path, self.endpoint.sha256, 'activate', status_timeout=.05)

    def test_periodic_hint_cached_reply_cannot_survive_socket_disappearance(self):
        hint, targets = self.hint_fixture()
        _, rpc = self.isolated_hint_status()
        self.assertTrue(hint.covered(self.config, targets[0]))
        self.endpoint.close()
        self.assertFalse(hint.covered(self.config, targets[0]))
        self.assertEqual(rpc.call_count, 1)

    def test_periodic_hint_cached_reply_cannot_survive_descriptor_change(self):
        hint, targets = self.hint_fixture()
        _, rpc = self.isolated_hint_status()
        self.assertTrue(hint.covered(self.config, targets[0]))
        self.assertTrue(hint.covered(self.config, targets[0]))
        spec = core.load_json(self.endpoint.spec_path, {})
        core.atomic_write_json(self.endpoint.spec_path, {**spec, 'nonce': 'replacement'})
        self.assertFalse(hint.covered(self.config, targets[0]))
        self.assertEqual(rpc.call_count, 1)

    def test_status_total_deadline_expires_despite_individual_reads_progressing(self):
        from unittest.mock import MagicMock
        # Each partial read advances time but would fit a per-recv timeout.
        clock = [100.0]
        connection = MagicMock()
        connection.__enter__.return_value = connection
        def partial(_):
            clock[0] += .02
            return b' '
        connection.recv.side_effect = partial
        with patch.object(service.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(service.socket, 'socket', return_value=connection):
            with self.assertRaises(TimeoutError):
                service.request(self.endpoint.spec_path, self.endpoint.sha256, 'status', status_timeout=.05)
        self.assertEqual(connection.recv.call_count, 3)

    def test_b_button_uses_original_endpoint_and_preserves_job_and_origin(self):
        before = copy.deepcopy(self.origin)
        result = self.invoke()
        self.assertEqual(self.owner.calls, [before])
        self.assertEqual(result['job_id'], self.jid)
        self.assertFalse(result['new_job'])
        self.assertEqual(self.origin, before)
        self.assertEqual(self.jobfile.read_bytes(), self.before)
        self.launch.assert_not_called()

    def test_second_button_is_status_only_for_original_action(self):
        self.invoke()
        old = self.origin['action_id']
        self.origin['action_id'] = str(uuid.uuid4())
        result = self.invoke()
        self.assertEqual(result['standby']['action_id'], old)
        self.assertEqual(len(self.owner.calls), 1)

    def test_preparing_does_not_activate_or_create(self):
        self.owner.phase = 'preparing'
        result = self.invoke()
        self.assertEqual(result['standby']['state'], 'preparing')
        self.assertEqual(self.owner.calls, [])
        self.launch.assert_not_called()

    def test_missing_owner_never_falls_back_to_cold_launch(self):
        self.endpoint.close()
        with self.assertRaises((OSError, ValueError)):
            self.invoke()
        self.launch.assert_not_called()
        self.assertEqual(self.jobfile.read_bytes(), self.before)

    def test_original_B_and_legacy_gateway_and_dry_call_rejected(self):
        for kw in ({'private_check': False}, {'launch': False},
                   {'private_check': False, 'access_check': True}):
            with self.subTest(kw=kw), self.assertRaises(RuntimeError):
                self.invoke(**kw)
        self.assertEqual(self.owner.calls, [])

    def test_missing_wrong_mode_source_or_incomplete_ui_origin_rejected(self):
        for value in (None, {**self.origin, 'mode': 'N'},
                      {**self.origin, 'source_hashes': {'other.py': 'b'*64}},
                      {**self.origin, 'events': self.origin['events'][:-1]}):
            with self.subTest(value=value), self.assertRaises((RuntimeError, ValueError)):
                self.invoke(ui_trace=value)
        self.assertEqual(self.owner.calls, [])

    def test_N_button_only_activates_N_job(self):
        self.job.update(standby_mode='N', native_access_policy=batch.NATIVE_ACCESS_POLICY)
        core.atomic_write_json(self.jobfile, self.job)
        self.endpoint.close()
        self.endpoint.spec_path.unlink()
        self.binding.unlink()
        self.owner.selected = launch.policy(self.job, self.config)
        self.endpoint = service.ServiceEndpoint(self.owner, self.endpoint.directory,
                                                binding_path=self.binding)
        self.addCleanup(self.endpoint.close)
        self.origin['mode'] = 'N'
        result = self.invoke(private_check=False, native_access=True)
        self.assertEqual(result['standby']['mode'], 'N')
        self.assertEqual(len(self.owner.calls), 1)

    def test_status_callback_cancels_live_authorization_before_activation(self):
        self.owner.on_status = lambda: self.store.mutate(lambda c: c.update(global_paused=True))
        with self.assertRaisesRegex(RuntimeError, '暂停'):
            self.invoke()
        self.assertEqual(self.owner.calls, [])

    def test_status_callback_changes_job_before_activation(self):
        self.owner.on_status = lambda: core.atomic_write_json(self.jobfile, {**self.job, 'changed': True})
        with self.assertRaisesRegex(RuntimeError, '变化'):
            self.invoke()
        self.assertEqual(self.owner.calls, [])

    def test_foreign_reply_cannot_authorize_activation(self):
        original = self.owner.status
        self.owner.status = lambda: {**original(), 'cohort_id': str(uuid.uuid4())}
        with self.assertRaisesRegex(RuntimeError, '其他批次'):
            self.invoke()
        self.assertEqual(self.owner.calls, [])

    def test_unknown_activation_reply_consumed_no_retry(self):
        original = self.owner.activate
        def lose(*a, **kw):
            original(*a, **kw)
            raise OSError('ACK lost')
        self.owner.activate = lose
        with self.assertRaises(ValueError):
            self.invoke()
        result = self.invoke()
        self.assertEqual(len(self.owner.calls), 1)
        self.assertEqual(result['standby']['action_id'], self.origin['action_id'])

    def test_deep_job_binding_uses_short_socket_and_original_descriptor(self):
        self.assertGreater(len(str(self.binding.with_suffix('.sock')).encode()), 104)
        self.assertLess(len(str(self.endpoint.socket_path).encode()), 104)
        self.assertEqual(self.invoke()['job_id'], self.jid)

    def test_binding_replacement_during_status_never_activates(self):
        self.owner.on_status = lambda: self.binding.write_bytes(self.binding.read_bytes() + b' ')
        with self.assertRaises((RuntimeError, ValueError)):
            self.invoke()
        self.assertEqual(self.owner.calls, [])


class ConfigurationReadTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='ccc-config-read-')
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name).resolve() / 'config.json'
        self.before = b'{"serial":"AAAA"}'
        self.after = b'{"serial":"BBBB"}'
        self.path.write_bytes(self.before)
        self.path.chmod(0o600)
        self.hint = entry.PeriodicObservationHint()

    def test_unchanged_private_bytes_reuse_decode_but_public_reads_are_independent(self):
        from ccc_standby_timing import _read
        with patch.object(entry.json, 'loads', wraps=json.loads) as decode:
            self.assertEqual(self.hint._config(self.path), ({'serial': 'AAAA'}, self.before))
            self.assertEqual(self.hint._config(self.path), ({'serial': 'AAAA'}, self.before))
            self.assertEqual(decode.call_count, 1)
        first, _ = _read(self.path)
        first['serial'] = 'changed by caller'
        self.assertEqual(_read(self.path), ({'serial': 'AAAA'}, self.before))

    def test_same_length_edit_with_restored_mtime_cannot_reuse_old_authorization(self):
        self.hint._config(self.path)
        original = self.path.stat()
        self.path.write_bytes(self.after)
        os.utime(self.path, ns=(original.st_atime_ns, original.st_mtime_ns))
        self.assertEqual(self.path.stat().st_size, original.st_size)
        self.assertEqual(self.path.stat().st_mtime_ns, original.st_mtime_ns)
        self.assertEqual(self.hint._config(self.path), ({'serial': 'BBBB'}, self.after))

    def test_cached_private_bytes_do_not_bypass_permissions_or_symlink_checks(self):
        self.hint._config(self.path)
        self.path.chmod(0o644)
        with self.assertRaises(ValueError):
            self.hint._config(self.path)
        self.path.chmod(0o600)
        original = self.path.with_name('original.json')
        self.path.rename(original)
        self.path.symlink_to(original)
        with self.assertRaises(ValueError):
            self.hint._config(self.path)

    def test_malformed_replacement_does_not_fall_back_to_cached_configuration(self):
        self.hint._config(self.path)
        self.path.write_bytes(b'[' + b' ' * (len(self.before) - 1))
        with self.assertRaises(ValueError):
            self.hint._config(self.path)

    def test_overlapping_reads_keep_each_decoded_value_bound_to_its_exact_bytes(self):
        decoding = threading.Event()
        release = threading.Event()
        results, errors = [], []
        original_decode = json.loads

        def decode(raw):
            if raw == self.before:
                decoding.set()
                if not release.wait(2):
                    raise TimeoutError('test decode was not released')
            return original_decode(raw)

        def read_old():
            try:
                results.append(self.hint._config(self.path))
            except Exception as error:
                errors.append(error)

        with patch.object(entry.json, 'loads', side_effect=decode):
            worker = threading.Thread(target=read_old)
            worker.start()
            try:
                self.assertTrue(decoding.wait(2))
                self.path.write_bytes(self.after)
                self.assertEqual(self.hint._config(self.path), ({'serial': 'BBBB'}, self.after))
            finally:
                release.set()
                worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(results, [({'serial': 'AAAA'}, self.before)])
            self.assertEqual(self.hint._config(self.path), ({'serial': 'BBBB'}, self.after))


if __name__ == '__main__':
    unittest.main()
