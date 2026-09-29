#!/usr/bin/env python3
"""Exercise fleet scheduling, native transcripts and durable sends without APIs.

cmux reads/inputs and PID lookups are isolated fixtures. The production parser,
watcher, scheduler, transcript reader, authorization and state writer are real.
This does not launch terminals or send input to any production surface.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import uuid
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cmux_codex_watch as core
from ccc_codex_queue import NativeCompletionWatcher
from ccc_native_processes import NativeProcessIndex
from ccc_scheduling import CoalescingWriter
from tests.test_watch import grid_payload, visible_lines


def run(count):
    with tempfile.TemporaryDirectory(prefix="ccc-scale-") as directory:
        root = Path(directory)
        sessions = root / ".codex/sessions"
        sessions.mkdir(parents=True)
        targets, frames, transcripts, bindings, starts, processes = [], {}, {}, {}, {}, []
        paused, protected = set(), set()
        workspaces = {}
        now = time.time()
        for i in range(count):
            sid, wid, session = (str(uuid.uuid5(uuid.NAMESPACE_URL, value)).upper() for value in
                                 (f"ccc-scale-s-{i}", f"ccc-scale-w-{i // 50}", f"ccc-scale-thread-{i}"))
            target = {"surface_id": sid, "workspace_id": wid, "ref": f"surface:{i}",
                      "enabled": True, "paused": i < count // 8}
            targets.append(target)
            if target["paused"]:
                paused.add(sid)
            blocked = i in {count - 1, count - 2}
            if blocked:
                protected.add(sid)
            error = ("We’re currently experiencing high demand, which may cause temporary errors."
                     if i % 2 else "rate limit exceeded: Your requests to gpt-6-astra for gpt-6-astra in eastus2 have exceeded rate limit.")
            assert core._match_error_block("■ " + error) in {"rate_limit", "high_demand"}
            frame = grid_payload([], error=error, columns=180, composer="busy" if i == count - 1 else "placeholder")
            frame["render_grid"]["surface_id"] = sid
            frames[sid] = frame
            transcript = sessions / f"rollout-{session}.jsonl"
            event = {"type": "event_msg", "timestamp": datetime.fromtimestamp(now - 10, timezone.utc).isoformat(),
                     "payload": {"type": "task_started" if i == count - 2 else "task_complete",
                                 "turn_id": f"failed-{i}", "error": None if i == count - 2 else {"message": error}}}
            transcript.write_text(json.dumps({"type": "session_meta", "payload": {"id": session}}) + "\n" + json.dumps(event) + "\n")
            transcripts[sid] = transcript
            pid = 100000 + i
            starts[pid] = now - 100
            bindings[session] = {"surfaceId": sid, "workspaceId": wid, "pid": pid,
                                 "pidStartSeconds": starts[pid], "transcriptPath": str(transcript)}
            processes.append({"surface_id": sid, "environment_workspace_id": wid, "pid": pid})
            workspace = workspaces.setdefault(wid, {"id": wid, "ref": f"workspace:{i // 50}",
                "panes": [{"id": str(uuid.uuid5(uuid.NAMESPACE_URL, wid)), "surfaces": []}]})
            workspace["panes"][0]["surfaces"].append({"id": sid, "ref": target["ref"], "type": "terminal"})
        config = core.default_config()
        config.update(mode="armed", global_paused=False, claude_enabled=False, observe_workers=8, send_workers=8,
                      targets=targets, network_guard={"enabled": False},
                      workspace_rules=[{"workspace_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"historic-{i}")),
                                        "enabled": True, "excluded_surface_ids": []} for i in range(318)])
        core.atomic_write_json(root / "config.json", config)
        core.atomic_write_json(root / ".cmuxterm/codex-hook-sessions.json", {"sessions": bindings})
        release = threading.Event()
        sends, reads, sent_at, maxima = Counter(), Counter(), {}, {"observing": 0, "sending": 0}
        class Client:
            top_calls = 0
            def tree(self):
                return {"windows": [{"workspaces": list(workspaces.values())}]}
            def top_all(self):
                self.top_calls += 1
                release.wait(150)
                raise core.CmuxError("isolated slow GUI inventory")
            def read_viewport(self, wid, sid, **kwargs):
                reads[sid] += 1
                payload = frames[sid]
                return "\n".join(visible_lines(payload)), core.Grid.from_rpc(payload, sid)
            def send(self, wid, sid, message):
                assert sid not in paused | protected
                assert message == core.MESSAGE
                assert next(t["workspace_id"] for t in targets if t["surface_id"] == sid) == wid
                sends[sid] += 1
                assert sends[sid] == 1, "duplicate input for one original failed turn"
                sent_at[sid] = time.monotonic()
                with transcripts[sid].open("a") as out:
                    out.write(json.dumps({"type": "event_msg", "timestamp": datetime.now(timezone.utc).isoformat(),
                        "payload": {"type": "task_started", "turn_id": "continued"}}) + "\n")
                frame = grid_payload([], working=True)
                frame["render_grid"]["surface_id"] = sid
                frames[sid] = frame
        client = Client()
        daemon = core.WatchDaemon(root / "config.json", root / "state.json", client=client)
        daemon._native_process_index = NativeProcessIndex(loader=lambda: processes)
        daemon._state_writer = CoalescingWriter(daemon._save_now)
        scheduler = daemon._start_scheduler()
        native = NativeCompletionWatcher(daemon._native_wakeup_sources, scheduler.request_observation,
                                         retry_needed=daemon._native_retry_needed)
        scheduler.observation_interval = native.observation_interval
        daemon._native_process_index.start()
        expected = {t["surface_id"] for t in targets} - paused - protected
        started, last_churn, last_report = time.monotonic(), 0.0, 0.0
        first_scan_at = None
        previous_switch = sys.getswitchinterval()
        sys.setswitchinterval(min(previous_switch, .001))  # Same as WatchDaemon.run.
        try:
            with patch("ccc_codex_queue.codex_process_starts", side_effect=lambda pids: {p: starts[p] for p in pids if p in starts}):
                native.start()
                while time.monotonic() - started < 120:
                    elapsed = time.monotonic() - started
                    if elapsed - last_churn >= 1 and elapsed < 5:
                        daemon._mutate_config(lambda c: c["workspace_rules"][0].update(last_batch_id=str(uuid.uuid4())))
                        last_churn = elapsed
                    scheduler.wakeup.clear()
                    scheduler.tick(core.effective_targets(daemon.config, []), generation=daemon._observation_policy.key)
                    stats = scheduler.snapshot()
                    for key in maxima:
                        maxima[key] = max(maxima[key], stats[key])
                    if expected <= reads.keys() and first_scan_at is None:
                        first_scan_at = elapsed
                    if elapsed - last_report >= 5:
                        print(json.dumps({"seconds": round(elapsed, 2), "read_surfaces": len(reads),
                                          "sent_surfaces": len(sends), **stats}), flush=True)
                        last_report = elapsed
                    if expected == set(sends) and time.monotonic() - max(sent_at.values(), default=started) >= 2:
                        break
                    scheduler.wakeup.wait(scheduler.wait_timeout())
                assert set(sends) == expected, {"missing": len(expected - sends.keys()), "sent": len(sends)}
                assert all(v == 1 for v in sends.values()) and not (paused & reads.keys())
                assert maxima["observing"] <= 8 and maxima["sending"] <= 8
                assert client.top_calls == 1 and not release.is_set()
                report = {"passed": True, "fixture": "isolated cmux and PID I/O; real watcher/parser/native/durable state",
                          "surfaces": count, "active": count - len(paused), "sent_once": len(sends),
                          "paused_untouched": len(paused), "native_active_or_composer_veto": len(protected),
                          "first_fleet_scan_seconds": first_scan_at,
                          "all_sends_seconds": max(sent_at.values()) - started,
                          "elapsed_seconds": time.monotonic() - started,
                          "max_concurrency": maxima, "gui_process_requests": client.top_calls,
                          "network_requests": 0, "production_inputs": 0}
        finally:
            release.set()
            native.close()
            daemon._native_process_index.close()
            scheduler.close()
            daemon._process_snapshots.close()
            daemon._state_writer.close()
            sys.setswitchinterval(previous_switch)
        return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=843)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 16 <= args.count <= 1200:
        parser.error("count must be between 16 and 1200")
    result = run(args.count)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
