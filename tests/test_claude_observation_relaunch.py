import copy
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tools import claude_observation_resume as resume
from tools import prepare_claude_observation_resume as capture


WRAPPER = Path('/Applications/cmux.app/Contents/Resources/bin/cmux-claude-wrapper')


class ClaudeObservationRelaunchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='ccc-relaunch-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.sid = '473cd1d0-f07b-414d-b5b7-060103069e28'
        self.surface = 'FC5DB919-47A7-4A8F-9A46-1182958CE532'
        self.config = self.root / '.claude'
        self.history = self.config / 'projects' / 'project' / (self.sid + '.jsonl')
        self.history.parent.mkdir(parents=True)
        self.history.write_text('{"sessionId":"' + self.sid + '"}\n')
        self.history.chmod(0o600)
        self.native = self.root / 'native-claude'
        self.native.write_text('#!' + sys.executable + '\nimport json, os, sys\nprint(json.dumps({"argv":sys.argv,"env":dict(os.environ)}))\n')
        self.native.chmod(0o700)
        self.observer = self.root / 'observer.cjs'
        self.observer.write_text('// fake observer\n')
        self.observer.chmod(0o600)
        self.app = self.root / 'App' / 'Contents' / 'Resources' / 'bin'
        self.app.mkdir(parents=True)
        self.wrapper = self.app / 'cmux-claude-wrapper'
        shutil.copyfile(WRAPPER, self.wrapper)
        self.wrapper.chmod(0o700)
        for name in ('cmux', 'cmux-cua'):
            p = self.app / name
            p.write_text('#!/bin/sh\nexit 0\n')
            p.chmod(0o700)
        self.token = self.root / 'mcp-token'
        self.token.write_text('fake-fresh-token')
        self.token.chmod(0o600)
        self.env = dict(HOME=str(self.root), PATH=os.environ['PATH'], TMPDIR=str(self.root),
            CLAUDE_CONFIG_DIR=str(self.config), CMUX_SURFACE_ID=self.surface,
            ANTHROPIC_API_KEY='fake-original-key', BUN_OPTIONS='--smol',
            CMUX_CUA_AUTH_TOKEN_FILE=str(self.token), CMUX_CUA_SOCKET_AUTH_TOKEN='fake-stale',
            CMUX_CLAUDE_PID='8123', CMUX_AGENT_LAUNCH_EXECUTABLE='/old/shim')
        self.process = dict(pid=8123, birth=[100, 22], argv=['Claude', '--resume', self.sid],
            environment=self.env, cwd=str(self.root), executable=str(self.native),
            executable_identity=capture.file_identity(self.native.stat()))
        self.expected = dict(pid=8123, birth=[100, 22], surface_id=self.surface, session_id=self.sid)

    def prepare(self, suffix='plan'):
        record = capture.capture(self.expected, lambda pid: copy.deepcopy(self.process))
        return resume.prepare(record, self.root / suffix, self.wrapper, self.observer, self.root / 'observations')

    def claim(self, plan):
        calls = []
        resume.apply(plan, lambda p: None, execute=lambda *args: calls.append(args))
        return calls[0][2]

    def test_prepared_only_original_identity_environment_and_private_wrapper(self):
        plan = self.prepare()
        self.assertEqual(plan['environment']['ANTHROPIC_API_KEY'], 'fake-original-key')
        self.assertEqual(plan['environment']['CLAUDE_CONFIG_DIR'], str(self.config))
        self.assertNotIn('CMUX_CLAUDE_PID', plan['environment'])
        self.assertIn('--smol --preload ', plan['environment']['BUN_OPTIONS'])
        self.assertEqual(plan['state'], 'prepared_not_launched')
        self.assertIsNone(plan['observed_api_key'])
        self.assertFalse(Path(plan['claim_path']).exists())
        self.assertEqual((self.root / 'plan' / 'resume.json').stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.wrapper.read_bytes(), WRAPPER.read_bytes())
        subprocess.run(['/bin/bash', '-n', plan['argv'][0]], check=True, timeout=3)

    def test_missing_or_duplicate_original_history_never_uses_another_root(self):
        self.history.unlink()
        elsewhere = self.root / 'other' / (self.sid + '.jsonl')
        elsewhere.parent.mkdir(); elsewhere.write_text('original')
        with self.assertRaises(ValueError): self.prepare()
        self.history.write_text('original')
        duplicate = self.history.parent / 'child' / self.history.name
        duplicate.parent.mkdir(); duplicate.write_text('duplicate')
        with self.assertRaises(ValueError): self.prepare()

    def test_history_append_and_same_bytes_inode_replacement_refuse_launch(self):
        plan = self.prepare()
        self.history.write_text(self.history.read_text() + '{}\n')
        calls = []
        with self.assertRaises(ValueError):
            resume.apply(plan, lambda p: None, execute=lambda *a: calls.append(a))
        self.assertEqual(calls, [])
        plan = self.prepare('plan2')
        raw = self.history.read_bytes(); self.history.unlink(); self.history.write_bytes(raw)
        with self.assertRaises(ValueError): resume.revalidate(plan)

    def test_two_live_boundaries_refuse_late_process_or_draft(self):
        plan = self.prepare()
        visits = []
        def boundary(p):
            visits.append(1)
            if len(visits) == 2: raise ValueError('new process')
        with self.assertRaises(ValueError): resume.apply(plan, boundary, execute=lambda *a: self.fail('exec'))
        self.assertEqual(len(visits), 2)
        self.assertFalse(Path(plan['claim_path']).exists())

    def test_one_time_claim_survives_failed_exec_and_new_plan(self):
        plan = self.prepare()
        def failed(*args): raise OSError('unknown exec result')
        with self.assertRaises(OSError): resume.apply(plan, lambda p: None, execute=failed)
        for p in (plan, self.prepare('plan2')):
            with self.assertRaises(FileExistsError):
                resume.apply(p, lambda p: None, execute=lambda *a: self.fail('repeat exec'))

    def test_claim_fsync_failure_does_not_execute(self):
        plan = self.prepare()
        with patch.object(os, 'fsync', side_effect=OSError('disk')):
            with self.assertRaises(OSError):
                resume.apply(plan, lambda p: None, execute=lambda *a: self.fail('exec'))

    def test_claim_io_drift_refuses_wrapper_and_native_but_keeps_claim(self):
        plan = self.prepare()
        real_claim = resume.durable_claim
        alive = True
        def late_claim(*args):
            nonlocal alive
            result = real_claim(*args)
            alive = False
            return result
        def boundary(p):
            if not alive: raise ValueError('identity drift during claim IO')
        with patch.object(resume, 'durable_claim', side_effect=late_claim):
            with self.assertRaises(ValueError):
                resume.apply(plan, boundary, execute=lambda *a: self.fail('wrapper exec'))
        self.assertTrue(Path(plan['claim_path']).exists())
        claim = json.loads(Path(plan['claim_path']).read_text())
        env = dict(plan['environment'], CCC_OBSERVATION_RESUME_TOKEN=claim['token'],
                   CCC_OBSERVATION_RESUME_SHA=claim['plan_sha256'])
        alive = True
        with patch.object(resume, 'durable_claim', side_effect=late_claim):
            with self.assertRaises(ValueError):
                resume.exec_native(plan, [str(self.native), '--resume', self.sid], boundary,
                    environment=env, execute=lambda *a: self.fail('native exec'))
        self.assertTrue(Path(plan['claim_path'] + '.native').exists())

    def test_native_rejects_auth_argument_settings_and_user_mcp_changes(self):
        settings = {'env':{'ANTHROPIC_API_KEY':'fake-profile'},
                    'permissions':{'deny':['Bash(rm *)']},
                    'hooks':{'Stop':[{'hooks':[{'type':'command','command':'user-hook'}]}]}}
        mcp = {'mcpServers':{'user':{'command':'/user/server'}}}
        self.process['argv'] += ['--settings='+json.dumps(settings), '--mcp-config='+json.dumps(mcp)]
        plan = self.prepare(); env = self.claim(plan)
        args = [str(self.native), *plan['argv'][1:]]
        variants = [args + ['--dangerously-skip-permissions']]
        for name in ('env','permissions','hooks'):
            changed = copy.deepcopy(settings); changed[name] = {}
            variants.append([a if not a.startswith('--settings=') else '--settings='+json.dumps(changed) for a in args])
        variants.append([a for a in args if not a.startswith('--settings=')])
        variants.append([a for a in args if not a.startswith('--mcp-config=')])
        variants.append([a if not a.startswith('--mcp-config=') else '--mcp-config={"mcpServers":{}}' for a in args])
        for n, variant in enumerate(variants):
            with self.subTest(variant=n), self.assertRaises(ValueError):
                resume.exec_native(plan, variant, lambda p: None, environment=env,
                    execute=lambda *a: self.fail('drifted exec'))
        with self.assertRaises(ValueError):
            resume.exec_native(plan,args,lambda p:None,environment=dict(env,ANTHROPIC_API_KEY='different'),
                               execute=lambda *a:self.fail('changed key'))
        calls=[]
        resume.exec_native(plan,args,lambda p:None,environment=env,execute=lambda *a:calls.append(a))
        self.assertEqual(len(calls),1)

    def test_full_private_wrapper_handoff_to_fake_native(self):
        # The driver changes only the terminal boundary in this isolated test;
        # preparation, wrapper, checkpoint, final checks and exec are real.
        driver = self.root / 'driver.py'
        driver.write_text('import sys\nsys.path.insert(0,' + repr(str(resume.ROOT)) + ')\n'
            'from tools import claude_observation_resume as m\n'
            'm.local_terminal_boundary=lambda p:None\nm.main()\n')
        driver.chmod(0o600)
        # The real wrapper requires a socket pathname plus its bundled ping.
        # The private helper exits successfully without connecting anywhere.
        fake_socket = socket.socket(socket.AF_UNIX)
        self.addCleanup(fake_socket.close)
        fake_socket.bind(str(self.root / 's'))
        self.env['CMUX_SOCKET_PATH'] = str(self.root / 's')
        settings = {'env':{'ANTHROPIC_API_KEY':'fake-profile'}, 'permissions':{'deny':['Bash(rm *)']},
            'hooks':{'Stop':[{'hooks':[{'type':'command','command':'user-hook'}]}]}}
        self.process['argv'] += ['--settings='+json.dumps(settings),
                                '--mcp-config={"mcpServers":{"user":{"command":"/user/server"}}}']
        with patch.object(resume,'__file__',str(driver)):
            plan=self.prepare()
        raw=capture.read_file(plan['plan_path'])
        result=subprocess.run([sys.executable,'-B',str(driver),'apply','--plan',plan['plan_path'],
            '--plan-sha256',raw['sha256']],text=True,capture_output=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)
        observed=json.loads(result.stdout)
        self.assertEqual(capture.session_from_argv(observed['argv']),self.sid)
        self.assertEqual(observed['env']['ANTHROPIC_API_KEY'],'fake-original-key')
        self.assertIn('--preload',observed['env']['BUN_OPTIONS'])
        self.assertTrue(Path(plan['claim_path']+'.native').exists())
        self.assertTrue(Path(plan['claim_path']+'.base').exists())
        final=json.loads(Path(capture.option_values(observed['argv'],'--settings')[0]).read_text())
        for key in ('env','permissions'):
            self.assertEqual(final[key],settings[key])
        self.assertEqual(final['hooks']['Stop'],settings['hooks']['Stop'])

    def test_managed_mcp_rejects_command_route_token_and_environment_drift(self):
        self.env.update(CMUX_CUA_RUNTIME_SCOPE='..a中b-..', CMUX_CUA_SOCKET_PATH='/original/cua.sock',
                        CMUX_CUA_STATE_DIR='/original/state')
        self.token.write_text(' fake-token \r\nignored second line\n')
        plan = self.prepare(); env = self.claim(plan)
        server = dict(command=str(self.app / 'cmux-cua'), args=['mcp','--socket','/original/cua.sock'], env={
            'CMUX_CUA_MCP_FORCE_PROXY':'1','CMUX_CUA_EXTERNAL_PERMISSION_FLOW':'1',
            'CMUX_CUA_SOCKET_AUTH_TOKEN':' fake-token \r','CMUX_CUA_DEFAULT_SESSION':'cmux-'+self.surface,
            'CMUX_CUA_STATE_OWNER_PID':str(os.getpid()),'CMUX_CUA_TELEMETRY_ENABLED':'false',
            'CMUX_CUA_UPDATE_CHECK':'false','CMUX_CUA_CURSOR_GRADIENT':'#12c7f5,#2d8cff,#6c5cff',
            'CMUX_CUA_CURSOR_BLOOM':'#2d8cff','CMUX_CUA_CURSOR_LABEL':'cmux',
            'CMUX_CUA_STATE_DIR':'/original/state','NODE_OPTIONS':'','BUN_OPTIONS':''})
        def args(value):
            return [str(self.native),'--mcp-config='+json.dumps({'mcpServers':{'cmux-cua':value}}),
                    '--resume',self.sid]
        resume.validate_final_inputs(plan,args(server),env)
        mutations = [('command','/other/cmux-cua'),('args',['mcp','--socket','/other/cua.sock'])]
        variants = []
        for field, value in mutations:
            candidate = copy.deepcopy(server); candidate[field]=value; variants.append(candidate)
        for field, value in [('CMUX_CUA_SOCKET_AUTH_TOKEN','fake-token'),
                             ('CMUX_CUA_STATE_OWNER_PID','8123'),('CMUX_CUA_STATE_DIR','/other/state'),
                             ('BUN_OPTIONS','--preload /unrelated'),('EXTRA','unknown')]:
            candidate=copy.deepcopy(server); candidate['env'][field]=value; variants.append(candidate)
        for i, candidate in enumerate(variants):
            with self.subTest(variant=i), self.assertRaises(ValueError):
                resume.validate_final_inputs(plan,args(candidate),env)
        # A simultaneous change of runtime env and server must not change the pinned route.
        with self.assertRaises(ValueError):
            resume.validate_final_inputs(plan,args(variants[1]),dict(env,CMUX_CUA_SOCKET_PATH='/other/cua.sock'))
        helper=self.app/'cmux-cua'; helper.write_text('#!/bin/sh\nexit 1\n')
        with self.assertRaises(ValueError): resume.validate_final_inputs(plan,args(server),env)

    def test_final_configuration_drift_during_native_claim_refuses_exec(self):
        self.process['argv'] += ['--settings={"permissions":{"deny":["Bash(rm *)"]}}']
        plan=self.prepare(); env=self.claim(plan)
        args=[str(self.native),*plan['argv'][1:]]
        settings=Path(capture.option_values(args,'--settings')[0])
        original_claim=resume.durable_claim
        def changed(*a):
            value=original_claim(*a)
            settings.write_text('{"permissions":{}}')
            return value
        with patch.object(resume,'durable_claim',side_effect=changed), self.assertRaises(ValueError):
            resume.exec_native(plan,args,lambda p:None,environment=env,execute=lambda *a:self.fail('exec'))
        self.assertTrue(Path(plan['claim_path']+'.native').exists())

    def test_native_boundary_checks_selection_observer_identity_and_one_time_use(self):
        plan = self.prepare(); env = self.claim(plan)
        args = [str(self.native), '--resume', self.sid]
        for key, value in [('CLAUDE_CONFIG_DIR', str(self.root)), ('BUN_OPTIONS', ''),
                           ('CCC_OBSERVATION_RESUME_TOKEN', 'wrong'), ('CMUX_SURFACE_ID', 'other')]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                resume.exec_native(plan, args, lambda p: None, environment=dict(env, **{key:value}))
        with self.assertRaises(ValueError):
            resume.exec_native(plan, ['/other/claude', *args[1:]], lambda p: None, environment=env)
        calls = []
        resume.exec_native(plan, args, lambda p: None, environment=env, execute=lambda *a: calls.append(a))
        self.assertEqual(len(calls), 1)
        self.assertNotIn('CCC_OBSERVATION_RESUME_TOKEN', calls[0][2])
        with self.assertRaises(FileExistsError):
            resume.exec_native(plan, args, lambda p: None, environment=env, execute=lambda *a: self.fail('repeat'))

    def test_native_executable_drift_after_wrapper_start_refuses_fallback(self):
        plan = self.prepare(); env = self.claim(plan)
        self.native.unlink()
        with self.assertRaises(OSError):
            resume.exec_native(plan, [str(self.native), '--resume', self.sid], lambda p: None, environment=env)
        result = subprocess.run([plan['argv'][0], *plan['argv'][1:]], env=env,
                                text=True, capture_output=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('fake-original-key', result.stdout)

    def test_configuration_and_mcp_transformation_preserves_user_inputs(self):
        managed = dict(command='/fixed/cmux-cua', args=['mcp','--socket','/fake/socket'], env={
            'CMUX_CUA_STATE_OWNER_PID':'8123', 'CMUX_CUA_DEFAULT_SESSION':'cmux-'+self.surface,
            'CMUX_CUA_MCP_FORCE_PROXY':'1', 'CMUX_CUA_EXTERNAL_PERMISSION_FLOW':'1',
            'CMUX_CUA_SOCKET_AUTH_TOKEN':'fake-old'})
        settings = {'env':{'ANTHROPIC_API_KEY':'fake-profile'}, 'permissions':{'deny':['Bash(rm *)']},
            'hooks':{'Stop':[{'hooks':[{'type':'command','command':'user-hook'}]}]},
            '__cmux':{'managed':'claude-hooks','hookFingerprints':[]}}
        user = {'command':'/user/server','args':['with spaces']}
        self.process['argv'][1:1] = ['--settings', json.dumps(settings), '--mcp-config',
            json.dumps({'mcpServers':{'cmux-cua':managed,'user-mcp':user}}),
            json.dumps({'mcpServers':{'another':{'command':'/another'}}})]
        plan = self.prepare()
        args = plan['argv']
        self.assertEqual(json.loads(capture.option_values(args,'--settings')[0] and
            Path(capture.option_values(args,'--settings')[0]).read_text()), settings)
        mcps = [json.loads(Path(p).read_text()) for p in capture.option_values(args,'--mcp-config',variadic=True)]
        self.assertEqual(mcps, [{'mcpServers':{'user-mcp':user}}, {'mcpServers':{'another':{'command':'/another'}}}])
        self.assertNotIn('CMUX_CUA_SOCKET_AUTH_TOKEN', plan['environment'])
        # Execute the unchanged real wrapper in a fake app tree. Its selected
        # executable only prints argv/env; fake helpers cannot reach cmux/model.
        env = dict(plan['environment'], CMUX_AGENT_RESTORE_LAUNCH='claude:'+self.sid)
        result = subprocess.run([str(self.wrapper), *args[1:]], env=env, cwd=self.root,
            text=True, capture_output=True, timeout=15, check=True)
        observed = json.loads(result.stdout)
        self.assertEqual(capture.session_from_argv(observed['argv']), self.sid)
        self.assertEqual(observed['env']['ANTHROPIC_API_KEY'], 'fake-original-key')
        self.assertEqual(observed['env']['BUN_OPTIONS'], plan['environment']['BUN_OPTIONS'])
        self.assertEqual(observed['env']['CLAUDE_CONFIG_DIR'], str(self.config))
        paths = capture.option_values(observed['argv'], '--settings')
        self.assertEqual(len(paths), 1)
        merged = json.loads(Path(paths[0]).read_text())
        self.assertEqual(merged['env'], settings['env'])
        self.assertEqual(merged['permissions'], settings['permissions'])
        self.assertEqual(merged['hooks']['Stop'], settings['hooks']['Stop'])
        final_mcps = [capture.json_input(p,str(self.root))[1] for p in
                      capture.option_values(observed['argv'],'--mcp-config',variadic=True)]
        cua = final_mcps[0]['mcpServers']['cmux-cua']['env']
        self.assertEqual(cua['CMUX_CUA_STATE_OWNER_PID'], observed['env']['CMUX_CLAUDE_PID'])
        self.assertNotEqual(cua['CMUX_CUA_STATE_OWNER_PID'], '8123')
        self.assertEqual(cua['CMUX_CUA_SOCKET_AUTH_TOKEN'], 'fake-fresh-token')
        self.assertEqual(final_mcps[1:], mcps)


if __name__ == '__main__': unittest.main()
