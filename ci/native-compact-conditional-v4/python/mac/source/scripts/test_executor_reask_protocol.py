"""Small file fixtures only: never import a release member or touch live state."""
import copy
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import executor_reask_protocol as protocol


# Keep the oldest v2 fixture as the default even when newer pins are added.
REVIEWED = tuple(sorted(protocol.LEGACY))
REVIEWED_V2 = tuple(name for name in REVIEWED if name not in protocol.LEGACY_FILE_PROTOCOL)
REVIEWED_V3 = tuple(protocol.LEGACY_FILE_PROTOCOL)
PYTHON = str(Path(sys.executable).resolve())
OWNER = '11111111-1111-4111-8111-111111111111'


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def identity(source, pins):
    members = protocol.historical_files(source.parent.name) if source.parent.name in REVIEWED else protocol.FILES
    return dict(source=str(source), python=PYTHON,
                files={key: pins[key]['sha256'] for key in members})


def issuer(source, pins):
    return dict(path=str(source / protocol.SCHEDULER), python=PYTHON,
                sha256=pins[protocol.SCHEDULER]['sha256'], label=protocol.LABEL)


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.home = Path(self.stack.enter_context(tempfile.TemporaryDirectory(
            prefix='reask-protocol-'))).resolve()
        self.stack.enter_context(patch.object(Path, 'home', return_value=self.home))
        self.base = self.home / '.local/share/multi-agent-collaboration/releases'
        self.releases = {name: self.make_release(name) for name in REVIEWED}
        legacy = {name: {key: pins[key]['sha256'] for key in (*protocol.historical_files(name), protocol.SCHEDULER)}
                  for name, (_source, pins, _raw) in self.releases.items()}
        manifests = {name: digest(raw) for name, (_source, _pins, raw) in self.releases.items()}
        self.stack.enter_context(patch.dict(protocol.LEGACY, legacy, clear=True))
        self.stack.enter_context(patch.dict(protocol.LEGACY_MANIFESTS, manifests, clear=True))
        self.current = self.make_release('0.4.99-current-fixture', current=True,
                                         source=self.home / 'writable-checkout')
        self.stack.enter_context(patch.object(protocol, '__file__', str(
            self.current[0] / 'scripts/executor_reask_protocol.py')))
        for member in self.current[1]:
            (self.current[0] / member).chmod(0o644)
        self.current_scheduler = issuer(*self.current[:2])

    def write_pin(self, path, raw, executable=False):
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            path.chmod(0o644)
        path.write_bytes(raw)
        path.chmod(0o555 if executable else 0o444)

    def make_release(self, name, *, current=False, source=None):
        source = source or self.base / name / 'source'
        members = protocol.FILES if current else protocol.historical_files(name)
        names = (*members, protocol.SCHEDULER, 'scripts/configuration_guardian.py',
                 'scripts/manage_install.py', 'README.md', 'bin/helper')
        pins = {}
        for member in names:
            # Valid manifest members, deliberately unsafe to execute/import.
            raw = ("raise AssertionError('release code executed')\n# " + name + '/' + member).encode()
            executable = member == 'bin/helper'
            self.write_pin(source / member, raw, executable)
            pins[member] = dict(bytes=len(raw), sha256=digest(raw), executable=executable)
        raw = json.dumps(dict(files=pins), sort_keys=True).encode()
        self.write_pin(source.parent / 'MANIFEST.json', raw)
        return source, pins, raw

    def episode(self, name=None, *, current=False):
        source, pins, _raw = self.current if current else self.releases[name or REVIEWED[0]]
        modern = current or name in protocol.LEGACY_FILE_PROTOCOL
        controller = identity(source, pins)
        continuation = dict(enabled=True, id='a' * 32, status='ACTIVE', overall_task='fixture task',
                            owner=OWNER, authorized_epoch=1.0, until_epoch=None,
                            authorization=dict(path=str(self.home / 'authorization.txt'),
                                               sha256='b' * 64, bytes=10),
                            controller=copy.deepcopy(controller))
        state = dict(version=3 if modern else 2, supervisor=OWNER,
                     controller=controller, continuation=continuation)
        record = dict(schema='executor-reask-enrollment-v2' if modern else 'executor-reask-enrollment-v1',
                      authorization='EXPLICIT_REASK_START', controller=copy.deepcopy(controller),
                      scheduler=issuer(source, pins))
        if modern:
            state['file_protocol'] = protocol.descriptor()
            record['file_protocol'] = protocol.descriptor()
        return state, record

    def assert_supported(self, state, record):
        self.assertEqual(protocol.state_controller(state, record), record['controller'])
        self.assertIs(protocol.supports_state(state, record), True)
        self.assertIs(protocol.supports_record(record, state, self.current_scheduler), True)

    def assert_rejected(self, state, record):
        self.assertIs(protocol.supports_state(state, record), False)
        self.assertIs(protocol.supports_record(record, state, self.current_scheduler), False)

    def test_reservation_inquiry_capability_keeps_reviewed_historical_authority(self):
        current, record = self.episode(current=True)
        current['continuation']['reservation_inquiries'] = True
        self.assert_supported(current, record)
        for name in REVIEWED:
            for value in (False, True):
                with self.subTest(release=name, value=value):
                    state, record = self.episode(name)
                    self.assert_supported(state, record)
                    state['continuation']['reservation_inquiries'] = value
                    if protocol.LEGACY_CAPABILITIES.get(name, {}).get('reservation_inquiries'):
                        self.assert_supported(state, record)
                    else:
                        self.assert_rejected(state, record)

    def test_reservation_inquiry_capability_requires_a_boolean(self):
        for name in (None, *protocol.LEGACY_CAPABILITIES):
            for value in (None, 1, 'true', [], {}):
                with self.subTest(release=name, value=value):
                    state, record = self.episode(name, current=name is None)
                    state['continuation']['reservation_inquiries'] = value
                    self.assert_rejected(state, record)

    def test_proactive_capability_is_boolean_and_preserves_only_original_grants(self):
        for name in (None, *REVIEWED):
            for flag in (True, False, 'true', 1):
                with self.subTest(release=name, flag=flag):
                    state, record = self.episode(name, current=name is None)
                    state['continuation'].update(reservation_inquiries=True, proactive_hook=flag)
                    if (name is None or protocol.LEGACY_CAPABILITIES.get(name, {}).get('proactive_hook')) and type(flag) is bool:
                        self.assert_supported(state, record)
                    else:
                        self.assert_rejected(state, record)

    def test_0456_proactive_contract_survives_upgrade_without_reenrollment(self):
        name = '0.4.56-bea19f7ff44e'
        state, record = self.episode(name)
        state['continuation'].update(reservation_inquiries=True, proactive_hook=True)
        before = copy.deepcopy((state, record))
        self.assert_supported(state, record)
        self.assertEqual((state, record), before)
        for member in ('scripts/executor_proactive_hook.py', 'scripts/native_user_record.py'):
            with self.subTest(member=member):
                bad_state, bad_record = copy.deepcopy(before)
                for value in (bad_state['controller'], bad_state['continuation']['controller'], bad_record['controller']):
                    value['files'].pop(member)
                self.assert_rejected(bad_state, bad_record)

    def test_0457_issuer_keeps_exact_original_scope(self):
        name = '0.4.57-e8685485a9c5'
        allowed = set(protocol.LEGACY_FILE_PROTOCOL[name]['controllers'])
        self.assertEqual(allowed, set(REVIEWED))
        self.assertNotIn(name, protocol.LEGACY_FILE_PROTOCOL['0.4.56-bea19f7ff44e']['controllers'])
        state, record = self.episode(name)
        state['continuation'].update(reservation_inquiries=True, proactive_hook=True)
        before = copy.deepcopy((state, record))
        self.assert_supported(state, record)
        self.assertEqual((state, record), before)
        for member in protocol.historical_files(name):
            with self.subTest(member=member):
                bad_state, bad_record = copy.deepcopy(before)
                for value in (bad_state['controller'], bad_state['continuation']['controller'], bad_record['controller']):
                    value['files'].pop(member)
                self.assert_rejected(bad_state, bad_record)

    def test_0456_original_registration_remains_active_in_new_stop_hook(self):
        import executor_proactive_hook as hook
        import executor_reask as reask
        from unittest.mock import patch
        for name, expected in (('0.4.57-e8685485a9c5', True), ('0.4.56-bea19f7ff44e', True), ('0.4.55-8064e2645b0e', False)):
            with self.subTest(name=name):
                state, record = self.episode(name)
                state.update(caller_surface_uuid=OWNER, state=reask.WAITING)
                state['continuation'].update(reservation_inquiries=True, proactive_hook=True)
                before = copy.deepcopy(state)
                with patch.object(hook.lifecycle, 'poll_authorization'), \
                     patch.object(hook.scheduler, 'registration_observation', return_value='REGISTERED'):
                    self.assertEqual(hook.active(state, 100.), expected)
                self.assertEqual(state, before)
                if expected:
                    message = hook.instruction(state)
                    self.assertIn(str(self.releases[name][0] / 'scripts/executor_reask.py'), message)
                    self.assertNotIn(str(self.current[0]), message)
                    state['controller']['files']['scripts/executor_proactive_hook.py'] = '0' * 64
                    with patch.object(hook.lifecycle, 'poll_authorization'), \
                         patch.object(hook.scheduler, 'registration_observation', return_value='REGISTERED'):
                        self.assertFalse(hook.active(state, 100.))

    def test_coordination_path_pin_keeps_each_original_controller_contract(self):
        member = 'scripts/cmux_coordination_paths.py'
        current, record = self.episode(current=True)
        self.assert_supported(current, record)
        for value in (current['controller'], current['continuation']['controller'], record['controller']):
            value['files'].pop(member)
        self.assert_rejected(current, record)
        for name in protocol.LEGACY_CAPABILITIES:
            with self.subTest(release=name):
                state, record = self.episode(name)
                original = copy.deepcopy((state, record))
                self.assert_supported(state, record)
                self.assertEqual((state, record), original)
                for value in (state['controller'], state['continuation']['controller'], record['controller']):
                    if member in protocol.historical_files(name):
                        value['files'].pop(member)
                    else:
                        value['files'][member] = self.current[1][member]['sha256']
                self.assert_rejected(state, record)

    def test_reviewed_0437_issuer_cannot_grant_reverse_or_future_authority(self):
        release37 = '0.4.37-c7258c383218'
        for older in REVIEWED_V3:
            if older == release37 or older not in protocol.LEGACY_FILE_PROTOCOL[release37]['controllers']:
                continue
            for origin, scheduler, expected in ((older, release37, True),
                                                 (release37, older, False)):
                with self.subTest(controller=origin, issuer=scheduler):
                    state, record = self.episode(origin)
                    original = copy.deepcopy(state)
                    record.update(scheduler=issuer(*self.releases[scheduler][:2]),
                                  authorization='EXPLICIT_REASK_ENROLL_EXISTING')
                    self.assertEqual(protocol.supports_record(record, state, self.current_scheduler), expected)
                    self.assertEqual(state, original)
        state, record = self.episode(current=True)
        record.update(scheduler=issuer(*self.releases[release37][:2]),
                      authorization='EXPLICIT_REASK_ENROLL_EXISTING')
        self.assertFalse(protocol.supports_record(record, state, self.current_scheduler))

    def test_reviewed_0441_issuer_keeps_original_and_rejects_reverse_or_future_authority(self):
        release41 = '0.4.41-28b36490b149'
        release37 = '0.4.37-c7258c383218'
        for origin, scheduler, expected in (
                ('0.4.28-ae66245771d0', release41, True),
                (release37, release41, True),
                (release41, release37, False)):
            with self.subTest(controller=origin, issuer=scheduler):
                state, record = self.episode(origin)
                original = copy.deepcopy(state)
                record.update(schema='executor-reask-enrollment-v2', file_protocol=protocol.descriptor(),
                              scheduler=issuer(*self.releases[scheduler][:2]),
                              authorization='EXPLICIT_REASK_ENROLL_EXISTING')
                self.assertEqual(protocol.supports_record(record, state, self.current_scheduler), expected)
                self.assertEqual(state, original)
        state, record = self.episode(current=True)
        record.update(scheduler=issuer(*self.releases[release41][:2]),
                      authorization='EXPLICIT_REASK_ENROLL_EXISTING')
        self.assertFalse(protocol.supports_record(record, state, self.current_scheduler))

    def test_reviewed_0444_issuer_preserves_original_authority(self):
        release44 = '0.4.44-2b9bce3b41eb'
        # This is the issuer's original reviewed scope, not today's registry.
        allowed = {
            '0.4.28-ae66245771d0', '0.4.30-ff75100795a9',
            '0.4.31-79ae83e9d5da', '0.4.32-c7338a400bf4',
            '0.4.33-09de16ceb823', '0.4.34-d333557fb498',
            '0.4.35-e62f35828c7a', '0.4.36-8ab59dbb5a5a',
            '0.4.37-c7258c383218', '0.4.41-28b36490b149',
            '0.4.42-5947b2d1f8aa', release44,
        }
        self.assertNotIn('0.4.46-37e945dea2e3', allowed)
        for origin in REVIEWED:
            with self.subTest(controller=origin, issuer=release44):
                state, record = self.episode(origin)
                record.update(schema='executor-reask-enrollment-v2', file_protocol=protocol.descriptor(),
                              scheduler=issuer(*self.releases[release44][:2]),
                              authorization='EXPLICIT_REASK_ENROLL_EXISTING')
                original = copy.deepcopy((state, record))
                self.assertEqual(protocol.supports_record(record, state, self.current_scheduler),
                                 origin in allowed)
                self.assertEqual((state, record), original)
        for older in ('0.4.42-5947b2d1f8aa', '0.4.37-c7258c383218'):
            state, record = self.episode(release44)
            record.update(scheduler=issuer(*self.releases[older][:2]),
                          authorization='EXPLICIT_REASK_ENROLL_EXISTING')
            self.assertFalse(protocol.supports_record(record, state, self.current_scheduler))
        state, record = self.episode(current=True)
        record.update(scheduler=issuer(*self.releases[release44][:2]),
                      authorization='EXPLICIT_REASK_ENROLL_EXISTING')
        self.assertFalse(protocol.supports_record(record, state, self.current_scheduler))
        unsupported = '0.4.43-unreviewed'
        self.releases[unsupported] = self.make_release(unsupported, current=True)
        state, record = self.episode(unsupported)
        record.update(schema='executor-reask-enrollment-v2', file_protocol=protocol.descriptor(),
                      scheduler=issuer(*self.releases[release44][:2]),
                      authorization='EXPLICIT_REASK_ENROLL_EXISTING')
        self.assertFalse(protocol.supports_record(record, state, self.current_scheduler))

    def test_reviewed_0446_issuer_preserves_original_authority(self):
        release46 = '0.4.46-37e945dea2e3'
        allowed = {
            '0.4.28-ae66245771d0', '0.4.30-ff75100795a9',
            '0.4.31-79ae83e9d5da', '0.4.32-c7338a400bf4',
            '0.4.33-09de16ceb823', '0.4.34-d333557fb498',
            '0.4.35-e62f35828c7a', '0.4.36-8ab59dbb5a5a',
            '0.4.37-c7258c383218', '0.4.41-28b36490b149',
            '0.4.42-5947b2d1f8aa', '0.4.44-2b9bce3b41eb', release46,
        }
        self.assertEqual(set(protocol.LEGACY_FILE_PROTOCOL[release46]['controllers']), allowed)
        for origin in REVIEWED:
            with self.subTest(controller=origin, issuer=release46):
                state, record = self.episode(origin)
                record.update(schema='executor-reask-enrollment-v2', file_protocol=protocol.descriptor(),
                              scheduler=issuer(*self.releases[release46][:2]),
                              authorization='EXPLICIT_REASK_ENROLL_EXISTING')
                original = copy.deepcopy((state, record))
                self.assertTrue(protocol.supports_state(state, record))
                self.assertEqual(protocol.supports_record(record, state, self.current_scheduler),
                                 origin in allowed)
                self.assertEqual((state, record), original)

    def test_reviewed_0446_rejects_reverse_future_and_unreviewed_authority(self):
        release46 = '0.4.46-37e945dea2e3'
        # Older issuers never acquire the newly reviewed controller's authority.
        for older in REVIEWED_V3:
            if (older == release46 or
                    older not in protocol.LEGACY_FILE_PROTOCOL[release46]['controllers']):
                continue
            with self.subTest(older_issuer=older):
                state, record = self.episode(release46)
                record.update(scheduler=issuer(*self.releases[older][:2]),
                              authorization='EXPLICIT_REASK_ENROLL_EXISTING')
                original = copy.deepcopy((state, record))
                self.assertFalse(protocol.supports_record(record, state, self.current_scheduler))
                self.assertEqual((state, record), original)
        # Reviewed 0.4.46 cannot enroll a future CURRENT or an unreviewed release.
        for origin in (None, '0.4.43-unreviewed', '0.4.45-unreviewed'):
            with self.subTest(controller=origin):
                if origin:
                    self.releases[origin] = self.make_release(origin, current=True)
                state, record = self.episode(origin, current=origin is None)
                record.update(schema='executor-reask-enrollment-v2', file_protocol=protocol.descriptor(),
                              scheduler=issuer(*self.releases[release46][:2]),
                              authorization='EXPLICIT_REASK_ENROLL_EXISTING')
                original = copy.deepcopy((state, record))
                self.assertFalse(protocol.supports_record(record, state, self.current_scheduler))
                self.assertEqual((state, record), original)

    def test_reviewed_0447_issuer_preserves_original_authority(self):
        release47 = '0.4.47-46395e3b21e5'
        # Fixed from the complete released 0.4.47 source, never today's registry.
        allowed = {
            '0.4.28-ae66245771d0', '0.4.30-ff75100795a9',
            '0.4.31-79ae83e9d5da', '0.4.32-c7338a400bf4',
            '0.4.33-09de16ceb823', '0.4.34-d333557fb498',
            '0.4.35-e62f35828c7a', '0.4.36-8ab59dbb5a5a',
            '0.4.37-c7258c383218', '0.4.41-28b36490b149',
            '0.4.42-5947b2d1f8aa', '0.4.44-2b9bce3b41eb',
            '0.4.46-37e945dea2e3', release47,
        }
        self.assertEqual(set(protocol.LEGACY_FILE_PROTOCOL[release47]['controllers']), allowed)
        for origin in REVIEWED:
            with self.subTest(controller=origin, issuer=release47):
                state, record = self.episode(origin)
                record.update(schema='executor-reask-enrollment-v2', file_protocol=protocol.descriptor(),
                              scheduler=issuer(*self.releases[release47][:2]),
                              authorization='EXPLICIT_REASK_ENROLL_EXISTING')
                original = copy.deepcopy((state, record))
                self.assertEqual(protocol.supports_record(record, state, self.current_scheduler),
                                 origin in allowed)
                self.assertEqual((state, record), original)
        for older in set(REVIEWED_V3) & (allowed - {release47}):
            with self.subTest(older_issuer=older):
                state, record = self.episode(release47)
                record.update(scheduler=issuer(*self.releases[older][:2]),
                              authorization='EXPLICIT_REASK_ENROLL_EXISTING')
                original = copy.deepcopy((state, record))
                self.assertFalse(protocol.supports_record(record, state, self.current_scheduler))
                self.assertEqual((state, record), original)
        for origin in (None, '0.4.43-unreviewed', '0.4.45-unreviewed'):
            with self.subTest(future_or_unreviewed_controller=origin):
                if origin:
                    self.releases[origin] = self.make_release(origin, current=True)
                state, record = self.episode(origin, current=origin is None)
                record.update(schema='executor-reask-enrollment-v2', file_protocol=protocol.descriptor(),
                              scheduler=issuer(*self.releases[release47][:2]),
                              authorization='EXPLICIT_REASK_ENROLL_EXISTING')
                original = copy.deepcopy((state, record))
                self.assertFalse(protocol.supports_record(record, state, self.current_scheduler))
                self.assertEqual((state, record), original)

    def test_reviewed_0448_keeps_original_scope_without_reverse_or_future_grants(self):
        release48 = '0.4.48-3b4304d24f90'
        original_controllers = (
            '0.4.28-ae66245771d0', '0.4.30-ff75100795a9', '0.4.31-79ae83e9d5da',
            '0.4.32-c7338a400bf4', '0.4.33-09de16ceb823', '0.4.34-d333557fb498',
            '0.4.35-e62f35828c7a', '0.4.36-8ab59dbb5a5a', '0.4.37-c7258c383218',
            '0.4.41-28b36490b149', '0.4.42-5947b2d1f8aa', '0.4.44-2b9bce3b41eb',
            '0.4.46-37e945dea2e3', '0.4.47-46395e3b21e5', release48,
        )
        for origin in REVIEWED:
            with self.subTest(controller=origin, issuer=release48):
                state, record = self.episode(origin)
                record.update(schema='executor-reask-enrollment-v2',
                              file_protocol=protocol.descriptor(),
                              scheduler=issuer(*self.releases[release48][:2]),
                              authorization='EXPLICIT_REASK_ENROLL_EXISTING')
                original = copy.deepcopy((state, record))
                self.assertEqual(protocol.supports_record(record, state, self.current_scheduler),
                                 origin in original_controllers)
                self.assertEqual((state, record), original)
        for older in original_controllers:
            if older == release48:
                continue
            with self.subTest(controller=release48, issuer=older):
                state, record = self.episode(release48)
                record.update(scheduler=issuer(*self.releases[older][:2]),
                              authorization='EXPLICIT_REASK_ENROLL_EXISTING')
                original = copy.deepcopy((state, record))
                self.assertFalse(protocol.supports_record(record, state, self.current_scheduler))
                self.assertEqual((state, record), original)
        state, record = self.episode(current=True)
        record.update(scheduler=issuer(*self.releases[release48][:2]),
                      authorization='EXPLICIT_REASK_ENROLL_EXISTING')
        self.assertFalse(protocol.supports_record(record, state, self.current_scheduler))

    def test_reviewed_0449_keeps_original_scope_without_reverse_or_future_grants(self):
        release49 = '0.4.49-689dda6455be'
        # Fixed from the statically inspected, fully verified 0.4.49 release.
        original_controllers = {
            '0.4.28-ae66245771d0', '0.4.30-ff75100795a9', '0.4.31-79ae83e9d5da',
            '0.4.32-c7338a400bf4', '0.4.33-09de16ceb823', '0.4.34-d333557fb498',
            '0.4.35-e62f35828c7a', '0.4.36-8ab59dbb5a5a', '0.4.37-c7258c383218',
            '0.4.41-28b36490b149', '0.4.42-5947b2d1f8aa', '0.4.44-2b9bce3b41eb',
            '0.4.46-37e945dea2e3', '0.4.47-46395e3b21e5',
            '0.4.48-3b4304d24f90', release49,
        }
        self.assertEqual(set(protocol.LEGACY_FILE_PROTOCOL[release49]['controllers']),
                         original_controllers)
        for origin in REVIEWED:
            with self.subTest(controller=origin, issuer=release49):
                state, record = self.episode(origin)
                record.update(schema='executor-reask-enrollment-v2',
                              file_protocol=protocol.descriptor(),
                              scheduler=issuer(*self.releases[release49][:2]),
                              authorization='EXPLICIT_REASK_ENROLL_EXISTING')
                original = copy.deepcopy((state, record))
                self.assertEqual(protocol.supports_record(record, state, self.current_scheduler),
                                 origin in original_controllers)
                self.assertEqual((state, record), original)
        for older in original_controllers - {release49}:
            with self.subTest(controller=release49, issuer=older):
                state, record = self.episode(release49)
                record.update(scheduler=issuer(*self.releases[older][:2]),
                              authorization='EXPLICIT_REASK_ENROLL_EXISTING')
                original = copy.deepcopy((state, record))
                self.assertFalse(protocol.supports_record(record, state, self.current_scheduler))
                self.assertEqual((state, record), original)
        state, record = self.episode(current=True)
        record.update(scheduler=issuer(*self.releases[release49][:2]),
                      authorization='EXPLICIT_REASK_ENROLL_EXISTING')
        original = copy.deepcopy((state, record))
        self.assertFalse(protocol.supports_record(record, state, self.current_scheduler))
        self.assertEqual((state, record), original)


    def test_reviewed_0450_keeps_original_scope_without_reverse_or_future_grants(self):
        release50 = '0.4.50-3dd2945b5069'
        # Frozen from all 306 release members verified against commit ba476465.
        original_controllers = {
            '0.4.28-ae66245771d0', '0.4.30-ff75100795a9', '0.4.31-79ae83e9d5da',
            '0.4.32-c7338a400bf4', '0.4.33-09de16ceb823', '0.4.34-d333557fb498',
            '0.4.35-e62f35828c7a', '0.4.36-8ab59dbb5a5a', '0.4.37-c7258c383218',
            '0.4.41-28b36490b149', '0.4.42-5947b2d1f8aa', '0.4.44-2b9bce3b41eb',
            '0.4.46-37e945dea2e3', '0.4.47-46395e3b21e5',
            '0.4.48-3b4304d24f90', '0.4.49-689dda6455be', release50,
        }
        self.assertEqual(set(protocol.LEGACY_FILE_PROTOCOL[release50]['controllers']),
                         original_controllers)
        for origin in REVIEWED:
            with self.subTest(controller=origin, issuer=release50):
                state, record = self.episode(origin)
                record.update(schema='executor-reask-enrollment-v2',
                              file_protocol=protocol.descriptor(),
                              scheduler=issuer(*self.releases[release50][:2]),
                              authorization='EXPLICIT_REASK_ENROLL_EXISTING')
                original = copy.deepcopy((state, record))
                self.assertEqual(protocol.supports_record(record, state, self.current_scheduler),
                                 origin in original_controllers)
                self.assertEqual((state, record), original)
        for older in original_controllers - {release50}:
            with self.subTest(controller=release50, issuer=older):
                state, record = self.episode(release50)
                record.update(scheduler=issuer(*self.releases[older][:2]),
                              authorization='EXPLICIT_REASK_ENROLL_EXISTING')
                original = copy.deepcopy((state, record))
                self.assertFalse(protocol.supports_record(record, state, self.current_scheduler))
                self.assertEqual((state, record), original)
        state, record = self.episode(current=True)
        record.update(scheduler=issuer(*self.releases[release50][:2]),
                      authorization='EXPLICIT_REASK_ENROLL_EXISTING')
        original = copy.deepcopy((state, record))
        self.assertFalse(protocol.supports_record(record, state, self.current_scheduler))
        self.assertEqual((state, record), original)


    def test_reviewed_0451_keeps_original_scope_without_reverse_or_future_grants(self):
        release51 = '0.4.51-587295817cef'
        # Frozen from all 307 release members verified against commit 744899c0.
        original_controllers = {
            '0.4.28-ae66245771d0', '0.4.30-ff75100795a9', '0.4.31-79ae83e9d5da',
            '0.4.32-c7338a400bf4', '0.4.33-09de16ceb823', '0.4.34-d333557fb498',
            '0.4.35-e62f35828c7a', '0.4.36-8ab59dbb5a5a', '0.4.37-c7258c383218',
            '0.4.41-28b36490b149', '0.4.42-5947b2d1f8aa', '0.4.44-2b9bce3b41eb',
            '0.4.46-37e945dea2e3', '0.4.47-46395e3b21e5',
            '0.4.48-3b4304d24f90', '0.4.49-689dda6455be', '0.4.50-3dd2945b5069', release51,
        }
        self.assertEqual(set(protocol.LEGACY_FILE_PROTOCOL[release51]['controllers']),
                         original_controllers)
        for origin in REVIEWED:
            with self.subTest(controller=origin, issuer=release51):
                state, record = self.episode(origin)
                record.update(schema='executor-reask-enrollment-v2',
                              file_protocol=protocol.descriptor(),
                              scheduler=issuer(*self.releases[release51][:2]),
                              authorization='EXPLICIT_REASK_ENROLL_EXISTING')
                original = copy.deepcopy((state, record))
                self.assertEqual(protocol.supports_record(record, state, self.current_scheduler),
                                 origin in original_controllers)
                self.assertEqual((state, record), original)
        for older in original_controllers - {release51}:
            with self.subTest(controller=release51, issuer=older):
                state, record = self.episode(release51)
                record.update(scheduler=issuer(*self.releases[older][:2]),
                              authorization='EXPLICIT_REASK_ENROLL_EXISTING')
                original = copy.deepcopy((state, record))
                self.assertFalse(protocol.supports_record(record, state, self.current_scheduler))
                self.assertEqual((state, record), original)
        state, record = self.episode(current=True)
        record.update(scheduler=issuer(*self.releases[release51][:2]),
                      authorization='EXPLICIT_REASK_ENROLL_EXISTING')
        original = copy.deepcopy((state, record))
        self.assertFalse(protocol.supports_record(record, state, self.current_scheduler))
        self.assertEqual((state, record), original)


    def test_reviewed_0452_keeps_original_scope_without_reverse_or_future_grants(self):
        release52 = '0.4.52-0ca4d8002da7'
        # Frozen from all 310 release members verified against commit d2fd11d1.
        original_controllers = {
            '0.4.28-ae66245771d0', '0.4.30-ff75100795a9', '0.4.31-79ae83e9d5da',
            '0.4.32-c7338a400bf4', '0.4.33-09de16ceb823', '0.4.34-d333557fb498',
            '0.4.35-e62f35828c7a', '0.4.36-8ab59dbb5a5a', '0.4.37-c7258c383218',
            '0.4.41-28b36490b149', '0.4.42-5947b2d1f8aa', '0.4.44-2b9bce3b41eb',
            '0.4.46-37e945dea2e3', '0.4.47-46395e3b21e5',
            '0.4.48-3b4304d24f90', '0.4.49-689dda6455be', '0.4.50-3dd2945b5069', '0.4.51-587295817cef', release52,
        }
        self.assertEqual(set(protocol.LEGACY_FILE_PROTOCOL[release52]['controllers']),
                         original_controllers)
        for origin in REVIEWED:
            with self.subTest(controller=origin, issuer=release52):
                state, record = self.episode(origin)
                record.update(schema='executor-reask-enrollment-v2',
                              file_protocol=protocol.descriptor(),
                              scheduler=issuer(*self.releases[release52][:2]),
                              authorization='EXPLICIT_REASK_ENROLL_EXISTING')
                original = copy.deepcopy((state, record))
                self.assertEqual(protocol.supports_record(record, state, self.current_scheduler),
                                 origin in original_controllers)
                self.assertEqual((state, record), original)
        for older in original_controllers - {release52}:
            with self.subTest(controller=release52, issuer=older):
                state, record = self.episode(release52)
                record.update(scheduler=issuer(*self.releases[older][:2]),
                              authorization='EXPLICIT_REASK_ENROLL_EXISTING')
                original = copy.deepcopy((state, record))
                self.assertFalse(protocol.supports_record(record, state, self.current_scheduler))
                self.assertEqual((state, record), original)
        state, record = self.episode(current=True)
        record.update(scheduler=issuer(*self.releases[release52][:2]),
                      authorization='EXPLICIT_REASK_ENROLL_EXISTING')
        original = copy.deepcopy((state, record))
        self.assertFalse(protocol.supports_record(record, state, self.current_scheduler))
        self.assertEqual((state, record), original)


    def test_reviewed_0453_keeps_original_scope_without_reverse_or_future_grants(self):
        release53 = '0.4.53-568efc13c7ae'
        # Frozen from all 315 release members verified against commit e76744c3.
        original_controllers = {
            '0.4.28-ae66245771d0', '0.4.30-ff75100795a9', '0.4.31-79ae83e9d5da',
            '0.4.32-c7338a400bf4', '0.4.33-09de16ceb823', '0.4.34-d333557fb498',
            '0.4.35-e62f35828c7a', '0.4.36-8ab59dbb5a5a', '0.4.37-c7258c383218',
            '0.4.41-28b36490b149', '0.4.42-5947b2d1f8aa', '0.4.44-2b9bce3b41eb',
            '0.4.46-37e945dea2e3', '0.4.47-46395e3b21e5',
            '0.4.48-3b4304d24f90', '0.4.49-689dda6455be', '0.4.50-3dd2945b5069', '0.4.51-587295817cef', '0.4.52-0ca4d8002da7', release53,
        }
        self.assertEqual(set(protocol.LEGACY_FILE_PROTOCOL[release53]['controllers']),
                         original_controllers)
        for origin in REVIEWED:
            with self.subTest(controller=origin, issuer=release53):
                state, record = self.episode(origin)
                record.update(schema='executor-reask-enrollment-v2',
                              file_protocol=protocol.descriptor(),
                              scheduler=issuer(*self.releases[release53][:2]),
                              authorization='EXPLICIT_REASK_ENROLL_EXISTING')
                original = copy.deepcopy((state, record))
                self.assertEqual(protocol.supports_record(record, state, self.current_scheduler),
                                 origin in original_controllers)
                self.assertEqual((state, record), original)
        for older in original_controllers - {release53}:
            with self.subTest(controller=release53, issuer=older):
                state, record = self.episode(release53)
                record.update(scheduler=issuer(*self.releases[older][:2]),
                              authorization='EXPLICIT_REASK_ENROLL_EXISTING')
                original = copy.deepcopy((state, record))
                self.assertFalse(protocol.supports_record(record, state, self.current_scheduler))
                self.assertEqual((state, record), original)
        state, record = self.episode(current=True)
        record.update(scheduler=issuer(*self.releases[release53][:2]),
                      authorization='EXPLICIT_REASK_ENROLL_EXISTING')
        original = copy.deepcopy((state, record))
        self.assertFalse(protocol.supports_record(record, state, self.current_scheduler))
        self.assertEqual((state, record), original)


    def test_reviewed_0442_issuer_preserves_original_authority(self):
        release42 = '0.4.42-5947b2d1f8aa'
        release41 = '0.4.41-28b36490b149'
        release37 = '0.4.37-c7258c383218'
        for origin, scheduler, expected in (
                ('0.4.28-ae66245771d0', release42, True),
                (release37, release42, True),
                (release41, release42, True),
                (release42, release42, True),
                (release42, release41, False),
                (release42, release37, False)):
            with self.subTest(controller=origin, issuer=scheduler):
                state, record = self.episode(origin)
                original = copy.deepcopy(state)
                record.update(schema='executor-reask-enrollment-v2', file_protocol=protocol.descriptor(),
                              scheduler=issuer(*self.releases[scheduler][:2]),
                              authorization='EXPLICIT_REASK_ENROLL_EXISTING')
                self.assertEqual(protocol.supports_record(record, state, self.current_scheduler), expected)
                self.assertEqual(state, original)
        state, record = self.episode(current=True)
        record.update(scheduler=issuer(*self.releases[release42][:2]),
                      authorization='EXPLICIT_REASK_ENROLL_EXISTING')
        self.assertFalse(protocol.supports_record(record, state, self.current_scheduler))
        for unsupported in ('0.4.38-unreviewed', '0.4.40-unreviewed'):
            with self.subTest(unsupported=unsupported):
                self.releases[unsupported] = self.make_release(unsupported, current=True)
                state, record = self.episode(unsupported)
                original = copy.deepcopy(state)
                record.update(schema='executor-reask-enrollment-v2', file_protocol=protocol.descriptor(),
                              scheduler=issuer(*self.releases[release42][:2]),
                              authorization='EXPLICIT_REASK_ENROLL_EXISTING')
                self.assertFalse(protocol.supports_record(record, state, self.current_scheduler))
                self.assertEqual(state, original)

    def test_extended_controller_cannot_drop_identity_authority_pins(self):
        for name in protocol.LEGACY_CAPABILITIES:
            with self.subTest(release=name):
                state, record = self.episode(name)
                self.assert_supported(state, record)
                for value in (state['controller'], state['continuation']['controller'], record['controller']):
                    value['files'] = {key: value['files'][key] for key in protocol.LEGACY_FILES}
                self.assert_rejected(state, record)

    def test_each_extended_identity_member_is_checked(self):
        for name in protocol.LEGACY_CAPABILITIES:
            source, pins, _raw = self.releases[name]
            state, record = self.episode(name)
            for member in set(protocol.historical_files(name)) - set(protocol.LEGACY_FILES):
                with self.subTest(release=name, member=member):
                    raw = (source / member).read_bytes()
                    self.write_pin(source / member, b'X' + raw[1:])
                    self.assert_rejected(state, record)
                    self.write_pin(source / member, raw)
                    self.assert_supported(state, record)

    def test_reviewed_v2_originals_and_missing_top_level_controller(self):
        for name in REVIEWED_V2:
            with self.subTest(release=name):
                state, record = self.episode(name)
                self.assert_supported(state, record)
                del state['controller']
                self.assert_supported(state, record)
                self.assertTrue(protocol.supports_state(state))
                state['continuation'] = None
                self.assert_supported(state, record)
                self.assertFalse(protocol.supports_state(state))

    def test_reviewed_v3_original_and_current_reenrollment_keep_controller(self):
        for name in REVIEWED_V3:
            with self.subTest(release=name):
                state, record = self.episode(name)
                original = copy.deepcopy(state)
                self.assert_supported(state, record)
                record.update(scheduler=self.current_scheduler,
                              authorization='EXPLICIT_REASK_ENROLL_EXISTING')
                self.assert_supported(state, record)
                self.assertEqual(state, original)
                del state['controller']
                self.assert_rejected(state, record)

    def test_reviewed_v2_issuer_keeps_its_historical_controller_range(self):
        for name in REVIEWED_V3:
            for origin in (*REVIEWED_V2, name):
                with self.subTest(issuer=name, controller=origin):
                    state, record = self.episode(origin)
                    record.update(schema='executor-reask-enrollment-v2',
                                  file_protocol=protocol.descriptor(),
                                  scheduler=issuer(*self.releases[name][:2]),
                                  authorization='EXPLICIT_REASK_ENROLL_EXISTING')
                    self.assert_supported(state, record)
                    record['authorization'] = 'EXPLICIT_REASK_START'
                    self.assertIs(protocol.supports_record(record, state, self.current_scheduler),
                                  origin == name)
            state, record = self.episode(current=True)
            record.update(scheduler=issuer(*self.releases[name][:2]),
                          authorization='EXPLICIT_REASK_ENROLL_EXISTING')
            # The old issuer could not enroll the new CURRENT's controller.
            self.assertFalse(protocol.supports_record(record, state, self.current_scheduler))

    def test_reviewed_v3_requires_its_actual_schemas_and_both_descriptors(self):
        for name in REVIEWED_V3:
            for target, key, value in (
                    ('state', 'version', 2), ('state', 'file_protocol', {}),
                    ('record', 'file_protocol', {}),
                    ('record', 'schema', 'executor-reask-enrollment-v1'),
                    ('record', 'authorization', 'INFERRED_FROM_INSTALLATION')):
                with self.subTest(release=name, target=target, field=key):
                    state, record = self.episode(name)
                    (state if target == 'state' else record)[key] = value
                    self.assertFalse(protocol.supports_record(record, state, self.current_scheduler))
                    if target == 'state':
                        self.assertFalse(protocol.supports_state(state))
            state, record = self.episode(name)
            record['scheduler']['sha256'] = '0' * 64
            self.assertFalse(protocol.supports_record(record, state, self.current_scheduler))

    def test_reviewed_issuers_keep_older_controllers_without_reverse_authority(self):
        release32 = '0.4.32-c7338a400bf4'
        release33 = '0.4.33-09de16ceb823'
        release34 = '0.4.34-d333557fb498'
        release35 = '0.4.35-e62f35828c7a'
        release36 = '0.4.36-8ab59dbb5a5a'
        for origin, scheduler, supported in (
                (release32, release33, True), (release33, release32, False),
                (release32, release34, True), (release34, release32, False),
                (release33, release34, True), (release34, release33, False),
                (release34, release35, True), (release35, release34, False),
                (release32, release35, True), (release35, release32, False),
                (release33, release35, True), (release35, release33, False),
                (release35, release36, True), (release36, release35, False),
                (release34, release36, True), (release36, release34, False),
                (release32, release36, True), (release36, release32, False)):
            with self.subTest(controller=origin, issuer=scheduler):
                state, record = self.episode(origin)
                original = copy.deepcopy((state, record['controller']))
                record.update(scheduler=issuer(*self.releases[scheduler][:2]),
                              authorization='EXPLICIT_REASK_ENROLL_EXISTING')
                self.assertIs(protocol.supports_state(state, record), True)
                self.assertIs(protocol.supports_record(record, state, self.current_scheduler),
                              supported)
                self.assertEqual((state, record['controller']), original)
                record['authorization'] = 'EXPLICIT_REASK_START'
                self.assertIs(protocol.supports_record(record, state, self.current_scheduler), False)

    def test_reviewed_v3_rechecks_complete_manifest_without_cached_trust(self):
        for name in REVIEWED_V3:
            source, _pins, manifest = self.releases[name]
            state, record = self.episode(name)
            self.assert_supported(state, record)
            member = source / 'README.md'
            raw = member.read_bytes()
            self.write_pin(member, b'X' + raw[1:])
            self.assert_rejected(state, record)
            self.write_pin(member, raw)
            self.assert_supported(state, record)
            self.write_pin(source.parent / 'MANIFEST.json', manifest + b'\n')
            self.assert_rejected(state, record)
            self.write_pin(source.parent / 'MANIFEST.json', manifest)
            self.assert_supported(state, record)

    def test_verified_current_v3_and_explicit_original_reenrollment(self):
        self.assert_supported(*self.episode(current=True))
        state, record = self.episode()
        record.update(schema='executor-reask-enrollment-v2', file_protocol=protocol.descriptor(),
                      scheduler=self.current_scheduler, authorization='EXPLICIT_REASK_ENROLL_EXISTING')
        self.assert_supported(state, record)
        # Only explicit re-enrollment may retain a different original interpreter.
        old_python = str(self.home / 'old-python')
        for value in (state['controller'], state['continuation']['controller'], record['controller']):
            value['python'] = old_python
        self.assert_supported(state, record)
        record['authorization'] = 'EXPLICIT_REASK_START'
        self.assertFalse(protocol.supports_record(record, state, self.current_scheduler))

    def test_no_old_import_execution_or_input_and_no_data_mutation(self):
        state, record = self.episode()
        before = copy.deepcopy((state, record))
        with patch.dict(sys.modules, {'executor_reask': None, 'configuration_guardian': None,
                                     'manage_install': None}), \
                patch('importlib.util.spec_from_file_location', side_effect=AssertionError('import')), \
                patch('subprocess.run', side_effect=AssertionError('subprocess')):
            self.assert_supported(state, record)
            self.assert_supported(*self.episode(current=True))
            for name in REVIEWED_V3:
                self.assert_supported(*self.episode(name))
        self.assertEqual((state, record), before)

    def test_raw_manifest_digest_cannot_be_redeclared(self):
        source, _pins, raw = self.releases[REVIEWED[0]]
        path = source.parent / 'MANIFEST.json'
        self.write_pin(path, raw + b'\n')
        self.assert_rejected(*self.episode())
        self.write_pin(path, raw)
        self.assert_supported(*self.episode())
        # Coherent attacker-supplied pins cannot replace the compiled trust root.
        manifest = json.loads(raw)
        payload = b'changed member'
        self.write_pin(source / 'README.md', payload)
        manifest['files']['README.md'].update(bytes=len(payload), sha256=digest(payload))
        self.write_pin(path, json.dumps(manifest).encode())
        self.assert_rejected(*self.episode())

    def test_every_manifest_member_is_checked_and_approvals_are_not_cached(self):
        source, pins, _raw = self.releases[REVIEWED[0]]
        state, record = self.episode()
        self.assert_supported(state, record)
        for member, pin in pins.items():
            with self.subTest(member=member):
                path = source / member
                raw = path.read_bytes()
                self.write_pin(path, b'X' + raw[1:], pin['executable'])
                self.assert_rejected(state, record)
                self.write_pin(path, raw, pin['executable'])
        self.assert_supported(state, record)

    def test_sweep_verification_reuses_bytes_but_next_sweep_reads_them_again(self):
        state, record = self.episode()
        verification = protocol.ReleaseVerification()
        with patch.object(protocol, '_read_regular', wraps=protocol._read_regular) as reads:
            self.assertTrue(protocol.supports_record(
                record, state, self.current_scheduler, verification=verification))
            first_reads = reads.call_count
            for _ in range(8):
                self.assertTrue(protocol.supports_state(state, record, verification=verification))
                self.assertTrue(protocol.supports_record(
                    record, state, self.current_scheduler, verification=verification))
            self.assertEqual(reads.call_count, first_reads)
            self.assertEqual(verification.full_verifications, 1)
            self.assertTrue(protocol.supports_state(
                state, record, verification=protocol.ReleaseVerification()))
            self.assertGreater(reads.call_count, first_reads)

    def test_sweep_reuse_detects_each_member_change_even_with_restored_mtime(self):
        source, pins, _raw = self.releases[REVIEWED[0]]
        state, record = self.episode()
        verification = protocol.ReleaseVerification()
        for member, pin in pins.items():
            with self.subTest(member=member):
                self.assertTrue(protocol.supports_state(state, record, verification=verification))
                path = source / member
                before, raw = path.stat(), path.read_bytes()
                self.write_pin(path, b'X' + raw[1:], pin['executable'])
                os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
                self.assertFalse(protocol.supports_record(
                    record, state, self.current_scheduler, verification=verification))
                self.write_pin(path, raw, pin['executable'])
        self.assertEqual(verification.invalidations, len(pins))

    def test_sweep_reuse_rejects_same_inode_through_a_symlinked_parent(self):
        source, _pins, _raw = self.releases[REVIEWED[0]]
        state, record = self.episode()
        verification = protocol.ReleaseVerification()
        self.assertTrue(protocol.supports_state(state, record, verification=verification))
        scripts, moved = source / 'scripts', source / 'moved-scripts'
        scripts.rename(moved)
        scripts.symlink_to(moved, target_is_directory=True)
        self.assertFalse(protocol.supports_state(state, record, verification=verification))
        self.assertEqual(verification.invalidations, 1)

    def test_sweep_reuse_rechecks_manifest_and_permissions(self):
        source, _pins, raw = self.releases[REVIEWED[0]]
        state, record = self.episode()
        verification = protocol.ReleaseVerification()
        self.assertTrue(protocol.supports_state(state, record, verification=verification))
        member = source / 'README.md'
        member.chmod(0o644)
        self.assertFalse(protocol.supports_state(state, record, verification=verification))
        member.chmod(0o444)
        self.assertTrue(protocol.supports_state(state, record, verification=verification))
        self.write_pin(source.parent / 'MANIFEST.json', raw + b'\n')
        self.assertFalse(protocol.supports_state(state, record, verification=verification))

    def test_sweep_snapshot_rejects_a_member_changed_after_its_first_read(self):
        source, _pins, _raw = self.releases[REVIEWED[0]]
        state, record = self.episode()
        manifest = source.parent / 'MANIFEST.json'
        read = protocol._read_regular
        manifest_reads = 0
        def change_after_read(path, *args, **kwargs):
            nonlocal manifest_reads
            raw = read(path, *args, **kwargs)
            if path == manifest:
                manifest_reads += 1
                if manifest_reads == 2:
                    member = source / 'README.md'
                    self.write_pin(member, b'X' + member.read_bytes()[1:])
            return raw
        with patch.object(protocol, '_read_regular', side_effect=change_after_read):
            self.assertFalse(protocol.supports_state(
                state, record, verification=protocol.ReleaseVerification()))

    def test_missing_writable_symlinked_or_special_members_are_refused(self):
        source, pins, raw = self.releases[REVIEWED[0]]
        manifest = source.parent / 'MANIFEST.json'
        manifest.chmod(0o644)
        self.assert_rejected(*self.episode())
        manifest.chmod(0o444)
        member = source / 'README.md'
        payload = member.read_bytes()
        member.chmod(0o644)
        self.assert_rejected(*self.episode())
        member.unlink()
        self.assert_rejected(*self.episode())
        target = self.home / 'same-content'
        self.write_pin(target, payload)
        member.symlink_to(target)
        self.assert_rejected(*self.episode())
        member.unlink()
        member.mkdir()
        self.assert_rejected(*self.episode())
        member.rmdir()
        self.write_pin(member, payload)
        script_dir = source / 'scripts'
        real_dir = source / 'real-scripts'
        script_dir.rename(real_dir)
        script_dir.symlink_to(real_dir, target_is_directory=True)
        self.assert_rejected(*self.episode())
        script_dir.unlink()
        real_dir.rename(script_dir)
        manifest.unlink()
        target = self.home / 'same-manifest'
        self.write_pin(target, raw)
        manifest.symlink_to(target)
        self.assert_rejected(*self.episode())

    def test_mode_and_byte_count_are_pins(self):
        source, pins, _raw = self.releases[REVIEWED[0]]
        script = source / 'bin/helper'
        script.chmod(0o444)
        self.assert_rejected(*self.episode())
        script.chmod(0o555)
        member = source / 'README.md'
        self.write_pin(member, member.read_bytes() + b'X')
        self.assert_rejected(*self.episode())

    def test_manifest_budget_path_and_pin_schema_fail_closed(self):
        source, _pins, raw = self.releases[REVIEWED[0]]
        for bad_name in ('../escape', '/absolute', './README.md', 'scripts//extra'):
            with self.subTest(member=bad_name):
                value = json.loads(raw)
                value['files'][bad_name] = dict(bytes=0, sha256=digest(b''), executable=False)
                changed = json.dumps(value).encode()
                self.write_pin(source.parent / 'MANIFEST.json', changed)
                # Supply an independent test trust root to exercise structural checks.
                with patch.dict(protocol.LEGACY_MANIFESTS, {REVIEWED[0]: digest(changed)}):
                    self.assert_rejected(*self.episode())
        self.write_pin(source.parent / 'MANIFEST.json', raw)
        with patch.object(protocol, 'MAX_RELEASE_FILES', 1):
            self.assert_rejected(*self.episode())
        with patch.object(protocol, 'MAX_RELEASE_BYTES', 1):
            self.assert_rejected(*self.episode())
        value = json.loads(raw)
        value['files']['README.md']['bytes'] = True
        changed = json.dumps(value).encode()
        self.write_pin(source.parent / 'MANIFEST.json', changed)
        with patch.dict(protocol.LEGACY_MANIFESTS, {REVIEWED[0]: digest(changed)}):
            self.assert_rejected(*self.episode())

    def test_descriptor_and_canonical_path_do_not_grant_unknown_v3_support(self):
        state, record = self.episode()
        state.update(version=3, file_protocol=protocol.descriptor())
        self.assert_rejected(state, record)
        unknown = self.make_release('0.4.100-unreviewed-fixture', current=True)
        controller = identity(*unknown[:2])
        state['controller'] = copy.deepcopy(controller)
        state['continuation']['controller'] = copy.deepcopy(controller)
        record.update(controller=controller, scheduler=issuer(*unknown[:2]),
                      schema='executor-reask-enrollment-v2', file_protocol=protocol.descriptor())
        self.assert_rejected(state, record)
        state['version'] = 2
        self.assert_rejected(state, record)
        state, record = self.episode(current=True)
        state['file_protocol']['sha256'] = '0' * 64
        self.assert_rejected(state, record)

    def test_continuation_must_be_complete_even_with_a_good_top_controller(self):
        for bad in ({}, [], False, '', {'controller': self.episode()[0]['controller']}):
            with self.subTest(continuation=bad):
                state, record = self.episode()
                state['continuation'] = bad
                self.assertIsNone(protocol.state_controller(state, record))
                self.assert_rejected(state, record)
        for field in self.episode()[0]['continuation']:
            with self.subTest(missing=field):
                state, record = self.episode()
                del state['continuation'][field]
                self.assert_rejected(state, record)

    def test_each_controller_candidate_must_match_in_full(self):
        for candidate in ('state', 'continuation', 'record'):
            with self.subTest(candidate=candidate):
                state, record = self.episode()
                other = self.episode(REVIEWED[1])[0]['controller']
                target = state if candidate == 'state' else record if candidate == 'record' else state['continuation']
                target['controller'] = other
                self.assertIsNone(protocol.state_controller(state, record))
                self.assert_rejected(state, record)
        state, record = self.episode()
        del record['controller']
        self.assert_rejected(state, record)
        state, record = self.episode(current=True)
        del state['controller']
        self.assert_rejected(state, record)

    def test_declared_six_member_combination_must_match_actual_pins(self):
        for member in protocol.LEGACY_FILES:
            with self.subTest(member=member):
                state, record = self.episode()
                for controller in (state['controller'], state['continuation']['controller'], record['controller']):
                    controller['files'][member] = '0' * 64
                self.assert_rejected(state, record)

    def test_scheduler_hash_source_python_label_and_producer_schema_are_bound(self):
        for field, value in (('sha256', '0' * 64), ('python', str(self.home / 'different-python')),
                             ('label', 'wrong'), ('path', str(self.releases[REVIEWED[1]][0] / protocol.SCHEDULER))):
            with self.subTest(field=field):
                state, record = self.episode()
                record['scheduler'][field] = value
                self.assertFalse(protocol.supports_record(record, state, self.current_scheduler))
        state, record = self.episode()
        record.update(schema='executor-reask-enrollment-v2', file_protocol=protocol.descriptor(),
                      authorization='EXPLICIT_REASK_ENROLL_EXISTING')
        self.assertFalse(protocol.supports_record(record, state, self.current_scheduler))
        state, record = self.episode(current=True)
        wrong = dict(self.current_scheduler, sha256='0' * 64)
        self.assertFalse(protocol.supports_record(record, state, wrong))
        record['file_protocol']['sha256'] = '0' * 64
        self.assertFalse(protocol.supports_record(record, state, self.current_scheduler))

    def test_current_fast_path_checks_actual_controller_and_execution_identity(self):
        state, record = self.episode(current=True)
        source, _pins, _raw = self.current
        self.assert_supported(state, record)  # Writable checkout, outside releases.
        member = source / 'scripts/executor_ready.py'
        payload = member.read_bytes()
        self.write_pin(member, b'X' + payload[1:])
        self.assert_rejected(state, record)
        self.write_pin(member, payload)
        with patch.object(protocol.sys, 'executable', str(self.home / 'different-python')):
            self.assert_rejected(state, record)
        scheduler = source / protocol.SCHEDULER
        payload = scheduler.read_bytes()
        self.write_pin(scheduler, b'X' + payload[1:])
        self.assertFalse(protocol.supports_record(record, state, self.current_scheduler))
        self.write_pin(scheduler, payload)
        self.assert_supported(state, record)

    def test_malformed_json_shapes_return_boolean_false(self):
        for value in (None, [], '', 0, {}, {'version': 2}, {'continuation': []}):
            with self.subTest(state=value):
                self.assertIs(protocol.supports_state(value), False)
                self.assertIs(protocol.supports_record(value, value, None), False)
        state, record = self.episode()
        for malformed in (None, [], {}, dict(record['controller'], files=list(protocol.FILES))):
            with self.subTest(controller=malformed):
                bad = copy.deepcopy(state)
                bad['controller'] = malformed
                self.assert_rejected(bad, record)


