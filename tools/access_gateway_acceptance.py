#!/usr/bin/env python3
"""Real loopback TLS/CONNECT pressure; never contacts a model service."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import resource
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
from ccc_access_gateway import Gateway, Upstream
from test_access_gateway import (ConnectFixture, UpstreamFixture, native_request,
                                 new_channel, tls_contexts)


async def run(count):
    # Change only this fixture's descriptor limit, never any system setting.
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    needed = max(soft, count * 50 * 10 + 256)
    if hard != resource.RLIM_INFINITY and needed > hard:
        raise RuntimeError('fixture descriptor limit cannot accommodate requested concurrency')
    resource.setrlimit(resource.RLIMIT_NOFILE, (needed, hard))
    started, usage_before = time.monotonic(), resource.getrusage(resource.RUSAGE_SELF)
    lag, stop, max_threads = [], asyncio.Event(), threading.active_count()
    async def heartbeat():
        nonlocal max_threads
        while not stop.is_set():
            expected = time.monotonic() + .01
            await asyncio.sleep(.01)
            lag.append(max(0, time.monotonic() - expected))
            max_threads = max(max_threads, threading.active_count())
    heartbeat_task = asyncio.create_task(heartbeat())
    with tempfile.TemporaryDirectory(prefix='ccc-access-pressure-') as root:
        server_tls, client_tls = tls_contexts(root)
        fixture = UpstreamFixture(count * 50)
        port = await fixture.start(server_tls)
        proxy = ConnectFixture(port)
        proxy_port = await proxy.start()
        channels = [new_channel(root, port, number=i) for i in range(count)]
        for i, channel in enumerate(channels):
            channel.upstream = Upstream(f'https://127.0.0.1:{port}/{i}/v1', 'gpt-6-astra',
                proxy_url=f'http://127.0.0.1:{proxy_port}', tls_context=client_tls, timeout=30)
            channel.cohort_timeout = 30
        fixture.modes[str(count - 1)] = 'error'
        gateway = Gateway({channel.budget.policy.job_id: channel for channel in channels})
        gateway_port = await gateway.start()
        try:
            async def wave(channel):
                return await asyncio.gather(*[native_request(gateway_port, channel, slot) for slot in range(50)])
            first = await asyncio.wait_for(asyncio.gather(*[wave(channel) for channel in channels]), 60)
            assert fixture.peak == count * 50, (fixture.peak, count * 50)
            assert all(status == 200 for rows in first[:-1] for status, _ in rows)
            assert all(status == 500 for status, _ in first[-1])
            first_requests = len(fixture.requests)
            # A completed workspace rejects every extra native submission.
            duplicates = await asyncio.wait_for(asyncio.gather(*[wave(c) for c in channels[:-1]]), 30)
            assert all(status == 409 for rows in duplicates for status, _ in rows)
            assert len(fixture.requests) == first_requests
            fixture.modes[str(count - 1)] = 'success'
            recovered = await asyncio.wait_for(wave(channels[-1]), 30)
            assert any(status == 200 for status, _ in recovered)
            assert all(status in (200, 409) for status, _ in recovered)
            # This job already dispatched its real first 50. During recovery,
            # the first completed check must block still-queued retries.
            assert count * 50 < len(fixture.requests) <= (count + 1) * 50
            assert all(channel.metrics['peak_active'] == 50 for channel in channels)
            gates = [g['gate_closed'] - g['response_observed'] for c in channels
                     for g in c.metrics['completion_gate_times']]
            assert max(gates) < 1, 'parser completion gate exceeded one second'
            usage_after = resource.getrusage(resource.RUSAGE_SELF)
            ordered = sorted(lag)
            return {'passed': True, 'jobs': count, 'native_cli_instances': 0,
                'real_simultaneous_upstream_http': fixture.peak,
                'actual_tls_connects': proxy.connections,
                'first_wave_requests': first_requests, 'total_upstream_requests': len(fixture.requests),
                'post_success_denied_without_upstream': (count - 1) * 50,
                'independent_failed_workspace_recovery': sum(status == 200 for status, _ in recovered),
                'recovery_queued_requests_denied': sum(status == 409 for status, _ in recovered),
                'max_parser_to_gate_ms': max(gates) * 1000,
                'event_loop_max_lag_ms': max(lag) * 1000,
                'event_loop_p99_lag_ms': ordered[int((len(ordered) - 1) * .99)] * 1000,
                'thread_peak': max_threads, 'storage_workers': 4,
                'duration_sec': time.monotonic() - started,
                'cpu_seconds': (usage_after.ru_utime + usage_after.ru_stime
                                - usage_before.ru_utime - usage_before.ru_stime),
                'max_rss_bytes_macos': usage_after.ru_maxrss,
                'production_requests': 0, 'scope': 'loopback HTTP/SSE over verified TLS and CONNECT',
                'source_sha256': {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                    for name in ('ccc_access_budget.py', 'ccc_access_gateway.py',
                                 'tests/test_access_gateway.py', 'tools/access_gateway_acceptance.py')}}
        finally:
            await gateway.close()
            await proxy.close()
            await fixture.close()
            stop.set()
            await heartbeat_task


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--jobs', type=int, default=20, choices=range(2, 21))
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    with args.output.open('x') as output:
        try:
            result = asyncio.run(run(args.jobs))
        except BaseException as error:
            output.write(json.dumps({'passed': False, 'error': type(error).__name__ + ': ' + str(error)}, indent=2))
            raise
        output.write(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result))
