#!/usr/bin/env python3
"""Route cmux-agent messages through the installed bridge's original journal.

This adapter never sends terminal keys, parses screens, or manufactures native
proof. Repeated requests retain their first marker; only the canonical message
journal can reconcile them. Delivery failure is exit 75, including queued input.
"""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import time
from uuid import UUID

# Bind imports to this installed release, never a cwd/PYTHONPATH transport.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import cmux_bridge as bridge
from cmux_message_journal import verified_no_input, verified_receipt
from cmux_evidence_io import snapshot as evidence_snapshot
import cmux_prompt_reference as prompt_reference

IDENTITY_KEYS = ("workspace_uuid", "caller_surface_uuid", "target_surface_uuid", "target_pane_uuid")
SCHEMA = "cmux-agent-bridge-adapter-v1"


class Unconfirmed(RuntimeError):
    def __init__(self, message, *, intent=None, details=None):
        super().__init__(message)
        self.intent = str(intent) if intent is not None else None
        self.details = details or {}


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def uuid(value):
    if not isinstance(value, str) or not value:
        raise Unconfirmed("HELPER_IDENTITY_REQUIRED")
    try:
        return str(UUID(value))
    except ValueError as exc:
        raise Unconfirmed("HELPER_INVALID_UUID") from exc


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".adapter-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(encoded(value) + "\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _snapshot(path):
    raw, identity = evidence_snapshot(path)
    st = path.lstat()
    if (not stat.S_ISREG(st.st_mode) or st.st_nlink != 1
            or (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns) != identity):
        raise Unconfirmed("HELPER_STATE_NOT_REGULAR: " + str(path))
    return raw, (*identity, hashlib.sha256(raw).hexdigest())


def snapshot(path):
    return _snapshot(path)[1]


def read_json(path):
    raw, pin = _snapshot(path)
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise Unconfirmed("HELPER_STATE_NOT_OBJECT: " + str(path))
    return value, pin


@contextlib.contextmanager
def locked(paths):
    with contextlib.ExitStack() as stack:
        pins = []
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
            stack.callback(os.close, fd)
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                raise Unconfirmed("HELPER_LOCK_NOT_REGULAR")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise Unconfirmed("HELPER_IN_PROGRESS") from exc
            st = os.fstat(fd)
            pins.append((path, fd, (st.st_dev, st.st_ino)))

        def check():
            for path, fd, pin in pins:
                st, opened = path.lstat(), os.fstat(fd)
                if (not stat.S_ISREG(st.st_mode) or st.st_nlink != 1
                        or opened.st_nlink != 1 or (st.st_dev, st.st_ino) != pin
                        or (opened.st_dev, opened.st_ino) != pin):
                    raise Unconfirmed("HELPER_LOCK_CHANGED")
        check()
        yield check
        check()


