"""Authenticated transport for the complete original private fixture source."""
from pathlib import Path, PurePosixPath
import argparse
import base64
import hashlib
import io
import json
import os
import re
import stat
import sys
import uuid
import zipfile
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

HERE = Path(__file__).resolve().parent
MANIFEST = "ec883555962ccbbb36a58d7315a78e98504b59d0479f98af2e90083f43c9c837"
KEY_ENV = "CCC_PRIVATE_FIXTURE_KEY"
SOURCE_AAD = base64.b64decode('eyJtZW1iZXJfY291bnQiOjQ2NiwibmF0aXZlX2F0dGVtcHQiOjEsIm5hdGl2ZV9oZWFkIjoiOTk2MDI1ZTlhMjUyN2VkOWExZjAzMDQ5ZTYwYzQwYzJhYjk0YzE0NSIsIm5hdGl2ZV9ydW4iOjQyMDM1MzQzMjEzLCJzY2hlbWEiOiJjY2MtcHJpdmF0ZS1zaGFyZWQtZml4dHVyZS1hZXMyNTZnY20tdjEiLCJzb3VyY2VfbWFuaWZlc3Rfc2hhMjU2IjoiZWM4ODM1NTU5NjJjY2JiYjM2YTU4ZDczMTVhNzhlOTg1MDRiNTlkMDQ3OWY5OGFmMmU5MDA4M2Y0M2M5YzgzNyJ9')
MAX_SOURCE = 32 * 1024**2
MAX_DIAGNOSTICS = 32 * 1024**2
DIAGNOSTICS = ('fixture.log', 'nightly-admission.log', 'gui/cmux.log', 'RUNNER-FAILURE.json', 'EXECUTE-FAILURE.json', 'RESULT.json', 'case/watcher.log', 'case/RESULT.json', 'case/GOAL-ORIGINAL-JOURNAL.json', 'case/GOAL-STARTED-READBACK.json', 'case/CROSS-GENERATION-READ-OBSERVATIONS.json', 'case/CROSS-GENERATION-READBACK.json', 'case/NATIVE-RPC-CALLS.json', 'case/TRANSPORT-CALLS.json', 'case/FIXTURE-PROCESS-CLEANUP.json', 'case/SHARED-DAEMON-CLEANUP.json', 'case/restart/SHARED-DAEMON-CLEANUP.json')

DIAGNOSTICS += ('case/BEFORE-NATIVE-TRANSCRIPT.json', 'case/AFTER-NATIVE-TRANSCRIPT.json', 'case/NATIVE-RAW-PINS.json', 'case/INITIAL-NATIVE-FAILURE.json', 'case/DELIVERY-LEDGER.json', 'case/PROVIDER-LEDGER.json', 'case/ORIGINAL-RUNTIME-OBSERVATIONS.json', 'case/GOAL-NATIVE-DATABASE.json', 'case/GOAL-BEFORE-NATIVE-DATABASE.json', 'case/terminal-owner/LAUNCH.json', 'case/terminal-owner/CLIENT-0.json', 'case/terminal-owner/CLIENT-1.json')
DIAGNOSTICS += ('case/PROVIDER-REQUESTS.json', 'case/RESERVED-PROVIDER-LEDGER.json',
    'case/RESERVED-ORIGINAL-ATTEMPT.json', 'case/AUTHENTICATED-SHARED-DAEMON.json',
    'case/restart/AUTHENTICATED-SHARED-DAEMON.json', 'case/WORKSPACE-CREATE-INTENT.json')
NATIVE_PROOF_FILES = ('case/NATIVE-RAW-PINS.json', 'case/INITIAL-NATIVE-FAILURE.json',
    'case/terminal-owner/LAUNCH.json', 'case/terminal-owner/CLIENT-0.json',
    'case/WORKSPACE-CREATE-INTENT.json')

DIAGNOSTICS += ('case/WORKSPACE-CLEANUP-OBSERVATIONS.json', 'case/PROVIDER-ERRORS.json', 'case/SHARED-DAEMON-STOP-REPLY.json', 'case/FINAL-DAEMON-STATE.json')

