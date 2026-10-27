"""Run semantic hook checks without aborting unittest discovery."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest

class SubmissionGuardCLITests(unittest.TestCase):
    def test_semantic_checks(self):
        # 真实 CLI 仍读取自己的持久状态；测试不得混入调用者的现场 journal。
        # 保留一次目标读屏和零输入断言，不因现场存在待核消息而放宽预算。
        with tempfile.TemporaryDirectory(prefix='submission-guard-cli-') as root:
            env = dict(os.environ, HOME=root)
            result = subprocess.run(
                [sys.executable, '-B', str(Path(__file__).with_name('check_cmux_submit_confirmation_guard.py'))],
                capture_output=True, text=True, timeout=60, env=env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('ALL SEMANTIC TESTS PASSED', result.stdout)
