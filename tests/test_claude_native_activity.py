"""The live Claude retry owns the turn, including both continuation writes."""
import copy
import tempfile
import time
import unittest
from unittest import mock

import cmux_codex_watch as w
from tests.test_watch import claude_grid_payload, span
from tests import test_claude_delivery_recovery as delivery_tests
from tests import test_claude_submit_not_sent as submit_tests
from tests import test_cmux_control_socket as socket_tests
from tests.test_cmux_viewport_socket import server, send, response


WAIT = "✻ Waiting for API response · will retry in 2m 36s · check your network"
META = "4% until auto-compact · ◎ /goal active (7h)"
ACTIVE = (
    WAIT,
    "✢ Generating… (5m 4s · almost done thinking with max effort)",
    "✽ Compacting conversation… (7m 7s · ↓ 5.9k tokens)",
    "✻ Reconnecting…",
    "✻ API error · Retrying in 18s · attempt 10/10",
)


def activity_frame(status=WAIT, *, echo=False, metadata=True):
    payload = claude_grid_payload(columns=180)
    grid = payload['render_grid']
    prompt = grid['cursor']['row']
    rows = status.splitlines()
    if 'Compacting' in status:
        rows.append('████████░░░░ 61%')
    if metadata:
        rows.append(META)
    grid['row_spans'].extend(
        span(prompt - len(rows) - 1 + i, 0, text, 0)
        for i, text in enumerate(rows)
    )
    if echo:
        grid['row_spans'].append(span(prompt, 2, w.CLAUDE_MESSAGE, 0))
    return payload


def state(payload):
    return w.classify_claude_grid(w.Grid.from_rpc(payload, 'surface-uuid'))


def hidden_activity_frame(status=WAIT, *, newer=None):
    """The measured hidden composer and configurable multi-row statusline."""
    payload = claude_grid_payload(columns=180)
    grid = payload['render_grid']
    grid['rows'] = 26
    grid['cursor']['row'] = 18
    rows = [(1, '❯ Original user task'), (2, '⏺ Previous assistant output.')]
    block = status.splitlines()
    if 'Compacting' in status:
        block.append('████████░░░░ 61%')
    if newer:
        block.append(newer)
    block.append(META)
    rows.extend((16 - len(block) + i, text) for i, text in enumerate(block))
    rows.extend([
        (17, '─' * 100), (19, '─' * 100),
        (20, '  [Opus 5] │ ~/repo │ ⏱️  93h 50m'),
        (21, '  上下文 █████░░░░░ 48%'),
        (22, '  2 CLAUDE.md | 9 MCPs | 7 钩子'),
        (23, '  ✓ Bash ×16 | ✓ Read ×4'),
        (24, '  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents'),
    ])
    grid['row_spans'] = [span(row, 0, text, 0) for row, text in rows]
    return payload


