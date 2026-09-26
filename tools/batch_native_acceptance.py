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
    parser.add_argument('--mode', choices=('private-check', 'existing'), default='private-check',
                        help='existing verifies the default B after fixture-only folder trust')
    args = parser.parse_args()
    private_check = args.mode == 'private-check'
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
        + ('' if private_check else 'projects = {' + json.dumps(str(root)) + ' = {trust_level="trusted"}}\n') +
        '[tui]\nscreen_reader_detection_done = true\n'
        '[features]\nplugins = false\napps = false\nhooks = false\nskip_host_skill_discovery = true\n'
        '[model_providers.local_fixture]\nname = "Loopback fixture"\nwire_api = "responses"\n'
        f'base_url = "http://127.0.0.1:{server.server_port}/v1"\n'
        'requires_openai_auth = false\nsupports_websockets = false\nrequest_max_retries = 0\nstream_max_retries = 0\n')
    original_native_config = (home / 'config.toml').read_bytes()
    config_path = root / 'ccc/config.json'
    config = core.default_config()
    config.update(mode='armed', global_paused=False, targets=[], workspace_rules=[])
    core.atomic_write_json(config_path, config)
    client = migration.cmux_client(config_path)
    original = scope.scan()
    record = {'phase': 'prepared', 'root': str(root), 'production_requests': 0,
              'automatic_pause': False, 'native_before': original, 'startup_mode': args.mode}
    core.atomic_write_json(output / 'result.json', record)
    cache, worker, wid = None, None, None
    captured_waits = set()
    input_calls = []
    original_env = dict(os.environ)
    try:
        name = 'CCC B50 本地验证·约2分钟后自动清理 ' + uuid.uuid4().hex[:12]
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
        def scoped_call(method, workspace_position, surface_position=None):
            original_method = getattr(client, method)
            def checked(*values, **options):
                assert values[workspace_position] == wid, 'fixture input escaped its workspace'
                entry = {'at': time.time(), 'method': method, 'workspace_id': wid}
                if surface_position is not None:
                    entry['surface_id'] = values[surface_position]
                input_calls.append(entry)
                return original_method(*values, **options)
            setattr(client, method, checked)
        scoped_call('new_codex_surface', 1)
        for method in ('draft_batch_session_name', 'send_text', 'send_key'):
            scoped_call(method, 0, 1)
        cache = SnapshotCache(workers=1)
        wrapped = SnapshotClient(client, cache)
        options = {'private_check': True} if private_check else {}
        job = batch.start(config_path, wid, client=wrapped, launch=False, **options)
        expected_prompt = batch.PROMPT if private_check else batch.LEGACY_PROMPT
        queue = native.QueueRecovery(root / 'ledger.json', root / 'missing-hooks', home / 'sessions', expected_prompt)
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
                for slot in worker.job['slots']:
                    naming = slot.get('naming', {})
                    if (slot.get('surface_id') and slot.get('phase') != 'confirmed'
                            and naming.get('submitted_at') and not naming.get('confirmed_name')
                            and time.time() - naming['submitted_at'] > 10
                            and slot['index'] not in captured_waits):
                        # Capture the original stalled composer before fixture
                        # cleanup; do not guess, press keys, or replay input.
                        captured_waits.add(slot['index'])
                        wait = {'slot': dict(slot)}
                        try:
                            target = worker._target(slot, fresh=True)
                            wait['native'] = worker._native(target, slot)
                            wait['viewport'] = client.replay(wid, slot['surface_id'])
                            grid = core.Grid.from_rpc(wait['viewport'], slot['surface_id'])
                            wait['exact_draft'] = worker._own_prompt_draft(grid, naming['command'])
                            wait['screen_kind'] = core.classify_grid(grid).kind
                            wait['composer'] = core._composer_status(grid)
                        except (core.CmuxError, RuntimeError, OSError) as exc:
                            wait['inspection_error'] = str(exc)
                        core.atomic_write_json(output / f'waiting-slot-{slot["index"]}.json', wait)
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
        assert batch.job_prompt(worker.job) == expected_prompt
        if private_check:
            assert worker.job.get('cwd_policy') == batch.EMPTY_CWD_POLICY
        else:
            assert not any(key in worker.job for key in ('cwd_policy', 'initial_prompt', 'name_policy'))
            assert not (worker.path.parent / 'work').exists()
            assert not any(call['method'] == 'draft_batch_session_name' for call in input_calls)
        native_arguments = {r['pid']: scope.arguments(r['pid'])[0] for r in owned}
        working_roots = []
        for slot in worker.job['slots']:
            expected = batch.working_directory(config_path, worker.job['id'], slot['index']) if private_check else root
            native_argv = native_arguments[slot['pid']]
            if private_check:
                assert '--cd' in native_argv and native_argv[native_argv.index('--cd') + 1] == str(expected)
                assert expected.is_dir() and not any(expected.iterdir())
            else:
                assert '--cd' not in native_argv and not any(a.startswith('projects=') for a in native_argv)
            with Path(slot['transcript']).open() as transcript:
                metadata = json.loads(transcript.readline())
            assert metadata['type'] == 'session_meta' and Path(metadata['payload']['cwd']).resolve() == expected
            working_roots.append(str(expected))
        assert len(set(working_roots)) == (50 if private_check else 1)
        assert (home / 'config.toml').read_bytes() == original_native_config
        if private_check:
            assert all(s.get('naming', {}).get('confirmed_name') for s in worker.job['slots'])
        else:
            assert not any(s.get('naming') for s in worker.job['slots'])
        complete_deadline = time.monotonic() + 10
        completions = []
        while time.monotonic() < complete_deadline:
            completions = [native.task_snapshot(Path(s['transcript']), s['session_id']) for s in worker.job['slots']]
            if all(t and t['kind'] == 'task_complete' and not t.get('error') for t in completions):
                break
            time.sleep(.1)
        assert all(t and t['kind'] == 'task_complete' and not t.get('error') for t in completions)
        deadline = time.monotonic() + 10
        while sum(not r['native_title'] for r in server.requests) < 50 and time.monotonic() < deadline:
            time.sleep(.1)
        time.sleep(1)
        primary = [r for r in server.requests if not r['native_title']]
        titles = [r for r in server.requests if r['native_title']]
        record.update(started=50, local_requests=len(server.requests), primary_requests=len(primary),
                      native_title_requests=len(titles), completed_responses=50,
                      all_input_calls_scoped_to_fixture=all(call['workspace_id'] == wid for call in input_calls))
        assert len(primary) == 50 and all(r['user_text'] == expected_prompt for r in primary), 'unexpected batch request'
        if private_check:
            assert not titles, 'the preassigned native name must avoid all hidden title requests'
        else:
            assert len(titles) <= 50, 'unexpected repeated native title requests'
        record['original_identity_changes'] = [r for r in original if not scope.matches(r)]
        assert not record['original_identity_changes'], 'an existing identity changed during acceptance'
        record.update(phase='passed', seconds=time.monotonic() - started, started=50,
                      local_requests=len(server.requests), primary_requests=len(primary), native_title_requests=len(titles),
                      original_native_retained=len(original), native_owned=owned,
                      working_directories=working_roots, persistent_trust_config_unchanged=True,
                      pretrusted_fixture_directory=not private_check, completed_responses=50,
                      named_before_model_request=private_check, legacy_prompt_preserved=not private_check)
        if private_check:
            record['distinct_empty_working_directories'] = working_roots
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
        core.atomic_write_json(output / 'input-calls.json', input_calls)
        core.atomic_write_json(output / 'result.json', record)
    print(json.dumps({k: v for k, v in record.items() if k not in {'native_before', 'native_owned'}}, indent=2))


if __name__ == '__main__':
    main()