def need(condition, reason):
    if not condition:
        raise ValueError(reason)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def write_new(path, data, mode=0o600):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def key_from_environment():
    value = os.environ.pop(KEY_ENV, "")
    key = base64.b64decode(value, validate=True)
    need(len(key) == 32, "private fixture key unavailable")
    return key


def seal(data, key, aad):
    nonce = os.urandom(12)
    return nonce + AESGCM(key).encrypt(nonce, data, aad)


def unseal(data, key, aad):
    need(28 <= len(data) <= MAX_SOURCE + 1024**2, "encrypted payload size invalid")
    return AESGCM(key).decrypt(data[:12], data[12:], aad)


def checked_archive(raw, manifest_sha=MANIFEST):
    need(len(raw) <= MAX_SOURCE, "source archive exceeds bound")
    archive = zipfile.ZipFile(io.BytesIO(raw))
    infos = archive.infolist()
    need(len(infos) == 466 and sum(i.file_size for i in infos) <= MAX_SOURCE, "source member budget differs")
    seen = set()
    for item in infos:
        path = PurePosixPath(item.filename)
        need(bool(path.parts) and not path.is_absolute() and ".." not in path.parts
             and "\\" not in item.filename and path.as_posix() == item.filename
             and item.filename not in seen and not item.is_dir(), "unsafe source member")
        mode = item.external_attr >> 16
        need(stat.S_IFMT(mode) in (0, stat.S_IFREG) and not item.flag_bits & 1, "nonregular source member")
        seen.add(item.filename)
    manifest_bytes = archive.read("PUBLIC-SOURCE-MANIFEST.json")
    need(sha(manifest_bytes) == manifest_sha, "original full manifest differs")
    manifest = json.loads(manifest_bytes)
    need(set(manifest["files"]) | {"PUBLIC-SOURCE-MANIFEST.json"} == seen, "full source closure differs")
    for name, row in manifest["files"].items():
        data = archive.read(name)
        expected = row["sha256"] if isinstance(row, dict) else row
        need(sha(data) == expected, "original source bytes differ")
    return archive


def extract(raw, output, manifest_sha=MANIFEST):
    archive = checked_archive(raw, manifest_sha)
    need(not output.exists(), "source output already exists")
    output.mkdir(mode=0o700)
    with archive:
        for member in archive.infolist():
            path = output / member.filename
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            # Preserve the exact original source mode recorded by the zip.
            mode = stat.S_IMODE(member.external_attr >> 16)
            need(mode in (0o400, 0o444, 0o500, 0o555, 0o600, 0o644, 0o700, 0o755), "unexpected original mode")
            write_new(path, archive.read(member), mode)


def runner_root():
    need(sys.platform == "darwin" and os.environ.get("GITHUB_ACTIONS") == "true"
         and os.environ.get("GITHUB_REPOSITORY") == "lzhs1995/ccc", "isolated macOS runner required")
    return Path(os.environ["RUNNER_TEMP"]).resolve(strict=True)


def decrypt_source():
    temp = runner_root()
    key = key_from_environment()
    contract = json.loads((HERE / "TRANSPORT.json").read_bytes())
    need(contract["manifest_sha256"] == MANIFEST, "unexpected source contract")
    encrypted = (HERE / "source.aesgcm").read_bytes()
    need(len(encrypted) == contract["ciphertext_bytes"] and sha(encrypted) == contract["ciphertext_sha256"],
         "ciphertext differs")
    raw = unseal(encrypted, key, SOURCE_AAD)
    need(sha(raw) == contract["archive_sha256"], "source archive differs")
    output = temp / ("ccc-private-source-" + uuid.uuid4().hex[:10])
    extract(raw, output)
    with Path(os.environ["GITHUB_ENV"]).open("a") as stream:
        stream.write("SHARED_FIXTURE_BUNDLE=" + str(output) + "\n")
    print("Authenticated complete original source in private runner directory.")


