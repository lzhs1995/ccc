"""Synthetic web receipt tests. Never invokes a Magpie binary or compiler."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from common import FLOOR, pin
import web_entry as W


class WebReceipt(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.out=self.root/'results';self.out.mkdir()
        self.binary=self.root/'magpie';self.binary.write_text('synthetic binary')
        self.build=self.root/'BUILD.json';self.save(self.build,{'status':'PASS','variant':'candidate','source_after_match':True,
            'binary':pin(self.binary),'package_pin':'BUILD-PIN'})
        self.row={'schema':W.SCHEMA,'status':'PASS','artifact':pin(self.binary),'artifact_path':str(self.binary),
            'build_receipt':pin(self.build),'build_package_pin':'BUILD-PIN','package_pin':'WEB-PIN',
            'argv':[str(self.binary),'serve','--web=127.0.0.1:39102'],
            'ports':{'gateway':39101,'web':39102,'upstream':39103},'private_path':str(self.root/'private'),
            'children_reaped':True,'fake_handlers_finished':True,'private_removed':True,'artifact_unchanged':True,
            'no_residual_listeners':True,'model_posts':0,'upstream_requests':[],'owned_pid':123,
            'listener_pids':{'gateway':[123],'web':[123]},'after_listener_pids':{'gateway':[],'web':[]},
            'capacity_samples':[{'free_bytes':40*2**30}]*2}
        (self.out/'codesign.stdout').write_text('');(self.out/'codesign.stderr').write_text('valid on disk')
        self.row['signature']={'argv':['/usr/bin/codesign','--verify','--strict',str(self.binary)],'returncode':0,
            'logs':{n:pin(self.out/n) for n in ['codesign.stdout','codesign.stderr']}}
        policy=W.policy(self.binary,self.root/'private',39101,39102,39103);(self.out/'gateway.sb').write_text(policy)
        self.row.update(policy=pin(self.out/'gateway.sb'),policy_text=policy)
        rows={
            'health':(200,b'{"magpie":"fixture"}',[],39101,'/'),
            'models':(200,json.dumps({'object':'list','data':[{'id':W.MODEL}]}).encode(),[],39101,'/v1/models'),
            'unauthenticated':(401,b'<html>magpie web Sign in</html>',[],39102,'/'),
            'bad_key':(401,b'wrong key',[],39102,'/?k=wrong-synthetic-key'),
            'login':(303,b'', [['Location','/'],['Set-Cookie','magpie_fixture='+W.SYNTHETIC_KEY+'; HttpOnly; Path=/']],39102,'/?k='+W.SYNTHETIC_KEY),
            'page':(200,b'<html><title>magpie</title></html>',[['Content-Type','text/html']],39102,'/')}
        judged={}
        for name,(status,body,headers,port,path) in rows.items():
            (self.out/(name+'.body')).write_bytes(body)
            meta={'method':'GET','path':path,'port':port,'status':status,'headers':headers,'body':pin(self.out/(name+'.body'))}
            self.save(self.out/(name+'.json'),meta);judged[name]=(meta,body)
        self.row['http_validation']=W.judge_http(judged)
        self.save(self.out/'providers.json',W.provider_config(39103))
        for name in ['settings.json','plugins.yml']:
            (self.out/name).write_bytes((Path(W.__file__).parent/name).read_bytes())
        for name in ['gateway.stdout','gateway.stderr']:(self.out/name).write_text('synthetic fixture log')
        self.repin()

    def save(self,p,v):p.write_text(json.dumps(v))
    def repin(self):self.row['files']={p.name:pin(p) for p in self.out.iterdir() if p.is_file()}
    def check(self):return W.verify_receipt(self.out,self.row,self.build,self.binary)
    def test_valid(self):self.assertEqual(self.check()['status'],'PASS')
    def test_model_posts(self):
        self.row['upstream_requests']=[{'method':'POST','path':'/v1/responses'}]
        with self.assertRaises(RuntimeError):self.check()
    def test_signature_nonzero(self):
        self.row['signature']['returncode']=1
        with self.assertRaises(RuntimeError):self.check()
    def test_signature_bool_false(self):
        self.row['signature']['returncode']=False
        with self.assertRaises(RuntimeError):self.check()
    def test_wrong_artifact(self):
        self.binary.write_text('changed')
        with self.assertRaises(RuntimeError):self.check()
    def test_wrong_build(self):
        self.build.write_text('{}')
        with self.assertRaises(RuntimeError):self.check()
    def test_wrong_argv(self):
        self.row['argv']=[str(self.binary),'serve']
        with self.assertRaises(RuntimeError):self.check()
    def test_policy_widening(self):
        self.row['policy_text']+='(allow network*)\n'
        (self.out/'gateway.sb').write_text(self.row['policy_text']);self.row['policy']=pin(self.out/'gateway.sb');self.repin()
        with self.assertRaises(RuntimeError):self.check()
    def test_wrong_config(self):
        self.save(self.out/'providers.json',{'providers':[]});self.repin()
        with self.assertRaises(RuntimeError):self.check()
    def test_missing_model(self):
        (self.out/'models.body').write_text('{"object":"list","data":[]}')
        p=self.out/'models.json';meta=json.loads(p.read_text());meta['body']=pin(self.out/'models.body');self.save(p,meta);self.repin()
        with self.assertRaises(RuntimeError):self.check()
    def test_page_is_login_form(self):
        (self.out/'page.body').write_text('<html>Sign in</html>')
        p=self.out/'page.json';meta=json.loads(p.read_text());meta['body']=pin(self.out/'page.body');self.save(p,meta);self.repin()
        with self.assertRaises(RuntimeError):self.check()
    def test_raw_body_tampered(self):
        (self.out/'page.body').write_text('tampered')
        with self.assertRaises(RuntimeError):self.check()
    def test_listener_wrong_owner(self):
        self.row['listener_pids']['web']=[999]
        with self.assertRaises(RuntimeError):self.check()
    def test_residual_listener(self):
        self.row['after_listener_pids']['web']=[123]
        with self.assertRaises(RuntimeError):self.check()
    def test_cleanup_incomplete(self):
        self.row['children_reaped']=False
        with self.assertRaises(RuntimeError):self.check()
    def test_floor(self):
        self.row['capacity_samples']=[{'free_bytes':FLOOR-1}]*2
        with self.assertRaises(RuntimeError):self.check()
    def test_wrong_http_target(self):
        p=self.out/'page.json';m=json.loads(p.read_text());m['port']=39103;self.save(p,m);self.repin()
        with self.assertRaises(RuntimeError):self.check()
    def test_seed_changed(self):
        (self.out/'plugins.yml').write_text('plugins: []');self.repin()
        with self.assertRaises(RuntimeError):self.check()
    def test_file_set_empty(self):
        self.row['files']={}
        with self.assertRaises(RuntimeError):self.check()
    def test_signature_path_escape(self):
        self.row['signature']['logs']={'../x':{}}
        with self.assertRaises(RuntimeError):self.check()
    def test_admission_headroom(self):
        self.row['capacity_samples']=[{'free_bytes':FLOOR}]*2
        with self.assertRaises(RuntimeError):self.check()
    def test_model_posts_bool(self):
        self.row['model_posts']=False
        with self.assertRaises(RuntimeError):self.check()
    def test_archived_verify_without_binary(self):
        expected=pin(self.binary);self.binary.unlink()
        self.assertEqual(W.verify_receipt(self.out,self.row,self.build,expected)['status'],'PASS')
    def test_inherited_home(self):
        with patch.dict(os.environ,{'HOME':'/synthetic/home','CODEX_HOME':'/synthetic/codex','OPENAI_API_KEY':'fake'},clear=True):
            env=W.environment(Path('/private/fixture'),39101)
            self.assertEqual(env['HOME'],'/synthetic/home');self.assertEqual(env['CODEX_HOME'],'/synthetic/codex')
            self.assertNotIn('OPENAI_API_KEY',env)


if __name__=='__main__':unittest.main(verbosity=2)
