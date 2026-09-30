"""Evidence gates for the real native, loopback-only sustained N acceptance."""
import json
import re
import threading
import time

import cmux_codex_watch as core


class SustainedNativeProbe:
    minimum_requests = 1250
    minimum_seconds = 150

    def __init__(self, output):
        self.output = output
        self.allow_success = threading.Event()
        self.first_request_at = None
        self.last_sample = -100.0
        self.reconnects = {}
        self.continued_sessions = set()
        self.release = None

    def reject(self):
        # Called under the fixture server's condition, before recording every
        # actual HTTP request. No counter is manufactured by the test client.
        if self.first_request_at is None:
            self.first_request_at = time.monotonic()
        return not self.allow_success.is_set()

    def sample(self, client, workspace_id, slots):
        now = time.monotonic()
        if now - self.last_sample < .25:
            return
        self.last_sample = now
        # Sampling two real viewports is enough to witness native reconnect;
        # no fifty-surface full scan is required to keep the request gate safe.
        candidates = [s for s in slots if s.get('surface_id') and s.get('submit_at')][:2]
        for slot in candidates:
            raw = client.replay(workspace_id, slot['surface_id'])
            grid = core.Grid.from_rpc(raw, slot['surface_id'])
            match = re.search(r'Reconnecting(?:\.{3}|…)?\s+(\d+)/5', '\n'.join(grid.lines))
            if not match:
                continue
            number = int(match.group(1))
            key = slot['surface_id'] + ':' + str(number)
            if key not in self.reconnects:
                value = {'at': time.time(), 'monotonic': now, 'surface_id': slot['surface_id'],
                         'session_id': slot['session_id'], 'number': number, 'of': 5, 'viewport': raw}
                self.reconnects[key] = value
                core.atomic_write_json(self.output / ('native-reconnect-' + str(slot['index']) + '-' +
                                                       str(number) + '.json'), value)

    def maybe_release(self, server, current, original_failed):
        for sid, task in current.items():
            if task and task.get('turn_id') and task['turn_id'] != original_failed[sid]['turn_id']:
                self.continued_sessions.add(sid)
        if self.allow_success.is_set() or self.first_request_at is None:
            return
        with server.condition:
            count = len(server.requests)
        elapsed = time.monotonic() - self.first_request_at
        if (count < self.minimum_requests or elapsed < self.minimum_seconds
                or len(self.continued_sessions) != 50
                or not any(row['number'] == 5 for row in self.reconnects.values())):
            return
        self.release = {'at': time.time(), 'monotonic': time.monotonic(), 'http_before_success_enabled': count,
                        'seconds_of_rejections': elapsed, 'continued_original_sessions': 50,
                        'native_5_of_5_observed': True}
        core.atomic_write_json(self.output / 'sustained-success-release.json', self.release)
        self.allow_success.set()

    def evidence(self):
        assert self.release and self.allow_success.is_set(), 'sustained native gates did not complete'
        return {**self.release, 'minimum_requests': self.minimum_requests,
                'minimum_seconds': self.minimum_seconds,
                'continued_surface_ids': sorted(self.continued_sessions),
                'reconnect_steps_observed': sorted({r['number'] for r in self.reconnects.values()}),
                'native_reconnect_viewport_samples': len(self.reconnects)}
