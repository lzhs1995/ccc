"""Legacy identity remains strict without launching slow ps processes."""
import ctypes
import unittest
from unittest.mock import patch

import ccc_codex_queue as queue


def args_payload(argv, env):
    return (len(argv).to_bytes(4, 'little', signed=True) + b'/opt/bin/codex\0\0'
            + b'\0'.join(argv) + b'\0' + b'\0'.join(env) + b'\0')


class NativePlacementTests(unittest.TestCase):
    def test_arguments_cannot_spoof_environment_or_hide_duplicate_ownership(self):
        target = {'CMUX_SURFACE_ID': 's', 'CMUX_WORKSPACE_ID': 'w'}
        payload = args_payload([b'codex', b'CMUX_SURFACE_ID=foreign', b''],
                               [b'CMUX_SURFACE_ID=s', b'PRIVATE_TOKEN=irrelevant', b'CMUX_WORKSPACE_ID=w'])
        self.assertEqual(queue._process_placement_args(payload), target)
        self.assertNotIn('PRIVATE_TOKEN', queue._process_placement_args(payload))
        self.assertEqual(queue._process_placement_args(args_payload(
            [b'codex', b'CMUX_SURFACE_ID=s'], [b'CMUX_WORKSPACE_ID=w'])), {'CMUX_WORKSPACE_ID': 'w'})
        self.assertIsNone(queue._process_placement_args(payload + b'CMUX_SURFACE_ID=s\0'))
        for bad in (b'', b'\x00' * 6, payload[:15], args_payload([b'zsh'], [])):
            with self.subTest(bad=bad):
                self.assertIsNone(queue._process_placement_args(bad))

    def test_uncached_ownership_checks_do_not_spawn_ps_and_fail_closed(self):
        target = {'surface_id': 's', 'workspace_id': 'w'}
        payload = args_payload([b'/opt/bin/codex'], [b'CMUX_SURFACE_ID=s', b'CMUX_WORKSPACE_ID=w'])
        def sysctl(mib, count, buffer, length, new, new_length):
            self.assertEqual(list(mib), [1, 49, 123])
            ctypes.memmove(buffer, payload, len(payload))
            ctypes.cast(length, ctypes.POINTER(ctypes.c_size_t))[0] = len(payload)
            return 0
        with patch.object(queue, '_proc_pidinfo', object()), \
             patch.object(queue, '_procargs_bytes', 4096), \
             patch.object(queue, '_procargs_sysctl', side_effect=sysctl) as read, \
             patch.object(queue, 'codex_process_starts', return_value={123: 1234}) as starts, \
             patch.object(queue.subprocess, 'run', side_effect=AssertionError('ps must not run')):
            self.assertEqual(queue.process_placement_start(123, target), 1234)
            self.assertIsNone(queue.process_placement_start(123, {**target, 'surface_id': 'foreign'}))
            starts.return_value = {123: 2345}
            self.assertEqual(queue.process_placement_start(123, target), 2345)
            read.side_effect = None
            read.return_value = -1
            self.assertIsNone(queue.process_placement_start(123, target))
            starts.return_value = {}
            self.assertIsNone(queue.process_placement_start(123, target))


if __name__ == '__main__':
    unittest.main()
