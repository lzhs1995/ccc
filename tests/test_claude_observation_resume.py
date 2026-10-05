import base64
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest

from tools import prepare_claude_observation_resume as resume


class ClaudeObservationResumeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='ccc-resume-context-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.sid = '473cd1d0-f07b-414d-b5b7-060103069e28'
        self.surface = 'FC5DB919-47A7-4A8F-9A46-1182958CE532'
        self.process = dict(pid=8123, birth=[100, 22],
            argv=['Claude', '--resume', self.sid],
            environment={'HOME': str(self.root), 'CMUX_SURFACE_ID': self.surface,
                         'ANTHROPIC_API_KEY': 'fake-original', 'CCP_PROFILE_DIR': '/profiles'},
            cwd=str(self.root), executable='/fixed/claude.exe', executable_identity=[1, 2])
        self.expected = dict(pid=8123, birth=[100, 22], surface_id=self.surface, session_id=self.sid)

    def capture(self):
        return resume.capture(self.expected, lambda pid: copy.deepcopy(self.process))

    def test_plain_and_pinned_config_keep_original_environment_without_global_fallback(self):
        for config in (None, '/profiles/independent/claude'):
            with self.subTest(config=config):
                if config:
                    self.process['environment']['CLAUDE_CONFIG_DIR'] = config
                data = self.capture()
                self.assertEqual(data['original_process'], self.process)
                self.assertEqual(data['inputs']['config_root'], config or str(self.root / '.claude'))
                self.assertFalse(data['inputs']['profile_inferred_from_root'])
                self.assertIsNone(data['observed_api_key'])
                self.assertFalse(data['executable_plan'])

    def test_actual_settings_survive_compact_metadata_omission(self):
        settings = dict(env={'ANTHROPIC_API_KEY': 'fake-pinned'},
            permissions={'allow': ['Read'], 'deny': ['Bash(rm *)']},
            hooks={'Stop': [{'hooks': [{'type': 'command', 'command': 'user-hook'}]}]},
            __cmux={'managed': 'claude-hooks'})
        path = self.root / 'settings with spaces.json'
        path.write_text(json.dumps(settings)); path.chmod(0o600)
        compact = b'/old/claude\0--resume\0' + self.sid.encode() + b'\0'
        self.process['environment']['CMUX_AGENT_LAUNCH_ARGV_B64'] = base64.b64encode(compact).decode()
        self.process['argv'][1:1] = ['--settings', path.name]
        data = self.capture()
        self.assertTrue(data['inputs']['compact_omits_actual_settings'])
        self.assertFalse(data['inputs']['compact_argv_is_authoritative'])
        self.assertEqual(json.loads(data['inputs']['settings'][0]['raw_utf8']), settings)

    def test_mcp_retains_user_servers_and_requires_regeneration_of_old_owner_binding(self):
        managed = dict(command='/cmux/cua', args=['mcp', '--socket', '/tmp/original.sock'], env={
            'CMUX_CUA_STATE_OWNER_PID': '8123', 'CMUX_CUA_SOCKET_AUTH_TOKEN': 'fake-token',
            'CMUX_CUA_DEFAULT_SESSION': 'cmux-' + self.surface,
            'CMUX_CUA_MCP_FORCE_PROXY': '1', 'CMUX_CUA_EXTERNAL_PERMISSION_FLOW': '1'})
        for owner, exact in (('8123', True), ('9999', False)):
            with self.subTest(owner=owner):
                managed['env']['CMUX_CUA_STATE_OWNER_PID'] = owner
                mcp = {'mcpServers': {'cmux-cua': managed, 'user-mcp': {'command': '/my/mcp'}}}
                self.process['argv'] = ['Claude', '--mcp-config=' + json.dumps(mcp), '--resume', self.sid]
                data = self.capture()
                self.assertEqual(json.loads(data['inputs']['mcp'][0]['raw_utf8']), mcp)
                self.assertEqual(data['inputs']['process_bound_mcp'][0]['matches_original_owner'], exact)
                self.assertIn('regenerate_process_bound_mcp', data['launch_blockers'])
                self.assertFalse(data['executable_plan'])

    def test_process_reuse_surface_session_and_late_environment_changes_rejected(self):
        mutations = [lambda p: p.update(birth=[100, 23]),
            lambda p: p['environment'].update(CMUX_SURFACE_ID='f87f75e8-600d-4f29-ab31-5936d3dd105d'),
            lambda p: p.update(argv=['Claude', '--resume', 'f87f75e8-600d-4f29-ab31-5936d3dd105d']),
            lambda p: p['environment'].update(ANTHROPIC_API_KEY='fake-other')]
        for mutate in mutations:
            changed = copy.deepcopy(self.process); mutate(changed)
            reads = iter([copy.deepcopy(self.process), changed])
            with self.assertRaises(ValueError):
                resume.capture(self.expected, lambda pid: next(reads))
        for flags in (['--continue'], ['--resume', self.sid, '--fork-session']):
            self.process['argv'] = ['Claude', *flags]
            with self.assertRaises(ValueError):
                self.capture()

    def test_replaced_settings_and_ambiguous_or_missing_inputs_rejected(self):
        from unittest.mock import patch
        path = self.root / 'settings.json'
        path.write_text('{"permissions":{"deny":["Bash"]}}'); path.chmod(0o600)
        self.process['argv'][1:1] = ['--settings', str(path)]
        original = resume.read_file
        calls = []
        def drift(p):
            row = original(p)
            calls.append(row)
            if len(calls) == 1:
                path.write_text('{"permissions":{"allow":["Bash"]}}')
            return row
        with patch.object(resume, 'read_file', side_effect=drift), self.assertRaises(ValueError):
            self.capture()
        path.unlink()
        with self.assertRaises(OSError):
            self.capture()
        self.process['argv'] = ['Claude', '--mcp-config', '--resume', self.sid]
        with self.assertRaises(ValueError):
            self.capture()

    def test_private_capture_is_exclusive_and_does_not_offer_a_launch_for_busy_or_unknown_ui(self):
        data = self.capture()
        self.assertIn('fresh_live_surface_and_input_state_required', data['launch_blockers'])
        self.assertEqual(data['terminal_inputs'], 0)
        self.assertEqual(data['model_requests'], 0)
        output = self.root / 'capture.json'
        resume.write_private(output, data)
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(FileExistsError):
            resume.write_private(output, data)
        self.root.chmod(0o755)
        with self.assertRaises(ValueError):
            resume.write_private(self.root / 'public.json', data)
        self.root.chmod(0o700)


if __name__ == '__main__':
    unittest.main()
