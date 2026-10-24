import json
from pathlib import Path
import plistlib
import tempfile
import unittest

import configuration_guardian as guardian
import executor_reask_scheduler as scheduler
import release_jobs


class ReleaseJobsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.manifest = self.home / '.local/state/multi-agent-collaboration/CURRENT.json'
        self.manifest.parent.mkdir(parents=True)
        self.source = self.home / 'release/source'

    def configure(self):
        self.manifest.write_text(json.dumps(dict(source=str(self.source), python='/python')))
        paths = []
        for module in (guardian, scheduler):
            path = self.home / 'Library/LaunchAgents' / (module.LABEL + '.plist')
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(plistlib.dumps(module.launchd_spec(self.home, self.source, '/python', self.manifest)))
            paths.append(path)
        return paths

    def test_both_current_jobs_pass_without_claiming_execution(self):
        self.configure()
        result = release_jobs.check(self.home)
        self.assertTrue(result['ok'])
        self.assertFalse(result['execution_proven'])

    def test_old_scheduler_is_detected_with_current_guardian(self):
        _, path = self.configure()
        spec = plistlib.loads(path.read_bytes())
        spec['ProgramArguments'][2] = '/previous/source/scripts/executor_reask_scheduler.py'
        path.write_bytes(plistlib.dumps(spec))
        self.assertFalse(release_jobs.check(self.home)['ok'])

    def test_missing_and_symlink_job_fail_closed(self):
        _, path = self.configure()
        original = path.with_suffix('.old')
        path.rename(original)
        self.assertFalse(release_jobs.check(self.home)['ok'])
        path.symlink_to(original)
        self.assertFalse(release_jobs.check(self.home)['ok'])

    def test_unmanaged_checkout_does_not_require_launchd(self):
        result = release_jobs.check(self.home)
        self.assertTrue(result['ok'])
        self.assertFalse(result['applicable'])


if __name__ == '__main__':
    unittest.main()