def read_diagnostic(root, relative, budget):
    path = root / relative
    need(path == path.resolve(strict=True) and path.is_relative_to(root), "linked diagnostic path")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        need(stat.S_ISREG(before.st_mode) and before.st_uid == os.geteuid()
             and before.st_nlink == 1 and not before.st_mode & 0o022, "unsafe diagnostic input")
        if before.st_size > budget:
            return None
        data = stream.read(budget + 1)
        after = os.fstat(stream.fileno())
    signature = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    need(len(data) <= budget and len(data) == before.st_size
         and signature(before) == signature(after) == signature(path.lstat())
         and path == path.resolve(strict=True), "diagnostic changed during bounded read")
    return data


def native_rollout_diagnostic(root, captured, budget):
    """Select one original fresh SID from sealed proof; no recursive walk.

    These are the fixture's recorded assertions after cleanup, not a fresh
    process authentication. Exact original bytes remain encrypted so the
    reviewer can revalidate public offsets against the raw native record.
    """
    missing = [name for name in NATIVE_PROOF_FILES if name not in captured]
    if missing:
        return {"name": "authenticated_native_rollout", "status": "omitted_missing_provenance"}, None
    proof = {name: json.loads(captured[name]) for name in NATIVE_PROOF_FILES}
    failure = proof['case/INITIAL-NATIVE-FAILURE.json']
    sid = failure['session_id']
    need(isinstance(sid, str) and str(uuid.UUID(sid)) == sid, "noncanonical original SID")
    pins = proof['case/NATIVE-RAW-PINS.json']
    need(pins.get('source_generated_by_actual_native') is True and isinstance(pins['files'], dict),
         "native raw provenance missing")
    launch = proof['case/terminal-owner/LAUNCH.json']
    client = proof['case/terminal-owner/CLIENT-0.json']
    create = proof['case/WORKSPACE-CREATE-INTENT.json']
    argv = launch['argv']
    need(isinstance(argv, list) and argv and all(isinstance(item, str) for item in argv)
         and client['argv'] == argv and type(client['pid']) is int
         and isinstance(client['birth'], list) and len(client['birth']) == 2
         and all(type(value) is int for value in client['birth'])
         and failure['pid'] == client['pid'] and failure['birth'] == client['birth'],
         "original fixture client differs")
    prompt = argv[-1]
    need(re.fullmatch(r'CCC_SHARED_BEGIN_[0-9a-f]{32}: Execute the supplied local fixture goal or read-only command only\.', prompt),
         "not a fresh synthetic launch")
    need(isinstance(create['argv'], list) and all(isinstance(item, str) for item in create['argv'])
         and str(root / 'case/terminal-owner') in ' '.join(create['argv']), "foreign creation intent")
    matches = [name for name in pins['files'] if isinstance(name, str)
               and name.startswith('sessions/') and name.endswith('-' + sid + '.jsonl')]
    need(len(matches) == 1, "original SID raw file missing or ambiguous")
    name = matches[0]
    path = PurePosixPath(name)
    need(not path.is_absolute() and '..' not in path.parts and '\\' not in name
         and path.as_posix() == name, "unsafe native raw path")
    pin = pins['files'][name]
    need(isinstance(pin, dict) and type(pin.get('bytes')) is int and pin['bytes'] >= 0
         and isinstance(pin.get('sha256'), str) and re.fullmatch(r'[0-9a-f]{64}', pin['sha256']),
         "invalid original native pin")
    relative = 'case/NATIVE-RAW/' + name
    if pin['bytes'] > budget:
        return {"name": relative, "status": "omitted_size_bound", "expected_bytes": pin['bytes']}, None
    data = read_diagnostic(root, relative, budget)
    need(data is not None and len(data) == pin['bytes'] and sha(data) == pin['sha256'],
         "raw native bytes differ from original pin")
    rows = data.splitlines()
    need(bool(rows), "native raw record empty")
    first = json.loads(rows[0])
    need(first.get('type') == 'session_meta' and first.get('payload', {}).get('id') == sid,
         "native raw header has another SID")
    # Reuse the exact original fixture's semantic user-record rule without
    # importing/executing decrypted source inside the process holding the key.
    modern, legacy = set(), 0
    for line in rows:
        record = json.loads(line)
        payload, metadata = record.get('payload', {}), record.get('metadata', {})
        if record.get('type') == 'event_msg' and payload.get('type') == 'user_message' and payload.get('message') == prompt:
            legacy += 1
        if record.get('type') != 'response_item' or payload.get('type') != 'message' or payload.get('role') != 'user':
            continue
        internal = payload.get('internal_chat_message_metadata_passthrough', {})
        retained = metadata.get('retained_source', {})
        identity = retained.get('id', {})
        if (payload.get('content') == [{'type': 'input_text', 'text': prompt}]
                and metadata.get('client_authored') is False
                and type(metadata.get('user_input_order')) is int and metadata['user_input_order'] >= 0
                and retained.get('complete') is True and identity.get('role') == 'user'
                and identity.get('message_id') == payload.get('id') and payload.get('id')
                and internal.get('turn_id') and internal.get('turn_id') == identity.get('turn_id')
                and internal.get('content_item_kinds') == ['user.text']
                and internal.get('content_item_metadata') == [{'provenance': {'type': 'user'}}]):
            modern.add((payload['id'], internal['turn_id']))
    need(len(modern) == 1 if modern else legacy == 1, "no unique original synthetic user record")
    return {"name": relative, "status": "sealed", "sha256": sha(data), "bytes": len(data),
            "original_pin_verified": True, "synthetic_provenance_verified": True}, data


