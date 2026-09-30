import copy
import json
import os
from pathlib import Path
import select
import tempfile
import unittest
from unittest.mock import patch

from ccc_standby_sources import NativeFileSources


class NativeSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / 'home'; self.home.mkdir()
        self.native = self.home / '.codex'; self.native.mkdir()
        self.cwd = self.root / 'workspace'; self.cwd.mkdir()
        self.system = self.root / 'system'; self.system.mkdir()
        self.runtime = self.root / 'runtime.py'; self.runtime.write_text('# runtime')
        self.binary = self.root / 'codex'; self.binary.write_text('native binary')
        self.config = self.native / 'config.toml'; self.config.write_text('')
        self.env = {'HOME': str(self.home), 'CODEX_HOME': str(self.native),
                    'TOKIO_WORKER_THREADS': '2', 'OPENAI_API_KEY': 'fixture-secret'}
        self.argv = [str(self.binary), '--cd', str(self.cwd), '--disable', 'skill_search']

    def source(self, **extra):
        return NativeFileSources(argv=self.argv, environment=self.env, cwd=self.cwd,
            runtime_files=[self.runtime], system_dir=self.system, **extra)

    def pin(self, **extra):
        pin = self.source().capture_files(lambda: {'fixture': 'no dynamic sources'}, **extra)
        self.addCleanup(pin.close)
        return pin

    def test_local_union_preserves_missing_and_untrusted_inputs_without_writes(self):
        before = sorted(str(p) for p in self.root.rglob('*'))
        inventory = self.source().discover()
        self.assertIn(str(self.cwd / '.codex' / 'config.toml'), inventory.documents)
        self.assertIsNone(inventory.documents[str(self.cwd / '.codex' / 'config.toml')])
        self.assertIn(str(self.home / '.agents' / 'skills'), inventory.roots['skills'])
        self.assertIn(str(self.system / 'requirements.toml'), inventory.roots['codex_config'])
        self.assertIn(str(self.native / 'plugins' / 'cache'), inventory.roots['skills'])
        self.assertEqual(before, sorted(str(p) for p in self.root.rglob('*')))
        self.assertNotIn('fixture-secret', repr(inventory))

    def test_typed_relative_dependencies_and_nested_agent_config(self):
        (self.native / 'agent.toml').write_text('model_instructions_file="role.md"')
        self.config.write_text('''model_instructions_file="instructions.md"
[agents.worker]
config_file="agent.toml"
[skills]
extra_roots=["extra"]
[[skills.config]]
path="local/SKILL.md"
enabled=false
[marketplaces.local]
source_type="local"
source="market"
''')
        inventory = self.source().discover()
        for name in ('instructions.md', 'agent.toml', 'role.md'):
            self.assertIn(str(self.native / name), inventory.roots['codex_config'])
        for name in ('extra', 'local/SKILL.md', 'market'):
            self.assertIn(str(self.native / name), inventory.roots['skills'])

    def test_profiles_and_cli_source_overrides_preserve_own_base(self):
        self.argv += ['--profile', 'work', '-c', 'model_instructions_file="cli.md"']
        (self.native / 'work.config.toml').write_text('model_catalog_json="models.json"')
        self.config.write_text('[profiles.legacy]\nmodel_instructions_file="legacy.md"')
        inventory = self.source().discover()
        self.assertIn(str(self.native / 'work.config.toml'), inventory.roots['profile'])
        self.assertIn(str(self.native / 'models.json'), inventory.roots['codex_config'])
        self.assertIn(str(self.native / 'legacy.md'), inventory.roots['codex_config'])
        self.assertIn(str(self.cwd / 'cli.md'), inventory.roots['codex_config'])

    def test_arbitrary_absolute_strings_and_output_locations_are_not_scanned(self):
        self.config.write_text('''sqlite_home="/private/output-db"
log_dir="/private/output-log"
[mcp_servers.local.env]
HOME="/arbitrary-home"
CACHE="/arbitrary-cache"
''')
        inventory = self.source().discover()
        all_paths = {p for values in inventory.roots.values() for p in values}
        for value in ('/private/output-db', '/private/output-log', '/arbitrary-home', '/arbitrary-cache'):
            self.assertNotIn(value, all_paths)
        for value in ('sessions', 'plugins/data', 'logs'):
            self.assertNotIn(str(self.native / value), all_paths)

    def test_config_symlink_keeps_original_name_and_relative_base(self):
        target = self.root / 'target.toml'; target.write_text('model_instructions_file="original-base.md"')
        self.config.unlink(); self.config.symlink_to(target)
        inventory = self.source().discover()
        self.assertIn(str(self.config), inventory.documents)
        self.assertIn(str(self.native / 'original-base.md'), inventory.roots['codex_config'])
        pin = self.pin()
        target.write_text('model_instructions_file="changed.md"')
        with self.assertRaises(ValueError): pin.current()

    def test_existing_dependency_new_content_invalidates(self):
        instructions = self.native / 'instructions.md'; instructions.write_text('first')
        self.config.write_text('model_instructions_file="instructions.md"')
        pin = self.pin()
        instructions.write_text('second')
        with self.assertRaises(ValueError): pin.current()

    def test_installed_git_marketplace_and_builtin_manifests_are_sources(self):
        for root in (self.native / '.tmp' / 'marketplaces' / 'installed',
                     self.native / '.tmp' / 'plugins' / '.agents' / 'plugins'):
            with self.subTest(root=root):
                root.mkdir(parents=True)
                manifest = root / 'marketplace.json'
                manifest.write_text('{}')
                pin = self.pin()
                manifest.write_text('{"plugins":[]}')
                with self.assertRaises(ValueError): pin.current()

    def test_typed_module_directories_from_profile_and_cli_are_sources(self):
        library = self.native / 'node_modules'
        library.mkdir()
        module = library / 'index.js'; module.write_text('one')
        self.config.write_text('[profiles.work]\njs_repl_node_module_dirs=["node_modules"]')
        self.argv += ['-c', 'js_repl_node_module_dirs=["cli-modules"]']
        inventory = self.source().discover()
        self.assertIn(str(library), inventory.roots['codex_config'])
        self.assertIn(str(self.cwd / 'cli-modules'), inventory.roots['codex_config'])
        pin = self.pin()
        module.write_text('two')
        with self.assertRaises(ValueError): pin.current()

    def test_runtime_iterator_is_not_consumed_by_validation(self):
        source = NativeFileSources(argv=self.argv, environment=self.env, cwd=self.cwd,
            runtime_files=iter([self.runtime]), system_dir=self.system)
        self.assertEqual(source.discover().roots['runtime'], (str(self.runtime),))

    def test_previously_missing_project_config_invalidates(self):
        pin = self.pin()
        (self.cwd / '.codex').mkdir()
        (self.cwd / '.codex' / 'config.toml').write_text('model="new"')
        with self.assertRaises(ValueError): pin.current()

    def test_config_change_during_generation_capture_cannot_omit_new_source(self):
        source = self.source()
        calls = [0]
        def dynamic():
            calls[0] += 1
            if calls[0] == 1:
                self.config.write_text('model_instructions_file="new-source.md"')
            return {'fixture': True}
        with self.assertRaisesRegex(ValueError, 'source graph changed'):
            source.capture_files(dynamic)

    def test_target_environment_is_not_ambient_or_mutable_callers(self):
        source = self.source(); original = source.discover().signature()
        self.env['TOKIO_WORKER_THREADS'] = '99'
        self.argv += ['--profile', 'later']
        with patch.dict(os.environ, {'HOME': '/unrelated-home', 'CODEX_HOME': '/unrelated-native'}):
            self.assertEqual(source.discover().signature(), original)
        self.assertNotEqual(self.source().discover().signature(), original)

    def test_dynamic_source_failure_and_change_invalidates(self):
        dynamic = {'forced_preferences': 'one'}
        pin = self.source().capture_files(lambda: copy.deepcopy(dynamic))
        self.addCleanup(pin.close)
        dynamic['forced_preferences'] = 'two'
        with self.assertRaises(ValueError): pin.current()
        dynamic['forced_preferences'] = 'one'
        with self.assertRaises(ValueError): pin.current()

    def test_config_bounds_and_fifo_fail_without_hanging(self):
        with self.assertRaises(ValueError): self.source(max_documents=1).discover()
        self.config.write_text('long=' + json.dumps('x' * 100))
        with self.assertRaises(ValueError): self.source(max_document_bytes=20).discover()
        self.config.unlink(); os.mkfifo(self.config)
        with self.assertRaisesRegex(ValueError, 'regular file'): self.source().discover()

    def test_resume_prompt_unknown_option_or_mismatched_cwd_refused(self):
        original = self.argv[:]
        for extra in (['resume'], ['task'], ['--unknown'], ['--cd', '/elsewhere'], ['-p', '../escape']):
            with self.subTest(extra=extra):
                self.argv = original + extra
                with self.assertRaises(ValueError): self.source()

    @unittest.skipUnless(hasattr(select, 'kqueue'), 'Darwin events')
    def test_activation_current_does_not_discover_or_parse_again(self):
        source = self.source(); pin = source.capture_files(lambda: {'fixture': True}, use_events=True)
        self.addCleanup(pin.close)
        with patch.object(source, 'discover', side_effect=AssertionError('full discovery in activation')):
            with patch('ccc_standby_sources.tomllib.loads', side_effect=AssertionError('parse in activation')):
                self.assertEqual(pin.current(), pin.value)


if __name__ == '__main__':
    unittest.main()
