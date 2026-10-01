import copy
import unittest

from tools.native_acceptance_metrics import evaluate_native_timings, evaluate_native_completion


class NativeCompletionTests(unittest.TestCase):
    def event(self, kind, turn=None, **extra):
        return {'type': 'event_msg', 'payload': {'type': kind, 'turn_id': turn, **extra}}

    def setUp(self):
        self.rows = [
            {'type': 'session_meta', 'payload': {'id': 'original'}},
            self.event('task_started', 'failed'),
            self.event('task_complete', 'failed', error={'message': 'rate limit exceeded'}),
            self.event('user_message', message='continue'),
            self.event('task_started', 'final'),
            self.event('task_complete', 'final', last_agent_message='OK')]

    def check(self, rows=None, rounds=1):
        return evaluate_native_completion(self.rows if rows is None else rows, 'original', rounds)

    def test_complete_failed_then_success_chain(self):
        self.assertTrue(self.check()['passed'])

    def test_success_followed_by_new_work_or_input_is_not_terminal(self):
        for tail in [self.event('task_started', 'later'), self.event('user_message', message='more')]:
            with self.subTest(tail=tail):
                self.assertIn('unfinished_tail', self.check(self.rows + [tail])['errors'])

    def test_wrong_turn_and_duplicate_completion_rejected(self):
        rows = copy.deepcopy(self.rows)
        rows[-1]['payload']['turn_id'] = 'other'
        self.assertIn('unmatched_completion', self.check(rows)['errors'])
        self.assertFalse(self.check(self.rows + [self.rows[-1]])['passed'])

    def test_missing_start_or_overlapping_start_rejected(self):
        self.assertFalse(self.check(self.rows[:4] + self.rows[5:])['passed'])
        self.assertFalse(self.check(self.rows[:2] + [self.event('task_started', 'other')] + self.rows[2:])['passed'])

    def test_original_session_and_expected_rounds_required(self):
        rows = copy.deepcopy(self.rows)
        rows[0]['payload']['id'] = 'replacement'
        self.assertFalse(self.check(rows)['passed'])
        self.assertFalse(self.check(rounds=2)['passed'])
        self.assertFalse(self.check(rounds=True)['passed'])

    def test_abort_or_early_success_rejected(self):
        self.assertFalse(self.check(self.rows[:2] + [self.event('turn_aborted', 'failed')] + self.rows[2:])['passed'])
        rows = copy.deepcopy(self.rows)
        del rows[2]['payload']['error']
        self.assertFalse(self.check(rows)['passed'])

    def test_final_error_and_malformed_records_rejected(self):
        rows = copy.deepcopy(self.rows)
        rows[-1]['payload']['error'] = {'message': 'failed'}
        self.assertFalse(self.check(rows)['passed'])
        for row in [None, {}, {'type': 'event_msg', 'payload': []}]:
            self.assertFalse(self.check(self.rows + [row])['passed'])

    def test_repeated_identity_or_turn_rejected(self):
        self.assertFalse(self.check(self.rows + [self.rows[0]])['passed'])
        rows = copy.deepcopy(self.rows)
        rows[-2]['payload']['turn_id'] = 'failed'
        rows[-1]['payload']['turn_id'] = 'failed'
        self.assertFalse(self.check(rows)['passed'])


class NativeAcceptanceMetricsTests(unittest.TestCase):
    def setUp(self):
        self.first = [{'surface_id': 's1', 'session_id': 'n1', 'task_at': 100.8},
                      {'surface_id': 's2', 'session_id': 'n2', 'task_at': 101.0}]
        self.rows = [{'surface_id': s, 'session_id': n, 'failed_turn': 'old', 'next_turn': 'new',
                      'input_count': 1, 'forward_ms': 600, 'ack_ms': 650, 'native_next_ms': 700}
                     for s, n in [('s1', 'n1'), ('s2', 'n2')]]

    def evaluate(self):
        return evaluate_native_timings(100, self.first, self.rows, 2, 1)

    def test_complete_in_budget(self):
        self.assertTrue(self.evaluate()['local_performance_passed'])

    def test_fast_ack_cannot_hide_slow_native_or_startup(self):
        self.rows[0]['native_next_ms'] = 1000
        self.assertFalse(self.evaluate()['continuation_passed'])
        self.rows[0]['native_next_ms'] = 700
        self.first[0]['task_at'] = 117
        result = self.evaluate()
        self.assertTrue(result['continuation_passed'])
        self.assertFalse(result['local_performance_passed'])

    def test_missing_duplicate_or_wrong_identity_fails(self):
        valid = copy.deepcopy(self.rows)
        for rows in [valid[:1], [valid[0], valid[0]], valid + [valid[0]]]:
            self.rows = rows
            self.assertFalse(self.evaluate()['continuation_passed'])
        self.rows = valid
        self.first[1]['session_id'] = 'n1'
        self.assertFalse(self.evaluate()['startup_complete'])

    def test_unknown_negative_and_nonfinite_timings_fail(self):
        for bad in [None, -1, float('nan'), float('inf'), True]:
            with self.subTest(bad=bad):
                self.rows[0]['native_next_ms'] = bad
                self.assertFalse(self.evaluate()['continuation_passed'])

    def test_failed_turn_needs_one_send_and_new_turn(self):
        self.rows[0]['input_count'] = 2
        self.assertFalse(self.evaluate()['continuation_passed'])
        self.rows[0]['input_count'] = 1
        self.rows[0]['next_turn'] = 'old'
        self.assertFalse(self.evaluate()['continuation_passed'])

    def test_invalid_or_missing_start_cannot_pass(self):
        for start in [None, True, float('nan')]:
            self.assertFalse(evaluate_native_timings(start, self.first, self.rows, 2, 1)['startup_passed'])

    def test_one_next_turn_or_input_cannot_complete_two_failures(self):
        rows = [self.rows[0], {**self.rows[0], 'failed_turn': 'old2'}]
        self.assertFalse(evaluate_native_timings(100, self.first[:1], rows, 1, 2)['continuation_passed'])
        rows = [{**r, 'input_id': 'same'} for r in self.rows]
        self.assertFalse(evaluate_native_timings(100, self.first, rows, 2, 1)['continuation_passed'])


if __name__ == '__main__':
    unittest.main()
