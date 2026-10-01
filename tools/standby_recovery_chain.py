"""Join original fixture transcript, forwarding ACK and durable CCC delivery.

Pure evidence evaluation; no process control or network calls.
"""
from datetime import datetime

from tools.native_acceptance_metrics import evaluate_native_completion


def evaluate(records, witness, responses, delivery, prompt):
    sid, surface = witness['session_id'], witness['surface_id']
    lifecycle = evaluate_native_completion(records, sid, 1)
    if not lifecycle['passed']:
        raise ValueError('original lifecycle incomplete')
    events = [r for r in records if r.get('type') == 'event_msg']
    starts = [r for r in events if r['payload'].get('type') == 'task_started']
    failed = next(r for r in events if r['payload'].get('type') == 'task_complete')
    if (failed['payload'].get('error') or {}).get('codex_error_info') != 'rate_limit_exceeded':
        raise ValueError('original failure is not terminal rate limit')
    failed_at = datetime.fromisoformat(failed['timestamp'].replace('Z', '+00:00')).timestamp()
    next_at = datetime.fromisoformat(starts[1]['timestamp'].replace('Z', '+00:00')).timestamp()
    inputs = [r for r in responses if r.get('request', {}).get('method') == 'terminal.paste'
              and r['request'].get('params', {}).get('surface_id') == surface
              and r['request']['params'].get('text') == prompt]
    if len(inputs) != 2:
        raise ValueError('expected exactly activation and recovery inputs')
    initial, recovery = sorted(inputs, key=lambda r: r['forward_monotonic_ns'])
    if not initial['forward_at'] < failed_at <= recovery['forward_at'] <= next_at:
        raise ValueError('recovery input not between final failure and new task')
    for row in inputs:
        request, reply = row['request'], row.get('reply', {})
        if (request['params'].get('submit_key') != 'enter' or reply.get('ok') is not True
                or reply.get('id') != request.get('id')):
            raise ValueError('original input ACK missing or mismatched')
    runtime = delivery.get('runtime', {})
    failed_turn = failed['payload']['turn_id']
    if (delivery.get('surface_id') != surface or runtime.get('send_count') != 1
            or runtime.get('delivery_status') not in {'accepted', 'confirmed'}
            or not runtime.get('send_attempt_id')
            or not runtime.get('codex_sent_turn_key', '').startswith(sid+':'+failed_turn+':')
            or not failed_at <= runtime.get('send_started_at', 0) <= recovery['forward_at']
            or not recovery['ack_at'] <= runtime.get('send_completed_at', 0)):
        raise ValueError('durable CCC attempt does not bind original recovery input')
    metrics = {'forward_ms': (recovery['forward_at']-failed_at)*1000,
               'ack_ms': (recovery['ack_at']-failed_at)*1000,
               'native_next_ms': (next_at-failed_at)*1000}
    return {'surface_id': surface, 'session_id': sid, 'failed_turn': failed_turn,
            'next_turn': starts[1]['payload']['turn_id'], 'input_id': recovery['request']['id'],
            'send_attempt_id': runtime['send_attempt_id'], 'input_count': 1,
            'causal_chain_verified': True, **metrics,
            'performance_passed': all(0 <= n < 1000 for n in metrics.values())}
