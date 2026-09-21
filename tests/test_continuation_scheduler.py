"""Exercise the production scheduler, not a serial fake-client bypass."""
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

from ccc_scheduling import CoalescingWriter, SnapshotCache, SnapshotClient, SurfaceScheduler


def targets(count):
    return [{"surface_id": str(i), "workspace_id": "workspace", "enabled": True} for i in range(count)]


class SchedulerTests(unittest.TestCase):
    def pump(self, scheduler, items, predicate, *, generation=1):
        deadline = time.monotonic() + 4
        while not predicate() and time.monotonic() < deadline:
            scheduler.wakeup.clear()
            scheduler.tick(items, generation=generation)
            scheduler.wakeup.wait(0.005)
        self.assertTrue(predicate(), scheduler.snapshot())

    def test_slow_observation_does_not_block_next_tick_of_39_peers(self):
        release, entered = threading.Event(), threading.Event()
        counts, lock = {}, threading.Lock()
        now = [100.0]
        def observe(target, current):
            sid = target["surface_id"]
            with lock:
                counts[sid] = counts.get(sid, 0) + 1
            if sid == "0":
                entered.set()
                release.wait(4)
        scheduler = SurfaceScheduler(observe, lambda *args: self.fail("unexpected send"),
                                     observe_workers=8, clock=lambda: now[0])
        try:
            items = targets(40)
            self.pump(scheduler, items, lambda: len(counts) == 40)
            self.assertTrue(entered.is_set())
            for second in (101, 102):
                now[0] = second
                self.pump(scheduler, items, lambda: all(counts.get(str(i), 0) >= second - 99
                                                     for i in range(1, 40)))
            self.assertEqual(counts["0"], 1)
        finally:
            release.set()
            scheduler.close()

    def test_172_targets_progress_while_one_send_waits_and_capacity_is_bounded(self):
        release = threading.Event()
        sent, observed, active = set(), set(), set()
        lock = threading.Lock()
        maxima = {"observe": 0, "send": 0}
        def enter(sid, phase):
            with lock:
                self.assertFalse(any(item[0] == sid for item in active))
                active.add((sid, phase))
                maxima[phase] = max(maxima[phase], sum(p == phase for _, p in active))
        def observe(target, current):
            sid = target["surface_id"]
            enter(sid, "observe")
            with lock:
                observed.add(sid)
                active.remove((sid, "observe"))
            return "error"
        def send(target, candidate, current):
            sid = target["surface_id"]
            enter(sid, "send")
            if sid == "0":
                release.wait(4)
            with lock:
                sent.add(sid)
                active.remove((sid, "send"))
        scheduler = SurfaceScheduler(observe, send, observe_workers=16, send_workers=4, clock=lambda: 100)
        try:
            self.pump(scheduler, targets(172), lambda: len(sent) == 171 and scheduler.snapshot()["sending"] == 1)
            self.assertEqual(len(observed), 172)
            self.assertNotIn("0", sent)
            self.assertLessEqual(maxima["observe"], 16)
            self.assertLessEqual(maxima["send"], 4)
            self.assertEqual(scheduler.snapshot()["sending"], 1)
        finally:
            release.set()
            scheduler.close()

    def test_pause_invalidates_inflight_observation_and_same_uuid_resume(self):
        entered, release = threading.Event(), threading.Event()
        sent, checks = [], []
        def observe(target, current):
            entered.set()
            release.wait(4)
            checks.append(current())
            return "error"
        scheduler = SurfaceScheduler(observe, lambda *args: sent.append(args), clock=lambda: 100)
        try:
            scheduler.tick(targets(1), generation=1)
            self.assertTrue(entered.wait(2))
            scheduler.tick([], generation=2)
            # The old worker must remain invalid even if the UUID is enabled again.
            scheduler.tick(targets(1), generation=3)
            release.set()
            self.pump(scheduler, [], lambda: bool(checks), generation=3)
            self.assertEqual(checks, [False])
            self.assertEqual(sent, [])
        finally:
            release.set()
            scheduler.close()

    def test_worker_exception_isolated_and_retry_keeps_interval(self):
        now, seen, errors = [100.0], [], []
        def observe(target, current):
            seen.append(target["surface_id"])
            if target["surface_id"] == "0":
                raise RuntimeError("one reader failed")
        scheduler = SurfaceScheduler(observe, lambda *args: None, clock=lambda: now[0],
                                     on_error=lambda target, phase, error: errors.append((target, phase)))
        try:
            self.pump(scheduler, targets(2), lambda: len(errors) == 1 and "1" in seen)
            for _ in range(3):
                scheduler.tick(targets(2), generation=1)
            self.assertEqual(seen.count("0"), 1)
            now[0] = 101
            self.pump(scheduler, targets(2), lambda: len(errors) == 2)
        finally:
            scheduler.close()


