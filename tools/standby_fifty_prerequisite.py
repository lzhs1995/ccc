"""Reopen UI50 original terminal and causal timing before fleet admission."""
import hashlib
from pathlib import Path

from ccc_standby_timing import _read
from ccc_workspace_batch import PROMPT
from tools.standby_performance_replay import verify as replay_performance
from tools.standby_run_manifest import RunManifest
from tools.standby_run_terminal import verify as verify_terminal


def verify(path, *, check=lambda: None):
    path = Path(path)
    pins = {}
    def read(name):
        check()
        p = Path(name)
        value, raw = _read(p)
        if p in pins and pins[p] != raw:
            raise ValueError('UI50 original evidence changed')
        pins[p] = raw
        return value

    report = read(path)
    run = path.parent/'run'
    manifest = RunManifest(run)
    if manifest.plan['planned_sessions'] != 50 or len(manifest.plan['batches']) != 1:
        raise ValueError('exact original UI50 run required')
    # A reader must not mint run bindings as a side effect.
    read(run/'run-bindings.json')
    bindings = manifest.resolve()
    binding = bindings['batches'][0]['binding']
    terminal = verify_terminal(run)
    if (terminal.get('succeeded') is not True or terminal.get('run_terminal') is not True
            or terminal != report.get('run_verification')):
        raise ValueError('UI50 successful original terminal changed or missing')
    run_record = read(terminal['terminal_path'])
    if len(run_record['batches']) != 1 or run_record['batches'][0]['batch_id'] != binding['batch_id']:
        raise ValueError('UI50 terminal batch differs from original')
    job = read(run_record['batches'][0]['terminal_path'])
    completion = read(job['completion']['observation_path'])
    ui_path = Path(binding['activation_ui_path'])
    ui = read(ui_path)
    slots = completion['slots']
    if (len(slots) != 50 or [r['index'] for r in slots] != list(range(50))
            or any(type(r['index']) is not int for r in slots)
            or completion['failed_rounds'] != 1 or completion['action_id'] != binding['action_id']
            or [r['original'] for r in slots] != ui['originals']):
        raise ValueError('UI50 completion not bound to original activation')
    witnesses = {r['index']: r for r in ui['originals']}
    # Reconstruct the replay input only from the terminal-bound originals.
    evidence = {}
    for p in (ui_path, ui_path.with_name('activation-terminal.json'), path.parent/'rpc-responses.ndjson'):
        check()
        if p == path.parent/'rpc-responses.ndjson':
            # NDJSON is read and bounded again by the performance verifier.
            with p.open('rb') as stream:
                raw = stream.read(32*1024*1024+1)
            if len(raw) > 32*1024*1024:
                raise ValueError('UI50 RPC evidence exceeds bound')
        else:
            read(p)
            raw = pins[p]
        evidence[str(p)] = hashlib.sha256(raw).hexdigest()
    result = dict(startup=report['startup_timing'], chains=report['recovery_chains'],
        evidence_sha256=evidence,
        transcript_bindings=[dict(index=r['index'], original=r['transcript'],
            saved=str(path.parent/('original-rollout-%02d.jsonl' % r['index'])),
            sha256=r['transcript_sha256']) for r in slots])
    proof = replay_performance(result, witnesses=witnesses,
        deliveries_path=path.parent/'original-deliveries.json', prompt=PROMPT, check=check)
    if proof['startup_passed'] is not True or proof['recovery_passed'] is not True:
        raise ValueError('UI50 original startup or recovery requirement failed')
    if verify_terminal(run) != terminal or manifest.resolve() != bindings:
        raise ValueError('UI50 terminal or binding changed during replay')
    manifest.current()
    for p in list(pins):
        read(p)
    from tools.standby_fleet_replay import recheck_dependencies
    recheck_dependencies([proof], check=check)
    return dict(original_ui50_replayed=True, terminal=terminal, performance=proof,
        evidence_sha256={str(p): hashlib.sha256(raw).hexdigest() for p, raw in pins.items()})
