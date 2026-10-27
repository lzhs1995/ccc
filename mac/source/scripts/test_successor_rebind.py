import hashlib
import json
import tempfile
import unittest
from unittest.mock import Mock, patch
from pathlib import Path

import successor_rebind as s
import cmux_bridge as bridge
import cmux_workspace_guard as guard


OLD_WS = "C72EE6C7-6338-4328-9EA7-6F81C3E5305D"
NEW_WS = "7388D9B9-F29D-4E81-8A20-CA543D163264"
OLD_SUP = "B4272AE6-CFF0-4DE7-B45D-4536B1FDC8BA"
SUCCESSOR = "5CDB0F6A-078F-4034-8D45-45EB99D5D68F"
TARGET = "BD472FA9-E2F6-4098-A478-98FD49DFA98B"
CALLER_PANE = "959D2EF4-6F01-4C4E-BEF7-B1887C32AF4E"
TARGET_PANE = "6AFA792F-539A-4E11-A1F9-0B1B7ED5CFD2"


class SuccessorRebindTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)

        def evidence(name, value):
            path = root / name
            path.write_text(json.dumps(value, sort_keys=True) + "\n")
            return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}

        self.failure = evidence("failure.json", {
            "status": "SUPERVISOR_FAILED", "old_supervisor_uuid": OLD_SUP,
            "failure_kind": "terminal_unavailable", "observed_at": 1791620000,
        })
        self.freeze = evidence("freeze.json", {
            "executor_frozen": True, "sentinel_stopped": True,
            "active_writers": [], "old_attempt_sha256": "a" * 64,
        })
        self.auth = evidence("authorization.json", {
            "authorization_source": "user_message",
            "successor_surface_uuid": SUCCESSOR, "target_executor_uuid": TARGET,
            "allow_cross_workspace_successor": True,
        })
        self.artifact = {
            "schema": s.SCHEMA, "binding_mode": s.MODE,
            "authorization_source": "user_message", "writes_frozen": True,
            "old_supervisor_uuid": OLD_SUP, "old_workspace_uuid": OLD_WS,
            "successor_surface_uuid": SUCCESSOR, "successor_workspace_uuid": NEW_WS,
            "successor_pane_uuid": CALLER_PANE, "target_executor_uuid": TARGET,
            "target_workspace_uuid": OLD_WS, "target_pane_uuid": TARGET_PANE,
            "task_id": "task-successor", "episode_id": "episode-1",
            "old_task_pack_sha256": "b" * 64, "old_attempt_sha256": "a" * 64,
            "successor_of_binding_sha256": "c" * 64,
            "authorization_record_sha256": self.auth["sha256"],
            "supervisor_failure_receipt": self.failure,
            "freeze_evidence": self.freeze, "authorization_record": self.auth,
            "expires_at": 1791629999,
        }

    def tearDown(self):
        self.tmp.cleanup()

    def tree(self):
        return {"windows": [{"workspaces": [
            {"ref": "workspace:14", "id": OLD_WS, "panes": [{
                "ref": "pane:32", "id": TARGET_PANE, "surfaces": [{
                    "ref": "surface:4546", "id": TARGET, "type": "terminal", "tty": "ttys052"
                }]
            }]},
            {"ref": "workspace:15", "id": NEW_WS, "panes": [{
                "ref": "pane:33", "id": CALLER_PANE, "surfaces": [{
                    "ref": "surface:46", "id": SUCCESSOR, "type": "terminal", "tty": "ttys034"
                }]
            }]},
        ]}]}

    def test_valid_successor_pair(self):
        identity = {"caller": {"surface_ref": "surface:46", "workspace_ref": "workspace:15", "pane_ref": "pane:33"}}
        pair = s.resolve_pair(identity, self.tree(), self.artifact, env={
            "CMUX_SURFACE_ID": SUCCESSOR, "CMUX_WORKSPACE_ID": NEW_WS
        }, now=1791621000)
        self.assertEqual(pair["caller"]["surface_uuid"], SUCCESSOR)
        self.assertEqual(pair["target"]["workspace_uuid"], OLD_WS)

    def test_missing_user_authorization_denied(self):
        bad = dict(self.artifact, authorization_source="peer_assertion")
        with self.assertRaisesRegex(s.SuccessorRebindError, "user authorization"):
            s.validate_artifact(bad, now=1791621000)

    def test_identity_unresolved_is_not_failure(self):
        path = Path(self.failure["path"])
        path.write_text(json.dumps({"status": "SUPERVISOR_FAILED", "old_supervisor_uuid": OLD_SUP,
                                    "identity_unresolved": True}) + "\n")
        self.artifact["supervisor_failure_receipt"] = {
            "path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()
        }
        with self.assertRaisesRegex(s.SuccessorRebindError, "identity unresolved"):
            s.validate_artifact(self.artifact, now=1791621000)

    def test_active_writer_denied(self):
        path = Path(self.freeze["path"])
        path.write_text(json.dumps({"executor_frozen": True, "sentinel_stopped": True,
                                    "active_writers": ["writer"], "old_attempt_sha256": "a" * 64}) + "\n")
        self.artifact["freeze_evidence"] = {
            "path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()
        }
        with self.assertRaisesRegex(s.SuccessorRebindError, "not frozen"):
            s.validate_artifact(self.artifact, now=1791621000)

    def test_target_workspace_drift_denied(self):
        tree = self.tree()
        tree["windows"][0]["workspaces"][0]["id"] = NEW_WS
        identity = {"caller": {"surface_ref": "surface:46", "workspace_ref": "workspace:15", "pane_ref": "pane:33"}}
        with self.assertRaisesRegex(s.SuccessorRebindError, "target workspace drift"):
            s.resolve_pair(identity, tree, self.artifact, env={"CMUX_SURFACE_ID": SUCCESSOR,
                                                               "CMUX_WORKSPACE_ID": NEW_WS}, now=1791621000)

    def test_append_is_create_only(self):
        out = Path(self.tmp.name) / "successor.json"
        record = s.append_record(out, self.artifact, now=1791621000)
        self.assertEqual(record["status"], "PENDING")
        with self.assertRaisesRegex(s.SuccessorRebindError, "new absolute path"):
            s.append_record(out, self.artifact, now=1791621000)

    def test_successor_paste_requires_both_target_uuids(self):
        proof = {
            "target_workspace_uuid": OLD_WS, "target_surface_uuid": TARGET,
            "caller_workspace_uuid": NEW_WS, "caller_surface_uuid": SUCCESSOR,
        }
        params = {"text": "HELLO", "submit_key": "none", "workspace_id": OLD_WS,
                  "surface_id": TARGET}
        with patch.object(bridge, "pin_successor_pair", return_value=proof), \
             patch.object(bridge.subprocess, "run", return_value=Mock(returncode=0, stdout="ok", stderr="")) as run:
            bridge._run_successor("rpc", "terminal.paste", json.dumps(params), artifact=self.artifact)
            sent = json.loads(run.call_args.args[0][3])
            self.assertEqual(sent["workspace_id"], OLD_WS)
            self.assertEqual(sent["surface_id"], TARGET)

    def test_successor_paste_rejects_caller_workspace_as_target(self):
        proof = {
            "target_workspace_uuid": OLD_WS, "target_surface_uuid": TARGET,
            "caller_workspace_uuid": NEW_WS, "caller_surface_uuid": SUCCESSOR,
        }
        params = {"text": "HELLO", "submit_key": "none", "workspace_id": NEW_WS,
                  "surface_id": TARGET}
        with patch.object(bridge, "pin_successor_pair", return_value=proof):
            with self.assertRaisesRegex(Exception, "target workspace override"):
                bridge._run_successor("rpc", "terminal.paste", json.dumps(params), artifact=self.artifact)


