"""Controller queue saturation is a retryable read outage, not a user pause."""
import unittest
from unittest import mock

import ccc_observation as health
import cmux_codex_watch as watch
from tests import test_cmux_observation_transport as transport


ERRORS = (
    'controller admission deadline exceeded before request',
    'local controller connection deadline exceeded before request',
)


class AdmissionObservationTests(unittest.TestCase):
    def test_initial_read_retries_without_touching_authorization_or_delivery(self):
        for error in ERRORS:
            for operation in ('read_screen', 'replay'):
                with self.subTest(error=error, operation=operation):
                    transport.ObservationTransportTests.check_retry(self, error, operation, False)

    def test_read_after_workspace_refresh_also_remains_monitored(self):
        for error in ERRORS:
            for operation in ('read_screen', 'replay'):
                with self.subTest(error=error, operation=operation):
                    transport.ObservationTransportTests.check_retry(self, error, operation, True)

    def test_identity_failure_overrides_transient_admission(self):
        for error in ERRORS:
            for identity in ('surface not found', 'workspace_not_found', 'identity mismatch', 'not a terminal'):
                wrapped = watch.CmuxError(identity)
                wrapped.__cause__ = watch.InputNotSentError(error)
                self.assertFalse(health.transient_observation_error(wrapped))

    def test_unrelated_deadline_is_not_reclassified(self):
        for error in ('provider deadline exceeded', 'authorization expired', 'input outcome unknown'):
            self.assertFalse(health.transient_observation_error(watch.CmuxError(error)))

    def test_manual_pause_and_disabled_target_are_not_resumed(self):
        for error in ERRORS:
            for enabled, origin in ((True, 'user'), (False, 'automatic_observation_error')):
                target = dict(paused=True, enabled=enabled, paused_reason=error, pause_origin=origin)
                self.assertEqual(health.pause_health(target), ('paused', 'explicitly_paused_or_disabled'))
                self.assertTrue(target['paused'])

    def test_observation_classification_does_not_retry_input(self):
        for error in ERRORS:
            client = watch.CmuxClient()
            with mock.patch.object(client, '_control_rpc', side_effect=watch.InputNotSentError(error)) as rpc:
                with self.assertRaises(watch.InputNotSentError):
                    client.send('workspace-uuid', 'surface-uuid', 'continue')
                rpc.assert_called_once()


if __name__ == '__main__':
    unittest.main()
