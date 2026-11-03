import logging
import unittest
from unittest.mock import patch

import ccc_jev_unknown as jev


def response(choice, confidence=1.0):
    return {'model': jev.MODEL, 'answers': {'diagnosis':
            {'type': 'choice', 'choice': choice, 'confidence': confidence}}}


class DecisionObservationTests(unittest.TestCase):
    def classifier(self, reply):
        def transport(*_):
            if isinstance(reply, Exception):
                raise reply
            return reply
        return jev.JevUnknownClassifier(transport, start=False, is_enabled=lambda: True)

    def enqueue(self, classifier):
        return classifier.resolve(None, jev.Gates(True, True, True), 'claude', '',
                                  'private draft/API key must never enter diagnostic logs')

    def test_one_decision_log_per_actual_request_not_per_cache_lookup(self):
        c = self.classifier(response('RATE_LIMIT'))
        self.assertIsNone(self.enqueue(c))
        with self.assertLogs('ccc-jev', logging.INFO) as logs:
            c.run_once()
            for _ in range(5):
                self.assertEqual(self.enqueue(c), 'rate_limit')
        self.assertEqual(c.calls, 1)
        self.assertEqual(len(logs.output), 1)
        self.assertIn('diagnosis=RATE_LIMIT', logs.output[0])
        self.assertIn('outcome=retry_diagnosis', logs.output[0])
        self.assertNotIn('private draft/API key', logs.output[0])

    def test_quota_diagnosis_is_visible_without_authorizing_retry(self):
        c = self.classifier(response('QUOTA'))
        self.enqueue(c)
        with self.assertLogs('ccc-jev', logging.INFO) as logs:
            c.run_once()
        self.assertIsNone(self.enqueue(c))
        self.assertIn('diagnosis=QUOTA', logs.output[0])
        self.assertIn('outcome=abstained', logs.output[0])
        self.assertIn('retry_code=None', logs.output[0])

    def test_invalid_answer_and_transport_failure_retain_unknown_and_omit_raw_text(self):
        for reply, outcome in [(response(['invalid']), 'invalid_response'),
                               (response('RATE_LIMIT', .4), 'invalid_response'),
                               (TimeoutError('secret exception text'), 'transport_error')]:
            with self.subTest(outcome=outcome):
                c = self.classifier(reply)
                self.enqueue(c)
                with self.assertLogs('ccc-jev', logging.INFO) as logs:
                    c.run_once()
                self.assertIsNone(self.enqueue(c))
                self.assertIn('outcome=' + outcome, logs.output[0])
                self.assertNotIn('secret exception text', logs.output[0])
                self.assertNotIn('private draft/API key', logs.output[0])

    def test_logging_failure_preserves_classification_and_worker_progress(self):
        c = self.classifier(response('UPSTREAM'))
        self.enqueue(c)
        with patch.object(logging.getLogger('ccc-jev'), 'info', side_effect=OSError('log unavailable')):
            self.assertTrue(c.run_once())
        self.assertEqual(self.enqueue(c), 'http_503')
        self.assertEqual(c.calls, 1)


if __name__ == '__main__':
    unittest.main()
