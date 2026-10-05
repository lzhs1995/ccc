"""Scheduler callbacks may wait for a surface; fleet progress must not wait."""
import logging
import threading
import unittest
from types import SimpleNamespace

from ccc_scheduling import SurfaceScheduler
from cmux_codex_watch import WatchDaemon


class CallbackIsolationTests(unittest.TestCase):
    def test_error_reporting_cannot_block_health_or_peer_observation(self):
        surface_lock = threading.RLock()
        entered, release, peer, health = (threading.Event() for _ in range(4))
        daemon = SimpleNamespace(_runtime_lock=threading.RLock(), runtime={},
            _surface_lock=lambda sid: surface_lock, logger=logging.getLogger("isolation"))
        def report(target, phase, error):
            entered.set()
            WatchDaemon._scheduled_error(daemon, target, phase, error)
        def observe(target, current):
            if target['surface_id'] == 'failed':
                raise RuntimeError('isolated failed read')
            peer.set()
        scheduler = SurfaceScheduler(observe, lambda *_: self.fail('unexpected send'),
            observe_workers=2, on_error=report, clock=lambda: 100)
        target = {'surface_id': 'failed', 'workspace_id': 'workspace'}
        observations = []
        readers = []
        def hold_surface():
            with surface_lock:
                release.set()
                if entered.wait(2):
                    def read_health():
                        scheduler.snapshot()
                        health.set()
                    reader = threading.Thread(target=read_health)
                    readers.append(reader)
                    reader.start()
                    observations.append(health.wait(.5))
        holder = threading.Thread(target=hold_surface, daemon=True)
        stop = threading.Event()
        def ticks():
            while not stop.is_set():
                scheduler.tick([target, {'surface_id': 'peer', 'workspace_id': 'workspace'}])
                stop.wait(.005)
        ticker = threading.Thread(target=ticks, daemon=True)
        holder.start()
        self.assertTrue(release.wait(1))
        ticker.start()
        # The holder releases its lock after the bounded observation so the
        # pre-fix negative run leaves no threads or executor jobs behind.
        holder.join(3)
        stop.set()
        ticker.join(2)
        for reader in readers:
            reader.join(2)
        scheduler.close()
        self.assertEqual(observations, [True], 'one surface error blocked fleet health snapshot')
        self.assertTrue(peer.is_set())

    def test_slow_dispatch_callback_does_not_block_tick_or_peer(self):
        entered, release, peer = (threading.Event() for _ in range(3))
        def dispatch(target, phase, lag):
            if target['surface_id'] == 'slow':
                entered.set()
                release.wait(2)
        scheduler = SurfaceScheduler(lambda target, _: peer.set() if target['surface_id'] == 'peer' else None,
            lambda *_: self.fail('unexpected send'), observe_workers=2, on_dispatch=dispatch)
        finished = threading.Event()
        def tick():
            scheduler.tick([{'surface_id': sid, 'workspace_id': 'workspace'} for sid in ('slow', 'peer')])
            finished.set()
        worker = threading.Thread(target=tick)
        try:
            worker.start()
            self.assertTrue(entered.wait(1))
            self.assertTrue(finished.wait(.5), 'dispatch callback blocked the fleet tick')
            self.assertTrue(peer.wait(.5))
        finally:
            release.set()
            worker.join(2)
            scheduler.close()

    def test_invalidated_read_does_not_report_error_or_send(self):
        entered, release = threading.Event(), threading.Event()
        errors, sent = [], []
        def observe(*args):
            entered.set()
            release.wait(2)
            raise RuntimeError('late invalidated read')
        scheduler = SurfaceScheduler(observe, lambda *_: sent.append(True),
            on_error=lambda *args: errors.append(args))
        try:
            scheduler.tick([{'surface_id': 'paused', 'workspace_id': 'workspace'}])
            self.assertTrue(entered.wait(1))
            scheduler.tick([])
            release.set()
            scheduler.close()
            self.assertEqual(errors, [])
            self.assertEqual(sent, [])
        finally:
            release.set()


if __name__ == '__main__':
    unittest.main()
