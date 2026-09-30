"""Sustained N keeps native retry and concurrency without resetting old budgets."""
import asyncio
import copy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import json
from pathlib import Path
import secrets
import tempfile
import time
import tracemalloc
import unittest
from unittest.mock import patch
import uuid

from ccc_access_budget import AccessBudget, AdmissionClosed, Policy
from ccc_access_gateway import BatchChannel, Gateway, Upstream
import ccc_access_service as service
import cmux_supervisor_tui as tui
from tests import test_access_service as service_fixture
from tests.test_access_gateway import UpstreamFixture, native_request


class SustainedAccountingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'journal.jsonl'
        self.policy = Policy(str(uuid.uuid4()), str(uuid.uuid4()),
                             max_attempts=None, attempt_mode='sustained')

    def test_unlimited_is_explicit_and_cannot_be_a_missing_finite_limit(self):
        for change in ({'max_attempts': None}, {'max_attempts': 0}, {'max_attempts': False},
                       {'max_attempts': 1000, 'attempt_mode': 'sustained'},
                       {'max_attempts': None, 'attempt_mode': None}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                Policy(self.policy.workspace_id, self.policy.job_id, **change)

    def test_more_than_one_thousand_attempts_remain_authorized_and_recover_without_reset(self):
        budget = AccessBudget(self.path, self.policy, create=True)
        try:
            for i in range(1251):
                reservation = budget.reserve(i % 50, 'native-' + str(i % 50))
                budget.begin_dispatch(reservation)
                budget.finish(reservation, 'rejected')
            self.assertEqual(budget.snapshot()['attempts'], 1251)
            budget.check_admission(0, 'native-0')
        finally:
            budget.close()
        prior = self.path.read_bytes()
        recovered = AccessBudget(self.path, self.policy)
        try:
            self.assertEqual(recovered.snapshot()['attempts'], 1251)
            self.assertEqual(self.path.read_bytes(), prior)
            reservation = recovered.reserve(1, 'native-1')
            self.assertEqual(reservation.number, 1252)
            recovered.begin_dispatch(reservation)
            recovered.note_success(reservation, 'actual-response')
            for slot in range(50):
                with self.assertRaises(AdmissionClosed):
                    recovered.reserve(slot, 'native-' + str(slot))
            recovered.finish(reservation, 'complete', response_id='actual-response')
        finally:
            recovered.close()

    def test_fifty_concurrent_reservations_have_replayable_order(self):
        budget = AccessBudget(self.path, self.policy, create=True)
        def submit(slot):
            reservation = budget.reserve(slot, 'native-' + str(slot))
            budget.begin_dispatch(reservation)
            budget.finish(reservation, 'rejected')
        try:
            with ThreadPoolExecutor(max_workers=12) as pool:
                list(pool.map(submit, range(50)))
        finally:
            budget.close()
        rows = [json.loads(line) for line in self.path.read_bytes().splitlines()]
        self.assertEqual([row['number'] for row in rows if row['kind'] == 'reserved'], list(range(1, 51)))
        recovered = AccessBudget(self.path, self.policy)
        self.addCleanup(recovered.close)
        self.assertEqual(recovered.snapshot()['attempts'], 50)
        self.assertEqual(recovered._seen_numbers, set())

    def test_legacy_journal_bytes_are_supported_and_cannot_become_sustained(self):
        legacy = Policy(self.policy.workspace_id, self.policy.job_id)
        header = {'kind': 'policy', 'version': 1, 'workspace_id': legacy.workspace_id,
                  'job_id': legacy.job_id, 'slots': 50, 'max_attempts': 1000, 'max_output_tokens': 128}
        self.path.write_text(json.dumps(header) + '\n')
        self.path.chmod(0o600)
        before = self.path.read_bytes()
        budget = AccessBudget(self.path, legacy)
        budget.close()
        self.assertEqual(self.path.read_bytes(), before)
        with self.assertRaises(ValueError):
            AccessBudget(self.path, self.policy)
        self.assertEqual(self.path.read_bytes(), before)

    def test_sustained_history_over_old_size_limit_streams_with_bounded_memory(self):
        count = 80000
        with self.path.open('w') as handle:
            handle.write(json.dumps(self.policy.journal_header()) + '\n')
            for number in range(1, count + 1):
                handle.write(json.dumps({'kind': 'reserved', 'number': number, 'slot': number % 50,
                    'session_id': 'native-' + str(number % 50), 'at': 1790500000.123456}) + '\n')
                handle.write(json.dumps({'kind': 'finished', 'number': number, 'outcome': 'rejected',
                    'response_id': '', 'usage': None, 'at': 1790500001.123456}) + '\n')
        self.path.chmod(0o600)
        self.assertGreater(self.path.stat().st_size, 16 * 1024 * 1024)
        tracemalloc.start()
        try:
            budget = AccessBudget(self.path, self.policy)
            try:
                self.assertEqual(budget.snapshot()['attempts'], count)
                self.assertEqual(budget._seen_numbers, set())
                self.assertEqual(len(budget._sessions), 50)
            finally:
                budget.close()
            self.assertLess(tracemalloc.get_traced_memory()[1], 2 * 1024 * 1024)
        finally:
            tracemalloc.stop()

    def test_incomplete_crash_still_preserves_uncertainty_and_never_refunds(self):
        budget = AccessBudget(self.path, self.policy, create=True)
        budget.reserve(0, 'native-0')
        budget.close()
        recovered = AccessBudget(self.path, self.policy)
        self.addCleanup(recovered.close)
        self.assertEqual(recovered.snapshot()['attempts'], 1)
        self.assertTrue(recovered.snapshot()['fault'])
        with self.assertRaises(AdmissionClosed):
            recovered.reserve(1, 'native-1')


class SustainedServiceTests(unittest.TestCase):
    def setUp(self):
        service_fixture.AccessServiceBoundaryTests.setUp(self)

    @unittest.skipIf(__import__('sys').version_info < (3, 11), 'native settings require tomllib')
    def test_new_N_inherits_original_native_retry_settings_without_changing_them(self):
        self.native_config.write_text(self.native_config.read_text() +
                                      'request_max_retries=4\nstream_max_retries=5\n')
        before = self.native_config.read_bytes()
        self.job['access_policy'] = service.prepare(self.config, self.job, owner=self.owner)
        with patch.object(service, 'ensure_gateway', return_value=self.owner):
            args = service.launch_arguments(self.config, self.job, 0)
        self.assertFalse(any('request_max_retries' in arg or 'stream_max_retries' in arg for arg in args))
        self.assertEqual(self.native_config.read_bytes(), before)
        self.assertEqual(self.job['access_mode'], 'sustained-api-check-v2')
        self.assertIsNone(self.job['access_policy']['max_attempts'])

    @unittest.skipIf(__import__('sys').version_info < (3, 11), 'native settings require tomllib')
    def test_corrupt_continuous_marker_cannot_convert_finite_or_missing_policy_to_unlimited(self):
        self.job['access_policy'] = service.prepare(self.config, self.job, owner=self.owner)
        value = service.read_private(service.job_root(self.config, self.job['id']) / 'access.json')
        for change in ('missing_mode', 'finite_mode', 'finite_null', 'missing_limit', 'missing_attempt_mode'):
            descriptor = copy.deepcopy(value)
            job = copy.deepcopy(self.job)
            if change == 'missing_mode':
                descriptor.pop('mode')
            elif change == 'finite_mode':
                descriptor['mode'], descriptor['version'] = service.LEGACY_MODE, 1
            elif change == 'finite_null':
                descriptor['mode'], descriptor['version'] = service.LEGACY_MODE, 1
                descriptor['policy']['attempt_mode'] = 'finite'
            elif change == 'missing_limit':
                descriptor['policy'].pop('max_attempts')
            else:
                descriptor['policy'].pop('attempt_mode')
            job['access_mode'] = descriptor.get('mode')
            job['access_policy'].update(mode=descriptor.get('mode'), version=descriptor['version'],
                                       descriptor_sha256=service.descriptor_sha(descriptor))
            with self.subTest(change=change), self.assertRaises(ValueError):
                service.verify_descriptor(descriptor, self.config, job)

    def test_counter_only_stays_live_beyond_one_thousand_while_old_limit_remains(self):
        now = time.time()
        value = {'job_id': self.job['id'], 'workspace_id': self.job['workspace_id'], 'updated_at': now,
                 'attempt_mode': 'sustained', 'max_attempts': None, 'attempts': 1500, 'forwarded': 1500,
                 'blocked_slots': [], 'in_flight': 0, 'complete': 0, 'authorized': True}
        binding = {'job_id': self.job['id'], 'index': 0}
        state = service.continuation_decision(binding, value, self.job['workspace_id'])
        self.assertTrue(state['allowed'])
        self.assertIn('持续接入', tui.access_batch_progress(value))
        self.assertNotIn('/None', tui.access_batch_progress(value))
        self.assertNotIn('1000', tui.access_batch_prompt('fixture'))
        for changes in ({'attempt_mode': 'finite'}, {'attempt_mode': None}, {'attempt_mode': []},
                        {'max_attempts': 1000}, {'attempts': True}):
            with self.subTest(changes=changes):
                self.assertFalse(service.continuation_decision(binding, {**value, **changes},
                                                               self.job['workspace_id'])['allowed'])
        old = {**value, 'attempt_mode': 'finite', 'max_attempts': 1000, 'attempts': 1000, 'forwarded': 1000}
        self.assertEqual(service.continuation_decision(binding, old, self.job['workspace_id'])['phase'], 'exhausted')


class SustainedHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_fifty_slot_http_continues_past_1000_then_success_closes_admission(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = UpstreamFixture()
            upstream_port = await fixture.start()
            fixture.mode = 'error'
            policy = Policy(str(uuid.uuid4()), str(uuid.uuid4()), max_attempts=None, attempt_mode='sustained')
            budget = AccessBudget(Path(temp) / 'journal.jsonl', policy, create=True)
            channel = BatchChannel(budget, Upstream(f'http://127.0.0.1:{upstream_port}/0/v1',
                'gpt-6-astra', allow_loopback=True, timeout=10),
                [secrets.token_urlsafe(32) for _ in range(50)],
                sessions={i: str(uuid.uuid4()) for i in range(50)})
            gateway = Gateway({policy.job_id: channel})
            port = await gateway.start()
            try:
                for _ in range(25):
                    responses = await asyncio.gather(*(native_request(port, channel, i) for i in range(50)))
                    self.assertTrue(all(code == 500 for code, _ in responses))
                self.assertEqual(len(fixture.requests), 1250)
                self.assertEqual(fixture.peak, 50)
                self.assertEqual(budget.snapshot()['attempts'], 1250)
                self.assertLessEqual(len(channel.metrics['dispatch_times']), 512)
                fixture.mode = 'success'
                code, _ = await native_request(port, channel, 0)
                self.assertEqual(code, 200)
                before = len(fixture.requests)
                denied = await asyncio.gather(*(native_request(port, channel, i) for i in range(50)))
                self.assertTrue(all(code == 409 for code, _ in denied))
                self.assertEqual(len(fixture.requests), before)
                self.assertTrue(all(r['body']['tools'] == [] and r['body']['tool_choice'] == 'none'
                                    and r['body']['max_output_tokens'] == 128 and r['bytes'] < 1024
                                    for r in fixture.requests))
            finally:
                await gateway.close()
                await fixture.close()


if __name__ == '__main__':
    unittest.main()
