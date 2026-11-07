import base64
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import plistlib
import stat
import tempfile
import unittest
from unittest.mock import patch
import warnings
import zipfile

import export_original as e

HERE = Path(__file__).resolve().parent
EXPECTED = json.loads((HERE / 'ORIGINAL-TEST-CONTRACT.json').read_bytes())


def raw(value):
    return json.dumps(value, sort_keys=True).encode()


def zip_bytes(members, *, duplicate=None, special=None):
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
        for name, value in members.items():
            zi = zipfile.ZipInfo(name)
            zi.external_attr = (stat.S_IFREG | 0o600) << 16
            if special == name:
                zi.external_attr = (stat.S_IFLNK | 0o777) << 16
            z.writestr(zi, value)
        if duplicate:
            with warnings.catch_warnings():
                warnings.simplefilter('ignore', UserWarning)
                z.writestr(duplicate, b'duplicate')
    return out.getvalue()


def fixture():
    c = json.loads((HERE / 'EXPORT-CONTRACT.pending.json').read_bytes())
    c.update(status='PINNED_ORIGINAL_SUCCESSFUL_ARTIFACT', artifact_id=999, artifact_size_in_bytes=1,
             artifact_api_digest_sha256='a' * 64, original_run_json_sha256='b' * 64,
             original_jobs_json_sha256='c' * 64, original_artifact_json_sha256='d' * 64)
    originals = {name: b'original log\n' for name in e.REQUIRED}
    originals['red-commit.txt'] = b'1' * 40 + b'\n'
    originals['green-commit.txt'] = b'2' * 40 + b'\n'
    receipts = {}
    for phase in e.PHASES:
        exit_code = 65 if phase == 'red' else 0
        rec = {'phase': phase, 'tests_patch_sha256': c['tests_patch_sha256'],
               'fix_patch_sha256': c['fix_patch_sha256'], 'exit_code': exit_code}
        if phase == 'package':
            rec.update(executed_test_count=6, passed_display_names=EXPECTED['package_test_display_names'])
        else:
            key = {'red': 'required_green_test_ids', 'green': 'required_green_test_ids',
                   'boundary': 'boundary_test_ids', 'existing-binding': 'existing_binding_test_ids'}[phase]
            results = {name: ('Failed' if phase == 'red' and name in EXPECTED['expected_red_test_ids'] else 'Passed')
                       for name in EXPECTED[key]}
            rec.update(executed_test_count=len(results), results=results)
            originals[phase + '/tests.json'] = raw({'testNodes': [phase]})
            originals[phase + '/result.tests.json'] = originals[phase + '/tests.json']
        originals[phase + '/exit-code.txt'] = str(exit_code).encode() + b'\n'
        originals[phase + '/command-argv.txt'] = b'original\ncommand\n'
        originals[phase + '/VERDICT.json'] = raw(rec)
        receipts[phase] = rec
    inner = zip_bytes({'fake.app/Contents/Info.plist': b'not executed'})
    p = {
        'status': 'CI_BUILD_ARCHIVE_VERIFIED_NOT_LAUNCHED',
        'upstream_commit': c['upstream_commit'], 'preparation_commit': c['original_head_sha'],
        'workflow_run_id': str(c['original_run_id']), 'workflow_run_attempt': str(c['original_run_attempt']),
        'tests_patch_sha256': c['tests_patch_sha256'], 'fix_patch_sha256': c['fix_patch_sha256'],
        'build_configuration': 'Debug', 'backend_mode': 'local',
        'app_name': f"cmux Nested Restore {c['original_run_id']}-{c['original_run_attempt']}.app",
        'bundle_id': f"com.cmuxterm.app.debug.nestedrestore.r{c['original_run_id']}a{c['original_run_attempt']}",
        'tag': f"nested-{c['original_run_id']}-{c['original_run_attempt']}",
        'source_shared_with_green_tests': True, 'same_binary_as_isolated_test_host': False,
        'full_nested_restore_verified': False, 'green_source_commit': '2' * 40,
        'green_source_tree': c['expected_green_tree'], 'test_receipts': receipts,
        'archive': {'name': 'cmux-nested-fixed.zip', 'size': len(inner), 'sha256': e.digest(inner)},
    }
    originals['retained-app/PROVENANCE.json'] = raw(p)
    outer = zip_bytes(originals | {e.APP_ARCHIVE: inner, 'result.xcresult/unselected': b'not exported'})
    c['artifact_size_in_bytes'] = len(outer)
    c['artifact_api_digest_sha256'] = e.digest(outer)
    run = {'repository': {'full_name': c['repository']}, 'id': c['original_run_id'],
           'run_attempt': 1, 'head_sha': c['original_head_sha'], 'path': c['workflow_path'],
           'status': 'completed', 'conclusion': 'success'}
    job = {'id': c['original_job_id'], 'run_id': c['original_run_id'], 'run_attempt': 1,
           'head_sha': c['original_head_sha'], 'name': 'regression', 'status': 'completed', 'conclusion': 'success'}
    artifact = {'id': c['artifact_id'], 'name': c['artifact_name'], 'size_in_bytes': len(outer),
                'digest': 'sha256:' + e.digest(outer), 'expired': False,
                'workflow_run': {'id': c['original_run_id'], 'head_sha': c['original_head_sha']}}
    metadata = {'BUILD-RUN.json': raw(run), 'BUILD-JOBS.json': raw({'total_count': 1, 'jobs': [job]}),
                'ARTIFACT.json': raw(artifact)}
    for name, key in [('BUILD-RUN.json', 'original_run_json_sha256'), ('BUILD-JOBS.json', 'original_jobs_json_sha256'),
                      ('ARTIFACT.json', 'original_artifact_json_sha256')]:
        c[key] = e.digest(metadata[name])
    return c, metadata, originals, p, outer


