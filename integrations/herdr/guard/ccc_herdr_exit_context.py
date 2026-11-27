"""Historical launch evidence for the ORIGINAL Herdr guard; no input or launch.

Capture uses the guard's current selected-thread reader and native process
reader. A saved document is a recovery locator, never current-thread authority.
This module deliberately has no transport, subprocess, signal or exec method.
"""
from __future__ import annotations

from ccc_public_protection import PUBLIC_PROTECTED_PID, PUBLIC_PROTECTED_SURFACE
import hashlib
import json
import os
from pathlib import Path
import stat
import uuid

SCHEMA = 'ccc-original-exit-context-v1'
PROTECTED = PUBLIC_PROTECTED_SURFACE
MAX_DOCUMENT = 2 * 1024 * 1024


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def need(condition, reason):
    if not condition:
        raise ValueError(reason)


def sid(value):
    need(isinstance(value, str) and str(uuid.UUID(value)) == value,
         'noncanonical_original_sid')
    return value


def history_header(evidence, session_id):
    """Read only the exact kernel-bound header. Appends are allowed, swaps not."""
    path = Path(evidence['path'])
    need(path.is_absolute() and path.resolve(strict=True) == path,
         'original_history_path_not_canonical')
    expected = (evidence['device'], evidence['inode'])
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        need(stat.S_ISREG(before.st_mode) and before.st_uid == os.getuid()
             and not before.st_mode & 0o022
             and (before.st_dev, before.st_ino) == expected,
             'original_history_kernel_identity_changed')
        raw = stream.readline(256 * 1024)
        need(raw.endswith(b'\n'), 'original_history_header_incomplete')
        event = json.loads(raw)
        need(event.get('type') == 'session_meta'
             and event.get('payload', {}).get('id') == session_id,
             'original_history_header_sid_mismatch')
        after = os.fstat(stream.fileno())
    current = path.lstat()
    need((current.st_dev, current.st_ino) == expected
         and (after.st_dev, after.st_ino) == expected
         and after.st_size >= before.st_size
         and current.st_size >= before.st_size,
         'original_history_replaced_or_truncated')
    return dict(path=str(path), device=before.st_dev, inode=before.st_ino,
                observed_size=before.st_size, header_bytes=len(raw),
                header_sha256=hashlib.sha256(raw).hexdigest())