class SnapshotTests(unittest.TestCase):
    def test_process_classification_is_shared_and_tracks_replaced_workspace_snapshot(self):
        now, calls = [100.0], []
        class Client:
            def top(self, workspace_id):
                return {"generation": now[0]}
        cache = SnapshotCache(clock=lambda: now[0])
        def classify(top):
            calls.append(top["generation"])
            return {"label": top["generation"]}
        try:
            clients = [SnapshotClient(Client(), cache) for _ in range(40)]
            with ThreadPoolExecutor(8) as pool:
                values = list(pool.map(lambda c: c.process_labels("w", classify, wait=True), clients))
            self.assertEqual(values, [{"label": 100.0}] * 40)
            self.assertEqual(calls, [100.0])
            now[0] = 106
            self.assertEqual(clients[0].process_labels("w", classify, wait=True), {"label": 106})
            self.assertEqual(calls, [100.0, 106])
            self.assertEqual(len(cache._entries), 2)
        finally:
            cache.close()

    def test_concurrent_cold_workspace_uses_exactly_one_rpc(self):
        cache = SnapshotCache()
        entered, release = threading.Event(), threading.Event()
        calls = []
        def load():
            calls.append(1)
            entered.set()
            release.wait(4)
            return {"workspace": "same"}
        try:
            with ThreadPoolExecutor(8) as pool:
                futures = [pool.submit(cache.get, "same", load, ttl=5) for _ in range(8)]
                self.assertTrue(entered.wait(2))
                self.assertIsNone(cache.get("same", load, ttl=5, wait=False))
                release.set()
                self.assertTrue(all(f.result(3) == {"workspace": "same"} for f in futures))
            self.assertEqual(len(calls), 1)
        finally:
            release.set()
            cache.close()

    def test_observation_can_continue_while_process_refresh_waits(self):
        cache = SnapshotCache()
        entered, release = threading.Event(), threading.Event()
        def load():
            entered.set()
            release.wait(4)
            return {"ok": True}
        try:
            self.assertIsNone(cache.get("workspace", load, ttl=5, wait=False))
            self.assertTrue(entered.wait(2))
            # A send preflight's topology read is not queued behind that scan.
            self.assertEqual(cache.get("tree", lambda: {"tree": True}, ttl=1), {"tree": True})
        finally:
            release.set()
            cache.close()

    def test_failure_backoff_and_ttl_start_when_query_finishes(self):
        now, calls = [100.0], []
        cache = SnapshotCache(clock=lambda: now[0])
        def fail():
            calls.append(1)
            raise ValueError("transport failed")
        try:
            for _ in range(4):
                with self.assertRaises(ValueError):
                    cache.get("workspace", fail, ttl=5)
            self.assertEqual(len(calls), 1)
            now[0] = 102
            def slow():
                now[0] = 110
                return {"ok": True}
            self.assertEqual(cache.get("workspace", slow, ttl=5), {"ok": True})
            now[0] = 111
            self.assertEqual(cache.get("workspace", fail, ttl=5), {"ok": True})
            self.assertEqual(len(calls), 1)
        finally:
            cache.close()


class PersistenceTests(unittest.TestCase):
    def test_requests_during_io_coalesce_and_wait_for_the_next_durable_write(self):
        entered = [threading.Event(), threading.Event()]
        release = [threading.Event(), threading.Event()]
        writes = []
        def write():
            index = len(writes)
            writes.append(index)
            entered[index].set()
            if not release[index].wait(4):
                raise TimeoutError("test write was not released")
        writer = CoalescingWriter(write, delay=0)
        try:
            writer.request(wait=False)
            self.assertTrue(entered[0].wait(2))
            with ThreadPoolExecutor(8) as pool:
                futures = [pool.submit(writer.request) for _ in range(8)]
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    with writer._condition:
                        if len(writer._waiters) == 8:
                            break
                    entered[1].wait(0.001)
                release[0].set()
                self.assertTrue(entered[1].wait(2))
                self.assertTrue(all(not f.done() for f in futures))
                release[1].set()
                for future in futures:
                    future.result(2)
            self.assertEqual(len(writes), 2)
        finally:
            for event in release:
                event.set()
            writer.close()

    def test_persistence_failure_reaches_waiter_and_next_request_can_recover(self):
        writes = []
        def write():
            writes.append(1)
            if len(writes) == 1:
                raise OSError("disk unavailable")
        writer = CoalescingWriter(write, delay=0)
        try:
            with self.assertRaisesRegex(OSError, "disk unavailable"):
                writer.request()
            writer.request()
            self.assertEqual(len(writes), 2)
        finally:
            writer.close()


if __name__ == "__main__":
    unittest.main()
