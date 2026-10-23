"""Real archived continuation, binding, reader, locks and original writer.

Only external I/O is replaced. --original-controller validates the same tests
against one complete immutable historical release in a fresh process.
"""
import copy
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import sys
import unittest

import recover_bridge_probe_clear as clear

if __name__ == '__main__' and '--original-controller' in sys.argv:
    index = sys.argv.index('--original-controller')
    clear.continuation.original_modules(sys.argv[index + 1])
    del sys.argv[index:index + 2]

import cmux_bridge as bridge
import cmux_evidence_io as evidence
import cmux_input_mutex as mutex
import cmux_native_delivery as native
import mac_harness as harness
import test_bridge_continuation as fixture_support


class ProbeClearTests(unittest.TestCase):
    patch = fixture_support.BridgeContinuationTests.patch
    save = fixture_support.BridgeContinuationTests.save
    send = fixture_support.BridgeContinuationTests.send
    key = fixture_support.BridgeContinuationTests.key
    files = fixture_support.BridgeContinuationTests.files

    def read_screen(self, *args, **kwargs):
        screen = fixture_support.BridgeContinuationTests.read_screen(self, *args, **kwargs)
        return screen.replace('[claude-opus-5]', 'unknown footer') if self.unparsed and self.compose else screen

    def setUp(self):
        self.unparsed = True
        fixture_support.BridgeContinuationTests.setUp(self)
        self.args.apply = True
        result = clear.continuation.continue_probe(self.args, self.modules)
        self.assertEqual(result['status'], 'BRIDGE_CONTINUATION_FAILED_NO_REPLAY', result)
        self.assertEqual(result['probe']['clear_key_count'], 0)
        self.assertTrue(result['probe']['token_sent'])
        self.fixture.key.assert_not_called()
        self.unparsed = False
        self.args.publish_bridge_evidence = False
        self.args.archive_sha256 = None
        self.args.apply = False
        # Production arm_task already creates these participant lock files.
        # The fixture writes its marker directly, so initialize the real lock
        # infrastructure before asserting that rejected applies change no files.
        with harness._marker_update_lock(self.marker_path, self.marker):
            pass
        self.original_files = self.files()
        self.original_bridge = (self.root / 'bridge-test-evidence.json').read_bytes()
        self.preview = clear.recover_clear(self.args, self.modules)
        self.assertEqual(self.preview['status'], 'STATIC_ELIGIBLE_NO_INPUT', self.preview)
        self.args.archive_sha256 = self.preview['archive_sha256']
        self.fixture.send.reset_mock()
        self.fixture.key.reset_mock()
        self.caller.reset_mock()
        self.screen.reset_mock()

    def run_clear(self, apply=True, publish=False):
        self.args.apply = apply
        self.args.publish_bridge_evidence = publish
        value = clear.recover_clear(self.args, self.modules)
        self.fixture.send.assert_not_called()
        self.fixture.rpc.assert_not_called()
        self.assertEqual(value['submission_operations'], 0)
        self.assertEqual(value['probe_invocations'], 0)
        return value

    def refuse(self, error, *, publish=False):
        before = self.files()
        value = self.run_clear(publish=publish)
        self.assertEqual(value['status'], 'PROBE_CLEAR_UNAVAILABLE', value)
        expected = (error,) if isinstance(error, str) else error
        self.assertTrue(any(item in value['error'] for item in expected), value)
        self.assertFalse(value['archive_created'])
        self.fixture.key.assert_not_called()
        self.assertEqual(before, self.files())
        return value

    def test_static_preview_is_read_only(self):
        before = self.files()
        value = self.run_clear(apply=False)
        self.assertEqual(value['status'], 'STATIC_ELIGIBLE_NO_INPUT', value)
        self.assertEqual(before, self.files())
        self.screen.assert_not_called()
        self.caller.assert_not_called()
        self.fixture.key.assert_not_called()

    def test_clear_only_retains_all_original_bytes(self):
        value = self.run_clear()
        self.assertEqual(value['status'], 'PROBE_CLEARED_ONLY', value)
        self.assertEqual(self.compose, '')
        self.assertEqual(value['input_operations'], len(self.token) + 1)
        self.assertEqual(self.fixture.key.call_args_list,
                         [unittest.mock.call('target', k) for k in ['end'] + ['backspace'] * len(self.token)])
        self.assertEqual(value['bridge_writes'], 0)
        for path, pin in self.original_files.items():
            self.assertEqual(evidence.snapshot(Path(path)), pin)
        slot = self.root / clear.ARCHIVE
        self.assertEqual(json.loads((slot / 'RESULT.json').read_bytes()), value)
        samples = list(slot.glob('*-before-*.json')) + list(slot.glob('*-after-*.json'))
        self.assertGreaterEqual(len(samples), 4 * (len(self.token) + 1))
        for sample in samples:
            saved = json.loads(sample.read_bytes())
            self.assertIn('screen', saved)
            self.assertEqual(saved['body'], saved['expected_prefix'])
            self.assertFalse(saved['busy'])

    def test_original_writer_publishes_linked_clear_proof_only(self):
        value = self.run_clear(publish=True)
        self.assertEqual(value['status'], 'PROBE_CLEARED_BRIDGE_PUBLISHED', value)
        self.assertEqual(value['bridge_writes'], 1)
        current = json.loads((self.root / 'bridge-test-evidence.json').read_bytes())
        self.assertTrue(current['clear_confirmed'])
        self.assertEqual(current['token'], self.token)
        self.assertEqual(current['clear_recovery']['archive_sha256'], self.preview['archive_sha256'])
        self.assertEqual(current, dict(current['executors'][0], executors=current['executors']))
        self.assertFalse((self.root / 'handshake-receipt.json').exists())
        for path, pin in self.original_files.items():
            if Path(path).parent == self.root / clear.continuation.ARCHIVE:
                self.assertEqual(evidence.snapshot(Path(path)), pin)

    def test_released_task_can_clear_but_never_rearm_or_publish(self):
        self.marker_path.unlink()
        value = self.run_clear()
        self.assertEqual(value['status'], 'PROBE_CLEARED_ONLY', value)
        self.assertFalse(value['reservation_present'])
        self.assertFalse(self.marker_path.exists())
        self.assertEqual((self.root / 'bridge-test-evidence.json').read_bytes(), self.original_bridge)

    def test_released_task_cannot_publish(self):
        self.marker_path.unlink()
        self.refuse('RELEASED_TASK_CANNOT_PUBLISH', publish=True)

    def test_expired_original_reservation_cannot_publish(self):
        self.patch(clear.continuation.time, 'time', return_value=(
            datetime.now(timezone.utc) + timedelta(hours=7)).timestamp())
        self.refuse('EXPIRED_OR_INVALID', publish=True)

    def test_original_owner_only(self):
        self.caller.return_value[3]['foreground']['thread_id'] = 'maintenance-thread'
        self.refuse('SUPERVISOR_THREAD_CHANGED')

    def test_original_native_identity_is_not_rebound(self):
        with self.patch(native, 'bind_target', side_effect=AssertionError('must never rebind')):
            self.fixture.process_state['birth'] = 'replacement process'
            self.refuse('PROCESS_BIRTH_OR_EXECUTABLE_CHANGED')

    def test_changed_pane_or_session_refused(self):
        self.fixture.identity['target_pane_uuid'] = 'different-pane'
        self.refuse('TARGET_IDENTITY_CHANGED')

    def test_archive_pin_required(self):
        self.args.archive_sha256 = '0' * 64
        self.refuse('PREVIEW_ARCHIVE_PIN_REQUIRED')

    def test_archive_change_after_preview(self):
        path = self.root / clear.continuation.ARCHIVE / 'RESULT.json'
        path.write_bytes(path.read_bytes() + b' ')
        self.refuse('PREVIEW_ARCHIVE_PIN_REQUIRED')

    def test_changed_original_evidence(self):
        (self.root / 'naming-proof.json').write_text('{}')
        self.refuse('ORIGINAL_FILES_CHANGED')

    def test_original_exit_attestation_required(self):
        self.args.original_harness_exited = False
        self.refuse('HARNESS_EXIT_ATTESTATION_REQUIRED')

    def test_foreign_draft_whitespace_and_token_prefix_refused(self):
        for text in ('user draft', ' ' + self.token, self.token + ' ', self.token + '\n',
                     self.token + '\u00a0', self.token[:-1], ''):
            with self.subTest(text=text):
                self.compose = text
                self.refuse('COMPOSER_CHANGED_PRESERVE')

    def test_unknown_footer_cannot_be_treated_as_idle(self):
        self.unparsed = True
        self.refuse('COMPOSER_BUSY_OR_UNKNOWN')

    def test_busy_and_queue_screens(self):
        original = self.read_screen
        for prefix in ('✻ Thinking…\n', 'Messages to be submitted after\n'):
            with self.subTest(prefix=prefix):
                self.screen.side_effect = lambda *a, **k: prefix + original()
                self.refuse('COMPOSER_BUSY_OR_UNKNOWN')

    def test_native_user_or_queue_forbids_clear(self):
        self.fixture.append_user(self.token)
        self.refuse('NATIVE_RECEIVED_QUEUED_OR_UNKNOWN')

    def test_native_queued_attachment_forbids_clear(self):
        self.fixture.append_queued(self.token)
        self.refuse('NATIVE_RECEIVED_QUEUED_OR_UNKNOWN')

    def test_native_enqueue_before_attachment_forbids_clear(self):
        self.fixture.append_record(dict(type='queue-operation', operation='enqueue',
            sessionId=self.fixture.session_id, content=self.token))
        self.refuse('NATIVE_QUEUE_OPERATION_PRESERVE')

    def test_partial_native_record_forbids_clear(self):
        with self.fixture.transcript.open('ab') as stream:
            stream.write(b'{"type":"user"')
        self.refuse('NATIVE_RECEIVED_QUEUED_OR_UNKNOWN')

    def test_other_executor_reservation_forbids_clear(self):
        other = copy.deepcopy(self.marker)
        other.update(collaboration_id='another-reservation', task_id='other-task')
        (self.marker_path.parent / 'another.json').write_text(json.dumps(other))
        self.refuse('OTHER_OR_UNKNOWN_RESERVATION')

    def test_real_input_mutex_contention(self):
        with mutex.hold(self.fixture.identity):
            self.refuse('INPUT_CHANNEL_BUSY')

    def test_real_root_lock_contention(self):
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.refuse('temporarily unavailable')
        finally:
            os.close(fd)

    def test_real_reservation_lock_contention(self):
        with harness._marker_update_lock(self.marker_path, self.marker):
            self.refuse(('executor inquiry episode is busy', 'temporarily unavailable'))

    def test_no_second_key_after_user_edit(self):
        self.fixture.key.side_effect = lambda *a: setattr(self, 'compose', 'user typed something')
        value = self.run_clear()
        self.assertEqual(value['status'], 'PROBE_CLEAR_INCOMPLETE_NO_REPLAY', value)
        self.assertIn('COMPOSER_CHANGED_PRESERVE', value['error'])
        self.assertEqual(self.fixture.key.call_count, 1)
        self.assertEqual(self.compose, 'user typed something')
        self.assertEqual((self.root / 'bridge-test-evidence.json').read_bytes(), self.original_bridge)

    def test_no_second_key_after_queue_arrives(self):
        self.fixture.key.side_effect = lambda *a: self.fixture.append_queued(self.token)
        value = self.run_clear()
        self.assertEqual(value['status'], 'PROBE_CLEAR_INCOMPLETE_NO_REPLAY', value)
        self.assertIn('NATIVE_RECEIVED_QUEUED_OR_UNKNOWN', value['error'])
        self.assertEqual(self.fixture.key.call_count, 1)

    def test_uncertain_key_permanently_spends_slot(self):
        self.fixture.key.side_effect = RuntimeError('uncertain key result')
        value = self.run_clear()
        self.assertEqual(value['status'], 'PROBE_CLEAR_INCOMPLETE_NO_REPLAY', value)
        self.assertIsNone(value['input_operations'])
        self.assertEqual(value['key_intents'], 1)
        self.assertTrue((self.root / clear.ARCHIVE / '00-KEY-INTENT.json').exists())
        self.fixture.key.reset_mock()
        self.refuse('SLOT_EXISTS_NO_REPLAY')

    def test_interrupt_after_intent_permanently_spends_slot(self):
        self.fixture.key.side_effect = KeyboardInterrupt('simulated crash')
        with self.assertRaises(KeyboardInterrupt):
            self.run_clear()
        self.assertTrue((self.root / clear.ARCHIVE / '00-KEY-INTENT.json').exists())
        self.fixture.key.reset_mock()
        self.refuse('SLOT_EXISTS_NO_REPLAY')

    def test_publication_exception_has_no_automatic_replay(self):
        self.patch(harness, '_write_bridge_evidence', side_effect=RuntimeError('publisher failed'))
        value = self.run_clear(publish=True)
        self.assertTrue(value['clear_confirmed'])
        self.assertEqual(value['status'], 'PROBE_CLEAR_INCOMPLETE_NO_REPLAY', value)
        self.assertTrue((self.root / clear.ARCHIVE / 'PUBLISH-INTENT.json').exists())
        self.fixture.key.reset_mock()
        self.refuse('SLOT_EXISTS_NO_REPLAY')

    def test_release_between_clear_and_publication_cannot_publish(self):
        old_key = self.key
        def release(*args):
            old_key(*args)
            if self.compose == '':
                self.marker_path.unlink()
        self.fixture.key.side_effect = release
        value = self.run_clear(publish=True)
        self.assertEqual(value['status'], 'PROBE_CLEAR_INCOMPLETE_NO_REPLAY', value)
        self.assertIn('RESERVATION_BOUNDARY_CHANGED', value['error'])
        self.assertEqual(value['bridge_writes'], 0)
        self.assertFalse(self.marker_path.exists())

    def test_delayed_key_observed_without_another_key(self):
        pending = []
        reads = [0]

        def key(surface, name):
            if name == 'backspace':
                pending.append((surface, name))
                reads[0] = 0

        def read():
            if pending:
                reads[0] += 1
                if reads[0] == 3:
                    self.key(*pending.pop())

        self.fixture.key.side_effect = key
        self.read_action = read
        value = self.run_clear()
        self.assertEqual(value['status'], 'PROBE_CLEARED_ONLY', value)
        self.assertEqual(self.fixture.key.call_count, len(self.token) + 1)
        self.assertEqual(self.compose, '')
        self.assertEqual(pending, [])

    def test_key_without_effect_stops_without_retry(self):
        self.fixture.key.side_effect = lambda *a: None
        value = self.run_clear()
        self.assertEqual(value['status'], 'PROBE_CLEAR_INCOMPLETE_NO_REPLAY', value)
        self.assertIn('COMPOSER_NOT_STABLE', value['error'])
        self.assertEqual(self.fixture.key.call_count, 2)  # end, one backspace
        self.assertEqual(self.compose, self.token)
        self.fixture.key.reset_mock()
        self.refuse('SLOT_EXISTS_NO_REPLAY')

    def test_archive_mutation_after_key_stops_input(self):
        path = self.root / clear.continuation.ARCHIVE / 'RESULT.json'
        self.fixture.key.side_effect = lambda *a: path.write_bytes(path.read_bytes() + b' ')
        value = self.run_clear()
        self.assertEqual(value['status'], 'PROBE_CLEAR_INCOMPLETE_NO_REPLAY', value)
        self.assertIn('ARCHIVE_FILE_CHANGED', value['error'])
        self.assertEqual(self.fixture.key.call_count, 1)
        self.assertFalse(value['clear_confirmed'])
        self.assertEqual(value['bridge_writes'], 0)

    def test_interrupt_after_original_writer_has_no_replay(self):
        writer = harness._write_bridge_evidence

        def write_then_interrupt(*args):
            writer(*args)
            raise KeyboardInterrupt('interrupted after publication')

        self.patch(harness, '_write_bridge_evidence', side_effect=write_then_interrupt)
        with self.assertRaises(KeyboardInterrupt):
            self.run_clear(publish=True)
        slot = self.root / clear.ARCHIVE
        self.assertTrue((slot / 'PUBLISH-INTENT.json').exists())
        self.assertFalse((slot / 'RESULT.json').exists())
        current = json.loads((self.root / 'bridge-test-evidence.json').read_bytes())
        self.assertTrue(current['clear_confirmed'])
        self.assertEqual(current['clear_recovery']['archive_sha256'], self.preview['archive_sha256'])
        self.fixture.key.reset_mock()
        self.refuse('ORIGINAL_FILES_CHANGED')

    def test_original_heartbeat_under_reservation_lock_is_nonblocking(self):
        # Keep the real writer and real marker locks. Heartbeat is best effort;
        # it cannot acquire our held locks or delay the evidence publication.
        touch = self.patch(harness, '_touch_active_marker', wraps=harness._touch_active_marker)
        value = self.run_clear(publish=True)
        self.assertEqual(value['status'], 'PROBE_CLEARED_BRIDGE_PUBLISHED', value)
        touch.assert_called_once_with(self.root / 'bridge-test-evidence.json')
        self.assertEqual(json.loads(self.marker_path.read_bytes()), self.marker)


