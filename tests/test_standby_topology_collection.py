"""The pre-read collection window cannot reuse an in-flight snapshot."""
import unittest
from functools import partial
from unittest.mock import patch
import ccc_standby_prepare as prep
from tests.test_standby_prepare import FreshTopologyTests

class CollectedTopologyTests(FreshTopologyTests):
    def setUp(self):
        constructor = prep.FreshTopology
        self.patcher = patch.object(prep, 'FreshTopology', partial(constructor, collect=True))
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

if __name__ == '__main__':
    unittest.main()
