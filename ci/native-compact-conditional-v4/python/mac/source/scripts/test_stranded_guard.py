"""Guard: Enter is not delivery, regardless of the marker's shape (2026-10-08)."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cmux_bridge as b
import cmux_submit_confirmation_guard as g

CALLER = "E7C1C83C-946C-4E59-8753-F506F2069A12"
TARGET = "2A2CDBE8-DD07-4185-A972-2E56BB3135DF"
MARKER = "C2596_R22_PREFLIGHT_STATUS_20261008T131247Z"  # not 16-hex, no protocol shape
FOOTER = "  GPT-6-Astra xhigh · ~/project · Context 28% used"
STUCK = "\n".join(["• Working (21m • esc to interrupt)", " ", "› " + MARKER + " | status text",
                   " ", FOOTER, "  tab to queue message"])
EMPTY = "\n".join(["› " + MARKER + " | status text", "• Read it", " ", "› Ask Codex to do anything",
                   " ", FOOTER])


class StrandedGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        patcher = patch.object(g, "_STATE_ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def attempt(self, caller=CALLER, phases=("PASTE_INTENT", "PASTED", "ENTER_INTENT", "ENTER_SENT",
                                              "POST_ENTER_OBSERVATION"), receipt=False):
        journal = self.root / "message-dispatch-v1" / "k1"
        journal.mkdir(parents=True)
        value = {"binding": {"marker": MARKER, "identity": {"caller_surface_uuid": caller,
                                                            "target_surface_uuid": TARGET}},
                 "phase": phases[-1], "events": [{"phase": p} for p in phases]}
        (journal / "attempt-0001.json").write_text(json.dumps(value))
        if receipt:
            (journal / "receipt.json").write_text("{}")

    def scan(self, screen):
        return g.stranded_attempts(CALLER, lambda surface, n: screen, b)

    def test_custom_marker_left_in_compose_is_reported(self):
        self.attempt()
        found = self.scan(STUCK)
        self.assertEqual([f["marker"] for f in found], [MARKER])
        self.assertFalse(found[0]["recovery_used"])
        # Shape-based classification alone misses this exact case.
        self.assertFalse(g.has_protocol_shape(MARKER + " | status text"))

    def test_consumed_or_receipted_or_foreign_attempts_are_quiet(self):
        self.attempt()
        self.assertEqual(self.scan(EMPTY), [])
        self.tmp.cleanup(); self.root.mkdir(parents=True)
        self.attempt(receipt=True)
        self.assertEqual(self.scan(STUCK), [])
        self.tmp.cleanup(); self.root.mkdir(parents=True)
        self.attempt(caller="FC5DB919-47A7-4A8F-9A46-1182958CE532")
        self.assertEqual(self.scan(STUCK), [])

    def test_never_pressed_enter_is_not_stranded(self):
        self.attempt(phases=("PREPARED", "NO_INPUT"))
        self.assertEqual(self.scan(STUCK), [])

    def test_used_recovery_is_flagged(self):
        self.attempt(phases=("PASTE_INTENT", "ENTER_SENT", "QUEUE_TAB_INTENT", "POST_QUEUE_TAB_OBSERVATION"))
        self.assertTrue(self.scan(STUCK)[0]["recovery_used"])

    def test_evaluate_warns_on_stranded_even_without_marker_in_command(self):
        self.attempt()
        payload = {"tool_input": {"command": "python3 - <<'EOF'\nimport cmux_bridge\n"
                                             "cmux_bridge.submit_text(SUP, text, marker=tag)\nEOF"}}
        result = g.evaluate(payload, reader=lambda surface, n: STUCK, bridge=b, caller=CALLER)
        self.assertEqual(result["action"], "warn")
        self.assertIn("STRANDED_IN_COMPOSE", g._render(result))
        self.assertIn("--recover-stranded", g._render(result))


if __name__ == "__main__":
    unittest.main()
