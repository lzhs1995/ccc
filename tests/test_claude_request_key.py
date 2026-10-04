from contextlib import ExitStack
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import ccc_claude_request_key as reader


class ClaudeRequestKeyTests(unittest.TestCase):
    def setUp(self):
        contexts = ExitStack()
        self.addCleanup(contexts.close)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'claude-123-request.json'
        self.result = SimpleNamespace(ok=True, agent_kind='claude', pid=123,
                                      session_id='12345678-1234-1234-1234-123456789012')
        self.birth = (int(time.time()) - 100, 0)
        self.args = (['/test/claude'], {'CCC_CLAUDE_REQUEST_OBSERVATIONS_DIR': str(self.root)})
        self.data = dict(schema=1, purpose='claude_request_attempt', pid=123,
                         session_id=self.result.session_id, sequence=1,
                         observer_epoch='12345678-1234-1234-1234-123456789013',
                         observed_at_ms=int(time.time() * 1000),
                         credential_scope='first_hop_headers', authorization=None,
                         api_key='fake-pinned')
        self.write()
        self.birth_mock = contexts.enter_context(patch.object(reader.scope, 'birth', return_value=self.birth))
        self.args_mock = contexts.enter_context(patch.object(reader.scope, 'arguments', return_value=self.args))

    def write(self):
        self.path.write_text(json.dumps(self.data))
        self.path.chmod(0o600)

    def test_actual_header_and_replacement(self):
        reader.observe(self.result)
        self.assertEqual(self.result.api_key_observed, 'fake-pinned')
        self.data.update(api_key='fake-new', sequence=2)
        self.write()
        reader.observe(self.result)
        self.assertEqual(self.result.api_key_observed, 'fake-new')

    def test_both_headers_remain_distinguishable(self):
        self.data['authorization'] = 'Bearer fake-token'
        self.write()
        reader.observe(self.result)
        self.assertEqual(self.result.api_key_observed, 'Bearer fake-token; x-api-key fake-pinned')

    def test_bad_binding_clears_old_value(self):
        for key, value in [('pid', 124), ('session_id', None), ('sequence', 0),
                           ('observer_epoch', 'bad'), ('observed_at_ms', 0),
                           ('credential_scope', 'opaque_redirect'), ('api_key', None)]:
            with self.subTest(key=key):
                old = self.data[key]
                self.data[key] = value
                self.write()
                self.result.api_key_observed = 'stale'
                reader.observe(self.result)
                self.assertEqual(self.result.api_key_observed, '')
                self.data[key] = old

    def test_pid_reuse_during_read(self):
        self.birth_mock.side_effect = [self.birth, (self.birth[0] + 1, 0)]
        reader.observe(self.result)
        self.assertEqual(self.result.api_key_observed, '')

    def test_environment_change_during_read(self):
        self.args_mock.side_effect = [self.args, (self.args[0], {})]
        reader.observe(self.result)
        self.assertEqual(self.result.api_key_observed, '')

    def test_process_arguments_failure_clears_old_value(self):
        self.result.api_key_observed = 'stale'
        self.args_mock.side_effect = RuntimeError('process exited')
        reader.observe(self.result)
        self.assertEqual(self.result.api_key_observed, '')

    def test_private_files_required(self):
        for mode in [0o644, 0o666]:
            self.path.chmod(mode)
            reader.observe(self.result)
            self.assertEqual(self.result.api_key_observed, '')

    def test_symlink_rejected(self):
        actual = self.root / 'actual'
        self.path.rename(actual)
        self.path.symlink_to(actual)
        reader.observe(self.result)
        self.assertEqual(self.result.api_key_observed, '')

    def test_record_replacement_during_read(self):
        def arguments(pid):
            replacement = self.root / 'replacement'
            replacement.write_text(self.path.read_text())
            replacement.chmod(0o600)
            os.replace(replacement, self.path)
            return self.args
        self.args_mock.side_effect = [self.args]
        calls = 0
        def changing(pid):
            nonlocal calls
            calls += 1
            return self.args if calls == 1 else arguments(pid)
        self.args_mock.side_effect = changing
        reader.observe(self.result)
        self.assertEqual(self.result.api_key_observed, '')

    def test_opt_in_required_no_config_fallback(self):
        self.args_mock.return_value = (['/test/claude'], {'ANTHROPIC_API_KEY': 'fake-global'})
        reader.observe(self.result)
        self.assertEqual(self.result.api_key_observed, '')
        self.assertEqual(self.result.api_key_observation_status, 'not_instrumented')
        self.assertIn('继续请求不会自动补出', self.result.api_key_observation_note)


if __name__ == '__main__':
    unittest.main()
