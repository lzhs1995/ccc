"""Foreground selection through enrollment and real native bind/revalidation.

NativeCase replaces only kernel/ps/cmux I/O; foreground files, transcript
metadata, session selection, binding and validation stay real. Every terminal
input operation fails immediately in the fixture.
"""
import json
from contextlib import ExitStack
from unittest import TestCase, main
from unittest.mock import patch

import cmux_foreground_thread as foreground
import cmux_hook_scope as scope
import cmux_native_delivery as native
import test_native_independent as independent


class NativeForegroundTests(TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.case = self.stack.enter_context(independent.NativeCase(provider='codex'))
        seconds, microseconds = self.case.process['birth']
        self.previous_process_stamp_ms = seconds * 1000 + microseconds // 1000 - 1
        self.stack.enter_context(patch.object(foreground, '_seen', set()))
        self.case.process['argv'] = [self.case.process['executable']]
        home = self.case.home / '.codex'
        home.chmod(0o700)
        self.case.process['env']['CODEX_HOME'] = str(home)
        self.directory = home / 'credential-observations'
        self.directory.mkdir(mode=0o700)
        marker = self.directory / 'enabled-v1'
        marker.write_bytes(b'ccc-request-credentials-v1\n')
        marker.chmod(0o600)
        self.path = self.directory / f'client-{independent.PID}-thread.json'
        self.data = dict(schema=1, purpose='client_foreground_thread',
                         pid=independent.PID, client_epoch=independent.CALLER,
                         thread_id=independent.SESSION,
                         published_at_ms=independent.EPOCH * 1000)
        self.write()

    def write(self, **changes):
        self.path.write_text(json.dumps(dict(self.data, **changes)))
        self.path.chmod(0o600)

    def select(self):
        return native._live_session(self.case.process, 'codex')

    def enroll(self):
        return scope.bind_participants(self.case.bridge, independent.WORKSPACE,
            [dict(role='supervisor', surface_uuid=independent.TARGET, provider='codex')])

    def check_bound(self, binding, *, read_only=False):
        return native.require_bound(self.case.bridge, independent.TARGET, binding,
                                    independent.PAYLOAD, read_only=read_only)

    def test_new_client_without_resume_has_native_session(self):
        self.assertEqual(self.select(), independent.SESSION)

    def test_native_selection_supersedes_old_resume_argument(self):
        self.case.process['argv'] += ['resume', independent.OTHER_SESSION]
        self.assertEqual(self.select(), independent.SESSION)

    def test_switch_reads_new_native_selection(self):
        self.assertEqual(self.select(), independent.SESSION)
        self.write(thread_id=independent.OTHER_SESSION)
        self.assertEqual(self.select(), independent.OTHER_SESSION)

    def test_clear_does_not_fall_back_to_resume_argument(self):
        self.case.process['argv'] += ['resume', independent.SESSION]
        self.write(thread_id=None)
        with self.assertRaisesRegex(native.NativeDeliveryError, 'FOREGROUND_CLEARED'):
            self.select()

    def test_required_missing_does_not_fall_back_to_resume_argument(self):
        self.case.process['argv'] += ['resume', independent.SESSION]
        self.case.process['env']['CODEX_CLIENT_THREAD_OBSERVER'] = '1'
        self.path.unlink()
        with self.assertRaisesRegex(native.NativeDeliveryError, 'FOREGROUND_INVALID'):
            self.select()

    def test_previously_seen_missing_remains_invalid(self):
        self.case.process['argv'] += ['resume', independent.SESSION]
        self.select()
        self.path.unlink()
        with self.assertRaisesRegex(native.NativeDeliveryError, 'FOREGROUND_INVALID'):
            self.select()

    def test_malformed_observation_does_not_fall_back_to_resume(self):
        self.case.process['argv'] += ['resume', independent.SESSION]
        self.path.write_text('{')
        with self.assertRaisesRegex(native.NativeDeliveryError, 'FOREGROUND_INVALID'):
            self.select()

    def test_legacy_resume_remains_supported(self):
        self.path.unlink()
        self.case.process['argv'] += ['resume', independent.SESSION]
        self.assertEqual(self.select(), independent.SESSION)
        binding = self.case.bind()
        self.assertEqual(binding['session_id'], independent.SESSION)
        self.check_bound(binding)

    def test_reused_pid_old_record_binds_only_current_legacy_session(self):
        self.write(published_at_ms=self.previous_process_stamp_ms,
                   thread_id=independent.OTHER_SESSION)
        self.case.process['argv'] += ['resume', independent.SESSION]
        before = self.case.snapshot()
        binding = self.case.bind()
        self.assertEqual(binding['session_id'], independent.SESSION)
        self.assertEqual(binding['transcript']['path'], str(self.case.transcript))
        self.check_bound(binding)
        self.check_bound(binding, read_only=True)
        self.assertEqual(self.case.snapshot(), before)

    def test_reused_pid_without_current_session_stays_unresolved(self):
        self.write(published_at_ms=self.previous_process_stamp_ms)
        with self.assertRaisesRegex(native.NativeDeliveryError, 'SESSION_UNRESOLVED'):
            self.select()

    def test_required_old_record_remains_invalid_with_resume(self):
        self.write(published_at_ms=self.previous_process_stamp_ms)
        self.case.process['env']['CODEX_CLIENT_THREAD_OBSERVER'] = '1'
        self.case.process['argv'] += ['resume', independent.SESSION]
        with self.assertRaisesRegex(native.NativeDeliveryError, 'FOREGROUND_INVALID'):
            self.select()

    def test_bound_current_selection_cannot_be_replaced_by_old_record(self):
        self.case.process['argv'] += ['resume', independent.SESSION]
        binding = self.case.bind()
        self.write(published_at_ms=self.previous_process_stamp_ms)
        with self.assertRaisesRegex(native.NativeDeliveryError, 'FOREGROUND_INVALID'):
            self.check_bound(binding)

    def test_no_observation_or_legacy_argument_is_unresolved(self):
        self.path.unlink()
        with self.assertRaisesRegex(native.NativeDeliveryError, 'SESSION_UNRESOLVED'):
            self.select()

    def test_new_thread_enrolls_exact_native_session(self):
        result = self.enroll()
        self.assertEqual(result, [dict(role='supervisor', surface_uuid=independent.TARGET,
                                      provider='codex', native_session_id=independent.SESSION)])

    def test_enrollment_rechecks_session_after_process_read(self):
        original = foreground.read
        calls = 0
        def changing_read(process):
            nonlocal calls
            calls += 1
            if calls == 2:
                self.write(thread_id=independent.OTHER_SESSION)
            return original(process)
        with patch.object(foreground, 'read', side_effect=changing_read):
            with self.assertRaisesRegex(native.NativeDeliveryError, 'TASK_SCOPE_PROCESS_CHANGED'):
                self.enroll()
        self.assertEqual(calls, 2)

    def test_enrollment_clear_during_final_read_denied(self):
        original = foreground.read
        calls = 0
        def clearing_read(process):
            nonlocal calls
            calls += 1
            if calls == 2:
                self.write(thread_id=None)
            return original(process)
        with patch.object(foreground, 'read', side_effect=clearing_read):
            with self.assertRaisesRegex(native.NativeDeliveryError, 'FOREGROUND_CLEARED'):
                self.enroll()

    def test_bind_and_revalidate_use_foreground_session(self):
        before = self.case.snapshot()
        binding = self.case.bind()
        self.assertEqual(binding['session_id'], independent.SESSION)
        self.assertEqual(binding['transcript']['path'], str(self.case.transcript))
        self.check_bound(binding)
        self.check_bound(binding, read_only=True)
        self.assertEqual(self.case.snapshot(), before)

    def test_native_binding_switch_is_rejected_on_revalidation(self):
        binding = self.case.bind()
        self.write(thread_id=independent.OTHER_SESSION)
        self.case.create_transcript(independent.OTHER_SESSION)
        for read_only in (False, True):
            with self.subTest(read_only=read_only):
                with self.assertRaisesRegex(native.NativeDeliveryError, 'PROCESS_SESSION_CHANGED'):
                    self.check_bound(binding, read_only=read_only)

    def test_native_binding_clear_is_rejected_on_revalidation(self):
        binding = self.case.bind()
        self.write(thread_id=None)
        with self.assertRaisesRegex(native.NativeDeliveryError, 'FOREGROUND_CLEARED'):
            self.check_bound(binding)

    def test_native_binding_missing_selection_is_rejected_on_revalidation(self):
        binding = self.case.bind()
        self.path.unlink()
        with self.assertRaisesRegex(native.NativeDeliveryError, 'FOREGROUND_INVALID'):
            self.check_bound(binding)

    def test_switch_during_bind_cannot_return_stale_binding(self):
        original = foreground.read
        calls = 0
        def switching_read(process):
            nonlocal calls
            calls += 1
            if calls == 2:
                self.write(thread_id=independent.OTHER_SESSION)
            return original(process)
        with patch.object(foreground, 'read', side_effect=switching_read):
            with self.assertRaisesRegex(native.NativeDeliveryError, 'PROCESS_SESSION_CHANGED'):
                self.case.bind()

    def test_switched_session_binds_its_own_transcript(self):
        switched = self.case.create_transcript(independent.OTHER_SESSION)
        self.write(thread_id=independent.OTHER_SESSION)
        binding = self.case.bind()
        self.assertEqual(binding['session_id'], independent.OTHER_SESSION)
        self.assertEqual(binding['transcript']['path'], str(switched))
        self.check_bound(binding)


class ClaudeCompatibilityTests(TestCase):
    def test_claude_registration_still_overrides_argv_without_foreground_read(self):
        with independent.NativeCase(provider='claude') as case:
            case.process['argv'][-1] = independent.OTHER_SESSION
            with patch.object(foreground, 'read', side_effect=AssertionError('Claude has no Codex selector')):
                binding = case.bind()
                self.assertEqual(binding['session_id'], independent.SESSION)
                native.require_bound(case.bridge, independent.TARGET, binding, independent.PAYLOAD)


if __name__ == '__main__':
    main()
