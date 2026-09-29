#!/usr/bin/env python3
"""Read-only collector for a real TUI action and its original batch proof."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ccc_batch_timing as timing
import ccc_workspace_batch as batch


def collector_hash():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--action-id', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    runtime_before = timing.source_hashes()
    collector_before = collector_hash()
    trace_path = timing.path(args.config, args.action_id)
    trace_bytes = trace_path.read_bytes()
    trace = timing.validate(json.loads(trace_bytes))
    if trace['action_id'] != args.action_id:
        raise ValueError('UI timing action identity changed')
    created = [e for e in trace['events'] if e['phase'] == 'job_created']
    job = {}
    job_bytes = b''
    job_file = None
    if len(created) == 1:
        # job_path resolves only a canonical UUID under the configured batch root.
        import uuid
        job_id = str(uuid.UUID(created[0]['job_id']))
        job_file = batch.job_path(args.config, job_id)
        job_bytes = job_file.read_bytes()
        job = json.loads(job_bytes)
    result = timing.evaluate(trace, job, current_hashes=runtime_before)
    if trace_path.read_bytes() != trace_bytes or (job_file and job_file.read_bytes() != job_bytes):
        result['problems'].append('evidence_changed_during_collection')
        result.update(startup_passed=False, evidence_complete=False, verdict='not_proven')
    runtime_after = timing.source_hashes()
    collector_after = collector_hash()
    if runtime_before != runtime_after or collector_before != collector_after:
        result['problems'].append('source_changed_during_collection')
        result.update(startup_passed=False, evidence_complete=False, verdict='not_proven')
    result.update(version=1, action_id=trace['action_id'], workspace_id=trace['workspace_id'],
                  mode=trace['mode'], job_id=job.get('id'),
                  boot_id=trace['events'][0].get('boot_id'), events=trace['events'],
                  trace_path=str(trace_path.resolve()),
                  trace_sha256=hashlib.sha256(trace_bytes).hexdigest(),
                  job_path=str(job_file.resolve()) if job_file else None,
                  job_sha256=hashlib.sha256(job_bytes).hexdigest() if job_file else None,
                  runtime_hashes_before=runtime_before, runtime_hashes_after=runtime_after,
                  collector_sha256_before=collector_before, collector_sha256_after=collector_after,
                  collected_at=timing.stamp())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x') as handle:
        handle.write(json.dumps(result, indent=2) + '\n')
    print(json.dumps({'startup_passed': result['startup_passed'], 'verdict': result['verdict'],
                      'output': str(args.output.resolve())}))
    return 0 if result['startup_passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