def capture(guard, codex, endpoint, pane, state):
    """Compose existing readers; never trust startup SID, labels or old status.

    Called only for a changed live generation by the original single writer.
    No observer constructor is invoked and no independent guard is created.
    """
    need(endpoint['cmux_surface_id'].upper() != PROTECTED,
         'protected_business_surface')
    need(endpoint.get('discovered_read_only') is not True,
         'monitor_only_endpoint')
    process = guard.foreground(endpoint, pane, guard.servers[endpoint['session']])
    need(process is not None and process.get('agent_kind') == 'codex'
         and process.get('pid') != PUBLIC_PROTECTED_PID, 'original_codex_not_foreground')
    turn, _ = codex.read_current(guard, endpoint, pane, process, state)
    need(turn.get('kind') != 'unknown' and codex.selected_current_thread(turn),
         'current_selected_thread_unverified')
    # The official reader exposes both exact kernel vnodes. Other readers may
    # be added only when their distinct evidence contract is implemented.
    proof = turn.get('native_selection_evidence') or {}
    need(turn.get('native_selection_source') == 'official_statusline_native_writer'
         and proof.get('schema') == 'ccc-official-statusline-native-writer-v1',
         'unsupported_original_reader_evidence')
    session_id = sid(turn['session_id'])
    binding = proof['binding']
    expected = dict(socket=endpoint['socket'], session=endpoint['session'],
                    pane_id=pane['pane_id'], terminal_id=pane['terminal_id'],
                    pid=process['pid'], birth=process['birth'],
                    cmux_workspace_id=endpoint['cmux_workspace_id'],
                    cmux_surface_id=endpoint['cmux_surface_id'],
                    server=guard.servers[endpoint['session']])
    need(binding == expected and proof.get('session_id') == session_id
         and turn.get('pid') == process['pid'] and turn.get('birth') == process['birth'],
         'original_reader_binding_mismatch')
    launch = guard.scope.process(process['pid'], launch=True)
    need(isinstance(launch, dict) and launch.get('birth') == process['birth']
         and launch.get('argv') == process['argv']
         and launch.get('environment') == process['environment']
         and not launch.get('remote') and not launch.get('backend'),
         'native_launch_observation_changed')
    need(isinstance(launch.get('cwd'), str) and Path(launch['cwd']).is_absolute()
         and Path(launch['cwd']).is_dir(), 'native_working_directory_unavailable')
    home = Path(process['environment'].get('CODEX_HOME', str(Path.home() / '.codex')))
    need(home.is_absolute() and home.resolve(strict=True) == home,
         'native_codex_home_not_canonical')
    writer = proof['writer_lock']
    need(Path(writer['path']) == home / 'thread-writer-locks' / (session_id + '.lock')
         and Path(proof['rollout']['path']).is_relative_to(home / 'sessions'),
         'original_history_namespace_mismatch')
    header = history_header(proof['rollout'], session_id)
    from ccc_resume_capture import collect
    configuration = collect(guard, dict(schema=SCHEMA, session_id=session_id,
        binding=binding, codex_home=str(home), history=header,
        original_launch_observation=launch, executable=process['executable']))
    # Do not cache a transient UNKNOWN under the completed format identity.
    # Retain the earlier historical records; the original monitor retries this
    # generation after its existing cooldown, without any recovery input.
    need(configuration.get('state') == 'HISTORICAL_CONFIGURATION_OBSERVED',
         'original_configuration_unverified:' + configuration.get('reason', 'unknown'))
    # Capture the launch *observed now*, not a guessed historical invocation.
    # argv can contain an initial prompt or an old resume ID. It must never be
    # executed wholesale; deriving a resume invocation is a separate action.
    current = guard.foreground(endpoint, pane, guard.servers[endpoint['session']])
    need(current == process, 'foreground_changed_during_capture')
    turn2, _ = codex.read_current(guard, endpoint, pane, current, state)
    need(turn2 == turn, 'selected_thread_or_native_lifecycle_changed')
    launch2 = guard.scope.process(process['pid'], launch=True)
    need(launch2 == launch, 'native_launch_changed_during_capture')
    document = dict(schema=SCHEMA, capture_format=2,
        configuration_evidence=configuration, binding=binding, session_id=session_id,
        codex_home=str(home), history=header, writer_lock=writer,
        original_launch_observation=launch,
        executable=process['executable'],
        executable_sha256=proof['official_binary_sha256'],
        selected_reader_evidence=proof, original_turn_signature=turn.get('signature'),
        current_thread_authority=False, input_authority=False,
        recovery_available=False, executable_resume_plan=False,
        no_business_restart=True)
    need(len(canonical(document)) <= MAX_DOCUMENT, 'original_context_too_large')
    return document