def maintenance_artifact(root, *, workspace=OLD_WS):
    message = "允许已授权的继承者 supervisor 与指定 Claude 重新握手；通信不重做旧任务。"
    pins = dict(successor_surface_uuid=SUCCESSOR, successor_workspace_uuid=NEW_WS,
                successor_pane_uuid=CALLER_PANE, target_executor_uuid=TARGET,
                target_workspace_uuid=workspace, target_pane_uuid=TARGET_PANE)
    auth = dict(pins, authorization_source="user_message", scope="maintenance_handshake",
                communication_only=True, user_message=message,
                user_message_sha256=hashlib.sha256(message.encode()).hexdigest())
    path = Path(root) / "maintenance-auth.json"
    path.write_text(json.dumps(auth, ensure_ascii=False), encoding="utf-8")
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    return dict(pins, schema=s.SCHEMA_V2, binding_mode=s.MODE_V2,
                authorization_source="user_message", scope="maintenance_handshake",
                communication_only=True, allow_task_replay=False, allow_shared_writes=False,
                authorization_record={"path": str(path), "sha256": sha}, authorization_record_sha256=sha)


class MaintenanceRebindTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.artifact = maintenance_artifact(self.temp.name)
        self.tree = SuccessorRebindTests.tree(self)
        self.identity = {"caller": {"surface_ref": "surface:46", "workspace_ref": "workspace:15", "pane_ref": "pane:33"}}

    def test_no_old_supervisor_resume_failure_freeze_or_task_requirement(self):
        result = s.resolve_pair(self.identity, self.tree, self.artifact)
        self.assertEqual(result["mode"], s.MODE_V2)
        self.assertNotIn("old_supervisor_uuid", result["artifact"])

    def test_same_workspace_user_authorized_pair_is_supported(self):
        self.artifact = maintenance_artifact(self.temp.name, workspace=NEW_WS)
        self.tree["windows"][0]["workspaces"][0]["id"] = NEW_WS
        self.assertEqual(s.resolve_pair(self.identity, self.tree, self.artifact)["mode"], s.MODE_V2)

    def test_both_pane_pins_are_enforced(self):
        for field in ("successor_pane_uuid", "target_pane_uuid"):
            changed = json.loads(json.dumps(self.tree))
            ws = 1 if field.startswith("successor") else 0
            changed["windows"][0]["workspaces"][ws]["panes"][0]["id"] = OLD_SUP
            with self.assertRaisesRegex(s.SuccessorRebindError, "pane drift"):
                s.resolve_pair(self.identity, changed, self.artifact)

    def test_authorization_cannot_be_reused_for_another_target(self):
        with self.assertRaisesRegex(s.SuccessorRebindError, "authorization pair mismatch"):
            s.validate_artifact(dict(self.artifact, target_executor_uuid=OLD_SUP))

    def test_body_pin_is_required(self):
        auth_path = Path(self.artifact["authorization_record"]["path"])
        auth = json.loads(auth_path.read_text())
        auth["user_message"] += " changed"
        auth_path.write_text(json.dumps(auth))
        sha = hashlib.sha256(auth_path.read_bytes()).hexdigest()
        self.artifact.update(authorization_record={"path": str(auth_path), "sha256": sha}, authorization_record_sha256=sha)
        with self.assertRaisesRegex(s.SuccessorRebindError, "message hash changed"):
            s.validate_artifact(self.artifact)

    def test_communication_does_not_grant_shared_writes_or_task_replay(self):
        for field in ("allow_task_replay", "allow_shared_writes"):
            with self.assertRaisesRegex(s.SuccessorRebindError, "communication scope"):
                s.validate_artifact(dict(self.artifact, **{field: True}))

    def test_revoked_or_expired_authorization_refused(self):
        for changes in ({"status": "REVOKED"}, {"expires_at": 1}):
            with self.assertRaises(s.SuccessorRebindError):
                s.validate_artifact(dict(self.artifact, **changes))

    def test_append_preserves_v2_schema(self):
        output = Path(self.temp.name) / "record.json"
        self.assertEqual(s.append_record(output, self.artifact)["schema"], s.SCHEMA_V2)


