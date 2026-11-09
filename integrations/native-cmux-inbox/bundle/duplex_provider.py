"""Two deterministic local providers; every send/read is a real native tool."""
import base64
import json
from pathlib import Path
import shlex
import threading
import uuid

import fixture_common as f
from fixture import FixtureProvider, message
import duplex_protocol as p


class Exchange:
    def __init__(self, root):
        self.root, self.nonce = root, uuid.uuid4().hex
        self.providers = {}
        self.bootstrap_b = threading.Event()
        self.a_sent = threading.Event()
        self.b_replied = threading.Event()
        self.lock = threading.Lock()
        self.pair = self.pair_sha = None

    def freeze_pair(self):
        with self.lock:
            if self.pair is not None:
                return self.pair, self.pair_sha
            actors = {}
            for label, provider in self.providers.items():
                binding_raw = f.strict_file(provider.root/'BINDING.json')
                client_raw = f.strict_file(provider.root/'CLIENT.json')
                actors[label] = dict(root=str(provider.root), binding=json.loads(binding_raw),
                    binding_sha256=f.sha(binding_raw), client=json.loads(client_raw), client_sha256=f.sha(client_raw))
            pair = dict(schema='private-native-duplex-pair-v1', root=str(self.root), nonce=self.nonce,
                        actors=actors, fixture_only=True, business_operations=0)
            p.endpoints(pair)
            self.pair_sha = f.save_once(self.root/'PAIR.json', pair)
            self.pair = pair
            (self.root/'bodies').mkdir(mode=0o700)
            return pair, self.pair_sha

    def sent(self, label):
        return json.loads(f.strict_file(self.providers[label].root/'SEND-RESULT.json'))

    def expected_rows(self):
        rows = {}
        for label, provider in self.providers.items():
            path = provider.root/'SEND-RESULT.json'
            if path.exists():
                proof = json.loads(f.strict_file(path))
                rows[proof['message']['id']] = proof['message']
        return rows


