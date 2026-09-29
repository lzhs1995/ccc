"""Exercise the real CmuxClient dispatch branch with isolated native evidence."""
import copy
import time
import unittest
from unittest.mock import patch

import cmux_codex_watch as core
import ccc_workspace_batch as batch
from tests.test_private_check import CheckFixture
from tests import test_private_check as private_fixture
from tests.test_watch import grid_payload, span, HIGH_DEMAND_TEXT


def draft_frame(sid, message):
    frame = grid_payload([], error=HIGH_DEMAND_TEXT)
    grid = frame['render_grid']
    grid['surface_id'] = sid
    row = grid['cursor']['row']
    grid['row_spans'] = [s for s in grid['row_spans'] if s['row'] != row]
    width = core._audit_display_width(message)
    grid['row_spans'].append(span(row, 0, '› ' + message, cell_width=2 + width))
    grid['cursor']['column'] = 2 + width
    return frame


class NativeClient(core.CmuxClient):
    def __init__(self, fake):
        self.fake = fake
        self.actions = []
        self.after_text = lambda: None
        self.before_key = lambda: None

    def replay(self, wid, sid):
        return copy.deepcopy(self.fake.payload)

    def tree(self):
        return self.fake.tree()

    def send_text(self, wid, sid, text):
        self.actions.append(('text', text))
        self.fake.payload = draft_frame(sid, text)
        self.after_text()

    def send_key(self, wid, sid, key):
        self.before_key()
        self.actions.append(('key', key))


class NativeEnterTests(CheckFixture, unittest.TestCase):
    def setUp(self):
        private_fixture.PrivateCheckDeliveryTests.setUp(self)
        self.native_client = NativeClient(self.client)

    def send(self):
        self.daemon._handle_state(self.target, self.runtime, self.state, self.native_client,
                                  send_guard_tree=self.native_client.tree())

    def mutate_enter_intent(self, change):
        persist = self.daemon._save_delivery
        changed = []
        def save(sid, runtime, *a, **kw):
            result = persist(sid, runtime, *a, **kw)
            if runtime.codex_input_phase == 'enter_pending' and not changed:
                changed.append(True)
                change()
            return result
        with patch.object(self.daemon, '_save_delivery', side_effect=save):
            self.send()
        self.assertTrue(changed)

    def assert_withheld(self):
        self.assertEqual(self.native_client.actions, [('text', batch.PROMPT)])
        self.assertEqual(self.runtime.delivery_status, 'unknown')
        self.runtime.awaiting = False
        self.runtime.last_send_at = 0
        self.send()
        self.assertEqual(self.native_client.actions, [('text', batch.PROMPT)])

    def test_private_text_then_durable_enter_then_key(self):
        def persisted():
            restored = {}
            self.daemon._delivery_store.restore(restored, core.TargetRuntime)
            self.assertEqual(restored[self.sid].codex_input_phase, 'enter_pending')
        self.native_client.before_key = persisted
        self.send()
        self.assertEqual(self.native_client.actions, [('text', batch.PROMPT), ('key', 'enter')])
        self.assertEqual(self.runtime.delivery_status, 'accepted')
        self.assertEqual(self.runtime.codex_input_phase, 'enter_acknowledged')

    def test_pause_during_enter_persistence_withholds_key_and_repaste(self):
        self.mutate_enter_intent(lambda: self.daemon.config_store.mutate(lambda c: c.update(global_paused=True)))
        self.assert_withheld()

    def test_user_draft_during_enter_persistence_is_retained(self):
        self.mutate_enter_intent(lambda: setattr(self.client, 'payload', draft_frame(self.sid, 'User draft')))
        self.assert_withheld()
        self.assertIn('User draft', str(self.client.payload))

    def test_new_turn_during_enter_persistence_withholds_key(self):
        self.mutate_enter_intent(lambda: self.append_turn('user', time.time(), message='My task'))
        self.assert_withheld()

    def test_user_item_without_new_turn_during_enter_persistence_withholds_key(self):
        self.mutate_enter_intent(lambda: self.append([{'type': 'response_item', 'payload': {
            'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'New work'}]}}]))
        self.assert_withheld()

    def test_lost_enter_ack_never_repeats_text_or_enter(self):
        key = self.native_client.send_key
        def lost(*args):
            key(*args)
            raise core.UncertainDeliveryError('lost Enter ACK')
        with patch.object(self.native_client, 'send_key', side_effect=lost):
            self.send()
            self.runtime.awaiting = False
            self.runtime.last_send_at = 0
            self.send()
        self.assertEqual(self.native_client.actions, [('text', batch.PROMPT), ('key', 'enter')])
        self.assertEqual(self.runtime.delivery_status, 'unknown')

    def test_generic_enter_rechecks_reloaded_global_authorization(self):
        for change in ({'global_paused': True}, {'mode': 'dry-run'}):
            with self.subTest(change=change):
                case = NativeEnterTests('test_generic_chinese_text_uses_explicit_enter')
                case.setUp()
                try:
                    case.append_turn('user', time.time() - 5, message='Fix my program')
                    def mutate():
                        case.daemon.config_store.mutate(lambda c: c.update(change))
                        case.daemon._reload_config_if_changed()
                    case.mutate_enter_intent(mutate)
                    self.assertEqual(case.native_client.actions, [('text', core.MESSAGE)])
                    self.assertEqual(case.runtime.delivery_status, 'unknown')
                    case.runtime.awaiting = False
                    case.runtime.last_send_at = 0
                    case.send()
                    self.assertEqual(case.native_client.actions, [('text', core.MESSAGE)])
                finally:
                    case.tearDown()
                    case.doCleanups()

    def test_generic_chinese_text_uses_explicit_enter(self):
        self.append_turn('user', time.time() - 5, message='Fix my program')
        self.send()
        self.assertEqual(self.native_client.actions, [('text', core.MESSAGE), ('key', 'enter')])


if __name__ == '__main__':
    unittest.main()
