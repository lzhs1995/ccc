"""Private acceptance timing only; every original call executes unchanged."""
import functools
import json
from pathlib import Path
import time


def run(initial, *args):
    import cmux_codex_watch as core
    import ccc_codex_queue as queue
    import ccc_private_check as private
    import ccc_native_lanes as lanes
    import ccc_guard_scope as scope
    settings = json.loads(initial)
    rows = []
    def wrap(cls, name):
        fn = getattr(cls, name)
        @functools.wraps(fn)
        def measured(self, *a, **kw):
            at = time.time()
            started_ns = time.perf_counter_ns()
            try:
                return fn(self, *a, **kw)
            finally:
                target = next((v for v in a if isinstance(v, dict) and 'surface_id' in v), {})
                sid = target.get('surface_id')
                if name in {'replay', 'send_text', 'send_key'} and len(a) >= 2:
                    sid = a[1]
                rows.append({'fn':cls.__name__+'.'+name,'at':at,'end':time.time(),'surface_id':sid,
                             'duration_ns':time.perf_counter_ns()-started_ns})
        setattr(cls, name, measured)
    for cls, names in [(core.WatchDaemon, ['_scheduled_native','_private_check_ready','_submit_native_draft','_save_delivery']),
                       (queue.QueueRecovery, ['current_turn', '_open_process_turn', '_idle_process_turn']),
                       (queue, ['process_placement_start', 'process_writable_files', 'task_snapshot']),
                       (scope, ['process', 'arguments']),
                       (private.PrivateChecks, ['select']),
                       (core.CmuxClient, ['replay','tree','workspace_tree','send_text','send_key'])]:
        for name in names:
            wrap(cls, name)
    try:
        return lanes.run_native_lane(initial, *args)
    finally:
        root = Path(settings['profiling_output'])
        root.mkdir(exist_ok=True)
        (root / ('lane-'+str(args[1])+'.json')).write_text(json.dumps(rows))
