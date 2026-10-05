import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from tools import claude_observation_resume as resume
from tools import prepare_claude_observation_resume as capture
from tests import test_claude_observation_relaunch as relaunch


class ClaudeResumeMergeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = relaunch.ClaudeObservationRelaunchTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_bool_number_hook_matches_real_cmux_merger(self):
        # Execute the installed wrapper's actual merger, not a second mock.
        source = relaunch.WRAPPER.read_text()
        anchor = source.index('CMUX_BASE_SETTINGS_PATH="$CMUX_SETTINGS_BASE_PATH"')
        start = source.index("node -e '\n", anchor) + len("node -e '\n")
        program = source[start:source.index("\n'", start)]
        self.assertIn('const stripManaged=', program)
        base = {'hooks': {'Stop': [{'enabled': True}]},
                '__cmux': {'managed': 'claude-hooks', 'version': 1}}
        original = {'hooks': {'Stop': [{'enabled': True}, {'enabled': 1},
                    {'hooks': [{'type': 'command', 'command': 'user-hook'}]}]}}
        root = self.fixture.root
        (root / 'base.json').write_text(json.dumps(base))
        (root / 'inputs').write_bytes(json.dumps(original).encode() + b'\0')
        env = dict(os.environ, CMUX_BASE_SETTINGS_PATH=str(root / 'base.json'),
                   CMUX_USER_SETTINGS_PATH=str(root / 'inputs'),
                   CMUX_MERGED_SETTINGS_PATH=str(root / 'merged.json'))
        subprocess.run([shutil.which('node'), '-e', program], env=env, check=True,
                       capture_output=True, text=True, timeout=10)
        actual = json.loads((root / 'merged.json').read_text())
        expected = resume.merge_settings(base, [original])
        self.assertEqual(json.dumps(expected, sort_keys=True), json.dumps(actual, sort_keys=True))
        self.assertEqual(len(expected['hooks']['Stop']), 3)
        self.assertIs(type(expected['hooks']['Stop'][1]['enabled']), int)

    def test_proto_rejected_before_creating_plan_or_claim(self):
        f = self.fixture
        for index, settings in enumerate(({'__proto__': {}},
                {'hooks': {'Stop': [{'__proto__': {'enabled': True}}]}})):
            with self.subTest(index=index):
                f.process['argv'] = ['Claude', '--resume', f.sid, '--settings=' + json.dumps(settings)]
                with self.assertRaisesRegex(ValueError, 'cannot preserve __proto__'):
                    f.prepare('invalid-' + str(index))
                self.assertFalse((f.root / ('invalid-' + str(index))).exists())
                self.assertFalse((f.root / 'observations').exists())
        # String contents are ordinary user data, not object keys.
        f.process['argv'] = ['Claude', '--resume', f.sid, '--settings={"description":"__proto__"}']
        self.assertEqual(f.prepare()['state'], 'prepared_not_launched')

    def test_final_settings_boolean_number_drift_blocks_native(self):
        f = self.fixture
        original = {'hooks': {'Stop': [{'hooks': [{'type': 'command', 'command': 'user-hook', 'async': True}]}]}}
        f.process['argv'] += ['--settings=' + json.dumps(original)]
        plan = f.prepare(); env = f.claim(plan)
        changed = copy.deepcopy(original)
        changed['hooks']['Stop'][0]['hooks'][0]['async'] = 1
        args = [str(f.native), '--resume', f.sid, '--settings=' + json.dumps(changed)]
        with self.assertRaisesRegex(ValueError, 'original settings'):
            resume.exec_native(plan, args, lambda _: None, environment=env,
                               execute=lambda *a: self.fail('drifted native exec'))
        self.assertFalse(Path(plan['claim_path'] + '.native').exists())
        calls = []
        resume.exec_native(plan, [str(f.native), *plan['argv'][1:]], lambda _: None,
                           environment=env, execute=lambda *a: calls.append(a))
        self.assertEqual(len(calls), 1)

    def test_final_user_mcp_boolean_number_drift_blocks_native(self):
        f = self.fixture
        mcp = {'mcpServers': {'user': {'command': '/user/server', 'enabled': True}}}
        f.process['argv'] += ['--mcp-config=' + json.dumps(mcp)]
        plan = f.prepare(); env = f.claim(plan)
        mcp['mcpServers']['user']['enabled'] = 1
        args = [str(f.native), '--resume', f.sid, '--mcp-config=' + json.dumps(mcp)]
        with self.assertRaisesRegex(ValueError, 'original user MCP'):
            resume.exec_native(plan, args, lambda _: None, environment=env,
                               execute=lambda *a: self.fail('drifted native exec'))
        self.assertFalse(Path(plan['claim_path'] + '.native').exists())

    def test_numeric_json_roundtrip_remains_valid(self):
        f = self.fixture
        f.process['argv'] += ['--settings={"timeout":1.0,"enabled":true}']
        plan = f.prepare(); env = f.claim(plan)
        calls = []
        resume.exec_native(plan, [str(f.native), '--resume', f.sid,
            '--settings={"enabled":true,"timeout":1}'], lambda _: None,
            environment=env, execute=lambda *a: calls.append(a))
        self.assertEqual(len(calls), 1)


if __name__ == '__main__':
    unittest.main()
