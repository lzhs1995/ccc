# -*- coding: utf-8 -*-
"""V14 独立审查的真实文件测试（只读 P9，不改 Root 文件）。

  V14_BACKUP_DIR=<目录> python3.14 -B -m unittest test_v14_review -v
V14_BACKUP_DIR 只决定 ccc_compact_backup 从哪里导入（默认 P9）；其余一律取 P9。
F3 用真实子进程 os._exit 模拟进程猝死（非进程内异常）；临时目录建在本审查目录下。
"""
import base64
import errno
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
P9 = Path(os.environ["CCC_COMPACT_TEST_RUNTIME"]).resolve()
BACKUP_DIR = Path(os.environ.get('V14_BACKUP_DIR', str(P9))).resolve()
sys.path[:0] = [str(BACKUP_DIR), str(P9)]

import ccc_compact_backup as backup  # noqa: E402
import ccc_compact_intents as intents  # noqa: E402
import ccc_compact_pin_retirement as pr  # noqa: E402
from ccc_compact_support import OwnedDirectory  # noqa: E402

# 子进程：在指定崩溃点 os._exit(97)，不执行任何清理（模拟进程猝死）
CHILD = r'''
import os, sys
sys.path[:0] = [sys.argv[1]]
import ccc_compact_intents as intents, ccc_compact_pin_retirement as pr
point = sys.argv[3]
def crash(name):
    if name == point:
        os._exit(97)
pr._crash = crash
index = intents.IntentIndex(sys.argv[2] + '/native-input-intents.json',
                            state_root=sys.argv[4] + '/mac-state', active_root=sys.argv[4])
index.control_is_pending()
os._exit(0)
'''


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=str(HERE))
        self.root = Path(self.tmp.name).resolve()
        self.state = self.root / 'state'
        self.state.mkdir(mode=0o700)
        self.journal = self.state / 'target-a'
        self.journal.mkdir(mode=0o700)
        self.n = 0

    def tearDown(self):
        self.tmp.cleanup()

    def index(self):
        return intents.IntentIndex(self.state / 'native-input-intents.json',
                                   state_root=self.root / 'mac-state', active_root=self.root)

    def terminal_pin(self, index):
        # 冻结一个原始控制并写入精确 not-interrupted 终态回执
        self.n += 1
        key = '%024x' % self.n
        req = dict(threadId='t', expectedTurnId='turn-' + key[:6], attemptId=key,
                   intentPath=str(self.journal / (key + '-interrupt.json')))
        index.freeze_for_control(req)
        path = self.journal / (key + '-not-interrupted.json')
        with open(path, 'x') as f:
            json.dump(dict(episode_key=key, original_intent=req['intentPath'],
                           fields=dict(turnId=req['expectedTurnId'], attemptId=key)), f)
        os.chmod(path, 0o600)
        return key, (self.state / 'native-control-pins-v4' / (key + '.json')).read_bytes(), path.read_bytes()


class F4BackupTempHygiene(Base):
    """失败的发布不得每次重试都留下一份完整大小的临时副本。"""

    def temps(self):
        return [e for e in os.listdir(self.journal) if e.startswith('.backup-')]

    def test_failed_rename_retries_do_not_accumulate_full_size_temps(self):
        raw = b'x' * 4096
        with OwnedDirectory(self.journal) as owned:
            with patch.object(backup, '_rename_exclusive',
                              side_effect=OSError(errno.ENOTSUP, 'unsupported')):
                for _ in range(5):
                    with self.assertRaises(OSError):
                        backup.publish_backup(owned, 'b.json', raw)
            self.assertFalse((self.journal / 'b.json').exists())
            self.assertEqual(self.temps(), [], 'each failed retry left a full-size temp copy')
            backup.publish_backup(owned, 'b.json', raw)
        self.assertEqual((self.journal / 'b.json').read_bytes(), raw)
        self.assertEqual(os.stat(self.journal / 'b.json').st_nlink, 1)

    @unittest.skip("Held-fd partial-write gate is test_backup_ownership")
    def test_failed_temp_write_does_not_leave_partial_temp(self):
        raw = b'y' * 4096
        real = OwnedDirectory.write_once_bytes

        def torn(self_, name, data):
            # 模拟 ENOSPC：临时件已建、只写入一半
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600,
                         dir_fd=self_.fds[-1])
            os.write(fd, data[:len(data) // 2])
            os.close(fd)
            raise OSError(errno.ENOSPC, 'No space left on device')
        with OwnedDirectory(self.journal) as owned:
            with patch.object(OwnedDirectory, 'write_once_bytes', torn):
                with self.assertRaises(OSError):
                    backup.publish_backup(owned, 'c.json', raw)
            self.assertEqual(self.temps(), [], 'partial temp left after ENOSPC')
            self.assertIs(OwnedDirectory.write_once_bytes, real)


class F3RealProcessDeath(Base):
    """真实子进程 os._exit：after_link / after_archive 后重启恢复且原字节保留。"""

    def test_process_death_after_link_and_after_archive_recovers_with_raw_bytes(self):
        for point in ('after_link', 'after_archive'):
            with self.subTest(point=point):
                key, pin_raw, receipt_raw = self.terminal_pin(self.index())
                p = subprocess.run([sys.executable, '-B', '-c', CHILD, str(P9), str(self.state), point, str(self.root)],
                                   capture_output=True, text=True, timeout=60)
                self.assertEqual(p.returncode, 97, p.stderr)
                active = self.state / 'native-control-pins-v4' / (key + '.json')
                settled = self.state / 'native-control-pins-settled-v1' / (key + '.json')
                self.assertEqual(active.read_bytes(), pin_raw, 'pin lost at process death')
                self.assertEqual(os.stat(settled).st_nlink, 2 if point == 'after_link' else 1)
                self.assertFalse(self.index().control_is_pending())
                self.assertFalse(active.exists())
                rec = json.loads(settled.read_bytes())
                self.assertEqual(base64.b64decode(rec['pin']['raw_base64']), pin_raw)
                self.assertEqual(base64.b64decode(rec['receipt']['raw_base64']), receipt_raw)
                self.assertEqual(os.stat(settled).st_nlink, 1)
                self.assertEqual([e for e in os.listdir(settled.parent) if e.startswith('.')], [])


class F3ConcurrentLegitimateCallers(Base):
    def test_threads_sharing_one_index_retire_each_pin_once(self):
        index = self.index()
        keys = [self.terminal_pin(index)[0] for _ in range(20)]
        errors, results = [], []
        barrier = threading.Barrier(8, timeout=15)

        def worker():
            barrier.wait()
            try:
                results.append(index.control_is_pending())
            except Exception as exc:  # 记录任何异常
                errors.append(repr(exc))
        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
            self.assertFalse(t.is_alive(), "retirement worker did not terminate")
        self.assertEqual(errors, [])
        self.assertEqual(results, [False] * 8)
        settled = self.state / 'native-control-pins-settled-v1'
        self.assertEqual(sorted(p[:-5] for p in os.listdir(settled)), keys)
        self.assertEqual(os.listdir(self.state / 'native-control-pins-v4'), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
