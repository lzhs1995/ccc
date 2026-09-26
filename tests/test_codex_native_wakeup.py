"""Native completions accelerate observation without granting input permission."""
import ctypes
import json
from pathlib import Path
import tempfile
import subprocess
import threading
import time
import unittest
from unittest.mock import patch

from ccc_codex_queue import NativeCompletionWatcher, QueueRecovery, _BsdInfo, codex_process_starts, process_matches


class NativeWakeupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "original.jsonl"
        self.path.write_text("")
        self.sources = [{"surface_id": "surface", "workspace_id": "workspace",
                         "session_id": "session", "path": self.path}]
        self.woken = []
        self.watcher = NativeCompletionWatcher(lambda: self.sources, self.wake)

    def wake(self, sid, wid):
        self.woken.append((sid, wid))
        return True

    def event(self, kind="task_complete", turn="turn", error=True):
        with self.path.open("a") as handle:
            handle.write(json.dumps({"type": "event_msg", "timestamp": "2026-09-22T10:00:00Z",
                "payload": {"type": kind, "turn_id": turn,
                            "error": {"message": "high demand"} if error else None}}) + "\n")

    def test_changed_file_wakes_failed_turn_once_and_unchanged_file_is_not_read(self):
        self.event()
        self.watcher.scan()
        self.assertEqual(self.woken, [("surface", "workspace")])
        with patch.object(Path, "open", side_effect=AssertionError("unchanged transcript reread")):
            self.watcher.scan()
        with self.path.open("a") as handle:
            handle.write(json.dumps({"type": "event_msg", "payload": {"type": "token_count"}}) + "\n")
        self.watcher.scan()
        self.assertEqual(len(self.woken), 1)
        self.event(turn="next-turn")
        self.watcher.scan()
        self.assertEqual(len(self.woken), 2)

    def test_started_aborted_successful_or_new_user_turn_never_wakes_old_failure(self):
        for kind in ("task_started", "turn_aborted", "user_message", "task_complete"):
            with self.subTest(kind=kind):
                self.path.write_text("")
                self.event()
                self.event(kind, turn="next", error=False)
                self.watcher.scan()
                self.assertEqual(self.woken, [])

    def test_hint_retries_when_surface_is_not_yet_scheduled(self):
        self.event()
        with patch.object(self.watcher, "wake", return_value=False):
            self.watcher.scan()
        self.watcher.scan()
        self.assertEqual(len(self.woken), 1)

    def test_bounded_tail_skips_large_or_incomplete_records(self):
        self.path.write_text(json.dumps({"type": "padding", "text": "x" * 200000}) + "\n")
        self.event()
        self.watcher.tail_bytes = 512
        self.watcher.scan()
        self.assertEqual(len(self.woken), 1)
        with self.path.open("a") as handle:
            handle.write('{"type":"event_msg","payload":')
        self.watcher.scan()
        self.assertEqual(len(self.woken), 1)

    def test_missing_source_does_not_block_other_surfaces(self):
        self.event()
        self.sources.insert(0, {**self.sources[0], "surface_id": "missing", "path": self.root / "gone"})
        self.watcher.scan()
        self.assertEqual(self.woken, [("surface", "workspace")])
        self.sources.clear()
        self.watcher.scan()
        self.assertEqual(self.watcher.signatures, {})

    def test_sources_use_enabled_workspace_uuid_and_latest_known_process(self):
        queue = QueueRecovery(self.root / "ledger", self.root / "bindings", self.root, "continue")
        queue.open_file_sources["surface"] = {**self.sources[0], "process_start": 20}
        records = {"old-session": {"surfaceId": "surface", "workspaceId": "workspace",
                                   "transcriptPath": str(self.root / "old.jsonl"), "pidStartSeconds": 10}}
        target = {"surface_id": "surface", "workspace_id": "workspace"}
        with patch.object(queue, "records", return_value=records):
            self.assertEqual(queue.wakeup_sources([target])[0]["session_id"], "session")
            for override in ({"paused": True}, {"enabled": False}, {"workspace_id": "other"}):
                self.assertEqual(queue.wakeup_sources([{**target, **override}]), [])
            queue.open_file_sources["surface"]["path"] = self.root.parent / "outside.jsonl"
            sources = queue.wakeup_sources([target])
            self.assertTrue(all(Path(s["path"]).is_relative_to(self.root.resolve()) for s in sources))

    def test_failed_observation_retries_cached_hint_until_native_turn_moves_on(self):
        now = [0.0]
        self.watcher.clock = lambda: now[0]
        self.watcher.retry_needed = lambda *args: True
        self.event()
        self.watcher.scan()
        now[0] = .5
        self.watcher.scan()
        self.assertEqual(len(self.woken), 1)
        now[0] = 1.1
        with patch.object(Path, "open", side_effect=AssertionError("unchanged transcript reread")):
            self.watcher.scan()
        self.assertEqual(len(self.woken), 2)
        self.event("task_started", turn="next", error=False)
        self.watcher.scan()
        now[0] = 10
        self.watcher.scan()
        self.assertEqual(len(self.woken), 2)

    def test_draft_or_confirmed_delivery_does_not_repeat_priority_work(self):
        now = [0.0]
        self.watcher.clock = lambda: now[0]
        self.watcher.retry_needed = lambda *args: False
        self.event()
        self.watcher.scan()
        now[0] = 10
        self.watcher.scan()
        self.assertEqual(len(self.woken), 1)

    def test_native_coverage_requires_fresh_matching_readable_source(self):
        now = [0.0]
        self.watcher.clock = lambda: now[0]
        self.sources[0]['identity_current'] = True
        target = {'surface_id': 'surface', 'workspace_id': 'workspace'}
        self.assertEqual(self.watcher.observation_interval(target, 1), 1)
        self.event('task_started', error=False)
        self.watcher.scan()
        self.assertEqual(self.watcher.observation_interval(target, 1), 10)
        self.assertEqual(self.watcher.observation_interval({**target, 'workspace_id': 'moved'}, 1), 1)
        now[0] = 1.1
        self.assertEqual(self.watcher.observation_interval(target, 1), 1)
        self.watcher.scan()
        self.assertEqual(self.watcher.observation_interval(target, 1), 10)
        self.sources[0]['identity_current'] = False
        self.watcher.scan()
        self.assertEqual(self.watcher.observation_interval(target, 1), 1)
        self.sources[0]['identity_current'] = True
        self.watcher.scan()
        self.path.unlink()
        self.watcher.scan()
        self.assertEqual(self.watcher.observation_interval(target, 1), 1)

    def test_malformed_or_unrecognized_tail_cannot_claim_native_coverage(self):
        self.path.write_text('not json\n')
        self.watcher.scan()
        self.assertEqual(self.watcher.coverage, {})
        self.path.write_text(json.dumps({'type': 'event_msg', 'payload': {'type': 'token_count'}})+'\n')
        self.watcher.scan()
        self.assertEqual(self.watcher.coverage, {})

    def test_coverage_is_published_before_the_rest_of_a_slow_scan(self):
        self.event('task_started', error=False)
        self.sources[0]['identity_current'] = True
        other = self.root / 'other.jsonl'
        other.write_bytes(self.path.read_bytes())
        self.sources.append({**self.sources[0], 'surface_id': 'other', 'path': other})
        entered, release = threading.Event(), threading.Event()
        original = Path.stat
        def stat(path, *args, **kwargs):
            if path == other:
                entered.set()
                release.wait(3)
            return original(path, *args, **kwargs)
        with patch.object(Path, 'stat', stat):
            thread = threading.Thread(target=self.watcher.scan)
            thread.start()
            try:
                self.assertTrue(entered.wait(1))
                self.assertEqual(self.watcher.observation_interval(
                    {'surface_id': 'surface', 'workspace_id': 'workspace'}, 1), 10)
            finally:
                release.set()
                thread.join(3)

    def test_measured_slow_scan_lease_is_bounded_and_failure_removes_it(self):
        now = [0.0]
        self.watcher.clock = lambda: now[0]
        self.sources[0]['identity_current'] = True
        def sources():
            now[0] += 3
            return self.sources
        self.watcher.sources = sources
        self.event('task_started', error=False)
        self.watcher.scan()
        target = {'surface_id': 'surface', 'workspace_id': 'workspace'}
        self.assertEqual(self.watcher.coverage_seconds, 5)
        now[0] = 5
        self.assertEqual(self.watcher.observation_interval(target, 1), 10)
        now[0] = 8.1
        self.assertEqual(self.watcher.observation_interval(target, 1), 1)
        self.path.unlink()
        self.watcher.scan()
        self.assertEqual(self.watcher.coverage, {})

    def test_unchanged_bound_paths_do_not_repeat_filesystem_resolution(self):
        queue = QueueRecovery(self.root / 'ledger', self.root / 'bindings', self.root, 'continue')
        records = {'session': {'surfaceId': 'surface', 'workspaceId': 'workspace', 'pid': 123,
            'pidStartSeconds': 100, 'transcriptPath': str(self.path)}}
        target = {'surface_id': 'surface', 'workspace_id': 'workspace'}
        with patch.object(queue, 'records', return_value=records), patch('ccc_codex_queue.codex_process_starts', return_value={123: 100}):
            first = queue.wakeup_sources([target])
            with patch.object(Path, 'resolve', side_effect=AssertionError('unchanged path resolved again')):
                self.assertEqual(queue.wakeup_sources([target]), first)
            replacement = self.root / 'next.jsonl'
            records['session']['transcriptPath'] = str(replacement)
            self.assertEqual(queue.wakeup_sources([target])[0]['path'], replacement.resolve())
            self.assertEqual(set(queue.wakeup_path_cache), {str(replacement)})

    def test_bound_process_coverage_survives_gui_refresh_but_rejects_pid_reuse(self):
        queue = QueueRecovery(self.root / 'ledger', self.root / 'bindings', self.root, 'continue')
        start = time.mktime(time.strptime('Tue Sep 22 08:00:00 2026', '%a %b %d %H:%M:%S %Y'))
        records = {'session': {'surfaceId': 'surface', 'workspaceId': 'workspace', 'pid': 123,
            'pidStartSeconds': start, 'transcriptPath': str(self.path)}}
        queue.process_lookup = lambda _: self.fail('native coverage waited on cmux GUI inventory')
        target = {'surface_id': 'surface', 'workspace_id': 'workspace'}
        with patch.object(queue, 'records', return_value=records), patch('ccc_codex_queue.codex_process_starts', return_value={123: start}) as run:
            self.assertTrue(queue.wakeup_sources([target])[0]['identity_current'])
            self.assertTrue(queue.wakeup_sources([target])[0]['identity_current'])
            self.assertEqual(run.call_count, 1)
            records['session']['pidStartSeconds'] = start - 10
            self.assertFalse(queue.wakeup_sources([target])[0]['identity_current'])

    def test_native_identity_checks_do_not_spawn_ps_and_reject_partial_or_foreign_records(self):
        def read(pid, flavor, arg, buffer, size):
            info = ctypes.cast(buffer, ctypes.POINTER(_BsdInfo)).contents
            info.pid, info.name, info.start_sec = pid, b'codex', 12345
            if pid == 2:
                return 0  # Process disappeared.
            if pid == 3:
                return size - 1  # Never trust an incomplete ABI response.
            if pid == 4:
                info.name = b'zsh'
            if pid == 5:
                info.pid = 6
            if pid == 6:
                info.status = 5  # Zombie.
            return size
        with patch('ccc_codex_queue._proc_pidinfo', side_effect=read), patch('ccc_codex_queue.subprocess.run', side_effect=AssertionError('ps must not run')):
            self.assertEqual(codex_process_starts([1, 2, 3, 4, 5, 6, -1, True, 2**40]), {1: 12345})
            self.assertTrue(process_matches({'pid': 1, 'pidStartSeconds': 12345}))
            self.assertFalse(process_matches({'pid': 1, 'pidStartSeconds': 12344}))
            self.assertFalse(process_matches({'pid': 2, 'pidStartSeconds': 12345}))

    def test_portable_identity_fallback_is_bounded_and_ignores_malformed_lines(self):
        reply = subprocess.CompletedProcess([], 0,
            '123 Tue Sep 22 08:00:00 2026 /opt/homebrew/bin/codex\n'
            'bad Tue Sep 22 08:00:00 2026 codex\n'
            '124 Tue Sep 22 08:00:00 2026 /bin/zsh\n', '')
        with patch('ccc_codex_queue._proc_pidinfo', None), patch('ccc_codex_queue.subprocess.run', return_value=reply) as run:
            self.assertEqual(set(codex_process_starts([123, 124])), {123})
            self.assertEqual(run.call_args.kwargs['timeout'], 1)
            run.side_effect = subprocess.TimeoutExpired('ps', 1)
            self.assertEqual(codex_process_starts([123]), {})


if __name__ == "__main__":
    unittest.main()
