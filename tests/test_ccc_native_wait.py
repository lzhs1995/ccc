"""Native Stop wait regression: real shared validator, temporary files, zero input.

Review mode: solo_self_review. These tests neither invoke hooks nor modify an
installed release, daemon state, or a live terminal.
"""
from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
import unittest
from unittest import mock
import uuid

from tests import test_ccc_supervisor_wait as fixtures

adapter = fixtures.adapter
recovery = fixtures.recovery
watch = fixtures.watch
iso = fixtures.iso
write = fixtures.write


class NativeFixture(fixtures.Fixture):
    def setUp(self):
        super().setUp()
        self.consensus_hook = self.hook.with_name("cmux_consensus_stop_guard.py")
        shutil.copyfile(fixtures.SHARED_BASE / self.consensus_hook.name, self.consensus_hook)
        self.process_started_at = self.now - 300
        self.stop = self.event(transcript_id=recovery.digest(str(self.native)))
        # Native Claude uses sessionId, never a manufactured session_id.
        self.summary = dict(
            type="system", subtype="stop_hook_summary", sessionId=self.identity["session_id"],
            isSidechain=False, uuid=str(uuid.uuid4()), toolUseID=str(uuid.uuid4()),
            timestamp=iso(self.now - 19), cwd=str(self.task_root), hookCount=1,
            hookInfos=[dict(command=shlex.join([sys.executable, "-B", str(self.consensus_hook)]),
                            durationMs=8)],
            hookErrors=[], hookAdditionalContext=[], preventedContinuation=True,
            stopReason="WAITING_SUPERVISOR: report frozen; original callback returned unconfirmed",
            hasOutput=True, level="suggestion")

    def append(self, row, *, path=None):
        with (path or self.native).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")

    def event(self, name="Stop", offset=-20, **changes):
        changes.setdefault("transcript_id", recovery.digest(str(self.native)))
        return super().event(name, offset, **changes)

    def large_native_file(self):
        # A sparse prefix tests the real 128 MiB failure boundary without
        # reading or allocating a full transcript in the test itself.
        payload = json.dumps(self.summary, ensure_ascii=False).encode("utf-8") + b"\n"
        with self.native.open("wb") as stream:
            stream.seek(recovery.TRANSCRIPT_LIMIT + 4096)
            stream.write(b"\n" + payload)
        return payload

    def replace_summary(self, **changes):
        row = copy.deepcopy(self.summary)
        row.update(changes)
        self.native.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
        return row

    def observe(self, *, event=None, **kwargs):
        values = dict(event=self.stop if event is None else event,
                      process_started_at=self.process_started_at, now=self.now)
        values.update(kwargs)
        return self.evidence.native_wait(self.identity, **values)

    def assert_no_receipt(self):
        self.assertFalse((self.task_root / "receipt.json").exists())


