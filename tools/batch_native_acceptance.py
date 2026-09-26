#!/usr/bin/env python3
"""Real B50 in one owned background cmux workspace, using only a loopback API.

Keeps automatic pause off, uses private native/CCC data, and closes only the
recorded fixture workspace. Existing Codex identities are checked, not signalled.
"""
import argparse
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import shlex
import tempfile
import threading
import time
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ccc_batch_guard as guard
import ccc_codex_queue as native
import ccc_guard_scope as scope
import ccc_guard_migration as migration
from ccc_scheduling import SnapshotCache, SnapshotClient
import ccc_workspace_batch as batch
import cmux_codex_watch as core
from tools.idle_session_native_acceptance import Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    assert not (output / 'result.json').exists(), 'keep previous acceptance evidence'
    assert not guard.AUTOMATIC_POOL_STOP and not guard.CONNECTION_CUT_ENABLED
    assert batch.COUNT == 50
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.requests = []
    server.fail_first = False
    threading.Thread(target=server.serve_forever, daemon=True).start()
    root = Path(tempfile.mkdtemp(prefix='ccc-b50-native-')).resolve()
    home = root / 'codex'
    home.mkdir()
    (home / 'sessions').mkdir()
    (home / 'config.toml').write_text(
        'model = "gpt-6-astra"\nmodel_provider = "local_fixture"\n'
        'approval_policy = "never"\nsandbox_mode = "read-only"\ncheck_for_update_on_startup = false\n'
        '[features]\nplugins = false\napps = false\nhooks = false\nskip_host_skill_discovery = true\n'
        '[model_providers.local_fixture]\nname = "Loopback fixture"\nwire_api = "responses"\n'
        f'base_url = "http://127.0.0.1:{server.server_port}/v1"\n'
        'requires_openai_auth = false\nsupports_websockets = false\nrequest_max_retries = 0\nstream_max_retries = 0\n'
        f'[projects.{json.dumps(str(root))}]\ntrust_level = "trusted"\n')
    config_path = root / 'ccc/config.json'
    config = core.default_config()
    config.update(mode='armed', global_paused=False, targets=[], workspace_rules=[])
    core.atomic_write_json(config_path, config)
    client = migration.cmux_client(config_path)
    original = scope.scan()
    record = {'phase': 'prepared', 'root': str(root), 'production_requests': 0,
              'automatic_pause': False, 'native_before': original}
    core.atomic_write_json(output / 'result.json', record)
    cache, worker, wid = None, None, None
    original_env = dict(os.environ)
    try:
        name = 'CCC B50 local acceptance ' + uuid.uuid4().hex[:12]
        record['fixture_name'] = name
        core.atomic_write_json(output / 'result.json', record)
        command = ['--json', '--id-format', 'both', 'new-workspace', '--name', name,
                   '--cwd', str(root), '--focus', 'false', '--command', '/bin/zsh']
        overrides = {'CODEX_HOME': str(home), 'NO_PROXY': '127.0.0.1,localhost', 'no_proxy': '127.0.0.1,localhost',
                     **{key: '' for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy',
                                            'all_proxy', 'OPENAI_API_KEY', 'OPENAI_BASE_URL', 'CODEX_SQLITE_HOME')}}
        for key, value in overrides.items():
            command += ['--env', key + '=' + value]
        # Metadata seeding runs in this process too. Do not copy the user's
        # native databases into the isolated fixture.
        os.environ.update(overrides)
        # Creation is one-shot. An uncertain acknowledgement is reconciled by
        # this unique title; it must never cause a second workspace creation.
        try:
            raw = client._run(command).stdout
            (output / 'create-response.txt').write_text(raw)
        except core.CmuxError as exc:
            (output / 'create-response.txt').write_text(str(exc))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            found = [w['id'] for win in client.tree().get('windows', []) for w in win.get('workspaces', []) if w.get('title') == name]
            if len(found) == 1:
                wid = found[0]
                break
            time.sleep(.2)
        assert wid, 'created workspace could not be uniquely confirmed'
        record.update(phase='created_fixture_workspace', workspace_id=wid)
        core.atomic_write_json(output / 'result.json', record)
        cache = SnapshotCache(workers=1)
        wrapped = SnapshotClient(client, cache)
        job = batch.start(config_path, wid, client=wrapped, launch=False)
        queue = native.QueueRecovery(root / 'ledger.json', root / 'missing-hooks', home / 'sessions', batch.PROMPT)
        worker = batch.BatchWorker(config_path, job['job_id'], client=wrapped, queue=queue)
        launch = worker._launch_command
        # Pin the provider environment again for every real B-created child;
        # shell startup files cannot route this fixture to a real account.
        worker._launch_command = lambda slot: shlex.join([
            '/usr/bin/env', *(key + '=' + value for key, value in overrides.items()),
            '/bin/sh', '-c', launch(slot)])
        started, last_report = time.monotonic(), 0.0
        with core.FileLock(worker.path.parent / 'worker.lock', timeout_sec=0):
            worker.job.update(worker_pid=os.getpid(), worker_version=batch.WORKER_VERSION)
            worker.save()
            while worker.step():
                elapsed = time.monotonic() - started
                if elapsed - last_report >= 5:
                    print(json.dumps({'seconds': round(elapsed, 2), **batch.counts(worker.job)}), flush=True)
                    last_report = elapsed
                assert elapsed < 240, 'B50 did not complete; retained job evidence'
                time.sleep(.5)
        record['job'] = worker.job
        core.atomic_write_json(output / 'job.json', worker.job)
        assert worker.job['status'] == 'complete' and batch.counts(worker.job)['started'] == 50, batch.counts(worker.job)
        assert not core.workspace_rule_by_id(worker.store.load(), wid).get('batch_start_holds')
        owned = [r for r in scope.scan() if r['environment_workspace_id'] == wid]
        assert len(owned) == 50 and len({s['session_id'] for s in worker.job['slots']}) == 50
        assert {r['pid'] for r in owned} == {s['pid'] for s in worker.job['slots']}
        assert all(scope.arguments(r['pid'])[1].get('CODEX_HOME') == str(home) for r in owned)
        assert all(scope.matches(r) for r in original), 'an existing identity changed during acceptance'
        deadline = time.monotonic() + 10
        while sum(not r['native_title'] for r in server.requests) < 50 and time.monotonic() < deadline:
            time.sleep(.1)
        time.sleep(1)
        primary = [r for r in server.requests if not r['native_title']]
        titles = [r for r in server.requests if r['native_title']]
        assert len(primary) == 50 and all(r['user_text'] == batch.PROMPT for r in primary), 'unexpected batch request'
        assert len(titles) <= 50 and all(r['user_text'].endswith(batch.PROMPT) for r in titles), 'unexpected title request'
        record.update(phase='passed', seconds=time.monotonic() - started, started=50,
                      local_requests=len(server.requests), primary_requests=len(primary), native_title_requests=len(titles),
                      original_native_retained=len(original), native_owned=owned)
        record.pop('job', None)
    except BaseException as exc:
        record.update(phase='failed', error=str(exc), local_requests=len(server.requests))
        if worker:
            core.atomic_write_json(output / 'job.json', worker.job)
        raise
    finally:
        os.environ.clear()
        os.environ.update(original_env)
        if wid:
            try:
                client._run(['close-workspace', '--workspace', wid], timeout=10)
                deadline = time.monotonic() + 10
                while any(r['environment_workspace_id'] == wid for r in scope.scan()) and time.monotonic() < deadline:
                    time.sleep(.2)
                record['owned_workspace_closed'] = not any(r['environment_workspace_id'] == wid for r in scope.scan())
            except (core.CmuxError, OSError, RuntimeError) as exc:
                record['cleanup_error'] = str(exc)
        if worker:
            worker.cache.close()
        if cache:
            cache.close()
        server.shutdown()
        server.server_close()
        core.atomic_write_json(output / 'requests.json', server.requests)
        core.atomic_write_json(output / 'result.json', record)
    print(json.dumps({k: v for k, v in record.items() if k not in {'native_before', 'native_owned'}}, indent=2))


if __name__ == '__main__':
    main()
