"""Launch the verified executable, never replace a session from terminal text."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import ccc_batch_guard as guard
import ccc_codex_launcher as launcher
import ccc_workspace_batch as batch
import cmux_codex_watch as core
from tests import test_workspace_batch as fixtures
from tests.test_watch import grid_payload

resolve_native_binary = guard.native_binary


class DirectBatchTests(unittest.TestCase):
    setUp = fixtures.WorkspaceBatchTests.setUp

    def test_disabled_batch_executes_native_even_with_old_guard_on_path(self):
        self.worker.step()
        slot = self.worker.job['slots'][0]
        self.worker.job['guard_version'] = 1  # An existing B job must work too.
        directory = self.root / 'Library/Application Support/cmux-codex-continue'
        directory.mkdir(parents=True)
        native = self.root / 'native codex'
        # The resolver now requires a native executable, not an arbitrary script.
        # Keep both the installed entry and execution entirely in this fixture.
        native.symlink_to(Path(sys.executable).resolve())
        installed = self.root / 'installed-codex'
        wrapper = directory / 'codex-guard'
        wrapper.write_text('#!/bin/sh\nexit 94\n')
        wrapper.chmod(0o700)
        installed.symlink_to(wrapper)
        shim = self.root / 'codex'
        shim.write_text('#!/bin/sh\nexit 93\n')
        shim.chmod(0o700)
        (directory / 'codex-launcher.json').write_text(json.dumps({'native_binary': str(native)}))
        with patch.object(Path, 'home', return_value=self.root), \
             patch.object(guard, 'native_binary', side_effect=lambda: resolve_native_binary(entrypoint=installed)):
            argv = batch.native_launch_argv(self.config, self.worker.job, slot['index'])
        self.assertEqual(argv[0], str(Path(sys.executable).resolve()))
        result = subprocess.run([argv[0], '-c', 'import json,sys; print(json.dumps(sys.argv[1:]))', *argv[1:]],
                                env={**os.environ, 'PATH': str(self.root)},
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        args = json.loads(result.stdout)
        self.assertEqual(args[args.index('--cd') + 1], str(
            batch.working_directory(self.config, self.worker.job['id'], slot['index'])))
        overrides = [args[i + 1] for i, value in enumerate(args[:-1]) if value == '-c']
        self.assertEqual(sum(value.startswith('sqlite_home=') for value in overrides), 1)
        self.assertNotIn('--remote', args)

    def test_disabled_batch_does_not_adopt_or_arm_original_sessions(self):
        before = self.store.load()['workspace_rules'][0].copy()
        with patch.object(guard, 'arm') as arm, patch.object(batch, '_launch') as launch:
            result = batch.start(self.config, self.wid, client=self.client)
        arm.assert_not_called()
        launch.assert_called_once()
        self.assertEqual(result['job_id'], self.job['job_id'])
        self.assertEqual(self.store.load()['workspace_rules'][0].get('batch_guard'), before.get('batch_guard'))

    def test_only_explicit_w_releases_stop_marker_and_keeps_its_evidence(self):
        marker = guard.pool_dir(self.config, self.wid) / 'STOP.json'
        core.atomic_write_json(marker, {'reason': 'operator_pause', 'session_id': 'keep'})
        original = marker.read_bytes()
        guard.arm(self.config, self.wid)
        self.assertEqual(marker.read_bytes(), original)
        with patch('ccc_guard_migration.adopt_workspace') as adopt, patch.object(guard, 'request') as request:
            guard.arm(self.config, self.wid, resume=True)
        self.assertFalse(marker.exists())
        self.assertEqual(next(marker.parent.glob('STOP.resumed-*.json')).read_bytes(), original)
        adopt.assert_not_called()
        request.assert_not_called()

    def test_concurrent_operator_pause_vetoes_w_marker_release(self):
        marker = guard.pool_dir(self.config, self.wid) / 'STOP.json'
        core.atomic_write_json(marker, {'reason': 'operator_pause'})
        self.store.mutate(lambda c: c['workspace_rules'][0].update(paused=True))
        with self.assertRaisesRegex(RuntimeError, 'paused again'):
            guard.arm(self.config, self.wid, resume=True)
        self.assertTrue(marker.exists())

    def test_dead_endpoint_text_never_erases_confirmed_session_or_respawns(self):
        self.worker.step()
        self.worker.step()
        slot = self.worker.job['slots'][0]
        self.worker._advance(slot, confirmation_only=True)
        self.assertEqual(slot['phase'], 'confirmed')
        original = copy.deepcopy(slot)
        frame = grid_payload(['Quoted incident: app-server session could not be restored; its ID was not received'])
        frame['render_grid']['surface_id'] = slot['surface_id']
        with patch.object(self.client, 'replay', return_value=frame), \
             patch.object(self.client, 'respawn_surface', create=True) as respawn, \
             patch.object(self.worker, '_create'):
            self.worker.step()
        respawn.assert_not_called()
        self.assertEqual(slot, original)

    def test_submitted_or_ambiguous_turn_is_preserved_despite_remote_error(self):
        for phase in ('submitted', 'uncertain'):
            with self.subTest(phase=phase):
                self.worker.step()
                slot = self.worker.job['slots'][0]
                slot.update(phase=phase, session_id='original', native_seen_session_id='original',
                            submit_at=self.now, pid=71, process_start=11, transcript='keep.jsonl')
                original = copy.deepcopy(slot)
                frame = grid_payload(['Reconnect failed — check the endpoint', 'its ID was not received'])
                frame['render_grid']['surface_id'] = slot['surface_id']
                with patch.object(self.client, 'replay', return_value=frame), \
                     patch.object(self.client, 'respawn_surface', create=True) as respawn, \
                     patch.object(self.worker, '_confirm', return_value=False), \
                     patch.object(self.worker, '_finish_submission'), patch.object(self.worker, '_create'):
                    self.worker.step()
                respawn.assert_not_called()
                self.assertEqual({k: slot[k] for k in original}, original)
                self.assertNotIn('reopen_attempts', slot)


class DisabledLauncherTests(unittest.TestCase):
    def test_managed_double_quote_entry_and_injection_rejection(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve() / 'package space'
            (root / 'bin').mkdir(parents=True)
            binary = root / 'bin/codex'
            binary.write_bytes(b'\x7fELFfixture')
            binary.chmod(0o700)
            (root / 'codex-package.json').write_text(json.dumps(
                {'layoutVersion': 1, 'variant': 'codex', 'entrypoint': 'bin/codex'}))
            entry = root / 'managed-codex'
            valid = '#!/bin/sh\nexport CODEX_CLIENT_THREAD_OBSERVER=1\nexec "' + str(binary) + '" "$@"\n'
            entry.write_text(valid)
            self.assertEqual(resolve_native_binary(entrypoint=entry), str(binary))
            for invalid in (valid + 'echo injected\n', valid.replace(str(binary), '/other/codex'),
                            valid.replace('exec "', 'exec "$(id)')):
                entry.write_text(invalid)
                with self.assertRaises(RuntimeError):
                    resolve_native_binary(entrypoint=entry)

    def test_disabled_launcher_is_transparent_without_migration_or_guard(self):
        with patch.object(guard, 'provenance', side_effect=AssertionError('must not adopt any session')), \
             patch.object(guard, '_arm') as arm, patch.object(guard, 'launch') as launch, \
             patch('os.execv', side_effect=SystemExit) as execute, \
             patch.dict(os.environ, CMUX_WORKSPACE_ID='12345678-1234-1234-1234-123456789012'):
            with self.assertRaises(SystemExit):
                launcher.run(Path('config.json'), '/native/codex', ['resume', 'original'])
        execute.assert_called_once_with('/native/codex', ['/native/codex', 'resume', 'original'])
        arm.assert_not_called()
        launch.assert_not_called()

    def test_disabled_arm_does_not_migrate_or_stop_any_workspace(self):
        with patch('ccc_guard_migration.adopt_workspace') as adopt, \
             patch.object(guard, 'ensure_service') as ensure, patch.object(guard, 'request') as request:
            result = guard._arm(Path('config.json'), '12345678-1234-1234-1234-123456789012')
        adopt.assert_not_called()
        ensure.assert_not_called()
        request.assert_not_called()
        self.assertEqual(result['phase'], 'disabled')

    def test_native_metadata_must_not_reenter_wrapper_or_relative_executable(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            app = home / 'Library/Application Support/cmux-codex-continue'
            app.mkdir(parents=True)
            wrapper = app / 'codex-guard'
            wrapper.write_text('#!/bin/sh\nexit 91\n')
            wrapper.chmod(0o700)
            alias = home / 'codex'
            alias.symlink_to(wrapper)
            for value in (str(wrapper), str(alias), 'codex', None):
                with self.subTest(value=value), patch.object(Path, 'home', return_value=home):
                    (app / 'codex-launcher.json').write_text(json.dumps({'native_binary': value}))
                    with self.assertRaisesRegex(RuntimeError, 'cannot be proved'):
                        resolve_native_binary(entrypoint=alias)


if __name__ == '__main__':
    unittest.main()