class NativeEvidenceTests(NativeFixture):
    def test_real_camelcase_summary_uses_validator_without_dispatch_or_writes(self):
        self.append(self.summary)
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        result = self.observe()
        self.assertIsNotNone(result)
        self.assertEqual(result["status"], adapter.WAITING)
        self.assertEqual(result["origin"], "native_stop_summary")
        self.assertEqual(result["binding"], self.identity)
        self.assertNotIn("dispatch_event_id", result["binding"])
        self.assertNotIn("completed", result)
        self.assertEqual(result["deadline_at"], self.attempt["ended_at_epoch"] + 300)
        self.assertEqual(result["validator"]["sha256"], fixtures.sha(self.helper))
        self.assertEqual(result["validator"]["evidence_io_sha256"], fixtures.sha(self.evidence_io))
        self.assertEqual(result["native_summary"]["hook"]["sha256"], fixtures.sha(self.consensus_hook))
        self.assertEqual(result["native_summary"]["uuid"], self.summary["uuid"])
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()})
        self.assert_no_receipt()

    def test_same_original_stop_observes_summary_appended_after_hook(self):
        original = copy.deepcopy(self.stop)
        self.assertIsNone(self.observe())
        self.append(self.summary)
        self.assertIsNotNone(self.observe())
        self.assertEqual(self.stop, original)
        self.assert_no_receipt()

    def test_marker_valid_at_stop_is_accepted_after_expiration_without_new_deadline(self):
        self.append(self.summary)
        marker = dict(self.task["marker"], ttl_seconds=110)
        write(self.task["marker_path"], marker)
        result = self.observe()
        self.assertIsNotNone(result)
        self.assertEqual(result["status"], adapter.WAITING)
        timed_out = self.observe(now=self.now + 600)
        self.assertIsNotNone(timed_out)
        self.assertEqual(timed_out["status"], adapter.TIMED_OUT)
        self.assertEqual(timed_out["deadline_at"], self.attempt["ended_at_epoch"] + 300)
        self.assertEqual(timed_out["deadline_at"], result["deadline_at"])
        self.assert_no_receipt()

    def test_marker_expired_at_stop_is_rejected(self):
        self.append(self.summary)
        write(self.task["marker_path"], dict(self.task["marker"], ttl_seconds=100))
        self.assertIsNone(self.observe())

    def test_summary_requires_real_identity_and_wait_result(self):
        for changes in (
            {"sessionId": "another-session"}, {"isSidechain": True},
            {"isSidechain": None}, {"type": "assistant"}, {"subtype": "text"},
            {"preventedContinuation": False}, {"hookErrors": ["failed"]},
            {"stopReason": "report ready"}, {"uuid": ""},
            {"timestamp": iso(self.process_started_at)}, {"timestamp": iso(self.now + 10)},
            {"timestamp": "invalid"}, {"cwd": str(self.root)}, {"hookInfos": []},
            {"hookInfos": self.summary["hookInfos"] * 2},
        ):
            with self.subTest(changes=changes):
                self.replace_summary(**changes)
                self.assertIsNone(self.observe())
        self.replace_summary()
        for key in adapter.IDENTITY_KEYS:
            with self.subTest(identity=key):
                bad = dict(self.stop, **{key: 999 if key == "agent_pid" else "wrong"})
                self.assertIsNone(self.observe(event=bad))
        self.assertIsNone(self.observe(event=dict(self.stop, synthetic_fallback=True)))
        self.assertIsNone(self.observe(event=dict(self.stop, transcript_id="")))

    def test_latest_stop_controls_wait_and_watermark_prevents_resurrection(self):
        self.append(self.summary)
        self.assertIsNotNone(self.observe())
        self.assertIsNone(self.observe(after=self.now - 19))
        self.append(dict(self.summary, uuid=str(uuid.uuid4()), timestamp=iso(self.now - 18),
                         preventedContinuation=False, stopReason=""))
        self.assertIsNone(self.observe())

    def test_report_pack_and_callback_changes_fail_real_validator(self):
        self.append(self.summary)
        self.assertIsNotNone(self.observe())
        changed_pack = dict(self.task["pack"], completion_nonce="different-frozen-nonce")
        changed_attempt = copy.deepcopy(self.attempt)
        changed_attempt["binding"]["report_sha256"] = "0" * 64
        replacements = {
            self.task_root / "task-pack.json": json.dumps(changed_pack).encode("utf-8"),
            self.attempt_path: json.dumps(changed_attempt).encode("utf-8"),
        }
        for path in (Path(self.task["pack"]["report"]), self.task_root / "task-pack.json",
                     self.attempt_path):
            with self.subTest(path=path.name):
                original = path.read_bytes()
                # Keep JSON valid: rejection must exercise the frozen binding,
                # rather than merely fail a parser on malformed fixture data.
                path.write_bytes(replacements.get(path, original + b"modified"))
                self.assertIsNone(self.observe())
                path.write_bytes(original)
                self.assertIsNotNone(self.observe())
        attempt = copy.deepcopy(self.attempt)
        attempt["ended_at_epoch"] = self.now - 18
        write(self.attempt_path, attempt)
        self.assertIsNone(self.observe())

    def test_generic_watchdog_after_stop_keeps_wait_but_later_human_owns_turn(self):
        self.append(self.summary)
        self.prompt(adapter.protocol.DEFAULT_CLAUDE_MESSAGE, offset=-15, prompt_kind="watchdog")
        self.assertIsNotNone(self.observe())
        human = self.prompt("Start the next independent native task", offset=-10)
        self.assertIsNone(self.observe())
        # The triggering human may discover the wait for the audited takeover.
        self.assertIsNotNone(self.observe(event=human))
        self.assertIsNone(self.observe(event=human, after=human["created_at"]))

    def test_partial_final_record_is_rejected_until_complete(self):
        self.append(self.summary)
        with self.native.open("ab") as stream:
            stream.write(b'{"type":"user"')
        self.assertIsNone(self.observe())
        self.replace_summary()
        self.assertIsNotNone(self.observe())

    def test_untrusted_hook_path_and_ambiguous_marker_do_not_prove_wait(self):
        for command in ("python -B " + str(self.consensus_hook),
                        shlex.join([sys.executable, str(self.consensus_hook)]),
                        shlex.join([sys.executable, "-B", str(self.root / self.consensus_hook.name)])):
            with self.subTest(command=command):
                self.replace_summary(hookInfos=[dict(command=command)])
                self.assertIsNone(self.observe())
        self.replace_summary()
        write(self.task["marker_path"].with_name("duplicate.json"), self.task["marker"])
        self.assertIsNone(self.observe())

    def test_no_terminal_callback_or_summary_text_alone_cannot_create_wait(self):
        self.append(self.summary)
        self.attempt_path.unlink()
        self.assertIsNone(self.observe())
        self.assert_no_receipt()


