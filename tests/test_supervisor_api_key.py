"""Credential display must not turn a mutable file into runtime auth proof."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

import cmux_supervisor_tui as tui

SID = "01a0e7c1-dbf1-7c23-a186-ce9af424ed55"


class CredentialDisplayTests(unittest.TestCase):
    def detail_candidate(self, key):
        return SimpleNamespace(session=tui.SessionResult(
            session_id=SID, api_key_config=key, api_key_note="运行态未确认",
            api_key_source="/tmp/用户/config.toml"))

    def test_detail_wrap_preserves_complete_long_key_at_narrow_widths(self):
        key = "sk-" + "Ab0123456789" * 40
        for width in (20, 79, 119, 159, 199):
            lines = tui.api_key_detail_lines(self.detail_candidate(key), width)
            self.assertIn(key, "".join(lines))
            self.assertTrue(all(tui.display_width(line) <= width for line in lines))
            self.assertIn("非运行态", "".join(lines))

    def test_detail_real_draw_exposes_every_key_segment_via_scroll(self):
        key = "sk-" + "0123456789abcdef" * 30
        for width in (20, 80, 120, 160):
            candidate = self.detail_candidate(key)
            lines = tui.api_key_detail_lines(candidate, width - 1)
            class Screen:
                def __init__(self):
                    self.seen = []
                    self.keys = iter([tui.curses.KEY_DOWN] * len(lines) + [ord("q")])
                def getmaxyx(self): return (7, width)
                def erase(self): pass
                def refresh(self): pass
                def getch(self): return next(self.keys)
                def addnstr(self, y, x, text, count, style):
                    self.seen.append((y, text))
                    assert 0 <= y < 7
                    assert tui.display_width(text) <= width - 1
            screen = Screen()
            tui._api_key_page(screen, candidate)
            for line in lines:
                self.assertIn(line, [text for y, text in screen.seen if y < 5])

    def test_detail_copy_is_explicit_exact_and_does_not_send_to_session(self):
        candidate = self.detail_candidate("sk-full-test-key")
        screen = SimpleNamespace(getmaxyx=lambda:(24,80), erase=lambda:None,
                                 refresh=lambda:None, addnstr=lambda *args:None)
        for key, calls in (("sk-full-test-key", 1), ("", 0)):
            candidate.session.api_key_config = key
            keys = iter([ord("y"), ord("q")])
            screen.getch = lambda: next(keys)
            with patch.object(tui.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
                tui._api_key_page(screen, candidate)
            self.assertEqual(run.call_count, calls)
            if calls:
                run.assert_called_once_with(["/usr/bin/pbcopy"], input=key,
                                            text=True, capture_output=True, timeout=2)

    def observe(self, *, births=None, argv=None, env_extra=None, config_extra=""):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            config = home / "config.toml"
            config.write_text('model_provider="custom"\n[model_providers.custom]\n'
                              'experimental_bearer_token="sk-test-new"\n' + config_extra)
            env = {"HOME": tmp, "CODEX_HOME": tmp, **(env_extra or {})}
            result = tui.SessionResult(status="ok", session_id=SID, pid=123,
                                       agent_kind="codex")
            with patch("ccc_guard_scope.birth", side_effect=births or [[1, 2], [1, 2]]), \
                 patch("ccc_guard_scope.arguments", return_value=(argv or ["codex", "resume", SID], env)):
                tui.observe_configured_api_key(result)
            return result

    def test_file_key_is_explicitly_configuration_only(self):
        result = self.observe()
        self.assertEqual(result.api_key_config, "sk-test-new")
        self.assertIn("运行态/项目覆盖未确认", result.api_key_note)
        self.assertNotIn("sk-test-new", repr(result))

    def test_pid_reuse_drops_observation(self):
        self.assertEqual(self.observe(births=[[1, 2], [1, 3]]).api_key_config, "")

    def test_other_session_drops_observation(self):
        self.assertEqual(self.observe(argv=["codex", "resume", "another-session"]).api_key_config, "")

    def test_env_key_precedes_literal_but_is_still_not_runtime_proof(self):
        result = self.observe(config_extra='env_key="TEST_TOKEN"\n',
                              env_extra={"TEST_TOKEN": "sk-env-test"})
        self.assertEqual(result.api_key_config, "sk-env-test")
        self.assertIn("未确认", result.api_key_note)

    def test_missing_required_env_key_does_not_display_literal(self):
        self.assertEqual(self.observe(config_extra='env_key="TEST_TOKEN"\n').api_key_config, "")

    def test_config_change_drops_observation(self):
        original = Path.open
        count = 0
        def changing(path, *args, **kwargs):
            nonlocal count
            if path.name == "config.toml" and args == ("rb",):
                count += 1
                if count == 2:
                    with original(path, "w") as f:
                        f.write('model_provider="changed"')
            return original(path, *args, **kwargs)
        with patch.object(Path, "open", changing):
            self.assertEqual(self.observe().api_key_config, "")

    def test_header_and_key_fit_together_without_partial_key(self):
        key = "配置:sk-test-new"
        cells = tuple("" for _ in tui.ROW_COLUMNS)
        for width in range(80, 400):
            row = tui._row_text("     ", cells, "标题", width, SID, api_key=key)
            header = tui.header_text(width)
            self.assertEqual(key in row, "api-key" in header)
            self.assertLessEqual(tui.display_width(row), max(width, tui.row_layout(None).head_cells))
            if key not in row:
                self.assertNotIn("sk-test", row)


if __name__ == "__main__":
    unittest.main()
