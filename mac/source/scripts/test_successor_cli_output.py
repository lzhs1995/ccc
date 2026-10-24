"""CLI result persistence around real journals; terminal/process I/O is simulated."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import successor_native_delivery as delivery
import successor_rebind_cli as cli
import claude_maintenance_context as maintenance
import test_successor_native_delivery as delivery_fixtures
from test_successor_rebind import maintenance_artifact


class SuccessorOutputTests(unittest.TestCase):
    journal = delivery_fixtures.SuccessorNativeDeliveryTests.journal
    record_bytes = delivery_fixtures.SuccessorNativeDeliveryTests.record_bytes
    append_record = delivery_fixtures.SuccessorNativeDeliveryTests.append_record

    def setUp(self):
        # Share the real-file fixture without inheriting and rerunning its tests.
        delivery_fixtures.SuccessorNativeDeliveryTests.setUp(self)
        paths = delivery._paths
        for context in (
                patch.object(delivery, "_paths", side_effect=lambda a, m, state_root=None:
                             paths(a, m, self.root)),
                patch.dict(sys.modules, {"cmux_bridge": self.bridge})):
            context.start()
            self.addCleanup(context.stop)
        self.artifact_path = self.root / "artifact.json"
        self.artifact_path.write_text(json.dumps(self.artifact), encoding="utf-8")
        self.text_path = self.root / "message.txt"
        self.text_path.write_text(self.text, encoding="utf-8")
        self.output = self.root / "result.json"

    def invoke(self, command="validate", *, output=None, extra=()):
        args = [command, "--artifact", str(self.artifact_path), "--output",
                str(self.output if output is None else output)]
        if command in ("submit", "reconcile"):
            args += ["--text-file", str(self.text_path), "--marker", self.marker,
                     "--wait-seconds", "0"]
        capture = io.StringIO()
        with contextlib.redirect_stdout(capture):
            code = cli.main(args + list(extra))
        raw = capture.getvalue().encode("utf-8")
        return code, json.loads(raw), raw  # Reject any duplicate JSON document.

    def reservation(self, output=None):
        path = self.output if output is None else output
        return path.with_name("." + path.name + ".successor-output-reservation")

    def maintenance_fixture(self):
        log = self.root / '.claude/projects/test' / self.transcript.name
        log.parent.mkdir(parents=True)
        self.transcript.rename(log)
        self.transcript = log
        for context in (patch.object(Path, 'home', return_value=self.root),
                        patch.object(delivery.native, '_transcript_path', return_value=log)):
            context.start()
            self.addCleanup(context.stop)
        self.bridge.response = 'queued'
        self.maintenance_reply = self.root / 'body-read.json'
        return ['--enroll-maintenance-context', '--maintenance-reply', str(self.maintenance_reply)]

    def test_opt_in_enrolls_only_original_queue_and_reconcile_is_idempotent_after_reply(self):
        options = self.maintenance_fixture()
        self.text += '\n' + '完整维护正文与精确回件。\t' * 80
        self.text_path.write_text(self.text, encoding='utf-8')
        code, first, _ = self.invoke('submit', extra=options)
        self.assertEqual(code, 0)
        self.assertEqual(first['state'], 'NATIVE_QUEUED')
        registered = first['maintenance_context']
        self.assertEqual(registered['state'], 'ENROLLED')
        offer = json.loads(Path(registered['offer']).read_text())
        self.assertEqual(offer['queued_record']['offset'], first['queued_proof']['offset'])
        self.assertIn('MESSAGE_REFERENCE_V2', self.bridge.wire)
        self.assertEqual(Path(first['body_reference']['path']).read_text(), self.text)
        exposed = []
        attempt = json.loads(Path(first['attempt']).read_text())
        with patch.object(maintenance.identity, 'process', return_value=attempt['native_binding']['process']):
            maintenance.discover(dict(session_id=self.session, transcript_path=str(self.transcript),
                                     hook_event_name='PostToolUse'), emit=exposed.append, now=100)
        self.assertEqual(len(exposed), 1)
        self.assertIn(self.text, exposed[0])
        cursor = maintenance.root() / (self.session + '.cursor.json')
        self.maintenance_reply.write_text(json.dumps(dict(offer_id=offer['offer_id'],
            session_id=self.session, marker=self.marker, body_sha256=delivery._hash(self.text.encode()), body_read=True)))
        before = {p: p.read_bytes() for p in [Path(first['attempt']), self.maintenance_reply,
                                             Path(registered['offer']), cursor,
                                             *self.journal().glob('event-*.json')]}
        code, again, _ = self.invoke('reconcile', output=self.root / 'again.json', extra=options)
        self.assertEqual(code, 0)
        self.assertEqual(again['maintenance_context']['offer_id'], offer['offer_id'])
        self.assertEqual(again['input_operations'], 0)
        self.assertEqual({p: p.read_bytes() for p in before}, before)
        self.assertEqual(self.bridge.operations, ['paste'])

    def complete_offer(self, result):
        offer = json.loads(Path(result['maintenance_context']['offer']).read_text())
        attempt = json.loads(Path(result['attempt']).read_text())
        Path(offer['reply_path']).write_text(json.dumps(dict(offer_id=offer['offer_id'],
            session_id=self.session, marker=attempt['marker'],
            body_sha256=attempt['body_sha256'], body_read=True)))
        return offer

    def next_notice(self, options):
        self.marker = 'NEXT_MAINTENANCE_NOTICE'
        self.bridge.wire = None
        self.text = 'STATUS: NEXT_MAINTENANCE_NOTICE independent authorized notice'
        self.text_path = self.root / 'next-message.txt'
        self.text_path.write_text(self.text)
        return options[:-1] + [str(self.root / 'next-reply.json')]

    def test_existing_unrelated_reply_and_missing_replacement_refuse_before_input(self):
        options = self.maintenance_fixture()
        self.maintenance_reply.write_text('{"body_read":true}')
        code, result, _ = self.invoke('submit', extra=options)
        self.assertEqual(code, 2)
        self.assertEqual(result['details']['input_operations'], 0)
        self.assertEqual(result['details']['stage'], 'MAINTENANCE_ENROLLMENT_PRECHECK')
        self.maintenance_reply.unlink()
        code, result, _ = self.invoke('submit', output=self.root / 'wrong-replacement.json',
            extra=options + ['--replace-completed-offer', '0' * 64])
        self.assertEqual(code, 2)
        self.assertEqual(result['details']['input_operations'], 0)
        self.assertEqual(self.bridge.operations, [])
        self.assertFalse(self.journal().exists())

    def test_new_marker_cannot_reuse_previous_exact_reply(self):
        options = self.maintenance_fixture()
        _, first, _ = self.invoke('submit', extra=options)
        offer = self.complete_offer(first)
        reply_before = self.maintenance_reply.read_bytes()
        self.next_notice(options)
        code, result, _ = self.invoke('submit', output=self.root / 'new-marker.json', extra=options)
        self.assertEqual(code, 2)
        self.assertEqual(result['details']['input_operations'], 0)
        self.assertEqual(self.bridge.operations, ['paste'])
        self.assertEqual(self.maintenance_reply.read_bytes(), reply_before)
        self.assertEqual(json.loads(Path(first['maintenance_context']['offer']).read_text()), offer)

    def test_replacement_and_archived_or_current_id_reconcile_preserve_evidence(self):
        options = self.maintenance_fixture()
        _, first, _ = self.invoke('submit', extra=options)
        old = self.complete_offer(first)
        old_attempt = Path(first['attempt']).read_bytes()
        old_reply = self.maintenance_reply.read_bytes()
        options = self.next_notice(options)
        code, second, _ = self.invoke('submit', output=self.root / 'replacement.json',
            extra=options + ['--replace-completed-offer', old['offer_id']])
        self.assertEqual(code, 0)
        self.assertEqual(second['maintenance_context']['state'], 'ENROLLED')
        current = self.complete_offer(second)
        archive = maintenance.root() / 'history' / self.session / old['offer_id'] / current['offer_id']
        self.assertEqual((archive / 'reply.json').read_bytes(), old_reply)
        self.assertEqual(json.loads((archive / 'offer.json').read_text()), old)
        self.assertEqual(json.loads((archive / 'next-offer.json').read_text()), current)
        paths = [Path(first['attempt']), Path(second['attempt']), self.maintenance_reply,
                 Path(current['reply_path']), Path(second['maintenance_context']['offer']),
                 maintenance.root() / (self.session + '.cursor.json')]
        before = {p: (p.stat().st_ino, p.read_bytes()) for p in paths}
        for index, expected in enumerate([old['offer_id'], current['offer_id']]):
            code, again, _ = self.invoke('reconcile', output=self.root / ('retry-%d.json' % index),
                extra=options + ['--replace-completed-offer', expected])
            self.assertEqual(code, 0)
            self.assertEqual(again['maintenance_context']['state'], 'ENROLLED')
            self.assertEqual(again['input_operations'], 0)
            self.assertEqual({p: (p.stat().st_ino, p.read_bytes()) for p in paths}, before)
        code, rejected, _ = self.invoke('reconcile', output=self.root / 'unrelated-id.json',
            extra=options + ['--replace-completed-offer', 'f' * 64])
        self.assertEqual(code, 2)
        self.assertEqual(rejected['details']['input_operations'], 0)
        self.assertEqual(Path(first['attempt']).read_bytes(), old_attempt)
        self.assertEqual(self.bridge.operations, ['paste', 'paste'])

    def test_damaged_unrelated_offer_does_not_block_independent_transport(self):
        options = self.maintenance_fixture()
        _, first, _ = self.invoke('submit', extra=options)
        registered = Path(first['maintenance_context']['offer'])
        before = registered.read_bytes()
        self.text_path.write_bytes(b'damaged original request pin')
        options = self.next_notice(options)
        code, second, _ = self.invoke('submit', output=self.root / 'independent.json', extra=options)
        self.assertEqual(code, 0)
        self.assertEqual(second['state'], 'NATIVE_QUEUED')
        self.assertEqual(second['maintenance_context']['state'], 'ENROLLMENT_FAILED')
        self.assertEqual(registered.read_bytes(), before)
        self.assertEqual(self.bridge.operations, ['paste', 'paste'])

    def test_missing_queue_proof_waits_without_enrollment_or_more_input(self):
        options = self.maintenance_fixture()
        self.bridge.response = 'none'
        _, first, _ = self.invoke('submit', extra=options)
        self.assertEqual(first['maintenance_context']['state'], 'WAITING_ORIGINAL_QUEUE_PROOF')
        self.assertFalse(maintenance.root().exists())
        self.append_record(self.bridge.wire, queued=True)
        _, second, _ = self.invoke('reconcile', output=self.root / 'queued-later.json', extra=options)
        self.assertEqual(second['maintenance_context']['state'], 'ENROLLED')
        self.assertEqual(self.bridge.operations, ['paste'])

    def test_enrollment_error_retains_transport_result_and_never_replaces_unread_offer(self):
        options = self.maintenance_fixture()
        _, first, _ = self.invoke('submit', extra=options)
        before = Path(first['maintenance_context']['offer']).read_bytes()
        self.marker = 'SECOND_MAINTENANCE_NOTICE'
        self.bridge.wire = None
        self.text_path.write_text('STATUS: SECOND_MAINTENANCE_NOTICE separate maintenance body')
        options[-1] = str(self.root / 'second-reply.json')
        code, second, _ = self.invoke('submit', output=self.root / 'second.json', extra=options)
        self.assertEqual(code, 0)
        self.assertEqual(second['state'], 'NATIVE_QUEUED')
        self.assertEqual(second['maintenance_context']['state'], 'ENROLLMENT_FAILED')
        self.assertEqual(second['queued_proof']['reception_kind'], 'native_queued_command')
        self.assertEqual(Path(first['maintenance_context']['offer']).read_bytes(), before)
        guidance = second['maintenance_context']['recovery']
        for required in ('original executor', 'read and consumed', 'original reconcile',
                         '--replace-completed-offer', 'exact old offer SHA256', 'do not resend'):
            self.assertIn(required, guidance)
        original_attempt = Path(second['attempt']).read_bytes()
        calls = list(self.bridge.operations)
        _, reconciled, _ = self.invoke('reconcile', output=self.root / 'second-reconcile.json',
                                      extra=options)
        self.assertEqual(reconciled['maintenance_context']['state'], 'ENROLLMENT_FAILED')
        self.assertEqual(reconciled['input_operations'], 0)
        self.assertEqual(self.bridge.operations, calls)
        self.assertEqual(Path(second['attempt']).read_bytes(), original_attempt)
        self.assertEqual(Path(first['maintenance_context']['offer']).read_bytes(), before)

    def test_invalid_enrollment_options_refuse_before_transport(self):
        variants = [(['--maintenance-reply', str(self.root / 'reply')], 'submit'),
                    (['--enroll-maintenance-context'], 'submit'),
                    (['--enroll-maintenance-context', '--maintenance-reply', 'relative'], 'submit'),
                    (['--enroll-maintenance-context', '--maintenance-reply', str(self.root / 'reply'),
                      '--replace-completed-offer', 'unknown'], 'submit'),
                    (['--enroll-maintenance-context', '--maintenance-reply', str(self.root / 'reply')], 'validate')]
        for index, (options, command) in enumerate(variants):
            with self.subTest(options=options):
                code, result, _ = self.invoke(command, output=self.root / ('invalid-%d.json' % index), extra=options)
                self.assertEqual(code, 2)
                self.assertEqual(result['state'], 'ERROR')
                self.assertEqual(self.bridge.operations, [])

    def test_validate_file_exactly_matches_stdout_including_unicode_and_newline(self):
        code, result, raw = self.invoke()
        self.assertEqual(code, 0)
        self.assertEqual(self.output.read_bytes(), raw)
        self.assertTrue(raw.endswith(b"\n"))
        self.assertNotIn("command_started", result)
        self.assertFalse(self.reservation().exists())
        self.assertFalse(list(self.root.glob(".successor-result-*.tmp")))

    def test_real_submit_and_reconcile_write_same_result_as_stdout(self):
        code, first, raw = self.invoke("submit")
        self.assertEqual(code, 0)
        self.assertTrue(first["confirmed"])
        self.assertEqual(self.output.read_bytes(), raw)
        before = (self.journal() / "attempt.json").read_bytes()
        code, second, raw = self.invoke("reconcile", output=self.root / "reconcile.json")
        self.assertEqual(code, 0)
        self.assertTrue(second["confirmed"])
        self.assertEqual(second["input_operations"], 0)
        self.assertEqual((self.root / "reconcile.json").read_bytes(), raw)
        self.assertEqual((self.journal() / "attempt.json").read_bytes(), before)
        self.assertEqual(self.bridge.operations, ["paste"])

    def test_pending_then_late_native_receipt_uses_original_attempt(self):
        self.bridge.response = "queued"
        code, pending, raw = self.invoke("submit")
        self.assertEqual(code, 0)
        self.assertFalse(pending["confirmed"])
        self.assertEqual(self.output.read_bytes(), raw)
        self.append_record(self.text)
        code, received, raw = self.invoke("reconcile", output=self.root / "late.json")
        self.assertEqual(code, 0)
        self.assertTrue(received["confirmed"])
        self.assertEqual(received["input_operations"], 0)
        self.assertEqual((self.root / "late.json").read_bytes(), raw)
        self.assertEqual(self.bridge.operations, ["paste"])

    def test_command_error_is_persisted_and_stdout_is_identical(self):
        self.artifact_path.write_bytes(b"{")
        code, result, raw = self.invoke("submit")
        self.assertEqual(code, 2)
        self.assertEqual(result["state"], "ERROR")
        self.assertEqual(self.output.read_bytes(), raw)
        self.assertEqual(self.bridge.operations, [])
        self.assertFalse(self.reservation().exists())

    def test_existing_files_and_special_nodes_refuse_before_transport(self):
        target = self.root / "owned-by-other"
        target.write_bytes(b"keep")
        for kind in ("file", "directory", "symlink", "dangling-symlink", "fifo"):
            with self.subTest(kind=kind):
                path = self.root / ("output-" + kind)
                if kind == "file":
                    path.write_bytes(b"original")
                elif kind == "directory":
                    path.mkdir()
                elif kind == "symlink":
                    path.symlink_to(target)
                elif kind == "dangling-symlink":
                    path.symlink_to(self.root / "absent")
                else:
                    os.mkfifo(path)
                before = path.lstat()
                code, result, _ = self.invoke("submit", output=path)
                self.assertEqual(code, 2)
                self.assertFalse(result["command_started"])
                self.assertEqual(result["input_operations"], 0)
                self.assertEqual(path.lstat().st_ino, before.st_ino)
        self.assertEqual(target.read_bytes(), b"keep")
        self.assertEqual(self.bridge.operations, [])

    def test_missing_symlink_and_relative_parents_refuse_before_transport(self):
        alias = self.root / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        for path in (self.root / "missing" / "result", alias / "result", Path("relative-result")):
            with self.subTest(path=path):
                code, result, _ = self.invoke("submit", output=path)
                self.assertEqual(code, 2)
                self.assertFalse(result["command_started"])
                self.assertEqual(result["input_operations"], 0)
        self.assertEqual(self.bridge.operations, [])

    @unittest.skipIf(os.geteuid() == 0, "root bypasses directory write permission")
    def test_unwritable_parent_refuses_before_transport(self):
        directory = self.root / "readonly"
        directory.mkdir(mode=0o500)
        self.addCleanup(directory.chmod, 0o700)
        code, result, _ = self.invoke("submit", output=directory / "result")
        self.assertEqual(code, 2)
        self.assertFalse(result["command_started"])
        self.assertEqual(self.bridge.operations, [])

    def test_concurrent_reservation_refuses_without_deleting_first(self):
        with cli.OutputReservation(self.output):
            before = self.reservation().read_bytes()
            inode = self.reservation().stat().st_ino
            code, result, _ = self.invoke("submit")
            self.assertEqual(code, 2)
            self.assertFalse(result["command_started"])
            self.assertEqual(self.reservation().read_bytes(), before)
            self.assertEqual(self.reservation().stat().st_ino, inode)
        self.assertEqual(self.bridge.operations, [])

    def test_target_appearing_after_send_preserves_native_result_and_never_replays(self):
        original = self.bridge._run_successor
        def target_appears(*args, **kwargs):
            result = original(*args, **kwargs)
            if args[0] == "paste":
                self.output.write_bytes(b"concurrent owner")
            return result
        with patch.object(self.bridge, "_run_successor", side_effect=target_appears):
            code, result, _ = self.invoke("submit")
        self.assertEqual(code, 2)
        self.assertTrue(result["command_started"])
        self.assertTrue(result["command_result"]["confirmed"])
        self.assertIn("original journal only", result["recovery"])
        self.assertEqual(self.output.read_bytes(), b"concurrent owner")
        self.assertTrue(self.reservation().exists())
        code, received, _ = self.invoke("reconcile", output=self.root / "observed.json")
        self.assertEqual(code, 0)
        self.assertTrue(received["confirmed"])
        self.assertEqual(received["input_operations"], 0)
        self.assertEqual(self.bridge.operations, ["paste"])

    def test_temporary_name_collision_preserves_other_file_and_reservation(self):
        path = self.root / ".successor-result-fixed.tmp"
        path.write_bytes(b"other")
        with patch.object(cli.secrets, "token_hex", return_value="fixed"):
            code, result, _ = self.invoke()
        self.assertEqual(code, 2)
        self.assertTrue(result["command_started"])
        self.assertIn("command_result", result)
        self.assertEqual(path.read_bytes(), b"other")
        self.assertTrue(self.reservation().exists())

    def test_replaced_temporary_is_not_published_or_deleted(self):
        check = cli.OutputReservation.check
        replacement = self.root / "replacement"
        replacement.write_bytes(b"replacement")
        def replace_temporary(output):
            if output.temporary:
                os.replace(replacement, self.root / output.temporary)
            return check(output)
        with patch.object(cli.OutputReservation, "check", replace_temporary):
            code, result, _ = self.invoke()
        self.assertEqual(code, 2)
        self.assertIn("OUTPUT_TEMPORARY_CHANGED", result["error"])
        self.assertFalse(self.output.exists())
        remaining = list(self.root.glob(".successor-result-*.tmp"))
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0].read_bytes(), b"replacement")

    def test_changed_reservation_contents_refuse_publication(self):
        execute = cli.execute
        def change_reservation(args):
            result = execute(args)
            self.reservation().write_bytes(b"other transaction")
            return result
        with patch.object(cli, "execute", side_effect=change_reservation):
            code, result, _ = self.invoke()
        self.assertEqual(code, 2)
        self.assertIn("OUTPUT_RESERVATION_CHANGED", result["error"])
        self.assertEqual(self.reservation().read_bytes(), b"other transaction")
        self.assertFalse(self.output.exists())

    def test_replaced_parent_refuses_publication_and_preserves_new_parent(self):
        directory = self.root / "parent"
        directory.mkdir()
        path = directory / "result.json"
        execute = cli.execute
        def replace_parent(args):
            result = execute(args)
            directory.rename(self.root / "old-parent")
            directory.mkdir()
            (directory / "keep").write_bytes(b"new parent")
            return result
        with patch.object(cli, "execute", side_effect=replace_parent):
            code, result, _ = self.invoke(output=path)
        self.assertEqual(code, 2)
        self.assertIn("OUTPUT_PARENT_CHANGED", result["error"])
        self.assertEqual((directory / "keep").read_bytes(), b"new parent")
        self.assertFalse(path.exists())

    def test_cleanup_failure_does_not_mask_success_or_duplicate_stdout(self):
        with patch.object(cli.os, "unlink", side_effect=PermissionError("cleanup denied")):
            code, result, raw = self.invoke()
        self.assertEqual(code, 0)
        self.assertEqual(self.output.read_bytes(), raw)
        self.assertNotEqual(result.get("state"), "ERROR")
        self.assertTrue(self.reservation().exists())

    def test_cleanup_never_unlinks_replaced_reservation(self):
        with cli.OutputReservation(self.output) as reservation:
            replacement = self.root / "other-reservation"
            replacement.write_bytes(b"keep replacement")
            os.replace(replacement, self.reservation())
        self.assertEqual(self.reservation().read_bytes(), b"keep replacement")
        self.assertIsNone(reservation.directory)

    def test_append_remains_create_only_and_matches_stdout(self):
        code, result, raw = self.invoke("append")
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "PENDING")
        self.assertEqual(self.output.read_bytes(), raw)
        before = self.output.read_bytes()
        code, _, _ = self.invoke("append")
        self.assertEqual(code, 2)
        self.assertEqual(self.output.read_bytes(), before)
        self.assertFalse(self.reservation().exists())


class SuccessorOutputCrashTests(unittest.TestCase):
    def test_process_crash_retains_reservation_and_blocks_second_command(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            artifact = root / "artifact.json"
            artifact.write_text(json.dumps(maintenance_artifact(root)))
            output = root / "result.json"
            scripts = str(Path(cli.__file__).resolve().parent)
            args = ["validate", "--artifact", str(artifact), "--output", str(output)]
            program = ("import os,sys;sys.path.insert(0,sys.argv[1]);"
                       "import successor_rebind_cli as c;"
                       "c.execute=lambda args:os._exit(91);c.main(sys.argv[2:])")
            result = subprocess.run([sys.executable, "-B", "-c", program, scripts] + args,
                                    capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 91, result.stderr)
            self.assertFalse(output.exists())
            reservation = root / ".result.json.successor-output-reservation"
            before = reservation.read_bytes()
            result = subprocess.run([sys.executable, "-B", str(Path(cli.__file__))] + args,
                                    capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertFalse(json.loads(result.stdout)["command_started"])
            self.assertEqual(reservation.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