class CursorProbeClearTests(unittest.TestCase):
    """An editor model independent of the cleanup's expected-prefix logic.

    All terminal I/O is replaced. A real trailing space and a painted cursor
    cell start with identical text AND cursor coordinates in the key regression.
    Only the editor's response to Home/End can distinguish them.
    """
    patch = ProbeClearTests.patch
    save = ProbeClearTests.save
    send = ProbeClearTests.send
    key = ProbeClearTests.key
    files = ProbeClearTests.files
    read_screen = ProbeClearTests.read_screen
    run_clear = ProbeClearTests.run_clear
    refuse = ProbeClearTests.refuse

    def setUp(self):
        ProbeClearTests.setUp(self)
        self.args.verify_cursor_navigation = True
        self.editor_text = self.token
        self.editor_position = len(self.token)
        self.columns = 24
        self.grid_change = lambda value: None
        self.after_key = lambda key: None
        self.delayed_keys = set()
        self.ignored_keys = set()
        self.pending_key = None
        self.delay_frames = 0
        self.screen.side_effect = self.editor_screen
        self.fixture.key.side_effect = self.editor_key
        original_run = self.fixture.run.side_effect
        self.grid_read = unittest.mock.Mock(side_effect=self.grid_rpc)
        self.fixture.run.reset_mock()
        self.fixture.run.side_effect = lambda *a, **k: (
            self.grid_read(*a, **k) if a[:2] == ('rpc', 'terminal.replay')
            else original_run(*a, **k))

    def editor_body(self):
        # A block cursor at EOL occupies a painted blank cell. It is not part
        # of editor_text and never changes the editor's navigation/deletion.
        return self.editor_text + (' ' if self.editor_position == len(self.editor_text) else '')

    def editor_screen(self, *args, **kwargs):
        if self.pending_key is not None:
            if self.delay_frames:
                self.delay_frames -= 1
            else:
                self.move_editor(self.pending_key)
                self.pending_key = None
        return '\n'.join(('─' * self.columns, '❯ ' + self.editor_body(),
                          '─' * self.columns, '[claude-opus-5]'))

    def move_editor(self, key):
        if key == 'home':
            self.editor_position = 0
        elif key == 'end':
            self.editor_position = len(self.editor_text)
        else:
            self.assertEqual(key, 'backspace')
            self.assertGreater(self.editor_position, 0)
            at = self.editor_position
            self.editor_text = self.editor_text[:at - 1] + self.editor_text[at:]
            self.editor_position -= 1

    def editor_key(self, surface, key):
        self.assertEqual(surface, 'target')
        self.assertIn(key, ('home', 'end', 'backspace'))
        intents = sorted((self.root / clear.ARCHIVE).glob('*-KEY-INTENT.json'))
        self.assertEqual(json.loads(intents[-1].read_bytes())['key'], key)
        if key in self.delayed_keys:
            self.pending_key, self.delay_frames = key, 2
        elif key not in self.ignored_keys:
            self.move_editor(key)
        self.after_key(key)

    def grid_rpc(self, *args, **kwargs):
        self.assertEqual(args[:2], ('rpc', 'terminal.replay'))
        self.assertEqual(len(args), 3)
        self.assertEqual(json.loads(args[2]), dict(workspace_id='workspace', surface_id='target'))
        # Do not advance the editor here: this is the same painted state that
        # the preceding text observation saw, with its actual cursor cells.
        lines = ['─' * self.columns, '❯ ' + self.editor_body(),
                 '─' * self.columns, '[claude-opus-5]']
        col = self.editor_position + 2
        spans = []
        for row, line in enumerate(lines):
            segments = [(0, line, 0)] if row != 1 else [
                (0, line[:col], 0), (col, line[col:col + 1], 1), (col + 1, line[col + 1:], 0)]
            for column, text, style in segments:
                if text:
                    spans.append(dict(row=row, column=column, cell_width=len(text),
                                      text=text, style_id=style))
        value = dict(workspace_id='workspace', surface_id='target',
            columns=self.columns, rows=4, render_grid=dict(
                surface_id='target', format='cmux.render-grid.v1', full=True,
                active_screen='primary', anchor='viewport', columns=self.columns, rows=4,
                styles=[dict(id=0, invisible=False, inverse=False),
                        dict(id=1, invisible=False, inverse=True)], row_spans=spans,
                cursor=dict(column=col, row=1, style='block', visible=False, blinking=True)))
        self.grid_change(value)
        return json.dumps(value)

    def keys(self):
        return [call.args[1] for call in self.fixture.key.call_args_list]

    def incomplete(self, expected_keys, error):
        value = self.run_clear()
        self.assertEqual(value['status'], 'PROBE_CLEAR_INCOMPLETE_NO_REPLAY', value)
        self.assertIn(error, value['error'])
        self.assertEqual(self.keys(), expected_keys)
        self.assertFalse(value['clear_confirmed'])
        self.assertEqual(value['bridge_writes'], 0)
        self.fixture.key.reset_mock()
        self.refuse('SLOT_EXISTS_NO_REPLAY')
        return value

    def test_painted_cell_requires_home_and_end_before_deletion(self):
        value = self.run_clear()
        self.assertEqual(value['status'], 'PROBE_CLEARED_ONLY', value)
        self.assertEqual(self.keys(), ['home', 'end'] + ['backspace'] * len(self.token))
        self.assertEqual(self.editor_text, '')
        self.assertEqual(value['input_operations'], len(self.token) + 2)
        self.assertEqual(value['cursor_navigation'], 'HOME_THEN_END_ACKNOWLEDGED')
        self.assertEqual(value['bridge_writes'], 0)
        for path, pin in self.original_files.items():
            self.assertEqual(evidence.snapshot(Path(path)), pin)
        slot = self.root / clear.ARCHIVE
        for sample in slot.glob('*-after-*.json'):
            saved = json.loads(sample.read_bytes())
            self.assertIn('grid_reply', saved)
            self.assertEqual(saved['body'], saved['cursor_proof']['body'])
        self.assertEqual(json.loads((slot / 'RESULT.json').read_bytes()), value)

    def test_opt_in_required_and_rejected_screen_preserved(self):
        self.args.verify_cursor_navigation = False
        value = self.refuse('COMPOSER_CHANGED_PRESERVE')
        self.assertEqual(value['last_observation']['body'], self.token + ' ')
        self.assertIn('screen', value['last_observation'])
        self.grid_read.assert_not_called()

    def test_real_tail_space_identical_initial_picture_never_deleted(self):
        original = self.grid_rpc('rpc', 'terminal.replay', json.dumps(
            dict(workspace_id='workspace', surface_id='target')))
        self.editor_text += ' '
        # Cursor before the real space: indistinguishable from the painted
        # cell before navigation, including inverse style and coordinates.
        self.assertEqual(original, self.grid_rpc('rpc', 'terminal.replay', json.dumps(
            dict(workspace_id='workspace', surface_id='target'))))
        self.incomplete(['home', 'end'], 'CURSOR_OR_CONTENT_CHANGED_PRESERVE')
        self.assertEqual(self.editor_text, self.token + ' ')

    def test_real_tail_with_cursor_at_actual_end_rejected_before_key(self):
        self.editor_text += ' '
        self.editor_position += 1
        self.refuse('COMPOSER_CHANGED_PRESERVE')

    def test_nbsp_prefix_changed_token_and_foreign_draft_preserved(self):
        for text in (self.token + '\u00a0', ' ' + self.token, self.token + 'x',
                     self.token[:-1], 'user draft', self.token + '  '):
            with self.subTest(text=text):
                self.editor_text, self.editor_position = text, len(text)
                self.refuse(('COMPOSER_CHANGED_PRESERVE', 'COMPOSER_BUSY_OR_UNKNOWN'))

    def test_wrong_initial_cursor_cannot_authorize_navigation(self):
        self.grid_change = lambda value: value['render_grid']['cursor'].update(column=3)
        self.refuse('CURSOR_UNPROVEN')

    def test_unhandled_home_exhausts_original_slot_without_delete(self):
        self.ignored_keys.add('home')
        self.incomplete(['home'], 'COMPOSER_NOT_STABLE')
        self.assertEqual(self.editor_text, self.token)

    def test_unhandled_end_exhausts_original_slot_without_delete(self):
        self.ignored_keys.add('end')
        self.incomplete(['home', 'end'], 'COMPOSER_NOT_STABLE')
        self.assertEqual(self.editor_text, self.token)

    def test_delayed_navigation_and_deletion_are_observed_never_repeated(self):
        self.delayed_keys.update(('home', 'end', 'backspace'))
        value = self.run_clear()
        self.assertEqual(value['status'], 'PROBE_CLEARED_ONLY', value)
        self.assertEqual(self.keys(), ['home', 'end'] + ['backspace'] * len(self.token))

    def test_user_edit_after_home_stops_before_end(self):
        self.after_key = lambda key: setattr(self, 'editor_text', 'edited by user')
        self.incomplete(['home'], 'CURSOR_OR_CONTENT_CHANGED_PRESERVE')
        self.assertEqual(self.editor_text, 'edited by user')

    def test_space_added_after_verified_end_is_preserved(self):
        def edit(key):
            if key == 'end':
                self.editor_text += ' '
                self.editor_position += 1
        self.after_key = edit
        self.incomplete(['home', 'end'], 'CURSOR_OR_CONTENT_CHANGED_PRESERVE')
        self.assertEqual(self.editor_text, self.token + ' ')

    def test_key_exception_retains_intent_and_disallows_another_attempt(self):
        self.fixture.key.side_effect = RuntimeError('key result unknown')
        value = self.incomplete(['home'], 'key result unknown')
        self.assertIsNone(value['input_operations'])
        self.assertEqual(value['key_intents'], 1)

    def test_interrupt_retains_original_slot(self):
        self.fixture.key.side_effect = KeyboardInterrupt('crash')
        with self.assertRaises(KeyboardInterrupt):
            self.run_clear()
        self.assertTrue((self.root / clear.ARCHIVE / '00-KEY-INTENT.json').exists())
        self.fixture.key.reset_mock()
        self.refuse('SLOT_EXISTS_NO_REPLAY')

    def test_queue_after_navigation_stops_before_another_key(self):
        self.after_key = lambda key: self.fixture.append_queued(self.token)
        self.incomplete(['home'], 'NATIVE_RECEIVED_QUEUED_OR_UNKNOWN')

    def test_receiver_identity_changed_during_grid_read_is_zero_key(self):
        self.grid_change = lambda value: self.fixture.identity.update(target_pane_uuid='other-pane')
        self.refuse('TARGET_IDENTITY_CHANGED')

    def test_busy_screen_prevents_even_grid_read(self):
        self.screen.side_effect = lambda *a, **k: '✻ Thinking…\n' + self.editor_screen()
        self.refuse('COMPOSER_BUSY_OR_UNKNOWN')
        self.grid_read.assert_not_called()

    def test_missing_partial_foreign_or_alternate_grid_never_authorizes_key(self):
        cases = [
            ('missing-grid', lambda v: v.pop('render_grid')),
            ('workspace', lambda v: v.update(workspace_id='other-workspace')),
            ('surface', lambda v: v.update(surface_id='other-surface')),
            ('grid-surface', lambda v: v['render_grid'].update(surface_id='other-surface')),
            ('partial', lambda v: v['render_grid'].update(full=False)),
            ('alternate', lambda v: v['render_grid'].update(active_screen='alternate')),
            ('scrollback', lambda v: v['render_grid'].update(anchor='scrollback')),
            ('dimensions', lambda v: v.update(columns=25)),
            ('duplicate-style', lambda v: v['render_grid']['styles'].append(
                dict(v['render_grid']['styles'][0]))),
            ('invisible', lambda v: v['render_grid']['styles'][1].update(invisible=True)),
            ('no-cursor-cell', lambda v: v['render_grid']['styles'][1].update(inverse=False)),
            ('wrong-cursor-row', lambda v: v['render_grid']['cursor'].update(row=2)),
            ('overlapping', lambda v: v['render_grid']['row_spans'].append(
                dict(v['render_grid']['row_spans'][1]))),
            ('span-gap', lambda v: v['render_grid']['row_spans'][1].update(column=1)),
            ('wide-probe', lambda v: v['render_grid']['row_spans'][1].update(cell_width=12)),
            ('different-screen', lambda v: v['render_grid']['row_spans'][1].update(text='❯ X')),
        ]
        for name, change in cases:
            with self.subTest(case=name):
                self.grid_change = change
                self.refuse(('GRID_', 'render_grid'))

    def test_grid_read_failure_is_zero_key(self):
        self.grid_read.side_effect = RuntimeError('replay unavailable')
        value = self.refuse('replay unavailable')
        self.assertEqual(value['last_observation']['grid_error'], 'replay unavailable')

    def test_geometry_change_after_home_preserves_text(self):
        self.after_key = lambda key: setattr(self, 'columns', 25)
        self.incomplete(['home'], 'GRID_GEOMETRY_CHANGED')
        self.assertEqual(self.editor_text, self.token)

    def test_released_reservation_remains_released(self):
        self.marker_path.unlink()
        value = self.run_clear()
        self.assertEqual(value['status'], 'PROBE_CLEARED_ONLY', value)
        self.assertFalse(self.marker_path.exists())
        self.assertEqual((self.root / 'bridge-test-evidence.json').read_bytes(), self.original_bridge)


if __name__ == '__main__':
    unittest.main()
