import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests import test_standby_recovery_chain as chain_fixture
from tools.standby_performance_replay import verify
from tools.standby_recovery_chain import evaluate


class ReplayTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.witnesses, deliveries, responses, bindings, chains = {}, {}, [], [], []
        for i in range(50):
            case = chain_fixture.ChainTests(); case.setUp()
            sid, surface = 'session'+str(i), 'surface'+str(i)
            records = json.loads(json.dumps(case.records).replace('"session"', json.dumps(sid)))
            rows = json.loads(json.dumps(case.responses).replace('"surface"', json.dumps(surface)))
            for row in rows:
                row['request']['id'] += str(i); row['reply']['id'] += str(i)
            delivery = copy.deepcopy(case.delivery)
            delivery['surface_id'] = surface
            delivery['runtime']['codex_sent_turn_key'] = sid+':one:2'
            witness = dict(index=i, session_id=sid, surface_id=surface)
            self.witnesses[i] = witness; deliveries[surface] = delivery; responses += rows
            raw = ('\n'.join(json.dumps(r) for r in records)+'\n').encode()
            original = self.root/f'original-{i}.jsonl'; original.write_bytes(raw)
            saved = self.root/f'saved-{i}.jsonl'; saved.write_bytes(raw)
            bindings.append(dict(index=i, original=str(original), saved=str(saved),
                                 sha256=hashlib.sha256(raw).hexdigest()))
            chains.append(evaluate(records, witness, rows, delivery, 'OK'))
        self.delivery = self.root/'original-deliveries.json'
        self.delivery.write_text(json.dumps(deliveries))
        self.startup = {'startup_passed': True}
        evidence = {}
        for name, value in [('activation-ui.json', '{}'), ('activation-terminal.json', '{}'),
                ('rpc-responses.ndjson', '\n'.join(json.dumps(r) for r in responses))]:
            path = self.root/name; path.write_text(value)
            evidence[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
        self.result = dict(transcript_bindings=bindings, chains=chains,
                           startup=self.startup, evidence_sha256=evidence)

    def replay(self, **kwargs):
        # Timing evaluator has its own real contract tests; all recovery and IO here are real.
        with patch('tools.standby_performance_replay.evaluate_startup', return_value=self.startup):
            return verify(self.result, witnesses=self.witnesses,
                          deliveries_path=self.delivery, prompt='OK', **kwargs)

    def test_replays_all_fifty_original_chains(self):
        result = self.replay()
        self.assertEqual(result['replayed_originals'], 50)
        self.assertTrue(result['recovery_passed'])
        self.assertEqual(len(result['evidence_sha256']), 104)

    def test_forged_stored_metric_refused(self):
        self.result['chains'][49]['native_next_ms'] = 0
        with self.assertRaisesRegex(ValueError, 'differs'):
            self.replay()

    def test_original_changed_even_when_saved_copy_intact_refused(self):
        Path(self.result['transcript_bindings'][49]['original']).write_text('{}')
        with self.assertRaisesRegex(ValueError, 'hash changed'):
            self.replay()

    def test_duplicate_transcript_index_refused(self):
        self.result['transcript_bindings'][49]['index'] = 0
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            self.replay()

    def test_delivery_changed_to_unknown_ack_refused(self):
        delivery = json.loads(self.delivery.read_text())
        delivery['surface49']['runtime']['delivery_status'] = 'unknown'
        self.delivery.write_text(json.dumps(delivery))
        with self.assertRaisesRegex(ValueError, 'durable'):
            self.replay()

    def test_deadline_prevents_any_read(self):
        def expired():
            raise TimeoutError('expired')
        with self.assertRaises(TimeoutError):
            self.replay(check=expired)

    def test_input_mutation_during_replay_is_refused(self):
        from tools import standby_performance_replay as module
        original = module.evaluate_recovery
        count = [0]
        def mutate(*args):
            result = original(*args); count[0] += 1
            if count[0] == 50:
                self.delivery.write_text('{}')
            return result
        with patch.object(module, 'evaluate_recovery', side_effect=mutate):
            with self.assertRaisesRegex(ValueError, 'changed during'):
                self.replay()
        self.assertEqual(count[0], 50)
