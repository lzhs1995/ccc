import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import manage_install as m
import cmux_bridge
import mac_harness


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='install-hook-fixture-')
        self.addCleanup(self.tmp.cleanup)
        self.active = Path(self.tmp.name) / 'active'
        self.active.mkdir()

    def test_doctor_probe_isolates_paths_environment_and_cwd_in_real_subprocess(self):
        # Exercise the actual bootstrap, not offline_test_hook's patched gates.
        script = Path(self.tmp.name) / 'probe.py'
        script.write_text('''import os
from pathlib import Path
import sys
assert sys.stdin.read() == '{}'
assert Path.home() == Path.cwd()
assert Path.cwd().name.startswith('mac-doctor-')
assert not any(key.startswith(('CMUX_', 'CODEX_', 'MULTI_AGENT_', 'PYTHON')) for key in os.environ)
assert 'HOME' not in os.environ
assert not (Path.home() / '.local/state/multi-agent-collaboration').exists()
(Path.home() / 'probe-wrote-only-here').write_text('diagnostic')
''')
        before = dict(os.environ)
        with patch.dict(os.environ, {'CMUX_WORKSPACE_ID': 'live-workspace',
                                     'CODEX_THREAD_ID': 'live-thread',
                                     'MULTI_AGENT_ROLE': 'executor',
                                     'PYTHONPATH': str(self.active)}):
            with patch.object(m, 'command', return_value=shlex.join([sys.executable, '-B', str(script)])):
                result = m.doctor_probe('cmux_workspace_guard')
        self.assertTrue(result['benignExitZero'], result)
        self.assertFalse(result['liveInvocation'])
        self.assertEqual(dict(os.environ), before)
        self.assertFalse((Path(self.tmp.name) / 'probe-wrote-only-here').exists())

    def test_doctor_preserves_real_entrypoint_failure(self):
        script = Path(self.tmp.name) / 'fail.py'
        script.write_text('raise SystemExit(2)\n')
        with patch.object(m, 'command', return_value=shlex.join([sys.executable, '-B', str(script)])):
            result = m.doctor_probe('cmux_workspace_guard')
        self.assertFalse(result['benignExitZero'])
        self.assertEqual(result['returncode'], 2)

    def test_skill_path_is_portable_and_shared(self):
        self.assertEqual(cmux_bridge.COLLABORATION_SKILL_PATH, m.ROOT / "SKILL.md")
        self.assertEqual(mac_harness.COLLABORATION_SKILL_PATH, cmux_bridge.COLLABORATION_SKILL_PATH)
        self.assertEqual(mac_harness.REQUIRED_GUARD_WIRING, m.GUARDS)

    def test_merge_is_idempotent_preserves_other_settings(self):
        old = {"custom": {"secret": "dummy"}, "hooks": {"Stop": [
            {"hooks": [{"type": "command", "command": "foreign-hook"}]}]}}
        new = m.transform(old)
        self.assertEqual(m.transform(new), new)
        self.assertEqual(old["custom"], new["custom"])
        removed = m.transform(new, True)
        self.assertEqual(removed["custom"], old["custom"])
        self.assertEqual(removed["hooks"]["Stop"], old["hooks"]["Stop"])

    def test_task_continuation_timeout_is_explicit_and_idempotent(self):
        value = m.transform({})
        hooks = [h for entry in value['hooks']['Stop'] for h in entry['hooks']
                 if 'cmux_executor_idle_guard.py' in h['command']]
        self.assertEqual(len(hooks), 1)
        self.assertEqual(hooks[0]['timeout'], 75)
        self.assertEqual(m.transform(value), value)

    def test_sandbox_install_doctor_uninstall(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            self.assertTrue(m.manage(home, "install")["ok"])
            self.assertEqual(list(home.iterdir()), [])
            m.manage(home, "install", True)
            self.assertTrue(m.manage(home, "doctor")["ok"])
            m.manage(home, "install", True)
            self.assertEqual(list(home.rglob("*.multi-agent-backup-*")), [])
            cfg = home / ".claude/settings.json"
            doc = json.loads(cfg.read_bytes())
            doc["keep"] = 123
            cfg.write_text(json.dumps(doc))
            m.manage(home, "uninstall", True)
            self.assertEqual(json.loads(cfg.read_bytes())["keep"], 123)
            self.assertFalse((home / ".claude/skills/multi-agent-collaboration").exists())
            # Two config backups plus both retired skill links are preserved.
            self.assertEqual(len(list(home.rglob("*.multi-agent-backup-*"))), 4)
            self.assertFalse(m.manage(home, "doctor")["ok"])

    def test_foreign_install_and_concurrent_edit_refused(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            foreign = root / ".claude/skills/multi-agent-collaboration"
            foreign.mkdir(parents=True)
            with self.assertRaisesRegex(RuntimeError, "foreign"):
                m.manage(root, "install", True)
            self.assertFalse((root / ".codex").exists())
            config = root / "test.json"
            config.write_text('{"new":1}')
            with self.assertRaisesRegex(RuntimeError, "concurrently"):
                m.replace(config, b"{}", {})
            self.assertEqual(json.loads(config.read_bytes()), {"new": 1})

    def test_unwired_doctor_rejects(self):
        with tempfile.TemporaryDirectory() as td:
            m.manage(td, "install", True)
            cfg = Path(td) / ".codex/hooks.json"
            cfg.write_text('{"hooks":{}}')
            self.assertFalse(m.manage(td, "doctor")["ok"])

    def test_doctor_stays_available_with_unresolved_frozen_report(self):
        with tempfile.TemporaryDirectory() as td:
            m.manage(td, 'install', True)
            root = Path(self.tmp.name) / 'task'
            root.mkdir()
            (root / 'task-pack.json').write_text(json.dumps(dict(
                draft=False, completion_receipt=str(root / 'missing-receipt.json'))))
            (self.active / 'offline-doctor.json').write_text(json.dumps(dict(
                task_id='offline-doctor-task', artifact_root=str(root),
                participants=[dict(role='executor', surface_uuid='offline-doctor-surface')])))
            result = m.manage(td, 'doctor')
            self.assertTrue(result['ok'])
            self.assertTrue(result['checks']['cmux_consensus_stop_guard']['benignExitZero'])

    def test_post_submit_hook_both_clients_and_missing_registration(self):
        with tempfile.TemporaryDirectory() as td:
            m.manage(td, "install", True)
            for client, filename in [('codex', 'hooks.json'), ('claude', 'settings.json')]:
                cfg = Path(td) / ('.' + client) / filename
                original = cfg.read_bytes()
                doc = json.loads(original)
                commands = [h['command'] for entry in doc['hooks']['PostToolUse']
                            for h in entry['hooks']]
                self.assertEqual(commands.count(m.command('cmux_native_delivery_guard')), 1)
                self.assertEqual(mac_harness._wired_guards(cfg)['cmux_native_delivery_guard'], {'PostToolUse'})
                doc['hooks']['PostToolUse'] = []
                cfg.write_text(json.dumps(doc))
                self.assertFalse(m.manage(td, 'doctor')['ok'])
                cfg.write_bytes(original)
            m.manage(td, 'uninstall', True)
            for client, filename in [('codex', 'hooks.json'), ('claude', 'settings.json')]:
                cfg = Path(td) / ('.' + client) / filename
                self.assertNotIn('cmux_native_delivery_guard', mac_harness._wired_guards(cfg))

    def test_owned_historical_hooks_retired_foreign_names_preserved(self):
        home = Path(self.tmp.name)
        release = home / '.local/share/multi-agent-collaboration/releases/old/source/scripts'
        foreign = '/opt/foreign/scripts/cmux_send_proof_stop_guard.py'
        original = {'keep': {'value': 7}, 'hooks': {'Stop': [{'matcher': 'custom', 'hooks': [
            {'type': 'command', 'command': f'python3 {release}/cmux_send_proof_stop_guard.py'},
            {'type': 'command', 'command': f'python3 -B {release}/cmux_executor_idle_guard.py'},
            {'type': 'command', 'command': f'python3 {foreign}'},
        ]}], 'PostToolUse': [{'hooks': [
            {'type': 'command', 'command': f'python3 -B {release}/cmux_native_delivery_guard.py'},
            {'type': 'command', 'command': f'python3 {release}/cmux_submit_confirmation_guard.py'},
        ]}]}}
        migrated = m.transform(original, home=home)
        self.assertEqual(migrated['keep'], original['keep'])
        self.assertEqual(migrated['hooks']['Stop'][0], {'matcher': 'custom', 'hooks': [
            {'type': 'command', 'command': f'python3 {foreign}'}]})
        commands = [h['command'] for entries in migrated['hooks'].values()
                    for entry in entries for h in entry['hooks']]
        self.assertFalse(any(str(release) in command for command in commands))
        for guard in m.GUARDS:
            self.assertEqual(commands.count(m.command(guard)), 1)
        stop_commands = [h['command'] for entry in migrated['hooks']['Stop'] for h in entry['hooks']]
        self.assertEqual(stop_commands.count(m.command('cmux_executor_idle_guard')), 1)
        self.assertEqual(m.transform(migrated, home=home), migrated)
        self.assertEqual(m.transform(migrated, True, home=home)['hooks']['Stop'],
                         [migrated['hooks']['Stop'][0]])

    def test_workflow_advisory_exact_ownership_and_mixed_upgrade(self):
        home = Path(self.tmp.name)
        old = home / '.local/share/multi-agent-collaboration/releases/old/source/scripts'
        name = 'cmux_consensus_stop_guard.py'
        wrapper = old / 'cmux_workflow_advisory.py'
        owned = [f'python3 -B {wrapper} --hook {name}',
                 f'python3 -B {old / name}']
        foreign = [f'python3 -B /opt/foreign/scripts/cmux_workflow_advisory.py --hook {name}',
                   f'python3 -B {wrapper} --hook foreign.py',
                   f'python3 -B {wrapper} --hook {name} --extra',
                   f'python3 -B {wrapper} --hook={name}']
        for command in owned:
            self.assertTrue(m.owned_command(command, home))
        for command in foreign:
            self.assertFalse(m.owned_command(command, home))
        original = {'hooks': {'Stop': [{'matcher': 'custom', 'hooks': [
            {'type': 'command', 'command': value} for value in owned + foreign]}]}}
        migrated = m.transform(original, home=home, executor_reask=True)
        self.assertEqual(m.transform(migrated, home=home, executor_reask=True), migrated)
        kept = migrated['hooks']['Stop'][0]['hooks']
        self.assertEqual([hook['command'] for hook in kept], foreign)
        commands = [hook['command'] for entries in migrated['hooks'].values()
                    for entry in entries for hook in entry['hooks']]
        self.assertEqual(set(foreign), set(commands) & set(foreign))
        for name in dict(m.GUARDS, **m.OPTIONAL_GUARDS):
            self.assertEqual(commands.count(m.command(name)), 1)
        for name in ('cmux_workspace_guard', 'cmux_agent_panel_guard'):
            self.assertEqual(Path(shlex.split(m.command(name))[2]).stem, name)

    def test_wrapper_migration_preserves_all_resources(self):
        home = Path(self.tmp.name).resolve() / 'wrapper-home'
        release = home / '.local/share/multi-agent-collaboration/releases/old/source'
        release.mkdir(parents=True)
        (release / 'SKILL.md').write_text('old immutable skill\n')
        wrappers = []
        for client in ('codex', 'claude'):
            wrapper = home / ('.' + client) / 'skills/multi-agent-collaboration'
            (wrapper / 'references').mkdir(parents=True)
            (wrapper / 'SKILL.md').write_text(
                f'---\nname: multi-agent-collaboration\n---\n[skill]({release}/SKILL.md)\n')
            (wrapper / 'references/local.md').write_bytes(b'preserve every byte\x00\n')
            (wrapper / 'references/original').symlink_to('local.md')
            wrappers.append((wrapper, m.skill_snapshot(wrapper)))
        m.manage(home, 'install', True)
        for wrapper, snapshot in wrappers:
            self.assertTrue(wrapper.is_symlink())
            self.assertEqual(wrapper.resolve(), m.ROOT)
            backups = list(wrapper.parent.glob(wrapper.name + '.multi-agent-backup-*'))
            self.assertEqual(len(backups), 1)
            self.assertEqual(m.skill_snapshot(backups[0]), snapshot)
        self.assertEqual((release / 'SKILL.md').read_text(), 'old immutable skill\n')

    def test_config_drift_aborts_before_any_client_is_changed(self):
        home = Path(self.tmp.name).resolve() / 'drift-home'
        codex = home / '.codex/hooks.json'
        claude = home / '.claude/settings.json'
        for path in (codex, claude):
            path.parent.mkdir(parents=True)
            path.write_text('{"original":true}')
        real_read = m.read
        changed = False

        def concurrent_read(path):
            nonlocal changed
            result = real_read(path)
            if path == claude and not changed:
                changed = True
                path.write_text('{"concurrent":true}')
            return result

        with patch.object(m, 'read', side_effect=concurrent_read):
            with self.assertRaisesRegex(RuntimeError, 'concurrently'):
                m.manage(home, 'install', True)
        self.assertEqual(json.loads(codex.read_text()), {'original': True})
        self.assertEqual(json.loads(claude.read_text()), {'concurrent': True})
        self.assertEqual(list(home.rglob('multi-agent-collaboration')), [])

    def test_skill_resource_drift_preserved(self):
        wrapper = Path(self.tmp.name) / 'wrapper'
        wrapper.mkdir()
        (wrapper / 'SKILL.md').write_text('old')
        snapshot = m.skill_snapshot(wrapper)
        (wrapper / 'new-reference.md').write_text('concurrent work')
        with self.assertRaisesRegex(RuntimeError, 'concurrently'):
            m.replace_skill(wrapper, snapshot)
        self.assertFalse(wrapper.is_symlink())
        self.assertEqual((wrapper / 'new-reference.md').read_text(), 'concurrent work')
        self.assertEqual(list(wrapper.parent.glob('wrapper.multi-agent-backup-*')), [])

    def test_install_from_immutable_source_pins_commands_and_links(self):
        home = Path(self.tmp.name).resolve() / 'immutable-home'
        release = home / '.local/share/multi-agent-collaboration/releases/new/source'
        release.mkdir(parents=True)
        (release / 'SKILL.md').write_text('current immutable skill')
        with patch.object(m, 'ROOT', release):
            m.manage(home, 'install', True)
            for client, filename in (('codex', 'hooks.json'), ('claude', 'settings.json')):
                link = home / ('.' + client) / 'skills/multi-agent-collaboration'
                self.assertEqual(link.resolve(), release)
                doc = json.loads((home / ('.' + client) / filename).read_text())
                commands = [hook['command'] for entries in doc['hooks'].values()
                            for entry in entries for hook in entry['hooks']]
                self.assertEqual(set(commands), {m.command(guard) for guard in m.GUARDS})
