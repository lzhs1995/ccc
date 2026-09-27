"""Real service authorization expiry under blocked local storage; no native CLI."""
import asyncio
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

import ccc_access_service as service
import cmux_codex_watch as core
from tests.test_access_gateway import UpstreamFixture, native_request


@unittest.skipUnless(sys.platform == 'darwin' and sys.version_info >= (3, 11),
                     'real service identity and optional mode require Darwin / Python 3.11+')
class RealAuthorizationLeaseTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, stalled):
        with tempfile.TemporaryDirectory(prefix='ccc-authorization-') as temp:
            root = Path(temp).resolve()
            config_path = root / 'ccc/config.json'
            native = root / 'native'
            native.mkdir()
            fixture = UpstreamFixture()
            upstream_port = await fixture.start()
            (native / 'config.toml').write_text(
                'model="gpt-6-astra"\nmodel_provider="fixture"\n'
                '[model_providers.fixture]\nname="Loopback"\nwire_api="responses"\n'
                f'base_url="http://127.0.0.1:{upstream_port}/v1"\n')
            jobs = [{'id': str(uuid.uuid4()), 'workspace_id': str(uuid.uuid4())} for _ in range(2)]
            config = core.default_config()
            config.update(mode='armed', global_paused=False, workspace_rules=[{
                'workspace_id': job['workspace_id'], 'active_batch_id': job['id'],
                'enabled': True, 'paused': False} for job in jobs])
            core.atomic_write_json(config_path, config)
            service.root(config_path).mkdir(mode=0o700)
            waiting, release = threading.Event(), threading.Event()
            real_load, real_write = core.ConfigStore.load, core.atomic_write_json
            calls = 0

            def load(store):
                nonlocal calls
                calls += 1
                if stalled == 'load' and calls == 2:
                    waiting.set()
                    if not release.wait(8):
                        raise RuntimeError('test did not release stalled configuration read')
                return real_load(store)

            def write(path, value):
                if stalled == 'publish' and Path(path).name == 'access-status.json' and not release.is_set():
                    waiting.set()
                    if not release.wait(8):
                        raise RuntimeError('test did not release stalled status fsync')
                return real_write(path, value)

            old_signals = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
            task = None
            try:
                with patch.object(core.ConfigStore, 'load', load), patch.object(core, 'atomic_write_json', write), \
                        patch.dict(os.environ, {'CODEX_HOME': str(native), 'HTTPS_PROXY': '', 'https_proxy': ''}):
                    task = asyncio.create_task(service.serve(config_path))
                    deadline = time.monotonic() + 5
                    owner_path = service.root(config_path) / 'owner.json'
                    while not owner_path.exists():
                        self.assertLess(time.monotonic(), deadline)
                        if task.done():
                            await task
                        await asyncio.sleep(.01)
                    owner = service.read_private(owner_path)
                    channels = []
                    for job in jobs:
                        job['access_policy'] = service.prepare(config_path, job, fixture=True, owner=owner)
                        directory = service.job_root(config_path, job['id'])
                        descriptor = service.read_private(directory / 'access.json')
                        real_write(directory / 'job.json', job)
                        sessions = {index: str(uuid.uuid4()) for index in range(50)}
                        for index, session in sessions.items():
                            service.create_private(directory / f'access-session-{index}.json', {
                                'job_id': job['id'], 'workspace_id': job['workspace_id'],
                                'index': index, 'session_id': session})
                        channels.append(SimpleNamespace(budget=SimpleNamespace(policy=service.Policy(
                            job['workspace_id'], job['id'])), tokens=descriptor['tokens'], sessions=sessions))
                    if stalled == 'publish':
                        # Load a channel without reserving an attempt. Publishing
                        # its first status then blocks the service's next reload.
                        self.assertEqual((await native_request(owner['port'], channels[0], 0,
                            session=str(uuid.uuid4())))[0], 409)
                    while not waiting.is_set():
                        self.assertLess(time.monotonic(), deadline)
                        await asyncio.sleep(.01)
                    config['workspace_rules'][0]['paused'] = True
                    real_write(config_path, config)
                    await asyncio.sleep(service.AUTHORIZATION_LEASE + .15)
                    rejected = await asyncio.wait_for(asyncio.gather(*[
                        native_request(owner['port'], channels[0], index) for index in range(50)]), 3)
                    self.assertEqual([status for status, _ in rejected], [409] * 50)
                    self.assertEqual(fixture.requests, [])
                    journal = service.job_root(config_path, jobs[0]['id']) / 'access-journal.jsonl'
                    self.assertEqual([json.loads(line)['kind'] for line in journal.read_text().splitlines()], ['policy'])
                    # A blocked status writer must not starve the independent
                    # policy reader or an unrelated authorized workspace.
                    if stalled == 'load':
                        release.set()
                        await asyncio.sleep(.4)
                    else:
                        self.assertFalse(release.is_set())
                        self.assertGreaterEqual(calls, 3)
                    self.assertEqual((await native_request(owner['port'], channels[0], 0))[0], 409)
                    healthy = await asyncio.wait_for(asyncio.gather(*[
                        native_request(owner['port'], channels[1], index) for index in range(50)]), 5)
                    self.assertEqual([status for status, _ in healthy], [200] * 50)
                    self.assertEqual(fixture.peak, 50)
                    self.assertEqual(len(fixture.requests), 50)
            finally:
                release.set()
                if task:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                await fixture.close()
                loop = asyncio.get_running_loop()
                for sig, handler in old_signals.items():
                    loop.remove_signal_handler(sig)
                    signal.signal(sig, handler)

    async def test_stalled_configuration_reload_expires_old_authorization(self):
        await self.exercise('load')

    async def test_stalled_status_fsync_cannot_starve_manual_pause_or_other_jobs(self):
        await self.exercise('publish')


if __name__ == '__main__':
    unittest.main()
