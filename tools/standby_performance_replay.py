"""Replay saved cohort performance from bound original evidence; no live input."""
import hashlib
import json
from pathlib import Path

from ccc_standby_timing import evaluate as evaluate_startup
from tools.standby_recovery_chain import evaluate as evaluate_recovery


def verify(result, *, witnesses, deliveries_path, prompt, check=lambda: None):
    """Require exact fifty originals and reread all consumed bytes before return."""
    consumed = {}

    def read(path, expected=None):
        check()
        path = Path(path)
        if not path.is_absolute() or path.resolve(strict=True) != path or path.is_symlink():
            raise ValueError('canonical performance evidence required')
        with path.open('rb') as stream:
            raw = stream.read(32*1024*1024+1)
        if len(raw) > 32*1024*1024:
            raise ValueError('performance replay evidence exceeds bound')
        if expected is not None and hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError('performance evidence hash changed')
        if path in consumed and consumed[path] != raw:
            raise ValueError('performance evidence changed during replay')
        consumed[path] = raw
        check()
        return raw

    if (set(witnesses) != set(range(50)) or len(result['transcript_bindings']) != 50
            or len(result['chains']) != 50):
        raise ValueError('exact fifty original performance bindings required')
    evidence = result['evidence_sha256']
    if len(evidence) != 3:
        raise ValueError('exact timing and RPC evidence required')
    by_name = {Path(p).name: read(p, sha) for p, sha in evidence.items()}
    if set(by_name) != {'activation-ui.json', 'activation-terminal.json', 'rpc-responses.ndjson'}:
        raise ValueError('original timing and RPC evidence names required')
    startup = evaluate_startup(json.loads(by_name['activation-ui.json']),
                               json.loads(by_name['activation-terminal.json']))
    responses = [json.loads(row) for row in by_name['rpc-responses.ndjson'].splitlines()]
    deliveries = json.loads(read(deliveries_path))
    chains, seen, identities = [], set(), set()
    for binding in result['transcript_bindings']:
        index = binding['index']
        if type(index) is not int or index not in witnesses or index in seen:
            raise ValueError('duplicate or foreign original transcript index')
        seen.add(index)
        witness = witnesses[index]
        identity = (witness['session_id'], witness['surface_id'])
        if any(identity[0] == s or identity[1] == u for s, u in identities):
            raise ValueError('duplicate original session or surface')
        identities.add(identity)
        saved = read(binding['saved'], binding['sha256'])
        if read(binding['original'], binding['sha256']) != saved:
            raise ValueError('saved transcript differs from original')
        records = [json.loads(row) for row in saved.splitlines()]
        chains.append(evaluate_recovery(records, witness, responses,
                      deliveries[witness['surface_id']], prompt))
    if startup != result['startup'] or chains != result['chains']:
        raise ValueError('saved performance differs from original evidence replay')
    for path, raw in list(consumed.items()):
        if read(path) != raw:
            raise ValueError('performance evidence changed during replay')
    check()
    return {'replayed_originals': 50, 'evidence_sha256': {
        str(p): hashlib.sha256(raw).hexdigest() for p, raw in consumed.items()},
        'startup_passed': startup['startup_passed'],
        'recovery_passed': all(c['causal_chain_verified'] is True
                              and c['performance_passed'] is True for c in chains),
        'scope': 'Timing and causal recovery replay only; not full fleet acceptance.'}
