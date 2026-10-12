import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import ccc_daemon_liveness as live


class LivenessIdentityTests(unittest.TestCase):
    def test_fresh_health_publication_does_not_hide_stale_observation(self):
        targets = [{'surface_id': 'a'}, {'surface_id': 'b'}]
        states = {'a': {'observation_timings': {'completed_at': 999}},
                  'b': {'observation_timings': {'completed_at': 800}}}
        self.assertFalse(live.observations_current(targets, states, now=1000))
        targets[1]['paused'] = True
        self.assertTrue(live.observations_current(targets, states, now=1000))
        self.assertFalse(live.observations_current(targets, {}, now=1000))

    def test_repeated_stall_backoff_is_bounded_and_expires(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'journal'
            self.assertEqual(live.retry_timeout(path, now=10000), 90)
            for count, expected in [(1, 180), (2, 360), (3, 720), (4, 900), (20, 900)]:
                path.write_text(''.join(json.dumps({'event':'stalled','at':9999})+'\n' for _ in range(count)))
                self.assertEqual(live.retry_timeout(path, now=10000), expected)
            self.assertEqual(live.retry_timeout(path, now=12000), 90)

    def test_unrelated_parent_never_signalled(self):
        with patch.object(live.os, 'getppid', return_value=12), patch.object(live.os, 'kill') as kill:
            self.assertEqual(live.monitor(-1, 34, [1, 2], Path('/unused'), birth=lambda _: [1, 2]), 2)
            kill.assert_not_called()

    def test_reused_pid_never_signalled(self):
        with patch.object(live.os, 'getppid', return_value=12), patch.object(live.os, 'kill') as kill:
            self.assertEqual(live.monitor(-1, 12, [1, 2], Path('/unused'), birth=lambda _: [1, 3]), 2)
            kill.assert_not_called()

    def test_no_progress_token_after_guard_exit(self):
        # Fatal watchdog loss must bypass even non-daemon worker cleanup.
        program = '''
import threading
import ccc_daemon_liveness as live
threading.Thread(target=threading.Event().wait).start()
live.LivenessGuard('/unused').progress(b'M')
'''
        result = subprocess.run([sys.executable, '-B', '-c', program],
                                cwd=Path(live.__file__).parent, timeout=3)
        self.assertEqual(result.returncode, 75)

    def test_missing_identity_backs_off_recovers_and_limits_diagnostics(self):
        for initially_armed in (False, True):
            for unavailable in ('none', 'exception'):
                with self.subTest(initially_armed=initially_armed, unavailable=unavailable):
                    now, recovered, calls = [0.0], [False], [0]
                    def birth(_):
                        calls[0] += 1
                        self.assertLess(calls[0], 30, 'identity retries must not spin')
                        if initially_armed and calls[0] == 1:
                            return [1, 2]
                        if now[0] < 2:
                            if unavailable == 'exception':
                                raise OSError('temporary native query failure')
                            return None
                        recovered[0] = True
                        return [1, 2]
                    with patch.object(live.os, 'getppid', return_value=12), \
                            patch.object(live.os, 'kill') as kill, \
                            patch.object(live, 'record') as record, \
                            patch.object(live.time, 'monotonic', side_effect=lambda: now[0]), \
                            patch.object(live.time, 'sleep', side_effect=lambda delay: now.__setitem__(0, now[0] + delay)) as sleep, \
                            patch.object(live.select, 'select', return_value=([99], [], [])), \
                            patch.object(live.os, 'read', side_effect=lambda *a: b'S' if recovered[0] else b'MH'):
                        self.assertEqual(live.monitor(99, 12, [1, 2], Path('/unused'), birth=birth), 0)
                    self.assertTrue(recovered[0])
                    self.assertEqual(now[0], 2)
                    self.assertEqual(sleep.call_count, 8)
                    self.assertEqual([c.kwargs['event'] for c in record.call_args_list].count('identity_unavailable'), 1)
                    kill.assert_not_called()

    def test_identity_loss_during_term_grace_waits_for_fresh_identity_before_kill(self):
        now, term_at, unknown = [0.0], [None], [False]
        observed_signals = []
        def birth(_):
            unknown[0] = term_at[0] is not None and now[0] < term_at[0] + 1
            return None if unknown[0] else [1, 2]
        def send_signal(pid, kind):
            self.assertFalse(unknown[0], 'missing identity must never authorize a signal')
            observed_signals.append(kind)
            if kind == signal.SIGTERM:
                term_at[0] = now[0]
            else:
                self.assertGreaterEqual(now[0] - term_at[0], 1)
        def select_ready(_r, _w, _x, delay):
            now[0] += delay
            return [], [], []
        with patch.object(live.os, 'getppid', return_value=12), \
                patch.object(live.os, 'kill', side_effect=send_signal), \
                patch.object(live, 'record') as record, \
                patch.object(live.time, 'monotonic', side_effect=lambda: now[0]), \
                patch.object(live.time, 'sleep', side_effect=lambda delay: now.__setitem__(0, now[0] + delay)), \
                patch.object(live.select, 'select', side_effect=select_ready):
            self.assertEqual(live.monitor(99, 12, [1, 2], Path('/unused'),
                                          timeout=.5, grace=.1, birth=birth), 0)
        self.assertEqual(observed_signals, [signal.SIGTERM, signal.SIGKILL])
        self.assertEqual([c.kwargs['event'] for c in record.call_args_list].count('identity_unavailable'), 1)

    def test_pipe_broken_after_successful_poll_exits_without_cleanup(self):
        read_fd, write_fd = os.pipe()
        os.close(read_fd)
        self.addCleanup(os.close, write_fd)
        guard = live.LivenessGuard('/unused')
        guard.process, guard.fd = Mock(), write_fd
        guard.process.poll.return_value = None
        with patch.object(live.os, '_exit', side_effect=SystemExit(75)) as exit:
            with self.assertRaises(SystemExit) as stopped:
                guard.progress(b'M')
        self.assertEqual(stopped.exception.code, 75)
        exit.assert_called_once_with(75)
        self.assertNotIn(b'M', guard.last)


@unittest.skipUnless(sys.platform == 'darwin', 'real Darwin process birth required')
class RealProcessLivenessTests(unittest.TestCase):
    def test_actual_watchdog_loss_bypasses_stuck_non_daemon_worker_and_finally(self):
        program = r'''
import sys, threading
from pathlib import Path
import ccc_daemon_liveness as live
root = Path(sys.argv[1])
guard = live.LivenessGuard(root / 'events.jsonl')
guard.start()
worker = threading.Thread(target=threading.Event().wait, daemon=False)
worker.start()
guard.process.kill()
guard.process.wait(timeout=2)
try:
    guard.progress(b'M')
finally:
    (root / 'entered-cleanup').touch()
    worker.join()
'''
        with tempfile.TemporaryDirectory() as folder:
            result = subprocess.run([sys.executable, '-B', '-c', program, folder],
                                    cwd=Path(live.__file__).parent, capture_output=True, timeout=3)
            self.assertEqual(result.returncode, 75, result.stderr)
            self.assertFalse((Path(folder) / 'entered-cleanup').exists())

    def run_parent(self, mode):
        # Signal only this throwaway process; it is the monitor's actual parent.
        program = r'''
import json, os, signal, subprocess, sys, time
import ccc_daemon_liveness as live
mode, journal = sys.argv[1:]
if mode == 'ignore_term': signal.signal(signal.SIGTERM, signal.SIG_IGN)
read_fd, write_fd = os.pipe()
channels = "(b'M',b'H',b'O')" if mode in ('stale_observers','health_file_failure') else "(b'M',b'H')"
code = 'import json,sys; from pathlib import Path; import ccc_daemon_liveness as l; raise SystemExit(l.monitor(int(sys.argv[1]),int(sys.argv[2]),json.loads(sys.argv[3]),Path(sys.argv[4]),timeout=0.6,grace=0.25,channels=' + channels + '))'
child = subprocess.Popen([sys.executable, '-B', '-c', code, str(read_fd), str(os.getpid()), json.dumps(live.process_birth(os.getpid())), journal], pass_fds=(read_fd,))
os.close(read_fd)
until = time.monotonic() + 1.3
while time.monotonic() < until:
    if mode == 'healthy': os.write(write_fd,b'MH')
    elif mode == 'main_only': os.write(write_fd,b'M')
    elif mode == 'health_only': os.write(write_fd,b'H')
    elif mode == 'stale_observers': os.write(write_fd,b'MH')
    elif mode == 'health_file_failure': os.write(write_fd,b'MO')
    elif mode == 'eof': break
    time.sleep(0.05)
os.close(write_fd)
child.wait(timeout=3)
'''
        with tempfile.TemporaryDirectory() as folder:
            journal = Path(folder) / 'events.jsonl'
            result = subprocess.run([sys.executable, '-B', '-c', program, mode, str(journal)],
                                    cwd=Path(live.__file__).parent, capture_output=True, timeout=6)
            rows = [json.loads(x) for x in journal.read_text().splitlines()] if journal.exists() else []
            return result, rows

    def test_real_stalled_daemon_receives_term(self):
        result, rows = self.run_parent('stalled')
        self.assertEqual(result.returncode, -signal.SIGTERM, result.stderr)
        self.assertIn('stalled', [r['event'] for r in rows])

    def test_real_observer_stall_not_hidden_by_main_and_health(self):
        result, rows = self.run_parent('stale_observers')
        self.assertEqual(result.returncode, -signal.SIGTERM, result.stderr)
        self.assertEqual(next(r for r in rows if r['event'] == 'stalled')['stalled_channels'], ['O'])

    def test_health_file_failure_preserves_working_daemon(self):
        result, rows = self.run_parent('health_file_failure')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('health_publication_degraded', [r['event'] for r in rows])
        self.assertNotIn('sigterm', [r['event'] for r in rows])

    def test_frozen_shutdown_gets_kill_only_for_original_parent(self):
        result, rows = self.run_parent('ignore_term')
        self.assertEqual(result.returncode, -signal.SIGKILL, result.stderr)
        self.assertEqual([r['event'] for r in rows], ['armed', 'stalled', 'sigterm', 'sigkill'])

    def test_busy_main_cannot_hide_dead_health_writer(self):
        result, rows = self.run_parent('main_only')
        self.assertEqual(result.returncode, -signal.SIGTERM, result.stderr)
        self.assertEqual(next(r for r in rows if r['event'] == 'stalled')['stalled_channels'], ['H'])

    def test_health_heartbeat_cannot_hide_stalled_scheduler(self):
        result, rows = self.run_parent('health_only')
        self.assertEqual(result.returncode, -signal.SIGTERM, result.stderr)
        self.assertEqual(next(r for r in rows if r['event'] == 'stalled')['stalled_channels'], ['M'])

    def test_real_progress_survives_more_than_two_deadlines(self):
        result, rows = self.run_parent('healthy')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([r['event'] for r in rows], ['armed'])

    def test_normal_shutdown_does_not_signal(self):
        result, rows = self.run_parent('eof')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('stalled', [r['event'] for r in rows])


if __name__ == '__main__':
    unittest.main()
