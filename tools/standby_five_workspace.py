"""Enclosing native five-workspace experiment. Defaults to read-only preflight.

Ten original cohorts retain separate providers, proxies, daemons and Runners.
This experimental entry never labels process overlap as full 500 acceptance.
"""
from __future__ import annotations

import argparse
import ast
import copy
from contextlib import closing
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def prerequisite_sources(source):
    """Read the original UI50 source manifest without importing candidate code."""
    runtime = []
    for node in ast.parse((source/'cmux_codex_watch.py').read_bytes()).body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'RUNTIME_FILES'
                                               for t in node.targets):
            runtime = list(ast.literal_eval(node.value))
        elif (isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name)
              and node.target.id == 'RUNTIME_FILES'):
            if not isinstance(node.op, ast.Add):
                raise ValueError('unsupported runtime manifest operation')
            runtime.extend(ast.literal_eval(node.value))
    if not runtime or any(not isinstance(n, str) or Path(n).name != n for n in runtime):
        raise ValueError('literal runtime source manifest required')
    return {source/n for n in runtime} | set(source.glob('ccc_standby_*.py')) | set(
        (source/'tools').glob('standby_*.py')) | {source/n for n in (
            'ccc_claude_request_key.py', 'cmux_supervisor_tui.py',
            'tools/native_acceptance_metrics.py', 'tools/native_dispatch_acceptance.py')}


def preflight(source, prerequisite, *, free_bytes=None):
    """Require actual UI50 success and unchanged tested product sources."""
    source = Path(source).resolve(strict=True)
    path = Path(prerequisite).resolve(strict=True)
    raw = path.read_bytes()
    value = json.loads(raw)
    reasons = []
    if (value.get('passed') is not True or value.get('originals_requested') != 50
            or value.get('activations') != 50 or value.get('recoveries') != 50
            or value.get('startup_timing', {}).get('startup_passed') is not True
            or value.get('run_verification', {}).get('succeeded') is not True):
        reasons.append('successful original UI50 prerequisite missing')
    frozen = value.get('source_before', {})
    try:
        required = prerequisite_sources(source)
        bound = bool(required) and all(p.is_file() and frozen.get(str(p)) == digest(p) for p in required)
        # Also retain the prerequisite's external fixture/configuration bindings.
        bound = bound and all(Path(p).is_file() and digest(p) == h for p, h in frozen.items())
    except (OSError, ValueError, TypeError, SyntaxError):
        bound = False
    if not bound or value.get('source_after') != frozen:
        reasons.append('UI50 tested source binding missing or changed')
    original_proof = None
    if not reasons:
        try:
            from tools.standby_fifty_prerequisite import verify as verify_fifty
            original_proof = verify_fifty(path)
            if (prerequisite_sources(source) != required
                    or any(not Path(p).is_file() or digest(p) != h for p, h in frozen.items())):
                raise ValueError('tested source changed during original UI50 replay')
        except (OSError, ValueError, TypeError, KeyError, IndexError) as exc:
            reasons.append('UI50 original evidence replay failed: '+str(exc))
    free = shutil.disk_usage(source).free if free_bytes is None else free_bytes
    # Conservative planning reserve, explicitly not a measured 500-session cost.
    reserve = 40 * 1024**3
    if free < reserve:
        reasons.append('below conservative 40 GiB planning reserve')
    if path.read_bytes() != raw:
        reasons.append('prerequisite changed during read')
    return dict(eligible=not reasons, reasons=reasons, free_bytes=free,
                planning_reserve_bytes=reserve, measured_capacity=False,
                prerequisite=str(path), prerequisite_sha256=hashlib.sha256(raw).hexdigest(),
                original_ui50_proof=original_proof,
                full_500_acceptance=False)


