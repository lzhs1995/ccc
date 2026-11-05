"""Offline acceptance-contract tests; no Rust/CLI/daemon/fixture execution."""
import ast
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location("combined_ci", HERE/"ci.py")
ci=importlib.util.module_from_spec(spec);spec.loader.exec_module(ci)
class MergeContractTests(unittest.TestCase):
    def setUp(self):
        self.value=json.loads((HERE/"freeze.json").read_bytes())
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
    def load_synthetic_frozen(self,value):
        # Test-only copy is never persisted as a source/build success receipt.
        value=copy.deepcopy(value);value["status"]="FROZEN_FOR_ISOLATED_BUILD"
        p=self.root/"synthetic-freeze.json";p.write_text(json.dumps(value))
        loaded=ci.load_freeze(p,ci.sha(p))
        ci.require_goal_merge_contract(loaded)
        return loaded
    def test_prepared_source_is_not_executable_frozen_contract(self):
        value=copy.deepcopy(self.value);value["status"]="PREPARED_AWAITING_AUTHENTICATED_GOAL_R2_ARTIFACT"
        p=self.root/"prepared.json";p.write_text(json.dumps(value))
        with self.assertRaises(ValueError):ci.load_freeze(p,ci.sha(p))
    def test_complete_exact115_profile_loads_only_synthetic_frozen_copy(self):
        v=self.load_synthetic_frozen(self.value)
        self.assertEqual(len(v["required_cases"]),115)
    def test_old102_460_shape_is_rejected(self):
        self.value["required_cases"]=self.value["required_cases"][:102]
        self.value["generated_schema_files"]={k:v for k,v in self.value["generated_schema_files"].items() if not k.startswith("v2/ThreadGoalRecover")}
        with self.assertRaises(ValueError):self.load_synthetic_frozen(self.value)
    def test_missing_state_package_cannot_hide_six_required_cases(self):
        with patch.object(ci,"PACKAGES",tuple(p for p in ci.PACKAGES if p!="codex-state")):
            with self.assertRaises(ValueError):self.load_synthetic_frozen(self.value)
    def test_same_count_replaced_test_name_is_rejected(self):
        self.value["required_cases"][-1]["name"]="unrelated_passing_test"
        with self.assertRaises(ValueError):self.load_synthetic_frozen(self.value)
    def test_actual_appserver_integration_binary_all_not_library(self):
        row=next(r for r in self.value["required_cases"] if r["name"].startswith("suite::v2::thread_goal_empty_responses::conditional_goal_receipt"))
        self.assertEqual(row["binary"],"all");row["binary"]="codex_app_server"
        with self.assertRaises(ValueError):self.load_synthetic_frozen(self.value)
    def test_duplicate_test_cannot_substitute_required_case(self):
        self.value["required_cases"][-1]=copy.deepcopy(self.value["required_cases"][-2])
        with self.assertRaises(ValueError):self.load_synthetic_frozen(self.value)
    def test_native34_cannot_be_moved_to_workspace(self):
        next(r for r in self.value["required_cases"] if r["package"]==ci.NATIVE_PACKAGE)["package"]="codex-tui"
        with self.assertRaises(ValueError):self.load_synthetic_frozen(self.value)
    def test_new_state_role_or_new_flag_change_is_rejected(self):
        next(r for r in self.value["required_cases"] if r["package"]=="codex-state")["new"]=False
        with self.assertRaises(ValueError):self.load_synthetic_frozen(self.value)
    def test_same_count_schema_digest_substitution_is_rejected(self):
        n=next(iter(self.value["generated_schema_files"]));self.value["generated_schema_files"][n]="0"*64
        with self.assertRaises(ValueError):self.load_synthetic_frozen(self.value)
    def test_stable_schema_export_cannot_claim_experimental464(self):
        self.value["schema_export_argv"]=["app-server","generate-json-schema"]
        with self.assertRaises(ValueError):self.load_synthetic_frozen(self.value)
    def test_correct_numbers_without_named_contract_are_rejected(self):
        self.value.pop("acceptance_contract")
        with self.assertRaises(ValueError):self.load_synthetic_frozen(self.value)
    def test_fresh_junit_does_not_accept_missing_skipped_failed_or_duplicates(self):
        required=[r["name"] for r in self.value["required_cases"] if r["package"]!="ccc-native-client-foreground"]
        self.assertEqual(len(required),81)
        cases=['<testcase name="'+n+'"/>' for n in required]
        p=self.root/"junit.xml"
        for items in [cases[:-1],cases[:-1]+[cases[0]],cases[:-1]+[cases[-1].replace('/>','><skipped/></testcase>')],cases[:-1]+[cases[-1].replace('/>','><failure/></testcase>')]]:
            p.write_text('<testsuites>'+''.join(items)+'</testsuites>')
            with self.assertRaises(ValueError):ci.check_junit(p,required)
        p.write_text('<testsuites>'+''.join(cases)+'</testsuites>')
        self.assertEqual(ci.check_junit(p,required)["cases"],81)
    def test_original_build_main_and_transport_test_invocation_unchanged(self):
        before=ast.parse((HERE/"merge-test-inputs/ci-851.py").read_text())
        after=ast.parse((HERE/"ci.py").read_text())
        left={n.name:ast.dump(n) for n in before.body if isinstance(n,ast.FunctionDef)}
        right={n.name:ast.dump(n) for n in after.body if isinstance(n,ast.FunctionDef)}
        self.assertEqual({k:v for k,v in left.items() if k!="main"},{k:v for k,v in right.items() if k not in ("main","require_goal_merge_contract")})
        oldmain=next(n for n in before.body if isinstance(n,ast.FunctionDef) and n.name=="main")
        newmain=next(n for n in after.body if isinstance(n,ast.FunctionDef) and n.name=="main")
        call=[n for n in newmain.body if isinstance(n,ast.Expr) and isinstance(n.value,ast.Call) and isinstance(n.value.func,ast.Name) and n.value.func.id=="require_goal_merge_contract"]
        self.assertEqual(len(call),1)
        newmain.body.remove(call[0]);self.assertEqual(ast.dump(oldmain),ast.dump(newmain))
    def test_real_entry_rejects_old_profile_before_actions_or_commands(self):
        value=copy.deepcopy(self.value);value["status"]="FROZEN_FOR_ISOLATED_BUILD";value.pop("acceptance_contract")
        p=self.root/"old-profile.json";p.write_text(json.dumps(value))
        argv=["ci.py","--ccc",str(self.root),"--codex",str(self.root),"--freeze",str(p),"--freeze-sha256",ci.sha(p),"--output",str(self.root/"out"),"--validate-only"]
        with patch.object(ci.sys,"argv",argv),patch.object(ci,"require_github_runner") as host,patch.object(ci,"run") as command:
            with self.assertRaisesRegex(ValueError,"exact Goal merge"):ci.main()
            host.assert_not_called();command.assert_not_called()
        self.assertFalse((self.root/"out").exists())
if __name__=="__main__":unittest.main(verbosity=2)
