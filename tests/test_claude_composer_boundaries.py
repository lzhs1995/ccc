"""Complete bordered composer ownership and the final Enter lease.

Only FakeClient sees Text/Enter; every lock lives in a private temp root.
"""
import copy
import fcntl
import hashlib
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import cmux_codex_watch as w
import tests as package
from tests import test_claude_provider_guard as provider_guard
from tests.test_watch import claude_wrapped_composer_payload, display_width, span, wrap_to_cells

FOOTER = "  [Opus 5 (1M context)] │ ~/repo"
HINT = "  ⏵⏵ bypass permissions on"
BUDGET_FIELDS = ("claude_submit_attempts", "claude_submit_not_sent", "claude_submit_write_unknown",
                 "claude_submit_last_attempt_at", "claude_submit_last_reason", "claude_submit_phase",
                 "claude_submit_event_id", "claude_orphan_enter_count", "claude_orphan_enter_at")


def payload_from_lines(rows_text, *, columns=60, cursor_row=0):
    """Render-grid payload whose visible rows are exactly ``rows_text``."""
    styles = [{"id": 0, "foreground": "#FFFFFF", "background": "#1E1E1E", "faint": False}]
    spans = [span(row, 0, text, 0, display_width(text)) for row, text in enumerate(rows_text) if text]
    return {"render_grid": {
        "format": "cmux.render-grid.v1", "surface_id": "surface-uuid",
        "rows": len(rows_text), "columns": columns,
        "cursor": {"row": cursor_row, "column": 2, "visible": False},
        "styles": styles, "row_spans": spans, "scrollback_spans": [], "history_rows": 0,
    }}


def bordered_rows(draft, *, columns=60, transcript=("previous output",), extra_below=()):
    """Rows of a live composer: border, ``❯`` + draft rows, identical border, footer."""
    rows_text = list(transcript)
    top = len(rows_text)
    rows_text.append("─" * columns)
    first, *rest = draft
    rows_text.append("❯ " + first if first else "❯")
    rows_text.extend(rest)
    bottom = len(rows_text)
    rows_text.append("─" * columns)
    rows_text.extend(extra_below)
    rows_text.extend([FOOTER, HINT])
    return top, bottom, rows_text


def bordered_payload(draft, *, columns=60, **kwargs):
    top, bottom, rows_text = bordered_rows(draft, columns=columns, **kwargs)
    return top, bottom, payload_from_lines(rows_text, columns=columns, cursor_row=bottom - 1)


def labeled_payload(draft, *, columns=60, **kwargs):
    top, bottom, rows_text = bordered_rows(draft, columns=columns, **kwargs)
    label = " ultracode ─"
    rows_text[top] = "─" * (columns - len(label)) + label
    return top, bottom, payload_from_lines(rows_text, columns=columns, cursor_row=bottom - 1)


def watchdog_draft(columns=60, message=w.CLAUDE_MESSAGE):
    """Our exact message as Claude wraps it: first row after ``❯ ``, then the gutter."""
    chunks = wrap_to_cells(message, columns - 4)
    return [chunks[0]] + ["  " + chunk for chunk in chunks[1:]]
class _Fixture:
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.locks = self.root / "mac-deliveries-v1"
        self.locks.mkdir()
        patcher = mock.patch.object(w, "DELIVERY_LOCK_ROOT", self.locks)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.serial = 0

    def case(self, *, pending=False):
        self.serial += 1
        folder = self.root / str(self.serial)
        folder.mkdir()
        return provider_guard.ProviderGuardTests().case(folder, pending=pending)

    def watchdog_case(self, *, pending):
        """The composer holds exactly our wrapped message inside complete borders."""
        d, t, r, e, c = self.case(pending=pending)
        c._submit_echo = False
        c.payload = c._base_payload = bordered_payload(watchdog_draft())[2]
        return d, t, r, e, c

    def lock_paths(self):
        # Public MAC deliveries-v1 naming contract, independent of CCC helpers.
        return (
            self.locks / ("pane-" + hashlib.sha256(b"WORKSPACE-UUID:PANE-UUID").hexdigest() + ".lock"),
            self.locks / ("target-" + hashlib.sha256(b"SURFACE-UUID").hexdigest() + ".lock"),
        )

    def assert_locks_free(self, paths=None):
        for path in paths or self.lock_paths():
            with path.open("a+") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def snapshot(runtime):
        return {key: getattr(runtime, key) for key in BUDGET_FIELDS}

    @staticmethod
    def grid(payload):
        return w.Grid.from_rpc(payload, "surface-uuid")


