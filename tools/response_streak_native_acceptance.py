#!/usr/bin/env python3
"""Real native Codex, private loopback SSE, no production surfaces or credentials."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ccc_batch_guard as guard
import cmux_codex_watch as core
from ccc_guard_transport import WebSocket
from tools.guard_native_acceptance import MockServer, rpc


async def case(enabled):
    server = MockServer(1, complete=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    saved = guard.CONNECTION_CUT_ENABLED, guard.AUTOMATIC_POOL_STOP
    guard.CONNECTION_CUT_ENABLED = guard.AUTOMATIC_POOL_STOP = enabled
    with tempfile.TemporaryDirectory(prefix='ccc-response-streak-') as temporary:
        root = Path(temporary)
        home = root / 'codex'
        home.mkdir()
        (home / 'config.toml').write_text(
            'model = "gpt-6-astra"\nmodel_provider = "local_acceptance"\n'
            'approval_policy = "never"\nsandbox_mode = "read-only"\ncheck_for_update_on_startup = false\n'
            '[features]\nplugins = false\napps = false\nhooks = false\nskip_host_skill_discovery = true\n'
            '[model_providers.local_acceptance]\nname = "Loopback fixture"\nwire_api = "responses"\n'
            f'base_url = "http://127.0.0.1:{server.server_port}/v1/0"\n'
            'requires_openai_auth = false\nsupports_websockets = false\n'
            'request_max_retries = 0\nstream_max_retries = 0\n')
        config_path = root / 'ccc/config.json'
        wid, sid, jid = str(uuid.uuid4()).upper(), str(uuid.uuid4()).upper(), str(uuid.uuid4())
        config = core.default_config()
        config.update(mode='armed', global_paused=False, workspace_rules=[{
            'workspace_id': wid, 'enabled': True, 'batch_guard': {'version': 1, 'origin_job_id': jid}}])
        core.atomic_write_json(config_path, config)
        core.atomic_write_json(config_path.parent / 'workspace-batches' / jid / 'job.json',
                               {'id': jid, 'workspace_id': wid, 'slots': [{'index': 0}]})
        async def membership(w, s):
            return w == wid and s == sid
        service = guard.GuardService(config_path, membership=membership)
        guard.private_directory(service.sockets)
        pool = endpoint = ws = None
        events = []
        try:
            await service.dispatch({'command': 'arm', 'workspace_id': wid})
            env = dict(os.environ, CODEX_HOME=str(home), NO_PROXY='127.0.0.1,localhost', no_proxy='127.0.0.1,localhost')
            for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy',
                        'OPENAI_API_KEY', 'OPENAI_BASE_URL', 'CODEX_SQLITE_HOME'):
                env.pop(key, None)
            result = await service.dispatch({'command': 'register', 'workspace_id': wid, 'surface_id': sid,
                'cwd': str(root), 'environment': env, 'frontend_pid': os.getpid(),
                'config_args': ['-c', 'sqlite_home=' + json.dumps(str(root / 'native-db'))]})
            pool = service.pools[wid]
            endpoint = pool.endpoints[sid]
            original_message = endpoint.native_message
            def observe(message):
                events.append(message)
                original_message(message)
            endpoint.native_message = observe
            original_pid, original_birth = endpoint.native.pid, endpoint.identity['birth']
            ws = await WebSocket.connect(result['endpoint'])
            await rpc(ws, 'initialize', {'clientInfo': {'name': 'ccc_response_acceptance', 'version': '1'},
                                         'capabilities': {'experimentalApi': True}}, 1)
            ws.write(json.dumps({'method': 'initialized'}))
            started = await rpc(ws, 'thread/start', {'cwd': str(root), 'modelProvider': 'local_acceptance',
                'baseInstructions': 'Reply OK. Do not use tools.', 'approvalPolicy': 'never', 'sandbox': 'read-only'}, 2)
            session = started['thread']['id']
            samples = []
            for index in range(1, 4):
                print(json.dumps({'fixture': enabled, 'starting_response': index}), flush=True)
                await rpc(ws, 'turn/start', {'threadId': session,
                    'input': [{'type': 'text', 'text': 'Reply OK.', 'text_elements': []}]}, index + 2)
                deadline = time.monotonic() + 45
                while endpoint.active or endpoint.awaiting_turn:
                    if time.monotonic() > deadline:
                        raise RuntimeError('native fixture turn did not complete')
                    await asyncio.sleep(.01)
                if endpoint.turn_error:
                    raise RuntimeError('native fixture turn failed: ' + str(endpoint.turn_error))
                if index < 3 or not enabled:
                    await asyncio.sleep(.04)
                    assert pool.phase == 'watching' and pool.trip is None
                    assert endpoint.native.returncode is None and endpoint.native.pid == original_pid
                    assert endpoint.identity['birth'] == original_birth
                samples.append({'response': index, 'session_id': endpoint.session_id,
                                'turn_id': endpoint.turn_id, 'phase': pool.phase, 'pid': original_pid})
            if enabled:
                if endpoint.evidence_task:
                    await endpoint.evidence_task
                assert pool.stop_task is not None, 'third complete answer did not qualify'
                await pool.stop_task
                await pool.pause_task
                assert pool.phase == 'stopped' and pool.trip['connected'] is True
                assert pool.trip['evidence']['consecutive_responses'] == 3
            else:
                assert not guard.blocked(config_path, wid) and pool.trip is None
            assert len(server.requests) == 3, 'the isolated fixture made duplicate model requests'
            assert {s['session_id'] for s in samples} == {session}
            return {'automatic_pause': enabled, 'samples': samples, 'local_requests': len(server.requests),
                    'phase': pool.phase, 'trip': pool.trip, 'pid': original_pid, 'birth': original_birth}
        except BaseException:
            print(json.dumps({'fixture': enabled, 'native_state': endpoint.summary() if endpoint else None,
                              'requests': len(server.requests), 'events': events[-15:]}, default=str), flush=True)
            raise
        finally:
            if endpoint and endpoint.native and endpoint.native.returncode is None:
                # This exact owned fixture child is the only process cleaned up.
                endpoint.park_requested = True
                endpoint.native.terminate()
                await endpoint.native.wait()
            if ws:
                ws.close()
            if endpoint and endpoint.socket_server:
                endpoint.socket_server.close()
                await endpoint.socket_server.wait_closed()
            if service.save_task:
                await service.save_task
            guard.CONNECTION_CUT_ENABLED, guard.AUTOMATIC_POOL_STOP = saved
            server.shutdown()
            server.server_close()


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    native = Path(guard.native_binary())
    result = {'native': str(native), 'native_sha256': hashlib.sha256(native.read_bytes()).hexdigest(),
              'disabled': await asyncio.wait_for(case(False), 180),
              'enabled_fixture_only': await asyncio.wait_for(case(True), 180)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'result': 'passed', 'output': str(args.output), 'local_requests': 6}))


if __name__ == '__main__':
    asyncio.run(main())
