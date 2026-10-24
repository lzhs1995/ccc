"""将真实 helper shell/CLI 入口回归纳入统一离线套件。"""
import importlib.util
from pathlib import Path
import unittest

_path = Path(__file__).resolve().parents[1] / 'tests/test_cmux_agent_adapter.py'
_spec = importlib.util.spec_from_file_location('helper_entrypoint_cases', _path)
_cases = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_cases)


def load_tests(loader, tests, pattern):
    return loader.loadTestsFromTestCase(_cases.HelperEntrypointTests)


if __name__ == '__main__':
    unittest.main()
