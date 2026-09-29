import copy
import dataclasses
import http.server
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ccc_mihomo as mihomo
import ccc_network_guard as network
from ccc_network_client import NetworkClient, summary
from tests.test_network_guard import fixtures
from tests import test_network_guard as legacy_tests


def config_and_routes():
    config, routes = fixtures()
    config['probe'] = {'validation_mode': 'reachability', 'url': 'https://anyrouter.test/v1/responses'}
    return config, routes


class ReachabilityClassifierTests(unittest.TestCase):
    def classify(self, status, body, headers=None):
        return mihomo.classify(status, 'application/json', body,
                               validation_mode='reachability', headers=headers)

    def test_structured_api_errors_prove_reachability_not_generation(self):
        for status in (400, 401, 403, 404, 405, 408, 413, 415, 422, 429, 500, 502, 503, 504):
            with self.subTest(status=status):
                result = self.classify(status, b'{"error":{"message":"authentication or upstream unavailable"}}')
                self.assertEqual(result.kind, 'accessible')
                self.assertFalse(result.deep)
                self.assertIn('not tested', result.detail)

    def test_explicit_waf_wins_over_auth_rate_limit_and_server_codes(self):
        for status in (200, 401, 403, 429, 500, 503):
            for body, headers in ((b'<html>/cdn-cgi/challenge-platform/foo</html>', {}),
                                  (b'{"error":{"code":"ip_blocked"}}', {}),
                                  (b'{"error":"denied"}', {'CF-Mitigated': 'challenge'})):
                with self.subTest(status=status, body=body):
                    self.assertEqual(self.classify(status, body, headers).kind, 'blocked')

    def test_generic_html_ray_id_and_unrecognized_json_do_not_prove_ban_or_admission(self):
        for status in (200, 403, 429, 500, 502):
            for body in (b'<html>Forbidden</html>', b'<html>502 Bad Gateway Cloudflare Ray ID: test</html>',
                         b'{"ok":true}', b'', b'upstream unavailable'):
                with self.subTest(status=status, body=body):
                    self.assertEqual(self.classify(status, body).kind, 'contract')

    def test_legacy_default_still_requires_model_proof(self):
        self.assertEqual(mihomo.classify(401, 'application/json', b'{"error":"no key"}').kind, 'auth')
        self.assertEqual(mihomo.classify(500, 'application/json', b'{"error":"overload"}').kind, 'upstream')


class ReachabilityWireTests(unittest.TestCase):
    def test_no_key_prompt_or_custom_auth_headers_even_when_config_contains_them(self):
        captured = []
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                captured.append((dict(self.headers), self.rfile.read(int(self.headers['Content-Length']))))
                raw = b'{"error":{"message":"missing api key"}}'
                self.send_response(401)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.send_header('Set-Cookie', 'one=fixture')
                self.send_header('Set-Cookie', 'two=fixture')
                self.end_headers()
                self.wfile.write(raw)
            def log_message(self, *_):
                pass
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        _, routes = config_and_routes()
        item = routes[0]
        config = {'validation_mode':'reachability','url':'http://127.0.0.1/v1/responses',
                  'auth_file':'/missing-auth', 'model':'paid-model',
                  'headers':{'Authorization':'Bearer must-not-send','Cookie':'must-not-send','X-API-Key':'must-not-send'}}
        with mock.patch.object(mihomo, 'probe_key', side_effect=AssertionError('must not load credential')):
            probe = mihomo.ResponsesProbe(config, {item.id:server.server_port}, credential='must-not-send')
            result = probe.run(item)
        self.assertEqual(result.kind, 'accessible')
        self.assertEqual(len(captured), 1)
        headers, body = captured[0]
        self.assertEqual(body, b'{"model":')
        with self.assertRaises(ValueError):
            json.loads(body)
        for key in ('Authorization', 'Cookie', 'X-API-Key'):
            self.assertNotIn(key, headers)
        self.assertEqual(probe.run(item, deep=True).kind, 'observer_error')
        with self.assertRaisesRegex(ValueError, 'cannot create'):
            probe.request_body()
        self.assertEqual(len(captured), 1)

    def test_binding_ignores_key_model_and_timeout_and_does_not_read_auth(self):
        config, _ = config_and_routes()
        before = network.probe_binding(config)
        config['probe'].update(auth_file='/missing-file',auth_env='PRIVATE_KEY',model='changed',timeout_sec=9)
        with mock.patch.object(network, 'probe_credential', side_effect=AssertionError('must not read')):
            self.assertEqual(network.probe_binding(config), before)
        config['probe']['url'] = 'https://anyrouter.test/other'
        self.assertNotEqual(network.probe_binding(config)[1], before[1])


