#!/usr/bin/env python3
"""Real B50 in one owned background cmux workspace, using only a loopback API.

Keeps automatic pause off, uses private native/CCC data, and closes only the
recorded fixture workspace. Existing Codex identities are checked, not signalled.
"""
import argparse
from collections import Counter
import ctypes
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import shlex
import signal
import statistics
import tempfile
import threading
import time
try:
    import tomllib
except ModuleNotFoundError:  # Optional acceptance dependency on Python 3.10.
    import tomli as tomllib
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ccc_batch_guard as guard
import ccc_codex_queue as native
import ccc_guard_scope as scope
import ccc_guard_migration as migration
from ccc_scheduling import CoalescingWriter, SnapshotCache, SnapshotClient
import ccc_workspace_batch as batch
import cmux_codex_watch as core
from tools.idle_session_native_acceptance import ERROR, Handler
from tools.access_sustained_native_probe import SustainedNativeProbe


class AccessHandler(BaseHTTPRequestHandler):
    """Actual fifty-request cohort; all billing/model behavior is loopback."""
    def log_message(self, *_):
        pass

    def end_headers(self):
        if getattr(self.server, 'repeat_response_headers', False):
            self.send_header('Set-Cookie', 'fixture_first=1; Path=/')
            self.send_header('Set-Cookie', 'fixture_second=2; Path=/')
            self.send_header('Vary', 'Accept-Encoding')
            self.send_header('Vary', 'Origin')
        super().end_headers()

    def do_POST(self):
        raw = self.rfile.read(int(self.headers['Content-Length']))
        body = json.loads(raw)
        assert len(raw) < 1024 and body['input'] == 'Reply exactly OK.'
        assert body['tools'] == [] and body['tool_choice'] == 'none' and body['max_output_tokens'] == 128
        with self.server.condition:
            number = len(self.server.requests)
            probe = getattr(self.server, 'sustained_probe', None)
            failed = probe.reject() if probe else self.server.fail_first_by_session and number < 50
            self.server.requests.append({'at': time.time(), 'monotonic': time.monotonic(),
                'body': body, 'bytes': len(raw), 'failed': failed, 'native_title': False,
                'rejection_transport': ('sse' if number % 2 else 'http') if failed else None,
                'user_text': body['input'], 'thread_id': self.headers.get('thread-id')})
            self.server.active += 1
            self.server.peak = max(self.server.peak, self.server.active)
            self.server.condition.notify_all()
            ready = self.server.condition.wait_for(lambda: len(self.server.requests) >= 50, timeout=120)
        try:
            assert ready, 'native first cohort did not reach fifty actual HTTP requests'
            if failed:
                sse = bool(number % 2)
                self.send_response(200 if sse else 500)
                self.send_header('Content-Type', 'text/event-stream' if sse else 'application/json')
                self.send_header('Connection', 'close')
                self.end_headers()
                if sse:
                    failure = {'type': 'response.failed', 'response': {
                        'id': 'resp_' + uuid.uuid4().hex, 'status': 'failed', 'output': [],
                        'error': {'code': 'server_error', 'message': 'We are currently experiencing high demand.'}}}
                    self.wfile.write(('data: ' + json.dumps(failure) + '\n\n').encode())
                else:
                    self.wfile.write(b'{"error":{"message":"fixture high demand"}}')
            else:
                response = {'id': 'resp_' + uuid.uuid4().hex, 'object': 'response', 'status': 'completed',
                    'output': [{'id': 'msg_' + uuid.uuid4().hex, 'type': 'message', 'role': 'assistant',
                                'status': 'completed', 'content': [{'type': 'output_text', 'text': 'OK'}]}],
                    'usage': {'input_tokens': 12, 'output_tokens': 1, 'total_tokens': 13}}
                raw = ('data: ' + json.dumps({'type': 'response.completed', 'response': response}) + '\n\n').encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            self.wfile.flush()
        finally:
            with self.server.condition:
                self.server.active -= 1


class AccessHTTPServer(ThreadingHTTPServer):
    request_queue_size = 256


def workspace_surfaces_by_id(client, workspace_id):
    # core's public map is keyed by display ref, not by immutable surface UUID.
    return {row['surface_id']: row for row in
            core.workspace_surface_records(client.tree(), workspace_id).values()}


