#!/usr/bin/env python3
"""Synthetic validator checks only. These are not Go execution evidence."""
import copy
import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from run_case import stop_owned
import summarize
from verify import PACKAGE, sha256, validate_events, validate_log, verify_package, verify_source


def event(action, name=None, output=None):
    value = {"Action": action, "Package": PACKAGE}
    if name is not None:
        value["Test"] = name
    if output is not None:
        value["Output"] = output
    return value


SPEC = {"expected_top_level": ["TestOne"], "count": 1}
RED_SPEC = dict(SPEC, expected_leaf="TestOne/success",
                expected_failure_message="real client received no complete ping")


def green(count=1):
    result = [event("start")]
    for _ in range(count):
        result += [event("run", "TestOne"), event("run", "TestOne/leaf"),
                   event("pass", "TestOne/leaf"), event("pass", "TestOne")]
    return result + [event("pass")]


def red():
    return [event("start"), event("run", "TestOne"), event("run", "TestOne/success"),
            event("output", "TestOne/success", "    fixture_test.go:75: real client received no complete ping\n"),
            event("fail", "TestOne/success"), event("fail", "TestOne"), event("fail")]


class EventTests(unittest.TestCase):
    def test_accept_green(self):
        self.assertTrue(validate_events(green(), 0, SPEC)["accepted"])

    def test_accept_race_exact_three(self):
        self.assertTrue(validate_events(green(3), 0, dict(SPEC, count=3))["accepted"])

    def test_accept_expected_red(self):
        self.assertTrue(validate_events(red(), 1, RED_SPEC, red=True)["accepted"])

    def test_reject_non_object(self):
        self.assertFalse(validate_events([None], 0, SPEC)["accepted"])

    def test_reject_non_json_stdout(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "test.jsonl"
            path.write_text("\n".join(json.dumps(e) for e in green()) + "\nnot json\n")
            self.assertFalse(validate_log(path, 0, SPEC)["accepted"])


def negative_case(name, edit, baseline=green, code=0, spec=SPEC, is_red=False):
    def test(self):
        events = copy.deepcopy(baseline())
        edited = edit(events)
        if isinstance(edited, list):
            events = edited
        result = validate_events(events, code, spec, red=is_red)
        self.assertFalse(result["accepted"], result)
    setattr(EventTests, "test_reject_" + name, test)


negative_case("zero_tests", lambda _: [event("start"), event("pass")])
negative_case("compile_failure", lambda _: [{"Action": "build-fail", "ImportPath": PACKAGE}], code=1)
negative_case("compile_failure_with_fake_pass", lambda e: e.insert(0, {"Action": "build-fail"}))
negative_case("nonzero_green", lambda e: e, code=1)
negative_case("skip", lambda e: e[3].update(Action="skip"))
negative_case("child_failure", lambda e: e[3].update(Action="fail"))
negative_case("wrong_root", lambda e: e[1].update(Test="TestOther"))
negative_case("missing_run", lambda e: e.pop(1))
negative_case("duplicate_run", lambda e: e.insert(2, event("run", "TestOne")))
negative_case("unfinished_child", lambda e: e.pop(3))
negative_case("duplicate_terminal", lambda e: e.insert(4, event("pass", "TestOne/leaf")))
negative_case("wrong_race_count", lambda e: e, spec=dict(SPEC, count=3))
negative_case("missing_package_terminal", lambda e: e.pop())
negative_case("duplicate_package_terminal", lambda e: e.append(event("pass")))
negative_case("missing_package_start", lambda e: e.pop(0))
negative_case("duplicate_package_start", lambda e: e.insert(0, event("start")))
negative_case("wrong_package", lambda e: e.append({"Action": "pass", "Package": "other"}))
negative_case("late_output", lambda e: e.append(event("output", output="late")))
negative_case("red_exit_zero", lambda e: e, baseline=red, code=0, spec=RED_SPEC, is_red=True)
negative_case("red_wrong_leaf", lambda e: e[2].update(Test="TestOne/other"), baseline=red, code=1, spec=RED_SPEC, is_red=True)
negative_case("red_message_missing", lambda e: e[3].update(Output="different error\n"), baseline=red, code=1, spec=RED_SPEC, is_red=True)
negative_case("red_extra_assertion", lambda e: e[3].update(Output=e[3]["Output"] + "    fixture_test.go:81: cleanup failed\n"), baseline=red, code=1, spec=RED_SPEC, is_red=True)
negative_case("red_panic", lambda e: e.insert(4, event("output", output="panic: unrelated\n")), baseline=red, code=1, spec=RED_SPEC, is_red=True)
negative_case("red_race", lambda e: e.insert(4, event("output", output="WARNING: DATA RACE\n")), baseline=red, code=1, spec=RED_SPEC, is_red=True)
negative_case("red_extra_passing_subtest", lambda e: e.__setitem__(slice(5,5), [event("run", "TestOne/extra"),event("pass", "TestOne/extra")]), baseline=red, code=1, spec=RED_SPEC, is_red=True)


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "source.go").write_text("package fixture\n")
        self.manifest = {"source_files": [{"path": "source.go", "sha256": sha256(self.root / "source.go")}]}

    def test_exact_source(self):
        self.assertEqual(verify_source(self.root, self.manifest), 1)

    def test_changed_source(self):
        (self.root / "source.go").write_text("changed\n")
        with self.assertRaises(ValueError):
            verify_source(self.root, self.manifest)

    def test_added_source(self):
        (self.root / "unexpected.go").write_text("extra\n")
        with self.assertRaises(ValueError):
            verify_source(self.root, self.manifest)

    def test_unsafe_path(self):
        self.manifest["source_files"][0]["path"] = "../source.go"
        with self.assertRaises(ValueError):
            verify_source(self.root, self.manifest)

    def test_package_pin_and_file_tamper(self):
        (self.root / "SOURCE-MANIFEST.json").write_text(json.dumps(self.manifest))
        (self.root / "PINNED-FILES.json").write_text(json.dumps({
            "SOURCE-MANIFEST.json": sha256(self.root / "SOURCE-MANIFEST.json")}))
        pin = sha256(self.root / "PINNED-FILES.json")
        self.assertEqual(verify_package(self.root, pin), self.manifest)
        (self.root / "SOURCE-MANIFEST.json").write_text("{}")
        with self.assertRaises(ValueError):
            verify_package(self.root, pin)

    def test_wrong_package_pin(self):
        (self.root / "PINNED-FILES.json").write_text("{}")
        with self.assertRaises(ValueError):
            verify_package(self.root, "0" * 64)


