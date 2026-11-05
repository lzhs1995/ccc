"""Finite offline port checks. No Herdr launch, input, build or signal."""
import ast
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import actions_context as context
import run_actions as runner


def digest(node):
    return hashlib.sha256(ast.dump(node).encode()).hexdigest()


class Normalize(ast.NodeTransformer):
    def visit_Dict(self, node):
        self.generic_visit(node)
        for i, key in enumerate(node.keys):
            if isinstance(key, ast.Constant) and key.value in ('USER', 'LOGNAME'):
                node.values[i] = ast.Constant(value='<runner-user>')
        return node


class PortGates(unittest.TestCase):
    def setUp(self):
        self.original = json.loads((HERE / 'ORIGINAL-CONTROLLER-PINS.json').read_bytes())
        self.tree = ast.parse((HERE / 'fixture/run_private_remote_namespace.py').read_text())

    def test_private_tui_class_unchanged(self):
        node = next(n for n in self.tree.body if isinstance(n, ast.ClassDef) and n.name == 'PrivateTui')
        self.assertEqual(digest(node), self.original['original_private_tui_ast_sha256'])

    def test_runtime_and_cleanup_body_unchanged_except_command_transport_and_username(self):
        main = next(n for n in self.tree.body if isinstance(n, ast.FunctionDef) and n.name == 'main')
        start = next(i for i, n in enumerate(main.body) if isinstance(n, ast.Assign)
                     and isinstance(n.targets[0], ast.Name) and n.targets[0].id == 'home')
        node = Normalize().visit(ast.Module(body=main.body[start:], type_ignores=[]))
        self.assertEqual(digest(node), self.original['original_runtime_body_normalized_ast_sha256'])

    def test_all_original_remote_verifier_functions_unchanged_except_gh_command(self):
        tree = ast.parse((HERE / 'fixture/remote_proof.py').read_text())
        actual = {n.name: digest(n) for n in tree.body if isinstance(n, ast.FunctionDef) and n.name != 'gh_json'}
        self.assertEqual(actual, self.original['original_verifier_functions'])

    def test_scope_dependencies_are_exact_original_full_files(self):
        self.assertEqual(sorted(p.name for p in (HERE / 'scope').glob('*.py')), sorted(self.original['scope_files']))
        for name, pin in self.original['scope_files'].items():
            self.assertEqual(runner.sha(HERE / 'scope' / name), pin)

    def test_full_scope_imports_and_real_own_pid_birth_arguments_scan(self):
        if sys.platform != 'darwin': self.skipTest('original native process API requires Darwin')
        sys.path.insert(0, str(HERE / 'scope'))
        import ccc_guard_scope as scope
        pid = os.getpid(); born = scope.birth(pid)
        argv, _ = scope.arguments(pid)
        self.assertIsNotNone(born); self.assertTrue(argv)
        self.assertEqual(scope.birth(pid), born)
        self.assertEqual(scope.scan(reader=lambda p: {'pid': p, 'birth': scope.birth(p)} if p == pid else None),
                         [{'pid': pid, 'birth': born}])
        imported = {Path(m.__file__).name for m in sys.modules.copy().values()
                    if getattr(m, '__file__', None) and Path(m.__file__).is_relative_to(HERE / 'scope')}
        self.assertEqual(imported, set(self.original['scope_files']))

    def valid_env(self, temp):
        return {'GITHUB_ACTIONS': 'true', 'CI': 'true', 'RUNNER_OS': 'macOS',
                'RUNNER_ARCH': 'ARM64', 'RUNNER_ENVIRONMENT': 'github-hosted',
                'GITHUB_REPOSITORY': 'lzhs1995/ccc', 'GITHUB_RUN_ID': '1',
                'GITHUB_RUN_ATTEMPT': '1', 'RUNNER_TEMP': temp}

    def test_local_and_wrong_host_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            good = self.valid_env(temp)
            self.assertEqual(context.require_actions(good, 'darwin', 'arm64'), Path(temp).resolve())
            for key, value in [('GITHUB_ACTIONS', 'false'), ('RUNNER_ENVIRONMENT', 'self-hosted'),
                               ('GITHUB_REPOSITORY', 'other/repo'), ('RUNNER_ARCH', 'X64'),
                               ('GITHUB_RUN_ATTEMPT', '0')]:
                bad = {**good, key: value}
                with self.subTest(key=key), self.assertRaises(RuntimeError):
                    context.require_actions(bad, 'darwin', 'arm64')
            with self.assertRaises(RuntimeError): context.require_actions(good, 'linux', 'arm64')

    def test_output_reuse_and_escape_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            self.assertEqual(context.require_output(root / 'new', root), root / 'new')
            for p in [root, root.parent / 'outside', root / '../outside']:
                with self.assertRaises(RuntimeError): context.require_output(p, root)
            (root / 'exists').mkdir()
            with self.assertRaises(RuntimeError): context.require_output(root / 'exists', root)

    def test_capacity_below_40_rejected(self):
        from types import SimpleNamespace
        with patch.object(context.os, 'statvfs', return_value=SimpleNamespace(f_bavail=39, f_frsize=1024**3)):
            with self.assertRaises(RuntimeError): context.require_capacity(HERE)
        with patch.object(context.os, 'statvfs', return_value=SimpleNamespace(f_bavail=40, f_frsize=1024**3)):
            self.assertEqual(context.require_capacity(HERE), 40 * 1024**3)

    def test_exact_artifact_metadata_and_wrong_origin_rejected(self):
        pin = runner.ARTIFACTS['package.zip']
        good = {'id': pin['id'], 'name': pin['name'], 'size_in_bytes': pin['bytes'],
                'digest': 'sha256:' + pin['sha256'], 'expired': False,
                'workflow_run': {'id': 42014273454, 'head_sha': 'be088d0a754edd7fc0603e4dac64e6ee1903edd0',
                                 'head_branch': 'codex/herdr-v3-production-namespace-20261105'}}
        runner.verify_artifact_metadata(good, pin)
        for key, value in [('id', 1), ('size_in_bytes', 1), ('digest', 'sha256:' + '0' * 64), ('expired', True)]:
            with self.subTest(key=key), self.assertRaises(ValueError): runner.verify_artifact_metadata({**good, key: value}, pin)
        bad = copy.deepcopy(good); bad['workflow_run']['head_sha'] = '0' * 40
        with self.assertRaises(ValueError): runner.verify_artifact_metadata(bad, pin)

    def test_bundle_missing_changed_extra_members_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'one').write_text('one')
            manifest = root / 'SOURCE-MANIFEST.json'
            manifest.write_text(json.dumps({'files': {'one': runner.sha(root / 'one')}}))
            pin = runner.sha(manifest)
            runner.verify_bundle(root, pin)
            (root / 'one').write_text('two')
            with self.assertRaises(ValueError): runner.verify_bundle(root, pin)
            (root / 'one').unlink()
            with self.assertRaises(ValueError): runner.verify_bundle(root, pin)
            (root / 'one').write_text('one'); (root / 'extra').write_text('x')
            with self.assertRaises(ValueError): runner.verify_bundle(root, pin)

    def test_original_contract_and_server_pin(self):
        self.assertEqual(runner.sha(HERE / 'CONTRACT.json'), runner.CONTRACT_SHA)
        contract = json.loads((HERE / 'CONTRACT.json').read_bytes())
        self.assertEqual((contract['run_id'], contract['run_attempt'], contract['head_sha']),
                         (42014273454, 1, 'be088d0a754edd7fc0603e4dac64e6ee1903edd0'))
        self.assertEqual(runner.SERVER_SHA, '6037394411775f1734830be1727d4863a72a95e5ff8ff4c75393f906d70287bd')

    def test_public_bundle_has_no_original_user_home_or_credentials(self):
        forbidden = ('/Users/' + 'lzhs',)
        for path in HERE.rglob('*'):
            if path.is_file() and path.suffix in ('.json', '.py', '.rs', '.patch'):
                text = path.read_text()
                for marker in forbidden:
                    self.assertNotIn(marker, text, path.name)


if __name__ == '__main__':
    unittest.main(verbosity=2)
