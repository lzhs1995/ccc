"""Private original invocations and plans; no native or service actions."""
import json
import unittest
import uuid
from unittest.mock import patch

from tests import test_standby_runner as runner_fixtures
from tools import standby_run_manifest as subject


class AttemptTests(unittest.TestCase):
    def setUp(self):
        runner_fixtures.RunnerTests.setUp(self)
        self.plan_directory = self.root / 'run'
        self.plan_directory.mkdir(mode=0o700)
        self.batch_id = str(uuid.uuid4())
        self.boot = str(uuid.uuid4())
        self.batches = [dict(batch_id=self.batch_id, workspace_id=self.value['workspace_id'],
                             mode='b', slots=50)]

    def clock(self, n):
        return dict(wall=n, monotonic=n, boot_id=self.boot)

    def declare(self, rows=None):
        subject.declare(self.plan_directory, rows or self.batches, clock=lambda: self.clock(1))
        return subject.RunManifest(self.plan_directory)

    def register(self, reader):
        return reader.register_attempt(self.batch_id, self.path, self.sha, self.output,
                                       clock=lambda: self.clock(2))

    def test_prestart_binding_retained_without_activation(self):
        reader = self.declare()
        registered = self.register(reader)
        self.assertEqual(registered['invocation_id'], self.value['invocation_id'])
        self.assertNotIn('action_id', registered)
        rows = subject.RunManifest(self.plan_directory).attempts()
        self.assertEqual(rows[0]['attempt'], registered)
        self.assertEqual(self.factory.call_count, 0)

    def test_reuse_does_not_retimestamp_or_rewrite(self):
        reader = self.declare()
        original = self.register(reader)
        (self.output / 'runner-intent.json').write_text('{}')
        with patch.object(subject, 'write_once', side_effect=AssertionError('rewrite')):
            self.assertEqual(self.register(reader), original)

    def test_late_registration_rejected(self):
        reader = self.declare()
        (self.output / 'runner-intent.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'before runner'):
            self.register(reader)
        self.assertFalse(list(self.plan_directory.glob('attempt-*.json')))

    def test_missing_declared_attempt_cannot_shrink_run(self):
        rows = self.batches + [dict(self.batches[0], batch_id=str(uuid.uuid4()))]
        reader = self.declare(rows)
        self.register(reader)
        with self.assertRaisesRegex(ValueError, 'complete declared'):
            reader.attempts()

    def test_same_invocation_cannot_fill_two_batches(self):
        second = str(uuid.uuid4())
        reader = self.declare(self.batches + [dict(self.batches[0], batch_id=second)])
        self.register(reader)
        other = self.root / 'other'; other.mkdir(mode=0o700)
        with self.assertRaisesRegex(ValueError, 'already consumed'):
            reader.register_attempt(second, self.path, self.sha, other, clock=lambda: self.clock(2))

    def test_invocation_drift_rejected_after_restart(self):
        reader = self.declare()
        self.register(reader)
        self.path.write_text('{}')
        with self.assertRaisesRegex(ValueError, 'hash mismatch'):
            subject.RunManifest(self.plan_directory).attempts()

    def test_runner_directory_replacement_rejected(self):
        reader = self.declare()
        self.register(reader)
        self.output.rename(self.root / 'old-output')
        self.output.mkdir(mode=0o700)
        with self.assertRaisesRegex(ValueError, 'identity changed'):
            reader.attempts()

    def test_wrong_workspace_rejected(self):
        reader = self.declare([dict(self.batches[0], workspace_id=str(uuid.uuid4()))])
        with self.assertRaisesRegex(ValueError, 'differs from declared'):
            self.register(reader)

    def test_existing_binding_drift_rejected(self):
        reader = self.declare()
        self.register(reader)
        path = self.plan_directory / f'attempt-{self.batch_id}.json'
        row = json.loads(path.read_text()); row['slots'] = 1
        path.write_text(json.dumps(row))
        with self.assertRaisesRegex(ValueError, 'differs from declared'):
            reader.attempts()


if __name__ == '__main__':
    unittest.main()
