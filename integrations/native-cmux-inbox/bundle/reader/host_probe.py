"""Unexecuted private GitHub fixture: native bootstrap, then automatic Post hook.

No GUI/client launcher, sender, mailbox mutation, or business enrollment. The
original complete reader is called in this same native hook process. Recording
callbacks delegate directly to original functions; no host response is mocked.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import fixture_common as f
import cmux_hook_thread as threads
import cmux_inbox_context as inbox
import cmux_native_hook_caller as caller_source
import duplex_capture

need = caller_source.need
canonical = caller_source.canonical


def saved(path, value):
    raw = json.dumps(value, sort_keys=True, indent=2).encode() + b'\n'
    f.write_new(path, raw, 0o400)
    return hashlib.sha256(raw).hexdigest()


def load_private(path, limit=65536):
    raw, signature = caller_source.private_read(path, limit)
    return caller_source.strict_json(raw), raw, signature


def context(root):
    root = f.require_runner(root)
    need(os.environ.get('RUNNER_ENVIRONMENT') == 'github-hosted', 'disposable runner required')
    need(Path.home() == root / 'h' and os.environ.get('CODEX_HOME') == str(root / 'h/.codex'),
         'private HOME/.codex namespace required')
    contract, raw, sig = load_private(root / 'RUN-CONTRACT.json')
    need(contract.get('schema') == 'private-native-hook-host-run-v1'
         and contract.get('run_id') == os.environ['GITHUB_RUN_ID']
         and contract.get('run_attempt') == os.environ['GITHUB_RUN_ATTEMPT']
         and contract.get('fixture_only') is True
         and contract.get('business_controller_migration') is False,
         'missing admitted private-run contract')
    source = Path(__file__).resolve().parent
    manifest_raw = f.pinned(source / 'MANIFEST.json', contract['source_manifest_sha256'])
    manifest = json.loads(manifest_raw)
    actual = {p.relative_to(source).as_posix() for p in source.rglob('*') if p.is_file()
              and p != source / 'MANIFEST.json'}
    need(actual == set(manifest['files']) and not any(p.is_symlink() for p in source.rglob('*')),
         'fixture source membership differs')
    for relative, pin in manifest['files'].items():
        f.pinned(f.contained(source, source / relative), pin['sha256'])
    pin_path = root / 'NATIVE-PIN.json'
    caller = caller_source.NativeHookCaller(pin_path, contract['native_pin_sha256'])
    need(caller.binary.is_relative_to(root), 'native binary must be a private admitted copy')
    check_cli(contract)
    return root, contract, (raw, sig), caller


def check_cli(contract):
    # Preserve original hardcoded reader bytes. Only the disposable runner may
    # provide this initially absent alias; preparation never creates it.
    alias = Path('/Applications/cmux.app')
    app = Path(contract['fixed_app_path'])
    need(contract['fixed_cli_sha256'] == '2c47f937d3f860c9e141486603d9db831de2c9cf2917ab0eb342b778237d4640'
         and app.name == 'cmux Nested Restore 42357868916-1.app'
         and app.is_relative_to(Path(os.environ['RUNNER_TEMP']).resolve(strict=True))
         and alias.is_symlink() and alias.resolve(strict=True) == app.resolve(strict=True)
         and not app.is_symlink() and app == app.resolve(strict=True), 'owned fixed-app alias absent')
    cli = Path(inbox.CMUX)
    need(cli.resolve(strict=True) == app / 'Contents/Resources/bin/cmux'
         and hashlib.sha256(cli.read_bytes()).hexdigest() == contract['fixed_cli_sha256'],
         'original reader CLI does not reach admitted fixed app')


def origin_observation(caller, expected_kind, payload=None):
    """Additional diagnostic query; it does not grant reader/send authority."""
    with caller_source.budget.limit(3.0):
        client, current, chain = caller._client()
        home = Path(client['env']['CODEX_HOME'])
        need(home == Path.home() / '.codex', 'native client namespace differs')
        root = home / 'credential-observations/compaction-recovery'
        marker = caller_source.private_read(root / 'enabled-v1', 64)
        need(marker[0] == b'ccc-native-compaction-recovery-v1\n', 'native marker differs')
        path = root / ('client-%s-control.json' % client['pid'])
        raw, cap_sig = caller_source.private_read(path)
        cap = caller_source.strict_json(raw)
        need(cap.get('schema') == 1 and cap.get('purpose') == 'native_compaction_recovery_control'
             and cap.get('pid') == client['pid'] and cap.get('birth') == client['birth']
             and cap.get('methods') == caller_source.METHODS
             and cap.get('controlRequiresNativeBackend') is True
             and cap.get('callerObservation') == dict(schema=1, method=caller_source.METHOD,
                 scope='caller-thread-readonly', requiresSameProcessSpawn=True), 'wrong native capability')
        epoch = canonical(cap.get('client_epoch'))
        sock = root / ('c-%s-%s.sock' % (client['pid'], epoch[:8]))
        before = sock.lstat()
        need(cap.get('socket') == str(sock) and stat.S_ISSOCK(before.st_mode)
             and before.st_uid == os.getuid() and not before.st_mode & 0o077
             and [before.st_dev, before.st_ino] == [cap.get('socket_device'), cap.get('socket_inode')],
             'native endpoint differs')
        reply, challenge = caller_source.request_caller(sock, client, epoch)
        keys = {'schema','purpose','scope','source','challenge','clientPid','clientBirth','clientEpoch',
                'peerPid','peerBirth','spawnPid','spawnBirth','threadId','callId','origin',
                'foreground','sendAuthority','controllerAuthority','inboxAuthority'}
        need(set(reply) == keys and type(reply['schema']) is int and reply['schema'] == 1
             and reply['purpose'] == 'native_caller_thread'
             and reply['scope'] == 'caller-thread-readonly'
             and reply['source'] == 'same-process-native-spawn'
             and reply['challenge'] == challenge
             and reply['clientPid'] == client['pid'] and reply['clientBirth'] == client['birth']
             and reply['clientEpoch'] == epoch and reply['peerPid'] == current['pid']
             and reply['peerBirth'] == current['birth'] and reply['foreground'] is None
             and all(reply[k] is False for k in ('sendAuthority','controllerAuthority','inboxAuthority')),
             'wrong native caller generation/scope')
        matches = [p for p in [current] + chain if p['pid'] == reply['spawnPid']
                   and p['birth'] == reply['spawnBirth']]
        need(len(matches) == 1 and matches[0]['ppid'] == client['pid'], 'caller is not an actual native spawn')
        need(isinstance(reply['callId'], str) and 0 < len(reply['callId']) <= 256, 'native call ID absent')
        if expected_kind == 'unified-exec':
            need(reply['origin'] == {'kind': 'unified-exec'}, 'bootstrap must be an actual ToolCtx exec')
        else:
            need(isinstance(payload, dict) and payload.get('hook_event_name') == 'PostToolUse'
                 and payload.get('tool_name') == 'Bash', 'automatic Post payload required')
            expected = dict(kind='native-command-hook', event='PostToolUse',
                            turnId=payload.get('turn_id'), toolUseId=payload.get('tool_use_id'),
                            handlerId=caller.pin['handler_id'])
            need(reply['origin'] == expected and reply['callId'] == expected['toolUseId']
                 and all(isinstance(expected[k], str) and 0 < len(expected[k]) <= 256
                         for k in ('turnId', 'toolUseId')), 'not the registered native automatic Post hook')
        canonical(reply['threadId'])
        need(caller._client() == (client, current, chain)
             and caller_source.private_read(path) == (raw, cap_sig)
             and caller_source.signature(sock.lstat()) == caller_source.signature(before)
             and caller_source.private_read(root / 'enabled-v1', 64) == marker,
             'native process/endpoint changed during query')
        record = dict(reply=reply, challenge=challenge, capability_sha256=hashlib.sha256(raw).hexdigest(),
                      client=dict(pid=client['pid'], birth=client['birth']),
                      current=dict(pid=current['pid'], birth=current['birth'], ppid=current['ppid']),
                      socket=dict(path=str(sock), device=before.st_dev, inode=before.st_ino))
        return record, client


def bootstrap(root):
    root, contract, contract_pin, caller = context(root)
    need(not (root / 'BINDING.json').exists(), 'original private binding already exists')
    record, client = origin_observation(caller, 'unified-exec')
    sid = canonical(record['reply']['threadId'])
    paths = list((Path.home() / '.codex/sessions').rglob('*-' + sid + '.jsonl'))
    need(len(paths) == 1, 'unique actual root rollout required')
    payload = dict(session_id=sid, transcript_path=str(paths[0]))
    facts = threads.describe(payload)
    need(facts.get('thread_scope') == 'ROOT_METADATA' and facts.get('actual_thread_id') == sid,
         'child/foreign metadata cannot establish root fixture policy')
    writer = caller._writer(client, facts)
    with caller_source.budget.limit(3.0):
        tty = subprocess.run(['/bin/ps', '-p', str(client['pid']), '-o', 'tty='],
                             check=True, capture_output=True, text=True,
                             timeout=caller_source.budget.timeout()).stdout.strip()
        identity = caller_source.workspace._read_json_command('identify', '--json')
        tree = caller_source.workspace._read_json_command('tree', '--workspace',
               canonical(client['env']['CMUX_WORKSPACE_ID']).upper(), '--json', '--id-format', 'both')
        caller_source.workspace.daemon_identity.resolve(identity, tree, os.environ,
             dict(source='embedded-native-client-v1', daemon=None, client=dict(client, tty=tty)))
        need(caller._client()[0] == client and caller._writer(client, facts) == writer,
             'root changed before private policy freeze')
    actor = dict(session_id=sid, workspace_uuid=canonical(client['env']['CMUX_WORKSPACE_ID']),
                 surface_uuid=canonical(client['env']['CMUX_SURFACE_ID']))
    policy = dict(schema='cmux-inbox-context-policy-v1', actors=[actor])
    need(load_private(root / 'RUN-CONTRACT.json')[1:] == contract_pin, 'run contract changed')
    policy_sha = saved(root / 'ACTOR-POLICY.json', policy)
    binding = dict(schema='private-native-hook-host-binding-v1', fixture_only=True,
        policy_sha256=policy_sha, actor=actor, native_pin_sha256=contract['native_pin_sha256'],
        client=record['client'], bootstrap=record, root_metadata=facts, writer=writer,
        host_identity=identity, host_tree=tree, business_controller_migration=False,
        business_task_registration=False, task_acceptance=False)
    digest = saved(root / 'BINDING.json', binding)  # O_EXCL; hook never creates or replaces this.
    print('HOST_FIXTURE_BOOTSTRAP:' + digest, flush=True)


def automatic(root):
    root, contract, contract_pin, caller = context(root)
    raw = sys.stdin.buffer.read(inbox.MAX_INPUT + 1)
    need(0 < len(raw) <= inbox.MAX_INPUT, 'actual hook stdin bound')
    payload = caller_source.strict_json(raw)
    record, client = origin_observation(caller, 'native-command-hook', payload)
    binding, binding_raw, binding_sig = load_private(root / 'BINDING.json', 256 * 1024)
    need(binding.get('schema') == 'private-native-hook-host-binding-v1'
         and binding.get('fixture_only') is True and binding.get('business_controller_migration') is False
         and binding['client'] == record['client']
         and binding['native_pin_sha256'] == contract['native_pin_sha256'], 'foreign private binding')
    facts = threads.describe(payload)
    need(facts.get('actual_thread_id') == record['reply']['threadId'], 'hook metadata/native thread disagree')
    role = 'root' if facts['actual_thread_id'] == binding['actor']['session_id'] else 'child'
    need((role == 'root' and facts.get('thread_scope') == 'ROOT_METADATA')
         or (role == 'child' and facts.get('thread_scope') == 'CHILD_METADATA'
             and facts.get('parent_thread_id') == binding['actor']['session_id']), 'unrelated hook thread')
    out = root / ('host-hook-%s-%s' % (record['current']['pid'], record['reply']['origin']['event']))
    out.mkdir(mode=0o700)
    f.write_new(out / 'STDIN.json', raw, 0o400)
    saved(out / 'NATIVE-ORIGIN.json', record)
    calls, queries, emissions = [], [], []

    def real_caller(p, metadata):
        value = caller(p, metadata)
        calls.append(value)
        return value

    def real_query(surface):
        check_cli(contract)
        value = duplex_capture.inbox(surface, out, inbox)
        queries.append(value)  # Same real CLI/argv/parser; original stdout and stderr also sealed.
        return value

    def real_emit(value):
        need(load_private(root / 'BINDING.json', 256 * 1024)[1:] == (binding_raw, binding_sig)
             and load_private(root / 'RUN-CONTRACT.json')[1:] == contract_pin, 'fixture binding changed')
        check_cli(contract)
        inbox.emit_context(value)  # actual stdout flush; failures cannot advance original cursor
        emissions.append(value)

    notices = inbox.discover(payload, inbox.read_policy(root / 'ACTOR-POLICY.json', binding['policy_sha256']),
           root / 'cursor', native_caller=real_caller, query=real_query, emit=real_emit)
    if role == 'child':
        need(not calls and not queries and not emissions and not notices, 'child reached root inbox')
        status = 'ACTUAL_CHILD_REFUSED_BEFORE_INBOX'
    else:
        need(len(queries) == 1 and calls, 'root full native/host reader did not reach actual inbox')
        status = 'FULL_READER_CONTEXT_EMITTED' if emissions else 'FULL_READER_CALLED_NO_NEW_CONTEXT'
        if emissions:
            need(len(calls) == 2 and len(emissions) == 1 and notices, 'incomplete full-reader output path')
    saved(out / 'RESULT.json', dict(status=status, role=role, metadata=facts, native_origin=record,
        full_reader_calls=calls, actual_parsed_host_queries=queries, emitted_contexts=emissions,
        notices=notices, source_modules_modified=False, host_mocks=False,
        manual_hook=False, mailbox_mutations=0, terminal_inputs=0,
        body_read_verified=False, bidirectional_receipt_verified=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--mode', choices=('bootstrap', 'hook'), required=True)
    args = parser.parse_args()
    if args.mode == 'bootstrap':
        bootstrap(args.root)
    else:
        try:
            automatic(args.root)
        except Exception as error:
            # Same nonblocking hook semantics. Record failure without claiming
            # zero exit is acceptance; never synthesize extra model context.
            try:
                root = f.require_runner(args.root)
                saved(root / ('HOST-HOOK-FAILURE-%s.json' % os.getpid()),
                      dict(status='FAILED_NOT_ACCEPTANCE', type=type(error).__name__, error=str(error)))
            except Exception:
                pass
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