class NativeActivityTests(unittest.TestCase):
    def test_hidden_composer_footer_cannot_hide_native_activity(self):
        for status, expected in zip(ACTIVE, ('retry', 'working', 'compacting', 'retry', 'retry')):
            with self.subTest(status=status):
                current = state(hidden_activity_frame(status))
                self.assertEqual((current.kind, current.claude_native_activity), ('working', expected))

    def test_hidden_composer_footer_keeps_newer_output_boundary(self):
        for newer in ('⏺ New unfinished answer.', 'API Error: 524 request failed', '✻ Worked for 3m'):
            with self.subTest(newer=newer):
                current = state(hidden_activity_frame(newer=newer))
                self.assertIsNone(current.claude_native_activity)
                self.assertNotEqual(current.kind, 'working')

    def test_current_native_activity_with_goal_metadata_is_working(self):
        for status in ACTIVE:
            with self.subTest(status=status):
                self.assertEqual(state(activity_frame(status)).kind, 'working')

    def test_waiting_retry_wraps_minutes_and_seconds(self):
        for duration in ('0.5s', '18s', '2m 36s', '1h 2m 3s'):
            for wrapped in (False, True):
                text = f'✻ Waiting for API response · will retry in {duration} · check your network'
                if wrapped:
                    text = text.replace(' · will', '\n  · will').replace(' · check', '\n  · check')
                with self.subTest(duration=duration, wrapped=wrapped):
                    result = state(activity_frame(text))
                    self.assertEqual((result.kind, result.error_type), ('working', 'claude_retry'))

    def test_goal_status_alone_is_not_evidence_of_a_running_turn(self):
        self.assertEqual(state(activity_frame('Unfinished answer.')).kind, 'claude_stopped')

    def test_old_retry_or_spinner_cannot_hide_new_output_or_terminal_error(self):
        for old in ACTIVE:
            for newer in ('Newer unfinished answer.', 'API Error: 524 request failed'):
                with self.subTest(old=old, newer=newer):
                    result = state(claude_grid_payload(lines=[old, newer], columns=180))
                    self.assertEqual(result.kind, 'recoverable_error' if newer.startswith('API') else 'claude_stopped')

    def test_echo_stays_owned_while_native_activity_blocks_enter(self):
        for status in ACTIVE:
            with self.subTest(status=status), tempfile.TemporaryDirectory() as root:
                d,t,r,e,c = submit_tests.SubmitNotSentTests().setup_case(root)
                c._base_payload = activity_frame(status)
                current = state(c.replay(t['workspace_id'],t['surface_id']))
                self.assertEqual(current.kind, 'composer_busy')
                self.assertTrue(current.watchdog_echo)
                self.assertFalse(d._send_claude_enter(t,r,c,reason='native',input_check=lambda:True))
                self.assertEqual(c.sent_keys, [])
                self.assertFalse(r.claude_submit_write_unknown)
                self.assertEqual(r.claude_submit_event_id, e['event_id'])

    def test_native_activity_cannot_confirm_or_expire_an_unsubmitted_transaction(self):
        for echo in (False, True):
            with self.subTest(echo=echo), tempfile.TemporaryDirectory() as root:
                d,t,r,e,c = submit_tests.SubmitNotSentTests().setup_case(root)
                r.claude_submit_since = time.time() - 1000
                current = state(activity_frame(echo=echo))
                self.assertTrue(d._reconcile_claude_submit(t,r,current,c))
                self.assertEqual(r.claude_submit_event_id, e['event_id'])
                self.assertEqual(r.claude_submit_phase, 'text_written')
                self.assertEqual(r.claude_submit_confirmed_at, 0)
                self.assertEqual(c.sent_keys, [])

    def test_runtime_and_context_guards_preserve_unsent_native_activity(self):
        for context, health in ((True, 'healthy'), (False, 'missing'),
                                (False, 'historical'), (False, 'legacy_override')):
            with self.subTest(context=context, health=health), tempfile.TemporaryDirectory() as root:
                d,t,r,e,c = submit_tests.SubmitNotSentTests().setup_case(root)
                r.claude_submit_since = time.time() - 1000
                r.claude_hook_health = health
                d.config['claude_context_enforcement'] = context
                current = state(hidden_activity_frame(ACTIVE[2] if context else WAIT))
                current = d._apply_claude_runtime_guards(t['surface_id'], r, current)
                current = d._apply_claude_context_guard(t['surface_id'], r, current)
                self.assertTrue(d._reconcile_claude_submit(t, r, current, c))
                self.assertEqual(r.claude_submit_phase, 'text_written')
                self.assertEqual(r.claude_submit_event_id, e['event_id'])
                self.assertEqual(r.claude_submit_confirmed_at, 0)
                self.assertEqual(c.sent_keys, [])

    def test_connected_enter_rechecks_native_activity(self):
        with tempfile.TemporaryDirectory() as root, server(lambda c,r:send(c,response(r))) as (transport,requests):
            d,t,r,e,c = submit_tests.SubmitNotSentTests().setup_case(root)
            live = socket_tests.ControlSocketTests().client(transport)
            before = c.replay(t['workspace_id'],t['surface_id'])
            after = activity_frame(echo=True)
            with mock.patch.object(live,'replay',side_effect=[before,after]):
                self.assertFalse(d._send_claude_enter(t,r,live,reason='connected',input_check=lambda:True))
            self.assertEqual(requests, [])
            self.assertFalse(r.claude_submit_write_unknown)
            self.assertTrue(r.claude_submit_not_sent)

    def test_text_boundary_rechecks_activity_after_preflight(self):
        with tempfile.TemporaryDirectory() as root:
            d,t,r,e,c = delivery_tests.DeliveryRecoveryTests().setup_case(root)
            original_mark = d.claude_event_ledger.mark
            def reserve(*args, **kwargs):
                result = original_mark(*args, **kwargs)
                c.payload = activity_frame()
                return result
            with mock.patch.object(d.claude_event_ledger,'mark',side_effect=reserve):
                sent, _ = d._send_claude_event(e,t,r,c)
            self.assertFalse(sent)
            self.assertEqual(c.sent_text, [])
            self.assertEqual(c.sent_keys, [])
            self.assertFalse(r.claude_submit_write_unknown)

    def test_connected_text_guard_uses_fresh_frame(self):
        with tempfile.TemporaryDirectory() as root, server(lambda c,r:send(c,response(r))) as (transport,requests):
            d,t,r,e,c = delivery_tests.DeliveryRecoveryTests().setup_case(root)
            live = socket_tests.ControlSocketTests().client(transport)
            before = copy.deepcopy(c.payload)
            frames = [before, before, before, activity_frame()]
            with mock.patch.object(live,'tree',side_effect=c.tree), \
                    mock.patch.object(live,'top',side_effect=c.top), \
                    mock.patch.object(live,'read_screen',side_effect=c.read_screen), \
                    mock.patch.object(live,'replay',side_effect=frames):
                sent, _ = d._send_claude_event(e,t,r,live)
            self.assertFalse(sent)
            self.assertEqual(requests, [])
            self.assertFalse(r.claude_submit_write_unknown)

    def test_retry_ending_releases_original_enter_once_without_repasting(self):
        with tempfile.TemporaryDirectory() as root:
            d,t,r,e,c = submit_tests.SubmitNotSentTests().setup_case(root)
            c._base_payload = activity_frame()
            self.assertFalse(d._send_claude_enter(t,r,c,reason='retrying',input_check=lambda:True))
            c._base_payload = claude_grid_payload(error='API Error: 524 request failed')
            d.save()
            restarted = w.WatchDaemon(d.config_path,d.state_path,client=c)
            recovered = restarted.runtime[t['surface_id']]
            with mock.patch.object(c,'send_text',side_effect=AssertionError('duplicate text')):
                self.assertTrue(restarted._send_claude_enter(t,recovered,c,reason='terminal',input_check=lambda:True))
            self.assertEqual(len(c.sent_text),1)
            self.assertEqual(len(c.sent_keys),1)
            self.assertEqual(recovered.send_count,1)
            self.assertEqual(restarted.claude_event_ledger.status_of(e['event_id']),'sent')


if __name__ == '__main__':
    unittest.main()