class ReachabilityEngineTests(unittest.TestCase):
    def setUp(self):
        self.config, self.routes = config_and_routes()
        self.e = network.Engine(self.config, self.routes, now=1000)
        self.ids = [r.id for r in self.routes]
        self.e.current = self.ids[0]

    def good(self, rid, now=1001):
        self.e.record(rid, mihomo.ProbeResult('accessible', status=401), now)

    def test_reachable_route_is_eligible_without_a_key_or_model(self):
        self.good(self.ids[0])
        self.assertTrue(self.e.ready(self.ids[0],1002))
        self.assertEqual(self.e.decision(1002),(self.ids[0],'healthy'))
        self.assertFalse(self.e.health[self.ids[0]].qualified)
        self.assertEqual(self.e.health[self.ids[0]].deep_ok_at,0)

    def test_other_route_survives_normal_sixty_second_interval_and_expires(self):
        self.good(self.ids[1])
        self.assertTrue(self.e.ready(self.ids[1],1061))
        self.assertTrue(self.e.ready(self.ids[1],1081))
        self.assertFalse(self.e.ready(self.ids[1],1081.001))
        self.assertFalse(self.e.ready(self.ids[1],1000))

    def test_first_timeout_is_rechecked_and_repeated_timeout_moves_to_other_route(self):
        self.good(self.ids[0]); self.good(self.ids[1])
        self.e.record(self.ids[0],mihomo.ProbeResult('timeout'),1002)
        self.assertEqual(self.e.decision(1002),(self.ids[0],'suspect'))
        self.e.record(self.ids[0],mihomo.ProbeResult('timeout'),1003)
        self.assertEqual(self.e.decision(1003),(self.ids[1],'healthy'))

    def test_waf_recovery_requires_cooldown_and_three_new_successes(self):
        rid = self.ids[0]
        self.good(rid)
        self.e.record(rid,mihomo.ProbeResult('blocked'),1002)
        for stamp in (1003,1004,1061,1063,1064):
            self.good(rid,stamp)
            self.assertFalse(self.e.ready(rid,stamp))
        self.good(rid,1065)
        self.assertTrue(self.e.ready(rid,1065))

    def test_late_success_started_before_block_cannot_recover_route(self):
        rid=self.ids[0]
        self.e.record(rid,mihomo.ProbeResult('blocked'),1002)
        self.e.record(rid,mihomo.ProbeResult('accessible'),1063,started_at=1001)
        self.assertEqual(self.e.reachability[rid].kind,'blocked')
        self.assertFalse(self.e.ready(rid,1063))

    def test_unknown_response_is_not_a_ban_or_a_new_candidate(self):
        self.e.record(self.ids[0],mihomo.ProbeResult('contract',status=502),1001)
        self.assertFalse(self.e.ready(self.ids[0],1001))
        self.assertFalse(self.e.reachability[self.ids[0]].quarantined)
        self.assertEqual(self.e.decision(1001),(self.ids[0],'checking'))

    def test_no_paid_scheduling_reservation_or_seed(self):
        self.good(self.ids[0])
        self.assertIsNone(self.e.deep_due(1002,set()))
        with self.assertRaisesRegex(RuntimeError,'disabled'):
            self.e.reserve_deep(self.ids[0],1002)
        self.e.seed({self.ids[1]:{'at':1001,'kind':'healthy','deep':True}},1002)
        self.assertFalse(self.e.ready(self.ids[1],1002))
        self.assertFalse(self.e.seed_consumed)
        self.e.record(self.ids[1],mihomo.ProbeResult('healthy',deep=True),1002)
        self.assertFalse(self.e.ready(self.ids[1],1002))

    def test_restart_keeps_legacy_model_evidence_and_spent_budget_without_using_it(self):
        legacy_config,routes=fixtures()
        old=network.Engine(legacy_config,routes,now=1000)
        rid=routes[0].id
        old.record(rid,mihomo.ProbeResult('accessible'),1001)
        old.record(rid,mihomo.ProbeResult('healthy',deep=True),1001)
        old.reserve_deep(rid,1001);old.settle_deep(1001)
        old.record(rid,mihomo.ProbeResult('upstream',deep=True,status=500),1002)
        saved=copy.deepcopy(old.saved())
        new=network.Engine(self.config,routes,saved,now=1003)
        self.assertEqual(dataclasses.asdict(new.health[rid]),saved['health'][rid])
        self.assertEqual(new.api.history,saved['api_probe']['history'])
        self.assertEqual(new.deep_starts,saved['deep_starts'])
        self.assertIsNone(new.deep_due(100000,set()))
        self.assertEqual(new.deep_starts,saved['deep_starts'])
        self.assertFalse(new.ready(rid,1003))
        new.record(rid,mihomo.ProbeResult('accessible'),1004)
        restarted=network.Engine(self.config,routes,new.saved(),now=1005)
        self.assertTrue(restarted.ready(rid,1005))

    def test_tokyo_primary_order_and_sticky_commercial(self):
        for rid in self.ids:
            self.good(rid)
        self.e.current,self.e.active_pool=self.ids[3],'Yeye'
        self.assertEqual(self.e.decision(1002),(self.ids[3],'healthy'))
        for rid in self.ids[:4]:
            self.e.record(rid,mihomo.ProbeResult('blocked'),1002)
        self.assertEqual(self.e.decision(1002),(self.ids[4],'fallback'))

    def test_rebinding_endpoint_keeps_quarantine_but_requires_new_samples(self):
        rid = self.ids[0]
        self.good(rid)
        self.e.record(rid, mihomo.ProbeResult('blocked'), 1002)
        saved = self.e.saved()
        restarted = network.Engine(self.config, self.routes, saved, now=1003,
                                   reuse_reachability_evidence=False)
        self.assertTrue(restarted.reachability[rid].quarantined)
        self.assertEqual(restarted.reachability[rid].at, 0)
        self.assertFalse(restarted.ready(rid, 1003))
        self.e.reset_reachability(preserve_current=True)
        self.assertTrue(self.e.reachability[rid].quarantined)
        self.assertEqual(self.e.reachability[rid].quarantine_until, 1062)
        for stamp in (1063, 1064):
            self.good(rid, stamp)
            self.assertFalse(self.e.ready(rid, stamp))
        self.good(rid, 1065)
        self.assertTrue(self.e.ready(rid, 1065))

    def test_restore_invalid_health_never_admits_or_causes_a_permanent_cooldown(self):
        rid = self.ids[0]
        self.good(rid)
        original = self.e.saved()
        for change in ({'kind': []}, {'status': '401'}, {'elapsed_ms': float('nan')},
                       {'ok_at': 1005}, {'stage': {}}, {'admitted': True, 'ok_at': 0}):
            saved = copy.deepcopy(original)
            saved['reachability_health'][rid].update(change)
            with self.subTest(change=change):
                new = network.Engine(self.config, self.routes, saved, now=1002)
                self.assertFalse(new.ready(rid, 1002))
        saved = copy.deepcopy(original)
        saved['reachability_health'][rid].update(quarantined=True, admitted=False,
                                                successes=0, quarantine_until=1e12)
        new = network.Engine(self.config, self.routes, saved, now=1002)
        self.assertEqual(new.reachability[rid].quarantine_until, 1062)
        for stamp in (1063, 1064, 1065):
            new.record(rid, mihomo.ProbeResult('accessible', status=401), stamp)
        self.assertTrue(new.ready(rid, 1065))

    def test_status_counts_include_reason_timestamp_and_no_fake_model_success(self):
        outcomes=('accessible','blocked','timeout','contract','observer_error')
        for rid,kind in zip(self.ids,outcomes):
            self.e.record(rid,mihomo.ProbeResult(kind,status=401 if kind=='accessible' else 0),1001)
        guard=object.__new__(network.Guard)
        guard.engine,guard.config=self.e,self.config
        guard.contract,guard.probe_binding_at='test',1000
        guard.observer_error=lambda:''
        guard.link=types.SimpleNamespace(status=lambda:{'available':True})
        rows=[{'id':rid,'ready':self.e.ready(rid,1002)} for rid in self.ids]
        value=guard.automatic_status(1002,rows)
        self.assertEqual(value['counts'],dict(reachable=1,blocked=1,failed=1,uncertain=1,pending=2))
        self.assertEqual(sum(value['counts'].values()),len(self.ids))
        self.assertEqual(value['last_checked_at'],1001)
        self.assertFalse(value['model_probes_enabled'])
        line=summary({'validation_mode':'reachability','phase':'manual','automatic':value,
                      'effective_route':{'selection':'chosen manual'}})
        for word in ('chosen manual','可达 1/6','拦截 1','失败 1','待确认 1','待检 2','最近'):
            self.assertIn(word,line)
        self.assertNotIn('自动候选 0',line)


