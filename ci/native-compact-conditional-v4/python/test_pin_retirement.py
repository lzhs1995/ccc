# -*- coding: utf-8 -*-
"""V13 真实文件测试：终态 pin 退役归档。V13_INTENTS_DIR 选择被测 intents（基线或候选）。

  V13_INTENTS_DIR=<目录> python3.14 -B -m unittest test_v13_pin_retirement -v
基线应在「退役/累计>1024」类用例 RED，候选全部 GREEN；崩溃注入类只对候选有意义（基线跳过）。
临时目录建在本审查目录下（非 /tmp），结束自动删除。
"""
import base64
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASE = Path(os.environ.get("CCC_BASELINE_INTENTS_DIR", str(HERE / "baseline"))).resolve()
INTENTS_DIR = Path(os.environ.get('V13_INTENTS_DIR', str(BASE))).resolve()
sys.path[:0] = [str(INTENTS_DIR), str(BASE)]

import ccc_compact_intents as intents  # noqa: E402
try:
    import ccc_compact_pin_retirement as pr  # noqa: E402
except ImportError:
    pr = None
CANDIDATE = pr is not None and Path(intents.__file__).resolve().parent == INTENTS_DIR != BASE


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=str(HERE))
        self.root = Path(self.tmp.name).resolve()
        self.state = self.root / 'state'
        self.state.mkdir(mode=0o700)
        self.journal = self.state / 'target-a'
        self.journal.mkdir(mode=0o700)
        self.index = self.new_index()
        self.n = 0
        if pr is not None:
            self.addCleanup(setattr, pr, '_crash', pr._crash)

    def tearDown(self):
        for p in self.root.rglob('*'):
            if p.is_dir() and not p.is_symlink():
                os.chmod(p, 0o700)
        self.tmp.cleanup()

    def new_index(self):
        # 重启 = 新 IntentIndex 读同一目录
        return intents.IntentIndex(self.state / 'native-input-intents.json',
                                   state_root=self.root / 'mac-state', active_root=self.root)

    def request(self, key):
        return dict(threadId='thread-a', expectedTurnId='turn-' + key[:6], attemptId=key,
                    intentPath=str(self.journal / (key + '-interrupt.json')))

    def key(self):
        self.n += 1
        return '%024x' % self.n

    def freeze(self, index=None):
        key = self.key()
        (index or self.index).freeze_for_control(self.request(key))
        return key

    def receipt(self, key, phase, **override):
        req = self.request(key)
        fields = {'compact-started': dict(turnId='new-' + key[:6], joinAccounting={'status': 'not_accounted'}),
                  'not-interrupted': dict(turnId=req['expectedTurnId'], attemptId=key),
                  'compact-denied': dict(turnId=req['expectedTurnId'], attemptId=key, reason='goal_changed')}[phase]
        fields.update(override.pop('fields', {}))
        value = dict(episode_key=key, original_intent=req['intentPath'], fields=fields)
        value.update(override)
        path = self.journal / (key + '-' + phase + '.json')
        with open(path, 'x') as f:
            json.dump(value, f)
        os.chmod(path, 0o600)
        return path.read_bytes()

    def active(self, key):
        return self.state / 'native-control-pins-v4' / (key + '.json')

    def settled(self, key):
        return self.state / 'native-control-pins-settled-v1' / (key + '.json')

    def assert_archived(self, key, pin_raw, receipt_raw, phase):
        rec = json.loads(self.settled(key).read_bytes())
        self.assertEqual(rec['terminal_phase'], phase)
        self.assertEqual(base64.b64decode(rec['pin']['raw_base64']), pin_raw)
        self.assertEqual(base64.b64decode(rec['receipt']['raw_base64']), receipt_raw)
        self.assertIs(rec['replay_authorized'], False)
        self.assertIs(rec['submission_budget_created'], False)
        self.assertEqual(os.stat(self.settled(key)).st_nlink, 1)


