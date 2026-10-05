"""Bind a closed activation without submission settlement; never certify success.

This reader grants no input or subsequent admission. A complete, original UI
commit and delivery record are required; incomplete/corrupt evidence stays open.
"""
import os
from pathlib import Path

from ccc_native_standby import COUNT, identifier
from ccc_standby_timing import ActivationTiming, _elapsed, _read, _sha


def evidence(config_path, job_id):
    import ccc_workspace_batch as batch
    directory = batch.job_path(Path(config_path), job_id).parent / 'standby'
    settlement = directory / 'submission-settled.json'
    if os.path.lexists(settlement):
        raise ValueError('submission settlement requires original job terminal path')
    receipt, receipt_raw = _read(directory / 'activation-ui.json')
    origin = receipt['origin']
    timing = ActivationTiming(config_path, job_id, origin,
                              current_hashes=lambda: origin['source_hashes'])
    values, binding = timing._binding()
    if (receipt.get('version') != 2 or receipt.get('kind') != 'standby_activated'
            or receipt.get('new_activation') is not True
            or any(receipt.get(k) != v for k, v in binding.items())
            or receipt.get('originals') != values['originals']
            or receipt.get('event', {}).get('phase') != 'standby_activated'
            or receipt.get('committed_monotonic') != values['activation']['committed_monotonic']):
        raise ValueError('partial activation UI differs from original activation')
    _elapsed(origin['events'][-1], receipt['event'], binding['boot_id'])
    if not origin['events'][-1]['monotonic'] <= receipt['committed_monotonic'] <= receipt['event']['monotonic']:
        raise ValueError('partial activation commit outside UI interval')
    delivery, delivery_raw = _read(directory / 'delivery-results.json')
    rows = delivery.get('outcomes')
    if (any(delivery.get(k) != binding[k] for k in ('action_id', 'cohort_id', 'workspace_id', 'boot_id'))
            or delivery.get('native_task_acceptance_evaluated') is not False
            or not isinstance(rows, list) or len(rows) != COUNT
            or [r.get('index') for r in rows] != list(range(COUNT))
            or any(type(r.get('acknowledged')) is not bool for r in rows)
            or type(delivery.get('acknowledged_inputs')) is not int
            or delivery['acknowledged_inputs'] != sum(r['acknowledged'] for r in rows)):
        raise ValueError('partial activation delivery binding differs')
    pinned = [(timing.jobfile, timing._job_raw), (directory / 'activation-ui.json', receipt_raw),
              (directory / 'delivery-results.json', delivery_raw)]
    pinned += [(directory / (name + '.json'), raw) for name, raw in timing._inputs.items()]
    names = set(directory.glob('input-*.json'))
    expected = {directory / f'input-{i}.json' for i in range(COUNT)}
    if not names <= expected:
        raise ValueError('unexpected partial activation input claim')
    inputs, input_ids = [], set()
    for i, row in enumerate(rows):
        path = directory / f'input-{i}.json'
        if path not in names:
            if row['acknowledged']:
                raise ValueError('acknowledged input lacks original consumption')
            continue
        claim, raw = _read(path)
        input_id = identifier(claim['input_id'])
        if (input_id in input_ids or claim.get('action_id') != binding['action_id']
                or claim.get('original') != values['originals'][i]
                or claim.get('activation_sha256') != binding['evidence_sha256']['activation']
                or claim.get('prompt') != values['activation']['prompt']):
            raise ValueError('partial activation input differs from original consumption')
        input_ids.add(input_id)
        inputs.append(i)
        pinned.append((path, raw))
    # A terminal may be absent, but an existing one is retained byte-for-byte.
    for name in ('activation-terminal.json', 'invalidated.json'):
        path = directory / name
        if os.path.lexists(path):
            pinned.append((path, _read(path)[1]))
    if (os.path.lexists(settlement) or set(directory.glob('input-*.json')) != names
            or any(_read(p)[1] != b for p, b in pinned)):
        raise ValueError('partial activation evidence changed during read')
    return {'activated': True, **binding, 'originals': values['originals'],
            'event': receipt['event'], 'consumed_inputs': inputs,
            'acknowledged_inputs': delivery['acknowledged_inputs'],
            'submission_settled': False, 'native_task_acceptance_evaluated': False,
            'files': {str(p): _sha(b) for p, b in pinned}}
