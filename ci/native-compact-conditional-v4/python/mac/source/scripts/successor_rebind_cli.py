#!/usr/bin/env python3
"""Validate, append, submit or reconcile an authorized successor handshake."""
import argparse
import contextlib
import json
import os
from pathlib import Path
import secrets
import stat
import re

import successor_rebind
from cmux_evidence_io import read_bytes


class OutputReservation:
    """Reserve before transport; publish one complete JSON without replacing a file.

    A crash leaves the reservation for diagnosis, never permission to resend.
    The final hard link is an atomic no-replace operation in the held directory.
    """

    def __init__(self, path):
        self.path = Path(path)
        self.directory = None
        self.reservation = None
        self.reservation_pin = None
        self.reservation_raw = None
        self.temporary = None
        self.temporary_pin = None
        self.keep_reservation = False

    def __enter__(self):
        path = self.path
        if (not path.is_absolute() or path.parent.resolve(strict=True) != path.parent
                or path.name in ("", ".", "..")):
            raise ValueError("OUTPUT_PATH_INVALID: use an absolute path with an existing real parent")
        self.directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        self.parent_pin = self._pin(os.fstat(self.directory))
        try:
            # Check before reservation and again afterwards. Do not truncate even
            # an empty old result, or open a FIFO/device/symlink supplied as output.
            self._require_absent()
            self.reservation = "." + path.name + ".successor-output-reservation"
            fd = os.open(self.reservation, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=self.directory)
            with os.fdopen(fd, "wb") as stream:
                self.reservation_pin = self._pin(os.fstat(stream.fileno()))
                self.reservation_raw = (json.dumps({"output": str(path), "pid": os.getpid(),
                                                   "state": "RESERVED_BEFORE_COMMAND"}) + "\n").encode()
                stream.write(self.reservation_raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.fsync(self.directory)
            self.check()
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    @staticmethod
    def _pin(info):
        return info.st_dev, info.st_ino

    def _require_absent(self):
        try:
            os.stat(self.path.name, dir_fd=self.directory, follow_symlinks=False)
        except FileNotFoundError:
            return
        raise FileExistsError("OUTPUT_ALREADY_EXISTS: preserve the original file")

    def check(self):
        parent = self.path.parent.lstat()
        if (not stat.S_ISDIR(parent.st_mode) or self._pin(parent) != self.parent_pin
                or self.path.parent.resolve(strict=True) != self.path.parent):
            raise ValueError("OUTPUT_PARENT_CHANGED")
        info = os.stat(self.reservation, dir_fd=self.directory, follow_symlinks=False)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or self._pin(info) != self.reservation_pin):
            raise ValueError("OUTPUT_RESERVATION_CHANGED")
        fd = os.open(self.reservation, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=self.directory)
        with os.fdopen(fd, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                    or self._pin(opened) != self.reservation_pin
                    or stream.read(len(self.reservation_raw) + 1) != self.reservation_raw):
                raise ValueError("OUTPUT_RESERVATION_CHANGED")
        self._require_absent()

    def publish(self, raw):
        self.check()
        temporary = ".successor-result-" + secrets.token_hex(12) + ".tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=self.directory)
        # Assign cleanup ownership only after exclusive creation succeeded.
        self.temporary = temporary
        with os.fdopen(fd, "wb") as stream:
            self.temporary_pin = self._pin(os.fstat(stream.fileno()))
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        self.check()
        info = os.stat(self.temporary, dir_fd=self.directory, follow_symlinks=False)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or self._pin(info) != self.temporary_pin):
            raise ValueError("OUTPUT_TEMPORARY_CHANGED")
        os.link(self.temporary, self.path.name, src_dir_fd=self.directory,
                dst_dir_fd=self.directory, follow_symlinks=False)
        os.fsync(self.directory)
        self.keep_reservation = False

    def _remove_owned(self, name, pin):
        if name is None or pin is None:
            return
        try:
            info = os.stat(name, dir_fd=self.directory, follow_symlinks=False)
            if self._pin(info) == pin and stat.S_ISREG(info.st_mode):
                os.unlink(name, dir_fd=self.directory)
        except OSError:
            # Cleanup must not mask the command result or delete a replacement.
            # A leftover reservation is conservative: later invocations refuse it.
            pass

    def __exit__(self, exc_type, exc_value, traceback):
        if self.directory is None:
            return
        try:
            self._remove_owned(self.temporary, self.temporary_pin)
            if not self.keep_reservation:
                self._remove_owned(self.reservation, self.reservation_pin)
            with contextlib.suppress(OSError):
                os.fsync(self.directory)
        finally:
            with contextlib.suppress(OSError):
                os.close(self.directory)
            self.directory = None


