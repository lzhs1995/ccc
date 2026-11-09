"""Actual native ToolCtx sender/body reader for one admitted disposable fixture.

Never invoked by an automatic hook. Sender environment is authenticated against
the original native process and host endpoint; RPC sender fields alone prove no
ownership. There is exactly one send per root, and no retry/claim/mark-read.
"""
import argparse
import base64
import json
import os
from pathlib import Path
import subprocess

import fixture_common as f
import host_probe as h
import cmux_hook_thread as threads
import duplex_protocol as p
import duplex_capture


def authenticated(root, pair, label):
    root, contract, contract_pin, caller = h.context(root)
    actors = p.endpoints(pair)
    own = actors[label]
    f.need(str(root) == own['root'], 'foreign actor directory')
    for row in actors.values():
        f.need(json.loads(f.pinned(Path(row['root'])/'BINDING.json', row['binding_sha256'])) == row['binding']
               and json.loads(f.pinned(Path(row['root'])/'CLIENT.json', row['client_sha256'])) == row['client'],
               'original pair binding changed')
    observation, client = h.origin_observation(caller, 'unified-exec')
    binding = own['binding']
    actor = binding['actor']
    f.need(observation['client'] == binding['client']
           and observation['reply']['threadId'] == actor['session_id']
           and client['env']['CMUX_WORKSPACE_ID'].lower() == actor['workspace_uuid']
           and client['env']['CMUX_SURFACE_ID'].lower() == actor['surface_uuid'],
           'send/read must originate in the actual enrolled root ToolCtx')
    payload = dict(session_id=actor['session_id'], transcript_path=binding['root_metadata']['transcript_path'])
    facts = threads.describe(payload)
    f.need(facts == binding['root_metadata'] and facts['thread_scope'] == 'ROOT_METADATA', 'root metadata changed')
    writer = caller._writer(client, facts)
    with h.caller_source.budget.limit(3.0):
        tty = subprocess.run(['/bin/ps', '-p', str(client['pid']), '-o', 'tty='],
            capture_output=True, text=True, check=True, timeout=h.caller_source.budget.timeout()).stdout.strip()
        f.need(tty and '/dev/'+tty == own['client']['tty'], 'live original native TTY changed')
        identity = h.caller_source.workspace._read_json_command('identify', '--json')
        tree = h.caller_source.workspace._read_json_command('tree', '--workspace',
                      actor['workspace_uuid'].upper(), '--json', '--id-format', 'both')
        h.caller_source.workspace.daemon_identity.resolve(identity, tree, os.environ,
             dict(source='embedded-native-client-v1', daemon=None, client=dict(client, tty=tty)))
    f.need(caller._client()[0] == client and caller._writer(client, facts) == writer, 'actual root changed')
    return caller, client, dict(native_origin=observation, root_metadata=facts, writer=writer,
                               host_identity=identity, host_tree=tree), (contract, contract_pin)