class NativeTailTests(NativeFixture):
    def test_short_file_preserves_first_byte_and_tail_provenance(self):
        self.append(self.summary)
        original = self.native.read_bytes()
        data, proof = adapter.native_tail(self.native)
        self.assertEqual(data, original)
        self.assertEqual(proof["offset_bytes"], 0)
        self.assertEqual(proof["bytes_read"], len(original))
        self.assertEqual(proof["sha256"], hashlib.sha256(original).hexdigest())

    def test_partial_first_record_is_discarded_at_exact_byte_boundary(self):
        raw = b'{"old":"a long old record"}\n{"current":true}\n'
        final = b'{"current":true}\n'
        self.native.write_bytes(raw)
        with mock.patch.object(adapter, "NATIVE_TAIL_LIMIT", len(final) + 5):
            data, proof = adapter.native_tail(self.native)
        self.assertEqual(data, final)
        self.assertEqual(proof["offset_bytes"], len(raw) - len(final))
        with mock.patch.object(adapter, "NATIVE_TAIL_LIMIT", len(final)):
            data, proof = adapter.native_tail(self.native)
        self.assertEqual(data, final)
        self.assertEqual(proof["offset_bytes"], len(raw) - len(final))

    def test_over_128_mib_native_wait_reads_only_bounded_suffixes(self):
        payload = self.large_native_file()
        self.assertGreater(self.native.stat().st_size, recovery.TRANSCRIPT_LIMIT)
        real_fdopen = os.fdopen
        identity = self.native.stat()
        reads = []

        class ReadSpy:
            def __init__(self, stream):
                self.stream = stream

            def __getattr__(self, name):
                return getattr(self.stream, name)

            def __enter__(self):
                self.stream.__enter__()
                return self

            def __exit__(self, *args):
                return self.stream.__exit__(*args)

            def read(self, size=-1):
                data = self.stream.read(size)
                reads.append((size, len(data)))
                return data

        def tracked_fdopen(fd, *args, **kwargs):
            stat = os.fstat(fd)
            stream = real_fdopen(fd, *args, **kwargs)
            return (ReadSpy(stream) if (stat.st_dev, stat.st_ino) ==
                    (identity.st_dev, identity.st_ino) else stream)

        with mock.patch.object(adapter.os, "fdopen", side_effect=tracked_fdopen):
            data, proof = adapter.native_tail(self.native)
            self.assertEqual(data, payload)
            self.assertEqual(sum(n for _, n in reads), adapter.NATIVE_TAIL_LIMIT + 1)
            self.assertEqual(proof["offset_bytes"], identity.st_size - len(payload))
            reads.clear()
            result = self.observe()
            self.assertIsNotNone(result)
            # Observation plus the final unchanged-source check: two bounded reads.
            self.assertLessEqual(sum(n for _, n in reads), 2 * (adapter.NATIVE_TAIL_LIMIT + 1))
            self.assertTrue(reads)
            self.assertTrue(all(0 < requested <= adapter.NATIVE_TAIL_LIMIT for requested, _ in reads))
        self.assert_no_receipt()

    def test_large_native_log_also_supports_verified_human_takeover_source(self):
        self.large_native_file()
        human = self.prompt("Start the next independently authorized task", offset=-10)
        wait = self.observe(event=human)
        self.assertIsNotNone(wait)
        proof = adapter.native_human_prompt(
            human, wait, self.identity, watermark=self.now - 20,
            watchdog_message=adapter.protocol.DEFAULT_CLAUDE_MESSAGE, now=self.now)
        self.assertGreater(proof["offset_bytes"], 0)
        self.assertLessEqual(proof["bytes_read"], adapter.NATIVE_TAIL_LIMIT)
        self.assertEqual(proof["message_hash"], human["message_hash"])
        self.assertTrue(adapter.native_prompt_current(proof, human))

    def test_incomplete_tail_symlink_fifo_and_writable_source_are_rejected(self):
        self.append(self.summary)
        original = self.native.read_bytes()
        self.native.write_bytes(original[:-1])
        with self.assertRaises(ValueError):
            adapter.native_tail(self.native)
        self.native.write_bytes(original)
        alias = self.root / "native-alias.jsonl"
        alias.symlink_to(self.native)
        with self.assertRaises(ValueError):
            adapter.native_tail(alias)
        fifo = self.root / "native-fifo.jsonl"
        os.mkfifo(fifo)
        with self.assertRaises(ValueError):
            adapter.native_tail(fifo)
        self.native.chmod(0o666)
        with self.assertRaises(ValueError):
            adapter.native_tail(self.native)

    def test_exclusive_writer_lock_and_changed_fingerprint_are_rejected(self):
        self.append(self.summary)
        with self.native.open("rb") as writer:
            fcntl.flock(writer.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):
                adapter.native_tail(self.native)
        original = recovery.fingerprint(self.native)
        changed = list(original)
        changed[-1] += 1
        with mock.patch.object(recovery, "fingerprint", side_effect=[original, changed]):
            with self.assertRaises(ValueError):
                adapter.native_tail(self.native)


