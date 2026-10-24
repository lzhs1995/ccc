"""Real-file native/journal behavior with only terminal and process I/O replaced."""
import contextlib
import io
import json
import tempfile
import time
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import successor_native_delivery as delivery
import successor_rebind as rebind
import successor_rebind_cli as cli
import test_successor_rebind as fixtures
from test_successor_rebind import maintenance_artifact, SUCCESSOR, TARGET, NEW_WS, OLD_WS


class FakeTerminal:
    def __init__(self, owner):
        self.owner = owner
        self.wire = None
        self.operations = []
        self.response = "user"
        self.before_read = None
        self.fail_paste = False
        self.occupied = False
        self.after_paste = None

    def pin_successor_pair(self, artifact, **kwargs):
        pair = rebind.resolve_pair(self.owner.identity, self.owner.tree, artifact)
        return dict(mode=pair["mode"],
                    caller_surface_uuid=pair["caller"]["surface_uuid"],
                    caller_workspace_uuid=pair["caller"]["workspace_uuid"],
                    caller_pane_uuid=pair["caller"]["pane_uuid"],
                    target_surface_uuid=pair["target"]["surface_uuid"],
                    target_workspace_uuid=pair["target"]["workspace_uuid"],
                    target_pane_uuid=pair["target"]["pane_uuid"])

    def _run(self, *args):
        return json.dumps(self.owner.tree)

    def read_screen_successor(self, artifact):
        if self.before_read is not None:
            action, self.before_read = self.before_read, None
            action()
        return "foreign draft" if self.occupied else "idle" if self.wire is None else "idle"

    def require_agent_input(self, *args):
        pass

    def compose_block_is_empty(self, screen):
        return screen == "idle"

    def receiver_cannot_submit_now(self, screen):
        return False

    def pending_queue_holds(self, screen, text):
        return False

    def _exact_pending_text(self, screen, text):
        return screen == "draft:" + text

    def _draft_structure(self, screen, text):
        return screen

    def _codex_tab_queue_allowed(self, screen, text):
        return False

    def _queued_or_active_input(self, screen):
        return False

    def _run_successor(self, *args, artifact, receipt_contract=None, json_output=False):
        if args[:2] == ('rpc', 'surface.input_state'):
            target = json.loads(args[2])
            return json.dumps(dict(target, state='draft' if self.occupied else 'empty',
                agent=True, terminal=True, blocks_typing=self.occupied))
        # Observe actual durable bytes at the terminal side-effect boundary.
        journal = self.owner.journal()
        attempt = json.loads((journal / "attempt.json").read_text())
        events = [json.loads(path.read_text()) for path in sorted(journal.glob("event-*.json"))]
        assert attempt["native_binding"]["transcript"]["inode"] == self.owner.transcript.stat().st_ino
        paste = [event for event in events if event["phase"] == "PASTE_INTENT"]
        assert len(paste) == 1 and "native_paste_fence" in paste[0]
        self.operations.append(args[0])
        if args[0] in ("rpc", "paste"):
            if args[0] == "paste":
                assert json_output and '--submit' in args and '--force' not in args[:-1]
                assert len([event for event in events if event['phase'] == 'ENTER_INTENT']) == 1
                if self.occupied:
                    raise RuntimeError('refused existing receiver draft')
            else:
                assert not any(event["phase"] == "ENTER_INTENT" for event in events)
            if self.fail_paste:
                raise RuntimeError("ambiguous paste failure")
            self.wire = args[-1] if args[0] == 'paste' else json.loads(args[2])["text"]
            if self.after_paste:
                self.after_paste()
        if args[0] != 'rpc':
            assert len([event for event in events if event["phase"] == "ENTER_INTENT"]) == 1
            if self.response == "user":
                self.owner.append_record(self.wire)
            elif self.response == "queued":
                self.owner.append_record(self.wire, queued=True)
            elif self.response == "partial":
                self.owner.append_record(self.wire, newline=False)
            elif self.response == "wrong":
                self.owner.append_record("unrelated user text without the marker")
            elif self.response == "cross_fence":
                with self.owner.transcript.open("ab") as stream:
                    stream.write(self.owner.cross_fence_tail)
        return json.dumps({'submitted': True}) if args[0] == 'paste' else "ok"


class SuccessorNativeDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.artifact = maintenance_artifact(self.root)
        self.tree = fixtures.SuccessorRebindTests.tree(self)
        self.identity = {"caller": {"surface_ref": "surface:46", "workspace_ref": "workspace:15", "pane_ref": "pane:33"}}
        self.session = str(uuid.uuid4())
        self.transcript = self.root / (self.session + ".jsonl")
        self.transcript.write_text(json.dumps({"sessionId": self.session, "type": "system"}) + "\n")
        self.process = dict(pid=50101, birth=[1, 0], executable="/local/claude", file_identity=[1, 2],
                            argv=["claude"], env={"CMUX_SURFACE_ID": TARGET, "CMUX_WORKSPACE_ID": OLD_WS})
        self.bridge = FakeTerminal(self)
        self.marker = "SUCCESSOR_TEST_ONE"
        self.text = "STATUS: " + self.marker + " acknowledge this maintenance communication."
        for context in (patch.object(delivery.native, "_target_process", return_value=self.process),
                        patch.object(delivery.native.identity, "process", return_value=self.process),
                        patch.object(delivery.native, "_live_session", return_value=self.session),
                        patch.object(delivery.native, "_transcript_path", return_value=self.transcript),
                        patch.object(delivery.native, "_native_root", return_value=self.root),
                        patch.object(delivery.native.subprocess, "run", return_value=Mock(stdout="ttys052")),
                        patch.object(delivery.time, "sleep")):
            context.start()
            self.addCleanup(context.stop)

    def record_bytes(self, text, *, queued=False):
        record = dict(sessionId=self.session, timestamp=datetime.now(timezone.utc).isoformat())
        if queued:
            record.update(type="attachment", attachment={"type": "queued_command", "prompt": text})
        else:
            record.update(type="user", message={"role": "user", "content": text})
        return json.dumps(record).encode() + b"\n"

    def append_record(self, text, *, queued=False, newline=True):
        raw = self.record_bytes(text, queued=queued)
        with self.transcript.open("ab") as stream:
            stream.write(raw if newline else raw[:-1])

    def journal(self):
        return delivery._paths(self.artifact, self.marker, self.root)[1]

    def submit(self):
        return delivery.submit(self.bridge, self.artifact, self.text,
                               marker=self.marker, state_root=self.root, wait_seconds=0)

    def reconcile(self):
        return delivery.reconcile(self.bridge, self.artifact, self.text,
                                  marker=self.marker, state_root=self.root)

    def test_actual_journal_precedes_paste_and_key_and_repeated_submit_has_zero_input(self):
        first = self.submit()
        self.assertTrue(first["confirmed"])
        self.assertEqual(self.bridge.operations, ["paste"])
        original_bytes = (self.journal() / "attempt.json").read_bytes()
        again = self.submit()
        self.assertTrue(again["confirmed"])
        self.assertEqual(again["input_operations"], 0)
        self.assertEqual(self.bridge.operations, ["paste"])
        self.assertEqual((self.journal() / "attempt.json").read_bytes(), original_bytes)

    def test_deferred_direct_recovery_after_compaction(self):
        self.bridge.response = 'none'
        with patch.object(self.bridge, 'read_screen_successor', return_value='Compacting context'):
            self.assertFalse(self.submit()['confirmed'])
            self.assertFalse(delivery.recover(self.bridge, self.artifact, self.text,
                marker=self.marker, state_root=self.root, wait_seconds=0)['confirmed'])
        self.assertEqual(self.bridge.operations, ['paste'])
        original = (self.journal() / 'attempt.json').read_bytes()
        self.bridge.response = 'user'
        with patch.object(self.bridge, 'read_screen_successor', return_value='> ' + self.text):
            result = delivery.recover(self.bridge, self.artifact, self.text,
                marker=self.marker, state_root=self.root, wait_seconds=0)
            self.assertTrue(result['confirmed'])
            with self.assertRaisesRegex(ValueError, 'BUDGET_UNAVAILABLE'):
                delivery.recover(self.bridge, self.artifact, self.text,
                    marker=self.marker, state_root=self.root, wait_seconds=0)
        self.assertEqual(self.bridge.operations, ['paste', 'send-key'])
        self.assertEqual((self.journal() / 'attempt.json').read_bytes(), original)

    def test_record_between_bind_and_paste_is_not_a_receipt(self):
        original_fence = delivery.native.capture_paste_fence
        def fence(binding):
            self.append_record(self.text)
            return original_fence(binding)
        self.addCleanup(patch.stopall)
        patch.object(delivery.native, "capture_paste_fence", side_effect=fence).start()
        self.bridge.response = "none"
        self.assertFalse(self.submit()["confirmed"])
        self.append_record(self.text)
        self.assertTrue(self.reconcile()["confirmed"])

    def test_fence_crossing_record_is_not_a_receipt(self):
        def partial_before_fence():
            raw = self.record_bytes(self.text)
            middle = len(raw) // 2
            with self.transcript.open("ab") as stream:
                stream.write(raw[:middle])
            self.cross_fence_tail = raw[middle:]
        original_fence = delivery.native.capture_paste_fence
        def fence(binding):
            partial_before_fence()
            return original_fence(binding)
        self.addCleanup(patch.stopall)
        patch.object(delivery.native, "capture_paste_fence", side_effect=fence).start()
        self.bridge.response = "cross_fence"
        self.assertFalse(self.submit()["confirmed"])
        self.append_record(self.text)
        self.assertTrue(self.reconcile()["confirmed"])

    def test_incomplete_native_record_waits_for_complete_line(self):
        self.bridge.response = "partial"
        result = self.submit()
        self.assertFalse(result["confirmed"])
        self.assertEqual(result["state"], "NATIVE_RECORD_INCOMPLETE")
        with self.transcript.open("ab") as stream:
            stream.write(b"\n")
        self.assertTrue(self.reconcile()["confirmed"])

    def test_queued_record_does_not_confirm_native_reception(self):
        self.bridge.response = "queued"
        result = self.submit()
        self.assertFalse(result["confirmed"])
        self.assertEqual(result["state"], "NATIVE_QUEUED")

    def test_unrelated_user_text_does_not_confirm_marker(self):
        self.bridge.response = "wrong"
        self.assertFalse(self.submit()["confirmed"])

    def test_long_body_is_immutable_reference_and_receipt_covers_notice_only(self):
        self.text += "\n" + "确切正文\t" * 200
        result = self.submit()
        self.assertTrue(result["confirmed"])
        self.assertEqual(result["confirmation_scope"], "reference_notice")
        self.assertFalse(result["body_read_confirmed"])
        self.assertNotIn("\n", self.bridge.wire)
        self.assertLessEqual(len(self.bridge.wire.encode()), 700)
        body = Path(result["body_reference"]["path"])
        self.assertEqual(body.read_bytes(), self.text.encode())
        self.assertEqual(body.stat().st_mode & 0o222, 0)

    def test_ambiguous_paste_never_replays_or_submits_a_key(self):
        self.bridge.fail_paste = True
        with self.assertRaisesRegex(RuntimeError, "ambiguous paste failure"):
            self.submit()
        result = self.submit()
        self.assertFalse(result["confirmed"])
        self.assertEqual(result["input_operations"], 0)
        self.assertEqual(self.bridge.operations, ["paste"])

    def assert_primary_failure_preserved(self, *, fail_annotation, fail_event):
        self.bridge.fail_paste = True
        annotate, event = delivery.annotate_attempt_error, delivery._event

        def annotate_or_fail(*args, **kwargs):
            if fail_annotation:
                raise OSError('annotation unavailable')
            return annotate(*args, **kwargs)

        def event_or_fail(journal, phase, **kwargs):
            if phase == 'ERROR' and fail_event:
                raise OSError('error journal unavailable')
            return event(journal, phase, **kwargs)

        with patch.object(delivery, 'annotate_attempt_error', side_effect=annotate_or_fail), \
                patch.object(delivery, '_event', side_effect=event_or_fail):
            with self.assertRaisesRegex(RuntimeError, 'ambiguous paste failure') as raised:
                self.submit()
        notes = '\n'.join(raised.exception.__notes__)
        if fail_annotation:
            self.assertIn('annotation unavailable', notes)
        if fail_event:
            self.assertIn('error journal unavailable', notes)
        events = [json.loads(p.read_text()) for p in self.journal().glob('event-*.json')]
        self.assertEqual(sum(e['phase'] == 'PASTE_INTENT' for e in events), 1)
        self.assertFalse(any(e['phase'] == 'KEY_INTENT' for e in events))
        errors = [e for e in events if e['phase'] == 'ERROR']
        self.assertEqual(len(errors), int(not fail_event))
        if errors:
            self.assertEqual(errors[0]['error'], 'ambiguous paste failure')
            self.assertEqual(errors[0]['input_operations_attempted'], 2)
        before = (self.journal() / 'attempt.json').read_bytes()
        for again in (self.reconcile, self.submit):
            result = again()
            self.assertFalse(result['confirmed'])
            self.assertEqual(result['input_operations'], 0)
        self.assertEqual((self.journal() / 'attempt.json').read_bytes(), before)
        self.assertEqual(self.bridge.operations, ['paste'])

    def test_annotation_failure_preserves_primary_and_original_input_budget(self):
        self.assert_primary_failure_preserved(fail_annotation=True, fail_event=False)

    def test_error_event_failure_preserves_primary_and_original_input_budget(self):
        self.assert_primary_failure_preserved(fail_annotation=False, fail_event=True)

    def test_both_diagnostic_failures_preserve_primary_and_original_input_budget(self):
        self.assert_primary_failure_preserved(fail_annotation=True, fail_event=True)

    def test_reconcile_annotation_failure_preserves_original_observation_error(self):
        self.bridge.response = 'none'
        self.submit()
        before = (self.journal() / 'attempt.json').read_bytes()

        class LegacyObservationError(delivery.native.NativeDeliveryError):
            # Python 3.10 has no BaseException.add_note.
            add_note = None

        for error_type in (delivery.native.NativeDeliveryError, LegacyObservationError):
            with self.subTest(error_type=error_type.__name__):
                primary = error_type('original transcript unavailable')
                primary.__notes__ = ['earlier diagnostic']
                with patch.object(delivery, '_observe', side_effect=primary), \
                        patch.object(delivery, 'annotate_attempt_error',
                                     side_effect=OSError('annotation unavailable')):
                    with self.assertRaises(delivery.native.NativeDeliveryError) as raised:
                        self.reconcile()
                self.assertIs(raised.exception, primary)
                self.assertEqual(primary.__notes__[0], 'earlier diagnostic')
                self.assertIn('annotation unavailable', '\n'.join(primary.__notes__))
                self.assertEqual((self.journal() / 'attempt.json').read_bytes(), before)
                self.assertEqual(self.bridge.operations, ['paste'])

    def test_foreign_draft_refusal_never_falls_through_to_a_key(self):
        self.bridge.occupied = True
        with self.assertRaisesRegex(RuntimeError, 'PROMPT_NOT_EMPTY'):
            self.submit()
        self.assertEqual(self.bridge.operations, [])
        self.assertIsNone(self.bridge.wire)

    def test_changed_body_cannot_reuse_original_marker(self):
        self.submit()
        self.text += " changed"
        with self.assertRaisesRegex(delivery.native.NativeDeliveryError, "ORIGINAL_ATTEMPT_CHANGED"):
            self.submit()
        self.assertEqual(self.bridge.operations, ["paste"])

    def test_pane_drift_during_host_request_never_earns_a_receipt_or_second_command(self):
        def drift():
            self.tree["windows"][0]["workspaces"][0]["panes"][0]["id"] = SUCCESSOR
        self.bridge.after_paste = drift
        with self.assertRaisesRegex(rebind.SuccessorRebindError, "pane drift"):
            self.submit()
        self.assertEqual(self.bridge.operations, ["paste"])

    def test_missing_paste_fence_cannot_be_reconstructed(self):
        binding = delivery.bind_target(self.bridge, self.artifact, self.text)
        self.append_record(self.text)
        with self.assertRaisesRegex(delivery.native.NativeDeliveryError, "PASTE_FENCE_REQUIRED"):
            delivery.scan_received(self.bridge, self.artifact, binding, self.text)

    def test_reconcile_resumes_partial_line_without_rescanning_old_records(self):
        self.bridge.response = "none"
        self.submit()
        original = (self.journal() / "attempt.json").read_bytes()
        self.append_record("unrelated complete record")
        self.append_record(self.text, newline=False)
        result = self.reconcile()
        self.assertFalse(result["confirmed"])
        cursor = json.loads((self.journal() / "native-scan.json").read_text())["next_offset"]
        self.assertGreater(cursor, json.loads(original)["native_binding"]["transcript"]["offset"])
        with self.transcript.open("ab") as stream:
            stream.write(b"\n")
        result = self.reconcile()
        self.assertTrue(result["confirmed"])
        self.assertEqual(result["input_operations"], 0)
        self.assertEqual((self.journal() / "attempt.json").read_bytes(), original)
        self.assertEqual(self.bridge.operations, ["paste"])

    def test_cached_proof_is_revalidated_against_exact_record_bytes(self):
        self.submit()
        proof = json.loads((self.journal() / "native-scan.json").read_text())["native_proof"]
        with self.transcript.open("r+b") as stream:
            stream.seek(proof["offset"])
            stream.write(b"!")
        with self.assertRaises(delivery.native.NativeDeliveryError):
            self.reconcile()
        self.assertEqual(self.bridge.operations, ["paste"])

    def test_transcript_rotation_and_truncation_cannot_reuse_cached_proof(self):
        self.submit()
        raw = self.transcript.read_bytes()
        self.transcript.write_bytes(b"")
        with self.assertRaisesRegex(delivery.native.NativeDeliveryError, "TRUNCATED"):
            self.reconcile()
        self.transcript.write_bytes(raw)
        replacement = self.transcript.with_suffix(".replacement")
        replacement.write_bytes(raw)
        replacement.replace(self.transcript)
        with self.assertRaisesRegex(delivery.native.NativeDeliveryError, "REPLACED"):
            self.reconcile()
        self.assertEqual(self.bridge.operations, ["paste"])

    def test_changed_cursor_binding_refuses_observation_without_new_input(self):
        self.bridge.response = "none"
        self.submit()
        path = self.journal() / "native-scan.json"
        cursor = json.loads(path.read_text())
        cursor["intent_sha256"] = "0" * 64
        path.write_text(json.dumps(cursor))
        with self.assertRaisesRegex(delivery.native.NativeDeliveryError, "SCAN_BINDING_CHANGED"):
            self.reconcile()
        self.assertEqual(self.bridge.operations, ["paste"])

    def test_invalid_window_never_creates_successor_attempt(self):
        for value in (-1, 61, True, float("nan")):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "CONFIRMATION_WINDOW_INVALID"):
                delivery.submit(self.bridge, self.artifact, self.text, marker=self.marker,
                                state_root=self.root, wait_seconds=value)
        self.assertFalse(self.journal().exists())
        self.assertEqual(self.bridge.operations, [])


class SuccessorCliTests(unittest.TestCase):
    def test_validate_and_error_both_emit_json(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            path = root / "artifact.json"
            artifact = maintenance_artifact(root)
            path.write_text(json.dumps(artifact))
            output = io.StringIO()
            with patch("sys.argv", ["cli", "validate", "--artifact", str(path)]), contextlib.redirect_stdout(output):
                self.assertEqual(cli.main(), 0)
            self.assertEqual(json.loads(output.getvalue())["schema"], rebind.SCHEMA_V2)
            output = io.StringIO()
            with patch("sys.argv", ["cli", "submit", "--artifact", str(path)]), contextlib.redirect_stdout(output):
                self.assertEqual(cli.main(), 2)
            self.assertEqual(json.loads(output.getvalue())["state"], "ERROR")


if __name__ == "__main__":
    unittest.main()
