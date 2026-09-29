#!/usr/bin/env python3
"""Real-PTY stress for ccp-new's builtin block editor.

Three categories, each a full end-to-end round in a *fresh* process on a *real*
pty, driving the real curses editor with real keystrokes:

  discard      menu b -> pick 1 -> Esc -> y      profile must be byte-identical
  save         menu b -> pick 1 -> e ... Enter -> Enter -> y   value must change
  field_cancel menu b -> pick 1 -> e ... Esc -> Enter -> y     value must NOT change

``field_cancel`` is the one that matters most: cancelling a *field* edit must
leave that field alone while the editor itself stays open, so a subsequent save
commits nothing.  Confusing it with a whole-editor discard is exactly the kind of
key-semantics bug a fake screen cannot catch.

Isolation: CCP_PROFILE_DIR points at a fresh temp dir per round, so no real
profile in ~/.claude-profiles is ever opened, read, or written.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import pty
import re
import select
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "ccp_new.py"
TEMPLATE = Path(__file__).resolve().parents[1] / "templates" / "_template.example.json"
PYTHON = sys.executable

# ccp-new hard-verifies GATED_KEYS (ANTHROPIC_AUTH_TOKEN / ANTHROPIC_API_KEY /
# ANTHROPIC_BASE_URL) against the live upstream and refuses to write when that
# fails.  The stress therefore edits a NON-credential, non-gated key: the edit
# stays on the advisory tier, no real endpoint is contacted, and the save path is
# still exercised end to end.  Verified against the real template's 37 keys --
# an earlier draft named a key the template does not have, and the harness
# refused to run rather than inventing one.
TARGET_KEY = "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"


def template_env() -> dict:
    """Read the explicitly selected, credential-free test template.

    Read-only: the template is never written.  If it is unreadable the stress
    refuses to invent one, because a profile with a hand-made key set would not
    exercise the same validation path.
    """

    tpl = json.loads(TEMPLATE.read_text(encoding="utf-8"))
    return dict(tpl["env"])


def _write_json(path: Path, payload: dict) -> Path:
    body = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(body)
    return path


def make_profile(directory: Path, name: str, env: dict) -> Path:
    """Seed a round's profile dir: one profile plus the template it validates against.

    The template is not optional scaffolding.  ``validate_env`` -> ``load_template``
    runs on every save, and a missing ``_template.json`` is a hard ``fail()``:
    the process exits mid-save.  Without this the save round reported
    "save did not modify the profile" -- a true statement about a harness gap,
    which would have read as a product defect.
    """

    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    # A copy, so the real template is only ever read.  The template legitimately
    # ships both credential keys empty -- that is its own hard rule -- so it is
    # copied verbatim.
    _write_json(directory / "_template.json", {"env": dict(env)})
    # The *profile*, by contrast, must carry a credential or validate_env()
    # rejects every save with "ANTHROPIC_AUTH_TOKEN 与 ANTHROPIC_API_KEY 不能双空".
    # That rejection is correct behaviour; a fixture copied straight from the
    # template made it look like the save path was broken.  This is a syntactically
    # valid but entirely fake token, and CCP_SKIP_VERIFY keeps it off the wire.
    profile_env = dict(env)
    profile_env["ANTHROPIC_AUTH_TOKEN"] = "sk-ant-stress-not-a-real-credential"
    profile_env["ANTHROPIC_BASE_URL"] = "https://stress.invalid"
    return _write_json(directory / f"{name}.json", {"env": profile_env})


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Session:
    """One ccp-new process on a real pty."""

    def __init__(self, directory: Path, env_overrides: dict | None = None):
        self.buffer = b""
        # How far read_until() has already consumed.  Without it, a wait for the
        # menu prompt is satisfied instantly by the FIRST menu still sitting in
        # the buffer, so ``q`` gets typed into the still-open editor and the
        # round times out.  Every round failed this way before the cursor.
        self.cursor = 0
        master, slave = pty.openpty()
        self.master = master
        # curses asks the *kernel* for the window size, not $LINES/$COLUMNS.  A
        # pty created without one reports 0x0, so every addnstr() is clipped away
        # and the editor draws nothing -- which looked like a hang.
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
        env = dict(os.environ)
        env["CCP_PROFILE_DIR"] = str(directory)
        env["TERM"] = "xterm-256color"
        env["LINES"] = "40"
        env["COLUMNS"] = "120"
        # Keep the save path offline.  The target key is non-gated, so ccp-new
        # treats verification as advisory; without this it makes a real 30s
        # network probe per round.  This flag cannot weaken the credential path:
        # ccp-new refuses it outright when a GATED key changed.
        env["CCP_SKIP_VERIFY"] = "1"
        # Never let a stray CCP_EDITOR route us into vim: this harness drives the
        # builtin editor, and an external editor would hang waiting for :wq.
        env.pop("CCP_EDITOR", None)
        # These two must be IGNORED by find_editor().  Setting them is part of
        # the test: if they ever route to vim again, every round would time out.
        env["VISUAL"] = "vim"
        env["EDITOR"] = "vim"
        for key, value in (env_overrides or {}).items():
            env[key] = value
        self.process = subprocess.Popen(
            [PYTHON, str(SCRIPT)],
            stdin=slave, stdout=slave, stderr=slave,
            env=env, cwd=str(directory), start_new_session=True,
        )
        os.close(slave)

    def read_until(self, pattern: str, timeout: float = 10.0) -> bool:
        """Pump output until ``pattern`` appears *after* the last match.

        The cursor is essential, not an optimisation.  Searching the whole
        accumulated buffer meant the first menu's ``选择 >`` satisfied the wait
        for the menu we expect to return to *later*, so the harness typed ``q``
        while the curses editor was still open, the editor swallowed it, and the
        process was SIGKILLed at the timeout -- rc=-9 on every round, which looks
        exactly like a product hang.  It was the probe measuring its own past.
        """

        needle = re.compile(pattern)
        deadline = time.time() + timeout
        while True:
            match = needle.search(self.text(), self.cursor)
            if match:
                self.cursor = match.end()
                return True
            if time.time() >= deadline:
                return False
            ready, _, _ = select.select([self.master], [], [], 0.05)
            if ready:
                try:
                    chunk = os.read(self.master, 65536)
                except OSError:
                    chunk = b""
                if not chunk:
                    # EOF: one last look, then give up rather than spin.
                    match = needle.search(self.text(), self.cursor)
                    if match:
                        self.cursor = match.end()
                        return True
                    return False
                self.buffer += chunk

    def text(self) -> str:
        return self.buffer.decode("utf-8", "replace")

    def send(self, data: str, settle: float = 0.12) -> bool:
        """Write keystrokes.  False when the child has already exited.

        A finished process makes the pty return EIO, which is not a failure in
        itself: several rounds legitimately end before the final ``q``.  Raising
        here turned a clean early exit into a harness crash.
        """

        try:
            os.write(self.master, data.encode("utf-8"))
        except OSError:
            return False
        # curses reads one key at a time; without a settle the whole script can
        # arrive inside a single getch() burst and later keys land in the wrong
        # widget.
        time.sleep(settle)
        return True

    def finish(self, timeout: float = 10.0) -> int:
        try:
            rc = self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(self.process.pid), signal.SIGKILL)
            rc = -9
        # Drain whatever is left so failures are diagnosable.
        while True:
            ready, _, _ = select.select([self.master], [], [], 0.05)
            if not ready:
                break
            try:
                chunk = os.read(self.master, 65536)
            except OSError:
                break
            if not chunk:
                break
            self.buffer += chunk
        os.close(self.master)
        return rc


def one_round(category: str, index: int, env: dict) -> dict:
    """Run one round; return a verdict dict."""

    root = Path(tempfile.mkdtemp(prefix=f"ccpstress-{category}-"))
    try:
        directory = root / "profiles"
        profile = make_profile(directory, "stress", env)
        before = sha(profile)
        before_env = json.loads(profile.read_text(encoding="utf-8"))["env"]
        new_value = f"{31000 + index}"

        session = Session(directory)
        problems = []
        if not session.read_until(r"选择 >"):
            problems.append("menu never appeared")
        session.send("b\n")
        if not session.read_until(r"序号（空行返回）"):
            problems.append("profile picker never appeared")
        session.send("1\n")
        # The builtin editor announces itself on stdout before curses takes over.
        if not session.read_until(r"内置编辑器"):
            problems.append("builtin editor never announced")
        time.sleep(0.3)

        if category == "discard":
            session.send("\x1b")          # Esc -> discard the whole edit
            session.send("y")             # confirm discard
        elif category == "save":
            session.send(_select_key(env))
            session.send("e")             # edit the selected field
            session.send(new_value)
            session.send("\n")            # accept the field value
            session.send("\n")            # Enter -> save the block
            session.send("y")             # confirm save
        elif category == "field_cancel":
            session.send(_select_key(env))
            session.send("e")
            session.send("99999")         # type something...
            session.send("\x1b")          # ...then Esc: cancel THIS FIELD only
            session.send("\n")            # editor still open -> try to save
            session.send("y")
        else:
            raise ValueError(category)

        # Back at the menu (or exiting).  Quit cleanly.
        session.read_until(r"选择 >|再见|未改动|逐键比对", timeout=8.0)
        session.send("q\n")
        rc = session.finish()
        output = session.text()

        after = sha(profile)
        after_env = json.loads(profile.read_text(encoding="utf-8"))["env"]
        changed = before != after

        # ``逐键比对无差异`` is printed only by action_block's diff pass, which
        # is reached only when the editor RETURNED a payload.  It is the marker
        # that separates the two "profile unchanged" outcomes: a whole-editor
        # discard never reaches the diff, a cancelled *field* does.  Without it
        # field_cancel would pass merely because nothing hit the disk -- which is
        # also true when Esc wrongly tears down the whole editor, i.e. the exact
        # bug this category exists to catch.
        diffed = "逐键比对无差异" in output

        if category == "discard":
            if changed:
                problems.append("discard modified the profile")
            if diffed:
                problems.append("Esc reached the diff pass instead of discarding")
        elif category == "save":
            if not changed:
                problems.append("save did not modify the profile")
            elif after_env.get(TARGET_KEY) != new_value:
                problems.append(
                    f"save wrote {after_env.get(TARGET_KEY)!r} not {new_value!r}")
            if set(after_env) != set(before_env):
                problems.append("save altered the key set")
        elif category == "field_cancel":
            if changed:
                problems.append("cancelled field edit still reached disk")
            if after_env.get(TARGET_KEY) != before_env.get(TARGET_KEY):
                problems.append("cancelled field edit changed the value")
            if not diffed:
                problems.append(
                    "field Esc tore down the whole editor (no diff pass reached)")

        if "Traceback" in output:
            problems.append("traceback in output")
        if rc not in (0, 1, 130):
            problems.append(f"unexpected rc={rc}")
        residue = sorted(p.name for p in directory.glob(".edit-*"))
        if residue:
            problems.append(f"plaintext temp residue: {residue}")

        return {
            "category": category, "index": index, "rc": rc,
            "changed": changed, "ok": not problems, "problems": problems,
            "value": after_env.get(TARGET_KEY),
            "tail": output[-400:] if problems else "",
        }
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _select_key(env: dict) -> str:
    """Keystrokes to move the selection onto TARGET_KEY.

    The editor lists ``sorted(env)`` starting at index 0, so the number of ``j``
    presses is the target's position in that sorted order.  Computing it instead
    of hardcoding keeps this correct if the template gains keys.
    """

    keys = sorted(env)
    return "j" * keys.index(TARGET_KEY)


def main() -> int:
    global SCRIPT, TEMPLATE
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=100)
    parser.add_argument("--categories", default="discard,save,field_cancel")
    parser.add_argument("--json", default="")
    parser.add_argument("--script", type=Path, default=SCRIPT)
    parser.add_argument("--template", type=Path, default=TEMPLATE)
    args = parser.parse_args()
    SCRIPT, TEMPLATE = args.script.resolve(strict=True), args.template.resolve(strict=True)

    env = template_env()
    if TARGET_KEY not in env:
        print(f"template has no {TARGET_KEY}; refusing to invent a key set")
        return 2

    results = []
    for category in args.categories.split(","):
        category = category.strip()
        if not category:
            continue
        failures = 0
        started = time.time()
        for index in range(args.rounds):
            verdict = one_round(category, index, env)
            results.append(verdict)
            if not verdict["ok"]:
                failures += 1
                print(f"  FAIL {category} #{index}: {verdict['problems']}")
                if failures <= 2:
                    print(f"    tail: {verdict['tail']!r}")
            if (index + 1) % 25 == 0:
                print(f"  {category}: {index + 1}/{args.rounds} "
                      f"({failures} failed, {time.time() - started:.0f}s)")
        passed = sum(1 for r in results if r["category"] == category and r["ok"])
        total = sum(1 for r in results if r["category"] == category)
        print(f"{category}: {passed}/{total} passed in {time.time() - started:.0f}s")

    print("=" * 70)
    for category in args.categories.split(","):
        category = category.strip()
        if not category:
            continue
        rows = [r for r in results if r["category"] == category]
        print(f"{category:<14} {sum(1 for r in rows if r['ok'])}/{len(rows)}")
    print("profile writes isolated with CCP_PROFILE_DIR in per-round temporary directories")
    if args.json:
        Path(args.json).write_text(json.dumps(results, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
