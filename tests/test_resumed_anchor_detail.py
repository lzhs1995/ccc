import json
from pathlib import Path
import tempfile
import unittest

import cmux_codex_watch as core
from cmux_supervisor_tui import SupervisorModel


ANCHOR = "incompatible: native live frame did not confirm screen anchoring"


class Client:
    def tree(self):
        return {"windows": [{"workspaces": [{"id": "w", "panes": [{
            "id": "p", "surfaces": [{"id": "s", "ref": "surface:44",
                                      "type": "terminal", "title": "codex"}],
        }]}]}]}

    def top_all(self):
        return {"windows": [{"workspaces": [{"surfaces": [{
            "kind": "surface", "ref": "surface:44", "processes": [
                {"kind": "process", "name": "codex", "path": "/bin/codex"}],
        }]}]}]}


class ResumedAnchorDetailTests(unittest.TestCase):
    def detail(self, *, reason=ANCHOR, global_pause=False, rule_pause=False,
               excluded=False, observed="working", explicit_pause=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = core.default_config()
            config.update(mode="armed", global_paused=global_pause)
            config["workspace_rules"] = [{"workspace_id": "w", "enabled": True,
                "paused": rule_pause, "agent": "codex",
                "excluded_surface_ids": ["s"] if excluded else []}]
            if explicit_pause:
                config["targets"] = [{"surface_id": "s", "workspace_id": "w",
                                      "enabled": True, "paused": True}]
            runtime = {"s": {"state": observed, "observed_state": observed,
                "observed_at": 1, "observed_reason": "fresh observation",
                "paused_reason": reason}}
            config_path = root / "config.json"
            config_path.write_text(json.dumps(config))
            state_path = root / "state.json"
            state_path.write_text(json.dumps(runtime))
            before = state_path.read_bytes()
            model = SupervisorModel(config_path, client=Client())
            model.refresh(force=True)
            row = next(row for row in model.candidates if row.surface_id == "s")
            self.assertEqual(state_path.read_bytes(), before)
            self.assertEqual(json.loads(config_path.read_text()), config)
            return row.status_detail

    def test_resumed_monitoring_shows_new_observation(self):
        for state in ("working", "queued_followup", "recoverable_error", "idle"):
            with self.subTest(state=state):
                self.assertEqual(self.detail(observed=state), "fresh observation")

    def test_active_pauses_and_exclusions_keep_reason(self):
        for field in ("global_pause", "rule_pause", "excluded", "explicit_pause"):
            with self.subTest(field=field):
                self.assertEqual(self.detail(**{field: True}), ANCHOR)

    def test_provider_manual_unknown_and_unobserved_keep_reason(self):
        for reason in ("provider retry cooldown or waiting for a shared retry slot; session preserved",
                       "manual pause", "incompatible: foreign surface"):
            with self.subTest(reason=reason):
                self.assertEqual(self.detail(reason=reason), reason)
        self.assertEqual(self.detail(observed="unknown"), ANCHOR)
