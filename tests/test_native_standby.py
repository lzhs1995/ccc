"""Offline lifecycle failure points; no native, controller or model requests."""
import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

import ccc_native_standby as standby


class StandbyLedgerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'cohort'
        self.now = 10.0
        self.boot, self.workspace, self.cohort, self.action = [str(uuid.uuid4()) for _ in range(4)]
        self.gen = 'a' * 64
        self.ledger = standby.StandbyLedger.create(self.path, cohort_id=self.cohort,
            workspace_id=self.workspace, boot_id=self.boot, mode='b', prompt='fixed prompt',
            config_generation=self.gen, clock=lambda: self.now)
        self.rows = [dict(index=i, launch_id=str(uuid.uuid4()), surface_id=str(uuid.uuid4()),
            session_id=str(uuid.uuid4()), workspace_id=self.workspace, pid=1000+i,
            birth=[12345, i], claim_sha256='b'*64, argv_sha256='c'*64,
            initialized=True, idle=True, composer_empty=True, pending_approval=False,
            task_count=0, user_input_count=0, model_request_count=0,
            observed_monotonic=self.now, boot_id=self.boot, generation=self.gen) for i in range(50)]
        self.sent = []

    def ready(self, rows=None):
        return self.ledger.observe_ready(self.rows if rows is None else rows,
            config_generation=self.gen, boot_id=self.boot, authorized=True)

    def activate(self, **changes):
        args = dict(action_id=self.action, mode='b', prompt='fixed prompt',
                    config_generation=self.gen, boot_id=self.boot, authorized=True)
        args.update(changes)
        return self.ledger.consume_activation(**args)

    def deliver(self, index=0, **changes):
        def transport(row, prompt, input_id, *, write_guard):
            with write_guard():
                self.sent.append((row, prompt, input_id))
        args = dict(action_id=self.action, observe=lambda i: copy.deepcopy(self.rows[i]),
                    authorized=lambda i: True, send=transport)
        args.update(changes)
        return self.ledger.deliver(index, **args)

    def test_fifty_originals_each_receive_one_input_and_repeated_actions_never_resend(self):
        self.ready()
        self.assertTrue(self.activate())
        self.assertFalse(self.activate())
        self.assertFalse(self.activate(action_id=str(uuid.uuid4())))
        for i in range(50):
            self.assertTrue(self.deliver(i))
            self.assertFalse(self.deliver(i))
        self.assertEqual(len(self.sent), 50)
        self.assertEqual(len({x[2] for x in self.sent}), 50)
        self.assertEqual({x[0]['session_id'] for x in self.sent}, {r['session_id'] for r in self.rows})

    def test_missing_original_or_duplicate_session_is_not_ready(self):
        for bad in (self.rows[:-1], self.rows + self.rows[:1]):
            with self.assertRaises(ValueError):
                self.ready(bad)
        self.assertFalse((self.path / 'activation.json').exists())

    def test_same_second_pid_reuse_permanently_invalidates_a_b_a(self):
        self.ready()
        bad = copy.deepcopy(self.rows)
        bad[0]['birth'][1] += 1
        with self.assertRaises(ValueError):
            self.ready(bad)
        with self.assertRaises(ValueError):
            self.ready()
        self.assertFalse(self.sent)

    def test_restart_cannot_restore_readiness_or_consumed_activation(self):
        self.ready()
        self.ledger = standby.StandbyLedger(self.path, clock=lambda: self.now)
        with self.assertRaises(ValueError):
            self.activate()
        self.ready()
        self.assertTrue(self.activate())
        self.ledger = standby.StandbyLedger(self.path, clock=lambda: self.now)
        self.assertFalse(self.activate())
        with self.assertRaises(ValueError):
            self.deliver()
        self.assertFalse(self.sent)

    def test_restart_preserves_original_identity_roster(self):
        self.ready()
        self.ledger = standby.StandbyLedger(self.path, clock=lambda: self.now)
        self.rows[0]['session_id'] = str(uuid.uuid4())
        with self.assertRaises(ValueError):
            self.ready()

    def test_missing_activation_record_after_restart_does_not_revive_cohort(self):
        self.ready()
        self.activate()
        (self.path / 'activation.json').unlink()
        self.ledger = standby.StandbyLedger(self.path, clock=lambda: self.now)
        with self.assertRaises(ValueError):
            self.ready()
        self.assertFalse(self.activate())

    def test_task_or_model_request_before_activation_invalidates_readiness(self):
        self.rows[0]['model_request_count'] = 1
        with self.assertRaises(ValueError):
            self.ready()
        self.rows[0]['model_request_count'] = 0
        with self.assertRaises(ValueError):
            self.ready()

    def test_observation_age_is_not_reset_when_publishing_readiness(self):
        self.now = 11.9
        self.ready()
        self.now = 12.1
        with self.assertRaises(ValueError):
            self.activate()

    def test_infinite_observation_is_rejected(self):
        self.rows[0]['observed_monotonic'] = float('inf')
        with self.assertRaises(ValueError):
            self.ready()

    def test_original_writer_inode_is_fixed_across_ready_and_delivery(self):
        row = self.rows[0]
        row.update(writer_lock='/native/thread-writer-locks/' + row['session_id'] + '.lock',
                   writer_identity=[1, 100])
        self.ready()
        self.activate()
        self.assertEqual(self.ledger._activation['originals'][0]['writer_identity'], [1, 100])
        row['writer_identity'][1] += 1
        with self.assertRaises(ValueError):
            self.deliver()
        self.assertFalse(self.sent)

    def test_runtime_observation_exception_permanently_invalidates_readiness(self):
        self.ready()
        self.activate()
        def broken(index):
            raise RuntimeError('native identity query failed')
        with self.assertRaises(RuntimeError):
            self.deliver(observe=broken)
        with self.assertRaises(ValueError):
            self.deliver()
        self.assertFalse(self.sent)

    def test_profile_changes_before_activation_do_not_restore_after_roundtrip(self):
        self.ready()
        with self.assertRaises(ValueError):
            self.activate(config_generation='d'*64)
        with self.assertRaises(ValueError):
            self.activate()

    def test_wrong_prompt_cannot_activate_the_prepared_mode(self):
        self.ready()
        with self.assertRaises(ValueError):
            self.activate(prompt='different prompt')

    def test_transport_exception_is_consumed_and_not_replayed(self):
        self.ready()
        self.activate()
        def uncertain(row, prompt, input_id, *, write_guard):
            with write_guard():
                self.sent.append(input_id)
            raise TimeoutError('ACK lost after write')
        with self.assertRaises(TimeoutError):
            self.deliver(send=uncertain)
        self.assertFalse(self.deliver())
        self.assertEqual(len(self.sent), 1)

    def test_missing_claim_after_attempt_cannot_restore_send_permission(self):
        self.ready()
        self.activate()
        self.deliver()
        (self.path / 'input-0.json').unlink()
        self.assertFalse(self.deliver())
        self.assertEqual(len(self.sent), 1)

    def test_storage_error_before_activation_does_not_authorize_delivery(self):
        self.ready()
        with patch.object(standby.os, 'fsync', side_effect=OSError(28, 'no space')):
            with self.assertRaises(OSError):
                self.activate()
        self.assertFalse(self.activate())
        with self.assertRaises(ValueError):
            self.deliver()

    def test_input_claim_storage_error_leaves_zero_sends_and_consumes_slot(self):
        self.ready()
        self.activate()
        with patch.object(standby.os, 'fsync', side_effect=OSError(28, 'no space')):
            with self.assertRaises(OSError):
                self.deliver()
        self.assertFalse(self.deliver())
        self.assertFalse(self.sent)

    def test_pause_between_claim_and_send_blocks_input(self):
        self.ready()
        self.activate()
        calls = []
        def allowed(index):
            calls.append(index)
            return len(calls) == 1
        with self.assertRaises(ValueError):
            self.deliver(authorized=allowed)
        self.assertTrue((self.path / 'input-0.json').exists())
        self.assertFalse(self.deliver())
        self.assertFalse(self.sent)

    def test_foreign_workspace_at_send_never_receives_input(self):
        self.ready()
        self.activate()
        self.rows[0]['workspace_id'] = str(uuid.uuid4())
        with self.assertRaises(ValueError):
            self.deliver()
        self.assertFalse(self.sent)

    def test_partial_activation_never_substitutes_originals(self):
        self.ready()
        self.activate()
        self.deliver(0)
        self.rows[1]['session_id'] = str(uuid.uuid4())
        with self.assertRaises(ValueError):
            self.deliver(1)
        with self.assertRaises(ValueError):
            self.deliver(2)
        self.assertEqual(len(self.sent), 1)

    def test_replaced_activation_evidence_never_sends(self):
        self.ready()
        self.activate()
        p = self.path / 'activation.json'
        record = json.loads(p.read_text())
        record['action_id'] = str(uuid.uuid4())
        p.write_text(json.dumps(record))
        with self.assertRaises(ValueError):
            self.deliver()
        self.assertFalse(self.sent)

    def test_roster_change_or_disappearance_cannot_revive_after_restore(self):
        for remove in (False, True):
            with self.subTest(remove=remove):
                self.setUp()
                self.ready()
                path = self.path / 'originals.json'
                raw = path.read_bytes()
                if remove:
                    path.unlink()
                else:
                    path.write_text('[]')
                with self.assertRaises((ValueError, OSError)):
                    self.ready()
                path.write_bytes(raw)
                with self.assertRaises(ValueError):
                    self.activate()

    def test_malformed_generation_permanently_invalidates_readiness(self):
        self.ready()
        with self.assertRaises(ValueError):
            self.activate(config_generation='')
        with self.assertRaises(ValueError):
            self.activate()

    def test_callback_blocked_during_other_slot_invalidation_cannot_send(self):
        self.ready()
        self.activate()
        waiting, resume = threading.Event(), threading.Event()
        calls, errors = [], []
        def observe(index):
            calls.append(index)
            if len(calls) == 2:
                waiting.set()
                self.assertTrue(resume.wait(2))
            return copy.deepcopy(self.rows[index])
        def attempt():
            try:
                self.deliver(0, observe=observe)
            except ValueError as exc:
                errors.append(str(exc))
        thread = threading.Thread(target=attempt)
        thread.start()
        self.assertTrue(waiting.wait(2))
        try:
            self.rows[1]['session_id'] = str(uuid.uuid4())
            with self.assertRaises(ValueError):
                self.deliver(1)
        finally:
            resume.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertFalse(self.sent)

    def test_invalidation_after_preflight_but_before_transport_write_is_rejected(self):
        self.ready()
        self.activate()
        def transport(row, prompt, input_id, *, write_guard):
            self.ledger._invalidate('concurrent change before write')
            with write_guard():
                self.sent.append(input_id)
        with self.assertRaises(ValueError):
            self.deliver(send=transport)
        self.assertFalse(self.sent)

    def test_identity_change_during_connection_is_rechecked_at_write(self):
        self.ready()
        self.activate()
        def transport(row, prompt, input_id, *, write_guard):
            self.rows[0]['birth'][1] += 1
            with write_guard():
                self.sent.append(input_id)
        with self.assertRaises(ValueError):
            self.deliver(send=transport)
        self.assertFalse(self.sent)

    def test_pause_during_connection_is_rechecked_at_write(self):
        self.ready()
        self.activate()
        active = [True]
        def transport(row, prompt, input_id, *, write_guard):
            active[0] = False
            with write_guard():
                self.sent.append(input_id)
        with self.assertRaises(ValueError):
            self.deliver(send=transport, authorized=lambda i: active[0])
        self.assertFalse(self.sent)


if __name__ == '__main__':
    unittest.main()