def execute(root, ticket_path, ticket_sha):
    root = f.require_runner(root)
    f.contained(root, ticket_path)
    ticket = json.loads(f.pinned(ticket_path, ticket_sha))
    f.need(ticket['schema'] == 'private-native-duplex-tool-v1'
           and ticket['mode'] in ('send', 'read'), 'admitted native tool ticket required')
    pair_path = root.parent/'PAIR.json'
    pair = json.loads(f.pinned(pair_path, ticket['pair_sha256']))
    f.need(pair['root'] == str(root.parent), 'foreign task root')
    mode, label = ticket['mode'], ticket['actor']
    caller, client, auth, contract_pin = authenticated(root, pair, label)
    sender = label if mode == 'send' else ('B' if label == 'A' else 'A')
    ref, notice = ticket['reference'], ticket['notice']
    f.need(ref['direction'] == sender+('B' if sender == 'A' else 'A'), 'foreign body direction')
    body_path = root.parent/'bodies'/(ref['direction']+'.txt')
    f.need(ref['path'] == str(body_path), 'body escaped fixed direction path')
    # Bounded, regular, private, stable original full-file read through O_NOFOLLOW.
    raw, signature = h.caller_source.private_read(body_path, 16384)
    expected_ref, expected_notice = p.reference(pair, sender, raw, ref['in_reply_to'], ref['parent_body_sha256'])
    f.need((ref, notice) == (expected_ref, expected_notice), 'full body hash/length/nonce changed')
    record = dict(schema='private-native-duplex-tool-proof-v1', mode=mode, actor=label,
                  ticket_sha256=ticket_sha, pair_sha256=ticket['pair_sha256'], authentication=auth,
                  reference=ref, notice=notice, body_signature=list(signature),
                  body_bytes=len(raw), body_sha256=f.sha(raw), body_read_complete=True)
    if mode == 'send':
        target = pair['actors']['B' if label == 'A' else 'A']['binding']['actor']['surface_uuid']
        argv = [h.inbox.CMUX, 'agent', 'message']
        if label == 'B':
            parent = json.loads(f.pinned(Path(pair['actors']['A']['root'])/'SEND-RESULT.json', ticket['parent_send_sha256']))
            p.message(parent['message'], pair, 'A', parent['reference'], parent['notice'])
            f.need(ref['in_reply_to'] == parent['message']['id']
                   and ref['parent_body_sha256'] == parent['reference']['sha256'], 'reply not linked to original body')
            # The actual upstream reply route resolves the original sender.
            argv += ['--reply-to', ref['in_reply_to']]
        else:
            argv += [target]
        argv += ['--from', 'native-'+label+'-'+pair['nonce'][:12], '--thread', pair['nonce'], '--json', '--', notice]
        # No env parameter: the CLI inherits this authenticated native ToolCtx.
        record.update(argv=argv, expected_recipient_surface=target, invocations=1, retries=0,
                      sender_fields_are_rpc_claims_only=True, original_native_sender_authenticated=True)
        f.save_once(root/'SEND-INTENT.json', record)
        h.check_cli(contract_pin[0])
        stdout, unused_stderr = duplex_capture.capture(argv, root, 'SEND-RAW', 65536, 3)
        row = h.inbox.strict_json(stdout)
        p.message(row, pair, sender, ref, notice)
        f.need(caller._client()[0] == client and h.caller_source.private_read(body_path, 16384) == (raw, signature),
               'original sender/body changed during send')
        record.update(message=row, raw_result_sha256=f.sha(f.strict_file(root/'SEND-RAW-RESULT.json')))
        digest = f.save_once(root/'SEND-RESULT.json', record)
        print('CMUX_NATIVE_SEND_V1:'+digest, flush=True)
    else:
        seen = json.loads(f.pinned(root/'CONTEXT-SEEN.json', ticket['context_seen_sha256']))
        f.need(seen['recipient_actor'] == label and seen['reference'] == ref
               and seen['notice'] == notice and seen['native_thread_id'] == auth['native_origin']['reply']['threadId'],
               'body read lacks original received context')
        incoming = seen['message']
        p.message(incoming, pair, sender, ref, notice)
        f.need(incoming['id'] == ticket['message_id'], 'body read changed original message')
        output = p.body_output(raw)
        record.update(message_id=incoming['id'], context_seen_sha256=ticket['context_seen_sha256'],
                      full_body_base64=base64.b64encode(raw).decode(), stdout_sha256=f.sha((output+'\n').encode()))
        f.save_once(root/'BODY-READ-INTENT.json', record)
        f.need(caller._client()[0] == client and h.caller_source.private_read(body_path, 16384) == (raw, signature),
               'reader/body changed before complete output')
        print(output, flush=True)
        f.save_once(root/'BODY-READ-RESULT.json', record)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--ticket', type=Path, required=True)
    parser.add_argument('--ticket-sha256', required=True)
    args = parser.parse_args()
    execute(args.root, args.ticket, args.ticket_sha256)


if __name__ == '__main__':
    main()
