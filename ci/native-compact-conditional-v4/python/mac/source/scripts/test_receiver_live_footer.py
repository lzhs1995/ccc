import unittest
import cmux_bridge as b

class LiveFooter(unittest.TestCase):
    def screen(self):
        return "\n".join(["────────────────", "❯", "────────────────", "[Opus 5 (1M context)] │ task git:(main)", "⏱️  4h 14m", "上下文 █░░░ 8%", "1 CLAUDE.md | 9 MCPs | 6 钩子", "◐ Bash: .../task | ✓ Bash ×17 | ✓ Read ×2", "⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents"])
    def test_current_footer(self):
        self.assertEqual(b.receiver_input_kind(self.screen()), 'AGENT_TUI')
    def test_unknown_tail_rejected(self):
        self.assertEqual(b.receiver_input_kind(self.screen()+'\nmy draft'), 'UNKNOWN')
    def test_shell_tail_rejected(self):
        self.assertEqual(b.receiver_input_kind(self.screen()+'\nuser@mac ~ %'), 'SHELL')
    def test_missing_border_rejected(self):
        self.assertEqual(b.receiver_input_kind(self.screen().replace('────────────────','')), 'UNKNOWN')
    def test_unknown_elapsed_rejected(self):
        self.assertEqual(b.receiver_input_kind(self.screen().replace('4h 14m','unknown command')), 'UNKNOWN')
