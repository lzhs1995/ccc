"""The real queued-body reader, including wrong sessions and failed output."""
import contextlib
import fcntl
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import claude_maintenance_context as context
import cmux_native_delivery as native
import cmux_workflow_advisory as advisory
import cmux_hook_runtime_audit as audit


class ClaudeMaintenanceContextTests(unittest.TestCase):
    SESSION = '11111111-1111-4111-8111-111111111111'
    FOREIGN = '22222222-2222-4222-8222-222222222222'

    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.home = Path(stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        stack.enter_context(patch.object(Path, 'home', return_value=self.home))
        self.log = self.home / '.claude/projects/test' / (self.SESSION + '.jsonl')
        self.log.parent.mkdir(parents=True)
        self.log.write_text(json.dumps(dict(sessionId=self.SESSION, type='user')) + '\n')
        self.transcript = native._pin_file(self.log, 'claude', self.SESSION)
        self.body = self.home / 'body.txt'
        self.body.write_text('Keep the original session. Stop waiting for the failed supervisor.\n中文正文。')
        self.auth = self.home / 'authorization.json'
        self.auth.write_text('{"authorization_source":"user_message"}')
        self.request = self.home / 'request.txt'
        self.request.write_text('Expose this exact queued maintenance notice without replaying it.')
        self.reply = self.home / 'executor-reply.json'
        self.process = dict(pid=501, birth=[1, 2], executable='/test/claude.exe',
                            file_identity=[1, 2, 3, 4])
        self.binding = dict(provider='claude', session_id=self.SESSION,
                            process=self.process, transcript=self.transcript,
                            identity=dict(target_surface_uuid=self.FOREIGN))
        self.wire = 'STATUS: MESSAGE_REFERENCE_V2 maintenance SHA=' + context.pin(self.body)['sha256']
        self.binding['payload_sha256'] = context.digest(self.wire.encode())
        record = dict(type='attachment', sessionId=self.SESSION, isSidechain=False,
                      attachment=dict(type='queued_command', prompt=self.wire))
        self.queue = (json.dumps(record) + '\n').encode()
        with self.log.open('ab') as stream:
            stream.write(self.queue)
        self.attempt = self.home / 'attempt.json'
        self.attempt.write_text(json.dumps(dict(
            schema='successor-attempt-v2', marker='maintenance', native_binding=self.binding,
            artifact=dict(scope='maintenance_handshake', communication_only=True,
                          allow_task_replay=False, allow_shared_writes=False,
                          target_executor_uuid=self.FOREIGN, authorization_record=context.pin(self.auth)),
            wire=self.wire, wire_sha256=context.digest(self.wire.encode()),
            body=self.body.read_text(), body_reference=context.pin(self.body),
            body_sha256=context.pin(self.body)['sha256'])))
        self.offer_path, self.offer = context.enroll(
            self.attempt, self.transcript['offset'], len(self.queue), self.request, self.reply)
        self.caller = stack.enter_context(patch.object(context.identity, 'process',
            return_value=dict(self.process, ppid=500)))
        self.payload = dict(session_id=self.SESSION, transcript_path=str(self.log),
                            hook_event_name='PostToolUse')

    def invoke(self, payload=None, now=100):
        output = []
        context.discover(self.payload if payload is None else payload, emit=output.append, now=now)
        return output

    def test_complete_original_body_reaches_only_its_session_and_preserves_attempt(self):
        attempt_before = self.attempt.read_bytes()
        log_before = self.log.read_bytes()
        output = self.invoke()
        self.assertEqual(len(output), 1)
        self.assertIn(self.body.read_text(), output[0])
        self.assertIn(self.offer['offer_id'], output[0])
        self.assertIn(str(self.reply), output[0])
        self.assertIn('not a new terminal message', output[0])
        self.assertFalse(self.reply.exists())
        self.assertEqual(self.attempt.read_bytes(), attempt_before)
        self.assertEqual(self.log.read_bytes(), log_before)
        cursor = json.loads((context.root() / (self.SESSION + '.cursor.json')).read_text())
        self.assertFalse(cursor['native_received'])
        self.assertFalse(cursor['body_read_confirmed'])

    def test_absent_foreign_payload_or_wrong_event_is_silent(self):
        for change in [dict(session_id=self.FOREIGN), dict(session_id='unknown'),
                       dict(transcript_path='/other'), dict(hook_event_name='Stop'),
                       dict(hook_event_name='PreToolUse')]:
            with self.subTest(change=change):
                self.assertEqual(self.invoke(dict(self.payload, **change)), [])

    def test_same_pid_with_changed_birth_or_executable_does_not_receive(self):
        for change in [dict(birth=[2, 3]), dict(executable='/other'),
                       dict(file_identity=[4, 3, 2, 1]), dict(pid=502)]:
            with self.subTest(change=change):
                self.caller.return_value = dict(self.process, ppid=500, **change)
                self.assertEqual(self.invoke(), [])

    def test_manual_python_ancestor_is_not_an_automatic_hook(self):
        self.caller.return_value = dict(pid=400, ppid=501, executable='/test/python')
        self.assertEqual(self.invoke(), [])

    def test_shell_parent_of_real_claude_is_supported(self):
        self.caller.side_effect = [dict(pid=400, ppid=501, executable='/bin/sh'),
                                  dict(self.process, ppid=500)]
        self.assertEqual(len(self.invoke()), 1)

    def test_reminder_interval_and_failed_flush(self):
        def fail(_):
            raise BrokenPipeError('closed hook output')
        context.discover(self.payload, emit=fail, now=100)
        self.assertEqual(len(self.invoke(now=101)), 1)
        self.assertEqual(self.invoke(now=160), [])
        self.assertEqual(len(self.invoke(now=161)), 1)

    def test_only_exact_read_ack_suppresses_reminders(self):
        self.reply.write_text('{broken')
        self.assertEqual(len(self.invoke()), 1)
        template = dict(offer_id=self.offer['offer_id'], session_id=self.SESSION,
                        marker='maintenance', body_sha256=context.pin(self.body)['sha256'], body_read=True)
        for field in ['offer_id', 'session_id', 'marker', 'body_sha256', 'body_read']:
            self.reply.write_text(json.dumps(dict(template, **{field: False})))
            self.assertEqual(len(self.invoke(now=200 + 60 * list(template).index(field))), 1)
        self.reply.write_text(json.dumps(template))
        self.assertEqual(self.invoke(now=1000), [])

    def test_changed_pinned_body_attempt_authorization_or_request_is_silent(self):
        for path in [self.body, self.attempt, self.auth, self.request]:
            with self.subTest(path=path.name):
                original = path.read_bytes()
                path.write_bytes(original + b'changed')
                self.assertEqual(self.invoke(), [])
                path.write_bytes(original)

    def test_replaced_truncated_or_changed_original_log_is_silent(self):
        original = self.log.read_bytes()
        for raw in [b'', original[:-2], b'X' + original[1:]]:
            self.log.write_bytes(raw)
            self.assertEqual(self.invoke(), [])
        self.log.write_bytes(original)
        replacement = self.log.with_suffix('.replacement')
        replacement.write_bytes(original)
        os.replace(replacement, self.log)
        self.assertEqual(self.invoke(), [])

    def test_native_log_growth_is_allowed_without_forging_a_user_record(self):
        with self.log.open('a') as stream:
            stream.write(json.dumps(dict(type='assistant', sessionId=self.SESSION)) + '\n')
        self.assertEqual(len(self.invoke()), 1)

    def test_registry_symlink_and_idempotent_enrollment(self):
        before = self.offer_path.read_bytes()
        path, offer = context.enroll(self.attempt, self.transcript['offset'], len(self.queue), self.request, self.reply)
        self.assertEqual((path, offer), (self.offer_path, self.offer))
        self.assertEqual(self.offer_path.read_bytes(), before)
        original = self.offer_path.read_bytes()
        replacement = self.home / 'copy.json'
        replacement.write_bytes(original)
        self.offer_path.unlink()
        self.offer_path.symlink_to(replacement)
        self.assertEqual(self.invoke(), [])
        with self.assertRaises((OSError, RuntimeError, ValueError)):
            context.enroll(self.attempt, self.transcript['offset'], len(self.queue), self.request, self.reply)

    def test_repeat_after_read_preserves_reply_cursor_and_original_attempt(self):
        self.invoke()
        self.completed_reply()
        paths = [self.offer_path, self.reply, self.attempt,
                 context.root() / (self.SESSION + '.cursor.json')]
        before = {p: (p.stat().st_ino, p.read_bytes()) for p in paths}
        context.enroll(self.attempt, self.transcript['offset'], len(self.queue), self.request, self.reply,
                       replace_completed=self.offer['offer_id'])
        self.assertEqual({p: (p.stat().st_ino, p.read_bytes()) for p in paths}, before)

    def test_repeat_refuses_invalid_or_unrelated_replacement_id(self):
        self.completed_reply()
        before = self.offer_path.read_bytes()
        for value in ['invalid', '../outside', 'f' * 64]:
            with self.subTest(value=value), self.assertRaises((ValueError, OSError)):
                context.enroll(self.attempt, self.transcript['offset'], len(self.queue),
                               self.request, self.reply, replace_completed=value)
            self.assertEqual(self.offer_path.read_bytes(), before)

    def test_original_queue_must_follow_intent_and_match_whole_wire(self):
        for change in [dict(offset=0), dict(bytes=0), dict(sha256='0' * 64)]:
            bad = dict(self.offer, queued_record=dict(self.offer['queued_record'], **change))
            with self.assertRaises((ValueError, native.NativeDeliveryError)):
                context.material(bad)

    def test_installed_wrapper_uses_supported_json_and_never_blocks(self):
        for hook, expected in [('cmux_native_delivery_guard.py', True),
                               ('cmux_executor_idle_guard.py', False)]:
            out = io.StringIO()
            with patch.object(sys, 'stdin', io.StringIO(json.dumps(self.payload))), \
                 patch.object(sys, 'argv', ['advisory', '--hook', hook]), \
                 patch.object(audit, 'record', side_effect=OSError('telemetry unavailable')), \
                 contextlib.redirect_stdout(out):
                self.assertEqual(advisory.main(), 0)
            if expected:
                obj = json.loads(out.getvalue())
                self.assertEqual(set(obj), {'hookSpecificOutput'})
                self.assertEqual(set(obj['hookSpecificOutput']), {'hookEventName', 'additionalContext'})
                self.assertIn(self.body.read_text(), obj['hookSpecificOutput']['additionalContext'])
            else:
                self.assertEqual(out.getvalue(), '')

    def completed_reply(self):
        reply = dict(offer_id=self.offer['offer_id'], session_id=self.SESSION,
                     marker='maintenance', body_sha256=context.pin(self.body)['sha256'], body_read=True)
        self.reply.write_text(json.dumps(reply))
        return reply

    def next_notice(self, change_binding=None):
        body = self.home / 'next-body.txt'
        body.write_text('The prior task is accepted. Await the separately dispatched next task.\n新通知。')
        wire = 'STATUS: MESSAGE_REFERENCE_V2 next SHA=' + context.pin(body)['sha256']
        binding = json.loads(json.dumps(self.binding))
        binding['transcript'] = native._pin_file(self.log, 'claude', self.SESSION)
        binding['payload_sha256'] = context.digest(wire.encode())
        if change_binding:
            change_binding(binding)
        queue = (json.dumps(dict(type='attachment', sessionId=self.SESSION, isSidechain=False,
                    attachment=dict(type='queued_command', prompt=wire))) + '\n').encode()
        with self.log.open('ab') as stream:
            stream.write(queue)
        attempt = json.loads(self.attempt.read_text())
        attempt.update(marker='next', native_binding=binding, wire=wire,
                       wire_sha256=context.digest(wire.encode()), body=body.read_text(),
                       body_reference=context.pin(body), body_sha256=context.pin(body)['sha256'])
        path = self.home / 'next-attempt.json'
        path.write_text(json.dumps(attempt))
        return (path, binding['transcript']['offset'], len(queue), self.request, self.home / 'next-reply.json')

    def test_completed_offer_advances_atomically_and_preserves_original_evidence(self):
        self.invoke(now=100)
        self.completed_reply()
        args = self.next_notice()
        cursor = context.root() / (self.SESSION + '.cursor.json')
        preserved = {p: p.read_bytes() for p in (self.offer_path, self.reply, cursor, self.attempt, self.log)}
        inode = cursor.stat().st_ino
        _, new = context.enroll(*args, replace_completed=self.offer['offer_id'])
        archive = context.root() / 'history' / self.SESSION / self.offer['offer_id'] / new['offer_id']
        for name, original in [('offer.json', self.offer_path), ('reply.json', self.reply), ('cursor.json', cursor)]:
            self.assertEqual((archive / name).read_bytes(), preserved[original])
        for original in (self.reply, cursor, self.attempt, self.log):
            self.assertEqual(original.read_bytes(), preserved[original])
        self.assertEqual(cursor.stat().st_ino, inode)
        self.assertEqual(json.loads(self.offer_path.read_text())['offer_id'], new['offer_id'])
        self.assertFalse(args[-1].exists())
        output = self.invoke(now=101)
        self.assertEqual(len(output), 1)
        self.assertIn('新通知', output[0])
        self.assertEqual(json.loads(cursor.read_text())['exposures'], 1)

    def test_pending_or_wrong_read_reply_cannot_be_replaced(self):
        args = self.next_notice()
        before = self.offer_path.read_bytes()
        with self.assertRaises(OSError):
            context.enroll(*args, replace_completed=self.offer['offer_id'])
        reply = self.completed_reply()
        for field in reply:
            with self.subTest(field=field):
                self.reply.write_text(json.dumps(dict(reply, **{field: 'wrong'})))
                with self.assertRaisesRegex(ValueError, 'no exact executor body-read'):
                    context.enroll(*args, replace_completed=self.offer['offer_id'])
                self.assertEqual(self.offer_path.read_bytes(), before)

    def test_default_remains_exclusive_even_after_a_reply(self):
        self.completed_reply()
        with self.assertRaises(FileExistsError):
            context.enroll(*self.next_notice())

    def test_wrong_expected_offer_never_replaces_registry(self):
        self.completed_reply()
        args = self.next_notice()
        before = self.offer_path.read_bytes()
        for wrong in ['0' * 64, '../../outside', None]:
            with self.subTest(wrong=wrong), self.assertRaises((ValueError, FileExistsError)):
                context.enroll(*args, replace_completed=wrong)
            self.assertEqual(self.offer_path.read_bytes(), before)

    def test_same_attempt_cannot_be_reenrolled_with_a_fresh_reply(self):
        self.completed_reply()
        with self.assertRaisesRegex(ValueError, 'already enrolled'):
            context.enroll(self.attempt, self.transcript['offset'], len(self.queue), self.request,
                           self.home / 'another-reply.json', replace_completed=self.offer['offer_id'])

    def test_completed_rotation_requires_same_executor_process(self):
        self.completed_reply()
        args = self.next_notice(lambda b: b['process'].update(birth=[2, 3]))
        with self.assertRaisesRegex(ValueError, 'original executor session and process'):
            context.enroll(*args, replace_completed=self.offer['offer_id'])

    def test_authorized_new_supervisor_does_not_require_old_supervisor_approval(self):
        self.completed_reply()
        args = self.next_notice(lambda b: b['identity'].update(caller_surface_uuid=self.SESSION))
        _, new = context.enroll(*args, replace_completed=self.offer['offer_id'])
        self.assertNotEqual(new['offer_id'], self.offer['offer_id'])

    def test_cursor_lock_contention_is_bounded_and_keeps_old_offer(self):
        self.completed_reply()
        args = self.next_notice()
        before = self.offer_path.read_bytes()
        cursor = context.root() / (self.SESSION + '.cursor.json')
        with cursor.open('a+b') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):
                context.enroll(*args, replace_completed=self.offer['offer_id'])
        self.assertEqual(self.offer_path.read_bytes(), before)

    def test_failed_publication_preserves_old_offer_and_retry_reuses_archive(self):
        self.completed_reply()
        args = self.next_notice()
        before = self.offer_path.read_bytes()
        with patch.object(context.os, 'replace', side_effect=OSError('disk unavailable')):
            with self.assertRaises(OSError):
                context.enroll(*args, replace_completed=self.offer['offer_id'])
        self.assertEqual(self.offer_path.read_bytes(), before)
        _, new = context.enroll(*args, replace_completed=self.offer['offer_id'])
        self.assertEqual(json.loads(self.offer_path.read_text())['offer_id'], new['offer_id'])

    def test_changed_reply_during_archiving_does_not_publish_new_offer(self):
        self.completed_reply()
        args = self.next_notice()
        before = self.offer_path.read_bytes()
        archive = context._archive_bytes
        def changed(path, raw):
            archive(path, raw)
            if path.name == 'next-offer.json':
                self.reply.write_text('{}')
        with patch.object(context, '_archive_bytes', side_effect=changed):
            with self.assertRaisesRegex(ValueError, 'evidence changed'):
                context.enroll(*args, replace_completed=self.offer['offer_id'])
        self.assertEqual(self.offer_path.read_bytes(), before)

    def test_archive_symlink_does_not_replace_or_write_outside_registry(self):
        self.completed_reply()
        args = self.next_notice()
        outside = self.home / 'outside'
        outside.mkdir()
        (context.root() / 'history').symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'archive must be private'):
            context.enroll(*args, replace_completed=self.offer['offer_id'])
        self.assertEqual(list(outside.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
