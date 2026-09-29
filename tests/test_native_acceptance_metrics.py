import copy
import unittest

from tools.native_acceptance_metrics import evaluate_native_timings


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
