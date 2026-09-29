"""Durable request admission, including storage/dispatch/crash boundaries."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch

from ccc_access_budget import AccessBudget, AdmissionClosed, Policy


class AccessBudgetTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.policy = Policy(str(uuid.uuid4()), str(uuid.uuid4()))
        self.path = self.root / 'admission.jsonl'
        self.budget = self.open(create=True)

    def open(self, create=False, path=None, policy=None):
        budget = AccessBudget(path or self.path, policy or self.policy, create=create)
        self.addCleanup(budget.close)
        return budget

    def dispatched(self, slot=0, session='main-session'):
        reservation = self.budget.reserve(slot, session)
        self.budget.begin_dispatch(reservation)
        return reservation

    def test_fifty_slots_can_all_be_dispatched(self):
        with ThreadPoolExecutor(max_workers=50) as workers:
            reservations = list(workers.map(lambda slot: self.budget.reserve(slot, f'session-{slot}'), range(50)))
        for reservation in reservations:
            self.budget.begin_dispatch(reservation)
        self.assertEqual(self.budget.snapshot()['in_flight'], 50)
        with self.assertRaises(AdmissionClosed):
            self.budget.reserve(0, 'session-0')
        with self.assertRaises(ValueError):
            self.budget.reserve(50, 'outside')

    def test_success_closes_other_slots_and_queued_durable_reservations(self):
        first = self.dispatched()
        queued = self.budget.reserve(1, 'session-1')
        self.budget.note_success(first, 'response-1')
        with self.assertRaises(AdmissionClosed):
            self.budget.begin_dispatch(queued)
        with self.assertRaises(AdmissionClosed):
            self.budget.reserve(2, 'session-2')
        self.budget.finish(queued, 'cancelled_before_dispatch')
        self.budget.finish(first, 'complete', response_id='response-1')

    def test_success_before_finish_crash_closes_whole_job_on_restart(self):
        reservation = self.dispatched()
        self.budget.note_success(reservation, 'response-without-finished-record')
        self.budget.close()
        recovered = self.open()
        self.assertEqual(recovered.snapshot()['attempts'], 1)
        for slot in (0, 1, 49):
            with self.subTest(slot=slot), self.assertRaises(AdmissionClosed):
                recovered.reserve(slot, 'next-session')

    def test_pending_before_dispatch_also_stays_closed_on_restart(self):
        self.budget.reserve(0, 'session')
        self.budget.close()
        recovered = self.open()
        with self.assertRaises(AdmissionClosed):
            recovered.reserve(49, 'different-slot')

    def test_completed_success_survives_restart_without_usage(self):
        reservation = self.dispatched()
        self.budget.finish(reservation, 'complete', response_id='response-1', usage=None)
        self.budget.close()
        recovered = self.open()
        self.assertEqual(recovered.snapshot()['first_complete']['response_id'], 'response-1')
        with self.assertRaises(AdmissionClosed):
            recovered.reserve(1, 'next')

    def test_observed_success_cannot_be_downgraded_before_a_restart(self):
        first = self.dispatched()
        self.budget.note_success(first, 'response-1')
        for outcome in ('rejected', 'uncertain', 'cancelled_before_dispatch'):
            with self.subTest(outcome=outcome), self.assertRaisesRegex(ValueError, 'irreversible|dispatch state'):
                self.budget.finish(first, outcome)
        with self.assertRaises(ValueError):
            self.budget.finish(first, 'complete', response_id='different-response')
        self.budget.close()
        recovered = self.open()
        with self.assertRaises(AdmissionClosed):
            recovered.reserve(1, 'after-restart')

    def test_other_in_flight_request_can_still_record_its_real_failure(self):
        first = self.dispatched()
        other = self.dispatched(1, 'session-1')
        self.budget.note_success(first, 'response-1')
        self.budget.finish(other, 'rejected')
        self.budget.finish(first, 'complete', response_id='response-1')
        self.assertEqual(self.budget.snapshot()['in_flight'], 0)

    def test_native_title_with_a_new_thread_cannot_spend_from_the_same_slot(self):
        reservation = self.dispatched()
        self.budget.finish(reservation, 'rejected')
        with self.assertRaises(AdmissionClosed):
            self.budget.reserve(0, 'independent-title-session')
        self.budget.close()
        recovered = self.open()
        with self.assertRaises(AdmissionClosed):
            recovered.reserve(0, 'independent-title-session')
        self.assertEqual(recovered.reserve(0, 'main-session').number, 2)

    def test_cancel_after_dispatch_is_an_invalid_transition(self):
        reservation = self.dispatched()
        with self.assertRaises(ValueError):
            self.budget.finish(reservation, 'cancelled_before_dispatch')
        self.assertEqual(self.budget.snapshot()['in_flight'], 1)
        self.budget.finish(reservation, 'rejected')

    def test_nondispatched_request_cannot_report_an_upstream_outcome(self):
        reservation = self.budget.reserve(0, 'session')
        for outcome in ('complete', 'rejected', 'uncertain'):
            with self.subTest(outcome=outcome), self.assertRaises(ValueError):
                self.budget.finish(reservation, outcome, response_id='fabricated')
        self.assertIsNone(self.budget.snapshot()['first_complete'])
        self.budget.finish(reservation, 'cancelled_before_dispatch')

    def test_cancel_persistence_race_cannot_dispatch_a_finishing_reservation(self):
        reservation = self.budget.reserve(0, 'session')
        entered, release = threading.Event(), threading.Event()
        original = self.budget._append
        def delayed(event):
            if event['kind'] == 'finished':
                entered.set()
                self.assertTrue(release.wait(3))
            original(event)
        with patch.object(self.budget, '_append', side_effect=delayed), ThreadPoolExecutor(max_workers=1) as workers:
            future = workers.submit(self.budget.finish, reservation, 'cancelled_before_dispatch')
            try:
                self.assertTrue(entered.wait(3))
                with self.assertRaises(AdmissionClosed):
                    self.budget.begin_dispatch(reservation)
            finally:
                release.set()
            future.result(timeout=3)

    def test_slow_reservation_storage_does_not_delay_success_gate(self):
        first = self.dispatched()
        entered, release = threading.Event(), threading.Event()
        original = self.budget._append
        def delayed(event):
            if event['kind'] == 'reserved':
                entered.set()
                self.assertTrue(release.wait(3))
            original(event)
        with patch.object(self.budget, '_append', side_effect=delayed), ThreadPoolExecutor(max_workers=1) as workers:
            future = workers.submit(self.budget.reserve, 1, 'session-1')
            try:
                self.assertTrue(entered.wait(3))
                started = time.monotonic()
                self.budget.note_success(first, 'response-1')
                self.assertLess(time.monotonic() - started, .1)
            finally:
                release.set()
            queued = future.result(timeout=3)
        with self.assertRaises(AdmissionClosed):
            self.budget.begin_dispatch(queued)

    def test_storage_error_closes_new_and_already_reserved_requests(self):
        queued = self.budget.reserve(0, 'session-0')
        with patch('ccc_access_budget.os.fsync', side_effect=OSError('disk unavailable')):
            with self.assertRaises(OSError):
                self.budget.reserve(1, 'session-1')
        with self.assertRaises(AdmissionClosed):
            self.budget.begin_dispatch(queued)
        with self.assertRaises(AdmissionClosed):
            self.budget.reserve(2, 'session-2')

    def test_attempts_are_not_refunded_after_cancel_or_restart(self):
        self.budget.close()
        path = self.root / 'limited.jsonl'
        policy = replace(self.policy, max_attempts=50)
        budget = self.open(create=True, path=path, policy=policy)
        for _ in range(25):
            reservation = budget.reserve(0, 'session')
            budget.finish(reservation, 'cancelled_before_dispatch')
        budget.close()
        recovered = self.open(path=path, policy=policy)
        for _ in range(25):
            reservation = recovered.reserve(0, 'session')
            recovered.begin_dispatch(reservation)
            recovered.finish(reservation, 'rejected')
        with self.assertRaises(AdmissionClosed):
            recovered.reserve(49, 'new-session')
        self.assertEqual(recovered.snapshot()['attempts'], 50)

    def test_other_workspace_remains_independent_after_success(self):
        other = self.open(create=True, path=self.root / 'other.jsonl',
                          policy=replace(self.policy, workspace_id=str(uuid.uuid4()), job_id=str(uuid.uuid4())))
        self.budget.finish(self.dispatched(), 'complete', response_id='response-1')
        reservation = other.reserve(0, 'other-session')
        other.begin_dispatch(reservation)
        self.assertEqual(other.snapshot()['in_flight'], 1)

    def test_same_journal_cannot_have_two_owners(self):
        with self.assertRaises(BlockingIOError):
            AccessBudget(self.path, self.policy)

    def test_different_job_or_policy_cannot_reuse_history(self):
        self.budget.close()
        with self.assertRaises(ValueError):
            AccessBudget(self.path, replace(self.policy, job_id=str(uuid.uuid4())))
        with self.assertRaises(ValueError):
            AccessBudget(self.path, replace(self.policy, max_attempts=100))

    def test_torn_history_is_not_treated_as_an_empty_budget(self):
        self.budget.close()
        with self.path.open('ab') as handle:
            handle.write(b'{"kind":"reserved"')
        with self.assertRaises(ValueError):
            AccessBudget(self.path, self.policy)

    def test_duplicate_active_slot_in_history_is_rejected(self):
        self.budget.reserve(0, 'session')
        self.budget.close()
        with self.path.open('a') as handle:
            handle.write(json.dumps({'kind': 'reserved', 'number': 2, 'slot': 0, 'session_id': 'session'}) + '\n')
        with self.assertRaises(ValueError):
            AccessBudget(self.path, self.policy)

    def test_closed_budget_cannot_dispatch_prepared_requests(self):
        queued = self.budget.reserve(0, 'session')
        self.budget.close()
        with self.assertRaises(AdmissionClosed):
            self.budget.begin_dispatch(queued)


if __name__ == '__main__':
    unittest.main()
