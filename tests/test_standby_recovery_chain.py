import copy
import unittest
from tools.standby_recovery_chain import evaluate


class ChainTests(unittest.TestCase):
    def setUp(self):
        self.witness = {'session_id': 'session', 'surface_id': 'surface'}
        self.records = [{'type': 'session_meta', 'payload': {'id': 'session'}}]
        for kind, turn, timestamp, extra in [
            ('task_started', 'one', '00:00:01', {}),
            ('task_complete', 'one', '00:00:02', {'error': {'codex_error_info': 'rate_limit_exceeded'}}),
            ('task_started', 'two', '00:00:02.300', {}),
            ('task_complete', 'two', '00:00:03', {'last_agent_message': 'OK'})]:
            self.records.append({'type': 'event_msg', 'timestamp': '1970-01-01T'+timestamp+'Z',
                                 'payload': {'type': kind, 'turn_id': turn, **extra}})
        self.responses = []
        for identifier, at in [('a', .9), ('b', 2.1)]:
            self.responses.append({'request': {'id': identifier, 'method': 'terminal.paste',
                'params': {'surface_id': 'surface', 'text': 'OK', 'submit_key': 'enter'}},
                'reply': {'id': identifier, 'ok': True}, 'forward_monotonic_ns': at*1e9,
                'forward_at': at, 'ack_at': at+.01})
        self.delivery = {'surface_id': 'surface', 'runtime': {'send_count': 1,
            'delivery_status': 'confirmed', 'send_attempt_id': 'attempt',
            'codex_sent_turn_key': 'session:one:2', 'send_started_at': 2.05,
            'send_completed_at': 2.12}}

    def check(self):
        return evaluate(self.records, self.witness, self.responses, self.delivery, 'OK')

    def test_complete_chain_and_timing(self):
        result = self.check()
        self.assertTrue(result['performance_passed'])
        self.assertAlmostEqual(result['native_next_ms'], 300)

    def test_send_during_reconnect_rejected(self):
        self.responses[1]['forward_at'] = 1.8
        with self.assertRaises(ValueError): self.check()

    def test_unrelated_delivery_rejected(self):
        for field, value in [('send_count', 2), ('codex_sent_turn_key', 'other:one:2'),
                             ('delivery_status', 'unknown')]:
            with self.subTest(field=field):
                original = copy.deepcopy(self.delivery)
                self.delivery['runtime'][field] = value
                with self.assertRaises(ValueError): self.check()
                self.delivery = original

    def test_duplicate_input_rejected(self):
        self.responses.append(copy.deepcopy(self.responses[1]))
        with self.assertRaises(ValueError): self.check()

    def test_unknown_ack_rejected(self):
        self.responses[1]['reply']['id'] = 'other'
        with self.assertRaises(ValueError): self.check()

    def test_slow_chain_not_performance_pass(self):
        self.records[3]['timestamp'] = '1970-01-01T00:00:03.100Z'
        self.assertFalse(self.check()['performance_passed'])
