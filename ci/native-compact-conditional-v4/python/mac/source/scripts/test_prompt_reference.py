"""Bounded notices retain exact bodies and cannot weaken ordinary-send contracts."""
import hashlib
import os
from pathlib import Path
import tempfile
import unittest

import cmux_prompt_reference as ref


class PromptReferenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve() / "bodies"
        self.marker = "REFERENCE_TEST_20261009"
        self.text = "STATUS: " + self.marker + "\r\n  中文e\u0301😀\t\\n\r\n\r\n" + ("正文\t \r\n" * 100) + "  \r\n"

    def prepare(self, text=None):
        text = self.text if text is None else text
        planned = ref.plan(text, self.marker, self.root)
        pin = ref.persist(planned["reference"], text)
        return planned, pin

    def test_short_bytes_and_whitespace_are_unchanged_without_files(self):
        text = "STATUS: " + self.marker + "  中文\\n  "
        self.assertEqual(ref.plan(text, self.marker, self.root),
                         dict(text=text, reference=None))
        self.assertIsNone(ref.persist(None, text))
        self.assertFalse(self.root.exists())

    def test_short_terminal_lf_crlf_and_cr_preserve_exact_body_in_reference(self):
        for suffix in ("\n", "\r\n", "\r", "\n\n", " \r\n"):
            with self.subTest(suffix=repr(suffix)):
                text = "STATUS: " + self.marker + "\r\n中文\t  " + suffix
                planned, pin = self.prepare(text)
                self.assertIsNotNone(planned["reference"])
                self.assertEqual(Path(planned["reference"]["path"]).read_bytes(),
                                 text.encode("utf-8"))
                self.assertEqual(ref.read_body(planned["reference"]), (text, pin))
                self.assertEqual(ref.validate_wire_body(planned["text"]), pin)
                self.assertLessEqual(len(planned["text"].encode()), ref.MAX_INLINE_BYTES)
                self.assertFalse(planned["text"].endswith(("\r", "\n")))
                with self.assertRaisesRegex(ValueError, "TRAILING_NEWLINE_REFERENCE_REQUIRED"):
                    ref.require_inline(text)

    def test_short_helper_send_ending_lf_keeps_original_bytes_and_marker(self):
        identity = dict(caller_surface_uuid="caller")
        message = "STATUS: original message\n"
        old = ref.helper_request(identity, "send", message, "newline-request",
                                 home=self.root, reference=False)
        new = ref.helper_request(identity, "send", message, "newline-request", home=self.root)
        self.assertEqual(old["marker"], new["marker"])
        self.assertTrue(old["payload"].endswith("\n"))
        ref.persist(new["body_reference"], ref.original_helper_payload(new))
        self.assertEqual(ref.read_body(new["body_reference"])[0], old["payload"])
        self.assertEqual(ref.validate_wire_body(new["payload"])["sha256"], old["payload_sha256"])

    def test_unicode_crlf_tabs_and_trailing_spaces_survive_exactly(self):
        plan, pin = self.prepare()
        body = Path(plan["reference"]["path"])
        self.assertEqual(body.read_bytes(), self.text.encode("utf-8"))
        self.assertEqual(plan["reference"]["bytes"], len(self.text.encode("utf-8")))
        self.assertEqual(plan["reference"]["sha256"],
                         hashlib.sha256(self.text.encode("utf-8")).hexdigest())
        self.assertLessEqual(len(plan["text"].encode("utf-8")), ref.MAX_INLINE_BYTES)
        self.assertEqual(ref.read_body(plan["reference"]), (self.text, pin))
        self.assertEqual(ref.validate_wire_body(plan["text"]), pin)
        self.assertEqual(ref.persist(plan["reference"], self.text), pin)

    def test_budget_is_utf8_bytes_and_oversize_body_has_zero_files(self):
        prefix = "STATUS: " + self.marker + " "
        exact = prefix + "x" * (ref.MAX_INLINE_BYTES - len(prefix.encode()))
        self.assertIsNone(ref.plan(exact, self.marker, self.root)["reference"])
        self.assertIsNotNone(ref.plan(exact + "中", self.marker, self.root)["reference"])
        with self.assertRaisesRegex(ValueError, "BODY_BUDGET"):
            ref.plan(prefix + "x" * ref.MAX_BODY_BYTES, self.marker, self.root)
        self.assertFalse(self.root.exists())

    def test_overlong_reference_path_cannot_create_an_overlong_wire(self):
        with self.assertRaisesRegex(ValueError, "REFERENCE_REQUIRED"):
            ref.plan(self.text, self.marker, self.root / ("中" * 300))
        self.assertFalse(self.root.exists())

    def test_formal_packs_and_callbacks_cannot_hide_in_long_bodies(self):
        for prefix in ("TASK: ", "TASK_PACK=", "TASK PACK: ", "TASK_PACK_V2 TASK=",
                       "DONE|id|nonce|REPORT=", "BLOCKED|id|nonce|REPORT="):
            with self.subTest(prefix=prefix):
                full = "[CMUX-AGENT][delivery:" + self.marker + "]\n" + prefix + self.text
                with self.assertRaisesRegex(ValueError, "TASK_BOUND_TRANSPORT_REQUIRED"):
                    ref.plan(full, self.marker, self.root)
        self.assertFalse(self.root.exists())

    def test_missing_or_writable_body_is_not_recreated(self):
        plan, _ = self.prepare()
        path = Path(plan["reference"]["path"])
        path.chmod(0o600)
        with self.assertRaisesRegex(ValueError, "MESSAGE_BODY_CHANGED"):
            ref.persist(plan["reference"], self.text)
        path.unlink()
        with self.assertRaises(OSError):
            ref.validate_wire_body(plan["text"])
        self.assertFalse(path.exists())

    def test_changed_bytes_are_never_repaired(self):
        plan, _ = self.prepare()
        path = Path(plan["reference"]["path"])
        path.chmod(0o600)
        path.write_bytes(b"corrupted")
        path.chmod(0o400)
        with self.assertRaisesRegex(ValueError, "MESSAGE_BODY_CHANGED"):
            ref.persist(plan["reference"], self.text)
        self.assertEqual(path.read_bytes(), b"corrupted")

    def test_symlinks_and_hardlinks_are_rejected(self):
        plan, _ = self.prepare()
        path = Path(plan["reference"]["path"])
        backup = path.with_name("original.txt")
        path.rename(backup)
        path.symlink_to(backup)
        with self.assertRaises((OSError, ValueError)):
            ref.validate_wire_body(plan["text"])
        path.unlink()
        os.link(backup, path)
        with self.assertRaises(ValueError):
            ref.validate_wire_body(plan["text"])

    def test_wire_changes_and_wrong_body_marker_are_rejected(self):
        plan, _ = self.prepare()
        for wire in (plan["text"] + "\n",
                     plan["text"].replace(self.marker, "DIFFERENT_MARKER"),
                     plan["text"].replace("BYTES=", "BYTES=0")):
            with self.subTest(wire=wire[:80]):
                with self.assertRaises((ValueError, OSError)):
                    ref.validate_wire_body(wire)

    def test_helper_keeps_original_marker_and_legacy_wire(self):
        identity = dict(caller_surface_uuid="caller")
        for mode in ("ask", "send", "broadcast"):
            with self.subTest(mode=mode):
                old = ref.helper_request(identity, mode, self.text, "request1",
                                         home=self.root, reference=False)
                new = ref.helper_request(identity, mode, self.text, "request1",
                                         home=self.root)
                self.assertEqual(old["marker"], new["marker"])
                self.assertEqual(ref.original_helper_payload(new), old["payload"])
                self.assertEqual(new["original_payload_sha256"], old["payload_sha256"])
                ref.persist(new["body_reference"], old["payload"])
                self.assertEqual(ref.read_body(new["body_reference"])[0], old["payload"])
                self.assertLessEqual(len(new["payload"].encode()), ref.MAX_INLINE_BYTES)
                self.assertIn(old["marker"], new["payload"])

    def test_internal_control_characters_always_reference_even_when_short(self):
        for char in ("\n", "\r", "\r\n", "\t"):
            with self.subTest(char=repr(char)):
                text = "STATUS: " + self.marker + char + "preserve  bytes"
                planned, pin = self.prepare(text)
                self.assertEqual(planned["reference"]["format"], ref.SINGLE_LINE_FORMAT)
                self.assertFalse(any(c in planned["text"] for c in "\r\n\t"))
                self.assertEqual(ref.read_body(planned["reference"])[0], text)
                self.assertEqual(ref.validate_wire_body(planned["text"]), pin)
                with self.assertRaisesRegex(ValueError, "MULTILINE_OR_TAB"):
                    ref.require_inline(text)

    def test_json_quoted_path_roundtrips_without_control_characters(self):
        self.root /= '中文 "quoted" \\ literal\tsegment'
        planned, pin = self.prepare()
        self.assertFalse(any(c in planned["text"] for c in "\r\n\t"))
        self.assertEqual(ref.wire_reference(planned["text"]), planned["reference"])
        self.assertEqual(ref.validate_wire_body(planned["text"]), pin)

    def test_legacy_v1_is_readable_but_cannot_be_pasted_again(self):
        planned, _ = self.prepare()
        legacy = dict(planned["reference"], format=ref.FORMAT)
        wire = ref._wire(self.marker, legacy)
        self.assertEqual(ref.wire_reference(wire), legacy)
        self.assertEqual(ref.validate_wire_body(wire)["sha256"], legacy["sha256"])
        with self.assertRaisesRegex(ValueError, "MULTILINE_OR_TAB"):
            ref.require_inline(wire)


if __name__ == "__main__":
    unittest.main()