def maintenance_options(args, artifact):
    """Reject malformed opt-in arguments before any transport operation."""
    enrolled = getattr(args, 'enroll_maintenance_context', False)
    reply = getattr(args, 'maintenance_reply', None)
    replaced = getattr(args, 'replace_completed_offer', None)
    if not enrolled:
        if reply is not None or replaced is not None:
            raise ValueError('maintenance options require --enroll-maintenance-context')
        return None
    if args.command not in ('submit', 'reconcile'):
        raise ValueError('maintenance enrollment requires submit or original reconcile')
    if (artifact.get('scope') != 'maintenance_handshake'
            or artifact.get('communication_only') is not True
            or artifact.get('allow_task_replay') is not False
            or artifact.get('allow_shared_writes') is not False):
        raise ValueError('maintenance enrollment requires communication-only maintenance authorization')
    if (reply is None or not reply.is_absolute() or reply.parent.resolve(strict=True) != reply.parent
            or not reply.parent.is_dir() or reply.name in ('', '.', '..') or reply.is_symlink()
            or (reply.exists() and not reply.is_file())):
        raise ValueError('--maintenance-reply requires an absolute file in an existing real directory')
    if replaced is not None and not re.fullmatch('[0-9a-f]{64}', replaced):
        raise ValueError('--replace-completed-offer requires the exact old offer SHA256')
    return reply


def enroll_result(result, args, reply):
    """Registration failure is independent of the actual transport outcome."""
    import claude_maintenance_context as context
    status = dict(input_operations=0, native_receipt_changed=False, body_read_confirmed=False)
    try:
        queued = result.get('queued_proof')
        if not queued:
            status.update(state='WAITING_ORIGINAL_QUEUE_PROOF',
                          recovery='Observe the same original attempt; do not submit another input.')
        else:
            path, offer = context.enroll(result['attempt'], queued['offset'], queued['length'],
                args.text_file, reply, replace_completed=getattr(args, 'replace_completed_offer', None))
            status.update(state='ENROLLED', offer=str(path), offer_id=offer['offer_id'],
                          reply_path=offer['reply_path'])
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        status.update(state='ENROLLMENT_FAILED', error=str(exc),
                      recovery='Preserve this transport result and any existing offer. '
                               'If an earlier offer occupies the slot, wait until the original executor '
                               'has read and consumed it, then retry enrollment via original reconcile '
                               'with --replace-completed-offer and the exact old offer SHA256. '
                               'Keep the original attempt, text and marker; do not resend input.')
    return dict(result, maintenance_context=status)