class NativeJournalTests(NativeFixture):
    def setUp(self):
        super().setUp()
        self.journal = self.root / "hook-events.jsonl"

    def test_exact_accepted_hook_is_returned_without_replaying_it(self):
        self.append(self.stop, path=self.journal)
        before = self.journal.read_bytes()
        event, proof = adapter.native_event_from_journal(self.journal, dict(self.stop))
        self.assertEqual(event, self.stop)
        self.assertEqual(proof["sha256"], hashlib.sha256(before).hexdigest())
        self.assertEqual(self.journal.read_bytes(), before)

    def test_each_accepted_identity_dimension_including_generation_must_match(self):
        for key in (*adapter.IDENTITY_KEYS, "event_name", "created_at"):
            with self.subTest(key=key):
                value = self.now - 21 if key == "created_at" else 999 if key == "agent_pid" else "wrong"
                write(self.journal, dict(self.stop, **{key: value}))
                with self.journal.open("a") as stream:
                    stream.write("\n")
                with self.assertRaises(ValueError):
                    adapter.native_event_from_journal(self.journal, self.stop)

    def test_missing_duplicate_synthetic_or_transcriptless_hook_is_rejected(self):
        for records in ([], [self.stop, self.stop], [dict(self.stop, synthetic_fallback=True)],
                        [dict(self.stop, transcript_id="")], [dict(self.stop, event_id="another")]):
            with self.subTest(records=records):
                self.journal.write_text("")
                for row in records:
                    self.append(row, path=self.journal)
                with self.assertRaises(ValueError):
                    adapter.native_event_from_journal(self.journal, self.stop)