class PinnedDirectory:
    """All file operations stay on the opened directory, even after a rename.

    Path checks detect namespace replacement; they are not an atomic lock
    against an external same-user writer. A refused publish may leave a whole
    immutable document in the original directory, never in its replacement.
    """
    def __init__(self, path, *, expected=None, forbidden_mode=0o077):
        self.path = Path(path)
        self.expected = expected
        self.forbidden_mode = forbidden_mode
        self.fd = None

    def __enter__(self):
        need(self.path.is_absolute() and self.path.resolve(strict=True) == self.path,
             'context_directory_not_private')
        self.fd = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(self.fd)
            self.identity = (info.st_dev, info.st_ino)
            need(self.expected is None or tuple(self.expected) == self.identity,
                 'context_directory_identity_changed')
            self.check()
        except BaseException:
            self.__exit__()
            raise
        return self

    def __exit__(self, *unused):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def check(self):
        opened = os.fstat(self.fd)
        current = self.path.lstat()
        need(self.path.resolve(strict=True) == self.path
             and all(stat.S_ISDIR(s.st_mode) and s.st_uid == os.getuid()
                     and not s.st_mode & self.forbidden_mode
                     and (s.st_dev, s.st_ino) == self.identity
                     for s in (opened, current)), 'context_directory_identity_changed')

    @staticmethod
    def name(value):
        need(isinstance(value, str) and value not in {'', '.', '..'}
             and '/' not in value and '\x00' not in value, 'invalid_context_filename')
        return value

    def stat(self, name):
        return os.stat(self.name(name), dir_fd=self.fd, follow_symlinks=False)

    def read(self, name, limit=MAX_DOCUMENT):
        self.check()
        try:
            before = self.stat(name)
        except FileNotFoundError:
            return None
        need(stat.S_ISREG(before.st_mode) and before.st_uid == os.getuid()
             and before.st_nlink == 1 and not before.st_mode & 0o077
             and before.st_size <= limit, 'existing_context_not_private')
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.fd)
        with os.fdopen(fd, 'rb') as stream:
            opened = os.fstat(stream.fileno())
            raw = stream.read(limit + 1)
            after = os.fstat(stream.fileno())
        ident = lambda s: (s.st_dev, s.st_ino, s.st_mode, s.st_uid, s.st_nlink,
                           s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        need(len(raw) <= limit and ident(before) == ident(opened) == ident(after)
             == ident(self.stat(name)), 'existing_context_changed')
        self.check()
        return raw

    def publish(self, name, raw, *, replace=False, expected=None):
        self.name(name)
        self.check()
        temporary = '.capture-' + uuid.uuid4().hex
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=self.fd)
        original = os.fstat(fd)
        created = False
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            self.check()
            if replace:
                need(self.read(name) == expected, 'context_index_external_change')
                self.check()
                os.replace(temporary, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
                created = True
            else:
                try:
                    os.link(temporary, name, src_dir_fd=self.fd, dst_dir_fd=self.fd,
                            follow_symlinks=False)
                    created = True
                except FileExistsError:
                    pass
            self.check()
        finally:
            try:
                current = self.stat(temporary)
            except FileNotFoundError:
                current = None
            if current is not None:
                need((current.st_dev, current.st_ino) == (original.st_dev, original.st_ino),
                     'temporary_context_replaced')
                os.unlink(temporary, dir_fd=self.fd)
        need(self.read(name) == raw, 'existing_context_changed')
        os.fsync(self.fd)
        self.check()
        return created


def store_once(directory, document, *, pinned=None):
    """Content-addressed private evidence. No overwrite or mutation of ledgers."""
    need(document.get('schema') == SCHEMA and document.get('input_authority') is False
         and document.get('executable_resume_plan') is False,
         'invalid_context_document')
    raw = canonical(document) + b'\n'
    need(len(raw) <= MAX_DOCUMENT, 'original_context_too_large')
    digest = hashlib.sha256(raw).hexdigest()
    if pinned is None:
        with PinnedDirectory(directory) as opened:
            return store_once(directory, document, pinned=opened)
    need(pinned.path == Path(directory), 'context_directory_argument_changed')
    name = digest + '.json'
    existing = pinned.read(name)
    if existing is not None:
        need(existing == raw, 'existing_context_changed')
        os.fsync(pinned.fd)
        pinned.check()
        created = False
    else:
        created = pinned.publish(name, raw)
    return dict(path=str(pinned.path / name), sha256=digest, created=created)


def exit_disposition(document, *, original_process_gone, current_binding,
                     shell_empty, no_writer_verified, goal_status,
                     last_event, no_business_restart=True):
    """Classify a proposed same-session recovery without authorizing input."""
    need(document.get('schema') == SCHEMA, 'unsupported_context_schema')
    original = document['binding']
    keys = ('socket', 'session', 'pane_id', 'terminal_id', 'cmux_workspace_id',
            'cmux_surface_id', 'server')
    same_terminal = all(current_binding.get(k) == original.get(k) for k in keys)
    reason = 'original_process_still_present'
    if original_process_gone is True:
        if not same_terminal:
            reason = 'original_terminal_generation_changed'
        elif last_event == 'turn_aborted':
            reason = 'user_abort_preserved'
        elif goal_status not in {'active', 'blocked'}:
            reason = 'original_goal_' + str(goal_status)
        elif shell_empty is not True:
            reason = 'original_shell_input_not_empty_or_unknown'
        elif no_writer_verified is not True:
            reason = 'original_no_writer_unverified'
        elif no_business_restart:
            reason = 'all_business_agents_no_restart'
        else:
            reason = 'resume_argv_and_one_use_original_intent_required'
    return dict(schema='ccc-original-exit-disposition-v1', reason=reason,
        saved_session_id=document['session_id'], input_operations=0,
        recovery_available=False, current_thread_authority=False,
        attention_required=(same_terminal and original_process_gone is True
            and goal_status in {'active', 'blocked'} and last_event != 'turn_aborted'))


def prepare_same_session(document):
    """Reviewable argv only, with no execution or original-task authority.

    Preserve recognized native launch flags byte-for-byte. Initial prompts,
    picker selectors and a different startup SID are not replayable evidence.
    The existing controller must additionally verify the effective model,
    provider configuration, writer absence and new native receipt.
    """
    need(document.get('schema') == SCHEMA, 'unsupported_context_schema')
    session_id = sid(document['session_id'])
    argv = document['original_launch_observation']['argv']
    need(isinstance(argv, list) and len(argv) >= 3
         and all(isinstance(x, str) and '\x00' not in x for x in argv),
         'invalid_original_argv')
    need(argv[-2:] == ['resume', session_id],
         'original_argv_is_not_exact_same_session_resume')
    prefix = argv[1:-2]
    i = 0
    while i < len(prefix):
        option = prefix[i]
        if option in {'--no-daemon', '--no-alt-screen'}:
            i += 1
        elif option in {'-c', '--config'} and i + 1 < len(prefix):
            need('=' in prefix[i + 1] and not any(c in prefix[i + 1] for c in '\r\n'),
                 'invalid_original_config_override')
            i += 2
        else:
            raise ValueError('unreviewed_original_launch_option')
    executable = document['executable']
    need(isinstance(executable, str) and Path(executable).is_absolute(),
         'original_executable_not_absolute')
    return dict(schema='ccc-original-resume-plan-v1',
        status='REVIEWABLE_NOT_EXECUTABLE', session_id=session_id,
        argv=[executable, *argv[1:]],
        cwd=document['original_launch_observation']['cwd'],
        environment_source='original_launch_observation.environment',
        executable_sha256=document['executable_sha256'],
        required_before_any_launch=[
            'explicit_original_recovery_policy_without_business_restart_conflict',
            'current_original_terminal_binding_and_empty_shell',
            'original_process_gone_and_fresh_native_no_writer_proof',
            'same_official_client_and_effective_model_provider_config',
            'original_controller_one_use_intent_and_native_same_sid_receipt'],
        input_operations=0, execution_authorized=False)


class ExitContextMonitor:
    """Historical capture inside the original owner loop, with no new sender."""
    def __init__(self, state, *, clock, provenance=None, limit=128):
        self.state = Path(state)
        self.directory = self.state / 'original-exit-contexts'
        self.clock, self.limit = clock, limit
        self.provenance = provenance
        self.directory_identity = None
        self.index = self.index_raw = None
        self.failures = {}
        self.cursor = 0

    def _owned_state(self, guard):
        expected = guard.guard_config['original_stores']
        need(str(self.state) == expected['state'], 'original_state_directory_changed')
        info = self.state.lstat()
        need([info.st_dev, info.st_ino] == expected['state_identity']
             and self.state.resolve(strict=True) == self.state
             and stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
             and not info.st_mode & 0o022, 'original_state_directory_changed')

    def _read_index(self, directory):
        return directory.read('index.json', 512 * 1024)

    def _load_index(self, directory, state_directory):
        self.directory_identity = directory.identity
        with os.scandir(directory.fd) as entries:
            for count, _ in enumerate(entries, 1):
                need(count <= self.limit * 2 + 1, 'context_directory_capacity_attention')
        raw = self._read_index(directory)
        if self.index is None:
            value = json.loads(raw) if raw is not None else {
                'schema': 'ccc-exit-context-index-v2', 'entries': {},
                'directory_identity': list(directory.identity),
                'state_identity': list(state_directory.identity)}
            need(value.get('schema') == 'ccc-exit-context-index-v2'
                 and isinstance(value.get('entries'), dict)
                 and len(value['entries']) <= self.limit
                 and value.get('directory_identity') == list(directory.identity)
                 and value.get('state_identity') == list(state_directory.identity),
                 'invalid_context_index_or_directory')
            self.index, self.index_raw = value, raw
        need(raw == self.index_raw, 'context_index_external_change')

    @staticmethod
    def _identity(row, launch):
        need(type(row.get('pid')) is int and row['pid'] > 1
             and isinstance(row.get('birth'), list) and len(row['birth']) == 2
             and all(type(n) is int and n >= 0 for n in row['birth']),
             'invalid_current_process_identity')
        need(isinstance(launch, dict) and launch.get('pid') == row['pid']
             and launch.get('birth') == row['birth']
             and not launch.get('remote') and not launch.get('backend'),
             'current_native_launch_unverified')
        evidence = row.get('native_selection_evidence')
        need(isinstance(evidence, dict) and evidence.get('schema') ==
             'ccc-official-statusline-native-writer-v1', 'current_selection_evidence_missing')
        value = {k: row[k] for k in ('key', 'session', 'pane_id', 'terminal_id',
                    'pid', 'birth', 'cmux_workspace_id', 'cmux_surface_id', 'server')}
        value['session_id'] = sid(row['native_session_id'])
        value['reader_evidence_sha256'] = hashlib.sha256(canonical(evidence)).hexdigest()
        value['launch_sha256'] = hashlib.sha256(canonical(launch)).hexdigest()
        value['capture_format'] = 2
        return hashlib.sha256(canonical(value)).hexdigest(), value

    @staticmethod
    def _match_document(doc, identity):
        expected = dict(identity)
        key, selected = expected.pop('key'), expected.pop('session_id')
        reader_sha, launch_sha = expected.pop('reader_evidence_sha256'), expected.pop('launch_sha256')
        need(doc.get('capture_format') == expected.pop('capture_format'),
             'stored_context_format_changed')
        bound = {k: doc['binding'].get(k) for k in expected}
        need(bound == expected and doc['session_id'] == selected
             and doc['binding']['socket'] + '#' + doc['binding']['pane_id'] == key
             and hashlib.sha256(canonical(doc['selected_reader_evidence'])).hexdigest() == reader_sha
             and hashlib.sha256(canonical(doc['original_launch_observation'])).hexdigest() == launch_sha,
             'stored_context_binding_changed')

    def _existing(self, directory, entry, identity):
        need(entry['identity'] == identity, 'context_index_identity_conflict')
        digest = entry['sha256']
        need(isinstance(digest, str) and len(digest) == 64
             and all(c in '0123456789abcdef' for c in digest), 'context_digest_invalid')
        raw = directory.read(digest + '.json')
        need(raw is not None and hashlib.sha256(raw).hexdigest() == digest,
             'stored_context_changed')
        doc = json.loads(raw)
        self._match_document(doc, identity)
        need(doc.get('capture_owner') == entry.get('capture_owner')
             and isinstance(doc.get('capture_owner'), dict), 'stored_context_owner_missing')
        return digest

    def tick(self, guard, codex, snapshot, state, *, owner_recheck):
        result = {'schema': 'ccc-exit-context-monitor-v2', 'mode': 'historical_capture_only',
            'input_operations': 0, 'recovery_available': False, 'new_captures': 0,
            'current_contexts': 0, 'held': 0, 'pending': 0, 'errors': []}
        if guard.guard_config.get('original_exit_capture', {}).get('enabled') is not True:
            return dict(result, phase='disabled')
        try:
            need(Path(state) == self.state, 'original_state_argument_changed')
            owner_recheck()
            self._owned_state(guard)
            stores = guard.guard_config['original_stores']
            need(isinstance(self.provenance, dict)
                 and self.provenance.get('original_stores') == stores,
                 'original_capture_owner_unbound')
            with PinnedDirectory(self.state, expected=stores['state_identity'],
                                 forbidden_mode=0o022) as state_directory:
                try:
                    os.mkdir(self.directory.name, mode=0o700, dir_fd=state_directory.fd)
                except FileExistsError:
                    pass
                state_directory.check()
                info = state_directory.stat(self.directory.name)
                identity = self.directory_identity or (info.st_dev, info.st_ino)
                with PinnedDirectory(self.directory, expected=identity) as directory:
                    self._load_index(directory, state_directory)
                    return self._tick(guard, codex, snapshot, owner_recheck,
                                      state_directory, directory, result)
        except Exception as error:
            # The original continuation loop and its input accounting survive.
            for row in snapshot.get('agents', []):
                if row.get('original_exit_context', {}).get('phase') == 'historical_context_available':
                    row['original_exit_context']['phase'] = 'capture_attention'
            return dict(result, phase='attention', current_contexts=0,
                        errors=[type(error).__name__ + ': ' + str(error)[:200]])

    def _tick(self, guard, codex, snapshot, owner_recheck, state_directory, directory, result):
        pending = []
        for row in snapshot.get('agents', []):
            if row.get('agent') != 'codex':
                continue
            public = {'phase': 'reader_unverified', 'input_authority': False,
                      'recovery_available': False,
                      'effective_configuration_verified': False}
            row['original_exit_context'] = public
            if row.get('cmux_surface_id', '').upper() == PROTECTED or row.get('pid') == PUBLIC_PROTECTED_PID:
                public['phase'] = 'protected'
                continue
            if row.get('auto_input_enabled') is not True or row.get('discovered_read_only') is True:
                public['phase'] = 'not_enrolled'
                continue
            if (row.get('process_bound') is not True or row.get('native_reader_available') is not True
                    or row.get('native_selection_source') != 'official_statusline_native_writer'):
                result['held'] += 1
                continue
            try:
                launch = guard.scope.process(row['pid'], launch=True)
                key, identity = self._identity(row, launch)
                if key in self.index['entries']:
                    public.update(phase='historical_context_available',
                        sha256=self._existing(directory, self.index['entries'][key], identity))
                    result['current_contexts'] += 1
                else:
                    public['phase'] = 'capture_pending'
                    result['pending'] += 1
                    if self.failures.get(key, 0) <= self.clock():
                        pending.append((key, identity, row))
            except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
                # One unavailable/corrupt panel must not starve the others.
                # Directory corruption still fails the final pinned checks.
                public.update(phase='capture_attention', reason=type(error).__name__)
                result['held'] += 1
                result['errors'].append(type(error).__name__ + ': ' + str(error)[:200])
        if not pending:
            state_directory.check(); directory.check()
            return dict(result, phase='attention' if result['errors'] else 'observed')
        if len(self.index['entries']) >= self.limit:
            return dict(result, phase='capacity_attention', held=result['held'] + len(pending))
        key, identity, row = pending[self.cursor % len(pending)]
        self.cursor += 1
        self.failures[key] = self.clock() + 60
        endpoint = next(e for e in guard.config['endpoints'] if e['session'] == row['session'])
        matches = [p for p in guard.pane_snapshots.get(row['session'], {}).get('panes', [])
                   if p['pane_id'] == row['pane_id'] and p['terminal_id'] == row['terminal_id']]
        need(len(matches) == 1, 'current_capture_pane_unavailable')
        document = capture(guard, codex, endpoint, matches[0], self.state)
        self._match_document(document, identity)
        document['capture_owner'] = self.provenance
        owner_recheck(); state_directory.check(); directory.check()
        need(self._read_index(directory) == self.index_raw, 'context_index_external_change')
        receipt = store_once(self.directory, document, pinned=directory)
        value = dict(self.index, entries=dict(self.index['entries']))
        value['entries'][key] = {'identity': identity, 'sha256': receipt['sha256'],
                                 'capture_owner': self.provenance}
        raw = canonical(value) + b'\n'
        need(len(raw) <= 512 * 1024, 'context_index_capacity_attention')
        owner_recheck(); state_directory.check(); directory.check()
        directory.publish('index.json', raw, replace=True, expected=self.index_raw)
        need(self._read_index(directory) == raw, 'published_context_index_changed')
        state_directory.check(); directory.check()
        self.index, self.index_raw = value, raw
        row['original_exit_context'].update(phase='historical_context_available', sha256=receipt['sha256'])
        return dict(result, phase='captured', new_captures=1,
                    pending=result['pending'] - 1, current_contexts=result['current_contexts'] + 1)