class ReachabilityGuardIntegrationTests(unittest.TestCase):
    def harness(self):
        fixture = legacy_tests.GuardLoopTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.config['probe'] = {'validation_mode': 'reachability',
                                   'url': 'https://anyrouter.test/v1/responses'}
        fixture.config['publish_transit'] = True
        return fixture

    def test_real_guard_loop_publishes_reachability_without_a_paid_dispatch(self):
        fixture = self.harness()
        fixture.stepped_loop([1000, 1001, 1002, 1003, 1006, 1061, 1062])
        self.assertTrue(fixture.probe.run.called)
        self.assertTrue(all(not call.args[1] for call in fixture.probe.run.call_args_list))
        self.assertEqual(fixture.guard.engine.deep_starts, [])
        latest = fixture.snapshots[-1]
        self.assertEqual(latest['validation_mode'], 'reachability')
        self.assertGreater(latest['automatic']['ready'], 0)
        self.assertFalse(latest['automatic']['model_probes_enabled'])
        self.assertTrue(all(not row['deep_ok_at'] for row in latest['routes']))

    def test_mode_change_rejects_old_model_result_and_never_dispatches_another(self):
        fixture = self.harness()
        response_config = copy.deepcopy(fixture.config)
        response_config['probe'] = {'allow_unauthenticated_test': True}
        fixture.guard.config = response_config
        fixture.config_reader.return_value = response_config
        def switch(clock):
            if clock[0] >= 1001:
                fixture.config_reader.return_value = fixture.config
        fixture.stepped_loop([1000, 1001, 1002, 1003, 1004, 1006], on_tick=switch)
        events = [json.loads(line) for line in (fixture.root/'events.ndjson').read_text().splitlines()]
        self.assertEqual(sum(row['event'] == 'deep_reserved' for row in events), 1)
        self.assertTrue(any(row.get('deep') and row.get('discard_reason') == 'obsolete_contract'
                            for row in events))
        self.assertTrue(fixture.guard.engine.reachability_enabled)
        self.assertTrue(any(row['automatic']['ready'] for row in fixture.snapshots
                            if row['validation_mode'] == 'reachability'))

    def test_client_does_not_use_previous_mode_outage_or_missing_probe_shape(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'network.json'
            config = {'state_dir': temp, 'service_host': 'anyrouter.test',
                      'probe': {'validation_mode': 'reachability'}}
            path.write_text(json.dumps(config))
            (Path(temp)/'status.json').write_text(json.dumps({'version': 1,
                'service_host': 'anyrouter.test', 'phase': 'network_wait', 'at': time.time()}))
            client = NetworkClient()
            self.assertEqual(client.snapshot({'enabled': True, 'config_path': str(path)})['phase'],
                             'observer_fault')
            for broken in (None, [], 'reachability', 1):
                path.write_text(json.dumps({**config, 'probe': broken}))
                client = NetworkClient()
                with self.subTest(probe=broken):
                    self.assertEqual(client.snapshot({'enabled': True, 'config_path': str(path)})['phase'],
                                     'observer_fault')

    def test_load_config_explicit_mode_and_endpoint_without_model(self):
        config, _ = config_and_routes()
        config.update(version=1, state_dir='/fixture/state', binary='/fixture/mihomo',
                      controller_socket='/fixture/core.sock', interface='en0',
                      sources=[{'path': '/fixture/subscription.json', 'pool': 'NTHU'}],
                      publish={'port': 18000, 'token': 'x'*32})
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'network.json'
            path.write_text(json.dumps(config))
            self.assertEqual(network.load_config(path)['probe']['validation_mode'], 'reachability')
            for mode in (None, [], {}, 'automatic', 'response'):
                changed = copy.deepcopy(config)
                changed['probe']['validation_mode'] = mode
                path.write_text(json.dumps(changed))
                with self.subTest(mode=mode), self.assertRaises(ValueError):
                    network.load_config(path)


if __name__=='__main__':
    unittest.main()
