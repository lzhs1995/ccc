# -*- coding: utf-8 -*-
"""V15 真实内核故障与边界负例（只读 P10，不改 Root 文件）。

  CCC_COMPACT_TEST_RUNTIME=<P10> python3.14 -B -m unittest test_v15_ownership_faults -v
不复刻实现内部检查：用真实 RLIMIT_FSIZE→EFBIG 半写、真实 rename 成功后中断、
外来硬链接、fd 计数等可观测后果断言。临时目录建在本审查目录下。
"""
import errno
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
RUNTIME = Path(os.environ['CCC_COMPACT_TEST_RUNTIME']).resolve()
sys.path.insert(0, str(RUNTIME))
import ccc_compact_backup as backup  # noqa: E402
from ccc_compact_support import OwnedDirectory  # noqa: E402

# 子进程：先写原件，再把 RLIMIT_FSIZE 压到原件一半，publish 时内核真实返回 EFBIG（半写）
CHILD = r'''
import json, os, resource, signal, sys
sys.path.insert(0, sys.argv[1])
import ccc_compact_backup as backup
from ccc_compact_support import OwnedDirectory
signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
raw = b'z' * 200000
with OwnedDirectory(sys.argv[2]) as owned:
    owned.write_once_bytes('original.json', raw)
    resource.setrlimit(resource.RLIMIT_FSIZE, (100000, resource.RLIM_INFINITY))
    try:
        backup.publish_backup(owned, 'b.json', raw)
        out = dict(raised=None)
    except OSError as exc:
        out = dict(raised=exc.errno, notes=getattr(exc, '__notes__', []))
print(json.dumps(out))
'''


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=str(HERE))
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name).resolve()
        self.raw = b'{"original":"retained"}\n' * 64

    def temps(self):
        return sorted(p.name for p in self.dir.glob('.backup-*.tmp'))


class RealKernelFaults(Base):
    def test_real_efbig_partial_write_leaves_no_temp_and_original_intact(self):
        p = subprocess.run([sys.executable, '-I', '-B', '-c', CHILD, str(RUNTIME), str(self.dir)],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        out = json.loads(p.stdout.strip().splitlines()[-1])
        self.assertEqual(out['raised'], errno.EFBIG, out)
        self.assertEqual(out['notes'], [], 'own partial temp was not discarded')
        self.assertEqual(self.temps(), [])
        self.assertFalse((self.dir / 'b.json').exists())
        self.assertEqual((self.dir / 'original.json').read_bytes(), b'z' * 200000)

    def test_interrupt_after_real_rename_never_deletes_published_final(self):
        real = backup._rename_exclusive

        def rename_then_interrupt(fd, source, destination):
            real(fd, source, destination)  # 真实独占 rename 已成功
            raise KeyboardInterrupt('signal after publication')
        with OwnedDirectory(self.dir) as owned:
            with patch.object(backup, '_rename_exclusive', rename_then_interrupt):
                with self.assertRaises(KeyboardInterrupt) as raised:
                    backup.publish_backup(owned, 'b.json', self.raw)
        self.assertEqual((self.dir / 'b.json').read_bytes(), self.raw)
        self.assertEqual(os.stat(self.dir / 'b.json').st_nlink, 1)
        self.assertEqual(self.temps(), [])
        # 清理拒绝（临时名已不存在）只追加 note，不掩盖原中断
        self.assertTrue(getattr(raised.exception, '__notes__', []))

    def test_foreign_hardlink_to_temp_before_failure_is_retained(self):
        alias = self.dir / 'foreign-alias'

        def link_then_fail(fd, source, destination):
            os.link(source, alias, src_dir_fd=fd)
            raise OSError(errno.ENOTSUP, 'original rename failure')
        with OwnedDirectory(self.dir) as owned:
            with patch.object(backup, '_rename_exclusive', link_then_fail):
                with self.assertRaisesRegex(OSError, 'original rename failure') as raised:
                    backup.publish_backup(owned, 'b.json', self.raw)
        self.assertTrue(any('retained' in n for n in getattr(raised.exception, '__notes__', [])))
        self.assertEqual(len(self.temps()), 1)
        self.assertEqual(alias.read_bytes(), self.raw)
        self.assertEqual(os.stat(alias).st_nlink, 2)
        self.assertFalse((self.dir / 'b.json').exists())

    def test_no_descriptor_leak_across_failures_and_success(self):
        def open_fds():
            return len(os.listdir('/dev/fd'))
        with OwnedDirectory(self.dir) as owned:
            before = open_fds()
            with patch.object(backup, '_rename_exclusive', side_effect=OSError(errno.ENOTSUP, 'x')):
                for _ in range(20):
                    with self.assertRaises(OSError):
                        backup.publish_backup(owned, 'b.json', self.raw)
            backup.publish_backup(owned, 'b.json', self.raw)
            self.assertEqual(open_fds(), before)
        self.assertEqual(self.temps(), [])

    def test_existing_final_with_different_bytes_is_never_overwritten(self):
        with OwnedDirectory(self.dir) as owned:
            owned.write_once_bytes('b.json', b'foreign final')
            with self.assertRaises(FileExistsError):
                backup.publish_backup(owned, 'b.json', self.raw)
        self.assertEqual((self.dir / 'b.json').read_bytes(), b'foreign final')
        self.assertEqual(self.temps(), [])

    def test_symlink_at_final_name_is_not_followed_or_replaced(self):
        target = self.dir / 'elsewhere'
        target.write_bytes(b'untouched')
        (self.dir / 'b.json').symlink_to(target)
        with OwnedDirectory(self.dir) as owned:
            with self.assertRaises((ValueError, FileExistsError)):
                backup.publish_backup(owned, 'b.json', self.raw)
        self.assertTrue((self.dir / 'b.json').is_symlink())
        self.assertEqual(target.read_bytes(), b'untouched')
        self.assertEqual(self.temps(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