class ComposerBoxTests(_Fixture, unittest.TestCase):
    """The whole bordered box is the composer; every row inside it is draft."""

    def test_ultracode_empty_frame_and_reply_classification(self):
        for columns in (60, 177):
            for reply, kind in (("unfinished answer", "claude_stopped"),
                                ("完成，建议检查 usage: /context", "claude_completed"),
                                ("API Error: 503 no available accounts", "recoverable_error")):
                with self.subTest(columns=columns, reply=reply):
                    top, bottom, payload = labeled_payload(
                        [""], columns=columns, transcript=("❯ previous user", "⏺ " + reply))
                    grid = self.grid(payload)
                    self.assertEqual(w._claude_composer_box(grid.lines), (top, bottom))
                    self.assertEqual(w._claude_composer_state(grid)[0], "empty")
                    self.assertNotIn("ultracode", w._claude_latest_reply(grid.lines, top + 1))
                    self.assertEqual(w.classify_claude_grid(grid).kind, kind)

    def test_ultracode_drafts_keep_all_send_routes_closed(self):
        for draft in (["actual user draft"], ["", "  hidden continuation"],
                      ["", "  [Opus 5] typed footer"],
                      watchdog_draft() + ["", "  other sender's notice"]):
            with self.subTest(draft=draft):
                top, bottom, payload = labeled_payload(draft)
                grid = self.grid(payload)
                self.assertEqual(w._claude_composer_box(grid.lines), (top, bottom))
                self.assertEqual(w._claude_composer_state(grid)[0], "busy")
                self.assertEqual(w.classify_claude_grid(grid).kind, "composer_busy")
                self.refused_everywhere(payload)

    def test_ultracode_does_not_hide_live_activity_or_reuse_old_activity(self):
        # Measured B mid-tool frame: the label is below the live spinner.
        statuses = (("✻ Spinning… (14m 24s · ↓ 6.3k tokens)", "working"),
                    ("✻ Waiting for API response · will retry in 2m 36s · check your network", "retry"),
                    ("✽ Compacting conversation… (7m 7s · ↓ 5.9k tokens)", "compacting"))
        for status, activity in statuses:
            with self.subTest(status=status):
                top, _, payload = labeled_payload(
                    [""], columns=177,
                    transcript=("❯ previous user", "⏺ Running command · 4s",
                                "     (ctrl+b to run in background)", "", status, ""))
                current = w.classify_claude_grid(self.grid(payload))
                self.assertEqual((current.kind, current.claude_native_activity), ("working", activity))
                d, t, r, e, c = self.case()
                c.payload = c._base_payload = copy.deepcopy(payload)
                self.assertFalse(d._send_claude_event(e, t, r, c)[0])
                self.assertEqual((c.sent_text, c.sent_keys), ([], []))
        for newer, expected in (("⏺ New unfinished answer.", "claude_stopped"),
                                ("API Error: 503 no available accounts", "recoverable_error"),
                                ("✻ Worked for 3m", "claude_stopped"),
                                ("⏺ 完成，建议检查 usage: /context", "claude_completed")):
            with self.subTest(newer=newer):
                payload = labeled_payload([""], columns=177,
                    transcript=("❯ previous user", statuses[0][0], newer))[2]
                current = w.classify_claude_grid(self.grid(payload))
                self.assertEqual(current.kind, expected)
                self.assertIsNone(current.claude_native_activity)

    def test_ultracode_requires_the_exact_complete_equal_width_frame(self):
        top, bottom, payload = labeled_payload([""])
        original = list(self.grid(payload).lines)
        shapes = {}
        for name, upper, lower in (
                ("other_label", original[top].replace("ultracode", "ultrac0de"), original[bottom]),
                ("wrong_lower_width", original[top], original[bottom][:-1]),
                ("labeled_lower", original[top], original[top]),
                ("indented_upper", " " + original[top], original[bottom]),
                ("partial_upper", "─" * 7 + " ultracode ─", "─" * 18)):
            rows = original.copy()
            rows[top], rows[bottom] = upper, lower
            shapes[name] = rows
        shapes["missing_lower"] = original[:bottom]
        shapes["later_prompt"] = original + ["❯ actual user draft"]
        for name, rows in shapes.items():
            with self.subTest(shape=name):
                changed = payload_from_lines(
                    rows, columns=max(60, *(display_width(row) for row in rows)),
                    cursor_row=min(bottom - 1, len(rows) - 1))
                grid = self.grid(changed)
                self.assertIsNone(w._claude_composer_box(grid.lines))
                self.assertNotEqual(w._claude_composer_state(grid)[0], "empty")
                self.refused_everywhere(changed)

    def refused_everywhere(self, payload):
        """No Text and no Enter through orphan, pending, direct Enter or a fresh paste."""
        for route in ("orphan", "pending", "enter", "event"):
            with self.subTest(route=route):
                d, t, r, e, c = self.case(pending=route in {"pending", "enter"})
                c._submit_echo = False
                c.payload = c._base_payload = copy.deepcopy(payload)
                texts = len(c.sent_text)
                if route == "event":
                    sent, reason = d._send_claude_event(e, t, r, c)
                    self.assertFalse(sent)
                    self.assertIn("composer", reason)
                elif route == "enter":
                    self.assertFalse(d._send_claude_enter(t, r, c, reason="boundary",
                                                          input_check=lambda: True))
                else:
                    d._reconcile_claude_submit(t, r, w.classify_claude_grid(self.grid(payload)), c)
                self.assertEqual(len(c.sent_text), texts)
                self.assertEqual(c.sent_keys, [])
                self.assertEqual(r.claude_submit_attempts, 0)
                self.assertEqual(r.claude_orphan_enter_count, 0)

    def test_hello_draft_above_a_pasted_prompt_row_is_never_hidden_by_the_last_prompt(self):
        # The draft's own second row starts with a pasted ``❯`` and our message.
        chunks = wrap_to_cells(w.CLAUDE_MESSAGE, 60 - 6)
        draft = ["hello", "  ❯ " + chunks[0]] + ["  " + chunk for chunk in chunks[1:]]
        top, bottom, payload = bordered_payload(draft)
        grid = self.grid(payload)
        # Shape check: the old "last/nearest ❯" anchor is the pasted row.
        legacy = [row for row, line in enumerate(grid.lines) if w.CLAUDE_PROMPT_RE.match(line.strip())]
        self.assertEqual(max(legacy), top + 2)
        self.assertEqual(w._claude_composer_box(grid.lines), (top, bottom))
        self.assertEqual(w._claude_composer_state(grid)[0], "busy")
        state = w.classify_claude_grid(grid)
        self.assertEqual(state.kind, "composer_busy")
        self.assertFalse(state.watchdog_echo, "hello above the pasted row is still the user's draft")
        self.refused_everywhere(payload)

    def test_footer_rule_and_prompt_like_draft_rows_belong_to_the_composer(self):
        ours = watchdog_draft()
        shapes = {
            "footer_like_after_echo": ours + ["  [Opus 5 is a model name I typed]"],
            "bypass_like_after_echo": ours + ["  bypass permissions on is my question"],
            "rule_like_after_echo": ours + ["  " + "─" * 20],
            "prompt_like_after_echo": ours + ["  ❯ quoted prompt"],
            "blank_then_foreign": ours + ["", "  foreign task notice"],
            "blank_then_footer_like": ours + ["", "  [Sonnet 5] quoted footer"],
            "empty_prompt_then_footer_like": ["", "  [Opus 5] typed below an empty first row"],
            "empty_prompt_then_rule_like": ["", "  " + "─" * 20],
        }
        for name, draft in shapes.items():
            with self.subTest(shape=name):
                top, bottom, payload = bordered_payload(draft)
                grid = self.grid(payload)
                self.assertEqual(w._claude_composer_box(grid.lines), (top, bottom))
                self.assertEqual(w._claude_composer_state(grid)[0], "busy")
                state = w.classify_claude_grid(grid)
                self.assertEqual(state.kind, "composer_busy")
                self.assertFalse(state.watchdog_echo)
                self.refused_everywhere(payload)

    def test_exact_watchdog_box_still_gets_one_enter_and_an_empty_box_one_paste(self):
        top, bottom, payload = bordered_payload(watchdog_draft())
        grid = self.grid(payload)
        self.assertEqual(w._claude_composer_box(grid.lines), (top, bottom))
        state = w.classify_claude_grid(grid)
        self.assertEqual((state.kind, state.watchdog_echo), ("composer_busy", True))
        for route in ("orphan", "pending"):
            with self.subTest(route=route):
                d, t, r, e, c = self.watchdog_case(pending=route == "pending")
                self.assertTrue(d._reconcile_claude_submit(t, r, state, c))
                self.assertEqual([key[-1] for key in c.sent_keys], ["enter"])
        with self.subTest(route="event"):
            d, t, r, e, c = self.case()
            c.payload = c._base_payload = bordered_payload([""])[2]
            empty = w.classify_claude_grid(self.grid(c.payload))
            self.assertEqual(w._claude_composer_state(self.grid(c.payload))[0], "empty")
            self.assertEqual(empty.kind, "claude_stopped")
            sent, reason = d._send_claude_event(e, t, r, c)
            self.assertTrue(sent, reason)
            self.assertEqual((len(c.sent_text), len(c.sent_keys)), (1, 1))

    def test_only_the_bottom_complete_live_box_counts(self):
        quoted = ["older output", "─" * 60, "❯ an old quoted composer", "─" * 60, "newer output"]
        top, bottom, rows = bordered_rows(watchdog_draft(), transcript=quoted)
        self.assertEqual(w._claude_composer_box(rows), (top, bottom))
        self.assertTrue(w.classify_claude_grid(self.grid(payload_from_lines(rows, cursor_row=bottom - 1)))
                        .watchdog_echo)
        top, bottom, rows = bordered_rows(watchdog_draft())
        malformed = {
            "column0_prompt_below_box": rows[:-1] + ["❯ stray prompt row", rows[-1]],
            "no_closing_border": rows[:bottom] + [""] * (len(rows) - bottom),
            "indented_borders": [("  " + "─" * 58) if row in (top, bottom) else text
                                 for row, text in enumerate(rows)],
            "unequal_closing_border": [("─" * 59) if row == bottom else text
                                       for row, text in enumerate(rows)],
        }
        for name, lines in malformed.items():
            with self.subTest(shape=name):
                payload = payload_from_lines(lines, cursor_row=bottom - 1)
                grid = self.grid(payload)
                self.assertIsNone(w._claude_composer_box(grid.lines))
                self.assertNotEqual(w._claude_composer_state(grid)[0], "empty")
                self.assertFalse(w.classify_claude_grid(grid).watchdog_echo)
                self.refused_everywhere(payload)

    def test_draft_rows_above_a_pasted_prompt_are_never_assistant_content(self):
        # The assistant fingerprint is read above the composer's own ❯ row.
        # Anchored on the later pasted ❯, the draft row would become "content".
        prints = set()
        for typed in ("typed line one", "typed line two"):
            _, _, payload = bordered_payload(["", "  " + typed, "  ❯ quoted"])
            prints.add(w.classify_claude_grid(self.grid(payload)).content_fingerprint)
        _, _, empty = bordered_payload([""])
        self.assertEqual(prints, {w.classify_claude_grid(self.grid(empty)).content_fingerprint})
        self.assertIsNotNone(next(iter(prints)))

    def test_enter_pins_the_whole_box_not_the_rows_after_the_last_prompt(self):
        # Only the row above a pasted ``❯`` changes between the two reads; the
        # classifier is forced to "ours" so the pin alone decides.
        tail = ["  ❯ quoted", "  tail"]
        first = bordered_payload(["hello"] + tail)[2]
        second = bordered_payload(["hullo"] + tail)[2]
        forced = w.ScreenState("composer_busy", message_kind="claude", watchdog_echo=True)
        for name, frames, keys in (("changed_above_last_prompt", [first, second], 0),
                                   ("unchanged", [first, copy.deepcopy(first)], 1)):
            with self.subTest(frames=name):
                d, t, r, e, c = self.case(pending=True)
                with mock.patch.object(w, "classify_claude_grid", return_value=forced), \
                        mock.patch.object(c, "replay", side_effect=frames) as replay:
                    self.assertEqual(d._send_claude_enter(t, r, c, reason=name, input_check=lambda: True),
                                     bool(keys))
                self.assertEqual(replay.call_count, 2)
                self.assertEqual(len(c.sent_keys), keys)