class NativeWatcherTests(NativeFixture):
    def setUp(self):
        super().setUp()
        self.client = fixtures.RefuseInput(self.workspace, self.surface, str(uuid.uuid4()))
        delivery_root = mock.patch.object(watch, "DELIVERY_LOCK_ROOT", self.root / "deliveries")
        delivery_root.start()
        self.addCleanup(delivery_root.stop)
        self.config_path, self.state_path = self.root / "config.json", self.root / "state.json"
        config = watch.default_config()
        config.update(mode="armed", claude_enabled=True, targets=[self.target])
        write(self.config_path, config)
        self.daemon = watch.WatchDaemon(config_path=self.config_path, state_path=self.state_path,
                                       client=self.client)
        self.daemon.claude_closeout = self.evidence
        self.runtime = watch.TargetRuntime()
        self.runtime.claude_session_id = self.identity["session_id"]
        self.runtime.claude_process_pid = self.identity["agent_pid"]
        self.runtime.claude_process_generation = self.identity["process_generation"]
        self.runtime.claude_process_started_at = iso(self.process_started_at)
        self.runtime.claude_hook_process_generation = self.identity["process_generation"]
        self.runtime.claude_hook_health = "healthy"
        self.runtime.claude_turn_started_at = self.now - 100
        self.runtime.claude_last_hook_at = self.now - 90
        self.runtime.claude_task_binding = None
        self.daemon.runtime[self.surface] = self.runtime

    handshake = fixtures.WatcherTests.handshake

    def handle(self, event):
        self.append(event, path=self.daemon.claude_event_inbox.journal_path)
        return fixtures.WatcherTests.handle(self, event)

    def test_null_binding_native_summary_enters_wait_without_input_or_receipt(self):
        self.append(self.summary)
        self.assertEqual(self.handle(self.stop), "suppressed_waiting_supervisor")
        wait = self.runtime.claude_supervisor_wait
        self.assertEqual(wait["origin"], "native_stop_summary")
        self.assertEqual(wait["binding"], self.identity)
        self.assertIsNone(self.runtime.claude_task_binding)
        self.assertFalse(self.runtime.claude_completed_latched)
        self.assertIsNone(self.runtime.claude_completion_event_id)
        self.assertEqual(self.client.inputs, [])
        self.assert_no_receipt()

    def test_native_wait_survives_watchdog_then_audited_human_takeover(self):
        # This is an actual queued CCC echo. The same sentence typed by a
        # human without a matching send record must still take the turn back.
        r = self.runtime
        source = self.event(offset=-24)
        r.claude_submit_event_id = source["event_id"]
        r.claude_last_submit_event_id = source["event_id"]
        r.claude_last_submit_at = self.now - 21
        r.claude_last_submit_message_hash = recovery.digest(adapter.protocol.DEFAULT_CLAUDE_MESSAGE)
        r.claude_last_submit_session_id = self.identity["session_id"]
        r.claude_last_submit_generation = self.identity["process_generation"]
        self.daemon.claude_event_ledger.mark(source, "sending")
        self.daemon._record_claude_submission(r)
        submitted = copy.deepcopy(self.daemon.claude_event_ledger.events[source["event_id"]])
        self.assertIn("watchdog_submission", submitted)
        self.append(self.summary)
        self.assertEqual(self.handle(self.stop), "suppressed_waiting_supervisor")
        wait = copy.deepcopy(self.runtime.claude_supervisor_wait)
        watchdog = self.prompt(adapter.protocol.DEFAULT_CLAUDE_MESSAGE, offset=-15,
                               prompt_kind="watchdog")
        self.assertEqual(self.handle(watchdog), "suppressed_waiting_supervisor")
        self.assertEqual(self.runtime.claude_supervisor_wait, wait)
        self.assertEqual(self.daemon.claude_event_ledger.events[source["event_id"]], submitted)
        human = self.prompt("Implement the next native Goal", offset=-10)
        self.assertEqual(self.handle(human), "human_prompt")
        audit = self.runtime.claude_supervisor_human_takeover
        self.assertEqual(audit["wait"], wait)
        self.assertIsNone(audit["task_binding"])
        self.assertIsNone(self.runtime.claude_supervisor_wait)
        self.assertIsNone(self.runtime.claude_task_binding)
        self.assertIsNone(self.daemon._claude_supervisor_wait_guard(self.target, self.runtime))
        restarted = watch.WatchDaemon(config_path=self.config_path, state_path=self.state_path,
                                      client=self.client)
        restarted.claude_closeout = self.evidence
        recovered = restarted.runtime[self.surface]
        self.assertEqual(recovered.claude_supervisor_human_takeover, audit)
        self.assertIsNone(recovered.claude_supervisor_wait)
        self.assertIsNone(recovered.claude_task_binding)
        self.assertIsNone(restarted._claude_supervisor_wait_guard(self.target, recovered))
        self.assertEqual(self.client.inputs, [])
        self.assert_no_receipt()

    def test_human_typing_watchdog_sentence_without_send_record_can_take_over(self):
        self.append(self.summary)
        self.assertEqual(self.handle(self.stop), "suppressed_waiting_supervisor")
        human = self.prompt(adapter.protocol.DEFAULT_CLAUDE_MESSAGE, prompt_kind="watchdog")
        self.assertEqual(self.handle(human), "human_prompt")
        self.assertIsNotNone(self.runtime.claude_supervisor_human_takeover)
        self.assertIsNone(self.runtime.claude_supervisor_wait)
        self.assertEqual(self.client.inputs, [])

    def test_raw_native_hook_and_local_process_time_use_one_fresh_inspection(self):
        self.append(self.summary)
        raw = dict(self.stop)
        raw.pop("process_generation")
        self.runtime.claude_process_started_at = watch.dt.datetime.fromtimestamp(
            self.process_started_at).isoformat()
        inspection = dict(generation=self.identity["process_generation"],
                          started_epoch=self.process_started_at)
        with mock.patch.object(watch, "inspect_claude_process", return_value=inspection) as inspect:
            self.assertEqual(self.handle(raw), "suppressed_waiting_supervisor")
            inspect.assert_called_once_with(self.identity["agent_pid"])
        self.assertEqual(self.runtime.claude_supervisor_wait["binding"], self.identity)
        self.assertEqual(self.runtime.claude_supervisor_wait["native_summary"]["process_started_at"],
                         self.process_started_at)
        self.assertNotIn("process_generation", raw)
        self.assertEqual(self.client.inputs, [])

    def test_raw_native_hook_cannot_inherit_saved_generation_after_process_change(self):
        self.append(self.summary)
        raw = dict(self.stop)
        raw.pop("process_generation")
        with mock.patch.object(watch, "inspect_claude_process", return_value=dict(
                generation="changed-live-process", started_epoch=self.process_started_at)):
            self.assertIsNone(self.daemon._claude_supervisor_wait_guard(
                self.target, self.runtime, raw))
        self.assertIsNone(self.runtime.claude_supervisor_wait)
        self.assertNotIn("process_generation", raw)
        self.assertEqual(self.client.inputs, [])

    def test_raw_human_hook_can_discover_and_take_over_native_wait(self):
        self.append(self.summary)
        human = self.prompt("Continue the explicitly requested task", offset=-10)
        human.pop("process_generation")
        inspection = dict(generation=self.identity["process_generation"],
                          started_epoch=self.process_started_at)
        with mock.patch.object(watch, "inspect_claude_process", return_value=inspection):
            self.assertEqual(self.handle(human), "human_prompt")
        audit = self.runtime.claude_supervisor_human_takeover
        self.assertEqual(audit["identity"], self.identity)
        self.assertEqual(audit["event"]["event_id"], human["event_id"])
        self.assertIsNone(self.runtime.claude_supervisor_wait)
        self.assertNotIn("process_generation", human)
        self.assertEqual(self.client.inputs, [])
        self.assert_no_receipt()

    def test_human_discovers_unobserved_native_wait_and_persists_takeover(self):
        self.append(self.summary)
        original_attempt = self.attempt_path.read_bytes()
        event = self.prompt("Take over with a new explicitly requested task", offset=-10)
        self.assertEqual(self.handle(event), "human_prompt")
        audit = self.runtime.claude_supervisor_human_takeover
        self.assertIsNotNone(audit)
        self.assertEqual(audit["wait"]["origin"], "native_stop_summary")
        self.assertIsNone(audit["task_binding"])
        self.assertEqual(audit["event"]["event_id"], event["event_id"])
        self.assertIsNone(self.runtime.claude_supervisor_wait)
        self.assertIsNone(self.runtime.claude_task_binding)
        self.assertEqual(self.attempt_path.read_bytes(), original_attempt)
        self.assertEqual(self.client.inputs, [])
        self.assert_no_receipt()

    def test_stop_accepted_before_summary_is_discovered_later_without_replaying_hook(self):
        event = copy.deepcopy(self.stop)
        self.handle(event)
        self.assertIsNone(self.runtime.claude_supervisor_wait)
        self.assertTrue(self.daemon.claude_event_ledger.contains(event["event_id"]))
        original_journal = self.daemon.claude_event_inbox.journal_path.read_bytes()
        self.append(self.summary)
        self.assertEqual(self.daemon._claude_supervisor_wait_guard(self.target, self.runtime),
                         adapter.WAITING)
        self.assertEqual(self.runtime.claude_supervisor_wait["native_summary"]["uuid"],
                         self.summary["uuid"])
        self.assertEqual(self.daemon.claude_event_inbox.journal_path.read_bytes(), original_journal)
        self.assertIsNone(self.runtime.claude_task_binding)
        self.assertEqual(self.client.inputs, [])
        self.assert_no_receipt()

    def test_verified_successor_handshake_does_not_resurrect_previous_native_wait(self):
        self.append(self.summary)
        self.assertEqual(self.handle(self.stop), "suppressed_waiting_supervisor")
        later = self.make_task("task-two", self.now - 10, self.now - 3)
        self.assertEqual(self.handshake(later, -5),
                         ("handshake_challenge", "handshake_wait", "task_dispatch"))
        self.assertIsNone(self.runtime.claude_supervisor_wait)
        self.assertEqual(self.runtime.claude_task_binding["task_id"], "task-two")
        self.assertIsNone(self.daemon._claude_supervisor_wait_guard(self.target, self.runtime))
        self.assertIsNone(self.runtime.claude_supervisor_wait)
        self.assertEqual(self.client.inputs, [])
        self.assert_no_receipt()

    def test_restart_preserves_native_wait_and_original_timeout(self):
        self.append(self.summary)
        self.assertEqual(self.handle(self.stop), "suppressed_waiting_supervisor")
        original = copy.deepcopy(self.runtime.claude_supervisor_wait)
        self.daemon.save()
        restarted = watch.WatchDaemon(config_path=self.config_path, state_path=self.state_path,
                                      client=self.client)
        runtime = restarted.runtime[self.surface]
        self.assertEqual(runtime.claude_supervisor_wait, original)
        self.assertIsNone(runtime.claude_task_binding)
        with mock.patch.object(adapter.time, "time", return_value=original["deadline_at"] + 5):
            self.assertEqual(restarted._claude_supervisor_wait_guard(self.target, runtime),
                             adapter.TIMED_OUT)
        self.assertEqual(runtime.claude_supervisor_wait["deadline_at"], original["deadline_at"])
        self.assertFalse(runtime.claude_completed_latched)
        self.assertEqual(self.client.inputs, [])
        self.assert_no_receipt()


if __name__ == "__main__":
    unittest.main()
