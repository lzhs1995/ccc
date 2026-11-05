"""Authenticated transport for the complete original private fixture source."""
from pathlib import Path, PurePosixPath
import argparse
import base64
import hashlib
import io
import json
import os
import stat
import sys
import uuid
import zipfile
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

HERE = Path(__file__).resolve().parent
MANIFEST = "9cf5f987c9444583f09ccd341ca840c5e66641e0f79a087ce8c457db09da6896"
KEY_ENV = "CCC_PRIVATE_FIXTURE_KEY"
SOURCE_AAD = ("ccc-original-private-fixture-v1:" + MANIFEST).encode()
MAX_SOURCE = 32 * 1024**2
MAX_DIAGNOSTICS = 32 * 1024**2
DIAGNOSTICS = (
    "fixture.log", "factory.log", "nightly-admission.log", "gui/cmux.log",
    "RUNNER-FAILURE.json", "FAILURE.json", "RESULT.json", "OWNED-CLEANUP.json",
    "ORIGINAL-NATIVE-TRANSCRIPT.jsonl", "ORIGINAL-GUARD-TIMELINE.json",
    "STANDALONE-CASE.json", "STANDALONE-EVIDENCE.json", "state/status.json",
    "state/herdr-codex-input-ledger.json", "admissions/herdr/verifier.log",
)


DIAGNOSTICS += ("state/INITIAL-IDENTITY-MISMATCH.json", "state/initial-owner.stderr", "state/initial-owner.stdout")

DIAGNOSTICS += ('state/INTERPRETER-PERMISSION-INTENT.json', 'state/INTERPRETER-PERMISSION-RESULT.json')

DIAGNOSTICS += ('state/INTERPRETER-PERMISSION-INTENT.json', 'state/INTERPRETER-PERMISSION-RESULT.json')

DIAGNOSTICS += ('state/INTERPRETER-PERMISSION-INTENT.json', 'state/INTERPRETER-PERMISSION-RESULT.json')
DIAGNOSTICS += ('FAILURE-NATIVE-TRANSCRIPT.jsonl','FAILURE-NATIVE-CAPTURE.json')
DIAGNOSTICS = tuple(dict.fromkeys(DIAGNOSTICS))

DIAGNOSTICS += ('state/INTERPRETER-PERMISSION-INTENT.json', 'state/INTERPRETER-PERMISSION-RESULT.json')
DIAGNOSTICS = tuple(dict.fromkeys(DIAGNOSTICS))

DIAGNOSTICS += ('state/INTERPRETER-PERMISSION-INTENT.json', 'state/INTERPRETER-PERMISSION-RESULT.json')
DIAGNOSTICS = tuple(dict.fromkeys(DIAGNOSTICS))

DIAGNOSTICS += ('state/INTERPRETER-PERMISSION-INTENT.json', 'state/INTERPRETER-PERMISSION-RESULT.json')
DIAGNOSTICS = tuple(dict.fromkeys(DIAGNOSTICS))

DIAGNOSTICS = tuple(dict.fromkeys(DIAGNOSTICS + ('ACTIVE-GOAL-EVIDENCE.json', 'provider/GOAL-BEFORE-NATIVE-DB.json', 'provider/GOAL-AFTER-NATIVE-DB.json', 'ORIGINAL-NATIVE-INTENT.json', 'ORIGINAL-NATIVE-GRANT.json', 'ORIGINAL-NATIVE-RECEIPT.json', 'provider/PROVIDER-RESULT.json', 'provider/ORIGINAL-TRANSPORT-RETIRED.json', 'provider/REQUEST-001.json', 'provider/REQUEST-002.json', 'provider/REQUEST-003.json', 'provider/REQUEST-004.json', 'provider/REQUEST-005.json', 'provider/REQUEST-006.json', 'provider/REQUEST-007.json', 'provider/REQUEST-008.json')))

DIAGNOSTICS = tuple(dict.fromkeys(DIAGNOSTICS + ('state/native-compact-logdb-observation.json', 'SHARED-COMPACTION-EVIDENCE.json', 'AUTHENTICATED-SHARED-DAEMON.json', 'SHARED-DAEMON-START-INTENT.json', 'SHARED-DAEMON-START-REPLY.json', 'SHARED-DAEMON-CLEANUP.json', 'SHARED-DAEMON-STOP-REPLY.json', 'SHARED-CLI-CHILDREN.json')))

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
    need(len(infos) == 559 and sum(i.file_size for i in infos) <= MAX_SOURCE, "source member budget differs")
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
        stream.write("CCC_PRIVATE_SOURCE=" + str(output) + "\n")
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


def seal_diagnostics(root, output):
    temp = runner_root()
    key = key_from_environment()
    root = root.resolve(strict=True)
    need(root.is_relative_to(temp) and root != temp, "foreign diagnostic root")
    # Retain private diagnostics even when the separate public exporter fails.
    need(output.is_absolute() and output == output.resolve(strict=False)
         and output.is_relative_to(temp) and output != temp, "foreign evidence export")
    output.mkdir(mode=0o700, exist_ok=True)
    need(output.is_dir() and not output.is_symlink()
         and output.stat().st_uid == os.geteuid() and not output.stat().st_mode & 0o022,
         "unsafe evidence export directory")
    output = output.resolve(strict=True)
    need(output.is_relative_to(temp) and output != temp, "foreign evidence export")
    rows = []
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
    run = {name: os.environ[name] for name in ("GITHUB_REPOSITORY", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_SHA")}
    aad = json.dumps({"schema": "ccc-private-diagnostics-v1", "run": run}, sort_keys=True).encode()
    encrypted = seal(buffer.getvalue(), key, aad)
    write_new(output / "private-diagnostics.aesgcm", encrypted)
    write_new(output / "PRIVATE-DIAGNOSTICS.json", (json.dumps({"aad_base64": base64.b64encode(aad).decode(),
        "ciphertext_sha256": sha(encrypted), "ciphertext_bytes": len(encrypted), "members": rows,
        "plaintext_exported": False}, indent=2) + "\n").encode())


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
