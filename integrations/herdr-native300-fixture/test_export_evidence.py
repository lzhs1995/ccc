import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import export_evidence as e

class ExportBoundary(unittest.TestCase):
    def test_old_source_screens_and_raw_logs_never_exported(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t).resolve()/'private';root.mkdir();out=Path(t).resolve()/'public'
            marker=b'PRIVATE_OLD_BUSINESS_SCREEN_DO_NOT_EXPORT'
            for name in ('pair/mac/source/verification/fixtures/old.txt','inputs/mac/screens.txt',
                         'guard-releases/release/SOURCE-ORIGINS.json','n/sessions/history.jsonl','h/.history','fixture.log'):
                p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(marker)
            (root/'RESULT.json').write_text(json.dumps({'standalone_case_passed':False,'full_contract_passed':False,'secret_extra':marker.decode()}))
            result=e.export_selected(root,out)
            self.assertFalse(result['raw_logs_exported']);self.assertFalse(result['old_controller_source_or_samples_exported'])
            for p in out.rglob('*'):
                if p.is_file():self.assertNotIn(marker,p.read_bytes(),str(p))
            self.assertTrue((out/'log-summaries/fixture.log.json').exists())
    def test_missing_evidence_is_explicit_not_fixture_pass(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t).resolve()/'private';root.mkdir();out=Path(t).resolve()/'public';result=e.export_selected(root,out)
            self.assertIn('RESULT.json',result['missing'])
            self.assertNotIn('passed',result);self.assertTrue(result['native_fixture_passed_not_inferred'])
    def test_symlink_and_hardlink_to_old_history_refused(self):
        with tempfile.TemporaryDirectory() as t:
            base=Path(t).resolve();root=base/'private';root.mkdir();old=base/'old';old.write_text('PRIVATE_OLD')
            (root/'RESULT.json').symlink_to(old)
            result=e.export_selected(root,base/'public')
            self.assertIn('RESULT.json',[r['path'] for r in result['rejected']])
            (root/'RESULT.json').unlink();import os;os.link(old,root/'RESULT.json')
            with self.assertRaisesRegex(ValueError,'unsafe'):e.read_exact(root,'RESULT.json')
    def test_error_content_is_only_hashed(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t).resolve()/'private';root.mkdir();out=Path(t).resolve()/'public'
            (root/'FAILURE.json').write_text(json.dumps({'type':'ValueError','error':'PRIVATE_BUSINESS_LINE'}))
            (root/'OWNED-CLEANUP.json').write_text(json.dumps({'phases':[{'phase':'guard','passed':False,'error':'PRIVATE_BUSINESS_LINE'}],'passed':False}))
            e.export_selected(root,out)
            for p in out.rglob('*'):
                if p.is_file():self.assertNotIn(b'PRIVATE_BUSINESS_LINE',p.read_bytes())
    def test_unproved_whole_native_history_never_copied(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t).resolve()/'private';root.mkdir();out=Path(t).resolve()/'public'
            (root/'ORIGINAL-NATIVE-TRANSCRIPT.jsonl').write_text('PRIVATE_OLD')
            (root/'STANDALONE-EVIDENCE.json').write_text(json.dumps({'passed':True,'schema':'wrong'}))
            result=e.export_selected(root,out)
            self.assertIn('STANDALONE-EVIDENCE.json',[r['path'] for r in result['rejected']])
            self.assertFalse((out/'ORIGINAL-NATIVE-TRANSCRIPT.jsonl').exists())
    def test_fresh_output_is_once(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t).resolve()/'private';root.mkdir();out=Path(t).resolve()/'public';e.export_selected(root,out)
            with self.assertRaises(FileExistsError):e.export_selected(root,out)

if __name__=='__main__':unittest.main(verbosity=2)


class DiagnosticNamesRegression(unittest.TestCase):
    def test_unique_sealed_names_and_failed_native_capture(self):
        import secure_bundle
        self.assertEqual(len(secure_bundle.DIAGNOSTICS),len(set(secure_bundle.DIAGNOSTICS)))
        self.assertIn('FAILURE-NATIVE-TRANSCRIPT.jsonl',secure_bundle.DIAGNOSTICS)
        self.assertIn('FAILURE-NATIVE-CAPTURE.json',secure_bundle.DIAGNOSTICS)