class EnterLeaseTests(_Fixture, unittest.TestCase):
    """The final Enter rechecks the lease; contention/replacement spends nothing."""

    def replace_on_unknown_save(self, d, r, index, displaced, holder=None):
        """After the last pre-Enter save, another delivery replaces lock ``index``."""
        raw = d._save_claude_surface

        def save(*args, **kwargs):
            result = raw(*args, **kwargs)
            if r.claude_submit_write_unknown and not displaced:
                path = self.lock_paths()[index]
                old = path.with_suffix(f".displaced-{self.serial}")
                path.rename(old)
                path.touch()
                displaced.append(old)
                if holder is not None:
                    handle = path.open("a+")
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    holder.append(handle)
            return result
        return save

    def test_lock_replaced_after_composer_checks_withholds_enter_and_spends_nothing(self):
        for index in (0, 1):
            for route in ("enter", "orphan", "pending"):
                with self.subTest(lock=index, route=route):
                    d, t, r, e, c = self.watchdog_case(pending=route != "orphan")
                    before = self.snapshot(r)
                    keys, displaced = list(c.sent_keys), []
                    state = w.classify_claude_grid(self.grid(c.payload))
                    with mock.patch.object(d, "_save_claude_surface", side_effect=self.replace_on_unknown_save(
                            d, r, index, displaced)):
                        if route == "enter":
                            self.assertFalse(d._send_claude_enter(t, r, c, reason="race",
                                                                  input_check=lambda: True))
                        else:
                            self.assertTrue(d._reconcile_claude_submit(t, r, state, c))
                    self.assertEqual(len(displaced), 1, "the final window was never reached")
                    self.assertEqual(c.sent_keys, keys)
                    self.assertEqual(self.snapshot(r), before)
                    if route != "pending":
                        self.assertEqual(r.state, "claude_delivery_wait")
                    self.assert_locks_free([*self.lock_paths(), *displaced])

    def held_lease(self):
        d, t, r, e, c = self.watchdog_case(pending=True)
        local = d._claude_delivery_local
        local.depth = 1
        self.addCleanup(setattr, local, "depth", 0)
        self.assertTrue(d._acquire_claude_delivery(t, r, c))
        self.addCleanup(self.release, local)
        return d, local

    @staticmethod
    def release(local):
        stack, local.stack, local.binding, local.pins = local.stack, None, None, ()
        if stack is not None:
            stack.close()

    def test_lease_predicate_requires_same_owner_single_link_and_the_held_inode(self):
        d, local = self.held_lease()
        d._verify_claude_delivery_lease()  # positive control: the real lease passes
        path = self.lock_paths()[1]
        with self.subTest(fault="foreign_owner"):
            with mock.patch.object(w.os, "geteuid", return_value=os.geteuid() + 1):
                with self.assertRaises(w.DeliveryLeaseError):
                    d._verify_claude_delivery_lease()
        with self.subTest(fault="extra_hard_link"):
            extra = path.with_suffix(".extra-link")
            os.link(path, extra)
            with self.assertRaises(w.DeliveryLeaseError):
                d._verify_claude_delivery_lease()
            extra.unlink()
            d._verify_claude_delivery_lease()
        with self.subTest(fault="replaced_inode"):
            moved = path.with_suffix(".moved")
            path.rename(moved)
            path.touch()
            with self.assertRaises(w.DeliveryLeaseError):
                d._verify_claude_delivery_lease()
            path.unlink()
            moved.rename(path)
            d._verify_claude_delivery_lease()
        with self.subTest(fault="not_held"):
            self.release(local)
            with self.assertRaises(w.DeliveryLeaseError):
                d._verify_claude_delivery_lease()

    def test_contention_at_the_connected_enter_guard_spends_nothing_then_enters_once(self):
        for index in (0, 1):
            with self.subTest(lock=index):
                d, t, r, e, c = self.watchdog_case(pending=True)
                displaced, holder = [], []

                def contend(kind):
                    # Another delivery replaced and holds the named lease at write.
                    if kind == "enter" and not displaced:
                        path = self.lock_paths()[index]
                        old = path.with_suffix(f".contended-{self.serial}")
                        path.rename(old)
                        path.touch()
                        displaced.append(old)
                        handle = path.open("a+")
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        holder.append(handle)

                provider_guard.ProviderGuardTests().guard_transport(c, before=contend)
                before = self.snapshot(r)
                self.assertFalse(d._send_claude_enter(t, r, c, reason="contended", input_check=lambda: True))
                self.assertEqual((len(displaced), c.sent_keys), (1, []))
                self.assertEqual(self.snapshot(r), before)
                self.assertEqual(r.state, "claude_delivery_wait")
                # Still held: refused before any composer read, still nothing spent.
                self.assertFalse(d._send_claude_enter(t, r, c, reason="still", input_check=lambda: True))
                self.assertEqual((c.sent_keys, self.snapshot(r)), ([], before))
                holder.pop().close()
                self.assertTrue(d._send_claude_enter(t, r, c, reason="released", input_check=lambda: True))
                self.assertEqual([key[-1] for key in c.sent_keys], ["enter"])
                self.assertEqual(r.claude_submit_attempts, before["claude_submit_attempts"] + 1)
                self.assert_locks_free([*self.lock_paths(), *displaced])

    def test_first_acquisition_contention_spends_nothing_on_any_route(self):
        for index in (0, 1):
            for route in ("event", "enter", "orphan", "pending"):
                with self.subTest(lock=index, route=route):
                    if route == "event":
                        d, t, r, e, c = self.case()
                        c.payload = c._base_payload = bordered_payload([""])[2]
                    else:
                        d, t, r, e, c = self.watchdog_case(pending=route != "orphan")
                    state = w.classify_claude_grid(self.grid(c.payload))
                    before, texts = self.snapshot(r), len(c.sent_text)
                    path = self.lock_paths()[index]
                    with path.open("a+") as held:
                        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        if route == "event":
                            self.assertFalse(d._send_claude_event(e, t, r, c)[0])
                        elif route == "enter":
                            self.assertFalse(d._send_claude_enter(t, r, c, reason="busy",
                                                                  input_check=lambda: True))
                        else:
                            self.assertTrue(d._reconcile_claude_submit(t, r, state, c))
                    self.assertEqual((len(c.sent_text), c.sent_keys), (texts, []))
                    self.assertEqual(self.snapshot(r), before)
                    self.assertEqual(r.state, "claude_delivery_wait")
                    self.assert_locks_free()

    def test_paste_and_enter_share_one_same_owner_lease(self):
        d, t, r, e, c = self.case()
        c.payload = c._base_payload = bordered_payload([""])[2]
        raw_flock, raw_key, locked, pinned = fcntl.flock, c.send_key, [], []

        def flock(handle, flags):
            fd = handle if isinstance(handle, int) else handle.fileno()
            if flags & fcntl.LOCK_EX and os.fstat(fd).st_ino in {
                    path.stat().st_ino for path in self.lock_paths() if path.exists()}:
                locked.append(os.fstat(fd).st_ino)
            return raw_flock(handle, flags)

        def key(*args):
            pinned.append(tuple(pin[2][1] for pin in d._claude_delivery_local.pins))
            return raw_key(*args)

        with mock.patch.object(w.fcntl, "flock", side_effect=flock), \
                mock.patch.object(c, "send_key", side_effect=key):
            sent, reason = d._send_claude_event(e, t, r, c)
        self.assertTrue(sent, reason)
        # One pair of locks for paste+Enter, and Enter ran under exactly that pair.
        self.assertEqual(len(locked), 2)
        self.assertEqual(pinned, [tuple(path.stat().st_ino for path in self.lock_paths())])
        self.assertEqual(sorted(pinned[0]), sorted(locked))
        self.assert_locks_free()