def close_fixture_surface(client, workspace_id, surface_id, originals):
    assert surface_id not in originals, 'cleanup may not close an original surface'
    current = workspace_surfaces_by_id(client, workspace_id)
    assert surface_id in current, 'fixture surface is no longer in its pinned workspace'
    # cmux resolves even UUIDs within a workspace context; the caller's active
    # workspace is not the fixture's workspace.
    client._run(['close-surface', '--workspace', workspace_id, '--surface', surface_id], timeout=10)


def native_resources(owned):
    """Darwin physical footprint, rather than adding shared RSS pages."""
    class RusageV2(ctypes.Structure):
        _fields_ = [('uuid', ctypes.c_ubyte * 16), ('values', ctypes.c_uint64 * 18)]
    function = ctypes.CDLL('/usr/lib/libproc.dylib').proc_pid_rusage
    function.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
    function.restype = ctypes.c_int
    rows = []
    for identity in owned:
        sample = RusageV2()
        assert scope.matches(identity)
        if function(identity['pid'], 2, ctypes.byref(sample)) != 0:
            raise RuntimeError('native physical footprint unavailable')
        assert scope.matches(identity)
        rows.append({'pid': identity['pid'], 'physical_footprint': sample.values[7], 'rss': sample.values[6]})
    return {'samples': rows, 'native_cli_instances': len(rows),
            'physical_footprint_total': sum(r['physical_footprint'] for r in rows),
            'physical_footprint_median': statistics.median(r['physical_footprint'] for r in rows)}


def access_panel_snapshot(config_path, client, slots):
    import cmux_supervisor_tui as tui
    from types import SimpleNamespace
    quiet = SimpleNamespace(maybe_refresh=lambda *args, **kwargs: None, snapshot=lambda: {})
    model = tui.SupervisorModel(config_path, client=client, janitor=quiet,
                                stack=quiet, collab=quiet, sessions=quiet)
    try:
        model.refresh(force=True)
        owned_ids = {slot['surface_id'] for slot in slots}
        rows = {row.surface_id: {'screen': tui.screen_label(row), 'error': tui.error_label(row),
                                'phase': row.access.get('phase'), 'allowed': row.access.get('allowed'),
                                'alarming': row.access.get('alarming')}
                for row in model.candidates if row.surface_id in owned_ids}
        assert set(rows) == owned_ids
        return rows
    finally:
        model.close()


