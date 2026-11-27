import hashlib
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import quote
from uuid import uuid4

import restore


class RestoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.cwd = str(self.home)
        self.agy, self.grok = str(uuid4()), str(uuid4())
        self.source = self.home / "source.json"
        self.out = self.home / "result"
        self.source_dir = self.home / ".gemini/antigravity-cli/conversations"
        self.source_dir.mkdir(parents=True)
        self.db = self.source_dir / (self.agy + ".db")
        with closing(sqlite3.connect(self.db)) as db, db:
            db.executescript("CREATE TABLE trajectory_meta(cascade_id TEXT); CREATE TABLE steps(idx INTEGER); INSERT INTO steps VALUES (1)")
            db.execute("INSERT INTO trajectory_meta VALUES (?)", (self.agy,))
        self.gdir = self.home / ".grok/sessions" / quote(self.cwd, safe="") / self.grok
        self.gdir.mkdir(parents=True)
        self.summary = self.gdir / "summary.json"
        self.summary.write_text(json.dumps({"info": {"id": self.grok, "cwd": self.cwd}}))
        (self.gdir / "chat_history.jsonl").write_text('{"type":"user","content":"example"}\n')
        self.updates = self.gdir / "updates.jsonl"
        self.updates.write_text(json.dumps({"method": "session/update", "params": {"sessionId": self.grok, "update": {
            "sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": "hello"}}}}) + "\n")
        self.panes = {
            "3": {"cwd": self.cwd, "label": "left", "agent_session": self.ref("agy", self.agy),
                  "launch_argv": ["obsolete-launcher"]},
            "8": {"cwd": self.cwd, "label": "right", "agent_session": self.ref("grok", self.grok),
                  "agent_resume": "obsolete-command", "managed_agent_kind": "grok"}}
        self.layout = {"Split": {"direction": "Horizontal", "ratio": 0.61,
                                 "first": {"Pane": 3}, "second": {"Pane": 8}}}
        self.tab = {"focused": 8, "root_pane": 3, "zoomed": False, "custom_name": "tools",
                    "panes": self.panes, "layout": self.layout}
        self.space = {"id": "wA", "tabs": [self.tab], "public_pane_numbers": {"3": 2, "8": 21},
                      "public_tab_numbers": [4], "next_public_tab_number": 5,
                      "next_public_pane_number": 22, "active_tab": 0}
        self.snapshot = {"version": 3, "active": 0, "selected": 0, "workspaces": [self.space]}

    def ref(self, agent, sid):
        return {"kind": "id", "agent": agent, "value": sid,
                "source": "herdr:" + ("antigravity_cli" if agent == "agy" else agent)}

    def stage(self, **kwargs):
        self.source.write_text(json.dumps(self.snapshot))
        return restore.stage(self.source, "demo", self.out, self.home, **kwargs)

    def test_full_topology_and_shell_only_default(self):
        plan = self.stage()
        self.assertEqual([(r["tab"], r["pane"], r["status"]) for r in plan["panes"]],
                         [("wA:t4", "wA:p2", "ready"), ("wA:t4", "wA:pN", "ready")])
        session = restore.read_json(self.out / "session.json")
        tab = session["workspaces"][0]["tabs"][0]
        self.assertEqual(tab["layout"], self.layout)
        self.assertEqual((tab["focused"], tab["root_pane"]), (8, 3))
        self.assertEqual(tab["panes"]["3"], {"cwd": self.cwd, "label": "left"})
        self.assertEqual(tab["panes"]["8"], {"cwd": self.cwd, "label": "right"})
        self.assertNotIn("ticket", plan["panes"][0])

    def test_auto_uses_only_ticketed_originals(self):
        plan = self.stage(auto=True)
        rows = plan["panes"]
        self.assertEqual(restore.native_args(rows[0]), ["--conversation", self.agy])
        self.assertEqual(restore.native_args(rows[1]), ["--resume", self.grok])
        self.assertNotEqual(rows[0]["ticket"], rows[1]["ticket"])
        session = restore.read_json(self.out / "session.json")
        argv = session["workspaces"][0]["tabs"][0]["panes"]["3"]["agent_resume"]["argv"]
        self.assertEqual(argv[0], "env")
        self.assertEqual(argv[-2:], ["--ticket", rows[0]["ticket"]])
        self.assertEqual((self.out / "plan.json").stat().st_mode & 0o777, 0o600)

    def test_duplicate_sid_held_in_both_seats(self):
        self.panes["8"]["agent_session"] = self.panes["3"]["agent_session"]
        self.assertTrue(all(r["status"] == "held" for r in self.stage(auto=True)["panes"]))

    def test_grok_rejects_raw_chat_without_official_resume_log(self):
        self.updates.unlink()
        self.assertEqual(self.stage(auto=True)["panes"][1]["status"], "held")

    def test_grok_rejects_foreign_update(self):
        self.updates.write_text(self.updates.read_text().replace(self.grok, self.agy))
        self.assertEqual(self.stage(auto=True)["panes"][1]["status"], "held")

    def test_grok_long_cwd_group_from_marker(self):
        import shutil
        cwd = self.home / ("a" * 120) / ("b" * 120)
        cwd.mkdir(parents=True)
        group = self.gdir.parent.parent / "native-slug"
        group.mkdir()
        shutil.move(str(self.gdir), str(group / self.grok))
        (group / ".cwd").write_text(str(cwd))
        (group / self.grok / "summary.json").write_text(json.dumps({"info": {"id": self.grok, "cwd": str(cwd)}}))
        row = {"agent": "grok", "sid": self.grok, "cwd": str(cwd)}
        self.assertEqual(restore.verify_native(row, self.home), group / self.grok / "updates.jsonl")
        shutil.copytree(group, group.with_name("second-group"))
        with self.assertRaisesRegex(restore.Held, "ambiguous"):
            restore.verify_native(row, self.home)

    def test_wrong_native_id_and_reused_grok_directory(self):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE trajectory_meta SET cascade_id = ?", (self.grok,))
        self.summary.write_text(json.dumps({"info": {"id": self.agy, "cwd": self.cwd}}))
        self.assertTrue(all(r["status"] == "held" for r in self.stage(auto=True)["panes"]))

    def test_source_and_no_cwd_hold_only_affected_pane(self):
        self.panes["3"]["agent_session"]["source"] = "title"
        self.panes["8"]["cwd"] = None
        self.assertTrue(all(r["status"] == "held" for r in self.stage(auto=True)["panes"]))

    def test_invalid_layout_has_no_output(self):
        self.layout["Split"]["second"] = {"Pane": 3}
        with self.assertRaises(restore.Held):
            self.stage()
        self.assertFalse(self.out.exists())

    def test_duplicate_public_or_tab_allocations_rejected(self):
        self.space["public_pane_numbers"]["8"] = 2
        with self.assertRaises(restore.Held):
            self.stage()

    def test_native_public_number_alphabet_rollover(self):
        self.assertEqual([restore.public_number(n) for n in (10, 21, 31, 32, 33, 64, 65)],
                         ["A", "N", "Z", "0", "11", "10", "21"])

    def test_layout_multi_workspace_tab_and_vertical_split(self):
        import copy
        other = copy.deepcopy(self.space)
        other["id"] = "wB"
        second = copy.deepcopy(other["tabs"][0])
        second.update(root_pane=9, focused=9, layout={"Pane": 9}, panes={"9": {"cwd": self.cwd}})
        other["tabs"][0]["layout"]["Split"]["direction"] = "Vertical"
        other["tabs"].append(second)
        other["public_pane_numbers"]["9"] = 22
        other.update(public_tab_numbers=[4, 8], next_public_tab_number=9, next_public_pane_number=23, active_tab=1)
        self.snapshot["workspaces"].append(other)
        self.snapshot["active"] = 1
        plan = self.stage()
        self.assertEqual(len(plan["panes"]), 5)
        self.assertEqual(plan["panes"][-1]["tab"], "wB:t8")
        session = restore.read_json(self.out / "session.json")
        self.assertEqual(session["active"], 1)
        self.assertEqual(session["workspaces"][1]["active_tab"], 1)

    def test_hash_addressed_archive_is_pinned(self):
        self.source = self.home / ("0" * 64 + ".json")
        with self.assertRaisesRegex(restore.Held, "digest"):
            self.stage()

    def test_second_stage_preserves_files(self):
        self.stage()
        original = (self.out / "plan.json").read_bytes()
        with self.assertRaises(FileExistsError):
            self.stage(auto=True)
        self.assertEqual((self.out / "plan.json").read_bytes(), original)

    def test_fleet_evidence_for_codex_and_claude(self):
        fleet = {"panes": {}}
        for internal, agent, sid, public, record in [
                ("3", "codex", self.agy, "wA:p2", {"type": "session_meta", "payload": {"id": self.agy}}),
                ("8", "claude", self.grok, "wA:pN", {"sessionId": self.grok})]:
            path = self.home / (agent + ".jsonl")
            raw = (json.dumps(record) + "\n").encode()
            path.write_bytes(raw)
            self.panes[internal]["agent_session"] = self.ref(agent, sid)
            fleet["panes"]["demo#" + public] = {
                "session": "demo", "cwd": self.cwd, "pane_id": public, "agent": agent, "sid": sid,
                "history": {"bytes": len(raw), "path": str(path), "sha256": hashlib.sha256(raw).hexdigest()}}
        rows = self.stage(auto=True, fleet=fleet)["panes"]
        self.assertEqual([r["status"] for r in rows], ["ready", "ready"])
        self.assertEqual(restore.native_args(rows[0]), ["resume", self.agy])
        self.assertEqual(restore.native_args(rows[1]), ["--resume", self.grok])
        (self.home / "codex.jsonl").write_bytes(b"replaced")
        with self.assertRaises(restore.Held):
            restore.verify_native(rows[0], self.home)

    def seat(self, *, owner=None, second_claim=None, agent=None, env_pane="wA:p2"):
        state = {"pane_id": "wA:p2", "workspace_id": "wA", "tab_id": "wA:t4", "terminal_id": "term_new", "agent": agent}
        info = {"pane_id": "wA:p2", "foreground_process_group_id": os.getpgrp(),
                "foreground_processes": [{"pid": owner or os.getpid()}]}
        def rpc(session, group, action, *args):
            return {"get": {"pane": state}, "process-info": {"process_info": info},
                    "list": {"panes": [state, {"agent_session": second_claim}]}}[action]
        with patch.dict(os.environ, {"HERDR_SESSION": "demo", "HERDR_WORKSPACE_ID": "wA",
                                     "HERDR_TAB_ID": "wA:t4", "HERDR_PANE_ID": env_pane}), \
             patch("os.isatty", return_value=True), patch("os.tcgetpgrp", return_value=os.getpgrp()):
            return restore.check_seat({"session": "demo"}, {"workspace": "wA", "tab": "wA:t4", "pane": "wA:p2",
                                                               "agent": "agy", "sid": self.agy}, rpc=rpc)

    def test_checked_process_owns_original_terminal(self):
        self.assertEqual(self.seat(), "term_new")

    def test_occupied_reused_or_ambiguous_seat(self):
        for args in [{"owner": os.getpid()+1}, {"agent": "grok"}, {"env_pane": "wA:pN"},
                     {"second_claim": self.ref("agy", self.agy)}]:
            with self.subTest(args=args), self.assertRaises(restore.Held):
                self.seat(**args)

    def test_durable_one_shot_before_exec(self):
        plan = self.stage(auto=True)
        ticket = plan["panes"][0]["ticket"]
        with patch.object(restore, "check_seat", return_value="term_test"), \
             patch.object(restore, "check_holders"), patch.object(Path, "home", return_value=self.home), \
             patch.object(restore.shutil, "which", return_value="/mock-cli"), patch("os.chdir"), \
             patch("os.execv", side_effect=OSError("lost result")) as execute:
            with self.assertRaisesRegex(OSError, "lost result"):
                restore.enter(self.out / "plan.json", ticket)
            with self.assertRaises(FileExistsError):
                restore.enter(self.out / "plan.json", ticket)
            self.assertEqual(execute.call_count, 1)
            self.assertEqual(execute.call_args.args, ("/mock-cli", ["/mock-cli", "--conversation", self.agy]))

    def test_no_foreign_ticket(self):
        self.stage(auto=True)
        with self.assertRaisesRegex(restore.Held, "ticket"):
            restore.enter(self.out / "plan.json", "0" * 32)

    def witness(self, mutate=None, sid=None):
        self.panes["3"].pop("agent_session", None)
        sid = sid or self.agy
        epoch = str(uuid4())
        native = self.home / ("rollout-" + sid + ".jsonl")
        raw = (json.dumps({"type": "session_meta", "payload": {"id": sid}}) + "\n").encode()
        native.write_bytes(raw)
        row = {"workspace": "wA", "tab": "wA:t4", "pane": "wA:p2", "terminal_id": "term_original",
               "session": "demo", "agent": "codex", "sid": sid, "cwd": self.cwd, "identity_source": "native_current_turn",
               "observed_process": {"pid": 114, "birth": [1600000000, 11]},
               "history": {"path": str(native), "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}}
        data = {"schema": "ccc-herdr-native-epoch-v1", "epoch": epoch, "session": "demo",
                "snapshot_sha256": hashlib.sha256(json.dumps(self.snapshot).encode()).hexdigest(),
                "observed_at_ns": 1700000000000000000, "panes": [row]}
        if mutate:
            mutate(data, row)
        path = self.home / (epoch + ".json")
        restore.private_json(path, data)
        return {"witness": path, "witness_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}

    def test_selected_epoch_restores_anonymous_seat(self):
        first = self.witness(sid=self.grok)
        second = self.witness()
        plan = self.stage(auto=True, **first)
        self.assertEqual(plan["witness_epoch"], first["witness"].stem)
        self.assertEqual((plan["panes"][0]["sid"], plan["panes"][0]["status"]), (self.grok, "ready"))
        self.assertEqual(plan["panes"][0]["witness_terminal_id"], "term_original")
        self.out = self.home / "second-plan"
        self.assertEqual(self.stage(**second)["panes"][0]["sid"], self.agy)

    def test_legacy_fleet_cannot_bind_anonymous_seat(self):
        w = self.witness()
        row = restore.read_json(w["witness"])["panes"][0]
        fleet = {"panes": {"demo#wA:p2": dict(row, pane_id=row["pane"])}}
        self.assertEqual(self.stage(auto=True, fleet=fleet)["panes"][0]["status"], "held")

    def test_identity_revalidated_from_history(self):
        w = self.witness()
        (self.home / ("rollout-" + self.agy + ".jsonl")).write_bytes(b"replaced\n")
        row = self.stage(auto=True, **w)["panes"][0]
        self.assertEqual(row["status"], "held")
        self.assertNotIn("ticket", row)

    def test_unpinned_replaced_or_wrong_permissions_rejected(self):
        w = self.witness()
        for args in [{"witness": w["witness"]}, {"witness_sha256": w["witness_sha256"]},
                     dict(w, witness_sha256="0"*64)]:
            with self.subTest(args=args), self.assertRaises(restore.Held):
                self.stage(**args)
        w["witness"].chmod(0o644)
        with self.assertRaises(restore.Held):
            self.stage(**w)
        self.assertFalse(self.out.exists())

    def test_witness_placement_source_and_epoch_mismatch(self):
        changes = [(True,"session","other"), (True,"snapshot_sha256","0"*64), (True,"epoch",str(uuid4())),
                   (False,"workspace","wB"), (False,"tab","wA:t6"), (False,"pane","wA:p3"), (False,"cwd","/"),
                   (False,"identity_source","native_resume_argv"), (False,"terminal_id",""),
                   (False,"observed_process",{"pid": 1, "birth": [0, 1]})]
        for header,key,value in changes:
            def change(data, row):
                (data if header else row)[key] = value
            with self.subTest(key=key), self.assertRaises(restore.Held):
                self.stage(**self.witness(change))
        self.assertFalse(self.out.exists())

    def test_witness_rejects_terminal_pid_and_sid_collisions(self):
        self.panes["8"].pop("agent_session")
        for duplicate in ("terminal_id", "observed_process", "sid"):
            def add(data, row):
                other = dict(row, pane="wA:pN", terminal_id="term_other", sid=self.grok,
                             observed_process={"pid": 225, "birth": [1600000030, 10]})
                other[duplicate] = row[duplicate]
                data["panes"].append(other)
            with self.subTest(duplicate=duplicate), self.assertRaises(restore.Held):
                self.stage(**self.witness(add))

    def test_conflict_with_native_ref_is_rejected(self):
        def change(data, row):
            self.panes["3"]["agent_session"] = self.ref("claude", self.grok)
            data["snapshot_sha256"] = hashlib.sha256(json.dumps(self.snapshot).encode()).hexdigest()
        with self.assertRaisesRegex(restore.Held, "conflicts"):
            self.stage(**self.witness(change))

    def test_cross_source_duplicate_sid_holds_both(self):
        self.panes["8"]["agent_session"] = self.ref("codex", self.agy)
        plan = self.stage(auto=True, **self.witness())
        self.assertEqual([r["status"] for r in plan["panes"]], ["held", "held"])


if __name__ == "__main__":
    unittest.main()
