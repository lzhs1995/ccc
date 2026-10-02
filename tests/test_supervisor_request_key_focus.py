"""Per-session observed request keys in narrow supervisor windows."""
from pathlib import Path
import tempfile
import unittest
import cmux_supervisor_tui as tui
import test_supervisor as geometry

class FocusKeyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.model, self.rows = geometry.ShortWindowAndPromptTests()._model_and_rows(Path(self.temp.name))
        for index, key in ((1, 'sk-fake-A-' + 'a' * 42), (3, 'sk-fake-B-' + 'b' * 42)):
            c = self.rows[index].candidate
            c.agent_kind = 'codex'
            c.session = tui.SessionResult(status='ok', agent_kind='codex', api_key_observed=key,
                                          api_key_config='fake-global-must-not-appear')

    def draw(self, index, width):
        screen = geometry.BoundedScreen(24, width)
        tui._draw(screen, self.model, self.rows, index, 'all', '', '')
        self.assertEqual(screen.violations, [])
        return ''.join(text for y, x, text in screen.writes if y == tui.layout(24)['focus_rule'])

    def test_two_sessions_at_89_columns(self):
        for index in (1, 3):
            line = self.draw(index, 89)
            self.assertIn(self.rows[index].candidate.session.api_key_observed, line)
            self.assertNotIn('fake-global', line)
            self.assertNotIn(self.rows[4-index].candidate.session.api_key_observed, line)

    def test_missing_observation_has_no_global_fallback(self):
        self.rows[1].candidate.session.api_key_observed = ''
        line = self.draw(1, 89)
        self.assertIn('未核实', line)
        self.assertNotIn('fake-global', line)

    def test_historical_request_is_distinct_from_current_request(self):
        candidate = self.rows[1].candidate
        candidate.session.api_key_observation_historical = True
        line = self.draw(1, 89)
        self.assertIn('历史请求', line)
        self.assertNotIn(candidate.session.api_key_observed, line)
        self.assertNotIn('fake-global', line)
        detail = ''.join(tui.api_key_detail_lines(candidate, 89))
        self.assertIn('早于当前 CLI 启动', detail)
        self.assertIn(candidate.session.api_key_observed, detail)
        self.assertIn(self.rows[3].candidate.session.api_key_observed, self.draw(3, 89))

    def test_long_key_not_partially_drawn(self):
        self.rows[1].candidate.session.api_key_observed = 'sk-fake-long-' + 'z' * 200
        line = self.draw(1, 89)
        self.assertIn('K 查看完整值', line)
        self.assertNotIn('sk-fake', line)
        self.assertIn(self.rows[1].candidate.session.api_key_observed,
                      ''.join(tui.api_key_detail_lines(self.rows[1].candidate, 88)))

    def test_small_windows_and_non_codex(self):
        for width in (30, 50, 70, 89, 120):
            self.draw(1, width)
        self.rows[1].candidate.agent_kind = 'claude'
        self.assertNotIn('sk-fake', self.draw(1, 89))
        self.assertNotIn('sk-fake', self.draw(0, 89))
