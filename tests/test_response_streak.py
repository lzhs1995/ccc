"""Count complete responses from one live native session, not chunks or peers."""
import unittest
from unittest.mock import patch

import ccc_batch_guard as guard
from tests.test_batch_guard import complete, delta


class ResponseStreakTests(unittest.TestCase):
    def setUp(self):
        toggle = patch.object(guard, 'CONNECTION_CUT_ENABLED', True)
        toggle.start()
        self.addCleanup(toggle.stop)
        self.counter = guard.ResponseStreak()
        self.identity = ('workspace', 'surface', 'session', 123, 1.25, (1, 250000), 'endpoint')

    def observe(self, event):
        return self.counter.observe(event, self.identity)

    def start(self, tid):
        return self.observe({'method': 'turn/started', 'params': {'threadId': 'session', 'turn': {'id': tid}}})

    def answer(self, tid, **kwargs):
        self.start(tid)
        return self.observe(complete(tid, **kwargs))

    def test_three_complete_successes_only(self):
        self.assertIsNone(self.answer('one'))
        self.assertIsNone(self.answer('two'))
        self.start('three')
        for _ in range(4):
            self.assertIsNone(self.observe(delta(turn='three')))
        result = self.observe(complete('three'))
        self.assertEqual(result['consecutive_responses'], 3)
        self.assertEqual(result['turn_ids'], ['one', 'two', 'three'])

    def test_failed_interrupted_empty_and_reasoning_turns_reset_streak(self):
        for outcome in ({'status': 'failed'}, {'status': 'interrupted'}, {'text': ' '},
                        {'error': {'message': 'high demand'}},
                        {'items': [{'id': 'thought', 'type': 'reasoning', 'text': 'thinking'}]},
                        {'items': [{'id': 'note', 'type': 'agentMessage', 'phase': 'commentary', 'text': 'working'}]}):
            with self.subTest(outcome=outcome):
                self.counter = guard.ResponseStreak()
                self.answer('one')
                self.answer('two')
                self.assertIsNone(self.answer('bad', **outcome))
                self.assertIsNone(self.answer('four'))
                self.assertIsNone(self.answer('five'))
                self.assertIsNotNone(self.answer('six'))

    def test_raw_items_history_and_duplicates_never_add_a_response(self):
        for tid in ('one', 'two', 'three'):
            self.assertIsNone(self.observe(complete(tid)))  # No live start.
        self.answer('one')
        for _ in range(5):
            self.assertIsNone(self.answer('one'))  # A replayed start and complete.
        self.assertEqual(self.counter.successes, ['one'])
        self.start('two')
        for method in ('rawResponseItem/completed', 'item/started'):
            self.assertIsNone(self.observe({'method': method, 'params': {
                'threadId': 'session', 'turnId': 'two', 'item': {
                    'type': 'function_call', 'id': 'tool', 'text': 'OK'}}}))
        self.assertIsNone(self.observe(complete('two')))

    def test_native_item_completion_still_waits_for_successful_turn_end(self):
        self.answer('one')
        self.answer('two')
        self.start('three')
        event = {'method': 'item/completed', 'params': {'threadId': 'session', 'turnId': 'three',
            'item': {'id': 'answer', 'type': 'agentMessage', 'text': 'final', 'phase': 'final_answer'}}}
        self.assertIsNone(self.observe(event))
        self.assertIsNone(self.observe(event))
        result = self.observe(complete('three', items=[]))
        self.assertEqual(result['consecutive_responses'], 3)

    def test_error_invalidates_pending_success_and_no_same_turn_recount(self):
        for tid in ('one', 'two', 'three'):
            self.answer(tid)
        self.assertIsNotNone(self.counter.qualified)
        self.start('four')
        self.observe({'method': 'error', 'params': {'threadId': 'session', 'turnId': 'four'}})
        self.assertIsNone(self.counter.qualified)
        self.assertIsNone(self.observe(complete('four')))
        self.assertEqual(self.counter.successes, [])

    def test_every_identity_component_resets_count(self):
        for index in range(len(self.identity)):
            with self.subTest(identity_component=index):
                self.counter = guard.ResponseStreak()
                self.answer('one')
                self.answer('two')
                moved = list(self.identity)
                moved[index] = ('different',) if index == 5 else 'different'
                self.counter.observe({'method': 'noop', 'params': {}}, tuple(moved))
                self.assertEqual(self.counter.successes, [])
                self.assertIsNone(self.counter.qualified)

    def test_overlapping_turn_and_start_failure_reset_count(self):
        self.answer('one')
        self.answer('two')
        self.start('unfinished')
        self.start('replacement')
        self.assertEqual(self.counter.successes, [])
        self.observe(complete('replacement'))
        self.counter.reset_failure()
        self.assertIsNone(self.answer('next'))


if __name__ == '__main__':
    unittest.main()