class NativeSamePaneTests(MaintenanceRebindTests):
    def same_pane(self, *, native=True):
        self.artifact = maintenance_artifact(self.temp.name, workspace=NEW_WS)
        self.artifact['target_pane_uuid'] = CALLER_PANE
        target = self.tree['windows'][0]['workspaces'].pop(0)['panes'][0]['surfaces'][0]
        self.tree['windows'][0]['workspaces'][0]['panes'][0]['surfaces'].append(target)
        path = Path(self.artifact['authorization_record']['path'])
        auth = json.loads(path.read_text())
        auth['target_pane_uuid'] = CALLER_PANE
        if native:
            fields = dict(transport=s.CODEX_SAME_PANE, caller_thread_id=OLD_SUP,
                          target_thread_id=OLD_WS)
            self.artifact.update(fields)
            auth.update(fields)
        path.write_text(json.dumps(auth))
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        self.artifact.update(authorization_record={'path': str(path), 'sha256': digest},
                             authorization_record_sha256=digest)

    def test_same_pane_terminal_contact_uses_exact_maintenance_authorization(self):
        self.same_pane(native=False)
        result = s.resolve_pair(self.identity, self.tree, self.artifact)
        self.assertEqual(result['target']['pane_uuid'], CALLER_PANE)
        self.assertNotEqual(result['caller']['surface_uuid'], result['target']['surface_uuid'])

    def test_same_pane_native_pair_with_original_threads(self):
        self.same_pane()
        self.assertEqual(s.resolve_pair(self.identity, self.tree, self.artifact)['target']['pane_uuid'], CALLER_PANE)
        for changes in ({'transport': 'terminal'}, {'caller_thread_id': TARGET},
                        {'target_thread_id': OLD_SUP}):
            with self.assertRaises(s.SuccessorRebindError):
                s.resolve_pair(self.identity, self.tree, dict(self.artifact, **changes))

    def test_original_same_pane_pair_can_use_terminal_fallback(self):
        self.same_pane()
        proof = dict(target_workspace_uuid=NEW_WS, target_surface_uuid=TARGET)
        with patch.object(bridge, 'pin_successor_pair', return_value=proof), \
                patch.object(bridge.subprocess, 'run') as run:
            run.return_value.returncode = 0
            run.return_value.stdout = '{}'
            params = dict(text='STATUS: ORIGINAL_PAIR', submit_key='none',
                          workspace_id=NEW_WS, surface_id=TARGET)
            for args in [('rpc', 'terminal.paste', json.dumps(params)),
                         ('send', '--surface', TARGET, 'STATUS: ORIGINAL_PAIR'),
                         ('send-key', '--surface', TARGET, 'enter')]:
                bridge._run_successor(*args, artifact=self.artifact, receipt_contract="native-user-marker-v1")
            self.assertEqual(run.call_count, 3)
            with self.assertRaisesRegex(Exception, 'target surface override'):
                bridge._run_successor('send-key', '--surface', OLD_SUP, 'enter',
                                      artifact=self.artifact, receipt_contract="native-user-marker-v1")
            self.assertEqual(run.call_count, 3)
            with self.assertRaisesRegex(Exception, 'Historical native-only'):
                bridge._run_successor('send-key', '--surface', TARGET, 'enter', artifact=self.artifact)
            self.assertEqual(run.call_count, 3)

    def test_actual_caller_foreground_thread_required(self):
        self.same_pane()
        for proof in (None, {'session': OLD_SUP}, {'foreground': {'thread_id': TARGET}}):
            with patch.object(guard, 'caller_snapshot', return_value=(self.identity, self.tree, {}, proof)):
                with self.assertRaisesRegex(Exception, 'foreground thread'):
                    guard.require_successor_pair(self.artifact)
        with patch.object(guard, 'caller_snapshot', return_value=(self.identity, self.tree, {},
                          {'foreground': {'thread_id': OLD_SUP}})):
            self.assertEqual(guard.require_successor_pair(self.artifact)['target_surface_uuid'], TARGET)


if __name__ == "__main__":
    unittest.main()
