"""Bounded wire notices for NEW ordinary messages; never rewrite an attempt.

The referenced bytes are local evidence, not a native user record. Only the
short wire notice can earn a native delivery receipt. Reading the body and
accepting its contents remain separate actions. Formal packs and completion
callbacks retain their dedicated, task-bound transports.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from cmux_evidence_io import snapshot

# A conservative transport budget, not a claim about a universal TUI threshold.
# Full stable composer equality and native reception are still required.
MAX_INLINE_BYTES = 700
MAX_BODY_BYTES = 1024 * 1024
FORMAT = "cmux-message-reference-v1"
SINGLE_LINE_FORMAT = "cmux-message-reference-v2"
HELPER_SCHEMA = "cmux-agent-bridge-adapter-v1"


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def body_root(home=None):
    return (Path.home() if home is None else Path(home)) / ".local/state/multi-agent-collaboration/message-bodies-v1"


def require_inline(text):
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_INLINE_BYTES:
        raise ValueError("LONG_MESSAGE_REFERENCE_REQUIRED: zero paste; prepare a new bounded reference, never rewrite an existing attempt")
    if text.endswith(("\r", "\n")):
        raise ValueError("TRAILING_NEWLINE_REFERENCE_REQUIRED: zero paste; preserve exact original bytes in a new reference, never strip an existing attempt")
    if any(char in text for char in "\r\n\t"):
        raise ValueError("MULTILINE_OR_TAB_REFERENCE_REQUIRED: zero paste; Claude folds multiline pastes and expands tabs; preserve original bytes in a reference")


def _ordinary(text):
    first = next((line for line in text.splitlines()
                  if line.strip() and not line.startswith("[CMUX-AGENT]")), "")
    if (re.match(r"^\s*(?:(?:TASK|TASK_PACK|TASK PACK)\s*[:=]|TASK_PACK_V2(?:\s|$))", first, re.I)
            or re.match(r"^\s*(?:DONE|BLOCKED)\|[^\n]*\|REPORT=", first)):
        raise ValueError("TASK_BOUND_TRANSPORT_REQUIRED: ordinary references cannot carry formal packs or callbacks")


def _wire(marker, ref):
    if ref['format'] == SINGLE_LINE_FORMAT:
        return (f"STATUS: MESSAGE_REFERENCE_V2 {marker} "
                f"BODY={json.dumps(ref['path'], ensure_ascii=False)} "
                f"SHA256={ref['sha256']} BYTES={ref['bytes']} "
                "Verify and read the exact UTF-8 body; follow its reply contract within existing authority. "
                "Native receipt proves this notice only; body reading and acceptance are separate.")
    return (f"STATUS: MESSAGE_REFERENCE_V1 {marker}\n"
            f"Read UTF-8 body: {ref['path']}\nSHA256={ref['sha256']} BYTES={ref['bytes']}\n"
            "Verify bytes before reading; handle only within existing authority. "
            "Follow the body's reply contract. Native reception proves this notice only; "
            "body reading and acceptance need separate evidence.")


def plan(text, marker, root=None):
    """Pure planning: no files, no input, and no new marker or nonce."""
    if not isinstance(text, str) or not isinstance(marker, str) or not marker or marker not in text:
        raise ValueError("MESSAGE_REFERENCE_MARKER_REQUIRED")
    raw = text.encode("utf-8")
    # TUI line framing can hide a final CR/LF even below the viewport budget.
    # Change only a new wire representation; never normalize the original body.
    if len(raw) <= MAX_INLINE_BYTES and not any(char in text for char in "\r\n\t"):
        return dict(text=text, reference=None)
    _ordinary(text)
    if len(raw) > MAX_BODY_BYTES:
        raise ValueError("MESSAGE_BODY_BUDGET_EXCEEDED")
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", marker):
        raise ValueError("MESSAGE_REFERENCE_MARKER_NOT_INLINE")
    root = body_root() if root is None else Path(root)
    if not root.is_absolute():
        raise ValueError("MESSAGE_BODY_ROOT_MUST_BE_ABSOLUTE")
    sha = hashlib.sha256(raw).hexdigest()
    path = root / (sha + ".txt")
    ref = dict(format=SINGLE_LINE_FORMAT, path=str(path), sha256=sha, bytes=len(raw))
    wire = _wire(marker, ref)
    require_inline(wire)
    return dict(text=wire, reference=ref)


def wire_reference(text):
    """Parse only our exact wire format; this does not attest to body reading."""
    if text.startswith("STATUS: MESSAGE_REFERENCE_V2 "):
        require_inline(text)
        match = re.match(r"STATUS: MESSAGE_REFERENCE_V2 ([A-Za-z0-9_.:-]{1,160}) BODY=", text)
        if match is None:
            raise ValueError("MESSAGE_REFERENCE_WIRE_CHANGED")
        try:
            path, end = json.JSONDecoder().raw_decode(text[match.end():])
        except (ValueError, TypeError) as exc:
            raise ValueError("MESSAGE_REFERENCE_WIRE_CHANGED") from exc
        rest = text[match.end() + end:]
        fields = re.match(r" SHA256=([0-9a-f]{64}) BYTES=([0-9]{1,7}) ", rest)
        if not isinstance(path, str) or fields is None:
            raise ValueError("MESSAGE_REFERENCE_WIRE_CHANGED")
        ref = dict(format=SINGLE_LINE_FORMAT, path=path, sha256=fields[1], bytes=int(fields[2]))
        if text != _wire(match[1], ref):
            raise ValueError("MESSAGE_REFERENCE_WIRE_CHANGED")
        return ref
    if not text.startswith("STATUS: MESSAGE_REFERENCE_V1 "):
        return None
    # Historical four-line notices remain readable for their original journal.
    # require_inline() refuses *new* pastes of this representation.
    if len(text.encode("utf-8")) > MAX_INLINE_BYTES:
        raise ValueError("MESSAGE_REFERENCE_WIRE_CHANGED")
    match = re.match(r"STATUS: MESSAGE_REFERENCE_V1 ([A-Za-z0-9_.:-]{1,160})\n"
                     r"Read UTF-8 body: ([^\n]+)\nSHA256=([0-9a-f]{64}) BYTES=([0-9]{1,7})\n", text)
    if match is None:
        raise ValueError("MESSAGE_REFERENCE_WIRE_CHANGED")
    marker, path, sha, size = match.groups()
    ref = dict(format=FORMAT, path=path, sha256=sha, bytes=int(size))
    if text != _wire(marker, ref):
        raise ValueError("MESSAGE_REFERENCE_WIRE_CHANGED")
    return ref


def read_body(reference):
    """Read the pinned original bytes; never treat a local read as delivery."""
    pin = verify(reference)
    raw, identity = snapshot(Path(reference['path']), limit=MAX_BODY_BYTES)
    if list(identity) != pin['identity'] or hashlib.sha256(raw).hexdigest() != pin['sha256']:
        raise ValueError("MESSAGE_BODY_CHANGED")
    return raw.decode('utf-8'), pin


def validate_wire_body(text):
    ref = wire_reference(text)
    if ref is None:
        return None
    body, pin = read_body(ref)
    _ordinary(body)
    marker = text.split(' ', 3)[2] if ref['format'] == SINGLE_LINE_FORMAT else text.splitlines()[0].split(' ', 2)[2]
    if marker not in body or text != _wire(marker, ref):
        raise ValueError("MESSAGE_REFERENCE_BODY_CHANGED")
    return pin


def verify(reference, text=None):
    """Read-only verification; return a stable file pin, never a receipt."""
    if not isinstance(reference, dict) or set(reference) != {"format", "path", "sha256", "bytes"}:
        raise ValueError("MESSAGE_BODY_BINDING_REQUIRED")
    path = Path(reference["path"])
    if (reference["format"] not in (FORMAT, SINGLE_LINE_FORMAT) or not path.is_absolute()
            or not re.fullmatch(r"[0-9a-f]{64}", reference["sha256"])
            or path.name != reference["sha256"] + ".txt"
            or type(reference["bytes"]) is not int
            or not 0 < reference["bytes"] <= MAX_BODY_BYTES):
        raise ValueError("MESSAGE_BODY_BINDING_CHANGED")
    raw, identity = snapshot(path, limit=MAX_BODY_BYTES)
    st = path.lstat()
    if (not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_mode & 0o222
            or (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns) != identity
            or len(raw) != reference["bytes"]
            or hashlib.sha256(raw).hexdigest() != reference["sha256"]
            or (text is not None and raw != text.encode("utf-8"))):
        raise ValueError("MESSAGE_BODY_CHANGED: preserve original; no input or delivery claim")
    return dict(path=str(path), identity=list(identity), sha256=reference["sha256"])


def persist(reference, text):
    """Exclusive create; a missing/partial/changed old body is never repaired."""
    if reference is None:
        return None
    if digest(text) != reference["sha256"] or len(text.encode("utf-8")) != reference["bytes"]:
        raise ValueError("MESSAGE_BODY_DOES_NOT_MATCH_PLAN")
    path = Path(reference["path"])
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        return verify(reference, text)
    with os.fdopen(fd, "wb") as handle:
        handle.write(text.encode("utf-8"))
        handle.flush()
        os.fsync(handle.fileno())
        os.fchmod(handle.fileno(), 0o400)
    return verify(reference, text)


def helper_request(identity, mode, message, request_id, *, home=None, reference=True):
    """Keep identity/marker; new multiline/long requests get single-line wire v2."""
    if mode not in ("ask", "send", "broadcast") or not isinstance(message, str) or not message.strip():
        raise ValueError("original helper message is missing")
    if not isinstance(request_id, str):
        raise ValueError("original helper request id is invalid")
    spec = dict(schema=HELPER_SCHEMA, identity=identity, mode=mode, message=message, request_id=request_id)
    marker = "CMUX_HELPER_" + digest(encoded(spec))
    full = "[CMUX-AGENT][delivery:" + marker + "][from:" + identity["caller_surface_uuid"] + "]\n" + message
    if mode in ("ask", "broadcast"):
        full += "\n\nReply with one leading marker: STATUS:, DONE:, or BLOCKED:."
    result = dict(spec, marker=marker, payload=full, payload_sha256=digest(full))
    if reference:
        prepared = plan(full, marker, body_root(home))
        if prepared["reference"]:
            result.update(payload=prepared["text"], payload_sha256=digest(prepared["text"]),
                          body_reference=prepared["reference"], original_payload_sha256=digest(full))
    return result


def original_helper_payload(value):
    return helper_request(value["identity"], value["mode"], value["message"],
                          value["request_id"], reference=False)["payload"]
