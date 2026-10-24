"""Real file rounds, original journals and JSONL proofs; external I/O is offline."""
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
from unittest.mock import patch

import executor_ready as ready
import executor_reask as reask
from test_codex_native_p2_regressions import OfflineCase, CALLER, TARGET, WORKSPACE


class ReaskNativeCadenceTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.now = 1791984000.0
        self.base = self.now
        self.fixture.stack.enter_context(patch.object(reask.time, 'time',
                                                     side_effect=lambda: self.now))
        self.rpc.receive = False
        self.rpc.before_mutation = lambda _method, params: self.receive(params['input'][0]['text'])
        self.channel = self.fixture.home / 'reask-channel'
        reask.start(CALLER, TARGET, task_id='native-cadence', now=self.now,
                    workspace=WORKSPACE, channels=[str(self.channel)])

    def receive(self, text, after=0):
        self.fixture.append_user(text, timestamp=datetime.fromtimestamp(
            self.now + after, timezone.utc).isoformat())

    def tick(self, elapsed=0, *, file_only=False):
        self.now = self.base + elapsed
        return reask.tick(CALLER, self.now, file_only=file_only)

    def original(self, ask):
        marker = ask['marker']
        request = ready.requests_dir() / (ready.request_key(CALLER, marker) + '.json')
        journal = self.state_root / 'message-dispatch-v1' / ready.request_key(CALLER, marker)
        paths = sorted(journal.glob('attempt-*.json'))
        self.assertEqual(len(paths), 1)
        return json.loads(request.read_text()), journal, {
            path: path.read_bytes() for path in (request, paths[0])}

    def assert_unchanged(self, snapshot):
        for path, raw in snapshot.items():
            self.assertEqual(path.read_bytes(), raw)

    def assert_file_round(self, state, number):
        self.assertEqual(state['ask_count'], number)
        self.assertEqual(state['file_posts'], number)
        current = json.loads((self.channel / reask.current_name(CALLER)).read_text())
        self.assertEqual(current['marker'], state['asks'][-1]['marker'])

    def test_new_input_allowed_at_exactly_sixty_seconds_after_native_receipt(self):
        first = self.tick()
        old = first['asks'][-1]
        self.assertEqual(old['outcome'], 'CONFIRMED')
        _record, _journal, snapshot = self.original(old)
        self.assertEqual(first['terminal_next_due_epoch'], self.base + 60)
        self.assertEqual(len(self.rpc.mutations), 1)
        second = self.tick(60)
        self.assertEqual(second['asks'][-1]['outcome'], 'CONFIRMED')
        self.assertNotEqual(second['asks'][-1]['marker'], old['marker'])
        self.assertEqual(len(self.rpc.mutations), 2)
        self.assert_file_round(second, 2)
        self.assert_unchanged(snapshot)

    def test_late_receipt_holds_input_but_file_rounds_continue(self):
        self.rpc.before_mutation = None
        first = self.tick()
        old = copy.deepcopy(first['asks'][-1])
        record, _journal, snapshot = self.original(old)
        self.assertEqual(len(self.rpc.mutations), 1)
        second = self.tick(60)
        self.assertEqual(second['asks'][-1]['outcome'], 'SKIPPED_UNVERIFIED')
        self.now = self.base + 90
        self.receive(record['text'])
        third = self.tick(120)
        self.assertEqual(third['asks'][-1]['outcome'], 'SKIPPED_NATIVE_RECEIPT_INTERVAL')
        self.assertEqual(third['terminal_next_due_epoch'], self.base + 150)
        self.assertEqual(third['terminal_last_confirmed']['marker'], old['marker'])
        self.assertEqual(len(self.rpc.mutations), 1)
        self.assert_file_round(third, 3)
        self.rpc.before_mutation = lambda _method, params: self.receive(params['input'][0]['text'])
        fourth = self.tick(180)
        self.assertEqual(fourth['asks'][-1]['outcome'], 'CONFIRMED')
        self.assertEqual(len(self.rpc.mutations), 2)
        self.assert_unchanged(snapshot)

    def test_cached_epoch_cannot_shorten_original_native_interval_after_reload(self):
        self.rpc.before_mutation = lambda _method, params: self.receive(params['input'][0]['text'], after=0.001)
        first = self.tick()
        state = reask.load(CALLER)
        state['terminal_last_confirmed']['receipt']['native_received_at_epoch'] = self.base - 1000
        state['terminal_next_due_epoch'] = self.base - 1
        reask.save(state)
        second = self.tick(60)
        self.assertEqual(second['asks'][-1]['outcome'], 'SKIPPED_NATIVE_RECEIPT_INTERVAL')
        self.assertAlmostEqual(second['terminal_next_due_epoch'], self.base + 60.001, places=5)
        self.assertEqual(len(self.rpc.mutations), 1)
        self.assertEqual(second['terminal_last_confirmed']['marker'], first['asks'][-1]['marker'])
        self.assert_file_round(second, 2)

    def test_pending_survives_history_rollover_and_reconciles_original_only(self):
        self.rpc.before_mutation = None
        first = self.tick()
        old = copy.deepcopy(first['asks'][-1])
        record, _journal, snapshot = self.original(old)
        for ordinal in range(1, reask.HISTORY + 3):
            state = self.tick(60 * ordinal)
        self.assertNotIn(old['marker'], [a['marker'] for a in state['asks']])
        self.assertEqual(state['terminal_pending']['marker'], old['marker'])
        self.assertEqual(len(self.rpc.mutations), 1)
        self.now += 30
        self.receive(record['text'])
        state = self.tick(60 * (reask.HISTORY + 3))
        self.assertEqual(state['asks'][-1]['outcome'], 'SKIPPED_NATIVE_RECEIPT_INTERVAL')
        self.assertEqual(state['terminal_last_confirmed']['marker'], old['marker'])
        self.assertEqual(len(self.rpc.mutations), 1)
        self.assert_unchanged(snapshot)

    def test_last_receipt_survives_file_only_history_rollover(self):
        first = self.tick()
        old = first['asks'][-1]
        _record, _journal, snapshot = self.original(old)
        for ordinal in range(1, reask.HISTORY + 2):
            state = self.tick(60 * ordinal, file_only=True)
        self.assertNotIn(old['marker'], [a['marker'] for a in state['asks']])
        self.assertEqual(state['terminal_last_confirmed']['marker'], old['marker'])
        # Corrupt the original proof after the file-only rounds: the cached
        # confirmation must not authorize transport even though 60s elapsed.
        self.fixture.transcript.write_bytes(self.fixture.transcript.read_bytes().replace(
            b'"role": "user"', b'"role": "tool"'))
        state = self.tick(60 * (reask.HISTORY + 2))
        self.assertEqual(state['asks'][-1]['outcome'], 'SKIPPED_UNVERIFIED')
        self.assertEqual(len(self.rpc.mutations), 1)
        self.assert_unchanged(snapshot)

    def test_changed_original_receipt_cannot_use_cached_confirmation(self):
        first = self.tick()
        _record, journal, _snapshot = self.original(first['asks'][-1])
        path = journal / 'receipt.json'
        body = json.loads(path.read_text())
        body['native_proof']['sha256'] = '0' * 64
        path.write_text(json.dumps(body))
        second = self.tick(60)
        self.assertEqual(second['asks'][-1]['outcome'], 'SKIPPED_UNVERIFIED')
        self.assertEqual(len(self.rpc.mutations), 1)

    def test_missing_original_request_refuses_input_after_reload(self):
        first = self.tick()
        _record, _journal, snapshot = self.original(first['asks'][-1])
        request = next(p for p in snapshot if p.parent == ready.requests_dir())
        request.unlink()
        second = self.tick(60)
        self.assertEqual(second['asks'][-1]['outcome'], 'SKIPPED_ORIGINAL_CONTROLLER_REQUIRED')
        self.assertEqual(len(self.rpc.mutations), 1)

    def test_changed_origin_never_migrates_to_current_controller(self):
        first = self.tick()
        state = reask.load(CALLER)
        state['terminal_last_confirmed']['origin']['controller']['source'] += '-different'
        reask.save(state)
        second = self.tick(60)
        self.assertEqual(second['asks'][-1]['outcome'], 'SKIPPED_ORIGINAL_CONTROLLER_REQUIRED')
        self.assertEqual(len(self.rpc.mutations), 1)
        self.assertEqual(second['terminal_last_confirmed']['marker'], first['asks'][-1]['marker'])

    def test_proven_no_input_does_not_start_a_receipt_interval(self):
        self.fixture.rpc.side_effect = TimeoutError('before input')
        first = self.tick()
        self.assertEqual(first['asks'][-1]['outcome'], 'NO_INPUT')
        _record, _journal, snapshot = self.original(first['asks'][-1])
        self.assertFalse(first.get('terminal_last_confirmed'))
        self.fixture.rpc.side_effect = lambda **kwargs: self.rpc
        second = self.tick(60)
        self.assertEqual(second['asks'][-1]['outcome'], 'CONFIRMED')
        self.assertEqual(len(self.rpc.mutations), 1)
        self.assert_unchanged(snapshot)
