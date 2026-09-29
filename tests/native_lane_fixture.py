"""Private lane acceptance: replace OS identity only; forbid actual cmux CLI."""
import json
from pathlib import Path


def run(initial, flags, lane_cell, commands, results):
    import ccc_codex_queue as native
    import ccc_native_lanes as lanes
    import cmux_codex_watch as core
    settings=json.loads(initial)
    root=Path(settings['config_path']).parent
    targets=json.loads((root/'fixture-targets.json').read_text())
    by_id={t['surface_id']:t for t in targets}
    def current_turn(self,target):
        item=by_id[target['surface_id']]
        return {**native.task_snapshot(Path(item['transcript']),item['session_id']),
                'pid':1,'process_start':1,'session_id':item['session_id']}
    def sources(self,selected):
        return [{'surface_id':t['surface_id'],'workspace_id':t['workspace_id'],
                 'session_id':by_id[t['surface_id']]['session_id'],
                 'path':Path(by_id[t['surface_id']]['transcript']),
                 'pid':1,'process_start':1,'identity_current':True} for t in selected]
    def forbidden(*args,**kwargs):
        raise AssertionError('private fixture must not contact the real cmux CLI')
    native.QueueRecovery.current_turn=current_turn
    native.QueueRecovery.wakeup_sources=sources
    native.process_placement_start=lambda pid,target:1 if pid==1 and target['surface_id'] in by_id else None
    core.CmuxClient._run=forbidden
    lanes.run_native_lane(initial,flags,lane_cell,commands,results)