def execute(args):
    value = json.loads(read_bytes(args.artifact))
    reply = maintenance_options(args, value)
    if args.command == "validate":
        return successor_rebind.validate_artifact(value, now=args.now)
    if args.command == "append":
        if args.output is None:
            raise successor_rebind.SuccessorRebindError("SUCCESSOR_REBIND_DENIED: --output required")
        return successor_rebind.append_record(args.output, value, now=args.now)
    if args.now is not None:
        raise ValueError("--now is only a validation/append diagnostic; live transport uses real time")
    if args.text_file is None or not args.marker:
        raise ValueError("--text-file and --marker required for submit/reconcile/recover")
    import cmux_bridge
    import successor_native_delivery as delivery
    text = read_bytes(args.text_file, limit=1024 * 1024).decode("utf-8")
    if reply is not None:
        import claude_maintenance_context as context
        if len(text.encode('utf-8')) > context.MAX_BYTES:
            raise ValueError('maintenance context body exceeds the bounded reader limit')
        try:
            _, journal = delivery._paths(value, args.marker)
            attempt_path = journal / 'attempt.json'
            if attempt_path.exists() or attempt_path.is_symlink():
                attempt, intent, _ = delivery._original(journal, value, text, args.marker)
                binding = attempt['native_binding']
                if intent is None:
                    raise ValueError('original maintenance attempt has no input intent')
            else:
                # No journal, offer or cursor is created by this identity check.
                if reply.exists() or reply.is_symlink():
                    raise ValueError('existing reply has no matching original maintenance attempt')
                binding = delivery.bind_target(cmux_bridge, value, text)
            context.preflight_enrollment(attempt_path, args.text_file, reply, binding,
                replace_completed=getattr(args, 'replace_completed_offer', None))
        except ERRORS as exc:
            exc.details = dict(getattr(exc, 'details', {}), input_operations=0,
                               stage='MAINTENANCE_ENROLLMENT_PRECHECK')
            raise
    operation = {"submit": delivery.submit, "reconcile": delivery.reconcile,
                 "recover": delivery.recover}[args.command]
    result = operation(cmux_bridge, value, text, marker=args.marker, wait_seconds=args.wait_seconds)
    return result if reply is None else enroll_result(result, args, reply)


ERRORS = (OSError, ValueError, RuntimeError, KeyError, TypeError)


def error_result(exc):
    return {"confirmed": False, "state": "ERROR", "error": str(exc),
            "details": getattr(exc, "details", {})}


def encoded(result):
    return (json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("validate", "append", "submit", "reconcile", "recover"))
    parser.add_argument("--artifact", required=True, type=Path)
    parser.add_argument("--output", type=Path,
                        help="append: new create-only receipt; other commands: new JSON result file")
    parser.add_argument("--now", type=float)
    parser.add_argument("--text-file", type=Path)
    parser.add_argument("--marker")
    parser.add_argument('--enroll-maintenance-context', action='store_true',
                        help='opt in to file-context recovery of this original queued Claude notice')
    parser.add_argument('--maintenance-reply', type=Path,
                        help='absolute executor body-read reply path; same path on original reconcile')
    parser.add_argument('--replace-completed-offer',
                        help='exact completed old offer id; unread offers are never replaced')
    parser.add_argument("--wait-seconds", type=float, default=None,
                        help="0–60 seconds; default 30 for submit, 0 for reconcile; no resend")
    args = parser.parse_args(argv)
    command_started = False
    result = None
    try:
        reservation = (OutputReservation(args.output) if args.output is not None
                       and args.command != "append" else contextlib.nullcontext())
        with reservation as output:
            if output is not None:
                # If publication fails after execution, preserve the reservation
                # and the command result. Missing --output never permits replay.
                output.keep_reservation = True
            command_started = True
            try:
                result, code = execute(args), 0
            except ERRORS as exc:
                result, code = error_result(exc), 2
            raw = encoded(result)
            if output is not None:
                output.publish(raw)
    except ERRORS as exc:
        failure = error_result(exc)
        failure["command_started"] = command_started
        if not command_started:
            failure["input_operations"] = 0
        elif result is not None:
            failure["command_result"] = result
            failure["recovery"] = "Observe the original journal only; output failure does not permit replay."
        raw, code = encoded(failure), 2
    # Print only after cleanup, so cleanup cannot emit a second JSON document.
    print(raw.decode("utf-8"), end="")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