class InstalledReleasePinsTests(unittest.TestCase):
    def test_real_current_checkout_controller_and_provenance(self):
        # Exercise the real checkout without importing the concurrently edited
        # scheduler/reask modules or requiring an installed manifest for tests.
        source = Path(protocol.__file__).resolve().parents[1]
        pins = {key: dict(sha256=digest((source / key).read_bytes()))
                for key in (*protocol.FILES, protocol.SCHEDULER)}
        controller, scheduler = identity(source, pins), issuer(source, pins)
        state = dict(version=3, controller=controller, continuation=None,
                     file_protocol=protocol.descriptor())
        record = dict(schema='executor-reask-enrollment-v2', authorization='EXPLICIT_REASK_START',
                      file_protocol=protocol.descriptor(), controller=copy.deepcopy(controller),
                      scheduler=scheduler)
        self.assertIs(protocol.supports_state(state, record), True)
        self.assertIs(protocol.supports_record(record, state, scheduler), True)

    def test_reviewed_installed_files_without_copying_or_loading_code(self):
        base = Path.home() / '.local/share/multi-agent-collaboration/releases'
        if not all((base / name / 'MANIFEST.json').is_file() for name in REVIEWED):
            self.skipTest('reviewed installed releases unavailable')
        for name in REVIEWED:
            with self.subTest(release=name):
                source = base / name / 'source'
                pins = {key: dict(sha256=value) for key, value in protocol.LEGACY[name].items()}
                controller = identity(source, pins)
                modern = name in protocol.LEGACY_FILE_PROTOCOL
                state = dict(version=3 if modern else 2, controller=controller, continuation=None)
                record = dict(schema='executor-reask-enrollment-v2' if modern else 'executor-reask-enrollment-v1', authorization='EXPLICIT_REASK_START',
                              controller=copy.deepcopy(controller), scheduler=issuer(source, pins))
                if modern:
                    state['file_protocol'] = protocol.descriptor()
                    record['file_protocol'] = protocol.descriptor()
                self.assertIs(protocol.supports_state(state, record), True)
                self.assertIs(protocol.supports_record(record, state, record['scheduler']), True)


if __name__ == '__main__':
    unittest.main()