class ExportTests(unittest.TestCase):
    def test_old_r6_source_tree_rejected_despite_success_receipts(self):
        c, _, originals, p, _ = fixture()
        p['green_source_tree'] = '28d6b1513324e8a35e9f49c750d62c7f3b919361'
        originals['retained-app/PROVENANCE.json'] = raw(p)
        with self.assertRaisesRegex(ValueError, 'independently reconstructed'):
            e.validate_provenance(c, originals, EXPECTED)

    def test_unbound_tree_contract_refuses_before_transfer(self):
        c, _, _, _, _ = fixture()
        c['expected_green_tree'] = 'f' * 40
        download = unittest.mock.Mock()
        with self.assertRaisesRegex(ValueError, 'before transfer'):
            e.execute({'contract': c}, HERE, Path('/missing/workspace'), Path('/missing/output'),
                      {'GITHUB_RUN_ID': '999999'}, download=download)
        download.assert_not_called()

    def test_expected_green_tree_is_required(self):
        c, _, _, _, _ = fixture()
        del c['expected_green_tree']
        with self.assertRaisesRegex(ValueError, 'source identity'):
            e.contract(c)

    def test_pending_contract_rejected_before_metadata(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d).resolve() / 'contract.json'
            path.write_bytes((HERE / 'EXPORT-CONTRACT.pending.json').read_bytes())
            with self.assertRaisesRegex(ValueError, 'pending'):
                e.prepare(path, e.app.sha(path), Path(d) / 'missing')

    def test_exact_successful_attempt_and_artifact_join(self):
        c, metadata, _, _, _ = fixture()
        self.assertEqual(e.api_join(e.contract(c), metadata)['job']['id'], c['original_job_id'])

    def test_wrong_run_attempt_head_workflow_and_job_reject(self):
        c, metadata, _, _, _ = fixture()
        for name, field, value in [('BUILD-RUN.json', 'run_attempt', 2), ('BUILD-RUN.json', 'head_sha', 'f'*40),
                                   ('BUILD-RUN.json', 'path', '.github/workflows/other.yml'),
                                   ('BUILD-RUN.json', 'conclusion', 'failure')]:
            with self.subTest(field=field):
                changed = deepcopy(metadata); data = json.loads(changed[name]); data[field] = value
                changed[name] = raw(data)
                with self.assertRaises(ValueError): e.api_join(c, changed)
        changed = deepcopy(metadata); data = json.loads(changed['BUILD-JOBS.json'])
        data['jobs'][0]['run_attempt'] = 2; changed['BUILD-JOBS.json'] = raw(data)
        with self.assertRaises(ValueError): e.api_join(c, changed)

    def test_artifact_digest_size_and_expiry_reject(self):
        c, metadata, _, _, _ = fixture()
        for field, value in [('digest', 'sha256:'+'f'*64), ('size_in_bytes', 1), ('expired', True), ('id', 1000)]:
            with self.subTest(field=field):
                changed = deepcopy(metadata); obj = json.loads(changed['ARTIFACT.json']); obj[field] = value
                changed['ARTIFACT.json'] = raw(obj)
                with self.assertRaises(ValueError): e.api_join(c, changed)

    def test_saved_api_raw_hash_not_reserialized(self):
        c, metadata, _, _, _ = fixture()
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve(); path = root / 'contract.json'; path.write_bytes(raw(c))
            for name, value in metadata.items(): (root / name).write_bytes(value)
            plan = e.prepare(path, e.app.sha(path), root)
            self.assertEqual(plan['raw_metadata'], metadata)
            (root / 'BUILD-RUN.json').write_bytes(metadata['BUILD-RUN.json'] + b' ')
            with self.assertRaisesRegex(ValueError, 'bytes changed'): e.prepare(path, e.app.sha(path), root)

    def test_exact_original_whitelist_and_no_package_typed_fabrication(self):
        _, _, originals, _, outer = fixture()
        with zipfile.ZipFile(io.BytesIO(outer)) as z:
            selected = e.collect_originals(z, e.zip_index(z))
        self.assertEqual(selected, originals)
        self.assertNotIn('package/tests.json', selected)

    def test_missing_required_member_rejects(self):
        _, _, originals, _, _ = fixture(); del originals['boundary/tests.json']
        with zipfile.ZipFile(io.BytesIO(zip_bytes(originals))) as z:
            with self.assertRaisesRegex(ValueError, 'missing'): e.collect_originals(z, e.zip_index(z))

    def test_oversized_required_log_rejects_without_truncation(self):
        _, _, originals, _, _ = fixture(); originals['red/execution.log'] = b'x' * (8 * e.MIB + 1)
        with zipfile.ZipFile(io.BytesIO(zip_bytes(originals))) as z:
            with self.assertRaisesRegex(ValueError, 'bound'): e.collect_originals(z, e.zip_index(z))

    def test_typed_copy_mismatch_rejects(self):
        _, _, originals, _, _ = fixture(); originals['green/result.tests.json'] = b'changed'
        with zipfile.ZipFile(io.BytesIO(zip_bytes(originals))) as z:
            with self.assertRaisesRegex(ValueError, 'typed copy'): e.collect_originals(z, e.zip_index(z))

    def test_unsafe_duplicate_and_special_zip_members_reject(self):
        for members, options in [({'../escape': b'x'}, {}), ({'/abs': b'x'}, {}),
                                  ({'a\\b': b'x'}, {}), ({'a': b'x', 'a/b': b'x'}, {}),
                                  ({'a': b'x'}, {'duplicate': 'a'}), ({'a': b'x'}, {'special': 'a'})]:
            with self.subTest(members=list(members)):
                with zipfile.ZipFile(io.BytesIO(zip_bytes(members, **options))) as z:
                    with self.assertRaises(ValueError): e.zip_index(z)

    def test_provenance_sanity_requires_complete_original_case_inventory(self):
        c, _, originals, p, _ = fixture()
        self.assertEqual(e.validate_provenance(c, originals, EXPECTED)['green_source_tree'],
                         c['expected_green_tree'])
        changed = deepcopy(originals); p['test_receipts']['green']['results'].pop(next(iter(p['test_receipts']['green']['results'])))
        changed['retained-app/PROVENANCE.json'] = raw(p)
        changed['green/VERDICT.json'] = raw(p['test_receipts']['green'])
        with self.assertRaisesRegex(ValueError, 'case set'): e.validate_provenance(c, changed, EXPECTED)

    def test_unmatched_original_receipt_and_command_reject(self):
        c, _, originals, _, _ = fixture()
        for name, value in [('green/VERDICT.json', b'{}'), ('green/command-argv.txt', b'other'),
                            ('green-commit.txt', b'4'*40+b'\n')]:
            with self.subTest(name=name):
                with self.assertRaises(ValueError): e.validate_provenance(c, originals | {name: value}, EXPECTED)

    def test_optional_log_omission_explicit(self):
        _, _, originals, _, _ = fixture()
        values = originals | {'retained-app/build.log': b'x' * (8 * e.MIB + 1)}
        with zipfile.ZipFile(io.BytesIO(zip_bytes(values))) as z:
            result = e.optional_originals(z, e.zip_index(z), originals)
        self.assertFalse(result['retained-app/build.log']['exported'])
        self.assertEqual(result['retained-app/build.log']['original_bytes'], 8 * e.MIB + 1)
        self.assertNotIn('retained-app/build.log', originals)

    def test_full_outer_digest_failure_precedes_observer(self):
        c, metadata, _, _, outer = fixture()
        c['artifact_api_digest_sha256'] = '0'*64
        plan = {'contract': c, 'raw_contract': raw(c), 'raw_metadata': metadata, 'contract_sha256': e.digest(raw(c))}
        called = []
        class Response(io.BytesIO):
            headers = {}
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {'RUNNER_TEMP': d, 'CCC_PRIVATE_FIXTURE_KEY': base64.b64encode(b'k'*32).decode()}), patch.object(e.stage, 'capacity'):
            root = Path(d).resolve()
            with self.assertRaisesRegex(ValueError, 'bytes differ'):
                e.execute(plan, HERE, root/'scratch', root/'output', exporter(),
                          download=lambda _: Response(outer), observer=lambda *args: called.append(args))
        self.assertEqual(called, [])

    def test_valid_synthetic_export_preserves_raw_bytes_and_no_app_archive(self):
        c, metadata, originals, _, outer = fixture()
        plan = {'contract': c, 'raw_contract': raw(c), 'raw_metadata': metadata, 'contract_sha256': e.digest(raw(c))}
        class Response(io.BytesIO):
            headers = {}
        key = b'k'*32
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {
            'RUNNER_TEMP': d, 'CCC_PRIVATE_FIXTURE_KEY': base64.b64encode(key).decode()}), patch.object(e.stage, 'capacity'):
            root = Path(d).resolve()
            public = e.execute(plan, HERE, root/'scratch', root/'output', exporter(),
                               download=lambda _: Response(outer),
                               observer=lambda *args: (b'original plist', {'app_launched': False}))
            payload = e.open_payload((root/'output/evidence.aesgcm').read_bytes(), public, public['aad'], key)
            self.assertEqual({n.removeprefix('original/'): v for n,v in payload.items() if n.startswith('original/')}, originals)
            self.assertNotIn('original/' + e.APP_ARCHIVE, payload)
            self.assertFalse(json.loads(payload['EXPORT-RECEIPT.json'])['commit_texts_prove_parent_chain'])
            self.assertEqual(set(p.name for p in (root/'output').iterdir()), {'ENVELOPE.json', 'evidence.aesgcm'})

    def test_cipher_tamper_and_wrong_exporter_aad_reject(self):
        key = b'k'*32; context = {'exporter': exporter(), 'original_digest': 'f'*64}
        with tempfile.TemporaryDirectory() as d:
            output = Path(d).resolve() / 'sealed'
            public = e.seal_payload({'original/a': b'raw original'}, context, key, output)
            cipher = (output/'evidence.aesgcm').read_bytes()
            with self.assertRaises(ValueError): e.open_payload(cipher[:-1]+bytes([cipher[-1]^1]), public, context, key)
            with self.assertRaises(ValueError): e.open_payload(cipher, public, context | {'other': 1}, key)

    def test_pending_has_no_app_pin_or_success_claim(self):
        c = json.loads((HERE/'EXPORT-CONTRACT.pending.json').read_bytes())
        self.assertIsNone(c['artifact_id']); self.assertIsNone(c['artifact_api_digest_sha256'])
        self.assertFalse(c['app_pin_created'])

    def test_local_execution_gate(self):
        with patch.dict(os.environ, {'GITHUB_ACTIONS': 'false'}, clear=True):
            with self.assertRaisesRegex(ValueError, 'GitHub Mac'): e.runner_identity()


    def test_missing_sealing_key_and_path_escape_reject_before_download(self):
        c, metadata, _, _, _ = fixture()
        plan = {'contract': c, 'raw_contract': raw(c), 'raw_metadata': metadata, 'contract_sha256': e.digest(raw(c))}
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {'RUNNER_TEMP': d, 'CCC_PRIVATE_FIXTURE_KEY': ''}):
            root = Path(d).resolve()
            with patch.object(e.stage, 'download_response') as download:
                with self.assertRaisesRegex(ValueError, 'key missing'):
                    e.execute(plan, HERE, root/'scratch', root/'output', exporter(), download=download)
                download.assert_not_called()
                with self.assertRaisesRegex(ValueError, 'contained'):
                    e.execute(plan, HERE, root/'scratch', root.parent/'foreign-output', exporter(), download=download)
                download.assert_not_called()

    def test_complete_source_closure_detects_changed_and_added_members(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            (root/'export_original.py').write_bytes((HERE/'export_original.py').read_bytes())
            (root/'helper.py').write_bytes(b'original helper')
            manifest = {'files': {p.name: e.app.sha(p) for p in root.iterdir()}}
            path = root/'SOURCE-MANIFEST.json'; path.write_bytes(raw(manifest)); wanted = e.app.sha(path)
            e.source_closure(root, wanted)
            (root/'helper.py').write_bytes(b'changed helper')
            with self.assertRaisesRegex(ValueError, 'closure differs'): e.source_closure(root, wanted)
            (root/'helper.py').write_bytes(b'original helper')
            (root/'foreign.py').write_bytes(b'added')
            with self.assertRaisesRegex(ValueError, 'closure differs'): e.source_closure(root, wanted)


def exporter():
    return {'GITHUB_REPOSITORY': 'lzhs1995/ccc', 'GITHUB_RUN_ID': '999999',
            'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_SHA': '9'*40}



class AppObservationTests(unittest.TestCase):
    def setup_bundle(self, root, *, wrong_plist=False):
        c, _, _, p, _ = fixture()
        appdir = root / 'fixture' / p['app_name']
        for name in ('Contents/MacOS', 'Contents/Resources/bin'):
            (appdir / name).mkdir(parents=True, mode=0o755)
        plist = plistlib.dumps({'CFBundleIdentifier': 'wrong.bundle' if wrong_plist else p['bundle_id'],
                               'CFBundleExecutable': 'cmux', 'LSEnvironment': {'CMUX_TAG': p['tag']}})
        (appdir/'Contents/Info.plist').write_bytes(plist)
        for name in ('Contents/MacOS/cmux', 'Contents/Resources/bin/cmux'):
            (appdir/name).write_bytes(b'synthetic-not-a-Mach-O')
            (appdir/name).chmod(0o755)
        p['bundle_members'] = e.app.inventory(appdir)
        p['signatures'] = {'display': {'stdout': '', 'stderr': 'CDHash=' + 'e'*40 + '\n'}}
        p['architecture'] = {'stdout': 'arm64\n'}
        archive = root / 'synthetic.zip'
        with zipfile.ZipFile(archive, 'w') as z:
            for item in sorted(appdir.rglob('*')):
                zi = zipfile.ZipInfo(item.relative_to(appdir.parent).as_posix() + ('/' if item.is_dir() else ''))
                zi.external_attr = item.lstat().st_mode << 16
                z.writestr(zi, b'' if item.is_dir() else item.read_bytes())
        def fake_ditto(argv, **kwargs):
            self.assertEqual(argv[:3], ['/usr/bin/ditto', '-x', '-k'])
            destination = Path(argv[-1])
            import shutil
            shutil.copytree(appdir, destination / appdir.name)
        return p, archive, plist, fake_ditto

    def commands(self, p, *, wrong_signature=False):
        seen = []
        def command(argv):
            seen.append(argv)
            if argv[0] == '/usr/bin/codesign':
                self.assertIn(argv[1], ('--verify', '--display'))
                if '--verbose=4' in argv:
                    text = ('Identifier=' + p['bundle_id'] + '\nCDHash=' +
                            ('f' if wrong_signature else 'e')*40 +
                            '\nSignature=adhoc\nTeamIdentifier=not set\n')
                    return {'argv': argv, 'stdout': '', 'stderr': text}
                return {'argv': argv, 'stdout': '', 'stderr': ''}
            self.assertEqual(argv[:2], ['/usr/bin/lipo', '-archs'])
            return {'argv': argv, 'stdout': 'arm64\n', 'stderr': ''}
        return command, seen

    def test_observation_compares_complete_inventory_signature_and_raw_plist_without_launch(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve(); p, archive, plist, ditto = self.setup_bundle(root)
            command, seen = self.commands(p)
            with patch.object(e.subprocess, 'run', ditto), patch.object(e.app, 'command', command):
                actual_plist, observation = e.observe_app(archive, root, p)
            self.assertEqual(actual_plist, plist)
            self.assertTrue(observation['complete_inventory_matched'])
            self.assertEqual(observation['application_sha256'], e.digest(b'synthetic-not-a-Mach-O'))
            self.assertFalse(observation['app_launched'])
            self.assertEqual({x[0] for x in seen}, {'/usr/bin/codesign', '/usr/bin/lipo'})

    def test_original_manifest_tamper_rejected_before_signature(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve(); p, archive, _, ditto = self.setup_bundle(root)
            p['bundle_members'] = p['bundle_members'][:-1]
            with patch.object(e.subprocess, 'run', ditto), patch.object(e.app, 'command') as command:
                with self.assertRaisesRegex(ValueError, 'complete archived bundle'):
                    e.observe_app(archive, root, p)
                command.assert_not_called()

    def test_wrong_actual_plist_identity_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve(); p, archive, _, ditto = self.setup_bundle(root, wrong_plist=True)
            with patch.object(e.subprocess, 'run', ditto), patch.object(e.app, 'command') as command:
                with self.assertRaisesRegex(ValueError, 'plist identity'):
                    e.observe_app(archive, root, p)
                command.assert_not_called()

    def test_original_signature_mismatch_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve(); p, archive, _, ditto = self.setup_bundle(root)
            command, _ = self.commands(p, wrong_signature=True)
            with patch.object(e.subprocess, 'run', ditto), patch.object(e.app, 'command', command):
                with self.assertRaisesRegex(ValueError, 'code directory hash'):
                    e.observe_app(archive, root, p)

    def test_inner_archive_escape_rejected_before_any_command(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve(); p, archive, _, _ = self.setup_bundle(root)
            archive.write_bytes(zip_bytes({'../escape': b'x'}))
            with patch.object(e.subprocess, 'run') as run:
                with self.assertRaises(ValueError): e.observe_app(archive, root, p)
                run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
