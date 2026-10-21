"""离线原生投递夹具：只替代外部 I/O，保留绑定、journal、proof 的真实代码。"""
import contextlib
import copy
import json
import subprocess
import sys
import tempfile
import types
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import cmux_bridge as bridge
import cmux_native_delivery as native


class ScreenSequence:
    """脚本化读屏；读完后保持最后一帧，允许有限只读等待。"""

    def __init__(self, screens):
        self.screens = list(screens)
        if not self.screens:
            raise ValueError("at least one screen is required")
        self.index = 0

    def __call__(self, *args, **kwargs):
        screen = self.screens[min(self.index, len(self.screens) - 1)]
        self.index += 1
        return screen() if callable(screen) else screen


class NativeFixture(contextlib.AbstractContextManager):
    """可变身份 + 真文件；默认不会制造 user 入站或成功回执。"""

    def __init__(self, home=None, identity=None, provider="codex"):
        self.stack = contextlib.ExitStack()
        if home is None:
            home = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.home = Path(home).resolve()
        self.provider = provider
        self.identity = identity if identity is not None else dict(
            workspace_uuid="workspace", caller_surface_uuid="caller",
            target_surface_uuid="target", target_pane_uuid="pane")
        self.tty = "ttys-native-test"
        self.process_tty = self.tty
        self.session_id = str(uuid.uuid4())
        self.process_state = dict(
            pid=987654, birth="test-process-birth-1", executable="/offline-test/" + provider,
            file_identity={"device": 1, "inode": 2},
            argv=["/offline-test/" + provider, "resume", self.session_id],
            env={"CMUX_WORKSPACE_ID": self.identity["workspace_uuid"],
                 "CMUX_SURFACE_ID": self.identity["target_surface_uuid"]})
        self.tree = {"windows": [{"workspaces": [{"id": self.identity["workspace_uuid"],
            "panes": [{"id": self.identity["target_pane_uuid"], "surfaces": [
                {"id": self.identity["target_surface_uuid"], "type": "terminal",
                 "tty": self.tty}]}]}]}]}
        self.native_root = (self.home / "native-fixture" / provider).resolve()
        self.native_root.mkdir(parents=True, exist_ok=True)
        filename = ("rollout-test-" if provider == "codex" else "") + self.session_id + ".jsonl"
        self.transcript = self.native_root / filename
        meta = ({"type": "session_meta", "payload": {"id": self.session_id}}
                if provider == "codex" else {"type": "system", "sessionId": self.session_id})
        self.transcript.write_text(json.dumps(meta) + "\n")

    def __enter__(self):
        self.stack.enter_context(patch.object(Path, "home", return_value=self.home))
        self.pin = self.stack.enter_context(patch.object(
            bridge, "pin_workspace", side_effect=lambda *_: copy.deepcopy(self.identity)))
        self.run = self.stack.enter_context(patch.object(bridge, "_run", side_effect=self._run))
        self.stack.enter_context(patch.object(native, "_native_root", side_effect=self._root))
        # process() / ps 为可变外部观测；_target_process、require_bound 不被 mock。
        facade = types.SimpleNamespace(process=self._process, ProcessExited=native.identity.ProcessExited,
                                      IdentityError=native.identity.IdentityError)
        self.stack.enter_context(patch.object(native, "identity", facade))
        self.stack.enter_context(patch.object(native, "subprocess", types.SimpleNamespace(
            run=self._ps, SubprocessError=subprocess.SubprocessError)))
        self.stack.enter_context(patch.object(bridge.time, "sleep"))
        # 夹具永远不触达 live surface；测试可替换 side_effect，并检查完整调用次数。
        self.send = self.stack.enter_context(patch.object(bridge, "send_text"))
        self.key = self.stack.enter_context(patch.object(bridge, "send_key"))
        def protected_submit(surface, text):
            self.send(surface, text)
            self.key(surface, 'enter')
            return {'submitted': True, 'submit_error': None}
        self.submit = self.stack.enter_context(patch.object(
            bridge, 'submit_text_protected', side_effect=protected_submit))
        # A new Codex send must explicitly supply a fake RPC client in its
        # test. No historical fixture may reach the user's live daemon.
        self.rpc = self.stack.enter_context(patch(
            "cmux_codex_input.DaemonRPC",
            side_effect=AssertionError("offline fixture forbids live Codex RPC")))
        return self

    def __exit__(self, *args):
        return self.stack.__exit__(*args)

    @classmethod
    def attach(cls, testcase, **kwargs):
        fixture = cls(**kwargs)
        fixture.__enter__()
        testcase.addCleanup(fixture.__exit__, None, None, None)
        return fixture

    def export_state(self, path):
        """向测试子进程传递外部观测；JSONL 和 journal 始终保留原文件。"""
        state = {name: copy.deepcopy(getattr(self, name)) for name in (
            "provider", "identity", "tty", "process_tty", "session_id",
            "process_state", "tree")}
        state.update({name: str(getattr(self, name)) for name in (
            "home", "native_root", "transcript")})
        state["format"] = "offline-native-fixture-v1"
        path = Path(path).resolve()
        path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        return path

    @classmethod
    def from_state(cls, path):
        """只读恢复夹具，禁止调用会重建或截断 transcript 的构造函数。"""
        state = json.loads(Path(path).read_text(encoding="utf-8"))
        if state.pop("format", None) != "offline-native-fixture-v1":
            raise ValueError("unexpected offline native fixture format")
        fixture = cls.__new__(cls)
        fixture.stack = contextlib.ExitStack()
        for name in ("provider", "identity", "tty", "process_tty", "session_id",
                     "process_state", "tree"):
            setattr(fixture, name, state[name])
        for name in ("home", "native_root", "transcript"):
            setattr(fixture, name, Path(state[name]))
        if not fixture.transcript.is_file():
            raise ValueError("original native transcript is missing")
        return fixture

    def _root(self, provider):
        if provider != self.provider:
            raise AssertionError("unexpected provider: " + provider)
        return self.native_root

    def _run(self, *args, **kwargs):
        if args == ("tree", "--all", "--json", "--id-format", "both"):
            return json.dumps(self.tree)
        if args[:2] == ('rpc', 'surface.input_state'):
            target = json.loads(args[2])
            return json.dumps(dict(target, state='empty', agent=True,
                                   terminal=True, blocks_typing=False))
        raise AssertionError("offline fixture forbids cmux command: " + str(args[0]))

    def _process(self, pid, **kwargs):
        if pid != self.process_state["pid"]:
            raise AssertionError("unexpected process id")
        return copy.deepcopy(self.process_state)

    def _ps(self, args, **kwargs):
        if args[:3] == ["/bin/ps", "-ww", "-U"]:
            output = str(self.process_state["pid"]) + " " + self.process_state["executable"] + "\n"
        elif args[:2] == ["/bin/ps", "-p"]:
            output = self.process_tty + "\n"
        else:
            raise AssertionError("unexpected native external command")
        return subprocess.CompletedProcess(args, 0, stdout=output, stderr="")

    def append_record(self, record):
        with self.transcript.open("a") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")

    def append_user(self, text, *, timestamp=None, session_id=None):
        stamp = timestamp or datetime.now(timezone.utc).isoformat()
        if self.provider == "codex":
            record = {"timestamp": stamp, "type": "response_item", "payload": {
                "type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}}
        else:
            record = {"timestamp": stamp, "type": "user", "sessionId": session_id or self.session_id,
                      "message": {"role": "user", "content": text}}
        self.append_record(record)

    def append_queued(self, text):
        if self.provider != "claude":
            raise ValueError("queued_command is a Claude native record")
        self.append_record({"timestamp": datetime.now(timezone.utc).isoformat(),
            "sessionId": self.session_id, "type": "attachment",
            "attachment": {"type": "queued_command", "prompt": text}})

    def receipt_on_key(self, text):
        """每个成功用例必须显式使用此回调或 append_user，不能默认确认。"""
        def receive(surface, key):
            self.append_user(text)
        return receive

    @staticmethod
    def draft(text, provider="codex"):
        glyph, footer = ("›", "GPT-6 high") if provider == "codex" else ("❯", "[claude-opus-5]")
        # TUI 后续行有两个字符的 gutter；保留 payload 自身的空白。
        return glyph + " " + text.replace("\n", "\n  ") + "\n" + footer

    @staticmethod
    def ready_screens(before, draft, *after):
        return ScreenSequence([before, draft, draft, *(after or (before,))])


def historical_terminal_dispatch(surface, text, marker=None, confirm_lines=200,
                                 task_pack_path=None, force_compose=False,
                                 delivery_observer=None, *, native_binding=None,
                                 native_attempt=None, wait_seconds=None):
    """Test-only route for immutable historical paste/key regression fixtures."""
    bridge._validate_submission(surface, text, task_pack_path, force_compose,
                                delivery_observer, native_binding, native_attempt)
    return bridge._submit_terminal_once(surface, text, marker, confirm_lines,
        task_pack_path, force_compose, delivery_observer,
        native_binding=native_binding, native_attempt=native_attempt,
        wait_seconds=wait_seconds)


class LegacyTerminalFixture(NativeFixture):
    """Build historical paste/key attempts for reconciliation regression tests.

    This fixture changes only dispatch selection. Contract validation, journals,
    bindings, native files and recovery checks remain production code. There is
    no production environment flag or sender option to select this route.
    """

    def __enter__(self):
        super().__enter__()

        self.stack.enter_context(patch.object(bridge, "_submit_text_once",
                                             side_effect=historical_terminal_dispatch))
        self.stack.enter_context(patch.object(native, "marker_binding",
                                             side_effect=lambda binding, text, marker: binding))
        return self


def native_hook_command(script, active_dir, state_path, *args):
    """真实 hook 的离线入口，仅附加已有 native 外部输入的复现。"""
    return [sys.executable, "-B", str(Path(__file__).resolve()), str(state_path),
            str(script), str(active_dir), *map(str, args)]


def main():
    import offline_test_hook
    state_path, script, active_dir, *args = sys.argv[1:]
    with NativeFixture.from_state(state_path) as fixture, patch.object(
            sys, "argv", [str(offline_test_hook.__file__), script, active_dir, *args]):
        try:
            return offline_test_hook.main()
        finally:
            # hook 的 proof 复核必须只读，不能借测试夹具隐蔽地重发消息。
            fixture.send.assert_not_called()
            fixture.key.assert_not_called()


if __name__ == "__main__":
    raise SystemExit(main())