class BothVariants(Fixture):
    """基线与候选都须成立（不变量 / 失败闭合）。"""

    def test_unreceipted_pin_blocks_and_survives_restart(self):
        key = self.freeze()
        raw = self.active(key).read_bytes()
        self.assertTrue(self.index.control_is_pending())
        restarted = self.new_index()
        self.assertTrue(restarted.control_is_pending())
        self.assertEqual(self.active(key).read_bytes(), raw)
        self.assertFalse(self.settled(key).exists())

    def test_foreign_or_malformed_receipt_keeps_pin_blocking(self):
        for bad in (dict(episode_key='f' * 24), dict(fields=dict(attemptId='e' * 24))):
            with self.subTest(bad=list(bad)):
                key = self.freeze()
                raw = self.active(key).read_bytes()
                self.receipt(key, 'not-interrupted', **bad)
                with self.assertRaises(ValueError):
                    self.index.control_is_pending()
                self.assertEqual(self.active(key).read_bytes(), raw)
                self.assertFalse(self.settled(key).exists())
                # 清掉本子例的坏件，避免影响下一子例（只删测试自建的回执）
                (self.journal / (key + '-not-interrupted.json')).unlink()
                self.active(key).unlink()


class RetirementRequired(Fixture):
    """基线 RED（pin 永不退役 / >1024 永久锁死），候选 GREEN。"""

    def test_each_exact_terminal_reason_retires_once(self):
        for phase in ('compact-started', 'not-interrupted', 'compact-denied'):
            with self.subTest(phase=phase):
                key = self.freeze()
                pin_raw = self.active(key).read_bytes()
                receipt_raw = self.receipt(key, phase)
                self.assertFalse(self.index.control_is_pending())
                self.assertFalse(self.active(key).exists(), 'terminal pin must retire')
                self.assert_archived(key, pin_raw, receipt_raw, phase)
                before = os.stat(self.settled(key))
                self.assertFalse(self.index.control_is_pending())
                after = os.stat(self.settled(key))
                self.assertEqual((before.st_ino, before.st_mtime_ns), (after.st_ino, after.st_mtime_ns))

    def test_more_than_1024_cumulative_settled_pins_do_not_freeze_evidence_writes(self):
        for _ in range(1030):
            key = self.freeze()
            self.receipt(key, 'not-interrupted')
            self.assertFalse(self.index.control_is_pending())
        self.index.state['entries']['probe'] = {}
        self.index.save()  # 证据写入不被锁死
        self.assertEqual(len(os.listdir(self.state / 'native-control-pins-v4')), 0)
        self.assertEqual(len(os.listdir(self.state / 'native-control-pins-settled-v1')), 1030)
        blocker = self.freeze()
        self.assertTrue(self.index.control_is_pending())
        self.assertTrue(self.active(blocker).exists())

    def test_legacy_directory_of_1025_terminal_active_pins_retires(self):
        # 基线遗留形态：已有终态回执但从未退役的 active pin 超过 1024
        keys = [self.freeze() for _ in range(1025)]
        for key in keys:
            self.receipt(key, 'compact-denied')
        self.assertFalse(self.index.control_is_pending())
        self.assertEqual(len(os.listdir(self.state / 'native-control-pins-v4')), 0)

    def test_settled_key_cannot_be_frozen_again(self):
        key = self.freeze()
        self.receipt(key, 'not-interrupted')
        self.assertFalse(self.index.control_is_pending())
        with self.assertRaisesRegex(ValueError, 'already settled'):
            self.index.freeze_for_control(self.request(key))
        self.assertFalse(self.active(key).exists())


class Crash(Exception):
    pass