def diagnostic_output(temp, output):
    """Permit diagnostics even if the plaintext exporter failed before mkdir."""
    output = Path(output)
    need(output.is_absolute() and output != temp and output.is_relative_to(temp)
         and output.parent.resolve(strict=True) == output.parent,
         "foreign or linked evidence export")
    try:
        output.mkdir(mode=0o700)
    except FileExistsError:
        pass
    info = output.lstat()
    need(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid()
         and stat.S_IMODE(info.st_mode) == 0o700 and output.resolve(strict=True) == output,
         "unsafe evidence export directory")
    return output


def seal_diagnostics(root, output):
    temp = runner_root()
    key = key_from_environment()
    root = root.resolve(strict=True)
    need(root.is_relative_to(temp) and root != temp, "foreign diagnostic root")
    output = diagnostic_output(temp, output)
    rows = []
    captured = {}
    used = 0
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for relative in DIAGNOSTICS:
            try:
                data = read_diagnostic(root, relative, MAX_DIAGNOSTICS - used)
            except FileNotFoundError:
                rows.append({"name": relative, "status": "missing"})
                continue
            except (OSError, ValueError):
                rows.append({"name": relative, "status": "refused_unstable_or_unsafe"})
                continue
            if data is None:
                rows.append({"name": relative, "status": "omitted_size_bound"})
                continue
            archive.writestr(relative, data)
            used += len(data)
            rows.append({"name": relative, "status": "sealed", "sha256": sha(data), "bytes": len(data)})
            if relative in NATIVE_PROOF_FILES:
                captured[relative] = data
        try:
            row, data = native_rollout_diagnostic(root, captured, MAX_DIAGNOSTICS - used)
        except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError):
            row, data = {"name": "authenticated_native_rollout", "status": "refused_unstable_unsafe_or_unproven"}, None
        if data is not None:
            archive.writestr(row['name'], data)
            used += len(data)
        rows.append(row)
    run = {name: os.environ[name] for name in ("GITHUB_REPOSITORY", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_SHA")}
    aad = json.dumps({"schema": "ccc-private-diagnostics-v1", "run": run}, sort_keys=True).encode()
    encrypted = seal(buffer.getvalue(), key, aad)
    write_new(output / "private-diagnostics.aesgcm", encrypted)
    write_new(output / "PRIVATE-DIAGNOSTICS.json", (json.dumps({"aad_base64": base64.b64encode(aad).decode(),
        "ciphertext_sha256": sha(encrypted), "ciphertext_bytes": len(encrypted), "members": rows,
        "plaintext_exported": False, "plaintext_bytes_sealed": used,
        "plaintext_total_limit": MAX_DIAGNOSTICS}, indent=2) + "\n").encode())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("decrypt", "seal-diagnostics"))
    parser.add_argument("--private-root", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.action == "decrypt":
        decrypt_source()
    else:
        seal_diagnostics(args.private_root, args.output)


if __name__ == "__main__":
    main()