def load_fixture(path):
    path = Path(path).resolve(strict=True)
    spec = importlib.util.spec_from_file_location('ccc_original_fifty_fixture', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def run_reviewed(source, prerequisite, fixture, output, *, seconds=1800):
    """Launch only the original UI50-tested code and persist final acceptance."""
    source = Path(source).resolve(strict=True)
    prerequisite = Path(prerequisite).resolve(strict=True)
    fixture = Path(fixture).resolve(strict=True)
    def source_paths():
        return set(source.glob('*.py')) | set((source/'tools').glob('*.py'))

    admitted_paths = source_paths()
    admission = preflight(source, prerequisite)
    if not admission['eligible']:
        raise ValueError('native launch blocked: '+ '; '.join(admission['reasons']))
    raw = prerequisite.read_bytes()
    if hashlib.sha256(raw).hexdigest() != admission['prerequisite_sha256']:
        raise ValueError('UI50 prerequisite changed before import')
    frozen = json.loads(raw)['source_before']
    if str(fixture) not in frozen or digest(fixture) != frozen[str(fixture)]:
        raise ValueError('fixture was not tested by original UI50')
    if Path(__file__).resolve() != source/'tools/standby_five_workspace.py':
        raise ValueError('entrypoint is not from the reviewed source')

    def current():
        if source_paths() != admitted_paths:
            raise ValueError('reviewed source set changed during execution')
        if prerequisite.read_bytes() != raw:
            raise ValueError('UI50 prerequisite changed during execution')
        if any(not Path(p).is_file() or digest(p) != h for p, h in frozen.items()):
            raise ValueError('UI50 tested source changed during execution')
        expected = {Path(p).stem: Path(p) for p in frozen
                    if Path(p).suffix == '.py' and Path(p).parent == source}
        expected.update({'tools.'+Path(p).stem: Path(p) for p in frozen
                         if Path(p).suffix == '.py' and Path(p).parent == source/'tools'})
        for name, path in expected.items():
            module = sys.modules.get(name)
            if module is not None and Path(getattr(module, '__file__', '')).resolve() != path:
                raise ValueError('loaded module differs from tested source: '+name)

    current()
    module = load_fixture(fixture)
    current()
    # A slow import must not consume the disk reserve unnoticed.
    if shutil.disk_usage(source).free < admission['planning_reserve_bytes']:
        raise ValueError('capacity changed before native launch')
    experiment = Experiment(source, module, output, seconds=seconds)
    experiment.write('launch-prerequisite.json', admission)
    result = experiment.run()
    final = dict(passed=False, full_500_acceptance=False, admission=admission,
                 remaining_acceptance=result.get('remaining_acceptance', []))
    try:
        current()
        from tools.standby_fifty_prerequisite import verify as verify_fifty
        if verify_fifty(prerequisite) != admission['original_ui50_proof']:
            raise ValueError('original UI50 proof changed after fleet execution')
        from tools.standby_fleet_replay import verify as replay_fleet
        proof = replay_fleet(experiment.output, experiment.plan)
        if proof != result.get('fleet_original_replay'):
            raise ValueError('final original fleet replay differs from collected proof')
        current()
        final.update(passed=True, full_500_acceptance=True, remaining_acceptance=[],
            fleet_original_replay=proof,
            scope='Five workspaces, 500 original sessions with bracketed process overlap, '
                  'startup/recovery timing, lifecycle and sampled resources; '
                  'not atomic continuous ownership or peak resource measurement.')
    except Exception as exc:
        final['error'] = repr(exc)
    experiment.write('final-acceptance.json', final)
    return final


class Experiment:
    """Retain concrete handles even after partial setup or uncertain writes."""
    def __init__(self, source, fixture, output, *, seconds=1800):
        if type(seconds) not in (float, int) or not math.isfinite(seconds) or not 1 <= seconds <= 3600:
            raise ValueError('bounded experiment duration required')
        self.source, self.fixture = Path(source).resolve(strict=True), fixture
        self.output = Path(output).resolve()
        self.output.mkdir(mode=0o700, parents=True, exist_ok=False)
        self.seconds = seconds
        self.resources, self.workspaces = {}, []
        self.pool = self.real = self.root = None
        self.completions = {}
        self.metrics = {}
        self.report = dict(full_500_acceptance=False, passed=False, cleanup_errors=[])

    def write(self, name, value):
        from ccc_native_standby import write_once
        write_once(self.output/name, value)

    def setup(self):
        import cmux_codex_watch as core
        import ccc_workspace_batch as batch
        import ccc_guard_scope as scope
        from ccc_batch_guard import native_binary
        from ccc_standby_environment import template
        from ccc_standby_target import select_provider
        from ccc_standby_runner import Invocation
        from ccc_guard_migration import cmux_client
        from tools.standby_cleanup_evidence import capture_baseline
        from tools.standby_run_manifest import declare
        from tools.standby_test_provider import LocalProvider
        from tools.standby_multi_owner import RetainedOwners

        self.before = scope.scan()
        self.resource_owner = dict(pid=os.getpid(), birth=scope.birth(os.getpid()))
        self.real = core.CmuxClient()
        caps = self.real.capabilities()
        capture_baseline(self.output, client=self.real)
        self.root = Path(tempfile.mkdtemp(prefix='ccc-five-', dir='/tmp')).resolve()
        self.write('root.json', dict(path=str(self.root)))
        frozen_paths = [*self.source.glob('*.py'), *self.source.joinpath('tools').glob('*.py'),
                        Path(self.fixture.__file__), Path(self.fixture.__file__).with_name('native_dispatch_trace99.py')]
        self.frozen = {str(p.resolve()): digest(p) for p in frozen_paths}
        self.write('module-origin-baseline.json', dict(source=str(self.source), python_sources=self.frozen))
        baseline = self.output/'module-origin-baseline.json'
        env = template(dict(os.environ))
        native_home = Path(env.get('CODEX_HOME') or str(Path(env['HOME']) / '.codex'))
        original_sessions = self.fixture.session_root_binding(native_home / 'sessions')
        original_hooks = self.fixture.session_root_binding(Path(env['HOME']) / '.cmuxterm')
        native = native_binary()
        provider_name, _ = select_provider([native], env)
        plan, invocations = [], []
        for wi in range(5):
            title = 'CCC five workspace ' + uuid.uuid4().hex
            # Persist before calling. A missing ACK never authorizes another create.
            item = dict(title=title, id=None)
            self.workspaces.append(item)
            self.write(f'workspace-{wi}-intent.json', item)
            result = self.real._run(['--json', '--id-format', 'both', 'new-workspace',
                                    '--name', title, '--cwd', str(self.root),
                                    '--focus', 'false', '--command', '/bin/zsh'])
            evidence = self.output/f'workspace-{wi}'
            evidence.mkdir(mode=0o700)
            item['id'] = self.fixture.await_created_workspace(self.real, title, result.stdout, evidence)
            self.write(f'workspace-{wi}-created.json', item)
            for ordinal in range(2):
                key = str(uuid.uuid4())
                directory = self.output/key
                directory.mkdir(mode=0o700)
                root = self.root/key
                root.mkdir(mode=0o700)
                resource = dict(root=root, directory=directory, daemon=None, daemon_log=None,
                                proxy=None, provider=None, proxy_started=False)
                self.resources[key] = resource
                provider = resource['provider'] = LocalProvider(root/'provider', batch.PROMPT)
                localenv = dict(env, CODEX_CA_CERTIFICATE=str(provider.cert))
                for name in ('no_proxy', 'NO_PROXY'):
                    localenv[name] = ','.join(filter(None, [env.get(name, ''), '127.0.0.1', 'localhost']))
                proxy = resource['proxy'] = self.fixture.Server(str(root/'rpc.sock'), self.fixture.Forward)
                proxy.lock, proxy.errors, proxy.output = threading.Lock(), [], directory
                proxy.workspace, proxy.surface, proxy.gate = item['id'], None, None
                proxy.creates = proxy.pastes = 0
                proxy.upstream = caps['socket_path']
                thread = resource['proxy_thread'] = threading.Thread(target=proxy.serve_forever, daemon=True)
                thread.start()
                resource['proxy_started'] = True
                self.fixture.write(root/'settings.json', dict(cmux_binary=self.real.binary,
                    original_sessions=original_sessions,
                    original_hooks=original_hooks,
                    source=str(self.source), module_origin_baseline=str(baseline),
                    module_origin_baseline_sha256=digest(baseline)))
                cli = root/'cmux-fixture'
                cli.write_text('#!/bin/sh\nexec '+shlex.join([sys.executable, '-B',
                    str(Path(self.fixture.__file__).resolve()), '--cli', str(root)])+' "$@"\n')
                cli.chmod(0o700)
                config = root/'ccc/config.json'
                resource['config'] = config
                value = core.default_config()
                value.update(mode='armed', global_paused=False, claude_enabled=False,
                             cmux_path=str(cli), targets=[], workspace_rules=[],
                             network_guard={'enabled': False})
                core.atomic_write_json(config, value)
                batch.authorize_workspace(config, item['id'], client=cmux_client(config))
                invocation = directory/'invocation.json'
                self.fixture.write(invocation, dict(version=1, kind='standby_production_invocation',
                    invocation_id=str(uuid.uuid4()), config_path=str(config), workspace_id=item['id'],
                    mode='b', argv=[native, '-c', 'model_providers.'+provider_name+'.base_url='+json.dumps(provider.url)],
                    provider=provider_name, upstream_url=provider.url, environment=localenv,
                    cmux_binary=str(cli), cmux_socket=str(root/'rpc.sock'), lifetime_seconds=self.seconds+120))
                runner_directory = directory/'runner'
                runner_directory.mkdir(mode=0o700)
                invocations.append((key, Invocation(invocation, digest(invocation)), runner_directory))
                plan.append(dict(batch_id=key, workspace_id=item['id'], mode='b', slots=50))
        self.plan = self.output/'run'
        self.plan.mkdir(mode=0o700)
        declare(self.plan, plan)
        self.pool = RetainedOwners(self.plan, invocations)

    def start_daemon(self, key, handle):
        self.native_setup.before_ui(key, handle)
        resource = self.resources[key]
        self.observe_binding(key, before_activation=True)
        if resource.get('daemon_attempted'):
            raise ValueError('daemon start consumed; no replacement')
        resource['daemon_attempted'] = True
        log = resource['daemon_log'] = (resource['directory']/'private-daemon.log').open('xb')
        resource['daemon'] = subprocess.Popen([sys.executable, '-B',
            str(Path(self.fixture.__file__).resolve()), '--daemon', str(resource['root'])],
            stdout=log, stderr=subprocess.STDOUT, cwd=self.source, start_new_session=True)
        from ccc_guard_scope import birth
        child = resource['daemon']
        resource['daemon_identity'] = dict(pid=child.pid, birth=birth(child.pid))
        if child.poll() is not None or resource['daemon_identity']['birth'] is None:
            raise ValueError('original private daemon exited before identity binding')

    def health(self):
        self.pool._health()
        for resource in self.resources.values():
            if resource['daemon'] is not None and resource['daemon'].poll() is not None:
                raise ValueError('original private daemon exited')
            if resource['proxy'].errors:
                raise ValueError('original forwarding gate rejected operation')

    def execute(self):
        from tools.standby_retained_activation import ActivationRun, NativeSetup
        from tools.standby_run_observer import observe_completion
        from tools.native_acceptance_metrics import evaluate_native_completion
        self.native_setup = NativeSetup(self.pool, self.resources, self.fixture.CohortGate)
        directory = self.output/'activation'
        directory.mkdir(mode=0o700)
        run = ActivationRun(self.pool, directory, seconds=self.seconds,
                            before_prepare=self.native_setup.before_prepare, before_ui=self.start_daemon)
        deadline = time.monotonic()+self.seconds
        def remaining():
            value = deadline-time.monotonic()
            if value <= 0:
                raise TimeoutError('original lifecycle observation deadline expired')
            return value
        while time.monotonic() < deadline:
            self.health()
            state = run.step()
            remaining()
            if state['all_settled']:
                break
            time.sleep(.1)
        else:
            raise TimeoutError('original activation deadline expired')
        self.collect_bindings('settled', deadline=deadline)
        remaining()
        overlap = self.output/'overlap'
        overlap.mkdir(mode=0o700)
        self.report['process_overlap'] = self.pool.observe_overlap(overlap, seconds=min(120, remaining()))
        remaining()
        self.collect_resources(seconds=min(30, remaining()))
        remaining()
        while time.monotonic() < deadline:
            self.health()
            for key, resolvers in self.native_setup.resolvers.items():
                if key in self.completions:
                    continue
                complete = []
                for resolver in resolvers.values():
                    remaining()
                    path = resolver()
                    remaining()
                    if path is None:
                        break
                    with path.open('rb') as stream:
                        raw = stream.read(32*1024*1024+1)
                    if len(raw) > 32*1024*1024:
                        raise ValueError('transcript exceeds observation budget')
                    records = [json.loads(r) for r in raw.splitlines(keepends=True) if r.endswith(b'\n')]
                    complete.append(evaluate_native_completion(records, resolver.row['session_id'], 1)['passed'])
                if len(complete) == 50 and all(complete):
                    output = self.resources[key]['directory']/'completion'
                    output.mkdir(mode=0o700, exist_ok=True)
                    observe_completion(self.plan, key, output, failed_rounds=1)
                    remaining()
                    self.completions[key] = output/'completion-observation.json'
            if len(self.completions) == 10:
                self.collect_bindings('completed', deadline=deadline)
                remaining()
                self.report['lifecycle_completed'] = True
                self.collect_performance(deadline=deadline)
                remaining()
                self.collect_identity_join(check=remaining)
                remaining()
                return
            time.sleep(.2)
        raise TimeoutError('original lifecycle completion deadline expired')

    def collect_identity_join(self, *, check):
        from tools.standby_fleet_identity import verify
        result = verify(witnesses=self.native_setup.witnesses, metrics=self.metrics,
            settled=self.report['observer_bindings_settled'],
            completed=self.report['observer_bindings_completed'],
            overlap=self.report['process_overlap'],
            resources=self.report['original_process_resources'], check=check)
        check()
        self.write('fleet-identity-join.json', result)
        self.report['fleet_identity_join'] = result

    def observe_binding(self, key, *, before_activation=False):
        """Keep original source watcher and route evidence through completion."""
        self.health()
        handle = self.pool.handles[key]
        caller = handle['runner'].caller
        sources, routes = caller.sources, caller.routes
        if sources is None or routes is None:
            raise ValueError('original source and route observers required')
        generation = copy.deepcopy(sources.current())
        route_identity = copy.deepcopy(routes.current())
        source_report = sources.resource_report()
        route_report = routes.report()
        watcher = source_report.get('watcher')
        if (source_report.get('closed') is not False or not isinstance(watcher, dict)
                or watcher.get('close_started') is not False
                or watcher.get('queue_closed') is not False or watcher.get('close_errors') != []
                or type(watcher.get('remaining_owned_fds')) is not int
                or watcher['remaining_owned_fds'] <= 0):
            raise ValueError('original source watcher no longer live')
        slots = route_report.get('slots')
        if (route_report.get('closed') is not False or route_report.get('failed')
                or route_report.get('unattributed_requests') != 0
                or not isinstance(slots, list) or len(slots) != 50
                or any(type(r.get('before_activation')) is not int or r['before_activation'] != 0
                       or type(r.get('requests')) is not int or r['requests'] < 0 for r in slots)):
            raise ValueError('original route evidence invalid')
        resource = self.resources[key]
        baseline = resource.get('observer_binding')
        if before_activation:
            if baseline is not None or route_report.get('action_id') is not None:
                raise ValueError('observer binding already consumed or released')
            if route_report.get('pending_connections') != 0 or any(r['requests'] for r in slots):
                raise ValueError('model request preceded original activation')
        else:
            if (baseline is None or baseline['caller'] is not caller
                    or baseline['sources'] is not sources or baseline['routes'] is not routes
                    or baseline['generation'] != generation or baseline['route_identity'] != route_identity):
                raise ValueError('original observer binding changed')
            settlement = handle['settlement']
            if settlement is None or route_report.get('action_id') != settlement[2]['action_id']:
                raise ValueError('route action differs from original settlement')
        # Resource/report callbacks can expose mutations; finish with the same
        # original readers and retained objects, never a replacement observer.
        if (sources.current() != generation or routes.current() != route_identity
                or handle['runner'].caller is not caller
                or caller.sources is not sources or caller.routes is not routes):
            raise ValueError('observer binding changed during collection')
        self.health()
        value = dict(generation_sha256=hashlib.sha256(json.dumps(generation, sort_keys=True).encode()).hexdigest(),
                     route_identity=route_identity, source_resources=source_report, routes=route_report)
        if before_activation:
            self.write('observer-baseline-'+key+'.json', value)
            resource['observer_binding'] = dict(caller=caller, sources=sources, routes=routes,
                                               generation=generation, route_identity=route_identity)
        return value

    def collect_bindings(self, phase, *, deadline):
        if phase not in {'settled', 'completed'} or len(self.pool.order) != 10:
            raise ValueError('exact ten cohort observation required')
        rows = {}
        for key in self.pool.order:
            if time.monotonic() >= deadline:
                raise TimeoutError('fleet binding observation deadline expired')
            rows[key] = self.observe_binding(key)
            if time.monotonic() >= deadline:
                raise TimeoutError('fleet binding observation deadline expired')
        writers = self.collect_writers(deadline=deadline)
        result = dict(cohorts=rows, writers=writers, phase=phase, full_500_acceptance=False,
                      scope='Sequential original source watcher and route snapshots; not atomic fleet continuity.')
        self.write('observer-bindings-'+phase+'.json', result)
        self.report['observer_bindings_'+phase] = result

    def collect_writers(self, *, deadline):
        """Reobserve every original writer, including already completed cohorts."""
        rows, sessions, processes, surfaces = {}, set(), set(), set()
        for key in self.pool.order:
            resolvers = self.native_setup.resolvers[key]
            witnesses = self.native_setup.witnesses[key]
            if set(resolvers) != set(range(50)) or set(witnesses) != set(range(50)):
                raise ValueError('exact fifty original writers required')
            cohort = []
            for index in range(50):
                if time.monotonic() >= deadline:
                    raise TimeoutError('original writer observation deadline expired')
                resolver, witness = resolvers[index], copy.deepcopy(witnesses[index])
                if resolver.row != witness:
                    raise ValueError('original writer witness changed')
                # OriginalTranscript performs fresh PID/birth/argv/claim and
                # writable-vnode checks. A pending read is not cached proof.
                path = resolver()
                if path is None:
                    raise ValueError('original writer observation pending')
                if (self.native_setup.resolvers[key] is not resolvers
                        or resolvers[index] is not resolver or resolver.row != witness
                        or self.native_setup.witnesses[key] is not witnesses
                        or witnesses[index] != witness):
                    raise ValueError('original writer changed during observation')
                identity = (witness['pid'], tuple(witness['birth']))
                if (witness['session_id'] in sessions or identity in processes
                        or witness['surface_id'] in surfaces):
                    raise ValueError('duplicate original writer in fleet')
                sessions.add(witness['session_id'])
                processes.add(identity)
                surfaces.add(witness['surface_id'])
                if time.monotonic() >= deadline:
                    raise TimeoutError('original writer observation deadline expired')
                cohort.append(dict(index=index, session_id=witness['session_id'],
                                   pid=witness['pid'], birth=witness['birth'],
                                   surface_id=witness['surface_id'], transcript=str(path)))
            rows[key] = cohort
        self.health()
        if len(rows) != 10 or len(sessions) != 500 or time.monotonic() >= deadline:
            raise ValueError('complete bounded original fleet writer observation required')
        return rows

    def collect_resources(self, *, seconds):
        from tools.standby_resource_sample import capture
        overlap = self.report['process_overlap']
        if overlap.get('process_overlap_proven') is not True:
            raise ValueError('original process overlap required before resource sample')
        self.health()
        auxiliaries = [self.resource_owner]
        if (len(self.resources) != 10 or len(self.pool.order) != 10
                or set(self.resources) != set(self.pool.order)):
            raise ValueError('all ten original cohort resources required')
        for resource in self.resources.values():
            child = resource['daemon']
            pinned = resource['daemon_identity']
            if child.poll() is not None or child.pid != pinned['pid']:
                raise ValueError('original private daemon identity changed')
            auxiliaries.append(pinned)
        ui_bindings = []
        for key in self.pool.order:
            handle = self.pool.handles[key]
            activation = handle.get('ui_activation')
            if (activation is None or activation.ui is None
                    or activation.ui.child is not activation.original_child):
                raise ValueError('original retained UI required')
            ui = activation.ui
            identity = ui.resource_identity()
            ui_bindings.append((key, handle, activation, ui, identity))
            auxiliaries.append(identity)
        result = capture(overlap['originals'], self.root, expected_count=500,
                         seconds=seconds, auxiliaries=auxiliaries)
        self.health()
        for key, handle, activation, ui, identity in ui_bindings:
            if (self.pool.handles[key] is not handle
                    or handle.get('ui_activation') is not activation
                    or activation.ui is not ui
                    or ui.child is not activation.original_child
                    or ui.resource_identity() != identity):
                raise ValueError('original retained UI changed during resource sample')
        result['retained_ui_count'] = len(ui_bindings)
        result['auxiliary_scope'] = ('Original experiment process including provider/proxy/Runner threads '
                                     'and ten retained private daemons and ten original Supervisor UI children; '
                                     'transient children and shared cmux costs excluded.')
        self.write('original-process-resources.json', result)
        self.report['original_process_resources'] = result

    def collect_performance(self, *, deadline=None):
        """Collect real UI timestamps and original native/RPC/delivery chains."""
        from ccc_standby_timing import evaluate as evaluate_startup
        from tools.standby_recovery_chain import evaluate as evaluate_recovery
        from ccc_workspace_batch import PROMPT
        if deadline is None:
            deadline = time.monotonic() + self.seconds
        def check_deadline():
            if time.monotonic() >= deadline:
                raise TimeoutError('original performance observation deadline expired')
        check_deadline()
        if set(self.completions) != set(self.pool.order):
            raise ValueError('all original completions required before performance collection')
        for key in self.pool.order:
            check_deadline()
            self.health()
            resource = self.resources[key]
            handle = self.pool.handles[key]
            jobfile = handle['runner'].owner.service.preparation.jobfile
            timing = Path(jobfile).parent/'standby'
            paths = [timing/'activation-ui.json', timing/'activation-terminal.json',
                     resource['directory']/'rpc-responses.ndjson']
            originals = {p: p.read_bytes() for p in paths}
            startup = evaluate_startup(json.loads(originals[paths[0]]), json.loads(originals[paths[1]]))
            responses = [json.loads(row) for row in originals[paths[2]].splitlines()]
            database = resource['config'].parent/'codex-delivery/delivery.sqlite3'
            with closing(sqlite3.connect(database.as_uri()+'?mode=ro', uri=True)) as connection:
                deliveries = {sid: json.loads(data) for sid, data in
                              connection.execute('SELECT surface_id, record FROM delivery')}
            self.write(key+'/original-deliveries.json', deliveries)
            chains, transcript_bindings = [], []
            resolvers = self.native_setup.resolvers[key]
            if len(resolvers) != 50:
                raise ValueError('exact fifty original transcript resolvers required')
            for index, resolver in resolvers.items():
                check_deadline()
                path = self.fixture.require_original_transcript(resolver, index, 'five_causal_initial')
                check_deadline()
                with path.open('rb') as stream:
                    raw = stream.read(32*1024*1024+1)
                if len(raw) > 32*1024*1024:
                    raise ValueError('original causal transcript exceeds evidence bound')
                records = [json.loads(row) for row in raw.splitlines()]
                chain = evaluate_recovery(records, resolver.row, responses,
                                          deliveries[resolver.row['surface_id']], PROMPT)
                check_deadline()
                if (self.fixture.require_original_transcript(resolver, index, 'five_causal_final') != path
                        or path.read_bytes() != raw):
                    raise ValueError('original causal transcript changed')
                check_deadline()
                target = resource['directory']/f'original-rollout-{index:02d}.jsonl'
                with target.open('xb') as stream:
                    stream.write(raw)
                transcript_bindings.append(dict(index=index, original=str(path), saved=str(target),
                                                sha256=hashlib.sha256(raw).hexdigest()))
                chains.append(chain)
            self.health()
            check_deadline()
            if any(p.read_bytes() != raw for p, raw in originals.items()):
                raise ValueError('timing or RPC evidence changed during performance capture')
            route = handle['runner'].caller.routes.report()
            slots = route.get('slots', [])
            zero = (len(slots) == 50 and not route.get('failed')
                    and route.get('pending_connections') == 0
                    and route.get('unattributed_requests') == 0
                    and all(r.get('before_activation') == 0 for r in slots))
            result = dict(startup=startup, chains=chains, transcript_bindings=transcript_bindings,
                          route=route, zero_requests_before_activation=zero,
                          evidence_sha256={str(p): hashlib.sha256(raw).hexdigest() for p, raw in originals.items()},
                          passed=(startup.get('startup_passed') is True and zero
                                  and all(c.get('causal_chain_verified') is True
                                          and c.get('performance_passed') is True for c in chains)))
            check_deadline()
            self.write(key+'/performance.json', result)
            check_deadline()
            from tools.standby_performance_replay import verify as replay_performance
            replay = replay_performance(result, witnesses=self.native_setup.witnesses[key],
                deliveries_path=self.output/key/'original-deliveries.json', prompt=PROMPT,
                check=check_deadline)
            self.write(key+'/performance-replay.json', replay)
            check_deadline()
            self.metrics[key] = result
        self.report['batch_performance'] = {key: row['passed'] for key, row in self.metrics.items()}
        self.report['performance_passed'] = len(self.metrics) == 10 and all(r['passed'] for r in self.metrics.values())

    def collect_terminals(self):
        """Run after owned resource closure, retaining every declared outcome."""
        from tools.standby_run_observer import settle
        from tools.standby_run_terminal import capture, verify
        if self.pool is None or not self.report.get('scoped_cleanup_complete'):
            raise ValueError('original resource closure required before run terminal')
        terminals, failures = {}, []
        for key in self.pool.order:
            directory = self.resources[key]['directory']/'terminal'
            directory.mkdir(mode=0o700)
            try:
                settle(self.plan, key, directory, self.output/'cleanup-baseline.json',
                       completion_path=self.completions.get(key), client=self.real)
                path = directory/'job-terminal.json'
                if not path.exists():
                    path = self.plan/f'terminal-attempt-{key}.json'
                if not path.is_file():
                    raise ValueError('original terminal artifact missing')
                terminals[key] = str(path)
            except Exception as exc:
                failures.append(dict(batch_id=key, error=repr(exc)))
        self.write('terminal-collection.json', dict(terminals=terminals, failures=failures))
        if failures or set(terminals) != set(self.pool.order):
            raise ValueError('complete declared original terminal set unavailable')
        capture(self.plan, terminals)
        self.report['run_verification'] = verify(self.plan)

    def cleanup(self):
        """Stop retained private handles before touching scoped native workspaces."""
        def attempt(name, action):
            try:
                return action()
            except BaseException as exc:
                self.report['cleanup_errors'].append(dict(step=name, error=repr(exc)))
                return False

        # UI and Runners are drained first. A pending child preserves its resources.
        owners_closed = self.pool is None
        if self.pool is not None:
            deadline = time.monotonic()+30
            while time.monotonic() < deadline:
                rows = attempt('retained_owners', lambda: self.pool.close(timeout=.2))
                if rows is False:
                    break
                if all(not r['alive'] and not r.get('ui_pending') for r in rows.values()):
                    owners_closed = True
                    break
                time.sleep(.1)
        daemons_closed = True
        for key, resource in self.resources.items():
            child = resource['daemon']
            if child is not None:
                def stop(child=child, resource=resource):
                    if child.poll() is None and not resource.get('stop_attempted'):
                        resource['stop_attempted'] = True
                        child.terminate()
                    child.wait(timeout=10)
                    return True
                if attempt('daemon-'+key, stop) is not True:
                    daemons_closed = False
            if child is None or child.poll() is not None:
                if resource['daemon_log'] is not None:
                    attempt('daemon_log-'+key, resource['daemon_log'].close)
        if not owners_closed or not daemons_closed:
            self.report['cleanup_errors'].append(dict(step='preserve_live_resources',
                                                      owners_closed=owners_closed, daemons_closed=daemons_closed))
            return
        workspaces_closed = True
        for item in self.workspaces:
            def close(item=item):
                def matches():
                    tree = self.real.tree()
                    if not isinstance(tree, dict) or not isinstance(tree.get('windows'), list):
                        raise ValueError('workspace absence requires a complete tree')
                    rows = []
                    for window in tree['windows']:
                        if not isinstance(window, dict) or not isinstance(window.get('workspaces'), list):
                            raise ValueError('workspace absence requires complete window rows')
                        for row in window['workspaces']:
                            if not isinstance(row, dict) or not row.get('id') or not isinstance(row.get('title'), str):
                                raise ValueError('workspace absence requires valid identity rows')
                            if row['title'] == item['title'] or (item['id'] and row['id'] == item['id']):
                                rows.append(row)
                    return rows
                rows = matches()
                if not rows:
                    item['closure_observed'] = True
                    return True
                if len(rows) != 1 or rows[0]['title'] != item['title'] or (item['id'] and rows[0]['id'] != item['id']):
                    raise ValueError('owned workspace identity ambiguous; preserve')
                item['id'] = rows[0]['id']
                if not item.get('close_attempted'):
                    item['close_attempted'] = True
                    self.write('workspace-close-'+item['id']+'-intent.json', dict(item))
                    self.real._run(['close-workspace', '--workspace', item['id']], timeout=10)
                deadline = time.monotonic()+10
                while time.monotonic() < deadline:
                    rows = matches()
                    if time.monotonic() >= deadline:
                        raise TimeoutError('workspace observation exceeded closure deadline')
                    if not rows:
                        item['closure_observed'] = True
                        self.write('workspace-close-'+item['id']+'-observed.json', dict(item))
                        return True
                    if len(rows) != 1 or rows[0]['id'] != item['id'] or rows[0]['title'] != item['title']:
                        raise ValueError('workspace identity changed during closure')
                    time.sleep(.1)
                raise TimeoutError('workspace closure not observed; preserve routes')
            if attempt('workspace-'+item['title'], close) is not True:
                workspaces_closed = False
        if not workspaces_closed:
            # A rejected/unknown workspace close can leave original natives using
            # these providers. Preserve their routes rather than strand them.
            self.report['cleanup_errors'].append(dict(step='preserve_workspace_resources'))
            return
        for key, resource in self.resources.items():
            proxy = resource['proxy']
            if proxy is not None:
                if resource['proxy_started']:
                    attempt('proxy_shutdown-'+key, proxy.shutdown)
                attempt('proxy_close-'+key, proxy.server_close)
            if resource['provider'] is not None:
                attempt('provider_close-'+key, resource['provider'].close)
        self.report['scoped_cleanup_complete'] = not self.report['cleanup_errors']

    def acceptance_progress(self):
        """Summarize collected evidence without promoting it to fleet acceptance."""
        keys = set(self.pool.order) if self.pool is not None else set()
        exact = len(keys) == 10
        checks = {
            'ten_cohorts': exact,
            'lifecycle_completed': self.report.get('lifecycle_completed') is True,
            'original_completions': exact and set(self.completions) == keys,
            'performance': exact and set(self.metrics) == keys and all(
                row.get('passed') is True for row in self.metrics.values()),
            'source_unchanged': self.report.get('source_unchanged') is True,
            'scoped_cleanup': self.report.get('scoped_cleanup_complete') is True
                              and self.report.get('cleanup_errors') == [],
            'run_terminal': self.report.get('run_verification', {}).get('run_terminal') is True
                            and self.report.get('run_verification', {}).get('succeeded') is True,
            'no_execution_error': not any(k in self.report for k in ('error', 'terminal_error')),
        }
        overlap = self.report.get('process_overlap', {})
        checks['original_process_overlap'] = (overlap.get('process_overlap_proven') is True
                                              and len(overlap.get('originals', [])) == 500)
        for phase in ('settled', 'completed'):
            binding = self.report.get('observer_bindings_'+phase, {})
            writers = binding.get('writers', {})
            checks['bindings_'+phase] = (exact and binding.get('phase') == phase
                and set(binding.get('cohorts', {})) == keys and set(writers) == keys
                and all(len(rows) == 50 for rows in writers.values()))
        resource = self.report.get('original_process_resources', {})
        checks['resource_sample'] = all(type(resource.get(k)) is int and resource[k] == v
            for k, v in (('native_process_count', 500), ('auxiliary_process_count', 21),
                         ('process_count', 521), ('retained_ui_count', 10)))
        # These are progress indicators, not an independent replay of raw proofs.
        # The CLI remains gated until final evidence replay is wired.
        self.report['acceptance_progress'] = checks
        self.report['remaining_acceptance'] = [k for k, passed in checks.items() if not passed]
        self.report['remaining_acceptance'].append('final_original_evidence_replay_and_entry')
        self.report['passed'] = False
        self.report['full_500_acceptance'] = False

    def run(self):
        try:
            self.setup()
            self.execute()
        except BaseException as exc:
            self.report['error'] = repr(exc)
        finally:
            self.cleanup()
            if self.pool is not None and self.report.get('scoped_cleanup_complete'):
                try:
                    self.collect_terminals()
                except Exception as exc:
                    self.report['terminal_error'] = repr(exc)
            self.report['source_unchanged'] = bool(getattr(self, 'frozen', {})) and all(
                Path(p).is_file() and digest(p) == h for p, h in getattr(self, 'frozen', {}).items())
            self.acceptance_progress()
            self.write('execution-state.json', self.report)
            if all(self.report['acceptance_progress'].values()):
                try:
                    from tools.standby_fleet_replay import verify as replay_fleet
                    proof = replay_fleet(self.output, self.plan)
                    self.write('fleet-original-replay.json', proof)
                    self.report['fleet_original_replay'] = proof
                except Exception as exc:
                    self.report['replay_error'] = repr(exc)
            self.write('result.json', self.report)
        return self.report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--prerequisite', type=Path, required=True)
    parser.add_argument('--fixture', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--seconds', type=float, default=1800)
    parser.add_argument('--run', action='store_true')
    args = parser.parse_args()
    result = preflight(args.source, args.prerequisite)
    if not args.run:
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result['eligible'] else 2
    if not result['eligible']:
        raise ValueError('native launch blocked: '+ '; '.join(result['reasons']))
    if args.fixture is None or args.output is None:
        parser.error('--run requires --fixture and --output')
    final = run_reviewed(args.source, args.prerequisite, args.fixture,
                         args.output, seconds=args.seconds)
    print(json.dumps(final, ensure_ascii=False))
    return 0 if final['passed'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