class DuplexProvider(FixtureProvider):
    def __init__(self, root, project, python, registration, gui, alias, exchange, label, rtk):
        super().__init__(root, project, python)
        self.registration, self.gui, self.alias = registration, gui, alias
        self.exchange, self.label, self.rtk = exchange, label, rtk
        self.steps, self.acceptances = {}, {}
        self.prompt = 'DUPLEX_ROOT_'+label+'_'+self.nonce+': isolated two-root fixture; run only issued tools.'
        exchange.providers[label] = self

    def wait(self, event, purpose):
        f.need(event.wait(25) and not self.stopping.is_set(), purpose+' incomplete; no replay')

    def issue(self, body, step, command, marker, role='root'):
        self.gui.stable_peer(); self.alias.check()
        f.need(step not in self.steps, 'duplicate original tool step')
        call = 'duplex_'+self.label+'_'+step+'_'+self.nonce
        args = dict(cmd=command, workdir=str(self.project), login=False, max_output_tokens=12000, yield_time_ms=10000)
        item = f.tool(body, call, 'unused', self.project)
        if item['type'] == 'function_call':
            item['arguments'] = json.dumps(args)
        else:
            item['input'] = 'text(await tools.exec_command('+json.dumps(args)+'));'
        issued = dict(item=item, role=role, command=command, marker=marker,
                      sid=self.root_sid if role == 'root' else self.child_sid)
        self.steps[step] = issued
        f.save_once(self.root/(step+'-ISSUED.json'), issued)
        return item

    def marker_tool(self, body, step, role='root'):
        marker = 'DUPLEX_TOOL_'+self.label+'_'+step+'_'+self.nonce
        return self.issue(body, step, "printf '%s\\n' "+shlex.quote(marker), marker, role)

    def ticket_tool(self, body, mode):
        pair, pair_sha = self.exchange.freeze_pair()
        if mode == 'send':
            parent = self.exchange.sent('A') if self.label == 'B' else None
            # Harmless generated fixture text is never inherited business text.
            lines = ['Isolated duplex body '+self.label+' '+self.exchange.nonce]
            lines += [str(i)+':'+uuid.uuid4().hex+uuid.uuid4().hex for i in range(72)]
            lines += ['END_FULL_BODY_'+self.label+'_'+self.exchange.nonce]
            raw = ('\n'.join(lines)+'\n').encode()
            ref, notice = p.reference(pair, self.label, raw,
                parent['message']['id'] if parent else None, parent['reference']['sha256'] if parent else None)
            f.write_new(Path(ref['path']), raw, 0o400)
            ticket = dict(schema='private-native-duplex-tool-v1', mode=mode, actor=self.label,
                          pair_sha256=pair_sha, reference=ref, notice=notice)
            if parent:
                ticket['parent_send_sha256'] = f.sha(f.strict_file(self.exchange.providers['A'].root/'SEND-RESULT.json'))
            marker = 'CMUX_NATIVE_SEND_V1:'
        else:
            seen_raw = f.strict_file(self.root/'CONTEXT-SEEN.json')
            seen = json.loads(seen_raw)
            ticket = dict(schema='private-native-duplex-tool-v1', mode=mode, actor=self.label,
                pair_sha256=pair_sha, reference=seen['reference'], notice=seen['notice'],
                message_id=seen['message']['id'], context_seen_sha256=f.sha(seen_raw))
            raw = f.pinned(Path(ticket['reference']['path']), ticket['reference']['sha256'])
            marker = p.body_output(raw)
        path = self.root/(mode+'-TICKET.json')
        digest = f.save_once(path, ticket)
        command = shlex.join([str(self.rtk), 'proxy', self.python, '-BE',
            str(Path(__file__).parent/'reader/duplex_probe.py'), '--root', str(self.root),
            '--ticket', str(path), '--ticket-sha256', digest])
        return self.issue(body, mode, command, marker)

    def accept(self, body, step, emit=False):
        self.gui.stable_peer(); self.alias.check()
        f.need(step not in self.acceptances, 'original tool already accepted')
        issued = self.steps[step]
        outputs = [r for r in f.items(body, 'function_call_output')+f.items(body, 'custom_tool_call_output')
                   if r.get('call_id') == issued['item']['call_id']]
        f.need(len(outputs) == 1 and issued['marker'] in str(outputs[0]), 'complete original native output missing')
        f.need(not list(self.root.glob('HOST-HOOK-FAILURE-*.json')), 'automatic hook failure preserved')
        matches = []
        for path in self.root.glob('host-hook-*/RESULT.json'):
            payload = json.loads(f.strict_file(path.parent/'STDIN.json'))
            if payload.get('tool_input') == {'command': issued['command']}:
                matches.append((path, payload, json.loads(f.strict_file(path))))
        f.need(len(matches) == 1, 'one natural Post hook per original tool required')
        path, payload, post = matches[0]
        f.need(post['role'] == issued['role']
               and post['native_origin']['reply']['origin']['handlerId'] == self.registration['handler_id'],
               'actual role or registered hook handler changed')
        incoming_proof = self.exchange.sent('B' if self.label == 'A' else 'A') if emit else None
        incoming = incoming_proof['message'] if emit else None
        inner = p.hook_scope(post, payload, issued['command'], issued['sid'], self.client, incoming)
        if issued['role'] == 'root':
            rows = post['actual_parsed_host_queries'][0]['messages']
            expected = self.exchange.expected_rows()
            f.need(len(rows) == len(expected) and {r['id'] for r in rows} == set(expected), 'unexpected actual host row')
            for row in rows:
                f.need(set(row) == p.MESSAGE_KEYS
                       and row == {k:expected[row['id']][k] for k in p.MESSAGE_KEYS}, 'complete actual inbox row changed')
            raw = json.loads(f.strict_file(path.parent/'INBOX-RAW-RESULT.json'))
            f.need(raw['status'] == 'COMPLETE'
                   and json.loads(base64.b64decode(raw['stdout_base64'], validate=True)) == post['actual_parsed_host_queries'][0],
                   'original inbox bytes disagree with parsed result')
        if step == 'bootstrap':
            f.need('HOST_FIXTURE_BOOTSTRAP:'+f.sha(f.strict_file(self.root/'BINDING.json')) in str(outputs[0]),
                   'bootstrap exact binding not in actual output')
        if step in ('send', 'read'):
            proof = json.loads(f.strict_file(self.root/('SEND-RESULT.json' if step == 'send' else 'BODY-READ-RESULT.json')))
            f.need(proof['authentication']['native_origin']['reply']['callId'] == inner
                   and proof['authentication']['native_origin']['reply']['threadId'] == self.root_sid,
                   'sender/full-reader ToolCtx is not original hook call')
            if step == 'send':
                raw_send = json.loads(f.pinned(self.root/'SEND-RAW-RESULT.json',proof['raw_result_sha256']))
                f.need(raw_send['launcher_pid'] == proof['authentication']['native_origin']['current']['pid']
                       and type(raw_send['popen_pid']) is int and raw_send['popen_pid'] > 1
                       and raw_send['popen_pid'] != raw_send['launcher_pid'],
                       'actual sender CLI handle is not linked to authenticated native helper')
                f.need('CMUX_NATIVE_SEND_V1:'+f.sha(f.strict_file(self.root/'SEND-RESULT.json')) in str(outputs[0]),
                       'exact send result missing from original native output')
            else:
                raw_body = p.full_body(proof, str(outputs[0]))
                f.need(p.body_output(raw_body) == issued['marker'], 'full original byte sequence changed')
        request = self.requests[-1]
        # Child and root can overlap only during A's final spawn; select SID explicitly.
        request = [r for r in self.requests if r['thread_id'] == issued['sid']][-1]
        if emit:
            context = post['emitted_contexts'][0]
            p.subsequent_context(body, context, issued['item'], outputs[0])
            pair, unused_pair_sha = self.exchange.freeze_pair()
            sender = 'B' if self.label == 'A' else 'A'
            p.message(incoming, pair, sender, incoming_proof['reference'], incoming_proof['notice'])
            f.save_once(self.root/'CONTEXT-SEEN.json', dict(recipient_actor=self.label, native_thread_id=self.root_sid,
                context=context, message=incoming, reference=incoming_proof['reference'], notice=incoming_proof['notice'],
                original_provider_request='REQUEST-%02d.json'%request['index'],
                provider_request_sha256=f.sha(f.strict_file(self.root/('REQUEST-%02d.json'%request['index']))),
                automatic_hook=str(path.relative_to(self.root)), automatic_hook_sha256=f.sha(f.strict_file(path)),
                source_send_sha256=f.sha(f.strict_file(self.exchange.providers[sender].root/'SEND-RESULT.json')),
                native_user_receipt=False))
        row = dict(step=step, role=issued['role'], outer_call_id=issued['item']['call_id'], native_call_id=inner,
                   hook_path=str(path.relative_to(self.root)), hook_sha256=f.sha(f.strict_file(path)),
                   actual_output=outputs[0], subsequent_request_index=request['index'],
                   subsequent_request_sha256=f.sha(f.strict_file(self.root/('REQUEST-%02d.json'%request['index']))))
        f.save_once(self.root/(step+'-ACCEPTANCE.json'), row)
        self.acceptances[step] = row

    def spawn(self, body):
        candidates = [(ns, tool) for ns, tool in f.tool_entries(body)
                      if tool.get('type') == 'function' and tool.get('name') == 'spawn_agent']
        f.need(len(candidates) == 1 and self.spawn_item is None, 'one advertised original spawn route required')
        ns, tool = candidates[0]
        f.need({'task_name', 'fork_turns'} <= set(tool.get('parameters', {}).get('properties', {})), 'V2 spawn required')
        item = dict(id='spawn_'+self.nonce, type='function_call', name='spawn_agent',
            call_id='spawn_'+self.nonce, status='completed', arguments=json.dumps(dict(
                task_name='caller_identity_fixture', message=self.child_prompt, fork_turns='none')))
        if ns: item['namespace'] = ns
        self.spawn_item = item
        f.save_once(self.root/'SPAWN-ISSUED.json', item)
        return item

    def respond(self, body, headers):
        users = [''.join(x.get('text', '') for x in item.get('content', []) if isinstance(x, dict))
                 for item in body.get('input', []) if isinstance(item, dict) and item.get('role') == 'user']
        if users and users[-1].startswith('Generate a concise, single-line task title'):
            return message('{"title":"Isolated duplex fixture"}')
        sid = headers.get('thread-id')
        f.need(p.canonical(sid) == sid, 'original native thread header required')
        with self.lock:
            if self.root_sid is None:
                f.need(any(self.prompt in x for x in users), 'foreign original root prompt')
                self.root_sid = sid
            role = 'root' if sid == self.root_sid else 'child'
            if role == 'child':
                f.need(self.label == 'A' and self.child_request_matches(body)
                       and self.child_sid in (None, sid), 'child cannot become root B')
                self.child_sid = sid
            stage = self.stages.get(role, 0)
            f.need(stage < (2 if role == 'child' else (6 if self.label == 'A' else 5)), 'extra native turn refused')
            self.stages[role] = stage+1
            row = dict(index=len(self.requests)+1, role=role, stage=stage, thread_id=sid,
                       body=body, native_metadata=headers.get('x-codex-turn-metadata'))
            self.requests.append(row)
            f.save_once(self.root/('REQUEST-%02d.json'%row['index']), row)
        self.wait(self.ready, 'actual TUI readiness')
        if role == 'child':
            if stage == 0:
                return self.marker_tool(body, 'child', 'child')
            self.accept(body, 'child'); self.child_done.set()
            return message('AUTOHOOK_CHILD_DONE:'+self.nonce)
        if stage == 0:
            return self.issue(body, 'bootstrap', self.registration['bootstrap_command'], 'HOST_FIXTURE_BOOTSTRAP:')
        if stage == 1:
            self.accept(body, 'bootstrap')
            if self.label == 'A':
                self.wait(self.exchange.bootstrap_b, 'B original bootstrap')
                return self.ticket_tool(body, 'send')
            self.exchange.bootstrap_b.set()
            self.wait(self.exchange.a_sent, 'A actual send and empty sender Post')
            return self.marker_tool(body, 'receive')
        if self.label == 'A':
            if stage == 2:
                self.accept(body, 'send'); self.exchange.a_sent.set()
                self.wait(self.exchange.b_replied, 'B original full read and reply')
                return self.marker_tool(body, 'receive')
            if stage == 3:
                self.accept(body, 'receive', True)
                return self.ticket_tool(body, 'read')
            if stage == 4:
                self.accept(body, 'read')
                return self.spawn(body)
            outputs = f.items(body, 'function_call_output')
            f.need(any(r.get('call_id') == 'spawn_'+self.nonce and 'caller_identity_fixture' in str(r)
                       for r in outputs), 'actual A child spawn output missing')
            self.wait(self.child_done, 'original A child completion')
        else:
            if stage == 2:
                self.accept(body, 'receive', True)
                return self.ticket_tool(body, 'read')
            if stage == 3:
                self.accept(body, 'read')
                return self.ticket_tool(body, 'send')
            self.accept(body, 'send'); self.exchange.b_replied.set()
        self.done.set()
        return message('AUTOHOOK_FIXTURE_DONE:'+self.nonce)