class Adapter:
    def __init__(self, home=None):
        self.home = Path.home() if home is None else Path(home)
        self.root = self.home / ".local/state/multi-agent-collaboration/cmux-agent-adapter-v1"

    def live(self, surface, expected=None):
        proof = bridge.pin_workspace(surface)
        identity = {key: uuid(proof.get(key)) for key in IDENTITY_KEYS}
        if expected is not None and identity != expected:
            raise Unconfirmed("HELPER_IDENTITY_CHANGED")
        rows = [row for row in bridge.list_surfaces()
                if uuid(row.get("surface_id")) == identity["target_surface_uuid"]]
        if len(rows) != 1 or rows[0].get("surface_type") != "terminal":
            raise Unconfirmed("HELPER_TARGET_NOT_UNIQUE_TERMINAL")
        if uuid(rows[0].get("workspace_id")) != identity["workspace_uuid"]:
            raise Unconfirmed("HELPER_WORKSPACE_CHANGED")
        return identity, rows[0]["ref"]

    def legacy(self, ref, target_uuid):
        roots = [self.home / ".local/state/cmux-agent"]
        override = os.environ.get("CMUX_AGENT_STATE_DIR")
        if override:
            extra = Path(override).expanduser()
            if not extra.is_absolute():
                raise Unconfirmed("HELPER_LEGACY_STATE_MUST_BE_ABSOLUTE")
            if extra not in roots:
                roots.append(extra)
        keys = {re.sub(r"[^a-zA-Z0-9_-]", "_", value)
                for value in (ref, target_uuid, target_uuid.upper())}
        locks = []
        for root in roots:
            for key in sorted(keys):
                # Old state has no trustworthy native baseline. Preserve it.
                for suffix in (".pending", ".lock"):
                    path = root / (key + suffix)
                    if path.exists() or path.is_symlink():
                        raise Unconfirmed("ORIGINAL_HELPER_CONTROLLER_REQUIRED: " + str(path))
            locks.append(root / (re.sub(r"[^a-zA-Z0-9_-]", "_", ref) + ".lock.flock"))
        return locks

    def channel(self, identity):
        key = digest(encoded([identity["workspace_uuid"], identity["target_surface_uuid"]]))
        return self.root / "targets" / key

    def request(self, identity, mode, message, request_id, *, reference=True):
        return prompt_reference.helper_request(identity, mode, message, request_id,
                                               home=self.home, reference=reference)

    def read_request(self, path, identity):
        value, pin = read_json(path)
        if (not isinstance(value, dict) or value.get("mode") not in ("ask", "send", "broadcast")
                or not isinstance(value.get("message"), str) or not value["message"].strip()
                or not isinstance(value.get("request_id"), str)):
            raise Unconfirmed("HELPER_REQUEST_CHANGED")
        expected = self.request(identity, value["mode"], value["message"], value["request_id"],
                                reference="body_reference" in value)
        if value != expected or path.name != expected["marker"] + ".json":
            raise Unconfirmed("HELPER_REQUEST_CHANGED")
        if "body_reference" in value:
            prompt_reference.verify(value["body_reference"], prompt_reference.original_helper_payload(value))
        return value, pin

    def refuse_foreign_pending(self, pending, channel, identity, check_locks):
        """Explain an intact other-caller pending; never move or adopt it."""
        pointer, pending_pin = read_json(pending)
        old_identity, marker = pointer.get('identity'), pointer.get('marker', '')
        if (not isinstance(old_identity, dict) or set(old_identity) != set(IDENTITY_KEYS)
                or not re.fullmatch(r'CMUX_HELPER_[0-9a-f]{64}', marker)
                or pointer != dict(marker=marker, identity=old_identity)):
            return
        if (any(uuid(old_identity[k]) != old_identity[k] for k in IDENTITY_KEYS)
                or old_identity['caller_surface_uuid'] == identity['caller_surface_uuid']
                or any(old_identity[k] != identity[k] for k in IDENTITY_KEYS if k != 'caller_surface_uuid')):
            return
        path = channel / 'intents' / (marker + '.json')
        _, intent_pin = self.read_request(path, old_identity)
        check_locks()
        self.live(identity['target_surface_uuid'], identity)
        if snapshot(pending) != pending_pin or snapshot(path) != intent_pin:
            raise Unconfirmed('HELPER_STATE_CHANGED')
        raise Unconfirmed('HELPER_PENDING_OTHER_CALLER: original pending belongs to another supervisor; zero input',
            intent=path, details=dict(input_operations=0, original_marker=marker,
                original_caller_surface_uuid=old_identity['caller_surface_uuid'],
                current_caller_surface_uuid=identity['caller_surface_uuid'],
                original_state_preserved=True,
                recovery='Keep the original pending, intent and controller. For a new explicitly user-authorized '
                         'maintenance contact use successor-rebind-v2 with exact endpoint UUIDs; '
                         'do not reconcile, replay or adopt the other caller\'s attempt.'))

    def deliver(self, surface, mode, message=None, request_id="", reconcile=False,
                expected_identity=None, outer_check=None, intent=None):
        if outer_check is not None:
            outer_check()
        identity, ref = self.live(surface, expected_identity)
        target = identity["target_surface_uuid"]
        channel = self.channel(identity)
        pending = channel / "pending.json"
        locks = [channel / "delivery.lock", *self.legacy(ref, target)]
        with locked(locks) as check_locks:
            self.live(target, identity)
            self.legacy(ref, target)
            prior = pending.exists() or pending.is_symlink()
            if reconcile:
                if intent is None:
                    if not prior:
                        raise Unconfirmed("HELPER_NO_PENDING: specify the absolute original --intent; no delivery inferred")
                    pointer, _ = read_json(pending)
                    marker = pointer.get("marker", "")
                    if (not re.fullmatch(r"CMUX_HELPER_[0-9a-f]{64}", marker)
                            or pointer != dict(marker=marker, identity=identity)):
                        self.refuse_foreign_pending(pending, channel, identity, check_locks)
                        raise Unconfirmed("HELPER_PENDING_CHANGED")
                    path = channel / "intents" / (marker + ".json")
                    self.read_request(path, identity)
                    # 原请求必须出现在调用输入里，供 PostToolUse 独立核收。
                    # 仅 surface 不能定位本次请求，更不能借同窗口旧回执授成功。
                    raise Unconfirmed("HELPER_INTENT_REQUIRED: reconcile requires --intent", intent=path)
                path = Path(intent)
                if (not path.is_absolute() or path.is_symlink()
                        or not re.fullmatch(r"CMUX_HELPER_[0-9a-f]{64}\.json", path.name)
                        or path.parent != channel / "intents"):
                    raise Unconfirmed("HELPER_INTENT_OUTSIDE_ORIGINAL_CHANNEL")
                path = channel / "intents" / path.name
                value, pin = self.read_request(path, identity)
            else:
                if not isinstance(message, str) or not message.strip():
                    raise Unconfirmed("HELPER_MESSAGE_REQUIRED")
                # Locate the original marker before considering a new wire format.
                # A saved v1 payload is never rewritten, including after a crash.
                value = self.request(identity, mode, message, request_id, reference=False)
                path = channel / "intents" / (value["marker"] + ".json")
                if prior:
                    pointer, _ = read_json(pending)
                    if pointer != dict(marker=value["marker"], identity=identity):
                        self.refuse_foreign_pending(pending, channel, identity, check_locks)
                        raise Unconfirmed("HELPER_PENDING: reconcile the original request; no new input")
                existed = path.exists() or path.is_symlink()
                if existed:
                    saved, pin = self.read_request(path, identity)
                    if any(saved[key] != value[key] for key in
                           ("schema", "identity", "mode", "message", "request_id", "marker")):
                        raise Unconfirmed("HELPER_REQUEST_CHANGED")
                    value = saved
                else:
                    if prior:
                        raise Unconfirmed("HELPER_ORIGINAL_INTENT_MISSING")
                    value = self.request(identity, mode, message, request_id)
                    prompt_reference.persist(value.get("body_reference"),
                                              prompt_reference.original_helper_payload(value))
                    write_json(path, value)
                    pin = snapshot(path)
                # Save before calling the sole sender. Never regenerate a marker
                # after an uncertain return, crash, or apparent empty composer.
                if not prior:
                    write_json(pending, dict(marker=value["marker"], identity=identity))
                reconcile = existed or prior
            has_pending = pending.exists() or pending.is_symlink()
            pending_pin = None
            if has_pending:
                pointer, pending_pin = read_json(pending)
                if pointer != dict(marker=value["marker"], identity=identity):
                    raise Unconfirmed("HELPER_PENDING_CHANGED")
            body = value.get("body_reference")
            body_pin = prompt_reference.verify(body) if body else None

            def unchanged():
                if outer_check is not None:
                    outer_check()
                check_locks()
                self.live(target, identity)
                self.legacy(ref, target)
                current_pending = snapshot(pending) if pending.exists() or pending.is_symlink() else None
                if snapshot(path) != pin or current_pending != pending_pin:
                    raise Unconfirmed("HELPER_STATE_CHANGED")
                if body and prompt_reference.verify(body) != body_pin:
                    raise Unconfirmed("HELPER_BODY_CHANGED")

            def settle_no_input():
                # Only the original caller's exact request can reach this point.
                # Keep its intent and journal; release just the adapter pointer,
                # while the canonical journal lock still excludes an input.
                settlement = channel / 'settlements' / (value['marker'] + '.json')

                def commit(proof):
                    if (proof.get('source') != 'revalidated_message_no_input_v1'
                            or proof.get('binding') != dict(identity=identity,
                                marker=value['marker'], payload_sha256=digest(value['payload']))
                            or proof.get('input_operations') != 0 or not proof.get('attempts')):
                        raise Unconfirmed('HELPER_NO_INPUT_PROOF_INVALID')
                    unchanged()
                    write_json(settlement, dict(schema='cmux-agent-no-input-settlement-v1',
                        request=str(path), marker=value['marker'], identity=identity,
                        no_input_validation=proof, confirmed=False, input_operations=0,
                        settled_epoch=time.time()))
                    unchanged()
                    if has_pending:
                        pending.unlink()

                proof = verified_no_input(bridge, target, value['payload'], value['marker'],
                                          commit=commit)
                if proof is not None:
                    raise Unconfirmed('HELPER_NO_INPUT_SETTLED: original attempt sent no input; '
                                      'this is not delivery', intent=path,
                        details=dict(input_operations=0, pending_cleared=has_pending,
                            original_state_preserved=True, settlement=str(settlement),
                            no_input_validation=proof,
                            recovery='Original intent and journal are retained. Repeating this request '
                                     'does not resend it. A separately authorized new request uses '
                                     'its own request id; no native receipt or business acceptance is inferred.'))

            unchanged()
            proof = verified_receipt(bridge, target, value["payload"], value["marker"])
            unchanged()
            result = None
            if proof is None:
                if reconcile:
                    settle_no_input()
                    unchanged()
                try:
                    result = bridge.submit_text(target, value["payload"], marker=value["marker"],
                                                force_compose=False, reconcile_only=reconcile)
                except Exception:
                    unchanged()
                    settle_no_input()
                    raise
                unchanged()
                if not isinstance(result, dict) or result.get("confirmed") is not True:
                    settle_no_input()
                    raise Unconfirmed("HELPER_NATIVE_RECEIPT_REQUIRED: queued/unknown is not delivery")
                proof = verified_receipt(bridge, target, value["payload"], value["marker"])
            if (not isinstance(proof, dict)
                    or proof.get("source") != "revalidated_message_dispatch_v1"
                    or {key: uuid(proof.get("identity", {}).get(key)) for key in IDENTITY_KEYS} != identity
                    or not proof.get("attempt") or not proof.get("receipt")):
                raise Unconfirmed("HELPER_NATIVE_RECEIPT_REQUIRED: no validated original receipt")
            unchanged()
            # The native receipt remains in its canonical journal; this is only
            # adapter bookkeeping, never a second source of delivery authority.
            if has_pending:
                pending.unlink()
            return dict(schema=SCHEMA, confirmed=True, marker=value["marker"],
                        receipt_validation=proof, request=str(path),
                        already_received=result is None, reconciled_read_only=reconcile,
                        confirmation_scope="reference_notice" if body else "inline_payload",
                        body_read_confirmed=False if body else None,
                        body_reference=body,
                        confirmation_source="canonical_message_journal_native_user")

    def broadcast(self, message, request_id=""):
        caller = bridge.whoami()
        caller_pin = dict(workspace_uuid=uuid(caller.get("workspace_id")),
                          caller_surface_uuid=uuid(caller.get("surface_id")))
        if not caller.get("pane_ref"):
            raise Unconfirmed("HELPER_CALLER_PANE_REQUIRED")
        if not isinstance(message, str) or not message.strip():
            raise Unconfirmed("HELPER_MESSAGE_REQUIRED")
        key = digest(encoded([caller_pin, message, request_id]))
        group = self.root / "broadcasts" / (key + ".json")
        with locked([group.with_suffix(".lock")]) as check_group_lock:
            if group.exists():
                specification, group_pin = read_json(group)
                identities = specification.get("identities")
                if (specification.get("schema") != SCHEMA
                        or specification.get("caller") != caller_pin
                        or specification.get("message") != message
                        or specification.get("request_id") != request_id
                        or not isinstance(identities, list) or not identities):
                    raise Unconfirmed("HELPER_BROADCAST_STATE_CHANGED")
            else:
                rows = [r for r in bridge.list_surfaces()
                        if r.get("surface_type") == "terminal"
                        and uuid(r.get("workspace_id")) == caller_pin["workspace_uuid"]
                        and uuid(r.get("surface_id")) != caller_pin["caller_surface_uuid"]]
                identities = sorted((self.live(r["surface_id"])[0] for r in rows),
                                    key=lambda item: item["target_surface_uuid"])
                if not identities:
                    raise Unconfirmed("HELPER_NO_ELIGIBLE_RECIPIENTS")
                specification = dict(schema=SCHEMA, caller=caller_pin, message=message,
                                     request_id=request_id, identities=identities)
                write_json(group, specification)
                group_pin = snapshot(group)

            # Reuse the original target set on retry. Newly appearing surfaces
            # are never silently added; tree order/case do not change identity.
            targets = [item["target_surface_uuid"] for item in identities]
            if (targets != sorted(set(targets))
                    or any({k: uuid(v) for k, v in item.items()} != item
                           or set(item) != set(IDENTITY_KEYS)
                           or any(item[k] != v for k, v in caller_pin.items())
                           for item in identities)):
                raise Unconfirmed("HELPER_BROADCAST_IDENTITY_CHANGED")

            def unchanged_group():
                check_group_lock()
                current = bridge.whoami()
                if (uuid(current.get("workspace_id")) != caller_pin["workspace_uuid"]
                        or uuid(current.get("surface_id")) != caller_pin["caller_surface_uuid"]
                        or current.get("pane_ref") != caller["pane_ref"]):
                    raise Unconfirmed("HELPER_BROADCAST_IDENTITY_CHANGED")
                if snapshot(group) != group_pin:
                    raise Unconfirmed("HELPER_BROADCAST_STATE_CHANGED")

            unchanged_group()
            # Validate all pinned targets before the first input, not halfway
            # through a broadcast. Per-target delivery rechecks the same pins.
            for item in identities:
                self.live(item["target_surface_uuid"], item)
            results = []
            for item in identities:
                results.append(self.deliver(item["target_surface_uuid"], "broadcast", message,
                                            request_id, expected_identity=item,
                                            outer_check=unchanged_group))
                unchanged_group()
        return dict(schema=SCHEMA, confirmed=True, recipients=results,
                    confirmation_source="canonical_message_journal_native_user")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("self", help="Resolve the current native caller, not inherited daemon environment")
    for name in ("ask", "send", "broadcast"):
        command = sub.add_parser(name)
        if name != "broadcast":
            command.add_argument("surface")
        command.add_argument("--request-id", default="", help="Explicit id for an intentionally new request; reuse for retries")
        command.add_argument("--no-force-compose", action="store_true", help="Always enforced")
        command.add_argument("message", nargs="+")
    reconcile = sub.add_parser("reconcile")
    reconcile.add_argument("surface")
    reconcile.add_argument("--intent", help="Absolute original intent returned by the first request; required for confirmation")
    args = parser.parse_args(argv)
    adapter = Adapter()
    try:
        if args.command == "self":
            from cmux_workspace_guard import caller_snapshot
            result, _tree, _env, proof = caller_snapshot()
            result = dict(result, native_caller_verified=proof is not None)
        elif args.command == "reconcile":
            result = adapter.deliver(args.surface, "reconcile", reconcile=True, intent=args.intent)
        elif args.command == "broadcast":
            result = adapter.broadcast(" ".join(args.message), args.request_id)
        else:
            result = adapter.deliver(args.surface, args.command, " ".join(args.message), args.request_id)
    except (Exception, KeyboardInterrupt) as exc:
        print(json.dumps(dict(schema=SCHEMA, confirmed=False, error=str(exc),
                              original_intent=getattr(exc, "intent", None),
                              details=getattr(exc, 'details', {}),
                              recovery=getattr(exc, 'details', {}).get('recovery',
                                  "Preserve the original intent; reconcile read-only. No automatic repaste or key.")),
                         ensure_ascii=False), file=sys.stderr)
        return 75
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
