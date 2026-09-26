"""Verify B can start in an empty private root without trusting an inherited cwd."""
from pathlib import Path
import shlex
import stat
import tempfile
import unittest
import uuid
from unittest.mock import Mock, patch

import ccc_workspace_batch as batch
import cmux_codex_watch as core


class BatchWorkingDirectoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ccc directory.with-dots-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.config = self.root / "ccc" / "config.json"
        self.job = {"id": str(uuid.uuid4()), "workspace_id": str(uuid.uuid4()),
                    "cwd_policy": batch.EMPTY_CWD_POLICY}
        self.job_dir = batch.job_path(self.config, self.job["id"]).parent
        self.job_dir.mkdir(parents=True)
        self.worker = batch.BatchWorker.__new__(batch.BatchWorker)
        self.worker.config_path = self.config
        self.worker.job = self.job

    def command(self, index=0):
        with patch("ccc_batch_guard.native_binary", return_value="/original/native/codex"):
            return batch.native_launch_argv(self.config, self.job, index)

    def test_new_batch_pins_the_private_working_directory_policy(self):
        store = core.ConfigStore(self.config)
        store.mutate(lambda c: c.update(mode="armed", global_paused=False))
        client = Mock()
        client.tree.return_value = {"windows": [{"id": "window", "workspaces": [{
            "id": self.job["workspace_id"], "ref": "workspace:1", "title": "selected", "panes": []}]}]}
        created = batch.start(self.config, self.job["workspace_id"], client=client, launch=False)
        saved = core.load_json(batch.job_path(self.config, created["job_id"]), {})
        self.assertEqual(saved["cwd_policy"], batch.EMPTY_CWD_POLICY)
        self.assertEqual(saved["initial_prompt"], batch.PROMPT)
        self.assertEqual(len(saved["slots"]), 50)
        repeated = batch.start(self.config, self.job["workspace_id"], client=client, launch=False)
        self.assertEqual(repeated["job_id"], created["job_id"])

    def test_launch_overrides_the_shell_cwd_and_only_trusts_that_exact_root(self):
        args = self.command(18)
        self.assertIn("--cd", args)
        actual = Path(args[args.index("--cd") + 1])
        self.assertEqual(actual, self.job_dir / "work" / "18")
        self.assertIn("/original/native/codex", args)
        config_values = [args[i + 1] for i, arg in enumerate(args[:-1]) if arg == "-c"]
        trust = next(value for value in config_values if value.startswith("projects="))
        self.assertIn(str(actual), trust)
        self.assertEqual(trust.count("trust_level"), 1)
        self.assertIn("projects={", trust)
        self.assertNotIn("projects.", trust)
        self.assertNotIn("--dangerously-bypass-hook-trust", args)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", args)
        self.assertNotIn("&&", args)

    def test_only_short_registration_command_is_sent_through_the_shell(self):
        slot = {"index": 18, "launch_id": str(uuid.uuid4())}
        command = self.worker._launch_command(slot)
        args = shlex.split(command)
        self.assertIn("--launch-native", args)
        self.assertNotIn("projects=", command)
        self.assertNotIn("sqlite_home=", command)
        self.assertNotIn("&&", args)
        self.assertLess(len(command.encode()), 1024)

    def test_slot_roots_are_empty_separate_and_outside_state_and_database(self):
        (self.job_dir / "job.json").write_text("historical state")
        database = batch.sqlite_home(self.config, self.job["id"], 0)
        database.mkdir()
        (database / "logs.sqlite").write_text("do not touch")
        first = batch.prepare_working_directory(self.config, self.job, 0)
        last = batch.prepare_working_directory(self.config, self.job, 49)
        self.assertNotEqual(first, last)
        self.assertEqual(list(first.iterdir()), [])
        self.assertEqual(list(last.iterdir()), [])
        self.assertNotIn(database, first.parents)
        self.assertEqual(stat.S_IMODE(first.stat().st_mode), 0o700)
        self.assertEqual((self.job_dir / "job.json").read_text(), "historical state")
        self.assertEqual((database / "logs.sqlite").read_text(), "do not touch")

    def test_repeated_preflight_reuses_the_same_directory(self):
        path = batch.prepare_working_directory(self.config, self.job, 0)
        inode = path.stat().st_ino
        self.assertEqual(batch.prepare_working_directory(self.config, self.job, 0), path)
        self.assertEqual(path.stat().st_ino, inode)

    def test_existing_files_are_preserved_and_never_automatically_trusted(self):
        path = batch.prepare_working_directory(self.config, self.job, 0)
        (path / "AGENTS.md").write_text("operator content")
        with self.assertRaisesRegex(RuntimeError, "not empty"):
            batch.prepare_working_directory(self.config, self.job, 0)
        self.assertEqual((path / "AGENTS.md").read_text(), "operator content")

    def test_symlink_to_an_operator_directory_is_rejected_before_creating_children(self):
        outside = self.root / "operator-project"
        outside.mkdir()
        (outside / "important.txt").write_text("keep")
        (self.job_dir / "work").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(RuntimeError):
            batch.prepare_working_directory(self.config, self.job, 0)
        self.assertEqual(sorted(p.name for p in outside.iterdir()), ["important.txt"])

    def test_a_symlinked_slot_is_not_accepted(self):
        outside = self.root / "empty-operator-project"
        outside.mkdir()
        (self.job_dir / "work").mkdir(mode=0o700)
        (self.job_dir / "work" / "0").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(RuntimeError):
            batch.prepare_working_directory(self.config, self.job, 0)
        self.assertEqual(list(outside.iterdir()), [])

    def test_writable_shared_parent_is_not_accepted(self):
        parent = self.job_dir / "work"
        parent.mkdir(mode=0o700)
        parent.chmod(0o777)
        with self.assertRaises(RuntimeError):
            batch.prepare_working_directory(self.config, self.job, 0)
        self.assertFalse((parent / "0").exists())

    def test_legacy_jobs_keep_their_original_context(self):
        self.job.pop("cwd_policy")
        self.assertIsNone(batch.prepare_working_directory(self.config, self.job, 0))
        self.assertEqual(batch.workspace_launch_context(self.config, self.job, 0), (None, []))
        self.assertNotIn("--cd", self.command())
        self.assertFalse((self.job_dir / "work").exists())

    def test_unknown_policy_does_not_silently_fall_back_to_the_user_home(self):
        self.job["cwd_policy"] = "unrecognized"
        with self.assertRaisesRegex(RuntimeError, "unknown"):
            batch.prepare_working_directory(self.config, self.job, 0)
        with self.assertRaisesRegex(RuntimeError, "unknown"):
            self.command()

    def test_invalid_indices_cannot_escape_the_batch_work_root(self):
        for index in (-1, 50, True, "../another", "0"):
            with self.subTest(index=index), self.assertRaises(RuntimeError):
                batch.working_directory(self.config, self.job["id"], index)


if __name__ == "__main__":
    unittest.main()
