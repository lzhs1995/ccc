"""A fresh input guard cannot reuse a query that started before the guard."""
from concurrent.futures import ThreadPoolExecutor
import threading
import unittest

import cmux_codex_watch as core
from ccc_scheduling import FreshReader, SnapshotCache, TreeSnapshot


class FreshSnapshotTests(unittest.TestCase):
    def test_caller_arriving_during_rpc_requires_another_rpc(self):
        entered, release = threading.Event(), threading.Event()
        reader = FreshReader()
        def first():
            entered.set()
            self.assertTrue(release.wait(2))
            return 'before-user-move'
        try:
            with ThreadPoolExecutor(2) as pool:
                old = pool.submit(reader.request, first)
                self.assertTrue(entered.wait(1))
                new = pool.submit(reader.request, lambda:'after-user-move')
                with reader._condition:
                    self.assertTrue(reader._condition.wait_for(lambda:len(reader._pending)==1,1))
                release.set()
                self.assertEqual(old.result(2),'before-user-move')
                self.assertEqual(new.result(2),'after-user-move')
        finally:
            release.set()
            reader.close()

    def test_simultaneous_pending_guards_share_one_read(self):
        reader = FreshReader()
        count = []
        # Hold the condition so the entire group is queued before dispatch.
        # Futures are not fabricated: all callers use the real request method.
        gate = threading.Barrier(9)
        def call():
            gate.wait()
            return reader.request(lambda:count.append(1) or object())
        try:
            with ThreadPoolExecutor(8) as pool:
                futures=[pool.submit(call) for _ in range(8)]
                gate.wait()
                values=[f.result(2) for f in futures]
            self.assertLess(len(count),8)
            self.assertEqual(len({id(v) for v in values}),len(count))
        finally:
            reader.close()

    def test_failure_is_not_cached_for_next_fresh_guard(self):
        cache=SnapshotCache()
        def fail():
            raise core.CmuxError('controller unavailable')
        try:
            with self.assertRaises(core.CmuxError):
                cache.fresh('tree',fail)
            self.assertEqual(cache.fresh('tree',lambda:'new'),'new')
            self.assertEqual(cache.fresh('tree',lambda:'newer'),'newer')
        finally:
            cache.close()
        with self.assertRaises(RuntimeError):
            cache.fresh('tree',lambda:None)

    def test_distinct_endpoints_do_not_share_a_read(self):
        cache=SnapshotCache()
        try:
            with ThreadPoolExecutor(2) as pool:
                a=pool.submit(cache.fresh,('tree','a'),lambda:'a')
                b=pool.submit(cache.fresh,('tree','b'),lambda:'b')
                self.assertEqual((a.result(2),b.result(2)),('a','b'))
        finally:
            cache.close()

    def test_index_matches_full_traversal_and_returns_unaliased_row(self):
        raw={'windows':[{'id':'window','workspaces':[{'id':'workspace','panes':[{
            'id':'pane','surfaces':[{'id':'surface','ref':'surface:42','type':'terminal'}]}]}]}]}
        snapshot=TreeSnapshot(raw)
        for selector in ('surface','surface:42','42'):
            self.assertEqual(core.find_main_surface(snapshot,selector),core.find_main_surface(raw,selector))
        row=core.find_main_surface(snapshot,'surface')
        row['workspace_id']='moved'
        self.assertEqual(core.find_main_surface(snapshot,'surface')['workspace_id'],'workspace')
        with self.assertRaises(core.CmuxError):
            core.find_main_surface(snapshot,'missing')


if __name__=='__main__':
    unittest.main()
