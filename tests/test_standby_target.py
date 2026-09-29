import copy
from pathlib import Path
import tempfile
import unittest

import ccc_standby_target as target


class TargetTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='ccc-target-', dir='/tmp')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.home = self.root / 'native'; self.home.mkdir()
        self.system = self.root / 'system'; self.system.mkdir()
        self.cwd = self.root / 'cwd'; self.cwd.mkdir()
        self.env = {'HOME': str(self.root), 'CODEX_HOME': str(self.home), 'API_KEY': 'test-only'}
        self.config = self.home / 'config.toml'
        self.config.write_text('model_provider="original"\nmodel="keep-model"\n'
            '[model_providers.original]\nbase_url="https://provider.invalid/v1"\n')
        self.argv = [str(self.root / 'codex')]
        self.target = {'argv': self.argv, 'provider': 'original',
            'upstream_url': 'https://provider.invalid/v1',
            'route_urls': [f'http://127.0.0.1:43123/{"a" * 48}/{i}' for i in range(50)]}

    def select(self, argv=None):
        return target.select_provider(argv or self.argv, self.env,
                                      cwd=self.cwd, system_dir=self.system)

    def test_all_slots_preserve_argv_and_only_append_selected_endpoint(self):
        self.target['argv'] += ['--model', 'unchanged', '--sandbox', 'workspace-write',
            '-c', 'skills.extra_roots=["/original/skills"]']
        for i in range(50):
            argv = target.slot_argv(self.target, i)
            self.assertEqual(argv[:-2], self.target['argv'])
            self.assertIn(self.target['route_urls'][i], argv[-1])
        self.assertEqual(self.select(), ('original', 'https://provider.invalid/v1'))

    def test_cli_override_wins_without_editing_files(self):
        raw = self.config.read_bytes()
        argv = self.argv + ['-c', 'model_providers.original.base_url="https://cli.invalid/v1"']
        self.assertEqual(self.select(argv), ('original', 'https://cli.invalid/v1'))
        self.assertEqual(self.config.read_bytes(), raw)

    def test_native_v2_profile_file_preserved(self):
        (self.home / 'named.config.toml').write_text(
            '[model_providers.original]\nbase_url="https://profile.invalid/v1"\n')
        self.assertEqual(self.select(self.argv + ['--profile', 'named']),
                         ('original', 'https://profile.invalid/v1'))

    def test_legacy_profile_and_v2_conflict_follows_native_refusal(self):
        with self.config.open('a') as f:
            f.write('\n[profiles.named]\nmodel_provider="original"\n')
        with self.assertRaises(ValueError): self.select(self.argv + ['--profile', 'named'])

    def test_legacy_config_selected_profile_kept(self):
        self.config.write_text('profile="named"\n' + self.config.read_text() +
            '\n[profiles.named.model_providers.original]\nbase_url="https://legacy.invalid/v1"\n')
        self.assertEqual(self.select(), ('original', 'https://legacy.invalid/v1'))

    def test_project_and_managed_route_changes_are_not_silently_overridden(self):
        for path in (self.cwd / 'config.toml', self.cwd / '.codex' / 'config.toml',
                     self.system / 'managed_config.toml'):
            with self.subTest(path=path):
                path.parent.mkdir(exist_ok=True)
                path.write_text('[model_providers.original]\nbase_url="https://different.invalid/v1"\n')
                with self.assertRaises(ValueError): self.select()
                path.unlink()

    def test_system_route_is_used_when_user_does_not_override(self):
        self.config.write_text('model="unchanged"\n')
        (self.system / 'config.toml').write_text('model_provider="system"\n'
            '[model_providers.system]\nbase_url="https://system.invalid/v1"\n')
        self.assertEqual(self.select(), ('system', 'https://system.invalid/v1'))

    def test_unrelated_project_skill_settings_still_work(self):
        (self.cwd / 'config.toml').write_text('[skills]\nextra_roots=["original-skills"]\n')
        self.assertEqual(self.select(), ('original', 'https://provider.invalid/v1'))

    def test_separate_provider_endpoint_refused(self):
        with self.config.open('a') as f: f.write('websocket_base_url="wss://other.invalid"\n')
        with self.assertRaises(ValueError): self.select()

    def test_slot_route_and_prompt_validation(self):
        variants = []
        for key, value in [('argv', self.argv + ['hello']), ('upstream_url', 42),
                           ('route_urls', [{}] * 50), ('provider', 'wrong.name')]:
            variants.append({**self.target, key: value})
        swapped = copy.deepcopy(self.target)
        swapped['route_urls'][0], swapped['route_urls'][1] = swapped['route_urls'][1], swapped['route_urls'][0]
        variants.append(swapped)
        for value in variants:
            with self.subTest(value=value), self.assertRaises(ValueError): target.normalize(value)

    def test_original_upstream_proxy_is_independent_of_native_loopback_bypass(self):
        env = {**self.env, 'HTTPS_PROXY': 'http://upper.invalid:1234',
               'https_proxy': 'http://user:password@lower.invalid:4321',
               'NO_PROXY': 'unrelated.invalid', 'SSL_CERT_FILE': '/original/ca.pem'}
        native, proxy, ca = target.network_environment(env, 'https://provider.invalid/v1')
        self.assertEqual(proxy, env['https_proxy'])
        self.assertEqual(ca, env['SSL_CERT_FILE'])
        self.assertEqual(native['NO_PROXY'], 'unrelated.invalid,127.0.0.1')
        self.assertEqual(native['API_KEY'], 'test-only')
        self.assertEqual(env['NO_PROXY'], 'unrelated.invalid')

    def test_original_no_proxy_and_explicit_empty_lowercase_are_respected(self):
        for changes in ({'no_proxy': 'provider.invalid'}, {'https_proxy': ''}):
            env = {**self.env, 'HTTPS_PROXY': 'http://proxy.invalid:1234', **changes}
            _, proxy, _ = target.network_environment(env, 'https://provider.invalid/v1')
            self.assertIsNone(proxy)

    def test_unsupported_ca_directory_is_not_silently_dropped(self):
        with self.assertRaises(ValueError):
            target.network_environment({**self.env, 'SSL_CERT_DIR': '/original/cas'},
                                       'https://provider.invalid/v1')

    def test_native_ca_precedence_and_empty_fallback(self):
        for changes, expected in (
            ({'CODEX_CA_CERTIFICATE': '/native/ca.pem'}, '/native/ca.pem'),
            ({'CODEX_CA_CERTIFICATE': '/native/ca.pem', 'SSL_CERT_FILE': '/generic.pem'}, '/native/ca.pem'),
            ({'CODEX_CA_CERTIFICATE': '', 'SSL_CERT_FILE': '/generic.pem'}, '/generic.pem'),
            ({'CODEX_CA_CERTIFICATE': '', 'SSL_CERT_FILE': ''}, None),
        ):
            with self.subTest(changes=changes):
                env = {**self.env, **changes}
                native, _, ca = target.network_environment(env, 'https://provider.invalid/v1')
                self.assertEqual(ca, expected)
                for name, value in env.items():
                    self.assertEqual(native[name], value)


if __name__ == '__main__': unittest.main()
