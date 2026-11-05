"""Synthetic file-only encrypted coverage; never launches a native binary."""
from pathlib import Path
import base64
import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch
import zipfile
import secure_bundle as s


class Coverage(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / 'private'
        self.root.mkdir(mode=0o700)
        self.output = self.base / 'evidence'
        self.sid = '00000000-0000-4000-8000-000000000001'
        self.nonce = 'a' * 32
        self.prompt = 'CCC_SHARED_BEGIN_' + self.nonce + ': Execute the supplied local fixture goal or read-only command only.'
        self.relative = 'sessions/2026/11/06/rollout-2026-11-06T00-00-00-' + self.sid + '.jsonl'
        self.raw_name = 'case/NATIVE-RAW/' + self.relative
        self.argv = ['/private/admitted/codex', '--no-alt-screen', self.prompt]
        self.client = dict(argv=self.argv, pid=101, birth=[1000, 2000])
        self.raw = b''.join((json.dumps(row, ensure_ascii=False) + '\n').encode() for row in [
            dict(type='session_meta', payload=dict(id=self.sid, developer_instructions='PRIVATE_SYNTHETIC_DEVELOPER')),
            dict(type='event_msg', payload=dict(type='user_message', message=self.prompt)),
        ])
        self.captured = {}
        self.put('case/INITIAL-NATIVE-FAILURE.json', dict(session_id=self.sid, pid=101, birth=[1000, 2000]))
        self.put('case/terminal-owner/LAUNCH.json', dict(argv=self.argv))
        self.put('case/terminal-owner/CLIENT-0.json', self.client)
        self.put('case/WORKSPACE-CREATE-INTENT.json', dict(argv=['--command', str(self.root / 'case/terminal-owner')]))
        self.set_raw(self.raw)
        self.put('case/PROVIDER-REQUESTS.json', [dict(body=dict(input=['PRIVATE_SYNTHETIC_PROVIDER']))])
        self.put('case/RESERVED-PROVIDER-LEDGER.json', dict(original_attempt='synthetic-a'))
        self.put('case/RESERVED-ORIGINAL-ATTEMPT.json', dict(attempt='synthetic-a'))
        self.put('case/AUTHENTICATED-SHARED-DAEMON.json', dict(peer=dict(pid=202, birth=[3000, 4000])))
        self.put('case/restart/AUTHENTICATED-SHARED-DAEMON.json', dict(peer=dict(pid=303, birth=[5000, 6000])))

    def put(self, name, value):
        raw = (json.dumps(value) + '\n').encode()
        path = self.root / name
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_bytes(raw)
        if name in s.NATIVE_PROOF_FILES:
            self.captured[name] = raw

    def set_raw(self, raw):
        self.raw = raw
        path = self.root / self.raw_name
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_bytes(raw)
        self.pins = dict(source_generated_by_actual_native=True,
                         files={self.relative:dict(bytes=len(raw), sha256=s.sha(raw))})
        self.put('case/NATIVE-RAW-PINS.json', self.pins)

    def seal_and_read(self, maximum=None):
        key = os.urandom(32)
        env = {s.KEY_ENV:base64.b64encode(key).decode(), 'GITHUB_REPOSITORY':'lzhs1995/ccc',
               'GITHUB_RUN_ID':'999', 'GITHUB_RUN_ATTEMPT':'1', 'GITHUB_SHA':'synthetic-head'}
        with patch.object(s, 'runner_root', return_value=self.base), patch.dict(os.environ, env), \
             patch.object(s, 'MAX_DIAGNOSTICS', maximum if maximum is not None else s.MAX_DIAGNOSTICS):
            s.seal_diagnostics(self.root, self.output)
        metadata = json.loads((self.output / 'PRIVATE-DIAGNOSTICS.json').read_bytes())
        encrypted = (self.output / 'private-diagnostics.aesgcm').read_bytes()
        archive = s.unseal(encrypted, key, base64.b64decode(metadata['aad_base64']))
        with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
            contents = {name:zipped.read(name) for name in zipped.namelist()}
        return metadata, encrypted, contents

    def test_actual_seal_decrypt_preserves_exact_raw_and_named_evidence(self):
        metadata, encrypted, files = self.seal_and_read()
        self.assertEqual(files[self.raw_name], self.raw)
        for name in ('case/PROVIDER-REQUESTS.json', 'case/RESERVED-PROVIDER-LEDGER.json',
                     'case/RESERVED-ORIGINAL-ATTEMPT.json', 'case/AUTHENTICATED-SHARED-DAEMON.json',
                     'case/restart/AUTHENTICATED-SHARED-DAEMON.json'):
            self.assertEqual(files[name], (self.root / name).read_bytes())
        self.assertNotIn(b'PRIVATE_SYNTHETIC_DEVELOPER', encrypted)
        self.assertNotIn('PRIVATE_SYNTHETIC_DEVELOPER', json.dumps(metadata))
        self.assertEqual(metadata['plaintext_bytes_sealed'], sum(map(len, files.values())))
        self.assertLessEqual(metadata['plaintext_bytes_sealed'], 32 * 1024**2)
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o700)

    def test_new_goal_and_mask_diagnostics_are_exact_and_sealed(self):
        names=('case/GOAL-ADMISSION-OBSERVATIONS.jsonl','case/MASKED-RESET.json',
               'case/MASKED-CURRENT-TURN.json','case/MASK-HOST-REPLY.json',
               'case/MASK-INPUT-INTENT.json','case/WORKSPACE-CLOSE-REPLY.json')
        for name in names:
            self.put(name, {'private_trace':name, 'original_gate_result':False})
        meta, encrypted, files=self.seal_and_read()
        for name in names:
            self.assertEqual(files[name],(self.root/name).read_bytes())
            self.assertNotIn(b'original_gate_result',encrypted)
            self.assertEqual(next(v for v in meta['members'] if v['name']==name)['status'],'sealed')

    def test_wrong_hash_or_size_is_refused_not_sealed(self):
        for key, value in [('sha256', '0' * 64), ('bytes', len(self.raw) + 1)]:
            with self.subTest(key=key):
                changed = json.loads(json.dumps(self.pins));changed['files'][self.relative][key] = value
                self.put('case/NATIVE-RAW-PINS.json', changed)
                with self.assertRaises(ValueError):
                    s.native_rollout_diagnostic(self.root, self.captured, 100000)

    def test_pin_path_escape_duplicate_sid_and_link_refused(self):
        for name in ['sessions/../../escaped-' + self.sid + '.jsonl', 'sessions//bad-' + self.sid + '.jsonl']:
            changed = dict(source_generated_by_actual_native=True, files={name:self.pins['files'][self.relative]})
            self.put('case/NATIVE-RAW-PINS.json', changed)
            with self.assertRaises(ValueError):s.native_rollout_diagnostic(self.root, self.captured, 100000)
        changed = json.loads(json.dumps(self.pins));changed['files']['sessions/another-' + self.sid + '.jsonl'] = changed['files'][self.relative]
        self.put('case/NATIVE-RAW-PINS.json', changed)
        with self.assertRaises(ValueError):s.native_rollout_diagnostic(self.root, self.captured, 100000)
        self.put('case/NATIVE-RAW-PINS.json', self.pins)
        path = self.root / self.raw_name
        target = path.with_suffix('.original');path.rename(target);path.symlink_to(target)
        with self.assertRaises(ValueError):s.native_rollout_diagnostic(self.root, self.captured, 100000)

    def test_foreign_header_or_initial_prompt_refused_even_with_matching_sha(self):
        original = self.raw
        for before, after in [(self.sid, '00000000-0000-4000-8000-000000000002'), (self.nonce, 'b'*32)]:
            raw = original.replace(before.encode(), after.encode())
            self.set_raw(raw)
            with self.assertRaises(ValueError):s.native_rollout_diagnostic(self.root, self.captured, 100000)

    def test_matching_modern_native_user_and_legacy_echo_count_once(self):
        turn = '00000000-0000-4000-8000-000000000003'
        message = dict(type='response_item', payload=dict(type='message', role='user', id='m1',
            content=[dict(type='input_text', text=self.prompt)],
            internal_chat_message_metadata_passthrough=dict(turn_id=turn, content_item_kinds=['user.text'],
                content_item_metadata=[dict(provenance=dict(type='user'))])),
            metadata=dict(client_authored=False, user_input_order=0,
                retained_source=dict(complete=True, id=dict(role='user', message_id='m1', turn_id=turn))))
        self.set_raw(self.raw + (json.dumps(message)+'\n').encode())
        row, data = s.native_rollout_diagnostic(self.root, self.captured, 100000)
        self.assertEqual(data, self.raw);self.assertTrue(row['synthetic_provenance_verified'])

    def test_actual_total_limit_records_raw_omission_without_read(self):
        fixed = sum((self.root / name).stat().st_size for name in s.DIAGNOSTICS if (self.root / name).exists())
        metadata, encrypted, files = self.seal_and_read(fixed + len(self.raw) - 1)
        self.assertNotIn(self.raw_name, files)
        self.assertEqual(next(r for r in metadata['members'] if r['name'] == self.raw_name)['status'], 'omitted_size_bound')
        self.assertEqual(metadata['plaintext_bytes_sealed'], fixed)
        self.assertLessEqual(metadata['plaintext_bytes_sealed'], metadata['plaintext_total_limit'])

    def test_bad_provenance_still_seals_other_diagnostics_with_explicit_refusal(self):
        self.put('case/terminal-owner/CLIENT-0.json', dict(self.client, pid=999))
        metadata, encrypted, files = self.seal_and_read()
        self.assertNotIn(self.raw_name, files)
        self.assertIn('case/PROVIDER-REQUESTS.json', files)
        self.assertEqual(metadata['members'][-1]['status'], 'refused_unstable_unsafe_or_unproven')

    def test_existing_private_output_accepts_and_preserves_public_evidence(self):
        self.output.mkdir(mode=0o700)
        (self.output / 'EXPORT-MANIFEST.json').write_bytes(b'original-public-projection')
        self.seal_and_read()
        self.assertEqual((self.output / 'EXPORT-MANIFEST.json').read_bytes(), b'original-public-projection')

    def test_external_or_symlink_or_runner_root_output_refused(self):
        outside = self.base.parent / ('outside-' + self.base.name)
        linked = self.base / 'linked';linked.symlink_to(self.root, target_is_directory=True)
        parent_link = self.base / 'parent-link';parent_link.symlink_to(self.root, target_is_directory=True)
        for output in (outside, linked, self.base, parent_link/'nested'):
            with self.subTest(output=str(output)), self.assertRaises(ValueError):
                s.diagnostic_output(self.base, output)
        self.assertFalse(outside.exists());self.assertFalse((self.root / 'nested').exists())


if __name__ == '__main__':unittest.main(verbosity=2)