@unittest.skipUnless(CANDIDATE, 'crash injection points exist only in the candidate module')
class CandidateCrashAndReplacement(Fixture):
    def crash_at(self, point, action=None):
        def hook(name):
            if name == point:
                if action:
                    action()
                raise Crash(name)
        pr._crash = hook

    def test_crash_at_every_step_preserves_pin_then_recovers_idempotently(self):
        for point in ('after_temp', 'after_link', 'after_archive', 'before_unlink'):
            with self.subTest(point=point):
                key = self.freeze()
                pin_raw = self.active(key).read_bytes()
                receipt_raw = self.receipt(key, 'compact-started')
                self.crash_at(point)
                with self.assertRaises(Crash):
                    self.index.control_is_pending()
                self.assertEqual(self.active(key).read_bytes(), pin_raw, 'pin lost before retirement')
                if point == 'after_link':
                    self.assertEqual(os.stat(self.settled(key)).st_nlink, 2)
                pr._crash = lambda name: None
                restarted = self.new_index()
                self.assertFalse(restarted.control_is_pending())
                self.assertFalse(self.active(key).exists())
                self.assert_archived(key, pin_raw, receipt_raw, 'compact-started')
                leftovers = [e for e in os.listdir(self.settled(key).parent)
                             if e.startswith('.' + key + '.json.publish-')]
                # after_temp 只留下惰性的完整临时件；其余步骤不留第二链接
                self.assertEqual(len(leftovers), 1 if point == 'after_temp' else 0, leftovers)

    def test_torn_temp_from_crash_mid_write_does_not_block(self):
        key = self.freeze()
        receipt_raw = self.receipt(key, 'not-interrupted')
        settled_dir = self.state / 'native-control-pins-settled-v1'
        settled_dir.mkdir(mode=0o700)
        torn = settled_dir / ('.' + key + '.json.publish-' + 'a' * 24 + '.tmp')
        torn.write_bytes(b'{"schema":"ccc-nat')
        os.chmod(torn, 0o600)
        self.assertFalse(self.index.control_is_pending())
        self.assertEqual(torn.read_bytes(), b'{"schema":"ccc-nat')
        self.assertEqual(json.loads(self.settled(key).read_bytes())['receipt']['sha256'],
                         __import__('hashlib').sha256(receipt_raw).hexdigest())

    def test_changed_receipt_after_archive_keeps_pin_blocking(self):
        key = self.freeze()
        pin_raw = self.active(key).read_bytes()
        path = self.journal / (key + '-not-interrupted.json')
        self.receipt(key, 'not-interrupted')
        self.crash_at('after_archive')
        with self.assertRaises(Crash):
            self.index.control_is_pending()
        archived = self.settled(key).read_bytes()
        pr._crash = lambda name: None
        # 同一终态但字节不同（仍是合法字段）→ 与已归档原件不符，不得释放
        value = json.loads(path.read_bytes())
        value['extra'] = 1
        os.chmod(path, 0o600)
        path.unlink()
        with open(path, 'x') as f:
            json.dump(value, f)
        os.chmod(path, 0o600)
        with self.assertRaisesRegex(ValueError, 'differs from the original evidence'):
            self.index.control_is_pending()
        self.assertEqual(self.active(key).read_bytes(), pin_raw)
        self.assertEqual(self.settled(key).read_bytes(), archived)

    def test_untrusted_archive_name_keeps_pin_blocking(self):
        for kind in ('foreign_bytes', 'symlink', 'second_link'):
            with self.subTest(kind=kind):
                key = self.freeze()
                pin_raw = self.active(key).read_bytes()
                self.receipt(key, 'compact-denied')
                d = self.state / 'native-control-pins-settled-v1'
                d.mkdir(mode=0o700, exist_ok=True)
                target = self.settled(key)
                if kind == 'foreign_bytes':
                    target.write_bytes(b'{"schema":"other"}\n')
                    os.chmod(target, 0o600)
                elif kind == 'symlink':
                    target.symlink_to(self.active(key))
                else:
                    target.write_bytes(b'{}\n')
                    os.chmod(target, 0o600)
                    os.link(target, d / ('other-' + key))
                with self.assertRaises(ValueError):
                    self.index.control_is_pending()
                self.assertEqual(self.active(key).read_bytes(), pin_raw)
                # 清理本子例：移走 active 以免阻断下一子例（测试自建件）
                self.active(key).unlink()

    def test_replacement_after_archive_crash_same_bytes_retires_different_bytes_blocks(self):
        for same in (True, False):
            with self.subTest(same_bytes=same):
                key = self.freeze()
                self.receipt(key, 'not-interrupted')
                active = self.active(key)

                def replace():
                    # 同 uid 写者在归档后、退役前替换 active pin（新 inode）
                    value = json.loads(active.read_bytes())
                    raw = active.read_bytes()
                    if not same:
                        value['note'] = 'foreign'
                        raw = json.dumps(value).encode()
                    tmp = active.with_name('.x.tmp')
                    tmp.write_bytes(raw)
                    os.chmod(tmp, 0o600)
                    os.rename(tmp, active)
                self.crash_at('after_archive', action=replace)
                with self.assertRaises(Crash):
                    self.index.control_is_pending()
                pr._crash = lambda name: None
                archived = self.settled(key).read_bytes()
                replaced = active.read_bytes()
                if same:
                    # 同字节：归档记录重算一致，按原次幂等收尾
                    self.assertFalse(self.new_index().control_is_pending())
                    self.assertFalse(active.exists())
                else:
                    # 不同字节：与已归档原件不符，不得释放，也不覆盖归档
                    with self.assertRaisesRegex(ValueError, 'differs from the original evidence'):
                        self.new_index().control_is_pending()
                    self.assertEqual(active.read_bytes(), replaced)
                    self.assertEqual(self.settled(key).read_bytes(), archived)
                    active.unlink()  # 清理本子例（测试自建件）

    def test_inode_swap_between_check_and_retire_is_refused(self):
        key = self.freeze()
        self.receipt(key, 'not-interrupted')
        active = self.active(key)
        original = pr._strict_identity
        calls = []

        def swapping(directory, name):
            if name == key + '.json' and directory.path.name == 'native-control-pins-v4':
                calls.append(1)
                if len(calls) == 2:  # _retire_active 复核前一刻替换（同字节、不同 inode）
                    raw = active.read_bytes()
                    tmp = active.with_name('.y.tmp')
                    tmp.write_bytes(raw)
                    os.chmod(tmp, 0o600)
                    os.rename(tmp, active)
            return original(directory, name)
        pr._strict_identity = swapping
        self.addCleanup(setattr, pr, '_strict_identity', original)
        with self.assertRaisesRegex(ValueError, 'replaced before retirement'):
            self.index.control_is_pending()
        self.assertTrue(active.exists())

    def test_settled_history_is_not_read_when_nothing_is_active(self):
        key = self.freeze()
        self.receipt(key, 'not-interrupted')
        self.assertFalse(self.index.control_is_pending())
        os.chmod(self.state / 'native-control-pins-settled-v1', 0o000)
        self.assertFalse(self.index.control_is_pending())
        os.chmod(self.state / 'native-control-pins-settled-v1', 0o700)

    def test_baseline_written_pin_bytes_retire_under_candidate(self):
        spec = importlib.util.spec_from_file_location('baseline_intents', BASE / 'ccc_compact_intents.py')
        baseline = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(baseline)
        old = baseline.IntentIndex(self.state / 'native-input-intents.json',
                                   state_root=self.root / 'mac-state', active_root=self.root)
        key = self.freeze(index=old)
        pin_raw = self.active(key).read_bytes()
        receipt_raw = self.receipt(key, 'compact-started')
        self.assertFalse(self.index.control_is_pending())
        self.assert_archived(key, pin_raw, receipt_raw, 'compact-started')


if __name__ == '__main__':
    unittest.main()