def continue_failed_batch(config_path, root, home, client, slots, owned, output, *, access_check=False, job_id='',
                          sustained_probe=None, server=None):
    """Use the real scheduler, viewport gates and native identity checks."""
    failed = {s['surface_id']: native.task_snapshot(Path(s['transcript']), s['session_id']) for s in slots}
    assert all(t and t['kind'] == 'task_complete' and
               (('high demand' in (t.get('error') or {}).get('message', '').lower()) if access_check
                else ERROR in (t.get('error') or {}).get('message', ''))
               for t in failed.values()), 'each original turn must actually fail first'
    core.atomic_write_json(output / 'failed-turns.json', failed)
    daemon = core.WatchDaemon(config_path, root / 'watch-state.json', client=client)
    daemon.codex_queue_recovery.sessions_root = home / 'sessions'
    daemon._state_writer = CoalescingWriter(daemon._save_now)
    scheduler = daemon._start_scheduler()
    notifier = native.NativeCompletionWatcher(daemon._native_wakeup_sources, scheduler.request_observation,
                                               retry_needed=daemon._native_retry_needed)
    scheduler.observation_interval = notifier.observation_interval
    previous_switch = sys.getswitchinterval()
    sys.setswitchinterval(min(previous_switch, .001))
    started, last_report, completed_at = time.monotonic(), 0.0, None
    completions = {}
    try:
        daemon._native_process_index.start()
        notifier.start()
        while time.monotonic() - started < (600 if sustained_probe else 150):
            elapsed = time.monotonic() - started
            daemon._reload_config_if_changed()
            daemon._refresh_dynamic_targets(daemon._observation_client())
            scheduler.wakeup.clear()
            targets = core.effective_targets(daemon.config, list(daemon.dynamic_targets.values()))
            assert all(t['workspace_id'] == slots[0]['fixture_workspace_id'] for t in targets)
            assert {t['surface_id'] for t in targets} <= {s['surface_id'] for s in slots}, 'fixture discovery escaped its fifty surfaces'
            scheduler.tick(targets, generation=daemon._observation_policy.key)
            current = {s['surface_id']: native.task_snapshot(Path(s['transcript']), s['session_id']) for s in slots}
            if sustained_probe:
                sustained_probe.sample(client, slots[0]['fixture_workspace_id'], slots)
                sustained_probe.maybe_release(server, current, failed)
            completions = {sid: t for sid, t in current.items()
                           if t and t['kind'] == 'task_complete' and not t.get('error')
                           and t['turn_id'] != failed[sid]['turn_id']}
            enough = len(completions) == len(slots)
            if access_check:
                from ccc_access_service import status
                enough = bool(completions and status(config_path, job_id).get('first_complete'))
            if enough:
                if completed_at is None:
                    completed_at = time.monotonic()
                if time.monotonic() - completed_at >= 5:
                    break
            if elapsed - last_report >= 5:
                print(json.dumps({'continuation_seconds': round(elapsed, 2), 'discovered': len(targets),
                                  'continued_original_sessions': len(completions), **scheduler.snapshot()}), flush=True)
                last_report = elapsed
            scheduler.wakeup.wait(scheduler.wait_timeout())
        assert (bool(completions) if access_check else len(completions) == len(slots)), f'only {len(completions)}/{len(slots)} original sessions continued'
        assert all(scope.matches(row) for row in owned), 'original fixture process changed'
        return {'passed': True, 'seconds': time.monotonic() - started,
                'original_failed_turns': failed, 'continued_turns': completions,
                'surface_sessions': {s['surface_id']: s['session_id'] for s in slots},
                'original_pid_birth_session_retained': len(slots), 'duplicate_detection_seconds': 5}
    finally:
        daemon.stop_requested = True
        notifier.close()
        scheduler.close()
        daemon._native_process_index.close()
        daemon._process_snapshots.close()
        daemon._state_writer.close()
        core.atomic_write_json(output / 'continuation-state.json', json.loads(daemon._serialize_state()))
        sys.setswitchinterval(previous_switch)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--mode', choices=('private-check', 'existing', 'access-check'), default='private-check',
                        help='existing verifies default B including first-time folder trust')
    parser.add_argument('--workspace', help='use an existing workspace; clean up only newly created fixture surfaces')
    parser.add_argument('--verify-continuation', action='store_true',
                        help='fail every original first turn; require real CCC continuation in all 50 sessions')
    parser.add_argument('--repeat-response-headers', action='store_true',
                        help='send legal repeated Cookie/Vary fields on every local API response (N only)')
    parser.add_argument('--native-reconnect', action='store_true',
                        help='N only: native retry defaults, 5/5 viewport proof, >1000 requests and >150s before success')
    parser.add_argument('--recover-setup-from', type=Path,
                        help='N only: reproduce old setup 409 with this prior source, then repair the same fifty sessions')
    args = parser.parse_args()
    access_check = args.mode == 'access-check'
    assert not args.repeat_response_headers or access_check
    assert not args.native_reconnect or (access_check and args.verify_continuation)
    assert not args.recover_setup_from or (access_check and args.native_reconnect and not args.workspace)
    private_check = args.mode != 'existing'
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    assert not (output / 'result.json').exists(), 'keep previous acceptance evidence'
    assert not guard.AUTOMATIC_POOL_STOP and not guard.CONNECTION_CUT_ENABLED
    assert batch.COUNT == 50
    server = (AccessHTTPServer if access_check else ThreadingHTTPServer)(
        ('127.0.0.1', 0), AccessHandler if access_check else Handler,
        bind_and_activate=not bool(args.recover_setup_from))
    if args.recover_setup_from:
        # Reserve the port without listening: the original gateway experiences
        # a real TCP refusal, with no HTTP request to any provider.
        server.server_bind()
    server.condition, server.active, server.peak = threading.Condition(), 0, 0
    server.requests = []
    server.fail_first = False
    server.fail_first_by_session = args.verify_continuation
    server.repeat_response_headers = args.repeat_response_headers
    server.sustained_probe = SustainedNativeProbe(output) if args.native_reconnect else None
    server.failed_sessions = set()
    server.failure_lock = threading.Lock()
    server_started = not bool(args.recover_setup_from)
    if server_started:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    root = Path(tempfile.mkdtemp(prefix='ccc-b50-native-')).resolve()
    home = root / 'codex'
    home.mkdir()
    (home / 'sessions').mkdir()
    if args.verify_continuation:
        (home / 'AGENTS.md').write_text('Local loopback acceptance. Do not use tools.\n')
    (home / 'config.toml').write_text(
        'model = "gpt-6-astra"\nmodel_provider = "local_fixture"\n'
        'approval_policy = "never"\nsandbox_mode = "read-only"\ncheck_for_update_on_startup = false\n'
        +
        '[tui]\nscreen_reader_detection_done = true\n'
        '[features]\nplugins = false\napps = false\nhooks = false\nskip_host_skill_discovery = true\n'
        '[model_providers.local_fixture]\nname = "Loopback fixture"\nwire_api = "responses"\n'
        f'base_url = "http://127.0.0.1:{server.server_port}/v1"\n'
        'requires_openai_auth = false\nsupports_websockets = false\n'
        + ('' if args.native_reconnect else 'request_max_retries = 0\nstream_max_retries = 0\n'))
    original_native_config = (home / 'config.toml').read_bytes()
    config_path = root / 'ccc/config.json'
    config = core.default_config()
    config.update(mode='armed', global_paused=False, claude_enabled=False,
                  targets=[], workspace_rules=[], network_guard={'enabled': False})
    core.atomic_write_json(config_path, config)
    client = migration.cmux_client(config_path)
    original = scope.scan()
    original_surfaces = set()
    if args.workspace:
        uuid.UUID(args.workspace)
        original_surfaces = set(workspace_surfaces_by_id(client, args.workspace))
        assert original_surfaces, 'the specified real workspace must exist before acceptance'
    source_root = Path(__file__).resolve().parents[1]
    source_files = {name: hashlib.sha256((source_root / name).read_bytes()).hexdigest()
                    for name in (*core.RUNTIME_FILES, 'cmux_supervisor_tui.py',
                                 'tools/batch_native_acceptance.py', 'tools/idle_session_native_acceptance.py',
                                 'tools/access_sustained_native_probe.py')}
    if args.recover_setup_from:
        source_files['tools/recover_access_setup.py'] = hashlib.sha256(
            (source_root / 'tools/recover_access_setup.py').read_bytes()).hexdigest()
    record = {'phase': 'prepared', 'root': str(root), 'production_requests': 0,
              'automatic_pause': False, 'native_before': original, 'startup_mode': args.mode,
              'repeat_response_headers': args.repeat_response_headers,
              'native_reconnect': args.native_reconnect,
              'source_files': source_files,
              'native_binary_sha256': hashlib.sha256(Path(guard.native_binary()).read_bytes()).hexdigest()}
    core.atomic_write_json(output / 'result.json', record)
    cache, worker, wid, access_owner = None, None, None, None
    captured_waits = set()
    input_calls = []
    original_env = dict(os.environ)
    original_ensure, previous_service = None, None
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
        if args.workspace:
            wid = args.workspace
        else:
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
                    assert entry['surface_id'] not in original_surfaces, 'fixture input reached a pre-existing surface'
                input_calls.append(entry)
                return original_method(*values, **options)
            setattr(client, method, checked)
        scoped_call('new_codex_surface', 1)
        for method in ('draft_batch_session_name', 'send_text', 'send_key'):
            scoped_call(method, 0, 1)
        scoped_call('send', 0, 1)
        cache = SnapshotCache(workers=1)
        wrapped = SnapshotClient(client, cache)
        if args.workspace:
            # The private test daemon must never send to the user's existing
            # surfaces, including the old B that remains explicitly held.
            batch.authorize_workspace(config_path, wid, client=wrapped)
            def exclude_original(value):
                core.workspace_rule_by_id(value, wid)['excluded_surface_ids'] = sorted(original_surfaces)
            core.ConfigStore(config_path).mutate(exclude_original)
        options = ({'access_check': True, '_access_fixture': True} if access_check
                   else {'private_check': True} if private_check else {})
        if args.recover_setup_from:
            import ccc_access_service as access_service
            old_path = args.recover_setup_from.resolve() / 'ccc_access_service.py'
            spec = importlib.util.spec_from_file_location('ccc_acceptance_previous_service', old_path)
            previous_service = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(previous_service)
            record['previous_service_source'] = previous_service.fingerprint()
            original_ensure = access_service.ensure_gateway
            access_service.ensure_gateway = previous_service.ensure_gateway
        job = batch.start(config_path, wid, client=wrapped, launch=False, **options)
        if access_check:
            from ccc_access_service import ensure_gateway
            access_owner = ensure_gateway(config_path)
        expected_prompt = batch.PROMPT if private_check else batch.LEGACY_PROMPT
        queue = native.QueueRecovery(root / 'ledger.json', root / 'missing-hooks', home / 'sessions', expected_prompt)
        worker = batch.BatchWorker(config_path, job['job_id'], client=wrapped, queue=queue)
        launch = worker._launch_command
        # Pin the provider environment again for every real B-created child;
        # shell startup files cannot route this fixture to a real account.
        def fixture_launch(slot):
            command = launch(slot)
            if args.recover_setup_from:
                old_batch = args.recover_setup_from.resolve() / 'ccc_workspace_batch.py'
                assert old_batch.read_bytes() == Path(batch.__file__).read_bytes()
                argv = shlex.split(command)
                original_path = str(Path(batch.__file__).resolve())
                assert argv.count(original_path) == 1
                argv[argv.index(original_path)] = str(old_batch)
                # Native argv is constructed in this registered child, too.
                # It must use the same original gateway as its parent worker.
                command = shlex.join(argv)
            return shlex.join(['/usr/bin/env', *(key + '=' + value for key, value in overrides.items()),
                               '/bin/sh', '-c', command])
        worker._launch_command = fixture_launch
        started, last_report = time.monotonic(), 0.0
        with core.FileLock(worker.path.parent / 'worker.lock', timeout_sec=0):
            worker.job.update(worker_pid=os.getpid(), worker_version=batch.WORKER_VERSION)
            worker.save()
            while worker.step():
                elapsed = time.monotonic() - started
                if server.sustained_probe:
                    server.sustained_probe.sample(client, wid, worker.job['slots'])
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
        owned_ids = {s['surface_id'] for s in worker.job['slots']}
        owned = [r for r in scope.scan() if r['environment_workspace_id'] == wid and r['surface_id'] in owned_ids]
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
        if args.native_reconnect:
            assert all(not any('request_max_retries' in arg or 'stream_max_retries' in arg for arg in argv)
                       for argv in native_arguments.values()), 'native retry defaults were overridden'
        working_roots = []
        for slot in worker.job['slots']:
            expected = batch.working_directory(config_path, worker.job['id'], slot['index']) if private_check else root
            native_argv = native_arguments[slot['pid']]
            assert '--cd' in native_argv and native_argv[native_argv.index('--cd') + 1] == str(expected)
            trust = [a for a in native_argv if a.startswith('projects=')]
            assert len(trust) == 1 and tomllib.loads(trust[0]) == {
                'projects': {str(expected): {'trust_level': 'trusted'}}}
            if private_check:
                assert expected.is_dir() and not any(expected.iterdir())
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
        complete_deadline = time.monotonic() + (600 if args.native_reconnect else 10)
        completions = []
        while time.monotonic() < complete_deadline:
            if server.sustained_probe:
                server.sustained_probe.sample(client, wid, worker.job['slots'])
            completions = [native.task_snapshot(Path(s['transcript']), s['session_id']) for s in worker.job['slots']]
            if all(t and t['kind'] == 'task_complete'
                   and bool(t.get('error')) == args.verify_continuation for t in completions):
                break
            time.sleep(.1)
        assert all(t and t['kind'] == 'task_complete'
                   and bool(t.get('error')) == args.verify_continuation for t in completions)
        if args.recover_setup_from:
            import ccc_access_service as access_service
            from tools import recover_access_setup as repair
            access_service.ensure_gateway = original_ensure
            original_ensure = None
            access_service._started_processes.update(previous_service._started_processes)
            desc = access_service.read_private(worker.path.parent / 'access.json')
            assert all(repair.terminal_failure(t, desc, s) for t, s in zip(completions, worker.job['slots']))
            assert not server.requests, 'old setup fault must not have sent an HTTP request'
            old_turns = {s['surface_id']: t for s, t in zip(worker.job['slots'], completions)}
            core.atomic_write_json(output / 'original-local-409-turns.json', old_turns)
            plan_path = output / 'setup-recovery-plan.json'
            repair.prepare(config_path, job['job_id'], plan_path, client=client)
            server.server_activate()
            threading.Thread(target=server.serve_forever, daemon=True).start()
            server_started = True
            receipt = repair.apply(plan_path, output / 'setup-recovery-receipt.json', client=client)
            access_owner = receipt['new_owner']
            assert receipt['phase'] == 'resumed_requires_observation'
            assert len(receipt['inputs']) == 50 and all(r.get('new_turn_id') for r in receipt['inputs'].values())
            assert all(scope.matches(row) for row in owned)
            record['setup_recovery'] = {'original_local_409_sessions': 50,
                'same_native_sessions_resumed': 50, 'prior_http_requests': 0,
                'original_port_retained': access_owner['port'] == desc['port'],
                'original_instance_retained': access_owner['instance'] == desc['gateway_instance']}
            # Also witness the repaired sessions exhausting normal native retry
            # and then the real watcher continuing them, not only our one input.
            complete_deadline = time.monotonic() + 180
            while time.monotonic() < complete_deadline:
                server.sustained_probe.sample(client, wid, worker.job['slots'])
                completions = [native.task_snapshot(Path(s['transcript']), s['session_id']) for s in worker.job['slots']]
                if all(t and t['kind'] == 'task_complete' and t.get('error')
                       and t['turn_id'] != old_turns[s['surface_id']]['turn_id']
                       for t, s in zip(completions, worker.job['slots'])):
                    break
                time.sleep(.1)
            assert all(t and t['kind'] == 'task_complete' and t.get('error')
                       and t['turn_id'] != old_turns[s['surface_id']]['turn_id']
                       for t, s in zip(completions, worker.job['slots']))
        if access_check and args.repeat_response_headers and args.verify_continuation:
            deadline = time.monotonic() + 5
            while True:
                observed = access_panel_snapshot(config_path, client, worker.job['slots'])
                if all(row['phase'] == 'retryable' and row['allowed'] for row in observed.values()):
                    break
                assert time.monotonic() < deadline, 'first-wave errors were not visible and retryable in the panel'
                time.sleep(.25)
            if args.native_reconnect:
                assert {row['error'] for row in observed.values()} <= {'HTTP500', 'HTTP503'}
            else:
                assert Counter(row['error'] for row in observed.values()) == {'HTTP500': 25, 'HTTP503': 25}
            core.atomic_write_json(output / 'panel-after-cookie-rejections.json', observed)
            record['panel_initial_errors'] = len(observed)
        if args.verify_continuation:
            pathless_contexts = 0
            for slot in worker.job['slots']:
                events = [json.loads(line) for line in Path(slot['transcript']).read_text().splitlines() if line]
                contexts = [part.get('text', '') for e in events if e.get('type') == 'response_item'
                            and e.get('payload', {}).get('role') == 'user'
                            for part in e['payload'].get('content', [])]
                pathless_contexts += any(c.startswith('# AGENTS.md instructions\n') for c in contexts)
            if not access_check:
                assert pathless_contexts == 50, 'native global context format was not exercised in every session'
            continuation = continue_failed_batch(config_path, root, home, client,
                [{**s, 'fixture_workspace_id': wid} for s in worker.job['slots']], owned, output,
                access_check=access_check, job_id=job['job_id'],
                sustained_probe=server.sustained_probe, server=server)
            record.update(continuation=continuation, native_pathless_context_sessions=pathless_contexts)
        deadline = time.monotonic() + 10
        while sum(not r['native_title'] for r in server.requests) < 50 and time.monotonic() < deadline:
            time.sleep(.1)
        time.sleep(1)
        primary = [r for r in server.requests if not r['native_title']]
        titles = [r for r in server.requests if r['native_title']]
        successful_responses = sum(not r['failed'] for r in primary)
        native_successes = (len(record.get('continuation', {}).get('continued_turns', {})) if args.verify_continuation
                            else sum(bool(t and t['kind'] == 'task_complete' and not t.get('error')) for t in completions))
        record.update(started=50, local_requests=len(server.requests), primary_requests=len(primary),
                      native_title_requests=len(titles), completed_responses=successful_responses,
                      upstream_success_responses=successful_responses, native_successful_sessions=native_successes,
                      all_input_calls_scoped_to_fixture=all(call['workspace_id'] == wid for call in input_calls))
        if access_check:
            from ccc_access_service import status as access_status, continuation_allowed
            check = access_status(config_path, job['job_id'])
            prepared_at = max(s['access_ready_at'] for s in worker.job['slots'])
            first_submit = min(s['submit_at'] for s in worker.job['slots'])
            assert first_submit >= prepared_at, 'HTTP submission began before all native sessions were prepared'
            assert check.get('first_complete'), 'no complete real native API check'
            assert not any(continuation_allowed(config_path, worker.store.load(),
                {'workspace_id': wid, 'surface_id': s['surface_id']}) for s in worker.job['slots'])
            assert server.peak == 50
            if args.native_reconnect:
                assert len(primary) > 1000
                record['sustained_native'] = server.sustained_probe.evidence()
                assert all(r['failed'] for r in primary[:record['sustained_native']['http_before_success_enabled']])
            else:
                assert 50 <= len(primary) <= 100
            assert all(r['bytes'] < 1024 and not r['native_title'] for r in primary)
            record.update(access_status=check, actual_simultaneous_http=server.peak,
                          all_native_prepared_before_first_submit=True,
                          native_preparation_seconds=prepared_at - min(s['created_at'] for s in worker.job['slots']),
                          first_submit_after_all_ready_seconds=first_submit - prepared_at,
                          first_to_last_submit_seconds=max(s['submit_at'] for s in worker.job['slots']) - first_submit,
                          ccc_continued_original_sessions=len(record.get('continuation', {}).get('continued_turns', {})))
            if args.repeat_response_headers:
                observed = access_panel_snapshot(config_path, client, worker.job['slots'])
                assert all(row['phase'] in {'complete', 'stopped', 'settling'}
                           and not row['allowed'] and not row['alarming'] for row in observed.values())
                assert sum(row['phase'] == 'complete' for row in observed.values()) == successful_responses
                core.atomic_write_json(output / 'panel-after-native-success.json', observed)
                record['panel_final_stop_visible'] = len(observed)
        elif args.verify_continuation:
            initial = [r for r in primary if r['user_text'] == expected_prompt and r['failed']]
            continued = [r for r in primary if r['user_text'] == core.MESSAGE and not r['failed']]
            sessions = {s['session_id'] for s in worker.job['slots']}
            assert len(primary) == 100 and len(initial) == len(continued) == 50, 'unexpected or duplicated request'
            assert {r['thread_id'] for r in initial} == {r['thread_id'] for r in continued} == sessions
            sends = Counter(call['surface_id'] for call in input_calls if call['method'] == 'send')
            assert sends == Counter({s['surface_id']: 1 for s in worker.job['slots']}), 'CCC did not send exactly once per surface'
            record['ccc_continued_original_sessions'] = 50
        else:
            assert len(primary) == 50 and all(r['user_text'] == expected_prompt for r in primary), 'unexpected batch request'
        if private_check:
            assert not titles, 'the preassigned native name must avoid all hidden title requests'
        else:
            assert len(titles) <= 50, 'unexpected repeated native title requests'
        record['original_identity_changes'] = [r for r in original if not scope.matches(r)]
        assert not record['original_identity_changes'], 'an existing identity changed during acceptance'
        if access_check:
            record['native_resources'] = native_resources(owned)
        assert all(hashlib.sha256((source_root / name).read_bytes()).hexdigest() == digest
                   for name, digest in source_files.items()), 'acceptance source changed during execution'
        record.update(phase='passed', seconds=time.monotonic() - started, started=50,
                      local_requests=len(server.requests), primary_requests=len(primary), native_title_requests=len(titles),
                      original_native_retained=len(original), native_owned=owned,
                      working_directories=working_roots, persistent_trust_config_unchanged=True,
                      pretrusted_fixture_directory=False, completed_responses=successful_responses,
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
        if original_ensure is not None:
            import ccc_access_service as access_service
            access_service.ensure_gateway = original_ensure
        os.environ.clear()
        os.environ.update(original_env)
        if args.workspace and wid and worker:
            try:
                # Prevent a late fixture bootstrap from gaining authorization
                # after cleanup starts. This modifies only the private config.
                def close_fixture(value):
                    core.workspace_rule_by_id(value, wid)['paused'] = True
                worker.store.mutate(close_fixture)
                actual = workspace_surfaces_by_id(client, wid)
                created_ids = {s.get('surface_id') for s in worker.job['slots'] if s.get('surface_id')}
                for slot in worker.job['slots']:
                    receipt = core.load_json(worker.path.parent / f"surface-{slot['index']}.json", {})
                    if (receipt.get('workspace_id') == wid and receipt.get('launch_id') == slot.get('launch_id')
                            and receipt.get('surface_id')):
                        created_ids.add(receipt['surface_id'])
                assert not created_ids & original_surfaces
                for sid in sorted(created_ids):
                    if sid in actual:
                        close_fixture_surface(client, wid, sid, original_surfaces)
                remaining = set(workspace_surfaces_by_id(client, wid))
                record['existing_workspace_retained'] = original_surfaces <= remaining
                record['owned_fixture_surfaces_closed'] = not created_ids & remaining
                record['pre_existing_surface_ids'] = sorted(original_surfaces)
                record['fixture_surface_ids'] = sorted(created_ids)
                record['remaining_surface_ids'] = sorted(remaining)
                assert record['existing_workspace_retained'] and record['owned_fixture_surfaces_closed']
            except (core.CmuxError, OSError, RuntimeError, AssertionError) as exc:
                record['cleanup_error'] = str(exc)
                record['phase'] = 'cleanup_failed'
        elif wid and not args.workspace:
            try:
                client._run(['close-workspace', '--workspace', wid], timeout=10)
                deadline = time.monotonic() + 10
                while any(r['environment_workspace_id'] == wid for r in scope.scan()) and time.monotonic() < deadline:
                    time.sleep(.2)
                record['owned_workspace_closed'] = not any(r['environment_workspace_id'] == wid for r in scope.scan())
            except (core.CmuxError, OSError, RuntimeError) as exc:
                record['cleanup_error'] = str(exc)
        elif args.workspace and wid:
            record['existing_workspace_cleanup_skipped'] = 'worker was never available; workspace is never closed'
        if worker:
            worker.cache.close()
        if cache:
            cache.close()
        if access_owner:
            from ccc_access_service import owner_alive, _started_processes
            owners = [access_owner]
            if args.recover_setup_from:
                from ccc_access_service import read_private
                owners += [read_private(p) for p in (config_path.parent / 'access-gateway').rglob('owner.json')]
                _started_processes.update(previous_service._started_processes if previous_service else {})
            stopped = set()
            for owner in owners:
                if owner['pid'] not in stopped and owner_alive(owner, config_path, check_runtime=False):
                    os.kill(owner['pid'], signal.SIGTERM)
                    stopped.add(owner['pid'])
                    process = _started_processes.pop(owner['pid'], None)
                    if process:
                        process.wait(timeout=10)
        if server_started:
            server.shutdown()
        server.server_close()
        record['original_identity_changes_after_cleanup'] = [r for r in original if not scope.matches(r)]
        if record['original_identity_changes_after_cleanup']:
            record['phase'] = 'original_identity_changed_during_cleanup'
        core.atomic_write_json(output / 'requests.json', server.requests)
        core.atomic_write_json(output / 'input-calls.json', input_calls)
        core.atomic_write_json(output / 'result.json', record)
    if record.get('phase') != 'passed':
        raise RuntimeError('native acceptance or cleanup did not pass; evidence retained')
    print(json.dumps({k: v for k, v in record.items() if k not in {'native_before', 'native_owned'}}, indent=2))


if __name__ == '__main__':
    main()