class DeliveryLockRootIsolationTests(unittest.TestCase):
    """The suite never takes, creates or leaves a lock in the operator's root."""

    def test_suite_lock_root_is_private_and_not_the_production_root(self):
        root = package.ISOLATED_DELIVERY_LOCK_ROOT
        self.assertEqual(w.DELIVERY_LOCK_ROOT, root, "a local patch leaked out of its test")
        self.assertNotEqual(root, package.PRODUCTION_DELIVERY_LOCK_ROOT)
        self.assertFalse(root.is_relative_to(package.PRODUCTION_DELIVERY_LOCK_ROOT))
        info = root.lstat()
        self.assertTrue(stat.S_ISDIR(info.st_mode))
        self.assertEqual((stat.S_IMODE(info.st_mode), info.st_uid), (0o700, os.geteuid()))
        # The production default is still the operator's MAC lease root.
        self.assertEqual(package.PRODUCTION_DELIVERY_LOCK_ROOT,
                         Path.home() / ".local" / "state" / "multi-agent-collaboration" / "deliveries-v1")

    def test_an_unpatched_delivery_takes_its_locks_only_in_the_isolated_root(self):
        production = package.PRODUCTION_DELIVERY_LOCK_ROOT
        listing = sorted(os.listdir(production)) if production.is_dir() else None
        with tempfile.TemporaryDirectory() as folder:
            d, t, r, e, c = provider_guard.ProviderGuardTests().case(Path(folder))
            c.payload = c._base_payload = bordered_payload([""])[2]
            sent, reason = d._send_claude_event(e, t, r, c)
        self.assertTrue(sent, reason)
        names = ("pane-" + hashlib.sha256(b"WORKSPACE-UUID:PANE-UUID").hexdigest() + ".lock",
                 "target-" + hashlib.sha256(b"SURFACE-UUID").hexdigest() + ".lock")
        for name in names:
            self.assertTrue((package.ISOLATED_DELIVERY_LOCK_ROOT / name).is_file(), name)
        self.assertEqual(sorted(os.listdir(production)) if production.is_dir() else None, listing)


if __name__ == "__main__":
    unittest.main()