class CleanupTests(unittest.TestCase):
    @patch("run_case.os.killpg")
    def test_completed_child_never_signalled(self, kill):
        child = Mock(pid=123, poll=Mock(return_value=0), wait=Mock(return_value=0))
        self.assertEqual(stop_owned(child), 0)
        kill.assert_not_called()

    @patch("run_case.os.killpg")
    def test_running_owned_child_terminated(self, kill):
        child = Mock(pid=123, poll=Mock(return_value=None), wait=Mock(return_value=-15))
        self.assertEqual(stop_owned(child), -15)
        self.assertEqual(kill.call_count, 1)

    @patch("run_case.os.killpg", side_effect=ProcessLookupError)
    def test_race_child_exit_reaped(self, kill):
        child = Mock(pid=123, poll=Mock(side_effect=[None, 0]), wait=Mock(return_value=0))
        self.assertEqual(stop_owned(child), 0)

    @patch("run_case.os.killpg", side_effect=PermissionError("fixture"))
    def test_permission_error_not_success(self, kill):
        child = Mock(pid=123, poll=Mock(return_value=None))
        with self.assertRaises(PermissionError):
            stop_owned(child)

    @patch("run_case.os.killpg")
    def test_timeout_escalates_owned_child(self, kill):
        child = Mock(pid=123, poll=Mock(return_value=None),
                     wait=Mock(side_effect=[subprocess.TimeoutExpired("fixture", 10), -9]))
        self.assertEqual(stop_owned(child), -9)
        self.assertEqual(kill.call_count, 2)


class SummaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.specs = {"focused": SPEC, "compat": SPEC, "race": dict(SPEC, count=3), "red": RED_SPEC}
        self.manifest = {"toolchain": "go1.26.8", "original_local_sdk": "MacOSX26.5.sdk", "cases": self.specs}
        for name, spec in self.specs.items():
            folder = self.root / name
            folder.mkdir()
            events = red() if name == "red" else green(spec["count"])
            (folder / "test.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n")
            (folder / "stderr.log").write_text("")
            value = {"case": name, "status": "EXPECTED_RED" if name == "red" else "PASS",
                     "compiler_started": True, "stopped_for_disk_floor": False,
                     "runner_error": None, "source_matches_after": True,
                     "live_child_after": False, "package_pin": "fixture",
                     "returncode": 1 if name == "red" else 0,
                     "samples": [{"free_bytes": 40 * 2**30}, {"free_bytes": 40 * 2**30}],
                     "environment": {"go_version": "go1.26.8", "os": "Darwin", "arch": "arm64",
                                     "same_production_binary_claimed": False, "sdk_version": "26.5",
                                     "clang": "fixture clang", "os_release": "fixture OS",
                                     "original_local_sdk": "MacOSX26.5.sdk"},
                     "log_hashes": {n: sha256(folder / n) for n in ("test.jsonl", "stderr.log")}}
            (folder / "RESULT.json").write_text(json.dumps(value))

    def run_summary(self):
        with patch("summarize.verify_package", return_value=self.manifest), \
             patch("sys.argv", ["summarize.py", "--results", str(self.root), "--package-pin", "fixture"]), \
             contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as result:
            summarize.main()
        return result.exception.code

    def test_accept_four_complete_cases(self):
        self.assertEqual(self.run_summary(), 0)

    def test_reject_missing_case(self):
        (self.root / "race/RESULT.json").unlink()
        self.assertEqual(self.run_summary(), 1)

    def test_reject_altered_test_log(self):
        with (self.root / "focused/test.jsonl").open("a") as stream:
            stream.write("{}\n")
        self.assertEqual(self.run_summary(), 1)

    def test_reject_runner_failure_states(self):
        path = self.root / "focused/RESULT.json"
        original = json.loads(path.read_text())
        changes = {"stopped_for_disk_floor": True, "runner_error": "PermissionError",
                   "live_child_after": True, "source_matches_after": False,
                   "compiler_started": False, "cleanup_error": "fixture",
                   "samples": [], "environment": {}}
        for key, value in changes.items():
            with self.subTest(key=key):
                altered = dict(original, **{key: value})
                path.write_text(json.dumps(altered))
                self.assertEqual(self.run_summary(), 1)
        path.write_text(json.dumps(original))

    def test_reject_floor_and_type(self):
        path = self.root / "focused/RESULT.json"
        original = json.loads(path.read_text())
        for samples in ([{"free_bytes": 40 * 2**30}],
                        [{"free_bytes": 40 * 2**30}, {"free_bytes": 29 * 2**30}],
                        [{"free_bytes": 40 * 2**30}, {"free_bytes": "42949672960"}]):
            with self.subTest(samples=samples):
                path.write_text(json.dumps(dict(original, samples=samples)))
                self.assertEqual(self.run_summary(), 1)

    def test_reject_missing_sdk_metadata(self):
        path = self.root / "focused/RESULT.json"
        original = json.loads(path.read_text())
        for key in ("sdk_version", "clang", "os_release", "original_local_sdk"):
            with self.subTest(key=key):
                altered = copy.deepcopy(original)
                del altered["environment"][key]
                path.write_text(json.dumps(altered))
                self.assertEqual(self.run_summary(), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
